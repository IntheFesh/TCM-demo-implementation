"""长任务的统一进度显示。**全项目只此一处实现**（CLAUDE.md「同一概念只能有
一处实现」），零第三方依赖。

    from core.progress import Progress

    with Progress(total=457, label="中药学.md", unit="块") as bar:
        for block in blocks:
            ...
            bar.advance()

输出样子（TTY）：

    中药学.md  [████████░░░░░░░░] 45%  208/457  12.3 块/分  已用 16:54  剩余约 20:12

## 为什么自己写而不是 tqdm/rich

这个项目一直是零额外依赖；而且 tqdm 在管道 / nohup 下要额外配置才不刷屏。
自己写十几行就够，还能按这个项目的实际需要定制两件事（下面两条）。

## 一、打 stderr，不打 stdout

stdout 可能是结构化输出（`--dry-run` 的清单、`collect_results` 的报告），
混进进度条会把它破坏掉。进度是**给人看的旁白**，归 stderr。

## 二、非 TTY 要降级，不是关掉

判据是 `stream.isatty()`：

  TTY（人盯着终端）    用 `\\r` 原地刷新，好看
  非 TTY（`| tee`、`nohup`、CI）  每 NON_TTY_INTERVAL_SECONDS 秒或每 N 项打**一整行**，
                       不刷出几千行 `\\r` 垃圾；行里带绝对进度，日志能直接读

## 三、心跳：静默和卡死不能长得一样

R8 那次 DeepSeek 挂死 46 分钟没被及时发现，就是因为"正在跑"和"卡住了"在屏幕上
是同一个样子（都没输出）。所以即使**一项都没完成**，超过 `heartbeat_seconds`
也要打一行「已等待 95 秒，仍在处理第 3/23 项」——由一个后台守护线程负责，
因为主线程正卡在那次调用里，它自己没机会打。

有了心跳，"超过心跳间隔还没有任何输出"就等于真卡住了，不再需要靠看文件 mtime
去猜（docs/onsite_troubleshooting.md 第 0 条据此改写）。
"""
from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

# 一项都没完成也要出声的间隔。30 秒：单次 S3 调用实测 6~8 秒、ReAct 单步 2~3 秒，
# 一次正常的调用不会超过它；抽取一块最慢也在 20 秒量级。取 30 秒是"比最慢的
# 正常一步长，比人开始怀疑卡死短"。
HEARTBEAT_SECONDS = 30.0
# 非 TTY 下两行之间至少隔这么久。15 秒：一小时的任务最多 240 行，日志能翻。
NON_TTY_INTERVAL_SECONDS = 15.0
# TTY 下两次重绘之间至少隔这么久，免得一秒刷几百次（快任务）把终端刷爆。
TTY_MIN_REDRAW_SECONDS = 0.2
BAR_WIDTH = 16
FILLED, EMPTY = "█", "░"


def format_duration(seconds: float) -> str:
    """16:54 / 1:02:03。不带单位后缀——进度行里前面有「已用」「剩余约」。"""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_rate(done: int, elapsed: float, unit: str) -> str:
    """取更易读的那个单位：每分钟能过 1 项以上就报「项/分」，否则报「秒/项」。
    一块药理层抽取 10~20 秒（3~6 块/分）、一条主诉两轮消融 40~90 秒
    （0.7~1.5 条/分）——两边都落在"项/分"里还算能读；SDT 那种几秒一条也一样。
    真正需要「秒/项」的是更慢的场景（本地模型首次加载、E9 全套），那时候
    "0.4 项/分"不如"148 秒/项"直观。"""
    if done <= 0 or elapsed <= 0:
        return f"-- {unit}/分"
    per_minute = done / elapsed * 60
    if per_minute >= 1:
        return f"{per_minute:.1f} {unit}/分"
    return f"{elapsed / done:.1f} 秒/{unit}"


class Progress:
    """一个长任务的进度。**线程安全**（心跳线程和主线程都会写 stream）。

    total=None 表示总数未知（只报已完成数和速度，不报百分比和剩余）。
    done 超过 total 时不崩、不撒谎：百分比封在 100%，后面挂一句「超出预估 N」
    ——record_fixtures 的 estimated_calls 本来就是量级估算，撞上了要能看出来。
    """

    def __init__(
        self,
        total: int | None,
        label: str,
        unit: str = "项",
        stream: TextIO | None = None,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        interval_seconds: float = NON_TTY_INTERVAL_SECONDS,
        now: Callable[[], float] = time.monotonic,
        enabled: bool = True,
    ) -> None:
        # stream 默认在**用的时候**取 sys.stderr，不在 import 时取：pytest 的
        # capsys 是替换 sys.stderr 对象，import 时取会拿到还没被替换的那个。
        self._stream = stream
        self.total = total
        self.label = label
        self.unit = unit
        self.done = 0
        self._now = now
        self._t0 = now()
        self._last_output = self._t0
        self._last_emit_done = 0
        self._interval = interval_seconds
        self._enabled = enabled
        self._lock = threading.Lock()
        self._bar_on_screen = False
        self._closed = False
        self._stop = threading.Event()
        self._heartbeat_seconds = heartbeat_seconds
        self._heartbeat_thread: threading.Thread | None = None
        if enabled and heartbeat_seconds > 0:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"progress-heartbeat-{label}", daemon=True)
            self._heartbeat_thread.start()

    # ---- 输出目标 ----

    @property
    def stream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stderr

    def _isatty(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except Exception:  # noqa: BLE001 - 被重定向到没有 isatty 的对象时按非 TTY 处理
            return False

    # ---- 对外 ----

    def advance(self, n: int = 1, note: str | None = None) -> None:
        """完成了 n 项。note 是这一项的一句话（当前处理的块号/主诉），跟在进度后面。"""
        with self._lock:
            self.done += n
            self._maybe_render(note)

    def note(self, text: str) -> None:
        """一行不属于进度条的话（"第 3 块调用失败，已记下"）。TTY 下先擦掉进度条
        再打，免得两者在同一行上打成一团。"""
        with self._lock:
            self._write_line(text)

    def close(self, summary: str | None = None) -> None:
        """收尾：停掉心跳线程，打一行最终状态（TTY 下把进度条那一行定下来）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
        with self._lock:
            elapsed = self._now() - self._t0
            tail = f"　{summary}" if summary else ""
            self._write_line(
                f"{self.label} 完成　{self.done}{('/' + str(self.total)) if self.total else ''} "
                f"{self.unit}　用时 {format_duration(elapsed)}　"
                f"{format_rate(self.done, elapsed, self.unit)}{tail}")

    def __enter__(self) -> Progress:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- 内部 ----

    def _maybe_render(self, note: str | None) -> None:
        if not self._enabled:
            return
        now = self._now()
        if not self._must_render():
            if self._isatty():
                if now - self._last_output < TTY_MIN_REDRAW_SECONDS:
                    return
            else:
                # 非 TTY：每 interval 秒或每 N 项一整行，谁先到算谁
                every_n = max(1, (self.total // 20) if self.total else 25)
                if (now - self._last_output < self._interval
                        and self.done - self._last_emit_done < every_n):
                    return
        self._emit(self._line(note))

    def _must_render(self) -> bool:
        """两种情况一定要画，不受节流限制。

        **第一项**——不然节流会把开头吞掉：非 TTY 下"每 50 项一行"意味着前 49 项
        一声不响，而"任务开始了"恰恰是最想第一时间看到的；TTY 下同理（第一次
        advance 距 _t0 不足 0.2 秒）。这个吞掉还有个副作用：屏幕上会先出现心跳
        而没有进度条，看起来像是一开始就卡住了。
        **最后一项**——不然屏幕上永远停在 99%。
        """
        return self._last_emit_done == 0 or self._is_last_item()

    def _is_last_item(self) -> bool:
        return self.total is not None and self.done >= self.total

    def _line(self, note: str | None = None) -> str:
        elapsed = self._now() - self._t0
        parts = [self.label] if self.label else []
        if self.total:
            pct = min(100, int(self.done / self.total * 100))
            filled = min(BAR_WIDTH, round(self.done / self.total * BAR_WIDTH))
            parts.append(f"[{FILLED * filled}{EMPTY * (BAR_WIDTH - filled)}] {pct:3d}%")
            parts.append(f"{self.done}/{self.total}")
        else:
            parts.append(f"{self.done} {self.unit}")
        parts.append(format_rate(self.done, elapsed, self.unit))
        parts.append(f"已用 {format_duration(elapsed)}")
        if self.total and self.done:
            remaining = self.total - self.done
            if remaining > 0:
                eta = elapsed / self.done * remaining
                parts.append(f"剩余约 {format_duration(eta)}")
        if self.total and self.done > self.total:
            parts.append(f"超出预估 {self.done - self.total}")
        if note:
            parts.append(note)
        return "　".join(parts)

    def _emit(self, line: str) -> None:
        """写一次进度。TTY 用 \\r 原地刷新，非 TTY 打整行。"""
        stream = self.stream
        if self._isatty():
            stream.write("\r\033[K" + line)
            self._bar_on_screen = True
        else:
            stream.write(line + "\n")
        try:
            stream.flush()
        except Exception:  # noqa: BLE001 - 关闭的流不该让任务本身崩掉
            pass
        self._last_output = self._now()
        self._last_emit_done = self.done

    def _write_line(self, line: str) -> None:
        """一行独立输出（note / 心跳 / 收尾）：TTY 下先擦掉进度条那一行。"""
        if not self._enabled:
            return
        stream = self.stream
        if self._isatty() and self._bar_on_screen:
            stream.write("\r\033[K")
            self._bar_on_screen = False
        stream.write(line + "\n")
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
        self._last_output = self._now()

    def _heartbeat_loop(self) -> None:
        """一项都没完成也要出声——主线程这会儿正卡在那次调用里，只能后台线程打。
        轮询间隔取心跳间隔的 1/3（上限 1 秒），这样"超时多久才出声"的误差不超过
        心跳间隔的三分之一。"""
        poll = min(1.0, max(0.05, self._heartbeat_seconds / 3))
        while not self._stop.wait(poll):
            with self._lock:
                if self._closed:
                    return
                silent = self._now() - self._last_output
                if silent < self._heartbeat_seconds:
                    continue
                position = f"第 {self.done + 1}"
                if self.total:
                    position += f"/{self.total}"
                self._write_line(
                    f"⏳ {self.label}　已等待 {int(silent)} 秒没有进展，仍在处理{position} "
                    f"{self.unit}（已完成 {self.done}，用时 "
                    f"{format_duration(self._now() - self._t0)}）"
                    "——**超过心跳间隔还没有下一行就是真卡住了**")
