"""R59：`scripts/build_herb_aliases.py` 的零 LLM 候选生成逻辑。用小型合成
本体测——不依赖真实 `data/standard/materia_medica.jsonl`（1232 味），保证
秒级跑完、结果确定。真实候选表见 `data/standard/herb_alias_candidates.tsv`
（`python -m scripts.build_herb_aliases` 生成），人工审核结论见 R59 报告。
"""
from __future__ import annotations

from scripts.build_herb_aliases import (
    _all_variants,
    _drops_a_distinguishing_marker,
    extract_herb_names_from_cases,
    generate_candidates,
)

CANON = {
    "茯苓", "白术", "当归", "牛膝", "川牛膝", "木香", "川木香",
    "地黄", "熟地黄", "半夏", "生半夏", "栝蒌根",
}


def test_producer_prefix_strip_finds_an_exact_high_confidence_match():
    out = generate_candidates("云茯苓", CANON)
    assert out == [{"candidate": "茯苓", "rule": "产地前缀", "confidence": "high"}]


def test_modifier_prefix_strip_finds_an_exact_high_confidence_match():
    out = generate_candidates("全当归", CANON)
    assert out == [{"candidate": "当归", "rule": "修饰前缀", "confidence": "high"}]


def test_producer_prefix_never_conflates_two_pharmacopoeia_distinct_entries():
    """怀牛膝→牛膝没问题，但"川牛膝"已经是本体自己的正名——`build()` 会先用
    `ont.herb()` 短路掉已经能查到的写法，根本不会替它生成候选（真正校验
    "两个药典品种不会被剥成一个名字"的是 tests/test_herbs.py 的
    `test_r59_huai_niuxi_alias_does_not_pull_in_chuan_niuxi`，走的是完整的
    `HERB_ALIASES` + `ont.herb()` 链路）。这里只验证 `generate_candidates`
    本身在直接拿"川牛膝"当输入时不会瞎猜出别的候选——产地前缀剥完就是
    "牛膝"，本体里精确命中，候选就该是"牛膝"而不是别的东西。"""
    out = generate_candidates("川牛膝", CANON)
    assert out == [{"candidate": "牛膝", "rule": "产地前缀", "confidence": "high"}]


def test_marker_guard_blocks_a_backward_match_that_would_drop_a_distinguishing_chu_shu():
    """"熟怀地黄"反向子串匹配到"地黄"会丢掉"熟"这个字——而本体里"熟地黄"
    是单独一条正名，丢标记就可能把写法错配到另一个品种。这条候选必须
    生成不出来，逼人工介入，而不是悄悄给一个看似"唯一命中"的候选。"""
    assert _drops_a_distinguishing_marker("熟怀地黄", "地黄", CANON) is True
    out = generate_candidates("熟怀地黄", CANON)
    assert out == []


def test_marker_guard_allows_the_established_sheng_dihuang_pattern():
    """"生怀地黄"丢"生"落到"地黄"是安全的——本体没有单独的"生地黄"正名，
    这跟"生地"→"地黄"是同一个既有先例，不该被 guard 误伤。"""
    assert _drops_a_distinguishing_marker("生怀地黄", "地黄", CANON) is False
    out = generate_candidates("生怀地黄", CANON)
    assert out == [{"candidate": "地黄", "rule": "简称补全（子串）", "confidence": "low"}]


def test_marker_guard_allows_a_processing_marker_with_no_distinguishing_sibling():
    """"熟半夏"丢"熟"落到"半夏"是安全的——本体虽然另有"生半夏"，但没有
    单独的"熟半夏"正名，丢标记不会让候选选错本体收录的另一个品种。"""
    assert _drops_a_distinguishing_marker("熟半夏", "半夏", CANON) is False


def test_stacked_rules_are_reported_as_medium_confidence():
    out = generate_candidates("净杭萸肉", {"萸肉", "山萸肉"})
    assert out and out[0]["candidate"] == "萸肉"
    assert out[0]["confidence"] == "medium"  # 净+杭 两层前缀叠加，不是 high


def test_no_candidate_when_nothing_matches():
    assert generate_candidates("丈菊子", CANON) == []


def test_multiple_substring_matches_are_reported_as_medium_not_auto_picked():
    """"车前"没有可剥的产地/修饰前缀，落不到精确匹配，只能走子串匹配——
    本体里"车前草"（全草）和"车前子"（种子）是两个不同部位的正名，两个都
    是"车前"打头，候选生成器不该替人挑一个，必须两个都报、标 medium，
    交人工判断到底是哪个部位。"""
    out = generate_candidates("车前", {"车前草", "车前子"})
    names = {c["candidate"] for c in out}
    assert names == {"车前草", "车前子"}
    assert all(c["confidence"] == "medium" for c in out)


def test_extract_herb_names_from_cases_strips_whitespace_and_skips_blanks():
    records = [{"herbs": [" 白术 ", "茯苓", ""]}, {"herbs": ["白术"]}, {}]
    counts = extract_herb_names_from_cases(records)
    assert counts["白术"] == 2
    assert counts["茯苓"] == 1
    assert "" not in counts


def test_all_variants_includes_the_original_name_unstripped_first():
    variants = _all_variants("云茯苓")
    assert variants[0] == ("云茯苓", [])
    assert ("茯苓", ["产地前缀"]) in variants
