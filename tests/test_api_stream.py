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
    # 等待预算给到 60 秒，不是因为起 uvicorn 慢（本机回环通常 0.2 秒内就绪，
    # 循环一就绪就退出，不会真等满），而是因为 app 的 startup 钩子会跑真实的
    # 检索器预热：这台沙箱没有 cases.json，预热几毫秒就跳过了；**有 cases.json
    # 的机器（AutoDL）会在这里真的加载 embedding 模型**，5 秒根本不够，
    # 这条测试会在那边莫名其妙地红。发现过程记在模块8：临时造了个合成
    # cases.json 做验证，这条测试立刻就红了，才看出预算是按空环境定的。
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            httpx.get(f"{base_url}/health", timeout=1.0)
            break
        except httpx.TransportError:
            time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn 没能在 60 秒内起来")

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


def test_exception_in_worker_becomes_error_event(monkeypatch, capsys):
    """契约变更（企业化整改）：error 事件的 detail 不再是 str(e) 原样下发。
    LLMError 的文本带着后端名、模型名、模型原始输出前 500 字，claude_cli 后端
    还带子进程整段 stderr——对匿名 HTTP 调用方就是泄露。现在客户端拿到的是
    异常类型 + 错误编号，完整异常只打到服务端 stderr，两边靠编号对上。
    之前这里断言的是 "LLM 后端挂了" in detail，那条断言现在反过来：原文
    **不能**出现在 detail 里。"""

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
    detail = next(d for name, d in events if name == "error")["detail"]
    assert "LLM 后端挂了" not in detail, "异常原文不该下发给客户端"
    assert "RuntimeError" in detail and "错误编号" in detail
    error_id = detail.split("错误编号 ")[1].split("）")[0]
    # 服务端 stderr 上有同一个编号 + 原文，运维凭编号能对上
    err = capsys.readouterr().err
    assert f"[consult-error {error_id}]" in err and "LLM 后端挂了" in err


def test_user_facing_value_error_is_passed_through_unchanged(monkeypatch):
    """模式名不认识那条 ValueError 是用户的请求写错，文案是给用户看的、不含
    内部信息，跟 /api/consult 的 400 一字不差地发——不能被上面那条脱敏规则
    误伤成"服务端处理失败"。"""

    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        raise ValueError("未知的 retriever_mode：'没有这个模式'")

    monkeypatch.setattr(api_main, "consult", fake_consult)
    client = TestClient(api_main.app)
    out_q: queue.Queue = queue.Queue()
    _read_stream_into(client, "纳差", out_q)
    events = []
    while not out_q.empty():
        events.append(out_q.get())
    detail = next(d for name, d in events if name == "error")["detail"]
    assert detail == "未知的 retriever_mode：'没有这个模式'"


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


# ---------- _ConsultStream：答案只在有问题挂起时才收，断开就取消 ----------


def _bridge() -> api_main._ConsultStream:
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    return api_main._ConsultStream("test-stream", sem)


def _drain(q: queue.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_answer_before_any_question_is_rejected():
    """之前流一开就登记答案队列，还没提问就能往里塞答案，下一个问题一问出来
    立刻被这个预先塞的答案"回答"——外人拿到 stream_id 就能替用户答追问。"""
    s = _bridge()
    assert s.deliver_answer("有") is False


def test_stale_answer_after_timeout_never_reaches_the_next_question(monkeypatch):
    """第一问超时之后迟到的答案不能留着喂给第二问。追问「有没有便血」如果吃到
    上一问的「有」，会凭空触发一次安全否决。"""
    monkeypatch.setattr(api_main, "ANSWER_TIMEOUT_SECONDS", 0.2)
    s = _bridge()
    assert s.ask("有没有便血？") is None  # 没人答，超时
    assert s.deliver_answer("有") is False, "问题已经超时，迟到的答案没地方接"

    def answer_second_question_with_no():
        deadline = time.time() + 3
        while time.time() < deadline:
            if s.deliver_answer("没有"):
                return
            time.sleep(0.01)

    t = threading.Thread(target=answer_second_question_with_no, daemon=True)
    t.start()
    assert s.ask("有没有口苦？") == "没有"
    t.join(3)
    answered = [d for name, d in _drain(s.events_q) if name == "followup_answered"]
    assert answered == [{"question": "有没有口苦？", "answer": "没有"}]


def test_second_answer_to_the_same_question_is_rejected():
    s = _bridge()
    got = {}

    def ask():
        got["answer"] = s.ask("有没有口苦？")

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and not s.deliver_answer("有"):
        time.sleep(0.01)
    t.join(3)
    assert got["answer"] == "有"
    assert s.deliver_answer("没有") is False, "同一个问题答过一次之后不该再收"


def test_cancel_unblocks_a_pending_question_with_stream_closed(monkeypatch):
    """客户端断开后不能让后台线程傻等满 ANSWER_TIMEOUT_SECONDS（300 秒）。"""
    monkeypatch.setattr(api_main, "ANSWER_TIMEOUT_SECONDS", 300)
    s = _bridge()
    result = {}

    def ask():
        try:
            s.ask("有没有口苦？")
        except api_main.StreamClosed:
            result["closed"] = True

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    time.sleep(0.1)
    s.cancel.set()
    t.join(3)
    assert not t.is_alive() and result.get("closed") is True


def test_emit_after_cancel_raises_stream_closed():
    s = _bridge()
    s.cancel.set()
    with pytest.raises(api_main.StreamClosed):
        s.emit("s1_done", {"symptoms": []})


def test_client_disconnect_stops_the_worker(monkeypatch, live_server):
    """标签页一关，后台线程要在下一次回调就停下来，不能把 S1→S3 全跑完、每次
    LLM 调用照样计费。要真实 socket：断开这件事只有真实连接能发生。"""
    steps: list[int] = []
    finished = threading.Event()
    closed = {}

    def fake_consult(complaint, ask_fn=None, on_step=None, **kw):
        try:
            for i in range(200):
                on_step("react_step", {"step": i})
                steps.append(i)
                time.sleep(0.02)
        except api_main.StreamClosed:
            closed["yes"] = True
            raise
        finally:
            finished.set()
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)

    with httpx.stream("POST", f"{live_server}/api/consult/stream",
                      json={"complaint": "纳差"}, timeout=10) as resp:
        for line in resp.iter_lines():
            if line.startswith("event: react_step"):
                break
    # 退出 with 块 = 客户端关掉连接

    assert finished.wait(3), "客户端断开 3 秒后后台线程还在跑——取消没有生效"
    assert closed.get("yes") is True
    assert len(steps) < 200
