"""R43：**全站交互延迟的预算**（总纲 §12：性能预算进测试）。

## 这一轮量到的第一件事：光看 INP 会得出错误结论

R43 的基线用 `PerformanceEventTiming` 把每一次交互拆成三段
（input delay / processing / presentation），跑完发现**全部 12 个交互的
`processing` 段都是 ~0 ms**——因为这个应用的点击处理函数都是立刻返回的，
真正的工作（fetch、合并、重排）在 await 之后，落在 INP 的观测窗口之外。

也就是说：**INP 说"一点延迟都没有"，而用户明明在等。**

所以这一轮的预算是**两个数**，各自回答一件事：

| 指标 | 回答 | 上限 | 上限的来处 |
|---|---|---|---|
| INP | 点下去有没有立刻响应（**卡不卡**） | 200 ms / 瞬时类 100 ms | Core Web Vitals 的 "good" 阈值 |
| settle（点到结果出现） | 这件事总共等了多久（**快不快**） | 1000 ms | Nielsen 三档里"不打断思路"的那一档 |

两个上限都是**外部基准**，不是自己拍的——自己定一个宽松的数然后宣布达标，
那个数就没有意义。

## 这个文件测什么、不测什么

**测得到**（不需要浏览器，秒级）：服务端那几个交互端点的延迟与载荷、
压缩有没有生效、SSE 有没有被压缩缓冲卡住、前端源码里那几条"立刻给反馈"
与"不重建全量索引"的判据。

**测不到**：真实的 INP 与 settle。那要真浏览器 + 可信输入，是
`scripts/profile_interactions.py` 的活（判据同样写在那个文件里，
数字进 `docs/reports/R43_report.md`）。
"""
from __future__ import annotations

import gzip
import json
import re
import statistics
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main

ROOT = Path(__file__).resolve().parent.parent
GRAPH_JS = (ROOT / "web" / "graph.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

#: 服务端交互端点的预算。**比 INP 的 200 ms 紧得多**：这只是"点到结果出现"
#: 那 1000 ms 里的一段，后面还有传输、解析、合并、重排。
ENDPOINT_BUDGET_MS = 150

#: 交互型端点清单。**"人点一下就会发的请求"**，不含 /api/consult
#: （那是几十秒的推理，R40 已经单独治过，判据在那一轮）。
INTERACTIVE_ENDPOINTS = [
    "/api/graph?limit=200",
    "/api/graph?node_types=syndrome&limit=200",
    "/api/graph/search?q=痛&limit=150",
    "/api/graph/neighbors?node=element::肝&limit=150",
    "/api/node_explain?node=herb::四君子汤::党参",
    "/api/node_explain?node=syn::x&name=肝胃不和证",
]


@pytest.fixture(scope="module")
def client():
    # **必须用 with**：不进上下文的话 lifespan 不跑、预热不跑，量到的是冷态
    # （R40 那条"量具错了它量出来的每个数都是错的"）。
    with TestClient(api_main.app) as c:
        yield c


def _bench(client, path: str, n: int = 11) -> tuple[float, float, int]:
    r = client.get(path)                      # 预热一次，把惰性初始化排除掉
    assert r.status_code == 200, (path, r.status_code)
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.get(path)
        xs.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200
    xs.sort()
    return statistics.median(xs), xs[-1], len(r.content)


# ---------- 一、服务端交互端点 ----------

@pytest.mark.parametrize("path", INTERACTIVE_ENDPOINTS)
def test_every_interactive_endpoint_stays_inside_its_budget(client, path):
    med, mx, size = _bench(client, path)
    assert med <= ENDPOINT_BUDGET_MS, (
        f"{path} 中位 {med:.1f} ms 超过预算 {ENDPOINT_BUDGET_MS} ms"
        f"（最大 {mx:.1f} ms，响应体 {size / 1024:.1f} KB）")


def test_the_budget_is_an_external_benchmark_not_a_number_we_picked(client):
    """预算的来处要写在代码里。**一个没有来处的上限等于没有上限**——
    它会在第一次红的时候被调大。"""
    src = Path(__file__).read_text(encoding="utf-8")
    assert "Core Web Vitals" in src
    assert "Nielsen" in src


# ---------- 二、压缩 ----------

def test_the_heaviest_interactive_payload_is_compressed(client):
    """点开「图谱」页签会拉这个。**R43 之前它 352 KB、一个字节都没压。**"""
    raw = client.get("/api/graph?limit=200", headers={"Accept-Encoding": "identity"})
    gz = client.get("/api/graph?limit=200", headers={"Accept-Encoding": "gzip"})
    assert gz.headers.get("content-encoding") == "gzip"
    on_wire = int(gz.headers["content-length"])
    assert on_wire < len(raw.content) * 0.25, (
        f"压完还有 {on_wire} 字节（原始 {len(raw.content)}），压缩像是没生效")


def test_the_sse_endpoint_is_never_compressed():
    """**SSE 不许走 gzip。**

    starlette 的 GZipMiddleware 对流式响应是逐块写进 gzip 缓冲再发，而 gzip
    在攒够一个块之前不产出任何字节——"一边推理一边出字"会变成"憋一会儿吐
    一大段"。R36 花了一整轮把流式做出来，不能在这里被压缩缓冲抵消掉。

    判据看的是**路径判定函数**，不是跑一次 SSE：跑一次要起真模型，
    而这条约束是纯逻辑的。"""
    assert api_main._gzip_skip("/api/consult/stream")
    assert api_main._gzip_skip("/api/consult/stream/abc/answer") is True
    assert not api_main._gzip_skip("/api/graph")


@pytest.mark.parametrize("path", [
    "/app/vendor/fonts/noto-serif-sc-400-subset.woff2",
    "/app/whatever.png",
])
def test_already_compressed_assets_are_not_recompressed(path):
    """woff2 / png 再压一遍省不下几个字节，却要为每个请求付一次 CPU。
    字体那 1.13 MB 是首屏的大头，白烧 CPU 会直接体现在首屏时间上。"""
    assert api_main._gzip_skip(path)


def test_the_static_javascript_is_compressed(client):
    """graph.js 145 KB。它不在首屏关键路径上（用时才加载），但点开图谱页
    要等它——属于交互延迟。"""
    gz = client.get("/app/graph.js", headers={"Accept-Encoding": "gzip"})
    assert gz.status_code == 200
    assert gz.headers.get("content-encoding") == "gzip"


def test_compression_does_not_change_the_bytes(client):
    """**不为性能牺牲正确性**（总纲 §12）：压完解开必须逐字节相同。"""
    raw = client.get("/api/graph?limit=200", headers={"Accept-Encoding": "identity"})
    gz = client.get("/api/graph?limit=200", headers={"Accept-Encoding": "gzip"})
    # TestClient 会自动解压，所以这里直接比解出来的内容
    assert gz.content == raw.content
    assert json.loads(gz.content) == json.loads(raw.content)


def test_a_client_that_cannot_gzip_still_gets_a_valid_response(client):
    r = client.get("/api/graph?limit=200", headers={"Accept-Encoding": "identity"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") is None
    assert json.loads(r.content)["graph"]["nodes"]


def test_the_gzip_middleware_reuses_starlettes_implementation():
    """自己写一遍 gzip 响应器就是同一件事的第二处实现。"""
    import inspect

    src = inspect.getsource(api_main.SelectiveGZipMiddleware)
    assert "GZipMiddleware" in src
    assert "zlib" not in src and "gzip.compress" not in src


def test_gzip_really_shrinks_this_payload_a_lot(client):
    """把"压缩有没有意义"量出来，不是假设它有意义。"""
    raw = client.get("/api/graph?limit=200", headers={"Accept-Encoding": "identity"}).content
    ratio = len(gzip.compress(raw)) / len(raw)
    assert ratio < 0.15, f"这份载荷的压缩率只有 {ratio:.2%}，压缩的收益没有想象中大"


# ---------- 三、载荷大小 ----------

def test_the_first_graph_page_is_not_unbounded(client):
    """分页上限在，且这一页的字节数有个上限。**没有上限的话，教材扩完之后
    这一页会悄悄长到几 MB**，而表现只是"点图谱页要等一会儿"。"""
    body = client.get("/api/graph?limit=200").json()
    assert body["page"]["returned"] <= 200
    assert body["page"]["total"] > body["page"]["returned"], "这条测试要在真图上跑"
    on_wire = int(client.get("/api/graph?limit=200",
                             headers={"Accept-Encoding": "gzip"}).headers["content-length"])
    assert on_wire < 64 * 1024, f"压缩后仍有 {on_wire / 1024:.1f} KB"


def test_node_explain_stays_inside_the_r42_budget(client):
    """R42 定的 50 ms（热态）。这里再钉一遍是因为 R43 新增的压缩中间件在
    每个响应上都加了一层——**加中间件不许把既有预算吃掉**。"""
    med, mx, _ = _bench(client, "/api/node_explain?node=herb::四君子汤::党参")
    assert med <= api_main.NODE_EXPLAIN_BUDGET_MS, f"中位 {med:.1f} ms，最大 {mx:.1f} ms"


# ---------- 四、前端：点下去立刻有反馈 ----------

def test_the_graph_browser_says_what_it_is_doing_before_the_response_arrives():
    """改之前状态栏要等响应回来才变——在三甲内网上点「搜索」之后按钮看起来
    是死的。**"点了没反应"跟静默出错是同一类失败**，只不过这一次是静默等待。"""
    body = GRAPH_JS.split("async function gbFetchInto")[1][:2200]
    assert "status.textContent = busy" in body
    assert 'setAttribute("aria-busy", "true")' in body
    # 文案要说清在做什么，不是一个笼统的「加载中」
    assert "GB_BUSY_TEXT" in GRAPH_JS
    for key in ("search", "expand", "layer"):
        assert f"{key}:" in GRAPH_JS.split("GB_BUSY_TEXT = {")[1][:300]


def test_the_busy_text_escalates_only_after_a_threshold():
    """300 ms 以下的等待人感觉不到，弹一个转圈反而制造"刚才是不是卡了"
    的印象。"""
    m = re.search(r"const GB_PENDING_HINT_MS = (\d+);", GRAPH_JS)
    assert m and 150 <= int(m.group(1)) <= 600, "升级文案的阈值不合理"


def test_every_fetch_call_site_says_which_kind_it_is():
    """`gbFetchInto` 的最后一个参数是"这次在做什么"。漏传的表现是状态栏显示
    一个笼统的「加载中…」——那比不显示好，但仍然说不清在等什么。"""
    calls = re.findall(r"gbFetchInto\(\s*\n?(.*?)\n\s*\);", GRAPH_JS, re.S)
    assert len(calls) >= 3, f"只找到 {len(calls)} 处调用"
    for c in calls:
        assert re.search(r'"(search|expand|layer)"', c), f"这处调用没说在做什么：{c[:80]}"


def test_a_stale_response_cannot_overwrite_a_newer_status():
    """连点两次搜索时，先回来的那个不该覆盖后点的那次——同
    `openNodeExplain` 的 `NODE_EXPLAIN_SEQ`，那是同一类竞态的另一个入口。"""
    body = GRAPH_JS.split("async function gbFetchInto")[1][:2600]
    assert "const seq = ++gbFetchSeq" in body
    assert "seq === gbFetchSeq" in body


def test_the_node_explain_panel_shows_a_skeleton_before_the_content():
    """点一个节点之后屏幕上什么都不变，人会以为"点了没反应"再点一下
    （于是又发一次请求）。"""
    assert "function showNodeExplainPending" in APP_JS
    body = APP_JS.split("async function openNodeExplain")[1][:900]
    assert "showNodeExplainPending(" in body
    skel = APP_JS.split("function showNodeExplainPending")[1][:700]
    assert "查询中" in skel
    assert 'setAttribute("aria-busy", "true")' in skel


def test_the_pending_skeleton_names_the_node_instead_of_spinning():
    """**不是一个转圈图标**：这里能立刻说出"正在查哪个节点"，那比一个匿名的
    加载动画有用得多（人据此确认自己点对了）。"""
    skel = APP_JS.split("function showNodeExplainPending")[1][:700]
    assert "ne-title" in skel and "title" in skel


# ---------- 五、前端：点击处理里不许有随规模增长的重活 ----------

def test_the_edge_id_set_is_not_rebuilt_on_every_merge():
    """改之前 `gbMergeGraph` 每次都 `new Set(全部边.map(...))`——O(已累积的边数)，
    而一次浏览会合并很多次（首屏、每次展开、每次搜索），合起来是
    O(边数 × 合并次数)。本沙盒的持久图是 2356 节点 / 3682 边，教材扩完还要
    翻几倍，**而这段代码跑在点击的处理函数里**。"""
    body = GRAPH_JS.split("function gbMergeGraph")[1][:900]
    assert "gbIndex.edgeIds" in body
    assert "new Set(gbGraphData.graph.edges" not in body, "又在每次合并时重建全量集合了"
    idx = GRAPH_JS.split("function gbBuildIndex")[1][:1400]
    assert "edgeIds" in idx, "索引里没有长期留着的边 id 集合"


def test_the_graph_data_really_is_big_enough_for_this_to_matter():
    """**判据要跟真实规模挂钩**，不然上面那条只是在防一个想象中的问题。"""
    data = json.loads((ROOT / "data" / "graph.json").read_text(encoding="utf-8"))
    assert len(data["nodes"]) > 1000, f"只有 {len(data['nodes'])} 个节点"
    assert len(data["edges"]) > 1000


# ---------- 六、量具本身 ----------

def test_the_profiler_measures_both_responsiveness_and_completion():
    """两个数缺一个，就会把"卡"和"慢"混成一件事。"""
    src = (ROOT / "scripts" / "profile_interactions.py").read_text(encoding="utf-8")
    assert "INP_BUDGET_MS" in src and "SETTLE_BUDGET_MS" in src
    assert "input_delay" in src and "processing" in src and "presentation" in src


def test_the_profiler_uses_trusted_input_not_el_click():
    """`el.click()` 造的是不可信事件，不进 `PerformanceEventTiming`
    ——量不到真实的输入延迟（R41 在 CLS 那条上已经踩过同一个坑）。"""
    src = (ROOT / "scripts" / "profile_interactions.py").read_text(encoding="utf-8")
    # **只看会真的跑起来的那部分**：模块文档里必须能提 `el.click()`
    # ——那整段话正是在解释为什么不用它（同 CLAUDE.md 那条数
    # `Field(min_length=1)` 的规矩：数法定死，不然判据自己会变成假绿）。
    import ast

    tree = ast.parse(src)
    doc_ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            doc_ids.add(id(first.value))
    code_strings = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and id(n) not in doc_ids]
    assert not any("el.click()" in s for s in code_strings), "量具里还在用不可信点击"
    assert "page.click(" in src and "page.mouse.click(" in src


def test_the_profiler_covers_every_kind_of_interaction_on_the_site():
    """**覆盖的是"人在这一页上真的会点的每一样东西"**，不是"容易量的那几样"。"""
    from scripts.profile_interactions import SCENES

    for want in ("tab_switch", "typing", "graph_node_click", "png_export",
                 "browser_search_broad", "browser_expand_repeat", "role_switch"):
        assert want in SCENES, f"量具没覆盖 {want}"
    assert len(SCENES) >= 12


def test_the_cold_only_settle_is_marked_as_such():
    """切页签那条只量第一次：第二次起数据已经在内存里，判据立刻成立——
    量出来是 9 ms，而那 9 ms 什么都没等。**报一个"什么都没等"的数比不报更糟。**"""
    from scripts.profile_interactions import SCENES

    assert SCENES["tab_switch"].get("settle_first_only") is True
