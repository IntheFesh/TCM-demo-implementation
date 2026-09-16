"""共享额度账本（D1 第二层）。

钉住的是四件容易写错的事：
  1. 计量单位是 llm_calls，不是请求数——开 ReAct 的一次问诊要多扣
  2. 闸门在调模型之前（decide 不碰后端），被拦的请求零成本
  3. 预占 → 按真实值结算，防并发穿透又不让账本记估算
  4. 超限降级到 replay，不是报错
"""
from __future__ import annotations

from datetime import date

import pytest

from core import usage


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FORCE_REPLAY", raising=False)
    monkeypatch.delenv("QUOTA_PER_IP_DAILY_CALLS", raising=False)
    monkeypatch.delenv("QUOTA_GLOBAL_DAILY_CALLS", raising=False)
    usage.reset_ledger()
    yield
    usage.reset_ledger()


def test_estimate_counts_react_as_several_times_the_baseline():
    """按请求数计费会让开 ReAct 的人用别人三倍的钱记同一笔账。"""
    plain = usage.estimate_calls(use_react=False, n_physicians=3)
    react = usage.estimate_calls(use_react=True, n_physicians=3)
    # **有意的契约变更（R22）**：原来写死 `plain == 5`（S1 + S2 + 三位医家各一次 S3）。
    # best-of-N 之后 plain 是 `2 + 3 × N`，期望值问 calls_per_consult() 这一处——
    # estimate_calls 内部也问它，两边对上才说明预占和折算用的是同一个公式
    # （不一致的后果是账本持续少扣，而少扣不会报错）。
    assert plain == usage.calls_per_consult(3)
    assert plain == usage.estimate_calls(use_react=False, n_physicians=3, best_of_n=None)
    # N=1 时退回历史上的 5，这条钉住"公式没换，只是多了一个因子"
    assert usage.calls_per_consult(3, 1) == 5
    assert react >= plain * 1.5, "ReAct 仍然明显更贵（N 变大时倍数会被摊薄，所以不是 3 倍）"


def test_a_fresh_ip_gets_the_shared_pool():
    led = usage.UsageLedger(per_ip_limit=25, global_limit=100)
    d = led.decide("1.2.3.4", has_own_key=False)
    assert d.mode == "shared" and not d.degraded


def test_own_key_never_touches_the_quota():
    """自带 key 花的是访问者自己的钱，既不计额度也不占额度。"""
    led = usage.UsageLedger(per_ip_limit=0, global_limit=0)  # 额度全满
    d = led.decide("1.2.3.4", has_own_key=True)
    assert d.mode == "byok"
    assert led.snapshot("1.2.3.4")["ip_used_calls"] == 0


def test_over_per_ip_limit_degrades_to_replay_not_an_error():
    led = usage.UsageLedger(per_ip_limit=10, global_limit=1000)
    led.settle(led.reserve("1.2.3.4", 10), 10)
    d = led.decide("1.2.3.4", has_own_key=False)
    assert d.mode == "replay"
    assert "回放" in d.reason and "API key" in d.reason
    # 别人不受影响
    assert led.decide("9.9.9.9", has_own_key=False).mode == "shared"


def test_global_limit_blocks_everyone_including_a_fresh_ip():
    led = usage.UsageLedger(per_ip_limit=10, global_limit=10)
    led.settle(led.reserve("1.1.1.1", 10), 10)
    assert led.decide("2.2.2.2", has_own_key=False).mode == "replay"


def test_force_replay_is_a_manual_breaker_that_beats_everything(monkeypatch):
    monkeypatch.setenv("FORCE_REPLAY", "1")
    led = usage.UsageLedger(per_ip_limit=1000, global_limit=1000)
    d = led.decide("1.2.3.4", has_own_key=True)  # 连自带 key 也熔断
    assert d.mode == "replay" and "熔断" in d.reason


def test_reservation_prevents_concurrent_pass_through():
    """十个请求同时进来都看到\"还有余额\"就都放行——预占堵的是这个。"""
    led = usage.UsageLedger(per_ip_limit=10, global_limit=1000)
    tokens = [led.reserve("1.2.3.4", 5) for _ in range(2)]
    assert led.decide("1.2.3.4", has_own_key=False).mode == "replay"
    assert len(tokens) == 2


def test_settle_replaces_the_estimate_with_the_real_number():
    """账本记的必须是真实花费，不是估算。"""
    led = usage.UsageLedger(per_ip_limit=100, global_limit=1000)
    token = led.reserve("1.2.3.4", 20)     # 估算 20
    assert led.snapshot("1.2.3.4")["ip_used_calls"] == 20
    led.settle(token, 7)                   # 实际只花了 7
    assert led.snapshot("1.2.3.4")["ip_used_calls"] == 7
    assert led.snapshot("1.2.3.4")["global_used_calls"] == 7


def test_settling_zero_calls_costs_nothing():
    """安全否决走在 S2 之前、一次模型都没调，这种请求不该扣额度。"""
    led = usage.UsageLedger(per_ip_limit=100, global_limit=1000)
    led.settle(led.reserve("1.2.3.4", 5), 0)
    assert led.snapshot("1.2.3.4")["ip_used_calls"] == 0


def test_settling_an_unknown_token_is_silent():
    """结算失败不该把一次已经成功的问诊变成错误。"""
    led = usage.UsageLedger()
    led.settle(999999, 5)  # 不抛


def test_the_ledger_rolls_over_at_midnight():
    days = [date(2026, 9, 15)]
    led = usage.UsageLedger(per_ip_limit=10, global_limit=100, today_fn=lambda: days[0])
    led.settle(led.reserve("1.2.3.4", 10), 10)
    assert led.decide("1.2.3.4", has_own_key=False).mode == "replay"
    days[0] = date(2026, 9, 16)
    assert led.decide("1.2.3.4", has_own_key=False).mode == "shared"
    assert led.snapshot("1.2.3.4")["ip_used_calls"] == 0


def test_snapshot_converts_calls_into_something_a_visitor_can_read():
    """llm_calls 对访问者没有意义，折算系数只有 core/usage.py 一处定义。"""
    led = usage.UsageLedger(per_ip_limit=25, global_limit=1000)
    snap = led.snapshot("1.2.3.4")
    # R22：CALLS_PER_CONSULT 从常量变成 calls_per_consult() 函数（它现在依赖
    # 医家数和 S3_BEST_OF_N，一个常量表达不了）。
    assert snap["remaining_consults_estimate"] == 25 // usage.calls_per_consult()
    assert snap["calls_per_consult"] == usage.calls_per_consult()
    assert snap["warn"] is False


def test_snapshot_warns_at_eighty_percent():
    led = usage.UsageLedger(per_ip_limit=10, global_limit=1000)
    led.settle(led.reserve("1.2.3.4", 8), 8)
    assert led.snapshot("1.2.3.4")["warn"] is True


def test_limits_come_from_env(monkeypatch):
    monkeypatch.setenv("QUOTA_PER_IP_DAILY_CALLS", "7")
    monkeypatch.setenv("QUOTA_GLOBAL_DAILY_CALLS", "9")
    usage.reset_ledger()
    assert usage.get_ledger().limits()["per_ip_calls"] == 7
    assert usage.get_ledger().limits()["global_calls"] == 9


def test_snapshot_says_since_when_because_the_ledger_is_in_memory():
    """账本在进程内存里、重启归零。这件事必须在数据里说出来，
    否则看板上的\"今日已用 3 次\"会被当成从零点开始算的。"""
    snap = usage.UsageLedger().snapshot("1.2.3.4")
    assert snap["since"] and snap["day"]


def test_release_keeps_the_charge_when_the_real_number_is_unknown():
    """跑起来了但拿不到 manifest（客户端中途断开）——钱已经花了，只是数不清。
    按 0 退等于\"开着流跑一半关掉就白嫖\"。"""
    led = usage.UsageLedger(per_ip_limit=100, global_limit=1000)
    token = led.reserve("1.2.3.4", 20)
    led.release(token)
    assert led.snapshot("1.2.3.4")["ip_used_calls"] == 20


def test_release_and_settle_are_not_the_same_thing():
    led = usage.UsageLedger(per_ip_limit=100, global_limit=1000)
    led.settle(led.reserve("a", 20), 0)   # 一次都没调 → 退还
    led.release(led.reserve("b", 20))     # 调了但数不清 → 记账
    assert led.snapshot("a")["ip_used_calls"] == 0
    assert led.snapshot("b")["ip_used_calls"] == 20
