"""两个性能基准脚本的离线测试（`scripts/bench_consult.py` / `scripts/bench_startup.py`）。

基准脚本本身也要有测试，理由跟 `tests/test_run_onsite.py` 一样：**它是上机时才跑的
东西**，写错了要到真机上、花了钱之后才发现。这里全部走假后端 + 合成语料，零网络、
零 LLM 调用、秒级。
"""
import json

import pytest
from pydantic import BaseModel

from scripts import bench_consult, bench_startup


@pytest.fixture(autouse=True)
def _restore_process_state():
    """两个脚本都是**进程级生效**的：它们为了不改业务代码，改的是模块属性和类属性。
    在自己的进程里跑没问题，跑在 pytest 里就会污染后面的测试，而且只在"收集序恰好
    是这个"时才炸——最难查的那一类。这条夹具把它们动过的四样东西全部还原：

      core.retrieval._retriever_singleton          --fake-cases 装的合成检索器
      DenseRetriever/HybridRetriever.__init__ 默认值  --self-test 指向合成 cases.json
      sys.modules["sentence_transformers"]         --self-test 塞的假编码器模块
      st.SentenceTransformer                       分段计时包的那一层

    写这条夹具时它就抓到了一个真问题：`--self-test` 之后
    `HybridRetriever.__init__.__defaults__` 一直指着合成语料，于是"没有 cases.json
    该报错"那条测试永远是绿的（它拿到的是上一条测试留下的合成语料）。
    """
    import sys

    from core import retrieval
    from core.retrieval_hybrid import HybridRetriever

    before = {
        "singleton": retrieval._retriever_singleton,
        "dense_defaults": retrieval.DenseRetriever.__init__.__defaults__,
        "hybrid_defaults": HybridRetriever.__init__.__defaults__,
        "st_module": sys.modules.get("sentence_transformers"),
    }
    st_before = getattr(before["st_module"], "SentenceTransformer", None)
    yield
    retrieval._retriever_singleton = before["singleton"]
    retrieval.DenseRetriever.__init__.__defaults__ = before["dense_defaults"]
    HybridRetriever.__init__.__defaults__ = before["hybrid_defaults"]
    if before["st_module"] is None:
        sys.modules.pop("sentence_transformers", None)
    else:
        sys.modules["sentence_transformers"] = before["st_module"]
        if st_before is not None:
            before["st_module"].SentenceTransformer = st_before


def _run_consult(tmp_path, *args) -> dict:
    out = tmp_path / "consult.json"
    code = bench_consult.main(["--backend", "fake", "--out", str(out), *args])
    assert code == 0, f"退出码 {code}"
    return json.loads(out.read_text(encoding="utf-8"))


# ---------- bench_consult ----------


def test_consult_bench_runs_with_a_fake_backend_and_writes_a_complete_report(tmp_path):
    report = _run_consult(tmp_path, "--repeat", "1")
    for key in ("kind", "generated_at", "config", "backend", "runs", "summary"):
        assert key in report, key
    assert report["kind"] == "consult"
    assert report["backend"]["id"] == "fake"
    # 假后端必须自报不可比——这几个秒数被当成真机数字引用过一次就毁了一份报告
    assert "不可用于报告" in report["backend"]["comparability_warning"]


def test_every_call_records_the_arguments_it_actually_received(tmp_path):
    """基准报的是**观测到的**参数，不是假设的。

    **R12 起断言翻过来了，这正是 R11 那条 docstring 预言的**：R11 写的是
    "thinking/reasoning_effort 现在恒为 None，是事实不是缺失；R12 传下去之后这个脚本
    不用改一行就会报出真值"。现在真值出来了，于是断言从"恒为 None"改成"按步取值
    跟 `thinking_for()` 一致"——脚本一行没改，改的是它观测到的世界。

    顺带钉住按步控制真的生效了：S1/S2 关思考、S3 开思考 + effort=high。
    """
    from core.llm import thinking_for

    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2")
    calls = report["runs"][0]["calls"]
    assert calls, "一次调用都没记到"
    for call in calls:
        for key in ("seq", "schema", "physician", "elapsed_s", "max_tokens",
                    "temperature", "thinking", "reasoning_effort", "usage", "error"):
            assert key in call, key
    assert [c["seq"] for c in calls] == list(range(1, len(calls) + 1))

    by_schema = {c["schema"]: c for c in calls}
    assert by_schema["S1Normalize"]["thinking"] == thinking_for("s1")["thinking"] == "disabled"
    assert by_schema["S2Elements"]["thinking"] == "disabled"
    s3_calls = [c for c in calls if c["schema"].startswith("S3")]
    assert s3_calls, "没有 S3 调用"
    for call in s3_calls:
        assert call["thinking"] == "enabled" and call["reasoning_effort"] == "high"


def test_repeat_does_not_let_one_run_record_another_runs_calls(tmp_path):
    """**修复前是红的。** CallRecorder 把 `backend._complete` 包了一层，而
    `--repeat N` 会建 N 个 recorder；不在每次跑完拆掉，第二次的包就套在第一次外面，
    同一次调用被记进两个 recorder。修复前实测（`--repeat 3 --fake-cases 2`）：

        run0: llm_calls=5 len(calls)=15      ← 自己的 5 次 + 后两轮各 5 次
        run1: llm_calls=5 len(calls)=10
        run2: llm_calls=5 len(calls)=5

    修复后三轮都是 5/5。判据用 `llm_calls == len(calls)`：manifest 的调用数是
    chain 自己数的，跟观测层是两个独立来源，对得上才说明观测层没有多记。
    """
    report = _run_consult(tmp_path, "--repeat", "3", "--fake-cases", "2")
    assert len(report["runs"]) == 3
    for i, run in enumerate(report["runs"]):
        assert run["llm_calls"] == len(run["calls"]), f"run{i} 观测到的调用数跟 manifest 对不上"


def test_fake_cases_make_s3_actually_run_for_every_physician(tmp_path):
    """没有 cases.json 时检索层一开口就 RetrievalUnavailable，三位医家的 S3 一次都不跑
    ——那样量到的只有 S1/S2，而 R12 要验的正是 S3 那一段。"""
    from core.physicians import physicians_enabled

    PHYSICIANS = physicians_enabled()

    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2")
    by_physician = report["runs"][0]["by_step"]["s3_by_physician"]
    assert set(by_physician) == set(PHYSICIANS), by_physician
    assert report["config"]["fake_cases"] == 2 * len(PHYSICIANS)


def test_s3_wall_drops_to_the_slowest_physician_while_sum_stays(tmp_path):
    """**R12 三医家并发的验收判据。** sum 是总共干了多少活、wall 是这一段占了多少
    墙钟、slowest 是最慢的那一位。

    契约变过两次，都写在这里：
    · R11 写的是 `wall ≈ sum`（那时串行，docstring 里就写明"R12 之后会变"）；
    · R12 翻成 `wall ≈ slowest` 且 `sum` 不变——**sum 不变正是"没有偷偷少干活"的
      证据**，只看 wall 变小的话，"某位医家被跳过了"跟"三位并发"长得一模一样。

    **R13 起判据一律用比值，不要再用绝对秒数。** R12 那版写的是
    `s3_sum == 0.3*N ± 0.15`，单跑三次全绿、跟全量一起跑时红：实测 `s3_sum 1.1162`
    vs 期望 `0.9 ± 0.15`——每位医家 0.3 秒被线程启动和 GIL 竞争抬到 0.372 秒，
    吃掉 17% 的容差。而 AutoDL 是 2GB 满载机器，这条早晚会红。
    **绝对秒数度量的是"这台机器有多快"，而这条测试要测的是"并发了没有"**，
    后者是一个比值，跟机器速度无关：

      sum   >= N × latency × 0.9      只设下界（慢机器只会更大，不会更小）
      wall / sum      ≈ 1/N ± 0.25    并发；串行时这个比值是 1.0
      wall / slowest  <= 1.5          墙钟没有明显超过最慢的那一位
    """
    from core.physicians import physicians_enabled

    PHYSICIANS = physicians_enabled()

    latency, n = 0.3, len(PHYSICIANS)
    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2",
                          "--fake-latency", str(latency))
    run = report["runs"][0]
    step = run["by_step"]
    assert {"s3_sum", "s3_wall", "s3_slowest"} <= set(step)
    assert run["llm_calls"] == 2 + n
    # 下界：每位医家至少睡满 latency。慢机器只会更慢，所以只卡下界不卡上界。
    assert step["s3_sum"] >= latency * n * 0.9, step
    # 这两个比值才是"并发了没有"的判据，跟机器速度无关
    assert step["s3_wall"] / step["s3_sum"] == pytest.approx(1 / n, abs=0.25), step
    assert step["s3_wall"] / step["s3_slowest"] <= 1.5, step


def test_the_concurrency_ratio_would_catch_a_serial_implementation():
    """对照：串行时 `wall/sum` 是 1.0，落在 `1/N ± 0.25` 之外（N=3 时上界 0.583）。
    没有这条的话，"比值判据"有可能宽到连串行都放过去——那它就什么都没测。"""
    n = 3
    serial_ratio = 1.0
    assert abs(serial_ratio - 1 / n) > 0.25, "比值判据宽到连串行都判成并发了"


def test_summary_counts_and_averages_the_runs(tmp_path):
    report = _run_consult(tmp_path, "--repeat", "2", "--fake-cases", "2")
    s = report["summary"]
    assert s["n_runs"] == 2 and s["n_ok"] == 2
    assert s["elapsed_s"]["min"] <= s["elapsed_s"]["mean"] <= s["elapsed_s"]["max"]
    assert s["llm_calls"]["mean"] == report["runs"][0]["llm_calls"]
    assert "s1" in s["by_step_mean"] and "s2" in s["by_step_mean"]


def test_usage_is_rolled_up_per_schema_when_it_is_available(tmp_path):
    """R13：开思考那一步的默认上限从 16384 提到 32768，"S3 还会不会截断"要靠
    completion_tokens + reasoning_tokens 贴着上限没有来判断，不是靠"这次没报错"。
    所以 summary 里要有按 schema 的用量汇总（**看 max 不只看 mean**——截断是被最长
    的那一次触发的，均值会把它抹平）。假后端没有 usage，汇总是空字典，字段仍在。"""
    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2")
    assert report["summary"]["usage_by_schema"] == {}

    rolled = bench_consult._usage_by_schema([{"calls": [
        {"schema": "S3Syndrome", "usage": {"completion_tokens": 900,
                                           "reasoning_tokens": 4000, "total_tokens": 6000}},
        {"schema": "S3Syndrome", "usage": {"completion_tokens": 1500,
                                           "reasoning_tokens": 9000, "total_tokens": 12000}},
        {"schema": "S1Normalize", "usage": {"completion_tokens": 80}},
        {"schema": None, "usage": {"completion_tokens": 1}},
    ]}])
    assert rolled["S3Syndrome"]["reasoning_tokens"] == {"mean": 6500.0, "max": 9000.0, "n": 2}
    assert rolled["S3Syndrome"]["completion_tokens"]["max"] == 1500.0
    assert rolled["S1Normalize"]["completion_tokens"]["n"] == 1
    assert None not in rolled, "没有 schema 的调用不该占一个桶"


def test_usage_is_reported_as_unavailable_instead_of_being_invented(tmp_path):
    """假后端没有 SDK 可包，就没有 usage。**说清楚为什么没有**，不填 0 也不留空——
    一个没解释的 null 会让人以为是脚本坏了。"""
    report = _run_consult(tmp_path, "--repeat", "1")
    run = report["runs"][0]
    assert run["usage_available"] is False
    assert "没有 usage 可读" in run["usage_note"]
    assert report["summary"]["usage_available"] is False


@pytest.mark.parametrize("schema_name", [
    "S1Normalize", "S2Elements", "S3Syndrome", "S3SyndromeUnreferenced",
    "FollowupResult", "ReActStep",
])
def test_minimal_payload_is_valid_for_every_schema_on_the_chain(schema_name):
    """假后端按 JSON Schema 造实例，而不是给每个 schema 手写一份。这条把链路上会用到的
    schema 逐个过一遍 pydantic 校验——手写那种做法会在 `--react` 才走到的分支上突然
    抛 AssertionError，而那条分支恰恰是最贵、最难在真机上重跑的。"""
    from core import schemas

    schema: type[BaseModel] = getattr(schemas, schema_name)
    schema.model_validate(bench_consult.minimal_payload(schema))


def test_repeat_must_be_at_least_one(tmp_path):
    assert bench_consult.main(["--backend", "fake", "--repeat", "0",
                               "--out", str(tmp_path / "x.json")]) == 2


# ---------- bench_startup ----------


def _run_startup(tmp_path, *args) -> dict:
    out = tmp_path / "startup.json"
    code = bench_startup.main(["--out", str(out), *args])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_startup_bench_splits_the_four_segments(tmp_path):
    code, report = _run_startup(tmp_path, "--self-test", "5", "--skip-import")
    assert code == 0
    assert set(report["segments_s"]) == set(bench_startup.SEGMENTS)
    for name in ("construct", "model_load", "encode"):
        assert report["segments_s"][name] is not None, name


def test_startup_bench_marks_synthetic_numbers_as_unusable(tmp_path):
    """合成语料 + 假编码器量出来的秒数**不代表任何真实性能**。报告里必须自己说清楚，
    不然它跟真机跑出来的 JSON 长得一模一样。"""
    _, report = _run_startup(tmp_path, "--self-test", "5", "--skip-import")
    assert report["synthetic"] is True
    assert "不代表任何真实性能" in report["synthetic_note"]


def test_startup_bench_second_run_is_marked_warm(tmp_path):
    """冷热两次的区分是 R12 embedding 缓存唯一的验收方式。"""
    _, report = _run_startup(tmp_path, "--self-test", "5", "--skip-import", "--repeat", "2")
    assert [r["warm"] for r in report["runs"]] == [False, True]
    assert report["runs"][0]["n_embeddings"] == report["runs"][0]["n_cases"] > 0


def test_startup_bench_reports_a_missing_cases_json_instead_of_crashing(tmp_path, monkeypatch):
    """`cases.json` 是生成物、不进版本控制，沙盒里就是没有。这时候要如实写进
    `error` 并给非 0 退出码，而不是抛异常——抛了就什么都量不到，连 import 那一段
    已经量到的数也丢了。"""
    from core import retrieval
    from core.retrieval_hybrid import HybridRetriever

    missing = tmp_path / "没有这个文件.json"
    monkeypatch.setattr(retrieval, "_retriever_singleton", None)
    # 两个类都要改：get_retriever() 建的是 HybridRetriever，只改基类的默认值不起作用
    # （子类 __init__ 自己有一份默认值）。
    monkeypatch.setattr(retrieval.DenseRetriever.__init__, "__defaults__", (missing,))
    monkeypatch.setattr(HybridRetriever.__init__, "__defaults__", (missing,))
    code, report = _run_startup(tmp_path, "--skip-import")
    assert code == 1
    assert "FileNotFoundError" in (report["error"] or "")
    assert report["segments_s"]["construct"] is not None

# ---------- 第 0 项：R11 基准工具的两个静默失败 ----------


def test_a_run_with_no_physician_results_is_not_reported_as_ok(tmp_path, monkeypatch):
    """**跑空必须 ok:false。** `consult()` 把"检索不可用"放进返回值的 `retrieval_error`
    而不是抛异常，于是 `run_once` 的 try/except 什么也没接住——三位医家一个都没跑，
    基准照样打印「1/1 次跑成功」。这种失败比崩掉危险得多：崩了会有人看，而一份
    `ok: true` 的报告会被直接引用。

    修复前实测（`--backend fake`，仓库里没有 cases.json）：
        ok= True llm_calls= 2 events= ['s1_done','s2_done','followup_done','physician_start']
    """
    from core import retrieval
    from core.retrieval_hybrid import HybridRetriever

    missing = tmp_path / "没有这个文件.json"
    monkeypatch.setattr(retrieval, "_retriever_singleton", None)
    monkeypatch.setattr(retrieval.DenseRetriever.__init__, "__defaults__", (missing,))
    monkeypatch.setattr(HybridRetriever.__init__, "__defaults__", (missing,))

    out = tmp_path / "empty.json"
    code = bench_consult.main(["--backend", "fake", "--repeat", "1",
                               "--no-auto-fake-cases", "--out", str(out)])
    report = json.loads(out.read_text(encoding="utf-8"))
    run = report["runs"][0]
    assert run["ok"] is False, "三位医家一个都没跑，却报了成功"
    assert "检索" in (run["error"] or "") or "医家" in (run["error"] or "")
    assert code == 1, "有跑失败时退出码必须非 0"


def test_a_valid_fake_run_makes_exactly_five_calls(tmp_path):
    """**llm_calls == 5 才算一次有效的基准**：S1 + S2 + 三位医家各一次 S3。
    少于 5 就说明有医家没跑到，这份数据不能用来比并发前后的耗时。"""
    from core.physicians import physicians_enabled

    PHYSICIANS = physicians_enabled()

    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "3")
    run = report["runs"][0]
    assert run["ok"] is True, run["error"]
    assert run["llm_calls"] == 2 + len(PHYSICIANS) == 5
    assert len(run["by_step"]["s3_by_physician"]) == len(PHYSICIANS)


def test_startup_bench_reports_a_missing_sentence_transformers_instead_of_crashing(
        tmp_path, monkeypatch):
    """`instrument_encoder` 原来无条件 import sentence_transformers，缺这个包直接
    ModuleNotFoundError——而这个脚本存在的意义之一就是"在什么都没装好的机器上也能
    告诉你缺什么"。装不上就如实写进报告，不是崩掉。"""
    import sys

    # sys.modules[name] = None 会让 import 抛 ImportError，等价于"这台机器没装"
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    out = tmp_path / "startup.json"
    code = bench_startup.main(["--out", str(out), "--skip-import"])
    report = json.loads(out.read_text(encoding="utf-8"))
    assert "sentence_transformers" in (report["encoder_note"] or "")
    assert report["segments_s"]["model_load"] is None
    assert code == 1  # 量不到就是量不到，退出码要能看出来


def test_fake_backend_installs_synthetic_cases_when_the_repo_has_none(tmp_path, monkeypatch):
    """**不该要求跑的人记得加 `--fake-cases`。** 假后端 + 没有 cases.json 是沙盒里的
    常态，这时自动装合成医案并在输出里标明，比让人拿到一份只有 S1/S2 的"成功"报告好。
    自动装了就必须能从报告里看出来——`fake_cases` 非 0 且 `fake_cases_auto` 为真。"""
    from core import retrieval
    from core.physicians import physicians_enabled

    PHYSICIANS = physicians_enabled()
    from core.retrieval_hybrid import HybridRetriever

    missing = tmp_path / "没有这个文件.json"
    monkeypatch.setattr(retrieval, "_retriever_singleton", None)
    monkeypatch.setattr(retrieval.DenseRetriever.__init__, "__defaults__", (missing,))
    monkeypatch.setattr(HybridRetriever.__init__, "__defaults__", (missing,))

    report = _run_consult(tmp_path, "--repeat", "1")
    assert report["config"]["fake_cases_auto"] is True
    assert report["config"]["fake_cases"] == 3 * len(PHYSICIANS)
    assert report["runs"][0]["llm_calls"] == 5
