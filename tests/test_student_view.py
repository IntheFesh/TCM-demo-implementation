"""R15：学生模式的三样东西（docs/DESIGN.md §3.4）。

    一、君臣佐使分组显示，君药加粗（已有，这里只钉住它没退化）
    二、点症状的路径高亮——三跳，其余 0.25 透明度，220ms
    三、推理过程默认展开（researcher 是默认折叠）

第二条是"最能说明系统在推理不是在检索"的交互（§3.4 原话），所以它的三个
参数——**跳数 3、透明度 0.25、时长 220ms**——都要各有一条断言。三个里任何
一个漂了，界面上都只是"看起来有点不一样"，不会报错。

真实 cytoscape 里的透明度由 `scripts/screenshot_states.py --only student_highlight`
验（那里读的是渲染后的 `style('opacity')`）；这里验的是这三个常量本身和
计算高亮集合的纯函数。
"""
import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js, load_css

APP = load_app_js()


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + APP + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


# 症状 → 证素 → 证型 → 方剂，两条互不相交的链。
GRAPH = {
    "nodes": [
        {"data": {"id": "sym::胃脘胀痛", "layer": 0}},
        {"data": {"id": "sym::口苦", "layer": 0}},
        {"data": {"id": "el::肝郁", "layer": 1}},
        {"data": {"id": "el::湿热", "layer": 1}},
        {"data": {"id": "syn::ye", "layer": 2}},
        {"data": {"id": "syn::wu", "layer": 2}},
        {"data": {"id": "formula::ye::柴胡疏肝散", "layer": 3}},
        {"data": {"id": "formula::wu::龙胆泻肝汤", "layer": 3}},
        {"data": {"id": "herb::ye::柴胡", "layer": 4}},
    ],
    "edges": [
        {"data": {"source": "sym::胃脘胀痛", "target": "el::肝郁"}},
        {"data": {"source": "sym::口苦", "target": "el::湿热"}},
        {"data": {"source": "el::肝郁", "target": "syn::ye"}},
        {"data": {"source": "el::湿热", "target": "syn::wu"}},
        {"data": {"source": "syn::ye", "target": "formula::ye::柴胡疏肝散"}},
        {"data": {"source": "syn::wu", "target": "formula::wu::龙胆泻肝汤"}},
        {"data": {"source": "formula::ye::柴胡疏肝散", "target": "herb::ye::柴胡"}},
    ],
}
G = json.dumps(GRAPH, ensure_ascii=False)


def test_the_highlight_stops_at_the_formula_layer_however_long_the_chain_is():
    """**到方剂层为止**——这个交互要说明的是"这个症状把几家推到了哪几个方子上"，
    不是"这个症状连着哪些药"。

    R42 把判据从"三跳"改成"到这个 node_type 为止"：原来写死 `hop < 3`，
    那正好是五层时代的 症状→证素→证型→方剂；九层之后同一条链多了治则（和
    结构化 S3 才有的病机/治法靶位），三跳只到治则，**方剂反而被淡掉**，
    表现是"点了症状，方子暗了"。层号会变，`node_type` 不会。"""
    out = json.loads(_run(
        f'const r = computeHighlightPath({G}.nodes, {G}.edges, "sym::胃脘胀痛");'
        'process.stdout.write(JSON.stringify([...r.nodeIds].sort()));'))
    assert out == sorted(["sym::胃脘胀痛", "el::肝郁", "syn::ye",
                          "formula::ye::柴胡疏肝散", "herb::ye::柴胡"])
    # 方剂**进**集合但不再往下展开：这份 fixture 里方剂→药材是一条真边
    # （R42 的真实图里药材是 compound 子节点、没有这条边），所以药材会被这一跳
    # 带进来，但它不会成为下一跳的起点——链条到此为止。
    body = _graph_js()
    assert "HIGHLIGHT_STOP_TYPES" in body
    # **只看代码行**：注释里必须能提到 `hop < 3`，那正是在解释为什么不该那么写
    # （同 CLAUDE.md 那条数 Field(min_length=1) 的规矩）。
    code = "\n".join(ln.split("//")[0] for ln in body.split("\n"))
    assert "hop < 3" not in code, "又写死跳数了"


def test_the_other_chain_stays_out_of_the_highlight():
    """对照：另一条链一个节点都不该进来。没有这条，"三跳"在"全图都亮"
    这种实现下也会绿。"""
    out = json.loads(_run(
        f'const r = computeHighlightPath({G}.nodes, {G}.edges, "sym::胃脘胀痛");'
        'process.stdout.write(JSON.stringify([...r.nodeIds]));'))
    for other in ("sym::口苦", "el::湿热", "syn::wu", "formula::wu::龙胆泻肝汤"):
        assert other not in out


def test_only_edges_with_both_ends_lit_stay_lit():
    """高亮的是**一条路径**，不是"一堆节点"。两端都在集合里的边才算在路径上
    ——否则会出现一条边亮着、另一端却是淡的，图看起来像断了。"""
    out = json.loads(_run(
        f'const r = computeHighlightPath({G}.nodes, {G}.edges, "sym::胃脘胀痛");'
        'process.stdout.write(JSON.stringify([...r.edgeIds].sort()));'))
    assert out == sorted([
        "sym::胃脘胀痛::el::肝郁", "el::肝郁::syn::ye", "syn::ye::formula::ye::柴胡疏肝散",
        "formula::ye::柴胡疏肝散::herb::ye::柴胡",
    ])


def test_the_faded_opacity_is_one_number_and_it_is_0_25():
    """§3.4 写死的是 0.25。R15 之前节点 0.15、边 0.06——两个数、都太狠：
    被淡掉的几乎看不见，"高亮一条路径"就变成了"只剩一条路径"，而学生要看的
    恰恰是这条路径**在整张图里的位置**。

    节点和边淡成不同的程度也没有任何理由，只会让边先消失、节点还在。"""
    graph_src = (__import__("pathlib").Path(__file__).resolve().parent.parent
                 / "web" / "graph.js").read_text(encoding="utf-8")
    assert "const FADED_OPACITY = 0.25;" in graph_src
    # 定义一次、节点一次、边一次。数的是代码行，不含注释里提到它的那几次
    # （注释里写 FADED_OPACITY 是好事，不该让这条断言反过来劝人别写注释）。
    code = [ln for ln in graph_src.splitlines() if not ln.strip().startswith("//")]
    assert "\n".join(code).count("FADED_OPACITY") == 3
    assert "opacity: 0.15" not in graph_src and "opacity: 0.06" not in graph_src


def test_the_highlight_transition_matches_the_css_token():
    """cytoscape 读不到 CSS 变量，所以那边只能是个数字。**但它必须跟
    `--t-highlight` 同值**——同一个视觉约定在两套系统里各写一遍，改一边
    另一边不会报错、只会看起来不一样。这条断言就是那根线。"""
    graph_src = (__import__("pathlib").Path(__file__).resolve().parent.parent
                 / "web" / "graph.js").read_text(encoding="utf-8")
    assert "const HIGHLIGHT_MS = 220;" in graph_src
    assert "transition-duration" in graph_src
    import re
    m = re.search(r"--t-highlight:\s*(\d+)ms", load_css())
    assert m and m.group(1) == "220", "CSS 令牌跟 cytoscape 那个数对不上"


def test_clicking_the_same_symptom_twice_clears_the_highlight():
    """再点一次取消。没有这条，学生点错一个症状之后只能重新问诊。"""
    body = APP[APP.index("function handleSymptomClick"):]
    body = body[:body.index("\nfunction ensureCanvas")]
    assert "highlightedSymptomId === nodeId" in body and "clearPathHighlight()" in body


def test_reasoning_is_open_by_default_only_for_students():
    """§3.4 第三条：学生要看的就是过程。researcher 默认折叠（信息密度优先），
    student 默认展开。"""
    out = json.loads(_run(
        'process.stdout.write(JSON.stringify(["researcher","student","doctor","patient"]'
        '.map(defaultDetailsOpenForMode)));'))
    assert out == [False, True, False, False]


def test_jun_herbs_stay_bold_in_their_own_class():
    """§3.4 第一条（已有，这里只钉住没退化）。君药加粗**不复用 .field b**
    ——那是行首标签的样式、颜色是灰的，而君药要的是正文本身加粗、
    颜色跟其余药材一致。两种加粗的意图不同。"""
    css = load_css()
    assert ".herb-jun" in css
    block = css[css.index(".herb-jun {"):]
    block = block[:block.index("}")]
    assert "font-weight" in block


def _graph_js() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent
            / "web" / "graph.js").read_text(encoding="utf-8")
