"""core/graph/store.py 的离线测试：NetworkXStore 的增删查改、持久化往返、层级无环、
边必须带 source。不需要网络。

命名成 test_graph_store.py 不是 test_graph.py——后者已经是 api/main.py 的
to_graph()（单次 consult 的可视化拼图，跟这里的持久化知识图谱是两回事）在用了。
"""
import networkx as nx

from core.graph.store import NetworkXStore
from core.schemas import SyndromeDefinition
from offline.build_graph import build_graph


def test_add_and_get_node():
    store = NetworkXStore()
    store.add_node("syndrome::S001", node_type="syndrome", name="脾胃气虚")
    node = store.get_node("syndrome::S001")
    assert node["node_type"] == "syndrome"
    assert node["name"] == "脾胃气虚"


def test_get_node_missing_returns_none():
    store = NetworkXStore()
    assert store.get_node("nope") is None


def test_add_edge_and_neighbors():
    store = NetworkXStore()
    store.add_node("a", node_type="symptom")
    store.add_node("b", node_type="element")
    store.add_edge("a", "b", edge_type="indicates", source="gb_standard")
    neighbors = store.neighbors("a")
    assert len(neighbors) == 1
    dst, attrs = neighbors[0]
    assert dst == "b"
    assert attrs["edge_type"] == "indicates"
    assert attrs["source"] == "gb_standard"


def test_neighbors_filters_by_edge_type():
    store = NetworkXStore()
    store.add_node("a", node_type="symptom")
    store.add_node("b", node_type="element")
    store.add_node("c", node_type="syndrome")
    store.add_edge("a", "b", edge_type="indicates", source="gb_standard")
    store.add_edge("a", "c", edge_type="composes", source="gb_standard")
    assert len(store.neighbors("a", edge_type="indicates")) == 1
    assert len(store.neighbors("a", edge_type="composes")) == 1
    assert len(store.neighbors("a")) == 2


def test_neighbors_missing_node_returns_empty_list():
    store = NetworkXStore()
    assert store.neighbors("nope") == []


def test_add_edge_same_type_updates_not_duplicates():
    """K2 靠这个语义给 indicates 边加 weight_by_physician：重复调用是更新，
    不是新增一条平行边。"""
    store = NetworkXStore()
    store.add_node("a", node_type="symptom")
    store.add_node("b", node_type="element")
    store.add_edge("a", "b", edge_type="indicates", source="gb_standard", weight=0.1)
    store.add_edge("a", "b", edge_type="indicates", source="gb_standard", weight=0.9)
    neighbors = store.neighbors("a", edge_type="indicates")
    assert len(neighbors) == 1
    assert neighbors[0][1]["weight"] == 0.9


def test_find_nodes_by_type_and_filter():
    store = NetworkXStore()
    store.add_node("syndrome::S001", node_type="syndrome", is_category=False)
    store.add_node("syndrome::S002", node_type="syndrome", is_category=True)
    store.add_node("element::脾", node_type="element")
    leaf_syndromes = store.find_nodes("syndrome", is_category=False)
    assert leaf_syndromes == ["syndrome::S001"]
    assert len(store.find_nodes("syndrome")) == 2


def test_save_and_load_roundtrip(tmp_path):
    store = NetworkXStore()
    store.add_node("a", node_type="symptom", name="胃脘痛")
    store.add_node("b", node_type="element", name="胃")
    store.add_edge("a", "b", edge_type="indicates", source="gb_standard")

    path = tmp_path / "graph.json"
    store.save(path)

    loaded = NetworkXStore()
    loaded.load(path)
    assert loaded.get_node("a")["name"] == "胃脘痛"
    neighbors = loaded.neighbors("a", edge_type="indicates")
    assert neighbors[0][0] == "b"


def _sample_definition(**overrides) -> SyndromeDefinition:
    base = dict(
        code="S001",
        name="脾胃气虚",
        is_category=False,
        parent=None,
        definition="脾胃气虚证是指脾胃气虚，运化失健所表现的证候。",
        location=["脾", "胃"],
        nature=["气虚"],
        cardinal_symptoms=["纳差", "乏力"],
        secondary_symptoms=["便溏"],
        tongue_pulse="舌淡苔白，脉细弱",
        source="gb_standard",
    )
    base.update(overrides)
    return SyndromeDefinition(**base)


def test_build_graph_every_edge_has_source():
    defs = [_sample_definition()]
    store = build_graph(defs)
    assert store.g.number_of_edges() > 0
    for _, _, data in store.g.edges(data=True):
        assert "source" in data
        assert data["source"] == "gb_standard"


def test_build_graph_marks_category_nodes():
    defs = [
        _sample_definition(code="S001", is_category=False, parent="S000"),
        _sample_definition(code="S000", name="脾胃病类", is_category=True, parent=None,
                            cardinal_symptoms=[], secondary_symptoms=[]),
    ]
    store = build_graph(defs)
    leaf = store.get_node("syndrome::S001")
    category = store.get_node("syndrome::S000")
    assert leaf["is_category"] is False
    assert category["is_category"] is True


def test_is_a_subgraph_has_no_cycle():
    defs = [
        _sample_definition(code="grandchild", parent="child", cardinal_symptoms=[], secondary_symptoms=[]),
        _sample_definition(code="child", parent="root", cardinal_symptoms=[], secondary_symptoms=[]),
        _sample_definition(code="root", parent=None, cardinal_symptoms=[], secondary_symptoms=[]),
    ]
    store = build_graph(defs)
    is_a_edges = [
        (u, v) for u, v, d in store.g.edges(data=True) if d.get("edge_type") == "is_a"
    ]
    is_a_subgraph = nx.DiGraph(is_a_edges)
    assert nx.is_directed_acyclic_graph(is_a_subgraph)
