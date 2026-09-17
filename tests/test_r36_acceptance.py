"""R36 验收：五条判据逐条核，**量不到的不编**。

| # | 判据 | 这里能不能量 |
|---|---|---|
| 1 | 一次问诊 ≤4 次 LLM 调用 | ✅ 沙盒可量（假后端记调用数） |
| 2 | S3 输出减少 ≥50% | ✅ 沙盒可量（best-of-N 3→1，按输出字数比） |
| 3 | 首 token ≤3 秒 | ⏳ 要真实 LLM；沙盒只能验通道（模拟流式） |
| 4 | 墙钟 ≤150 秒 | ⏳ 要真实 LLM |
| 5 | 超时失败 0 次 | ⏳ 要真实 LLM；沙盒验的是超时值本身与重试语义 |

⏳ 那三条在 `eval/RESULTS.md` 里标 ⏳ 并附上机命令——**不在这里编一个数**
（同 bench_sandbox 对"热启动 ≤20s"的处理）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core import chain
from core.llm import API_TIMEOUTS, s3_best_of_n
from core.physicians import PHYSICIANS, physicians_for_mode
from core.usage import calls_per_consult

from tests.test_s3_mode import StructuredFakeLLM, _case as _s3_case
from tests.test_chain import FakeRetriever
from tests.web_harness import DOM_STUB, js_tmp, load_app_js

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def product_config(monkeypatch):
    """**产品默认配置**：structured + best_of_n 默认 + S1S2 不合并（R36 的默认）。

    conftest 把 S3_MODE 钉成 legacy 让上百条老测试继续测 legacy；这里显式设回
    structured，因为验收要验的就是用户真的跑的那条路。
    """
    monkeypatch.setenv("S3_MODE", "structured")
    monkeypatch.delenv("S3_BEST_OF_N", raising=False)
    monkeypatch.delenv("S1S2_MERGED", raising=False)
    from core.physicians import physicians_for_synthesis

    cases = [_s3_case(pid) for pid in physicians_for_synthesis(PHYSICIANS)]
    llm = StructuredFakeLLM({}, case_ids=[c.case_id for c in cases])
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
    return llm


# ---------- 判据 1：调用数 ≤4 ----------

def test_one_consult_costs_at_most_four_calls(product_config):
    llm = product_config
    out = chain.consult("纳差乏力，食后腹胀")
    calls = out["manifest"]["llm_calls"]
    assert calls <= 4, f"实测 {calls} 次：{llm.calls}"
    # 对照：R35 的默认（best_of_n=3）下同一条路是 2 + 3 = 5 次
    assert calls_per_consult(5, 3, "structured") == 5
    assert calls_per_consult(5, 1, "structured") == 3


def test_the_formula_reflects_the_new_default(monkeypatch):
    monkeypatch.delenv("S3_BEST_OF_N", raising=False)
    monkeypatch.setenv("S3_MODE", "structured")
    assert s3_best_of_n() == 1
    assert calls_per_consult() == 3, "S1 + S2 + 一次融合 S3"
    assert len(physicians_for_mode("structured")) == 5, "五家仍然都在，只是在同一次调用里"


# ---------- 判据 2：S3 输出 −≥50% ----------

def test_s3_output_volume_drops_by_more_than_half(monkeypatch, product_config):
    """**口径写清楚**：分子分母都是"一次问诊里 S3 这一步产出的字数总和"。
    best-of-N 从 3 降到 1 之后采样次数变成三分之一，产出的字数也就变成约三分之一
    ——省的不是"每次输出短了"，是"不再为同一份产出付三次"。
    """
    def total_s3_chars(out) -> int:
        return sum(len(r["s3"].model_dump_json()) * len(r["candidates_scored"])
                   for r in out["results"])

    monkeypatch.setenv("S3_BEST_OF_N", "3")
    before = total_s3_chars(chain.consult("纳差乏力"))
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    after = total_s3_chars(chain.consult("纳差乏力"))
    assert before > 0 and after > 0
    drop = 1 - after / before
    assert drop >= 0.5, f"只降了 {drop:.0%}（{before} → {after} 字）"


# ---------- 判据 3/5：沙盒能验的那部分 ----------

def test_the_streaming_channel_works_end_to_end_even_offline():
    """首 token ≤3 秒要真实 LLM，但**整条通道**（后端 → 链路 → 事件 → 计数）
    在沙盒里可以验通：bench_consult 的假后端会模拟流式，跑一次看有没有增量。
    这条挡的是"上机才发现事件压根没发出来"。"""
    proc = subprocess.run(
        ["python", "-m", "scripts.bench_consult", "--backend", "fake",
         "--repeat", "1", "--fake-cases", "3"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    path = [ln for ln in proc.stdout.splitlines() if ln.startswith("→ ")][-1][2:].strip()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    s = data["summary"]
    assert s["n_ok"] == 1, s.get("runs")
    assert s["n_deltas_total"] > 0, "一帧增量都没有——通道断了"
    assert s["ttft_from_open_s"] is not None, "首 token 延迟没量到"
    # **模拟的就要说是模拟的**：这个数不反映真实推理
    assert any("模拟" in n for n in s["streaming_notes"]), s["streaming_notes"]


def test_the_timeout_values_are_the_ones_the_round_claims():
    """判据 5（超时失败 0 次）要上机才能验，但"超时值调到了多少"当场可核。"""
    assert API_TIMEOUTS.read == 600.0
    assert API_TIMEOUTS.deadline == 900.0


@pytest.mark.parametrize("item", [
    "一次问诊墙钟 ≤150 秒",
    "首 token ≤3 秒",
    "超时导致的失败 0 次",
])
def test_the_on_machine_items_are_declared_in_results_md(item):
    """量不到的三条必须在 RESULTS.md 里**标着 ⏳ 并附上机命令**。
    这条测试就是"不许悄悄漏掉"的机器判据——R19 起沙盒量不了的每一项都走这条路。"""
    text = (ROOT / "eval" / "RESULTS.md").read_text(encoding="utf-8")
    assert item in text, f"RESULTS.md 里没有这一项：{item}"
    line = [ln for ln in text.splitlines() if item in ln][0]
    assert "⏳" in line, line
    assert "bench_consult" in line or "run_onsite" in line, line


# ---------- 前端：新事件有人接 ----------

def test_the_frontend_handles_the_three_new_events():
    """新加的 `s3_delta` / `s3_done` / `heartbeat` 前端都要有去处。

    **`s3_delta` 与 `heartbeat` 刻意不进日志**（几十帧会把日志顶得看不见 /
    每 15 秒一行噪音），所以对它们的断言是"翻译函数返回 null"，
    而它们真正的去处（流式区、只重置看门狗）在 submitConsult 的分派里。
    """
    js = """
process.stdout.write(JSON.stringify({
  delta_log: describeProgressEvent("s3_delta", {text: "x", kind: "content"}),
  done_log: describeProgressEvent("s3_done", {physician_name: "五家综合", events: 7,
      first_delta_s: 1.2, chars_content: 300, chars_reasoning: 40}),
  done_nostream: describeProgressEvent("s3_done", {physician_name: "五家综合", events: 0,
      streaming_note: "后端 fake 不支持流式输出"}),
  heartbeat_log: describeProgressEvent("heartbeat", {elapsed_s: 30}),
  delta_step: columnStepForEvent("s3_delta", {physician: "ye_tianshi"}),
  done_step: columnStepForEvent("s3_done", {physician: "ye_tianshi"}),
}));
"""
    proc = subprocess.run(["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["delta_log"] is None and got["heartbeat_log"] is None
    assert "7 帧" in got["done_log"] and "1.2s" in got["done_log"]
    assert "无增量" in got["done_nostream"] and "不支持流式" in got["done_nostream"]
    assert got["delta_step"] == {"physician": "ye_tianshi", "step": "s3"}
    assert got["done_step"] == {"physician": "ye_tianshi", "step": "s3"}


def test_the_stream_box_exists_and_is_separate_from_the_log():
    """流式文本原地刷新、日志一行一件事只增不改——合成一个会让日志被增量顶掉。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="s3-stream"' in html and 'id="progress-log"' in html
    css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    assert "#s3-stream" in css
