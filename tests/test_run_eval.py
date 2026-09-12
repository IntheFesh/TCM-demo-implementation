"""eval/run_eval.py 的离线测试：不调用真实 consult()，用手写的结果字典
（跟 core/chain.consult() 的真实返回形状一致）直接喂给各个 metric 函数。
"""
import json
from types import SimpleNamespace

import pytest

from eval import run_eval as re


def _consult_result(
    rejected=False, insufficient=False, herb_jaccard=None,
    physician_results=None, llm_calls=6,
):
    divergence = None
    if not rejected and not insufficient:
        divergence = {"herb_jaccard": herb_jaccard} if herb_jaccard is not None else {"herb_jaccard": None}
    return {
        "rejected": rejected, "insufficient": insufficient,
        "divergence": divergence,
        "results": physician_results or [],
        "manifest": {"llm_calls": llm_calls},
    }


def _physician_result(hallucinated=None, no_reference_cases=False):
    return {"hallucinated": hallucinated or [], "no_reference_cases": no_reference_cases}


# ---------- divergence_vs_epsilon ----------


def test_divergence_unavailable_without_epsilon():
    r = re.divergence_vs_epsilon([_consult_result(herb_jaccard=0.5)], epsilon_online=None)
    assert r["available"] is False
    assert "estimate_epsilon" in r["note"]


def test_divergence_splits_above_and_within_epsilon():
    results = [
        _consult_result(herb_jaccard=0.1),  # 在噪声地板以内
        _consult_result(herb_jaccard=0.8),  # 真实分歧
        _consult_result(herb_jaccard=0.05),  # 在噪声地板以内
    ]
    r = re.divergence_vs_epsilon(results, epsilon_online=0.2)
    assert r["n_above_epsilon"] == 1
    assert r["n_within_epsilon"] == 2
    assert r["rate_above_epsilon"] == pytest.approx(1 / 3, abs=1e-3)  # 结果已 round(3)


def test_divergence_excludes_rejected_and_insufficient():
    results = [
        _consult_result(rejected=True),
        _consult_result(insufficient=True),
        _consult_result(herb_jaccard=0.9),
    ]
    r = re.divergence_vs_epsilon(results, epsilon_online=0.2)
    assert r["n_usable"] == 1
    assert r["n_queries"] == 3


def test_divergence_empty_usable_set_does_not_divide_by_zero():
    results = [_consult_result(rejected=True)]
    r = re.divergence_vs_epsilon(results, epsilon_online=0.2)
    assert r["rate_above_epsilon"] is None


# ---------- hallucination_by_reference_availability ----------


def test_hallucination_splits_by_reference_availability():
    results = [
        _consult_result(physician_results=[
            _physician_result(hallucinated=["fake-1"], no_reference_cases=False),
            _physician_result(hallucinated=[], no_reference_cases=False),
        ]),
        _consult_result(physician_results=[
            _physician_result(hallucinated=["fake-2"], no_reference_cases=True),
        ]),
    ]
    r = re.hallucination_by_reference_availability(results)
    assert r["with_reference_cases"] == {"n": 2, "n_hallucinated": 1, "rate": 0.5}
    assert r["without_reference_cases"] == {"n": 1, "n_hallucinated": 1, "rate": 1.0}


def test_hallucination_skips_rejected_and_insufficient_queries():
    results = [
        _consult_result(rejected=True, physician_results=[_physician_result(hallucinated=["should-not-count"])]),
        _consult_result(physician_results=[_physician_result()]),
    ]
    r = re.hallucination_by_reference_availability(results)
    assert r["with_reference_cases"]["n"] == 1


def test_hallucination_empty_bucket_rate_is_none():
    r = re.hallucination_by_reference_availability([])
    assert r["with_reference_cases"]["rate"] is None


# ---------- safety_veto_summary ----------


def test_safety_veto_summary_counts_and_avg_calls():
    results = [
        _consult_result(rejected=True, llm_calls=1),
        _consult_result(rejected=True, llm_calls=2),
        _consult_result(rejected=False, llm_calls=8),
        _consult_result(rejected=False, llm_calls=10),
    ]
    r = re.safety_veto_summary(results)
    assert r["n_vetoed"] == 2 and r["n_normal"] == 2
    assert r["veto_rate"] == 0.5
    assert r["avg_llm_calls_vetoed"] == 1.5
    assert r["avg_llm_calls_normal"] == 9.0


def test_safety_veto_summary_empty_input():
    r = re.safety_veto_summary([])
    assert r["veto_rate"] is None
    assert r["avg_llm_calls_vetoed"] is None


# ---------- retrieval_mode_comparison ----------


def test_retrieval_mode_comparison_bounded_modes_use_score_threshold():
    """dense/graph 都是 [0,1] 有界的真实相似度，可以套 MIN_RETRIEVAL_SCORE。"""
    per_query = {
        "dense": [("a", 0.9), ("b", 0.5), None, ("d", 0.8)],
        "graph": [("a", 0.9), ("b2", 0.9), ("c", 0.75), None],
    }
    r = re.retrieval_mode_comparison(per_query, "dense", "graph")
    assert r["n_queries"] == 4
    assert "MIN_RETRIEVAL_SCORE" in r["criterion"]
    # dense 达标: [T(0.9>=0.7), F(0.5), F(None), T(0.8)] -> 2
    assert r["n_confident_a"] == 2
    # graph 达标: [T(0.9), T(0.9), T(0.75), F(None)] -> 3
    assert r["n_confident_b"] == 3
    # top1 差异：idx0 same "a", idx1 b vs b2 differ, idx2 None vs c differ, idx3 d vs None differ
    assert r["n_top1_differs"] == 3
    assert "mcnemar" in r and "p_value" in r["mcnemar"]


def test_retrieval_mode_comparison_bm25_falls_back_to_has_result_criterion():
    """bm25 的展示分无界（常见 10-30），套 MIN_RETRIEVAL_SCORE=0.70 毫无意义——
    这不是"bm25 更自信"，是刻度不可比。跟 bm25 比较时必须退化成"有没有
    返回结果"这个两种刻度下都成立的判据。"""
    per_query = {
        "dense": [("a", 0.9), None, ("c", 0.3)],
        "bm25": [("a", 25.0), ("b", 12.0), None],
    }
    r = re.retrieval_mode_comparison(per_query, "dense", "bm25")
    assert "MIN_RETRIEVAL_SCORE" not in r["criterion"]
    # dense 有结果: [T, F, T] -> 2；bm25 有结果: [T, T, F] -> 2
    # 注意 idx2 dense 分数 0.3 明显低于阈值，但"有结果"判据下仍算命中——
    # 这正是刻度不可比时刻意选的更弱、但双方都能用的判据。
    assert r["n_confident_a"] == 2
    assert r["n_confident_b"] == 2


def test_retrieval_mode_comparison_length_mismatch_raises():
    with pytest.raises(ValueError, match="查询数不一致"):
        re.retrieval_mode_comparison({"a": [None], "b": [None, None]}, "a", "b")


def test_retrieval_mode_comparison_identical_modes_zero_discordant():
    per_query = {"a": [("x", 0.9), None], "b": [("x", 0.9), None]}
    r = re.retrieval_mode_comparison(per_query, "a", "b")
    assert r["mcnemar"]["n_discordant"] == 0
    assert r["n_top1_differs"] == 0


# ---------- 消融通用机制：collect_ablation_pairs / collect_refs_mode_pairs /
#             ablation_output_effect（E3/E4/E9 共用）----------


def _fake_consult(rejected=False, insufficient=False, herbs_by_physician=None):
    results = [
        {"physician": p, "s3": SimpleNamespace(herbs=herbs)}
        for p, herbs in (herbs_by_physician or {}).items()
    ]
    return {"rejected": rejected, "insufficient": insufficient, "results": results}


def _epsilon_detail(entries):
    """entries: [(query, physician, mean), ...] -> epsilon_online_detail 形状。"""
    per_query = {}
    for query, physician, mean in entries:
        per_query.setdefault(query, {"query": query, "skipped": False, "by_physician": {}})
        per_query[query]["by_physician"][physician] = {"mean": mean}
    return {"per_query": list(per_query.values())}


def test_collect_ablation_pairs_pairs_by_query_and_physician():
    calls = []

    def consult_fn(query, **kwargs):
        calls.append((query, kwargs.get("use_react")))
        herbs = {False: {"叶天士": ["党参", "白术"]}, True: {"叶天士": ["党参", "黄芪"]}}
        return _fake_consult(herbs_by_physician=herbs[kwargs["use_react"]])

    pairs = re.collect_ablation_pairs(
        ["主诉甲"], {"use_react": False}, {"use_react": True}, consult_fn=consult_fn
    )
    assert calls == [("主诉甲", False), ("主诉甲", True)]
    assert len(pairs) == 1
    p = pairs[0]
    assert p["skipped"] is False
    assert p["query"] == "主诉甲" and p["physician"] == "叶天士"
    assert p["own_herbs"] == {"党参", "白术"}
    assert p["ablated_herbs"] == {"党参", "黄芪"}


def test_collect_ablation_pairs_skips_when_baseline_side_rejected():
    def consult_fn(query, **kwargs):
        if not kwargs["use_react"]:
            return _fake_consult(rejected=True)
        return _fake_consult(herbs_by_physician={"叶天士": ["党参"]})

    pairs = re.collect_ablation_pairs(
        ["主诉甲"], {"use_react": False}, {"use_react": True}, consult_fn=consult_fn
    )
    assert pairs == [{"query": "主诉甲", "skipped": True,
                       "reason": "基线或消融侧被安全否决/信息不足，无法配对比较"}]


def test_collect_ablation_pairs_skips_when_ablated_side_insufficient():
    def consult_fn(query, **kwargs):
        if kwargs["use_react"]:
            return _fake_consult(insufficient=True)
        return _fake_consult(herbs_by_physician={"叶天士": ["党参"]})

    pairs = re.collect_ablation_pairs(
        ["主诉甲"], {"use_react": False}, {"use_react": True}, consult_fn=consult_fn
    )
    assert pairs[0]["skipped"] is True


def test_collect_ablation_pairs_only_keeps_physicians_present_on_both_sides():
    def consult_fn(query, **kwargs):
        if not kwargs["use_react"]:
            return _fake_consult(herbs_by_physician={"叶天士": ["党参"], "吴鞠通": ["黄芪"]})
        return _fake_consult(herbs_by_physician={"叶天士": ["党参"]})  # 吴鞠通这次没有结果

    pairs = re.collect_ablation_pairs(
        ["主诉甲"], {"use_react": False}, {"use_react": True}, consult_fn=consult_fn
    )
    assert [p["physician"] for p in pairs] == ["叶天士"]


def test_collect_ablation_pairs_isolating_defaults_can_be_overridden():
    """baseline_kwargs/ablated_kwargs 里显式传的 use_react 要覆盖隔离默认值
    ——E9 消融的就是这个开关本身。"""
    seen_use_react = []

    def consult_fn(query, **kwargs):
        seen_use_react.append(kwargs["use_react"])
        return _fake_consult(herbs_by_physician={"叶天士": ["党参"]})

    re.collect_ablation_pairs(
        ["主诉甲"], {"use_react": False}, {"use_react": True}, consult_fn=consult_fn
    )
    assert seen_use_react == [False, True]


def test_collect_refs_mode_pairs_shares_own_across_modes():
    """E3+E4 一起跑时 own 只应该被调用一次每条主诉，不是每个 ablated_mode
    各调用一次——这是它跟 collect_ablation_pairs 分开存在的唯一理由。"""
    calls = []

    def consult_fn(query, **kwargs):
        calls.append(kwargs["refs_mode"])
        herbs = {
            "own": {"叶天士": ["党参"]},
            "swapped": {"叶天士": ["黄芪"]},
            "none": {"叶天士": []},
        }
        return _fake_consult(herbs_by_physician=herbs[kwargs["refs_mode"]])

    pairs_by_mode = re.collect_refs_mode_pairs(["主诉甲"], ["swapped", "none"], consult_fn=consult_fn)
    assert calls.count("own") == 1
    assert calls.count("swapped") == 1 and calls.count("none") == 1
    assert pairs_by_mode["swapped"][0]["ablated_herbs"] == {"黄芪"}
    assert pairs_by_mode["none"][0]["ablated_herbs"] == set()


def test_ablation_output_effect_change_rate_is_mean_jaccard_distance():
    pairs = [
        {"skipped": False, "query": "q1", "physician": "叶天士",
         "own_herbs": {"党参", "白术"}, "ablated_herbs": {"党参", "白术"}},  # 距离 0
        {"skipped": False, "query": "q2", "physician": "叶天士",
         "own_herbs": {"党参"}, "ablated_herbs": {"黄芪"}},  # 距离 1（毫无重叠）
    ]
    r = re.ablation_output_effect(pairs, epsilon_online_detail=None, label="swapped")
    assert r["change_rate"] == pytest.approx(0.5)
    assert r["n_usable"] == 2 and r["n_total"] == 2
    assert r["label"] == "swapped"


def test_ablation_output_effect_gate_threshold():
    high = [{"skipped": False, "query": "q1", "physician": "叶天士",
             "own_herbs": {"党参"}, "ablated_herbs": {"黄芪"}}]  # 距离 1.0
    low = [{"skipped": False, "query": "q1", "physician": "叶天士",
            "own_herbs": {"党参", "白术"}, "ablated_herbs": {"党参", "白术", "黄芪"}}]  # 距离 1/3
    assert re.ablation_output_effect(high, None, "swapped")["gate_pass"] is True
    assert re.ablation_output_effect(low, None, "swapped")["gate_pass"] is False


def test_ablation_output_effect_skips_skipped_pairs_from_denominator():
    pairs = [
        {"skipped": True, "query": "q1", "reason": "x"},
        {"skipped": False, "query": "q2", "physician": "叶天士",
         "own_herbs": {"党参"}, "ablated_herbs": {"党参"}},
    ]
    r = re.ablation_output_effect(pairs, None, "swapped")
    assert r["n_total"] == 2 and r["n_usable"] == 1


def test_ablation_output_effect_no_usable_pairs_returns_none_change_rate():
    r = re.ablation_output_effect(
        [{"skipped": True, "query": "q1", "reason": "x"}], None, "swapped"
    )
    assert r["change_rate"] is None and r["gate_pass"] is None


def test_ablation_output_effect_pairs_epsilon_by_query_and_physician_not_global():
    """核心行为：ε 必须按 (query, physician) 找配对值，不能用全局 ε 做减法——
    这里两条样本的原始距离一样，但配对 ε 不同，一条该判真实差异，另一条不该。"""
    pairs = [
        {"skipped": False, "query": "q1", "physician": "叶天士",
         "own_herbs": {"党参", "白术"}, "ablated_herbs": {"党参", "黄芪"}},
        {"skipped": False, "query": "q2", "physician": "叶天士",
         "own_herbs": {"党参", "白术"}, "ablated_herbs": {"党参", "黄芪"}},
    ]
    # 两条距离都是 1 - 1/3 = 2/3 ≈ 0.667
    epsilon_detail = _epsilon_detail([
        ("q1", "叶天士", 0.9),   # ε 比距离大 -> 不算真实差异
        ("q2", "叶天士", 0.1),   # ε 比距离小 -> 算真实差异
    ])
    r = re.ablation_output_effect(pairs, epsilon_detail, "swapped")
    assert r["n_above_paired_epsilon"] == 1
    assert r["n_within_paired_epsilon"] == 1
    assert r["n_no_paired_epsilon"] == 0


def test_ablation_output_effect_missing_paired_epsilon_counted_separately():
    pairs = [
        {"skipped": False, "query": "q1", "physician": "叶天士",
         "own_herbs": {"党参"}, "ablated_herbs": {"黄芪"}},
    ]
    r = re.ablation_output_effect(pairs, epsilon_online_detail=None, label="swapped")
    assert r["n_no_paired_epsilon"] == 1
    assert r["n_above_paired_epsilon"] == 0 and r["n_within_paired_epsilon"] == 0
    assert r["rate_above_paired_epsilon"] is None


# ---------- E8：collect_retriever_mode_samples / retriever_mode_output_effect ----------


def test_collect_retriever_mode_samples_records_herb_sets_per_mode():
    def consult_fn(query, **kwargs):
        herbs = {"dense": ["党参"], "graph": ["党参", "白术"]}
        return {
            "retrieval_error": None, "rejected": False, "insufficient": False,
            "results": [{"physician": "叶天士", "s3": SimpleNamespace(herbs=herbs[kwargs["retriever_mode"]])}],
        }

    records = re.collect_retriever_mode_samples(["主诉甲"], ["dense", "graph"], consult_fn=consult_fn)
    assert len(records) == 1
    r = records[0]
    assert r["query"] == "主诉甲" and r["physician"] == "叶天士"
    assert r["herb_sets"] == [{"党参"}, {"党参", "白术"}]
    assert r["n_modes_available"] == 2
    assert r["unavailable_modes"] == []


def test_collect_retriever_mode_samples_records_unavailable_modes_without_dropping_query():
    def consult_fn(query, **kwargs):
        if kwargs["retriever_mode"] == "graph":
            return {"retrieval_error": "缺 element_index.json", "rejected": False,
                     "insufficient": False, "results": []}
        return {"retrieval_error": None, "rejected": False, "insufficient": False,
                "results": [{"physician": "叶天士", "s3": SimpleNamespace(herbs=["党参"])}]}

    records = re.collect_retriever_mode_samples(["主诉甲"], ["dense", "graph"], consult_fn=consult_fn)
    assert len(records) == 1
    assert records[0]["n_modes_available"] == 1
    assert records[0]["unavailable_modes"] == ["graph"]


def test_retriever_mode_output_effect_uses_pairwise_jaccard_stats():
    records = [
        {"query": "q1", "physician": "叶天士", "herb_sets": [{"党参"}, {"黄芪"}],
         "n_modes_available": 2, "unavailable_modes": []},
    ]
    r = re.retriever_mode_output_effect(records, ["dense", "graph"])
    assert r["output_difference_rate"] == pytest.approx(1.0)  # 毫无重叠
    assert r["n_usable"] == 1 and r["n_total"] == 1


def test_retriever_mode_output_effect_excludes_samples_with_fewer_than_two_modes():
    records = [
        {"query": "q1", "physician": "叶天士", "herb_sets": [{"党参"}],
         "n_modes_available": 1, "unavailable_modes": ["graph"]},
    ]
    r = re.retriever_mode_output_effect(records, ["dense", "graph"])
    assert r["n_usable"] == 0
    assert r["output_difference_rate"] is None
    assert r["n_unavailable_by_mode"]["graph"] == 1


def test_retriever_mode_output_effect_carries_graph_caveat_only_when_graph_included():
    r_with_graph = re.retriever_mode_output_effect([], ["dense", "graph"])
    r_without_graph = re.retriever_mode_output_effect([], ["dense", "bm25"])
    assert r_with_graph["graph_mode_caveat"] is not None
    assert "element_index" in r_with_graph["graph_mode_caveat"]
    assert r_without_graph["graph_mode_caveat"] is None


def test_element_index_coverage_computed_from_current_file_not_hardcoded(tmp_path, monkeypatch):
    """覆盖率现算，不写死——element_index.json 会随 cases.json 重新抽取而变，
    写死一个数字下次数据一变就是假的（跟 syndromes.jsonl 17->337 那次同类
    教训）。"""
    index_path = tmp_path / "element_index.json"
    index_path.write_text(json.dumps({
        "c1": {"physician": "ye_tianshi", "elements": ["胃", "气滞"]},
        "c2": {"physician": "ye_tianshi", "elements": []},
        "c3": {"physician": "wu_jutong", "elements": ["脾"]},
    }), encoding="utf-8")
    monkeypatch.setattr("core.retrieval_graph.ELEMENT_INDEX_PATH", index_path)

    covered, total = re._element_index_coverage()
    assert (covered, total) == (2, 3)
    caveat = re._graph_mode_caveat()
    assert "2/3" in caveat and "67%" in caveat


def test_element_index_coverage_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("core.retrieval_graph.ELEMENT_INDEX_PATH", tmp_path / "no_such_file.json")
    assert re._element_index_coverage() is None
    caveat = re._graph_mode_caveat()
    assert "未知" in caveat and "build_element_index" in caveat


# ---------- E9：collect_react_process_samples / react_process_summary ----------


def _react_trace(n_steps=2, terminated_by="finish", llm_calls=3):
    return SimpleNamespace(
        steps=[object()] * n_steps, terminated_by=terminated_by, llm_calls=llm_calls,
    )


def test_collect_react_process_samples_records_steps_and_terminated_by():
    def consult_fn(query, **kwargs):
        return {
            "rejected": False, "insufficient": False,
            "results": [
                {"physician": "叶天士", "react_trace": _react_trace(3, "finish")},
                {"physician": "吴鞠通", "react_trace": _react_trace(5, "max_steps")},
            ],
        }

    records = re.collect_react_process_samples(["主诉甲"], consult_fn=consult_fn)
    assert len(records) == 2
    assert records[0] == {"query": "主诉甲", "physician": "叶天士", "n_steps": 3,
                           "terminated_by": "finish", "llm_calls": 3}
    assert records[1]["terminated_by"] == "max_steps" and records[1]["n_steps"] == 5


def test_collect_react_process_samples_skips_rejected_and_missing_traces():
    def consult_fn(query, **kwargs):
        return {"rejected": True, "insufficient": False, "results": []}

    assert re.collect_react_process_samples(["主诉甲"], consult_fn=consult_fn) == []


def test_react_process_summary_distributions():
    records = [
        {"query": "q1", "physician": "叶天士", "n_steps": 2, "terminated_by": "finish", "llm_calls": 3},
        {"query": "q1", "physician": "吴鞠通", "n_steps": 5, "terminated_by": "max_steps", "llm_calls": 6},
    ]
    r = re.react_process_summary(records)
    assert r["n_samples"] == 2
    assert r["terminated_by_distribution"] == {"finish": 1, "max_steps": 1}
    assert r["step_distribution"]["mean"] == pytest.approx(3.5)
    assert "prompt" in r["terminated_by_caveat"]


def test_react_process_summary_empty_records():
    r = re.react_process_summary([])
    assert r["n_samples"] == 0
    assert r["step_distribution"] is None
    assert r["terminated_by_caveat"]  # 空样本时也要带这条限定，不是只有有数据才提醒


# ---------- divergence_per_query_detail ----------


def test_divergence_per_query_detail_computes_net_difference_and_verdict():
    results = [
        {"query": "q1", "rejected": False, "insufficient": False,
         "divergence": {"herb_jaccard": 0.6}},
        {"query": "q2", "rejected": False, "insufficient": False,
         "divergence": {"herb_jaccard": 0.1}},
    ]
    epsilon_detail = _epsilon_detail([("q1", "叶天士", 0.3), ("q1", "吴鞠通", 0.1),
                                       ("q2", "叶天士", 0.5)])
    out = re.divergence_per_query_detail(results, epsilon_detail)
    # q1: ε = mean(0.3, 0.1) = 0.2, net = 0.6-0.2 = 0.4 > 0 -> real_divergence
    assert out[0] == {"query": "q1", "herb_jaccard": 0.6, "epsilon": 0.2,
                       "net_difference": 0.4, "verdict": "real_divergence"}
    # q2: ε = 0.5, net = 0.1-0.5 = -0.4 <= 0 -> within_noise_floor
    assert out[1] == {"query": "q2", "herb_jaccard": 0.1, "epsilon": 0.5,
                       "net_difference": -0.4, "verdict": "within_noise_floor"}


def test_divergence_per_query_detail_unusable_when_rejected():
    results = [{"query": "q1", "rejected": True, "insufficient": False, "divergence": None}]
    out = re.divergence_per_query_detail(results, None)
    assert out[0]["verdict"] == "unusable"


def test_divergence_per_query_detail_no_epsilon_data_when_query_not_found():
    results = [{"query": "q-not-in-epsilon", "rejected": False, "insufficient": False,
                "divergence": {"herb_jaccard": 0.5}}]
    out = re.divergence_per_query_detail(results, _epsilon_detail([("其他主诉", "叶天士", 0.2)]))
    assert out[0]["verdict"] == "no_epsilon_data"
    assert out[0]["herb_jaccard"] == 0.5 and out[0]["epsilon"] is None


def test_divergence_per_query_detail_missing_query_tag_does_not_guess():
    out = re.divergence_per_query_detail(
        [{"rejected": False, "insufficient": False, "divergence": {"herb_jaccard": 0.5}}], None
    )
    assert out[0]["verdict"] == "no_query_tag"
    assert out[0]["query"] is None


# ---------- build_report / render_markdown ----------


def test_build_report_shape(monkeypatch):
    monkeypatch.setattr(re, "load_epsilon_online", lambda: 0.2)
    results = [_consult_result(herb_jaccard=0.5, physician_results=[_physician_result()])]
    report = re.build_report(results)
    assert report["n_queries"] == 1
    assert report["divergence_vs_epsilon"]["available"] is True
    assert "hallucination" in report and "safety_veto" in report
    json.dumps(report)  # 必须能序列化，前端/报告直接消费


def test_render_markdown_includes_all_sections(monkeypatch):
    monkeypatch.setattr(re, "load_epsilon_online", lambda: None)
    report = re.build_report([_consult_result()])
    md = re.render_markdown(report)
    assert "分歧度" in md and "幻觉率" in md and "安全否决" in md and "检索模式" in md


# ---------- CLI ----------


def test_main_dry_run_does_not_call_consult(tmp_path, monkeypatch, capsys):
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n主诉二\n", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("--dry-run 不该真的调用")

    monkeypatch.setattr("core.chain.consult", boom)
    re.main(["--queries-path", str(queries_path), "--dry-run"])
    out = capsys.readouterr().out
    assert "预估调用数" in out


def test_main_writes_report_json_and_md(tmp_path, monkeypatch):
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n", encoding="utf-8")
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"

    fake_result = _consult_result(herb_jaccard=0.3, physician_results=[_physician_result()])
    monkeypatch.setattr("core.chain.consult", lambda q: fake_result)
    monkeypatch.setattr(re, "load_epsilon_online", lambda: 0.1)

    re.main(["--queries-path", str(queries_path), "--out-json", str(out_json), "--out-md", str(out_md)])

    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["n_queries"] == 1
    assert out_md.exists()


def test_main_runs_e3_e4_e8_e9_end_to_end(tmp_path, monkeypatch):
    """接线测试：--e3/--e4/--e8/--e9 真的把 main() 的参数传对了地方，不是
    只有各自的收集器/度量函数单测通过——单测传的是手写好的 pairs/records，
    测不出 main() 里参数名拼错、忘记传 consult_fn 之类的接线错误。"""
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n", encoding="utf-8")
    out_json = tmp_path / "report.json"
    out_md = tmp_path / "report.md"

    def fake_consult(complaint, **kwargs):
        herbs = ["黄芪"] if kwargs.get("refs_mode") == "swapped" else ["党参"]
        trace = _react_trace(2, "finish") if kwargs.get("use_react") else None
        return {
            "rejected": False, "insufficient": False, "retrieval_error": None,
            "divergence": {"herb_jaccard": 0.4},
            "results": [{
                "physician": "叶天士", "s3": SimpleNamespace(herbs=herbs),
                "hallucinated": [], "no_reference_cases": False, "react_trace": trace,
            }],
            "manifest": {"llm_calls": 6},
        }

    monkeypatch.setattr("core.chain.consult", fake_consult)
    monkeypatch.setattr(re, "load_epsilon_online", lambda: 0.1)
    monkeypatch.setattr(re, "load_epsilon_online_detail", lambda: None)

    re.main(["--queries-path", str(queries_path), "--out-json", str(out_json),
             "--out-md", str(out_md), "--e3", "--e4", "--e8", "--e9"])

    data = json.loads(out_json.read_text(encoding="utf-8"))
    labels = {a["label"] for a in data["ablations"]}
    assert labels == {"swapped", "none", "react_on"}
    assert data["retriever_mode_effect"] is not None
    assert data["retriever_mode_effect"]["n_total"] > 0
    assert data["react_process"] is not None
    assert data["react_process"]["n_samples"] > 0
    md = out_md.read_text(encoding="utf-8")
    assert "E8" in md or "检索模式消融" in md
    assert "ReAct 过程统计" in md
