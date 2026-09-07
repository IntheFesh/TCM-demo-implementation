"""api/main.py 里 /api/consult/stream 的离线测试：mock api.main.consult，不需要
网络、不需要真实 LLM。

真实 SSE 事件流（curl -N 对真实 uvicorn）另外验证——这里测的是跟传输方式无关
的契约：事件顺序、need_input 暂停/恢复的队列机制、超时兜底、异常转成 error
事件、stream_id 用完即焚。mock 的 consult 自己调 ask_fn/on_step，模拟真实
consult() 会怎么用这两个参数，而不是只返回一个静态字典——那样测不出暂停/恢复
这条最容易出 bug 的路径。
"""
import json
import queue
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

import api.main as api_main
from tests.test_api import _fake_outcome


def _parse_sse(lines):
    """把 iter_lines() 吐出来的原始行重新拼成 (event, data) 对。"""
    event_name = None
    data_lines: list[str] = []
    for line in lines:
        if line == "":
            if event_name is not None:
                yield event_name, json.loads("".join(data_lines))
            event_name, data_lines = None, []
            continue
        if line.startswith("event: "):
            event_name = line[len("event: "):]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])


def _read_stream_into(client: TestClient, complaint: str, out_q: queue.Queue) -> None:
    """在独立线程里跑：一直读到流关闭为止，把每个 (event, data) 塞进 out_q。"""
    with client.stream("POST", "/api/consult/stream", json={"complaint": complaint}) as resp:
        for event_name, data in _parse_sse(resp.iter_lines()):
            out_q.put((event_name, data))


@pytest.fixture
def live_server():
    """`fastapi.testclient.TestClient` 底下的 httpx ASGITransport 会把整个
    ASGI app 跑完（`await self.app(scope, receive, send)`）才把 Response 交还
    给调用方——读过它的源码（httpx/_transports/asgi.py）能确认这一点：body
    是先整段收集进 `body_parts` 再一次性交出去的，`client.stream()` 看着像
    真流式，实际上第一个字节都要等整条 SSE 流跑完才能读到。need_input 这条
    路径要测的正是"流还没跑完、中途另开一个请求把它接着推下去"，TestClient
    这种全缓冲的传输层测不出来——不是随便下的结论，是看了源码之后确认的。

    所以这条测试要一个真的监听 127.0.0.1 的服务：本地回环，不需要外网、
    不需要 API key，一两百毫秒内就能起停，没有违反 CLAUDE.md 对 tests/
    "不需要网络、秒级跑完"的要求（那条要求防的是打真实外部服务/真实 LLM，
    不是防本机回环 socket）。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(api_main.app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=0.5)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn 没能在 5 秒内起来")

    yield base_url

    server.should_exit = True
    thread.join(timeout=5)


# ---------- 基本事件顺序：stream_id 先到，done 最后 ----------


def test_stream_id_arrives_first_then_progress_then_done(monkeypatch):
    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        on_step("s1_done", {"symptoms": ["纳差"]})
        on_step("s2_done", {"elements": []})
        on_step("physician_start", {"physician": "ye_tianshi"})
        on_step("physician_done", {"physician": "ye_tianshi"})
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    out_q: queue.Queue = queue.Queue()
    _read_stream_into(client, "纳差", out_q)

    events = []
    while not out_q.empty():
        events.append(out_q.get())

    names = [e[0] for e in events]
    assert names == ["stream_id", "s1_done", "s2_done", "physician_start", "physician_done", "done"]
    assert "stream_id" in events[0][1]


def test_done_event_payload_matches_consult_response_shape(monkeypatch):
    """done 事件的 data 必须和 /api/consult 对同一个 outcome 产出的响应体完全
    一样——两条端点共用 _consult_response()，这条测试钉住"共用"这件事本身，
    不是分别测两条端点再凭观察说它们应该一致。"""
    outcome = _fake_outcome()

    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        return outcome

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)

    resp = client.post("/api/consult", json={"complaint": "纳差"})
    expected = resp.json()

    out_q: queue.Queue = queue.Queue()
    _read_stream_into(client, "纳差", out_q)
    events = []
    while not out_q.empty():
        events.append(out_q.get())
    done_data = next(d for name, d in events if name == "done")

    assert done_data == expected


# ---------- need_input 暂停 / 恢复 ----------


def _read_live_stream_into(base_url: str, complaint: str, out_q: queue.Queue) -> None:
    with httpx.stream("POST", f"{base_url}/api/consult/stream",
                      json={"complaint": complaint}, timeout=15) as resp:
        for event_name, data in _parse_sse(resp.iter_lines()):
            out_q.put((event_name, data))


def test_need_input_pauses_stream_until_answer_posted(monkeypatch, live_server):
    """核心行为：consult 内部调 ask_fn 会让后台线程阻塞，SSE 流这段时间不产出
    新事件；从独立的 answer 端点 POST 答案后，线程解除阻塞、流继续往下走，
    ask_fn 拿到的确实是刚才 POST 的那个答案（不是别的、也不是 None）。

    这条必须打真实 socket（live_server），不能用 TestClient——TestClient 的
    ASGITransport 会把整个请求跑完才交还响应，"中途暂停、另一个请求把它推
    下去"这件事在它上面根本观察不到（live_server 那条注释有源码依据）。
    """
    seen_answer = {}

    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        on_step("s1_done", {"symptoms": ["纳差"]})
        answer = ask_fn("有没有口苦？")
        seen_answer["value"] = answer
        on_step("physician_done", {"physician": "ye_tianshi"})
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    monkeypatch.setattr(api_main, "ANSWER_TIMEOUT_SECONDS", 10)

    out_q: queue.Queue = queue.Queue()
    t = threading.Thread(target=_read_live_stream_into, args=(live_server, "纳差", out_q), daemon=True)
    t.start()

    stream_id = out_q.get(timeout=5)[1]["stream_id"]
    name, data = out_q.get(timeout=5)
    assert name == "s1_done"
    name, data = out_q.get(timeout=5)
    assert name == "need_input"
    assert data["question"] == "有没有口苦？"

    # 这时候后台线程应该正卡在 ask_fn 里——流里暂时不该再有新东西，
    # 直接 get(timeout=极短) 应该超时，不该"碰巧"已经有下一条事件在等着。
    try:
        extra = out_q.get(timeout=0.3)
        raise AssertionError(f"need_input 之后不该有更多事件，却收到了 {extra}")
    except queue.Empty:
        pass

    resp = httpx.post(f"{live_server}/api/consult/stream/{stream_id}/answer",
                      json={"answer": "没有"}, timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    name, data = out_q.get(timeout=5)
    assert name == "followup_answered"
    assert data == {"question": "有没有口苦？", "answer": "没有"}
    name, data = out_q.get(timeout=5)
    assert name == "physician_done"
    name, data = out_q.get(timeout=5)
    assert name == "done"

    t.join(timeout=5)
    assert seen_answer["value"] == "没有"


def test_answer_to_unknown_stream_id_is_404(monkeypatch):
    client = TestClient(api_main.app)
    resp = client.post("/api/consult/stream/does-not-exist/answer", json={"answer": "有"})
    assert resp.status_code == 404


def test_ask_fn_returns_none_when_answer_times_out(monkeypatch):
    """没人回答时不能让后台线程无限期挂着——超时后 ask_fn 必须像"提问方不打算
    回答"那样返回 None（AskFn 的既有契约），consult 侧的流程照常走完，
    stream 正常收尾而不是悬挂。"""
    seen_answer = {}

    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        seen_answer["value"] = ask_fn("有没有口苦？")
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    monkeypatch.setattr(api_main, "ANSWER_TIMEOUT_SECONDS", 0.2)

    client = TestClient(api_main.app)
    out_q: queue.Queue = queue.Queue()
    _read_stream_into(client, "纳差", out_q)

    events = []
    while not out_q.empty():
        events.append(out_q.get())
    names = [e[0] for e in events]
    assert "need_input" in names
    assert "followup_answered" not in names  # 超时不算"答上了"
    assert names[-1] == "done"
    assert seen_answer["value"] is None


# ---------- 异常与资源清理 ----------


def test_exception_in_worker_becomes_error_event(monkeypatch):
    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        on_step("s1_done", {"symptoms": ["纳差"]})
        raise RuntimeError("LLM 后端挂了")

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    out_q: queue.Queue = queue.Queue()
    _read_stream_into(client, "纳差", out_q)

    events = []
    while not out_q.empty():
        events.append(out_q.get())
    names = [e[0] for e in events]
    assert names == ["stream_id", "s1_done", "error"]
    error_data = next(d for name, d in events if name == "error")
    assert "LLM 后端挂了" in error_data["detail"]


def test_stream_id_is_removed_after_stream_finishes(monkeypatch):
    """流结束（不管是正常 done 还是 error）之后，answer 端点必须查不到这个
    stream_id 了——不清理的话 _answer_queues 会无限增长，也可能让一个早就
    结束的 stream 收到一条永远没人读的迟到答案。"""

    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    out_q: queue.Queue = queue.Queue()
    _read_stream_into(client, "纳差", out_q)
    stream_id = out_q.get()[1]["stream_id"]

    resp = client.post(f"/api/consult/stream/{stream_id}/answer", json={"answer": "有"})
    assert resp.status_code == 404
