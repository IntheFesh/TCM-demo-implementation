"""R24 补丁：自绘下拉的程序化填充回归 + 两环展开上限。

**这个文件的存在本身是一条教训。** R24 那轮 `tests/test_ui_r24.py` 30 条全绿、
Playwright 20 种状态全过，而 `docs/screenshots/r24_rings.png` 上图谱浏览器工具栏的
两个下拉**是空的**——纯函数测试测的是 `selectKeyAction` 这类映射，
Playwright 的判据查的是"元素在不在、可不可交互"，两边都不问
「按钮上有没有字」。这一轮把那个问题补成判据。

判据用 `DOM_FAKE`（`tests/web_harness.py`）而不是 `DOM_STUB`：后者是个
什么都接住的 Proxy，在它上面断言"按钮文本非空"恒真——**恒真的断言正是这个 bug
当初没被发现的原因**。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_FAKE, ROOT, js_tmp, load_ui_js

WEB = ROOT / "web"


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_FAKE + load_ui_js() + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json(body: str):
    """`body` 是一段**语句**，最后一句 `return` 要断言的对象。

    包进一个 IIFE 再 stringify：直接 `JSON.stringify(<一整段语句>)` 是语法错误
    （`SyntaxError: Unexpected token 'const'`），而那个错误长得像被测代码坏了。
    """
    return json.loads(_run(
        f"process.stdout.write(JSON.stringify((() => {{\n{body}\n}})()));"))


# 程序化填充的写法跟 graph.js 的 populateGbPhysicianSelect 一模一样：
# 先 enhance（页面加载时 enhanceAllSelects 干的），之后数据才回来、才填选项。
_FILL_AFTER_ENHANCE = """
const sel = fakeSelect("gb-physician-select", []);
const made = enhanceSelect(sel);
for (const [v, label] of [["ye_tianshi", "叶天士"], ["wu_jutong", "吴鞠通"]]) {
  const opt = document.createElement("option");
  opt.value = v; opt.textContent = label; sel.appendChild(opt);
}
"""


def test_the_button_text_follows_programmatic_option_filling():
    """**这条就是 r24_rings.png 上那两个空下拉。** 页面加载时下拉还是空的，
    `enhanceAllSelects()` 给它包了一张皮、按钮文本渲染成空串；数据回来之后
    选项是填进原生元素的，自绘按钮没人通知它——于是屏幕上是两个空框。
    """
    out = _json(_FILL_AFTER_ENHANCE + """
      refreshSelect(sel);
      return {text: made.button.textContent, value: sel.value};
    """)
    assert out["text"] == "叶天士", "填完选项要能在按钮上看到第一项"
    assert out["value"] == "ye_tianshi"


def test_refresh_syncs_hidden_from_the_native_element_to_the_wrapper():
    """`populateGbCategorySelect` 在"一个门类都没有"时把原生元素 `hidden = true`。
    自绘之后原生元素本来就看不见（`opacity: 0`），真正要藏的是**壳子**——
    不同步的话页面上会留下一个只有占位项、点开也没内容的空下拉。"""
    out = _json(_FILL_AFTER_ENHANCE + """
      sel.hidden = true; refreshSelect(sel);
      const hiddenAfter = made.wrap.hidden;
      sel.hidden = false; refreshSelect(sel);
      return {hiddenAfter, shownAfter: made.wrap.hidden};
    """)
    assert out["hiddenAfter"] is True
    assert out["shownAfter"] is False


def test_change_still_fires_after_a_refresh():
    """刷新之后选项仍然点得动、`change` 仍然派发——自绘层是原生元素的一张皮，
    刷新不能把这层关系刷掉（值的唯一来源仍是原生元素，CLAUDE.md 第 31 条）。"""
    out = _json(_FILL_AFTER_ENHANCE + """
      refreshSelect(sel);
      let fired = 0;
      sel.addEventListener("change", () => { fired += 1; });
      made.button.dispatchEvent(new Event("click"));
      made.list.children[1].dispatchEvent(new Event("mousedown"));
      return {fired, value: sel.value, text: made.button.textContent};
    """)
    assert out["fired"] == 1, "选完要派发 change，否则页面看起来变了、实际什么都没发生"
    assert out["value"] == "wu_jutong"
    assert out["text"] == "吴鞠通"


def test_refreshing_an_unenhanced_select_is_a_harmless_noop():
    """调用方（graph.js）不该先判断"这个下拉被包过没有"——那个判断一旦漏写一处，
    症状又是一个空按钮。所以这里吃下未增强的情况并返回 false。"""
    out = _json("""
      const plain = fakeSelect("not-enhanced", [["a", "甲"]]);
      return {ret: refreshSelect(plain), retNull: refreshSelect(null)};
    """)
    assert out["ret"] is False and out["retNull"] is False


def test_the_graph_browser_populate_functions_refresh_the_custom_select():
    """真正的回归守卫：两个 populate 函数填完选项后必须调 `refreshSelect`。
    上面三条测的是 `refresh` 本身对不对，这条测的是**有没有人调它**
    ——r24_rings.png 上那两个空下拉，坏的正是后者。"""
    src = (WEB / "graph.js").read_text(encoding="utf-8")
    for fn in ("populateGbPhysicianSelect", "populateGbCategorySelect"):
        body = src[src.index(f"function {fn}("):]
        body = body[:body.index("\n}\n")]
        assert "refreshSelect(sel)" in body, f"{fn} 填完选项没有刷新自绘层"


# ---------- 0.2 两环展开：上限、扇形、内环半径 ----------


def _nodes(n, counts=None):
    """造 n 个证型节点；counts 给每个节点的 n_symptoms（不给就按下标递增）。"""
    rows = []
    for i in range(n):
        c = counts[i] if counts else i
        rows.append(f'{{data: {{id: "s{i}", label: "证{i}", node_type: "syndrome", n_symptoms: {c}}}}}')
    return "[" + ",".join(rows) + "]"


def test_the_expansion_cap_is_twenty():
    """20 不是随手定的：1280×800 下 20 个带标签的节点摆成扇面之后标签还认得出来，
    再多就开始压字。真判据在 screenshot_states 的 rings（两两比包围盒）。"""
    assert _json("return {cap: GB_EXPAND_CAP};")["cap"] == 20


def test_cap_keeps_the_syndromes_with_the_most_symptoms():
    """取哪 20 个要有依据。按症状数取前 N——那是学生最可能想看的那几个；
    按加载顺序取前 N 看起来也有理由，其实取决于 networkx 的遍历顺序。"""
    out = _json(f"""
      const graph = {{nodes: {_nodes(5, [3, 9, 1, 7, 5])}, edges: []}};
      const capped = gbCapExpansion(graph, 3);
      return {{kept: capped.graph.nodes.map(n => n.data.id), dropped: capped.dropped}};
    """)
    assert out["kept"] == ["s1", "s3", "s4"]      # 9 / 7 / 5
    assert out["dropped"] == 2


def test_cap_breaks_ties_by_id_so_the_same_click_gives_the_same_picture():
    out = _json(f"""
      const graph = {{nodes: {_nodes(4, [5, 5, 5, 5])}, edges: []}};
      const a = gbCapExpansion(graph, 2).graph.nodes.map(n => n.data.id);
      const b = gbCapExpansion(graph, 2).graph.nodes.map(n => n.data.id);
      return {{a, b}};
    """)
    assert out["a"] == out["b"] == ["s0", "s1"]


def test_cap_drops_the_edges_of_the_nodes_it_drops():
    """留下半条边的后果是画布上出现指向不存在节点的线。"""
    out = _json(f"""
      const graph = {{nodes: {_nodes(3, [9, 5, 1])}, edges: [
        {{data: {{id: "e0", source: "hub", target: "s0"}}}},
        {{data: {{id: "e2", source: "hub", target: "s2"}}}},
      ]}};
      const capped = gbCapExpansion(graph, 2);
      return {{edges: capped.graph.edges.map(e => e.data.id)}};
    """)
    assert out["edges"] == ["e0"]


def test_cap_is_a_noop_when_there_is_nothing_to_drop():
    out = _json(f"""
      const graph = {{nodes: {_nodes(3)}, edges: []}};
      const capped = gbCapExpansion(graph, 20);
      return {{n: capped.graph.nodes.length, dropped: capped.dropped}};
    """)
    assert out == {"n": 3, "dropped": 0}


def test_the_status_line_says_how_many_are_left_and_how_to_reach_them():
    """**"还有 N 个"必须说出来**：不说的话用户以为这个证素就这 20 个证型，
    而那是一个静默的谎——比报错更难发现。"""
    out = _json("""
      return {
        capped: gbExpandStatusText({total: 61, returned: 61, truncated: false}, 41, "证型"),
        plain: gbExpandStatusText({total: 5, returned: 5, truncated: false}, 0, "证型"),
        empty: gbExpandStatusText({total: 0, returned: 0, truncated: false}, 0, "证型"),
      };
    """)
    assert "还有 41 个" in out["capped"] and "搜索直达" in out["capped"]
    assert "20" in out["capped"], "要说清是按什么取的前 20 个"
    assert out["plain"] == "展开了 5 个证型——再点一次收起"
    assert out["empty"] == "这个节点下没有证型"


def test_the_inner_ring_radius_never_goes_below_the_spec_floor():
    """"内环不许塌成一点"这条规格的代码化：短半轴 ≥ 画布短边的 0.18。
    r24_rings.png 上 20 个证素挤成中心一个点，就是因为半径是由
    concentric 按节点数算出来的、没有下限。"""
    out = _json("""
      const sizes = [[1114, 520], [800, 800], [1920, 400], [400, 900]];
      return sizes.map(([w, h]) => {
        const inner = gbInnerRadii(w, h);
        return Math.min(inner.rx, inner.ry) / Math.min(w, h);
      });
    """)
    assert all(r >= 0.18 for r in out), out


def test_the_fan_stays_on_one_side_and_starts_at_forty_degrees():
    """节点少的时候扇面就是 ±40°（设计默认值）；多到放不下才张开，
    上限 ±90°（半圈）——**永远不退化成整圈**，否则"这些是从哪个证素点开的"
    就看不出来了。"""
    out = _json("""
      const outer = {rx: 500, ry: 230}, inner = {rx: 200, ry: 95};
      const spanOf = (n) => {
        const slots = gbFanSlots(0, n, outer, inner);
        const angs = slots.map(s => s.angle);
        return {n: slots.length, span: (Math.max(...angs) - Math.min(...angs)) * 180 / Math.PI};
      };
      return {small: spanOf(5), big: spanOf(20)};
    """)
    assert out["small"]["n"] == 5
    assert out["small"]["span"] <= 80.5, "五个节点不该把扇面张开"
    assert out["big"]["n"] == 20
    assert out["big"]["span"] <= 180.5, "再挤也不许摊成整圈"


def test_every_fan_slot_stays_outside_the_inner_ring():
    """扇面最里面那一排必须让开内环，否则展开出来的证型会插进证素堆里。"""
    out = _json("""
      const outer = {rx: 500, ry: 230}, inner = {rx: 200, ry: 95};
      const slots = gbFanSlots(0, 20, outer, inner);
      return {minR: Math.min(...slots.map(s => s.scale * outer.ry)), innerRy: inner.ry};
    """)
    assert out["minR"] > out["innerRy"], out


def test_the_focused_hub_is_rotated_to_the_long_axis():
    """扇面朝上和朝右能用的面积差四倍（横幅画布）。正在展开的那个枢纽转到正右方
    不是好看，是让 20 个标签放得下。"""
    out = _json("""
      const hubs = ["a", "b", "c", "d"];
      const withFocus = gbHubAngles(hubs, "c");
      const without = gbHubAngles(hubs, null);
      return {focus: withFocus.get("c"), firstNoFocus: without.get("a")};
    """)
    assert abs(out["focus"]) < 1e-9, "焦点枢纽要在 0 弧度（正右方）"
    assert abs(out["firstNoFocus"] + 3.141592653589793 / 2) < 1e-9, "没有焦点时从正上方起排"


def test_layout_positions_put_every_expanded_node_outside_every_hub():
    """两环的形状判据：外圈每一个都比最靠里的枢纽远。"""
    out = _json("""
      const hubIds = Array.from({length: 20}, (_, i) => "h" + i);
      const kids = Array.from({length: 20}, (_, i) => "k" + i);
      const w = 1114, h = 520;
      const pos = gbLayoutPositions({hubIds, fans: [{hubId: "h0", childIds: kids}],
                                     others: [], width: w, height: h, focusHubId: "h0"});
      const cx = w / 2, cy = h / 2;
      const d = (id) => Math.hypot(pos[id].x - cx, pos[id].y - cy);
      return {innerMin: Math.min(...hubIds.map(d)), outerMin: Math.min(...kids.map(d)),
              short: Math.min(w, h)};
    """)
    assert out["outerMin"] > out["innerMin"]
    assert out["innerMin"] >= out["short"] * 0.18
