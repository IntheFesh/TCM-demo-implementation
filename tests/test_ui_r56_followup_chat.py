"""R56 §6 第 9 条：③ 追问段从"轮数：N（停因）"一行统计改成一问一答的聊天
气泡，标题"补充问诊"——医师要看的是过程（问了什么、答了什么），不是一个计数。

`f.history` 的每一项带 `question`/`answer`（core/schemas.py::HistoryItem），
这里原样摆成气泡；轮数/停因/汇总出的确认与排除项挪到气泡下面一行"小结"，
不再是唯一的呈现方式。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js

APP = load_app_js()


def _run(js_tail: str) -> str:
    proc = subprocess.run(["node", js_tmp(DOM_STUB + APP + "\n" + js_tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _followup_chat(f: dict) -> str:
    return _run(f"process.stdout.write(followupChatHtml({json.dumps(f, ensure_ascii=False)}));")


def test_each_history_turn_becomes_a_question_and_answer_bubble():
    f = {
        "rounds": 2, "stopped_by": "converged", "stopped_by_label": "再问也问不出新信息",
        "asserted": ["口苦"], "denied": ["便血"],
        "history": [
            {"question": "有没有口苦？", "answer": "有", "asserted": ["口苦"], "denied": []},
            {"question": "有没有便血？", "answer": "没有", "asserted": [], "denied": ["便血"]},
        ],
    }
    out = _followup_chat(f)
    assert "补充问诊" in out
    assert "有没有口苦？" in out and "有没有便血？" in out
    assert out.count("followup-bubble followup-q") == 2
    assert out.count("followup-bubble followup-a") == 2


def test_the_summary_line_still_carries_rounds_and_stop_reason():
    f = {"rounds": 3, "stopped_by": "max_rounds", "stopped_by_label": "问满轮次上限",
         "asserted": [], "denied": [], "history": []}
    out = _followup_chat(f)
    assert "3 轮" in out and "问满轮次上限" in out


def test_no_history_still_renders_without_an_empty_chat_title():
    """没有 history（旧回放数据、`f.history` 缺省为空数组）时不该印一个空的
    "补充问诊"标题——那是给"有对话内容"这件事用的，不是给"有过追问"用的。"""
    f = {"rounds": 1, "stopped_by": "no_candidate", "stopped_by_label": "没有可问的候选",
         "asserted": [], "denied": []}
    out = _followup_chat(f)
    assert "补充问诊" not in out
    assert "1 轮" in out


def test_asserted_and_denied_lines_still_appear_when_present():
    f = {"rounds": 1, "stopped_by": "converged", "stopped_by_label": "再问也问不出新信息",
         "asserted": ["口苦", "纳差"], "denied": ["便血"], "history": []}
    out = _followup_chat(f)
    assert "口苦、纳差" in out
    assert "便血" in out


def test_bubble_text_is_escaped():
    f = {"rounds": 1, "stopped_by": "converged", "stopped_by_label": "x",
         "asserted": [], "denied": [],
         "history": [{"question": "<script>alert(1)</script>", "answer": "x&y", "asserted": [], "denied": []}]}
    out = _followup_chat(f)
    assert "<script>" not in out
    assert "&amp;" in out
