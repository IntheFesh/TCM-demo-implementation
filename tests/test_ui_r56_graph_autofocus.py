"""R56 §6：图谱页改"本例知识地图"——切到图谱页时，如果有一次活跃问诊结论，
直接聚焦到这次的证型节点（`gbFocusOnNodeId`），不再回退到脾胃门首屏、
让医师自己点开证素、找到证型再双击。

## 这个文件测什么

- `gbFocusOnNodeId`（graph.js）：入口函数本身不是新判据——真正的加载/合并/
  聚焦各自只有一处实现（`gbFetchInto`/`gbMergeGraph`/`gbFocus`），这里只测
  "从一个 syn:: id 直接到位"这条编排路径：节点已经在索引里就直接聚焦；
  不在索引里且拉不到（数据覆盖缺口）就返回 null，不抛；已经在聚焦中的话
  先退出旧的聚焦再进新的（不然聚焦栈会把两次聚焦叠在一起）。

- `switchTab`（app.js）：只在证型**变了**的时候才自动聚焦（`gbAutoFocusedSyndrome`
  记的是上一次自动聚焦过的证型名）——医师在图谱页手动探索到别处、切回问诊页
  看一眼结果、再切回来，不该被强制拉回原地。找不到节点时不强行打开一个空
  释义面板。没有活跃问诊结论时走原来的路径（首屏 `loadGraphBrowserData`）。

真实渲染（聚焦之后节点有没有重叠、侧栏真的滑出来）不是这个文件的事，
那是 Playwright 的活。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js

APP = load_app_js()

#: 跟 test_graph_browser_focus.py 同一张假图：证素「脾」连两个证型。
FAKE_GRAPH = {
    "nodes": [
        {"data": {"id": "element::脾", "label": "脾", "node_type": "element"}},
        {"data": {"id": "syn::脾气虚证", "label": "脾气虚证", "node_type": "syndrome"}},
        {"data": {"id": "syn::脾阳虚证", "label": "脾阳虚证", "node_type": "syndrome"}},
        {"data": {"id": "symptom::纳差", "label": "纳差", "node_type": "symptom"}},
    ],
    "edges": [
        {"data": {"id": "e1", "source": "element::脾", "target": "syn::脾气虚证"}},
        {"data": {"id": "e2", "source": "element::脾", "target": "syn::脾阳虚证"}},
        {"data": {"id": "e3", "source": "symptom::纳差", "target": "syn::脾气虚证"}},
    ],
}


def _run(js_tail: str) -> str:
    proc = subprocess.run(["node", js_tmp(DOM_STUB + APP + "\n" + js_tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json_async(expr: str):
    return json.loads(_run(
        f"Promise.resolve({expr}).then(v => process.stdout.write(JSON.stringify(v)));"
    ))


#: `gbFocus` 真的聚焦成功时会调 `gbFitIfNeeded`，它读 `gbCy.elements().boundingBox()`
#: 这类返回值再做加法；`gbExitFocus` 在还没进过总览态时会退到 `gbResetView`，
#: 那条路径会重新给全部节点算布局坐标（`width / 2` 这类算术）。两处的共同点
#: 是：DOM_STUB 的 Proxy 什么调用都接得住，但接不住"读出来的值再参与运算"
#: （`bb.w + padding * 2`、`width / 2` 都要把 Proxy 转成原始数值，直接抛
#: TypeError）。这两条都不是这个文件要测的东西（真实的"聚焦/重铺之后有没有
#: 摆好"是 Playwright 的活），所以桩掉，跟 test_done_fallback.py 桩
#: appendProgress 是同一条理由。
_STUB_FIT = "gbFitIfNeeded = () => {}; gbResetView = () => {};\n"


# ---------- 一、gbFocusOnNodeId：入口编排 ----------

def test_focusing_a_node_already_in_the_index_does_not_need_the_network():
    """节点已经在 `gbIndex` 里（比如上一次浏览已经拉过），不该多打一次请求
    ——这也是"聚焦只走 gbFetchInto/gbMergeGraph/gbFocus 这一套"的另一面：
    已经有的数据不用重新拉。"""
    got = _json_async(f"""(async () => {{
      {_STUB_FIT}
      let fetchCalled = false;
      globalThis.fetch = () => {{ fetchCalled = true; return Promise.reject(new Error("不该打这个")); }};
      gbGraphData = {{ graph: {json.dumps(FAKE_GRAPH, ensure_ascii=False)} }};
      gbBuildIndex();
      const r = await gbFocusOnNodeId("syn::脾气虚证");
      return {{ fetchCalled, found: r !== null, n_nodes: r ? r.n_nodes : null }};
    }})()""")
    assert got["fetchCalled"] is False
    assert got["found"] is True
    assert got["n_nodes"] and got["n_nodes"] > 0


def test_a_node_missing_from_the_index_is_fetched_via_the_same_neighbors_endpoint():
    """节点不在已加载的那部分里——走 `/api/graph/neighbors`（跟手动点开一个
    节点展开邻域同一条路径），不是另起一个"直接拿这一个节点"的端点。"""
    got = _json_async(f"""(async () => {{
      {_STUB_FIT}
      let calledUrl = null;
      globalThis.fetch = (url) => {{
        calledUrl = url;
        return Promise.resolve({{ ok: true, json: () => Promise.resolve({{
          graph: {{
            nodes: [{{data: {{id: "syn::新证", label: "新证", node_type: "syndrome"}}}}],
            edges: [],
          }},
          page: null,
        }}) }});
      }};
      gbGraphData = {{ graph: {json.dumps(FAKE_GRAPH, ensure_ascii=False)} }};
      gbBuildIndex();
      const r = await gbFocusOnNodeId("syn::新证");
      return {{ calledUrl, found: r !== null }};
    }})()""")
    assert got["calledUrl"] is not None
    assert "/api/graph/neighbors" in got["calledUrl"]
    assert "node=syn%3A%3A%E6%96%B0%E8%AF%81" in got["calledUrl"]
    assert got["found"] is True


def test_a_node_the_server_does_not_have_either_returns_null_not_a_throw():
    """证候表覆盖不到这个证型——数据覆盖缺口，不是程序错误。调用方
    （switchTab）据此决定要不要打开释义面板；这里只保证不抛。"""
    got = _json_async(f"""(async () => {{
      globalThis.fetch = () => Promise.resolve({{ ok: true, json: () => Promise.resolve({{
        graph: {{ nodes: [], edges: [] }}, page: null,
      }}) }});
      gbGraphData = {{ graph: {json.dumps(FAKE_GRAPH, ensure_ascii=False)} }};
      gbBuildIndex();
      const r = await gbFocusOnNodeId("syn::压根没有这个证");
      return {{ r }};
    }})()""")
    assert got["r"] is None


def test_when_the_graph_has_not_loaded_yet_it_loads_first():
    """图谱页从没打开过（`gbGraphData` 还是空的）就直接切过来——
    `gbFocusOnNodeId` 自己兜底先加载，不是要求调用方先手动展开一遍。"""
    got = _json_async(f"""(async () => {{
      {_STUB_FIT}
      let loadCalls = 0;
      loadGraphBrowserData = () => {{
        loadCalls += 1;
        gbGraphData = {{ graph: {json.dumps(FAKE_GRAPH, ensure_ascii=False)} }};
        gbBuildIndex();
        return Promise.resolve();
      }};
      gbGraphData = null;
      const r = await gbFocusOnNodeId("syn::脾气虚证");
      return {{ loadCalls, found: r !== null }};
    }})()""")
    assert got["loadCalls"] == 1
    assert got["found"] is True


def test_focusing_while_already_in_focus_exits_the_old_focus_first():
    """连续聚焦两个不同的证型——不能把新的聚焦压在旧的聚焦栈上面，
    不然退出聚焦时恢复的是"聚焦 A 之前"那批节点，位置就乱了。"""
    got = _json_async(f"""(async () => {{
      {_STUB_FIT}
      globalThis.fetch = () => Promise.reject(new Error("不该打网络"));
      gbGraphData = {{ graph: {json.dumps(FAKE_GRAPH, ensure_ascii=False)} }};
      gbBuildIndex();
      await gbFocusOnNodeId("syn::脾气虚证");
      const stackAfterFirst = gbFocusStack.length;
      await gbFocusOnNodeId("syn::脾阳虚证");
      return {{ stackAfterFirst, stackAfterSecond: gbFocusStack.length,
                root: gbFocusStack[gbFocusStack.length - 1] }};
    }})()""")
    assert got["stackAfterFirst"] == 1
    assert got["stackAfterSecond"] == 1, "第二次聚焦没有先退出第一次，栈叠起来了"
    assert got["root"] == "syn::脾阳虚证"


# ---------- 二、switchTab：只在证型变了的时候才自动聚焦 ----------

def _switch_with(last_result: dict | None, extra_setup: str = "") -> dict:
    js = f"""(async () => {{
      const calls = {{ focus: [], explain: [] }};
      gbFocusOnNodeId = (id) => {{ calls.focus.push(id); return Promise.resolve({{n_nodes: 1}}); }};
      openNodeExplain = (id, name) => {{ calls.explain.push([id, name]); return Promise.resolve(); }};
      let loadCalls = 0;
      loadGraphBrowserData = () => {{ loadCalls += 1; return Promise.resolve(); }};
      LAST_RESULT = {json.dumps(last_result, ensure_ascii=False)};
      gbGraphData = null;
      gbAutoFocusedSyndrome = null;
      {extra_setup}
      switchTab("graph-browser");
      // switchTab 里那段 .then(...) 是微任务，让它有机会跑完再读 calls。
      await new Promise((r) => setTimeout(r, 0));
      return {{ calls, loadCalls, gbAutoFocusedSyndrome }};
    }})()"""
    return _json_async(js)


_STRUCTURED_RESULT = {
    "results": [{"s3_structured": {"syndrome": {"name": "脾气虚证"}}}],
}
_LEGACY_RESULT = {"results": [{"s3": {"syndrome": "脾气虚证"}}]}


def test_switching_to_the_graph_tab_focuses_the_current_consults_syndrome():
    got = _switch_with(_STRUCTURED_RESULT)
    assert got["calls"]["focus"] == ["syn::脾气虚证"]
    assert got["calls"]["explain"] == [["syn::脾气虚证", "脾气虚证"]]
    assert got["gbAutoFocusedSyndrome"] == "脾气虚证"


def test_it_reads_the_legacy_s3_syndrome_field_too():
    """`s3_structured` 是新结构；legacy 模式下证型名在 `s3.syndrome`——
    `currentSyndromeName` 两条路径都要认，不是只认新结构。"""
    got = _switch_with(_LEGACY_RESULT)
    assert got["calls"]["focus"] == ["syn::脾气虚证"]


def test_it_does_not_reopen_the_explain_panel_when_the_node_is_not_found():
    """证候表覆盖不到这个证型——不强行打开一个空释义面板，那会让医师以为
    点错了，实际是数据覆盖缺口。"""
    js = f"""(async () => {{
      const calls = {{ focus: [], explain: [] }};
      gbFocusOnNodeId = (id) => {{ calls.focus.push(id); return Promise.resolve(null); }};
      openNodeExplain = (id, name) => {{ calls.explain.push([id, name]); return Promise.resolve(); }};
      LAST_RESULT = {json.dumps(_STRUCTURED_RESULT, ensure_ascii=False)};
      gbGraphData = null;
      gbAutoFocusedSyndrome = null;
      switchTab("graph-browser");
      await new Promise((r) => setTimeout(r, 0));
      return {{ calls }};
    }})()"""
    got = _json_async(js)
    assert got["calls"]["focus"] == ["syn::脾气虚证"]
    assert got["calls"]["explain"] == [], "没找到节点却还是打开了释义面板"


def test_switching_back_and_forth_does_not_refocus_the_same_syndrome_twice():
    """医师在图谱页手动探索到别处、切回问诊页看一眼结果、再切回来——
    不该被强制拉回原地，那是打断医师自己的浏览，不是"回到本例地图"。
    同一个证型，第二次 `switchTab("graph-browser")` 不应该再调用一次
    `gbFocusOnNodeId`。"""
    js = f"""(async () => {{
      const calls = {{ focus: [] }};
      gbFocusOnNodeId = (id) => {{ calls.focus.push(id); return Promise.resolve({{n_nodes: 1}}); }};
      openNodeExplain = () => Promise.resolve();
      let loadCalls = 0;
      loadGraphBrowserData = () => {{ loadCalls += 1; return Promise.resolve(); }};
      LAST_RESULT = {json.dumps(_STRUCTURED_RESULT, ensure_ascii=False)};
      gbGraphData = null;
      gbAutoFocusedSyndrome = null;
      switchTab("graph-browser");
      await new Promise((r) => setTimeout(r, 0));
      switchTab("consult");
      switchTab("graph-browser");
      await new Promise((r) => setTimeout(r, 0));
      return {{ calls, loadCalls }};
    }})()"""
    got = _json_async(js)
    assert got["calls"]["focus"] == ["syn::脾气虚证"], "同一个证型被自动聚焦了不止一次"


def test_a_new_syndrome_after_a_new_consult_is_focused_again():
    """跟上一条相反：证型**变了**（做了下一次问诊），这次要重新聚焦，
    不能因为"已经自动聚焦过一次"就锁死。"""
    js = f"""(async () => {{
      const calls = {{ focus: [] }};
      gbFocusOnNodeId = (id) => {{ calls.focus.push(id); return Promise.resolve({{n_nodes: 1}}); }};
      openNodeExplain = () => Promise.resolve();
      loadGraphBrowserData = () => Promise.resolve();
      gbGraphData = null;
      gbAutoFocusedSyndrome = null;
      LAST_RESULT = {json.dumps(_STRUCTURED_RESULT, ensure_ascii=False)};
      switchTab("graph-browser");
      await new Promise((r) => setTimeout(r, 0));
      switchTab("consult");
      LAST_RESULT = {{ results: [{{ s3_structured: {{ syndrome: {{ name: "脾阳虚证" }} }} }}] }};
      switchTab("graph-browser");
      await new Promise((r) => setTimeout(r, 0));
      return {{ calls }};
    }})()"""
    got = _json_async(js)
    assert got["calls"]["focus"] == ["syn::脾气虚证", "syn::脾阳虚证"]


def test_with_no_active_consult_it_falls_back_to_loading_the_overview():
    """还没问诊过（`LAST_RESULT` 是空的）——走原来的路径：首屏
    `loadGraphBrowserData`，不聚焦任何东西（没有"这一次"可聚焦）。"""
    got = _switch_with(None)
    assert got["calls"]["focus"] == []
    assert got["loadCalls"] == 1


def test_switching_to_the_consult_tab_never_touches_graph_focus():
    got = _json_async(f"""(async () => {{
      const calls = {{ focus: [] }};
      gbFocusOnNodeId = (id) => {{ calls.focus.push(id); return Promise.resolve(null); }};
      LAST_RESULT = {json.dumps(_STRUCTURED_RESULT, ensure_ascii=False)};
      switchTab("consult");
      return {{ calls }};
    }})()""")
    assert got["calls"]["focus"] == []
