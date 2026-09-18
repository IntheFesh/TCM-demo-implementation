"""R51 §1.4：治则规则对证候表的�covered覆盖率。

判据是`core.theory.coverage_stats()`：把 `data/standard/syndromes.jsonl`
过一遍 `filter_by_keywords` 得到脾胃门 174 条，对每条用它的 nature+location
去查 `principles_for`，报出「查得到治则」的比例。**必须 ≥ 80%**，不足要回
R51 补规则，不许悄悄把判据改松。
"""
from pathlib import Path

import pytest

from core import theory
from offline.build_graph import (
    DEFAULT_FILTER_KEYWORDS,
    STANDARD_PATH,
    filter_by_keywords,
    load_syndrome_definitions,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset():
    theory.reset_for_tests()
    yield
    theory.reset_for_tests()


def _load_syndromes() -> list[dict]:
    return [d.model_dump() for d in load_syndrome_definitions(STANDARD_PATH)]


def _pinyi_men_syndromes() -> list[dict]:
    """脾胃门那 174 条——跟 `core/tools.py` 里"174 个证候"是同一份过滤
    （`offline/build_graph.py` 的默认关键词、同一个加载函数），不另写一套筛法。"""
    defs = load_syndrome_definitions(STANDARD_PATH)
    return [d.model_dump() for d in filter_by_keywords(defs, DEFAULT_FILTER_KEYWORDS)]


def test_the_filtered_subset_matches_the_known_174():
    """这条钉住"174"这个数字本身没有漂移——它是后面覆盖率判据的分母。"""
    rows = [r for r in _pinyi_men_syndromes() if not r.get("is_category")]
    assert len(rows) == 174, f"脾胃门筛出 {len(rows)} 条，跟已知的 174 不一致"


def test_coverage_is_at_least_80_percent():
    rows = _pinyi_men_syndromes()
    s = theory.coverage_stats(rows)
    assert s["total"] == 174
    assert s["ratio"] >= 0.80, (
        f"治则覆盖率只有 {s['ratio']:.1%}（{s['matched']}/{s['total']}），"
        "低于 80% 底线——回 R51 补 principles_for 能匹配到的规则，"
        "不许把这条判据改松")


def test_coverage_stats_reports_the_raw_numbers_not_just_a_verdict():
    """CLAUDE.md：任何数字都必须带对照——这里的对照就是分子分母都要报。"""
    rows = _pinyi_men_syndromes()
    s = theory.coverage_stats(rows)
    assert set(s) == {"total", "matched", "ratio"}
    assert s["matched"] <= s["total"]


def test_coverage_skips_category_rows():
    """`is_category=True` 的行是门类节点，不是具体证型，不该算进分母。"""
    rows = [{"nature": [], "location": [], "is_category": True}]
    s = theory.coverage_stats(rows)
    assert s["total"] == 0


def test_coverage_on_empty_input_does_not_divide_by_zero():
    s = theory.coverage_stats([])
    assert s == {"total": 0, "matched": 0, "ratio": 0.0}


def test_every_nature_value_in_the_full_table_has_at_least_one_principle():
    """13 种证素病性里，任何一种单独查都要能推出治则——这是覆盖率能到
    80%+ 的根本原因（通用治则兜底），这条钉住兜底本身没有漏掉哪一种。"""
    rows = _load_syndromes()
    natures = {n for r in rows for n in (r.get("nature") or [])}
    assert natures, "证候表里没有 nature 字段，测试数据本身有问题"
    missing = [n for n in natures if not theory.principles_for(n, [])]
    assert not missing, f"这些病性一条治则都推不出来：{missing}"


def test_organ_specific_principles_are_more_specific_than_universal_ones():
    """脾+湿要能同时拿到"健脾化湿"（具体）和"虚则补之"这类通用规则
    （宽泛）——两者都该在，不是二选一。"""
    hits = theory.principles_for("湿", "脾")
    specific = [h for h in hits if h.payload["when_location"]]
    universal = [h for h in hits if not h.payload["when_nature"]]
    assert specific, "脾+湿 一条具体化治则都没有"
    assert universal, "脾+湿 一条通用治则都没有"


def test_the_top_frequency_combos_are_all_covered():
    """按实测频次最高的几个 (location, nature) 组合逐一验，不只看总体比例
    ——总体达标不代表高频组合都覆盖到了。"""
    rows = _load_syndromes()
    from collections import Counter

    combos = Counter()
    for r in rows:
        if r.get("is_category"):
            continue
        for loc in r.get("location") or []:
            for nat in r.get("nature") or []:
                combos[(loc, nat)] += 1
    top10 = [c for c, _ in combos.most_common(10)]
    uncovered = [(loc, nat) for loc, nat in top10 if not theory.principles_for(nat, loc)]
    assert not uncovered, f"高频组合里这些查不到治则：{uncovered}"
