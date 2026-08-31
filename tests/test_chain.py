"""core/chain.py 的离线测试：用假 LLM 后端和假检索器，不需要网络。"""
from core import chain
from core.retrieval import Retriever
from core.schemas import CaseRecord, ElementHit, S1Normalize, S2Elements, S3Syndrome


class FakeLLM:
    """按 schema 类型返回预设响应，同时记录调用次数方便断言 S1 只跑一次。"""

    def __init__(self, s3_by_physician: dict[str, S3Syndrome]):
        self.s3_by_physician = s3_by_physician
        self.calls: list[str] = []
        self._current_physician: str | None = None

    def generate(self, system: str, user: str, schema, temperature: float = 0.0, **kwargs):
        self.calls.append(schema.__name__)
        if schema is S1Normalize:
            return S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
        if schema is S2Elements:
            return S2Elements(
                elements=[
                    ElementHit(
                        element="脾",
                        kind="location",
                        supporting_symptoms=["纳差"],
                        confidence="high",
                    )
                ],
                unexplained_symptoms=[],
            )
        if schema is S3Syndrome:
            # 依赖 system 提示词里包含医家姓名来区分两位医家的返回值
            for physician_name, s3 in self.s3_by_physician.items():
                if physician_name in system:
                    return s3
            raise AssertionError("无法从 system 提示词判断当前医家")
        raise AssertionError(f"未预期的 schema: {schema}")


class FakeRetriever(Retriever):
    def __init__(self, cases: list[CaseRecord]):
        self.cases = cases

    def search(self, query: str, physician: str, k: int = 3):
        hits = [c for c in self.cases if c.physician == physician][:k]
        return [(c, 0.9) for c in hits]


def _fake_cases() -> list[CaseRecord]:
    return [
        CaseRecord(
            case_id="ye_tianshi-001",
            physician="ye_tianshi",
            raw="原文",
            symptoms=["纳差"],
            tongue="淡红",
            pulse="细弱",
            syndrome="脾胃气虚",
            herbs=["党参", "白术"],
        ),
        CaseRecord(
            case_id="wu_jutong-001",
            physician="wu_jutong",
            raw="原文",
            symptoms=["纳差"],
            tongue="淡红",
            pulse="细弱",
            syndrome="脾胃气虚",
            herbs=["党参", "白术"],
        ),
    ]


def test_s1_runs_exactly_once_for_two_physicians(monkeypatch):
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力，脉细弱",
        treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-001"],
    )
    s3_wu = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力，脉细弱",
        treatment_principle="健脾益气",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert fake_llm.calls.count("S1Normalize") == 1
    assert fake_llm.calls.count("S2Elements") == 2
    assert fake_llm.calls.count("S3Syndrome") == 2
    assert len(outcome["results"]) == 2


def test_divergence_true_when_syndromes_differ(monkeypatch):
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="...",
        treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-001"],
    )
    s3_wu = S3Syndrome(
        syndrome="湿热中阻",
        reasoning="...",
        treatment_principle="清热化湿",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert outcome["divergence"]["same"] is False
    assert outcome["divergence"]["method"] == "exact_string_match"


def test_hallucination_detected_when_cited_id_not_in_refs(monkeypatch):
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="...",
        treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-999"],  # 不在检索结果里
    )
    s3_wu = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="...",
        treatment_principle="健脾益气",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    ye_result = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")
    wu_result = next(r for r in outcome["results"] if r["physician"] == "wu_jutong")
    assert ye_result["hallucinated"] == ["ye_tianshi-999"]
    assert wu_result["hallucinated"] == []
