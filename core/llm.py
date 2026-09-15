"""LLM 抽象层：prompt 加载/渲染 + 后端封装。所有 LLM 调用必须经过这里，禁止在业务代码里直接 import openai。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
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


def _min_json_length(node: dict, defs: dict, _depth: int = 0) -> int:
    """给定一段 JSON Schema 节点（`model_json_schema()` 的某个 properties 值，
    或整份 schema），估算它能编码出的**最短**合法 JSON 实例的字符数：只用
    必填字段，每个字段取它自己类型下最短的合法取值（空字符串按 minLength、
    最短的枚举值、一位数字……）。这是一个下界估计，不是精确值——真实的最短
    合法实例可能因为业务约束（比如 model_validator）比这个数还长，但从不会
    比它短，因为 schema 本身已经排除了更短的取值。低估比高估安全：低估只会让
    截断判定的门槛设低了一点，顶多多判几次真截断成"值得重试"（白烧一次调用，
    不会崩）；高估才会把真正的截断误判成"太短不算"，反而漏判。

    存在的理由：AutoDL 实测过一次"2 字符响应（内容是 `{"`）被判成截断"——
    EOF 确实落在文本末尾，`_looks_like_truncated_json` 的 EOF 判据本身没错，
    但它没有排除"这段输出短到连这个 schema 的最短合法实例都编不出来"这种
    情况——那不可能是"生成到一半被 max_tokens 砍断"（砍断意味着已经生成了
    大量内容，不可能只有 2 个字符），更像是网络抖动/限流吐回了一个几乎空的
    响应，应该走正常重试，不该被判定为"重试无意义"直接放弃。

    不看 max_tokens 数字本身（跟 `_looks_like_truncated_json` 原来的理由一样：
    token 数和字符数的换算在中文文本上不可靠），改用 schema 自身能推出的
    下界——这个下界跟语言、跟 max_tokens 具体设了多少都无关，是结构上的
    硬约束：不管 max_tokens 有多大，任何合法实例都不可能比它更短。
    """
    if "$ref" in node:
        return _min_json_length(defs.get(node["$ref"].rsplit("/", 1)[-1], {}), defs, _depth)
    if _depth > 8:
        return 2  # 防御自引用/深度嵌套 schema——这个项目里不会出现，纯保险
    for key in ("anyOf", "oneOf"):
        if key in node:
            options = node[key]
            return min(_min_json_length(o, defs, _depth + 1) for o in options)
    if "enum" in node:
        return min(len(json.dumps(v, ensure_ascii=False)) for v in node["enum"])
    node_type = node.get("type")
    if node_type == "object" or "properties" in node:
        required = node.get("required", [])
        if not required:
            return 2  # "{}"
        props = node.get("properties", {})
        field_parts = sum(
            len(json.dumps(name)) + 1 + _min_json_length(props.get(name, {}), defs, _depth + 1)
            for name in required
        )
        return 2 + field_parts + (len(required) - 1)  # 花括号 + 字段间逗号
    if node_type == "array":
        min_items = node.get("minItems", 0)
        if min_items == 0:
            return 2  # "[]"
        item_len = _min_json_length(node.get("items", {}), defs, _depth + 1)
        return 2 + min_items * item_len + (min_items - 1)
    if node_type == "string":
        return 2 + node.get("minLength", 0)  # 引号 + 内容
    if node_type in ("integer", "number"):
        return 1
    if node_type == "boolean":
        return 4  # "true"
    if node_type == "null":
        return 4  # "null"
    return 2  # 未识别的节点类型（这份 schema 词表之外），保守取最小值不高估


def _min_plausible_output_length(schema: type[BaseModel]) -> int:
    """schema 对应的最短合法 JSON 实例长度，见 _min_json_length。"""
    full = schema.model_json_schema()
    return _min_json_length(full, full.get("$defs", {}))


# **AutoDL 实测复现：_min_plausible_output_length 单独当下限对宽松 schema 形同
# 虚设。** S2Elements 的两个字段都有默认值、没有 required，最短合法实例就是
# "{}" = 2 字符——schema 越宽松这个下限就越趋近 2，而真实崩溃的返回恰好也是
# 2 字符（`{"`），下限="2 < 2"判定为 False，完全挡不住。全项目扫一遍会调用
# generate() 的 schema：CaseStructured/CaseTripleExtraction/FormulaSafety/
# FormularyExtraction/MateriaMedicaExtraction/S1Normalize/S2Elements/
# SegmentPatients/VisitStructured/SelectedOptions 等一大批都是 2（字段全部
# 可选或压根没有必填约束），这个方向本身在这些 schema 上就是无效防护。
#
# 真正该问的不是"这个 schema 最短能有多短"，是"这次返回相对于一次正常/被
# 真实砍断的生成，短到什么程度"——这跟 schema 松紧无关，跟"生成到一半被
# max_tokens 砍断"这个事件本身的性质有关：砍断的前提是已经生成了相当多
# 内容，不可能只有几个字符。用这个项目里两次真实观测定绝对下限：
#   - X3（offline/extract_case_triples.py）那次真实截断发生在 20724 字符处
#   - S3Syndrome（本项目最重的输出 schema）光结构下限就是 195，真实带三个
#     候选方剂的输出实测 1500+ 字符
#   - 这次误判的输入只有 2 字符
# 100 字符是这三个数字之间一个保守的分界：任何 schema 的真实截断都不会只
# 产出两位数字符——那不是"被砍断"，是"几乎没输出"，只可能是 API 抖动/限流，
# 应该重试。跟 schema 下限取较大值：宽松 schema 用这个绝对下限兜底，严格
# schema（S3Syndrome=195）用它自己更高的结构下限。
TRUNCATION_MIN_LENGTH = 100


def _truncation_length_threshold(schema: type[BaseModel]) -> int:
    """截断判定的实际长度门槛：schema 结构下限和绝对下限取较大值，见
    TRUNCATION_MIN_LENGTH 的文档字符串。"""
    return max(TRUNCATION_MIN_LENGTH, _min_plausible_output_length(schema))


def _looks_like_truncated_json(error: Exception, text: str, schema: type[BaseModel]) -> bool:
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

    但"末尾"本身不够：文本短到连 _truncation_length_threshold(schema) 都够不到
    时，不管 EOF 落在哪，都不可能是"生成到一半被砍断"——那需要先生成足够
    内容才谈得上"半路被砍"。门槛是 schema 结构下限和绝对下限
    （TRUNCATION_MIN_LENGTH）取较大值，不是单独用 schema 下限——见后者的
    文档字符串：单独用 schema 下限在宽松 schema 上形同虚设
    （AutoDL 实测 S2Elements + 2 字符输入就是这么漏过的）。
    """
    if len(text) < _truncation_length_threshold(schema):
        return False
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


class LLMCallTimeout(TimeoutError):
    """一次 `_complete` 超过墙钟上限还没返回。**故意继承 TimeoutError**：
    `generate()` 的传输类 `except Exception` 会接住它走既有重试路径，而
    `core/batch.py` 的 `classify_llm_failure` 按异常类型归类时它落在超时那一类。"""


@dataclass(frozen=True)
class CallTimeouts:
    """一次 LLM 调用的四个 HTTP 相位超时 + 一个墙钟兜底。

    **四个相位分别设，不是一个总超时**：openai SDK 收到一个 float 时会把它
    铺给四个相位（connect/read/write/pool 都等于那个数），于是"连不上"要等和
    "读不出来"一样久——连接建立本来是秒级的事，等 120 秒没有意义。

    `deadline` 是这一层最要紧的东西，理由是 R8 段 6 那次实测：客户端**已经**
    设了 `timeout=120`，进程还是卡了 46 分钟（wchan=do_poll、socket 还在）。
    因为 **httpx 的 read 超时是"单次 socket 读"的上限，不是整个响应的期限**：
    中间任何一跳（CDN、网关、反代）只要每隔几十秒吐一个字节/一个保活帧，
    每次读都没超时，整个请求可以挂到天荒地老。`deadline` 从外面给整次调用
    封一个墙钟上限，超了就抛 LLMCallTimeout 走重试——**这才是"重试逻辑永远
    不会触发"那个洞真正的补法**。

    值的依据（实测，写在这里免得下次有人直接调大）：
      单次 S3 调用 6~8 秒；ReAct 单步 2~3 秒；药理层抽一块 10~20 秒；
      S0 抽多病人粗段最慢到 60 秒量级（max_tokens=8192）。
    """

    connect: float
    read: float
    write: float
    pool: float
    deadline: float

    def httpx_timeout(self):
        """惰性 import httpx：没装 openai/httpx 的机器也要能 import core.llm
        （tests/test_local_backend.py 有一条钉这个）。"""
        import httpx

        return httpx.Timeout(connect=self.connect, read=self.read,
                             write=self.write, pool=self.pool)

    def with_seconds(self, seconds: float) -> CallTimeouts:
        """`LLM_TIMEOUT_SECONDS` 只给一个数时怎么摊：它说的是"一次调用最长等多久"
        ——所以它直接改 read 和 deadline（deadline 留 1.5 倍余量给重定向/重连这类
        同一次调用里的多段网络交互），connect/write 不跟着放大（连接和发请求慢到
        30 秒以上一定是网络坏了，等更久没有意义）。"""
        return CallTimeouts(
            connect=min(self.connect, seconds), read=seconds,
            write=min(self.write, seconds), pool=min(self.pool, seconds),
            deadline=max(seconds * 1.5, seconds + 10),
        )


# 云端 API（DeepSeek）：读 120 秒远超正常值（最慢的 S0 在 60 秒量级）、远低于
# "挂死"；连接 15 秒、发请求 30 秒（prompt 最大几十 KB）；墙钟 180 秒。
API_TIMEOUTS = CallTimeouts(connect=15.0, read=120.0, write=30.0, pool=15.0, deadline=180.0)
# 本地 vLLM server：权重是 server 自己启动时加载的（scripts/start_vllm.sh），
# 但**首个请求**要等它把 CUDA graph / 预热做完，实测几十秒到几分钟；排队时
# 单个请求也可能等很久。读 600 秒、墙钟 900 秒。
LOCAL_SERVER_TIMEOUTS = CallTimeouts(connect=10.0, read=600.0, write=30.0, pool=10.0, deadline=900.0)
# 进程内 vLLM：**第一次调用会在进程内加载权重**（VLLMInProcessBackend._engine
# 是惰性的），1.5B 模型实测几分钟、更大的更久。墙钟给 1800 秒——比它慢就是真卡住。
# 这里没有 HTTP，四个相位的值用不上，填同一个数只是为了 dataclass 完整。
INPROC_TIMEOUTS = CallTimeouts(connect=1800.0, read=1800.0, write=1800.0, pool=1800.0,
                               deadline=1800.0)


# 单次输出上限的默认值。**两档，按模型是不是推理模型分**。
#
# DeepSeek 默认 4096 token：S0 抽多病人粗段的 JSON 会被截断，截断的 JSON 回灌重试
# 也只会以同样方式再截断三次。所以非推理模型给到 8192。
DEFAULT_MAX_TOKENS = 8192
# 推理模型（deepseek-v4-pro）要另算：**max_tokens 同时盖住不可见的 reasoning
# tokens**，不是只盖可见输出。2026-09-15 实测「你好」这一句就花了 45 个 token、
# 其中 36 个是 reasoning；8192 下 S3 的可见输出在 2081 字符处被砍断——而 8192 这个
# 数字看起来完全够用，所以症状是"输出莫名截断"，看不出跟推理有关。
REASONING_MAX_TOKENS = 16384
# 已知的推理模型。**按名字判**：服务端不给"这是不是推理模型"这个字段，而这件事
# 只影响一个默认值、猜错的代价是上限偏大或偏小（`LLM_MAX_TOKENS` 能覆盖），
# 不值得为它加一次探测调用。新模型上线时往这里加一行。
REASONING_MODELS = frozenset({"deepseek-v4-pro"})


# DeepSeek 官方错误码表（https://api-docs.deepseek.com/zh-cn/quick_start/error_codes）：
#   400 格式错误 / 401 认证失败（API key 错）/ 402 余额不足 / 422 参数错误
#   429 请求速率达到上限 / 500 服务器故障 / 503 服务器繁忙
# 前四个是**确定性**的：同样的 key、同样的请求体，重试三次结果一模一样，只是把
# 一次失败变成三次失败加两次退避。429/500/503 才是"等一下可能就好了"。
# 这个区分在本文件里已有先例——LLMTruncatedError 就是因为"同样的输入会在同一处
# 再次被截断"而拒绝重试。这里照同一条理由办。
NON_RETRYABLE_STATUS = {400, 401, 402, 422}


class LLMAuthError(LLMError):
    """认证/余额/请求体这类确定性失败，不重试。

    单独立一个类型是因为**它要说给访问者听**：BYOK 场景下 401 是"你填的 key 不对"、
    402 是"你的账户余额不足"，这两件事只有访问者能修，而通用的
    「服务端处理失败（错误编号 xxxx）」会让他去找站点管理员。
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _status_code_of(exc: Exception) -> int | None:
    """从 OpenAI SDK 的异常里取 HTTP 状态码。SDK 的 APIStatusError 带
    .status_code；取不到就返回 None，按可重试处理（宁可多试一次）。"""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


class LLMBackend(ABC):
    """后端基类。**重试/校验/错误回灌只在这里实现一份**，子类只实现 `_complete`
    这个"单次原始调用"。

    为什么把重试提到基类：切后端时如果每个后端各写一套重试，两边的失败率、
    调用次数就不可比了——同一条主诉在 DeepSeek 上算 1 次调用、在另一个后端上
    因为多重试一次算 2 次，manifest 里的 llm_calls 就失去意义。
    """

    MAX_ATTEMPTS = 3  # 首次 + 最多 2 次重试
    # 这个后端的超时。子类覆盖（本地模型要长得多）；`LLM_TIMEOUT_SECONDS`
    # （旧名 `LLM_TIMEOUT` 仍然认）覆盖所有后端。
    TIMEOUTS: CallTimeouts = API_TIMEOUTS
    # 传输类错误（超时、429、连接断）两次重试之间的等待秒数，按重试序号取。
    # 只对传输错误退避：校验错误是模型输出格式不对，回灌错误信息立刻重问才有
    # 意义，等一秒不会让它答得更对。之前是零间隔立刻重试，429 会变成三个
    # 连续的 429。测试里把它 monkeypatch 成 (0, 0)，不真等。
    RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0)
    _sleep = staticmethod(time.sleep)  # 留个缝给测试换掉，不真睡

    def timeouts(self) -> CallTimeouts:
        """本次调用生效的超时。环境变量优先：`LLM_TIMEOUT_SECONDS`，兼容旧名
        `LLM_TIMEOUT`（README 和 .env.example 里已经写了一轮，不能悄悄失效）。
        解析不了就用后端默认值并**吼一声**——静默回退到默认值会让人以为自己
        设的值生效了。"""
        raw = os.environ.get("LLM_TIMEOUT_SECONDS") or os.environ.get("LLM_TIMEOUT")
        if not raw:
            return self.TIMEOUTS
        try:
            seconds = float(raw)
            if seconds <= 0:
                raise ValueError("必须是正数")
        except ValueError as e:
            print(f"[llm] LLM_TIMEOUT_SECONDS={raw!r} 解析不了（{e}），"
                  f"这次用后端 {self.backend_id()} 的默认值 "
                  f"{self.TIMEOUTS.read} 秒读超时 / {self.TIMEOUTS.deadline} 秒墙钟",
                  file=sys.stderr)
            return self.TIMEOUTS
        return self.TIMEOUTS.with_seconds(seconds)

    def _default_max_tokens(self) -> int:
        """这次调用没有显式传 max_tokens 时用多少。`LLM_MAX_TOKENS` 优先。
        **放在基类**：每个后端都要回答这个问题，各写一份的话"推理模型要更大"
        这条会只在其中一个后端上生效。

        **推理模型要更大**：它的 max_tokens 同时盖住不可见的 reasoning tokens，
        所以 8192 留给可见输出的远不止少一点点——2026-09-15 实测 deepseek-v4-pro
        在 8192 下 S3 的输出在 2081 字符处被砍断。这一档是把推理模型的**可见**
        输出上限拉回到跟非推理模型同一个量级，不是"推理模型更强所以给更多"。

        判据放在代码里而不是让人去 .env 里记一个数：一个"必须手动设对、否则
        静默截断"的环境变量迟早有一次会忘，而忘了的代价是一整段的钱。
        """
        env = os.environ.get("LLM_MAX_TOKENS")
        if env:
            return int(env)
        return (REASONING_MAX_TOKENS if self.model_name() in REASONING_MODELS
                else DEFAULT_MAX_TOKENS)

    def abort_in_flight(self) -> None:
        """墙钟超时之后清理这个后端里挂着的东西。默认什么都不做；
        OpenAICompatBackend 覆盖成"丢掉那个 HTTP 客户端"，好让卡住的那次读
        随着连接池被回收而死掉，不然重试会排在同一个坏连接后面。"""

    def _complete_within_deadline(
        self, messages: list[dict], temperature: float, max_tokens: int | None,
        schema: type[BaseModel] | None, physician: str | None, deadline: float, **kwargs,
    ) -> str:
        """在墙钟上限内跑一次 `_complete`，超了抛 LLMCallTimeout。

        **为什么要这一层**（R8 段 6 实测）：HTTP 客户端那边已经设了 120 秒读超时，
        进程还是卡了 46 分钟——read 超时管的是"单次 socket 读"，对方每隔几十秒
        吐一个字节就永远不触发。`MAX_ATTEMPTS=3` 的前提是"这次调用返回了"，
        挂住不返回时重试逻辑根本没机会跑。

        实现是"工作线程 + join(deadline)"：blocking 的 socket 读没法从外面取消，
        所以超时后**不等它**（daemon 线程，进程退出不会被它拖住），只把它丢在
        后台自己去死（`abort_in_flight()` 顺手断掉连接池加速这件事）。代价是
        最坏情况下有几个僵住的线程，换来的是主流程一定能往前走。
        顺带一个好处：主线程这会儿卡在 `join()` 上而不是卡在 C 层的 read 里，
        Ctrl-C 立刻生效——上一轮"杀不掉的卡死"也是这个原因。
        """
        result: dict[str, object] = {}

        def _run() -> None:
            try:
                result["value"] = self._complete(
                    messages, temperature, max_tokens=max_tokens,
                    schema=schema, physician=physician, **kwargs,
                )
            except BaseException as e:  # noqa: BLE001 - 原样带回主线程再抛
                result["error"] = e

        worker = threading.Thread(target=_run, name="llm-call", daemon=True)
        worker.start()
        worker.join(timeout=deadline)
        if worker.is_alive():
            self.abort_in_flight()
            raise LLMCallTimeout(
                f"一次 LLM 调用超过 {deadline:.0f} 秒墙钟上限还没返回"
                f"（backend={self.backend_id()}, model={self.model_name()}）。"
                "HTTP 读超时管的是单次 socket 读，对方细水长流地吐字节时不会触发，"
                "所以这里从外面封一个上限。调大用 LLM_TIMEOUT_SECONDS。"
            )
        if "error" in result:
            raise result["error"]  # type: ignore[misc]
        return str(result.get("value", ""))

    @abstractmethod
    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        **kwargs,
    ) -> str:
        """单次原始调用：给定 [{"role", "content"}] 返回模型原始文本。
        不做 schema 校验、不重试——那些由 generate() 统一负责。

        max_tokens 是显式参数不是塞进 **kwargs：OpenAICompatBackend 原来
        自己读环境变量算这个值，如果调用方也通过 kwargs 传一份同名参数，
        会在传给 SDK 时撞上"重复关键字参数"。None 表示"用这个后端自己的
        默认值"（不是"不设上限"——CLI 后端本来就没有这个旋钮）。

        schema / physician 同样是显式参数、同样不塞 **kwargs，理由也一样：
        OpenAICompatBackend 把 `**kwargs` 原样转给 OpenAI SDK，多一个它不认的
        关键字参数就是 TypeError。两个参数都只有本地后端用得上：
          - schema：vLLM 的 guided_decoding 要拿 `schema.model_json_schema()`
            从解码层保证输出合法（比"prompt 里塞 schema + json_object"这种
            弱约束强）。API 后端拿不到这个能力，如实忽略。
          - physician：阶段五每位医家一个 LoRA，vLLM server 支持按请求切
            adapter。用显式参数而不是线程局部/全局"当前医家"：LoRA 选错会让
            "张锡纯用的是他自己的 LoRA"这句声称变成假的，而隐式上下文一旦
            哪个调用点忘了设，错的是静默的——看 run_physician 这一行看不出
            adapter 是从哪来的。api/main.py 是多线程并发问诊，模块级"当前
            医家"还会有竞态。知道医家是谁的调用点（core/chain.py 的
            run_physician、core/react.py 的 run_react）自己报出来，最直接。
        """
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

    def lora_for(self, physician: str | None) -> str | None:
        """这次调用实际会挂哪个 LoRA adapter 名，None = 基座模型/没有这回事。

        只有本地 vLLM 后端会返回非 None。做成基类方法（而不是让调用方判断
        "如果是 VLLMBackend 就问一下"）是为了让 core/chain.py 那一行无条件
        可写：调用方不该知道有几种后端、哪种支持 LoRA。
        """
        return None

    def lora_dir(self) -> str | None:
        """这一轮配置的 LoRA 根目录，None = 没配。manifest 记它是为了说明
        "这次跑的 adapter 是从哪来的"——per-physician 的实际 adapter 记在每位
        医家的结果里（见 core/chain.py::run_physician 的 "lora" 字段）。"""
        return None

    def replay_info(self) -> dict | None:
        """这一次的输出是不是回放的录制结果；None = 实时调用。

        非 None 时至少带 recorded_at / model / git_commit（见
        core/llm_replay.py::ReplayBackend.replay_info），manifest 原样带上，
        前端据此显示那行"演示模式"小字。跟 lora_for/lora_dir 同一个理由做成
        基类方法：调用方不该知道有几种后端，`llm.replay_info()` 要无条件可写。

        **这个方法存在的意义是不许伪装成实时调用。** 回放的结果如果在 manifest
        里看起来跟实时跑的一样，"这是我们系统跑出来的"这句话就变成了假的——
        跟 model_name() 禁止伪装成别的模型是同一条纪律。
        """
        return None

    def generate(
        self,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        max_tokens: int | None = None,
        physician: str | None = None,
        **kwargs,
    ) -> T:
        """给定 system/user 提示与目标 pydantic 模型，返回校验通过的模型实例。

        physician 只在本地后端（vLLM + LoRA）下有意义：知道这一次是替哪位医家
        推理的调用点（run_physician / run_react）显式传医家 id，后端据此选
        adapter；其余后端如实忽略。不传 = 不指定 adapter（走基座模型），
        S1/S2 这类跟医家无关的调用就是这种情况。理由详见 _complete 的文档。

        重试语义：首次 + 最多 2 次重试；第 2 次起把上次的原始返回和 pydantic
        校验错误一起回灌，要求模型修正。实测这一步是必要的——换模型时字段名
        猜错（比如把 element 写成 name）靠这一轮就能纠正。

        max_tokens 不传就用各后端自己的默认值（OpenAICompatBackend 见
        `_default_max_tokens()`：非推理模型 8192、推理模型 16384，
        `LLM_MAX_TOKENS` 覆盖两者）。**不要全局调高默认值**：S1/S2/S3 用不到
        那么多 token，调高只会让真正失控的输出更晚才被发现；某个 prompt 确实
        需要更大上限（比如 S5 一张方子的「含」关系会重复带出 source_span，
        实测容易顶到 8192），在那一处调用点单独传。

        R10 给推理模型单独一档，**不是违反上面那句**：对推理模型来说
        max_tokens 盖的是 reasoning + 可见输出两部分，8192 里能给可见输出的
        并不是 8192（实测 S3 在 2081 字符处被砍），所以这一档不是"调高上限"，
        是把上限还原到跟非推理模型同一个量级的可见输出。
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
                raw = self._complete_within_deadline(
                    messages, temperature, max_tokens=max_tokens,
                    schema=schema, physician=physician,
                    deadline=self.timeouts().deadline, **kwargs,
                )
            except Exception as e:  # noqa: BLE001 - 传输类错误：超时/非零退出/API 异常
                # 401/402/422 这类确定性失败直接抛，不进重试（见 NON_RETRYABLE_STATUS
                # 上面那段注释）。**这一条对 BYOK 尤其要紧**：访问者填错一个 key，
                # 原来要等三次往返加两次退避，最后拿到一条看不出是自己 key 的
                # 通用错误。
                status = _status_code_of(e)
                if status in NON_RETRYABLE_STATUS:
                    raise LLMAuthError(_auth_error_message(status, e), status) from e
                # 其余没有"上一次输出"可回灌——回灌上一轮的陈旧 raw 或空串只会让
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
                if _looks_like_truncated_json(e, stripped, schema):
                    # max_tokens=None 不是"没配置成功"，是"这次调用没有显式传，
                    # 会走各后端自己的默认值"（比如 OpenAICompatBackend 走
                    # LLM_MAX_TOKENS，或 _default_max_tokens()）——原来直接打
                    # "max_tokens=None" 容易让人以为配置丢了，去查环境变量，其实哪儿都没错。
                    max_tokens_desc = (
                        f"未设置（走后端默认值 {self._default_max_tokens()}）"
                        if max_tokens is None else str(max_tokens)
                    )
                    raise LLMTruncatedError(
                        f"疑似输出在 max_tokens 上限处被截断（JSON 在文本末尾附近"
                        f"解析失败，不是格式错误，不会重试）。backend={self.backend_id()}, "
                        f"model={self.model_name()}, schema={schema.__name__}, "
                        f"原始返回长度={len(stripped)} 字符, max_tokens={max_tokens_desc}, "
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

        # from last_error 保留异常链：调用方（比如 X3 批量抽取）要按失败原因
        # 分布做统计时，靠字符串里的"最后错误={last_error}"去解析文本很脆弱
        # （错误信息格式一变就解析错）。__cause__ 是结构化的，
        # type(err.__cause__).__name__ 直接给出真实的异常类型
        # （TimeoutError/RateLimitError/ValidationError……），不用猜。
        raise LLMError(
            f"LLM 调用在 {self.MAX_ATTEMPTS} 次尝试后仍失败。"
            f"backend={self.backend_id()}, model={self.model_name()}, "
            f"schema={schema.__name__}, 最后错误={last_error}, "
            f"最后原始返回前 500 字={last_raw[:500]!r}"
        ) from last_error


class OpenAICompatBackend(LLMBackend):
    """走 OpenAI 兼容接口（DeepSeek 等）。惰性创建 client，避免模块加载时就要求
    环境变量齐全（测试环境可能没有 LLM_API_KEY）。

    VLLMBackend 继承它：vLLM 起了 `vllm.entrypoints.openai.api_server` 之后接口
    跟 OpenAI 完全兼容，客户端构造、重试语义、max_tokens 默认值这些逻辑一模一样，
    不该有第二份（CLAUDE.md「同一概念只能有一处实现」）。差异只落在下面这几个
    可覆盖的钩子上：`_default_base_url` / `_api_key` / `_request_model_name`，
    以及子类自己在 `_complete` 里补 extra_body 后委托回 `super()._complete`。
    """

    def __init__(self) -> None:
        self._client = None

    def _default_base_url(self) -> str:
        """LLM_BASE_URL 没设时用的地址。子类（本地 vLLM）覆盖成本机 server。"""
        return "https://api.deepseek.com"

    def _api_key(self) -> str | None:
        """子类覆盖：vLLM server 不校验 api_key，但 OpenAI SDK 要求非空。"""
        return os.environ.get("LLM_API_KEY")

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key(),
                base_url=os.environ.get("LLM_BASE_URL", self._default_base_url()),
                # SDK 默认读超时 600s 且自带 2 次静默重试：一次挂起的连接最坏阻塞
                # 3(SDK)×3(generate)×600s，而且 SDK 的重试不计入 llm_calls，
                # 让"重试只在基类实现一份"这句话不成立。重试统一交给 generate()。
                #
                # **四个相位分别设**（R8 段 6 之后）：原来这里传的是一个 float，
                # SDK 会把它铺给四个相位，于是"连不上"要等和"读不出来"一样久。
                # 每个值的依据见 CallTimeouts 的文档字符串。
                timeout=self.timeouts().httpx_timeout(),
                max_retries=0,
            )
        return self._client

    def abort_in_flight(self) -> None:
        """墙钟超时之后把客户端丢掉：连接池跟着被回收，卡住的那次读会随之出错
        死掉，重试也不会排在同一个坏连接后面。下次用 client 时惰性重建。"""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 - 清理路径不该盖住真正的超时错误
                pass

    def model_name(self) -> str:
        """默认值 R10 从 `deepseek-chat` 改成 `deepseek-v4-pro`：**deepseek-chat
        已经下线**（2026-09-15 实测 `/models` 只剩 deepseek-flash 和 deepseek-v4-pro；
        拿 deepseek-chat 发请求得到的是 HTTP 200 + **空响应体**，不是 404）。
        留着一个已经不存在的默认值，症状是"一次调用返回空串 → 校验失败 → 重试三次 →
        LLMError"，而错误信息里看不出"模型名不存在"这件事。

        ⚠ **换模型 = 所有既有数字不可比**：ε / E3 / E4 / E8 / E9 / SDT 全是
        deepseek-chat 跑的，那个模型现在不存在了，任何重跑都换了模型。
        manifest 的 comparability_warning 会如实记录这次的 model。"""
        return os.environ.get("LLM_MODEL", "deepseek-v4-pro")

    def _request_model_name(self) -> str:
        """HTTP 请求里 `model` 字段的值。默认跟 model_name() 同一个——对 DeepSeek
        这类云端 API，"模型叫什么"和"请求里填什么"本来就是一件事。

        留这个钩子是为了 vLLM：`--served-model-name tcm-local` 可以跟权重路径
        完全不同，请求必须填 served name 才认，而 manifest 要记的是权重路径
        （"tcm-local"这种别名对复现毫无用处）。两个值分开，而不是让 model_name()
        为了让请求能通就去报别名——那正是 model_name() 文档里禁止的"伪装"。
        """
        return self.model_name()

    def backend_id(self) -> str:
        return "api"

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        **kwargs,
    ) -> str:
        # schema / physician 在这一层如实忽略：云端 API 既没有 guided_decoding
        # 也没有 LoRA adapter 可切。**不能转给 SDK**——多一个它不认的关键字
        # 参数就是 TypeError（这也是这两个参数为什么是显式形参、不塞 kwargs）。
        resp = self.client.chat.completions.create(
            model=self._request_model_name(),
            messages=messages,
            temperature=temperature,
            response_format={"type": "json_object"},
            # 默认值见 _default_max_tokens()（推理模型要更大，理由在那儿）。
            # 调用方（generate() 的 max_tokens 参数）能覆盖它——不是全局调高，
            # 是某个 prompt 明确知道自己需要更大上限时单独传（比如 S5 一张方子的
            # 多条「含」关系）。
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens(),
            **kwargs,
        )
        content = resp.choices[0].message.content or ""
        if not content.strip():
            # HTTP 200 + 空响应体 = **模型名很可能不存在/已下线**（2026-09-15 实测：
            # 用已下线的 deepseek-chat 请求就是这个表现，不是 404）。不专门报出来的话
            # 症状是"空串过不了 schema 校验 → 重试三次 → LLMError"，错误信息里全是
            # 校验失败，看不出根因在模型名上。**照旧抛异常走重试**（网络抖动也可能
            # 返回空），但把这条线索写进错误里。
            raise LLMError(
                f"后端返回了 HTTP 200 但响应体是空的（model={self._request_model_name()!r}）。"
                "最常见的原因是**模型名不存在或已下线**——2026-09-15 实测 deepseek-chat "
                "已下线，拿它发请求就是这个表现（不是 404）。"
                "用 `curl $LLM_BASE_URL/models` 看当前可用的模型名，再设 LLM_MODEL。"
            )
        return content


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
    # 子进程那边已经有 subprocess 的 timeout（self.timeout），墙钟只是兜底：
    # 比它多 30 秒，好让 subprocess 自己的超时先触发（那条错误信息更具体）。
    TIMEOUTS = CallTimeouts(connect=10.0, read=float(DEFAULT_TIMEOUT),
                            write=10.0, pool=10.0, deadline=DEFAULT_TIMEOUT + 30.0)

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
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        **kwargs,
    ) -> str:
        # schema / physician 如实忽略：CLI 既没有 guided_decoding 也没有 LoRA。
        # schema 的约束已经通过 generate() 拼进 system prompt 了（弱约束，
        # 靠重试兜底），这里没有更强的手段可用。
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


# vLLM 的 guided_decoding 参数名在版本间变过（`guided_json` 走 extra_body 是
# 0.4~0.8.x 一直支持的写法，更新的版本另外支持 OpenAI 标准的
# `response_format: json_schema`）。默认用 `guided_json`，留一个环境变量是因为
# 这个项目没法在沙盒里对着真实 vLLM 验版本——真撞上版本不认这个键时，运维侧
# 改一个环境变量就能绕过，不必改代码等下一轮。**不是**给"随便调调看哪个能跑"
# 用的：scripts/verify_local_backend.py 会把实际生效的键打出来。
VLLM_GUIDED_JSON_KEY = os.environ.get("VLLM_GUIDED_JSON_KEY", "guided_json")


def _resolve_lora_path(lora_dir: str | None, physician: str | None) -> Path | None:
    """按医家找 LoRA adapter 目录。返回 None = 这次不挂 adapter，走基座模型。

    三种情况分清楚（两个本地后端共用这一处判断，不各写一遍）：
      - `LORA_DIR` 没设置：阶段五的 LoRA 还没训，走基座模型，返回 None。
      - 设置了但这次调用没带 physician（S1/S2 这类跟医家无关的步骤）：
        同样返回 None——不是错误，这些步骤本来就没有"哪位医家"可言。
      - 设置了、带了 physician、但目录不存在：**抛异常，不静默退化成基座模型**。
        静默退化会让"张锡纯用的是他自己的 LoRA"这句声称变成假的，而且是静默
        变假——报告照样写着 LoRA 跑的，实际跑的是基座，没有任何地方能看出来。
        宁可这次调用失败，让人去修路径。
    """
    if not lora_dir or physician is None:
        return None
    path = Path(lora_dir) / physician
    if not path.is_dir():
        raise LLMError(
            f"LORA_DIR={lora_dir} 已设置，但医家 {physician!r} 的 adapter 目录不存在："
            f"{path}。不静默退化成基座模型——那会让「这位医家用的是他自己的 LoRA」"
            f"这句声称变成假的。请确认 adapter 已训好并放在 {lora_dir}/<physician_id>/，"
            f"或者取消设置 LORA_DIR 明确表示这一轮跑基座模型。"
        )
    return path


class VLLMBackend(OpenAICompatBackend):
    """本地 vLLM，**server 模式**（`LLM_MODE=local`）：对着
    `python -m vllm.entrypoints.openai.api_server` 起的 OpenAI 兼容接口说话。

        LLM_MODE=local
        LLM_BASE_URL=http://127.0.0.1:8000/v1     # 不设就用这个默认值
        LLM_MODEL=tcm-local                        # server 的 --served-model-name
        LLM_MODEL_PATH=/root/autodl-tmp/models/Qwen2.5-1.5B-Instruct
        LORA_DIR=/root/autodl-tmp/lora             # 可选，阶段五 LoRA 训好之后

    继承 OpenAICompatBackend 而不是复制它：客户端构造、重试语义、max_tokens
    默认值这些完全一样，差异只有 base_url/api_key/请求里的 model 名，以及
    多传一个 extra_body。启动命令见 scripts/start_vllm.sh。

    比云端 API 多出来的两个能力，都在 extra_body 里：
      - guided_json：从解码层保证输出符合 schema。云端那套"prompt 里塞 schema
        + response_format=json_object"是弱约束，模型仍可能给出不合 schema 的
        JSON，靠 generate() 三次重试兜底；guided_decoding 生效时理论上不该再
        触发那些重试——如果本地模型仍然频繁重试，说明这个键没生效（版本不认），
        是配置问题，要暴露出来而不是静默退化，scripts/verify_local_backend.py
        专门查这一点。
      - lora_request：阶段五每位医家一个 adapter，一个 server 进程按请求切换。
    """

    # 本地 server 的首个请求要等预热/CUDA graph，排队时单个请求也可能等很久，
    # 所以超时比云端长得多（见 LOCAL_SERVER_TIMEOUTS 的依据）。
    TIMEOUTS = LOCAL_SERVER_TIMEOUTS

    def __init__(self) -> None:
        super().__init__()
        self._model_path = os.environ.get("LLM_MODEL_PATH")
        self._lora_dir = os.environ.get("LORA_DIR")

    def _default_base_url(self) -> str:
        return "http://127.0.0.1:8000/v1"

    def _api_key(self) -> str | None:
        # vLLM server 默认不校验 api_key，但 OpenAI SDK 不允许空值（会去找
        # OPENAI_API_KEY 环境变量、找不到就抛）。给一个明显是占位的串，
        # 同时仍然尊重显式设置的 LLM_API_KEY（server 可以用 --api-key 开鉴权）。
        return os.environ.get("LLM_API_KEY") or "EMPTY"

    def model_name(self) -> str:
        """manifest 记的是权重路径，不是 served name 别名：别名（"tcm-local"）
        对复现毫无用处，路径才说明跑的是哪个模型。两者都没有就如实说没配置。"""
        return self._model_path or os.environ.get("LLM_MODEL") or "vllm-unconfigured"

    def _request_model_name(self) -> str:
        """请求里填 served name（server 只认它）；没设 served name 时 vLLM 用
        权重路径当模型名，那就填路径。"""
        return os.environ.get("LLM_MODEL") or self._model_path or "vllm-unconfigured"

    def backend_id(self) -> str:
        return "local"

    def lora_for(self, physician: str | None) -> str | None:
        """这次调用实际会挂哪个 adapter（None = 基座模型）。manifest 用它如实
        记录，不靠"配置了 LORA_DIR 就假设每位医家都用上了自己的 adapter"。"""
        path = _resolve_lora_path(self._lora_dir, physician)
        return physician if path is not None else None

    def lora_dir(self) -> str | None:
        return self._lora_dir

    def comparability_warning(self) -> str | None:
        lora = f"LoRA: {self._lora_dir}（按医家挂 adapter）" if self._lora_dir else "LoRA: 未加载"
        return (
            f"后端：local（vLLM {self.model_name()}），非 DeepSeek。{lora}。"
            "数字不可与 API 后端（DeepSeek）直接比较。"
        )

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        **kwargs,
    ) -> str:
        extra_body = dict(kwargs.pop("extra_body", None) or {})
        if schema is not None:
            extra_body[VLLM_GUIDED_JSON_KEY] = schema.model_json_schema()
        lora_path = _resolve_lora_path(self._lora_dir, physician)
        if lora_path is not None:
            extra_body["lora_request"] = {
                "lora_name": physician,
                "lora_path": str(lora_path),
            }
        # 委托回父类：客户端、max_tokens 默认值、response_format 全部复用，
        # 这里只负责把本地特有的 extra_body 补上。schema/physician 不再往下传
        # ——父类那一层只会如实忽略它们，而它们要表达的东西已经变成 extra_body。
        return super()._complete(
            messages, temperature, max_tokens=max_tokens,
            extra_body=extra_body, **kwargs,
        )


class VLLMInProcessBackend(LLMBackend):
    """本地 vLLM，**进程内模式**（`LLM_MODE=local_inproc`）：不起 server，直接在
    本进程里 `vllm.LLM(...)` 加载权重。

        LLM_MODE=local_inproc
        LLM_MODEL_PATH=/root/autodl-tmp/models/Qwen2.5-1.5B-Instruct
        LORA_DIR=/root/autodl-tmp/lora     # 可选

    跟 server 模式的取舍：批量评测（run_eval/estimate_epsilon 动辄上千次调用）
    省掉每次的 HTTP 往返和 JSON 编解码；代价是模型跟评测脚本绑在同一个进程里，
    起停慢、并发要自己管，也没法给 api/main.py 的多线程问诊共用。所以在线服务
    和演示用 server 模式，离线批量评测用这个。

    **`import vllm` 必须延迟到真正要用的时候**（`_engine` 属性里），不能放模块
    顶层：沙盒和 CI 都没装 vllm，顶层 import 会让 `core.llm` 整个 import 不了，
    1600 多个测试全崩——而那些测试跟 vLLM 一点关系都没有。
    """

    # **第一次调用会在进程内加载权重**（_engine 是惰性的），所以墙钟上限要能盖住
    # 加载时间，见 INPROC_TIMEOUTS 的依据。这里没有 HTTP，四个相位的值用不上。
    TIMEOUTS = INPROC_TIMEOUTS

    def __init__(self) -> None:
        self._model_path = os.environ.get("LLM_MODEL_PATH")
        self._lora_dir = os.environ.get("LORA_DIR")
        self._llm = None
        self._lora_ids: dict[str, int] = {}  # adapter 名 -> LoRARequest 的整数 id

    @property
    def _engine(self):
        """惰性加载 vllm.LLM。加载模型是几十秒级的重 IO，不能在 __init__ 里做
        （get_backend() 在 manifest 里问一句 model_name() 都会触发加载）。"""
        if self._llm is None:
            if not self._model_path:
                raise LLMError(
                    "LLM_MODE=local_inproc 需要 LLM_MODEL_PATH 指向本地权重目录，"
                    "现在没有设置。"
                )
            import vllm  # 延迟 import：见类文档

            self._llm = vllm.LLM(
                model=self._model_path,
                # LoRA 要在引擎创建时就开，之后不能改；没配 LORA_DIR 时不开，
                # 省掉 LoRA 的显存与调度开销。
                enable_lora=bool(self._lora_dir),
                max_model_len=int(os.environ.get("VLLM_MAX_MODEL_LEN", "8192")),
                gpu_memory_utilization=float(
                    os.environ.get("VLLM_GPU_MEMORY_UTILIZATION", "0.85")
                ),
            )
        return self._llm

    def model_name(self) -> str:
        return self._model_path or "vllm-unconfigured"

    def backend_id(self) -> str:
        return "local_inproc"

    def lora_for(self, physician: str | None) -> str | None:
        path = _resolve_lora_path(self._lora_dir, physician)
        return physician if path is not None else None

    def lora_dir(self) -> str | None:
        return self._lora_dir

    def comparability_warning(self) -> str | None:
        lora = f"LoRA: {self._lora_dir}（按医家挂 adapter）" if self._lora_dir else "LoRA: 未加载"
        return (
            f"后端：local_inproc（进程内 vLLM {self.model_name()}），非 DeepSeek。"
            f"{lora}。数字不可与 API 后端（DeepSeek）直接比较。"
        )

    def _lora_request(self, physician: str | None):
        """构造 vllm.lora.request.LoRARequest。id 必须在进程内稳定且唯一——
        同一个 adapter 每次给不同 id，vLLM 会当成不同 adapter 反复加载。"""
        path = _resolve_lora_path(self._lora_dir, physician)
        if path is None:
            return None
        from vllm.lora.request import LoRARequest  # 延迟 import

        if physician not in self._lora_ids:
            self._lora_ids[physician] = len(self._lora_ids) + 1
        return LoRARequest(physician, self._lora_ids[physician], str(path))

    def _sampling_params(
        self, temperature: float, max_tokens: int | None,
        schema: type[BaseModel] | None,
    ):
        from vllm import SamplingParams  # 延迟 import

        guided = None
        if schema is not None:
            from vllm.sampling_params import GuidedDecodingParams

            guided = GuidedDecodingParams(json=schema.model_json_schema())
        return SamplingParams(
            temperature=temperature,
            max_tokens=max_tokens if max_tokens is not None else self._default_max_tokens(),
            guided_decoding=guided,
        )

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        **kwargs,
    ) -> str:
        outputs = self._engine.chat(
            messages,
            sampling_params=self._sampling_params(temperature, max_tokens, schema),
            lora_request=self._lora_request(physician),
            **kwargs,
        )
        # chat() 按输入的 batch 返回列表；这里一次只喂一轮对话，所以取第 0 条。
        # 结构不符合预期时报错而不是靜默返回空串：空串会被 generate() 当成
        # "模型输出了不合 schema 的东西"重试三次，真实原因（vLLM 版本返回
        # 结构变了）就被埋掉了。
        if not outputs or not getattr(outputs[0], "outputs", None):
            raise LLMError(
                f"进程内 vLLM 返回结构不符合预期（拿到 {outputs!r}）：期望 "
                "[RequestOutput(outputs=[CompletionOutput(text=...)])]。"
                "大概率是 vllm 版本的返回结构变了，核对 vllm.LLM.chat 的文档。"
            )
        return outputs[0].outputs[0].text or ""


class ByokBackend(OpenAICompatBackend):
    """访问者自带 key（D1 第一层）。

    key **只活在这一次请求里**：存在实例上、随请求结束一起回收，不写环境变量
    （环境变量是进程级的，两个并发请求会互相串 key）、不落盘、不进日志、不进
    manifest。`model_name()` / 超时 / 重试语义全部继承，唯一的差别就是这把 key。
    """

    def __init__(self, api_key: str) -> None:
        super().__init__()
        self._byok_key = api_key

    def _api_key(self) -> str | None:
        return self._byok_key


def _auth_error_message(status: int | None, exc: Exception) -> str:
    """给访问者看的原话。**不含 key**——异常里本来也没有（SDK 不把 Authorization
    头放进异常），这里也绝不去把它拼进来。"""
    if status == 401:
        return "API key 认证失败（HTTP 401）：这把 key 不正确或已失效。"
    if status == 402:
        return "账户余额不足（HTTP 402）：这把 key 对应的账户需要充值后才能继续调用。"
    if status == 422:
        return f"请求参数被服务端拒绝（HTTP 422）：{exc}"
    return f"请求被服务端拒绝（HTTP {status}）：{exc}"


def check_api_key(api_key: str, base_url: str | None = None, timeout: float = 10.0) -> dict:
    """用官方的「查询余额」接口验一把 key，**不消耗任何 token**。

    GET {base}/user/balance，Authorization: Bearer <key>，返回
    `is_available`（这个账户还能不能调 API）+ `balance_infos[]`
    （currency / total_balance / granted_balance / topped_up_balance，都是字符串）。
    见 https://api-docs.deepseek.com/zh-cn/api/get-user-balance

    为什么要有这个：没有它，访问者只能靠"跑一次问诊"来知道 key 行不行，而那一次
    可能已经走完 S1/S2 才失败。返回里**绝不回显 key**。
    """
    import httpx

    base = (base_url or os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com").rstrip("/")
    # LLM_BASE_URL 允许带 /v1（OpenAI 兼容路径），而 /user/balance 挂在根上。
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        resp = httpx.get(
            f"{base}/user/balance",
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 - 网络问题跟 key 无效是两回事，要分开说
        return {"valid": None, "reason": f"验证请求没发出去（{type(e).__name__}）：{e}"}

    if resp.status_code == 401:
        return {"valid": False, "reason": _auth_error_message(401, None)}
    if resp.status_code != 200:
        return {"valid": None, "reason": f"验证接口返回 HTTP {resp.status_code}，无法判定。"}
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        return {"valid": None, "reason": "验证接口返回的不是 JSON，无法判定。"}

    infos = body.get("balance_infos") or []
    return {
        "valid": True,
        "is_available": bool(body.get("is_available")),
        # 只回余额，不回 key
        "balances": [
            {"currency": i.get("currency"), "total_balance": i.get("total_balance")}
            for i in infos
        ],
        "reason": "" if body.get("is_available") else "key 有效，但这个账户当前没有可用余额。",
    }


def get_backend() -> LLMBackend:
    """按 LLM_MODE 环境变量返回后端实例，默认 api。"""
    mode = os.environ.get("LLM_MODE", "api")
    if mode == "local":
        return VLLMBackend()
    if mode == "local_inproc":
        return VLLMInProcessBackend()
    if mode == "claude_cli":
        return ClaudeCLIBackend()
    if mode == "replay":
        # 惰性 import：core/llm_replay.py 要拿 cases_sha256（在 core.chain 里），
        # 模块级 import 会形成 llm → llm_replay → chain → llm 的环。
        from core.llm_replay import ReplayBackend

        return ReplayBackend()
    return OpenAICompatBackend()


_llm_singleton: LLMBackend | None = None
_llm_lock = threading.Lock()

# 逐请求的后端覆盖。ContextVar 而不是 thread-local：FastAPI 的同步端点跑在
# anyio 线程池里、上下文会被复制过去；但**裸 threading.Thread 不继承**，
# /api/consult/stream 的 worker 必须自己显式带上（见 api/main.py 里的
# _run_with_backend）。
_llm_override: ContextVar["LLMBackend | None"] = ContextVar("_llm_override", default=None)


@contextmanager
def use_llm(backend: "LLMBackend | None"):
    """在这个上下文里 get_llm() 返回指定后端。backend 为 None 时不覆盖。"""
    if backend is None:
        yield
        return
    token = _llm_override.set(backend)
    try:
        yield
    finally:
        _llm_override.reset(token)


def get_llm() -> LLMBackend:
    """惰性单例。模块底部不创建全局实例，避免模块导入时就要求环境变量齐全。

    加锁不是因为建后端对象重（它很轻），而是 manifest 里的 model/backend
    从这个对象问：两个线程各建一份、在途请求引用着不同的那份，同一批评测里
    两条记录就可能标着不同的后端。
    """
    override = _llm_override.get()
    if override is not None:
        # 逐请求覆盖（D1）：BYOK 用访问者自己的 key，超额降级用 ReplayBackend。
        # 覆盖走 ContextVar 而不是改单例——单例是进程级的，两个并发请求会互相
        # 串后端（一个用自己的 key、另一个跟着一起用）。
        return override
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
