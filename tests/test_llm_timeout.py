"""R9-1：LLM 调用的超时。**这组测试的判据是"不挂死"**——所以每一条都自己带
一个上限（`time.monotonic()` 差值断言），挂住了测试自己会红，不会把 CI 拖死。

R8 段 6 那次实测：客户端**已经**设了 `timeout=120`，进程还是卡了 46 分钟
（wchan=do_poll、socket 还在、最后一份 fixture 写于 46 分钟前）。原因不是"没设
超时"，是 **httpx 的 read 超时管的是单次 socket 读，不是整个响应的期限**——
中间任何一跳每隔几十秒吐一个字节，每次读都不超时，请求可以挂到天荒地老。
所以补法有两层：四个相位分别设（下面第一组），以及从外面给整次调用封一个
墙钟上限（第二组，这一层才是"重试永远不触发"那个洞真正的补法）。
"""
from __future__ import annotations

import time

import pytest
from pydantic import BaseModel

from core.batch import classify_llm_failure
from core.llm import (
    API_TIMEOUTS,
    INPROC_TIMEOUTS,
    LOCAL_SERVER_TIMEOUTS,
    CallTimeouts,
    ClaudeCLIBackend,
    LLMBackend,
    LLMCallTimeout,
    LLMError,
    OpenAICompatBackend,
    VLLMBackend,
    VLLMInProcessBackend,
)


class Out(BaseModel):
    ok: str


class _Backend(LLMBackend):
    """把 `_complete` 的行为留给每条测试指定。`TIMEOUTS` 也按需覆盖。"""

    TIMEOUTS = CallTimeouts(connect=1.0, read=1.0, write=1.0, pool=1.0, deadline=0.3)
    RETRY_BACKOFF_SECONDS = (0.0, 0.0)

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls = 0
        self.aborts = 0

    def model_name(self) -> str:
        return "fake"

    def backend_id(self) -> str:
        return "fake"

    def abort_in_flight(self) -> None:
        self.aborts += 1

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs):
        self.calls += 1
        return self.behaviour(self.calls)


# ---------- 一、四个相位分别设 ----------


def test_call_timeouts_maps_each_phase_separately_not_one_number():
    """openai SDK 收到一个 float 会把它铺给四个相位，于是"连不上"要等和
    "读不出来"一样久。这里断言四个值真的各自不同、并且原样落进 httpx.Timeout。"""
    httpx = pytest.importorskip("httpx")
    t = API_TIMEOUTS.httpx_timeout()
    assert isinstance(t, httpx.Timeout)
    assert (t.connect, t.read, t.write, t.pool) == (15.0, 120.0, 30.0, 15.0)
    assert t.connect != t.read, "连接和读的上限不该是同一个数"


def test_api_read_timeout_is_far_above_normal_and_far_below_hung():
    """值的依据：单次 S3 调用实测 6~8 秒、ReAct 单步 2~3 秒、S0 最慢 60 秒量级。
    120 秒远超正常值、远低于"挂死"。这条钉住那个区间，防止有人随手调成 600。"""
    assert 100 <= API_TIMEOUTS.read <= 180
    assert API_TIMEOUTS.deadline > API_TIMEOUTS.read, "墙钟必须比单次读长，否则读超时没机会触发"


def test_openai_client_gets_a_phased_timeout_object_and_no_sdk_retries(monkeypatch):
    """SDK 自带的静默重试必须关掉（它不计入 llm_calls，会让"重试只在基类实现
    一份"这句话不成立），传进去的必须是分相对象而不是一个 float。"""
    httpx = pytest.importorskip("httpx")
    captured: dict = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def close(self):
            captured["closed"] = True

    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT", raising=False)
    backend = OpenAICompatBackend()
    assert backend.client is not None
    assert captured["max_retries"] == 0
    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].read == API_TIMEOUTS.read
    assert not isinstance(captured["timeout"], float)
    # 墙钟超时之后要把客户端丢掉，卡住的那次读随连接池一起死
    backend.abort_in_flight()
    assert captured.get("closed") is True
    assert backend._client is None


# ---------- 二、按后端区分的默认值 ----------


@pytest.mark.parametrize("backend_cls,expected", [
    (OpenAICompatBackend, API_TIMEOUTS),
    (VLLMBackend, LOCAL_SERVER_TIMEOUTS),
    (VLLMInProcessBackend, INPROC_TIMEOUTS),
])
def test_timeouts_differ_per_backend(backend_cls, expected):
    assert backend_cls.TIMEOUTS is expected


def test_local_backends_allow_much_longer_than_the_cloud_api():
    """本地模型首次调用要加载权重 / 做预热，几分钟是正常的——拿云端那套 120 秒
    去卡它会把正常启动判成故障。进程内模式最长（权重在进程里加载）。"""
    assert API_TIMEOUTS.deadline < LOCAL_SERVER_TIMEOUTS.deadline < INPROC_TIMEOUTS.deadline
    assert INPROC_TIMEOUTS.deadline >= 1800
    # CLI 后端的墙钟要比它自己的 subprocess 超时长，好让更具体的那条错误先抛出来
    assert ClaudeCLIBackend.TIMEOUTS.deadline > ClaudeCLIBackend.DEFAULT_TIMEOUT


def test_env_override_scales_read_and_deadline_but_not_connect(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45")
    resolved = OpenAICompatBackend().timeouts()
    assert resolved.read == 45.0
    assert resolved.deadline >= 45.0 * 1.5
    assert resolved.connect == min(API_TIMEOUTS.connect, 45.0), "连接慢到 45 秒一定是网络坏了"


def test_legacy_env_name_still_works(monkeypatch):
    """README 和 .env.example 里写了一轮 LLM_TIMEOUT，不能悄悄失效。"""
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    assert OpenAICompatBackend().timeouts().read == 60.0


def test_new_env_name_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "90")
    assert OpenAICompatBackend().timeouts().read == 90.0


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_unparsable_env_falls_back_loudly_not_silently(monkeypatch, capsys, bad):
    """静默回退会让人以为自己设的值生效了。"""
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", bad)
    assert OpenAICompatBackend().timeouts() is API_TIMEOUTS
    err = capsys.readouterr().err
    assert "LLM_TIMEOUT_SECONDS" in err and "默认值" in err


# ---------- 三、墙钟兜底：挂住不返回也要能走重试 ----------


def test_a_hung_call_does_not_hang_the_process_and_goes_through_retries():
    """**这条就是 R8 段 6 那个 bug 的回归测试。** 后端 sleep(999) 模拟"请求挂住
    不返回"：HTTP 层的读超时对它完全无效（根本没有 HTTP），只有墙钟兜底能救。
    判据三条：不挂死（自己带上限）、走完 MAX_ATTEMPTS 次重试、最后抛 LLMError。"""
    backend = _Backend(lambda _n: time.sleep(999))
    t0 = time.monotonic()
    with pytest.raises(LLMError) as exc:
        backend.generate("s", "u", Out)
    elapsed = time.monotonic() - t0
    assert backend.calls == LLMBackend.MAX_ATTEMPTS == 3
    # 三次 × 0.3 秒墙钟 + 退避（测试里为 0），给足余量但远低于"挂死"
    assert elapsed < 5, f"耗时 {elapsed:.1f} 秒——墙钟兜底没生效就会挂在这里"
    assert isinstance(exc.value.__cause__, LLMCallTimeout)
    assert "墙钟上限" in str(exc.value.__cause__)


def test_timeout_is_classified_as_a_timeout_not_as_a_generic_llmerror():
    """`core/batch.py` 按 `__cause__` 的类型名归类失败原因；LLMCallTimeout 继承
    TimeoutError，所以它落在超时那一类，报告里能跟"格式错"区分开。"""
    backend = _Backend(lambda _n: time.sleep(999))
    with pytest.raises(LLMError) as exc:
        backend.generate("s", "u", Out)
    assert classify_llm_failure(exc.value) == "LLMCallTimeout"
    assert isinstance(exc.value.__cause__, TimeoutError)


def test_deadline_expiry_calls_abort_in_flight_every_time():
    """超时之后要清理挂着的连接，否则重试会排在同一个坏连接后面。"""
    backend = _Backend(lambda _n: time.sleep(999))
    with pytest.raises(LLMError):
        backend.generate("s", "u", Out)
    assert backend.aborts == LLMBackend.MAX_ATTEMPTS


def test_a_call_that_recovers_on_retry_succeeds():
    """第一次挂住、第二次正常返回 → 整体成功，不该因为第一次超时就放弃。"""
    def behaviour(n: int) -> str:
        if n == 1:
            time.sleep(999)
        return '{"ok": "好了"}'

    backend = _Backend(behaviour)
    t0 = time.monotonic()
    assert backend.generate("s", "u", Out).ok == "好了"
    assert backend.calls == 2
    assert time.monotonic() - t0 < 5


def test_a_transport_timeout_raised_by_the_client_also_retries():
    """真实路径上超时是 httpx 抛出来的（`httpx.ReadTimeout`）：它必须被
    `generate()` 的传输类 `except Exception` 接住走重试，不是直接崩出去。"""
    httpx = pytest.importorskip("httpx")
    calls: list[int] = []

    def behaviour(n: int):
        calls.append(n)
        raise httpx.ReadTimeout("读超时（测试用）")

    backend = _Backend(behaviour)
    with pytest.raises(LLMError) as exc:
        backend.generate("s", "u", Out)
    assert calls == [1, 2, 3]
    assert classify_llm_failure(exc.value) == "ReadTimeout"


def test_normal_calls_are_not_slowed_down_by_the_watchdog():
    """墙钟兜底是一个工作线程 + join，正常路径上不该有可感的开销。
    20 次调用要远快于一次墙钟（0.3 秒）。"""
    backend = _Backend(lambda _n: '{"ok": "x"}')
    t0 = time.monotonic()
    for _ in range(20):
        backend.generate("s", "u", Out)
    assert time.monotonic() - t0 < 1.0


def test_exceptions_from_the_worker_thread_keep_their_type():
    """工作线程里抛的异常要原样带回主线程——否则 classify_llm_failure 归类
    全变成"ThreadError"这类无用信息。"""
    class Weird(RuntimeError):
        pass

    def behaviour(n: int):
        raise Weird("特定类型")

    backend = _Backend(behaviour)
    with pytest.raises(LLMError) as exc:
        backend.generate("s", "u", Out)
    assert isinstance(exc.value.__cause__, Weird)
