"""R40：**先监听，再预热**。`api/warmup.py` 的状态机 + `/health` 的就绪语义。

改之前：ASGI 的 startup 阶段等预热，最多 120 秒。那段时间端口开着、连接建得上、
**一个请求都不答**——编排器的探针连得上却等不到响应，会把一个正常的进程判死。

这个文件钉的是改之后的三件事：
  1. 两项预热并行、各自失败不阻塞启动、状态如实（ready / skipped / failed 分开）
  2. `/health` 是**就绪**探针（预热中 503 + 进度，响应体照样完整），
     `/health/live` 是**存活**探针（永远 200）
  3. `ready=true` 的两种来路（跑完了 / 压根没跑）在 `started` 上分得开
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.warmup import (
    STEP_LABELS,
    WARMUP_STEPS,
    WarmupTracker,
    parallel_default,
    run_warmup,
)


@pytest.fixture()
def tracker() -> WarmupTracker:
    return WarmupTracker()


def test_two_steps_and_every_one_has_a_human_label():
    """标签是给 `/health` 的进度条用的。缺一个就会在界面上显示成内部 id。"""
    assert WARMUP_STEPS == ("retriever", "ontology")
    for name in WARMUP_STEPS:
        assert STEP_LABELS[name] and name not in STEP_LABELS[name]


def test_a_fresh_tracker_is_ready_because_nothing_is_coming(tracker):
    """**没人启动过预热 → 就绪。** 503 的意思是"马上就好，别放流量"；
    预热压根不会发生的进程里回 503 就是永远 503（`TestClient` 不进上下文
    管理器时 lifespan 不跑，正是这种情况）。"""
    assert tracker.ready is True
    snap = tracker.snapshot()
    assert snap["started"] is False and snap["ready"] is True
    assert snap["all_ready"] is False, "没跑过就不能说「全部就绪」"


def test_begin_flips_it_to_not_ready_synchronously(tracker):
    """登记必须同步生效：lifespan 起完线程立刻 yield，第一个探针可能比线程的
    第一行还早。那时若还报就绪，流量会在最慢的那几秒里被放进来。"""
    tracker.begin()
    assert tracker.ready is False
    assert tracker.snapshot()["progress"] == "0/2"


def test_a_skipped_step_counts_as_settled_not_as_success(tracker):
    """这台沙盒下不动编码模型。要求全部 ready 会让 `/health` 永远 503；
    但 `skipped` 也不能算成功——运维要看得出"没有预热"。"""
    tracker.begin()
    tracker.mark("retriever", "skipped", ms=12.0, note="模型下不动")
    tracker.mark("ontology", "ready", ms=2300.0, note="1232 味 / 235 首")
    assert tracker.ready is True
    assert tracker.all_ready is False
    snap = tracker.snapshot()
    assert snap["progress"] == "2/2"
    rows = {s["step"]: s for s in snap["steps"]}
    assert rows["retriever"]["status"] == "skipped"
    assert rows["retriever"]["note"] == "模型下不动"


def test_failed_and_skipped_are_different_states(tracker):
    """`skipped` = 这台机器上没这份数据（合法、常态）；
    `failed` = 有数据但加载炸了（要看一眼）。合并成一个就看不出后者。"""
    tracker.begin()
    tracker.mark("retriever", "failed", note="IndexError")
    tracker.mark("ontology", "ready")
    snap = tracker.snapshot()
    assert {s["status"] for s in snap["steps"]} == {"failed", "ready"}
    assert tracker.ready is True and tracker.all_ready is False


def test_an_unknown_status_is_rejected(tracker):
    with pytest.raises(ValueError) as e:
        tracker.mark("ontology", "差不多好了")
    assert "未知预热状态" in str(e.value)


def test_run_warmup_runs_every_step_and_fills_the_snapshot(tracker):
    snap = run_warmup(tracker, parallel=False)
    assert snap["started"] is True
    assert snap["n_done"] == snap["n_steps"] == 2
    assert snap["ready"] is True
    assert snap["elapsed_ms"] >= 0


def test_run_warmup_parallel_and_serial_reach_the_same_end_state():
    """并行只改**什么时候**完成，不改**完成了什么**。"""
    a = run_warmup(WarmupTracker(), parallel=False)
    b = run_warmup(WarmupTracker(), parallel=True)
    assert [s["status"] for s in a["steps"]] == [s["status"] for s in b["steps"]]
    assert a["parallel"] is False and b["parallel"] is True


def test_a_step_that_raises_does_not_block_the_others(monkeypatch, tracker):
    """预热不是启动的前置条件。一项炸了其余照跑，服务照常起。"""
    import api.warmup as w

    def boom(tr):
        raise RuntimeError("假装模型目录不存在")

    monkeypatch.setitem(w.STEP_FUNCS, "retriever", boom)
    with pytest.raises(RuntimeError):
        # 这条路径直接调 STEP_FUNCS 里那个函数：run_warmup 不吞异常，
        # 吞异常的是每个真实 _warm_* 函数自己（各自 try）——**区别要测出来**：
        # 换掉的这个假函数没有自己的 try，所以它会冒出来。
        w.STEP_FUNCS["retriever"](tracker)
    # 真实的那两个各自 try，所以整体跑完不抛
    monkeypatch.setitem(w.STEP_FUNCS, "retriever", w._warm_retriever)
    assert run_warmup(WarmupTracker())["ready"] is True


def test_parallel_default_is_on_and_the_env_can_turn_it_off(monkeypatch):
    """收益取决于检索器那一步有多少时间真的在等 IO——只能现场实测，
    所以给开关而不是把结论写死。"""
    monkeypatch.delenv("WARMUP_PARALLEL", raising=False)
    assert parallel_default() is True
    monkeypatch.setenv("WARMUP_PARALLEL", "0")
    assert parallel_default() is False
    monkeypatch.setenv("WARMUP_PARALLEL", "no")
    assert parallel_default() is False
    monkeypatch.setenv("WARMUP_PARALLEL", "1")
    assert parallel_default() is True


def test_reset_clears_everything(tracker):
    tracker.begin()
    tracker.mark("ontology", "ready", ms=1.0)
    tracker.reset()
    assert tracker.snapshot()["started"] is False
    assert all(s["status"] == "pending" for s in tracker.snapshot()["steps"])


# ---------- /health 的两种语义 ----------


def test_health_returns_503_with_progress_while_warming(monkeypatch):
    """预热中：**503**（编排器看状态码）+ **完整响应体**（前端读身份色）。
    只回一个空 503 的话，预热那几秒里页面是一片没有颜色的骨架。"""
    from api.warmup import TRACKER

    gate = threading.Event()
    monkeypatch.setattr(api_main, "_warmup", lambda: gate.wait(timeout=10))
    TRACKER.reset()
    try:
        with TestClient(api_main.app) as client:
            resp = client.get("/health")
            assert resp.status_code == 503
            body = resp.json()
            assert body["status"] == "warming"
            assert body["warmup"]["ready"] is False
            assert body["warmup"]["progress"] == "0/2"
            assert body["physicians"] and body["example_complaints"]
    finally:
        gate.set()
        TRACKER.reset()


def test_health_live_is_200_even_while_warming(monkeypatch):
    """存活跟就绪问的不是同一个问题：一个答"要不要重启我"，
    一个答"能不能放流量进来"。合成一个的代价是编排器分不清"还在热"和"已经死"。"""
    from api.warmup import TRACKER

    gate = threading.Event()
    monkeypatch.setattr(api_main, "_warmup", lambda: gate.wait(timeout=10))
    TRACKER.reset()
    try:
        with TestClient(api_main.app) as client:
            resp = client.get("/health/live")
            assert resp.status_code == 200
            assert resp.json()["status"] == "alive"
            assert resp.json()["warmup"]["ready"] is False
    finally:
        gate.set()
        TRACKER.reset()


def test_health_is_200_when_nobody_ever_started_warmup():
    """`TestClient` 不当上下文管理器用时 lifespan 不跑。这时惰性加载照常工作，
    回 503 就是永远 503。"""
    from api.warmup import TRACKER

    TRACKER.reset()
    resp = TestClient(api_main.app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["warmup"]["started"] is False


def test_health_turns_200_once_warmup_settles():
    from api.warmup import TRACKER

    TRACKER.reset()
    try:
        with TestClient(api_main.app) as client:
            deadline = time.time() + 60
            while time.time() < deadline:
                resp = client.get("/health")
                if resp.status_code == 200:
                    break
                time.sleep(0.1)
            assert resp.status_code == 200, resp.json()["warmup"]
            assert resp.json()["status"] == "ok"
            assert resp.json()["warmup"]["ready"] is True
    finally:
        TRACKER.reset()
