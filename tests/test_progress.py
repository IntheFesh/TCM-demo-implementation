"""R9-2：统一进度显示（core/progress.py）。零 LLM 调用、零第三方依赖。

四组：格式（速度/时长/进度条）、TTY 原地刷新、非 TTY 降级（不刷屏）、心跳
（**一项都没完成也要出声**——这是"静默和卡死长得一样"那个问题的解药）。
最后一组钉住七个调用方真的接上了。
"""
from __future__ import annotations

import io
import re
import time
from pathlib import Path

import pytest

from core.progress import (
    HEARTBEAT_SECONDS,
    NON_TTY_INTERVAL_SECONDS,
    Progress,
    format_duration,
    format_rate,
)

ROOT = Path(__file__).resolve().parent.parent


class FakeStream(io.StringIO):
    """可以谎报 isatty 的流。真终端在 pytest 下拿不到，所以两种模式都靠它测。"""

    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


class Clock:
    """可控时钟：速度和剩余时间的断言不能依赖真实耗时。"""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _bar(tty: bool, total=10, clock=None, **kw) -> tuple[Progress, FakeStream]:
    stream = FakeStream(tty)
    bar = Progress(total=total, label="任务", unit="块", stream=stream,
                   now=clock or time.monotonic, heartbeat_seconds=0, **kw)
    return bar, stream


# ---------- 一、格式 ----------


@pytest.mark.parametrize("seconds,expected", [
    (0, "00:00"), (59, "00:59"), (60, "01:00"), (1014, "16:54"), (3723, "1:02:03"),
    (-5, "00:00"),                     # 负数（时钟回拨）不该打出 "-1:59"
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_rate_picks_the_more_readable_unit():
    # 每分钟过 1 项以上 → 项/分
    assert format_rate(done=208, elapsed=1014, unit="块") == "12.3 块/分"
    # 慢于 1 项/分 → 秒/项（"0.4 块/分"不如"148 秒/块"直观）
    assert format_rate(done=2, elapsed=296, unit="块") == "148.0 秒/块"
    # 还没有完成量 / 还没有时间：不编一个数
    assert format_rate(done=0, elapsed=10, unit="块") == "-- 块/分"
    assert format_rate(done=5, elapsed=0, unit="块") == "-- 块/分"


def test_line_has_percent_bar_counts_rate_elapsed_and_eta():
    """用户点名要的五样东西：百分比 + 进度条、当前/总数、速度、已用、预计剩余。"""
    clock = Clock()
    bar, stream = _bar(tty=False, total=457, clock=clock)
    clock.advance(1014)
    bar.advance(208)
    line = stream.getvalue().strip()
    assert "45%" in line
    assert "█" in line and "░" in line
    assert "208/457" in line
    assert "12.3 块/分" in line
    assert "已用 16:54" in line
    # 剩余 249 块 ÷ (208/1014 块每秒) ≈ 1214 秒 ≈ 20:14
    assert re.search(r"剩余约 20:1\d", line), line


def test_no_total_means_no_percent_and_no_eta():
    """总数未知时只报已完成数和速度，不编一个百分比。"""
    clock = Clock()
    stream = FakeStream(tty=False)
    bar = Progress(total=None, label="任务", unit="条", stream=stream,
                   now=clock, heartbeat_seconds=0)
    clock.advance(60)
    bar.advance()
    line = stream.getvalue()
    assert "1 条" in line and "%" not in line and "剩余" not in line


def test_overflowing_the_estimate_is_shown_not_hidden():
    """record_fixtures 的 estimated_calls 是量级估算，撞上了要能看出来。"""
    clock = Clock()
    bar, stream = _bar(tty=False, total=5, clock=clock)
    clock.advance(10)
    bar.advance(7)
    line = stream.getvalue()
    assert "7/5" in line and "超出预估 2" in line
    assert "100%" in line, "百分比封在 100%，不打 140%"


# ---------- 二、TTY：原地刷新 ----------


def test_tty_uses_carriage_return_and_does_not_pile_up_lines():
    clock = Clock()
    bar, stream = _bar(tty=True, total=10, clock=clock)
    for _ in range(10):
        clock.advance(1)          # 超过 TTY_MIN_REDRAW_SECONDS，每次都重绘
        bar.advance()
    out = stream.getvalue()
    assert out.count("\r") == 10
    assert out.count("\n") == 0, "TTY 下进度不该换行堆积"


def test_tty_throttles_redraw_for_fast_tasks():
    """一秒刷几百次会把终端刷爆：两次重绘之间至少隔 TTY_MIN_REDRAW_SECONDS。
    最后一项例外——它必须画出来，不然屏幕上停在 99%。"""
    clock = Clock()
    bar, stream = _bar(tty=True, total=100, clock=clock)
    for _ in range(99):
        bar.advance()             # 时钟不走，全被节流
    assert stream.getvalue().count("\r") == 1, "第一次画了，后面被节流"
    bar.advance()                 # 第 100 项 = 最后一项，必画
    assert stream.getvalue().count("\r") == 2


def test_note_clears_the_bar_line_before_printing_in_tty():
    """note/心跳/收尾都是整行输出，TTY 下要先擦掉进度条那一行，免得打成一团。"""
    bar, stream = _bar(tty=True, total=10)
    bar.advance()
    bar.note("第 3 块调用失败")
    out = stream.getvalue()
    assert "\r\033[K第 3 块调用失败\n" in out


# ---------- 三、非 TTY：降级而不是关掉 ----------


def test_non_tty_prints_whole_lines_and_does_not_spam():
    """`| tee` / nohup / CI：每 interval 秒或每 N 项一整行。1000 项不该打 1000 行。"""
    clock = Clock()
    stream = FakeStream(tty=False)
    bar = Progress(total=1000, label="任务", unit="块", stream=stream,
                   now=clock, heartbeat_seconds=0)
    for _ in range(1000):
        bar.advance()             # 时钟不走：只靠"每 N 项"这条触发
    out = stream.getvalue()
    assert "\r" not in out, "非 TTY 不该有 \\r"
    n_lines = out.count("\n")
    # total//20 = 50 项一行 → 20 行左右，远小于 1000
    assert 15 <= n_lines <= 25, n_lines


def test_non_tty_also_emits_on_the_time_interval():
    """慢任务（每项几十秒）不能等到"每 N 项"才出声。"""
    clock = Clock()
    stream = FakeStream(tty=False)
    bar = Progress(total=1000, label="任务", unit="块", stream=stream,
                   now=clock, heartbeat_seconds=0)
    bar.advance()                                  # 第一行
    clock.advance(NON_TTY_INTERVAL_SECONDS + 1)
    bar.advance()                                  # 时间到了，第二行
    assert stream.getvalue().count("\n") == 2


def test_progress_goes_to_stderr_by_default():
    """**stdout 可能是结构化输出**（--dry-run 的清单），混进进度条会破坏它。"""
    import sys

    bar = Progress(total=3, label="任务", heartbeat_seconds=0)
    assert bar.stream is sys.stderr


def test_default_stream_is_looked_up_lazily(capsys):
    """默认流要在**用的时候**取 sys.stderr：import 时取会拿到还没被 capsys
    替换的那个，测试里就看不到输出。"""
    bar = Progress(total=1, label="任务", unit="块", heartbeat_seconds=0)
    bar.advance()
    bar.close()
    captured = capsys.readouterr()
    assert "任务" in captured.err
    assert captured.out == "", "一个字都不该进 stdout"


def test_close_prints_a_final_line_with_totals_and_summary():
    clock = Clock()
    bar, stream = _bar(tty=False, total=2, clock=clock)
    clock.advance(30)
    bar.advance(2)
    bar.close("抽出 17 条")
    last = stream.getvalue().strip().splitlines()[-1]
    assert "完成" in last and "2/2" in last and "用时 00:30" in last and "抽出 17 条" in last


def test_context_manager_closes_even_on_exception():
    stream = FakeStream(tty=False)
    with pytest.raises(RuntimeError):
        with Progress(total=2, label="任务", stream=stream, heartbeat_seconds=0) as bar:
            bar.advance()
            raise RuntimeError("中途崩了")
    assert "完成" in stream.getvalue()


def test_disabled_progress_writes_nothing():
    stream = FakeStream(tty=False)
    bar = Progress(total=5, label="任务", stream=stream, enabled=False, heartbeat_seconds=0)
    bar.advance(5)
    bar.note("x")
    bar.close()
    assert stream.getvalue() == ""


# ---------- 四、心跳：一项都没完成也要出声 ----------


def test_heartbeat_fires_while_nothing_completes():
    """**这条是"静默 = 卡死"那个问题的解药。** 主线程卡在一次调用里（这里用
    sleep 模拟），后台心跳线程要报"已等待 N 秒没有进展"。用真实时钟跑，
    把间隔调到 0.2 秒，整条测试不到一秒。"""
    stream = FakeStream(tty=False)
    bar = Progress(total=23, label="录制", unit="次调用", stream=stream,
                   heartbeat_seconds=0.2)
    bar.advance(2)                     # 完成 2 项，然后"卡住"
    time.sleep(0.7)                    # 模拟一次挂住的调用
    bar.close()
    out = stream.getvalue()
    assert "⏳" in out and "已等待" in out
    assert "仍在处理第 3/23" in out, out
    assert "已完成 2" in out
    assert "超过心跳间隔还没有下一行就是真卡住了" in out


def test_heartbeat_does_not_fire_while_things_are_moving():
    stream = FakeStream(tty=False)
    bar = Progress(total=100, label="任务", unit="块", stream=stream, heartbeat_seconds=0.3)
    for _ in range(12):
        time.sleep(0.05)
        bar.advance()
    bar.close()
    assert "⏳" not in stream.getvalue()


def test_heartbeat_thread_is_a_daemon_and_is_joined_on_close():
    bar = Progress(total=5, label="任务", stream=FakeStream(tty=False), heartbeat_seconds=0.1)
    thread = bar._heartbeat_thread
    assert thread is not None and thread.daemon, "不能让它拖住进程退出"
    bar.close()
    time.sleep(0.05)
    assert not thread.is_alive()


def test_close_is_idempotent():
    stream = FakeStream(tty=False)
    bar = Progress(total=1, label="任务", stream=stream, heartbeat_seconds=0)
    bar.close()
    bar.close()
    assert stream.getvalue().count("完成") == 1


def test_default_heartbeat_interval_is_longer_than_a_normal_call():
    """30 秒的依据：单次 S3 调用 6~8 秒、ReAct 单步 2~3 秒、抽一块 10~20 秒——
    比最慢的正常一步长，比人开始怀疑卡死短。"""
    assert 20 <= HEARTBEAT_SECONDS <= 60


# ---------- 五、七个调用方真的接上了 ----------


@pytest.mark.parametrize("path,label_hint", [
    ("scripts/record_fixtures.py", "次调用"),          # 粒度到单次 LLM 调用
    ("offline/extract_case_triples.py", "条"),
    ("offline/extract_reference_triples.py", "块"),
    ("offline/estimate_epsilon.py", "轮"),
    ("eval/run_eval.py", "轮"),
    ("eval/sdt/run.py", "条"),
    ("scripts/run_pharmacology_extraction.py", "源"),
    ("core/chain.py", "条"),                           # consult_many（E1/E2 和 MES 的主干）
])
def test_every_long_running_caller_uses_the_shared_component(path, label_hint):
    src = (ROOT / path).read_text(encoding="utf-8")
    assert "from core.progress import Progress" in src, f"{path} 没接统一进度组件"
    assert f'unit="{label_hint}"' in src, f"{path} 的进度单位不是 {label_hint}"


def test_sdt_run_no_longer_has_its_own_every_ten_lines():
    """原来是 `if i % 10 == 0: print(...)`——统一成新组件后那一套要撤掉，
    不能两套并存（CLAUDE.md：同一概念只能有一处实现）。"""
    src = (ROOT / "eval" / "sdt" / "run.py").read_text(encoding="utf-8")
    assert "i % 10 == 0" not in src


def test_no_third_party_progress_dependency():
    """零额外依赖是这个项目一直的性质：requirements 里不该出现 tqdm/rich。"""
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    assert "tqdm" not in reqs and "rich" not in reqs
    src = (ROOT / "core" / "progress.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        if line.startswith(("import ", "from ")):
            assert not line.startswith(("import tqdm", "import rich", "from tqdm", "from rich"))


def test_run_eval_baseline_phase_is_no_longer_silent():
    """**用户点名最要紧的一条**：E3/E4 的 own 基线那一轮原来从头到尾没有输出，
    5 分钟静默，三次被误判成卡死、杀掉了正常进程。"""
    src = (ROOT / "eval" / "run_eval.py").read_text(encoding="utf-8")
    baseline_fn = src[src.index("def collect_refs_mode_pairs"):src.index("def ablation_output_effect")]
    assert "Progress(" in baseline_fn, "E3/E4 基线收集器没有进度条"
    assert "bar.advance(" in baseline_fn
    # own 那一轮自己也要推进，不是只在 ablated 之后才动
    assert 'own"' in baseline_fn and "bar.advance(note=f\"「{query[:12]}」own\")" in baseline_fn
