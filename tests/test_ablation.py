"""R38：四组消融的判据。**零 LLM、零网络**——真跑在 eval/ablation.py 里。

这里测的是那几件"错了也不会报错"的事：开关有没有串味、"不适用"有没有被
写成 0、假后端的内容指标有没有被当成真数、比率带没带分母。
"""
from __future__ import annotations

import os

import pytest

from eval.ablation import (
    GROUPS,
    KNOBS,
    aggregate,
    apply_group,
    compare_to_baseline,
    group_by_key,
    metrics_from_result,
    to_markdown,
)


def test_the_four_groups_change_exactly_one_knob_each():
    """**一组只动一个开关**。同时动两个就没法归因——而这件事只有在写组定义
    的时候看得出来，跑出来的数字里看不出来。"""
    assert [g.key for g in GROUPS] == ["A", "B", "C", "D"]
    assert GROUPS[0].env == {}, "A 组是基线，一个开关都不该设"
    for g in GROUPS[1:]:
        assert len(g.env) == 1, f"{g.key} 组动了 {len(g.env)} 个开关：{g.env}"
        assert set(g.env) <= set(KNOBS), f"{g.key} 组动了不在旋钮表里的变量"


def test_every_group_says_what_question_it_answers():
    """没有"它回答什么问题"的消融组，读报告的人只能看到一列数字。"""
    for g in GROUPS:
        assert g.asks.strip(), f"{g.key} 组没写它回答什么"
        assert g.name.strip()


def test_applying_a_group_clears_the_other_knobs(monkeypatch):
    """**串味是消融里最难查的错**：它不报错，只是让某一组的数字莫名其妙。
    跑 B 组时 C 组留下的 `S3_BEST_OF_N` 必须已经被清掉。"""
    monkeypatch.setenv("S3_BEST_OF_N", "3")
    monkeypatch.setenv("S1S2_MERGED", "1")
    # **进来之前是什么就还原成什么**——conftest 为了让上百条老测试继续测 legacy
    # 把 `S3_MODE` 钉住了，所以这里不能断言"跑完之后它不存在"，
    # 要断言的是"跑完之后它跟进来之前一样"。
    before = {k: os.environ.get(k) for k in KNOBS}
    with apply_group(group_by_key("B")):
        assert os.environ.get("S3_MODE") == "legacy"
        assert "S3_BEST_OF_N" not in os.environ, "上一组的 best-of-N 串进来了"
        assert "S1S2_MERGED" not in os.environ
    assert {k: os.environ.get(k) for k in KNOBS} == before


def test_applying_a_group_restores_even_on_exception(monkeypatch):
    """异常路径也要还原——否则一次失败会把后面所有组的环境都带歪。"""
    monkeypatch.setenv("S3_MODE", "structured")
    monkeypatch.delenv("S3_BEST_OF_N", raising=False)
    with pytest.raises(RuntimeError):
        with apply_group(group_by_key("C")):
            raise RuntimeError("boom")
    assert os.environ.get("S3_MODE") == "structured"
    # 本来没有的那个，异常之后也不许留下（留下就是下一组的串味源头）
    assert "S3_BEST_OF_N" not in os.environ


def _result(*, ratios=(0.5,), first_pass=(True,), hallucinated=(), mode="structured",
            calls=3):
    results = []
    for ratio, fp in zip(ratios, first_pass, strict=True):
        r = {"s3": object(), "herbs_grounded_ratio": ratio, "hallucinated": list(hallucinated)}
        if fp is not None:
            r["verifier_metrics"] = {"verifier_first_pass": fp}
        results.append(r)
    return {"results": results, "manifest": {"llm_calls": calls, "s3_mode": mode}}


def _row(result, *, ok=True, elapsed=1.0):
    return {"ok": ok, "error": None, "elapsed_s": elapsed,
            "llm_calls": (result or {}).get("manifest", {}).get("llm_calls"),
            "metrics": metrics_from_result(result)}


def test_a_legacy_group_reports_not_applicable_not_zero():
    """**这一条是这个模块存在的理由之一。** legacy 那一支不跑符号验证器，
    把"不适用"写成 0 会让对照组看起来像"一次都没过"——那是拿一个不存在的
    失败去抹黑它。"""
    rows = [_row(_result(first_pass=(None,), mode="legacy"))]
    agg = aggregate(rows, content_valid=True)
    assert agg["verifier_first_pass_rate"] is None
    assert agg["verifier_applicable"] is False
    assert "不适用" in agg["verifier_note"]
    assert "不适用" in to_markdown({
        "backend": {}, "n_queries": 1, "content_metrics_valid": True,
        "groups": {"B": {**agg, "name": "三列集注"}}})


def test_no_output_is_not_the_same_as_never_passing():
    """跑了但一次结论都没产出 ≠ "一次都没过"。两句话要不一样。"""
    rows = [_row(_result(ratios=(), first_pass=(), mode="structured"))]
    agg = aggregate(rows, content_valid=True)
    assert agg["verifier_applicable"] is True
    assert "没跑到" in agg["verifier_note"]


def test_a_fake_backend_never_produces_content_numbers():
    """假后端的产出是固定假文本。**它的"带本体出处占比"只反映假数据长什么样**，
    出数就是在编。调用数照报——那是结构性的，跟后端真假无关。"""
    rows = [_row(_result(ratios=(0.9,), first_pass=(True,)))]
    agg = aggregate(rows, content_valid=False)
    assert agg["herbs_grounded_ratio_mean"] is None
    assert agg["verifier_first_pass_rate"] is None
    assert agg["hallucination_rate"] is None
    assert agg["llm_calls_mean"] == 3
    assert "假后端" in agg["content_note"]
    md = to_markdown({"backend": {"id": "fake", "model": "x"}, "n_queries": 1,
                      "content_metrics_valid": False,
                      "groups": {"A": {**agg, "name": "产品默认"}}})
    assert "⏳" in md and "假后端" in md


def test_every_rate_carries_its_denominator():
    """比率必须带分子分母。分母写两处就会有一处忘了改（R35 那个百分比就是
    这么错的）。"""
    rows = [_row(_result(ratios=(0.4, 0.6), first_pass=(True, False))),
            _row(_result(ratios=(1.0,), first_pass=(True,), hallucinated=("x-1",)))]
    agg = aggregate(rows, content_valid=True)
    fp = agg["verifier_first_pass_rate"]
    assert fp == {"value": 0.6667, "n": 2, "denominator": 3}
    hr = agg["hallucination_rate"]
    assert hr["denominator"] == 2 and hr["n"] == 1 and hr["value"] == 0.5
    assert agg["herbs_grounded_ratio_mean"] == pytest.approx(0.6667, abs=1e-4)
    assert agg["herbs_grounded_n"] == 3
    assert "分母" in agg["herbs_grounded_denominator"] or "药味数" in agg["herbs_grounded_denominator"]


def test_failed_runs_do_not_enter_any_denominator():
    """跑挂的那几条不该进分母——否则失败会被算成"这一组表现差"。"""
    rows = [_row(_result(ratios=(0.5,), first_pass=(True,))),
            _row(None, ok=False)]
    agg = aggregate(rows, content_valid=True)
    assert agg["n_queries"] == 2 and agg["n_ok"] == 1
    assert agg["verifier_first_pass_rate"]["denominator"] == 1
    assert agg["hallucination_rate"]["denominator"] == 1


def test_the_baseline_diff_is_none_when_one_side_has_no_number():
    """缺一边就不给差值，**不拿 0 顶替**（0 会被读成"两组一样"）。"""
    groups = {
        "A": {"herbs_grounded_ratio_mean": 0.5, "llm_calls_mean": 3.0,
              "elapsed_s_mean": 10.0,
              "verifier_first_pass_rate": {"value": 0.5, "n": 1, "denominator": 2}},
        "B": {"herbs_grounded_ratio_mean": None, "llm_calls_mean": 5.0,
              "elapsed_s_mean": 12.0, "verifier_first_pass_rate": None},
    }
    out = compare_to_baseline(groups)
    assert out["B"]["delta_llm_calls_mean"] == 2.0
    assert out["B"]["delta_herbs_grounded_ratio_mean"] is None
    assert out["B"]["delta_verifier_first_pass_rate"] is None
    assert "A" not in out, "基线不跟自己比"


def test_metrics_read_the_manifest_not_the_env():
    """**这一组实际跑成了什么形状，从 manifest 读回来**——不信自己设的环境变量。
    设了 legacy 却跑成 structured（比如变量拼错）时，要能从报告里看出来。"""
    rows = [_row(_result(mode="legacy"))]
    agg = aggregate(rows, content_valid=True)
    assert agg["observed"]["s3_mode"] == "legacy"


def test_a_blocked_consult_contributes_nothing_but_still_counts_as_ok():
    """安全否决拦下的问诊没有方，**不进内容指标的分母**，但它是一次成功的
    问诊（系统如实拒绝了）——不能算成失败。"""
    blocked = {"results": [], "manifest": {"llm_calls": 1, "s3_mode": "structured"}}
    agg = aggregate([_row(blocked)], content_valid=True)
    assert agg["n_ok"] == 1
    assert agg["herbs_grounded_n"] == 0
    assert agg["herbs_grounded_ratio_mean"] is None
