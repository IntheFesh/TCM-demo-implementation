"""共享额度账本（D1 的第二层）。

**为什么按 `llm_calls` 计量而不是按请求数**：开不开 ReAct，一次问诊的调用数差
三倍以上（不开约 5 次：S1 + S2 + 三位医家各一次 S3；开了每位医家还要走一轮
取证循环）。按请求数计，开着 ReAct 的访问者花的是别人的三倍钱却记同样一笔。

**为什么还是对外说"几次问诊"**：`llm_calls` 对访问者没有意义。所以额度在内部
以调用数为单位，前端按 `CALLS_PER_CONSULT` 折算成"约剩 N 次问诊"，并注明开
ReAct 消耗约三倍——折算系数只有这一处定义，前端不再自己算一份。

**闸门放在调第一次 LLM 之前**（`decide()` 只看账本、不碰模型），所以被拦下来的
请求是零成本的。但真实消耗只有跑完才知道，所以是"先按估算预占、跑完按
`manifest.llm_calls` 结算"两步：预占是为了防并发穿透（十个请求同时进来，都看到
"还有余额"就都放行），结算是为了让账本记的是真实花费而不是估算。

**账本在进程内存里，重启归零。** 这是刻意的：加一个持久化后端（文件/redis）就要
处理并发写、损坏恢复、部署时的路径，对一个 demo 的防滥用闸门不成比例。代价是
重启后额度重置——`snapshot()` 里带 `since` 字段如实说明它从什么时候开始算。
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Literal

# 不开 ReAct 的一次问诊大约花多少次调用：S1 + S2 + 三位医家各一次 S3。
# 追问、安全否决、校验重试都会让真实值上下浮动，所以这是"折算系数"不是"定值"，
# 只用来把调用数换算成对访问者有意义的"次数"。
CALLS_PER_CONSULT = 5
# 开了 ReAct 之后每位医家额外的取证循环步数（估算用，结算时会被真实值覆盖）。
REACT_STEPS_PER_PHYSICIAN = 5

Mode = Literal["byok", "shared", "replay"]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def force_replay_enabled() -> bool:
    """手动熔断。部署时出了事（key 泄漏、被刷、账单异常）要有一个不改代码、
    不重新构建就能把所有真实调用停掉的开关。"""
    return os.environ.get("FORCE_REPLAY", "").strip().lower() in {"1", "true", "yes", "on"}


def estimate_calls(use_react: bool, n_physicians: int) -> int:
    """一次问诊的调用数估算。只用于预占，跑完会按真实值结算。"""
    base = 2 + max(n_physicians, 1)  # S1 + S2 + 每位医家一次 S3
    if use_react:
        base += max(n_physicians, 1) * REACT_STEPS_PER_PHYSICIAN
    return base


@dataclass(frozen=True)
class QuotaDecision:
    """这一次请求该用哪个后端，以及为什么。`reason` 是给访问者看的原话。"""

    mode: Mode
    reason: str
    ip_used: int
    ip_limit: int
    global_used: int
    global_limit: int

    @property
    def degraded(self) -> bool:
        return self.mode == "replay"


class UsageLedger:
    """按天滚动的调用数账本。线程安全。

    `today_fn` 可注入：测试要跨天而不能真的等到明天。
    """

    def __init__(
        self,
        per_ip_limit: int | None = None,
        global_limit: int | None = None,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        self._per_ip = (
            per_ip_limit if per_ip_limit is not None
            else _env_int("QUOTA_PER_IP_DAILY_CALLS", CALLS_PER_CONSULT * 5)
        )
        self._global = (
            global_limit if global_limit is not None
            else _env_int("QUOTA_GLOBAL_DAILY_CALLS", CALLS_PER_CONSULT * 200)
        )
        self._today_fn = today_fn or date.today
        self._lock = threading.Lock()
        self._day: date | None = None
        self._by_ip: dict[str, int] = {}
        self._global_used = 0
        self._reservations: dict[int, tuple[str, int]] = {}
        self._next_token = 1
        self._since = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ---- 内部 ----

    def _roll_locked(self) -> None:
        today = self._today_fn()
        if self._day != today:
            self._day = today
            self._by_ip.clear()
            self._global_used = 0
            # 预占**不清**：跨天时刻还在跑的那次请求，结算时要有地方落账。
            # 它落到新的一天，宁可多算一点也不要凭空消失。

    # ---- 对外 ----

    def limits(self) -> dict:
        return {
            "per_ip_calls": self._per_ip,
            "global_calls": self._global,
            "calls_per_consult": CALLS_PER_CONSULT,
        }

    def snapshot(self, ip: str) -> dict:
        with self._lock:
            self._roll_locked()
            ip_used = self._by_ip.get(ip, 0)
            ip_left = max(self._per_ip - ip_used, 0)
            global_left = max(self._global - self._global_used, 0)
            left = min(ip_left, global_left)
            return {
                "day": str(self._day),
                "since": self._since,
                "ip_used_calls": ip_used,
                "ip_limit_calls": self._per_ip,
                "global_used_calls": self._global_used,
                "global_limit_calls": self._global,
                "remaining_calls": left,
                # 折算只在这里做一次，前端不再自己算
                "remaining_consults_estimate": left // CALLS_PER_CONSULT,
                "calls_per_consult": CALLS_PER_CONSULT,
                "force_replay": force_replay_enabled(),
                # 80% 预警：用掉的比例按"两条限额里更紧的那条"算
                "warn": _warn_ratio(ip_used, self._per_ip, self._global_used, self._global) >= 0.8,
                "used_ratio": round(
                    _warn_ratio(ip_used, self._per_ip, self._global_used, self._global), 4
                ),
            }

    def decide(self, ip: str, *, has_own_key: bool, force_replay: bool | None = None) -> QuotaDecision:
        """**只读账本，绝不调用模型**——所以被拦下的请求零成本。"""
        if force_replay is None:
            force_replay = force_replay_enabled()
        with self._lock:
            self._roll_locked()
            ip_used = self._by_ip.get(ip, 0)
            snap = (ip_used, self._per_ip, self._global_used, self._global)

        if force_replay:
            return QuotaDecision(
                "replay",
                "站点当前处于手动熔断状态（FORCE_REPLAY），只回放已录制的演示问诊。",
                *snap,
            )
        if has_own_key:
            # 自带 key 的不计额度也不占额度：花的是访问者自己的钱。
            return QuotaDecision("byok", "使用你自己的 API key，不占用站点额度。", *snap)
        if self._global_limit_hit(snap):
            return QuotaDecision(
                "replay",
                "站点今日共享额度已用完，已切换到录制回放（只有演示用的几条主诉有结果）。"
                "填入你自己的 API key 可以不受限制。",
                *snap,
            )
        if ip_used >= self._per_ip:
            return QuotaDecision(
                "replay",
                "你今天的共享额度已用完，已切换到录制回放（只有演示用的几条主诉有结果）。"
                "填入你自己的 API key 可以不受限制。",
                *snap,
            )
        return QuotaDecision("shared", "使用站点共享额度。", *snap)

    @staticmethod
    def _global_limit_hit(snap: tuple[int, int, int, int]) -> bool:
        _ip_used, _ip_limit, global_used, global_limit = snap
        return global_used >= global_limit

    def reserve(self, ip: str, estimate: int) -> int:
        """预占。返回结算用的 token。

        预占是防并发穿透用的：不预占的话十个请求同时通过 `decide()`，十次都花钱。
        """
        estimate = max(int(estimate), 0)
        with self._lock:
            self._roll_locked()
            token = self._next_token
            self._next_token += 1
            self._reservations[token] = (ip, estimate)
            self._by_ip[ip] = self._by_ip.get(ip, 0) + estimate
            self._global_used += estimate
            return token

    def settle(self, token: int, actual_calls: int) -> None:
        """按真实 `llm_calls` 结算，把预占和真实值的差补回去。

        `actual_calls` 为 0 也照样结算（安全否决走在 S2 之前、一次模型都没调，
        这种请求不该扣额度）。token 未知（比如账本被换过）时静默忽略——
        结算失败不该把一次已经成功的问诊变成错误。
        """
        actual_calls = max(int(actual_calls), 0)
        with self._lock:
            found = self._reservations.pop(token, None)
            if found is None:
                return
            ip, estimate = found
            delta = actual_calls - estimate
            self._by_ip[ip] = max(self._by_ip.get(ip, 0) + delta, 0)
            self._global_used = max(self._global_used + delta, 0)


    def bucket_for(self, ip: str, max_tracked: int) -> str:
        """把 IP 映射到一个计数桶，桶数有上限。

        没有上限的话，轮换 IP 的请求会让 `_by_ip` 无限增长（MDN 在 XFF 那条
        警告里把"内存耗尽"跟"限流被绕过"并列，是同一个成因）。超过上限之后新来的
        IP 一律归进共用桶：**宁可让少数后来者互相挤额度，也不让进程被撑爆**。
        已经在账本里的 IP 永远保留自己的桶，不会因为别人刷而被挤掉。
        """
        with self._lock:
            self._roll_locked()
            if ip in self._by_ip or len(self._by_ip) < max_tracked:
                return ip
        return "__overflow__"

    def release(self, token: int) -> None:
        """丢掉预占但**保留已经记上的账**。

        用在"跑起来了、但拿不到 manifest"的场合——最典型的是流式请求的客户端
        中途关掉标签页：`consult()` 抛 StreamClosed，outcome 是 None，可那一刻
        S1/S2 甚至 S3 的钱已经花出去了。这时候按 0 结算等于把预占全额退还，
        **谁想白嫖，开着流跑一半关掉就行**。拿不到真实值时宁可按估算记账。

        跟 settle(token, 0) 的区别就是这个：那条是"一次模型都没调"（比如并发位
        满了根本没跑起来、或者安全否决走在 S2 之前），这条是"调了但数不清"。
        """
        with self._lock:
            self._reservations.pop(token, None)


def _warn_ratio(ip_used: int, ip_limit: int, g_used: int, g_limit: int) -> float:
    ratios = []
    if ip_limit > 0:
        ratios.append(ip_used / ip_limit)
    if g_limit > 0:
        ratios.append(g_used / g_limit)
    return max(ratios) if ratios else 0.0


_ledger: UsageLedger | None = None
_ledger_lock = threading.Lock()


def get_ledger() -> UsageLedger:
    global _ledger
    if _ledger is None:
        with _ledger_lock:
            if _ledger is None:
                _ledger = UsageLedger()
    return _ledger


def reset_ledger() -> None:
    """给测试用：额度限额从环境变量读，改了环境变量要能重建账本。"""
    global _ledger
    _ledger = None
