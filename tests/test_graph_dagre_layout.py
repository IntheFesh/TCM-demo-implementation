"""R42：布局换成 dagre，**结构性**解决标签重叠。

## 为什么这件事值得一个文件

改之前是一套手写的分层布局：横向位置由 `LAYER_X` 写死，纵向由 `evenY` 等距分。
它的毛病不是不好看，是**它不知道标签有多宽**——一个五个字的证型名比一个两个字
的证素宽一倍多。R24 为此加了一串"标签上限"常量去压字，而 R37 又实测出
`text-max-width` 断不了中文（只在空白/换行处断行），于是那串常量既压不动最宽的
标签、又让别的标签白挨一刀。

dagre 的做法是反过来的：**把每个节点的真实盒子尺寸告诉布局算法**，由它保证
同层之间留 `nodesep`、层间留 `ranksep`。重叠因此是结构上不可能。

## 这个文件测得到什么、测不到什么

测得到：尺寸估算**偏大不偏小**、compound 留位 ≥ 渲染内边距、dagre 取不到时
退回兜底而不是崩、Worker 里 dagre 先 import、同一份输入两次结果一致。

测不到：**真实的零重叠**。dagre 按 `measureLabel()` 的估算留位，而估算与浏览器
真实量出来的包围盒不是一回事——那是 `window.__graphPerf.overlapStats()` 在
Playwright 里读真实 `renderedBoundingBox` 的活。CLAUDE.md 那条硬约定说的就是
这件事：改了层结构，JSON/源码测试全绿也不算过。
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from tests.web_harness import DOM_STUB, js_tmp

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
GRAPH_JS = (WEB / "graph.js").read_text(encoding="utf-8")
VENDOR = WEB / "vendor" / "dagre"


def _load_dagre_js() -> str:
    """把 vendor 里那份 dagre 变成 node 里的一个全局 `dagre`。

    **真的加载那个文件，不 mock**：一个假的 dagre 只能证明「调用了它」，
    证明不了布局算出来的坐标是对的。

    那份 bundle 是 UMD，在 node 里会走 `module.exports = f()` 那一支，于是
    压根不会有全局 `dagre`（直接拼进来的表现是 `dagre is not defined`）。
    所以用一个函数作用域把 `module` / `exports` / `define` 三个名字**局部化**，
    让它照常走 CJS 那一支，再把 `module.exports` 取出来挂成全局——
    这跟浏览器里 `<script src>` 的效果等价（那时 `g = window`，它自己挂全局）。
    """
    src = (VENDOR / "dagre.min.js").read_text(encoding="utf-8")
    return ("globalThis.dagre = (function () {\n"
            "  const module = { exports: {} };\n"
            "  const exports = module.exports;\n"
            "  const define = undefined;\n"
            f"{src}\n"
            "  return module.exports;\n"
            "})();\n")


def _run(tail: str, with_dagre: bool = False) -> str:
    head = DOM_STUB
    if with_dagre:
        head += _load_dagre_js()
    proc = subprocess.run(["node", js_tmp(head + GRAPH_JS + "\n" + tail)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"node 失败：\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _const(name: str) -> float:
    m = re.search(rf"const {name} = ([\d.]+);", GRAPH_JS)
    assert m, f"没有常量 {name}"
    return float(m.group(1))


# ---------- 一、vendor 里的两个文件真的在 ----------

def test_the_dagre_bundle_is_vendored_not_loaded_from_a_cdn():
    """三甲内网取不到 cdnjs，而"断网可用"是录制回放这条路线存在的理由。"""
    assert (VENDOR / "dagre.min.js").exists()
    assert (VENDOR / "dagre.min.js").stat().st_size > 100_000, "文件太小，可能是个占位"
    # **只看代码行，不看注释**（同 CLAUDE.md 那条数 Field(min_length=1) 的规矩）：
    # graph.js 的注释里必须能提 cdnjs——那段话正是在解释为什么把 CDN 那一路删了。
    code = "\n".join(ln.split("//")[0] for ln in GRAPH_JS.split("\n"))
    for bad in ("cdnjs", "unpkg", "jsdelivr"):
        assert bad not in code, f"graph.js 又从 {bad} 取东西了"


def test_both_licences_are_kept_next_to_the_bundle():
    """MIT 要求保留版权声明。**不是形式**：这份代码会随交付包进医院。"""
    for name in ("LICENSE.dagre", "LICENSE.cytoscape-dagre"):
        text = (VENDOR / name).read_text(encoding="utf-8")
        assert "MIT" in text and "Copyright" in text, name


def test_dagre_is_loaded_lazily_like_cytoscape():
    """图谱页不开的人不该为这 280 KB 等待。"""
    assert "_loadScript(DAGRE_SRC" in GRAPH_JS
    assert "function ensureDagre()" in GRAPH_JS
    head = (WEB / "index.html").read_text(encoding="utf-8").split("<body")[0]
    assert "dagre" not in head


def test_dagre_is_optional_but_cytoscape_is_not():
    """两个库的失败后果不同：cytoscape 取不到图整个画不出来（硬失败），
    dagre 取不到只是布局退回等距铺开（图还能看）。**这个区别必须体现在返回值上**
    ——都当硬失败的话，一次 dagre 404 会让整页显示"图谱库没加载"。"""
    body = GRAPH_JS.split("async function ensureGraphLibs()")[1][:400]
    assert "const [hasCy]" in body, "返回值没有只取 cytoscape 那一个"
    assert "return hasCy" in body


# ---------- 二、尺寸估算宁可偏大 ----------

def test_the_label_estimate_errs_large_never_small():
    """估小了 dagre 留的间距不够，重叠又回来了。CJK 按一个字宽算是**上界**
    （真实字宽 ≈ 字号，不会更宽），ASCII 0.55 倍同理。"""
    out = _run("""
      const cjk = measureLabel("脾胃气虚证", 15);
      const ascii = measureLabel("abcde", 15);
      process.stdout.write(JSON.stringify({cjk, ascii}));
    """)
    d = json.loads(out)
    # 五个 CJK 字 × 15px + 左右内边距
    assert d["cjk"]["w"] >= 5 * 15, d["cjk"]
    # 同样字数的 ASCII 必须窄一些，否则这个估算在英文药名上白留一大片
    assert d["ascii"]["w"] < d["cjk"]["w"]


def test_the_estimate_has_a_floor_so_a_one_character_node_is_still_clickable():
    out = _run("""
      process.stdout.write(JSON.stringify(measureLabel("肝", 15)));
    """)
    d = json.loads(out)
    assert d["w"] >= _const("NODE_MIN_W")
    assert d["h"] >= _const("NODE_MIN_H")


def test_an_empty_label_does_not_collapse_the_box():
    for arg in ('""', "null", "undefined"):
        d = json.loads(_run(f"process.stdout.write(JSON.stringify(measureLabel({arg}, 13)));"))
        assert d["w"] >= _const("NODE_MIN_W") and d["h"] >= _const("NODE_MIN_H")


def test_the_font_size_table_covers_all_nine_layers():
    """dagre 按一个字号留位、渲染按另一个字号画，重叠就回来了——
    所以这张表必须跟九层一一对应。"""
    from api.main import CHAIN_LAYERS

    out = _run("process.stdout.write(JSON.stringify(LAYER_FONT_SIZE));")
    table = {int(k): v for k, v in json.loads(out).items()}
    assert set(table) == {n for n, _, _ in CHAIN_LAYERS}
    for layer, size in table.items():
        assert 9 <= size <= 20, f"layer {layer} 的字号 {size} 不合理"


def test_the_layer_font_size_is_what_the_stylesheet_uses():
    """**同一个数只能有一处**。样式表里再写一遍字号 = 留位与渲染两套尺寸。"""
    body = GRAPH_JS.split("if (slot !== \"browser\") {")[1][:900]
    assert "layerFontSize(layer)" in body, "样式表没引用同一个函数"


# ---------- 三、compound 留位 ----------

def test_dagre_reserves_no_less_than_cytoscape_draws():
    render = _const("COMPOUND_RENDER_PAD")
    expr = re.search(r"const COMPOUND_PAD = (.+?);", GRAPH_JS).group(1)
    assert "COMPOUND_RENDER_PAD" in expr, f"又写成字面量了：{expr}"
    reserve = eval(expr.replace("COMPOUND_RENDER_PAD", str(render)))  # noqa: S307
    assert reserve >= render


def test_compound_parents_get_no_explicit_position():
    """cytoscape 自己按子节点算父节点的包围盒，给了父节点坐标会跟它算出来的
    打架（父节点被拉走、子节点留在原地）。"""
    body = GRAPH_JS.split("function computeLayout")[1][:5000]
    assert "childrenOf.get(it.id)" in body
    assert "if (kids && kids.length) {" in body and "continue;" in body


def test_the_children_are_kept_out_of_the_dagre_graph_entirely():
    """dagre 0.8.5 **不能处理"一条边的端点是 compound 父节点"**，而方剂节点
    正好同时是 compound 父和 治则/治法→方剂 那条边的终点。

    这条测试钉住的是那个避法：`compound: false`（dagre 图里压根没有父子关系）
    + 子节点不 setNode + 父节点的尺寸按"装得下全部子节点"算。
    **少了任何一项，`dagre.layout` 会抛，而 try/catch 会静默退到等距铺开**
    ——图还画得出来，只是重叠回来了、没有任何报错。"""
    body = GRAPH_JS.split("function computeLayout")[1][:5000]
    assert "compound: false" in body, "dagre 图又开了 compound"
    assert "if (d.parent && byId.has(d.parent)) continue;" in body, "子节点又进了 dagre"
    assert "COMPOUND_ROW_H" in body and "COMPOUND_TITLE_H" in body


def test_the_reason_survives_the_trip_back_through_layout_async():
    """`layoutAsync` 的 `done()` 会写 `layoutStats.fallback_reason`。
    **它不许把 computeLayout 自己记的那个原因覆盖掉**——两层回落各自独立
    （Worker 起不来 / dagre 没加载上），诊断价值也不同：前者说线程，
    后者说图会不会重叠。二选一的写法会让"图为什么重叠"在 layoutStats 里查不到。"""
    body = GRAPH_JS.split("async function layoutAsync")[1][:1600]
    assert "[reason, _computeFallbackReason].filter(Boolean)" in body
    # 真的走一遍 layoutAsync（没有 dagre、也没有 Worker），看原因有没有留下来。
    out = _run("""
      layoutAsync([{data:{id:'a',layer:0,label:'甲'}}], []).then(() => {
        process.stdout.write(JSON.stringify(layoutStats));
      });
    """)
    assert "dagre 没加载上" in out, out
    assert "Worker 建不起来" in out, "另一层的原因被盖掉了"
    assert '"where":"main"' in out, out


def test_the_catch_records_why_it_fell_back_instead_of_swallowing_it():
    """那条 try/catch 曾经掩盖过上面那个 dagre bug 整整一轮：布局抛异常 →
    静默退到 fallback → 图还能看 → 没人发现。**回落必须留痕**。"""
    body = GRAPH_JS.split("dagre.layout(g);")[1][:800]
    assert "_computeFallbackReason" in body
    assert "dagre.layout 抛异常" in body
    # 每次进来先清空，否则上一次的原因会挂在这一次头上。
    head = GRAPH_JS.split("function computeLayout(nodes, edges) {")[1][:200]
    assert "_computeFallbackReason = null;" in head


# ---------- 四、兜底 ----------

def test_without_dagre_the_layout_falls_back_instead_of_throwing():
    out = _run("""
      const nodes = [{data:{id:'a',layer:0,label:'甲'}},{data:{id:'b',layer:1,label:'乙'}}];
      const pos = computeLayout(nodes, [{data:{source:'a',target:'b'}}]);
      process.stdout.write(JSON.stringify({dagre: dagreAvailable(), pos}));
    """)
    d = json.loads(out)
    assert d["dagre"] is False
    assert set(d["pos"]) == {"a", "b"}, "兜底也要给出每个节点的坐标"


def test_the_fallback_is_documented_as_not_an_equivalent_implementation():
    """兜底**不保证不重叠**。把它写成"等价实现"的话，dagre 静默没加载上时
    没人会发现——而表现只是"图有点挤"。"""
    body = GRAPH_JS.split("function fallbackLayout")[0][-900:]
    assert "不保证不重叠" in body
    assert "fallback_reason" in body


def test_the_worker_imports_dagre_before_graph_js():
    body = GRAPH_JS.split("function _layoutWorker_()")[1][:1600]
    assert "_dagreUrlFrom(url)" in body
    assert "[dagreUrl, url]" in body, "顺序不是「先库、后用库的人」"
    assert "[url]" in body, "dagre 的 URL 推不出来时没有只 import graph.js 的那一路"


def test_the_dagre_url_is_derived_from_the_graph_js_url_not_hardcoded():
    """部署可能挂在子路径下——写死绝对路径的表现是 Worker 里 dagre 一直取不到，
    而 layoutAsync 会静默回落到主线程。"""
    body = GRAPH_JS.split("function _dagreUrlFrom")[1][:400]
    assert "new URL(DAGRE_SRC, graphJsUrl)" in body


# ---------- 五、真的跑一遍 dagre ----------

def test_with_dagre_the_nine_layers_come_out_in_ascending_x_order():
    """rankdir=LR：层号越大越靠右。**这条是"分层"这件事本身的判据**——
    dagre 跑出来但层序乱了的话，图上看不出链条的方向。"""
    nodes = [{"data": {"id": f"n{i}", "layer": i, "label": f"层{i}"}} for i in range(9)]
    edges = [{"data": {"source": f"n{i}", "target": f"n{i+1}"}} for i in range(8)]
    out = _run(f"""
      const pos = computeLayout({json.dumps(nodes)}, {json.dumps(edges)});
      process.stdout.write(JSON.stringify({{avail: dagreAvailable(), pos}}));
    """, with_dagre=True)
    d = json.loads(out)
    assert d["avail"] is True, "vendor 里那份 dagre 没跑起来"
    xs = [d["pos"][f"n{i}"]["x"] for i in range(9)]
    assert xs == sorted(xs), xs
    assert len(set(xs)) == 9, f"九层没有分成九列：{xs}"


def test_with_dagre_same_layer_nodes_do_not_share_a_y():
    """同层节点的纵向间距是 `nodesep` 的活。**这不是零重叠的判据**
    （真实包围盒要 Playwright 量），但 y 全相同就说明 dagre 压根没起作用。"""
    nodes = [{"data": {"id": f"s{i}", "layer": 0, "label": f"症状{i}"}} for i in range(6)]
    out = _run(f"""
      const pos = computeLayout({json.dumps(nodes)}, []);
      process.stdout.write(JSON.stringify(pos));
    """, with_dagre=True)
    ys = sorted(v["y"] for v in json.loads(out).values())
    assert len(set(ys)) == 6
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert min(gaps) >= _const("DAGRE_NODESEP") * 0.9, f"同层间距太小：{gaps}"


def test_with_dagre_the_layout_is_deterministic():
    """并行/异步化最容易出的 bug 是结果变了。`ranker: network-simplex` 是确定性的
    ——同一份输入跑两遍必须逐字节相同。"""
    nodes = [{"data": {"id": f"n{i}", "layer": i % 4, "label": f"节点{i}"}} for i in range(12)]
    edges = [{"data": {"source": f"n{i}", "target": f"n{i+1}"}} for i in range(11)]
    out = _run(f"""
      const a = JSON.stringify(computeLayout({json.dumps(nodes)}, {json.dumps(edges)}));
      const b = JSON.stringify(computeLayout({json.dumps(nodes)}, {json.dumps(edges)}));
      process.stdout.write(a === b ? "SAME" : a + "\\n!=\\n" + b);
    """, with_dagre=True)
    assert out == "SAME", out


def test_a_compound_child_lands_inside_its_parents_span():
    """方剂(7) 是 compound 父、君臣佐使(8) 是子。子节点的 x 必须在父节点这一列
    右边（它是下一层），而父节点**不给坐标**（见上面那条）。"""
    nodes = [
        {"data": {"id": "principle::健脾", "layer": 5, "label": "健脾益气"}},
        {"data": {"id": "formula::四君子汤", "layer": 7, "label": "四君子汤"}},
        {"data": {"id": "herb::四君子汤::党参", "layer": 8, "label": "党参",
                  "parent": "formula::四君子汤"}},
    ]
    edges = [{"data": {"source": "principle::健脾", "target": "formula::四君子汤"}}]
    d = json.loads(_run(f"""
      process.stdout.write(JSON.stringify(
        computeLayout({json.dumps(nodes)}, {json.dumps(edges)})));
    """, with_dagre=True))
    assert "formula::四君子汤" not in d, "compound 父节点又给了坐标"
    assert d["herb::四君子汤::党参"]["x"] > d["principle::健脾"]["x"]


# ---------- 六、taxi 边只给问诊图 ----------

def test_the_consult_graph_uses_taxi_edges_and_the_browser_keeps_bezier():
    """taxi 只在"分层有向图"上讲得通：它把边画成"先横走、中间转折、再横走"，
    读起来就是"这一层流到下一层"。图谱浏览器那张图是同心圆/力导向，
    节点没有层的概念，直角折线在上面会画出一堆莫名的拐弯。"""
    body = GRAPH_JS.split('selector: "edge",')[1][:1800]
    assert 'slot === "browser" ? "bezier" : "taxi"' in body
    assert '"taxi-direction": "rightward"' in body, "rankdir=LR 时主方向必须向右"


def test_the_taxi_turn_distance_is_not_the_cytoscape_default():
    """默认的 2px 会让相邻两层的折线贴到节点边上。"""
    m = re.search(r'"taxi-turn-min-distance": (\d+)', GRAPH_JS)
    assert m and int(m.group(1)) >= 6, "taxi 的转折最小距离太小"


def test_the_fit_padding_is_big_enough_for_the_outermost_labels():
    """LR 布局下最左一列（症状）和最右一列（药名）的标签会贴到画布边上。
    24px 时药名会被切掉半个字（R24 那个值是五层时代的）。"""
    assert _const("FIT_PADDING") >= 30
    assert "cy.fit(undefined, FIT_PADDING)" in GRAPH_JS


# ---------- 七、零重叠/零穿越的量具本身要正确 ----------

def test_the_overlap_metric_does_not_count_a_parent_containing_its_child():
    """compound 父节点与它自己的子节点必然相交——**那是包含不是重叠**。
    不排除的话这个量具永远报一堆假重叠，于是没人会再看它。"""
    body = GRAPH_JS.split("function overlapStats")[1][:1200]
    assert "A.parent === B.id || B.parent === A.id" in body


def test_the_overlap_metric_only_compares_within_a_layer():
    body = GRAPH_JS.split("function overlapStats")[1][:1200]
    assert "byLayer" in body


def test_the_crossing_metric_ignores_edges_sharing_an_endpoint():
    """同一个节点出来的两条边在起点必然共点，那不是"线交叉"。"""
    body = GRAPH_JS.split("function _seg(")[1][:700]
    assert "same(p, r) || same(p, s) || same(q, r) || same(q, s)" in body


def test_the_metrics_are_exposed_for_playwright_not_in_the_tcm_contract():
    i = GRAPH_JS.index("window.__graphPerf = {")
    perf = GRAPH_JS[i:GRAPH_JS.index("};", i)]
    for name in ("overlapStats", "crossingStats", "boundingBoxes", "bands"):
        assert name in perf
    j = GRAPH_JS.index("window.TCM = Object.assign")
    tcm = GRAPH_JS[j:GRAPH_JS.index("});", j)]
    assert "overlapStats" not in tcm


# ---------- 八、导出 PNG ----------

def test_the_png_export_is_full_graph_and_two_x():
    """截屏只能拿到视口里的半条链；1 倍在投影仪和打印上是糊的。"""
    body = GRAPH_JS.split("function exportGraphPng")[1][:1200]
    assert "full: true" in body
    assert _const("PNG_SCALE") >= 2


def test_the_png_background_comes_from_a_css_token_with_no_literal_fallback():
    """graph.js 里一个十六进制都不许有（tests/test_graph_layout.py 钉住）。"""
    body = GRAPH_JS.split("function exportGraphPng")[1][:1200]
    assert 'cssVar("--paper")' in body
    assert not re.search(r"#[0-9a-fA-F]{6}\b", body)


def test_the_png_filename_has_no_colon_because_windows_rejects_it():
    body = GRAPH_JS.split("function pngStamp")[1][:600]
    assert ":" not in body.split("return")[1].split(";")[0], "时间戳里带冒号"
    assert "padStart" in body


@pytest.mark.parametrize("hint", ["", "胃脘胀痛"])
def test_the_png_export_reports_instead_of_silently_doing_nothing(hint):
    """没有 cy 实例时要喊一声，不是静默返回——"点了没反应、不报错"是这个项目
    一直在防的那种失败。"""
    out = _run(f"""
      let said = null;
      setGraphHooks({{ onError: (m) => {{ said = m; }} }});
      const r = exportGraphPng(null, {json.dumps(hint)});
      process.stdout.write(JSON.stringify({{r, said}}));
    """)
    d = json.loads(out)
    assert d["r"] is None and d["said"], d
