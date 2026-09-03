"""api/main.py 的离线测试：mock core.chain.consult，不需要网络。"""
from fastapi.testclient import TestClient

import api.main as api_main
from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome


def _fake_outcome() -> dict:
    s1 = S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(
        elements=[
            ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")
        ]
    )
    s3 = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力",
        treatment_principle="健脾益气",
        herbs=["党参", "白术"],
        cited_case_ids=["ye_tianshi-001"],
    )
    results = [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": s2,
            "s3": s3,
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        }
    ]
    return {
        "s1": s1,
        "results": results,
        "divergence": {"same": True, "method": "exact_string_match"},
        "rejected": False,
        "reject_reason": None,
    }


def _fake_rejected_outcome() -> dict:
    s1 = S1Normalize(symptoms=["解黑色柏油样便"], tongue="淡", pulse="细数", unmapped=[])
    return {
        "s1": s1,
        "results": [],
        "divergence": None,
        "rejected": True,
        "reject_reason": "检测到危重症状信号（柏油样便），本 demo 不适用于此类情况，请立即就医。",
    }


def test_health():
    client = TestClient(api_main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_root_redirects_to_app():
    client = TestClient(api_main.app, follow_redirects=False)
    resp = client.get("/")
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/app/index.html"


def test_consult_endpoint_returns_graph_with_valid_edges(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint: _fake_outcome())
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "纳差乏力"})
    assert resp.status_code == 200
    body = resp.json()
    assert "graph" in body
    node_ids = {n["data"]["id"] for n in body["graph"]["nodes"]}
    for e in body["graph"]["edges"]:
        assert e["data"]["source"] in node_ids
        assert e["data"]["target"] in node_ids
    assert body["rejected"] is False


def test_consult_endpoint_returns_rejection_without_calling_to_graph(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint: _fake_rejected_outcome())
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "近日解黑色柏油样便"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected"] is True
    assert "柏油样便" in body["reject_reason"]
    assert body["results"] == []
    assert body["divergence"] is None
    assert body["graph"] == {"nodes": [], "edges": []}
