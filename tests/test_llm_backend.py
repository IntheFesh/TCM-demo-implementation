"""core/llm.py 后端层的离线测试：LLM_MODE 分派、共享重试、截断检测与各后端的
元数据。全部不联网——SDK 客户端用假对象挡掉。
"""
import os
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from core.llm import (
    DEFAULT_MAX_TOKENS,
    THINKING_MAX_TOKENS,
    REASONING_MODELS,
    LLMBackend,
    LLMError,
    LLMTruncatedError,
    OpenAICompatBackend,
    VLLMBackend,
    VLLMInProcessBackend,
    _looks_like_truncated_json,
    get_backend,
    get_llm,
    reset_llm_singleton,
)


class Tiny(BaseModel):
    ok: bool
    note: str = Field(min_length=1)


class ScriptedBackend(LLMBackend):
    """按预设脚本依次返回原始文本的假后端，用来测基类的重试语义
    （不测某个具体厂商的实现）。"""

    def __init__(self, raws: list[str]):
        self.raws = raws
        self.calls: list[list[dict]] = []
        self.kwargs_seen: list[dict] = []

    def model_name(self) -> str:
        return "scripted"

    def backend_id(self) -> str:
        return "scripted"

    def _complete(self, messages, temperature, **kwargs) -> str:
        # 深拷一份：generate 会往同一个 list 里 append，不拷的话历史会被后续修改覆盖
        self.calls.append([dict(m) for m in messages])
        self.kwargs_seen.append(dict(kwargs))
        return self.raws[len(self.calls) - 1]


# ---------- LLM_MODE 分派 ----------

def test_get_backend_defaults_to_openai_compat(monkeypatch):
    monkeypatch.delenv("LLM_MODE", raising=False)
    assert isinstance(get_backend(), OpenAICompatBackend)


def test_get_backend_local(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "local")
    assert isinstance(get_backend(), VLLMBackend)


def test_get_backend_unknown_mode_falls_back_to_api(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "什么鬼模式")
    assert isinstance(get_backend(), OpenAICompatBackend)


def test_reset_singleton_lets_mode_switch_take_effect(monkeypatch):
    """LLM_MODE 是进程级变量，不清单例的话切换不生效——这是切后端时最容易
    出错的地方，所以要有测试守着。"""
    monkeypatch.setenv("LLM_MODE", "api")
    reset_llm_singleton()
    assert isinstance(get_llm(), OpenAICompatBackend)

    monkeypatch.setenv("LLM_MODE", "local")
    assert isinstance(get_llm(), OpenAICompatBackend)  # 单例还在，切换不生效
    assert not isinstance(get_llm(), VLLMBackend)

    reset_llm_singleton()
    assert isinstance(get_llm(), VLLMBackend)
    reset_llm_singleton()


# ---------- manifest 用的三个元数据方法 ----------

def test_openai_backend_reports_real_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    b = OpenAICompatBackend()
    assert b.model_name() == "deepseek-chat"
    assert b.backend_id() == "api"
    # 默认后端不带警告，否则每份正常报告都会挂一条噪音
    assert b.comparability_warning() is None


def test_vllm_backend_metadata(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_PATH", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)  # model_name() 会回落到它
    b = VLLMBackend()
    assert b.backend_id() == "local"
    assert b.model_name() == "vllm-unconfigured"
    assert b.comparability_warning() is not None


def test_vllm_complete_is_a_real_implementation():
    """VLLMBackend 的 `_complete` 是真正的实现，不是抛 NotImplementedError 的桩。

    这里只查"不是桩"这一件事——真正的行为（guided_json / LoRA / 两种模式）
    在 tests/test_llm_local_backend.py 里用假 OpenAI 客户端和假 vllm 模块测，
    不在这个文件里重复一遍。"""
    import inspect

    src = inspect.getsource(VLLMBackend._complete)
    assert "NotImplementedError" not in src
    # 抽象方法本身（LLMBackend._complete）应该抛 NotImplementedError——那是接口声明，不是桩
    assert "NotImplementedError" in inspect.getsource(LLMBackend._complete)


# ---------- 共享重试语义（基类，不是某个后端各写一套） ----------

def test_generate_succeeds_first_try():
    b = ScriptedBackend(['{"ok":true,"note":"good"}'])
    out = b.generate("sys", "usr", Tiny)
    assert out.ok is True
    assert len(b.calls) == 1


def test_generate_strips_markdown_fence():
    b = ScriptedBackend(['```json\n{"ok":true,"note":"fenced"}\n```'])
    assert b.generate("sys", "usr", Tiny).note == "fenced"


def test_generate_retries_and_feeds_error_back():
    """第一次字段错，第二次修对——例如模型把 element 写成 name——
    必须靠回灌纠正。"""
    b = ScriptedBackend(['{"ok":true}', '{"ok":true,"note":"fixed"}'])
    out = b.generate("sys", "usr", Tiny)
    assert out.note == "fixed"
    assert len(b.calls) == 2
    # 第二次的消息里必须带着上次的原始输出和校验错误
    second = b.calls[1]
    assert any(m["role"] == "assistant" and m["content"] == '{"ok":true}' for m in second)
    assert any("上一次输出未通过校验" in m["content"] for m in second if m["role"] == "user")


def test_generate_gives_up_after_three_attempts():
    b = ScriptedBackend(['{"bad":1}', '{"bad":2}', '{"bad":3}'])
    with pytest.raises(LLMError) as ei:
        b.generate("sys", "usr", Tiny)
    msg = str(ei.value)
    assert len(b.calls) == 3
    # 报错要能定位问题：后端、模型、schema、最后原始返回都在
    assert "backend=scripted" in msg
    assert "model=scripted" in msg
    assert "schema=Tiny" in msg
    assert "bad" in msg


def test_generate_gives_up_preserves_exception_chain():
    """__cause__ 要能拿到真实的底层异常，不是只能从格式化好的字符串里猜。
    调用方（比如 core/batch.py 的批量统计）按失败原因分布做统计时，
    type(err.__cause__).__name__ 是结构化信号，解析"最后错误={...}"
    这句拼出来的文本反而脆弱——错误信息格式一变解析就错。"""
    b = ScriptedBackend(['{"bad":1}', '{"bad":2}', '{"bad":3}'])
    with pytest.raises(LLMError) as ei:
        b.generate("sys", "usr", Tiny)
    assert ei.value.__cause__ is not None
    assert isinstance(ei.value.__cause__, ValidationError)


def test_transport_error_llm_error_also_preserves_cause():
    """传输类错误（超时/连接断）耗尽重试后同样要能拿到底层异常类型，
    不止是校验错误这一条路径。"""
    class AlwaysTimesOut(LLMBackend):
        def model_name(self): return "m"
        def backend_id(self): return "t"
        def _complete(self, messages, temperature, **kw):
            raise TimeoutError("超时")

    b = AlwaysTimesOut()
    b.RETRY_BACKOFF_SECONDS = (0.0, 0.0)
    with pytest.raises(LLMError) as ei:
        b.generate(system="s", user="u", schema=Tiny)
    assert isinstance(ei.value.__cause__, TimeoutError)


def test_generate_injects_schema_into_system():
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate("我的业务提示词", "usr", Tiny)
    system_msg = b.calls[0][0]["content"]
    assert "我的业务提示词" in system_msg
    assert "JSON Schema" in system_msg


def test_generate_pins_field_names_in_schema_hint():
    """字段名约束加在 schema hint 里（一处覆盖所有 prompt），不加在各个 yaml 里
    ——yaml 里的手写示例会跟 schemas.py 漂移，schema hint 是自动导出的不会。"""
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate("业务提示词", "usr", Tiny)
    system_msg = b.calls[0][0]["content"]
    assert "字段名必须与上述 schema 完全一致" in system_msg
    # 括号里的例子对应模型常见的字段名错误（element 写成 name），是文档也是约束，别删
    assert "element" in system_msg and "name" in system_msg


# ---------- 传输错误不回灌陈旧输出 / 围栏剥离 / prompt 模板变量集合 ----------

def test_transport_errors_retry_without_feeding_back_stale_output():
    """超时/非零退出这类传输错误没有"上一次输出"可回灌——回灌上一次尝试留下的
    陈旧 raw 或空串，只会让模型收到文不对题的纠错指令。"""
    from core.llm import LLMBackend
    from pydantic import BaseModel

    class Out(BaseModel):
        a: int

    class Flaky(LLMBackend):
        def __init__(self):
            self.seen = []

        def model_name(self): return "m"
        def backend_id(self): return "t"

        def _complete(self, messages, temperature, **kw):
            self.seen.append(len(messages))
            if len(self.seen) == 1:
                raise TimeoutError("超时")
            return '{"a": 1}'

    b = Flaky()
    assert b.generate(system="s", user="u", schema=Out).a == 1
    assert b.seen == [2, 2], "传输错误后 messages 不该多出回灌的两条"


def test_validation_errors_still_feed_back():
    from core.llm import LLMBackend
    from pydantic import BaseModel

    class Out(BaseModel):
        a: int

    class Wrong(LLMBackend):
        def __init__(self):
            self.seen = []

        def model_name(self): return "m"
        def backend_id(self): return "t"

        def _complete(self, messages, temperature, **kw):
            self.seen.append(len(messages))
            return '{"a": "x"}' if len(self.seen) == 1 else '{"a": 1}'

    b = Wrong()
    assert b.generate(system="s", user="u", schema=Out).a == 1
    assert b.seen == [2, 4]


@pytest.mark.parametrize("text,expected", [
    ('```JSON\n{"a": 1}\n```', '{"a": 1}'),
    ('好的，结果如下：\n```json\n{"a": 1}\n```\n以上。', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('{"a": 1}', '{"a": 1}'),
])
def test_strip_code_fence_variants(text, expected):
    from core.llm import strip_code_fence

    assert strip_code_fence(text) == expected


def test_render_rejects_missing_placeholders_for_every_prompt():
    """每个 yaml 的占位符集合与调用方传的 kwargs 必须逐一吻合。这里钉住占位符
    集合本身：yaml 新加一个 $var 而调用方没跟上，这条会先红。"""
    import re
    from core.llm import PROMPTS_ROOT, load_prompt

    expected = {
        "s0_extract_case": {"raw_text", "follow_hints"},
        "s1_normalize": {"complaint"},
        "s2_elements": {"elements", "symptoms", "tongue", "pulse"},
        # S1+S2 合一的那份（`S1S2_MERGED=1` 走它）。占位符是两份的并集减去
        # 中间那一层——S2 那份要 symptoms/tongue/pulse 是因为它们来自上一次调用的
        # 输出；合一之后那三样在同一次调用里产生，所以只剩 complaint + elements。
        "s1s2_merged": {"complaint", "elements"},
        "s3_syndrome": {"name", "elements_summary", "symptoms", "refs"},
        # 结构化模式的 s3 prompt（`S3_MODE=structured` 走它）。
        # 没有 `name`——它不模拟某一位医家，而是把五家融合成一份结论，
        # 所以是 `physicians`（五位中文名）+ `physician_ids`（id(中文名) 清单，
        # 模型填 id 时照着看）。调用方是 core/chain.py::run_synthesis。
        "s3_structured": {"physicians", "physician_ids", "elements_summary",
                          "symptoms", "refs"},
        # 第一相「演绎推导」的 prompt（`S3_MODE=derived` 走它，产品默认）。
        # 没有 `refs`——这一相不检索任何医案，$refs 占位符从设计上就不存在。
        # $theory_rules 是医理规则块（core/chain.py::_format_theory_rules），
        # $knowledge 是本草/方剂本体块（跟 s3_structured 的 $refs 里含知识块
        # 不同，这里知识块单独占一个占位符，因为没有医案块可以合并进去）。
        # 调用方是 core/chain.py::run_derivation。
        "s3_derived": {"elements_summary", "symptoms", "theory_rules", "knowledge"},
        # physician_id 是传给模型的医家 id（模型要填 id 而不是中文名，见
        # docs/ARCHITECTURE.md §5），调用方 core/react.py::run_react 传它。
        "s3_react": {"name", "physician_id", "symptoms", "elements_summary", "tools", "history", "remaining"},
        "patient_sim": {"profile", "history", "question"},
        "sdt_extract": {"clinical_data"},
        "sdt_select": {"reasoning_block", "clinical_data", "pathogenesis_options", "syndrome_options"},
        "sdt_summary": {"reasoning_block", "clinical_data"},
        "s5_extract_triples": {"raw_text"},
        # 药理层的两份抽取 prompt（offline/extract_reference_triples.py 调用）
        "s6_extract_materia_medica": {"raw_text"},
        "s7_extract_formulary": {"raw_text"},
        # 两个轻量能力的 prompt。它们**不是问诊链的一环**
        # （见 core/assist.py 的模块文档那张表），所以占位符里没有
        # $elements_summary / $theory_rules ——它们拿到的是已经定下来的
        # 证型治法，不重新辨一次证。
        "assist_edit": {"syndrome", "principle", "profile", "preferences",
                        "herbs", "diff", "violations", "n_min", "n_max"},
        "assist_compose": {"syndrome", "principle", "profile", "preferences",
                           "herbs", "rule_findings"},
        # 复诊调方。它跟上面两个同类（不重新辨证），但多拿一份
        # 上一诊的方与本次改动（$prev_herbs / $changes_text），因为"效不更方"
        # 这个判断的依据全在两诊之差里，不在当下这一诊的症状里。
        "assist_followup": {"syndrome", "principle", "doses_count", "usage",
                            "prev_herbs", "profile", "changes_text", "preferences"},
    }
    for path in (PROMPTS_ROOT / "v1").glob("*.yaml"):
        found = set(re.findall(r"(?<!\$)\$\{?([A-Za-z_]\w*)\}?", load_prompt(path.stem)["system"]))
        assert found == expected[path.stem], f"{path.name} 的占位符变了：{found}"


# ---------- s3_syndrome.yaml 的嵌入示例与硬约束 ----------


def _s3_prompt_embedded_example() -> dict:
    """s3_syndrome.yaml 中段嵌了一段格式示例——schema hint 对嵌套结构
    表达力有限，需要示例来示范输出形状。这段示例是手写的 JSON，藏在一大段
    prose 里，改 prompt 时最容易被顺手改坏（多一个逗号、少一个引号）而不会有
    任何报错提示——除非有测试盯着它。

    示例位于 $elements_summary/$symptoms/$refs 之前（参考医案要紧贴输出指令，
    见 core/chain.py::_format_case_block 的文档字符串），示例后面还跟着
    证素分析/患者症状/参考医案/引用要求这些 prose——所以不能假设"从第一个
    顶格 { 到文件末尾"就是完整示例，而要用 json.JSONDecoder.raw_decode
    按大括号配平找真正的结束位置，忽略后面的 trailing 内容。
    """
    import json

    from core.llm import load_prompt

    system = load_prompt("s3_syndrome")["system"]
    lines = system.split("\n")
    opening_braces = [i for i, line in enumerate(lines) if line.strip() == "{"]
    assert opening_braces, "s3_syndrome.yaml 里没找到嵌入的 JSON 示例（顶格的 { 都没有）"
    example_text = "\n".join(lines[opening_braces[0]:])
    obj, _ = json.JSONDecoder().raw_decode(example_text)
    return obj


def test_s3_prompt_embedded_example_is_valid_json():
    _s3_prompt_embedded_example()  # 解析失败会直接抛 JSONDecodeError


def test_s3_prompt_embedded_example_validates_against_the_real_schema():
    """不仅要是合法 JSON，还要真的能喂进 core.schemas.S3Syndrome——包括它的
    model_validator（selected 越界检查、base_formula 双向约束）。
    示例本身违反自己教模型遵守的约束，比没有示例更糟。

    示例里用的是泛称（"方名A"/"药名X"）而不是真实方名/药名——完整
    病例示例会教会模型"这种情况开这个方"而不是"输出应该长这个形状"，
    模型就会照抄示例而不是依据真实的参考医案。所以这里断言"方名A"是
    刻意的：示例只示范结构。"""
    from core.schemas import S3Syndrome

    obj = _s3_prompt_embedded_example()
    s3 = S3Syndrome.model_validate(obj)
    assert s3.formula == "方名A"  # 对应 selected=0 那个 classic 候选方（泛称，不是真实方名）


def test_s3_prompt_embedded_example_covers_all_three_sources_and_varied_confidence():
    """示例存在的意义是"教会模型怎么填三种来源、置信度不能都填 high"——如果
    示例自己三个都写 classic 或者三个都 high，等于示范了一个错误答案。"""
    obj = _s3_prompt_embedded_example()
    cands = obj["formula_candidates"]
    assert 2 <= len(cands) <= 3
    assert {c["source"] for c in cands} == {"classic", "modified", "composed"}
    assert len({c["confidence"] for c in cands}) > 1, "示例不该三个候选方置信度都一样"
    assert any(c["source"] == "classic" for c in cands)


def test_s3_prompt_embedded_example_includes_reasoning_plain_without_jargon():
    """reasoning_plain 是可选 schema 字段（构造点不必都补），但 prompt
    层面是硬要求——示例本身要示范"这是什么样子"，而且不能自己就带着专业
    术语（反面示范比没有示范更糟）。"""
    obj = _s3_prompt_embedded_example()
    assert obj.get("reasoning_plain"), "示例里 reasoning_plain 不能是空的"
    jargon = ["肝木乘土", "中焦气机", "阴虚阳亢", "横逆犯胃", "疏泄"]
    for term in jargon:
        assert term not in obj["reasoning_plain"], (
            f"reasoning_plain 示例文本里出现了专业术语「{term}」，"
            "这是给患者看的通俗版，不该带这类词"
        )


def test_s3_prompt_example_has_no_real_formula_or_herb_names():
    """示例只示范字段结构，不该出现任何真实方名/药名/证型——完整
    病例示例会教会模型"这种情况开这个方"而不是"输出应该长这个形状"。这里
    把示例序列化回文本再 grep，不是只查 formula 字段：herb_items 的 name、
    rationale 里也可能不小心带真实药名。"""
    import json

    obj = _s3_prompt_embedded_example()
    example_text = json.dumps(obj, ensure_ascii=False)
    banned = [
        "柴胡疏肝散", "瓜蒌薤白半夏汤", "保和丸", "六君子汤",
        "柴胡", "白芍", "陈皮", "香附", "枳壳", "瓦楞子", "佛手",
        "郁金", "黄连", "吴茱萸", "木香", "砂仁",
        "肝胃不和证", "脾胃气虚证", "脾胃湿热证",
    ]
    found = [term for term in banned if term in example_text]
    assert not found, f"示例里出现了真实方名/药名/证型，会带偏模型：{found}"


def test_s3_prompt_example_comes_before_reference_cases():
    """示例要排在参考医案（$refs）前面，参考医案紧贴输出指令——
    recency 效应下，最该被模型利用的内容（参考医案）要放在离结论最近的
    位置，不能被夹在示例和输出指令之间被稀释。"""
    from core.llm import load_prompt

    system = load_prompt("s3_syndrome")["system"]
    lines = system.split("\n")
    opening_braces = [i for i, line in enumerate(lines) if line.strip() == "{"]
    assert opening_braces, "找不到嵌入的 JSON 示例"
    example_pos = opening_braces[0]
    refs_pos = next(i for i, line in enumerate(lines) if "$refs" in line)
    output_instruction_pos = next(
        i for i, line in enumerate(lines) if "只输出符合 schema 的 JSON" in line
    )
    assert example_pos < refs_pos < output_instruction_pos, (
        "顺序应为：示例 → ... → 参考医案($refs) → ... → 输出指令，"
        f"实际位置 example={example_pos} refs={refs_pos} 输出指令={output_instruction_pos}"
    )


@pytest.mark.parametrize("phrase", [
    "至少要有一个",  # 至少一个 classic 候选方的硬约束
    "不要三个候选方都填high",
    "剂量不确定时填null，不要猜一个数",
    "这个字段关系到用药安全",  # decoction 字段的安全性说明
    "不能出现",  # reasoning_plain 禁止专业术语那句的开头
    "参考医案中哪一条",  # 显式要求引用参考医案的具体内容
    "cited_case_ids仍然填相关度最高的那一条",  # 不相关时也不放松 min_length=1
])
def test_s3_prompt_contains_the_hard_constraints(phrase):
    """这几句不是随手写的修饰语，是针对具体问题（模型倾向三个都填
    high、role 大量为 null）的明文约束——被误删或改写成模糊表述时，
    这条测试要能先红，而不是等真实调用跑出退步的格式遵从度才发现。

    yaml 里的 prose 为了可读性手动折行，逐字匹配会被行内换行拆散（"不要\n
    三个候选方都填 high" 这种），所以先把连续空白（含换行）压成单个空格再比对，
    这样只要语义短语完整、不管折在哪一行都能测到——真正要盯住的是"这句话
    还在不在"，不是"它有没有被折成两行"。
    """
    import re

    from core.llm import load_prompt

    normalized = re.sub(r"\s+", "", load_prompt("s3_syndrome")["system"])
    assert re.sub(r"\s+", "", phrase) in normalized


# ---------- 传输错误重试之间的退避 ----------


class _FlakyThenOk(LLMBackend):
    """前 n_fail 次 _complete 抛传输错误，之后返回合法 JSON。"""

    RETRY_BACKOFF_SECONDS = (1.0, 2.0)  # conftest 把基类清零了，这里显式设回真实值

    def __init__(self, n_fail: int):
        self.n_fail = n_fail
        self.calls = 0
        self.sleeps: list[float] = []
        self._sleep = self.sleeps.append  # 不真睡，只记录

    def model_name(self):
        return "m"

    def backend_id(self):
        return "t"

    def _complete(self, messages, temperature, **kw):
        self.calls += 1
        if self.calls <= self.n_fail:
            raise TimeoutError("超时")
        return '{"ok": true, "note": "x"}'


def _within_jitter(actual: float, base: float) -> bool:
    return base * (1 - LLMBackend.RETRY_JITTER) <= actual <= base * (1 + LLMBackend.RETRY_JITTER)


def test_transport_retry_backs_off_exponentially_with_jitter():
    """判据是"落在 1.0/2.0 的 ±20% 区间里"，不是"等于 1.0/2.0"：三位医家是
    **同时**出发的，一起撞 429 就会一起在同一时刻重试，形成一波波同步冲击。
    抖动把它们错开——而带抖动的等待时间按定义就不可能等于一个定值。
    另外两件事同样钉着：退避是递增的、只退避两次。"""
    b = _FlakyThenOk(n_fail=2)
    assert b.generate(system="s", user="u", schema=Tiny).ok is True
    assert len(b.sleeps) == 2
    assert _within_jitter(b.sleeps[0], 1.0), b.sleeps
    assert _within_jitter(b.sleeps[1], 2.0), b.sleeps
    assert b.sleeps[1] > b.sleeps[0], "退避必须是递增的"


def test_backoff_is_not_always_the_same_number():
    """抖动要真的抖：连跑 12 次拿到的 24 个等待时间里，第一次退避不该全是同一个数。
    （抖动写成常数 0 的话上面那条区间断言照样绿，这条才抓得到。）"""
    firsts: set[float] = set()
    for _ in range(12):
        b = _FlakyThenOk(n_fail=2)
        b.generate(system="s", user="u", schema=Tiny)
        firsts.add(round(b.sleeps[0], 6))
    assert len(firsts) > 1, f"12 次退避全是同一个数，抖动没生效：{firsts}"


def test_no_backoff_after_the_last_attempt():
    """三次全挂：只退避两次（两次尝试之间），最后一次失败后直接抛，不再白等。"""
    b = _FlakyThenOk(n_fail=3)
    with pytest.raises(LLMError):
        b.generate(system="s", user="u", schema=Tiny)
    assert len(b.sleeps) == 2
    assert _within_jitter(b.sleeps[0], 1.0) and _within_jitter(b.sleeps[1], 2.0)


def test_validation_errors_retry_immediately_without_backoff():
    """校验错误是模型格式没对，回灌错误信息立刻重问才有意义，等一秒不会答得更对。"""
    b = ScriptedBackend(['{"ok": true}', '{"ok": true, "note": "x"}'])  # 第一次少 note
    sleeps: list[float] = []
    b._sleep = sleeps.append
    b.RETRY_BACKOFF_SECONDS = (1.0, 2.0)
    assert b.generate(system="s", user="u", schema=Tiny).note == "x"
    assert sleeps == []


# ---------- max_tokens：显式参数，不塞进 **kwargs ----------


def test_generate_passes_max_tokens_to_complete():
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate(system="s", user="u", schema=Tiny, max_tokens=16384)
    assert b.kwargs_seen[0]["max_tokens"] == 16384


def test_generate_defaults_max_tokens_to_none():
    """不传就是 None，各后端自己决定默认值（OpenAICompatBackend 落到环境变量，
    进程内 vLLM 按档位取）——generate() 本身不该替后端猜一个数字。"""
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate(system="s", user="u", schema=Tiny)
    assert b.kwargs_seen[0]["max_tokens"] is None


def test_openai_backend_max_tokens_overrides_env_var(monkeypatch):
    """显式传的 max_tokens 要真的传到 SDK 调用里，不是只存在签名上没用上。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    monkeypatch.setenv("LLM_MAX_TOKENS", "8192")
    b = OpenAICompatBackend()
    b._client = FakeClient()
    b._complete([{"role": "user", "content": "x"}], 0.0, max_tokens=16384)
    assert captured["max_tokens"] == 16384


def test_openai_backend_max_tokens_falls_back_to_env_var_when_not_passed(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    monkeypatch.setenv("LLM_MAX_TOKENS", "12000")
    b = OpenAICompatBackend()
    b._client = FakeClient()
    b._complete([{"role": "user", "content": "x"}], 0.0)
    assert captured["max_tokens"] == 12000


# ---------- _default_max_tokens：推理模型要更大 ----------


def test_default_max_tokens_follows_this_calls_thinking_setting(monkeypatch):
    """**按这次调用的 thinking 设置分档，不按模型名。**

    思考是**按步**设的：S1/S2/追问/残差/ReAct 关了思考，而 S3 是唯一开思考的
    一步，它的上限要同时装 reasoning tokens 和可见输出。按模型名分档的话，
    关了思考的步骤白拿大额度，**截断风险全部压到了 S3 一步上**。
    """
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    b = OpenAICompatBackend()
    assert b._default_max_tokens(thinking="disabled") == DEFAULT_MAX_TOKENS == 8192
    assert b._default_max_tokens(thinking="enabled") == THINKING_MAX_TOKENS == 32768


def test_unspecified_thinking_falls_back_to_the_model_default(monkeypatch):
    """不传 thinking = 走 API 自己的默认。推理模型的 API 默认就是开思考，
    所以这时要按"开思考"给上限——按"关思考"给会在这条路径上继续截断。
    非推理模型的 API 默认是不思考，给 8192。"""
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    assert OpenAICompatBackend()._default_max_tokens() == THINKING_MAX_TOKENS
    monkeypatch.setenv("LLM_MODEL", "some-non-reasoning-model")
    assert OpenAICompatBackend()._default_max_tokens() == DEFAULT_MAX_TOKENS


def test_the_request_gets_the_thinking_sized_budget(monkeypatch):
    """上面两条测的是那个方法，这条测它真的被接到请求上了——S3 那一次
    （thinking=enabled）拿到的必须是 32768，不是 8192。"""
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    b = OpenAICompatBackend()
    b._client = FakeClient()
    b._complete([{"role": "user", "content": "x"}], 0.0, thinking="enabled")
    assert captured["max_tokens"] == THINKING_MAX_TOKENS
    captured.clear()
    b._complete([{"role": "user", "content": "x"}], 0.0, thinking="disabled")
    assert captured["max_tokens"] == DEFAULT_MAX_TOKENS


def test_the_thinking_tier_is_separate_from_the_disabled_tier(monkeypatch):
    """上限按这次调用的 thinking 设置分档：开思考 32768，关思考 8192——
    大上限只给需要装下推理过程的那一档，不是全局调高。"""
    assert THINKING_MAX_TOKENS == 32768
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    b = OpenAICompatBackend()
    assert b._default_max_tokens(thinking="disabled") == DEFAULT_MAX_TOKENS == 8192


def test_the_default_model_is_one_of_the_reasoning_models():
    """不设任何环境变量时，默认模型必须登记在 REASONING_MODELS 里——默认模型是
    推理模型而默认上限按非推理模型给，等于默认配置就会截断。"""
    import os

    env_model = os.environ.pop("LLM_MODEL", None)
    try:
        assert OpenAICompatBackend().model_name() in REASONING_MODELS
    finally:
        if env_model is not None:
            os.environ["LLM_MODEL"] = env_model


def test_llm_max_tokens_env_var_still_wins_over_both_defaults(monkeypatch):
    """环境变量优先：不然"某台机器上上限不对"就没有不改代码的补救办法。"""
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("LLM_MAX_TOKENS", "4096")
    assert OpenAICompatBackend()._default_max_tokens() == 4096


def test_default_max_tokens_lives_on_the_base_class_so_every_backend_gets_it():
    """这个判断只能有一处实现（docs/ARCHITECTURE.md §4）：各后端各写一份的话，
    "推理模型要更大"会只在其中一个后端上生效。判据是它定义在基类上、子类不覆盖。"""
    assert "_default_max_tokens" in vars(LLMBackend)
    for cls in (OpenAICompatBackend, VLLMBackend, VLLMInProcessBackend):
        assert "_default_max_tokens" not in vars(cls), cls.__name__


def test_openai_backend_uses_the_reasoning_default_when_nothing_is_passed(monkeypatch):
    """上面三条测的是那个方法本身，这条测它真的被接到 SDK 调用上了
    （调用处若自己去读环境变量，改错这个方法就没有人发现）。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")
    b = OpenAICompatBackend()
    b._client = FakeClient()
    b._complete([{"role": "user", "content": "x"}], 0.0)
    assert captured["max_tokens"] == THINKING_MAX_TOKENS


def test_truncation_error_names_the_default_it_actually_used(monkeypatch):
    """截断报错里要写出实际生效的上限，不是笼统的"未设置（走后端默认值）"：
    "8192 还是 32768"正是判断"是不是推理 token 把预算吃掉了"要看的东西。"""
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4-pro")

    class Truncating(LLMBackend):
        def model_name(self):
            return os.environ.get("LLM_MODEL", "deepseek-v4-pro")

        def backend_id(self):
            return "fake"

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kw):
            return '{"ok":true,"note":"' + "医" * 400

    with pytest.raises(LLMTruncatedError) as e:
        Truncating().generate(system="s", user="u", schema=Tiny)
    # 报错里同时带上 thinking 与 reasoning_effort：上限分了三档
    # （关思考 8192 / 开思考 32768 / effort=max 65536），只报一个数字
    # 看不出这次走的是哪一档。
    assert (f"未设置（走后端默认值 {THINKING_MAX_TOKENS}，"
            f"thinking=None, reasoning_effort=None）") in str(e.value)


# ---------- 输出被截断（撞 max_tokens）：直接失败，不当格式错误重试 ----------


def _truncated_json_for(schema) -> str:
    """构造一个在字符串字段中途被切断的 JSON，模拟真实撞 max_tokens 的输出。

    这段文本重复拼接到超过 100 字符（core.llm.TRUNCATION_MIN_LENGTH）——
    这是截断判定的绝对长度下限，任何长度小于它的文本都不会被判成截断，不管
    EOF 落在哪里（2 字符的近空响应是限流/抖动，不是截断）。调用方
    （test_looks_like_truncated_json_detects_eof_at_end_of_text、
    test_generate_raises_truncated_error_without_retrying）要测的是
    "EOF 在末尾 → 判成截断"这条分支，所以构造的样本必须越过这个下限。
    用重复拼接而不是手写一段定长字符串，是为了避免"人肉数字符数"算错。
    """
    filler = "这是一段模拟真实输出被 max_tokens 砍断的示例文本，" * 4
    text = '{"ok": true, "note": "' + filler
    assert len(text) > 100, "重复次数不够，没有越过 TRUNCATION_MIN_LENGTH"
    return text


def test_looks_like_truncated_json_detects_eof_at_end_of_text():
    from pydantic import ValidationError

    text = _truncated_json_for(Tiny)
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, Tiny) is True


def test_looks_like_truncated_json_does_not_flag_mid_text_syntax_errors():
    """缺逗号这类语法错误报在文本中间，不是"读到末尾断了"，不该被当成截断——
    这类错误重试有意义（模型只是格式没对），误判成截断会让本该能修好的输出
    白白被跳过。"""
    from pydantic import ValidationError

    text = '{"ok": true "note": "缺个逗号"}'
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, Tiny) is False


def test_looks_like_truncated_json_handles_multiline_output():
    """模型有时会把 JSON 打印成多行（缩进/换行），"末尾"要按最后一行算，
    不能直接拿 len(text) 跟 pydantic 报的 column 比——column 是行内位置，
    多行时那样比较会永远比不上，把真截断当成不是截断。

    文本长度要超过 100 字符（core.llm.TRUNCATION_MIN_LENGTH）：更短的文本
    无论 EOF 落在哪都会被直接判定为"太短不算截断"，测不到这条测试真正要测的
    多行 EOF 定位逻辑。所以最后一行写得足够长，结构（三行、最后一行未闭合）
    不变。"""
    from pydantic import ValidationError

    long_tail = "这一行被砍断了没有闭合引号，为了越过这道绝对长度下限故意重复写长一些，" * 3
    text = '{\n  "ok": true,\n  "note": "' + long_tail
    assert len(text) > 100
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, Tiny) is True


def test_looks_like_truncated_json_ignores_non_json_invalid_errors():
    """字段类型错（不是 JSON 语法错）走的是另一条 pydantic 错误类型，
    不该被这个只管"JSON 本身解析失败"的判断函数误伤。"""
    from pydantic import ValidationError

    try:
        Tiny.model_validate_json('{"ok": "不是布尔值", "note": "x"}')
        raise AssertionError("这段构造的输入应该校验失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, '{"ok": "不是布尔值", "note": "x"}', Tiny) is False


# ---------- 短响应不该被判成截断（2 字符的 `{"` 是近空响应，不是截断） ----------


def test_looks_like_truncated_json_rejects_output_shorter_than_schema_minimum():
    """核心回归：长度低于这个 schema 的最短合法实例时，不管 EOF 落在哪，
    都不能判成截断——2 个字符不可能是"生成到一半被 max_tokens 砍断"，
    更像网络抖动/限流吐回了几乎空的响应，应该走正常重试。"""
    from pydantic import ValidationError

    text = '{"'
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, Tiny) is False


def test_looks_like_truncated_json_still_detects_truncation_on_a_larger_schema():
    """长度门槛不是关掉截断判定——超过这个 schema 的最短合法长度、EOF 又在
    末尾时，仍然要判成截断。文本长度要超过 100 字符
    （core.llm.TRUNCATION_MIN_LENGTH），原因同上：更短的文本会被直接判成
    "太短"，测不到这条测试真正要测的"超过 schema 下限时仍要正确识别截断"
    这件事。"""
    from pydantic import ValidationError

    class Larger(BaseModel):
        thought: str = Field(min_length=1)
        action: str = Field(min_length=1)
        note: str = Field(min_length=1)

    text = (
        '{"thought": "先看看证素对应哪些证候，这一步的推理稍微长一点，'
        '多写几句话把这条测试的文本长度拉过这道绝对下限", '
        '"action": "query_graph", "note": "半路被砍'
    )
    assert len(text) > 100
    try:
        Larger.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, Larger) is True


# ---------- 宽松 schema（无 required 字段）：绝对下限兜底 ----------
#
# 上面用的 Tiny（ok: bool 必填 + note min_length=1 必填）最短合法长度
# 远大于 2，`'{"'` 天然小于它，测不出宽松 schema 上的问题——
# core/schemas.py::S2Elements 两个字段都有默认值、没有 required，
# _min_json_length 算出的下限本身就是 2，跟近空输入的长度打平
# （2 < 2 为 False），单靠 schema 下限完全挡不住。这里直接用真实 schema，
# 不用 Tiny 这种恰好有严格约束的替身。


def test_looks_like_truncated_json_rejects_near_empty_response_even_when_schema_has_no_required_fields():
    """核心回归：S2Elements 所有字段都有默认值，schema 结构下限只有 2——
    单靠这个下限判定 `'{"'`（2 字符）会因为 "2 < 2 为 False" 而误判成截断。
    TRUNCATION_MIN_LENGTH 这个绝对下限就是为了兜住这类"schema 越宽松、
    下限越没用"的场景：不管 schema 结构下限多低，都不能因为文本长度没跌破
    100 就直接放行判定。
    """
    from pydantic import ValidationError

    from core.schemas import S2Elements

    text = '{"'
    try:
        S2Elements.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, S2Elements) is False


def test_looks_like_truncated_json_still_detects_real_truncation_on_strict_schema():
    """绝对下限不能把真截断也放过：S3Syndrome 是这个项目里最重的输出 schema
    （结构下限 195，比绝对下限 TRUNCATION_MIN_LENGTH=100 更高——它自己的
    结构下限才是实际生效的门槛），构造一段超过这个门槛、EOF 落在末尾的
    截断文本，仍然要判定为截断。

    门槛要用 core.llm._truncation_length_threshold(S3Syndrome) 现算，不能
    只保证 >100——S3Syndrome 的实际门槛是 195，不是 100，文本长度卡在
    100~195 之间会被判成"太短"，那不是这条测试想测的东西（想测的是"超过
    门槛时仍要识别截断"），也不是真的 bug。"""
    from pydantic import ValidationError

    from core.llm import _truncation_length_threshold
    from core.schemas import S3Syndrome

    threshold = _truncation_length_threshold(S3Syndrome)
    filler = "肝气犯胃、横逆克伐、脾胃升降失常，"
    text = '{"syndrome_name": "肝胃不和证", "pathogenesis": "' + filler * (threshold // len(filler) + 2)
    assert len(text) > threshold
    try:
        S3Syndrome.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text, S3Syndrome) is True


def test_truncation_min_length_floor_covers_every_generate_schema():
    """全项目扫一遍所有真的会喂给 generate(schema=...) 的 pydantic 模型——不只
    core/schemas.py 里的（还有 eval/patient_sim.py::PatientAnswer、
    eval/sdt/adapter.py 的三个）——断言每一个的实际截断门槛都不低于
    TRUNCATION_MIN_LENGTH。防的是将来有人往 core/schemas.py 加一个全字段
    都有默认值的新 schema、又不小心把 TRUNCATION_MIN_LENGTH 改小或删掉这层
    max()：这条测试会先红。"""
    import inspect

    import core.schemas as schemas_module
    from core.llm import TRUNCATION_MIN_LENGTH, _truncation_length_threshold
    from eval.patient_sim import PatientAnswer
    from eval.sdt.adapter import CaseSummary, ExtractedInfo, SelectedOptions

    all_schemas = [
        obj for _, obj in vars(schemas_module).items()
        if inspect.isclass(obj) and issubclass(obj, BaseModel)
        and obj is not BaseModel and obj.__module__ == schemas_module.__name__
    ]
    all_schemas += [PatientAnswer, ExtractedInfo, SelectedOptions, CaseSummary]
    assert len(all_schemas) >= 30  # 防止 import 路径写错、悄悄扫到空列表就全绿

    for schema in all_schemas:
        assert _truncation_length_threshold(schema) >= TRUNCATION_MIN_LENGTH, schema.__name__


# ---------- _min_json_length / _min_plausible_output_length：纯函数 ----------


def test_min_plausible_output_length_matches_a_hand_built_minimal_instance():
    """Tiny（ok: bool 必填, note: str min_length=1 必填）的最短合法实例
    就是 {"ok":true,"note":"a"}，逐字数出来的长度必须跟估算值一致。"""
    from core.llm import _min_plausible_output_length

    minimal = '{"ok":true,"note":"a"}'
    assert _min_plausible_output_length(Tiny) == len(minimal)
    assert Tiny.model_validate_json(minimal)  # 顺带确认这确实是一个合法实例


def test_min_json_length_handles_array_enum_optional_and_ref():
    """跟其他几条纯算长度的测试不一样，这条直接拿一个手写的、能通过校验的
    最短实例字符串做基准（而不是手算每个字段的贡献再相加）——手算字符串
    长度这种"人肉数一遍"的活极易在多层嵌套时算错，一个真实、可校验的最短
    实例才是可信的对照物。"""
    from core.llm import _min_json_length

    class Item(BaseModel):
        name: str = Field(min_length=1)

    class Rich(BaseModel):
        items: list[Item] = Field(min_length=1)
        status: str  # 无约束的必填字符串，最短取空串
        source: Literal["classic", "modern"]
        note: str | None = None  # 有默认值，不在 required 里，不计入下界

    full = Rich.model_json_schema()
    defs = full.get("$defs", {})

    minimal = '{"items":[{"name":"a"}],"status":"","source":"modern"}'  # "modern" 比 "classic" 短
    assert Rich.model_validate_json(minimal)  # 确认这真的是一个合法实例（note 可省略）
    assert _min_json_length(full, defs) == len(minimal)
    # note 是可选字段（不在 required），漏了它不会让估算值变大
    assert "note" not in full.get("required", [])


def test_min_json_length_object_with_no_required_fields_is_empty_braces():
    from core.llm import _min_json_length

    class AllOptional(BaseModel):
        maybe: str | None = None

    full = AllOptional.model_json_schema()
    assert _min_json_length(full, full.get("$defs", {})) == 2  # "{}"


# ---------- 端到端：短垃圾响应走正常重试，不是直接判失败放弃 ----------


def test_generate_retries_short_garbage_instead_of_treating_it_as_truncated():
    """连续返回几乎空的响应（不是真截断）时，generate() 应该走正常的
    "回灌错误重试"路径，重试耗尽后报普通 LLMError（可以被上层
    当成"值得重跑"的失败），不是 LLMTruncatedError（那意味着"重试无意义，
    直接放弃"）。"""
    b = ScriptedBackend(['{"', '{"', '{"'])
    with pytest.raises(LLMError) as ei:
        b.generate(system="s", user="u", schema=Tiny)
    assert not isinstance(ei.value, LLMTruncatedError)
    assert len(b.calls) == 3  # 三次都重试了，不是撞截断直接放弃


def test_generate_raises_truncated_error_without_retrying():
    """截断了就直接失败，不重试三次——同样的输入会在同一处再次被截断，
    重试是白烧调用。这条用真实会撞到的场景构造：只给一次截断响应，
    如果代码还在重试就会 IndexError（脚本只有一条）而不是我们要的
    LLMTruncatedError，能确认"真的只调用了一次"。"""
    b = ScriptedBackend([_truncated_json_for(Tiny)])
    with pytest.raises(LLMTruncatedError) as ei:
        b.generate(system="s", user="u", schema=Tiny)
    assert len(b.calls) == 1  # 没有重试
    msg = str(ei.value)
    assert "截断" in msg
    assert "backend=scripted" in msg


def test_generate_truncated_error_message_distinguishes_unset_from_explicit_max_tokens():
    """max_tokens=None 不是"配置丢了"，是"这次调用没有显式传，会走后端自己的
    默认值"——报错里直接打 "max_tokens=None" 容易让人去查环境变量，其实哪儿
    都没错。未传时消息要说"未设置"，显式传了具体值时要如实显示那个值，
    不能两种情况都打印同一个 None。"""
    b = ScriptedBackend([_truncated_json_for(Tiny)])
    with pytest.raises(LLMTruncatedError) as ei:
        b.generate(system="s", user="u", schema=Tiny)  # 没传 max_tokens
    msg = str(ei.value)
    assert "max_tokens=未设置" in msg
    assert "max_tokens=None" not in msg

    b2 = ScriptedBackend([_truncated_json_for(Tiny)])
    with pytest.raises(LLMTruncatedError) as ei2:
        b2.generate(system="s", user="u", schema=Tiny, max_tokens=16384)
    msg2 = str(ei2.value)
    assert "max_tokens=16384" in msg2
    assert "未设置" not in msg2


def test_generate_truncated_error_is_also_an_llm_error():
    """子类关系：广义捕获 LLMError 的调用方不用为了这条改代码。"""
    assert issubclass(LLMTruncatedError, LLMError)
