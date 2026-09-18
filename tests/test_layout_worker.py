"""R41：图布局算到 Worker 线程上去。**这一项暴露了一条看不见的反向依赖。**

`computeLayout` 是纯函数（只读 nodes/edges，不碰 DOM、不碰 cytoscape），
所以能整段搬到 Worker 上算。搬的时候第一次跑就报
`Worker 报错：LAYER_X is not defined`——`LAYER_X` / `Y_MIN` / `Y_MAX` /
`HERB_GAP` 这四个**只有 graph.js 在用**的布局常量，定义在 **app.js** 里。
浏览器里两个 script 共享全局作用域，所以从来没报错过；而 Worker 只
`importScripts("graph.js")`，那四个常量就不在了。

这是一条反方向的跨文件依赖（文件顶部写明依赖方向只能 app → graph），
**它是被 profiler 打出 `fallback_reason` 才看见的**——不打那个字段的话，
Worker 每次静默回落到主线程，表现就是"Worker 没有收益"。

搬它的理由不是"现在慢"：问诊图（十几个节点）的布局是亚毫秒级。理由是
**图会长大**——R42 要换成 dagre 布局，图谱浏览器那张持久图是几百到几千节点，
dagre 在那个规模上是几十到几百毫秒，那正是一个会让点击没反应的长任务。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tests.web_harness import DOM_STUB, js_tmp

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
GRAPH_JS = (WEB / "graph.js").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")

#: R42：布局改成 dagre 之后，Worker 里要跑起来需要的是这些——
#: 旧的那四个手写布局常量（`LAYER_X` / `Y_MIN` / `Y_MAX` / `HERB_GAP`）
#: **已经退役**（见 graph.js 里那段退役说明），所以判据从"它们在 graph.js 里"
#: 换成"dagre 这条路在 graph.js 里自洽"。
WORKER_NEEDS = ("measureLabel", "LAYER_FONT_SIZE", "dagreAvailable",
                "fallbackLayout", "computeLayout")

#: 已退役、不许回来的那几个名字。回来意味着有人又在手写间距。
RETIRED = ("LAYER_X", "Y_MIN", "Y_MAX", "HERB_GAP", "evenY")


def test_everything_the_layout_needs_lives_in_graph_js():
    """**这条是这个文件存在的理由。** Worker 只 `importScripts("graph.js")`
    （加 dagre），布局要用的东西必须都在这一份文件里，否则 Worker 里就是
    undefined——R41 踩的正是这个（四个常量在 app.js，Worker 报
    `LAYER_X is not defined`，而它是被 profiler 打出 `fallback_reason`
    才看见的：静默回落到主线程的表现就是"Worker 没有收益"）。"""
    for name in WORKER_NEEDS:
        assert re.search(rf"^(function|const) {name}\b", GRAPH_JS, re.M), (
            f"{name} 不在 graph.js 的顶层——Worker 里会 undefined")


def test_the_hand_written_spacing_constants_stay_retired():
    """R42 退役了它们（dagre 拿真实盒子尺寸，重叠结构上不可能）。
    **回来一个就说明有人又在手写间距**——那条路 R24/R37 已经走过一遍，
    结论是压字压不动中文标签。"""
    for name in RETIRED:
        assert not re.search(rf"^(function|const) {name}\b", GRAPH_JS, re.M), (
            f"{name} 又回来了——手写间距那条路 R24/R37 已经证明走不通")
        assert not re.search(rf"^(function|const) {name}\b", APP_JS, re.M)


def test_the_font_size_table_covers_every_layer():
    """dagre 按 `LAYER_FONT_SIZE` 估盒子尺寸，渲染按 `buildStylesheet` 的字号画。
    **两处不一致时 dagre 按一个尺寸留位、渲染按另一个尺寸画，重叠就回来了。**
    这条至少保证那张表九层齐全（具体数值的一致性由下面那条比）。"""
    m = re.search(r"const LAYER_FONT_SIZE = \{([^}]+)\}", GRAPH_JS, re.S)
    assert m
    keys = {int(k) for k in re.findall(r"(\d+):", m.group(1))}
    assert keys == set(range(9)), f"字号表缺层：{set(range(9)) - keys}"


def test_the_measure_errs_on_the_large_side():
    """**估小了 dagre 留的间距不够，重叠又回来。** CJK 必须按整字宽算
    （不是 0.5、也不是 0.8），并且有内边距与最小尺寸兜底。"""
    assert "NODE_PAD_X" in GRAPH_JS and "NODE_MIN_W" in GRAPH_JS
    body = GRAPH_JS.split("function measureLabel")[1][:900]
    assert "1 : 0.55" in body or "? 1 :" in body, "CJK 不是按整字宽算的"


def test_graph_js_can_be_imported_without_a_window():
    """Worker 里没有 `window`。graph.js 顶层只要有一处裸 `window.` 就会在
    `importScripts` 那一刻 ReferenceError，Worker 永远起不来。"""
    guard = 'if (typeof window !== "undefined") {'
    assert guard in GRAPH_JS, "window.TCM 那一段没有护栏"
    # 护栏必须**包住**所有顶层 window 访问：护栏之前不许有裸的 `window.`
    before = GRAPH_JS[:GRAPH_JS.index(guard)]
    for m in re.finditer(r"^window\.", before, re.M):
        raise AssertionError(f"护栏之前还有顶层 window 访问，位置 {m.start()}")


def test_importing_graph_js_in_a_node_worker_like_env_defines_computeLayout():
    """真的在**没有 window 的环境**里 import 一遍，然后调 computeLayout。
    这是对"Worker 能不能用"最接近的离线复现（node 里没有 Worker/importScripts，
    但"没有 window 也要能 import 并计算"这件事一模一样）。"""
    script = (
        "globalThis.document = undefined; globalThis.window = undefined;\n"
        + GRAPH_JS
        + "\nconst nodes = [{data:{id:'s1', layer:0}}, {data:{id:'e1', layer:1}}];\n"
          "const edges = [{data:{id:'x', source:'s1', target:'e1'}}];\n"
          "const p = computeLayout(nodes, edges);\n"
          "console.log(JSON.stringify([p['s1'].x, p['e1'].x]));\n")
    path = js_tmp(script)
    out = subprocess.run(["node", path], capture_output=True, text=True)
    assert out.returncode == 0, f"没有 window 的环境里跑不起来：{out.stderr[:500]}"
    xs = out.stdout.strip()
    assert xs.startswith("[") and "," in xs, xs


def test_the_worker_imports_graph_js_instead_of_reimplementing_the_layout():
    """**一个算法只能有一处实现。** Worker 的引导脚本里只有 importScripts +
    一个 onmessage 转发，不许出现第二份布局逻辑。"""
    i = GRAPH_JS.index("const boot = ")
    boot = GRAPH_JS[i:GRAPH_JS.index("_layoutWorker = new Worker", i)]
    assert "importScripts(" in boot
    assert "computeLayout(e.data.nodes, e.data.edges)" in boot
    assert "LAYER_X" not in boot, "引导脚本里又抄了一份布局常量"


def test_every_fallback_path_records_why():
    """三条兜底（建不起来 / 报错 / 超时）都要把原因记进 `layoutStats`。
    **静默回落的表现是"Worker 毫无收益"**，而人会以为是 Worker 没用——
    R41 就是这么被绕了一圈的。"""
    for reason in ("Worker 建不起来", "Worker 报错", "Worker 超时", "postMessage 失败"):
        assert reason in GRAPH_JS, f"少了兜底原因「{reason}」"


def test_the_stats_object_has_the_four_fields_the_profiler_reads():
    m = re.search(r"const layoutStats = \{([^}]+)\}", GRAPH_JS)
    assert m
    for field in ("where", "ms", "fallback_reason", "n_nodes"):
        assert field in m.group(1), f"layoutStats 少了 {field}"


def test_replies_are_claimed_by_id():
    """同一个 Worker 会被连续几次布局复用。不认 id 的话上一次**迟到的回复**
    会被当成这一次的结果，图会用错的坐标画出来——而它不报错。"""
    assert "ev.data.id !== id" in GRAPH_JS
    assert "const id = ++_layoutSeq" in GRAPH_JS


def test_there_is_a_timeout_and_it_is_not_absurdly_long():
    m = re.search(r"const LAYOUT_WORKER_TIMEOUT_MS = (\d+);", GRAPH_JS)
    assert m, "没有超时"
    ms = int(m.group(1))
    assert 500 <= ms <= 5000, f"超时 {ms} ms 不合理（卡住的 Worker 会让图一直不出来）"


def test_the_perf_hooks_are_not_smuggled_into_the_tcm_contract():
    """`window.TCM` 的契约是"恰好等于 app.js 真正调用到的那些"（有一条测试从
    源码算真实调用集合来比）。量具用的钩子挂在 `window.__graphPerf` 上——
    混进 TCM 就等于给那条"清单齐全"的测试留一个假绿点。"""
    i = GRAPH_JS.index("window.TCM = Object.assign")
    tcm = GRAPH_JS[i:GRAPH_JS.index("});", i)]
    assert "layoutStats" not in tcm and "layoutAsync" not in tcm
    # R42 又往 __graphPerf 上加了三把量具（零重叠/零穿越/层名列头快照），
    # 所以判据从"逐字符一行"改成"这几个名字都在 __graphPerf 那个块里、
    # 一个都不在 TCM 里"——这比比一整行强：加第四把量具时它仍然有效。
    j = GRAPH_JS.index("window.__graphPerf = {")
    perf = GRAPH_JS[j:GRAPH_JS.index("};", j)]
    for name in ("layoutStats", "layoutAsync", "computeLayout",
                 "overlapStats", "crossingStats", "bands"):
        assert name in perf, f"__graphPerf 里没有 {name}"
        assert name not in tcm, f"{name} 混进了 window.TCM"


def test_grow_graph_awaits_the_async_layout():
    """接线判据：`layoutAsync` 写了但 `growGraph` 还在直接调 `computeLayout`
    的话，Worker 那一路永远不跑，而上面所有测试照样绿。"""
    assert "await layoutAsync(graph.nodes, graph.edges)" in GRAPH_JS
    body = GRAPH_JS.split("async function growGraph")[1][:3000]
    assert "computeLayout(graph.nodes" not in body, "growGraph 还在同步算布局"


def test_the_layout_is_still_deterministic():
    """并行/异步化最容易出的 bug 是结果变了。同一份输入跑两遍必须逐字节相同。"""
    script = (DOM_STUB + GRAPH_JS
              + "\nconst nodes = [{data:{id:'a',layer:0}},{data:{id:'b',layer:0}},"
                "{data:{id:'c',layer:1}}];\n"
                "const edges = [{data:{id:'e1',source:'a',target:'c'}}];\n"
                "const p1 = JSON.stringify(computeLayout(nodes, edges));\n"
                "const p2 = JSON.stringify(computeLayout(nodes, edges));\n"
                "console.log(p1 === p2 ? 'same' : p1 + ' vs ' + p2);\n")
    path = js_tmp(script)
    out = subprocess.run(["node", path], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "same"
