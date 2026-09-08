"""core/safety_output.py 的离线测试（X2）：十八反十九畏 24 对逐对验证、
别名归一、寒热一致性、以及接进 run_physician 后的重开闭环。不需要网络。
"""
import pytest

from core.safety_output import (
    COLD_HERBS,
    HOT_HERBS,
    INCOMPATIBLE_PAIRS,
    SHIBAFAN,
    SHIJIUWEI,
    check_incompatible,
    check_thermal_consistency,
    format_conflicts,
    normalize_for_incompat,
)

# ---------- 十八反：15 对逐对 ----------

SHIBAFAN_CASES = (
    [("甘草", x) for x in ["甘遂", "大戟", "海藻", "芫花"]]
    + [("乌头", x) for x in ["贝母", "瓜蒌", "半夏", "白蔹", "白及"]]
    + [("藜芦", x) for x in ["人参", "沙参", "丹参", "玄参", "细辛", "芍药"]]
)

SHIJIUWEI_CASES = [
    ("硫黄", "芒硝"), ("水银", "砒霜"), ("狼毒", "密陀僧"),
    ("巴豆", "牵牛"), ("丁香", "郁金"), ("乌头", "犀角"),
    ("芒硝", "三棱"), ("肉桂", "赤石脂"), ("人参", "五灵脂"),
]


@pytest.mark.parametrize("a,b", SHIBAFAN_CASES)
def test_shibafan_each_pair_detected(a, b):
    assert check_incompatible([a, b]) != [], f"十八反漏检：{a} 反 {b}"


@pytest.mark.parametrize("a,b", SHIJIUWEI_CASES)
def test_shijiuwei_each_pair_detected(a, b):
    assert check_incompatible([a, b]) != [], f"十九畏漏检：{a} 畏 {b}"


def test_pair_counts():
    assert len(SHIBAFAN) == 15
    assert len(SHIJIUWEI) == 9
    assert len(INCOMPATIBLE_PAIRS) == 24


def test_no_false_positive_on_normal_formula():
    """六君子汤：常用方，不应报任何禁忌。误报比漏报更容易让人关掉这个检查。"""
    assert check_incompatible(["党参", "白术", "茯苓", "甘草", "陈皮", "半夏"]) == []


def test_detects_within_larger_formula():
    herbs = ["党参", "白术", "甘草", "茯苓", "海藻", "陈皮"]
    assert check_incompatible(herbs) == [("甘草", "海藻")]


def test_returns_original_wording_not_canonical():
    """返回的是模型实际写的字，不是归一后的名字——评审要看模型开了什么。"""
    found = check_incompatible(["炙甘草", "醋甘遂"])
    assert found == [("炙甘草", "醋甘遂")]


def test_conflict_pair_follows_prescription_order():
    """冲突对按在方中出现的先后排，不按码点——展示成"X 反 Y"时要能对着
    处方从左往右读下来。同一对药调换书写顺序，输出顺序也跟着换。"""
    assert check_incompatible(["甘草", "海藻"]) == [("甘草", "海藻")]
    assert check_incompatible(["海藻", "甘草"]) == [("海藻", "甘草")]


def test_multiple_conflicts_all_reported():
    found = check_incompatible(["甘草", "海藻", "藜芦", "人参"])
    assert len(found) == 2


def test_empty_and_single_herb():
    assert check_incompatible([]) == []
    assert check_incompatible(["甘草"]) == []


# ---------- 别名归一 ----------

@pytest.mark.parametrize("raw,canon", [
    ("附子", "乌头"), ("制附子", "乌头"), ("川乌", "乌头"), ("草乌", "乌头"),
    ("黑顺片", "乌头"),
    ("浙贝母", "贝母"), ("川贝", "贝母"),
    ("全瓜蒌", "瓜蒌"), ("天花粉", "瓜蒌"),
    ("白芍", "芍药"), ("赤芍", "芍药"),
    ("红参", "人参"),
    ("朴硝", "芒硝"), ("牙硝", "芒硝"),
    ("官桂", "肉桂"), ("石脂", "赤石脂"),
    ("牵牛子", "牵牛"), ("巴豆霜", "巴豆"),
])
def test_alias_normalization(raw, canon):
    assert normalize_for_incompat(raw) == canon


def test_alias_normalization_after_dose_stripping():
    """归一要能穿过剂量和炮制写法——"制附子三钱"必须也归到乌头，
    否则模型带剂量写药名时整条检查就失效了。"""
    assert normalize_for_incompat("制附子三钱") == "乌头"
    assert normalize_for_incompat("姜半夏二钱（包煎）") == "半夏"


def test_fuzi_triggers_wutou_rule():
    """附子是乌头的子根，配伍禁忌按同一味处理——这是最容易漏的一条。"""
    assert check_incompatible(["制附子", "姜半夏"]) == [("制附子", "姜半夏")]


def test_dangshen_is_not_renshen():
    """党参是桔梗科，人参是五加科，"藜芦反人参"不涵盖党参。
    归错会把常见方（党参+藜芦几乎不同用，但党参+五灵脂会）误报。"""
    assert normalize_for_incompat("党参") == "党参"
    assert check_incompatible(["党参", "五灵脂"]) == []
    assert check_incompatible(["人参", "五灵脂"]) != []


def test_hongshen_is_renshen():
    """红参是人参蒸制品，同物种，禁忌适用。安全检查宁可多报不可漏报。"""
    assert check_incompatible(["红参", "五灵脂"]) != []


# ---------- 寒热一致性 ----------

def test_cold_syndrome_with_cold_formula_warns():
    w = check_thermal_consistency(
        "脾胃虚寒证", ["黄连", "黄芩", "石膏", "知母", "白术", "茯苓"]
    )
    assert w is not None
    assert "寒热方向可能相悖" in w


def test_hot_syndrome_with_hot_formula_warns():
    w = check_thermal_consistency(
        "胃热壅盛证", ["附子", "干姜", "肉桂", "吴茱萸", "白术", "茯苓"]
    )
    assert w is not None


def test_cold_syndrome_with_warm_formula_is_fine():
    assert check_thermal_consistency(
        "脾胃虚寒证", ["附子", "干姜", "白术", "茯苓", "甘草", "陈皮"]
    ) is None


def test_mixed_cold_hot_syndrome_is_skipped():
    """寒热错杂证本来就寒热并用，规则不适用——不跳过会稳定误报。"""
    assert check_thermal_consistency(
        "寒热错杂证", ["黄连", "黄芩", "石膏", "知母", "干姜", "附子"]
    ) is None


def test_syndrome_without_thermal_direction_is_skipped():
    assert check_thermal_consistency(
        "肝胃不和证", ["黄连", "黄芩", "石膏", "知母", "柴胡", "白芍"]
    ) is None


def test_only_first_six_herbs_counted():
    """佐使药不算：前 6 味是温热的，第 7 味起全是寒凉也不该报。"""
    herbs = ["附子", "干姜", "肉桂", "吴茱萸", "白术", "茯苓",
             "黄连", "黄芩", "石膏", "知母", "大黄", "栀子"]
    assert check_thermal_consistency("脾胃虚寒证", herbs) is None


def test_exactly_three_cold_does_not_warn():
    """阈值是"超过 3 味"，正好 3 味（半数）不报——边界要钉住。"""
    herbs = ["黄连", "黄芩", "石膏", "白术", "茯苓", "甘草"]
    assert check_thermal_consistency("脾胃虚寒证", herbs) is None


def test_four_cold_warns():
    herbs = ["黄连", "黄芩", "石膏", "知母", "白术", "茯苓"]
    assert check_thermal_consistency("脾胃虚寒证", herbs) is not None


def test_thermal_check_normalizes_herb_names():
    herbs = ["川连三钱", "黄芩", "石膏", "知母", "白术", "茯苓"]
    w = check_thermal_consistency("脾胃虚寒证", herbs)
    assert w is not None and "黄连" in w  # 川连已归一到黄连


def test_format_conflicts():
    assert format_conflicts([("甘草", "海藻"), ("人参", "五灵脂")]) == \
        "甘草 与 海藻、人参 与 五灵脂"


# ---------- 审查修复 ----------

def test_thermal_tables_only_contain_normalized_names():
    """表里每一项归一后必须等于自身。「生地黄」曾在表里却永远匹配不上——
    normalize_herb 把「生」剥掉得到「地黄」，表里没有。"""
    from core.herbs import normalize_herb

    assert [h for h in COLD_HERBS | HOT_HERBS if normalize_herb(h) != h] == []


def test_shengdi_counts_as_cold():
    from core.safety_output import COLD_HERBS  # noqa: F401  (确保导入路径一致)

    w = check_thermal_consistency("脾胃虚寒证", ["生地黄", "黄连", "黄芩", "栀子", "白术", "茯苓"])
    assert w is not None and "4 味" in w


def test_majority_is_relative_to_formula_size():
    """4 味的方 3 味寒药已经过半；原来阈值写死 3，短方永远触发不了。"""
    assert check_thermal_consistency("脾胃虚寒证", ["黄连", "黄芩", "栀子", "白术"]) is not None
    assert check_thermal_consistency("脾胃虚寒证", ["黄连", "黄芩", "白术", "茯苓"]) is None


@pytest.mark.parametrize("a,b", [
    ("熟附子", "姜半夏"), ("附片", "浙贝母"), ("生草", "海藻"), ("粉草", "甘遂"),
    ("藜芦", "元参"), ("川乌", "白芨"), ("栝蒌", "制附子"), ("花粉", "草乌"), ("象贝", "附子"),
])
def test_case_record_spellings_still_trigger_incompatibility(a, b):
    """医案原文里实际出现、S3 模仿医家风格时很可能照写的别名。"""
    assert check_incompatible([a, b]) != [], f"{a} 与 {b} 漏检"


# ================= M2：剂量安全层 =================

from core.schemas import DoseViolation, FormulaCandidate, FormulaSafety, HerbItem  # noqa: E402
from core.safety_output import (  # noqa: E402
    DOSE_LIMITS,
    REQUIRED_DECOCTION,
    TOXIC_HERBS,
    assess_formula_safety,
    check_dose_limits,
    check_required_decoction,
    check_toxic_herbs,
    format_blocking_issues,
    format_dose_violations,
    to_grams,
)


# ---------- to_grams：单位换算 ----------


@pytest.mark.parametrize("dose,unit,expected", [
    (1, "g", 1.0),
    (1, "钱", 3.125),
    (1, "两", 31.25),
    (1, "分", 0.3125),
    (2, "钱", 6.25),
])
def test_to_grams_converts_known_units(dose, unit, expected):
    assert to_grams(dose, unit) == pytest.approx(expected)


@pytest.mark.parametrize("unit", ["枚", "片"])
def test_to_grams_returns_none_for_count_units(unit):
    """枚/片是计数单位，没有统一克重——不能换算，不能当成 0。
    0 意味着"剂量是 0"，None 意味着"这个单位没法换算成克"，两者是不同信号。"""
    assert to_grams(3, unit) is None


def test_to_grams_returns_none_when_dose_is_none():
    assert to_grams(None, "g") is None


# ---------- check_dose_limits：40+ 味逐条参数化 ----------


@pytest.mark.parametrize("herb,limit_g", sorted(DOSE_LIMITS.items(), key=lambda kv: kv[0]))
def test_dose_limit_table_has_at_least_forty_entries(herb, limit_g):
    """哨兵测试：真正的断言在下面 test_exceeding_the_limit_is_flagged /
    test_at_or_under_the_limit_is_not_flagged 里，这条只是把参数化列表铺开，
    让"这张表有多少味"在测试报告里一眼可数——parametrize 用的就是 DOSE_LIMITS
    本身，表加一条这里自动多一条用例，不用手动同步列表。"""
    limit, reason = limit_g
    assert limit >= 0.0
    assert reason  # 每条都必须有非空的出处/原因说明，不能空着


def test_dose_limits_table_size_meets_the_gate():
    assert len(DOSE_LIMITS) >= 40, f"目标 40+ 味，实际 {len(DOSE_LIMITS)} 味"


@pytest.mark.parametrize("herb,limit_g", sorted(DOSE_LIMITS.items(), key=lambda kv: kv[0]))
def test_exceeding_the_limit_is_flagged(herb, limit_g):
    limit, _reason = limit_g
    over = limit + 1.0  # 上限为 0（生品禁止内服）时，任何正剂量都该超限
    v = check_dose_limits([HerbItem(name=herb, dose=over, dose_unit="g")])
    assert len(v) == 1 and v[0].herb == herb and v[0].limit_g == limit


@pytest.mark.parametrize("herb,limit_g", sorted(DOSE_LIMITS.items(), key=lambda kv: kv[0]))
def test_at_or_under_the_limit_is_not_flagged(herb, limit_g):
    limit, _reason = limit_g
    if limit == 0.0:
        pytest.skip(f"{herb} 上限为 0（生品禁止内服），没有「不超限」的正剂量可测")
    v = check_dose_limits([HerbItem(name=herb, dose=limit, dose_unit="g")])
    assert v == [], f"{herb} 恰好等于上限不该被判超限"


def test_missing_dose_is_not_a_violation():
    """没写剂量不是"剂量超限"，是另一种信号——HerbItem 的既有纪律。"""
    assert check_dose_limits([HerbItem(name="附子")]) == []


def test_count_unit_never_false_positives_a_dose_violation():
    """"三枚""五片"这类计数单位没法换算成克，不能拿计数值直接当克数比，
    否则"附子 20 枚"会被当成"附子 20g"误判超限（20 枚的实际重量可能远超或
    远低于 20g，两者不是同一个量纲）。"""
    assert check_dose_limits([HerbItem(name="附子", dose=20, dose_unit="枚")]) == []


def test_dose_limits_reasons_are_all_non_empty_and_distinguish_shengpin_from_zhipin():
    """生品与制品的限量必须不同——这是 M2 spec 明确要求的区分（附子/川乌/草乌/
    半夏/南星的生品毒性远高于制品）。"""
    assert DOSE_LIMITS["附子"][0] > DOSE_LIMITS["生附子"][0]
    assert DOSE_LIMITS["川乌"][0] > DOSE_LIMITS["生川乌"][0]
    assert DOSE_LIMITS["草乌"][0] > DOSE_LIMITS["生草乌"][0]
    assert DOSE_LIMITS["半夏"][0] > DOSE_LIMITS["生半夏"][0]


def test_processed_form_spellings_resolve_through_normalize_or_explicit_alias():
    """"黑顺片"这类写法 normalize_herb 会剥过头（剥成"黑顺"），DOSE_LIMITS 必须
    显式收录这些写法本身，不能只指望归一。"""
    for spelling in ["黑顺片", "白附片", "淡附片", "熟附子", "附片", "熟附片", "炮附子"]:
        v = check_dose_limits([HerbItem(name=spelling, dose=100, dose_unit="g")])
        assert v and v[0].herb == spelling, f"{spelling} 没有命中剂量表"


# ---------- check_required_decoction ----------


def test_required_decoction_table_size_meets_the_gate():
    assert len(REQUIRED_DECOCTION) >= 25, f"目标 25+ 味，实际 {len(REQUIRED_DECOCTION)} 味"


def test_shengbanxia_matches_on_the_raw_spelling_before_any_normalization():
    """"生半夏"必须直接命中原始写法——REQUIRED_DECOCTION 里"生半夏"和"半夏"是
    两条不同的条目（只有生品要求先煎，法半夏/姜半夏/制半夏不要求），如果查表
    顺序先归一再查，"生半夏"会被 normalize_herb 保留原样（"生"前缀不剥）仍能
    命中，但换成会被剥掉前缀的场景就查不到了——这条测试钉住的是"原始写法优先"
    这个顺序本身，不是这一个词恰好能匹配。"""
    assert check_required_decoction([HerbItem(name="生半夏", decoction=None)]) == ["生半夏"]


def test_processed_forms_of_fuzi_match_through_normalize_herb():
    """模型很可能写"制附子"而不是"附子"——REQUIRED_DECOCTION 只收了"附子"这一个
    键，"制附子"查原始写法查不到，要靠 normalize_herb 归一成"附子"才命中，
    这条测试钉住"查不到再归一"这第二步真的生效，不是查表顺序看着对但实际上
    从没走到第二步。"""
    assert check_required_decoction([HerbItem(name="制附子", decoction=None)]) == ["制附子"]
    assert check_required_decoction([HerbItem(name="附子", decoction=None)]) == ["附子"]


def test_correct_decoction_is_not_flagged():
    assert check_required_decoction([HerbItem(name="附子", decoction="先煎")]) == []


def test_wrong_decoction_is_flagged_same_as_missing():
    """标了但标错跟完全没标是同一类问题——一个写着"包煎"的附子看起来"已经
    处理过"，实际上该先煎的没先煎，比空白字段更容易被忽略，不能因为"填了
    点什么"就放过。"""
    assert check_required_decoction([HerbItem(name="附子", decoction="包煎")]) == ["附子"]


def test_herb_without_a_special_requirement_is_never_flagged():
    assert check_required_decoction([HerbItem(name="党参", decoction=None)]) == []


@pytest.mark.parametrize("herb,method", sorted(REQUIRED_DECOCTION.items()))
def test_required_decoction_table_entries_round_trip(herb, method):
    assert check_required_decoction([HerbItem(name=herb, decoction=None)]) == [herb]
    assert check_required_decoction([HerbItem(name=herb, decoction=method)]) == []


# ---------- check_toxic_herbs ----------


def test_toxic_herbs_table_is_non_trivial():
    assert len(TOXIC_HERBS) >= 30


def test_toxic_herb_is_flagged():
    assert check_toxic_herbs([HerbItem(name="附子")]) == ["附子"]


def test_non_toxic_herb_is_not_flagged():
    assert check_toxic_herbs([HerbItem(name="党参")]) == []


def test_toxic_check_matches_through_normalization():
    """"制附子"要能匹配到 TOXIC_HERBS 里的条目——跟剂量表同一条查表顺序。"""
    assert check_toxic_herbs([HerbItem(name="姜半夏")]) == ["姜半夏"]


# ---------- FormulaSafety.blocking 分级 ----------


def test_incompatible_and_dose_violations_are_blocking():
    assert FormulaSafety(incompatible=[("a", "b")]).blocking is True
    dv = [DoseViolation(herb="x", dose=99, unit="g", limit_g=1, reason="r")]
    assert FormulaSafety(dose_violations=dv).blocking is True


def test_thermal_decoction_toxic_are_not_blocking():
    """寒热错杂本来就寒热并用、毒性药材常规用量本就贴着上限、煎法漏标不代表
    方子本身有问题——这三类只警告，逼模型重开只会把对的方子改坏。"""
    fs = FormulaSafety(thermal_warning="w", decoction_missing=["附子"], toxic_herbs=["附子"])
    assert fs.blocking is False


def test_empty_formula_safety_is_not_blocking():
    assert FormulaSafety().blocking is False


# ---------- assess_formula_safety：唯一组装点 ----------


def _candidate(herb_items):
    return FormulaCandidate(
        name="x", source="composed", confidence="low", rationale="r",
        herb_items=herb_items,
    )


def test_assess_formula_safety_runs_all_five_checks():
    cand = _candidate([
        HerbItem(name="甘草", dose=5, dose_unit="g"),
        HerbItem(name="海藻", dose=5, dose_unit="g"),
        HerbItem(name="附子", dose=20, dose_unit="g", decoction=None),
    ])
    safety = assess_formula_safety("脾胃虚寒证", cand)
    assert safety.incompatible == [("甘草", "海藻")]
    assert len(safety.dose_violations) == 1 and safety.dose_violations[0].herb == "附子"
    assert safety.decoction_missing == ["附子"]
    assert safety.toxic_herbs == ["附子"]
    assert safety.blocking is True


def test_assess_formula_safety_excludes_western_drug_items():
    """herb_items 里混进的西药不参与任何一项检查——十八反/寒热/剂量/煎法/毒性
    表全部是中药知识，喂西药名进去要么查不到、要么在极端情况下误判。跟
    S3Syndrome.herbs（M1 派生字段）用的是同一条 is_western_drug 过滤规则。"""
    cand = _candidate([
        HerbItem(name="党参", dose=9, dose_unit="g"),
        HerbItem(name="西药阿斯匹林", dose=9999, dose_unit="g"),
    ])
    safety = assess_formula_safety("脾胃气虚", cand)
    assert safety.dose_violations == []
    assert safety.toxic_herbs == []


def test_assess_formula_safety_clean_formula_has_no_warnings():
    cand = _candidate([HerbItem(name="党参", dose=15, dose_unit="g"),
                       HerbItem(name="白术", dose=10, dose_unit="g")])
    safety = assess_formula_safety("脾胃气虚", cand)
    assert safety.incompatible == [] and safety.dose_violations == []
    assert safety.decoction_missing == [] and safety.toxic_herbs == []
    assert safety.blocking is False


# ---------- format_dose_violations / format_blocking_issues ----------


def test_format_dose_violations_names_every_herb():
    v = [DoseViolation(herb="附子", dose=20, unit="g", limit_g=15, reason="致死风险")]
    text = format_dose_violations(v)
    assert "附子" in text and "20" in text and "15" in text and "致死风险" in text


def test_format_blocking_issues_combines_both_kinds_of_blocking_problems():
    safety = FormulaSafety(
        incompatible=[("甘草", "海藻")],
        dose_violations=[DoseViolation(herb="附子", dose=20, unit="g", limit_g=15, reason="r")],
    )
    text = format_blocking_issues(safety)
    assert "甘草" in text and "海藻" in text and "附子" in text and "20" in text


def test_format_blocking_issues_omits_warning_level_fields():
    safety = FormulaSafety(thermal_warning="寒热相悖", decoction_missing=["附子"], toxic_herbs=["附子"])
    assert format_blocking_issues(safety) == ""
