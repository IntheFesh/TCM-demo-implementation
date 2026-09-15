"""R14：首屏三条示例主诉只有一处定义（`core/examples.py`）。

## 为什么这件事值得一个文件

回放按**主诉原文的哈希**索引（`core/llm_replay.py`），演示时差一个标点就是
`LLMError: 回放未命中`。这三条原本有三份副本：DEMO.md 的「复制粘贴用」代码块、
`scripts/record_fixtures.TRIAGE_COMPLAINT`、`tests/queries.txt` 的第 1/10 条。
R14 要在首屏摆三条可点击的示例，照抄进 `web/app.js` 就是第四份——而
CLAUDE.md 第 31 条前端小节写明**写死的常量也算一处实现**。

所以反过来收口：`core/examples.py` 是运行期唯一定义，`record_fixtures` 从这里取，
`/health` 下发给前端，DEMO.md 由下面这条测试钉住跟它逐字一致。
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

import api.main as api_main
import scripts.record_fixtures as rf
from core.examples import EXAMPLE_COMPLAINTS, TRIAGE_COMPLAINT, example_complaint_texts

ROOT = Path(__file__).resolve().parent.parent


def _demo_pasted() -> list[str]:
    demo = (ROOT / "DEMO.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(A: .+?)\n```", demo, re.S)
    assert block, "DEMO.md 里找不到「复制粘贴用」的主诉代码块"
    return [line.split(": ", 1)[1].strip()
            for line in block.group(1).splitlines() if ": " in line]


def test_the_registry_matches_demo_md_character_for_character():
    """DEMO.md 是演示者照着念的那份，`core/examples.py` 是界面上摆的那份。
    两者漂一个标点，演示现场就是"页面上点一下能跑、自己手打的那条报错"
    ——而演示者会以为是系统坏了。"""
    assert example_complaint_texts() == _demo_pasted()


def test_the_labels_match_demo_mds_a_b_c():
    """label 不是装饰：演示脚本里写的是"粘贴主诉 A"、"粘贴主诉 C"，
    界面上的标号跟它对不上的话，那几句话就指不到东西。"""
    assert [e["label"] for e in EXAMPLE_COMPLAINTS] == ["A", "B", "C"]


def test_every_example_says_what_it_demonstrates():
    """三条主诉长得都像一串症状。没有这行小字，首屏上没人知道该点哪条。"""
    for e in EXAMPLE_COMPLAINTS:
        assert e["hint"].strip(), f"{e['label']} 没写用来看什么"


def test_the_triage_complaint_is_the_same_object_not_a_second_literal():
    """`record_fixtures.TRIAGE_COMPLAINT` 现在从这里取值。它仍然必须在录制
    清单里——没录过的主诉在 replay 下必然未命中。"""
    assert TRIAGE_COMPLAINT == EXAMPLE_COMPLAINTS[1]["text"]
    assert rf.TRIAGE_COMPLAINT is TRIAGE_COMPLAINT
    assert TRIAGE_COMPLAINT in {s.complaint for s in rf.build_plan()}


def test_all_three_examples_are_in_the_record_plan():
    """首屏摆出来的每一条都要能在 replay 下真的跑通——摆一条点下去就报错的
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
