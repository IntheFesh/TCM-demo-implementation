"""core/safety_output.py 的离线测试（X2）：十八反十九畏 24 对逐对验证、
别名归一、寒热一致性、以及接进 run_physician 后的重开闭环。不需要网络。
"""
import pytest

from core.safety_output import (
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
