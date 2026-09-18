"""R53 第二相：符号验证扩到医理一致性——四条新规则，数据源是 `core/theory.py`
（R51 医理规则层），不是 `core/ontology.py`（本草/方剂本体）。

跟 `tests/test_formula_verifier.py` 的分工：那份文件测 R34 起（R59/R60 各拆出一条后九条）的本体规则
（含规则表的通用行为、`passed`/`status` 定义），这份文件专测 R53 新增的
`principle_matches_syndrome`/`method_not_contraindicated`/
`pathomechanism_consistent`/`role_structure_by_rule` 四条，以及它们跟本体
规则相互独立的可用性开关。

用**真实的** `data/standard/tcm_theory.jsonl`（不像本体那样构造合成数据）：
这四条规则本身就是在查那份表，构造一份假的医理规则表意义不大——真正要测的
是"给定这份真实规则表，四条判据的逻辑对不对"。
"""
from __future__ import annotations

import pytest

from core.formula_verifier import (
    ALL_RULES,
    ONTOLOGY_RULES,
    REVISE_RULES,
    RULE_FUNCS,
    RULE_LABELS,
    THEORY_RULES,
    verify_formula,
)
from core.ontology import Ontology
from core.schemas import S3Structured
from core.theory import load_theory


def mk(herbs, *, roles=None, organ="脾", second_organ=None,
       syn="脾胃气虚证", method="健脾益气", targets=("脾失健运",),
       effect="补中益气") -> S3Structured:
    roles = roles if roles is not None else ["君"] + ["臣"] * (len(herbs) - 1)
    items = [{"name": h, "dose": 9.0, "role": r} for h, r in zip(herbs, roles)]
    organs = [{"organ": organ, "supporting_symptoms": ["纳差"], "pathogenesis": "x"}]
    from_organs = [organ]
    if second_organ:
        organs.append({"organ": second_organ, "supporting_symptoms": ["乏力"],
                       "pathogenesis": "y"})
        from_organs.append(second_organ)
    return S3Structured(
        organs=organs,
        syndrome={"name": syn, "from_organs": from_organs, "reasoning": "x",
                  "reasoning_plain": "y"},
        method={"principle": method, "from_syndrome": syn, "targets": list(targets)},
        formula={"from_method": method, "candidate": {
            "name": "方", "source": "composed", "confidence": "high",
            "rationale": "x", "herb_items": items}},
        herb_choices=[{"item": it, "for_element": organ, "effect_cited": effect}
                      for it in items],
        physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                               "contribution": "x", "cited_case_ids": ["a"]}],
        cited_case_ids=["a"])


@pytest.fixture
def ont() -> Ontology:
    """空本体——这四条规则不查它，传个空的只是为了让 `verify_formula` 走完整流程。
    真要独立测规则函数本身，直接调 `RULE_FUNCS[...](s3, None)` 即可（这四条
    签名里的 `ont` 参数不用）。"""
    return Ontology(materia_rows=[], formulary_rows=[], patterns=[])


def _rules(result, severity=None):
    return sorted({v.rule for v in result.violations
                   if severity is None or v.severity == severity})


# ---------- 规则表结构 ----------

def test_theory_rules_is_exactly_four_and_all_revise():
    assert len(THEORY_RULES) == 4
    assert set(THEORY_RULES) == {
        "principle_matches_syndrome", "method_not_contraindicated",
        "pathomechanism_consistent", "role_structure_by_rule",
    }
    assert set(THEORY_RULES) <= set(REVISE_RULES), "R53 四条全是 revise，不是 veto"


def test_ontology_and_theory_rules_partition_all_rules():
    assert set(ONTOLOGY_RULES) | set(THEORY_RULES) == set(ALL_RULES)
    assert set(ONTOLOGY_RULES) & set(THEORY_RULES) == set(), "两张表不能有交集"
    assert len(ONTOLOGY_RULES) == 9 and len(THEORY_RULES) == 4


def test_every_theory_rule_has_a_chinese_label_and_an_implementation():
    for rule in THEORY_RULES:
        assert rule in RULE_LABELS and RULE_LABELS[rule].strip()
        assert rule in RULE_FUNCS


# ---------- principle_matches_syndrome ----------

def test_principle_matches_syndrome_passes_when_the_keyword_overlaps(ont):
    """脾对应的治则规则 method_keywords 含「健脾」，method.principle 正是
    「健脾益气」——对得上。"""
    r = verify_formula(mk(["党参"], organ="脾", method="健脾益气"), ontology=ont)
    assert "principle_matches_syndrome" not in _rules(r)
    assert "principle_matches_syndrome" in r.checked_rules


def test_principle_matches_syndrome_fires_when_nothing_overlaps(ont):
    r = verify_formula(mk(["党参"], organ="脾", syn="脾胃气虚证",
                          method="疏肝理气", targets=("肝气郁结",)), ontology=ont)
    assert "principle_matches_syndrome" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "principle_matches_syndrome")
    assert "疏肝理气" in v.reason
    assert v.counterexample.strip() and len(v.counterexample) > 5


def test_principle_matches_syndrome_is_unverifiable_without_a_matching_rule(ont):
    """脏腑不在医理规则层覆盖范围内时判不了，不是通过。"""
    r = verify_formula(mk(["党参"], organ="玄府", syn="玄府闭塞证"), ontology=ont)
    u = [u for u in r.unverifiable if u.rule == "principle_matches_syndrome"]
    assert len(u) == 1 and u[0].missing_predicate == "治则规则"


def test_principle_matches_syndrome_counterexample_cites_a_real_rule_id(ont):
    r = verify_formula(mk(["党参"], organ="脾", method="疏肝理气",
                          targets=("肝气郁结",)), ontology=ont)
    v = next(v for v in r.violations if v.rule == "principle_matches_syndrome")
    rule_ids = {rr.id for rr in load_theory()}
    cited = v.counterexample.split("]")[0].lstrip("[")
    assert cited in rule_ids, "反例要指着一条真实存在的规则 id"


# ---------- method_not_contraindicated ----------

def test_method_not_contraindicated_passes_by_default(ont):
    r = verify_formula(mk(["党参"], organ="脾", method="健脾益气",
                          targets=("脾失健运",)), ontology=ont)
    assert "method_not_contraindicated" not in _rules(r)
    assert "method_not_contraindicated" in r.checked_rules


def test_method_not_contraindicated_fires_when_a_forbidden_method_is_used(ont):
    """脾对应的「脾宜升则健」规则明确把「峻下」列为禁忌方法。"""
    r = verify_formula(mk(["党参"], organ="脾", method="健脾益气",
                          targets=("峻下",)), ontology=ont)
    assert "method_not_contraindicated" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "method_not_contraindicated")
    assert "峻下" in v.reason
    assert "ZZ-044" in v.counterexample or "峻下" in v.counterexample


def test_method_not_contraindicated_is_unverifiable_without_a_matching_rule(ont):
    r = verify_formula(mk(["党参"], organ="玄府", syn="玄府闭塞证"), ontology=ont)
    u = [u for u in r.unverifiable if u.rule == "method_not_contraindicated"]
    assert len(u) == 1 and u[0].missing_predicate == "治则规则"


def test_method_not_contraindicated_and_principle_matches_are_independent(ont):
    """治法可以既不在推荐关键词里、也没踩中禁忌——两条规则都不违规，这是
    合法状态（"依据不算强"是 S3Derived.insufficient 该报的事，不是这里）。"""
    r = verify_formula(mk(["党参"], organ="脾", method="平和调理",
                          targets=("脾失健运",)), ontology=ont)
    assert "principle_matches_syndrome" in _rules(r, "revise")
    assert "method_not_contraindicated" not in _rules(r)


# ---------- pathomechanism_consistent ----------

def test_pathomechanism_consistent_passes_trivially_with_a_single_organ(ont):
    r = verify_formula(mk(["党参"], organ="脾"), ontology=ont)
    assert "pathomechanism_consistent" not in _rules(r)
    assert "pathomechanism_consistent" in r.checked_rules


def test_pathomechanism_consistent_passes_when_organs_are_really_linked(ont):
    """肝→心是真实的藏象关系（ZX-001：木生火）。"""
    r = verify_formula(
        mk(["党参"], organ="肝", second_organ="心", syn="肝心两脏证"), ontology=ont)
    assert "pathomechanism_consistent" not in _rules(r)
    assert "pathomechanism_consistent" in r.checked_rules


def test_pathomechanism_consistent_fires_when_organs_are_unrelated(ont):
    """胃与大肠在医理规则层里没有任何关系能把它们连起来（实测：两者的
    `organ_relations`/`transitions` 均为空，见本文件顶部的说明）。"""
    r = verify_formula(
        mk(["党参"], organ="胃", second_organ="大肠", syn="胃肠同病证"), ontology=ont)
    assert "pathomechanism_consistent" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "pathomechanism_consistent")
    assert "胃" in v.reason and "大肠" in v.reason


def test_pathomechanism_consistent_checks_every_pair_not_just_the_first(ont):
    """脾胃是相邻脏腑（有真实关系），不该因为第一对凑巧无关就整体误判——
    这里用真实相关的一对再验一次正向情形，确保规则查的是"有没有任意一条
    关系"，不是漏看了第二个方向。"""
    r = verify_formula(
        mk(["党参"], organ="脾", second_organ="胃", syn="脾胃同病证"), ontology=ont)
    assert "pathomechanism_consistent" not in _rules(r)


# ---------- role_structure_by_rule ----------

def test_role_structure_by_rule_passes_when_the_chief_targets_an_organ(ont):
    r = verify_formula(mk(["党参"], organ="脾", roles=["君"]), ontology=ont)
    assert "role_structure_by_rule" not in _rules(r)
    assert "role_structure_by_rule" in r.checked_rules


def test_role_structure_by_rule_fires_when_the_chief_only_targets_a_derived_target(ont):
    """君药的 for_element 只落在 method.targets 的某条派生目标上，不落在
    辨出的脏腑上——君药理应针对主病机。"""
    v_func, u_func, c_func = RULE_FUNCS["role_structure_by_rule"](
        S3Structured(
            organs=[{"organ": "脾", "supporting_symptoms": ["纳差"], "pathogenesis": "x"}],
            syndrome={"name": "脾胃气虚证", "from_organs": ["脾"],
                     "reasoning": "x", "reasoning_plain": "y"},
            method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                   "targets": ["脾失健运", "兼夹湿滞"]},
            formula={"from_method": "健脾益气", "candidate": {
                "name": "方", "source": "composed", "confidence": "high",
                "rationale": "x", "herb_items": [{"name": "苍术", "role": "君"}]}},
            herb_choices=[{"item": {"name": "苍术", "role": "君"},
                          "for_element": "兼夹湿滞", "effect_cited": "燥湿"}],
            physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                                   "contribution": "x", "cited_case_ids": ["a"]}],
            cited_case_ids=["a"]),
        None)
    assert c_func == ["role_structure_by_rule"]
    assert v_func, "君药只针对派生 target、不针对脏腑，该报违规"
    assert "苍术" in v_func[0].herbs
    assert "PW-008" in v_func[0].counterexample or "君药" in v_func[0].counterexample


def test_role_structure_by_rule_is_checked_when_there_is_no_chief(ont):
    """没标君药：跟既有 `role_structure` 已经报过"role 全空/没有君药"不重复，
    这条算过、不违规。"""
    r = verify_formula(mk(["党参", "白术"], roles=["臣", "臣"]), ontology=ont)
    assert "role_structure_by_rule" not in _rules(r)
    assert "role_structure_by_rule" in r.checked_rules


def test_role_structure_by_rule_counterexample_quotes_the_definition_rule(ont):
    from core.theory import role_construction_rules
    chief = next(r for r in role_construction_rules() if r.payload["relation"] == "君药")
    assert "针对主病" in chief.span or "主证" in chief.span


# ---------- 数据源独立可用性 ----------

def test_theory_unavailable_puts_all_four_theory_rules_in_unverifiable(monkeypatch, ont):
    import core.formula_verifier as fv

    monkeypatch.setattr(fv, "load_theory", lambda: ())
    r = verify_formula(mk(["党参"], roles=["君"]), ontology=ont)
    assert r.theory_available is False
    got = {u.rule: u for u in r.unverifiable if u.rule in THEORY_RULES}
    assert set(got) == set(THEORY_RULES)
    for u in got.values():
        assert u.missing_predicate == "医理规则层数据"
        assert "tcm_theory.jsonl" in u.reason


def test_ontology_unavailable_does_not_block_theory_rules():
    """本体不可用不该连累医理规则层那四条——两个数据源独立，见
    `verify_formula` 的文档字符串（R53 的核心设计）。"""
    empty = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    r = verify_formula(mk(["党参"], organ="脾", method="健脾益气"), ontology=empty)
    assert r.ontology_available is False
    assert r.theory_available is True
    assert set(THEORY_RULES) <= set(r.checked_rules)


def test_the_four_theory_rules_also_work_on_s3_derived(ont):
    """R52 的 `S3Derived` 跟 `S3Structured` 字段名相同（organs/syndrome/method/
    herb_choices），这四条规则是鸭子类型——不用为 derived 模式另写一份。
    这里直接拿 R52 测试里验证过的合法 payload 构造一个 `S3Derived`，
    确认 `verify_formula` 对它同样能跑完四条新规则。"""
    from core.schemas import S3Derived
    from core.theory import load_theory as _lt

    def _ref(kind):
        rid = next(r.id for r in _lt() if r.kind == kind)
        return {"rule_id": rid, "note": "x"}

    s3 = S3Derived(
        organs=[{"organ": "脾", "supporting_symptoms": ["纳差"], "pathogenesis": "x",
                "rule_refs": [_ref("organ_relation")]}],
        syndrome={"name": "脾胃气虚证", "from_organs": ["脾"], "reasoning": "x",
                 "reasoning_plain": "y", "rule_refs": [_ref("pathomechanism")]},
        method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
               "targets": ["脾失健运"], "rule_refs": [_ref("treatment_principle")]},
        formula={"from_method": "健脾益气", "candidate": {
            "name": "方", "source": "composed", "confidence": "high",
            "rationale": "x", "herb_items": [{"name": "党参", "role": "君"}]},
            "rule_refs": [_ref("compatibility")]},
        herb_choices=[{"item": {"name": "党参", "role": "君"}, "for_element": "脾",
                      "effect_cited": "补中益气", "rule_refs": [_ref("compatibility")]}])
    r = verify_formula(s3, ontology=ont)
    assert set(THEORY_RULES) <= set(r.checked_rules)
    assert "principle_matches_syndrome" not in _rules(r)
    assert "role_structure_by_rule" not in _rules(r)
