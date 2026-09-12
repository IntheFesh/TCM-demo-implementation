"""core/retrieval.py 的离线测试：只测不需要下载 embedding 模型的部分。"""

import json

import pytest

from core.retrieval import (
    ADAPTIVE_MIN_SCORE_FLOOR,
    CASE_TO_TEXT_EXCERPT_CHARS,
    DenseRetriever,
    Retriever,
    _case_to_text,
    _percentile,
    adaptive_min_score,
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
    assert adaptive_min_score(r, "q", "ye_tianshi") == ADAPTIVE_MIN_SCORE_FLOOR


def test_adaptive_min_score_uses_p25_of_probe_when_above_floor():
    # 10 个分数全在 0.65-0.98 之间（医案多、覆盖广的医家该有的分布），
    # p25（线性插值）算出来是 0.7625，明显高于 ADAPTIVE_MIN_SCORE_FLOOR=0.60
    scores = [0.65, 0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.95, 0.98]
    r = _ScoreListRetriever(scores)
    result = adaptive_min_score(r, "q", "ye_tianshi")
    assert result == pytest.approx(_percentile(sorted(scores), 25))
    assert result > ADAPTIVE_MIN_SCORE_FLOOR


def test_adaptive_min_score_never_goes_below_floor():
    """医案很少/覆盖窄的医家：探测到的分数普遍很低，p25 也很低，
    仍然不能低于 ADAPTIVE_MIN_SCORE_FLOOR——否则退化成"什么都收"。"""
    r = _ScoreListRetriever([0.05, 0.08, 0.1])
    assert adaptive_min_score(r, "q", "ye_tianshi") == ADAPTIVE_MIN_SCORE_FLOOR


def test_adaptive_min_score_probes_with_min_score_zero_and_k_ten():
    r = _ScoreListRetriever([0.9] * 10)
    adaptive_min_score(r, "主诉", "ye_tianshi")
    assert len(r.calls) == 1
    assert r.calls[0]["min_score"] == 0.0
    assert r.calls[0]["k"] == 10


def test_adaptive_min_score_forwards_search_kwargs_to_probe():
    """探测调用要跟真正查询用同一份 mode/query_elements，否则探测出来的
    相似度分布跟真正检索用的不是同一路信号，算出来的阈值没有意义。"""
    r = _ScoreListRetriever([0.9] * 10)
    adaptive_min_score(r, "主诉", "ye_tianshi", mode="graph", query_elements=["脾"])
    assert r.calls[0]["mode"] == "graph"
    assert r.calls[0]["query_elements"] == ["脾"]


def test_adaptive_min_score_passes_no_extra_kwargs_when_none_given():
    """不传 search_kwargs 时探测调用也不该多带任何关键字——保持跟
    core/chain.py::_search_cases「不传 mode 就不加这个关键字」同一条规则。"""
    r = _ScoreListRetriever([0.9] * 10)
    adaptive_min_score(r, "主诉", "ye_tianshi")
    assert set(r.calls[0]) == {"query", "physician", "k", "min_score"}


def test_percentile_single_value_returns_it():
    assert _percentile([0.5], 25) == 0.5


def test_percentile_matches_hand_computed_linear_interpolation():
    # rank = 0.25 * 3 = 0.75 -> 在下标 0 和 1 之间插值 75%
    assert _percentile([0.0, 4.0, 8.0, 12.0], 25) == pytest.approx(3.0)
