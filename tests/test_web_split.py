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
        # R41：三个都带 `defer`。判据从"逐字匹配整个标签"改成"src 在且带 defer"
        # ——前者会因为多一个属性就红，而那不是它要防的事（它防的是内联脚本
        # 和漏引某个文件）。defer 本身由下面那条专门的测试钉。
        assert f'<script src="{name}"' in html, f"index.html 没有引 {name}"
    assert '<link rel="stylesheet" href="app.css">' in html


def test_every_script_is_deferred_so_html_parsing_and_downloading_overlap():
    """R41：三个脚本都要带 `defer`。

    它们在 `</body>` 之前，本来就不阻塞首次绘制——`defer` 买到的是**边解析
    HTML 边并行下载**这三个文件（实测 157 + 89 + 10 KB），而不是解析到那一行
    才开始取。

    `defer` 而不是 `async`：`async` **不保证执行顺序**，而这三个有硬顺序
    （app.js 用 graph.js 定义的 `formulaSourceLabel`，顺序反了是 ReferenceError）。
    这条测试同时钉住"不许改成 async"。
    """
    html = load_html()
    for name in SCRIPT_FILES:
        i = html.index(f'<script src="{name}"')
        tag = html[i:html.index(">", i) + 1]
        assert " defer" in tag, f"{name} 没带 defer：{tag}"
        assert " async" not in tag, f"{name} 用了 async——顺序就不保证了：{tag}"


def test_the_head_no_longer_loads_cytoscape_synchronously():
    """R41：`<head>` 里那个同步的 CDN `<script>` 已经去掉了。

    实测它 `renderBlockingStatus: "blocking"`、240 ms，而那 373 KB 只有图谱页
    要用。加上三甲内网取不到 cdnjs，那条请求会一直挂到超时——而"断网可用"
    正是录制回放这条路线存在的理由。

    唯一的加载路径现在是 `graph.js` 的 `ensureCytoscape()`（用时插入本地副本）。
    """
    html = load_html()
    assert "cdnjs" not in html, "index.html 又从 CDN 取东西了"
    # 判据是 `<head>` 里**一个 `<script` 标签都没有**，不是"没出现 cytoscape
    # 这个词"——注释里提它是正常的（那正是解释为什么不在这里加载）。
    head = html.split("<body")[0]
    assert "<script" not in head, f"<head> 里又出现了 script 标签：{head[-300:]}"
    graph_js = (WEB / "graph.js").read_text(encoding="utf-8")
    # R42 把插 <script> 那几行抽成了 `_loadScript`（dagre 也要走同一条路），
    # 所以判据从"有这一行赋值"改成"这个文件名被交给了 _loadScript"。
    # **意图没变**：本地副本是唯一的加载路径，index.html 里不加载。
    assert '_loadScript("vendor/cytoscape.min.js"' in graph_js, (
        "本地副本那条加载路径没了——那 index.html 里也不加载的话图就永远画不出来")
    assert "_loadScript(DAGRE_SRC" in graph_js, "dagre 没走同一条本地加载路径"
    assert 'DAGRE_SRC = "vendor/dagre/dagre.min.js"' in graph_js


#: 结构文件的行数上限。R13 定 250；**R41 提到 254**，多出来的四行是
#: 两条 `<link rel="preload">`（首屏字体，见 test_the_first_screen_fonts_are_preloaded）
#: 加一行解释性注释再加一行余量——它们都是结构，不是逻辑。
#:
#: **R42 提到 272**，多出来的 18 行逐项是：
#:   +1  `#gb-breadcrumb`（图谱浏览器的聚焦路径）
#:   +2  它的解释性注释
#:   +1  余量
#:   （以下 14 行是同一轮问诊图那一侧的）
#:   +1  `#png-btn`（导出 PNG 按钮）
#:   +1  `#graph-layers`（九层的层名列头容器）
#:   +2  `#cy` 加 tabindex/role/aria-label（无障碍，属性太长只能折行）
#:   +2  `#graph-tooltip` 加 role/aria-live/aria-hidden（同上）
#:   +8  上述四处各自的解释性注释（为什么导出要 2 倍白底、为什么缺层也要出列头、
#:       为什么画布要能聚焦、为什么是 polite 而不是 assertive）
#: 都是结构与无障碍属性，**一行逻辑都没有**。
#:
#: 这个数的用途是防"HTML 又长回 3574 行、改版面得在里面找 DOM"，不是卡到个位数。
#: 每次提它都要在这里写明多出来的是什么，否则它会一轮一轮地被磨掉。
INDEX_HTML_MAX_LINES = 272


def test_index_html_is_structure_only():
    """结构文件要短到能一眼读完。拆分的全部意义就是
    "改版面时不用在 3574 行里找 DOM"。上限见 `INDEX_HTML_MAX_LINES`。"""
    lines = load_html().splitlines()
    assert len(lines) <= INDEX_HTML_MAX_LINES, (
        f"index.html 有 {len(lines)} 行，上限 {INDEX_HTML_MAX_LINES}"
        "——结构文件不该这么长")


def test_the_first_screen_fonts_are_preloaded():
    """R41：首屏那两个字重要 `preload`。

    不 preload 时它们要等 CSS 解析完、布局判定"这一段确实用到这个字重"之后
    才开始下载，而 CJK 子集是 246–320 KB/个——那段等待就是 FOUT（先系统字体、
    再换成 Noto）的长度。

    **另外两个字重刻意不 preload**：serif 600 / sans 500 在首屏之下，
    preload 它们会把带宽从真正要用的那两个身上抢走（preload 是"现在就下"，
    不是"提前知道"）。这条测试同时钉住这个边界——四个全 preload 跟一个都不
    preload 一样是错的。
    """
    html = load_html()
    preloaded = [ln for ln in html.splitlines() if 'rel="preload"' in ln]
    assert len(preloaded) == 2, f"preload 的字体不是两个：{preloaded}"
    for want in ("noto-serif-sc-400-subset.woff2", "noto-sans-sc-400-subset.woff2"):
        assert any(want in ln for ln in preloaded), f"没 preload {want}"
    for dont in ("noto-serif-sc-600", "noto-sans-sc-500"):
        assert not any(dont in ln for ln in preloaded), f"{dont} 不该 preload"
    for ln in preloaded:
        # 字体的 preload **必须带 crossorigin**，否则浏览器会再下一遍
        # （字体请求本身是匿名 CORS 模式，两个请求的缓存键不一样）。
        assert "crossorigin" in ln, f"字体 preload 少了 crossorigin：{ln}"
        assert 'as="font"' in ln, f"preload 少了 as=font：{ln}"


def test_the_scripts_load_in_the_same_order_the_tests_concatenate_them():
    """浏览器里的加载顺序必须跟 `load_app_js()` 的拼接顺序一致，否则 node 里跑得通
    的代码可能因为初始化顺序在浏览器里炸。**graph.js 在前、app.js 在后**：app.js
    末尾有一批加载时就执行的初始化，它必须排在所有函数定义之后。"""
    html = load_html()
    positions = [html.index(f'<script src="{name}"') for name in SCRIPT_FILES]
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
    """顶层定义的名字。**函数与常量对象都算**：R42 起 graph.js 还导出一个
    `NODE_ID`（节点 id 的构造表），app.js 通过 `NODE_ID.syndrome(...)` 用它
    ——只认 `function` 的话这类导出会被判成"清单里有、实际没人用"。"""
    fns = re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", text, re.M)
    # 顶层 `const X = {` / `const X = new ...`：只取**大写开头或全大写**的，
    # 那是这个项目里"导出用的常量表"的写法；小写的局部常量不算导出候选。
    consts = re.findall(r"^const ([A-Z][\w$]*)\s*=", text, re.M)
    return set(fns) | set(consts)


def _calls(text: str) -> set[str]:
    """被调用/被取属性的名字。`foo(` 与 `Foo.bar` 都算——后者是常量表的用法。"""
    called = re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", text)
    dotted = re.findall(r"\b([A-Z][\w$]*)\.[A-Za-z_$]", text)
    return set(called) | set(dotted)


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
