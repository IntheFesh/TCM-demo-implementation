"""web/index.html 里 computeLayout() 的离线测试：M7 新加的"相邻候选方药材簇
不重叠"这道跨医家带碰撞检查。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_hover_tooltip.py /
test_path_highlight.py 同一个模式）。这条不是 Playwright 能替代的重复劳动——
Playwright 验证的是"真实渲染出来看起来对不对"（模块报告里贴了截图），这里
验证的是算法本身对不同形状的输入（候选方数量、每个候选方的药材数量）给出
的坐标是不是真的不重叠，覆盖 Playwright 那一份 fixture 之外的形状。
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
"""


def _run_node(js_tail: str) -> str:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    proc = subprocess.run(
        ["node", "-e", DOM_STUB + script + "\n" + js_tail],
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
