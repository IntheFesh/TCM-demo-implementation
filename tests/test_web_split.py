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
import json
import re
import subprocess
from pathlib import Path

from tests.web_harness import (
    DOM_STUB, SCRIPT_FILES, js_tmp, load_app_js, load_css, load_html,
)

WEB_FILES: dict[str, str] = {}


def _read(name: str) -> str:
    if name not in WEB_FILES:
        WEB_FILES[name] = (ROOT / "web" / name).read_text(encoding="utf-8")
    return WEB_FILES[name]

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
    # escapeHtml / sleep 不在这张表里：第 0 项断循环依赖时它们**下沉到了 graph.js**
    # （纯工具没有 UI 归属，放在底层两边都能取，依赖方向才是单向的）。
    # cardHtml 在 R14 改名成 columnHtml（三列集注，不再是卡片）；
    # 这条测的是"问诊页的渲染在 app.js、图谱的渲染在 graph.js"，跟名字无关。
    for marker in ("function columnHtml", "function submitConsult", "function renderDivergence"):
        assert marker in app_js, f"app.js 里没有 {marker}"
        assert marker not in graph_js, f"{marker} 同时出现在 graph.js 里——拆重复了"


def _defs(text: str) -> set[str]:
    return set(re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text, re.M))


def _calls(text: str) -> set[str]:
    return set(re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", text))


def _tcm_list(text: str) -> set[str]:
    """从一份源码里抓 `window.TCM = Object.assign(...)` 挂上去的名字。"""
    m = re.search(r"window\.TCM = Object\.assign\(window\.TCM \|\| \{\}, \{(.*?)\n\}\)",
                  text, re.S)
    if not m:
        return set()
    body = re.sub(r"//[^\n]*", "", m.group(1))          # 去注释
    return {n for n in re.findall(r"[A-Za-z_$][\w$]*", body)}


def test_the_dependency_between_the_two_files_goes_only_one_way():
    """**graph.js 不许调用 app.js 的任何函数。**

    R13 拆分时两边留下了双向的裸全局互调（graph.js 直接调 app.js 的 showError /
    openEvidence / closeEvidence / escapeHtml / sleep），形成循环依赖——谁也不能单独
    被理解或替换。R14 前把它断开：纯工具（escapeHtml/sleep）下沉到 graph.js，
    宿主 UI（错误条、证据侧栏）改成 `setGraphHooks` 注入。
    """
    app, graph = _read("app.js"), _read("graph.js")
    back_edges = sorted(_calls(graph) & _defs(app))
    assert not back_edges, f"graph.js 反过来调了 app.js 的函数：{back_edges}"


def test_the_tcm_list_equals_the_real_cross_file_call_set():
    """**清单必须恰好等于真实跨文件调用集合，多一个少一个都红。**

    R13 那版是手写的 33 个名字，而实测两边**对面一个都没用到**（app→graph 真实只有
    5 个、graph→app 5 个，两份清单里 27 个名字纯属装饰）。于是那条"清单齐全"永远绿
    ——删掉 `showError` 也绿，而浏览器里表现为"点了没反应、不报错"，正是 graph.js
    自己的注释担心的那种事故。

    所以判据改成**从源码算**：真实调用集合由 `_calls ∩ _defs` 得到，跟两份
    `window.TCM` 清单的并集比较。手写清单和代码任何一边漂了，这条都会红。
    """
    app, graph = _read("app.js"), _read("graph.js")
    real = (_calls(app) & _defs(graph)) | (_calls(graph) & _defs(app))
    listed = _tcm_list(app) | _tcm_list(graph)
    assert real, "一个跨文件调用都没算出来——多半是正则没跟上代码风格的变化"
    assert listed == real, (
        f"window.TCM 清单跟真实跨文件调用对不上。\n"
        f"  清单里有、实际没人用：{sorted(listed - real)}\n"
        f"  实际用了、清单里没有：{sorted(real - listed)}")


def test_app_js_exports_nothing_and_registers_hooks_instead():
    """依赖单向的落法：app.js 不往 window.TCM 上挂东西，改成把自己的 UI 注册进去。"""
    app, graph = _read("app.js"), _read("graph.js")
    assert _tcm_list(app) == set(), f"app.js 还在往 window.TCM 上挂：{sorted(_tcm_list(app))}"
    assert "setGraphHooks({" in app and "onError: showError" in app
    assert "function setGraphHooks" in graph
    # 默认实现不能是静默空函数——出了错还是要有痕迹，静默才是最坏的情况
    assert "console.error" in graph


def test_the_hooks_are_really_wired_at_load_time_not_just_declared():
    """静态断言只能证明 app.js 里写着 `setGraphHooks({...})`，证明不了它跑过。
    会出事的形态是「注册语句被挪进某个没被调用的函数里」——源码上一模一样，
    运行时 graph.js 报的错全落进 console.error 那个兜底，页面上什么都不显示。

    所以把两份脚本按 index.html 的顺序真喂给 node，比对**函数身份**：注册跑过的话
    `graphHooks.onError` 就是 app.js 那个 `showError` 本身。
    刻意不去断言 `#error-box` 的文字——`DOM_STUB` 是个什么都接住的 Proxy，
    故意不做成像样的 DOM（理由见 web_harness 的注释）；真实渲染归 Playwright 管。
    """
    tail = """
console.log(JSON.stringify({
  error: graphHooks.onError === showError,
  open: graphHooks.onOpenEvidence === openEvidence,
  close: graphHooks.onCloseEvidence === closeEvidence,
}));
"""
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    out = json.loads(proc.stdout)
    assert out == {"error": True, "open": True, "close": True}, f"钩子没在加载时注册：{out}"


def test_every_element_the_scripts_look_up_exists_in_the_html():
    """**R14 靠 Playwright 才抓到这条，所以补一条秒级的。**

    改版时把 `#results` 改名成 `#columns`，`getElementById("results")` 漏改了
    两处——而那两处是加载时就执行的 `addEventListener`，返回 null 直接抛，
    **整份 app.js 停在那一行**，页面上什么都不会发生。

    node 测试一个字都测不出来：`DOM_STUB` 是个什么都接住的 Proxy，
    `getElementById` 永远返回一个能挂监听器的对象。所以判据只能是静态的
    ——脚本里查的每一个 id，HTML 里都得有。
    """
    ids = set(re.findall(r'id="([^"]+)"', load_html()))
    for name in SCRIPT_FILES:
        src = _read(name)
        # 脚本自己拼出来的节点（拦截页那颗"换一条主诉"、追问记录框）当然不在
        # HTML 里。它们是同一份源码里生成、同一份源码里查的，不会分叉——
        # 这条测试防的是"HTML 改了名字、JS 没跟着改"。
        ids |= set(re.findall(r'id="([^"]+)"', src))
        ids |= set(re.findall(r'\.id = "([^"]+)"', src))
        used = set(re.findall(r'getElementById\("([^"]+)"\)', src))
        missing = sorted(used - ids)
        assert not missing, f"{name} 查了 index.html 里不存在的 id：{missing}"


def test_css_carries_the_styles_that_used_to_be_inline():
    css = load_css()
    assert ":root" in css and len(css.splitlines()) > 300
