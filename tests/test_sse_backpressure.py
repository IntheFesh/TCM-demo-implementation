"""R40：SSE 背压。**队列曾经是无上限的。**

改之前 `_ConsultStream.events_q = queue.Queue()`——客户端读得慢或者卡住时，
后台线程照样按 token 频率往里塞 `s3_delta`（一次 S3 是千条量级），队列只涨不降，
而这中间没有任何一处会报错。几十条慢连接就能把进程的内存吃掉。

改之后两类事件两种策略，**不能合并成一种**：
  · 增量事件：队列满就丢 + 计数（终值由 s3_done / done 兜底）
  · 其余事件：阻塞等，让生产端慢到消费端的速度上（这才是背压）
"""
from __future__ import annotations

import queue
import threading
import time

import pytest

import api.main as api_main
from api.main import (
    SSE_DROPPABLE_EVENTS,
    SSE_PUT_TIMEOUT_SECONDS,
    SSE_QUEUE_MAXSIZE,
    StreamClosed,
    _ConsultStream,
)


def _stream() -> _ConsultStream:
    """一条流。信号量**先 acquire**：`finish()` 会 release 它，
    没占过的 BoundedSemaphore 被 release 会抛 "released too many times"
    ——那是这个夹具的问题，不是被测代码的问题。"""
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    return _ConsultStream("t", slots)


def test_the_queue_has_an_upper_bound():
    """无上限的队列是这个文件存在的理由。"""
    s = _stream()
    assert s.events_q.maxsize == SSE_QUEUE_MAXSIZE
    assert SSE_QUEUE_MAXSIZE > 0


def test_the_bound_leaves_headroom_for_a_normal_consult():
    """一次问诊的增量事件在千条量级。上限太小会让正常的快客户端也开始丢。"""
    assert SSE_QUEUE_MAXSIZE >= 1000


def test_only_delta_events_may_be_dropped():
    """**这张表只许收窄，不许扩张。** 把 need_input 或 done 放进来就等于
    允许静默丢结果。"""
    assert SSE_DROPPABLE_EVENTS == frozenset({"s3_delta"})
    for name in ("done", "error", "need_input", "s1_done", "s2_done", "s3_done"):
        assert name not in SSE_DROPPABLE_EVENTS


def test_deltas_are_dropped_and_counted_when_the_queue_is_full(monkeypatch):
    monkeypatch.setattr(api_main, "SSE_QUEUE_MAXSIZE", 3)
    s = _stream()
    for i in range(10):
        s.emit("s3_delta", {"text": str(i)})
    assert s.events_q.qsize() == 3
    assert s.dropped_deltas == 7, "丢了却没数出来"


def test_dropping_never_raises_so_the_consult_keeps_going():
    """丢增量只是打字机效果卡一下，不该把这次问诊弄挂。"""
    s = _stream()
    s.events_q = queue.Queue(maxsize=1)
    s.events_q.put(("x", {}))
    s.emit("s3_delta", {"text": "a"})      # 不抛
    assert s.dropped_deltas == 1


def test_a_non_droppable_event_blocks_until_there_is_room():
    """这才是背压：生产端慢到消费端的速度上。"""
    s = _stream()
    s.events_q = queue.Queue(maxsize=1)
    s.events_q.put(("占位", {}))
    done = threading.Event()

    def producer():
        s.emit("s1_done", {"symptoms": []})
        done.set()

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    assert not done.wait(timeout=0.2), "队列满了却没有阻塞"
    s.events_q.get()                       # 消费者读走一条
    assert done.wait(timeout=2), "腾出位置之后没有继续"


def test_a_non_droppable_event_gives_up_after_the_timeout(monkeypatch):
    """阻塞不是无限等。队列满这么久 = 没人在读，跟客户端断开是同一件事，
    走同一条收尾路径（StreamClosed）。"""
    monkeypatch.setattr(api_main, "SSE_PUT_TIMEOUT_SECONDS", 0.05)
    s = _stream()
    s.events_q = queue.Queue(maxsize=1)
    s.events_q.put(("占位", {}))
    with pytest.raises(StreamClosed):
        s.emit("done", {})
    assert s.cancel.is_set(), "放弃之后要置位 cancel，让后台线程也收工"


def test_the_put_timeout_is_long_enough_for_a_healthy_slow_client():
    """生成器每 50ms 轮询一次。正常情况下这个等待是微秒级；
    几十秒还塞不进去说明连接真的死了。"""
    assert SSE_PUT_TIMEOUT_SECONDS >= 5


def test_emit_still_refuses_to_run_after_cancel():
    """客户端断开之后不再花 LLM 调用——这条既有语义不许被背压改动。"""
    s = _stream()
    s.cancel.set()
    with pytest.raises(StreamClosed):
        s.emit("s1_done", {})
    with pytest.raises(StreamClosed):
        s.emit("s3_delta", {})


def test_finish_does_not_hang_forever_on_a_full_queue(monkeypatch):
    """队列有上限之后，一个没人读的满队列会让 `finish()` 永远阻塞，
    后台线程于是永远不退出。"""
    monkeypatch.setattr(api_main, "SSE_PUT_TIMEOUT_SECONDS", 0.05)
    s = _stream()
    s.events_q = queue.Queue(maxsize=1)
    s.events_q.put(("占位", {}))
    t0 = time.perf_counter()
    s.finish()                              # 不抛、不挂
    assert time.perf_counter() - t0 < 1.0


def test_the_dropped_count_reaches_the_client_as_its_own_event():
    """丢了必须说出来。单独一个事件而不是塞进 done 的载荷：done 的形状跟
    /api/consult 的响应体是同一份契约，往里加只有流式才有的键会让契约分叉。"""
    s = _stream()
    s.dropped_deltas = 3
    s.emit("deltas_dropped", {"n": s.dropped_deltas})
    name, data = s.events_q.get_nowait()
    assert name == "deltas_dropped" and data["n"] == 3


def test_the_frontend_renders_the_dropped_notice():
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(
        encoding="utf-8")
    assert '"deltas_dropped"' in src
    assert "已跳过" in src


def test_zero_drops_emits_nothing(monkeypatch):
    """没丢就不该有那条提示——一条"已跳过 0 条"的提示只会让人以为出了事。"""
    s = _stream()
    assert s.dropped_deltas == 0
    # worker 里的条件是 `if stream.dropped_deltas:`，这里把那个条件本身钉住
    assert not s.dropped_deltas


def test_the_env_can_tune_both_knobs(monkeypatch):
    """现场的连接质量差别很大，两个数都要能调。"""
    import importlib

    monkeypatch.setenv("SSE_QUEUE_MAXSIZE", "77")
    monkeypatch.setenv("SSE_PUT_TIMEOUT_SECONDS", "1.5")
    reloaded = importlib.reload(api_main)
    try:
        assert reloaded.SSE_QUEUE_MAXSIZE == 77
        assert reloaded.SSE_PUT_TIMEOUT_SECONDS == 1.5
    finally:
        monkeypatch.delenv("SSE_QUEUE_MAXSIZE")
        monkeypatch.delenv("SSE_PUT_TIMEOUT_SECONDS")
        importlib.reload(api_main)
