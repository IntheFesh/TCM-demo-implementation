"""LLM 抽象层：prompt 加载/渲染 + 后端封装。所有 LLM 调用必须经过这里，禁止在业务代码里直接 import openai。"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import TypeVar

import yaml
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

PROMPTS_ROOT = Path(__file__).resolve().parent.parent / "prompts"

# 围栏不限定顶头顶尾、语言标记不限大小写：模型会写 ```JSON，也会在围栏前后
# 加一句说明。用 ^...$ 锚定的话，这两种情况都会整段原样返回，白烧一次重试。
_FENCE_RE = re.compile(r"```(?:[A-Za-z]+)?[ \t]*\n?(.*?)\n?```", re.DOTALL)


def load_prompt(name: str, version: str = "v1") -> dict:
    """读 prompts/{version}/{name}.yaml，返回 {system, notes} 字典。"""
    path = PROMPTS_ROOT / version / f"{name}.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_PLACEHOLDER_RE = re.compile(r"(?<!\$)\$\{?([A-Za-z_]\w*)\}?")


def render(template_str: str, **kwargs) -> str:
    """用 string.Template 渲染，**缺变量直接报错**。

    只用 safe_substitute 的话，yaml 里加了一个 $var 而调用方漏传时，变量会原样
    留在 prompt 里（模型看到一个字面的 "$refs"），所有测试照样通过——静默 bug。
    所以先检查缺失的变量再替换；替换本身仍用 safe_substitute（模板里的 $$ 之类
    不受影响）。
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

    LLMError 的子类——广义捕获 LLMError 的调用方照常工作；需要单独处理
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

    存在的理由：EOF 落在文本末尾并不足以判定截断。一段短到连这个 schema 的
    最短合法实例都编不出来的输出（比如只有 `{"` 两个字符），不可能是"生成到
    一半被 max_tokens 砍断"（砍断意味着已经生成了大量内容），更像是网络抖动/
    限流吐回了一个几乎空的响应，应该走正常重试，不该被判定为"重试无意义"
    直接放弃。

    不看 max_tokens 数字本身（理由同 `_looks_like_truncated_json`：token 数和
    字符数的换算在中文文本上不可靠），用 schema 自身能推出的下界——这个下界
    跟语言、跟 max_tokens 具体设了多少都无关，是结构上的硬约束：不管
    max_tokens 有多大，任何合法实例都不可能比它更短。
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


# 截断判定的绝对长度下限。**_min_plausible_output_length 单独当下限，对宽松
# schema 形同虚设**：S2Elements 的两个字段都有默认值、没有 required，最短合法
# 实例就是 "{}" = 2 字符——schema 越宽松这个下限就越趋近 2，而一次几乎空的返回
# （`{"`）也是 2 字符，"2 < 2" 为 False，完全挡不住。会调用 generate() 的
# schema 里有一大批都是这样（S1Normalize、S2Elements、CaseStructured、
# CaseTripleExtraction、SegmentPatients……字段全部可选或没有必填约束）。
#
# 真正该问的不是"这个 schema 最短能有多短"，是"这次返回相对于一次被真实砍断的
# 生成短到什么程度"——这跟 schema 松紧无关：砍断的前提是已经生成了相当多内容，
# 任何 schema 的真实截断都不会只产出两位数字符。那不是"被砍断"，是"几乎没输出"，
# 只可能是 API 抖动/限流，应该重试。跟 schema 下限取较大值：宽松 schema 用这个
# 绝对下限兜底，严格 schema（S3Syndrome 的结构下限是 195）用它自己更高的下限。
TRUNCATION_MIN_LENGTH = 100


def _truncation_length_threshold(schema: type[BaseModel]) -> int:
    """截断判定的实际长度门槛：schema 结构下限和绝对下限取较大值，见
    TRUNCATION_MIN_LENGTH 的注释。"""
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
    注释：单独用 schema 下限在宽松 schema 上形同虚设（S2Elements 的结构下限
    只有 2 字符，一个 2 字符的近空返回照样能漏过去）。
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

    `deadline` 是这一层最要紧的东西：**httpx 的 read 超时是"单次 socket 读"的
    上限，不是整个响应的期限**。中间任何一跳（CDN、网关、反代）只要每隔几十秒
    吐一个字节/一个保活帧，每次读都不超时，整个请求可以无限期挂住——而重试以
    "这次调用返回了"为前提，挂住时重试逻辑永远不会触发。`deadline` 从外面给
    整次调用封一个墙钟上限，超了就抛 LLMCallTimeout 走重试。
    """

    connect: float
    read: float
    write: float
    pool: float
    deadline: float

    def httpx_timeout(self):
        """惰性 import httpx：没装 openai/httpx 的机器也要能 import core.llm。"""
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


# 云端 API（DeepSeek）：读 600 秒、墙钟 900 秒；连接 15 秒、发请求 30 秒
# （prompt 最大几十 KB）。
#
# 读超时要盖住 S3 的一整次生成：S3 的提示词带知识块、产出是五步链结构化输出，
# 而且这一步**开着思考**（`thinking_by_step`），推理 token 也算在这一次响应里。
# 非流式调用的整个响应是一次 socket 读，读超时短于整代生成时间的话，一次正常的
# S3 会被判成"网络故障"然后重试三次、每次都在同一处超时——**表现是"三倍的钱换
# 一个超时错误"**，而不是"快速失败"。
#
# 读超时放宽不等于放弃兜底：`deadline` 仍然是墙钟上限（read 的 1.5 倍），堵的是
# "对方细水长流地吐字节"，跟读超时设多大无关（见 CallTimeouts）。
API_TIMEOUTS = CallTimeouts(connect=15.0, read=600.0, write=30.0, pool=15.0, deadline=900.0)
# 本地 vLLM server：权重是 server 自己启动时加载的（scripts/start_vllm.sh），
# 但**首个请求**要等它把 CUDA graph / 预热做完；排队时单个请求也可能等很久。
# 读 600 秒、墙钟 900 秒。
LOCAL_SERVER_TIMEOUTS = CallTimeouts(connect=10.0, read=600.0, write=30.0, pool=10.0, deadline=900.0)
# 进程内 vLLM：**第一次调用会在进程内加载权重**（VLLMInProcessBackend._engine
# 是惰性的），模型越大越慢。墙钟给 1800 秒，超过它就当作卡住。
# 这里没有 HTTP，四个相位的值用不上，填同一个数只是为了 dataclass 完整。
INPROC_TIMEOUTS = CallTimeouts(connect=1800.0, read=1800.0, write=1800.0, pool=1800.0,
                               deadline=1800.0)


# 单次输出上限的默认值，**按这次调用开不开思考分三档**（取值逻辑在
# LLMBackend._default_max_tokens）：关思考 8192、开思考 32768、effort=max 65536。
#
# 关思考这一档：4096 的上限会截断 S0 抽多病人粗段的 JSON，而截断的 JSON 回灌
# 重试也只会以同样方式再截断三次。
DEFAULT_MAX_TOKENS = 8192
# 开着思考的调用要另算：**max_tokens 同时盖住不可见的 reasoning tokens**，不是只盖
# 可见输出。沿用 8192 的话，S3 的可见输出会被推理过程挤掉一大截——而 8192 这个
# 数字看起来完全够用，所以症状是"输出莫名截断"，看不出跟推理有关。
#
# 思考是**按步**开的（见 STEP_THINKING），这一档只落在开思考的 S3 上：它既要装
# 推理过程又要装完整方药。DeepSeek 文档里思考模式不设 max_tokens 时默认 64K，
# 这里取它的一半：既远离截断，又保留"输出失控时还能被发现"这个上限本来的作用。
THINKING_MAX_TOKENS = 32768
# `reasoning_effort=max` 那一档的上限：max 档的推理过程本身就能吃掉几万 token，
# 沿用 32768 会让推理没写完就撞上限。65536 远低于模型自身的输出上限——这不是
# "顶格给"，是给 max 档的推理过程留出它真正需要的量。
MAX_EFFORT_MAX_TOKENS = 65536
# 已知的推理模型。**只在"这次调用没说开不开思考"时用它兜底**：那时走的是 API 自己
# 的默认，而推理模型的 API 默认就是开思考。按名字判是因为服务端不给这个字段，
# 猜错的代价只是上限偏大或偏小（`LLM_MAX_TOKENS` 能覆盖）。新模型上线时加一行。
REASONING_MODELS = frozenset({"deepseek-v4-pro"})


# ---------- 思考模式按步控制 ----------
#
# DeepSeek 的推理模型**默认开思考**（`thinking.type=enabled`，effort=high）。这带来
# 两个后果，都要按步处理：
#   ① 慢且贵：S1（症状标准化）和 S2（证素推断）是结构化抽取，思考对它们没有增益，
#      只让调用变慢变贵；
#   ② **思考模式下 temperature 不生效**——同样的输入会给出不同的输出，
#      ε（噪声地板）和 fixture 的可复现性随之一起失效。
#
# 所以：抽取类的步骤一律关思考；真正需要推理的 S3（开方）默认开，
# 但留一个环境变量能整体关掉，好让"关思考版本"作为一组独立的对照数字去跑。
#
# **这张表是唯一的一处实现**：各调用点自己写 `thinking="disabled"` 的话，新增一个
# 步骤时很容易漏掉，而漏掉的表现只是"那一步莫名变慢"，很难追到这里。
STEP_THINKING: dict[str, str] = {
    "s1": "disabled",
    "s2": "disabled",
    # S1+S2 合一步（S1S2_MERGED=1）。跟 s1/s2 一样关思考：合的是两个结构化抽取，
    # 合起来之后开思考等于换了实验条件。
    "s1s2": "disabled",
    "followup": "disabled",
    "residual": "disabled",
    "react": "disabled",
    "s3": "enabled",
}
S3_THINKING_DEFAULT = "enabled"
# `reasoning_effort`：可配，不设时按检索方式取默认（见 s3_reasoning_effort）。
# 官方四档，顺序是"想得越多越贵越慢"。未知值不静默走默认——拼错一档的表现是
# "这次悄悄用了别的设置"，跟没设一样看不出来（同 thinking_for 未知 step 那条）。
REASONING_EFFORTS = ("low", "medium", "high", "max")
S3_REASONING_EFFORT_ENV = "S3_REASONING_EFFORT"
# top3 系检索下默认 low。确定的成本来自"开着思考"本身，不来自档位名：档位之间
# 的耗时差异在测量噪声以内，更高的档位也没有可验证的质量收益来支撑它的费用。
# 思考本身仍然默认开着（`S3_THINKING_DEFAULT`）——关思考会让输出质量坍缩，
# 跟选哪一档是两回事。要对照各档质量，用 `S3_REASONING_EFFORT` 显式指定档位
# 跑评测，比较证型、主方、验证器一次通过率与 rule_refs 完整率。
S3_REASONING_EFFORT_TOP3 = "low"
# full_context 下默认 medium，比 top3 高一档：这一模式的输入本来就大，而 S3 是
# 整条链上唯一开思考的一步，留一档缓冲，作为省成本与保质量之间更保守的折中。
S3_REASONING_EFFORT_FULL_CONTEXT = "medium"


# S3 这一步产出哪种形状。
#   derived    —— 演绎推导：五步链 + 医理规则依据（S3Derived），
#                  prompt 里**没有任何医案**，检索只在验证通过之后做佐证
#   structured —— 五位医家融合成**一份**结构化诊断（S3Structured，五步链、一张方），
#                  推导前就把检索到的医案摆进 prompt 当参考
#   legacy     —— 各家各自一份 S3Syndrome（2–3 个候选方），多列并置
# **默认 derived**：产品的定位是「按医理药理演绎推导，医案退为事后佐证」，
# 不是「检索几位医家再模仿」——structured 与 legacy 都在推导之前就把医案摆进
# prompt，默认走哪一档就是把产品做成哪一种。structured/legacy 保留成两档：
# 消融实验（eval/ablation/）拿它们当对照组，没有它们，演绎推导就没有可比的基线。
S3_MODES = ("derived", "structured", "legacy")
S3_MODE_ENV = "S3_MODE"
S3_MODE_DEFAULT = "derived"


def s3_mode() -> str:
    """S3 这一步产出哪种形状：`S3Derived`、`S3Structured` 还是各家的 `S3Syndrome`。

    **拼错一档要报错，不静默走默认**——跟 `S3_BEST_OF_N` 同一条理由：这个旋钮
    决定的不是"快一点慢一点"，是**产出的形状**。悄悄走错一档的表现是
    "怎么又出了五份答案"，而那时人会去找前端的 bug。

    `s3_thinking` / `s3_reasoning_effort` 那两个未知值只打一句 stderr 就走默认，
    是因为它们错了只影响成本与质量；这一个错了下游拿到的是另一种 schema。
    """
    raw = (os.environ.get(S3_MODE_ENV) or "").strip().lower()
    if not raw:
        return S3_MODE_DEFAULT
    if raw not in S3_MODES:
        raise ValueError(
            f"{S3_MODE_ENV}={raw!r} 不认识，只能是：{' / '.join(S3_MODES)}。"
            "这个旋钮决定 S3 产出哪种 schema，不静默按默认处理。"
        )
    return raw


def s3_thinking() -> str:
    """S3 这一步开不开思考。`S3_THINKING=disabled` 关掉——**关掉之后跑出来的数字
    跟默认配置下的不可比**，manifest 会带上这句话。"""
    value = (os.environ.get("S3_THINKING") or S3_THINKING_DEFAULT).strip().lower()
    if value not in ("enabled", "disabled"):
        print(f"[llm] S3_THINKING={value!r} 只认 enabled / disabled，"
              f"这次按默认 {S3_THINKING_DEFAULT} 处理", file=sys.stderr)
        return S3_THINKING_DEFAULT
    return value


def s3_reasoning_effort() -> str:
    """S3 这一步想多久。`S3_REASONING_EFFORT` 显式指定优先；不设时**按检索方式
    取默认**——full_context 下 `S3_REASONING_EFFORT_FULL_CONTEXT`（medium），
    top3 系下 `S3_REASONING_EFFORT_TOP3`（low），理由见这两个常量的注释。

    检索方式用函数内 import 取：这个模块是全项目最底层的一层，
    在模块顶层 import core.retrieval_hybrid 会把"检索"这条依赖倒灌进"调模型"。
    """
    raw = (os.environ.get(S3_REASONING_EFFORT_ENV) or "").strip().lower()
    from core.retrieval_hybrid import DEFAULT_MODE, effective_mode

    default = (S3_REASONING_EFFORT_FULL_CONTEXT if effective_mode() == DEFAULT_MODE
               else S3_REASONING_EFFORT_TOP3)
    if not raw:
        return default
    if raw not in REASONING_EFFORTS:
        print(f"[llm] {S3_REASONING_EFFORT_ENV}={raw!r} 只认 {'/'.join(REASONING_EFFORTS)}，"
              f"这次按默认 {default} 处理", file=sys.stderr)
        return default
    return raw


# S3 采样几次、挑分最高的那次（best-of-N）。1 = 只采一次。
S3_BEST_OF_N_ENV = "S3_BEST_OF_N"
#: 默认 1：把不合规的方挑掉这件事由**符号验证器**负责——验证器拿本体原文判
#: veto/revise，判据可核、反例可读，比"分最高的那一次"强。best-of-N 叠加上去
#: 等于同一件事付两次钱：一次问诊的 S3 调用数与墙钟都乘以 N，而验证器的修订
#: 闭环本身还会再开调用。旋钮保留（`S3_BEST_OF_N=3` 可用），消融实验拿它当对照组。
S3_BEST_OF_N_DEFAULT = 1


def s3_best_of_n() -> int:
    """S3 采几次、挑分最高的那次。**默认 1**（理由见 `S3_BEST_OF_N_DEFAULT`）。

    放在这个模块而不是 core/chain.py：它跟 `S3_THINKING` / `S3_REASONING_EFFORT`
    是同一族旋钮（都决定"S3 这一步怎么调模型"），而 `core/usage.py` 算
    「一次问诊几次调用」时也要读它——usage 依赖 llm 这条边本来就有（同族常量），
    依赖 chain 会把"推理链"倒灌进"额度账本"。

    非正整数不静默按默认处理——**它直接决定钱**：把 3 写成 30，一次问诊的 S3
    调用数就翻十倍，而表现只是"这次好慢"。
    """
    raw = (os.environ.get(S3_BEST_OF_N_ENV) or "").strip()
    if not raw:
        # FAST_MODE 下恒为 1，不随 S3_BEST_OF_N_DEFAULT 变：best-of-N 是 S3 调用数的
        # 直接倍数，默认值调高时 FAST_MODE 不跟着降的话就名不副实——"以为省了预算、
        # 实际还在花"正是 fast_mode_enabled() 的文档里点名要防的那件事。
        # 显式设了 S3_BEST_OF_N 的话尊重它（显式 > 兜底，跟 EVAL_MODE 那条一致）。
        from core.followup import fast_mode_enabled

        return 1 if fast_mode_enabled() else S3_BEST_OF_N_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        n = 0
    if n < 1:
        print(f"[llm] {S3_BEST_OF_N_ENV}={raw!r} 不是 ≥1 的整数，"
              f"这次按默认 {S3_BEST_OF_N_DEFAULT} 处理", file=sys.stderr)
        return S3_BEST_OF_N_DEFAULT
    return n


#: S1（症状标准化）与 S2（证素推断）合成一次调用。
#:
#: 为什么可以合：S2 的输入**只有** S1 的输出（症状 + 舌 + 脉），没有第三方数据要
#: 在两步之间取；而两步都是"照着给定词表做结构化抽取"，同一次调用里做完不改变
#: 任何一步的判据。省下来的是一整次往返（连同它的排队与连接开销）。
#:
#: 「S1 全局只跑一次，所有医家共用」（docs/ARCHITECTURE.md §3）合一之后照样成立
#: ——连 S2 都只跑一次了。但追问之后要**只重跑 S2**（`infer_elements`），那条路径
#: 不合并：追问改变的是症状集合的后验，症状标准化不必重做。所以两个函数都留着，
#: 合一只发生在链路开头那一次。
#:
#: **默认关**，打开用 `S1S2_MERGED=1`。关着的理由是次序（详见
#: `core.chain.normalize_and_infer_merged` 的文档）：**危重症状的拦截必须发生在
#: 证素推断之前**（docs/ARCHITECTURE.md §3），而合一之后证素推断跟症状标准化在
#: 同一次调用里完成——拦截最早只能早到"那一次调用之前"，S1 归一之后才露出来的
#: 危重词（原文「呕吐咖啡色物」→ 归一「呕血」）就挡不住证素推断了。省一次调用，
#: 换掉的是一条结构性保证，所以这条路完整实现、有测试、可以打开，但默认不开。
#:
#: 消融实验拿 `S1S2_MERGED=1` 量"合一之后证素质量变没变"，两条路都保留。
S1S2_MERGED_ENV = "S1S2_MERGED"
S1S2_MERGED_DEFAULT = False
_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")


def s1s2_merged() -> bool:
    """S1+S2 合成一次调用还是分两次。

    认不出的值**只打一句 stderr 走默认**，不抛异常——跟 `S3_MODE` 分开对待是
    有意的：这个旋钮不改下游拿到的形状（两条路都产出同一对
    `(S1Normalize, S2Elements)`），只改调用次数；而 `S3_MODE` 改的是 schema，
    拼错一档静默走默认的表现是"怎么又出了五份答案"。
    """
    raw = (os.environ.get(S1S2_MERGED_ENV) or "").strip().lower()
    if not raw:
        return S1S2_MERGED_DEFAULT
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    print(f"[llm] {S1S2_MERGED_ENV}={raw!r} 只认 "
          f"{'/'.join(_TRUE_WORDS)} 或 {'/'.join(_FALSE_WORDS)}，"
          f"这次按默认 {S1S2_MERGED_DEFAULT} 处理", file=sys.stderr)
    return S1S2_MERGED_DEFAULT


def thinking_for(step: str) -> dict[str, str | None]:
    """某一步该传的思考参数，直接 `**` 进 `generate()`。

    未知的 step 名不静默走默认值，而是在 stderr 明说——拼错一个步骤名的表现是
    "那一步悄悄用了别的设置"，跟没设一样看不出来。
    """
    if step not in STEP_THINKING:
        print(f"[llm] thinking_for({step!r})：没有这个步骤，按不指定处理（走 API 默认）。"
              f"可用：{sorted(STEP_THINKING)}", file=sys.stderr)
        return {"thinking": None, "reasoning_effort": None}
    mode = s3_thinking() if step == "s3" else STEP_THINKING[step]
    return {
        "thinking": mode,
        # effort 只在开着思考时有意义（关了思考传它 = 一个不生效却出现在 manifest
        # 里的实验条件）。取值问 s3_reasoning_effort() 这一处。
        "reasoning_effort": s3_reasoning_effort() if mode == "enabled" else None,
    }


def thinking_by_step() -> dict[str, str]:
    """整次跑的思考设置，写进 manifest。**换了这张表 = 数字不可比**，跟换模型同级。"""
    return {step: (s3_thinking() if step == "s3" else mode)
            for step, mode in STEP_THINKING.items()}


# 进程内**同时在途**的 LLM 调用数上限。**跟 MAX_CONCURRENT_CONSULTS 是两件事**：
# 那个限"同时进行几次问诊"，这个限"同时打到模型的请求数"。多位医家并发推理时
# 两者相乘——问诊槽数 × 医家数路请求同时打 API，很容易撞上速率限制（429）。
# 只留一个闸拦不住：把问诊槽调小只会让排队变长，而每次问诊仍然并发开多路；
# 把医家改回串行又等于放弃并发的收益。
DEFAULT_MAX_INFLIGHT = 6
_inflight_sem: threading.BoundedSemaphore | None = None
_inflight_limit: int | None = None
_inflight_lock = threading.Lock()


def llm_max_inflight() -> int:
    """当前的在途上限。每次现读环境变量——上限是部署侧设置，不是逐请求行为开关，
    读环境变量没问题（跟 MAX_CONCURRENT_CONSULTS 同一条理由）。"""
    raw = os.environ.get("LLM_MAX_INFLIGHT")
    if not raw:
        return DEFAULT_MAX_INFLIGHT
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value <= 0:
        print(f"[llm] LLM_MAX_INFLIGHT={raw!r} 不是正整数，按默认 {DEFAULT_MAX_INFLIGHT} 处理",
              file=sys.stderr)
        return DEFAULT_MAX_INFLIGHT
    return value


def inflight_semaphore() -> threading.BoundedSemaphore:
    """在途闸。上限变了就重建——上限只在进程启动或测试里变，重建时可能有调用在飞，
    那一次的计数会丢，这是知道并接受的代价：为一个只在启动时读的值维护一套可变
    信号量不值得。"""
    global _inflight_sem, _inflight_limit
    limit = llm_max_inflight()
    with _inflight_lock:
        if _inflight_sem is None or _inflight_limit != limit:
            _inflight_sem = threading.BoundedSemaphore(limit)
            _inflight_limit = limit
        return _inflight_sem


# 这一次问诊/这一批跑的重试统计。ContextVar 而不是模块级计数器：并发的两次问诊
# 各自要拿到自己的数，一个全局计数器会把别人的重试算到自己头上。放的是**可变字典**，
# 所以并发医家线程（走 copy_context）里加的数，父线程读得到——ContextVar 拷贝的是
# 引用不是内容。
_retry_stats: ContextVar[dict | None] = ContextVar("_retry_stats", default=None)


def new_retry_stats() -> dict:
    """给这一次调用链开一份新的重试统计并装进 ContextVar，返回那个字典本身。
    调用方（consult）拿着它写进 manifest。"""
    stats = {"total": 0, "rate_limited": 0, "timeout": 0, "other": 0}
    _retry_stats.set(stats)
    return stats


def current_retry_stats() -> dict | None:
    """这一次调用链到此为止的重试统计；没开过统计就是 None。manifest 从这里取，
    调用方不用把它一路当参数传下去——传参会让每个 `_build_manifest` 调用点都得记得
    带上它，漏一处就是那条路径上的 manifest 少一个字段。"""
    stats = _retry_stats.get()
    return dict(stats) if stats is not None else None


# 前缀缓存命中读数。**跟重试统计同一套机制**（ContextVar + 可变字典），
# 理由也一样：并发的各位医家在各自线程里累加，父线程（consult 组 manifest）读得到，
# 而并发的两次问诊各自拿到自己的数。合成一个模块级计数器会把别人的命中算到自己头上。
#
# 累加而不是"记最后一次"：一次问诊有多次调用，manifest 要的是整次的命中率。
# 只留最后一次的话，S1/S2 那几次短调用（几乎全未命中）会把 S3 那次大命中盖掉。
_usage_stats: ContextVar[dict | None] = ContextVar("_usage_stats", default=None)

#: DeepSeek 响应 usage 里的缓存字段名（官方文档
#: https://api-docs.deepseek.com/guides/kv_cache/）。写成常量是因为
#: OpenAICompatBackend 取它、测试断言它，两处引同一个名字。
CACHE_HIT_FIELD = "prompt_cache_hit_tokens"
CACHE_MISS_FIELD = "prompt_cache_miss_tokens"


def new_usage_stats() -> dict:
    stats = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0,
             "reasoning_tokens": 0, "completion_tokens": 0, "n_reported": 0}
    _usage_stats.set(stats)
    return stats


def current_usage_stats() -> dict | None:
    """这一次调用链到此为止的 usage 累加；没开过统计、或者一次都没有后端报过
    这些字段，就是 None。

    `n_reported == 0` 也返回 None（不是一份全 0 的字典）：全 0 会被读成
    "跑了但一次没命中"，而真相是"这个后端不报这个数"（replay / vLLM 都不报）。
    两件事必须分得开。
    """
    stats = _usage_stats.get()
    if stats is None or not stats.get("n_reported"):
        return None
    return dict(stats)


def record_usage(usage) -> None:
    """把一次响应的 usage 累加进当前统计。usage 可以是 SDK 对象或 dict。

    三类信号（缓存命中、completion_tokens、reasoning_tokens）**互相独立**，谁报了
    就记谁，不拿"有没有缓存字段"当"这条 usage 有没有东西可记"的总闸门：报
    `completion_tokens_details.reasoning_tokens` 但不报 DeepSeek 缓存字段的后端
    （比如其它走扩展思考的 OpenAI 兼容后端）照样要记下推理 token，不然 manifest
    上的 reasoning_tokens 会恒为 None。

    `n_reported` 只要**这三类里有任意一类真的取到值**就算一次；三类都没取到
    （比如 usage 是个空对象）才不计——它回答的是"这次响应有没有报任何 usage
    信息"，不是"报没报 DeepSeek 缓存字段"，这是两个问题。
    """
    stats = _usage_stats.get()
    if stats is None or usage is None:
        return

    def _get(name):
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    reported = False

    hit, miss = _get(CACHE_HIT_FIELD), _get(CACHE_MISS_FIELD)
    if hit is not None or miss is not None:
        stats["prompt_cache_hit_tokens"] += int(hit or 0)
        stats["prompt_cache_miss_tokens"] += int(miss or 0)
        reported = True

    completion = _get("completion_tokens")
    if completion is not None:
        stats["completion_tokens"] += int(completion or 0)
        reported = True

    details = _get("completion_tokens_details")
    if details is not None:
        reasoning = (details.get("reasoning_tokens") if isinstance(details, dict)
                     else getattr(details, "reasoning_tokens", None))
        if reasoning is not None:
            stats["reasoning_tokens"] += int(reasoning or 0)
            reported = True

    if reported:
        stats["n_reported"] += 1


def _record_retry(error: BaseException) -> None:
    """记一次重试。分三类而不是只记总数：429（该降并发）、超时（该查网络或调超时）、
    其它（多半是模型输出格式问题）——这三种的处置完全不同，合成一个数就没法处置。"""
    stats = _retry_stats.get()
    if stats is None:
        return
    status = _status_code_of(error)
    if status == 429:
        kind = "rate_limited"
    elif isinstance(error, TimeoutError):
        kind = "timeout"
    else:
        kind = "other"
    stats["total"] = stats.get("total", 0) + 1
    stats[kind] = stats.get(kind, 0) + 1


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
    # 覆盖所有后端。
    TIMEOUTS: CallTimeouts = API_TIMEOUTS
    #: 这个后端能不能边生成边把片段吐出来。
    #:
    #: **默认 False，而且 `generate()` 只在 True 时才把 `on_delta` 往下传。**
    #: 两条理由：一是有的后端（比如进程内 vLLM）拿不到增量，多传一个
    #: 关键字参数只会变成 TypeError 或被 `**kwargs` 静默吞掉；二是"请求了流式"
    #: 和"真的流式了"必须分得开——分不开的话前端转着圈等，而 manifest 里写着
    #: streaming=True，没人能看出问题在哪。
    SUPPORTS_STREAMING: bool = False
    #: 流式是真的增量还是拿已有的整段文本切出来的（回放后端）。
    #: 回放模式下切出来的"流式"看起来跟真的一样，所以必须有一个字段说清楚。
    STREAMING_IS_SIMULATED: bool = False
    # 传输类错误（超时、429、连接断）两次重试之间的等待秒数，按重试序号取。
    # 只对传输错误退避：校验错误是模型输出格式不对，回灌错误信息立刻重问才有
    # 意义，等一秒不会让它答得更对；而传输错误零间隔立刻重试的话，一个 429 会
    # 变成三个连续的 429。测试里把它 monkeypatch 成 (0, 0)，不真等。
    #
    # **指数 + 抖动**：1s / 2s / 4s，各带 ±20% 的随机扰动。指数是因为 429 说明对面
    # 正忙，线性等一秒多半还是 429；抖动是因为多位医家是**同时**出发的，一起撞 429
    # 就会在同一时刻一起重试，形成一波一波的同步冲击（thundering herd）。
    RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
    RETRY_JITTER = 0.2
    _sleep = staticmethod(time.sleep)  # 留个缝给测试换掉，不真睡
    _random = staticmethod(random.uniform)  # 同上：测试要能固定抖动

    def _backoff_seconds(self, attempt: int) -> float:
        """第 attempt 次重试前等多久。测试把 RETRY_BACKOFF_SECONDS 设成 (0, 0) 时
        这里返回 0（0 的 ±20% 还是 0），所以离线测试不会真的睡。"""
        base = self.RETRY_BACKOFF_SECONDS[min(attempt, len(self.RETRY_BACKOFF_SECONDS) - 1)]
        if base <= 0:
            return 0.0
        return base * (1.0 + self._random(-self.RETRY_JITTER, self.RETRY_JITTER))

    def timeouts(self) -> CallTimeouts:
        """本次调用生效的超时。环境变量 `LLM_TIMEOUT_SECONDS` 优先。
        解析不了就用后端默认值，并在 stderr 明说——静默回退到默认值会让人以为
        自己设的值生效了。"""
        raw = os.environ.get("LLM_TIMEOUT_SECONDS")
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

    def _default_max_tokens(self, thinking: str | None = None,
                            reasoning_effort: str | None = None) -> int:
        """这次调用没有显式传 max_tokens 时用多少。`LLM_MAX_TOKENS` 优先。
        **放在基类**：每个后端都要回答这个问题，各写一份的话这条会只在其中一个
        后端上生效。

        **按这次调用的 thinking 设置分档，不是按模型名。** 开着思考时 max_tokens
        同时盖住不可见的 reasoning tokens，8192 留给可见输出的会少一大截。思考是
        **按步**设的，按模型名分档的话，关了思考的 S1/S2/追问/ReAct 白拿大额度，
        而唯一开思考的 S3 反倒要用同一个数去装推理过程 + 完整方药。

        `thinking=None` 表示这次没指定、走 API 自己的默认——推理模型的 API 默认就是
        开思考，所以那时按"开"给；非推理模型按"关"给。

        判据放在代码里而不是让人去 .env 里记一个数：一个"必须手动设对、否则静默
        截断"的环境变量迟早有一次会忘，而忘了的代价是一整批调用的钱。
        """
        env = os.environ.get("LLM_MAX_TOKENS")
        if env:
            return int(env)
        if thinking is None:
            thinking = "enabled" if self.model_name() in REASONING_MODELS else "disabled"
        if thinking != "enabled":
            return DEFAULT_MAX_TOKENS
        # `effort=max` 单独再高一档。max_tokens 盖住的是 reasoning + 可见输出
        # 两部分，而 max 档的推理过程本身就能吃掉几万 token——沿用 32768 的后果是
        # **推理没写完就撞上限**，而那表现为一个在末尾处解析失败的 JSON
        # （_looks_like_truncated_json 会把它认出来，但那已经是白烧一次调用之后了）。
        return MAX_EFFORT_MAX_TOKENS if reasoning_effort == "max" else THINKING_MAX_TOKENS

    def abort_in_flight(self) -> None:
        """墙钟超时之后清理这个后端里挂着的东西。默认什么都不做；
        OpenAICompatBackend 覆盖成"丢掉那个 HTTP 客户端"，好让卡住的那次读
        随着连接池被回收而死掉，不然重试会排在同一个坏连接后面。"""

    def _complete_within_deadline(
        self, messages: list[dict], temperature: float, max_tokens: int | None,
        schema: type[BaseModel] | None, physician: str | None, deadline: float, **kwargs,
    ) -> str:
        """在墙钟上限内跑一次 `_complete`，超了抛 LLMCallTimeout。

        **为什么要这一层**：HTTP 客户端的 read 超时管的是"单次 socket 读"，对方
        每隔几十秒吐一个字节就永远不触发。`MAX_ATTEMPTS=3` 的前提是"这次调用
        返回了"，挂住不返回时重试逻辑根本没机会跑。

        实现是"工作线程 + join(deadline)"：blocking 的 socket 读没法从外面取消，
        所以超时后**不等它**（daemon 线程，进程退出不会被它拖住），只把它丢在
        后台自己去死（`abort_in_flight()` 顺手断掉连接池加速这件事）。代价是
        最坏情况下有几个僵住的线程，换来的是主流程一定能往前走。
        顺带一个好处：主线程这会儿卡在 `join()` 上而不是卡在 C 层的 read 里，
        Ctrl-C 立刻生效。
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

        # 工作线程要**带着调用方的 ContextVar 上下文**跑。
        #
        # `threading.Thread` 起的线程拿到的是一份空 Context，调用方设的 ContextVar
        # 在里面看不见：`record_usage()` 在 `_complete` 里调，而 `_complete` 就在这个
        # 工作线程里跑——不带上下文的话它读到的 `_usage_stats` 永远是默认的 None，
        # manifest 里的 cache_hit_ratio 恒为 None，而日志里看不出任何错。
        #
        # `copy_context().run()` 拷的是 ContextVar → 值的映射，值本身是**同一个对象**
        # ——所以工作线程往那个可变字典里加的数，调用方读得到（跟 chain.py 给各位
        # 医家线程用的是同一套办法）。重试统计（`_record_retry`）在 generate() 的
        # 重试循环里调，那是调用方线程，不经过这里。
        ctx = copy_context()

        def _run_in_context() -> None:
            ctx.run(_run)

        # 在途闸**在主线程取、在主线程放**，不放在工作线程里：墙钟超时之后工作线程
        # 被丢下不管（daemon），许可要是在它手里就**永远不会还**——攒够
        # LLM_MAX_INFLIGHT 次挂死，整个进程的 LLM 调用全部死锁
        # （tests/test_llm_inflight.py 有一条"故意挂住"的用例钉住这一点）。
        #
        # 所以语义是"同时**等待**的调用数"：join 一返回就还许可，哪怕那个被丢下的
        # 工作线程还在飞。代价是超时之后短暂地可能超过上限——但紧跟着的
        # `abort_in_flight()` 会把连接池掐掉、让那次读死掉，所以窗口很短。
        # 拿"短暂超限"换"永不死锁"。
        #
        # 排队的时间不算进墙钟预算：许可是在 join 开始**之前**取的，取到才开表。
        sem = inflight_semaphore()
        sem.acquire()
        try:
            worker = threading.Thread(target=_run_in_context, name="llm-call", daemon=True)
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
        finally:
            sem.release()
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

        max_tokens 是显式参数不是塞进 **kwargs：后端自己会算一个默认值传给 SDK，
        调用方要是再通过 kwargs 传一份同名参数，就会在传给 SDK 时撞上"重复关键字
        参数"。None 表示"用这个后端自己的默认值"（见 `_default_max_tokens`），
        不是"不设上限"。

        schema / physician 同样是显式参数、同样不塞 **kwargs，理由也一样：
        OpenAICompatBackend 把 `**kwargs` 原样转给 OpenAI SDK，多一个它不认的
        关键字参数就是 TypeError。两个参数都只有本地后端用得上：
          - schema：vLLM 的 guided_decoding 要拿 `schema.model_json_schema()`
            从解码层保证输出合法（比"prompt 里塞 schema + json_object"这种
            弱约束强）。API 后端拿不到这个能力，如实忽略。
          - physician：本地后端可以给每位医家挂一个按医家训练的 LoRA adapter，
            vLLM server 支持按请求切 adapter。用显式参数而不是线程局部/全局
            "当前医家"：LoRA 选错会让"张锡纯用的是他自己的 LoRA"这句声称
            变成假的，而隐式上下文一旦哪个调用点忘了设，错的是静默的——
            看 run_physician 这一行看不出
            adapter 是从哪来的。api/main.py 是多线程并发问诊，模块级"当前
            医家"还会有竞态。知道医家是谁的调用点（core/chain.py 的
            run_physician、core/react.py 的 run_react）自己报出来，最直接。
        """
        raise NotImplementedError

    def streaming_note(self) -> str | None:
        """这次为什么没有真流式 / 流式是模拟的。能真流式就返回 None。

        写成一句人话而不是一个布尔：manifest 与前端都要能直接显示它，
        而"这个后端不支持"和"支持但是模拟的"要修的东西完全不同。
        """
        if not self.SUPPORTS_STREAMING:
            return (f"后端 {self.backend_id()} 不支持流式输出，"
                    "这次是一次性返回（前端不会有增量，不是卡住了）")
        if self.STREAMING_IS_SIMULATED:
            return (f"后端 {self.backend_id()} 的"
                    "流式是把已有的整段文本切开发的（模拟），不是模型边生成边吐——"
                    "耗时与首字延迟都不反映真实推理")
        return None

    @abstractmethod
    def model_name(self) -> str:
        """实际使用的模型名，写进 manifest。

        **禁止伪装成别的模型。** manifest 是报告里"这个数字是怎么来的"的唯一
        凭据，写错等于伪造实验条件——本地模型跑出来的分数标成 DeepSeek，
        会让这个数被误放进跟 DeepSeek 的对比表里。
        """
        raise NotImplementedError

    @abstractmethod
    def backend_id(self) -> str:
        """api / local / local_inproc / replay，写进 manifest。"""
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
        """本次运行配置的 LoRA 根目录，None = 没配。manifest 记它是为了说明
        "这次跑的 adapter 是从哪来的"——per-physician 的实际 adapter 记在每位
        医家的结果里（见 core/chain.py::run_physician 的 "lora" 字段）。"""
        return None


    def replay_info(self) -> dict | None:
        """这一次的输出是不是回放的录制结果；None = 实时调用。

        非 None 时至少带 recorded_at / model / git_commit（见
        core/llm_replay.py::ReplayBackend.replay_info），manifest 原样带上，
        前端据此显示那行"回放模式"小字。跟 lora_for/lora_dir 同一个理由做成
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
        on_delta: Callable[[str, str], None] | None = None,
        **kwargs,
    ) -> T:
        """给定 system/user 提示与目标 pydantic 模型，返回校验通过的模型实例。

        on_delta 是流式增量回调，签名 `(文本片段, 种类) -> None`，
        种类是 `"content"`（正式输出）或 `"reasoning"`（思考过程，推理模型才有）。
        **两种分开报**：S3 开着思考，思考 token 先到、正式输出后到，合成一种的话
        前端会把思考过程当成方药渲染出来。不传就是非流式调用。

        `SUPPORTS_STREAMING=False` 的后端**不会收到这个参数**（见那个类属性的注释）：
        调用方照样可以传，只是不会发生流式——用 `streaming_note()` 问"这次为什么
        没流式"，不要靠猜。

        physician 只在本地后端（vLLM + LoRA）下有意义：知道这一次是替哪位医家
        推理的调用点（run_physician / run_react）显式传医家 id，后端据此选
        adapter；其余后端如实忽略。不传 = 不指定 adapter（走基座模型），
        S1/S2 这类跟医家无关的调用就是这种情况。理由详见 _complete 的文档。

        重试语义：首次 + 最多 2 次重试；第 2 次起把上次的原始返回和 pydantic
        校验错误一起回灌，要求模型修正。这一步是必要的——换模型时字段名猜错
        （比如把 element 写成 name），靠回灌的错误信息就能纠正。

        max_tokens 不传就用各后端自己的默认值（见 `_default_max_tokens()`：
        关思考 8192、开思考 32768、`effort=max` 65536，`LLM_MAX_TOKENS` 覆盖三者）。
        **不要全局调高默认值**：S1/S2/S3 用不到那么多 token，调高只会让真正失控的
        输出更晚才被发现；某个 prompt 确实需要更大上限（比如 S5 一张方子的「含」
        关系会重复带出 source_span，容易顶到 8192），在那一处调用点单独传。

        开思考的那几档**不是违反上面那句**：max_tokens 盖的是 reasoning + 可见输出
        两部分，8192 里能给可见输出的远不到 8192，所以这几档不是"调高上限"，是把
        可见输出的上限还原到跟关思考时同一个量级。
        """
        # 字段名那一句针对的是模型把字段名换成同义词（比如把 ElementHit.element
        # 写成 name）；注入 schema 本身已经能挡住大部分，这句是加固。
        # 放在 schema 后面而不是写进各个 prompt 的 yaml：
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
                # 不支持流式的后端一个多余的关键字都不给（见 SUPPORTS_STREAMING）
                stream_kwargs = ({"on_delta": on_delta}
                                 if on_delta is not None and self.SUPPORTS_STREAMING else {})
                raw = self._complete_within_deadline(
                    messages, temperature, max_tokens=max_tokens,
                    schema=schema, physician=physician,
                    deadline=self.timeouts().deadline, **stream_kwargs, **kwargs,
                )
            except Exception as e:  # noqa: BLE001 - 传输类错误：超时/非零退出/API 异常
                # 401/402/422 这类确定性失败直接抛，不进重试（见 NON_RETRYABLE_STATUS
                # 上面那段注释）。**这一条对 BYOK 尤其要紧**：访问者填错一个 key，
                # 走重试的话要等三次往返加两次退避，最后拿到一条看不出是自己 key 的
                # 通用错误。
                status = _status_code_of(e)
                if status in NON_RETRYABLE_STATUS:
                    raise LLMAuthError(_auth_error_message(status, e), status) from e
                # 其余没有"上一次输出"可回灌——回灌更早一次尝试的陈旧 raw 或空串只会让
                # 模型收到文不对题的纠错指令。原样重试，但重试前先退避一下。
                last_error = e
                if attempt < self.MAX_ATTEMPTS - 1:
                    _record_retry(e)
                    self._sleep(self._backoff_seconds(attempt))
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
                # 让调用方（比如医案三元组批量抽取）决定要不要跳过这条输入。
                if _looks_like_truncated_json(e, stripped, schema):
                    # max_tokens=None 不是"没配置成功"，是"这次调用没有显式传，
                    # 会走各后端自己的默认值"（比如 OpenAICompatBackend 走
                    # LLM_MAX_TOKENS，或 _default_max_tokens()）——只打一个
                    # "max_tokens=None" 容易让人以为配置丢了、去查环境变量，其实哪儿都没错。
                    # 带上 thinking：同一个后端在"开思考"和"关思考"两种情形下默认
                    # 上限差四倍，只报一个数字看不出这次走的是哪一档。
                    call_thinking = kwargs.get("thinking")
                    call_effort = kwargs.get("reasoning_effort")
                    max_tokens_desc = (
                        f"未设置（走后端默认值 "
                        f"{self._default_max_tokens(call_thinking, call_effort)}，"
                        f"thinking={call_thinking}, reasoning_effort={call_effort}）"
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

        # from last_error 保留异常链：调用方（比如医案三元组批量抽取）要按失败原因
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
    不该有第二份（docs/ARCHITECTURE.md §4）。差异只落在下面这几个
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
                # **四个相位分别设**：传一个 float 的话 SDK 会把它铺给四个相位，
                # 于是"连不上"要等和"读不出来"一样久。
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
        """默认 `deepseek-v4-pro`，`LLM_MODEL` 覆盖。

        默认值必须是服务端当前真实可用的模型名：拿一个已下线的模型名发请求，
        得到的是 HTTP 200 + **空响应体**（不是 404），症状是"一次调用返回空串 →
        校验失败 → 重试三次 → LLMError"，错误信息里看不出"模型名不存在"这件事
        （`_complete` 对空响应单独报出这条线索）。

        注意：**换模型 = 所有既有数字不可比**（ε / E3 / E4 / E8 / E9 / SDT 都跟模型
        绑定）。manifest 如实记录每次跑的 model。"""
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

    #: 云端 OpenAI 兼容 API 支持 SSE 流式（`stream=True`），而且是**真**增量。
    SUPPORTS_STREAMING = True

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
        on_delta: Callable[[str, str], None] | None = None,
        **kwargs,
    ) -> str:
        # schema / physician 在这一层如实忽略：云端 API 既没有 guided_decoding
        # 也没有 LoRA adapter 可切。**不能转给 SDK**——多一个它不认的关键字
        # 参数就是 TypeError（这也是这两个参数为什么是显式形参、不塞 kwargs）。
        extra_body: dict = {}
        if thinking is None:
            # 离线抽取这类调用点不传 step、因而不传 thinking，会落到 API 默认——
            # 而推理模型的默认是**开思考**。抽三元组是结构化信息提取，思考模式
            # 对它没有价值，只烧时间和输出 token，批量抽取时尤其明显。
            # 用环境变量给这类调用一个统一兜底，不改各调用点。
            import os as _os
            _fb = (_os.environ.get("LLM_DEFAULT_THINKING") or "").strip().lower()
            if _fb in ("enabled", "disabled"):
                thinking = _fb
        if thinking is not None:
            extra_body["thinking"] = {"type": thinking}
        extra: dict = {"extra_body": extra_body} if extra_body else {}
        if reasoning_effort is not None:
            extra["reasoning_effort"] = reasoning_effort
        # **思考模式下 temperature 不生效**（DeepSeek 文档）。既然不生效就不传：
        # 传了会让 manifest 里那个 temperature 看起来像是生效了的实验条件，
        # "同样输入不同输出"就会被错误地归到别的原因上。
        if thinking != "enabled":
            extra["temperature"] = temperature
        if on_delta is not None:
            # **`include_usage` 必须开**：流式响应的 usage 只在最后一个空 choices 的
            # chunk 里，不开的话 `record_usage` 一次也收不到，manifest 的
            # cache_hit_ratio 静默变 None——而日志里看不出任何错。
            extra["stream"] = True
            extra["stream_options"] = {"include_usage": True}
        resp = self.client.chat.completions.create(
            model=self._request_model_name(),
            messages=messages,
            response_format={"type": "json_object"},
            **extra,
            # 默认值见 _default_max_tokens()：按**这次调用的 thinking 设置**分档。
            # 调用方（generate() 的 max_tokens 参数）能覆盖它——不是全局调高，
            # 是某个 prompt 明确知道自己需要更大上限时单独传（比如 S5 一张方子的
            # 多条「含」关系）。
            max_tokens=(max_tokens if max_tokens is not None
                        else self._default_max_tokens(thinking, reasoning_effort)),
            **kwargs,
        )
        if on_delta is not None:
            content = self._drain_stream(resp, on_delta)
        else:
            # 缓存命中读数就在这里取。**取完立刻记**，不等 generate() 层——
            # 一次 generate 可能重试多次，每次请求都有自己的 usage，漏掉重试那几次
            # 会让命中率偏高（重试的请求前缀完全相同，几乎必定命中）。
            record_usage(getattr(resp, "usage", None))
            content = resp.choices[0].message.content or ""
        if not content.strip():
            # HTTP 200 + 空响应体 = **模型名很可能不存在/已下线**（服务端对已下线的
            # 模型名就是这个表现，不是 404）。不专门报出来的话症状是"空串过不了
            # schema 校验 → 重试三次 → LLMError"，错误信息里全是校验失败，看不出
            # 根因在模型名上。**照旧抛异常走重试**（网络抖动也可能返回空），但把
            # 这条线索写进错误里。
            raise LLMError(
                f"后端返回了 HTTP {'200（流式）' if on_delta is not None else '200'} "
                f"但一个字都没收到（model={self._request_model_name()!r}）。"
                "最常见的原因是**模型名不存在或已下线**——服务端对已下线的模型名"
                "返回的就是 200 + 空响应体（不是 404）。"
                "用 `curl $LLM_BASE_URL/models` 看当前可用的模型名，再设 LLM_MODEL。"
            )
        return content

    def _drain_stream(self, stream, on_delta: Callable[[str, str], None]) -> str:
        """读完一个流式响应，边读边喂 `on_delta`，返回拼起来的**正式输出**。

        三条不许省的：

        1. **返回值里只有 content，不含 reasoning。** 下游是
           `schema.model_validate_json()`，思考过程拼进去就没有一次能过校验。
        2. **usage 从最后那个 chunk 取**（`include_usage`），不是从第一个。
        3. **回调抛异常不能把这次调用变成"网络错误"。** 前端断开时
           `on_step` 会抛 `StreamClosed`，那时应该原样冒泡让上层收工；
           而把它裹进传输类错误会触发 `generate()` 的重试——客户端都走了还重试三次。
        """
        parts: list[str] = []
        usage = None
        for chunk in stream:
            got = getattr(chunk, "usage", None)
            if got is not None:
                usage = got
            for text, kind in _delta_texts(chunk):
                if kind == "content":
                    parts.append(text)
                on_delta(text, kind)
        record_usage(usage)
        return "".join(parts)


def _delta_texts(chunk) -> list[tuple[str, str]]:
    """一个流式 chunk 里的 (文本, 种类)。**思考与正式输出分开**：推理模型把思考
    放在 `delta.reasoning_content`（DeepSeek）或 `delta.reasoning`（部分兼容实现），
    合成一种的话前端会把思考过程当方药渲染出来。

    取不到就返回空列表——chunk 里没有 choices（最后那个只带 usage 的）是正常的，
    不是错误。
    """
    out: list[tuple[str, str]] = []
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return out
    delta = getattr(choices[0], "delta", None)
    if delta is None:
        return out
    for attr, kind in (("content", "content"),
                       ("reasoning_content", "reasoning"),
                       ("reasoning", "reasoning")):
        piece = getattr(delta, attr, None)
        if piece:
            out.append((str(piece), kind))
            if kind == "reasoning":
                # 两个别名只认先取到的那个，不然同一段思考会被发两遍
                break
    return out


# vLLM 的 guided_decoding 参数名在版本间变过（`guided_json` 走 extra_body 是
# 0.4~0.8.x 一直支持的写法，更新的版本另外支持 OpenAI 标准的
# `response_format: json_schema`）。默认用 `guided_json`，留一个环境变量是为了让
# 部署侧在所用的 vLLM 版本不认这个键时，改一个环境变量就能切换，不必改代码。
# 实际生效的键用 scripts/verify_local_backend.py 确认（它会把这个值打出来）。
VLLM_GUIDED_JSON_KEY = os.environ.get("VLLM_GUIDED_JSON_KEY", "guided_json")


def _resolve_lora_path(lora_dir: str | None, physician: str | None) -> Path | None:
    """按医家找 LoRA adapter 目录。返回 None = 这次不挂 adapter，走基座模型。

    三种情况分清楚（两个本地后端共用这一处判断，不各写一遍）：
      - `LORA_DIR` 没设置：不挂 adapter，走基座模型，返回 None。
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
            f"或者取消设置 LORA_DIR 明确表示本次跑基座模型。"
        )
    return path


class VLLMBackend(OpenAICompatBackend):
    """本地 vLLM，**server 模式**（`LLM_MODE=local`）：对着
    `python -m vllm.entrypoints.openai.api_server` 起的 OpenAI 兼容接口说话。

        LLM_MODE=local
        LLM_BASE_URL=http://127.0.0.1:8000/v1     # 不设就用这个默认值
        LLM_MODEL=tcm-local                        # server 的 --served-model-name
        LLM_MODEL_PATH=/path/to/models/Qwen2.5-1.5B-Instruct
        LORA_DIR=/path/to/lora                     # 可选，按医家训练的 LoRA adapter

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
      - lora_request：每位医家一个按医家训练的 LoRA adapter，一个 server 进程
        按请求切换。
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
        # OPENAI_API_KEY 环境变量、找不到就抛）。给一个明显不是真 key 的串，
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
        LLM_MODEL_PATH=/path/to/models/Qwen2.5-1.5B-Instruct
        LORA_DIR=/path/to/lora             # 可选，按医家训练的 LoRA adapter

    跟 server 模式的取舍：批量评测（run_eval/estimate_epsilon 动辄上千次调用）
    省掉每次的 HTTP 往返和 JSON 编解码；代价是模型跟评测脚本绑在同一个进程里，
    起停慢、并发要自己管，也没法给 api/main.py 的多线程问诊共用。所以在线服务
    用 server 模式，离线批量评测用这个。

    **`import vllm` 必须延迟到真正要用的时候**（`_engine` 属性里），不能放模块
    顶层：CI 和没有 GPU 的机器都不装 vllm，顶层 import 会让 `core.llm` 整个
    import 不了，整个测试套件一起崩——而绝大多数测试跟 vLLM 一点关系都没有。
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
        """惰性加载 vllm.LLM。加载模型是耗时的重 IO，不能在 __init__ 里做
        （否则 get_backend() 之后在 manifest 里问一句 model_name() 都会触发加载）。"""
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
        schema: type[BaseModel] | None, thinking: str | None = None,
        reasoning_effort: str | None = None,
    ):
        """thinking / reasoning_effort 只用来挑默认的 max_tokens：进程内 vLLM 跑的是
        本地基座，没有"思考模式"也没有 effort 这两个开关，但**上限该给多少仍然取决于
        调用方这一步要不要长输出**——所以参数照收，语义是"这一步的预算档位"。
        接收 effort 这个参数就是为了这一点：`effort=max` 那一档在云端后端上
        默认上限更高，本地后端不跟着走的话，同一段代码在两个后端上会在不同的
        长度处被截断，而那种差异极难归因。"""
        from vllm import SamplingParams  # 延迟 import

        guided = None
        if schema is not None:
            from vllm.sampling_params import GuidedDecodingParams

            guided = GuidedDecodingParams(json=schema.model_json_schema())
        return SamplingParams(
            temperature=temperature,
            max_tokens=(max_tokens if max_tokens is not None
                        else self._default_max_tokens(thinking, reasoning_effort)),
            guided_decoding=guided,
        )

    def _complete(
        self, messages: list[dict], temperature: float,
        max_tokens: int | None = None,
        schema: type[BaseModel] | None = None,
        physician: str | None = None,
        thinking: str | None = None,
        reasoning_effort: str | None = None,
        **kwargs,
    ) -> str:
        # thinking / reasoning_effort 显式接住：本地基座没有这个开关，但**不能塞进
        # **kwargs**——它们会被原样转给 vllm 的 chat()，那是一个 TypeError。
        # thinking 仍然影响默认的 max_tokens 档位（见 _sampling_params 的文档）。
        outputs = self._engine.chat(
            messages,
            sampling_params=self._sampling_params(temperature, max_tokens, schema,
                                                  thinking, reasoning_effort),
            lora_request=self._lora_request(physician),
            **kwargs,
        )
        # chat() 按输入的 batch 返回列表；这里一次只喂一轮对话，所以取第 0 条。
        # 结构不符合预期时报错而不是静默返回空串：空串会被 generate() 当成
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
    """访问者自带 key（BYOK）。

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
        # 逐请求覆盖：BYOK 用访问者自己的 key，超额降级用 ReplayBackend。
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
