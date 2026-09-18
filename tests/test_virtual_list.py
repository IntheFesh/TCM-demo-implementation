"""R41：窗口化列表（虚拟滚动）。**默认配置下它不生效，这是刻意的。**

R41 实测的 DOM 规模：整页 154~297 个节点，最长的列表 **14 个子节点**。参考医案
在默认配置下是 20 条（`REFS_IN_RESPONSE`）。这个量级上做虚拟滚动是纯亏。

但 `REFS_IN_RESPONSE` 是环境变量，医院调到 500 是合法配置。那时的实测对照
（真 Chromium，500 条 × 3 列）：

| | DOM 节点 | 构建 + 布局 |
|---|---:|---:|
| 全渲染 | 6000（1500 个 `.ref-item`） | 57.4 ms |
| 窗口化 | **81**（72 个 `.ref-row`） | **2.5 ms** |
| | **−98.6%** | **−95.6%** |

所以机制建好、**按阈值启用**：默认那条路径的 DOM 逐字节不变。
这个文件钉三件事：阈值行为、两处行高必须一致、窗口大小的算法。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.web_harness import DOM_STUB, js_tmp, load_app_js

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")


def _js_const(name: str) -> int:
    m = re.search(rf"const {name} = (\d+);", APP_JS)
    assert m, f"app.js 里没有 {name}"
    return int(m.group(1))


def _run(script: str) -> str:
    import subprocess

    path = js_tmp(DOM_STUB + load_app_js() + "\n" + script)
    out = subprocess.run(["node", path], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _refs_js(n: int) -> str:
    return (f"const refs = Array.from({{length: {n}}}, (_, i) => ({{"
            "case_id: 'ye_tianshi-' + String(i).padStart(4, '0'),"
            "score: 0.9, visit_label: '初诊', symptoms: ['纳差', '腹胀'],"
            "syndrome: '脾胃气虚证', excerpt: '某某某'}));")


def test_the_threshold_is_well_above_the_default_ref_count():
    """默认 20 条必须走**全渲染**那条路。阈值降到 20 以下就等于把默认界面
    换成了单行摘要，那是产品降级，不是优化。"""
    from api.main import REFS_IN_RESPONSE

    assert _js_const("VIRTUAL_LIST_THRESHOLD") > REFS_IN_RESPONSE


def test_below_the_threshold_the_markup_is_the_old_full_render():
    out = _run(_refs_js(20) + "console.log(refFoldHtml(refs, {refs_total: 20}));")
    assert "ref-item" in out
    assert "virtual-list" not in out


def test_above_the_threshold_it_switches_to_the_window():
    n = _js_const("VIRTUAL_LIST_THRESHOLD") + 1
    out = _run(_refs_js(n) + f"console.log(refFoldHtml(refs, {{refs_total: {n}}}));")
    assert "virtual-list" in out
    assert "ref-item" not in out, "两条路径混在一起了"
    assert f'data-count="{n}"' in out


def test_the_spacer_height_equals_all_rows_so_the_scrollbar_is_honest():
    """滚动条的长度必须跟"全部 n 行"一致。撑不到那个高度的话用户看到的
    滚动比例是**假的**——拖到底却发现还有一半没看完。"""
    n = 500
    row_h = _js_const("VIRTUAL_ROW_HEIGHT")
    out = _run(_refs_js(n) + "console.log(virtualRefsHtml(refs));")
    assert f"height:{n * row_h}px" in out


def test_the_viewport_height_is_capped_so_a_long_list_does_not_take_the_page():
    """外层高度取 min(12, n) 行——500 条不该把整页撑满。"""
    row_h = _js_const("VIRTUAL_ROW_HEIGHT")
    out = _run(_refs_js(500) + "console.log(virtualRefsHtml(refs));")
    assert f"height:{12 * row_h}px" in out


def test_a_short_list_above_the_threshold_still_sizes_to_its_own_rows():
    row_h = _js_const("VIRTUAL_ROW_HEIGHT")
    n = _js_const("VIRTUAL_LIST_THRESHOLD") + 1
    out = _run(_refs_js(n) + "console.log(virtualRefsHtml(refs));")
    assert f"height:{min(12, n) * row_h}px" in out


def test_the_row_height_in_js_matches_the_one_in_css():
    """**窗口化靠 `scrollTop / 行高` 算"现在该画第几行"。** 两处不一致的话
    越滚越偏——滚到一半会发现行号跳了。这是这个机制最容易出的 bug，
    而且症状（"滚下去有些医案看不到"）看起来像数据问题。"""
    js_h = _js_const("VIRTUAL_ROW_HEIGHT")
    i = CSS.index(".ref-row {")
    block = CSS[i:CSS.index("}", i)]
    m = re.search(r"height:\s*(\d+)px", block)
    assert m, f".ref-row 没有固定高度：{block}"
    assert int(m.group(1)) == js_h, (
        f"CSS 行高 {m.group(1)}px ≠ app.js 的 VIRTUAL_ROW_HEIGHT {js_h}")


def test_the_row_is_single_line_with_ellipsis():
    """固定行高的前提是内容不会换行。没有 `nowrap` + `ellipsis` 的话长文本
    会溢出到下一行，行高假设当场失效。"""
    i = CSS.index(".ref-row {")
    block = CSS[i:CSS.index("}", i)]
    for prop in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"):
        assert prop in block, f".ref-row 少了 {prop}"


def test_the_row_text_carries_the_same_facts_as_the_full_row():
    """收成一行不等于丢信息：case_id / 诊次 / 相似度 / 证型 都要还在
    （完整原文点开进证据侧栏看）。"""
    out = _run(_refs_js(1) + "console.log(virtualRowText(refs[0]));")
    for want in ("ye_tianshi-0000", "初诊", "相似度", "脾胃气虚证", "纳差"):
        assert want in out, f"单行摘要里丢了 {want}：{out}"


def test_the_row_has_a_title_attribute_so_the_full_text_is_reachable():
    """一行装不下的部分靠 `title` 兜住——省略号后面不能什么都没有。"""
    assert 'title="${' in APP_JS.split("function mountVirtualLists")[1][:1200]


def test_scroll_redraws_are_coalesced_into_a_frame():
    """`scroll` 一秒能来上百次。每次都重排一遍 DOM 就是自己造抖动。"""
    body = APP_JS.split("function mountVirtualLists")[1][:1600]
    assert "requestAnimationFrame" in body
    assert "queued" in body


def test_overscan_is_small_but_not_zero():
    """0 会在快速滚动时露白，太大就等于没窗口化。"""
    n = _js_const("VIRTUAL_OVERSCAN")
    assert 2 <= n <= 12


def test_mounting_is_a_no_op_when_there_is_no_virtual_list():
    """默认路径（20 条）页面里一个 `.virtual-list` 都没有，挂载函数必须空转、
    不抛——它在 `renderColumns` 末尾无条件被调。"""
    out = _run("mountVirtualLists(null, []); console.log('ok');")
    assert out == "ok"


@pytest.mark.parametrize("n", [0, 1, 20, 49, 50, 51, 500])
def test_the_boundary_around_the_threshold(n):
    thr = _js_const("VIRTUAL_LIST_THRESHOLD")
    out = _run(_refs_js(n) + f"console.log(refFoldHtml(refs, {{refs_total: {n}}}));")
    if n == 0:
        assert "col-refs-empty" in out
    elif n > thr:
        assert "virtual-list" in out
    else:
        assert "ref-item" in out and "virtual-list" not in out
