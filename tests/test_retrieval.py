"""core/retrieval.py 的离线测试：只测不需要下载 embedding 模型的部分。"""
from pathlib import Path

import pytest

from core.retrieval import DenseRetriever, _case_to_text
from core.schemas import CaseRecord


def test_dense_retriever_raises_clear_error_when_cases_json_missing(tmp_path):
    missing_path = tmp_path / "cases.json"
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        DenseRetriever(cases_path=missing_path)


def test_case_to_text_includes_symptoms_and_tongue_pulse():
    case = CaseRecord(
        case_id="ye_tianshi-001",
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
        physician="ye_tianshi",
        raw="原文",
        symptoms=["纳差"],
    )
    text = _case_to_text(case)
    assert "未记" in text
