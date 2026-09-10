"""LLM 抽象层：prompt 加载/渲染 + 后端封装。所有 LLM 调用必须经过这里，禁止在业务代码里直接 import openai。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from string import Template
from typing import TypeVar

import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"

# 围栏不限定顶头顶尾、语言标记不限大小写：实测模型会写 ```JSON、也会在围栏前后
# 加一句说明。原来的 ^...$ 锚定在这两种情况下整段原样返回，白烧一次重试。
_FENCE_RE = re.compile(r"```(?:[A-Za-z]+)?[ \t]*\n?(.*?)\n?```", re.DOTALL)


def load_prompt(name: str, version: str = "v1") -> dict:
    """读 prompts/{version}/{name}.yaml，返回 {system, notes} 字典。"""
    path = PROMPTS_ROOT / version / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_PLACEHOLDER_RE = re.compile(r"(?<!\$)\$\{?([A-Za-z_]\w*)\}?")


def render(template_str: str, **kwargs) -> str:
    """用 string.Template 渲染，**缺变量直接报错**。

    原来用 safe_substitute 是为"可选段落缺变量时不抛异常"留的口子，但审查时数了一遍：
    10 个 yaml 的占位符集合与 10 处 render() 的 kwargs 逐一吻合，没有任何调用方在用
    这个口子。留着它的代价是：将来 yaml 加一个 $var 而调用方漏传，占位符会原样留在
    prompt 里（模型看到一个字面的 "$refs"），全部测试照样通过——静默 bug。
    仍用 safe_substitute 做替换本身（模板里若有 $$ 之类不影响），但先检查缺失。
    """
    missing = sorted(set(_PLACEHOLDER_RE.findall(template_str)) - set(kwargs))
    if missing:
        raise KeyError(f"prompt 模板缺变量：{missing}（调用方传了 {sorted(kwargs)}）")
    return Template(template_str).safe_substitute(**kwargs)


def strip_code_fence(text: str) -> str:
    """剥离 LLM 返回里可能带的 markdown 围栏（```json ... ``` 或 ``` ... ```）。
    做成模块级函数是为了能单独单元测试，不依赖网络。"""
    stripped = text.strip()
    m = _FENCE_RE.search(stripped)
    if m:
        return m.group(1).strip()
    return stripped


class LLMError(RuntimeError):
    """LLM 调用在重试耗尽后仍失败时抛出，携带足够定位问题的上下文。"""


class LLMTruncatedError(LLMError):
    """输出疑似在 max_tokens 上限处被截断，不是普通的格式错误。

    LLMError 的子类——原来广义捕获 LLMError 的调用方不用改；需要单独处理
    "截断"这一种失败（比如跳过这条输入而不是让整批崩掉）的调用方可以单独
    catch 这个子类。是不是截断由 core.llm 的 generate() 在 JSON 解析失败时判断，
    不是各调用方各猜一遍。
    """


_JSON_EOF_RE = re.compile(r"EOF while parsing.*line (\d+) column (\d+)")


def _looks_like_truncated_json(error: Exception, text: str) -> bool:
    """区分"输出被截断"和"随便一种 JSON 语法错误"。

    EOF 类错误（pydantic 报 "EOF while parsing ... at line L column C"）只会在
    解析器真的走到输入末尾、结构还没闭合时出现——按定义就发生在文本末尾，
    不需要额外猜"离末尾多近"；这里仍然核对一遍 (L, C) 落在 text 的最后一行、
    且离行尾很近，是防御性的双重确认，不是主判据。
    普通语法错误（缺逗号、多引号）报在文本中间，那种值得重试——模型只是
    格式没对，回灌错误信息有机会修正；截断类错误重试没有意义，同样的输入
    会在同一处再次被截断，三次重试只是白烧三次调用。
    不看 max_tokens 数字本身：token 数和字符数的换算在中文文本上不可靠，
    "解析失败的位置是不是文本末尾"是更直接、不需要猜换算比例的信号。
    """
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return False
    lines = text.split("\n")
    for e in error.errors():
        if e.get("type") != "json_invalid":
            continue
        msg = e.get("ctx", {}).get("error", "")
        m = _JSON_EOF_RE.search(msg)
        if not m:
            continue
        line_no, col = int(m.group(1)), int(m.group(2))
        if line_no != len(lines):
            continue  # 报错行不是最后一行，不是"读到末尾断了"这种情况
        if len(lines[line_no - 1]) - col <= 5:  # 留几个字符余量
            return True
    return False


class LLMBackend(ABC):
    """后端基类。**重试/校验/错误回灌只在这里实现一份**，子类只实现 `_complete`
    这个"单次原始调用"。

    为什么把重试提到基类：切后端时如果每个后端各写一套重试，两边的失败率、
    调用次数就不可比了——同一条主诉在 DeepSeek 上算 1 次调用、在另一个后端上
    因为多重试一次算 2 次，manifest 里的 llm_calls 就失去意义。
    """

    MAX_ATTEMPTS = 3  # 首次 + 最多 2 次重试
    # 传输类错误（超时、429、连接断）两次重试之间的等待秒数，按重试序号取。
    # 只对传输错误退避：校验错误是模型输出格式不对，回灌错误信息立刻重问才有
    # 意义，等一秒不会让它答得更对。之前是零间隔立刻重试，429 会变成三个
    # 连续的 429。测试里把它 monkeypatch 成 (0, 0)，不真等。
    RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)
    _sleep = staticmethod(time.sleep)  # 留个缝给测试换掉，不真睡

    @abstractmethod
    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None, **kwargs,
    ) -> str:
        """单次原始调用：给定 [{"role", "content"}] 返回模型原始文本。
        不做 schema 校验、不重试——那些由 generate() 统一负责。

        max_tokens 是显式参数不是塞进 **kwargs：OpenAICompatBackend 原来
        自己读环境变量算这个值，如果调用方也通过 kwargs 传一份同名参数，
        会在传给 SDK 时撞上"重复关键字参数"。None 表示"用这个后端自己的
        默认值"（不是"不设上限"——CLI 后端本来就没有这个旋钮）。"""
        raise NotImplementedError

    @abstractmethod
    def model_name(self) -> str:
        """实际使用的模型名，写进 manifest。

        **禁止伪装成别的模型。** manifest 是报告里"这个数字是怎么来的"的唯一
        凭据，写错等于伪造实验条件——用 claude_cli 跑出来的分数标成 deepseek-chat，
        会让这个数被误放进跟 DeepSeek 的对比表里。
        """
        raise NotImplementedError

    @abstractmethod
    def backend_id(self) -> str:
        """api / claude_cli / local，写进 manifest。"""
        raise NotImplementedError

    def comparability_warning(self) -> str | None:
        """非默认后端跑出来的数字不能和 DeepSeek 的直接比较。这句话跟着 manifest
        一路带到报告里，不靠人记得手加——默认后端返回 None。"""
        return None

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        **kwargs,
    ) -> T:
        """给定 system/user 提示与目标 pydantic 模型，返回校验通过的模型实例。

        重试语义：首次 + 最多 2 次重试；第 2 次起把上次的原始返回和 pydantic
        校验错误一起回灌，要求模型修正。实测这一步是必要的——换模型时字段名
        猜错（比如把 element 写成 name）靠这一轮就能纠正。

        max_tokens 不传就用各后端自己的默认值（OpenAICompatBackend 读
        LLM_MAX_TOKENS 环境变量，默认 8192）。**不要全局调高默认值**：
        S1/S2/S3 用不到那么多 token，调高只会让真正失控的输出更晚才被
        发现；某个 prompt 确实需要更大上限（比如 S5 一张方子的「含」关系
        会重复带出 source_span，实测容易顶到 8192），在那一处调用点单独传。
        """
        # 字段名那一句是实测来的：裸 prompt（不注入 schema）下模型 3/3 把
        # ElementHit.element 写成 name。注入 schema 后 3/3 一次过，所以这句是
        # 加固不是救命。放在 schema 后面而不是写进各个 prompt 的 yaml：
        # schema 从 pydantic 自动导出，永远不会跟 schemas.py 漂移；写进 yaml 的
        # 手写示例会——改了字段名而忘了同步 yaml，示例反而会误导模型。
        schema_hint = (
            f"\n\n你的回答必须是且只能是一个符合以下 JSON Schema 的 JSON 对象，"
            f"不要输出任何解释、前后缀或 markdown 围栏，只输出 JSON 本身：\n"
            f"{schema.model_json_schema()}\n"
            f'字段名必须与上述 schema 完全一致，不要改写、不要用同义词'
            f'（例如 schema 里是 "element" 就不能写成 "name"）。'
        )
        messages = [
            {"role": "system", "content": system + schema_hint},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        last_raw = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                raw = self._complete(messages, temperature, max_tokens=max_tokens, **kwargs)
            except Exception as e:  # noqa: BLE001 - 传输类错误：超时/非零退出/API 异常
                # 这一类没有"上一次输出"可回灌——回灌上一轮的陈旧 raw 或空串只会让
                # 模型收到文不对题的纠错指令。原样重试，但重试前先退避一下。
                last_error = e
                if attempt < self.MAX_ATTEMPTS - 1:
                    backoff = self.RETRY_BACKOFF_SECONDS
                    self._sleep(backoff[min(attempt, len(backoff) - 1)])
                continue
            last_raw = raw
            stripped = strip_code_fence(raw)
            try:
                return schema.model_validate_json(stripped)
            except Exception as e:  # noqa: BLE001 - 校验错误：把原始输出和错误一起回灌
                last_error = e
                # 输出被截断（撞 max_tokens）跟"格式错了"是两类问题：格式错误
                # 回灌错误信息重试有意义，截断重试没有意义——同样的输入会在
                # 同一处再次被截断，三次重试只是白烧三次调用。直接失败，
                # 让调用方（比如 X3 批量抽取）决定要不要跳过这条输入。
                if _looks_like_truncated_json(e, stripped):
                    raise LLMTruncatedError(
                        f"疑似输出在 max_tokens 上限处被截断（JSON 在文本末尾附近"
                        f"解析失败，不是格式错误，不会重试）。backend={self.backend_id()}, "
                        f"model={self.model_name()}, schema={schema.__name__}, "
                        f"原始返回长度={len(stripped)} 字符, max_tokens={max_tokens}, "
                        f"原始错误={e}"
                    ) from e
                if attempt < self.MAX_ATTEMPTS - 1:
                    messages.append({"role": "assistant", "content": last_raw})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"上一次输出未通过校验，错误信息：{e}\n"
                                "请修正后重新输出一个符合 schema 的 JSON 对象，"
                                "不要输出解释或围栏。"
                            ),
                        }
                    )

        raise LLMError(
            f"LLM 调用在 {self.MAX_ATTEMPTS} 次尝试后仍失败。"
            f"backend={self.backend_id()}, model={self.model_name()}, "
            f"schema={schema.__name__}, 最后错误={last_error}, "
            f"最后原始返回前 500 字={last_raw[:500]!r}"
        )


class OpenAICompatBackend(LLMBackend):
    """走 OpenAI 兼容接口（DeepSeek 等）。惰性创建 client，避免模块加载时就要求
    环境变量齐全（测试环境可能没有 LLM_API_KEY）。"""

    def __init__(self) -> None:
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=os.environ.get("LLM_API_KEY"),
                base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
                # SDK 默认读超时 600s 且自带 2 次静默重试：一次挂起的连接最坏阻塞
                # 3(SDK)×3(generate)×600s，而且 SDK 的重试不计入 llm_calls，
                # 让"重试只在基类实现一份"这句话不成立。重试统一交给 generate()。
                timeout=float(os.environ.get("LLM_TIMEOUT", "120")),
                max_retries=0,
            )
        return self._client

    def model_name(self) -> str:
        return os.environ.get("LLM_MODEL", "deepseek-chat")

    def backend_id(self) -> str:
        return "api"

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None, **kwargs,
    ) -> str:
        resp = self.client.chat.completions.create(
            model=self.model_name(),
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            # DeepSeek 默认输出上限 4096 token：S0 抽多病人粗段的 JSON 会被截断，
            # 截断的 JSON 回灌重试也只会以同样方式再截断三次。默认给到 8192；
            # 调用方（generate() 的 max_tokens 参数）能覆盖这个默认值——
            # 不是全局调高，是某个 prompt 明确知道自己需要更大上限时单独传
            # （比如 S5 一张方子的多条「含」关系）。
            max_tokens=(
                max_tokens if max_tokens is not None
                else int(os.environ.get("LLM_MAX_TOKENS", "8192"))
            ),
            **kwargs,
        )
        return resp.choices[0].message.content or ""


class ClaudeCLIBackend(LLMBackend):
    """走本机的 `claude` CLI（非交互 -p 模式）。用于没有 DeepSeek 网络access 的
    环境里做真实模型冒烟——证明代码路径在真实模型下跑得通，**不是**用来出可比的数字。

    为什么不是替代品，两条实测理由：
      1. 慢且贵：精简调用约 4-8s、约 $0.06/次（DeepSeek 约 1-3s、约 $0.0007/次）。
         跑满一轮评测（约 3000 次）要 $190 量级，DeepSeek 是 $2 量级。
      2. 模型不同：这是 Claude，不是 deepseek-chat。SDT 基准分、ε 噪声地板这类
         "要和别人的数比"的指标，用它跑出来是无效的——所以 model_name() 如实
         返回 Claude 的模型名，comparability_warning() 也会一路带进 manifest。

    CLI 参数是按"当纯补全用"选的：换掉系统提示词、禁掉全部工具、不连 MCP、
    不落盘会话。实测这套参数把单次调用从约 31s 压到约 4-8s——省下的是
    Claude Code 自身的 agent 开销（工具定义、MCP 握手、CLAUDE.md 加载）。
    """

    DEFAULT_MODEL = "claude-sonnet-5"
    DEFAULT_TIMEOUT = 180  # 实测均值 4-8s，慢的时候到 48s；180s 给足余量

    # 纯补全不需要任何工具。逐个列出来而不是靠 --restricted：--restricted 只去掉
    # 执行类工具，Read/Glob 之类还在，模型可能真去读文件，那就不是纯补全了。
    _DISALLOWED_TOOLS = [
        "Bash", "Edit", "Write", "Read", "Glob", "Grep", "MultiEdit",
        "WebFetch", "WebSearch", "Task", "Agent", "TodoWrite", "NotebookEdit",
        "NotebookRead", "BashOutput", "KillShell", "Skill", "SlashCommand",
        "EnterPlanMode", "ExitPlanMode",
    ]

    _SYSTEM_PROMPT = (
        "You are a JSON generator. Output only a single valid JSON object. "
        "No explanation, no commentary, no markdown fences."
    )

    def model_name(self) -> str:
        return os.environ.get("CLAUDE_CLI_MODEL", self.DEFAULT_MODEL)

    def backend_id(self) -> str:
        return "claude_cli"

    def comparability_warning(self) -> str | None:
        return (
            f"后端：claude_cli（模型 {self.model_name()}），非 DeepSeek。"
            "本次结果仅用于验证代码路径可运行，不可与 AutoDL 上 DeepSeek 的数字直接比较。"
        )

    @property
    def timeout(self) -> int:
        return int(os.environ.get("CLAUDE_CLI_TIMEOUT", self.DEFAULT_TIMEOUT))

    def build_command(self) -> list[str]:
        """拼 CLI 参数。单独拆出来是为了能不真调就测参数对不对。"""
        cmd = [
            "claude", "-p",
            "--model", self.model_name(),
            "--system-prompt", self._SYSTEM_PROMPT,
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format", "json",
        ]
        cmd.append("--disallowedTools")
        cmd.extend(self._DISALLOWED_TOOLS)
        return cmd

    @staticmethod
    def flatten_messages(messages: list[dict]) -> str:
        """把 OpenAI 风格的多轮消息压成 CLI 的单个 prompt。

        system 已经通过 --system-prompt 传了纯 JSON 指令，这里要把业务 system
        （真正的辨证提示词）也带上，否则模型什么都不知道。重试轮次的
        assistant/user 对必须保留——错误回灌就靠它们，压掉了重试就等于没重试。
        """
        parts: list[str] = []
        for m in messages:
            role, content = m.get("role"), m.get("content", "")
            if not content:
                continue
            if role == "system":
                parts.append(content)
            elif role == "assistant":
                parts.append(f"[你上一次的输出]\n{content}")
            else:
                parts.append(content)
        return "\n\n".join(parts)

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None, **kwargs,
    ) -> str:
        # temperature：CLI 没有对应开关，这里如实忽略而不是假装设置了。
        # 影响：claude_cli 后端下 temperature=0 的"可复现"承诺不成立，
        # 所以它更不能用来测 ε（噪声地板）——ε 本来就是在测抖动。
        # max_tokens 同样忽略：`claude -p` 没有对应的输出长度上限开关
        # （`claude -p --help` 确认过），CLI 是完整 agent 会话不是裸 completion，
        # 截断风险由 generate() 里的 EOF 检测兜底，不是这里能设一个数解决的。
        prompt = self.flatten_messages(messages)
        proc = subprocess.run(
            self.build_command(),
            input=prompt,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise LLMError(
                f"claude CLI 退出码 {proc.returncode}\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise LLMError(
                f"claude CLI 的 --output-format json 输出无法解析：{e}\n"
                f"--- stdout ---\n{proc.stdout}\n"
                f"--- stderr ---\n{proc.stderr}"
            ) from e

        if payload.get("is_error"):
            raise LLMError(
                f"claude CLI 报告 is_error=true，"
                f"subtype={payload.get('subtype')}, "
                f"api_error_status={payload.get('api_error_status')}\n"
                f"--- 完整 payload ---\n{proc.stdout}"
            )

        result = payload.get("result")
        if not isinstance(result, str):
            raise LLMError(
                f"claude CLI 返回里没有可用的 result 字段（拿到 {type(result).__name__}）\n"
                f"--- 完整 payload ---\n{proc.stdout}"
            )
        return result


class VLLMBackend(LLMBackend):
    """正式阶段本地部署时启用，需核对 vLLM 版本 API；支持 guided_decoding 与 LoRA 热切换。

    demo 阶段不要求能跑通——这里只占位声明接口形状，让 get_backend() 的分支
    完整、将来切换时不用改调用方。真正接入时大致是：
      - 用 vllm.LLM 或 vllm 的 OpenAI 兼容 server（走 OpenAICompatBackend 复用即可）
      - guided_decoding 传 schema.model_json_schema() 做结构化约束，替代
        OpenAICompatBackend 里"提示词里塞 schema + json_object"的弱约束方式
      - LoRA 热切换通过 vllm 的 lora_request 参数，按 physician 选择不同 LoRA_DIR
    """

    def __init__(self) -> None:
        self._model_path = os.environ.get("LLM_MODEL_PATH")
        self._lora_dir = os.environ.get("LORA_DIR")

    def model_name(self) -> str:
        return self._model_path or "vllm-unconfigured"

    def backend_id(self) -> str:
        return "local"

    def comparability_warning(self) -> str | None:
        return "后端：local（vLLM），非 DeepSeek，数字不可与 AutoDL 直接比较。"

    def _complete(self, messages: list[dict], temperature: float, **kwargs) -> str:
        raise NotImplementedError(
            "VLLMBackend 尚未实现，正式阶段本地部署时补全（见类注释）。"
        )


def get_backend() -> LLMBackend:
    """按 LLM_MODE 环境变量返回后端实例，默认 api。"""
    mode = os.environ.get("LLM_MODE", "api")
    if mode == "local":
        return VLLMBackend()
    if mode == "claude_cli":
        return ClaudeCLIBackend()
    return OpenAICompatBackend()


_llm_singleton: LLMBackend | None = None
_llm_lock = threading.Lock()


def get_llm() -> LLMBackend:
    """惰性单例。模块底部不创建全局实例，避免模块导入时就要求环境变量齐全。

    加锁不是因为建后端对象重（它很轻），而是 manifest 里的 model/backend
    从这个对象问：两个线程各建一份、在途请求引用着不同的那份，同一批评测里
    两条记录就可能标着不同的后端。
    """
    global _llm_singleton
    if _llm_singleton is None:
        with _llm_lock:
            if _llm_singleton is None:
                _llm_singleton = get_backend()
    return _llm_singleton


def reset_llm_singleton() -> None:
    """清掉单例。给测试用——LLM_MODE 是进程级环境变量，不清单例的话
    第二个用例拿到的还是第一个用例建的后端。"""
    global _llm_singleton
    _llm_singleton = None
