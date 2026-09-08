"""api/main.py 企业化整改那一轮加的硬约束：请求体上限、并发上限、存活探针不排队、
lifespan 预热、发给客户端的文字里不带项目绝对路径。全部 mock consult，不联网。"""
import inspect
import threading
import time

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from tests.test_api import _fake_outcome


# ---------- 请求体上限 ----------


def test_complaint_over_limit_is_422_before_any_llm_call(monkeypatch):
    called = []
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: called.append(1) or _fake_outcome())
    client = TestClient(api_main.app)
    too_long = "痛" * (api_main.MAX_COMPLAINT_CHARS + 1)
    assert client.post("/api/consult", json={"complaint": too_long}).status_code == 422
    assert client.post("/api/consult/stream", json={"complaint": too_long}).status_code == 422
    assert called == [], "超长主诉必须在校验层挡下，一次 consult 都不该调"


def test_complaint_exactly_at_limit_is_accepted(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _fake_outcome())
    client = TestClient(api_main.app)
    resp = client.post("/api/consult", json={"complaint": "痛" * api_main.MAX_COMPLAINT_CHARS})
    assert resp.status_code == 200


def test_empty_complaint_is_422(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: pytest.fail("空主诉不该调 consult"))
    client = TestClient(api_main.app)
    assert client.post("/api/consult", json={"complaint": ""}).status_code == 422


def test_answer_over_limit_is_422():
    client = TestClient(api_main.app)
    resp = client.post(
        "/api/consult/stream/whatever/answer",
        json={"answer": "有" * (api_main.MAX_ANSWER_CHARS + 1)},
    )
    assert resp.status_code == 422


# ---------- 并发上限 ----------


def test_consult_returns_503_with_retry_after_when_all_slots_busy(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    assert sem.acquire(blocking=False)  # 把唯一的槽占住
    monkeypatch.setattr(api_main, "_consult_slots", sem)
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: pytest.fail("满了就不该调 consult"))
    client = TestClient(api_main.app)

    resp = client.post("/api/consult", json={"complaint": "纳差"})
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "10"
    assert "上限" in resp.json()["detail"]

    resp = client.post("/api/consult/stream", json={"complaint": "纳差"})
    assert resp.status_code == 503


def test_slot_is_released_after_success_400_and_500(monkeypatch):
    """三条出口都要放回槽位，漏一条就是慢性泄漏：几次报错之后服务永远 503。"""
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(api_main, "_consult_slots", sem)
    client = TestClient(api_main.app, raise_server_exceptions=False)

    def _free() -> bool:
        if sem.acquire(blocking=False):
            sem.release()
            return True
        return False

    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _fake_outcome())
    assert client.post("/api/consult", json={"complaint": "纳差"}).status_code == 200
    assert _free()

    def _bad_mode(complaint, **kw):
        raise ValueError("未知的 retriever_mode")

    monkeypatch.setattr(api_main, "consult", _bad_mode)
    assert client.post("/api/consult", json={"complaint": "纳差"}).status_code == 400
    assert _free()

    def _boom(complaint, **kw):
        raise RuntimeError("后端挂了")

    monkeypatch.setattr(api_main, "consult", _boom)
    assert client.post("/api/consult", json={"complaint": "纳差"}).status_code == 500
    assert _free()


def test_slot_is_released_after_stream_finishes(monkeypatch):
    sem = threading.BoundedSemaphore(1)
    monkeypatch.setattr(api_main, "_consult_slots", sem)
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: _fake_outcome())
    client = TestClient(api_main.app)
    with client.stream("POST", "/api/consult/stream", json={"complaint": "纳差"}) as resp:
        body = "".join(resp.iter_text())
    assert "event: done" in body
    assert sem.acquire(blocking=False), "流跑完后槽位必须放回"
    sem.release()


# ---------- 存活探针与预热 ----------


def test_health_is_async_so_it_never_queues_behind_the_threadpool():
    """同步端点跑在 anyio 线程池（默认 40 个槽）里；几十条并发问诊把槽占满时，
    同步的 /health 会跟着排队超时，编排器就会把一个活着的进程重启掉。"""
    assert inspect.iscoroutinefunction(api_main.health)


def test_lifespan_stops_waiting_for_a_stuck_warmup_and_serves(monkeypatch):
    """预热卡住（模型下载重试）时服务不能跟着卡：超过 WARMUP_TIMEOUT_SECONDS 就
    先开始监听，/health 能答；预热线程留在后台。"""
    gate = threading.Event()
    started = threading.Event()

    def stuck_warmup():
        started.set()
        gate.wait(timeout=10)

    monkeypatch.setattr(api_main, "_warmup", stuck_warmup)
    monkeypatch.setattr(api_main, "WARMUP_TIMEOUT_SECONDS", 0.2)
    t0 = time.monotonic()
    try:
        with TestClient(api_main.app) as client:
            assert started.is_set()
            assert client.get("/health").json() == {"status": "ok"}
        assert time.monotonic() - t0 < 5, "lifespan 没有在超时后放行"
    finally:
        gate.set()


def test_lifespan_runs_warmup_exactly_once(monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "_warmup", lambda: calls.append(1))
    with TestClient(api_main.app) as client:
        assert client.get("/health").json() == {"status": "ok"}
    assert calls == [1]


# ---------- 对外文字不带绝对路径 ----------


def test_public_text_strips_project_root_but_keeps_file_name():
    root = str(api_main.ROOT)
    out = api_main._public_text(f"未找到 {root}/data/element_index.json。请先运行 x")
    assert root not in out
    assert out.startswith("未找到 data/element_index.json。")


def test_retrieval_error_in_response_has_no_absolute_path(monkeypatch):
    root = str(api_main.ROOT)
    outcome = dict(_fake_outcome())
    outcome.update({
        "results": [],
        "divergence": None,
        "retrieval_error": f"检索模式「graph」在这台机器上不可用：未找到 {root}/data/element_index.json。",
    })
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "纳差", "retriever_mode": "graph"}).json()
    assert root not in body["retrieval_error"]
    assert "element_index.json" in body["retrieval_error"]


def test_graph_503_detail_has_no_absolute_path(monkeypatch):
    monkeypatch.setattr(api_main, "get_graph_store", lambda: None)
    client = TestClient(api_main.app)
    detail = client.get("/api/graph").json()["detail"]
    assert str(api_main.ROOT) not in detail
    assert "graph.json" in detail


def test_trajectories_503_detail_has_no_absolute_path(monkeypatch):
    import core.transition as transition

    def _missing():
        raise FileNotFoundError(f"未找到 {api_main.ROOT}/cases.json。请先运行 extract_cases")

    monkeypatch.setattr(transition, "load_trajectories", _missing)
    client = TestClient(api_main.app)
    resp = client.get("/api/trajectories/ye_tianshi")
    assert resp.status_code == 503
    assert str(api_main.ROOT) not in resp.json()["detail"]
    assert "cases.json" in resp.json()["detail"]
