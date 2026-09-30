"""启动预热的状态机。**先监听，再预热**，预热进度对外可见。

## 启动顺序

  1. startup 阶段**不等**预热，立刻 yield → 服务马上开始答请求；
  2. 两项预热（检索器、本体层）**并行**跑在两个线程里；
  3. `/health` 在没就绪之前回 **503 + 进度**，就绪后回 200——编排器据此
     决定什么时候放流量进来（readiness），而 `/health/live` 永远 200
     （liveness），两个探针问的不是同一个问题，不能共用一个语义。

## 为什么 startup 不等预热

ASGI 的 startup 没走完，uvicorn 不会开始处理请求。startup 若等预热，这段
时间里端口是开的、连接能建上、**但一个请求都不答**：编排器的存活探针连得上
却等不到响应，会把一个其实正常的进程判死。

## 为什么两项要并行

两项之间**没有依赖**——本体层读 `data/standard/*.jsonl`，检索器读仓库根目录的
`cases.json` 加模型，谁先谁后都不影响结果，串行只会把两段耗时加在一起。
并行是否真的更快取决于部署环境，见 `parallel_default()`。

## 为什么不是"预热失败就不启动"

数据不全是这台服务的正常状态（`cases.json` 是生成物，不进版本控制）。
预热失败只是"没有预热"，不是"服务不可用"——每一项各自 try，异常写进该项的
`note` 并打到 stderr，不静默当成成功（那样 `/health` 会报就绪而首个问诊照样
卡在惰性加载上，比不预热更难查）。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field

#: 预热项。**并行跑**，彼此无依赖。顺序只影响 `/health` 里列出来的顺序。
WARMUP_STEPS: tuple[str, ...] = ("retriever", "ontology")

STEP_LABELS = {
    "retriever": "检索器（加载编码模型 + 编码医案）",
    "ontology": "本体层（本草 + 方剂 + 证型）",
}

#: 状态取值。`skipped` 与 `failed` 分开：前者是"没有这份数据"
#: （合法，常态），后者是"有数据但加载炸了"（要看一眼）。两者都不是 ready。
STATUSES = ("pending", "running", "ready", "skipped", "failed")


@dataclass
class StepState:
    name: str
    status: str = "pending"
    ms: float = 0.0
    note: str | None = None

    def to_dict(self) -> dict:
        return {"step": self.name, "label": STEP_LABELS.get(self.name, self.name),
                "status": self.status, "ms": round(self.ms, 1), "note": self.note}


@dataclass
class WarmupTracker:
    """线程安全的预热进度。`/health` 每次调用都现读它，不缓存。"""

    steps: dict[str, StepState] = field(default_factory=dict)
    started_at: float | None = None
    finished_at: float | None = None
    parallel: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        for name in WARMUP_STEPS:
            self.steps.setdefault(name, StepState(name=name))

    # ---------- 写 ----------

    def mark(self, name: str, status: str, *, ms: float | None = None,
             note: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"未知预热状态 {status!r}，合法值 {STATUSES}")
        with self._lock:
            st = self.steps.setdefault(name, StepState(name=name))
            st.status = status
            if ms is not None:
                st.ms = ms
            if note is not None:
                st.note = note

    def begin(self) -> None:
        """**同步**登记"预热开始了"。

        必须在起预热线程**之前**调，不能等线程自己标：lifespan 起完线程就 yield，
        第一个 `/health` 完全可能赶在线程执行第一行之前到达。那时若 `started_at`
        仍是 None，`/health` 会答 200「没有预热在进行」——而预热其实马上就要开始，
        流量被放进来的正是最慢的那几秒。这个竞态窗口很短，但编排器的
        readiness 探针恰好是启动后立刻打的。
        """
        with self._lock:
            self.started_at = time.monotonic()
            self.finished_at = None

    def reset(self) -> None:
        """只给测试和重启用。"""
        with self._lock:
            self.steps = {n: StepState(name=n) for n in WARMUP_STEPS}
            self.started_at = None
            self.finished_at = None

    # ---------- 读 ----------

    @property
    def ready(self) -> bool:
        """"有没有预热在挡路"。**三个状态，不是两个**：

        | 情况 | ready | `/health` | 为什么 |
        |---|---|---|---|
        | 没人启动过预热（`started_at is None`） | True | 200 | 没有东西在来，惰性加载照常工作。回 503 会永远 503——这个进程里预热根本不会发生（`TestClient` 不进上下文管理器时 lifespan 不跑，就是这种情况） |
        | 正在预热 | False | 503 | 别放流量进来，否则首个患者得等到预热结束 |
        | 预热跑完了 | True | 200 | 就绪 |

        **`skipped` 与 `failed` 算"这一步结束了"**——服务照常可用，只是没有
        预热。定义是"没有处于 `pending` / `running` 的项"，不是"全部成功"：
        无法访问 Hugging Face Hub 时编码模型下载不下来，要求全部 ready 会让
        `/health` 永远 503。严格版在 `all_ready`，运维看板要区分这两个数。
        """
        with self._lock:
            if self.started_at is None:
                return True
            return all(s.status in ("ready", "skipped", "failed")
                       for s in self.steps.values())

    @property
    def all_ready(self) -> bool:
        """严格版：每一项都真的加载成功了。运维看板要区分这两个数。"""
        with self._lock:
            return all(s.status == "ready" for s in self.steps.values())

    def snapshot(self) -> dict:
        with self._lock:
            steps = [self.steps[n].to_dict() for n in WARMUP_STEPS if n in self.steps]
            done = sum(1 for s in steps if s["status"] in ("ready", "skipped", "failed"))
            started = self.started_at is not None
            elapsed = ((self.finished_at or time.monotonic()) - self.started_at) * 1000 \
                if self.started_at else 0.0
        settled = all(s["status"] in ("ready", "skipped", "failed") for s in steps)
        return {
            # `started` 要下发：ready=true 有两种来路（跑完了 / 压根没跑），
            # 运维看板上这两件事不能长得一样。
            "started": started,
            "ready": (not started) or settled,
            "all_ready": started and all(s["status"] == "ready" for s in steps),
            "progress": f"{done}/{len(steps)}",
            "n_done": done,
            "n_steps": len(steps),
            "elapsed_ms": round(elapsed, 1),
            "parallel": self.parallel,
            "steps": steps,
        }


#: 进程级单例。`/health` 与 lifespan 共用这一份——**两份就会打架**。
TRACKER = WarmupTracker()


def _warm_retriever(tracker: WarmupTracker) -> None:
    t0 = time.perf_counter()
    tracker.mark("retriever", "running")
    try:
        from core.retrieval import get_retriever

        get_retriever()._ensure_encoded()
    except Exception as e:  # noqa: BLE001 - 预热失败只是没有预热，服务照常起
        tracker.mark("retriever", "skipped", ms=(time.perf_counter() - t0) * 1000,
                     note=f"{type(e).__name__}: {e}")
        print(f"[warmup] 检索器预热跳过：{e}", file=sys.stderr)
        return
    tracker.mark("retriever", "ready", ms=(time.perf_counter() - t0) * 1000)


def _warm_ontology(tracker: WarmupTracker) -> None:
    t0 = time.perf_counter()
    tracker.mark("ontology", "running")
    try:
        from core.ontology import get_ontology

        ont = get_ontology()
    except Exception as e:  # noqa: BLE001
        tracker.mark("ontology", "skipped", ms=(time.perf_counter() - t0) * 1000,
                     note=f"{type(e).__name__}: {e}")
        print(f"[warmup] 本体层预热跳过：{e}", file=sys.stderr)
        return
    ms = (time.perf_counter() - t0) * 1000
    if ont.available:
        note = f"{len(ont.herbs)} 味 / {len(ont.formulas)} 首"
        tracker.mark("ontology", "ready", ms=ms, note=note)
        print(f"[warmup] 本体层已就绪：{note}（{ms:.0f} ms）", file=sys.stderr)
    else:
        tracker.mark("ontology", "skipped", ms=ms, note="药理层数据不可用")


STEP_FUNCS = {"retriever": _warm_retriever, "ontology": _warm_ontology}


def parallel_default() -> bool:
    """默认并行；`WARMUP_PARALLEL=0` 关掉。

    **为什么留这个开关**：并行的收益取决于检索器那一步有多少时间真的在等 IO
    （读盘、下载模型），只有这部分才能跟本体层的解析重叠。检索器那一步若主要是
    在重试下载（例如无法访问 Hugging Face Hub），可重叠的等待很少，两个线程
    还要争 GIL，并行反而可能比串行慢。

    默认仍是并行：等待占比是这一步的固有性质（加载模型 = 读磁盘/网络），
    "模型根本取不到"是异常态，不是常态。但收益只能在部署环境里实测确认，
    所以给一个开关，而不是把结论写死。
    """
    return (os.environ.get("WARMUP_PARALLEL", "1").strip() or "1") not in ("0", "false", "no")


def run_warmup(tracker: WarmupTracker | None = None, *,
               parallel: bool | None = None) -> dict:
    """跑完两项预热并返回快照。**同步函数**，调用方自己决定放哪个线程。

    `parallel=False` 逐项串行跑，用来对照并行的耗时；
    `None` 走 `parallel_default()`（读环境变量）。
    """
    tr = tracker if tracker is not None else TRACKER
    if parallel is None:
        parallel = parallel_default()
    tr.parallel = parallel
    if tr.started_at is None:
        tr.begin()
    if parallel:
        threads = [threading.Thread(target=STEP_FUNCS[n], args=(tr,),
                                    name=f"warmup-{n}", daemon=True)
                   for n in WARMUP_STEPS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for n in WARMUP_STEPS:
            STEP_FUNCS[n](tr)
    tr.finished_at = time.monotonic()
    return tr.snapshot()
