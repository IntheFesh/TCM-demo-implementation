"""R36：S1+S2 合一（`S1S2_MERGED`）与这一轮的几个提速旋钮。

**这个文件最重要的一条是"为什么默认不开"有测试。** 合一之后证素推断跟症状
标准化在同一次调用里完成，而 CLAUDE.md 那条铁律要求危重症状的拦截发生在
**证素推断之前**。省一次 2~4 秒的调用不值得把一条结构性保证换成流程约定，
所以默认关；但那条路要完整、要能开（R38 的消融要用），所以下面既测了它跑得对，
也测了默认值是关的、以及开着的时候被拦截的请求照样不产出证素。
"""
from __future__ import annotations

import pytest

from core import chain
from core.llm import (
    S1S2_MERGED_DEFAULT,
    S3_BEST_OF_N_DEFAULT,
    load_prompt,
    s1s2_merged,
    s3_best_of_n,
    thinking_for,
)
from core.schemas import ElementHit, S1Normalize, S1S2Merged, S2Elements, S3Syndrome
from core.usage import (
    CALLS_PER_CONSULT_FIXED_STEPS,
    CALLS_PER_CONSULT_FIXED_STEPS_MERGED,
    calls_per_consult,
    fixed_steps_per_consult,
)

from tests.test_chain import FakeRetriever, _case


# ---------- 夹具 ----------

class MergeFakeLLM:
    """按 schema 返回预设响应，记下每次调用的 schema 名。"""

    def __init__(self, *, s3: S3Syndrome | None = None,
                 merged: S1S2Merged | None = None,
                 s1: S1Normalize | None = None, s2: S2Elements | None = None):
        self.calls: list[str] = []
        self.s3 = s3 or S3Syndrome(
            syndrome="脾胃气虚", reasoning="纳差乏力。", treatment_principle="健脾益气",
            formula="四君子汤", herbs=["党参", "白术"], cited_case_ids=["ye_tianshi-001"])
        self.merged = merged or S1S2Merged(
            symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[],
            elements=[ElementHit(element="脾", kind="location",
                                 supporting_symptoms=["纳差"], confidence="high")],
            unexplained_symptoms=["乏力"])
        self.s1 = s1 or S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红",
                                    pulse="细弱", unmapped=[])
        self.s2 = s2 or S2Elements(
            elements=[ElementHit(element="脾", kind="location",
                                 supporting_symptoms=["纳差"], confidence="high")],
            unexplained_symptoms=["乏力"])

    def model_name(self): return "merge-fake"
    def backend_id(self): return "fake"
    def comparability_warning(self): return "测试用"
    def lora_for(self, physician=None): return None
    def lora_dir(self): return None
    def replay_info(self): return None

    def generate(self, system, user, schema, **kw):
        self.calls.append(schema.__name__)
        if schema is S1S2Merged:
            return self.merged
        if schema is S1Normalize:
            return self.s1
        if schema is S2Elements:
            return self.s2
        return self.s3.model_copy(deep=True)


def _run(monkeypatch, llm, complaint="纳差乏力", **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["党参", "白术"])
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever([case]))
    return chain.consult(complaint)


# ---------- 一、默认值与理由 ----------

def test_the_merge_is_off_by_default():
    """**这条断言就是那条理由本身。** 改默认值的人会先撞到它，然后去读
    `core.chain.normalize_and_infer_merged` 的文档字符串，那里写着为什么。"""
    assert S1S2_MERGED_DEFAULT is False
    assert s1s2_merged() is False


@pytest.mark.parametrize("raw,want", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_the_switch_reads_the_usual_words(monkeypatch, raw, want):
    monkeypatch.setenv("S1S2_MERGED", raw)
    assert s1s2_merged() is want


def test_an_unrecognised_value_falls_back_with_a_warning(monkeypatch, capsys):
    """认不出的值只打一句 stderr 走默认——它不改下游拿到的形状，跟 S3_MODE
    （改 schema，拼错就抛）刻意区别对待。"""
    monkeypatch.setenv("S1S2_MERGED", "ture")
    assert s1s2_merged() is S1S2_MERGED_DEFAULT
    assert "S1S2_MERGED" in capsys.readouterr().err


def test_best_of_n_defaults_to_one_now():
    """R36 从 3 改成 1：挑分最高那一次的活由 R34 的符号验证器接了，
    两者叠着用等于同一件事付两次钱。"""
    assert S3_BEST_OF_N_DEFAULT == 1
    assert s3_best_of_n() == 1


def test_the_merged_step_keeps_thinking_off():
    """合的是两个结构化抽取，两边原来都关着思考；合起来开思考等于偷偷换实验条件。"""
    assert thinking_for("s1s2")["thinking"] == "disabled"
    assert thinking_for("s1")["thinking"] == "disabled"
    assert thinking_for("s2")["thinking"] == "disabled"


# ---------- 二、调用数只有一处折算 ----------

def test_the_fixed_step_count_follows_the_switch(monkeypatch):
    assert CALLS_PER_CONSULT_FIXED_STEPS == 2
    assert CALLS_PER_CONSULT_FIXED_STEPS_MERGED == 1
    monkeypatch.setenv("S1S2_MERGED", "0")
    assert fixed_steps_per_consult() == 2
    monkeypatch.setenv("S1S2_MERGED", "1")
    assert fixed_steps_per_consult() == 1


def test_the_quota_formula_uses_that_one_function(monkeypatch):
    """额度折算与链路结算问的必须是同一处，否则合一之后账本持续多扣一次，
    而多扣不会报错。"""
    monkeypatch.setenv("S1S2_MERGED", "1")
    assert calls_per_consult(5, 1, "structured") == 1 + 1
    monkeypatch.setenv("S1S2_MERGED", "0")
    assert calls_per_consult(5, 1, "structured") == 2 + 1


# ---------- 三、合一那条路跑得对 ----------

def test_merged_mode_makes_one_call_instead_of_two(monkeypatch):
    llm = MergeFakeLLM()
    out = _run(monkeypatch, llm, S1S2_MERGED="1")
    assert llm.calls.count("S1S2Merged") == 1
    assert "S1Normalize" not in llm.calls and "S2Elements" not in llm.calls
    assert out["manifest"]["s1s2_merged"] is True
    # 合一省下的正好是一次：这里在 conftest 钉住的 legacy 下跑（每位医家各一次
    # S3），所以只断言"S1/S2 这一段只花了 1 次"，不断言总数
    # ——总数的验收（≤4）在 structured 那条产品路径上测，见
    # tests/test_r36_acceptance.py。
    n_s3 = llm.calls.count("S3Syndrome") + llm.calls.count("S3SyndromeUnreferenced")
    # 残差那一步（有未解释症状时重跑一次 S2）也算一次——夹具里
    # `unexplained_symptoms=["乏力"]` 会触发它，所以要显式算进来，
    # 不然这条断言就在偷偷容忍一个漏算。
    n_residual = 1 if out["residual"] else 0
    assert out["manifest"]["llm_calls"] == 1 + n_residual + n_s3


def test_split_mode_still_makes_two(monkeypatch):
    llm = MergeFakeLLM()
    out = _run(monkeypatch, llm, S1S2_MERGED="0")
    assert llm.calls.count("S1Normalize") == 1
    assert llm.calls.count("S2Elements") == 1
    assert "S1S2Merged" not in llm.calls
    assert out["manifest"]["s1s2_merged"] is False


def test_both_paths_produce_the_same_shape(monkeypatch):
    """合一只是形状合一：拆出来的 s1/s2 跟分两次拿到的逐字段同型。"""
    llm = MergeFakeLLM()
    merged = _run(monkeypatch, llm, S1S2_MERGED="1")
    split = _run(monkeypatch, MergeFakeLLM(), S1S2_MERGED="0")
    assert merged["s1"].model_dump() == split["s1"].model_dump()
    assert merged["s2"].model_dump() == split["s2"].model_dump()


def test_to_s1_and_to_s2_split_without_loss():
    m = S1S2Merged(
        symptoms=["纳差"], tongue="淡红", pulse="细", unmapped=["三年前"],
        elements=[ElementHit(element="脾", kind="location",
                             supporting_symptoms=["纳差"], confidence="low")],
        unexplained_symptoms=["口苦"])
    assert m.to_s1() == S1Normalize(symptoms=["纳差"], tongue="淡红", pulse="细",
                                    unmapped=["三年前"])
    assert m.to_s2().unexplained_symptoms == ["口苦"]
    assert m.to_s2().elements[0].element == "脾"


def test_the_merged_schema_keeps_the_anti_hallucination_constraint():
    """`ElementHit.supporting_symptoms` 的 `min_length=1` 照旧生效——
    合并不放松任何约束（CLAUDE.md 那条铁律）。"""
    with pytest.raises(Exception):
        S1S2Merged(symptoms=["纳差"], elements=[{
            "element": "脾", "kind": "location",
            "supporting_symptoms": [], "confidence": "high"}])


def test_the_merged_prompt_carries_both_rule_sets():
    """合一的 prompt 是两份规则的并置，**一条没删**。抽查两边各自最要紧的那条。"""
    merged = load_prompt("s1s2_merged")["system"]
    assert "舌象、脉象单独提取到 tongue / pulse 字段" in merged      # 来自 s1
    assert "完全逐字相同" in merged                                  # 来自 s2
    assert "$complaint" in merged and "$elements" in merged
    # 两份原文照旧保留（S1S2_MERGED=0 走的是它们）
    assert load_prompt("s1_normalize")["system"]
    assert load_prompt("s2_elements")["system"]


# ---------- 四、合一模式下安全否决的语义不许变 ----------

def test_a_vetoed_request_still_yields_no_elements_when_merged(monkeypatch):
    """合一模式下证素**已经算出来了**，命中安全否决时必须丢掉、返回 s2=None
    ——对外可见的行为要跟分两次那条路逐字段一致。"""
    llm = MergeFakeLLM(merged=S1S2Merged(
        symptoms=["呕血", "乏力"], tongue=None, pulse=None, unmapped=[],
        elements=[ElementHit(element="胃", kind="location",
                             supporting_symptoms=["呕血"], confidence="high")],
        unexplained_symptoms=[]))
    out = _run(monkeypatch, llm, complaint="呕血", S1S2_MERGED="1")
    assert out["rejected"] is True
    assert out["s2"] is None, "被拦截的请求不产出证素"
    assert out["results"] == []
    assert llm.calls == ["S1S2Merged"], "S3 一次都不该调"
    # 这一段只花了一次调用，manifest 要如实记 1（分两次那条路在同一位置也是 1）
    assert out["manifest"]["llm_calls"] == 1


def test_the_split_path_vetoes_at_the_same_place(monkeypatch):
    llm = MergeFakeLLM(s1=S1Normalize(symptoms=["呕血"], tongue=None, pulse=None,
                                      unmapped=[]))
    out = _run(monkeypatch, llm, complaint="呕血", S1S2_MERGED="0")
    assert out["rejected"] is True and out["s2"] is None
    assert llm.calls == ["S1Normalize"], "S2 一次都不该调——拦截在证素推断之前"
    assert out["manifest"]["llm_calls"] == 1


def test_the_merge_does_not_touch_the_followup_rerun(monkeypatch):
    """追问之后只重跑 S2（`infer_elements`），那条路**不合并**：追问改的是症状
    集合的后验，症状标准化不必重做。"""
    import inspect

    src = inspect.getsource(chain.consult)
    assert "s2_pending if s2_pending is not None else infer_elements(s1)" in src
    # 追问后那一次重跑仍然是 infer_elements（不是又来一次合一调用）
    assert src.count("infer_elements(s1)") >= 2
