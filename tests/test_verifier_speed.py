"""R40：符号验证器的批量查表（7N→1）与投机执行。

## 先说清楚这一轮在验证器上量到了什么

R40 的第一版 profile 报 `verify` = **2429 ms**，据此本轮的目标是"验证器 ≤ 200 ms"。
**那个数是错的**：2.4 秒是 `get_ontology()` 首次加载（1232 味 / 235 首）被记到了
`verify` 头上——`core/formula_verifier.py` 顶上写的是
`from core.ontology import get_ontology`，只给 `core.ontology.get_ontology` 包插桩
的话，验证器里那次调用走不到包装。修掉插桩、并让 profiler 真的跑 lifespan
（预热）之后，`verify` 是 **0.4 ms**。

所以批量查表省下来的是 **0.157 ms → 0.137 ms（−12.6%，12 味的方）**——
绝对值很小，如实写小。它仍然做，因为 7N 次重复归一是白做的功，而且
本体层将来换成远程服务时（一次查表变成一次 RPC）这个倍数才是关键。
"""
from __future__ import annotations

import time

import pytest

from core.formula_verifier import (
    ALL_RULES,
    INCREMENTAL_RULES,
    RULE_FUNCS,
    BatchedOntology,
    _all_names,
    verify_formula,
    verify_incremental,
)
from core.ontology import Ontology
from core.schemas import HerbItem
from tests.test_formula_verifier import _row, mk


@pytest.fixture()
def ont() -> Ontology:
    return Ontology(materia_rows=[
        _row("党参", "性味", "甘，平"), _row("党参", "归经", "归脾、肺经"),
        _row("党参", "功效", "补中益气、健脾益肺"), _row("党参", "用量", "9~30g"),
        _row("白术", "性味", "苦、甘，温"), _row("白术", "归经", "归脾、胃经"),
        _row("白术", "功效", "健脾益气、燥湿利水"), _row("白术", "用量", "6~12g"),
    ], formulary_rows=[], patterns=[])


# ---------- 批量查表 ----------


def test_herbs_batch_keeps_missing_names_as_none(ont):
    """查不到的名字**保留键、值为 None**。省掉的话调用方分不清"没查"和
    "查了没有"，而那正是 `Unverifiable` 与 `Violation` 的分界。"""
    got = ont.herbs_batch(["党参", "并不存在的药"])
    assert set(got) == {"党参", "并不存在的药"}
    assert got["党参"] is not None
    assert got["并不存在的药"] is None


def test_herbs_batch_normalizes_each_distinct_name_once(ont):
    """重复的名字只归一一次——这就是 7N→N 的来源。"""
    got = ont.herbs_batch(["党参", "党参", "白术"])
    assert list(got) == ["党参", "白术"]


def test_herbs_batch_goes_through_the_same_normalization_as_herb(ont):
    """**不许另写一套匹配**（CLAUDE.md 第 31 条）：炮制前缀/剂量后缀的处理
    只能有一处实现。"炙党参三钱" 两条路必须得到同一个条目。"""
    assert ont.herbs_batch(["炙党参三钱"])["炙党参三钱"] is ont.herb("炙党参三钱")


def test_batched_ontology_answers_the_same_as_the_raw_one(ont):
    b = BatchedOntology(ont, ["党参", "白术"])
    for name in ("党参", "白术", "石膏"):
        assert b.herb(name) is ont.herb(name)


def test_batched_ontology_reports_hits_and_misses(ont):
    """misses 远大于 hits 就说明预解析的名字集合取错了（规则在查方子以外的
    药名），批量化没生效——这个数要能看见，不能靠猜。"""
    b = BatchedOntology(ont, ["党参"])
    b.herb("党参")
    b.herb("党参")
    b.herb("白术")
    assert (b.hits, b.misses) == (2, 1)


def test_batched_ontology_delegates_everything_else(ont):
    """委托而不是继承：本体的其余方法原样透出去。"""
    b = BatchedOntology(ont, ["党参"])
    assert b.available is ont.available
    assert len(b.herbs) == len(ont.herbs)
    assert b.is_incompatible("甘草", "甘遂") == ont.is_incompatible("甘草", "甘遂")


def test_all_names_covers_herb_choices_too():
    """`check_herb_grounded` 遍历的是 `herb_choices`。只取 `herb_items` 会让
    grounded 那一条全部落到 misses 上。"""
    s3 = mk(["党参", "白术"])
    assert sorted(set(_all_names(s3))) == ["党参", "白术"]
    assert len(_all_names(s3)) == 4, "两个字段各贡献一份（去重交给 herbs_batch）"


def test_verify_formula_resolves_every_name_through_the_batch(ont, monkeypatch):
    """**7N→1 的验收判据**：一次 `verify_formula` 里 `herbs_batch` 只被调一次，
    而七条规则加起来查了不止一次表——这两个数一起才说明批量真的生效了。"""
    calls = []
    original = Ontology.herbs_batch

    def spy(self, names):
        calls.append(list(names))
        return original(self, names)

    monkeypatch.setattr(Ontology, "herbs_batch", spy)
    verify_formula(mk(["党参", "白术"]), ontology=ont)
    assert len(calls) == 1, f"herbs_batch 被调了 {len(calls)} 次，批量没生效"


def test_the_batch_is_per_call_not_a_process_cache(ont):
    """缓存范围是**一次 verify_formula**。跨调用缓存会让换本体之后的验证读到旧值。"""
    b1 = BatchedOntology(ont, ["党参"])
    other = Ontology(materia_rows=[_row("党参", "性味", "改过的")],
                     formulary_rows=[], patterns=[])
    b2 = BatchedOntology(other, ["党参"])
    assert b1.herb("党参") is not b2.herb("党参")


def test_batching_does_not_change_the_verdict(ont):
    """**不为性能牺牲正确性。** 逐条查和批量查必须给出一字不差的结论。"""
    s3 = mk(["党参", "白术"])
    batched = verify_formula(s3, ontology=ont).to_dict()
    # 逐条查：直接用裸本体跑七条规则，自己拼同一份结论
    raw_v, raw_u, raw_c = [], [], []
    for rule in ALL_RULES:
        v, u, c = RULE_FUNCS[rule](s3, ont)
        raw_v.extend(v)
        raw_u.extend(u)
        raw_c.extend(c)
    assert [x.rule for x in raw_v] == [x["rule"] for x in batched["violations"]]
    assert [x.rule for x in raw_u] == [x["rule"] for x in batched["unverifiable"]]
    assert raw_c == list(batched["checked_rules"])


def test_a_twelve_herb_formula_verifies_well_under_the_budget():
    """R40 验收项「验证器耗时 ≤ 200 ms」。真本体（1232 味）、12 味的方。
    **性能预算进测试**（R40 §12 第 3 条），不是只写在报告里。"""
    herbs = ["柴胡", "白芍", "当归", "白术", "茯苓", "甘草",
             "薄荷", "生姜", "香附", "川芎", "陈皮", "枳壳"]
    s3 = mk(herbs, roles=["君", "君", "臣", "臣", "臣", "使"] + ["佐"] * 6)
    verify_formula(s3)                      # 预热：把本体层加载排除在计时之外
    t0 = time.perf_counter()
    for _ in range(20):
        verify_formula(s3)
    per_call_ms = (time.perf_counter() - t0) / 20 * 1000
    assert per_call_ms < 200, f"一次验证 {per_call_ms:.1f} ms，超了 200 ms 预算"


# ---------- 投机执行 ----------


def test_only_two_rules_need_nothing_but_the_herb_list():
    """**这张表多放一条就是在不完整的输入上下结论。**"""
    assert INCREMENTAL_RULES == ("incompatible_pair", "dose_exceeds")
    assert set(INCREMENTAL_RULES) < set(ALL_RULES)


def test_incremental_catches_an_incompatible_pair_from_names_alone():
    r = verify_incremental([HerbItem(name="甘草", dose=9.0),
                            HerbItem(name="甘遂", dose=3.0)])
    rules = {v.rule for v in r.vetoes}
    assert "incompatible_pair" in rules
    assert r.status == "vetoed"


def test_incremental_catches_an_over_limit_dose():
    r = verify_incremental([HerbItem(name="附子", dose=200.0),
                            HerbItem(name="甘草", dose=6.0)])
    assert any(v.rule == "dose_exceeds" for v in r.vetoes)


def test_incremental_runs_only_its_two_rules(ont):
    r = verify_incremental([HerbItem(name="党参", dose=9.0),
                            HerbItem(name="白术", dose=9.0)], ontology=ont)
    assert set(r.checked_rules) <= set(INCREMENTAL_RULES)
    assert "meridian_coverage" not in r.checked_rules


def test_incremental_without_an_ontology_reports_unverifiable_not_pass():
    """本体不可用时"符号验证通过"必须报成"一条都没验"——这条语义在
    投机执行这条路上同样不许松（它是 R34 那条铁律的同一句话）。"""
    empty = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    r = verify_incremental([HerbItem(name="甘草", dose=9.0)], ontology=empty)
    assert r.ontology_available is False
    assert r.passed is False
    assert {u.rule for u in r.unverifiable} == set(INCREMENTAL_RULES)


def test_incremental_does_not_build_a_full_s3_schema():
    """流式中途填不出合法的 `S3Structured`（每个字段都有 min_length=1 防幻觉
    约束）。**正确做法是新建一个不含那些字段的形状，不是放松原来的约束**
    ——CLAUDE.md 那条铁律原文就是这么写的。"""
    from core.formula_verifier import _ItemsOnlyS3

    shim = _ItemsOnlyS3((HerbItem(name="甘草", dose=9.0),))
    assert not hasattr(shim, "syndrome")
    assert not hasattr(shim, "method")
    assert len(shim.formula.candidate.herb_items) == 1


def test_the_final_verification_still_runs_all_seven_rules():
    """投机执行的结果**不复用**进最终验证。半截输入上的"通过"不能算通过。"""
    s3 = mk(["党参", "白术"])
    result = verify_formula(s3)
    covered = set(result.checked_rules) | {u.rule for u in result.unverifiable}
    assert covered == set(ALL_RULES)
