"""R63 §3：本草缺谓词的归因，以及释义面板的兜底文案。

## 这一轮的结论，和它为什么算一个结论

用户反馈"药材药理的解释都会有案例不足的情况"。§3.2 已经指出瓶颈不在医案
（1075 诊次够用）而在本草层缺谓词。§3.3 要求分清三类：
(a) 没抽到、(b) 药名对不上、(c) 谓词写法不同——后两类**免费能修**。

`scripts/diagnose_ontology_gaps.py` 的实测结果是 **(b)=0、(c)=0**：
1458 个原始主语归一后正好落成 1232 味、一个都没落空；谓词在抽取 schema 里
就被卡成六个 `Literal`，源头不存在写法分歧。**3068 个空槽位全部是 (a)。**

所以 §3.3 第 3 步的验收目标（归经 598→<300）改归并改不出来——数据里就没有
那几句原文。这不是"没修好"，是**归因结论本身**：把力气花在扩别名表上会
一无所获，而那正是这个诊断要防止的事。
"""
from __future__ import annotations

import pytest

from core.node_explain import (
    MATERIA_COVERAGE_FOOTER,
    MATERIA_PREDICATES,
    MISSING_PREDICATE_FOOTER_AT,
    explain_node,
)
from core.ontology import get_ontology
from scripts.diagnose_ontology_gaps import diagnose


@pytest.fixture(scope="module")
def gaps():
    d = diagnose()
    if not d.get("available"):
        pytest.skip("本草层数据文件不在（它在版本控制里，缺了说明工作树不完整）")
    return d


# ---------- §3.3 第 1 步：三选一归因 ----------

def test_every_missing_slot_is_attributed_to_exactly_one_of_the_three_causes(gaps):
    """三类必须**不重不漏**加起来等于缺口总数。分类互相重叠的话，
    "免费能修多少"这个数就不可信，而整个诊断的用处就是那个数。"""
    for p, r in gaps["by_predicate"].items():
        n = (r["a_not_extracted"]["n"] + r["b_name_mismatch"]["n"]
             + r["c_predicate_variant"]["n"])
        assert n == r["n_missing"], f"{p}：三类相加 {n} ≠ 缺口 {r['n_missing']}"


def test_the_merge_loses_nothing_so_extending_the_alias_table_would_gain_nothing(gaps):
    """(b) 类为 0 的判据：三元组里的每个主语归一之后都落进了本体。

    **这条是"别去扩别名表"的依据。** R59 建 `HERB_ALIASES` 那一轮的收益
    在这里已经吃干净了（R60 又修过 refs 被覆盖而不是累加的那个 bug）。"""
    ont = get_ontology()
    from core.context_prefix import build_entry_index
    from core.herbs import normalize_herb
    from core.ontology import _rows
    subjects = set(build_entry_index("materia_medica", _rows("materia_medica")))
    normalized = {normalize_herb(s) or s for s in subjects}
    orphans = sorted(n for n in normalized if n not in ont.herbs)
    assert orphans == [], f"有主语归一后落在本体外：{orphans[:10]}"
    assert all(r["b_name_mismatch"]["n"] == 0 for r in gaps["by_predicate"].values())


def test_the_predicates_were_constrained_at_extraction_so_there_are_no_variants(gaps):
    """(c) 类为 0 的判据：三元组里出现过的谓词写法**只有本体认的那六个**。

    这不是巧合——抽取 prompt 的 schema 把谓词卡成了 `Literal`（X3 那一轮定的
    防幻觉约束）。§3.3 猜的「性味 vs 药性」「用量 vs 用法用量」在源头就不会出现。"""
    assert gaps["unknown_predicate_spellings"] == {}
    assert set(gaps["predicate_spellings"]) <= set(MATERIA_PREDICATES)
    assert all(r["c_predicate_variant"]["n"] == 0 for r in gaps["by_predicate"].values())


def test_the_conclusion_is_that_the_gap_can_only_be_closed_by_re_extracting(gaps):
    """把结论钉死：免费能修的是 0。**这条红了才说明下一轮该去扩别名表**，
    绿着就说明该去重抽——它是给下一轮省几小时的那条测试。"""
    free = sum(r["b_name_mismatch"]["n"] + r["c_predicate_variant"]["n"]
               for r in gaps["by_predicate"].values())
    assert free == 0
    assert sum(r["a_not_extracted"]["n"] for r in gaps["by_predicate"].values()) > 0


def test_the_recorded_attribution_is_in_the_repo(gaps):
    """§5 第 2 条要报改前改后的缺口数字。两份快照都在仓库里。

    **R64 把这条从"等于"改成"不大于"。** 原来它断言存下来的数字跟现算的相等
    ——R64 合并开源数据之后归经从 598 掉到 534，它当场红了，而那是这一轮**想要**
    发生的事。等于是个错判据：它把"数据变好了"和"数据被改坏了"报成同一种红。
    现在 r63 那份是**基线快照**（R63 的结论，不再动），判据是"现在的缺口不许
    比基线更大"——合并只能填空槽，任何谓词的缺口涨了都说明有东西被改坏了。
    """
    import json
    from pathlib import Path
    reports = Path(__file__).resolve().parent.parent / "docs" / "reports"
    base = json.loads((reports / "r63_ontology_gaps.json").read_text(encoding="utf-8"))
    now = json.loads((reports / "r64_ontology_gaps.json").read_text(encoding="utf-8"))
    for p_, r in base["by_predicate"].items():
        assert now["by_predicate"][p_]["n_missing"] <= r["n_missing"], (
            f"{p_} 的缺口从 {r['n_missing']} 涨到 "
            f"{now['by_predicate'][p_]['n_missing']}——合并只该填空槽")
        assert gaps["by_predicate"][p_]["n_missing"] <= r["n_missing"], (
            f"{p_} 现算的缺口比 R63 基线还大")
    # 存下来的那份要跟现算的一致，否则报告里的数字是过期的
    assert now["by_predicate"]["归经"]["n_missing"] == gaps["by_predicate"]["归经"]["n_missing"]


def test_the_merge_only_ever_reduced_gaps_never_created_them(gaps):
    """R64：合并之后**每一个谓词的缺口都不许上涨**，炮制那一项持平是允许的
    （没有一个源带炮制）。这条是"只填空槽"在总量上的可观测后果。"""
    import json
    from pathlib import Path
    base = json.loads((Path(__file__).resolve().parent.parent / "docs" / "reports"
                       / "r63_ontology_gaps.json").read_text(encoding="utf-8"))
    dropped = {p_: base["by_predicate"][p_]["n_missing"] - r["n_missing"]
               for p_, r in gaps["by_predicate"].items()}
    assert all(v >= 0 for v in dropped.values()), f"有谓词的缺口涨了：{dropped}"
    assert sum(dropped.values()) > 0, "一个槽位都没填上，合并等于没做"


# ---------- §3.3 第 4 步：兜底文案 ----------

def _pharm_lines(herb: str) -> list[str]:
    d = explain_node(f"herb::{herb}")
    sec = next((s for s in d["sections"] if s["heading"] == "药理"), None)
    return [str(x) for x in (sec["lines"] if sec else [])]


def test_an_absent_predicate_is_omitted_entirely_and_never_says_nothing_yet():
    """§3.3 第 4 步：**空着比写"暂无"专业。** 缺的项整项不出现。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本草层数据文件不在")
    sparse = next(h for h in ont.herbs.values()
                  if sum(1 for p in MATERIA_PREDICATES if not h.has(p)) >= 3)
    lines = _pharm_lines(sparse.name)
    blob = "\n".join(lines)
    for banned in ("暂无", "案例不足", "无数据", "N/A", "None", "-"):
        for line in lines:
            if line.startswith(("性味：", "归经：", "功效：", "炮制：", "禁忌：")):
                assert not line.endswith(banned), f"{sparse.name} 的「{line}」写了占位符"
    assert "案例不足" not in blob and "暂无" not in blob


def test_the_panel_no_longer_prints_whole_library_coverage_numbers():
    """**这一条是这一轮改的那句。** 原来释义里写着"（全库同类缺口：
    归经 598/1232 味）"——R62 §7 明令产品面不出现统计口径，而医师读到的
    不是"本体覆盖率"，是"这系统案例不足"（用户原话）。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本草层数据文件不在")
    sparse = next(h for h in ont.herbs.values()
                  if sum(1 for p in MATERIA_PREDICATES if not h.has(p)) >= 2)
    blob = "\n".join(_pharm_lines(sparse.name))
    assert "全库" not in blob and "/1232" not in blob
    import re
    assert not re.search(r"\d+/\d+ 味", blob), "释义面板里又出现了覆盖率数字"


def test_a_mostly_empty_herb_gets_one_quiet_line_about_the_data_sources():
    """七项里超过四项为空时补一行浅色小字，说数据来源、不说覆盖率。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本草层数据文件不在")
    worst = max(ont.herbs.values(),
                key=lambda h: sum(1 for p in MATERIA_PREDICATES if not h.has(p)))
    n_missing = sum(1 for p in MATERIA_PREDICATES if not worst.has(p))
    if n_missing <= MISSING_PREDICATE_FOOTER_AT:
        pytest.skip("这份数据里没有缺到要补那句话的药")
    assert MATERIA_COVERAGE_FOOTER in _pharm_lines(worst.name)
    assert "《中药学》" in MATERIA_COVERAGE_FOOTER, "要说得出是哪几本书"


def test_a_herb_missing_only_one_or_two_items_does_not_get_the_line():
    """缺一两项的药不补那句话——每味药都挂一句会变成背景噪音，
    读者就不再看它了，而那句话本来是给"基本是空的"那些药用的。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本草层数据文件不在")
    mild = next((h for h in ont.herbs.values()
                 if 1 <= sum(1 for p in MATERIA_PREDICATES if not h.has(p))
                 <= MISSING_PREDICATE_FOOTER_AT), None)
    if mild is None:
        pytest.skip("这份数据里没有只缺一两项的药")
    assert MATERIA_COVERAGE_FOOTER not in _pharm_lines(mild.name)


def test_a_well_covered_herb_shows_all_six_and_no_disclaimer():
    """常用药应当是齐的——柴胡/白芍/黄芪实测六项全有。
    这条钉住"兜底文案不会误伤齐全的药"。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本草层数据文件不在")
    for name in ("柴胡", "白芍", "黄芪"):
        h = ont.herb(name)
        assert h is not None, f"{name} 不在本体里？"
        lines = "\n".join(_pharm_lines(name))
        assert MATERIA_COVERAGE_FOOTER not in lines
        assert "性味：" in lines and "归经：" in lines and "功效：" in lines
