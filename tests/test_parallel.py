"""R40：`core/parallel.py` 的判据。**并行最容易出的 bug 不是慢，是结果变了。**

RRF 融合对输入顺序敏感：三路检索并行之后按完成顺序收结果，融合结果就变——
那不是"更快"，是"不一样"。而这种 bug 在单测里看不出来（每一路各自都对），
只有把顺序钉死才防得住。

另一条同样隐蔽：`ThreadPoolExecutor` **不复制 ContextVar**。这个项目里
`use_llm(backend)`（这次用哪个后端、BYOK 的 key）就挂在 ContextVar 上——
不带过去的话工作线程拿到默认后端，不报错，只是结果来自另一个模型。
"""
from __future__ import annotations

import threading
import time
from contextvars import ContextVar

import pytest

from core.parallel import parallel_off, run_indexed, run_routes, worker_count


def test_results_come_back_by_key_not_by_completion_order():
    """先提交的那一路故意最慢。按完成顺序收就会把键错位。"""
    order: list[str] = []

    def slow():
        time.sleep(0.08)
        order.append("slow")
        return "A"

    def fast():
        order.append("fast")
        return "B"

    out = run_routes([("dense", slow), ("bm25", fast)])
    assert out == {"dense": "A", "bm25": "B"}
    assert order == ["fast", "slow"], "这条测试的前提（快的先完成）没成立"
    assert list(out) == ["dense", "bm25"], "返回的键序必须是调用方给的顺序"


def test_tasks_really_run_at_the_same_time():
    """不是"包了个线程池就算并行"：两路各睡 60ms，总墙钟必须明显小于 120ms。"""
    def sleeper():
        time.sleep(0.06)
        return 1

    t0 = time.perf_counter()
    run_routes([("a", sleeper), ("b", sleeper)])
    assert (time.perf_counter() - t0) < 0.11


def test_duplicate_keys_are_rejected():
    """重复的键意味着调用方把两件活当成一件，结果会被静默覆盖。"""
    with pytest.raises(ValueError) as e:
        run_routes([("dense", lambda: 1), ("dense", lambda: 2)])
    assert "键重复" in str(e.value)


def test_one_route_failing_raises_after_the_others_finish():
    """不留跑了一半的线程，也不把半份结果交给调用方——半份结果在辨证链上
    会被当成完整的用。"""
    done: list[str] = []

    def ok():
        time.sleep(0.03)
        done.append("ok")
        return 1

    def boom():
        raise RuntimeError("这一路炸了")

    with pytest.raises(RuntimeError):
        run_routes([("a", boom), ("b", ok)])
    assert done == ["ok"], "另一路没被等完"


def test_the_raised_error_is_the_first_by_task_order_not_by_timing():
    """同一份输入必须给出同一个错误。"最先抛出"取决于线程调度。"""
    def slow_boom():
        time.sleep(0.05)
        raise ValueError("我排在前面")

    def fast_boom():
        raise KeyError("我先抛")

    with pytest.raises(ValueError):
        run_routes([("first", slow_boom), ("second", fast_boom)])


def test_a_single_task_runs_inline():
    """开池的代价比活本身还大。也顺便保证单路时异常路径不变（直接冒出来）。"""
    names: list[str] = []
    run_routes([("only", lambda: names.append(threading.current_thread().name))])
    assert names == [threading.main_thread().name]


def test_contextvars_cross_into_the_worker_threads():
    """**这条是这个模块存在的第二个理由。** ContextVar 不带过去 = BYOK 静默失效。"""
    var: ContextVar[str] = ContextVar("probe", default="默认后端")
    var.set("这次问诊指定的后端")
    out = run_routes([("a", var.get), ("b", var.get)])
    assert out == {"a": "这次问诊指定的后端", "b": "这次问诊指定的后端"}


def test_enabled_false_runs_serially_and_gives_the_same_answer():
    """对照实测要能一键关掉，而关掉之后**答案必须一样**。"""
    calls: list[str] = []

    def a():
        calls.append("a")
        return 1

    def b():
        calls.append("b")
        return 2

    assert run_routes([("a", a), ("b", b)], enabled=False) == {"a": 1, "b": 2}
    assert calls == ["a", "b"], "串行时的执行顺序必须是声明顺序"


def test_the_env_switch_turns_everything_serial(monkeypatch):
    monkeypatch.setenv("PARALLEL_OFF", "1")
    assert parallel_off() is True
    names: list[str] = []
    run_routes([("a", lambda: names.append(threading.current_thread().name)),
                ("b", lambda: names.append(threading.current_thread().name))])
    assert set(names) == {threading.main_thread().name}


def test_env_switch_is_off_by_default(monkeypatch):
    monkeypatch.delenv("PARALLEL_OFF", raising=False)
    assert parallel_off() is False


# ---------- run_indexed：同一件活的 N 份 ----------


def test_run_indexed_keeps_the_input_alignment():
    """结果必须跟 items 一一对齐——`consult_many` 的返回值是按下标读的。"""
    def fn(x):
        time.sleep(0.02 if x == 0 else 0.0)   # 第 0 条最慢
        return x * 10

    assert run_indexed([0, 1, 2, 3], fn, workers=4) == [0, 10, 20, 30]


def test_run_indexed_leaves_none_where_it_failed_and_does_not_raise():
    """一条主诉失败不能把整批已经花钱跑完的结果一起丢掉。"""
    errs: list[tuple[int, str]] = []

    def fn(x):
        if x == 1:
            raise RuntimeError("第 2 条挂了")
        return x

    out = run_indexed([0, 1, 2], fn, workers=3,
                      on_error=lambda i, e: errs.append((i, type(e).__name__)))
    assert out == [0, None, 2]
    assert errs == [(1, "RuntimeError")]


def test_run_indexed_callbacks_run_on_the_main_thread():
    """进度条只在主线程动，所以回调本身不需要加锁。"""
    seen: list[str] = []
    run_indexed([1, 2, 3], lambda x: x, workers=3,
                on_done=lambda i, r: seen.append(threading.current_thread().name))
    assert set(seen) == {threading.main_thread().name}


def test_run_indexed_serial_path_matches_the_concurrent_one():
    def fn(x):
        if x == 1:
            raise ValueError("boom")
        return x

    errs_p: list[int] = []
    errs_s: list[int] = []
    par = run_indexed([0, 1, 2], fn, workers=3, on_error=lambda i, e: errs_p.append(i))
    ser = run_indexed([0, 1, 2], fn, workers=1, on_error=lambda i, e: errs_s.append(i))
    assert par == ser == [0, None, 2]
    assert errs_p == errs_s == [1]


def test_run_indexed_on_an_empty_list_does_nothing():
    assert run_indexed([], lambda x: x, workers=4) == []


def test_run_indexed_carries_contextvars_too():
    var: ContextVar[str] = ContextVar("probe2", default="默认")
    var.set("指定的")
    assert run_indexed([1, 2], lambda _x: var.get(), workers=2) == ["指定的", "指定的"]


def test_worker_count_reads_the_env_and_never_returns_zero(monkeypatch):
    """0 或负数一律当 1（串行），不当成"无限"——无限并发在分钟级的 LLM 调用上
    等于一次把几十条请求全打出去，对方限流之后整批一起失败。"""
    monkeypatch.delenv("X_WORKERS", raising=False)
    assert worker_count("X_WORKERS", 4) == 4
    monkeypatch.setenv("X_WORKERS", "8")
    assert worker_count("X_WORKERS", 4) == 8
    monkeypatch.setenv("X_WORKERS", "0")
    assert worker_count("X_WORKERS", 4) == 1
    monkeypatch.setenv("X_WORKERS", "-3")
    assert worker_count("X_WORKERS", 4) == 1
    monkeypatch.setenv("X_WORKERS", "不是数字")
    assert worker_count("X_WORKERS", 4) == 4
