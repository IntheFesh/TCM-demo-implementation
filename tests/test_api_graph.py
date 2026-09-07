"""api/main.py 的 GET /api/graph 离线测试：图谱浏览器页签用的持久知识图谱端点。

跟 /api/consult 用的 to_graph() 不是同一个函数、也不是同一份数据——这里测的是
core/graph/store.py::NetworkXStore（data/graph.json）到 Cytoscape 形状的转换，
以及 has_case_layer / lambda1_note 这两个字段有没有如实反映真实数据（不是
硬编码"没有 case 层"）。
"""
from fastapi.testclient import TestClient

import api.main as api_main
from core.graph.store import NetworkXStore
from core.tools import get_graph_store
from offline.graph_stats import compute_stats, lambda1_note


def _fake_store_with_case_layer() -> NetworkXStore:
    """一个带 case 节点、且 case->syndrome 不对齐（没有 evidences 边）的合成图——
    用来验证 has_case_layer=True 这条分支，以及 lambda1_note 切到"case 节点存在
    但没对齐"那段文字，不是只验证这个 sandbox 真实数据恰好落入的那一种情况。"""
    store = NetworkXStore()
    store.add_node("symptom::纳呆", node_type="symptom", name="纳呆")
    store.add_node("element::脾", node_type="element", name="脾", category="location")
    store.add_node("syndrome::SP-01", node_type="syndrome", name="脾胃气虚证",
                   code="SP-01", is_category=False, definition="def", tongue_pulse="tp")
    store.add_edge("symptom::纳呆", "element::脾", edge_type="indicates", source="official_consensus",
                   via_syndrome="SP-01", is_cardinal=True,
                   weight_by_physician={"ye_tianshi": 1.0}, lambda1_by_physician={"ye_tianshi": 0.0})
    store.add_edge("element::脾", "syndrome::SP-01", edge_type="composes", source="official_consensus")
    store.add_node("case::ye_tianshi-001", node_type="case", physician="ye_tianshi",
                   symptoms=["纳呆"], syndrome="胃阳虚")  # 古籍证型，对不上 SP-01
    return store


# ---------- 503：图谱骨架还没建 ----------


def test_graph_endpoint_503_when_store_missing(monkeypatch):
    monkeypatch.setattr(api_main, "get_graph_store", lambda: None)
    client = TestClient(api_main.app)
    resp = client.get("/api/graph")
    assert resp.status_code == 503
    assert "build_graph.py" in resp.json()["detail"]


# ---------- 真实数据：这个 sandbox 的 data/graph.json 现状 ----------


def test_graph_endpoint_matches_real_store_counts():
    """不硬编码 123 个节点/377 条边这类具体数字——那样 AutoDL 上重新生成
    data/graph.json（比如挂了医案之后）这条测试就会变脆。改成动态跟真实
    store 的节点/边数对账，数字变了测试跟着变，测的是"转换有没有丢东西"
    这件事本身，不是这个 sandbox 当前的具体数字。"""
    store = get_graph_store()
    assert store is not None, "这个仓库应该带着 data/graph.json"
    client = TestClient(api_main.app)
    resp = client.get("/api/graph")
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["graph"]["nodes"]) == store.g.number_of_nodes()
    assert len(data["graph"]["edges"]) == store.g.number_of_edges()


def test_graph_endpoint_has_case_layer_reflects_real_data():
    """这个 sandbox 没有 cases.json，data/graph.json 只挂了国标层——如实应该是
    False。用真实 store 动态算，不是断言写死的 False：如果哪天这个 sandbox
    的 data/graph.json 真的挂了医案，这条测试要能跟着测出 True，而不是
    继续断言一个已经过时的 False。"""
    store = get_graph_store()
    expected = any(d.get("node_type") == "case" for _, d in store.g.nodes(data=True))
    client = TestClient(api_main.app)
    data = client.get("/api/graph").json()
    assert data["has_case_layer"] is expected


def test_graph_endpoint_lambda1_note_reuses_offline_graph_stats_verbatim():
    """闸门：这段免责声明只能有一处实现，接口不能自己重写或精简一遍。
    直接调 offline.graph_stats.lambda1_note()（跟接口内部调的是同一个函数）
    算出期望值，逐字符比较——这不是在测"文字大概对不对"，是在测"接口有没有
    真的调这个函数，而不是抄了一份改写"。"""
    store = get_graph_store()
    expected = lambda1_note(compute_stats(store))
    client = TestClient(api_main.app)
    data = client.get("/api/graph").json()
    assert data["lambda1_note"] == expected
    assert len(expected) > 0


def test_graph_endpoint_physicians_match_registry():
    from core.physicians import PHYSICIANS

    client = TestClient(api_main.app)
    data = client.get("/api/graph").json()
    assert [p["id"] for p in data["physicians"]] == list(PHYSICIANS.keys())
    for p in data["physicians"]:
        assert p["name"] == PHYSICIANS[p["id"]]["name"]


def test_graph_endpoint_node_and_edge_ids_are_unique():
    """cytoscape 靠 id 去重/定位元素，重复 id 会静默丢数据或渲染错乱——
    这条不是多余的，边 id 是拼出来的（f"{src}::{dst}::{key}"），拼错了才会
    在这种真实规模的数据上暴露重复。"""
    client = TestClient(api_main.app)
    data = client.get("/api/graph").json()
    node_ids = [n["data"]["id"] for n in data["graph"]["nodes"]]
    edge_ids = [e["data"]["id"] for e in data["graph"]["edges"]]
    assert len(node_ids) == len(set(node_ids))
    assert len(edge_ids) == len(set(edge_ids))


# ---------- 分支覆盖：case 层存在时 ----------


def test_graph_endpoint_has_case_layer_true_and_unaligned_note_when_case_nodes_exist(monkeypatch):
    """不能只测这个 sandbox 恰好落入的"没有 case 节点"这一种情况——用合成
    store 覆盖另一条分支，确认 has_case_layer=True、lambda1_note 切到
    "case 节点存在但没对齐"那段文字（不是继续吐"没有 case 节点"那段，
    两段文字如果对不上当前图的真实内容，比没有文字更误导，这正是
    offline/graph_stats.py 自己文档字符串里强调的）。"""
    fake_store = _fake_store_with_case_layer()
    monkeypatch.setattr(api_main, "get_graph_store", lambda: fake_store)
    client = TestClient(api_main.app)
    data = client.get("/api/graph").json()

    assert data["has_case_layer"] is True
    assert "已挂入" in data["lambda1_note"]
    assert "没有 case 节点" not in data["lambda1_note"]

    case_nodes = [n for n in data["graph"]["nodes"] if n["data"]["node_type"] == "case"]
    assert len(case_nodes) == 1
    assert case_nodes[0]["data"]["id"] == "case::ye_tianshi-001"


# ---------- 单元：_persistent_graph_to_cytoscape 的转换正确性 ----------


def test_persistent_graph_to_cytoscape_separates_provenance_source_from_edge_endpoints():
    """每条边自带的 provenance 属性也叫 source（gb_standard/case/…），跟
    cytoscape 要求的边端点字段 source 撞名——这正是 core/graph/store.py 的
    save()/load() 已经踩过一次的坑（改用 _node_src/_node_dst 避开）。这里的
    转换函数面向前端，把 source/target 这两个字段名让给端点，provenance
    改名成 data_source；这条测试直接钉住"两者没有互相覆盖"。"""
    store = NetworkXStore()
    store.add_node("symptom::纳呆", node_type="symptom", name="纳呆")
    store.add_node("element::脾", node_type="element", name="脾", category="location")
    store.add_edge("symptom::纳呆", "element::脾", edge_type="indicates", source="official_consensus")

    graph = api_main._persistent_graph_to_cytoscape(store)
    edge = graph["edges"][0]["data"]
    assert edge["source"] == "symptom::纳呆"
    assert edge["target"] == "element::脾"
    assert edge["data_source"] == "official_consensus"
    assert edge["edge_type"] == "indicates"


def test_persistent_graph_to_cytoscape_preserves_is_category_and_label():
    store = NetworkXStore()
    store.add_node("syndrome::CAT-01", node_type="syndrome", name="脾胃病类",
                   code="CAT-01", is_category=True, definition="", tongue_pulse="")

    graph = api_main._persistent_graph_to_cytoscape(store)
    node = graph["nodes"][0]["data"]
    assert node["id"] == "syndrome::CAT-01"
    assert node["label"] == "脾胃病类"
    assert node["is_category"] is True
    assert node["node_type"] == "syndrome"


def test_persistent_graph_to_cytoscape_falls_back_to_id_when_name_missing():
    store = NetworkXStore()
    store.add_node("weird::node", node_type="weird")  # 没有 name 字段的边界情况
    graph = api_main._persistent_graph_to_cytoscape(store)
    assert graph["nodes"][0]["data"]["label"] == "weird::node"
