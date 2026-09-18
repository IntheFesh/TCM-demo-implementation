"""R40：分段计时器本身的判据。**零 LLM、零网络、秒级。**

这个文件存在的理由：R40 的第一版 profile 把 2.4 秒的本体层首次加载记到了
`verify` 头上，于是"符号验证器慢"这个结论整轮都是错的。量具自己错了，
它量出来的每个数都是错的，而且**看起来完全正常**。所以量具要有自己的测试：
四个数各自的语义、嵌套的归属、同名段的父子关系、以及"没量到"跟"没跑"的区别。
"""
from __future__ import annotations

import threading
import time

import pytest

from core.profiling import STAGES, Profiler, StageRecord, compare


def test_the_stage_table_is_the_contract_and_has_no_duplicates():
    """21 段，顺序即链路顺序。重复的段名会让累加表把两处的耗时混成一个数。"""
    assert len(STAGES) == 21, f"段数变了（{len(STAGES)}）——报告里那张表要跟着改"
    assert len(set(STAGES)) == len(STAGES)
    assert STAGES[0] == "import", "import 必须是第一段（它量的是进程起来那一下）"


def test_wall_and_cpu_and_blocked_answer_three_different_questions():
    """`blocked_ms = wall − cpu`。睡一觉：墙钟涨、CPU 不涨、等待≈墙钟。
    **这三个数混成一个"耗时"就分不出"该并行"还是"该换算法"**，
    那正是这个模块存在的全部理由。"""
    prof = Profiler(trace_alloc=False)
    with prof.stage("s1"):
        time.sleep(0.05)
    rec = prof.stages()[0]
    assert rec.name == "s1"
    assert rec.wall_ms >= 45
    assert rec.cpu_ms < 20, "睡觉不该算 CPU"
    assert rec.blocked_ms >= 40


def test_cpu_heavy_work_shows_up_as_cpu_not_as_blocked():
    """纯算的那一段：CPU 接近墙钟、等待接近 0 → 处方是"换算法"，不是"并行"。"""
    prof = Profiler(trace_alloc=False)
    with prof.stage("verify"):
        total = 0
        for i in range(400_000):
            total += i * i
    rec = prof.stages()[0]
    assert rec.wall_ms > 0
    assert rec.blocked_ms / rec.wall_ms < 0.5, "纯计算却报成大半在等待"


def test_blocked_is_never_negative():
    """计时精度与线程切换会让 cpu 略大于 wall。负的等待时间会让汇总的
    `blocked_ratio` 变成一个没法解释的数。"""
    rec = StageRecord(name="x", wall_ms=1.0, cpu_ms=1.5)
    assert rec.blocked_ms == 0.0


def test_nested_stages_record_their_parent():
    prof = Profiler(trace_alloc=False)
    with prof.stage("s3_generate"), prof.stage("verify"):
        pass
    by = {r.name: r for r in prof.stages()}
    assert by["s3_generate"].parent is None
    assert by["verify"].parent == "s3_generate"


def test_a_stage_is_never_its_own_parent():
    """同名嵌套（`run_synthesis` 里再调 `run_physician`，两者都映射到
    `s3_generate`）。让一段成为自己的父段会让火焰图的父→子遍历自指死循环
    ——TARGETS 表里本来就有两对不同函数映射到同一段名，所以这不是假想。"""
    prof = Profiler(trace_alloc=False)
    with prof.stage("s3_generate"), prof.stage("s3_generate"):
        pass
    rec = prof.stages()[0]
    assert rec.parent is None
    assert rec.n_calls == 2
    # 火焰图必须能画出来（不死循环、不递归爆栈）
    assert "s3_generate" in prof.flamegraph_text()


def test_two_different_parents_are_reported_not_silently_merged():
    """`render` 这种被 s1/s2/s3 都调到的函数只会挂在第一个父段下面。
    **要标出来**，否则读起来像"它只在 s1 里跑过"。"""
    prof = Profiler(trace_alloc=False)
    with prof.stage("s1"), prof.stage("knowledge_build"):
        pass
    with prof.stage("s2"), prof.stage("knowledge_build"):
        pass
    by = {r.name: r for r in prof.stages()}
    assert by["knowledge_build"].n_parents == 2
    assert "⚠" in prof.flamegraph_text()


def test_repeated_entries_accumulate_and_count_calls():
    """同名阶段进入多次要累加，`n_calls` 记次数。**这里不替调用方做除法**
    ——分母口径由报告决定（CLAUDE.md「任何数字都必须带对照」）。"""
    prof = Profiler(trace_alloc=False)
    for _ in range(3):
        with prof.stage("retrieve"):
            time.sleep(0.01)
    rec = prof.stages()[0]
    assert rec.n_calls == 3
    assert rec.wall_ms >= 25


def test_missing_stages_lists_what_never_ran():
    prof = Profiler(trace_alloc=False)
    with prof.stage("s1"):
        pass
    missing = prof.missing_stages()
    assert "s1" not in missing
    assert "import" in missing and "serialize" in missing
    assert len(missing) == len(STAGES) - 1


def test_an_unknown_stage_name_does_not_crash_the_measured_program():
    """计时器不该把被测程序弄挂。表外的段照记，排在已知段后面。"""
    prof = Profiler(trace_alloc=False)
    with prof.stage("某个新段"):
        pass
    names = [r.name for r in prof.stages()]
    assert names == ["某个新段"]


def test_known_stages_come_out_in_table_order_not_insertion_order():
    prof = Profiler(trace_alloc=False)
    for name in ("graph_build", "s1", "verify"):
        with prof.stage(name):
            pass
    names = [r.name for r in prof.stages()]
    assert names == ["s1", "verify", "graph_build"]


def test_disabled_profiler_costs_one_if_and_records_nothing():
    prof = Profiler(enabled=False, trace_alloc=False)
    with prof.stage("s1"):
        pass
    assert prof.stages() == []


def test_concurrent_stages_do_not_lose_counts():
    """三位医家是并发跑的。累加表是一把锁，阶段栈是 ContextVar（每线程一份）
    ——这条钉住"并发下 n_calls 不丢"。"""
    prof = Profiler(trace_alloc=False)

    def work():
        with prof.stage("s3_generate"):
            time.sleep(0.01)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert prof.stages()[0].n_calls == 8


def test_cpu_uses_thread_time_so_concurrency_does_not_zero_out_waiting():
    """**`process_time` 在并发时会把 `wall − cpu` 压成负数**，恰好抹掉
    "在等网络"这件事。这条用"一个线程睡觉、另外几个线程狂算"来钉住：
    睡觉那一段的等待必须还在。"""
    prof = Profiler(trace_alloc=False)
    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            x += 1

    burners = [threading.Thread(target=burn, daemon=True) for _ in range(4)]
    for t in burners:
        t.start()
    try:
        with prof.stage("retrieve"):
            time.sleep(0.1)
    finally:
        stop.set()
        for t in burners:
            t.join(timeout=2)
    rec = prof.stages()[0]
    assert rec.blocked_ms > 50, f"并发下等待被抹掉了：{rec.to_dict()}"


def test_to_dict_reports_blocked_ratio_as_the_headline_number():
    prof = Profiler(trace_alloc=False)
    with prof.stage("s1"):
        time.sleep(0.03)
    out = prof.to_dict()
    assert out["kind"] == "profile"
    assert 0.0 <= out["blocked_ratio"] <= 1.0
    assert out["n_stages"] == 1
    assert out["missing_stages"]


def test_alloc_traced_flag_distinguishes_off_from_zero():
    """`alloc_mb == 0` 有两种意思：没开 tracemalloc、和这一段真的没分配。
    `alloc_traced` 把两者分开——不分开的话报告里会出现"这一段不吃内存"这种假话。"""
    prof = Profiler(trace_alloc=False)
    with prof.stage("s1"):
        _ = [0] * 100_000
    out = prof.to_dict()
    assert out["alloc_traced"] is False
    assert out["stages"][0]["alloc_mb"] == 0.0


def test_record_lets_non_function_stages_be_measured():
    """import / serialize / sse_flush 不是"一个函数"，由调用方自己掐表。"""
    prof = Profiler(trace_alloc=False)
    prof.record("import", wall_ms=320.5, cpu_ms=300.0)
    rec = prof.stages()[0]
    assert rec.name == "import" and rec.wall_ms == pytest.approx(320.5)
    assert rec.n_calls == 1


def test_compare_marks_stages_that_exist_on_only_one_side():
    """插桩变了和真的变快了是两件事。只在一边的段必须如实标出来，
    不能给一个看起来像"省了 100%"的差值。"""
    old = {"stages": [{"name": "verify", "wall_ms": 100.0, "blocked_ms": 0.0},
                      {"name": "s1", "wall_ms": 10.0, "blocked_ms": 0.0}]}
    new = {"stages": [{"name": "verify", "wall_ms": 40.0, "blocked_ms": 0.0},
                      {"name": "serialize", "wall_ms": 5.0, "blocked_ms": 0.0}]}
    rows = {r["name"]: r for r in compare(old, new)}
    assert rows["verify"]["delta_ms"] == pytest.approx(-60.0)
    assert rows["verify"]["delta_pct"] == pytest.approx(-60.0)
    assert rows["s1"]["delta_ms"] is None and "只在旧的里有" in rows["s1"]["note"]
    assert rows["serialize"]["note"] == "这次新增的段"


def test_compare_keeps_the_table_order():
    old = {"stages": [{"name": "graph_build", "wall_ms": 1.0, "blocked_ms": 0.0}]}
    new = {"stages": [{"name": "s1", "wall_ms": 1.0, "blocked_ms": 0.0},
                      {"name": "graph_build", "wall_ms": 1.0, "blocked_ms": 0.0}]}
    assert [r["name"] for r in compare(old, new)] == ["s1", "graph_build"]


def test_flamegraph_says_so_when_nothing_was_recorded():
    """空图要说"一段都没记到"，不是画一张空白——空白会被当成"跑得很快"。"""
    assert "一段都没记到" in Profiler(trace_alloc=False).flamegraph_text()
