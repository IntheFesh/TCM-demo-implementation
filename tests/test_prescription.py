"""core/prescription.py 的离线测试：compute_herb_diffs（比对模型建议方跟
医生最终定方）、format_pharmacy_text（药房格式导出文本）。"""
from core.prescription import _display_width, compute_herb_diffs, format_pharmacy_text
from core.schemas import FormulaCandidate, HerbItem


def _formula(herb_items, **overrides) -> FormulaCandidate:
    base = dict(name="测试方", source="composed", confidence="medium", rationale="r")
    base.update(overrides)
    return FormulaCandidate(herb_items=herb_items, **base)


# ---------- compute_herb_diffs：五类描述格式 ----------


def test_diff_reports_removed_herb():
    old = _formula([HerbItem(name="甘草", dose=5, dose_unit="g")])
    new = _formula([HerbItem(name="党参", dose=9, dose_unit="g")])
    diffs = compute_herb_diffs(old, new)
    assert "去 甘草" in diffs


def test_diff_reports_added_herb_with_dose():
    old = _formula([HerbItem(name="党参", dose=9, dose_unit="g")])
    new = _formula([
        HerbItem(name="党参", dose=9, dose_unit="g"),
        HerbItem(name="海藻", dose=15, dose_unit="g"),
    ])
    diffs = compute_herb_diffs(old, new)
    assert "加 海藻 15g" in diffs


def test_diff_reports_added_herb_without_dose():
    """古籍医案常常不写剂量——加的这味药如果没标剂量，不能显示"加 X None"，
    应该干净地只显示药名。"""
    old = _formula([HerbItem(name="党参")])
    new = _formula([HerbItem(name="党参"), HerbItem(name="海藻", dose=None)])
    diffs = compute_herb_diffs(old, new)
    assert "加 海藻" in diffs
    assert not any("None" in d for d in diffs)


def test_diff_reports_dose_change():
    old = _formula([HerbItem(name="附子", dose=10, dose_unit="g")])
    new = _formula([HerbItem(name="附子", dose=15, dose_unit="g")])
    diffs = compute_herb_diffs(old, new)
    assert "附子 10g→15g" in diffs


def test_diff_reports_processing_change():
    old = _formula([HerbItem(name="半夏", dose=9, dose_unit="g", processing="生")])
    new = _formula([HerbItem(name="半夏", dose=9, dose_unit="g", processing="姜制")])
    diffs = compute_herb_diffs(old, new)
    assert any(d == "半夏 炮制 生→姜制" for d in diffs)


def test_diff_reports_decoction_change():
    old = _formula([HerbItem(name="附子", dose=15, dose_unit="g", decoction=None)])
    new = _formula([HerbItem(name="附子", dose=15, dose_unit="g", decoction="先煎")])
    diffs = compute_herb_diffs(old, new)
    assert any(d == "附子 煎法 无→先煎" for d in diffs)


def test_diff_same_herb_multiple_changes_produces_multiple_lines():
    """同一味药同时改了剂量和炮制——两条独立的 diff，不是合并成一句
    （合并会丢信息，没法从一条 diff 反推具体是哪个字段变了）。"""
    old = _formula([HerbItem(name="附子", dose=10, dose_unit="g", processing="生")])
    new = _formula([HerbItem(name="附子", dose=15, dose_unit="g", processing="炮制")])
    diffs = compute_herb_diffs(old, new)
    assert "附子 10g→15g" in diffs
    assert "附子 炮制 生→炮制" in diffs
    assert len(diffs) == 2


def test_diff_identical_formulas_produce_no_diffs():
    items = [HerbItem(name="党参", dose=9, dose_unit="g")]
    old = _formula([HerbItem(name="党参", dose=9, dose_unit="g")])
    new = _formula([HerbItem(name="党参", dose=9, dose_unit="g")])
    assert compute_herb_diffs(old, new) == []
    assert items  # 上面两份是独立构造的对象，不是共用引用——避免这条测试意外通过


def test_diff_worked_example_from_task_spec():
    """任务描述原文给的例子：["附子 10g→15g", "去 甘草", "加 海藻 15g"]。
    顺序不强求一致（去/加/改剂量三段各自的内部顺序才有意义），但三条都要
    出现且不多不少。"""
    old = _formula([
        HerbItem(name="附子", dose=10, dose_unit="g"),
        HerbItem(name="甘草", dose=5, dose_unit="g"),
    ])
    new = _formula([
        HerbItem(name="附子", dose=15, dose_unit="g"),
        HerbItem(name="海藻", dose=15, dose_unit="g"),
    ])
    diffs = compute_herb_diffs(old, new)
    assert set(diffs) == {"附子 10g→15g", "去 甘草", "加 海藻 15g"}


# ---------- format_pharmacy_text ----------


def test_pharmacy_text_includes_name_doses_count_and_usage():
    formula = _formula(
        [HerbItem(name="瓜蒌", dose=15, dose_unit="g")],
        name="瓜蒌薤白半夏汤加减", doses_count=7, usage="水煎服，每日1剂，分2次温服",
    )
    text = format_pharmacy_text(formula)
    lines = text.split("\n")
    assert lines[0].startswith("瓜蒌薤白半夏汤加减")
    assert "7 剂" in lines[0]
    assert "瓜蒌" in lines[1] and "15g" in lines[1]
    assert lines[-1] == "用法：水煎服，每日1剂，分2次温服"


def test_pharmacy_text_shows_processing_and_decoction_when_present():
    formula = _formula([
        HerbItem(name="半夏", dose=9, dose_unit="g", processing="姜制", decoction="先煎"),
    ])
    text = format_pharmacy_text(formula)
    assert "姜制" in text
    assert "先煎" in text


def test_pharmacy_text_omits_doses_count_and_usage_when_absent():
    """doses_count/usage 缺失时不编一个"7 剂"/"水煎服"出来——这是要给药房
    照单抓药的文本，编造用法/剂数比留白更危险。"""
    formula = _formula([HerbItem(name="党参", dose=9, dose_unit="g")],
                       doses_count=None, usage=None)
    text = format_pharmacy_text(formula)
    assert "剂" not in text.split("\n")[0]
    assert "用法" not in text


def test_pharmacy_text_herb_names_are_vertically_aligned():
    """中文药名按显示宽度（东亚宽字符占 2 列）对齐，不是按字符数——"党参"
    （2 字/显示宽度 4）和"海藻昆布"（4 字/显示宽度 8）这两行的剂量数字
    应该落在同一个显示列，而不是同一个字符下标（字符下标必然不同：药名
    补的是空格数而不是字符数，宽字符越多、需要补的空格越少，"党参"那行
    补 6 个空格、"海藻昆布"那行只补 2 个空格，字符长度天然不等，但两者
    加起来的*显示宽度*应该相等——这才是真实等宽字体渲染出来对不对齐的
    判据，不是 Python 字符串下标）。"""
    formula = _formula([
        HerbItem(name="党参", dose=9, dose_unit="g"),
        HerbItem(name="海藻昆布", dose=15, dose_unit="g"),
    ])
    text = format_pharmacy_text(formula)
    herb_lines = text.split("\n")[1:]
    dose_display_columns = [_display_width(line[: line.index("g")]) for line in herb_lines]
    assert len(set(dose_display_columns)) == 1, f"剂量列显示宽度没对齐：{herb_lines!r}"


def test_pharmacy_text_dose_without_trailing_zero():
    """15.0 应该显示成 15g，不是 15.0g——跟前端 herbItemLabel() 的行为
    保持一致（同一份数据两处展示不能出现"15g" vs "15.0g"这种不一致）。"""
    formula = _formula([HerbItem(name="瓜蒌", dose=15.0, dose_unit="g")])
    text = format_pharmacy_text(formula)
    assert "15g" in text
    assert "15.0g" not in text
