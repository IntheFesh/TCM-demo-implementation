"""R21：峰谷分时。

DeepSeek 2026-08-17 起按峰谷计价，高峰 UTC 01–04 与 06–10（= 北京 09–12 与 14–18），
其余时段五折。**只记不改额度**：让额度跟着时段变会让"今天还能问几次"每隔几小时
跳一次，没人看得懂。
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.usage import PEAK_UTC_HOUR_RANGES, UsageLedger, is_peak, peak_note

ROOT = Path(__file__).resolve().parent.parent


def _utc(hour, minute=0):
    return datetime(2026, 9, 15, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour,expected", [
    (0, False), (1, True), (2, True), (3, True), (4, False), (5, False),
    (6, True), (7, True), (8, True), (9, True), (10, False), (23, False),
])
def test_peak_ranges_are_half_open(hour, expected):
    """半开区间：01:00–04:00 含 01/02/03，不含 04。写成半开是为了让两个区间
    之间不会有一个既算高峰又算低谷的整点。"""
    assert is_peak(_utc(hour)) is expected


def test_the_ranges_match_beijing_0912_and_1418():
    assert PEAK_UTC_HOUR_RANGES == ((1, 4), (6, 10))
    # 北京 = UTC+8：UTC 01 → 北京 09；UTC 06 → 北京 14
    bj = timezone(timedelta(hours=8))
    assert _utc(1).astimezone(bj).hour == 9
    assert _utc(6).astimezone(bj).hour == 14


def test_peak_note_says_both_cases():
    """不是只在高峰时才提醒——"现在没提醒"跟"现在是五折"是两件事。"""
    assert "高峰时段" in peak_note(_utc(2))
    assert "五折" in peak_note(_utc(12))
    # 两句都带北京时间，好让人核对
    assert "北京时间" in peak_note(_utc(2)) and "北京时间" in peak_note(_utc(12))


def test_the_ledger_records_peak_calls_without_changing_the_quota():
    ledger = UsageLedger(per_ip_limit=100, global_limit=100)
    before = ledger.snapshot("1.1.1.1")
    ledger.record_tokens(None, calls=5, now=_utc(2))
    after = ledger.snapshot("1.1.1.1")
    assert after["peak_calls_today"] == 5
    # 额度一个数都没动
    assert after["ip_used_calls"] == before["ip_used_calls"]
    assert after["remaining_calls"] == before["remaining_calls"]


def test_off_peak_calls_are_not_counted_as_peak():
    ledger = UsageLedger()
    ledger.record_tokens(None, calls=7, now=_utc(12))
    assert ledger.snapshot("1.1.1.1")["peak_calls_today"] == 0


def test_tokens_today_keeps_hit_and_miss_apart():
    """命中和未命中差 30 倍价钱，合成一个"总 token"就看不出这次便不便宜。"""
    ledger = UsageLedger()
    ledger.record_tokens({"prompt_cache_hit_tokens": 1800,
                          "prompt_cache_miss_tokens": 200,
                          "completion_tokens": 120})
    snap = ledger.snapshot("1.1.1.1")["tokens_today"]
    assert snap["cache_hit"] == 1800
    assert snap["cache_miss"] == 200
    assert snap["output"] == 120
    assert snap["cache_hit_ratio"] == 0.9


def test_tokens_today_ratio_is_none_before_anything_is_reported():
    assert UsageLedger().snapshot("1.1.1.1")["tokens_today"]["cache_hit_ratio"] is None


def test_a_backend_that_reports_nothing_does_not_add_zeros():
    """加 0 和"没报"在 snapshot 里长得一样，而它们要分得开。"""
    ledger = UsageLedger()
    ledger.record_tokens(None, calls=3, now=_utc(2))
    snap = ledger.snapshot("1.1.1.1")["tokens_today"]
    assert snap["cache_hit"] == 0 and snap["cache_hit_ratio"] is None


def test_tokens_reset_when_the_day_rolls():
    day = {"v": __import__("datetime").date(2026, 9, 15)}
    ledger = UsageLedger(today_fn=lambda: day["v"])
    ledger.record_tokens({"prompt_cache_hit_tokens": 100, "prompt_cache_miss_tokens": 10})
    assert ledger.snapshot("1.1.1.1")["tokens_today"]["cache_hit"] == 100
    day["v"] = __import__("datetime").date(2026, 9, 16)
    assert ledger.snapshot("1.1.1.1")["tokens_today"]["cache_hit"] == 0


# ---------- 上机剧本 ----------

def test_the_runbook_prints_the_peak_note_first():
    out = subprocess.run(["bash", "scripts/run_onsite.sh", "--dry-run"], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    first = out.stdout.strip().splitlines()[0]
    assert "北京时间" in first
    assert ("高峰时段" in first) or ("五折" in first)


def test_the_runbook_does_not_compute_the_timezone_itself():
    """判定只有一处实现（core/usage.py）。bash 那边自己算时区的话，
    夏令时/UTC 偏移会有一处弄错，而弄错的表现是"按五折估的预算，实际按原价扣"。"""
    src = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    body = src[src.index("peak_note() {"):src.index("print_plan() {")]
    assert "from core.usage import" in body
    for forbidden in ("TZ=", "date -u +%H", "+0800"):
        assert forbidden not in body, f"剧本自己算时区了：{forbidden}"


def test_only_the_costly_segments_get_the_warning():
    """段 0/1 零调用，提醒它们只是噪声；提醒多了就没人看了。

    **有意的契约变更（R25）**：期望值从 5/6/7/8 变成 5/6/7/8/9。第 9 段
    （R21~R24 的上机项）估 320 次真实调用，是全剧本里第二贵的一段，
    不提醒它等于这条判据漏了一个应该提醒的段。这里必须改期望值、
    不能改成"只要 5 在里面就算过"——后者会让这条测试从此再也发现不了
    "某段花钱但没被登记进 COSTLY_SEGMENTS"这类漏登记。
    """
    src = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if ln.startswith("COSTLY_SEGMENTS="))
    assert line.split("=", 1)[1].strip().strip('"').split() == ["5", "6", "7", "8", "9"]


def test_the_warning_does_not_block():
    """有时就是得现在跑。"""
    src = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    body = src[src.index("warn_if_peak() {"):src.index("print_plan() {")]
    assert "不拦你" in body
    assert "read " not in body, "提醒不许变成一个要人回车的卡点"
