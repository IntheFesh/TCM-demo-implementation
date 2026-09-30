"""把几件**互不依赖**的活并行跑，结果按固定键取回。全项目只有这一处实现。

## 为什么要有这个模块，而不是各处自己 `ThreadPoolExecutor`

目前用到它的有三处：hybrid 的三路检索（`core/retrieval_hybrid.py`）、知识块里
互不依赖的两段（`core/context_prefix.py`）和批量问诊 `consult_many`
（`core/chain.py`）。各处自己写一遍的话，下面四件事会各写多遍：

1. **顺序**。并行之后按 `as_completed` 收结果是最自然的写法，也是最容易出的
   bug：RRF 融合对输入顺序敏感，顺序一变**结果就变**——那不是"更快"，是
   "不一样"。这里强制按调用方给的键顺序取回。
2. **异常**。一路抛了，别的路要不要等完？这里的选择是"等完再抛第一个异常"，
   不是让调用方拿到一半结果——半份结果在辨证链上会被当成完整的用。
3. **开关**。并行的收益取决于每一路里有多少时间真的在等 IO / 在 numpy 里
   放开了 GIL。这个只能实测，所以每一处都要能一键关掉做对照
   （`PARALLEL_OFF=1` 全局关；单处用 `enabled=` 参数）。
4. **线程数**。任务数很小（2~5），池的大小直接取任务数：线程数有明确的
   上界，不随机器的 CPU 核数变化。

## 不是所有并行都省时间

Python 的 GIL 下，**纯 Python 计算并行跑不会变快，只会因为争 GIL 稍微变慢**。
没有可重叠的等待（网络 / 磁盘 IO，或放开 GIL 的 numpy 调用）时，并行没有收益。

所以这个模块只提供机制，**不承诺收益**；用到它的地方要拿 `PARALLEL_OFF=1`
做对照实测，收益为负就不该并行。
"""
from __future__ import annotations

import concurrent.futures as cf
import contextvars
import os
from collections.abc import Callable, Sequence

#: 全局关掉并行（对照实测用）。单处的开关走 `enabled=` 参数，优先级更高。
_OFF_VALUES = ("1", "true", "yes", "on")


def parallel_off() -> bool:
    return (os.environ.get("PARALLEL_OFF", "").strip().lower()) in _OFF_VALUES


def run_routes(tasks: Sequence[tuple[str, Callable[[], object]]], *,
               enabled: bool | None = None,
               thread_name_prefix: str = "route") -> dict[str, object]:
    """把 `tasks` 并行跑完，返回 `{键: 结果}`。

    · **键不许重复**——重复意味着调用方把两件活当成一件，结果会被静默覆盖。
    · 任务数 ≤ 1 时直接串行跑（开池的代价比活本身还大）。
    · 任何一路抛异常：其余路照样等完（避免留下跑了一半的线程），然后抛出
      **按 `tasks` 顺序排在最前面的那个**异常——不是最先抛出的那个。
      理由：同一份输入应当给出同一个错误，而"最先抛出"取决于线程调度。
    """
    names = [n for n, _ in tasks]
    if len(set(names)) != len(names):
        raise ValueError(f"run_routes 的键重复了：{names}")
    use_parallel = (not parallel_off()) if enabled is None else enabled
    if len(tasks) <= 1 or not use_parallel:
        return {n: fn() for n, fn in tasks}

    out: dict[str, object] = {}
    errors: dict[str, BaseException] = {}
    with cf.ThreadPoolExecutor(max_workers=len(tasks),
                               thread_name_prefix=thread_name_prefix) as pool:
        # **ContextVar 必须自己带过去。** `ThreadPoolExecutor` 不复制上下文，
        # 而这个项目里有两件东西挂在 ContextVar 上：`use_llm(backend)`（这次用哪个
        # 后端、BYOK 的 key）和 `new_retry_stats()`（重试计数）。不带的话工作线程
        # 拿到的是**默认后端**——BYOK 静默失效、超额降级静默失效，而且不报错，
        # 只是结果来自另一个模型。`api/main.py` 的 SSE worker 里有同一条约束的说明。
        #
        # 每个任务各 `copy_context()` 一份：同一个 Context 不能被两个线程同时
        # 进入（会抛 "cannot enter context ... already entered"）。
        futs = {n: pool.submit(contextvars.copy_context().run, fn) for n, fn in tasks}
        for n in names:                      # **按调用方的顺序取回**，不按完成顺序
            try:
                out[n] = futs[n].result()
            except BaseException as e:       # noqa: BLE001 - 收集完再统一抛
                errors[n] = e
    if errors:
        first = next(n for n in names if n in errors)
        raise errors[first]
    return out


def run_indexed(items: Sequence[object], fn: Callable[[object], object], *,
                workers: int, on_done: Callable[[int, object], None] | None = None,
                on_error: Callable[[int, BaseException], None] | None = None,
                thread_name_prefix: str = "item") -> list[object | None]:
    """把 `fn` 并发套在 `items` 上，返回**跟 items 一一对齐**的结果列表。

    跟 `run_routes` 的分工：那个是"几件不同的活"（键固定、都要成功），
    这个是"同一件活的 N 份"（下标对齐、允许个别失败）。

    · 失败的位置留 `None`，并调 `on_error(i, exc)`——**不抛**：一条主诉失败
      不能把已经花钱跑完的其余条目一起丢掉（`consult_many` 依赖这一点）。
    · `on_done` / `on_error` 在**主线程**里按完成顺序调（进度条只在这里动），
      所以回调本身不需要加锁。
    · `workers <= 1` 时退化成串行 for（顺序、异常路径都跟并发路径一致）。
    """
    n = len(items)
    out: list[object | None] = [None] * n
    if n == 0:
        return out
    if workers <= 1 or parallel_off():
        for i, item in enumerate(items):
            try:
                out[i] = fn(item)
            except BaseException as e:  # noqa: BLE001 - 见上：一条失败不拖累整批
                if on_error:
                    on_error(i, e)
                continue
            if on_done:
                on_done(i, out[i])
        return out
    with cf.ThreadPoolExecutor(max_workers=min(workers, n),
                               thread_name_prefix=thread_name_prefix) as pool:
        futs = {pool.submit(contextvars.copy_context().run, fn, item): i
                for i, item in enumerate(items)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                out[i] = fut.result()
            except BaseException as e:  # noqa: BLE001
                if on_error:
                    on_error(i, e)
                continue
            if on_done:
                on_done(i, out[i])
    return out


def worker_count(env_var: str, default: int) -> int:
    """从环境变量读并发度。**0 或负数一律当 1**（串行），不当成"无限"——
    "无限并发"在分钟级的 LLM 调用上等于一次把几十条请求全打出去，
    对方限流之后整批一起失败。"""
    raw = (os.environ.get(env_var) or "").strip()
    try:
        n = int(raw) if raw else default
    except ValueError:
        n = default
    return max(1, n)
