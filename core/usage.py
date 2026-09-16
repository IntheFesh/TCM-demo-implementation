"""共享额度账本（D1 的第二层）。

**为什么按 `llm_calls` 计量而不是按请求数**：开不开 ReAct，一次问诊的调用数差
三倍以上（不开约 5 次：S1 + S2 + 三位医家各一次 S3；开了每位医家还要走一轮
取证循环）。按请求数计，开着 ReAct 的访问者花的是别人的三倍钱却记同样一笔。

**为什么还是对外说"几次问诊"**：`llm_calls` 对访问者没有意义。所以额度在内部
以调用数为单位，前端按 `calls_per_consult()` 折算成"约剩 N 次问诊"，并注明开
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
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal

# 不开 ReAct 的一次问诊大约花多少次调用。
# **R22 起这个数是算出来的，不是写死的 5**：S1 + S2 + 每位医家 N 次 S3
# （best-of-N 采样），也就是 `2 + n_physicians × N`。写死 5 的话，把
# `S3_BEST_OF_N` 从 1 调到 3 之后，"约剩 N 次问诊"会虚报三倍——
# 访问者看到还剩 10 次、实际只够 3 次，这是折算系数存在的全部意义所在的地方。
#
# 追问、安全否决、校验重试都会让真实值上下浮动，所以它仍然是"折算系数"不是
# "定值"（真实花费由 `manifest.llm_calls` 结算）。
CALLS_PER_CONSULT_FIXED_STEPS = 2   # S1 + S2，跟医家数和 N 都无关


def calls_per_consult(n_physicians: int | None = None, best_of_n: int | None = None) -> int:
    """`2 + n_physicians × best_of_n`。**全项目这个折算系数只有这一处实现**——
    前端不自己算（它读 `/api/usage` 给的 `calls_per_consult`），额度默认值、
    看板、README 都问这里。

    两个参数都默认从当前配置取：医家数问 `core.physicians.physicians_enabled()`
    （注册表是唯一名单，R18 扩到五位时就是靠这一点没改到别处），N 问
    `core.llm.s3_best_of_n()`。函数内 import 是为了不把"额度账本"绑死在
    "推理链的注册表"上：这个模块在测试里常被单独拿来跑。
    """
    if n_physicians is None:
        from core.physicians import physicians_enabled

        n_physicians = len(physicians_enabled())
    if best_of_n is None:
        from core.llm import s3_best_of_n

        best_of_n = s3_best_of_n()
    return CALLS_PER_CONSULT_FIXED_STEPS + n_physicians * best_of_n
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


def estimate_calls(use_react: bool, n_physicians: int, best_of_n: int | None = None) -> int:
    """一次问诊的调用数估算。只用于预占，跑完会按真实值结算。

    **不自己算 `2 + n × N`**，问 `calls_per_consult()`：R22 把 S3 改成采 N 次之后，
    这里和那里回答的是同一个问题（"不开 ReAct 一次问诊几次调用"）。两处各算一遍的
    后果很具体——预占按 1 次 S3 估、实际花 3 次，账本会持续少算，
    而"少算"这种偏差不会报错，只会让额度形同虚设。
    """
    base = calls_per_consult(max(n_physicians, 1), best_of_n)
    if use_react:
        base += max(n_physicians, 1) * REACT_STEPS_PER_PHYSICIAN
    return base


# R21：DeepSeek 2026-08-17 起按峰谷分时计价。**高峰 UTC 01:00–04:00 与
# 06:00–10:00**（= 北京 09–12 与 14–18），其余时段五折。
#
# 只在这里定义一次：`scripts/run_onsite.sh` 开头那行提示、账本的 `peak` 标记、
# 前端用量面板都从这里取。写两处的话夏令时/时区换算会有一处算错，
# 而算错的表现是"按五折估的预算，实际按原价扣"。
#
# **只记不改额度**：额度仍按调用数算（`calls_per_consult()`），峰谷只影响钱。
# 让额度跟着时段变会让"今天还能问几次"这个数每隔几小时跳一次，没人看得懂。
PEAK_UTC_HOUR_RANGES = ((1, 4), (6, 10))


def is_peak(now: datetime | None = None) -> bool:
    """现在是不是高峰时段（UTC 小时落在 PEAK_UTC_HOUR_RANGES 的任一区间内）。

    区间按 `start <= hour < end` 判：UTC 01:00–04:00 含 01/02/03 三个整点，
    不含 04——04:00:00 那一刻已经是谷段了。写成半开区间是为了让两个区间
    之间不会有一个既算高峰又算低谷的整点。
    """
    hour = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).hour
    return any(start <= hour < end for start, end in PEAK_UTC_HOUR_RANGES)


def peak_note(now: datetime | None = None) -> str:
    """给人看的一句话。高峰/五折两种情况都说清楚，不是只在高峰时才提醒
    ——"现在没提醒"跟"现在是五折"是两件事，前者可能只是忘了看。"""
    n = now or datetime.now(timezone.utc)
    beijing = n.astimezone(timezone(timedelta(hours=8)))
    stamp = beijing.strftime("%Y-%m-%d %H:%M")
    if is_peak(n):
        return (f"北京时间 {stamp}：**高峰时段**（北京 09–12、14–18），"
                "按原价计费。跑贵的段建议等到谷段（五折）。")
    return f"北京时间 {stamp}：谷段，**五折**计费。适合跑贵的段。"


#: 每百万 token 的价格（美元，DeepSeek 2026-08-17 起的峰谷分时表，**高峰价**）。
#: 谷段五折（`OFF_PEAK_MULTIPLIER`）。汇率按 7.2 折成人民币——**这是估算**，
#: 不是账单，用途是让人在花钱之前知道量级。
#:
#: **R26 把这三个数从 `api/main.py` 搬到这里**，理由是第 31 条：蒸馏脚本
#: （`offline/distill_from_v4.py`）要算"这一跑花多少钱"，验 key 的提示语要算
#: "第一次问诊花多少钱"，两处回答的是同一个问题（token → 人民币）。
#: 价格表写两份的后果很具体：官方调一次价，改了一处、另一处继续按旧价估，
#: 而两处都不会报错——一个按旧价算出来的预算正好是 §0.5 那条 ¥30 闸门要看的数。
PRICE_USD_PER_MTOK_MISS = 1.32
PRICE_USD_PER_MTOK_HIT = 0.044
PRICE_USD_PER_MTOK_OUT = 3.96
USD_TO_CNY = 7.2
OFF_PEAK_MULTIPLIER = 0.5


def cost_cny(*, miss_tokens: int = 0, hit_tokens: int = 0, out_tokens: int = 0,
             peak: bool | None = None) -> float:
    """三类 token → 人民币（估算）。**全项目只有这一处把 token 折成钱。**

    `peak=None` 表示"按现在的时段算"（问 `is_peak()`）；传 True/False 是为了
    让调用方能算"如果放到谷段跑"。谷段五折只影响钱、不影响额度（额度按调用数）。
    """
    if peak is None:
        peak = is_peak()
    usd = (miss_tokens * PRICE_USD_PER_MTOK_MISS
           + hit_tokens * PRICE_USD_PER_MTOK_HIT
           + out_tokens * PRICE_USD_PER_MTOK_OUT) / 1_000_000
    if not peak:
        usd *= OFF_PEAK_MULTIPLIER
    return usd * USD_TO_CNY


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
            else _env_int("QUOTA_PER_IP_DAILY_CALLS", calls_per_consult() * 5)
        )
        self._global = (
            global_limit if global_limit is not None
            else _env_int("QUOTA_GLOBAL_DAILY_CALLS", calls_per_consult() * 200)
        )
        self._today_fn = today_fn or date.today
        self._lock = threading.Lock()
        self._day: date | None = None
        self._by_ip: dict[str, int] = {}
        self._global_used = 0
        self._reservations: dict[int, tuple[str, int]] = {}
        self._next_token = 1
        # R21：今天的 token 用量（命中/未命中/输出三项分开）。
        # **跟调用数分开记、不参与限额**：限额按调用数算（见 PEAK_UTC_HOUR_RANGES
        # 上面那段），token 数是给人看成本的——命中和未命中差 30 倍价钱，
        # 合成一个"总 token"就看不出这次问诊到底便不便宜。
        self._tokens: dict[str, int] = {"cache_hit": 0, "cache_miss": 0, "output": 0}
        # 高峰时段发生的调用数。只记不改额度。
        self._peak_calls = 0
        self._since = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ---- 内部 ----

    def _roll_locked(self) -> None:
        today = self._today_fn()
        if self._day != today:
            self._day = today
            self._by_ip.clear()
            self._global_used = 0
            self._tokens = {"cache_hit": 0, "cache_miss": 0, "output": 0}
            self._peak_calls = 0
            # 预占**不清**：跨天时刻还在跑的那次请求，结算时要有地方落账。
            # 它落到新的一天，宁可多算一点也不要凭空消失。

    # ---- 对外 ----

    def limits(self) -> dict:
        return {
            "per_ip_calls": self._per_ip,
            "global_calls": self._global,
            "calls_per_consult": calls_per_consult(),
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
                "remaining_consults_estimate": left // calls_per_consult(),
                "calls_per_consult": calls_per_consult(),
                "force_replay": force_replay_enabled(),
                # 80% 预警：用掉的比例按"两条限额里更紧的那条"算
                "warn": _warn_ratio(ip_used, self._per_ip, self._global_used, self._global) >= 0.8,
                "used_ratio": round(
                    _warn_ratio(ip_used, self._per_ip, self._global_used, self._global), 4
                ),
                # R21：今天的 token 三项 + 命中率。前端在「…」菜单的用量面板显示。
                # 命中率在这里算一次，前端不自己除（除法写两处，分母改一次就会有
                # 一处忘了改——R17 额度三档那一条踩过同一个形状）。
                "tokens_today": {
                    **self._tokens,
                    "cache_hit_ratio": (
                        round(self._tokens["cache_hit"]
                              / (self._tokens["cache_hit"] + self._tokens["cache_miss"]), 4)
                        if (self._tokens["cache_hit"] + self._tokens["cache_miss"]) else None
                    ),
                },
                "peak_calls_today": self._peak_calls,
                "is_peak_now": is_peak(),
                "peak_note": peak_note(),
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


    def record_tokens(self, usage: dict | None, *, calls: int = 0,
                      now: datetime | None = None) -> None:
        """把一次问诊的 token 用量记进今天的账。

        `usage` 是 `core.llm.current_usage_stats()` 的形状；None（后端不报这些
        字段）时只记高峰调用数，不往 token 上加 0——加 0 和"没报"在
        `snapshot()` 里长得一样，而它们要分得开。
        """
        with self._lock:
            self._roll_locked()
            if usage:
                self._tokens["cache_hit"] += int(usage.get("prompt_cache_hit_tokens") or 0)
                self._tokens["cache_miss"] += int(usage.get("prompt_cache_miss_tokens") or 0)
                self._tokens["output"] += int(usage.get("completion_tokens") or 0)
            if calls and is_peak(now):
                self._peak_calls += int(calls)

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
