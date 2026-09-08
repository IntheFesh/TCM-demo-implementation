"""api/main.py 的离线测试：mock core.chain.consult，不需要网络。"""
import pytest
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


# ---------- M6：role 参数与四种模式的字段裁剪 ----------


def _rich_outcome(disease: str | None = "胸痹", syndrome: str = "痰浊闭阻证") -> dict:
    """带 formula_candidates/reasoning_plain/react_trace/manifest 的完整
    outcome，M6 的角色裁剪测试都基于这份数据——disease 默认填"胸痹"（M4
    参考表里 urgency=high 的真实条目），方便同一份 fixture 既能测常规裁剪
    又能测 urgency=high 的用药建议闸门。"""
    from core.schemas import DoseViolation, FormulaCandidate, FormulaSafety, HerbItem, ReActStepRecord, ReActTrace

    s1 = S1Normalize(symptoms=["胸闷", "胸痛"], tongue="淡红", pulse="弦", unmapped=[])
    s2 = S2Elements(elements=[
        ElementHit(element="心", kind="location", supporting_symptoms=["胸闷"], confidence="high")
    ])
    s3 = S3Syndrome(
        disease=disease,
        syndrome=syndrome,
        reasoning="胸阳不振，痰浊内阻，故见胸闷胸痛。",
        reasoning_plain="你这个胸闷胸痛，是因为体内有些痰湿堵住了胸部气血运行的通道。",
        treatment_principle="通阳泄浊，豁痰宣痹",
        formula_candidates=[FormulaCandidate(
            name="瓜蒌薤白半夏汤", source="classic", confidence="high",
            rationale="经典方，专为胸痹痰浊闭阻而设。",
            herb_items=[HerbItem(name="瓜蒌", dose=15.0, dose_unit="g", role="君"),
                        HerbItem(name="薤白", dose=9.0, dose_unit="g", role="臣")],
        )],
        cited_case_ids=["ye_tianshi-001"],
    )
    s3.formula_candidates[0].safety = FormulaSafety(
        dose_violations=[DoseViolation(herb="细辛", dose=10.0, unit="g", limit_g=3.0, reason="细辛不过钱")],
    )
    trace = ReActTrace(
        steps=[ReActStepRecord(step=1, thought="查一下", action="query_graph",
                               action_input={"node": "胸闷"}, observation="{}")],
        terminated_by="finish", llm_calls=1,
    )
    results = [{
        "physician": "ye_tianshi", "physician_name": "叶天士", "s2": s2, "s3": s3,
        "refs": [{"case_id": "ye_tianshi-001", "score": 0.9, "visit_index": 0,
                  "visit_label": "初诊", "symptoms": ["胸闷"], "tongue": None, "pulse": None,
                  "syndrome": None, "treatment_principle": None, "formula": None,
                  "herbs": [], "excerpt": "某 胸闷胸痛"}],
        "hallucinated": [], "no_reference_cases": False,
        "safety_output": {"incompatible": [], "thermal_warning": None, "revised": False},
        "react_trace": trace, "disease_candidates": [("胸痹", 0.8)],
    }]
    return {
        "s1": s1, "s2": s2, "results": results,
        "divergence": {"same": True, "method": "exact_string_match", "herb_jaccard": 0.0},
        "rejected": False, "reject_reason": None, "residual": None, "followup": None,
        "insufficient": False, "insufficient_reason": None, "coverage": 1.0,
        "safety_flag": None, "manifest": {"model": "fake", "llm_calls": 4},
    }


_ROLE_FIELD_TABLE = {
    # role: (triage_present, formula_candidates_present, food_therapy_present,
    #        react_trace_present, refs_nonempty, divergence_present, manifest_present)
    "patient": (True, False, True, False, False, False, False),
    "doctor": (True, True, True, False, True, False, False),
    "student": (False, True, False, True, True, True, False),
    "researcher": (False, True, False, True, True, True, True),
}


def _post_consult(client, role: str | None, disease="胸痹"):
    body = {"complaint": "胸闷胸痛"}
    if role is not None:
        body["role"] = role
    return client.post("/api/consult", json=body)


@pytest.mark.parametrize("role", ["patient", "doctor", "student", "researcher"])
def test_role_field_sets_match_the_m6_table(monkeypatch, role):
    """M6 闸门表逐条断言：该有的有，不该有的没有——不是"有但为空"。"""
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome())
    client = TestClient(api_main.app)
    resp = _post_consult(client, role)
    assert resp.status_code == 200
    body = resp.json()
    r = body["results"][0]
    (triage_present, fc_present, food_present, react_present,
     refs_nonempty, divergence_present, manifest_present) = _ROLE_FIELD_TABLE[role]

    assert ("triage" in body) is triage_present, f"role={role} triage 字段存在性不对"
    assert ("formula_candidates" in r["s3"]) is fc_present, f"role={role} formula_candidates 字段存在性不对"
    assert ("food_therapy" in body) is food_present, f"role={role} food_therapy 字段存在性不对"
    assert ("react_trace" in r) is react_present, f"role={role} react_trace 字段存在性不对"
    if refs_nonempty:
        assert r["refs"], f"role={role} refs 不该被清空"
    else:
        assert r["refs"] == [], f"role={role} refs 应该是空列表"
    assert ("divergence" in body and body["divergence"] is not None) is divergence_present, (
        f"role={role} divergence 存在性不对"
    )
    assert ("manifest" in body and body["manifest"] is not None) is manifest_present, (
        f"role={role} manifest 存在性不对"
    )


def test_patient_role_response_never_contains_formula_candidates_key(monkeypatch):
    """单独一条测试，因为它是安全边界：patient 角色的响应体里
    formula_candidates 这个键**根本不存在**，不是"存在但是空列表"——两者对
    一个只看 `"formula_candidates" in data` 就决定要不要渲染处方区块的
    前端来说是完全不同的信号，"存在但空"反而更容易被当成 bug 悄悄改成
    兜底显示空处方卡片。同时检查 formula/herbs/western_drugs/selected 这几个
    从 formula_candidates 派生、同样会泄露处方内容的旧式扁平字段——只删
    formula_candidates 本身、留着这几个字段没删是同一类漏洞。"""
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome())
    client = TestClient(api_main.app)
    body = _post_consult(client, "patient").json()
    s3 = body["results"][0]["s3"]
    for key in ("formula_candidates", "formula", "herbs", "western_drugs", "selected"):
        assert key not in s3, f"patient 响应体的 s3 里不该有键「{key}」"
    # 图本身也不能通过 layer 3/4 节点把药名重新泄露回去（见 to_graph 的
    # role 参数文档字符串）。
    graph_layers = {n["data"]["layer"] for n in body["graph"]["nodes"]}
    assert 3 not in graph_layers and 4 not in graph_layers, (
        "patient 角色的图里不该有方剂(3)/药材(4) 层，那两层节点本身带着真实药名"
    )


def test_researcher_role_response_matches_pre_m6_shape_byte_for_byte(monkeypatch):
    """默认角色（不传 role，等同 role=researcher）响应要跟改造前逐字节一致
    ——这是 M6 闸门明确要求的回归保护。用同一份 outcome 分别以"不传 role"
    和"显式传 role=researcher"各请求一次，两次响应体必须完全相同；同时
    确认响应体里没有 M6 新增的 triage/food_therapy/patent_medicines 这几个
    键（改造前的响应形状里压根没有这几个键，加了但为 None/[] 也不算
    "逐字节一致"）。"""
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome())
    client = TestClient(api_main.app)
    body_default = _post_consult(client, None).json()
    body_explicit = _post_consult(client, "researcher").json()
    assert body_default == body_explicit
    for key in ("triage", "food_therapy", "patent_medicines"):
        assert key not in body_default, f"researcher/默认角色不该出现 M6 新增字段「{key}」"
    assert "manifest" in body_default and body_default["manifest"] is not None
    assert "react_trace" in body_default["results"][0]


def test_urgency_high_patient_mode_returns_no_medication_fields(monkeypatch):
    """M6 闸门明确要求的一条：urgency=high 时患者模式不返回任何用药字段
    ——胸痹是 M4 参考表里 urgency=high 的真实条目，这里用它触发真实的
    urgency=high 分支，不是手搓一个假的 urgency 值。"""
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome(disease="胸痹"))
    client = TestClient(api_main.app)
    body = _post_consult(client, "patient").json()
    assert body["triage"]["urgency"] == "high"
    assert body["food_therapy"] == []
    assert body["patent_medicines"] == []
    assert "formula_candidates" not in body["results"][0]["s3"]


def test_apply_medication_gate_suppresses_even_nonempty_content_when_urgency_high():
    """直接单测 _apply_medication_gate 本身，不依赖 food_therapy 现在恰好总是
    空列表这个巧合——喂一份非空的假内容进去，urgency=high 时也必须被清空。
    这是为 M9 接入真实食疗/中成药数据源之后这道闸门依然生效画的一条底线，
    不是等 M9 做完才补的测试。"""
    from api.main import _apply_medication_gate

    assert _apply_medication_gate(["真实食疗建议"], "high") == []
    assert _apply_medication_gate(["真实食疗建议"], "medium") == ["真实食疗建议"]
    assert _apply_medication_gate(["真实食疗建议"], "low") == ["真实食疗建议"]
    assert _apply_medication_gate(["真实食疗建议"], None) == ["真实食疗建议"]


def test_patient_reasoning_uses_plain_version_and_drops_raw_reasoning(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome())
    client = TestClient(api_main.app)
    body = _post_consult(client, "patient").json()
    s3 = body["results"][0]["s3"]
    assert s3["reasoning"] == "你这个胸闷胸痛，是因为体内有些痰湿堵住了胸部气血运行的通道。"
    assert "reasoning_plain" not in s3
    assert "胸阳不振" not in s3["reasoning"]  # 原始专业推理不能残留


def test_patient_reasoning_falls_back_to_placeholder_not_raw_reasoning_when_plain_missing(monkeypatch):
    """reasoning_plain 是可选 schema 字段——真实缺失时（比如旧式构造、或
    模型这次没给）patient 角色不能悄悄退回显示专业版 reasoning，那样会让
    "schema 层可选"这个决定破坏掉安全边界。"""
    outcome = _rich_outcome()
    outcome["results"][0]["s3"].reasoning_plain = None
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    client = TestClient(api_main.app)
    body = _post_consult(client, "patient").json()
    s3 = body["results"][0]["s3"]
    assert "胸阳不振" not in s3["reasoning"]
    assert s3["reasoning"]  # 不能是空字符串，得有个占位说明


def test_patient_safety_output_is_simplified_without_herb_names(monkeypatch):
    """safety_output 简化：不能带着具体药名（配伍禁忌药对/寒热警告文本都会
    提到药材），否则从这条后门把 formula_candidates 已经删掉的药名信息
    重新泄露出去。"""
    outcome = _rich_outcome()
    outcome["results"][0]["safety_output"] = {
        "incompatible": [("甘草", "海藻")], "thermal_warning": "方中黄连、黄芩性寒", "revised": True,
    }
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    client = TestClient(api_main.app)
    body = _post_consult(client, "patient").json()
    safety = body["results"][0]["safety_output"]
    assert safety == {"has_safety_note": True, "revised": True}
    assert "甘草" not in str(safety) and "海藻" not in str(safety) and "黄连" not in str(safety)


def test_doctor_role_keeps_full_safety_output_and_reasoning(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome())
    client = TestClient(api_main.app)
    body = _post_consult(client, "doctor").json()
    r = body["results"][0]
    assert r["safety_output"]["incompatible"] == []  # 原样传递，不简化
    assert r["s3"]["reasoning"] == "胸阳不振，痰浊内阻，故见胸闷胸痛。"  # 专业版原文
    assert r["s3"]["formula_candidates"][0]["herb_items"][0]["dose"] == 15.0  # 剂量原样保留


def test_triage_absent_when_model_disease_not_in_reference_table(monkeypatch):
    """模型填的病名不在 M4 参考表里（或没填）时，triage 如实返回 None，
    不伪造一个导诊结果——这条呼应 M4 那条"错误科室指向比不给更糟"的原则。"""
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _rich_outcome(disease=None))
    client = TestClient(api_main.app)
    body = _post_consult(client, "patient").json()
    assert body["triage"] is None
    # triage 是 None 时 urgency 也是 None，_apply_medication_gate 不该因此报错，
    # 且默认放行（urgency 未知不等于 urgency=high，不能因为不确定就一律清空）。
    assert body["food_therapy"] == []  # 本轮食疗内容恒为空，这里验证的是不崩，不是这条闸门
