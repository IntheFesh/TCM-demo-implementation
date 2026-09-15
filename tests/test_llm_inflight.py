"""R12-B：进程内同时在途的 LLM 调用数闸门 + 429 的指数退避。

`MAX_CONCURRENT_CONSULTS`（同时几次问诊）和 `LLM_MAX_INFLIGHT`（同时几个请求打到
模型）是两个闸，不是一个。R12 三位医家改并发之后两者会相乘：4 个问诊槽 × 3 位医家
= 12 路同时打 API，稳稳撞上 DeepSeek 的速率限制。
"""
import threading
import time

import pytest
from pydantic import BaseModel, Field

from core import llm as llm_mod
from core.llm import LLMBackend


class Out(BaseModel):
    ok: bool
    note: str = Field(min_length=1)


class _Counting(LLMBackend):
    """记录同时在途峰值的假后端。"""

    RETRY_BACKOFF_SECONDS = (0.0, 0.0)

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.peak = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def model_name(self) -> str:
        return "fake"

    def backend_id(self) -> str:
        return "fake"

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs) -> str:
        with self._lock:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self._in_flight -= 1
        return '{"ok": true, "note": "n"}'


@pytest.fixture(autouse=True)
def _reset_semaphore(monkeypatch):
    """信号量是模块级的。每条测试前后都清掉，不然上一条设的上限会跟着走。"""
    monkeypatch.setattr(llm_mod, "_inflight_sem", None)
    monkeypatch.setattr(llm_mod, "_inflight_limit", None)
    yield
    llm_mod._inflight_sem = None
    llm_mod._inflight_limit = None


def _fan_out(backend, n: int) -> None:
    threads = [threading.Thread(target=backend.generate,
                               kwargs={"system": "s", "user": "u", "schema": Out})
               for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)


def test_inflight_limit_caps_concurrent_calls(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    b = _Counting(delay=0.05)
    _fan_out(b, 8)
    assert b.peak <= 2, f"同时在途冲到了 {b.peak}，闸没生效"


def test_without_the_limit_calls_really_do_pile_up(monkeypatch):
    """对照组：上限放大之后峰值确实上去了——不做这条的话，"峰值 ≤ 2"有可能只是
    因为这些调用本来就没并发起来，闸是不是生效根本没被验证。"""
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "8")
    b = _Counting(delay=0.05)
    _fan_out(b, 8)
    assert b.peak > 2, f"放宽到 8 之后峰值只有 {b.peak}，这个测法本身没测到并发"


def test_limit_reads_the_env_var_and_falls_back_loudly(monkeypatch, capsys):
    monkeypatch.delenv("LLM_MAX_INFLIGHT", raising=False)
    assert llm_mod.llm_max_inflight() == llm_mod.DEFAULT_MAX_INFLIGHT
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "3")
    assert llm_mod.llm_max_inflight() == 3
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "不是数字")
    assert llm_mod.llm_max_inflight() == llm_mod.DEFAULT_MAX_INFLIGHT
    assert "LLM_MAX_INFLIGHT" in capsys.readouterr().err, "回退到默认值必须吼一声"


def test_a_hung_call_does_not_hold_its_permit_forever(monkeypatch):
    """**第一版把闸放在工作线程里，这条会挂。** 墙钟超时之后工作线程被丢下不管
    （daemon），它手里那个许可永远不还——攒够 LLM_MAX_INFLIGHT 次挂死，整个进程的
    LLM 调用全部死锁。现在许可在主线程取、join 一返回就还。"""
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "1")

    class Hanging(LLMBackend):
        MAX_ATTEMPTS = 1
        TIMEOUTS = llm_mod.CallTimeouts(connect=1, read=1, write=1, pool=1, deadline=0.05)

        def model_name(self):
            return "fake"

        def backend_id(self):
            return "fake"

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kwargs):
            time.sleep(30)  # 永远不返回，模拟"细水长流地吐字节"
            return "{}"

    with pytest.raises(llm_mod.LLMError):
        Hanging().generate(system="s", user="u", schema=Out)
    # 挂死一次之后，正常调用必须还能拿到许可
    ok = _Counting(delay=0.0)
    ok.generate(system="s", user="u", schema=Out)
    assert ok.peak == 1


def test_rate_limited_retries_are_counted_into_the_manifest_stats(monkeypatch):
    """429 要单独计数：它的处置是"降并发"，跟超时（查网络）、格式错（改 prompt）
    完全不同，合成一个总数就没法处置。"""
    class RateLimited(LLMBackend):
        RETRY_BACKOFF_SECONDS = (0.0, 0.0)

        def __init__(self):
            self.n = 0

        def model_name(self):
            return "fake"

        def backend_id(self):
            return "fake"

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kwargs):
            self.n += 1
            if self.n < 3:
                err = RuntimeError("Too Many Requests")
                err.status_code = 429
                raise err
            return '{"ok": true, "note": "n"}'

    stats = llm_mod.new_retry_stats()
    RateLimited().generate(system="s", user="u", schema=Out)
    assert stats["total"] == 2 and stats["rate_limited"] == 2
    assert stats["timeout"] == 0 and stats["other"] == 0
    assert llm_mod.current_retry_stats() == stats


def test_retry_stats_are_none_when_nobody_opened_a_collection():
    """离线脚本直接调 run_physician 之类的路径没开统计——那时 manifest 里该是
    None（"没统计"），不是 0（"统计了，没发生"）。这两件事必须分得开。"""
    import contextvars

    def read_in_fresh_context():
        return llm_mod.current_retry_stats()

    assert contextvars.Context().run(read_in_fresh_context) is None
