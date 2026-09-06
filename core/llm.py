"""LLM 抽象层：prompt 加载/渲染 + 后端封装。所有 LLM 调用必须经过这里，禁止在业务代码里直接 import openai。"""
from __future__ import annotations

import json
import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from string import Template
from typing import TypeVar

import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def load_prompt(name: str, version: str = "v1") -> dict:
    """读 prompts/{version}/{name}.yaml，返回 {system, notes} 字典。"""
    path = PROMPTS_ROOT / version / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render(template_str: str, **kwargs) -> str:
    """用 string.Template 渲染。用 safe_substitute 而不是 substitute：
    prompt 里常有 $var 没被传入的情况（比如可选段落），缺变量时不该抛异常。"""
    return Template(template_str).safe_substitute(**kwargs)


def strip_code_fence(text: str) -> str:
    """剥离 LLM 返回里可能带的 markdown 围栏（```json ... ``` 或 ``` ... ```）。
    做成模块级函数是为了能单独单元测试，不依赖网络。"""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return stripped


class LLMError(RuntimeError):
    """LLM 调用在重试耗尽后仍失败时抛出，携带足够定位问题的上下文。"""


class LLMBackend(ABC):
    """后端基类。**重试/校验/错误回灌只在这里实现一份**，子类只实现 `_complete`
    这个"单次原始调用"。

    为什么把重试提到基类：切后端时如果每个后端各写一套重试，两边的失败率、
    调用次数就不可比了——同一条主诉在 DeepSeek 上算 1 次调用、在另一个后端上
    因为多重试一次算 2 次，manifest 里的 llm_calls 就失去意义。
    """

    MAX_ATTEMPTS = 3  # 首次 + 最多 2 次重试

    @abstractmethod
    def _complete(self, messages: list[dict], temperature: float, **kwargs) -> str:
        """单次原始调用：给定 [{"role", "content"}] 返回模型原始文本。
        不做 schema 校验、不重试——那些由 generate() 统一负责。"""
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
        **kwargs,
    ) -> T:
        """给定 system/user 提示与目标 pydantic 模型，返回校验通过的模型实例。

        重试语义：首次 + 最多 2 次重试；第 2 次起把上次的原始返回和 pydantic
        校验错误一起回灌，要求模型修正。实测这一步是必要的——换模型时字段名
        猜错（比如把 element 写成 name）靠这一轮就能纠正。
        """
        schema_hint = (
            f"\n\n你的回答必须是且只能是一个符合以下 JSON Schema 的 JSON 对象，"
            f"不要输出任何解释、前后缀或 markdown 围栏，只输出 JSON 本身：\n"
            f"{schema.model_json_schema()}"
        )
        messages = [
            {"role": "system", "content": system + schema_hint},
            {"role": "user", "content": user},
        ]

        last_error: Exception | None = None
        last_raw = ""
        for attempt in range(self.MAX_ATTEMPTS):
            try:
                raw = self._complete(messages, temperature, **kwargs)
                last_raw = raw
                return schema.model_validate_json(strip_code_fence(raw))
            except Exception as e:  # noqa: BLE001 - 校验错误与调用错误都要走同一条重试路径
                last_error = e
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
            )
        return self._client

    def model_name(self) -> str:
        return os.environ.get("LLM_MODEL", "deepseek-chat")

    def backend_id(self) -> str:
        return "api"

    def _complete(self, messages: list[dict], temperature: float, **kwargs) -> str:
        resp = self.client.chat.completions.create(
            model=self.model_name(),
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
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
        "Bash", "Edit", "Write", "Read", "Glob", "Grep",
        "WebFetch", "WebSearch", "Task", "TodoWrite", "NotebookEdit",
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

    def _complete(self, messages: list[dict], temperature: float, **kwargs) -> str:
        # temperature：CLI 没有对应开关，这里如实忽略而不是假装设置了。
        # 影响：claude_cli 后端下 temperature=0 的"可复现"承诺不成立，
        # 所以它更不能用来测 ε（噪声地板）——ε 本来就是在测抖动。
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


def get_llm() -> LLMBackend:
    """惰性单例。模块底部不创建全局实例，避免模块导入时就要求环境变量齐全。"""
    global _llm_singleton
    if _llm_singleton is None:
        _llm_singleton = get_backend()
    return _llm_singleton


def reset_llm_singleton() -> None:
    """清掉单例。给测试用——LLM_MODE 是进程级环境变量，不清单例的话
    第二个用例拿到的还是第一个用例建的后端。"""
    global _llm_singleton
    _llm_singleton = None
