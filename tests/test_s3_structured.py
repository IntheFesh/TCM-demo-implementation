"""R33 `S3Structured` schema：五步链 + 不可跳步 + 只出一张方。

**判据为什么在 schema 层而不是 prompt 层。** prompt 只能*请求*模型别跳步；
schema 能让跳了步的输出**根本构造不出来**，于是 `generate()` 的两次重试会把
具体的校验错误回灌给模型（CLAUDE.md 那条重试约定）。所以这个文件的主体是
「哪些输出必须被拒绝」，每一条都对应链上一个可以凭空出现的东西。
"""
from __future__ import annotations

import pydantic
import pytest

from core.schemas import (
    S3_CHAIN_STEPS,
    FormulaStep,
    HerbChoice,
    MethodStep,
    OntologyRef,
    OrganLocus,
    PhysicianInfluence,
    S3Structured,
    S3StructuredUnreferenced,
    S3Syndrome,
    S3SyndromeUnreferenced,
    SyndromeStep,
    _S3StructuredBase,
)


def _payload(**over) -> dict:
    """一份能通过全部校验的最小输入。每条测试只改它的一处。"""
    d = dict(
        organs=[{"organ": "脾", "supporting_symptoms": ["纳差", "乏力"],
                 "pathogenesis": "脾失健运，气血生化不足"}],
        syndrome={"name": "脾胃气虚证", "disease": "痞满", "from_organs": ["脾"],
                  "reasoning": "纳差乏力、舌淡脉细，脾失健运",
                  "reasoning_plain": "消化功能变弱了，吃得少也没力气"},
        method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                "targets": ["脾失健运"]},
        formula={"from_method": "健脾益气",
                 "candidate": {"name": "四君子汤", "source": "classic",
                               "confidence": "high", "rationale": "对应脾胃气虚",
                               "herb_items": [{"name": "党参", "dose": 9.0, "role": "君"}]}},
        herb_choices=[{"item": {"name": "党参", "dose": 9.0, "role": "君"},
                       "for_element": "脾", "effect_cited": "补中益气"}],
        physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                               "contribution": "用药轻灵，剂量偏小",
                               "cited_case_ids": ["ye_tianshi-001"]}],
        cited_case_ids=["ye_tianshi-001"],
    )
    d.update(over)
    return d


def _err(**over) -> str:
    with pytest.raises(pydantic.ValidationError) as e:
        S3Structured(**_payload(**over))
    return str(e.value)


# ---------- 合法构造与五步链本身 ----------

def test_a_valid_chain_constructs():
    s = S3Structured(**_payload())
    assert isinstance(s.organs[0], OrganLocus)
    assert isinstance(s.syndrome, SyndromeStep)
    assert isinstance(s.method, MethodStep)
    assert isinstance(s.formula, FormulaStep)
    assert isinstance(s.herb_choices[0], HerbChoice)
    assert isinstance(s.physician_influences[0], PhysicianInfluence)


def test_the_five_steps_come_from_the_application_document():
    """申报书 2.1：「病变脏腑-证型-治法-方剂-药物组成」。步名顺序即依赖顺序。"""
    assert S3_CHAIN_STEPS == ("organ", "syndrome", "method", "formula", "herbs")


def test_the_influence_step_literal_matches_the_chain_steps():
    """`PhysicianInfluence.step` 的 Literal 跟 `S3_CHAIN_STEPS` 是同一组值。
    pydantic 的 Literal 不能引用变量，只能写两遍——所以要一条测试钉住它们一致，
    不然改了一处另一处不会跟着改（第 31 条的一个变体）。"""
    import typing

    hints = typing.get_type_hints(PhysicianInfluence)
    assert set(typing.get_args(hints["step"])) == set(S3_CHAIN_STEPS)


def test_only_one_formula_not_a_candidate_list():
    """§0.1「不要给出多个答案」落在这里：`formula.candidate` 是**单个**，
    `S3Syndrome.formula_candidates` 那个 2–3 个候选的形状在这套 schema 里不存在。"""
    import typing

    hints = typing.get_type_hints(FormulaStep)
    assert hints["candidate"].__name__ == "FormulaCandidate"
    assert typing.get_origin(hints["candidate"]) is None, "不是 list——只出一张方"


# ---------- 不可跳步：四条 ----------

def test_syndrome_must_come_from_a_located_organ():
    msg = _err(syndrome={**_payload()["syndrome"], "from_organs": ["肝"]})
    assert "from_organs" in msg and "不能跳步" in msg


def test_syndrome_can_cite_several_organs_if_all_were_located():
    s = S3Structured(**_payload(
        organs=[{"organ": "脾", "supporting_symptoms": ["纳差"], "pathogenesis": "脾失健运"},
                {"organ": "肝", "supporting_symptoms": ["胁胀"], "pathogenesis": "肝气郁结"}],
        syndrome={**_payload()["syndrome"], "from_organs": ["脾", "肝"]},
    ))
    assert s.syndrome.from_organs == ["脾", "肝"]


def test_method_must_quote_the_syndrome_verbatim():
    """允许"包含"的话模型可以写「上述证型」，校验照样通过——而那正是跳步：
    这一步没有真的接住上一步的结论，只是提了一句。"""
    for bad in ("上述证型", "脾胃气虚", "脾胃气虚证（见上）"):
        msg = _err(method={**_payload()["method"], "from_syndrome": bad})
        assert "逐字相同" in msg, bad


def test_formula_must_quote_the_method_verbatim():
    msg = _err(formula={**_payload()["formula"], "from_method": "益气健脾"})
    assert "逐字相同" in msg


def test_every_herb_in_the_formula_needs_a_reason():
    msg = _err(formula={"from_method": "健脾益气",
                        "candidate": {"name": "四君子汤", "source": "classic",
                                      "confidence": "high", "rationale": "x",
                                      "herb_items": [{"name": "党参"}, {"name": "白术"}]}})
    assert "白术" in msg and "用药理由" in msg


def test_a_reason_for_a_herb_not_in_the_formula_is_rejected():
    msg = _err(herb_choices=[
        {"item": {"name": "党参"}, "for_element": "脾", "effect_cited": "补中益气"},
        {"item": {"name": "附子"}, "for_element": "脾", "effect_cited": "回阳救逆"},
    ])
    assert "附子" in msg and "并不在这张方" in msg


def test_both_directions_of_the_mismatch_are_reported_at_once():
    """错误会被 `generate()` 回灌给模型重试，而重试只有两次。只报一半的话
    它改完一半再撞另一半，白花一次重试。"""
    msg = _err(herb_choices=[{"item": {"name": "附子"}, "for_element": "脾",
                              "effect_cited": "回阳救逆"}])
    assert "党参" in msg and "附子" in msg
    assert "用药理由" in msg and "并不在这张方" in msg


# ---------- 来源校验：两条 ----------

def test_a_herb_cannot_target_a_pathogenesis_that_was_never_diagnosed():
    msg = _err(herb_choices=[{"item": {"name": "党参", "dose": 9.0, "role": "君"},
                              "for_element": "肾阳虚", "effect_cited": "补中益气"}])
    assert "肾阳虚" in msg and "无依据的加减" in msg


def test_a_herb_may_target_a_method_target_instead_of_an_organ():
    """`for_element` 的合法来源是「脏腑 ∪ 治法 targets」两者，不是只有脏腑。"""
    s = S3Structured(**_payload(
        method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                "targets": ["脾失健运", "气血不足"]},
        herb_choices=[{"item": {"name": "党参", "dose": 9.0, "role": "君"},
                       "for_element": "气血不足", "effect_cited": "补中益气"}],
    ))
    assert s.herb_choices[0].for_element == "气血不足"


def test_an_influence_must_cite_a_case_that_was_actually_retrieved():
    msg = _err(physician_influences=[{"physician": "li_ke", "step": "herbs",
                                      "contribution": "重用附子",
                                      "cited_case_ids": ["li_ke-999"]}])
    assert "li_ke-999" in msg and "指得出医案" in msg


# ---------- 防幻觉字段 ----------

def test_an_influence_without_a_case_id_is_rejected():
    """声称"综合了某位医家的经验"却指不出他哪一条医案，等于替他背书他没说过的话
    ——这是整份输出里最容易出现、也最难被看出来的一类编造，因为它读起来最像学术表述。"""
    msg = _err(physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                                      "contribution": "x", "cited_case_ids": []}])
    assert "cited_case_ids" in msg


def test_no_influences_at_all_is_rejected():
    """给不出任何一家的影响，这就不是综合分析，是模型自己开了个方。"""
    assert "physician_influences" in _err(physician_influences=[])


def test_empty_cited_case_ids_is_rejected():
    assert "cited_case_ids" in _err(cited_case_ids=[])


def test_no_organs_is_rejected():
    assert "organs" in _err(organs=[])


def test_an_organ_without_supporting_symptoms_is_rejected():
    """"病在脾"这个判断的依据只能是患者的症状。"""
    msg = _err(organs=[{"organ": "脾", "supporting_symptoms": [],
                        "pathogenesis": "脾失健运"}])
    assert "supporting_symptoms" in msg


def test_reasoning_plain_is_required_here_unlike_the_legacy_schema():
    """`_S3Base.reasoning_plain` 是可选的（不逼几十处旧构造补字段），
    这套 schema 没有历史构造点，所以从一开始就要求给——患者模式要显示的就是它。"""
    d = _payload()["syndrome"]
    del d["reasoning_plain"]
    assert "reasoning_plain" in _err(syndrome=d)


def test_an_ontology_ref_without_a_span_is_rejected():
    """一条"引用"如果说不出原文写了什么，它就不是引用，只是又一句模型自己的话。"""
    with pytest.raises(pydantic.ValidationError) as e:
        OntologyRef(kind="herb", name="党参", predicate="功效", span="")
    assert "span" in str(e.value)


def test_an_ontology_ref_may_omit_the_book():
    """`book` 刻意不设 min_length=1：模型看到的是知识块里那一段，不一定带书名。
    设了只会逼它编一个。真实性由 R34 回查本体核对。"""
    r = OntologyRef(kind="herb", name="党参", predicate="功效", span="补中益气")
    assert r.book is None


# ---------- 派生视图 ----------

def test_physicians_cited_dedups_and_keeps_first_appearance_order():
    s = S3Structured(**_payload(physician_influences=[
        {"physician": "li_ke", "step": "herbs", "contribution": "a",
         "cited_case_ids": ["ye_tianshi-001"]},
        {"physician": "ye_tianshi", "step": "formula", "contribution": "b",
         "cited_case_ids": ["ye_tianshi-001"]},
        {"physician": "li_ke", "step": "method", "contribution": "c",
         "cited_case_ids": ["ye_tianshi-001"]},
    ]))
    assert s.physicians_cited == ["li_ke", "ye_tianshi"]


def test_ontology_refs_merges_formula_and_herb_level_and_dedups():
    ref = {"kind": "herb", "name": "党参", "predicate": "功效", "span": "补中益气"}
    s = S3Structured(**_payload(
        formula={**_payload()["formula"], "ontology_refs": [
            {"kind": "formula", "name": "四君子汤", "predicate": "主治", "span": "脾胃气虚"}]},
        herb_choices=[{"item": {"name": "党参", "dose": 9.0, "role": "君"},
                       "for_element": "脾", "effect_cited": "补中益气",
                       "ontology_refs": [ref, dict(ref)]}],
    ))
    assert len(s.ontology_refs) == 2, "同一条引用写两遍只算一条"
    assert s.ontology_refs[0].kind == "formula", "方级在前"


def test_herbs_grounded_ratio_counts_choices_with_refs():
    base = _payload()
    two_herbs = dict(
        formula={"from_method": "健脾益气",
                 "candidate": {"name": "四君子汤", "source": "classic",
                               "confidence": "high", "rationale": "x",
                               "herb_items": [{"name": "党参"}, {"name": "白术"}]}},
        herb_choices=[
            {"item": {"name": "党参"}, "for_element": "脾", "effect_cited": "补中益气",
             "ontology_refs": [{"kind": "herb", "name": "党参", "predicate": "功效",
                                "span": "补中益气、健脾益肺"}]},
            {"item": {"name": "白术"}, "for_element": "脾", "effect_cited": "健脾益气"},
        ],
    )
    s = S3Structured(**{**base, **two_herbs})
    assert s.herbs_grounded_ratio() == 0.5
    assert S3Structured(**base).herbs_grounded_ratio() == 0.0


def test_the_zero_ratio_has_two_different_causes_and_the_docstring_says_so():
    """本体不在（模型没东西可引）和本体在、模型没引，是两件事。
    比率分不出来，靠 manifest 的 knowledge_entries.available 分。"""
    doc = _S3StructuredBase.herbs_grounded_ratio.__doc__ or ""
    assert "两种完全不同的原因" in doc
    assert "knowledge_entries.available" in doc


# ---------- 转成下游认识的形状 ----------

def test_to_s3_syndrome_gives_exactly_one_candidate():
    s3 = S3Structured(**_payload()).to_s3_syndrome()
    assert isinstance(s3, S3Syndrome)
    assert len(s3.formula_candidates) == 1 and s3.selected == 0
    assert s3.syndrome == "脾胃气虚证" and s3.disease == "痞满"
    assert s3.treatment_principle == "健脾益气"
    assert s3.herbs == ["党参"]
    assert s3.cited_case_ids == ["ye_tianshi-001"]


def test_to_s3_syndrome_puts_the_chain_and_the_influences_into_reasoning():
    """旧界面的证据链侧栏读的是 `reasoning`。不追加的话「融合了五家」这件事
    在 R37 之前完全看不见——新字段存在但没有界面读它，等于没做。"""
    s3 = S3Structured(**_payload()).to_s3_syndrome()
    assert "五步链条" in s3.reasoning
    assert "病变脏腑：脾" in s3.reasoning
    assert "名医思路影响" in s3.reasoning
    assert "ye_tianshi" in s3.reasoning and "ye_tianshi-001" in s3.reasoning
    assert s3.reasoning.startswith("纳差乏力"), "原推理仍在最前面"


def test_to_s3_syndrome_keeps_reasoning_plain_for_the_patient_role():
    s3 = S3Structured(**_payload()).to_s3_syndrome()
    assert s3.reasoning_plain == "消化功能变弱了，吃得少也没力气"


# ---------- 检索为空那条路 ----------

def test_the_unreferenced_variant_drops_both_citation_fields():
    """CLAUDE.md：新场景导致校验失败时**新建一个不含该字段的 schema**，
    不是放松原来的约束。这里一并去掉 `physician_influences`——一条"医家影响"
    必须指得出医案，而这个场景下一条医案都没有。"""
    d = _payload()
    del d["cited_case_ids"], d["physician_influences"]
    s = S3StructuredUnreferenced(**d)
    assert s.cited_case_ids == []
    assert s.physician_influences == []
    assert "cited_case_ids" not in s.model_dump()
    assert "physician_influences" not in s.model_dump()


def test_the_unreferenced_variant_still_enforces_the_whole_chain():
    """没有医案可引，不等于可以跳步。"""
    d = _payload(method={**_payload()["method"], "from_syndrome": "上述证型"})
    del d["cited_case_ids"], d["physician_influences"]
    with pytest.raises(pydantic.ValidationError) as e:
        S3StructuredUnreferenced(**d)
    assert "逐字相同" in str(e.value)


def test_the_unreferenced_variant_converts_to_the_unreferenced_legacy_schema():
    """引用为空时不给 `S3Syndrome` 塞一个假 id——那会把「这次没有任何医案支撑」
    这个信号洗掉，而它正是前端要明示的东西。"""
    d = _payload()
    del d["cited_case_ids"], d["physician_influences"]
    s3 = S3StructuredUnreferenced(**d).to_s3_syndrome()
    assert isinstance(s3, S3SyndromeUnreferenced)
    assert s3.cited_case_ids == []
    assert "名医思路影响" not in s3.reasoning, "一家都没有时不摆这个空标题"


def test_s3structured_is_not_a_subclass_of_the_legacy_base():
    """并列而非继承：两者字段形状不同，继承会让其中一个的约束污染另一个。"""
    from core.schemas import _S3Base

    assert not issubclass(S3Structured, _S3Base)
    assert not issubclass(S3Syndrome, _S3StructuredBase)


def test_the_legacy_schemas_were_not_touched():
    """§0.6 明确保留：`S3_MODE=legacy` 仍走 S3Syndrome，那两个类一个字没动。"""
    assert S3Syndrome.model_fields["cited_case_ids"].metadata, "min_length 约束要还在"
    fc = S3Syndrome.model_fields["formula_candidates"].metadata
    assert fc, "formula_candidates 的 2–3 个约束要还在"
