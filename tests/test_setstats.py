"""core/setstats.py 的离线测试：Jaccard 距离与其重复采样统计量的边界。"""
import pytest

from core.setstats import aggregate_stats, jaccard_distance, pairwise_jaccard_stats


def test_jaccard_distance_identical_is_zero():
    assert jaccard_distance({"a", "b"}, {"a", "b"}) == 0.0


def test_jaccard_distance_disjoint_is_one():
    assert jaccard_distance({"a"}, {"b"}) == 1.0


def test_jaccard_distance_both_empty_is_zero():
    """两者都为空定义为 0——"没有可比较的内容"不等于"完全不同"。"""
    assert jaccard_distance(set(), set()) == 0.0


def test_jaccard_distance_one_empty_is_one():
    assert jaccard_distance(set(), {"a"}) == 1.0


def test_jaccard_distance_partial_overlap():
    assert jaccard_distance({"a", "b", "c"}, {"b", "c", "d"}) == pytest.approx(1 - 2 / 4)


def test_pairwise_jaccard_stats_needs_at_least_two():
    assert pairwise_jaccard_stats([]) is None
    assert pairwise_jaccard_stats([{"a"}]) is None


def test_pairwise_jaccard_stats_skips_none():
    """某次重复失败/跳过时上游传 None 占位，不应参与统计，也不该报错。"""
    stats = pairwise_jaccard_stats([{"a"}, None, {"a"}])
    assert stats is not None
    assert stats["n_pairs"] == 1
    assert stats["mean"] == 0.0


def test_pairwise_jaccard_stats_three_identical_sets():
    stats = pairwise_jaccard_stats([{"a", "b"}, {"a", "b"}, {"a", "b"}])
    assert stats["n_pairs"] == 3
    assert stats["mean"] == 0.0
    assert stats["p50"] == 0.0
    assert stats["p95"] == 0.0


def test_pairwise_jaccard_stats_reports_all_pair_values():
    stats = pairwise_jaccard_stats([{"a"}, {"b"}, {"a", "b"}])
    # (a,b)=1.0  (a,{a,b})=0.5  (b,{a,b})=0.5
    assert stats["n_pairs"] == 3
    assert sorted(stats["values"]) == [0.5, 0.5, 1.0]
    assert stats["mean"] == pytest.approx(2.0 / 3, abs=1e-4)  # 结果已 round(4)，容差要覆盖它


def test_aggregate_stats_empty_is_none():
    assert aggregate_stats([]) is None


def test_aggregate_stats_basic():
    stats = aggregate_stats([0.0, 0.5, 1.0])
    assert stats["mean"] == pytest.approx(0.5)
    assert stats["n"] == 3


def test_aggregate_stats_p95_is_within_range():
    stats = aggregate_stats([0.1, 0.2, 0.3, 0.4, 0.5])
    assert 0.0 <= stats["p95"] <= 0.5
