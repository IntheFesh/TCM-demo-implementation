"""开源数据合并的三条铁律（见 `offline/merge_open_sources.py` 的模块文档）+ 两条
安全边界（药典剂量表与十八反十九畏表不许被动）。

合并这种事的风险不在"少合并了几条"，在**悄悄改坏已经验证过的数据**。
所以这份测试盯的全是守卫，不是产量。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.herbs import HERB_ALIASES, merged_aliases, normalize_herb
from core.ontology import get_ontology
from core.safety_output import DOSE_LIMITS, INCOMPATIBLE_PAIRS, dose_limit_entry
from offline.merge_open_sources import (
    SPLIT_SPECIES,
    _is_truncation_of,
    _span_ok,
)

ROOT = Path(__file__).resolve().parent.parent
MATERIA = ROOT / "data" / "standard" / "materia_medica.jsonl"


def _rows():
    if not MATERIA.exists():
        pytest.skip("本草三元组不在")
    return [json.loads(x) for x in MATERIA.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------- 铁律二：每条都要带出处 ----------

def test_every_merged_row_carries_a_source_and_a_nonempty_span():
    """**这是本项目核心主张的探针**："每一步注明所依据的出处"。
    来路不明的数据进来，那个主张就废了。`--stats` 里"span 为空 0 条"是同一条。"""
    # 合并来的行由 offline/merge_open_sources.py 写上 `dataset`（数据集出处）；
    # `source` 只区分 classic / modern，自有数据同样有。
    merged = [r for r in _rows() if r.get("dataset")]
    if not merged:
        pytest.skip("本草三元组里没有合并来源的行")
    for r in merged:
        assert r.get("source"), f"{r['s']}/{r['p']} 没有 source"
        assert _span_ok(r.get("source_span") or ""), f"{r['s']}/{r['p']} 的 span 是空的"
        assert r.get("book"), f"{r['s']}/{r['p']} 没有书名"


def test_the_whole_ontology_still_has_zero_empty_spans():
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本体层数据文件不在")
    assert ont.stats()["empty_span_refs"] == 0


# ---------- 铁律一：只填空槽 ----------

def test_no_herb_predicate_got_two_conflicting_merged_values():
    """只填空槽的可观测后果：**一个 (药, 谓词) 槽位不会既有原本的值又有合并来的值**。
    合并的那条要是覆盖或并排塞进去，这条会红。"""
    from collections import defaultdict
    slots = defaultdict(set)
    for r in _rows():
        origin = "merged" if r.get("dataset") else "own"
        slots[(r["s"], r["p"])].add(origin)
    both = [k for k, v in slots.items() if v == {"merged", "own"}]
    assert both == [], f"这些槽位同时有自有数据和合并数据（说明没只填空槽）：{both[:5]}"


# ---------- 安全边界：药典两张表不许被动 ----------

def test_the_pharmacopoeia_dose_limits_are_untouched_by_the_merge():
    """合并来的"用量"**只进释义，不进 `dose_limit`**（见 `offline/merge_open_sources.py`
    的"不碰安全判据"一节）。这条逐一比对药典上限表里的每一味——合并让其中任何
    一个变了就是安全判据被污染。
    基线写死在这里：`DOSE_LIMITS` 是 62 条，合并前后都必须是 62 条。"""
    assert len(DOSE_LIMITS) == 62, f"药典上限表条数变了：{len(DOSE_LIMITS)}"
    for name, expected in DOSE_LIMITS.items():
        hit = dose_limit_entry(name)
        assert hit is not None, f"{name} 查不到药典上限了"
        # `dose_limit_entry` 返回 (克数, 理由)，跟表里的值同形——逐一比整个元组
        assert hit == expected, f"{name} 的上限从 {expected} 变成了 {hit}"


def test_the_incompatible_pairs_table_is_untouched():
    assert len(INCOMPATIBLE_PAIRS) == 24, f"十八反十九畏表条数变了：{len(INCOMPATIBLE_PAIRS)}"


def test_a_merged_dose_string_never_becomes_a_dose_limit():
    """合并进来的用量里有「二，三分。以至钱许」这种古制写法。
    它要是进了 `dose_limit`，剂量红条就会拿一个错的上限去判。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本体层数据文件不在")
    h = ont.herb("丹砂")
    if h is None or not h.has("用量"):
        pytest.skip("这一味没有合并到用量")
    assert dose_limit_entry("丹砂") is None or "丹砂" in DOSE_LIMITS


# ---------- 别名合并的两道守卫 ----------

@pytest.mark.parametrize("group", SPLIT_SPECIES)
def test_split_species_never_share_a_normalized_name(group):
    """药典分列的品种归一了就等于开错药。`tests/test_herbs.py` 已经钉住，这条从合并这一侧再钉一次。"""
    names = [n for n in group if n != group[0]]
    for n in names:
        assert n not in merged_aliases(), f"{n} 被合并表当成别名了"


def test_a_truncated_name_is_never_accepted_as_an_alias():
    """「洋参→西洋参」这种掉字写法必须丢——它会把两味药并成一味
    （`tests/test_herbs.py::test_distinct_herbs_never_collapse_into_one_name` 钉着这些对）。
    反方向（加修饰字，「紫丹参→丹参」）是正常的，要保留。"""
    assert _is_truncation_of("洋参", "西洋参")
    assert _is_truncation_of("五味子", "南五味子")
    assert not _is_truncation_of("紫丹参", "丹参")
    assert not _is_truncation_of("田七", "三七")
    for a, c in merged_aliases().items():
        assert not _is_truncation_of(a, c), f"掉字别名漏进来了：{a} → {c}"


def test_the_manual_table_wins_over_the_merged_one():
    """人工审过的那份优先。两份都有同一个写法时以人工那份为准
    ——否则人工审查的结论会被一次批量合并悄悄覆盖。"""
    overlap = set(HERB_ALIASES) & set(merged_aliases())
    for k in overlap:
        assert normalize_herb(k) == HERB_ALIASES[k], f"{k} 被合并表覆盖了"


def test_no_merged_alias_points_at_something_that_is_itself_a_herb():
    """别名本身是本体里另一味药时不许收——那会把两味药并成一味。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("本体层数据文件不在")
    # **比的是本体的正名键，不是 `ont.herb(a)`**：那个查询自己会过一次归一，
    # 合并表生效之后「一把伞南星」当然能查到天南星——拿它当判据是循环论证
    # （它测的只是"别名生效了没有"）。
    for a, c in merged_aliases().items():
        assert a not in ont.herbs, f"{a} 本身就是本体里的一味药，不该当别名"
        assert c in ont.herbs, f"{a} 指向的 {c} 不是本体里的正名"


def test_the_merged_alias_table_is_in_version_control_with_its_provenance():
    """花过成本的产物必须进版本控制（重新生成需要外部数据仓库的本地副本）。
    每行还要带出处（铁律二对这张表同样适用）。"""
    p = ROOT / "data" / "standard" / "herb_aliases_merged.tsv"
    assert p.exists()
    rows = [x for x in p.read_text(encoding="utf-8").splitlines()
            if x.strip() and not x.startswith("#") and not x.startswith("别名")]
    assert len(rows) > 300, f"表只有 {len(rows)} 行，像是被重跑截断了"
    for r in rows:
        cols = r.split("\t")
        assert len(cols) == 3 and all(c.strip() for c in cols), f"这一行缺列：{r}"


def test_regenerating_the_alias_table_is_idempotent():
    """重新生成别名表必须是幂等的：`normalize_herb` 会读合并表，所以第二次跑时
    上一次已合并的别名会全部落进"已经能归一了"那道过滤，输出几乎为空并把表
    覆盖成寥寥几行。生成时先清空内存里的合并表就避免了这一点，这条盯住它别被删掉。"""
    src = (ROOT / "offline" / "merge_open_sources.py").read_text(encoding="utf-8")
    assert "_herbs._merged = {}" in src, "生成别名时没有清空上一次的合并表"
