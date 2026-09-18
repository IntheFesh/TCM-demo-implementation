"""web/index.html 里 computeLayout() 的离线测试：M7 新加的"相邻候选方药材簇
不重叠"这道跨医家带碰撞检查。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_hover_tooltip.py /
test_path_highlight.py 同一个模式）。这条不是 Playwright 能替代的重复劳动——
Playwright 验证的是"真实渲染出来看起来对不对"（模块报告里贴了截图），这里
验证的是算法本身对不同形状的输入（候选方数量、每个候选方的药材数量）给出
的坐标是不是真的不重叠，覆盖 Playwright 那一份 fixture 之外的形状。
"""
import re
import json
import subprocess

from pathlib import Path
from tests.web_harness import DOM_STUB, js_tmp, load_app_js

ROOT = Path(__file__).resolve().parent.parent



def _run_node(js_tail: str) -> str:
    script = load_app_js()
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + script + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _n(node_id, **extra):
    return {"data": {"id": node_id, **extra}}


def _e(source, target, **extra):
    return {"data": {"source": source, "target": target, **extra}}


def _build_two_physician_graph(herb_counts_by_phys):
    """herb_counts_by_phys: {phys: [该医家每个候选方各几味药, ...]}。"""
    nodes = [_n("sym::主诉", layer=0), _n("elem::证素", layer=1)]
    edges = [_e("sym::主诉", "elem::证素")]
    for phys, herb_counts in herb_counts_by_phys.items():
        syn_id = f"syn::{phys}"
        nodes.append(_n(syn_id, layer=2, phys=phys))
        edges.append(_e("elem::证素", syn_id, phys=phys))
        for i, count in enumerate(herb_counts):
            fid = f"formula::{phys}::候选{i}"
            nodes.append(_n(fid, layer=3, phys=phys))
            edges.append(_e(syn_id, fid, phys=phys))
            for j in range(count):
                hid = f"herb::{phys}::候选{i}::药{j}"
                nodes.append(_n(hid, layer=4, phys=phys, parent=fid))
    return nodes, edges


def _layout(nodes, edges):
    js = f"""
    const positions = computeLayout({json.dumps(nodes, ensure_ascii=False)}, {json.dumps(edges, ensure_ascii=False)});
    process.stdout.write(JSON.stringify(positions));
    """
    return json.loads(_run_node(js))


def _herb_span_of(positions, herb_ids):
    ys = [positions[h]["y"] for h in herb_ids]
    return min(ys), max(ys)


def test_adjacent_formulas_across_two_physicians_do_not_overlap_with_many_herbs():
    """M7 真实截图抓到的 bug：一位医家带内最后一个候选方跟下一位医家带内
    第一个候选方之间只隔着固定的 40（gap 常量），候选方药材多的时候
    （比如 5 味）两个方框会在画面上压住。构造一个容易触发这个边界情况的
    输入（两位医家各 2 个候选方，每个候选方 5 味药）。

    如实说明这条测试能覆盖到什么、覆盖不到什么：这里只验证"跨医家带的
    碰撞检查"这个几何算法本身——两个候选方各自的药材簇（纯模型坐标，不
    含 cytoscape 实际渲染尺寸）的 y 范围不重叠，这是 FORMULA_MARGIN 只要
    大于 0 就恒成立的必要条件，不需要跑到 60 才过。真正让 20 不够、必须
    调到 60 的原因是 compound 父节点样式里 `padding: "14px"`——这个 px
    后缀在 cytoscape 里是固定屏幕像素、不随 cy.fit() 的缩放系数一起缩小，
    内容越多缩得越小、这份固定像素的 padding 占比就越大，纯 JSON 断言看
    不到这一层（这里跑的是 computeLayout() 的返回值，不涉及任何真实
    cytoscape 渲染或缩放），只有真实渲染出来量像素才看得出来——这正是
    模块报告里贴 Playwright 截图、把 20 调到 60 的依据，不是靠这条测试。
    这条测试仍然值得留着：它钉住"跨医家带的碰撞检查这个算法本身没退化
    成误判成重叠/漏判"，是 Playwright 那次性验证之外的常规回归保护。"""
    nodes, edges = _build_two_physician_graph({
        "ye_tianshi": [5, 5],
        "wu_jutong": [5, 5],
    })
    positions = _layout(nodes, edges)

    formula_ids = [n["data"]["id"] for n in nodes if n["data"]["layer"] == 3]
    formula_ids.sort(key=lambda fid: positions[fid]["y"])

    # 每个候选方自己的药材 y 范围（含节点本身估算的半个身位，这里用一个
    # 保守的最小安全余量代替真实渲染尺寸——这条测试不依赖 cytoscape 实际
    # 渲染出的像素高度，只验证"相邻两个候选方藏材簇的 y 范围之间确实留了
    # 正的间隔"，不要求间隔多大，真实像素级验证见模块报告的 Playwright 截图）。
    spans = []
    for fid in formula_ids:
        herb_ids = [
            n["data"]["id"] for n in nodes
            if n["data"]["layer"] == 4 and n["data"].get("parent") == fid
        ]
        lo, hi = _herb_span_of(positions, herb_ids)
        spans.append((fid, lo, hi))

    for (fid_a, _, hi_a), (fid_b, lo_b, _) in zip(spans, spans[1:]):
        assert lo_b > hi_a, (
            f"{fid_a}（药材 y 上界 {hi_a}）跟 {fid_b}（药材 y 下界 {lo_b}）"
            "的药材簇在 y 轴上重叠了"
        )


def test_single_formula_per_physician_still_lays_out_without_error():
    """每位医家只有 1 个候选方（真实产出里也会发生，比如模型只给了一个方）
    时，跨医家带碰撞检查不能因为"只有一个候选方、没有同带内的邻居"就报错
    或者算出 NaN。"""
    nodes, edges = _build_two_physician_graph({
        "ye_tianshi": [8],
        "wu_jutong": [8],
    })
    positions = _layout(nodes, edges)
    for n in nodes:
        pos = positions.get(n["data"]["id"])
        assert pos is not None
        assert not (pos["y"] != pos["y"])  # NaN != NaN 恒真，用来判 NaN


def test_formula_with_no_herbs_gets_zero_span_and_does_not_break_neighbors():
    """极端情况：某个候选方一味药都没有（herb_items 理论上有 min_length=1
    约束不应该发生，但布局函数不能假设后端契约永远不出错，得兜住）。"""
    nodes, edges = _build_two_physician_graph({
        "ye_tianshi": [0, 6],
        "wu_jutong": [6],
    })
    positions = _layout(nodes, edges)
    formula_ids = [n["data"]["id"] for n in nodes if n["data"]["layer"] == 3]
    for fid in formula_ids:
        assert fid in positions


# ---------- R16：两张图共用一份样式表（§3.2 规格 7） ----------


def test_there_is_only_one_stylesheet_builder():
    """R16 之前有两份：`buildStylesheet()`（问诊图）和
    `buildGraphBrowserStylesheet()`（浏览器）。基础节点样式、边样式、证素的紫、
    医案的橙……逐条重复——**同样的值写两遍，就是下一次只改一遍的开始**。

    差异只在一个参数：问诊图按医家染色，浏览器不染（那张图上没有"这是谁的
    判断"这回事，染了只会误导）。"""
    src = _graph_js()
    assert src.count("function buildStylesheet") == 1
    assert "function buildGraphBrowserStylesheet" not in src
    assert "buildStylesheet({ physicianColors: PHYSICIAN_COLORS })" in src


def test_the_browser_gets_the_same_stylesheet_without_physician_colours():
    """浏览器调的是同一个函数、不传 physicianColors。传了的话国标证型会被
    染成某位医家的颜色——那是在说"这个国标证型是叶天士的"，而它不是。

    R37 起这个调用多带一个 `slot: "browser"`（label 宽度与字号按槽位取），
    **判据跟着改，问的还是同一件事**：同一个 builder + 不传医家色。
    写死 `buildStylesheet()` 那种"一个字都不许多"的断言会把"加一个跟染色
    无关的参数"也判成违规，而那不是这条要防的事。"""
    src = _graph_js()
    body = src[src.index("function ensureGraphBrowserCanvas"):]
    body = body[:body.index("function gbBuildIndex")]
    assert "style: buildStylesheet({ slot: \"browser\" })," in body
    # 只看代码行：注释里提到 physicianColors 是在解释"为什么不传"，
    # 不该让这条断言反过来劝人别写注释。
    code = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    assert "physicianColors" not in code


def test_node_colours_come_from_css_tokens_not_from_literals():
    """cytoscape 读不到 CSS 变量，所以颜色要在 JS 里取一次计算值——**但那是
    "读出来交给 cytoscape"，不是第二处定义**。整份 graph.js 里一个十六进制
    色值都不该有。

    R16 实测踩到的：给 `cssVar` 写兜底值 `cssVar("--verified", "#2C5F5A")`，
    而那个值恰好等于叶天士的身份色，
    `test_the_frontend_injects_them_instead_of_hard_coding` 当场红。
    那条断言是对的，改法是**不写兜底**：取不到就把这一条样式整个略掉
    （`pick()`），cytoscape 用它自己的默认值。"""
    import re
    src = _graph_js()
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("//"))
    assert not re.findall(r"#[0-9a-fA-F]{6}\b", code), "graph.js 里还有写死的色值"
    assert "function cssVar" in src and "function pick" in src


def test_formula_border_encodes_the_three_sources():
    """§3.2 规格 1：classic 实线 / modified 虚线 / composed 点线。
    "这是仲景的方还是他自己拟的"一眼可辨。classic 走默认实线不另写规则。"""
    src = _graph_js()
    assert 'node[node_type = "formula"][source = "modified"]' in src
    assert 'node[node_type = "formula"][source = "composed"]' in src


def test_elements_are_typographically_the_hub():
    """§3.2 规格 11：节点 label 13px 黑体，证素 15px 宋体 600。证素比别的节点
    大一号，因为**它是图谱浏览器的枢纽**，在问诊图上也是"症状收敛到哪里"那一层。

    R37 把字号绑到槽位（`nodeFontFor` / `elementFontFor` 读 CSS 令牌），所以
    这里不再查那两个常量名出现在样式块里，而是**查真正在用的那几个值**：
    两张图各自的证素字号都要严格大于它自己的节点字号。常量只剩兜底作用
    （令牌取不到时），那一层也一起查。"""
    import re
    src = _graph_js()
    assert "const NODE_FONT_SIZE = 13;" in src
    assert "const ELEMENT_FONT_SIZE = 15;" in src
    block = src[src.index('node[node_type = "element"]'):]
    block = block[:block.index("},")]
    assert "elementFontFor(slot)" in block and '"font-weight": 600' in block
    # 令牌层：两张图各自都要"证素大一号"。这是规格说的那件事，
    # 而它现在写在 app.css 里，不在 graph.js 里。
    css = (Path(__file__).resolve().parent.parent / "web" / "app.css").read_text(encoding="utf-8")
    def token(name: str) -> float:
        m = re.search(rf"{name}:\s*([0-9.]+)px", css)
        assert m, f"app.css 里没有 {name}"
        return float(m.group(1))
    for slot in ("consult", "browser"):
        assert token(f"--element-font-{slot}") > token(f"--node-font-{slot}"), \
            f"{slot} 这张图上证素没有比别的节点大"


def test_the_lambda1_note_text_has_exactly_one_source():
    """§3.2 规格 2：图上那一行说明的文字来自 `offline/graph_stats.lambda1_note()`。
    前端两处（问诊图、浏览器）都只是原样显示——那段话是这个项目的一个真实
    发现，改写或精简它比图上有 bug 更严重。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    hits = [str(p.relative_to(root))
            for p in list(root.glob("core/*.py")) + list(root.glob("offline/*.py"))
            if "def lambda1_note" in p.read_text(encoding="utf-8")]
    assert hits == ["offline/graph_stats.py"]
    app = (root / "web" / "app.js").read_text(encoding="utf-8")
    assert "renderConsultLambda1Note" in app
    assert "health.lambda1_note" in app


def test_dagre_reserves_at_least_as_much_compound_padding_as_cytoscape_draws():
    """R42：`FORMULA_MARGIN = 60` 那个常量随手写布局一起退役了，但它防的那件事
    没有退役——**compound 的 padding 是固定屏幕像素**（不随缩放变化），
    所以"药材中心间距够了"不等于"方框边缘不重叠"。

    现在的形式是两个常量的不等式：dagre 按 `COMPOUND_PAD` 留位、cytoscape 按
    `COMPOUND_RENDER_PAD` 画框，**留得比画得少就会压住相邻的候选方**。
    这条比原来那条强：原来只查"注释里提到了 20 和 60"，改个数照样绿。"""
    src = _graph_js()
    render = int(re.search(r"const COMPOUND_RENDER_PAD = (\d+);", src).group(1))
    reserve_expr = re.search(r"const COMPOUND_PAD = (.+?);", src).group(1)
    # 表达式必须是"从渲染内边距推出来的"，不是又写一个字面量
    assert "COMPOUND_RENDER_PAD" in reserve_expr, (
        f"COMPOUND_PAD 又写成了字面量（{reserve_expr}）——两个数会漂")
    reserve = eval(reserve_expr.replace("COMPOUND_RENDER_PAD", str(render)))  # noqa: S307
    assert reserve >= render, f"dagre 留 {reserve}px、cytoscape 画 {render}px，方框会压住"
    # 样式表那边也必须引用同一个常量，不是再写一个 14px
    assert 'padding: `${COMPOUND_RENDER_PAD}px`' in src, (
        "node:parent 的 padding 又写成了字面量")


def _graph_js():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / "web" / "graph.js").read_text(encoding="utf-8")
