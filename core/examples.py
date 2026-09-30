"""首屏的三条示例主诉：**唯一一处定义**。

`scripts/record_fixtures.py` 从这里取录制清单里的 B（`TRIAGE_COMPLAINT`），`/health`
把它下发给前端，docs/DEPLOYMENT.md「录制回放」一节的示例主诉由一条测试钉住与这里逐字一致。
`tests/queries.txt` 的第 1/10 条与 A、C 相同，但那个文件回答的是另一个问题（ε 估算
跑哪 10 条），不是"界面上摆哪三条"。

写死在前端的常量也算一处实现（docs/ARCHITECTURE.md §4），所以前端不另抄一份。
回放按主诉原文的哈希索引（`core/llm_replay.py`），差一个标点就是未命中并抛 LLMError，
各处副本一旦漂移，示例在回放模式下就跑不通。
"""
from __future__ import annotations

# label 与 docs/DEPLOYMENT.md 的 A/B/C 对应；hint 是首屏上示例下面那行小字，说明这条
# 是用来看什么的：三条主诉看起来都是"一串症状"，不写这行的话不知道该点哪条。
EXAMPLE_COMPLAINTS: tuple[dict[str, str], ...] = (
    {
        "label": "A",
        "text": "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。",
        "hint": "脾胃门典型主诉：三家辨证并列，用药对照带一眼看出分歧",
    },
    {
        "label": "B",
        "text": "胸闷胸痛，冷汗",
        "hint": "另一个门类：切到患者模式看导诊形态",
    },
    {
        "label": "C",
        "text": "胃脘疼痛数月，近日解黑色柏油样便，头晕心慌，面色苍白，倦怠乏力，舌淡，脉细数。",
        "hint": "危重症状：在证素推断之前被整页拦截，不产出任何处方",
    },
)

# 患者模式导诊那条。record_fixtures 和它的测试都按 `TRIAGE_COMPLAINT` 这个名字
# 引用；值取自上面的表，不是第二处字面量。
TRIAGE_COMPLAINT = EXAMPLE_COMPLAINTS[1]["text"]


def example_complaint_texts() -> list[str]:
    """只要正文，不要 label/hint。给"这三条都在录制清单里吗"这类检查用。"""
    return [e["text"] for e in EXAMPLE_COMPLAINTS]
