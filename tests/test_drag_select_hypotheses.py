"""R63 §2：拖选卡顿的五条假设，逐条钉在测试里。

**实测结论是"这五条在产品前端里都不成立"**（真浏览器数字见
`docs/reports/r63_drag_select.json`：1143 个 DOM 节点、51 个可点术语、
一次拖选选中 1013 个字，长任务 0 个、掉帧 0 帧、最长帧 18ms）。

那为什么还要写这几条测试？因为"现在不成立"和"以后不会成立"是两件事：
这五条每一条都是一行代码就能引回来的（给 term 加个 hover 阴影、
在 selectionchange 上挂个查词、把角标改成伪元素）。真浏览器的数字每轮不会
重跑，这几条静态判据会。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web" / "product"
JS_FILES = ("app.js", "lab.js", "knowledge.js")


def _js() -> str:
    return "\n".join((WEB / n).read_text(encoding="utf-8") for n in JS_FILES)


def _css() -> str:
    return (WEB / "app.css").read_text(encoding="utf-8")


# ---------- 假设 1、5：mousemove / selectionchange 上挂重活 ----------

@pytest.mark.parametrize("event", ["mousemove", "selectionchange"])
def test_nothing_is_bound_to_the_events_that_fire_during_a_drag(event):
    """一次拖选触发上百次 `selectionchange`（实测 192 次）与每像素一次
    `mousemove`。这两个事件上挂任何 DOM 查询或重渲染，代价都要乘以那个次数。

    要挂的话必须节流（≥50ms）或改到 `mouseup` 再做——那时这条测试要连同
    节流的证据一起改，不是直接删掉。"""
    assert event not in _js(), f"{event} 上挂了东西，要么节流要么挪到 mouseup"


# ---------- 假设 2：术语靠逐字包 span，拖选触发大量样式重算 ----------

def test_clickable_terms_go_through_event_delegation_not_per_span_listeners():
    """§2.2 第 2 条的正解就是事件委托。这里已经是了——`document` 上一个
    click 监听 + `closest("[data-term]")` 判断点了什么。
    改成给每个 span 各挂一个监听，术语一多就是上百个监听器。"""
    js = _js()
    assert 'closest("[data-term]")' in js or "closest('[data-term]')" in js
    # 渲染术语的地方不许自己 addEventListener
    assert not re.search(r'data-term[^\n]*addEventListener', js)


def test_a_term_is_one_span_for_the_whole_word_not_one_per_character():
    """逐字包 span 才是那条假设说的情形。实测 1013 个字只有 51 个 span
    ——平均一个 span 管 20 个字，那是按术语包的。"""
    app = (WEB / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function term\(id, text\) \{\s*return `([^`]*)`", app)
    assert m, "term() 的实现变了，这条判据要跟着改"
    body = m.group(1)
    assert body.count("<span") == 1, "一个术语一个 span"
    assert "${esc(text)}" in body, "整个词一次放进去，不是逐字拆"


# ---------- 假设 3：hover 里有 box-shadow / filter / transform ----------

def test_no_hover_rule_on_running_text_repaints_with_shadow_or_transform() -> None:
    """拖选会依次经过正文里的每一个可 hover 元素。hover 改 `box-shadow`/
    `filter`/`transform` 的话每经过一个就多一次合成；改 `color`/`border-color`
    只是重绘，代价小一个量级。

    判据只管**正文里的**元素（`.term`）：按钮、chip、页签上的 hover 阴影
    随便加——拖选不会横扫一排按钮。"""
    css = _css()
    term_hover = re.findall(r"\.term:hover\s*\{([^}]*)\}", css)
    assert term_hover, ".term:hover 没了？这条判据要跟着改"
    for decl in term_hover:
        for costly in ("box-shadow", "filter", "transform"):
            assert costly not in decl, f".term:hover 里出现了 {costly}"


# ---------- 假设 4：可点术语的角标用伪元素，参与选区计算 ----------

def test_terms_have_no_pseudo_element_content():
    """伪元素的 `content` 会参与选区与复制。术语上挂一个 `ⓘ` 角标，
    医师复制一段处方说明就会带一串 ⓘ。"""
    css = _css()
    assert not re.search(r"\.term(:hover)?::(before|after)", css)


def test_every_decorative_pseudo_element_is_out_of_the_way_of_a_drag():
    """现存的三处伪元素 content：进度条的 ✓/◌ 与急症水印。
    进度条在右栏的日志里、水印是 `pointer-events: none` 的覆盖层，
    实测都不进选区（`水印文字被一起选中: False`）。
    这条钉住"新加伪元素 content 时要想一下它会不会被复制走"。"""
    css = _css()
    with_content = re.findall(r"([^\s{}]+)::(?:before|after)\s*\{[^}]*content:", css)
    assert set(with_content) <= {".prog-done", ".prog-now", ".wm"}, (
        f"多了带 content 的伪元素：{with_content}——先确认它不会被一起选中复制")


# ---------- 测量脚本自己 ----------

def test_the_profiler_checks_that_it_measured_anything_at_all():
    """这个脚本踩过的第二个坑：不清上一段的选区，第三段就量了个零，
    而"耗时短、帧数少"看着像发现了瓶颈。`selected_chars` 和
    `removeAllRanges` 是防这件事的两道判据，不许被顺手删掉。"""
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "profile_drag_select.py").read_text(encoding="utf-8")
    assert "removeAllRanges" in src and "_selected_chars" in src
    assert "getSelection" in src


def test_the_recorded_numbers_are_in_the_repo_with_their_scale():
    """§2 要求报改前改后的长任务数。改前的数字在这里，**带规模**
    （多少节点、多少术语、选中多少字）——没有规模的性能数字等于没有数字。"""
    import json
    rows = json.loads((Path(__file__).resolve().parent.parent / "docs" / "reports"
                       / "r63_drag_select.json").read_text(encoding="utf-8"))
    assert rows, "录到的数据是空的"
    stress = [r for r in rows if r.get("dom_nodes")]
    assert stress, "压力版那一档没录到"
    for r in rows:
        assert r["selected_chars"] > 100, f"{r['where']} 只选中 {r['selected_chars']} 个字，等于没量"
        assert r["longtasks"] == 0, f"{r['where']} 出现了长任务：{r['longtasks']} 个"
        assert r["frames_over_50ms"] == 0, f"{r['where']} 掉帧 {r['frames_over_50ms']} 帧"
