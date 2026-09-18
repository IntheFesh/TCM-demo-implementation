"""R42：图谱浏览器的「聚焦 + 分层 + 面包屑」。

## 为什么加这个模式，以及为什么两种布局都留着

同心双环回答的是"这张图上都有什么"——它是一张**概览**。真要看一个证型时想问
的是另一个问题：**"这一条是怎么连起来的"**。在环上这个问题答不了：相关的节点
散在环的不同角度上，边跨过圆心互相交叉。

所以加聚焦：双击一个节点 → 只看它的 k 跳邻域，**按跳距分层**（dagre，跟问诊图
那张一套）。层号 = 跳距，于是"它 → 直接邻居 → 邻居的邻居"就是三列。

**不是"dagre 比同心环好"**——两者回答的不是同一个问题，所以概览留环、聚焦用
dagre。真正要量的是"dagre 在这个规模上够不够快"，那是
`window.__gbPerf.benchLayouts()` 的活，数字进 R42 报告（总纲 §12：先测量后优化）。

## 这个文件测什么

BFS 的正确性（无向、截断计数、case 层不泄漏）、面包屑的结构、退出聚焦恢复的是
**进聚焦前那批节点**而不是重铺首屏，以及"一个算法只有一处实现"——聚焦的坐标
走的是问诊图那个 `computeLayout`，不是另写一份分层。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tests.web_harness import DOM_STUB, js_tmp

ROOT = Path(__file__).resolve().parent.parent
GRAPH_JS = (ROOT / "web" / "graph.js").read_text(encoding="utf-8")

#: 一小张假图：证素「脾」连两个证型，每个证型连两个症状，另有一个 case 节点。
#: 跳距因此是 脾=0 / 证型=1 / 症状=2。
FAKE_GRAPH = {
    "nodes": [
        {"data": {"id": "element::脾", "label": "脾", "node_type": "element"}},
        {"data": {"id": "syndrome::A", "label": "脾气虚证", "node_type": "syndrome"}},
        {"data": {"id": "syndrome::B", "label": "脾阳虚证", "node_type": "syndrome"}},
        {"data": {"id": "symptom::纳差", "label": "纳差", "node_type": "symptom"}},
        {"data": {"id": "symptom::乏力", "label": "乏力", "node_type": "symptom"}},
        {"data": {"id": "symptom::畏寒", "label": "畏寒", "node_type": "symptom"}},
        {"data": {"id": "case::x-1", "label": "医案 x-1", "node_type": "case"}},
    ],
    "edges": [
        {"data": {"id": "e1", "source": "element::脾", "target": "syndrome::A"}},
        {"data": {"id": "e2", "source": "element::脾", "target": "syndrome::B"}},
        {"data": {"id": "e3", "source": "symptom::纳差", "target": "syndrome::A"}},
        {"data": {"id": "e4", "source": "symptom::乏力", "target": "syndrome::A"}},
        {"data": {"id": "e5", "source": "symptom::畏寒", "target": "syndrome::B"}},
        {"data": {"id": "e6", "source": "case::x-1", "target": "syndrome::A"}},
    ],
}


def _run(tail: str) -> str:
    """喂一张假图给 gbIndex，再跑被测函数。

    **不起 cytoscape**：这个文件测的是 BFS / 分层 / 面包屑这几个纯函数，
    真实渲染（聚焦之后节点有没有重叠）是 Playwright 的活。
    """
    setup = (
        f"gbGraphData = {{ graph: {json.dumps(FAKE_GRAPH, ensure_ascii=False)} }};\n"
        "gbBuildIndex();\n"
    )
    proc = subprocess.run(["node", js_tmp(DOM_STUB + GRAPH_JS + "\n" + setup + tail)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node 失败：\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


# ---------- 一、BFS ----------

def test_the_neighbourhood_walks_edges_in_both_directions():
    """**无向**：这张图的边方向是 症状→证型、证素→证型，证型没有出边。
    只走出边的话，点一个证型什么都看不到。"""
    out = _run("""
      const nh = gbNeighborhood("syndrome::A", 1);
      process.stdout.write(JSON.stringify([...nh.ids].sort()));
    """)
    got = set(json.loads(out))
    assert "element::脾" in got, "证素在上游，只走出边就摸不到它"
    assert {"symptom::纳差", "symptom::乏力"} <= got


def test_the_hop_distance_is_what_becomes_the_layer_number():
    out = _run("""
      const nh = gbNeighborhood("element::脾", 2);
      process.stdout.write(JSON.stringify([...nh.hop.entries()].sort()));
    """)
    hop = dict(json.loads(out))
    assert hop["element::脾"] == 0
    assert hop["syndrome::A"] == 1 and hop["syndrome::B"] == 1
    assert hop["symptom::纳差"] == 2 and hop["symptom::畏寒"] == 2


def test_the_case_layer_does_not_leak_when_it_is_switched_off():
    """`gbShowCaseLayer` 为假时按钮本身都不出现；这里再挡一道，防的是
    "从别的节点展开时，边的另一端恰好是 case 节点"这种间接泄漏
    （跟 gbAddNodes 那道是同一条边界）。"""
    off = json.loads(_run("""
      gbShowCaseLayer = false;
      process.stdout.write(JSON.stringify([...gbNeighborhood("syndrome::A", 1).ids]));
    """))
    on = json.loads(_run("""
      gbShowCaseLayer = true;
      process.stdout.write(JSON.stringify([...gbNeighborhood("syndrome::A", 1).ids]));
    """))
    assert "case::x-1" not in off
    assert "case::x-1" in on, "打开医案层之后反而还看不到，那是另一个 bug"


def test_the_cap_truncates_and_counts_instead_of_silently_dropping():
    """"肝"这样的枢纽证素两跳能摸到上千个节点。**超了要说出来**
    （同 GB_MAX_NEW_NODES 那条理由：静默少给看起来像"就这么多"）。"""
    out = _run("""
      const nh = gbNeighborhood("element::脾", 2, 3);
      process.stdout.write(JSON.stringify({n: nh.ids.size, truncated: nh.truncated}));
    """)
    d = json.loads(out)
    assert d["n"] <= 3
    assert d["truncated"] > 0


def test_the_root_is_always_in_its_own_neighbourhood():
    out = _run("""
      process.stdout.write(JSON.stringify([...gbNeighborhood("symptom::畏寒", 0).ids]));
    """)
    assert json.loads(out) == ["symptom::畏寒"]


# ---------- 二、分层走的是同一个 computeLayout ----------

def test_the_focus_layout_reuses_the_consult_graphs_layout_function():
    """**一个算法只有一处实现**（CLAUDE.md 第 31 条）。为浏览器另写一份分层
    布局的话，改一边的时候另一边不会跟着动——而两张图的分层是同一件事。"""
    body = GRAPH_JS.split("function gbFocusPositions")[1][:900]
    assert "computeLayout(nodes, edges)" in body
    assert "layer: nh.hop.get(id)" in body, "跳距没有被当成层号"


def test_the_focus_positions_cover_every_node_in_the_neighbourhood():
    out = _run("""
      const nh = gbNeighborhood("element::脾", 2);
      const r = gbFocusPositions(nh);
      process.stdout.write(JSON.stringify({
        n_pos: Object.keys(r.positions).length, n_ids: nh.ids.size,
        n_edges: r.edges.length}));
    """)
    d = json.loads(out)
    assert d["n_pos"] == d["n_ids"], "有节点没拿到坐标，渲染时会堆在原点"
    assert d["n_edges"] >= 4


def test_only_edges_with_both_ends_inside_the_neighbourhood_are_kept():
    """判据跟 `to_graph.add_edge` 是同一个道理：两端节点都存在，边才有意义。"""
    out = _run("""
      const nh = gbNeighborhood("syndrome::B", 1);
      const r = gbFocusPositions(nh);
      process.stdout.write(JSON.stringify(r.edges.map((e) => e.data.id).sort()));
    """)
    ids = json.loads(out)
    assert "e3" not in ids and "e4" not in ids, "邻域外的边被带进来了"


# ---------- 三、面包屑 ----------

def test_the_breadcrumb_starts_with_a_way_back_to_the_whole_graph():
    out = _run("""
      gbFocusStack = ["element::脾", "syndrome::A"];
      process.stdout.write(gbBreadcrumbHtml(gbFocusStack));
    """)
    assert 'data-gb-crumb="-1"' in out and "全图" in out
    assert 'data-gb-crumb="0"' in out and 'data-gb-crumb="1"' in out


def test_the_breadcrumb_marks_the_current_step_and_labels_it_for_screen_readers():
    out = _run("""
      gbFocusStack = ["element::脾", "syndrome::A"];
      process.stdout.write(gbBreadcrumbHtml(gbFocusStack));
    """)
    assert 'aria-current="page"' in out
    assert out.count("is-current") == 1, "当前那一格不止一个"


def test_the_breadcrumb_shows_labels_not_raw_ids():
    out = _run("""
      gbFocusStack = ["syndrome::A"];
      process.stdout.write(gbBreadcrumbHtml(gbFocusStack));
    """)
    assert "脾气虚证" in out
    assert "syndrome::A" not in out


def test_the_breadcrumb_escapes_its_labels():
    """证型名来自数据文件，而这段 HTML 是拼出来的。"""
    out = _run("""
      gbGraphData.graph.nodes.push({data:{id:"syndrome::X",
        label:"<img src=x onerror=alert(1)>", node_type:"syndrome"}});
      gbBuildIndex();
      gbFocusStack = ["syndrome::X"];
      process.stdout.write(gbBreadcrumbHtml(gbFocusStack));
    """)
    assert "<img" not in out and "&lt;img" in out


def test_an_empty_stack_renders_nothing_not_an_empty_bar():
    out = _run('process.stdout.write(JSON.stringify(gbBreadcrumbHtml([])));')
    assert json.loads(out) == ""


def test_the_breadcrumb_is_buttons_not_links():
    """它不导航到别的地址，是就地换视图——`<a href="#">` 会让键盘用户按回车
    跳到页首。"""
    out = _run("""
      gbFocusStack = ["element::脾"];
      process.stdout.write(gbBreadcrumbHtml(gbFocusStack));
    """)
    assert "<button" in out and "<a " not in out


# ---------- 四、进出聚焦的状态 ----------

def test_exiting_focus_restores_the_nodes_that_were_on_screen_before():
    """**不是重铺首屏**：重铺会把用户展开了半天的那些节点全丢掉。"""
    body = GRAPH_JS.split("function gbExitFocus")[1][:700]
    assert "gbOverviewIds" in body
    assert "gbAddNodes(back)" in body
    assert "不是重铺首屏" in body


def test_entering_focus_snapshots_the_overview_only_once():
    """连续往里聚焦两层，快照不能被第二层覆盖——否则退出时恢复的是第一层
    聚焦的那批节点，而不是概览。"""
    body = GRAPH_JS.split("function gbFocus(nodeId)")[1][:600]
    assert "if (!gbInFocus()) gbOverviewIds = new Set(gbVisibleIds);" in body


def test_reset_view_also_clears_the_focus_state():
    """不清的话点「重置视图」之后面包屑还挂着，而画布已经是全图了。"""
    body = GRAPH_JS.split("function gbResetView")[1][:900]
    assert "gbFocusStack = []" in body
    assert "gbOverviewIds = null" in body
    assert "renderGbBreadcrumb()" in body


def test_focus_is_bound_to_double_click_so_it_does_not_steal_single_click():
    """单击已经是"展开/收起"（gbExpandNode），不能抢。"""
    assert 'gbCy.on("dbltap", "node"' in GRAPH_JS
    assert "gbFocus(evt.target.id())" in GRAPH_JS


def test_the_breadcrumb_click_handler_is_delegated_once():
    """每次聚焦都会换掉整串，逐格挂监听等于每次泄一批。"""
    body = GRAPH_JS.split('const crumbs = document.getElementById("gb-breadcrumb")')[1][:500]
    assert "dataset.bound" in body


# ---------- 五、聚焦模式下的说明文字 ----------

def test_the_ring_legend_is_replaced_not_left_describing_the_other_layout():
    """环图例在聚焦模式下没有意义（没有内外圈了）。留着一句描述另一种布局的
    话比没有更糟。"""
    body = GRAPH_JS.split("function gbRenderFocus")[1][:1400]
    assert "gbFocusLegendText(nh)" in body


def test_the_focus_legend_reports_per_column_counts_and_the_truncation():
    out = _run("""
      gbFocusStack = ["element::脾"];
      process.stdout.write(gbFocusLegendText(gbNeighborhood("element::脾", 2)));
    """)
    assert "聚焦" in out and "脾" in out
    assert "第 0 列" in out and "第 2 列" in out


def test_the_focus_legend_says_when_dagre_is_missing():
    """dagre 没加载上时聚焦会退到等距铺开、**可能重叠**——这件事要写在图旁边，
    不是只记在 layoutStats 里（看图的人不开控制台）。"""
    out = _run("""
      gbFocusStack = ["element::脾"];
      process.stdout.write(gbFocusLegendText(gbNeighborhood("element::脾", 2)));
    """)
    assert "dagre 没加载上" in out


# ---------- 六、量具 ----------

def test_the_layout_benchmark_times_both_layouts_on_the_same_node_set():
    """"改用了 dagre"不是一个可核的说法。两种布局必须在**同一批节点**上计时，
    否则那两个数没有可比性（总纲：任何数字都必须带对照）。"""
    body = GRAPH_JS.split("benchLayouts:")[1][:2000]
    assert "gbLayoutPositions(" in body and "computeLayout(nodes, edges)" in body
    assert "ring_ms" in body and "dagre_ms" in body
    assert "n_nodes" in body, "没报规模的耗时数没有意义"


def test_the_benchmark_takes_a_median_not_a_single_run():
    body = GRAPH_JS.split("benchLayouts:")[1][:2000]
    assert re.search(r"runs\s*=\s*\d+", body)
    assert "med(" in body


def test_the_focus_hooks_live_on_gb_perf_not_on_the_tcm_contract():
    """`window.TCM` 的契约是"恰好等于 app.js 真正调用到的那些"。
    量具混进去就是给那条"清单齐全"的测试留一个假绿点。"""
    i = GRAPH_JS.index("window.TCM = Object.assign")
    tcm = GRAPH_JS[i:GRAPH_JS.index("});", i)]
    for name in ("gbFocus", "gbExitFocus", "benchLayouts", "gbNeighborhood"):
        assert name not in tcm, f"{name} 混进了 window.TCM"
    j = GRAPH_JS.index("window.__gbPerf = {")
    perf = GRAPH_JS[j:GRAPH_JS.index("\n};", j)]
    for name in ("focus", "exitFocus", "breadcrumb", "neighborhood", "benchLayouts"):
        assert f"{name}:" in perf, f"__gbPerf 里没有 {name}"
