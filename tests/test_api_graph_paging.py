"""F1：/api/graph 分页 + neighbors + search。

为什么分页要连带两个新端点：原来的图谱浏览器是"一次拿全图 → 本地建邻接表 →
展开和搜索全在本地做"。只加一个 limit 参数，展开和搜索会**静默退化**成
"只在已经画出来的那部分里找"——那不是没找到，是没找过，比报错更误导。

默认行为（不传 limit）必须跟分页之前逐字节一样：旧客户端和
test_graph_endpoint_matches_real_store_counts 都依赖它。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from api.main import get_graph_store


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture
def store():
    s = get_graph_store()
    assert s is not None, "这个仓库应该带着 data/graph.json"
    return s


def test_no_limit_means_the_whole_graph_exactly_like_before(client, store):
    data = client.get("/api/graph").json()
    assert len(data["graph"]["nodes"]) == store.g.number_of_nodes()
    assert len(data["graph"]["edges"]) == store.g.number_of_edges()
    assert data["page"]["limit"] == 0 and data["page"]["next_cursor"] is None


def test_limit_returns_one_page_and_a_cursor_to_the_next(client, store):
    first = client.get("/api/graph?limit=10").json()
    assert len(first["graph"]["nodes"]) == 10
    assert first["page"]["total"] == store.g.number_of_nodes()
    assert first["page"]["next_cursor"] == 10

    second = client.get(f"/api/graph?limit=10&cursor={first['page']['next_cursor']}").json()
    ids1 = {n["data"]["id"] for n in first["graph"]["nodes"]}
    ids2 = {n["data"]["id"] for n in second["graph"]["nodes"]}
    assert not (ids1 & ids2), "翻页重复发了同一批节点——游标是位置偏移，顺序必须稳定"


def test_paging_all_the_way_through_covers_every_node_exactly_once(client, store):
    seen, cursor = [], 0
    while True:
        page = client.get(f"/api/graph?limit=250&cursor={cursor}").json()
        seen += [n["data"]["id"] for n in page["graph"]["nodes"]]
        if page["page"]["next_cursor"] is None:
            break
        cursor = page["page"]["next_cursor"]
    assert len(seen) == len(set(seen)) == store.g.number_of_nodes()


def test_node_types_filter_narrows_the_page_and_the_total(client, store):
    page = client.get("/api/graph?node_types=syndrome&limit=5").json()
    assert all(n["data"]["node_type"] == "syndrome" for n in page["graph"]["nodes"])
    expected = sum(1 for _n, d in store.g.nodes(data=True) if d.get("node_type") == "syndrome")
    assert page["page"]["total"] == expected


def test_a_page_only_carries_edges_whose_both_ends_are_on_it(client):
    page = client.get("/api/graph?limit=40").json()
    ids = {n["data"]["id"] for n in page["graph"]["nodes"]}
    for e in page["graph"]["edges"]:
        assert e["data"]["source"] in ids and e["data"]["target"] in ids


def _some_node_with_neighbors(store):
    for nid in store.g.nodes():
        if store.g.degree(nid) > 0:
            return nid
    pytest.skip("图里没有带边的节点")


def test_neighbors_returns_the_nodes_on_the_other_end(client, store):
    nid = _some_node_with_neighbors(store)
    data = client.get("/api/graph/neighbors", params={"node": nid}).json()
    got = {n["data"]["id"] for n in data["graph"]["nodes"]}
    expected = set(store.g.successors(nid)) | set(store.g.predecessors(nid))
    expected.discard(nid)
    assert got == expected
    assert data["page"]["total"] == len(expected)


def test_neighbors_counts_incoming_edges_too(client, store):
    """图谱浏览器展示的是关联不是流向：只看出边会让症状点不开它的证素。"""
    target = None
    for nid in store.g.nodes():
        if store.g.in_degree(nid) > 0 and store.g.out_degree(nid) == 0:
            target = nid
            break
    if target is None:
        pytest.skip("图里没有纯入边节点")
    data = client.get("/api/graph/neighbors", params={"node": target}).json()
    assert data["graph"]["nodes"], "只看出边的话这里会是空的"


def test_neighbors_truncates_loudly(client, store):
    nid = max(store.g.nodes(), key=lambda n: store.g.degree(n))
    if store.g.degree(nid) < 3:
        pytest.skip("图太小")
    data = client.get("/api/graph/neighbors", params={"node": nid, "limit": 2}).json()
    assert data["page"]["returned"] == 2
    assert data["page"]["truncated"] is True
    assert data["page"]["total"] > 2


def test_neighbors_404_for_an_unknown_node(client):
    assert client.get("/api/graph/neighbors", params={"node": "没有这个节点"}).status_code == 404


def test_search_matches_labels_across_the_whole_graph_not_just_a_page(client, store):
    label = None
    for _nid, d in store.g.nodes(data=True):
        if d.get("node_type") == "symptom" and d.get("name"):
            label = d["name"]
            break
    if not label:
        pytest.skip("图里没有带名字的症状节点")
    data = client.get("/api/graph/search", params={"q": label}).json()
    assert data["page"]["total"] >= 1
    assert any(n["data"]["label"] == label for n in data["graph"]["nodes"])


def test_search_reports_the_real_hit_count_even_when_it_truncates(client):
    data = client.get("/api/graph/search", params={"q": "痛", "limit": 3}).json()
    if data["page"]["total"] <= 3:
        pytest.skip("这份图里\"痛\"命中太少")
    assert data["page"]["returned"] == 3
    assert data["page"]["truncated"] is True


def test_search_with_an_empty_query_returns_nothing_rather_than_everything(client):
    data = client.get("/api/graph/search", params={"q": "   "}).json()
    assert data["page"]["total"] == 0 and data["graph"]["nodes"] == []


def test_search_can_be_restricted_by_node_type(client):
    data = client.get("/api/graph/search", params={"q": "证", "node_types": "syndrome"}).json()
    assert all(n["data"]["node_type"] == "syndrome" for n in data["graph"]["nodes"])
