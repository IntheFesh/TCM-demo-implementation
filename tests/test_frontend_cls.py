"""R41：版面跳（CLS）的判据。**两种口径不许混成一个数。**

R41 基线（真 Chromian，`scripts/profile_frontend.py`）：

| 场景 | CLS 基线 | CLS 改后 | 说明 |
|---|---:|---:|---|
| 首屏（纯加载，无交互） | 0.0269 | **0.0024** | 示例主诉是 /health 回来后填的，填进去把 textarea 整个推下去 |
| 真实用户路径（点「辨证」→ 等 → 结果） | 0.0817 | **0.0020** | 九段骨架的 chain-body 高度 0 → N 行，把下面的证据区与输入区推下去 |
| 直接渲染终态（**无点击**，上界） | 0.6711 | 0.6520 | 首屏→终态那一大跳整个计进 CLS。真实世界里这一跳在点击后 500 ms 内、被排除 |

**上界那一栏不是"没修好"**，是另一种口径：CLS 排除"用户输入后 500 ms 内"的
位移，而直接渲染没有输入。刷新页面恢复会话会落在这个口径上，所以它也要报，
但不能拿它当"真实用户体验"。

判据全在 CSS 与那两个数据源之间的**绑定**上——"我记得给它留了位"是一句
无法核实的话。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")


def _var(name: str) -> str:
    m = re.search(rf"--{name}:\s*([^;]+);", CSS)
    assert m, f"CSS 里没有 --{name}"
    return m.group(1).strip()


def test_the_examples_area_reserves_space_before_the_fetch_comes_back():
    """示例主诉是 `/health` 回来之后由 JS 填的。不留位的话填进去那一下会把
    textarea 整个推下去——实测贡献 CLS 0.0257，而它发生在**纯页面加载**期间，
    没有任何用户交互，是不折不扣的版面跳。"""
    assert "min-height: calc(var(--examples-title-h)" in CSS
    assert "--examples-rows" in CSS


def test_the_reserved_row_count_matches_the_backend_example_count():
    """**这条是这个文件的核心。** 预留几行取决于后端下发几条示例，而那张表在
    `core/examples.py`。两处各写一个数，后端加第四条示例之后前端仍然只留三行的位，
    CLS 会悄悄回来——这条测试就是那个"悄悄"的解药。"""
    from core.examples import EXAMPLE_COMPLAINTS

    assert _var("examples-rows") == str(len(EXAMPLE_COMPLAINTS)), (
        f"CSS 留了 {_var('examples-rows')} 行，而后端下发 "
        f"{len(EXAMPLE_COMPLAINTS)} 条示例——改一边没改另一边")


def test_the_reserved_height_per_row_is_a_real_number_not_zero():
    h = _var("examples-row-h")
    assert h.endswith("px")
    assert 30 <= float(h[:-2]) <= 120, f"每行 {h} 不像一条示例的高度"


def test_each_chain_section_reserves_one_line():
    """九段骨架里 `chain-body` 是空的（高度 0），结果填进来每段长出 1~4 行，
    九段累加把下面整个推下去——实测这是真实用户路径上剩下的唯一一处位移
    （CLS 0.0816，肇事元素 `details#detail-zone` + `div#input-panel`）。"""
    i = CSS.index(".chain-sec .chain-body {")
    block = CSS[i:CSS.index("}", i)]
    assert "min-height: 1.75em" in block, f"没给 chain-body 留一行的位：{block}"


def test_the_reserved_line_matches_the_line_height():
    """留的位要正好是一行：`line-height: 1.75` 配 `min-height: 1.75em`。
    两个数不一致的话留的位要么不够（还是会跳）要么多了（running 态一片空白）。"""
    i = CSS.index(".chain-sec .chain-body {")
    block = CSS[i:CSS.index("}", i)]
    assert "line-height: 1.75" in block and "min-height: 1.75em" in block


def test_the_reservation_does_not_balloon_into_dead_space():
    """**不留更多。** 留三行会让 running 态出现三倍空白——那是用体验换指标。"""
    i = CSS.index(".chain-sec .chain-body {")
    block = CSS[i:CSS.index("}", i)]
    m = re.search(r"min-height:\s*([\d.]+)em", block)
    assert m and float(m.group(1)) <= 2.0, "预留超过两行了"


def test_css_containment_is_used_on_the_blocks_that_reserve_space():
    """`contain: layout style`：这两块的内部变化不该让外面重排。
    留位与 containment 是**一对**——只留位的话，内容变化仍然会触发祖先重排。"""
    for selector in (".state-first #examples", ".chain-sec .chain-body"):
        i = CSS.index(selector + " {") if selector + " {" in CSS else CSS.index(selector)
        block = CSS[i:CSS.index("}", i)]
        assert "contain:" in block, f"{selector} 没有 contain：{block}"


def test_the_virtual_list_uses_strict_containment():
    """窗口化列表的 `contain: strict` 是它能便宜的前提：内部 transform 与
    innerHTML 替换都不会让页面其余部分重排。"""
    i = CSS.index(".virtual-list {")
    assert "contain: strict" in CSS[i:CSS.index("}", i)]


def test_animations_only_touch_transform_and_opacity():
    """**只有 transform / opacity 能在合成线程上跑**，其余属性（height、top、
    margin、width）每帧都要重排+重绘，在低配机器上直接掉帧。
    §2.4 只允许三处动效，这条测试钉住那几处用的是对的属性。"""
    for m in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", CSS):
        body = CSS[m.end():CSS.index("\n  }", m.end())]
        props = {p.strip() for p in re.findall(r"([a-z-]+)\s*:", body)}
        assert props <= {"opacity", "transform"}, (
            f"@keyframes {m.group(1)} 动的是 {props - {'opacity', 'transform'}}"
            "——那些属性每帧都要重排")


def test_transitions_do_not_animate_layout_properties():
    """`transition` 同理。允许 color / opacity / transform / visibility；
    出现 height / width / top / left / margin 就是在动布局。"""
    banned = {"height", "width", "top", "left", "right", "bottom",
              "margin", "padding", "font-size"}
    for m in re.finditer(r"transition:\s*([^;]+);", CSS):
        for part in m.group(1).split(","):
            prop = part.strip().split()[0]
            assert prop not in banned, f"transition 动了布局属性 {prop}：{m.group(1)}"


def test_reduced_motion_is_respected():
    """`prefers-reduced-motion` 下把动效关掉——这既是无障碍要求，
    也顺带让 CLS 与 INP 在那些机器上更稳。"""
    assert "prefers-reduced-motion" in CSS


def test_the_warmup_banner_is_not_alarm_coloured():
    """R40 的预热横幅走中性底（跟降级提示同一种），不走朱砂。
    这跟 CLS 无关，但跟"每次正常启动看起来都像出了事"有关——同一段版面里
    颜色语义错了，跟版面跳一样是可见的缺陷。"""
    i = CSS.index("#warmup-banner.show {")
    block = CSS[i:CSS.index("}", i)]
    assert "--surface-2" in block and "--danger" not in block
