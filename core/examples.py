"""演示用的三条示例主诉——**唯一一处定义**。

这三条在项目里原本有三份副本：`DEMO.md` 的「复制粘贴用」代码块（给演示者看的）、
`scripts/record_fixtures.py` 的 `TRIAGE_COMPLAINT`（录制清单里的 B）、以及
`tests/queries.txt` 的第 1/10 条（A 和 C，但那个文件回答的是另一个问题——
"ε 估算跑哪 10 条"，不是"界面上摆哪三条"）。

R14 的首屏要在输入框下方摆三条可点击填入的示例，照抄一份进 `web/app.js`
就是第四处。CLAUDE.md 第 31 条前端小节写明**写死的常量也算一处实现**，所以
反过来收口：这里是运行期唯一的定义，`record_fixtures` 从这里取，`/health`
把它下发给前端，`DEMO.md` 由一条测试钉住跟这里逐字一致。

**回放按主诉原文的哈希索引**（`core/llm_replay.py`），演示时差一个标点就是
未命中并抛 LLMError。三份副本各自漂移的后果就是演示当场炸——这也是
`test_every_complaint_demo_tells_you_to_paste_is_in_the_record_plan` 的由来。
"""
from __future__ import annotations

# label 跟 DEMO.md 的 A/B/C 对应；hint 是首屏上示例下面那行小字，说明这条
# 是用来看什么的——三条主诉长得都像"一串症状"，不写这行的话没人知道该点哪条。
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

# 患者模式导诊那条。名字保留 `TRIAGE_COMPLAINT` 是因为 record_fixtures 和它的
# 测试都按这个名字引用；值从上面的表里取，不再是第二处字面量。
TRIAGE_COMPLAINT = EXAMPLE_COMPLAINTS[1]["text"]


def example_complaint_texts() -> list[str]:
    """只要正文，不要 label/hint。给"这三条都在录制清单里吗"这类检查用。"""
    return [e["text"] for e in EXAMPLE_COMPLAINTS]
