"""core/herbs.py 的离线测试。审查时实测「炒广皮」「广皮炭」「云苓块」归不到陈皮/茯苓、
「一钱半」残留在药名里——两位医家写法不同就被算成两味药，Jaccard 被系统性推高。"""
import pytest

from core.herbs import normalize_herb, strip_dose_and_parens


@pytest.mark.parametrize("variants,canonical", [
    (["广皮", "炒广皮", "广皮炭", "橘皮", "陈皮"], "陈皮"),
    (["云苓", "云苓块", "茯苓块", "茯苓皮", "白苓", "茯苓"], "茯苓"),
    (["川连", "云连", "炒川连", "姜川连", "真云连", "黄连"], "黄连"),
    (["生地", "生地黄", "干地黄", "大生地"], "地黄"),
    (["炙草", "炙甘草", "生甘草", "甘草"], "甘草"),
])
def test_same_herb_different_spellings_normalize_identically(variants, canonical):
    assert {normalize_herb(v) for v in variants} == {canonical}


def test_shudi_is_not_merged_into_dihuang():
    """熟地黄温、生地黄寒，是两味药性相反的药，不能归到一起。"""
    assert normalize_herb("熟地黄") != normalize_herb("生地黄")


@pytest.mark.parametrize("raw,expected", [
    ("白芍一钱半", "白芍"), ("茯苓钱半", "茯苓"), ("甘草一钱五分", "甘草"),
    ("党参三钱", "党参"), ("旋覆花二钱（包煎）", "旋覆花"), ("石膏一两半", "石膏"),
])
def test_dose_forms_are_stripped(raw, expected):
    assert normalize_herb(raw) == expected


def test_dose_regex_does_not_eat_herb_names_ending_in_unit_chars():
    """「黑顺片」「白附片」末尾的「片」不是剂量单位，前面没有数量词就不能剥。"""
    assert strip_dose_and_parens("黑顺片") == "黑顺片"
    assert strip_dose_and_parens("白附片") == "白附片"


def test_two_char_names_are_never_stripped_to_one_char():
    assert normalize_herb("生姜") == "生姜"
    assert normalize_herb("川芎") == "川芎"


# ---------- 第二轮复核：产地字不是炮制字 ----------

@pytest.mark.parametrize("a,b", [
    ("西洋参", "洋参"), ("川牛膝", "怀牛膝"), ("川木香", "木香"),
    ("北五味子", "南五味子"), ("生首乌", "制首乌"), ("生地黄", "熟地黄"),
])
def test_distinct_herbs_never_collapse_into_one_name(a, b):
    """产地字是药名的一部分，逐字剥会把不同的药归成同一个名字——比不归一更糟。
    生/制 同理：生首乌解毒通便、制首乌补益，功用相反。"""
    assert normalize_herb(a) != normalize_herb(b), f"{a} 与 {b} 被归成了同一个名字"


def test_chuanlianzi_keeps_its_canonical_name():
    """川楝子的 canonical 名就带「川」，剥成「楝子」是破坏药名。"""
    assert normalize_herb("川楝子") == "川楝子"


@pytest.mark.parametrize("raw,expected", [
    ("何首乌一两二钱", "何首乌"), ("白术三钱三分五厘", "白术"), ("石膏二两半", "石膏"),
])
def test_compound_doses_are_fully_stripped(raw, expected):
    assert normalize_herb(raw) == expected


# ---------- R59：HERB_ALIASES 从 70 条扩到 293 条，逐一核实没有把
# 药典分列的品种收成一条。这五对是 R59 任务书点名的例子——南/北五味子、
# 川/怀牛膝、川/广木香、生/熟地黄、生/制首乌各自药性或基原不同，新扩的
# 产地前缀候选（"广木香"→"木香"这类）必须只把写法并到它自己那个品种，
# 不能连带把另一个药典分列的品种也拖进来。 ----------

@pytest.mark.parametrize("a,b", [
    ("南五味子", "北五味子"),
    ("川牛膝", "怀牛膝"),
    ("川木香", "广木香"),
    ("生地黄", "熟地黄"),
    ("生首乌", "制首乌"),
])
def test_r59_pharmacopoeia_distinct_pairs_stay_apart(a, b):
    """这五对药典分列的品种，扩表前后都不能被 normalize_herb 并成一个名字。"""
    assert normalize_herb(a) != normalize_herb(b), f"{a} 与 {b} 被归成了同一个名字"


def test_r59_guangmuxiang_alias_does_not_pull_in_chuanmuxiang():
    """R59 新收的"广木香"→"木香"是产地前缀候选，"川木香"本体里是单独一条
    正名（跟"木香"不同基原）——广木香并入木香，不能连带把川木香也拖过来。"""
    from core.ontology import get_ontology
    ont = get_ontology()
    guang = ont.herb("广木香")
    chuan = ont.herb("川木香")
    assert guang is not None and chuan is not None
    assert guang.name == "木香"
    assert chuan.name == "川木香"
    assert guang.name != chuan.name


def test_r59_huai_niuxi_alias_does_not_pull_in_chuan_niuxi():
    """同理："怀牛膝"并入本体的"牛膝"，"川牛膝"必须仍是它自己那条正名。"""
    from core.ontology import get_ontology
    ont = get_ontology()
    huai = ont.herb("怀牛膝")
    chuan = ont.herb("川牛膝")
    assert huai is not None and chuan is not None
    assert huai.name == "牛膝"
    assert chuan.name == "川牛膝"
    assert huai.name != chuan.name
