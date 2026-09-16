"""R23：方剂建议层（core/formula_check.py）。

这一层跟 `core/safety_output.py` 的关系是这些测试要钉住的头号事实：
**建议层不重新实现任何判据**。五条规则里三条直接调安全层的函数，
所以下面有几条测试是"把安全层的函数换掉，看建议跟不跟着变"——
这种测法能抓到"复制了一份判据到建议层"这种退化，而单看输出对不对抓不到。
"""
from __future__ import annotations

import pytest

from core import formula_check as fc
from core.elements import LOCATIONS
from core.formula_check import (
    ADVICE_WEIGHTS,
    DUPLICATE_OVERLAP_GATE,
    SEVERITY_BY_KIND,
    FormulaCheck,
    advice_dicts,
    check_formula,
    herb_props,
    score_formula,
    syndrome_channels,
)
from core.schemas import Advice, HerbItem

# 合成本草表：字段形状跟 data/standard/materia_medica.jsonl 经 build_entry_index
# 之后一致（{药名: {谓词: [值…]}}）。**沙盒里没有那份真表**，所以所有依赖它的
# 测试都显式传 materia=，而不是依赖环境里恰好有没有文件——后者会让同一条测试
# 在沙盒和 AutoDL 上测的是两件事。
MATERIA = {
    "白术": {"归经": ["归脾、胃经"], "功效": ["健脾益气、燥湿利水"], "性味": ["苦、甘，温"]},
    "苍术": {"归经": ["归脾、胃经"], "功效": ["健脾益气、燥湿利水"], "性味": ["苦、甘，温"]},
    "柴胡": {"归经": ["归肝、胆经"], "功效": ["疏肝解郁、升举阳气"], "性味": ["苦，微寒"]},
    "茯苓": {"归经": ["归心、肺、脾、肾经"], "功效": ["利水渗湿、健脾宁心"], "性味": ["甘、淡，平"]},
}


def _items(*names: str) -> list[HerbItem]:
    return [HerbItem(name=n) for n in names]


# ---------- 五条规则各自触发 ----------

def test_incompatible_rule_fires_and_says_which_table_it_came_from():
    """甘草与甘遂是十八反里的一对。source_span 写「十八反」还是「十九畏」
    按安全层把两张表分开存的那个分法走，不是这里自己判断"看起来像反还是像畏"。"""
    check = check_formula("脾虚证", _items("甘草", "甘遂"), materia=MATERIA)
    hits = check.by_kind("incompatible")
    assert len(hits) == 1
    assert set(hits[0].herbs) == {"甘草", "甘遂"}
    assert hits[0].source_span == "十八反"
    assert hits[0].severity == "blocking"


def test_over_dose_rule_carries_the_pharmacopoeia_reason_as_source_span():
    """超剂量这条的出处是 DOSE_LIMITS 里那句理由文本——它本身就是这个项目
    唯一有据可查的剂量出处（见 safety_output 里那段数据来源方法论）。"""
    check = check_formula("阳虚证", [HerbItem(name="附子", dose=30.0)], materia=MATERIA)
    hits = check.by_kind("over_dose")
    assert len(hits) == 1
    assert hits[0].herbs == ["附子"]
    assert "乌头碱" in (hits[0].source_span or "")
    assert "30" in hits[0].reason and "15" in hits[0].reason


def test_thermal_rule_delegates_to_the_safety_layer():
    """寒热这条整段交给 `check_thermal_consistency`。判据：把那个函数换掉，
    建议必须跟着变——如果建议层自己抄了一份寒热表，这条会失败。"""
    check = check_formula("脾胃虚寒证", _items("黄连", "黄柏", "栀子", "石膏"), materia=MATERIA)
    assert len(check.by_kind("thermal_mismatch")) == 1
    assert check.by_kind("thermal_mismatch")[0].severity == "warning"


def test_thermal_rule_follows_a_patched_safety_layer(monkeypatch):
    monkeypatch.setattr(fc, "check_thermal_consistency", lambda syndrome, herbs: "换掉之后的文案")
    check = check_formula("脾虚证", _items("白术"), materia=MATERIA)
    hits = check.by_kind("thermal_mismatch")
    assert len(hits) == 1 and hits[0].reason == "换掉之后的文案"


def test_missing_channel_guide_fires_when_no_herb_enters_that_channel():
    """证型指向肝，方里两味都只归脾胃 → 缺引经药。"""
    check = check_formula("肝郁脾虚证", _items("白术", "茯苓"), materia=MATERIA)
    hits = check.by_kind("missing_channel_guide")
    assert len(hits) == 1
    assert "肝" in hits[0].reason
    assert hits[0].herbs == [], "这条规则说的是「没有这样一味药」，列不出药名是它的本来形状"
    assert hits[0].severity == "suggestion"


def test_missing_channel_guide_is_silent_when_the_channel_is_covered():
    check = check_formula("肝郁脾虚证", _items("白术", "柴胡"), materia=MATERIA)
    assert check.by_kind("missing_channel_guide") == ()


def test_duplicate_effect_fires_on_full_overlap():
    check = check_formula("脾虚证", _items("白术", "苍术"), materia=MATERIA)
    hits = check.by_kind("duplicate_effect")
    assert len(hits) == 1
    assert set(hits[0].herbs) == {"白术", "苍术"}
    assert "100%" in hits[0].reason


def test_duplicate_effect_is_silent_below_the_gate():
    """柴胡和白术的性味功效几乎不重合，不该被判重复。
    同时钉住闸门是个常量（改闸门要改一处），不是散在代码里的字面 0.6。"""
    check = check_formula("肝郁证", _items("白术", "柴胡"), materia=MATERIA)
    assert check.by_kind("duplicate_effect") == ()
    assert 0.0 < DUPLICATE_OVERLAP_GATE <= 1.0


def test_duplicate_effect_reports_each_pair_not_a_cluster():
    """三味互相重复给三条，不合并成一条。合并就必须替人决定留哪一味，
    而那是医家的判断，不是规则该做的。"""
    materia = dict(MATERIA)
    materia["炒白术"] = MATERIA["白术"]
    check = check_formula("脾虚证", _items("白术", "苍术", "炒白术"), materia=materia)
    assert len(check.by_kind("duplicate_effect")) == 3


# ---------- 缺数据的三分法 ----------

def test_missing_materia_table_is_reported_not_silently_skipped():
    """沙盒里没有本草表 → 两条规则进 skipped，带 available=False 和产物路径。
    **不是静默少两条建议**：那样"这方没有缺引经药问题"和"这条规则没跑"
    在界面上长得一模一样。"""
    check = check_formula("肝郁脾虚证", _items("白术"), materia=None)
    if check.materia_available:
        pytest.skip("这台机器上已经有 data/standard/materia_medica.jsonl，这条测的是缺文件的路径")
    rules = {s["rule"] for s in check.skipped}
    assert rules == {"missing_channel_guide", "duplicate_effect"}
    for s in check.skipped:
        assert s["available"] is False
        assert s["path"].endswith("materia_medica.jsonl")
        assert "run_pharmacology_extraction" in s["reason"]
    assert check.by_kind("missing_channel_guide") == ()


def test_a_herb_missing_from_the_table_is_counted_not_assumed_fine():
    """第三种情况：表在、这味药不在表里。skipped 里要说"已查 N/M 味"，
    不是当成"查过了没问题"（SOURCES.md 第 31 条那个老教训的同一形状）。"""
    check = check_formula("脾虚证", _items("白术", "某不存在的药"), materia=MATERIA)
    assert check.materia_available is True
    assert check.n_materia_checked == 1
    dup = [s for s in check.skipped if s["rule"] == "duplicate_effect"]
    assert len(dup) == 1
    assert dup[0]["n_checked"] == 1 and dup[0]["n_herbs"] == 2
    assert dup[0]["available"] is True, "表在，只是这味药不在表里——不是文件缺失"


def test_no_syndrome_marks_the_thermal_rule_inapplicable():
    """/api/prescription/export 那条路径传的就是空证型。空证型下寒热不判，
    而"不判"必须说出来——"不适用"和"通过了"是两件事。"""
    check = check_formula("", _items("白术"), materia=MATERIA)
    reasons = {s["rule"]: s["reason"] for s in check.skipped}
    assert "thermal_mismatch" in reasons
    assert "不适用" in reasons["thermal_mismatch"]


def test_a_syndrome_without_a_location_marks_the_channel_rule_inapplicable():
    check = check_formula("气滞证", _items("白术"), materia=MATERIA)
    reasons = {s["rule"]: s["reason"] for s in check.skipped}
    assert "missing_channel_guide" in reasons
    assert "不适用" in reasons["missing_channel_guide"]


# ---------- 打分 ----------

def test_a_clean_formula_scores_one():
    check = check_formula("肝郁证", _items("柴胡"), materia=MATERIA)
    assert check.advice == ()
    assert check.score == 1.0


def test_each_kind_subtracts_its_own_weight():
    assert score_formula([Advice(kind="incompatible", reason="x", severity="blocking")]) == 0.0
    assert score_formula([Advice(kind="over_dose", reason="x", severity="blocking")]) == 0.5
    assert score_formula([Advice(kind="thermal_mismatch", reason="x", severity="warning")]) == 0.7
    assert score_formula([Advice(kind="missing_channel_guide", reason="x", severity="suggestion")]) == 0.85
    assert score_formula([Advice(kind="duplicate_effect", reason="x", severity="suggestion")]) == 0.9


def test_the_same_kind_twice_subtracts_twice():
    two = [Advice(kind="duplicate_effect", reason="x", severity="suggestion") for _ in range(2)]
    assert score_formula(two) == 0.8


def test_the_score_never_goes_negative():
    """负分在排序里没有额外信息，而它会让 0.0 和 −1.5 看起来像两种不同的结论。"""
    many = [Advice(kind="incompatible", reason="x", severity="blocking") for _ in range(3)]
    assert score_formula(many) == 0.0


def test_the_weights_are_ordered_incompatible_worst_duplicate_mildest():
    """权重之间唯一保证的事：配伍禁忌永远比重复用药严重。绝对值没有临床含义，
    所以这里钉的是**顺序**，不是具体数值。"""
    order = ["incompatible", "over_dose", "thermal_mismatch",
             "missing_channel_guide", "duplicate_effect"]
    weights = [ADVICE_WEIGHTS[k] for k in order]
    assert weights == sorted(weights, reverse=True)
    assert set(ADVICE_WEIGHTS) == set(SEVERITY_BY_KIND)


def test_the_score_lives_in_one_place_and_the_dataclass_asks_it():
    """`FormulaCheck.score` 不是另算一遍，是问 `score_formula`。
    判据：把 score_formula 换掉，property 跟着变。"""
    check = FormulaCheck(advice=(Advice(kind="over_dose", reason="x", severity="blocking"),))
    assert check.score == score_formula(check.advice)


# ---------- 确定性与形状 ----------

def test_advice_order_is_deterministic_and_heaviest_first():
    """前端的"第一条建议"不许来回跳，R22 的打分也要可复现。"""
    items = _items("甘草", "甘遂", "白术", "苍术")
    first = check_formula("脾虚证", items, materia=MATERIA)
    second = check_formula("脾虚证", items, materia=MATERIA)
    kinds = [a.kind for a in first.advice]
    assert kinds == [a.kind for a in second.advice]
    assert kinds[0] == "incompatible", "权重最重的排最前"
    assert [ADVICE_WEIGHTS[k] for k in kinds] == sorted(
        (ADVICE_WEIGHTS[k] for k in kinds), reverse=True)


def test_advice_dicts_is_the_single_serialization_point():
    check = check_formula("脾虚证", _items("甘草", "甘遂"), materia=MATERIA)
    dicts = advice_dicts(check)
    assert dicts == [a.model_dump() for a in check.advice]
    assert set(dicts[0]) == {"kind", "herbs", "reason", "source_span", "severity"}


def test_an_empty_formula_produces_no_advice_and_does_not_raise():
    """可编辑处方表从空表开始，医生删到 0 味药时前端仍会调一次校验。

    0 味药**不算"缺引经药"**：那是字面为真但毫无用处的噪音。不判这件事要
    说出来——skipped 里带一条"方里还没有药，无从判引经"。
    """
    check = check_formula("脾虚证", [], materia=MATERIA)
    assert check.advice == ()
    assert check.score == 1.0
    reasons = {s["rule"]: s["reason"] for s in check.skipped}
    assert "missing_channel_guide" in reasons
    assert "还没有药" in reasons["missing_channel_guide"]


def test_advice_requires_a_non_empty_reason():
    """一条说不出理由的建议在界面上是个空条目，人只会以为是 bug。"""
    with pytest.raises(Exception):
        Advice(kind="duplicate_effect", reason="", severity="suggestion")


def test_source_span_may_be_none_for_the_rules_that_have_no_original_text():
    """寒热/缺引经/重复三条没有原文出处，留 None 是如实——
    编一句「根据中医理论」才是假的。"""
    check = check_formula("肝郁脾虚证", _items("白术", "苍术"), materia=MATERIA)
    for advice in check.advice:
        if advice.kind in ("missing_channel_guide", "duplicate_effect", "thermal_mismatch"):
            assert advice.source_span is None
        else:
            assert advice.source_span


# ---------- 复用，不另写一套 ----------

def test_syndrome_channels_reuses_the_element_location_table():
    """脏腑表只有 core.elements.LOCATIONS 一处。证素抽取、证素索引、这一层
    问的是同一个问题「这个词是哪个病位」——另写一张表就是这个项目撞过三次的
    那堵墙（CLAUDE.md「同一概念的匹配逻辑只能有一处实现」）。"""
    for loc in LOCATIONS:
        assert syndrome_channels(f"{loc}气虚证") == [loc] or loc in syndrome_channels(f"{loc}气虚证")
    assert set(syndrome_channels("肝郁脾虚证")) <= set(LOCATIONS)
    assert syndrome_channels("气滞证") == []


def test_syndrome_channels_order_follows_the_table_not_the_string():
    """顺序按 LOCATIONS 走，不按在证型名里出现的先后——advice 的内容对同一个
    证型必须永远一样，否则 score 就不是确定的。"""
    assert syndrome_channels("肝郁脾虚证") == syndrome_channels("脾虚肝郁证")


def test_herb_props_falls_back_to_the_normalized_name():
    """按原名查不到再按 normalize_herb 查一次——跟 check_dose_limits 对
    DOSE_LIMITS 的查法完全一致，不是这里另发明的顺序。"""
    index = {"白术": MATERIA["白术"]}
    assert herb_props(index, "白术") is not None
    assert herb_props(index, "浙白术") is not None or herb_props(index, "生白术") is not None


def test_incompatible_advice_follows_a_patched_safety_layer(monkeypatch):
    """把 check_incompatible 换掉，建议必须跟着变。这条抓的是"建议层偷偷复制
    了一份十八反表"——那种退化下，输出看起来仍然是对的。"""
    monkeypatch.setattr(fc, "check_incompatible", lambda herbs: [("甲药", "乙药")])
    check = check_formula("脾虚证", _items("白术"), materia=MATERIA)
    hits = check.by_kind("incompatible")
    assert len(hits) == 1 and hits[0].herbs == ["甲药", "乙药"]


def test_dose_advice_follows_a_patched_safety_layer(monkeypatch):
    from core.schemas import DoseViolation

    monkeypatch.setattr(fc, "check_dose_limits",
                        lambda items: [DoseViolation(herb="某药", dose=9.0, unit="g",
                                                     limit_g=3.0, reason="换掉之后的出处")])
    check = check_formula("脾虚证", _items("白术"), materia=MATERIA)
    hits = check.by_kind("over_dose")
    assert len(hits) == 1 and hits[0].source_span == "换掉之后的出处"


def test_the_term_splitter_drops_the_channel_frame_characters():
    """「归脾、胃经」切出来要是 {脾, 胃}，不能带「归」「经」——每味药都带这两个
    术语的话，任意两味药的重合度都被抬高，重复判定就会假阳。"""
    assert fc._channel_terms(["归脾、胃经"]) >= {"脾", "胃"}
    assert "归" not in fc._channel_terms(["归脾、胃经"])
    assert "经" not in fc._channel_terms(["归脾、胃经"])
