"""R40：`scripts/loadtest.py` 与 `scripts/live_server.py` 的判据。

压测脚本自己错了，它给出的每个数都是错的而且看起来正常——所以延迟分位数、
失败分类、闸门断言这三件事各自要有测试。**不起真服务、不跑真压测**
（那是上机的事），这里测的是纯函数与分类逻辑。
"""
from __future__ import annotations

import json

import pytest

from scripts.live_server import HEALTH_CHECK_SLACK_SECONDS, free_port, health_check_budget
from scripts.loadtest import _pct, one_request, run_level


# ---------- 分位数 ----------


def test_percentiles_work_on_a_single_sample():
    """**不用 statistics.quantiles**：它在 n < 2 时抛异常，而冒烟跑一两条
    的情况恰好要能出数。"""
    assert _pct([42.0], 0.5) == 42.0
    assert _pct([42.0], 0.99) == 42.0


def test_percentiles_on_an_empty_list_are_none_not_zero():
    """0 会被读成"非常快"。"没有成功的请求"必须是 None。"""
    assert _pct([], 0.5) is None


def test_percentiles_pick_the_right_order_statistic():
    """口径是**最近秩**（nearest-rank on n−1），不是插值。1..100 上
    p50 = xs[round(0.5×99)] = xs[50] = 51。把口径钉在测试里，
    否则报告里的 p95 换个实现就悄悄变了一个数。"""
    xs = [float(i) for i in range(1, 101)]
    assert _pct(xs, 0.50) == 51.0
    assert _pct(xs, 0.90) == 90.0
    assert _pct(xs, 0.95) == 95.0
    assert _pct(xs, 0.99) == 99.0
    assert _pct(xs, 1.0) == 100.0


def test_percentiles_do_not_depend_on_input_order():
    assert _pct([3.0, 1.0, 2.0], 0.5) == _pct([1.0, 2.0, 3.0], 0.5)


# ---------- 失败分类 ----------


class _Resp:
    def __init__(self, status, headers=None, text="", content=b""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.content = content


def _patch_httpx(monkeypatch, fn):
    import httpx

    monkeypatch.setattr(httpx, "post", fn)


def test_a_200_is_ok_and_carries_the_body_size(monkeypatch):
    _patch_httpx(monkeypatch, lambda *a, **k: _Resp(200, content=b"x" * 123))
    row = one_request("http://x", "胃脘胀痛", 5.0)
    assert row["kind"] == "ok" and row["bytes"] == 123 and row["ms"] >= 0


def test_a_503_is_the_concurrency_gate_not_a_server_error(monkeypatch):
    """闸门挡下来跟服务器出错是两件事。混成一类的话"闸门工作正常"会被
    当成"有 N 个 5xx"。"""
    _patch_httpx(monkeypatch,
                 lambda *a, **k: _Resp(503, headers={"Retry-After": "10"}))
    row = one_request("http://x", "胃脘胀痛", 5.0)
    assert row["kind"] == "gate_503"
    assert row["retry_after"] == "10"


def test_a_503_without_retry_after_is_reported(monkeypatch):
    """没有 Retry-After 客户端只能瞎重试，而瞎重试会把一次尖峰变成持续过载。"""
    _patch_httpx(monkeypatch, lambda *a, **k: _Resp(503))
    row = one_request("http://x", "x", 5.0)
    assert row["retry_after"] is None


def test_4xx_and_5xx_are_different_kinds(monkeypatch):
    _patch_httpx(monkeypatch, lambda *a, **k: _Resp(400, text="模式名不认识"))
    assert one_request("http://x", "x", 5.0)["kind"] == "client_error"
    _patch_httpx(monkeypatch, lambda *a, **k: _Resp(500, text="boom"))
    assert one_request("http://x", "x", 5.0)["kind"] == "server_error"


def test_a_timeout_is_its_own_kind(monkeypatch):
    """问诊是分钟级的请求。超时必须单独列，不混进延迟分布——一条超时对应的
    是一个真的失败了的患者，把它算进 p95 只会让 p95 变成一个无法解释的数。"""
    import httpx

    def boom(*a, **k):
        raise httpx.ReadTimeout("超时")

    _patch_httpx(monkeypatch, boom)
    assert one_request("http://x", "x", 0.1)["kind"] == "timeout"


def test_a_connection_failure_is_its_own_kind(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("连不上")

    _patch_httpx(monkeypatch, boom)
    row = one_request("http://x", "x", 1.0)
    assert row["kind"] == "transport" and "ConnectError" in row["error"]


# ---------- 一个并发档的汇总 ----------


def test_throughput_counts_only_successful_requests(monkeypatch):
    """把 503 算进吞吐会让"闸门把请求全挡掉"看起来像"吞吐很高"。"""
    import httpx

    seq = [_Resp(200, content=b"x"), _Resp(503, headers={"Retry-After": "1"}),
           _Resp(503, headers={"Retry-After": "1"}), _Resp(503, headers={"Retry-After": "1"})]

    def fake(*a, **k):
        return seq.pop()

    monkeypatch.setattr(httpx, "post", fake)
    row = run_level("http://x", ["a"], concurrency=1, n_requests=4, timeout=5.0)
    assert row["n_ok"] == 1
    assert row["kinds"]["gate_503"] == 3
    assert row["throughput_rps"] is not None


def test_a_level_with_no_successes_reports_none_not_zero_latency(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _Resp(503, headers={"Retry-After": "1"}))
    row = run_level("http://x", ["a"], concurrency=2, n_requests=4, timeout=5.0)
    assert row["p50_ms"] is None and row["mean_ms"] is None
    assert row["gate_503_without_retry_after"] == 0


def test_missing_retry_after_is_counted_at_the_level(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(503))
    row = run_level("http://x", ["a"], concurrency=2, n_requests=3, timeout=5.0)
    assert row["gate_503_without_retry_after"] == 3


def test_error_samples_are_capped_so_the_json_stays_readable(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "post",
                        lambda *a, **k: _Resp(500, text="boom"))
    row = run_level("http://x", ["a"], concurrency=2, n_requests=20, timeout=5.0)
    assert row["kinds"]["server_error"] == 20
    assert len(row["errors"]) <= 5
    json.dumps(row)          # 必须可序列化（要落盘）


# ---------- live_server ----------


def test_the_health_budget_tracks_the_server_warmup_ceiling(monkeypatch):
    """**必须现读** `api.main.WARMUP_TIMEOUT_SECONDS`，不能在 import 时抄一份。
    AutoDL 上就这么错位过：硬编码 60 秒 vs 服务端 120 秒上限。"""
    import api.main as api_main

    monkeypatch.setattr(api_main, "WARMUP_TIMEOUT_SECONDS", 5.0)
    assert health_check_budget() == 5.0 + HEALTH_CHECK_SLACK_SECONDS
    monkeypatch.setattr(api_main, "WARMUP_TIMEOUT_SECONDS", 150.0)
    assert health_check_budget() >= 150.0


def test_free_port_returns_something_bindable():
    import socket

    port = free_port()
    assert 1024 < port < 65536
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_the_stream_profiler_and_the_loadtest_share_one_live_server():
    """同一概念只有一处实现（CLAUDE.md 第 31 条）：起真服务这件事
    profiler、压测、`tests/test_api_stream.py` 三个消费方共用一份。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for f in ("scripts/profile_consult.py", "scripts/loadtest.py",
              "tests/test_api_stream.py"):
        src = (root / f).read_text(encoding="utf-8")
        assert "live_server" in src, f"{f} 没用共享的那一份"
        assert "uvicorn.Server(" not in src, f"{f} 自己又起了一个服务器"


@pytest.mark.parametrize("q", [0.0, 0.5, 0.9, 0.95, 0.99, 1.0])
def test_every_reported_quantile_is_within_range(q):
    xs = [1.0, 5.0, 9.0]
    assert 1.0 <= _pct(xs, q) <= 9.0
