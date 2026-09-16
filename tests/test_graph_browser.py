"""web/index.html 里图谱浏览器页签（模块7）前端代码的离线测试。

跑真实上线的那份 <script>（跟别的前端测试同一个模式），只测不需要真实
cytoscape 就能验证的部分：gbBuildIndex 的索引构建。像 gbAddNodes/gbExpandNode/
gbSearch/gbToggleLayer/gbApplyPhysicianWeighting 这些函数内部都会调
真实 cytoscape 实例的方法（cy.add/cy.getElementById/cy.nodes().filter(...)/
ele.style(...)），DOM 代理桩测不出来——桩对象对任何属性访问、任何调用都返回
自己，`cy.nodes().filter(...)` 这类链式调用不会抛异常也不会报出任何有意义的
错误，测出来的只是"没崩"，测不出"filter 出来的到底是不是我要的那几个节点"。
这部分交给真实 Playwright + 真实 cytoscape 验证（见模块7报告），不在这里
用桩硬凑一份看起来测了、实际什么都没测到的测试。
"""
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


def _sample_graph_data() -> dict:
    return {
        "graph": {
            "nodes": [
                {"data": {"id": "a", "label": "A", "node_type": "symptom"}},
                {"data": {"id": "b", "label": "B", "node_type": "element"}},
                {"data": {"id": "c", "label": "C", "node_type": "syndrome"}},
            ],
            "edges": [
                {"data": {"id": "a::b::indicates", "source": "a", "target": "b", "edge_type": "indicates"}},
                {"data": {"id": "b::c::composes", "source": "b", "target": "c", "edge_type": "composes"}},
            ],
        },
    }


def test_gb_build_index_maps_all_nodes():
    js = f"""
    gbGraphData = {json.dumps(_sample_graph_data(), ensure_ascii=False)};
    gbBuildIndex();
    process.stdout.write(JSON.stringify([...gbIndex.nodeById.keys()].sort()));
    """
    assert json.loads(_run_node(js)) == ["a", "b", "c"]


def test_gb_build_index_edges_are_bidirectional():
    """节点 b 既是 a->b 这条边的终点、又是 b->c 这条边的起点——两条边都要能
    从 b 这一侧查到，展开 b 节点时才能同时露出 a 和 c 这两个方向的邻居。"""
    js = f"""
    gbGraphData = {json.dumps(_sample_graph_data(), ensure_ascii=False)};
    gbBuildIndex();
    const out = {{
      edgesOfA: gbIndex.edgesByNode.get("a").map((e) => e.data.id),
      edgesOfB: gbIndex.edgesByNode.get("b").map((e) => e.data.id).sort(),
      edgesOfC: gbIndex.edgesByNode.get("c").map((e) => e.data.id),
    }};
    process.stdout.write(JSON.stringify(out));
    """
    out = json.loads(_run_node(js))
    assert out["edgesOfA"] == ["a::b::indicates"]
    assert out["edgesOfB"] == ["a::b::indicates", "b::c::composes"]
    assert out["edgesOfC"] == ["b::c::composes"]


def test_gb_build_index_node_with_no_edges_has_no_entry():
    """孤立节点（假设图里存在）在 edgesByNode 里不该有条目——gbExpandNode 对
    这种节点会用 `|| []` 兜底，这里钉住"没有条目"这个前提本身是真的，不是
    凭空假设。"""
    graph_data = {
        "graph": {
            "nodes": [{"data": {"id": "lonely", "label": "孤立", "node_type": "symptom"}}],
            "edges": [],
        },
    }
    js = f"""
    gbGraphData = {json.dumps(graph_data, ensure_ascii=False)};
    gbBuildIndex();
    process.stdout.write(JSON.stringify(gbIndex.edgesByNode.has("lonely")));
    """
    assert json.loads(_run_node(js)) is False


# ============================================================================
# R16：图谱浏览器按新规格重做（§3.2 规格 5–11）
# ============================================================================
#
# 这一批仍然守着文件开头那条纪律：**只测不需要真实 cytoscape 就能验的部分**。
# "首屏真的只有证素""枢纽真的在内圈""再点真的收起"这类要看渲染结果的，
# 交给 `scripts/screenshot_states.py --only browser_home / browser_expanded`
# （真浏览器、真 cytoscape，每种带 DOM 判据）。这里测的是数据流与常量表。


def _graph_js() -> str:
    return (ROOT / "web" / "graph.js").read_text(encoding="utf-8")


def test_the_home_screen_asks_for_elements_not_syndromes():
    """§3.2 规格 5：首屏铺**证素**。

    之前是 80 个证型方块，互不相连、全同色、cose 摊成几排——总纲 §1 的 F6
    点名的就是这个。证型之间本来就没有边，力导向对一堆孤立节点只能摊平，
    那张图不传达任何东西。证素只有 20 个，而且每个证型都挂在证素下面。"""
    src = _graph_js()
    body = src[src.index("async function loadGraphBrowserData"):]
    body = body[:body.index("let growToken")]
    assert "node_types=element" in body
    assert "node_types=syndrome" not in body


def test_the_element_limit_is_not_a_hard_coded_twenty():
    """证素数量是数据决定的（实测 20）。写死 20 的话，教材扩充后多出来的
    那几个会**静默不显示**——而"少了几个枢纽"这件事在图上看不出来。"""
    src = _graph_js()
    assert "const GB_ELEMENT_LIMIT" in src
    assert "node_types=element&limit=${GB_ELEMENT_LIMIT}" in src


def test_expansion_is_layered_element_to_syndrome_to_symptom():
    """§3.2 规格 6：点证素 → 证型，点证型 → 症状。

    **不筛类型的后果是实测过的**：证素「肝」有 499 个邻居，其中 61 个证型、
    其余基本都是症状。不筛的话点一下就是 150 个症状铺满画布——跟 R16 要治的
    F6 是同一个病。"""
    out = json.loads(_run_node("process.stdout.write(JSON.stringify(GB_EXPAND_TARGET));"))
    assert out == {"element": "syndrome", "syndrome": "symptom", "symptom": "element"}


def test_the_type_filter_is_applied_by_the_server_not_the_client():
    """筛在前端意味着先把 499 个全拉回来再扔掉 480 个，而且 `limit` 会先在
    服务端把想要的那些截掉——**截断发生在筛之前**，结果是"限 150 个邻居里
    恰好有几个证型就显示几个"，而那个数完全取决于 networkx 的遍历顺序。
    这种错不报错，只让人以为脾没几个证型。"""
    src = _graph_js()
    assert "node_types=${encodeURIComponent(want)}" in src
    import inspect

    import api.main as api_main
    sig = inspect.signature(api_main.api_graph_neighbors)
    assert "node_types" in sig.parameters


def test_the_neighbors_endpoint_filters_by_type():
    """端点层面的实测：不筛 499，只要证型 61。"""
    from fastapi.testclient import TestClient

    import api.main as api_main
    client = TestClient(api_main.app)
    all_ = client.get("/api/graph/neighbors", params={"node": "element::肝"}).json()
    syn = client.get("/api/graph/neighbors",
                     params={"node": "element::肝", "node_types": "syndrome"}).json()
    assert syn["page"]["total"] < all_["page"]["total"]
    assert all(n["data"]["node_type"] == "syndrome" for n in syn["graph"]["nodes"])


def test_clicking_an_expanded_node_collapses_it():
    """R13 那版刻意没做收起（"加一套折叠状态管理跟本身价值不成比例"）。
    规格改了之后它是必需的：首屏 20 个证素，每个展开出十几个证型，点开三四个
    就又变成一屏摊平的方块。"""
    src = _graph_js()
    body = src[src.index("async function gbExpandNode"):]
    body = body[:body.index("function gbRelayout")]
    assert "gbExpanded.has(nodeId)" in body and "gbCollapseNode(nodeId)" in body


def test_collapse_only_removes_what_this_node_brought_in():
    """一个证型可能同时挂在两个证素下面。按"邻居"删会把另一个证素展开出来的
    东西也删掉——图上表现为"我明明没动那一支，它却少了一半"。"""
    src = _graph_js()
    body = src[src.index("function gbCollapseNode"):]
    body = body[:body.index("// R16 §3.2 规格 5：**concentric**")]
    assert "gbExpanded.get(nodeId)" in body
    assert "some((ids) => ids.includes(id))" in body


def test_the_layout_is_concentric_with_hubs_inside():
    """§3.2 规格 5：环形布局，证素在内圈。

    为什么不是 cose：cose 是力导向，摆出来的位置取决于连边的拉扯，
    "谁是枢纽"在图上看不出来——而这张图的整个心智模型就是"从证素往外长"。

    **有意的契约变更（R24 补丁）**：布局引擎从 cytoscape 的 `concentric` 换成
    `preset` + 自己算位置。规格没变（内圈枢纽、外圈展开），换的是实现——
    concentric 不接受半径下限、扇形范围、椭圆，而这三样正是 r24_rings.png 上
    "20 个枢纽挤成中心一个点、61 个证型摊成整圆"的直接原因。
    位置对不对现在由**纯函数判据**管（tests/test_ui_r24_patch.py 那一组），
    这里只钉住"不是力导向、也不是随机摆"这件事。
    """
    src = _graph_js()
    body = src[src.index("function gbRelayout"):]
    body = body[:body.index("function gbRingLegendText")]
    assert 'name: "preset"' in body
    assert "gbLayoutPositions(" in body, "位置要由那个纯函数算，不是就地拍"
    assert "gbHubIds.has(id)" in body, "内外圈仍然按是不是枢纽来分"
    # 节点太多时仍然退回 grid：力导向/环形对上千节点都会卡住浏览器。
    assert "GB_COSE_MAX_NODES" in body and '"grid"' in body


def test_browse_by_category_replaces_load_more():
    """§3.2 规格 9：「加载更多证型」→「按门类浏览」。

    「加载更多」回答的是"再给我 80 个"，而用户想问的是"脾的证型有哪些"。
    翻页在一堆互不相连的证型上没有意义：翻到第 3 页看到的还是一堆孤立方块。"""
    src = _graph_js()
    assert "function gbBrowseCategory" in src
    assert "gbLoadMoreSyndromes" not in src
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="gb-category-select"' in html
    assert 'id="gb-more"' not in html


def test_the_category_list_is_built_from_the_location_elements():
    """门类 = 证候表的 `location` 字段（脾/胃/肝/肠/中焦……）。**图里已经有
    这一层**：build_graph 把每个 location 建成一个 category="location" 的证素
    节点。所以「按门类浏览」= 展开那个证素——复用同一条路径，不另写一套按门类
    拉数据的逻辑（CLAUDE.md 第 31 条）。"""
    src = _graph_js()
    body = src[src.index("function populateGbCategorySelect"):]
    body = body[:body.index("async function gbBrowseCategory")]
    assert 'n.data.category !== "location"' in body
    expand = src[src.index("async function gbBrowseCategory"):]
    expand = expand[:expand.index("function renderGbLambda1Note")]
    assert "gbExpandNode(elementId)" in expand


def test_the_category_select_hides_itself_when_there_is_nothing_to_browse():
    """一个门类都没有时藏起来，不留一个只有占位项的空下拉——那是点了没反应的
    控件，跟"切换到医案层"按钮在没有医案层时藏起来是同一条理由。"""
    src = _graph_js()
    assert "sel.hidden = sel.options.length <= 1;" in src


def test_search_still_goes_to_the_server_and_reports_the_total():
    """§3.2 规格 8（已有，这里钉住没退化）：本地只有已加载的那部分，在本地搜
    等于"只在画布上已经有的东西里找"——搜不到的时候用户会以为图里没有，
    其实是没搜过。"""
    src = _graph_js()
    body = src[src.index("async function gbSearch"):]
    body = body[:body.index("async function gbToggleLayer")]
    assert "/api/graph/search?q=" in body
    assert "找到 ${page.total} 个匹配节点" in body
    assert "gb-search-hit" in body and "fit:" in body
