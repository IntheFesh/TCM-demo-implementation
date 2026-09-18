"""R40：分段计时。**先测量，后优化**——没有基线的优化不许合入。

## 为什么不用 cProfile

cProfile 给的是"哪个函数被调了多少次、自身耗时多少"，而这条链路的问题是
**等待**：一次问诊 480 秒里绝大部分是在等 LLM 的 socket，函数自身耗时接近 0。
cProfile 会把这类等待均摊进调用栈，看起来"到处都慢"。

所以这里量的是四个数，每个回答一个不同的问题：

| 数 | 怎么来的 | 回答什么 |
|---|---|---|
| `wall_ms` | `perf_counter` | 人等了多久 |
| `cpu_ms` | `thread_time`（**本线程**，不是进程） | 真的在算 |
| `blocked_ms` | `wall − cpu` | 在等什么（网络/磁盘/锁） |
| `alloc_mb` | `tracemalloc` 峰值差 | 这一段吃了多少内存 |

`blocked_ms` 高 = 该并行或该缓存；`cpu_ms` 高 = 该换算法。两者的处方完全不同，
混成一个"耗时"就分不出来——这是这个模块存在的全部理由。

## 为什么 cpu 用 `thread_time` 而不是 `process_time`

问诊内部三位医家是并发跑的（`ThreadPoolExecutor`）。`process_time` 是**进程级**
累计，并发时同一段墙钟里会被算进多个线程的 CPU，于是 `wall − cpu` 变成负数、
`blocked_ms` 归零——恰好把"在等网络"这件事抹掉。`thread_time` 是本线程的，
每个 stage 各记各的。

## 线程安全

阶段栈是 `ContextVar`（每个线程/协程各一份，天然隔离），累加表是一把锁。
并发问诊时同名阶段会累加到一起，`n_calls` 记它被进入过几次——**均值自己除**，
这里不替调用方做除法（分母口径由报告决定，见 CLAUDE.md 那条铁律）。
"""
from __future__ import annotations

import threading
import time
import tracemalloc
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

#: 一次问诊要分的段。**顺序即链路顺序**，报告里的表照这个顺序排。
#: 少一段就是少一处可以被优化的地方，所以这张表是 R40 的验收项之一。
STAGES: tuple[str, ...] = (
    "import", "config_load", "ontology_load", "retriever_load", "embedding_encode",
    "graph_load", "s1", "s2", "followup", "retrieve", "knowledge_build",
    "s3_generate", "verify", "revise", "safety", "advice", "role_slice",
    "graph_build", "serialize", "sse_flush", "audit_write",
)

_MB = 1024 * 1024

#: 当前所在的阶段栈（每线程一份）。嵌套时子段的耗时**同时**计入父段——
#: 火焰图要的就是这个包含关系；扣不扣自身耗时由展示层决定。
_stack: ContextVar[tuple[str, ...]] = ContextVar("profiling_stack", default=())


@dataclass
class StageRecord:
    name: str
    wall_ms: float = 0.0
    cpu_ms: float = 0.0
    alloc_mb: float = 0.0
    n_calls: int = 0
    #: 父段名（第一次进入时记下）。同一段在两个不同父段下出现时记第一个，
    #: 并把 `n_parents` 加一——**火焰图会标出来**，不静默合并。
    parent: str | None = None
    n_parents: int = 0
    parents_seen: set[str] = field(default_factory=set)

    @property
    def blocked_ms(self) -> float:
        """在等什么。**不许为负**：计时精度与线程切换会让 cpu 略大于 wall。"""
        return max(0.0, self.wall_ms - self.cpu_ms)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "wall_ms": round(self.wall_ms, 3),
            "cpu_ms": round(self.cpu_ms, 3),
            "blocked_ms": round(self.blocked_ms, 3),
            "alloc_mb": round(self.alloc_mb, 3),
            "n_calls": self.n_calls,
            "parent": self.parent,
            "n_parents": self.n_parents,
        }


class Profiler:
    """一次问诊的分段计时器。`enabled=False` 时所有开销退化成一次 if。"""

    def __init__(self, *, enabled: bool = True, trace_alloc: bool = True) -> None:
        self.enabled = enabled
        # tracemalloc 自带 2~3 倍的内存开销与可观的 CPU 代价，所以它可关；
        # 关掉时 alloc_mb 恒为 0 并在输出里标 `alloc_traced: false`——
        # **不是"这一段没分配内存"**，两者差别很大。
        self.trace_alloc = trace_alloc and not tracemalloc.is_tracing()
        self._own_tracing = False
        self._lock = threading.Lock()
        self._records: dict[str, StageRecord] = {}
        self._t0 = time.perf_counter()

    def start(self) -> None:
        if self.trace_alloc:
            tracemalloc.start()
            self._own_tracing = True

    def stop(self) -> None:
        if self._own_tracing:
            tracemalloc.stop()
            self._own_tracing = False

    @contextmanager
    def stage(self, name: str):
        """进一段。**未知段名不抛异常**——计时器不该把被测程序弄挂；
        表外的段照记，由 `stages()` 排在已知段后面，报告层自己决定怎么说。"""
        if not self.enabled:
            yield
            return
        stack = _stack.get()
        # 父段取**栈里最近一个跟自己不同名的段**。递归/同名嵌套（`run_synthesis`
        # 里再调 `run_physician`，两者都映射到 `s3_generate`）若让一段成为自己的
        # 父段，火焰图的父→子遍历就会自指死循环——而这不是假想：TARGETS 表里
        # 本来就有两对不同函数映射到同一段名。
        parent = next((n for n in reversed(stack) if n != name), None)
        token = _stack.set((*stack, name))
        w0 = time.perf_counter()
        c0 = time.thread_time()
        a0 = tracemalloc.get_traced_memory()[0] if self._own_tracing else 0
        peak0 = tracemalloc.get_traced_memory()[1] if self._own_tracing else 0
        try:
            yield
        finally:
            wall = (time.perf_counter() - w0) * 1000.0
            cpu = (time.thread_time() - c0) * 1000.0
            alloc = 0.0
            if self._own_tracing:
                _, peak = tracemalloc.get_traced_memory()
                alloc = max(0, max(peak, peak0) - a0) / _MB
            _stack.reset(token)
            with self._lock:
                rec = self._records.get(name)
                if rec is None:
                    rec = StageRecord(name=name, parent=parent)
                    self._records[name] = rec
                rec.wall_ms += wall
                rec.cpu_ms += cpu
                rec.alloc_mb = max(rec.alloc_mb, alloc)
                rec.n_calls += 1
                if parent is not None and parent not in rec.parents_seen:
                    rec.parents_seen.add(parent)
                    rec.n_parents = len(rec.parents_seen)

    def record(self, name: str, *, wall_ms: float, cpu_ms: float = 0.0,
               alloc_mb: float = 0.0) -> None:
        """直接记一段（给"不是函数"的那几段用：import、serialize、sse_flush）。"""
        with self._lock:
            rec = self._records.setdefault(name, StageRecord(name=name))
            rec.wall_ms += wall_ms
            rec.cpu_ms += cpu_ms
            rec.alloc_mb = max(rec.alloc_mb, alloc_mb)
            rec.n_calls += 1

    # ---------- 输出 ----------

    def stages(self) -> list[StageRecord]:
        """按 `STAGES` 的顺序排，表外的段排在后面（按墙钟降序）。"""
        with self._lock:
            known = [self._records[n] for n in STAGES if n in self._records]
            extra = sorted((r for n, r in self._records.items() if n not in STAGES),
                           key=lambda r: -r.wall_ms)
        return known + extra

    def missing_stages(self) -> list[str]:
        """`STAGES` 里没被记到的段。**报出来而不是忽略**：一段没被记到，
        要么是它真的没跑（合法），要么是插桩掉了（bug），两者都要人看一眼。"""
        with self._lock:
            return [n for n in STAGES if n not in self._records]

    def to_dict(self) -> dict:
        rows = [r.to_dict() for r in self.stages()]
        total_wall = sum(r["wall_ms"] for r in rows)
        total_blocked = sum(r["blocked_ms"] for r in rows)
        return {
            "kind": "profile",
            "alloc_traced": self._own_tracing or self.trace_alloc,
            "elapsed_ms": round((time.perf_counter() - self._t0) * 1000.0, 3),
            "n_stages": len(rows),
            "missing_stages": self.missing_stages(),
            "total_stage_wall_ms": round(total_wall, 3),
            "total_blocked_ms": round(total_blocked, 3),
            # **等待占比**是这份报告的头号数字：它决定"该并行"还是"该换算法"
            "blocked_ratio": (round(total_blocked / total_wall, 4) if total_wall else None),
            "stages": rows,
        }

    def flamegraph_text(self, width: int = 48) -> str:
        """终端能读的缩进火焰图。**不依赖外部工具**——上机的机器上装不了
        speedscope 那一套，而"看不了的图等于没有图"。"""
        rows = self.stages()
        if not rows:
            return "（一段都没记到）"
        top = max(r.wall_ms for r in rows) or 1.0
        by_parent: dict[str | None, list[StageRecord]] = {}
        for r in rows:
            by_parent.setdefault(r.parent, []).append(r)
        lines: list[str] = []

        seen: set[str] = set()

        def emit(rec: StageRecord, depth: int) -> None:
            if rec.name in seen:      # 双保险：父段计算已经排除自指，这里防数据是外面塞进来的
                return
            seen.add(rec.name)
            bar = "█" * max(1, int(round(rec.wall_ms / top * width)))
            wait = f" 等 {rec.blocked_ms / rec.wall_ms:.0%}" if rec.wall_ms > 0 else ""
            multi = f" ⚠{rec.n_parents} 个父段" if rec.n_parents > 1 else ""
            lines.append(f"{'  ' * depth}{rec.name:<16s} {bar} "
                         f"{rec.wall_ms:8.1f}ms{wait}{multi}")
            for child in by_parent.get(rec.name, []):
                emit(child, depth + 1)

        for root in by_parent.get(None, []):
            emit(root, 0)
        return "\n".join(lines)


def compare(old: dict, new: dict) -> list[dict]:
    """改前改后逐段 diff。**两边都有的段才给差值**，只在一边的段如实标出来
    ——插桩变了和真的变快了，是两件事。"""
    o = {s["name"]: s for s in old.get("stages", [])}
    n = {s["name"]: s for s in new.get("stages", [])}
    out: list[dict] = []
    for name in sorted(set(o) | set(n), key=lambda x: (STAGES.index(x) if x in STAGES else 999)):
        a, b = o.get(name), n.get(name)
        row = {"name": name,
               "old_wall_ms": (a or {}).get("wall_ms"),
               "new_wall_ms": (b or {}).get("wall_ms"),
               "old_blocked_ms": (a or {}).get("blocked_ms"),
               "new_blocked_ms": (b or {}).get("blocked_ms")}
        if a and b and a["wall_ms"]:
            row["delta_ms"] = round(b["wall_ms"] - a["wall_ms"], 3)
            row["delta_pct"] = round((b["wall_ms"] - a["wall_ms"]) / a["wall_ms"] * 100, 2)
            row["note"] = None
        else:
            row["delta_ms"] = row["delta_pct"] = None
            row["note"] = "只在旧的里有（这次没跑到？）" if a else "这次新增的段"
        out.append(row)
    return out
