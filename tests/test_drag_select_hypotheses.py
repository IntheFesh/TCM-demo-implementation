"""拖选卡顿的五条假设，逐条钉在测试里。

拖选在真浏览器里的表现（长任务数、帧时间）由 `scripts/profile_drag_select.py`
量取，但那个测量不会随每次改动重跑。这五条假设每一条又都是一行代码就能引回来的
（给 term 加个 hover 阴影、在 selectionchange 上挂个查词、把角标做成伪元素），
所以把它们写成静态判据，每次跑测试都检查。
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
    """一次拖选会触发上百次 `selectionchange` 与每像素一次 `mousemove`。
    这两个事件上挂任何 DOM 查询或重渲染，代价都要乘以那个次数。

    要挂的话必须节流（≥50ms）或改到 `mouseup` 再做——那时这条测试要连同
    节流的证据一起改，不是直接删掉。"""
    assert event not in _js(), f"{event} 上挂了东西，要么节流要么挪到 mouseup"


# ---------- 假设 2：术语靠逐字包 span，拖选触发大量样式重算 ----------

def test_clickable_terms_go_through_event_delegation_not_per_span_listeners():
    """可点术语用事件委托：`document` 上一个 click 监听 +
    `closest("[data-term]")` 判断点了什么。
    给每个 span 各挂一个监听的话，术语一多就是上百个监听器。"""
    js = _js()
    assert 'closest("[data-term]")' in js or "closest('[data-term]')" in js
    # 渲染术语的地方不许自己 addEventListener
    assert not re.search(r'data-term[^\n]*addEventListener', js)


def test_a_term_is_one_span_for_the_whole_word_not_one_per_character():
    """假设说的是"逐字包 span"。这里一个术语只包一个 span，
    一个 span 管一整个词，不是一字一个。"""
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
    """现有的三处伪元素 content：进度条的 ✓/◌ 与急症水印。
    进度条在右栏的日志里、水印是 `pointer-events: none` 的覆盖层，
    都不在拖选会经过的正文里。
    这条钉住"新加伪元素 content 时要想一下它会不会被复制走"。"""
    css = _css()
    with_content = re.findall(r"([^\s{}]+)::(?:before|after)\s*\{[^}]*content:", css)
    assert set(with_content) <= {".prog-done", ".prog-now", ".wm"}, (
        f"多了带 content 的伪元素：{with_content}——先确认它不会被一起选中复制")


# ---------- 测量脚本自己 ----------

def test_the_profiler_checks_that_it_measured_anything_at_all():
    """测量脚本必须先清掉上一段的选区：不清的话后一段量到的是零，
    而"耗时短、帧数少"看着像没有瓶颈。`selected_chars` 和
    `removeAllRanges` 是防这件事的两道判据，不许被顺手删掉。"""
    src = (Path(__file__).resolve().parent.parent
           / "scripts" / "profile_drag_select.py").read_text(encoding="utf-8")
    assert "removeAllRanges" in src and "_selected_chars" in src
    assert "getSelection" in src
