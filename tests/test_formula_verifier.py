"""R34 符号验证器七条规则。

**每条规则都要能指着本体原文说出哪里不对**——那是 LLM-Modulo 那条实证
（幻觉 63% → 1.7%）的机制所在，也是这一层跟既有两层（safety_output 判"能不能发"、
formula_check 判"拟得好不好"）的分界线。所以这个文件里每一条断言违规的测试，
都同时断言 `counterexample` 里有本体原文。

数据用**就地构造的合成本体**：真实的两份 jsonl 在版本控制里（R34 起），但测试
不该依赖它们的具体内容——那份数据会随抽取脚本变，而这些测试要钉的是规则逻辑。
另有一组显式标 `real_ontology` 的测试拿真数据跑，验的是"规则在真实覆盖率下
会落进哪一档"。
"""
from __future__ import annotations

import pytest

from core.formula_verifier import (
    ALL_RULES,
    MAX_REVISE_ROUNDS,
    REVISE_RULES,
    THEORY_RULES,
    VETO_RULES,
    RULE_FUNCS,
    Unverifiable,
    VerificationResult,
    Violation,
    format_violations_for_revise,
    max_revise_rounds,
    ontology_coverage_of_corpus,
    verifier_metrics,
    verify_formula,
)
from core.ontology import Ontology
from core.schemas import S3Structured


# ---------- 夹具 ----------

def _row(s, p, o, *, book="中药学"):
    return {"s": s, "p": p, "o": o, "book": book, "source": "modern",
            "source_span": f"【{p}】{o}"}


@pytest.fixture
def ont() -> Ontology:
    """一份够小、够全的合成本体：四味药，性味/归经/功效/用量齐备。"""
    return Ontology(materia_rows=[
        _row("党参", "性味", "甘，平"), _row("党参", "归经", "归脾、肺经"),
        _row("党参", "功效", "补中益气、健脾益肺"), _row("党参", "用量", "9~30g"),
        _row("白术", "性味", "苦、甘，温"), _row("白术", "归经", "归脾、胃经"),
        _row("白术", "功效", "健脾益气、燥湿利水"), _row("白术", "用量", "6~12g"),
        _row("柴胡", "性味", "苦、辛，微寒"), _row("柴胡", "归经", "归肝、胆经"),
        _row("柴胡", "功效", "疏肝解郁、和解表里"), _row("柴胡", "用量", "3~10g"),
        _row("石膏", "性味", "辛、甘，大寒"), _row("石膏", "归经", "归肺、胃经"),
        _row("石膏", "功效", "清热泻火、除烦止渴"), _row("石膏", "用量", "15~60g"),
    ], formulary_rows=[], patterns=[])


def mk(herbs, *, roles=None, doses=None, refs=None, organ="脾",
       syn="脾胃气虚证", method="健脾益气", targets=("脾失健运",),
       effect="补中益气") -> S3Structured:
    roles = roles if roles is not None else ["君"] + ["臣"] * (len(herbs) - 1)
    doses = doses if doses is not None else [9.0] * len(herbs)
    items = [{"name": h, "dose": d, "role": r} for h, d, r in zip(herbs, doses, roles)]
    return S3Structured(
        organs=[{"organ": organ, "supporting_symptoms": ["纳差"],
                 "pathogenesis": "脾失健运"}],
        syndrome={"name": syn, "from_organs": [organ], "reasoning": "x",
                  "reasoning_plain": "y"},
        method={"principle": method, "from_syndrome": syn, "targets": list(targets)},
        formula={"from_method": method, "candidate": {
            "name": "方", "source": "composed", "confidence": "high",
            "rationale": "x", "herb_items": items}},
        herb_choices=[{"item": it, "for_element": organ, "effect_cited": effect,
                       "ontology_refs": (refs or {}).get(it["name"], [])}
                      for it in items],
        physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                               "contribution": "x", "cited_case_ids": ["a"]}],
        cited_case_ids=["a"])


def _rules(result, severity=None):
    return sorted({v.rule for v in result.violations
                   if severity is None or v.severity == severity})


# ---------- 规则表本身 ----------

def test_eleven_rules_split_into_three_veto_and_eight_revise():
    """R53：七条（R34，本体）扩到十一条（+ 四条 R53 医理一致性规则）。"""
    assert len(ALL_RULES) == 11
    assert len(VETO_RULES) == 3 and len(REVISE_RULES) == 8
    assert set(VETO_RULES) == {"incompatible_pair", "dose_exceeds", "herb_grounded"}
    assert set(REVISE_RULES) == {
        "meridian_coverage", "nature_conflict", "effect_matches_method", "role_structure",
        "principle_matches_syndrome", "method_not_contraindicated",
        "pathomechanism_consistent", "role_structure_by_rule",
    }
    assert set(VETO_RULES) & set(REVISE_RULES) == set(), "一条规则不能又是 veto 又是 revise"


def test_every_rule_has_an_implementation_and_vice_versa():
    assert set(RULE_FUNCS) == set(ALL_RULES)
    assert list(RULE_FUNCS) == list(ALL_RULES), "字典顺序要跟 ALL_RULES 一致（报告顺序）"


def test_severity_is_a_property_of_the_rule_not_of_the_instance():
    """级别写死在规则上：同一条规则在两处有两种后果的话，"要不要下发"
    就取决于是谁构造了那个 Violation。"""
    with pytest.raises(ValueError) as e:
        Violation(rule="incompatible_pair", severity="revise", herbs=("甘草",),
                  reason="x", counterexample="y")
    assert "级别固定是 veto" in str(e.value)
    with pytest.raises(ValueError):
        Violation(rule="role_structure", severity="veto", herbs=(), reason="x",
                  counterexample="y")


def test_an_unknown_rule_is_rejected():
    with pytest.raises(ValueError):
        Violation(rule="我编的规则", severity="veto", herbs=(), reason="x",
                  counterexample="y")
    with pytest.raises(ValueError):
        Unverifiable(rule="我编的规则", herbs=(), missing_predicate="x", reason="y")


def test_an_empty_counterexample_is_rejected():
    """一条要回灌给模型的违规必须能指着本体原文说出哪里不对。"""
    for blank in ("", "   ", "\n"):
        with pytest.raises(ValueError) as e:
            Violation(rule="role_structure", severity="revise", herbs=(),
                      reason="没有君药", counterexample=blank)
        assert "空反例等于没有反例" in str(e.value)


def test_unverifiable_requires_saying_what_is_missing():
    for kw, reason in (("", "有理由"), ("归经", "")):
        with pytest.raises(ValueError):
            Unverifiable(rule="meridian_coverage", herbs=(), missing_predicate=kw,
                         reason=reason)


# ---------- veto 三条 ----------

def test_incompatible_pair_vetoes_and_quotes_the_pair(ont):
    r = verify_formula(mk(["甘草", "甘遂", "白术"]), ontology=ont)
    assert "incompatible_pair" in _rules(r, "veto")
    v = next(v for v in r.violations if v.rule == "incompatible_pair")
    assert set(v.herbs) == {"甘草", "甘遂"}
    assert v.counterexample.strip()
    assert r.status == "vetoed" and r.passed is False


def test_incompatible_pair_delegates_to_safety_output_not_a_second_table(ont):
    """24 对全过一遍。**判据整个来自 `core.safety_output.INCOMPATIBLE_PAIRS`**
    ——验证器不持第二份配伍表（第 31 条）。"""
    from core.safety_output import INCOMPATIBLE_PAIRS

    src = (__import__("pathlib").Path("core/formula_verifier.py")
           .read_text(encoding="utf-8"))
    assert "INCOMPATIBLE_PAIRS = " not in src, "不许在验证器里重新定义配伍表"
    assert "check_incompatible" in src
    missed = []
    for pair in INCOMPATIBLE_PAIRS:
        a, b = sorted(pair)
        r = verify_formula(mk([a, b, "白术"]), ontology=ont)
        if "incompatible_pair" not in _rules(r, "veto"):
            missed.append((a, b))
    assert missed == [], f"这些配伍对没被查出来：{missed}"


def test_dose_exceeds_vetoes_with_the_limit_and_the_prescribed_amount(ont):
    r = verify_formula(mk(["附子", "白术"], doses=[60.0, 9.0]), ontology=ont)
    assert "dose_exceeds" in _rules(r, "veto")
    v = next(v for v in r.violations if v.rule == "dose_exceeds")
    assert "60.0" in v.counterexample and "15.0g" in v.counterexample
    assert "上限" in v.counterexample


def test_a_dose_within_the_limit_does_not_fire(ont):
    r = verify_formula(mk(["附子", "白术"], doses=[9.0, 9.0]), ontology=ont)
    assert "dose_exceeds" not in _rules(r)


def test_a_missing_dose_is_unverifiable_not_a_pass(ont):
    """「没写剂量」不是「剂量合规」。"""
    s3 = mk(["附子", "白术"], doses=[None, 9.0])
    r = verify_formula(s3, ontology=ont)
    assert "dose_exceeds" not in _rules(r)
    u = [u for u in r.unverifiable if u.rule == "dose_exceeds"]
    assert len(u) == 1 and u[0].herbs == ("附子",)
    assert u[0].missing_predicate == "剂量"
    assert "不是" in u[0].reason


def test_herb_grounded_vetoes_a_fabricated_span(ont):
    """引用了本体里不存在的原文 = 编造出处。"""
    refs = {"党参": [{"kind": "herb", "name": "党参", "predicate": "功效",
                      "span": "回阳救逆、通经活络"}]}
    r = verify_formula(mk(["党参", "白术"], refs=refs), ontology=ont)
    assert "herb_grounded" in _rules(r, "veto")
    v = next(v for v in r.violations if v.rule == "herb_grounded")
    assert "回阳救逆" in v.counterexample, "要把模型写的那段引出来"
    assert "补中益气" in v.counterexample, "也要把本体里的真原文引出来"
    assert v.refs and v.refs[0].span == "回阳救逆、通经活络"


def test_herb_grounded_accepts_a_real_span_even_if_only_partially_quoted(ont):
    """span 用双向子串比而不是相等：本体原文往往是一整段，模型照抄时可能只抄一句，
    要求逐字相等会把正确的引用判成编造。"""
    for span in ("补中益气", "【功效】补中益气、健脾益肺", "补中益气、健脾益肺"):
        refs = {"党参": [{"kind": "herb", "name": "党参", "predicate": "功效",
                          "span": span}]}
        r = verify_formula(mk(["党参"], refs=refs), ontology=ont)
        assert "herb_grounded" not in _rules(r), f"「{span}」被误判成编造"


def test_a_herb_absent_from_the_ontology_is_unverifiable_not_a_veto(ont):
    """**这是 R34 最要紧的一条设计**：实测 `cases.json` 里归一后 578 种药名，
    本体查得到 230 种（39.8%）。把"不在本体里"当 veto，几乎每张方都会被否掉
    ——而那不是模型的错，是药理层覆盖不全这个数据事实。"""
    r = verify_formula(mk(["鲤鱼", "党参"]), ontology=ont)
    assert "herb_grounded" not in _rules(r, "veto")
    u = [u for u in r.unverifiable
         if u.rule == "herb_grounded" and u.herbs == ("鲤鱼",)]
    assert len(u) == 1
    assert u[0].missing_predicate == "本体条目"
    assert "不是模型编造" in u[0].reason


def test_a_herb_with_no_refs_at_all_is_unverifiable(ont):
    r = verify_formula(mk(["党参"]), ontology=ont)
    u = [u for u in r.unverifiable if u.rule == "herb_grounded"]
    assert len(u) == 1 and u[0].missing_predicate == "ontology_refs"
    assert "不算编造，算没引" in u[0].reason


def test_a_ref_to_a_predicate_the_ontology_lacks_is_unverifiable(ont):
    """模型引了「党参的禁忌」，而本体里党参没有禁忌这一项——对不了，不是编造。"""
    refs = {"党参": [{"kind": "herb", "name": "党参", "predicate": "禁忌",
                      "span": "孕妇慎用"}]}
    r = verify_formula(mk(["党参"], refs=refs), ontology=ont)
    assert "herb_grounded" not in _rules(r, "veto")
    assert any(u.missing_predicate == "禁忌" for u in r.unverifiable)


# ---------- revise 四条 ----------

def test_meridian_coverage_fires_when_no_herb_reaches_the_organ(ont):
    r = verify_formula(mk(["党参", "白术"], organ="肝", syn="肝郁气滞证",
                          method="疏肝理气", targets=("肝气郁结",)), ontology=ont)
    assert "meridian_coverage" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "meridian_coverage")
    assert "归脾、肺经" in v.counterexample or "归脾、胃经" in v.counterexample
    assert r.status == "revise_needed"


def test_meridian_coverage_passes_when_one_herb_reaches_it(ont):
    r = verify_formula(mk(["柴胡", "党参"], organ="肝", syn="肝郁气滞证",
                          method="疏肝理气", targets=("肝气郁结",),
                          effect="疏肝解郁"), ontology=ont)
    assert "meridian_coverage" not in _rules(r)
    assert "meridian_coverage" in r.checked_rules


def test_meridian_coverage_is_unverifiable_when_no_herb_has_a_meridian():
    """**一味都判不了的时候不出违规**：那时"没覆盖"只是"不知道"，
    报一条违规会让模型去改一个本来可能对的方。"""
    thin = Ontology(materia_rows=[_row("鲤鱼", "功效", "利水消肿")],
                    formulary_rows=[], patterns=[])
    r = verify_formula(mk(["鲤鱼"]), ontology=thin)
    assert "meridian_coverage" not in _rules(r)
    assert "meridian_coverage" not in r.checked_rules
    assert any(u.rule == "meridian_coverage" and u.missing_predicate == "归经"
               for u in r.unverifiable)


def test_meridian_coverage_is_unverifiable_when_the_organ_is_not_a_known_location(ont):
    r = verify_formula(mk(["党参"], organ="玄府", syn="玄府闭塞"), ontology=ont)
    assert any(u.rule == "meridian_coverage" and u.missing_predicate == "病位"
               for u in r.unverifiable)


def test_nature_conflict_fires_with_the_nature_spans(ont):
    r = verify_formula(mk(["石膏", "石膏", "石膏", "石膏", "石膏"],
                          syn="脾胃虚寒证", method="温中散寒", targets=("中焦寒盛",),
                          effect="清热泻火"), ontology=ont)
    # 同一味药重复五次：check_thermal_consistency 看主方前 6 味的寒凉药占比
    assert "nature_conflict" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "nature_conflict")
    assert "大寒" in v.counterexample, "反例要带本草性味原文"


def test_nature_conflict_is_unverifiable_when_the_syndrome_has_no_direction(ont):
    """证型没有明确寒热方向、或寒热错杂时这条规则**不适用**——不是通过。"""
    r = verify_formula(mk(["党参"], syn="脾胃气虚证"), ontology=ont)
    u = [u for u in r.unverifiable if u.rule == "nature_conflict"]
    assert len(u) == 1 and u[0].missing_predicate == "证型寒热方向"
    assert "不是通过，是判不了" in u[0].reason


def test_nature_conflict_skips_mixed_cold_and_heat(ont):
    r = verify_formula(mk(["石膏"] * 5, syn="上热下寒证"), ontology=ont)
    assert "nature_conflict" not in _rules(r)
    assert any(u.rule == "nature_conflict" for u in r.unverifiable)


def test_effect_matches_method_goes_through_the_synonym_table(ont):
    """治法「疏肝理气」要能匹配功效写「疏肝解郁」的柴胡——裸子串比匹配不上，
    那条规则就变成恒假（见 core/effect_synonyms.py 的文档）。"""
    r = verify_formula(mk(["柴胡"], organ="肝", syn="肝郁气滞证",
                          method="疏肝理气", targets=("肝气郁结",),
                          effect="疏肝解郁"), ontology=ont)
    assert "effect_matches_method" not in _rules(r)
    assert "effect_matches_method" in r.checked_rules


def test_effect_matches_method_fires_when_nothing_matches(ont):
    r = verify_formula(mk(["石膏"], organ="肺", syn="肺气虚证",
                          method="温中散寒", targets=("中焦寒盛",)), ontology=ont)
    assert "effect_matches_method" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "effect_matches_method")
    assert "清热泻火" in v.counterexample


def test_effect_matches_method_needs_only_one_matching_herb(ont):
    """一味药对不上不算违规：佐使药本来就可能针对兼夹症。"""
    r = verify_formula(mk(["党参", "石膏"]), ontology=ont)
    assert "effect_matches_method" not in _rules(r)


def test_role_structure_requires_a_sovereign_herb(ont):
    r = verify_formula(mk(["党参", "白术"], roles=["臣", "臣"]), ontology=ont)
    assert "role_structure" in _rules(r, "revise")
    v = next(v for v in r.violations if v.rule == "role_structure")
    assert "没有君药" in v.reason
    assert "君 0 味" in v.counterexample


def test_role_structure_rejects_too_many_sovereigns(ont):
    r = verify_formula(mk(["党参", "白术", "柴胡", "石膏"],
                          roles=["君"] * 4), ontology=ont)
    assert "role_structure" in _rules(r, "revise")
    assert "都是君等于没有君" in next(
        v for v in r.violations if v.rule == "role_structure").reason


def test_role_structure_rejects_adjuncts_outnumbering_the_core(ont):
    """R1 量过：ε_online 里相当一部分是无依据的佐使加减。"""
    r = verify_formula(mk(["党参", "白术", "柴胡", "石膏"],
                          roles=["君", "佐", "佐", "使"]), ontology=ont)
    v = next(v for v in r.violations if v.rule == "role_structure")
    assert "佐使共 3 味，多于君臣 1 味" in v.reason


def test_role_structure_is_unverifiable_when_no_role_is_labelled(ont):
    r = verify_formula(mk(["党参", "白术"], roles=[None, None]), ontology=ont)
    assert "role_structure" not in _rules(r)
    u = [u for u in r.unverifiable if u.rule == "role_structure"]
    assert len(u) == 1 and u[0].missing_predicate == "role"


def test_role_structure_does_not_depend_on_the_ontology(ont):
    """role 是模型自己标的，这条规则不查本体——空本体下它照样判得了。
    （空本体会在 verify_formula 那一层短路，所以这里直接调规则函数。）"""
    empty = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    v, u, c = RULE_FUNCS["role_structure"](mk(["党参"], roles=["臣"]), empty)
    assert c == ["role_structure"] and v and not u


# ---------- 每条违规都带本体原文 ----------

@pytest.mark.parametrize("label,s3kw", [
    ("配伍禁忌", dict(herbs=["甘草", "甘遂", "白术"])),
    ("超剂量", dict(herbs=["附子", "白术"], doses=[60.0, 9.0])),
    ("编造出处", dict(herbs=["党参"], refs={"党参": [
        {"kind": "herb", "name": "党参", "predicate": "功效", "span": "回阳救逆"}]})),
    ("归经覆盖", dict(herbs=["党参", "白术"], organ="肝", syn="肝郁气滞证",
                      method="疏肝理气", targets=("肝气郁结",))),
    ("寒热相悖", dict(herbs=["石膏"] * 5, syn="脾胃虚寒证", method="温中散寒",
                      targets=("中焦寒盛",), effect="清热泻火")),
    ("君臣结构", dict(herbs=["党参", "白术"], roles=["臣", "臣"])),
])
def test_every_violation_carries_a_nonempty_counterexample(ont, label, s3kw):
    r = verify_formula(mk(**s3kw), ontology=ont)
    assert r.violations, f"{label} 这一组没有产出任何违规"
    for v in r.violations:
        assert v.counterexample.strip(), f"{label} 的 {v.rule} 反例是空的"
        assert len(v.counterexample) > 10, f"{label} 的 {v.rule} 反例太短，指不出东西"


# ---------- passed / status 的定义（R34a） ----------

def test_passed_requires_unverifiable_to_be_empty():
    """R34a：`passed` = 无 veto、无 revise、**且 unverifiable 为空**。
    少了第三条，"已验证通过"这句话在覆盖率只有一半的本体上依然成立。"""
    clean = VerificationResult(checked_rules=ALL_RULES)
    assert clean.passed is True and clean.status == "verified"
    partial = VerificationResult(unverifiable=(Unverifiable(
        rule="meridian_coverage", herbs=("鲤鱼",), missing_predicate="归经",
        reason="本体里没有这味药"),), checked_rules=("role_structure",))
    assert partial.passed is False
    assert partial.status == "partially_verified"


def test_status_orders_by_severity():
    veto = Violation(rule="dose_exceeds", severity="veto", herbs=("附子",),
                     reason="超量", counterexample="上限 15g，本方 60g")
    rev = Violation(rule="role_structure", severity="revise", herbs=(),
                    reason="没有君药", counterexample="君 0 味")
    unv = Unverifiable(rule="nature_conflict", herbs=(), missing_predicate="性味",
                       reason="本体没有性味")
    assert VerificationResult(violations=(veto, rev), unverifiable=(unv,)).status == "vetoed"
    assert VerificationResult(violations=(rev,), unverifiable=(unv,)).status == "revise_needed"
    assert VerificationResult(unverifiable=(unv,)).status == "partially_verified"
    assert VerificationResult().status == "verified"


def test_an_unavailable_ontology_puts_only_the_ontology_rules_in_unverifiable():
    """药理层数据不在的机器上，本体那七条规则「符号验证通过」必须报成
    「一条都没验」——否则那句话在没有本体的环境里恒真，而它恒真时毫无意义。

    R53：本体不可用**不该连累医理规则层那四条**（数据源不是一回事，见
    `verify_formula` 的文档字符串）——`ONTOLOGY_RULES` 全部进 unverifiable，
    `THEORY_RULES` 该跑还跑（这里没有毒化 `load_theory`，用的是仓库里真实的
    `data/standard/tcm_theory.jsonl`，跟真实部署一致）。
    """
    from core.formula_verifier import ONTOLOGY_RULES

    empty = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    r = verify_formula(mk(["党参"]), ontology=empty)
    assert r.ontology_available is False
    assert set(r.checked_rules) == set(THEORY_RULES)
    assert {u.rule for u in r.unverifiable} == set(ONTOLOGY_RULES)
    assert r.passed is False and r.status in ("partially_verified", "revise_needed")


def test_an_unavailable_theory_layer_puts_only_the_theory_rules_in_unverifiable(
    monkeypatch, ont):
    """跟上一条对称：医理规则层不在时，只有 `THEORY_RULES` 那四条进
    unverifiable，本体那七条该跑还跑。"""
    import core.formula_verifier as fv

    monkeypatch.setattr(fv, "load_theory", lambda: ())
    r = verify_formula(mk(["党参"]), ontology=ont)
    assert r.theory_available is False
    assert set(r.checked_rules) & set(THEORY_RULES) == set()
    assert {u.rule for u in r.unverifiable} >= set(THEORY_RULES)


def test_to_dict_is_json_serialisable_and_keeps_the_counts(ont):
    import json

    d = verify_formula(mk(["甘草", "甘遂"]), ontology=ont).to_dict()
    assert json.dumps(d, ensure_ascii=False)
    assert d["status"] == "vetoed" and d["n_veto"] >= 1
    assert d["violations"][0]["counterexample"]
    assert set(d) >= {"status", "passed", "ontology_available", "checked_rules",
                      "n_veto", "n_revise", "n_unverifiable"}


# ---------- 回灌文本 ----------

def test_the_revise_feedback_lists_vetoes_and_revisables_separately(ont):
    r = verify_formula(mk(["甘草", "甘遂"], roles=["臣", "臣"]), ontology=ont)
    text = format_violations_for_revise(r)
    assert "必须解决" in text and "需要修正" in text
    assert "incompatible_pair" in text and "role_structure" in text
    assert "依据：" in text


def test_the_revise_feedback_omits_unverifiable(ont):
    """本体缺数据时模型改方也改不出数据来，写进去只会让它去改一个本来可能对的地方。"""
    r = verify_formula(mk(["鲤鱼", "党参"], roles=["臣", "臣"]), ontology=ont)
    assert r.unverifiable, "这一组应当有判不了的条目"
    text = format_violations_for_revise(r)
    assert "鲤鱼" not in text or "role_structure" in text
    for u in r.unverifiable:
        assert u.reason not in text, "unverifiable 的理由不该出现在回灌文本里"


def test_no_violations_means_no_feedback_text():
    assert format_violations_for_revise(VerificationResult()) == ""


# ---------- 三指标（含 34b 的分母） ----------

def test_herbs_grounded_ratio_denominator_is_this_formula_not_the_ontology(ont):
    """R34b：本体 1232 味 vs 这张方的药味数——两个完全不同的分母。"""
    from core.formula_verifier import herbs_grounded_ratio

    refs = {"党参": [{"kind": "herb", "name": "党参", "predicate": "功效",
                      "span": "补中益气"}]}
    s3 = mk(["党参", "白术"], refs=refs)
    assert herbs_grounded_ratio(s3) == 0.5, "1 味有引用 / 共 2 味"
    assert herbs_grounded_ratio(s3) == s3.herbs_grounded_ratio(), "转发，不是第二份实现"
    doc = herbs_grounded_ratio.__doc__ or ""
    assert "不是本体总药味数" in doc and "1232" in doc


def test_ontology_coverage_of_corpus_reports_both_ratios():
    """只报按种数会低估它对真实问诊的支撑，只报按次数会掩盖长尾缺口。"""
    cov = ontology_coverage_of_corpus()
    if not cov.get("available"):
        pytest.skip(f"语料不在：{cov.get('note')}")
    assert cov["n_corpus_herb_names"] > 0
    assert 0 < cov["coverage_by_name"] <= 1
    assert 0 < cov["coverage_by_occurrence"] <= 1
    assert cov["coverage_by_occurrence"] > cov["coverage_by_name"], (
        "常用药应当比长尾覆盖得好——这个方向反了说明归一或数据出了问题"
    )
    assert cov["n_ontology_herbs"] != cov["n_corpus_herb_names"], (
        "这两个数就是 R34b 要区分的那两个分母，相等说明取错了其中一个"
    )


def test_verifier_metrics_separates_first_round_from_final(ont):
    """"改了三轮才过"和"一次就过"在最终态上看起来一模一样。"""
    bad = verify_formula(mk(["党参", "白术"], roles=["臣", "臣"]), ontology=ont)
    good = verify_formula(mk(["党参", "白术"]), ontology=ont)
    m = verifier_metrics([bad, good], mk(["党参"]))
    assert m["n_rounds"] == 2 and m["revise_rounds"] == 1
    assert m["verifier_first_pass"] is False
    assert m["first_pass_status"] == "revise_needed"
    assert m["final_status"] == good.status
    assert m["statuses"] == ["revise_needed", good.status]


def test_verifier_metrics_on_an_empty_round_list_says_none():
    m = verifier_metrics([])
    assert m["n_rounds"] == 0 and m["verifier_first_pass"] is False
    assert m["first_pass_status"] is None and m["final_status"] is None


# ---------- 轮数上限 ----------

def test_max_revise_rounds_default_and_override(monkeypatch):
    """R53：产品默认从 3 降到 1——规则从七条扩到十一条之后，第二三轮改的多半
    是同一类没改对的地方，见 `MAX_REVISE_ROUNDS` 的文档字符串。"""
    monkeypatch.delenv("MAX_REVISE_ROUNDS", raising=False)
    assert max_revise_rounds() == MAX_REVISE_ROUNDS == 1
    monkeypatch.setenv("MAX_REVISE_ROUNDS", "0")
    assert max_revise_rounds() == 0, "0 = 关掉闭环（做消融用）"
    monkeypatch.setenv("MAX_REVISE_ROUNDS", "5")
    assert max_revise_rounds() == 5
    for bad in ("-1", "三", "3.5"):
        monkeypatch.setenv("MAX_REVISE_ROUNDS", bad)
        with pytest.raises(ValueError):
            max_revise_rounds()


# ---------- 真实本体：规则在实际覆盖率下落进哪一档 ----------

def test_with_the_real_ontology_a_plain_formula_is_at_worst_partially_verified():
    """真数据上一张普通的四君子汤：不该出违规，但会因为缺 ontology_refs 与
    证型无寒热方向落进 partially_verified——**这正是 34a 要的行为**。"""
    from core.ontology import get_ontology

    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    r = verify_formula(mk(["党参", "白术", "茯苓", "甘草"]), ontology=ont)
    assert r.violations == [] or r.violations == (), f"不该有违规：{r.violations}"
    assert r.status == "partially_verified"
    assert {u.rule for u in r.unverifiable} <= set(ALL_RULES)


def test_with_the_real_ontology_the_missing_predicate_counts_are_reported():
    """归经缺 49%、用量缺 55%——引用"符号验证通过"的人必须能看到这个。"""
    from core.ontology import get_ontology

    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    s = ont.stats()
    missing = s["missing_predicate_counts"]
    assert set(missing) == {"性味", "归经", "功效", "用量", "禁忌", "炮制"}
    assert missing["归经"] > 0 and missing["用量"] > 0
    # 缺谓词数不可能超过本草总数
    for pred, n in missing.items():
        assert n <= s["n_herbs"], f"{pred} 缺 {n} 条 > 本草 {s['n_herbs']} 味"
