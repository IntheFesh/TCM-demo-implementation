"""R13：前端从单文件拆成四个文件之后的结构约束。

**替换的是 `tests/test_web_single_script.py`**（同一个文件 git mv 过来，测试数只增
不减）。那条钉的是"字面 `<script>` 标签只有一个"，因为当时 12 个前端测试用
`html.split("<script>")[-1]` 抽内联脚本，多一个标签就会**静默抽错**——测试照样全绿，
只是测的不是上线那份代码。

拆分之后那个抽法不存在了（`tests/web_harness.load_app_js()` 直接读文件），但它守的
那件事仍然要守，只是判据翻过来：**index.html 里不许再有任何内联脚本**。有的话，
node 测试读的是 app.js/graph.js，而浏览器跑的是它俩加上那段内联的——同一个静默分叉
换了个形状回来。
"""
import subprocess
from pathlib import Path

from tests.web_harness import SCRIPT_FILES, load_app_js, load_css, load_html

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def test_the_four_files_exist_and_are_not_empty():
    for name in ("index.html", "app.css", "app.js", "graph.js"):
        path = WEB / name
        assert path.exists(), f"缺 {name}"
        assert path.stat().st_size > 200, f"{name} 几乎是空的"


def test_index_html_has_no_inline_script_or_style():
    """**拆分之后这条是新的静默分叉防线**（理由见模块文档字符串）。
    样式同理：内联一段 `<style>` 的话，`tests/test_css_tokens.py` 查的是 app.css，
    查不到那一段。"""
    html = load_html()
    assert "<script>" not in html, "index.html 里又出现了内联脚本"
    assert "<style>" not in html, "index.html 里又出现了内联样式"
    for name in SCRIPT_FILES:
        assert f'<script src="{name}"></script>' in html, f"index.html 没有引 {name}"
    assert '<link rel="stylesheet" href="app.css">' in html


def test_index_html_is_structure_only():
    """结构文件要短到能一眼读完——R13 的判据是 ≤ 250 行。拆分的全部意义就是
    "改版面时不用在 3574 行里找 DOM"。"""
    lines = load_html().splitlines()
    assert len(lines) <= 250, f"index.html 有 {len(lines)} 行，结构文件不该这么长"


def test_the_scripts_load_in_the_same_order_the_tests_concatenate_them():
    """浏览器里的加载顺序必须跟 `load_app_js()` 的拼接顺序一致，否则 node 里跑得通
    的代码可能因为初始化顺序在浏览器里炸。**graph.js 在前、app.js 在后**：app.js
    末尾有一批加载时就执行的初始化，它必须排在所有函数定义之后。"""
    html = load_html()
    positions = [html.index(f'<script src="{name}"></script>') for name in SCRIPT_FILES]
    assert positions == sorted(positions), f"HTML 里的顺序跟 SCRIPT_FILES 不一致：{SCRIPT_FILES}"


def test_both_scripts_pass_node_syntax_check():
    """拆错了最常见的表现是某一半少了个花括号。`node --check` 一秒就能发现，
    比等某条用例在运行时炸出一个莫名其妙的错强。"""
    for name in SCRIPT_FILES:
        out = subprocess.run(["node", "--check", str(WEB / name)],
                             capture_output=True, text=True)
        assert out.returncode == 0, f"{name} 语法错误：{out.stderr}"


def test_the_graph_code_went_to_graph_js_and_the_rest_to_app_js():
    """拆分的判据是"cytoscape 那两张图的逻辑在 graph.js，其余在 app.js"。
    只断言文件都在是不够的——把所有代码都塞进 app.js、graph.js 留一行注释，
    上面那几条照样全绿。"""
    graph_js = (WEB / "graph.js").read_text(encoding="utf-8")
    app_js = (WEB / "app.js").read_text(encoding="utf-8")
    for marker in ("function computeLayout", "function buildStylesheet",
                   "async function growGraph", "function ensureCytoscape",
                   "async function gbExpandNode"):
        assert marker in graph_js, f"graph.js 里没有 {marker}"
        assert marker not in app_js, f"{marker} 同时出现在 app.js 里——拆重复了"
    for marker in ("function cardHtml", "function escapeHtml", "function renderDivergence"):
        assert marker in app_js, f"app.js 里没有 {marker}"
        assert marker not in graph_js, f"{marker} 同时出现在 graph.js 里——拆重复了"


def test_the_tcm_namespace_exposes_the_agreed_function_list():
    """两个文件靠 `window.TCM` 协作。**清单写死在测试里**：少一个名字就是某处
    协作点被悄悄改掉了，而那种改动在浏览器里表现为"点了没反应"，没有报错。"""
    script = load_app_js()
    expected = [
        # graph.js 侧
        "ensureCytoscape", "computeLayout", "buildStylesheet", "growGraph", "renderGraph",
        "replayGraph", "skipAnimation", "computeHighlightPath", "applyPathHighlight",
        "clearPathHighlight", "handleSymptomClick", "describeNodeTooltip",
        "describeEdgeTooltip", "showTooltip", "hideTooltip", "loadGraphBrowserData",
        "gbExpandNode", "gbSearch", "gbResetView", "gbToggleLayer",
        # app.js 侧
        "cardHtml", "escapeHtml", "renderDivergence", "divergenceBannerText",
        "groupHerbsByRole", "herbGroupsHtml", "buildEvidenceIndex", "openEvidence",
        "renderTriage", "demoModeText", "usageText", "switchTab",
    ]
    assert len(expected) >= 25
    missing = [name for name in expected if f"{name}," not in script and f"{name}:" not in script]
    assert not missing, f"window.TCM 的清单里缺这些：{missing}"
    for name in expected:
        assert f"function {name}" in script or f"async function {name}" in script, name


def test_css_carries_the_styles_that_used_to_be_inline():
    css = load_css()
    assert ":root" in css and len(css.splitlines()) > 300
