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
