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
    """基准报的是**观测到的**参数，不是假设的。R11 还没做思考模式按步控制，
    所以 thinking / reasoning_effort 现在恒为 None——那是事实不是缺失；R12 把它们
    传进 `_complete` 之后，这个脚本不用改一行就会报出真值。"""
    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2")
    calls = report["runs"][0]["calls"]
    assert calls, "一次调用都没记到"
    for call in calls:
        for key in ("seq", "schema", "physician", "elapsed_s", "max_tokens",
                    "temperature", "thinking", "reasoning_effort", "usage", "error"):
            assert key in call, key
        assert call["thinking"] is None and call["reasoning_effort"] is None
    assert [c["seq"] for c in calls] == list(range(1, len(calls) + 1))


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
    from core.physicians import PHYSICIANS

    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2")
    by_physician = report["runs"][0]["by_step"]["s3_by_physician"]
    assert set(by_physician) == set(PHYSICIANS), by_physician
    assert report["config"]["fake_cases"] == 2 * len(PHYSICIANS)


def test_s3_wall_and_s3_sum_are_both_reported_and_equal_while_serial(tmp_path):
    """这两个数是 R12 三医家并发的**验收判据**：sum 是总共干了多少活、wall 是这一段
    占了多少墙钟。R11 还是串行，所以 wall ≈ sum；并发之后 wall 应该掉到最慢那位附近，
    而 sum 不变——sum 不变正是"没有偷偷少干活"的证据。

    这条断言在 R12 之后**会变**（那时 wall < sum），那是有意的契约变更，不是回归。
    """
    report = _run_consult(tmp_path, "--repeat", "1", "--fake-cases", "2",
                          "--fake-latency", "0.05")
    step = report["runs"][0]["by_step"]
    assert {"s3_sum", "s3_wall", "s3_slowest"} <= set(step)
    assert step["s3_wall"] == pytest.approx(step["s3_sum"], abs=0.05)
    assert step["s3_slowest"] < step["s3_sum"]


def test_summary_counts_and_averages_the_runs(tmp_path):
    report = _run_consult(tmp_path, "--repeat", "2", "--fake-cases", "2")
    s = report["summary"]
    assert s["n_runs"] == 2 and s["n_ok"] == 2
    assert s["elapsed_s"]["min"] <= s["elapsed_s"]["mean"] <= s["elapsed_s"]["max"]
    assert s["llm_calls"]["mean"] == report["runs"][0]["llm_calls"]
    assert "s1" in s["by_step_mean"] and "s2" in s["by_step_mean"]


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
