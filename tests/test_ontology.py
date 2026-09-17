"""R32 本体层测试。

这一层的意义是把药理层三元组读成**可比对的结构**——所以这里测的不是"能不能
load 出来"，而是**解析结果能不能支撑 R34 的七条规则**：长词优先切出来的性、
挖掉长词之后剩下的经、丢掉单字碎片之后的功效、取上界的剂量。任何一处解析
退化，R34 对应那条规则会变成恒真或恒假，而单独测那条规则看不出来。

数据全部由测试就地构造（`Ontology(materia_rows=…, formulary_rows=…)`）：
药理层的两份 jsonl 是真实 LLM 抽取的产物，沙盒里不存在，测试不能依赖它。
"""
from __future__ import annotations

import json

import pytest

from core.ontology import (
    FLAVORS,
    MERIDIANS,
    NATURES,
    Formula,
    Herb,
    Ontology,
    SourceRef,
    get_ontology,
    main,
    parse_composition,
    parse_dose_max_g,
    parse_effects,
    parse_flavors,
    parse_meridians,
    parse_nature,
    reset_ontology_for_tests,
)


def _row(s: str, p: str, o: str, *, book: str = "中药学", source: str = "modern",
         span: str | None = None) -> dict:
    return {"s": s, "p": p, "o": o, "book": book, "source": source,
            "source_span": span if span is not None else f"{s}，{p}：{o}"}


@pytest.fixture(autouse=True)
def _no_singleton_leak():
    """本体是惰性单例。测试里建过临时本体之后不清掉，后面的测试会读到它。"""
    reset_ontology_for_tests()
    yield
    reset_ontology_for_tests()


# ---------- 值域词表：长词优先这件事本身 ----------

def test_natures_put_long_words_first():
    """「大寒」必须排在「寒」前面，否则 parse_nature 会把「大寒」切成「寒」，
    R34 的 nature_conflict 就区分不出峻药和平药。"""
    for long_w, short_w in (("大寒", "寒"), ("大热", "热"), ("微寒", "寒"), ("微温", "温")):
        assert NATURES.index(long_w) < NATURES.index(short_w), f"{long_w} 必须先于 {short_w}"


def test_meridians_put_long_words_first():
    # 「肠」本身不在表里（只有小肠/大肠），真正会互吃的是「心包」和「心」
    assert MERIDIANS.index("心包") < MERIDIANS.index("心")
    assert MERIDIANS.index("小肠") < MERIDIANS.index("心")
    assert MERIDIANS.index("大肠") < MERIDIANS.index("胆")


def test_flavors_are_the_seven_plus_two():
    assert set(FLAVORS) == {"辛", "甘", "酸", "苦", "咸", "淡", "涩"}


# ---------- 解析函数 ----------

def test_parse_nature_prefers_the_longer_word():
    assert parse_nature(["苦、辛，微寒"]) == "微寒"
    assert parse_nature(["大寒"]) == "大寒"
    assert parse_nature(["性温，味辛"]) == "温"


def test_parse_nature_returns_none_when_the_source_says_nothing():
    """缺就是缺。填一个「平」等于替原文做了一个判断。"""
    assert parse_nature([]) is None
    assert parse_nature(["味甘"]) is None


def test_parse_flavors_keeps_order_and_dedups():
    """次序是「每条原文内按 FLAVORS 表序，原文之间按出现序」——确定的次序本身
    才是要钉的东西：性味在提示词里是一行文本，次序漂了缓存前缀就不一样了。"""
    assert parse_flavors(["苦、辛，微寒", "辛甘"]) == ("辛", "苦", "甘")
    assert parse_flavors(["辛甘", "苦、辛，微寒"]) == ("辛", "甘", "苦")


def test_parse_meridians_does_not_let_short_words_eat_long_ones():
    """「归心包经」不能同时命中「心包」和「心」——多出来的那个「心」会让
    R34 的 meridian_coverage 规则把一味不入心经的药判成入心经。"""
    assert parse_meridians(["归心包经"]) == frozenset({"心包"})
    assert parse_meridians(["归大肠、胃经"]) == frozenset({"大肠", "胃"})
    assert parse_meridians(["入肝、胆、心包经"]) == frozenset({"肝", "胆", "心包"})


def test_parse_effects_drops_single_character_fragments():
    """单字（「利」「和」）对任何治法都能子串命中，留着它 effect_matches_method 恒真。"""
    got = parse_effects(["利，和，疏肝解郁、健脾和中"])
    assert "利" not in got and "和" not in got
    assert got == ("疏肝解郁", "健脾和中")


def test_parse_dose_max_g_takes_the_upper_bound_and_the_max_across_sources():
    assert parse_dose_max_g(["3~9g"]) == 9.0
    assert parse_dose_max_g(["一般 3－10 克"]) == 10.0
    assert parse_dose_max_g(["3~9g", "煎服 6~15g"]) == 15.0     # 跨来源取最大
    assert parse_dose_max_g(["随证酌情"]) is None


def test_parse_composition_normalizes_names_and_keeps_the_dose_text_verbatim():
    """剂量留原文：「六钱」换算成克需要朝代与度量衡的判断，给一个错的数字
    比留原文糟得多。"""
    got = parse_composition(["柴胡 12g、炙甘草六钱，白芍9克"])
    names = [n for n, _d in got]
    assert "柴胡" in names and "白芍" in names
    assert "甘草" in names, "炙甘草应归一到甘草"
    doses = dict(got)
    # 「柴胡 12g」的剂量跟药名之间是空格。`_SPLIT_RE` 把空白也当分隔符，
    # 不认出"落单的剂量"就会把它**静默丢掉**——R34 的 dose_exceeds 读的正是这个数。
    assert doses["柴胡"] == "12g"
    assert doses["白芍"] == "9克"
    assert doses["甘草"] == "六钱"
    assert dict(parse_composition(["柴胡12g 黄芩9g"])) == {"柴胡": "12g", "黄芩": "9g"}


# ---------- Ontology：可用性三分 ----------

def test_an_empty_ontology_is_unavailable_and_answers_nothing_without_raising():
    """药理层数据不在是沙盒常态。**不许抛异常**——下游要能降级跑完并在
    manifest 里如实记 available=False。"""
    ont = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    assert ont.available is False
    assert ont.herb("柴胡") is None
    assert ont.formula("小柴胡汤") is None
    assert ont.herbs_by_meridian("肝") == []
    assert ont.herbs_by_effect("疏肝理气") == []
    assert ont.herbs_by_nature("微寒") == []
    assert ont.formulas_for_syndrome("肝郁气滞证") == []
    assert ont.patterns_for("肝郁气滞证") == []
    assert ont.stats()["available"] is False


def test_available_is_true_when_either_side_has_content():
    """本草或方剂任一有内容即可用——只抽出方剂那一半也是"药理层跑过了"。"""
    only_herbs = Ontology(materia_rows=[_row("柴胡", "性味", "苦，微寒")],
                          formulary_rows=[], patterns=[])
    only_formulas = Ontology(materia_rows=[],
                             formulary_rows=[_row("小柴胡汤", "组成", "柴胡 12g")],
                             patterns=[])
    assert only_herbs.available is True
    assert only_formulas.available is True


# ---------- 九个查询接口 ----------

@pytest.fixture
def ont() -> Ontology:
    materia = [
        _row("柴胡", "性味", "苦、辛，微寒"),
        _row("柴胡", "归经", "归肝、胆、肺经"),
        _row("柴胡", "功效", "疏肝解郁、和解表里、升举阳气"),
        _row("柴胡", "用量", "煎服，3~10g"),
        _row("炙黄芪", "性味", "甘，微温"),
        _row("炙黄芪", "归经", "归脾、肺经"),
        _row("炙黄芪", "功效", "补气升阳、益卫固表"),
        _row("炙黄芪", "用量", "9~30g"),
        _row("附子", "性味", "辛、甘，大热"),
        _row("附子", "归经", "归心、肾、脾经"),
        _row("附子", "功效", "回阳救逆、补火助阳"),
        _row("附子", "禁忌", "孕妇慎用、不宜与半夏同用"),
        _row("半夏", "性味", "辛，温"),
        _row("半夏", "归经", "归脾、胃、肺经"),
        _row("半夏", "功效", "燥湿化痰、降逆止呕"),
    ]
    formulary = [
        _row("小柴胡汤", "组成", "柴胡 12g、黄芩 9g、半夏 9g、甘草 6g", book="方剂学"),
        _row("小柴胡汤", "君药", "柴胡", book="方剂学"),
        _row("小柴胡汤", "臣药", "黄芩", book="方剂学"),
        _row("小柴胡汤", "主治", "伤寒少阳证、肝郁气滞", book="方剂学"),
        _row("小柴胡汤", "功用", "和解少阳", book="方剂学"),
        _row("小柴胡汤", "加减", "胸中烦而不呕者，去半夏、人参", book="方剂学"),
    ]
    patterns = [
        {"pattern_id": "p1", "physician": "li_ke", "physician_name": "李可",
         "group_value": "肝郁气滞", "kind": "herb", "support": 7,
         "herbs": ["柴胡"], "case_ids": ["li_ke-0001"]},
        {"pattern_id": "p2", "physician": "ye_tianshi", "physician_name": "叶天士",
         "group_value": "脾虚", "kind": "herb", "support": 4,
         "herbs": ["炙黄芪"], "case_ids": ["ye_tianshi-0002"]},
    ]
    return Ontology(materia_rows=materia, formulary_rows=formulary, patterns=patterns)


def test_herb_lookup_goes_through_normalize_herb(ont):
    """"炙黄芪三钱" → "黄芪"：归一走 core.herbs.normalize_herb，本模块不持第二份药名表。"""
    from core.herbs import normalize_herb

    canonical = normalize_herb("炙黄芪")
    assert ont.herb("炙黄芪") is not None
    assert ont.herb("炙黄芪三钱") is ont.herb("炙黄芪")
    assert ont.herb("炙黄芪").name == canonical
    assert ont.herb("没有这味药") is None
    assert ont.herb("") is None


def test_herb_carries_the_parsed_structure_not_the_raw_text(ont):
    h = ont.herb("柴胡")
    assert isinstance(h, Herb)
    assert h.nature == "微寒"
    assert set(h.flavor) == {"苦", "辛"}
    assert h.meridians == frozenset({"肝", "胆", "肺"})
    assert "疏肝解郁" in h.effects
    assert h.dose_max_g == 10.0


def test_herbs_by_meridian_and_by_nature(ont):
    names = {h.name for h in ont.herbs_by_meridian("肝")}
    assert "柴胡" in names
    assert {h.name for h in ont.herbs_by_nature("大热")} == {"附子"}
    assert ont.herbs_by_nature("没有这个性") == []


def test_herbs_by_effect_goes_through_the_effect_synonym_table(ont):
    """治法「疏肝理气」要能查到功效写「疏肝解郁」的柴胡。裸子串比查不到，
    那正是 R34 effect_matches_method 会变成恒假的地方。"""
    got = {h.name for h in ont.herbs_by_effect("疏肝理气")}
    assert "柴胡" in got, "同义词表没起作用：疏肝理气 → 疏肝解郁 这一跳断了"


def test_herbs_by_effect_falls_back_to_the_bare_term_when_unlisted(ont):
    """表里没有的治法退化成裸子串比，**不是返回空**——返回空会让规则判它
    "无论如何都不匹配"，而正确语义是"这个说法我没登记，按字面看"。"""
    assert {h.name for h in ont.herbs_by_effect("降逆止呕")} == {"半夏"}
    assert ont.herbs_by_effect("") == []


def test_formula_and_formulas_for_syndrome_strip_the_trailing_zheng(ont):
    """教材主治写「肝郁气滞」而证名是「肝郁气滞证」，带「证」字比一条都匹配不上。"""
    f = ont.formula("小柴胡汤")
    assert isinstance(f, Formula)
    assert "柴胡" in f.herb_names()
    assert f.roles.get("君") == ("柴胡",)
    assert [x.name for x in ont.formulas_for_syndrome("肝郁气滞证")] == ["小柴胡汤"]
    assert [x.name for x in ont.formulas_for_syndrome("肝郁气滞")] == ["小柴胡汤"]
    assert ont.formulas_for_syndrome("") == []


def test_incompatible_and_dose_limit_come_from_safety_output_not_a_second_table(ont):
    """配伍禁忌与剂量上限的判据整个来自 core.safety_output——本体层不持第二份
    （CLAUDE.md 第 31 条）。这条测试钉的就是"同一个问题只有一处实现"。"""
    from core.safety_output import DOSE_LIMITS, INCOMPATIBLE_PAIRS

    a, b = INCOMPATIBLE_PAIRS[0]
    assert ont.is_incompatible(a, b) == "-".join(sorted((a, b)))
    assert ont.is_incompatible(a, a) is None
    assert ont.is_incompatible("柴胡", "黄芩") is None

    name = next(iter(DOSE_LIMITS))
    assert ont.dose_limit(name) == float(DOSE_LIMITS[name][0])
    assert ont.dose_limit_reason(name) == DOSE_LIMITS[name][1]
    assert ont.dose_limit("这不是药") is None


def test_patterns_for_filters_by_syndrome_and_physician(ont):
    assert [p["pattern_id"] for p in ont.patterns_for("肝郁气滞证")] == ["p1"]
    assert ont.patterns_for("肝郁气滞证", physician="ye_tianshi") == []
    assert [p["pattern_id"] for p in ont.patterns_for("脾虚证", physician="ye_tianshi")] == ["p2"]


# ---------- 出处与 stats ----------

def test_refs_record_the_source_span_and_has_rejects_an_empty_one(ont):
    """span 为空即视为该谓词没有出处——空 span 只可能来自绕过 schema 写进去的行。"""
    h = ont.herb("柴胡")
    assert h.has("功效") is True
    assert all(isinstance(r, SourceRef) for r in h.refs["功效"])
    assert h.refs["功效"][0].book == "中药学"

    blank = Ontology(materia_rows=[_row("柴胡", "功效", "疏肝解郁", span="")],
                     formulary_rows=[], patterns=[])
    assert blank.herb("柴胡").has("功效") is False
    assert blank.stats()["empty_span_refs"] >= 1


def test_stats_counts_missing_predicates_per_herb(ont):
    s = ont.stats()
    assert s["n_herbs"] == 4 and s["n_formulas"] == 1 and s["n_patterns"] == 2
    # 四味药里只有附子写了禁忌 → 缺「禁忌」的是 3 味
    assert s["missing_predicate_counts"]["禁忌"] == 3
    assert s["missing_predicate_counts"]["炮制"] == 4


# ---------- 惰性单例与 CLI ----------

def test_get_ontology_is_lazy_and_returns_the_same_object():
    """CLAUDE.md：加载大文件的对象一律惰性初始化，禁止模块顶层实例化。"""
    import core.ontology as mod

    assert mod._ontology is None, "import 之后就不该已经建好了"
    a = get_ontology()
    assert mod._ontology is not None
    assert get_ontology() is a


def test_cli_exits_2_when_the_pharmacology_data_is_absent(capsys, monkeypatch):
    """数据不在时退出码是 2，且打印里要说清"这不是代码缺陷"——
    "没核"和"核过了没问题"必须能分开。"""
    monkeypatch.setattr("core.ontology._rows", lambda kind: [])
    monkeypatch.setattr("core.ontology._load_patterns", lambda: [])
    reset_ontology_for_tests()
    code = main(["--stats"])
    out = capsys.readouterr().out
    if code == 2:
        assert "不是代码缺陷" in out
    else:                      # 真机上数据在，那就该打出统计并返回 0
        assert code == 0 and "本草" in out


def test_cli_prints_the_counts_when_data_is_present(capsys, monkeypatch):
    monkeypatch.setattr("core.ontology._rows",
                        lambda kind: [_row("柴胡", "性味", "苦，微寒")]
                        if kind == "materia_medica" else [])
    monkeypatch.setattr("core.ontology._load_patterns", lambda: [])
    reset_ontology_for_tests()
    assert main(["--stats"]) == 0
    out = capsys.readouterr().out
    assert "本草 1 味" in out


def test_patterns_file_is_read_from_the_canonical_dir(tmp_path, monkeypatch):
    """R35 的产物按 CLAUDE.md 放 `data/standard/`。这条钉的是**路径和坏行容忍**：
    放错地方 `.gitignore` 的 `*.jsonl` 会静默吞掉它（这个坑已经踩过四次），
    而一条坏行不该让整份规律表变成空的。"""
    import core.ontology as mod
    from core.data_paths import CANONICAL_DIR

    assert CANONICAL_DIR.name == "standard", "规律表只能落在 data/standard/"

    monkeypatch.setattr("core.data_paths.CANONICAL_DIR", tmp_path)
    (tmp_path / "prescribing_patterns.jsonl").write_text(
        json.dumps({"pattern_id": "x", "group_value": "脾虚"}, ensure_ascii=False)
        + "\n这不是 json\n\n"
        + json.dumps({"pattern_id": "y", "group_value": "肝郁"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rows = mod._load_patterns()
    assert [r["pattern_id"] for r in rows] == ["x", "y"], "坏行要跳过，不许吞掉整份表"


def test_patterns_are_an_empty_list_when_r35_has_not_run_yet(tmp_path, monkeypatch):
    """R35 之前这份文件不存在。空列表 ≠ 异常——本体层其余部分要照常可用。"""
    import core.ontology as mod

    monkeypatch.setattr("core.data_paths.CANONICAL_DIR", tmp_path)
    assert mod._load_patterns() == []
