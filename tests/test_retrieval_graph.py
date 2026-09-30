"""core/retrieval_graph.py 的离线测试：纯本地 JSON + Jaccard 计算，不需要网络。"""
import json

import pytest

from core.retrieval_graph import ElementRetriever


def _write_index(tmp_path, index: dict):
    path = tmp_path / "element_index.json"
    path.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return path


def test_raises_clear_error_when_index_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_element_index"):
        ElementRetriever(index_path=tmp_path / "nope.json")


def test_ranking_prefers_more_overlap(tmp_path):
    index = {
        "a": {"physician": "ye_tianshi", "elements": ["脾", "气虚"]},
        "b": {"physician": "ye_tianshi", "elements": ["脾", "气虚", "湿"]},
        "c": {"physician": "ye_tianshi", "elements": ["肝", "血瘀"]},
    }
    path = _write_index(tmp_path, index)
    retriever = ElementRetriever(index_path=path)

    ranking = retriever.ranking(["脾", "气虚", "湿"], ["a", "b", "c"])
    assert [cid for cid, _ in ranking] == ["b", "a"]  # c 无共同证素，不返回


def test_ranking_score_is_real_jaccard_similarity(tmp_path):
    index = {"a": {"physician": "ye_tianshi", "elements": ["脾", "气虚"]}}
    path = _write_index(tmp_path, index)
    retriever = ElementRetriever(index_path=path)

    ranking = retriever.ranking(["脾", "湿"], ["a"])
    # 交集 {脾} 并集 {脾,气虚,湿} -> 1/3
    assert ranking[0] == ("a", pytest.approx(1 / 3))


def test_ranking_skips_case_not_in_index(tmp_path):
    path = _write_index(tmp_path, {"a": {"physician": "ye_tianshi", "elements": ["脾"]}})
    retriever = ElementRetriever(index_path=path)
    ranking = retriever.ranking(["脾"], ["a", "unknown_case"])
    assert [cid for cid, _ in ranking] == ["a"]


def test_ranking_skips_case_with_no_elements(tmp_path):
    path = _write_index(tmp_path, {
        "a": {"physician": "ye_tianshi", "elements": []},
        "b": {"physician": "ye_tianshi", "elements": ["脾"]},
    })
    retriever = ElementRetriever(index_path=path)
    ranking = retriever.ranking(["脾"], ["a", "b"])
    assert [cid for cid, _ in ranking] == ["b"]


def test_ranking_no_overlap_returns_empty(tmp_path):
    path = _write_index(tmp_path, {"a": {"physician": "ye_tianshi", "elements": ["肝", "血瘀"]}})
    retriever = ElementRetriever(index_path=path)
    assert retriever.ranking(["脾", "气虚"], ["a"]) == []


def test_ranking_empty_case_ids_returns_empty(tmp_path):
    path = _write_index(tmp_path, {"a": {"physician": "ye_tianshi", "elements": ["脾"]}})
    retriever = ElementRetriever(index_path=path)
    assert retriever.ranking(["脾"], []) == []
