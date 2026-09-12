"""core/retrieval.py 的离线测试：只测不需要下载 embedding 模型的部分。"""

import json

import pytest

from core.retrieval import (
    ADAPTIVE_MIN_SCORE_FLOOR,
    CASE_TO_TEXT_EXCERPT_CHARS,
    LOW_DISCRIMINATION_MARGIN,
    DenseRetriever,
    Retriever,
    _case_to_text,
    _percentile,
    adaptive_min_score,
    apply_low_discrimination_cutoff,
    low_discrimination_cutoff_enabled,
)
from core.schemas import CaseRecord


def _case(**overrides):
    base = dict(
        case_id="ye_tianshi-001", case_group_id="ye_tianshi-001",
        physician="ye_tianshi", raw="原文",
    )
    base.update(overrides)
    return CaseRecord(**base)


def test_dense_retriever_raises_clear_error_when_cases_json_missing(tmp_path):
    missing_path = tmp_path / "cases.json"
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        DenseRetriever(cases_path=missing_path)


def test_case_to_text_includes_symptoms_and_tongue_pulse():
    case = CaseRecord(
        case_id="ye_tianshi-001",
        case_group_id="ye_tianshi-001",
        physician="ye_tianshi",
        raw="原文",
        symptoms=["纳差", "乏力"],
        tongue="淡红",
        pulse="细弱",
    )
    text = _case_to_text(case)
    assert "纳差" in text
    assert "乏力" in text
    assert "舌淡红" in text
    assert "脉细弱" in text


def test_case_to_text_handles_missing_tongue_pulse():
    case = CaseRecord(
        case_id="ye_tianshi-002",
        case_group_id="ye_tianshi-002",
        physician="ye_tianshi",
        raw="原文",
        symptoms=["纳差"],
    )
    text = _case_to_text(case)
    assert "未记" in text


# ---------- P0-6：_case_to_text 纳入 raw_excerpt ----------


def test_case_to_text_appends_raw_excerpt_when_symptoms_present():
    case = _case(symptoms=["纳差"], raw_excerpt="患者形瘦神疲，纳谷不香。")
    text = _case_to_text(case)
    assert "纳差" in text  # 结构化字段仍然在
    assert "患者形瘦神疲" in text  # 原文摘录也要进来，不是二选一


def test_case_to_text_no_symptoms_uses_only_raw_excerpt():
    """约一半复诊记录只写"加减了什么药"，没有症状描述——这类医案不该编码出
    "（无记录症状）。舌未记，脉未记"这种占位文字（P0-6 根因：这种占位文字
    让所有这类医案的向量几乎完全相同，变成检索噪声）。"""
    case = _case(symptoms=[], raw_excerpt="六两，加∶葶苈(一钱五分) 二帖。")
    text = _case_to_text(case)
    assert text == "六两，加∶葶苈(一钱五分) 二帖。"
    assert "无记录症状" not in text
    assert "未记" not in text


def test_case_to_text_no_symptoms_no_raw_excerpt_returns_none():
    """两者都没有：这条医案编码不出任何有意义的文本，调用方（DenseRetriever.
    __init__）该跳过它、不建索引，不是硬凑一条占位文本。"""
    case = _case(symptoms=[], raw_excerpt=None)
    assert _case_to_text(case) is None


def test_case_to_text_no_symptoms_empty_raw_excerpt_returns_none():
    case = _case(symptoms=[], raw_excerpt="")
    assert _case_to_text(case) is None


def test_case_to_text_truncates_raw_excerpt_to_configured_length():
    case = _case(symptoms=["纳差"], raw_excerpt="甲" * 500)
    text = _case_to_text(case)
    assert "甲" * CASE_TO_TEXT_EXCERPT_CHARS in text
    assert "甲" * (CASE_TO_TEXT_EXCERPT_CHARS + 1) not in text


def test_case_to_text_no_symptoms_raw_excerpt_also_truncated():
    case = _case(symptoms=[], raw_excerpt="乙" * 500)
    text = _case_to_text(case)
    assert text == "乙" * CASE_TO_TEXT_EXCERPT_CHARS


def test_case_to_text_visit_framing_still_applies_when_symptoms_present():
    """P0-6 只改了"symptoms 为空时不再拼占位文字"这一条，有 symptoms 时
    复诊框架（跟初诊区分开）必须保持不变。"""
    case = _case(symptoms=["肿胀未除"], visit_index=1, response_to_prior="肿势稍减")
    text = _case_to_text(case)
    assert "复诊第2诊" in text
    assert "肿势稍减" in text


# ---------- P0-6：DenseRetriever 跳过无内容医案并计数 ----------


def _write_cases_json(tmp_path, dicts):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(dicts), encoding="utf-8")
    return path


def test_dense_retriever_skips_cases_with_no_encodable_content(tmp_path, capsys):
    cases = [
        _case(case_id="a", symptoms=["纳差"]).model_dump(),
        _case(case_id="b", symptoms=[], raw_excerpt=None).model_dump(),
        _case(case_id="c", symptoms=[], raw_excerpt="加葶苈子二帖").model_dump(),
    ]
    path = _write_cases_json(tmp_path, cases)
    retriever = DenseRetriever(cases_path=path)

    assert [c.case_id for c in retriever._cases] == ["a", "c"]
    assert retriever.skipped_no_content_ids == ["b"]
    assert len(retriever._case_texts) == len(retriever._cases)  # 下标必须对齐


def test_dense_retriever_prints_skip_summary_to_stderr(tmp_path, capsys):
    cases = [_case(case_id="b", symptoms=[], raw_excerpt=None).model_dump()]
    path = _write_cases_json(tmp_path, cases)
    DenseRetriever(cases_path=path)
    err = capsys.readouterr().err
    assert "1 条医案" in err
    assert "b" in err


def test_dense_retriever_no_skips_means_no_stderr_output(tmp_path, capsys):
    cases = [_case(case_id="a", symptoms=["纳差"]).model_dump()]
    path = _write_cases_json(tmp_path, cases)
    DenseRetriever(cases_path=path)
    assert capsys.readouterr().err == ""


# ---------- P0-7：adaptive_min_score ----------


class _ScoreListRetriever(Retriever):
    """探测调用返回固定的一组分数，不管 query/physician 是什么——只用来测
    adaptive_min_score 怎么把 search() 的返回值转成一个阈值,不测检索本身。"""

    def __init__(self, scores):
        self._scores = scores
        self.calls: list[dict] = []

    def search(self, query, physician, k=3, min_score=0.0, **kwargs):
        self.calls.append({"query": query, "physician": physician, "k": k,
                            "min_score": min_score, **kwargs})
        fake_case = _case(case_id="x")
        return [(fake_case, s) for s in self._scores[:k]]


def test_adaptive_min_score_empty_probe_returns_floor():
    r = _ScoreListRetriever([])
    assert adaptive_min_score(r, "q", "ye_tianshi", mode="dense") == ADAPTIVE_MIN_SCORE_FLOOR


def test_adaptive_min_score_uses_p25_of_probe_when_above_floor():
    # 10 个分数全在 0.65-0.98 之间（医案多、覆盖广的医家该有的分布），
    # p25（线性插值）算出来是 0.7625，明显高于 ADAPTIVE_MIN_SCORE_FLOOR=0.60
    scores = [0.65, 0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.98]
    r = _ScoreListRetriever(scores)
    result = adaptive_min_score(r, "q", "ye_tianshi", mode="dense")
    assert result == pytest.approx(_percentile(sorted(scores), 25))
    assert result > ADAPTIVE_MIN_SCORE_FLOOR


def test_adaptive_min_score_never_goes_below_floor():
    """医案很少/覆盖窄的医家：探测到的分数普遍很低，p25 也很低，
    仍然不能低于 ADAPTIVE_MIN_SCORE_FLOOR——否则退化成"什么都收"。"""
    r = _ScoreListRetriever([0.05, 0.08, 0.1])
    assert adaptive_min_score(r, "q", "ye_tianshi", mode="dense") == ADAPTIVE_MIN_SCORE_FLOOR


def test_adaptive_min_score_probes_with_min_score_zero_and_k_ten():
    r = _ScoreListRetriever([0.9] * 10)
    adaptive_min_score(r, "主诉", "ye_tianshi", mode="dense")
    assert len(r.calls) == 1
    assert r.calls[0]["min_score"] == 0.0
    assert r.calls[0]["k"] == 10


def test_adaptive_min_score_forwards_mode_dense_to_the_probe_call():
    """探测调用要跟真正查询用同一个 mode，否则探测出来的相似度分布跟真正
    检索用的不是同一路信号，算出来的阈值没有意义。"""
    r = _ScoreListRetriever([0.9] * 10)
    adaptive_min_score(r, "主诉", "ye_tianshi", mode="dense")
    assert r.calls[0]["mode"] == "dense"


def test_adaptive_min_score_passes_no_extra_kwargs_beyond_mode():
    """探测调用不该多带 mode 以外的任何关键字——保持跟 core/chain.py::
    _search_cases「不传 mode 就不加这个关键字」同一条规则，只是这里
    mode 本身必须传（下面几条 P0-13 测试断言的就是"不传/传别的值时
    根本不会走到这次调用"）。"""
    r = _ScoreListRetriever([0.9] * 10)
    adaptive_min_score(r, "主诉", "ye_tianshi", mode="dense")
    assert set(r.calls[0]) == {"query", "physician", "k", "min_score", "mode"}


# ---------- P0-13 改动 2：非 dense 模式下探测是纯浪费，必须跳过 ----------


def test_adaptive_min_score_skips_probe_when_mode_is_absent():
    """不传 mode 时（会在 HybridRetriever.search() 里解析成 hybrid）——
    P0-13 改动 1 之后 hybrid 分支完全不读 min_score，探测出来的值没有
    任何下游用途，不该发起这次完整的 search() 调用。"""
    r = _ScoreListRetriever([0.9] * 10)
    result = adaptive_min_score(r, "主诉", "ye_tianshi")
    assert result == 0.0
    assert r.calls == []


@pytest.mark.parametrize("mode", ["bm25", "graph", "hybrid"])
def test_adaptive_min_score_skips_probe_for_non_dense_modes(mode):
    r = _ScoreListRetriever([0.9] * 10)
    result = adaptive_min_score(r, "主诉", "ye_tianshi", mode=mode)
    assert result == 0.0
    assert r.calls == []


def test_percentile_single_value_returns_it():
    assert _percentile([0.5], 25) == 0.5


def test_percentile_matches_hand_computed_linear_interpolation():
    # rank = 0.25 * 3 = 0.75 -> 在下标 0 和 1 之间插值 75%
    assert _percentile([0.0, 4.0, 8.0, 12.0], 25) == pytest.approx(3.0)


# ---------- P0-11：herbs 为空的医案排序降权，不剔除 ----------


def _retriever(tmp_path):
    """_rank_score 不碰 _model/_embeddings，随便一条医案就能建实例。"""
    path = _write_cases_json(tmp_path, [_case(case_id="seed", symptoms=["纳差"]).model_dump()])
    return DenseRetriever(cases_path=path)


def test_rank_score_penalizes_cases_with_no_herbs(tmp_path):
    retriever = _retriever(tmp_path)
    with_herbs = _case(herbs=["党参"], visit_index=1)  # visit_index=1：不叠加初诊加成，隔离变量
    without_herbs = _case(herbs=[], visit_index=1)
    assert retriever._rank_score(without_herbs, 0.8) == pytest.approx(0.8 * 0.9)
    assert retriever._rank_score(with_herbs, 0.8) == pytest.approx(0.8)


def test_rank_score_no_herbs_penalty_does_not_exclude_the_case():
    """P0-11 的要求是降权不剔除——这里只是确认惩罚后的分数仍然是正数、
    不是被打成 0 或负数变相等于剔除。"""
    retriever_score = DenseRetriever.NO_HERBS_PENALTY
    assert 0 < retriever_score < 1


def test_rank_score_combines_initial_visit_boost_and_no_herbs_penalty(tmp_path):
    """初诊加成和无方剂惩罚是两个独立的调整项，同一条医案可能同时命中——
    没方药又是初诊的医案：两个系数要连乘，不是互斥的 if/elif。"""
    retriever = _retriever(tmp_path)
    case = _case(herbs=[], visit_index=0)  # 初诊 + 无方
    expected = 0.8 * DenseRetriever.INITIAL_VISIT_BOOST * DenseRetriever.NO_HERBS_PENALTY
    assert retriever._rank_score(case, 0.8) == pytest.approx(expected)


# ---------- P0-12：candidates 之间没有真实区分度时只留 top-1 ----------


def _hits(*scores):
    return [(_case(case_id=f"c{i}"), s) for i, s in enumerate(scores)]


def test_low_discrimination_cutoff_enabled_default_on(monkeypatch):
    monkeypatch.delenv("LOW_DISCRIMINATION_CUTOFF", raising=False)
    assert low_discrimination_cutoff_enabled() is True


def test_low_discrimination_cutoff_enabled_reads_env_var(monkeypatch):
    monkeypatch.setenv("LOW_DISCRIMINATION_CUTOFF", "0")
    assert low_discrimination_cutoff_enabled() is False
    monkeypatch.setenv("LOW_DISCRIMINATION_CUTOFF", "1")
    assert low_discrimination_cutoff_enabled() is True


def test_apply_cutoff_truncates_to_top1_when_scores_are_close():
    hits = _hits(0.80, 0.79, 0.78)  # 分差 0.02 < LOW_DISCRIMINATION_MARGIN=0.03
    result, triggered = apply_low_discrimination_cutoff(hits, mode="dense", enabled=True)
    assert triggered is True
    assert len(result) == 1
    assert result[0][1] == 0.80


def test_apply_cutoff_keeps_all_hits_when_scores_have_real_spread():
    hits = _hits(0.90, 0.70, 0.60)  # 分差 0.30，明显有区分度
    result, triggered = apply_low_discrimination_cutoff(hits, mode="dense", enabled=True)
    assert triggered is False
    assert result == hits


def test_apply_cutoff_boundary_exactly_at_margin_is_not_low_discrimination():
    """分差正好等于 LOW_DISCRIMINATION_MARGIN 时不算"没有区分度"——判据是
    "小于"margin，不是"小于等于"，跟 min_score 用 >= 的方向一致（差多少
    才算够，边界值算"够"而不是"不够"）。"""
    hits = _hits(0.80, 0.80 - LOW_DISCRIMINATION_MARGIN)
    result, triggered = apply_low_discrimination_cutoff(hits, mode="dense", enabled=True)
    assert triggered is False
    assert result == hits


def test_apply_cutoff_single_hit_never_triggers():
    hits = _hits(0.80)
    result, triggered = apply_low_discrimination_cutoff(hits, mode="dense", enabled=True)
    assert triggered is False
    assert result == hits


def test_apply_cutoff_empty_hits_never_triggers():
    result, triggered = apply_low_discrimination_cutoff([], mode="dense", enabled=True)
    assert triggered is False
    assert result == []


def test_apply_cutoff_disabled_returns_hits_unchanged_even_when_close():
    hits = _hits(0.80, 0.79, 0.78)
    result, triggered = apply_low_discrimination_cutoff(hits, mode="dense", enabled=False)
    assert triggered is False
    assert result == hits


def test_apply_cutoff_enabled_none_reads_environment(monkeypatch):
    """enabled=None（默认）时才读环境变量——显式传参优先，跟 retriever_mode/
    refs_mode 同一条规则。"""
    hits = _hits(0.80, 0.79, 0.78)
    monkeypatch.setenv("LOW_DISCRIMINATION_CUTOFF", "0")
    result, triggered = apply_low_discrimination_cutoff(hits, mode="dense")
    assert triggered is False
    assert result == hits


def test_apply_cutoff_graph_mode_also_valid():
    """graph 模式的展示分（真实 Jaccard 相似度）就是排序依据本身，跟 dense
    同理——这条判据对它也成立。"""
    hits = _hits(0.80, 0.79, 0.78)
    result, triggered = apply_low_discrimination_cutoff(hits, mode="graph", enabled=True)
    assert triggered is True
    assert len(result) == 1


# ---------- P0-13 改动 3：hybrid/bm25 模式下这条判据不成立 ----------


@pytest.mark.parametrize("mode", [None, "hybrid", "bm25"])
def test_apply_cutoff_does_not_apply_to_hybrid_or_bm25_or_default(mode):
    """P0-13 契约变更：hybrid（含缺省，会解析成 hybrid）模式下展示分是
    dense 相似度，但排序依据是 RRF 融合分，两者脱钩——比较展示分差值
    判断"有没有区分度"是在比较错误的维度：一条 BM25 精确命中、dense 分
    很低的医案可能排在很靠前的融合名次，却可能因为跟另一条同样低 dense
    分的医案凑巧分差很小而被误砍。bm25 模式的展示分是无界原始分，跟这个
    按 dense 余弦相似度校准的 0.03 阈值不是一个刻度，同样不成立。这两种
    情况即使分差很小也不该触发截断。"""
    hits = _hits(0.80, 0.79, 0.78)  # 分差 0.02，若判据成立会触发
    result, triggered = apply_low_discrimination_cutoff(hits, mode=mode, enabled=True)
    assert triggered is False
    assert result == hits


def test_apply_cutoff_hybrid_does_not_cut_bm25_found_low_dense_item():
    """P0-13 自查清单第 5 条要求的那条测试：hybrid 模式下，三条候选彼此的
    dense 分都很低、很接近（0.02 < margin），但它们在真正的排序依据（RRF
    融合分）上可能分得很开——dense 分凑巧聚在一起不代表"排序没有区分度"，
    只代表它们都不是 dense 那一路的强项（真实排名是靠 BM25/graph 分出来的）。

    **这条测试第一版是空验证**（独立审计发现的）：原来用 (0.85, 0.55, 0.53)，
    top-1 和 bottom 的分差是 0.32，早就超过 margin=0.03，不管 mode 参数
    是什么、甚至不管有没有 P0-13 这条 mode 限制，triggered 恒为 False——
    测的根本不是 mode 限制在起作用，去掉限制这条测试照样绿。这一版换成
    分差本身就 < margin 的三条（0.55/0.54/0.53，分差 0.02），这样如果
    mode="hybrid" 时这条限制不生效（退回旧的、不分 mode 的判据），
    triggered 会变成 True、只剩 1 条——用这个可验证的差异证明限制确实
    在起作用，不是巧合地绿。"""
    hits = _hits(0.55, 0.54, 0.53)  # 分差 0.02 < LOW_DISCRIMINATION_MARGIN=0.03
    result, triggered = apply_low_discrimination_cutoff(hits, mode="hybrid", enabled=True)
    assert triggered is False
    assert result == hits
    assert len(result) == 3

    # 对照：同样的分数在 dense 模式下（这条判据成立的场景）应该真的触发——
    # 证明上面 hybrid 的"不触发"不是因为这组分数本身永远不触发，
    # 而是 mode="hybrid" 这个限制真的挡住了它。
    contrast_result, contrast_triggered = apply_low_discrimination_cutoff(
        hits, mode="dense", enabled=True
    )
    assert contrast_triggered is True
    assert len(contrast_result) == 1


