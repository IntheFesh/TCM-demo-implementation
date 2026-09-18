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

#: `computeLayout` / `evenY` 真正用到的、原来放错文件的那几个常量。
LAYOUT_CONSTANTS = ("LAYER_X", "Y_MIN", "Y_MAX", "HERB_GAP")


def test_the_layout_constants_live_with_the_function_that_uses_them():
    """**这条是这个文件存在的理由。** 它们必须在 graph.js 里，否则
    `importScripts("graph.js")` 之后 Worker 里就是 undefined。"""
    for name in LAYOUT_CONSTANTS:
        # `Y_MIN` / `Y_MAX` 是一行里一起声明的（`const Y_MIN = 60, Y_MAX = 620;`），
        # 所以判据是"顶层某个 const 声明里绑了这个名字"，不是"行首就是它"。
        assert re.search(rf"^const [^;]*\b{name}\s*=", GRAPH_JS, re.M), (
            f"{name} 不在 graph.js 的顶层——Worker 里会 undefined")


def test_app_js_no_longer_defines_them():
    """留一份在 app.js 就是两处实现，而 graph.js 那份才是被用的那份。"""
    for name in LAYOUT_CONSTANTS:
        assert not re.search(rf"^const [^;]*\b{name}\s*=", APP_JS, re.M), (
            f"app.js 里又定义了 {name}")


def test_app_js_does_not_use_them_either():
    """搬走的前提是对面真的不用。用的话搬完 app.js 就该炸了（而它没炸，
    说明确实只有 graph.js 在用）。"""
    for name in LAYOUT_CONSTANTS:
        assert name not in APP_JS, f"app.js 还在用 {name}"


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
    assert "window.__graphPerf = { layoutStats, layoutAsync, computeLayout };" in GRAPH_JS


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
