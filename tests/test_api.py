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
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _fake_outcome())
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
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _fake_rejected_outcome())
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "近日解黑色柏油样便"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected"] is True
    assert "柏油样便" in body["reject_reason"]
    assert body["results"] == []
    assert body["divergence"] is None
    assert body["graph"] == {"nodes": [], "edges": [], "dropped_edges": 0}


def test_serialize_result_passes_react_trace_through():
    """开了 ReAct 只把结论传给前端等于白跑——取证轨迹要如实带出去。"""
    from api.main import _serialize_result
    from core.schemas import ReActStepRecord, ReActTrace, S2Elements, S3Syndrome

    trace = ReActTrace(
        steps=[ReActStepRecord(step=1, thought="查一下", action="query_graph",
                               action_input={"node": "纳呆"}, observation="{}")],
        terminated_by="finish", llm_calls=1,
    )
    base = {
        "physician": "ye_tianshi", "physician_name": "叶天士",
        "s2": S2Elements(), "s3": S3Syndrome(
            syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
            cited_case_ids=["ye_tianshi-001"]),
        "refs": [], "hallucinated": [], "safety_output": None,
    }
    assert _serialize_result({**base, "react_trace": None})["react_trace"] is None
    out = _serialize_result({**base, "react_trace": trace})["react_trace"]
    assert out["terminated_by"] == "finish"
    assert out["steps"][0]["action"] == "query_graph"


def test_serialize_followup_roundtrip():
    from api.main import _serialize_followup
    from core.schemas import FollowupResult, HistoryItem

    assert _serialize_followup(None) is None
    r = FollowupResult(
        history=[HistoryItem(question="有没有口苦？", answer="没有",
                             symptom="口干或口苦", denied=["口干或口苦"])],
        denied=["口干或口苦"], rounds=1, stopped_by="max_rounds",
    )
    out = _serialize_followup(r)
    assert out["stopped_by"] == "max_rounds"
    assert out["history"][0]["denied"] == ["口干或口苦"]


def test_serialize_result_fills_cited_case_ids_for_unreferenced_s3():
    from api.main import _serialize_result
    from core.schemas import S2Elements, S3SyndromeUnreferenced

    out = _serialize_result({
        "physician": "ye_tianshi", "physician_name": "叶天士", "s2": S2Elements(),
        "s3": S3SyndromeUnreferenced(syndrome="x", reasoning="x", treatment_principle="x"),
        "refs": [], "hallucinated": [], "safety_output": None, "no_reference_cases": True,
    })
    assert out["s3"]["cited_case_ids"] == []
    assert out["no_reference_cases"] is True


def test_to_graph_emits_shared_symptom_element_edges_once():
    """S2 全局共享，症状->证素的边只该发一遍，不按医家重复。"""
    from api.main import to_graph
    from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome

    s2 = S2Elements(elements=[ElementHit(element="脾", kind="location",
                                         supporting_symptoms=["纳差"], confidence="high")])
    results = [
        {"physician": p, "physician_name": n, "s2": s2,
         "s3": S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                          herbs=["党参"], cited_case_ids=[f"{p}-001"]),
         "refs": [], "hallucinated": []}
        for p, n in [("ye_tianshi", "叶天士"), ("wu_jutong", "吴鞠通")]
    ]
    graph = to_graph(S1Normalize(symptoms=["纳差"]), results, s2)
    sym_elem = [e for e in graph["edges"] if e["data"]["source"] == "sym::纳差" and e["data"]["target"] == "elem::脾"]
    assert len(sym_elem) == 1


def test_to_graph_residual_element_does_not_hijack_a_main_element():
    """主路径与残差路径同名的证素：先加主证素，否则 add_node 先到先得会把它
    整个标成 residual=True（虚线样式）。"""
    from api.main import to_graph
    from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome

    s2 = S2Elements(elements=[ElementHit(element="脾", kind="location",
                                         supporting_symptoms=["纳差"], confidence="high")])
    residual = {"newly_explained": ["乏力"], "s2": S2Elements(elements=[ElementHit(
        element="脾", kind="location", supporting_symptoms=["乏力"], confidence="low")])}
    results = [{"physician": "ye_tianshi", "physician_name": "叶天士", "s2": s2,
                "s3": S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                                 cited_case_ids=["ye_tianshi-001"]),
                "refs": [], "hallucinated": []}]
    graph = to_graph(S1Normalize(symptoms=["纳差", "乏力"]), results, s2, residual)
    node = next(n["data"] for n in graph["nodes"] if n["data"]["id"] == "elem::脾")
    assert not node.get("residual")
    states = {n["data"]["id"]: n["data"].get("state") for n in graph["nodes"] if n["data"]["layer"] == 0}
    assert states == {"sym::纳差": "explained", "sym::乏力": "residual"}


def test_api_consult_insufficient_path(monkeypatch):
    """chain 的「信息不足」分支要原样到达前端，之前这条路径没有任何测试。"""
    import api.main as api_mod
    from core.schemas import S1Normalize, S2Elements
    from fastapi.testclient import TestClient

    monkeypatch.setattr(api_mod, "consult", lambda complaint, **kw: {
        "s1": S1Normalize(symptoms=["胸闷"]), "results": [], "divergence": None,
        "rejected": False, "reject_reason": None, "s2": S2Elements(), "residual": None,
        "followup": None, "insufficient": True, "insufficient_reason": "请补充更多信息",
        "coverage": 0.0, "manifest": {"llm_calls": 2},
    })
    resp = TestClient(api_mod.app).post("/api/consult", json={"complaint": "胸闷"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["insufficient"] is True and body["insufficient_reason"] == "请补充更多信息"
    assert body["results"] == [] and body["followup"] is None


# ---------- /api/trajectories/{physician} ----------


def test_api_trajectories_unknown_physician_404():
    resp = TestClient(api_main.app).get("/api/trajectories/nobody")
    assert resp.status_code == 404


def test_api_trajectories_missing_data_returns_503(monkeypatch):
    import core.transition as transition_mod

    def boom(*a, **k):
        raise FileNotFoundError("未找到 cases.json")

    monkeypatch.setattr(transition_mod, "load_trajectories", boom)
    resp = TestClient(api_main.app).get("/api/trajectories/ye_tianshi")
    assert resp.status_code == 503


def test_api_trajectories_returns_only_requested_physician(monkeypatch):
    import core.transition as transition_mod

    fake = {
        "ye_tianshi": [{"case_group_id": "g1", "n_visits": 2, "visits": []}],
        "wu_jutong": [{"case_group_id": "g2", "n_visits": 3, "visits": []}],
    }
    monkeypatch.setattr(transition_mod, "load_trajectories", lambda: fake)
    resp = TestClient(api_main.app).get("/api/trajectories/ye_tianshi")
    assert resp.status_code == 200
    body = resp.json()
    assert body["physician"] == "ye_tianshi"
    assert len(body["trajectories"]) == 1
    assert body["trajectories"][0]["case_group_id"] == "g1"


def test_api_trajectories_physician_with_no_trajectories_returns_empty_list(monkeypatch):
    import core.transition as transition_mod

    monkeypatch.setattr(transition_mod, "load_trajectories", lambda: {})
    resp = TestClient(api_main.app).get("/api/trajectories/ye_tianshi")
    assert resp.status_code == 200
    assert resp.json()["trajectories"] == []


# ---------- 模块8：逐请求检索模式 ----------


def test_consult_endpoint_threads_retriever_mode_through(monkeypatch):
    """请求体里的 retriever_mode 要原样传给 consult()——不是存到什么服务端
    设置里，也不是设环境变量。"""
    seen = {}

    def fake_consult(complaint, **kwargs):
        seen.update(kwargs)
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "纳差乏力", "retriever_mode": "bm25"})
    assert resp.status_code == 200
    assert seen["retriever_mode"] == "bm25"


def test_consult_endpoint_defaults_retriever_mode_to_none(monkeypatch):
    seen = {}

    def fake_consult(complaint, **kwargs):
        seen.update(kwargs)
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    client.post("/api/consult", json={"complaint": "纳差乏力"})
    assert seen["retriever_mode"] is None


def test_unknown_retriever_mode_is_400_not_500(monkeypatch):
    """模式名写错是请求的问题，要回 400 并带上人能看懂的原因，不是 500。"""
    def fake_consult(complaint, **kwargs):
        raise ValueError("未知的 retriever_mode='xxx'，目前支持 ['bm25', 'dense', 'graph', 'hybrid']")

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "纳差乏力", "retriever_mode": "xxx"})
    assert resp.status_code == 400
    assert "未知的 retriever_mode" in resp.json()["detail"]


def test_retrieval_error_reaches_the_frontend_as_a_field_not_a_500(monkeypatch):
    """检索模式不可用时是 200 + retrieval_error 字段，不是 500 裸奔——
    前端能把这句话显示出来，而不是看到一个"服务器内部错误"。"""
    def fake_consult(complaint, **kwargs):
        outcome = _fake_outcome()
        outcome["results"] = []
        outcome["divergence"] = None
        outcome["retrieval_error"] = "检索模式「graph」在这台机器上不可用：未找到 element_index.json"
        return outcome

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "纳差乏力", "retriever_mode": "graph"})
    assert resp.status_code == 200
    body = resp.json()
    assert "graph" in body["retrieval_error"]
    assert body["results"] == []
    assert body["rejected"] is False
    assert body["insufficient"] is False


def test_retrieval_error_key_present_on_normal_path_too(monkeypatch):
    """键集一致：正常返回也要带 retrieval_error（None），前端按同一份契约读。"""
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _fake_outcome())
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "纳差乏力"}).json()
    assert "retrieval_error" in body
    assert body["retrieval_error"] is None
