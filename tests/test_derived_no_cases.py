"""R52 第一相：演绎推导，`S3_MODE=derived`（产品默认）**看不到任何医案**。

跟 `tests/test_s3_mode.py` 的关系：那份文件测的是"三档怎么分派"，这份测的是
"derived 这一档本身的行为"——五步链没有 `cited_case_ids`/`physician_influences`/
`physician_source`/`dose_evidence`，依据换成 `rule_refs`/`insufficient`，
`_search_cases` 全程不被调用。

`tests/conftest.py` 的全局夹具把 `S3_MODE` 钉成 `legacy`，这里每条都显式
`S3_MODE=derived` 覆盖掉那个钉子（跟 `test_s3_mode.py` 的 `structured` 夹具
同一个理由）。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from core import chain
from core.physicians import physicians_for_mode
from core.schemas import (
    HerbChoiceDerived,
    InsufficientNote,
    OrganLocusDerived,
    S3Derived,
    S3SyndromeUnreferenced,
    TheoryRef,
)
from core.theory import load_theory
from core.usage import calls_per_consult

from tests.test_chain import FakeLLM


def _some_rule_id(kind: str) -> str:
    """规则库里任意一条给定 kind 的真实 id——不硬编码具体编号，规则库改了
    这个测试文件不用跟着改，只要那个 kind 还有至少一条规则。"""
    for r in load_theory():
        if r.kind == kind:
            return r.id
    raise AssertionError(
        f"data/standard/tcm_theory.jsonl 里没有 kind={kind!r} 的规则——"
        "R51 的产物是不是没生成？运行 python -m offline.extract_tcm_theory"
    )


def _ref(kind: str = "organ_relation", note: str = "符合规则里的条件") -> dict:
    return {"rule_id": _some_rule_id(kind), "note": note}


def _derived_payload(*, herbs=("党参", "白术"), organ="脾",
                     insufficient_step: str | None = None) -> dict:
    """一份能通过 `S3Derived` 全部校验的最小合法 payload。

    `insufficient_step` 传 "organs"/"syndrome"/"method"/"formula" 之一时，
    那一步改成 `insufficient` 而不是 `rule_refs`——用来测"依据不足"这条路径
    （不传 herb_choices，那个粒度更细，需要的场景由专门的测试自己构造）。
    """
    items = [{"name": h, "dose": 9.0, "role": "君" if i == 0 else "臣",
              "function_in_formula": "补气健脾"} for i, h in enumerate(herbs)]
    payload = {
        "organs": [{"organ": organ, "supporting_symptoms": ["纳差"],
                    "pathogenesis": f"{organ}失健运", "rule_refs": [_ref("organ_relation")]}],
        "syndrome": {"name": f"{organ}胃气虚证", "from_organs": [organ],
                     "reasoning": "推导过程：脏腑定位 -> 病机传变 -> 证型成立",
                     "reasoning_plain": "消化功能弱了",
                     "rule_refs": [_ref("pathomechanism")]},
        "method": {"principle": "健脾益气", "from_syndrome": f"{organ}胃气虚证",
                   "targets": [f"{organ}失健运"], "rule_refs": [_ref("treatment_principle")]},
        "formula": {"from_method": "健脾益气",
                    "candidate": {"name": "健脾方", "source": "composed", "confidence": "high",
                                  "rationale": "君臣佐使搭配得当", "herb_items": items},
                    "rule_refs": [_ref("compatibility")]},
        "herb_choices": [{"item": it, "for_element": organ, "effect_cited": "补中益气",
                          "rule_refs": [_ref("compatibility")]} for it in items],
    }
    if insufficient_step is not None:
        step = payload[insufficient_step]
        step["rule_refs"] = []
        step["insufficient"] = {"what": "规则表里没有能支持这一步的条目",
                                "missing_rule_kind": "organ_relation"}
    return payload


class DerivedFakeLLM(FakeLLM):
    """S1/S2 沿用 `FakeLLM`，S3 这一步按 `S3Derived` 返回结构化产出。"""

    def __init__(self, *args, payload=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.payload = payload
        self.s3_systems: list[str] = []
        self.s3_schemas: list[type] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        if schema is S3Derived:
            self.calls.append(schema.__name__)
            self.s3_systems.append(system)
            self.s3_schemas.append(schema)
            return S3Derived(**(self.payload or _derived_payload()))
        return super().generate(system, user, schema, temperature=temperature, **kwargs)


def _poison(name: str):
    """monkeypatch 目标：调用即失败，用来断言"这条路径这一相不该走到"。"""

    def _raise(*args, **kwargs):
        raise AssertionError(f"{name} 不该在 S3_MODE=derived 下被调用——这一相设计上不检索医案")

    return _raise


@pytest.fixture
def derived(monkeypatch):
    """钉成 derived + best_of_n=1，检索层全部毒化——真的被调用就让测试爆炸，
    而不是默默返回点什么让人看不出区别。

    **`CORROBORATION=off`**：这份文件测的是第一相「演绎推导」本身
    （`run_derivation` 从构造 prompt 到验证闭环那一段）看不看得到医案，
    不是第三相「医案佐证」（R54，`core/corroboration.py`）——那一相**故意**
    在推导定型之后调用 `_search_cases`，这份夹具的毒化如果连它也拦下来，
    测的就不再是这份文件标题说的那件事。R54 自己的测试
    （`tests/test_corroboration.py`）单独钉住佐证阶段的检索行为，
    默认 `CORROBORATION=on` 时它确实会调用 `_search_cases`。
    """
    monkeypatch.setenv("S3_MODE", "derived")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    monkeypatch.setenv("CORROBORATION", "off")
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "_search_cases", _poison("_search_cases"))
    monkeypatch.setattr(chain, "get_retriever", _poison("get_retriever"))
    return llm


# ---------- 一、_search_cases 全程不被调用 ----------

def test_search_cases_is_never_called(derived):
    """核心保证：poison 过的 `_search_cases` 一次都不该响。consult() 能正常
    跑完本身就是证据——真调用了会直接抛 AssertionError 而不是拿到结果。"""
    out = chain.consult("胃脘胀满，纳差乏力")
    assert out["manifest"]["s3_mode"] == "derived"
    assert len(out["results"]) == 1


def test_get_retriever_is_never_called(derived):
    out = chain.consult("胃脘胀满，纳差乏力")
    assert out["results"][0]["refs"] == []


def test_react_is_rejected_before_any_s3_call(derived):
    """接了 ReAct 等于从工具调用把医案检索请回来——必须在第一次**S3**调用之前
    就拒绝（S1/S2 仍会跑，那两步跟医案检索无关），不能等到工具真被调用才发现。"""
    with pytest.raises(ValueError, match="use_react"):
        chain.consult("胃脘胀满", use_react=True, s3_mode_override="derived")
    assert "S3Derived" not in derived.calls, "S3 这一步不该被调用"


# ---------- 二、返回值里没有案例字段的真实内容 ----------

def test_refs_is_always_empty(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["refs"] == []
    assert r["no_reference_cases"] is True
    assert r["low_discrimination"] is False


def test_physician_influences_key_is_absent_not_empty(derived):
    """R54 起彻底去掉这个键，不是留空列表占位——那个字段说的是"检索到的
    医案影响了推导过程"，这一相从设计上就没有这件事，键都不该出现。"""
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert "physician_influences" not in r
    assert r["physicians_cited"] == []


def test_hallucinated_is_always_empty(derived):
    """schema 校验已经把编造的 rule_id 挡在 `S3Derived` 能被构造出来之前，
    不存在"引了假规则却通过了校验"这种情况。"""
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["hallucinated"] == []


def test_react_trace_and_safety_flag_are_none(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["react_trace"] is None
    assert r["safety_flag"] is None


def test_the_physician_id_is_the_reserved_synthesis_id(derived):
    """derived 复用跟 structured 同一个保留身份——对外都是"这次问诊的一份
    结论，不挂在某位医家名下"。"""
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["physician"] == "synthesis"
    assert r["physician_name"] == "本次辨证"


# ---------- 三、S3Derived 原件与派生视图 ----------

def test_the_derived_result_carries_the_chain_original(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert isinstance(r["s3_structured"], S3Derived)
    assert r["s3_structured"].organs[0].organ == "脾"


def test_the_s3_view_is_unreferenced(derived):
    """`to_s3_syndrome()` 恒返回 `S3SyndromeUnreferenced`，不是 `S3Syndrome`
    ——后者要求非空 `cited_case_ids`，而这一相天生没有。"""
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert isinstance(r["s3"], S3SyndromeUnreferenced)
    assert r["s3"].cited_case_ids == []


def test_rule_refs_are_flattened_and_deduped(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert len(r["rule_refs"]) >= 1
    assert all("rule_id" in ref and "note" in ref for ref in r["rule_refs"])


def test_insufficient_notes_reported_when_a_step_lacks_rules(monkeypatch):
    monkeypatch.setenv("S3_MODE", "derived")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    monkeypatch.setenv("CORROBORATION", "off")
    llm = DerivedFakeLLM({}, payload=_derived_payload(insufficient_step="method"))
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "_search_cases", _poison("_search_cases"))
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert len(r["insufficient_notes"]) == 1
    assert r["insufficient_notes"][0]["missing_rule_kind"] == "organ_relation"
    assert r["derivation_completeness_ratio"] < 1.0


def test_derivation_completeness_ratio_is_one_when_every_step_cites_a_rule(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["derivation_completeness_ratio"] == 1.0


def test_herbs_grounded_ratio_is_zero_without_ontology_refs(derived):
    """假 LLM 的 payload 没给 `ontology_refs`——这个比率报 0 是诚实的，
    不是 bug（跟 `S3Structured` 那边同一条语义）。"""
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["herbs_grounded_ratio"] == 0.0


# ---------- 四、theory/knowledge 统计与验证闭环 ----------

def test_theory_stats_are_reported(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert "theory" in r
    assert r["theory"]["available"] is True
    assert r["theory"]["n_rules"] >= 1


def test_knowledge_stats_are_reported(derived):
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert "knowledge" in r
    assert "available" in r["knowledge"]


def test_verification_closure_still_runs(derived):
    """R34 的验证闭环对 `S3Derived` 原样生效（鸭子类型）——即便本体不可用，
    `verifier_metrics` 也该有一个确定性的结构，不是缺字段。"""
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["verification"] is not None
    assert r["verifier_metrics"]["n_rounds"] >= 1


def test_manifest_synthesis_summary_is_none_for_derived(derived):
    """`_synthesis_summary` 只答 structured 的那个问题（哪几家真的影响了结论）
    ——derived 没有"家"这个概念，返回 None 是"这个问题不适用"，不是漏算。"""
    out = chain.consult("胃脘胀满，纳差乏力")
    assert out["manifest"]["synthesis"] is None


# ---------- 五、医家名单与额度折算：derived 下没有"医家"这个维度 ----------

def test_no_physicians_participate_in_derivation():
    assert physicians_for_mode("derived") == {}


def test_calls_per_consult_does_not_multiply_by_physician_count():
    """derived 下没有医家数这回事——跟 structured 走同一条"一次调用产出一份
    结论"的公式，不是意外地被并进 legacy 那条按医家数相乘的分支。"""
    assert calls_per_consult(mode="derived", best_of_n=1) == 3
    assert calls_per_consult(mode="derived", best_of_n=3) == 5
    assert calls_per_consult(mode="structured", best_of_n=3) == \
        calls_per_consult(mode="derived", best_of_n=3)


def test_lora_lookup_uses_the_reserved_physician_id(derived, monkeypatch):
    seen: list[str | None] = []
    orig_lora_for = derived.lora_for

    def _tracking_lora_for(physician):
        seen.append(physician)
        return orig_lora_for(physician)

    monkeypatch.setattr(derived, "lora_for", _tracking_lora_for)
    chain.consult("胃脘胀满，纳差乏力")
    assert "synthesis" in seen


# ---------- 六、schema 层：TheoryRef / InsufficientNote 的防幻觉校验 ----------

def test_a_fabricated_rule_id_is_rejected_at_construction():
    with pytest.raises(ValidationError, match="ZX-99999"):
        TheoryRef(rule_id="ZX-99999", note="编的")


def test_a_real_rule_id_is_accepted():
    ref = TheoryRef(rule_id=_some_rule_id("organ_relation"), note="真实存在")
    assert ref.rule_id


def test_a_step_needs_rule_refs_or_insufficient_not_neither():
    with pytest.raises(ValidationError, match="依据不足"):
        OrganLocusDerived(organ="脾", supporting_symptoms=["纳差"], pathogenesis="脾失健运")


def test_insufficient_alone_is_enough():
    step = OrganLocusDerived(
        organ="脾", supporting_symptoms=["纳差"], pathogenesis="脾失健运",
        insufficient=InsufficientNote(what="缺一条脾相关的藏象规则",
                                      missing_rule_kind="organ_relation"))
    assert step.rule_refs == []
    assert step.insufficient is not None


# ---------- 六、五步链每一步单独钉住"不存在没依据但给了结论"这第三种状态 ----------
#
# `rule_refs` 字段本身用 `Field(default_factory=list)` 而不是逐字面的
# `Field(min_length=1)`——后者会让 `insufficient` 这条逃生舱结构上永远走不到
# （字段级约束不看别的字段，min_length=1 会无条件拒绝空列表，不管
# insufficient 填没填）。真正的约束在 `_cites_or_flags` 这条跨字段校验器上：
# rule_refs 非空、或 insufficient 非空，二选一，两者都空才拒绝——这五条
# 逐个把这件事钉死，不靠读代码猜。

def test_syndrome_step_derived_rejects_neither():
    with pytest.raises(ValidationError, match="依据不足"):
        from core.schemas import SyndromeStepDerived

        SyndromeStepDerived(name="脾胃气虚证", from_organs=["脾"],
                            reasoning="x", reasoning_plain="y")


def test_method_step_derived_rejects_neither():
    with pytest.raises(ValidationError, match="依据不足"):
        from core.schemas import MethodStepDerived

        MethodStepDerived(principle="健脾益气", from_syndrome="脾胃气虚证",
                          targets=["脾失健运"])


def test_formula_step_derived_rejects_neither():
    with pytest.raises(ValidationError, match="依据不足"):
        from core.schemas import FormulaStepDerived

        FormulaStepDerived(from_method="健脾益气", candidate={
            "name": "方", "source": "composed", "confidence": "high",
            "rationale": "x", "herb_items": [{"name": "党参"}]})


def test_herb_choice_derived_rejects_neither():
    with pytest.raises(ValidationError, match="依据不足"):
        HerbChoiceDerived(item={"name": "党参"}, for_element="脾", effect_cited="补中益气")


def test_organ_locus_derived_rejects_neither_again_with_a_different_organ():
    """跟顶部 `test_a_step_needs_rule_refs_or_insufficient_not_neither` 是同一个
    断言点，换一个脏腑重复一遍——防止前一条测试的通过是因为「脾」这个具体值
    走了什么特殊分支（`_cites_or_flags` 不该按 organ 的值分支）。"""
    with pytest.raises(ValidationError, match="依据不足"):
        OrganLocusDerived(organ="肝", supporting_symptoms=["胁痛"], pathogenesis="肝失疏泄")


def test_a_full_s3_derived_is_rejected_if_any_single_step_omits_both():
    """五步链整体构造：其余四步都给全了依据，只有 method 这一步既没引规则
    也没标 insufficient——整个 `S3Derived` 必须在这一步上就被拒绝，不能因为
    "别的步骤都合规"就放过它。这是"不存在没依据但给了结论"这条铁律在
    完整链条层面的钉子，不只是单个步骤类的钉子。"""
    ref = {"rule_id": _some_rule_id("organ_relation"), "note": "x"}
    with pytest.raises(ValidationError, match="依据不足"):
        S3Derived(
            organs=[{"organ": "脾", "supporting_symptoms": ["纳差"],
                    "pathogenesis": "脾失健运", "rule_refs": [ref]}],
            syndrome={"name": "脾胃气虚证", "from_organs": ["脾"], "reasoning": "x",
                     "reasoning_plain": "y", "rule_refs": [ref]},
            # method 这一步既没有 rule_refs 也没有 insufficient——五步里唯一
            # 违规的一步，仍然要让整个 S3Derived 构造失败。
            method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                   "targets": ["脾失健运"]},
            formula={"from_method": "健脾益气", "candidate": {
                "name": "方", "source": "composed", "confidence": "high",
                "rationale": "x", "herb_items": [{"name": "党参"}]}, "rule_refs": [ref]},
            herb_choices=[{"item": {"name": "党参"}, "for_element": "脾",
                          "effect_cited": "补中益气", "rule_refs": [ref]}])


def test_herb_choice_derived_has_no_physician_source_field():
    """`HerbChoiceDerived` 没有 `physician_source`——这是从设计上消除的字段，
    不是留着不填。"""
    assert "physician_source" not in HerbChoiceDerived.model_fields


def test_s3_derived_has_no_top_level_case_fields():
    """`S3Structured` 顶层有 `physician_influences`/`cited_case_ids`
    （`Field(min_length=1)`）——`S3Derived` 顶层字段集里两个都不存在，不是
    存在但放宽成了可选（CLAUDE.md 那条铁律：不许把必填改成可选，这里是
    干脆没有这个字段，跟"新建 schema 不放松旧约束"是一回事）。"""
    assert "cited_case_ids" not in S3Derived.model_fields
    assert "physician_influences" not in S3Derived.model_fields
    # 医案相关字段**逐个点名**，不再断言"字段集恰好等于这六个"。
    # R62 §3.2 往顶层加了 key_points/differential/modifications/self_assessment
    # 四项，跟医案一点关系都没有；一条"字段集必须一字不差"的断言会在每一次
    # 正常的新增上变红，而它本来要守的东西（这一相看不到医案）根本没被碰。
    # 判据改成"这几个名字一个都不许出现"，加字段不会误红，真把医案字段抄回来
    # 会当场红。
    for banned in ("cited_case_ids", "physician_influences", "physician_source",
                   "dose_evidence", "refs", "retrieved_case_ids"):
        assert banned not in S3Derived.model_fields, (
            f"{banned} 是医案检索那条路径上的字段，演绎推导这一相不该有它"
        )


def test_s3_derived_carries_the_four_r62_sections():
    """R62 §3.2 的四项在顶层，且**都带默认值**——录制于 R52~R61 的 fixture
    里没有这四个字段，必填会让每一份历史录制当场解析失败。"""
    for name in ("key_points", "differential", "modifications", "self_assessment"):
        assert name in S3Derived.model_fields
        assert not S3Derived.model_fields[name].is_required(), (
            f"{name} 不该是必填：R52~R61 的录制语料里没有这个字段"
        )
