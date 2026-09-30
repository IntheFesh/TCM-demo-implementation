"""首屏三条示例主诉只有一处定义（`core/examples.py`）。

回放按主诉原文的哈希索引（`core/llm_replay.py`），差一个标点就是
`LLMError: 回放未命中`。所以 `core/examples.py` 是运行期唯一定义：`record_fixtures` 从这里取，
`/health` 下发给前端，docs/DEPLOYMENT.md 的示例主诉由下面这条测试钉住与它逐字一致。
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

import api.main as api_main
import scripts.record_fixtures as rf
from core.examples import EXAMPLE_COMPLAINTS, TRIAGE_COMPLAINT, example_complaint_texts

ROOT = Path(__file__).resolve().parent.parent


def _doc_pasted() -> list[str]:
    doc = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(A: .+?)\n```", doc, re.S)
    assert block, "docs/DEPLOYMENT.md 里找不到示例主诉的代码块"
    return [line.split(": ", 1)[1].strip()
            for line in block.group(1).splitlines() if ": " in line]


def test_the_registry_matches_the_deployment_doc_character_for_character():
    """文档里让人复制的那份与界面上摆的那份差一个标点，回放模式下手动输入的那条就会未命中。"""
    assert example_complaint_texts() == _doc_pasted()


def test_the_labels_are_a_b_c():
    """界面上的标号与文档里的 A/B/C 一致。"""
    assert [e["label"] for e in EXAMPLE_COMPLAINTS] == ["A", "B", "C"]


def test_every_example_says_what_to_look_at():
    """三条主诉长得都像一串症状。没有这行小字，首屏上没人知道该点哪条。"""
    for e in EXAMPLE_COMPLAINTS:
        assert e["hint"].strip(), f"{e['label']} 没写用来看什么"


def test_the_triage_complaint_is_the_same_object_not_a_second_literal():
    """`record_fixtures.TRIAGE_COMPLAINT` 从这里取值，不是第二份字面量。它必须在
    录制清单里——没录过的主诉在回放模式下必然未命中。"""
    assert TRIAGE_COMPLAINT == EXAMPLE_COMPLAINTS[1]["text"]
    assert rf.TRIAGE_COMPLAINT is TRIAGE_COMPLAINT
    assert TRIAGE_COMPLAINT in {s.complaint for s in rf.build_plan()}


def test_all_three_examples_are_in_the_record_plan():
    """首屏摆出来的每一条都要能在回放模式下真的跑通——摆一条点下去就报错的
    示例，比不摆更糟。"""
    recorded = {s.complaint for s in rf.build_plan()}
    for text in example_complaint_texts():
        assert text in recorded, f"首屏摆了 {text!r}，但录制清单里没有它"


def test_health_hands_the_examples_to_the_front_end():
    """前端不写死这三条，从 /health 拿。这条测的是"接线接上了没有"——
    少了这个键，首屏就只剩一个空输入框，而页面不会报任何错。"""
    client = TestClient(api_main.app)
    body = client.get("/health").json()
    assert [e["text"] for e in body["example_complaints"]] == example_complaint_texts()
    assert all({"label", "text", "hint"} <= set(e) for e in body["example_complaints"])
