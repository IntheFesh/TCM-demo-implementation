"""core/graph/store.py 的离线测试：NetworkXStore 的增删查改、持久化往返、层级无环、
边必须带 source。不需要网络。

命名成 test_graph_store.py 不是 test_graph.py——后者已经是 api/main.py 的
to_graph()（单次 consult 的可视化拼图，跟这里的持久化知识图谱是两回事）在用了。
"""
import networkx as nx

from core.graph.store import NetworkXStore
from core.schemas import SyndromeDefinition
from offline.build_graph import build_graph, check_corpus_coverage


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


def test_add_edge_explicit_key_keeps_parallel_edges():
    """同一对节点之间、同一类型、来源不同的多条边必须都留下。默认 key=edge_type
    的语义（上一个用例）在这里会静默丢数据，所以 add_edge 收 edge_key。"""
    store = NetworkXStore()
    store.add_node("a", node_type="symptom")
    store.add_node("b", node_type="element")
    store.add_edge("a", "b", edge_key="indicates::S1", edge_type="indicates",
                   source="gb_standard", via_syndrome="S1", is_cardinal=True)
    store.add_edge("a", "b", edge_key="indicates::S2", edge_type="indicates",
                   source="gb_standard", via_syndrome="S2", is_cardinal=False)
    neighbors = store.neighbors("a", edge_type="indicates")
    assert len(neighbors) == 2
    assert {d["via_syndrome"] for _, d in neighbors} == {"S1", "S2"}
    assert {d["is_cardinal"] for _, d in neighbors} == {True, False}


def test_in_neighbors_returns_incoming_edges():
    """图里的边都是单向的（symptom->element->syndrome）。没有 in_neighbors 的话
    「这个证候由哪些证素构成」查不出任何东西——G1 的 query_graph 依赖它。"""
    store = NetworkXStore()
    store.add_node("e", node_type="element")
    store.add_node("s", node_type="syndrome")
    store.add_edge("e", "s", edge_type="composes", source="gb_standard")
    assert store.neighbors("s") == []
    incoming = store.in_neighbors("s")
    assert [src for src, _ in incoming] == ["e"]
    assert store.in_neighbors("s", edge_type="indicates") == []
    assert store.in_neighbors("不存在的节点") == []


def test_parallel_indicates_edges_survive_save_load(tmp_path):
    store = NetworkXStore()
    store.add_node("a", node_type="symptom")
    store.add_node("b", node_type="element")
    for code in ("S1", "S2", "S3"):
        store.add_edge("a", "b", edge_key=f"indicates::{code}", edge_type="indicates",
                       source="gb_standard", via_syndrome=code, is_cardinal=False)
    path = tmp_path / "g.json"
    store.save(path)
    loaded = NetworkXStore()
    loaded.load(path)
    assert {d["via_syndrome"] for _, d in loaded.neighbors("a")} == {"S1", "S2", "S3"}


def test_build_graph_keeps_one_indicates_edge_per_syndrome():
    """一个症状被两条证候定义共用、且两条证候共享同一个证素时，图里必须留下
    两条边。之前共用 key="indicates" 时后写的会盖掉先写的——真实数据上丢掉
    323 条事实里的 29 条，其中 4 条连主症/次症标注都被改写。"""
    defs = [
        _sample_definition(code="S1", name="甲证", location=["脾"], nature=[],
                           cardinal_symptoms=["纳呆"], secondary_symptoms=[]),
        _sample_definition(code="S2", name="乙证", location=["脾"], nature=[],
                           cardinal_symptoms=[], secondary_symptoms=["纳呆"]),
    ]
    store = build_graph(defs)
    edges = store.neighbors("symptom::纳呆", edge_type="indicates")
    assert len(edges) == 2
    assert {(d["via_syndrome"], d["is_cardinal"]) for _, d in edges} == {("S1", True), ("S2", False)}


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


def test_save_and_load_roundtrip_preserves_edge_source_attribute(tmp_path):
    """回归测试：networkx node_link_data 默认用 "source"/"target" 当边端点的
    结构字段名，跟我们自己边上的 source 属性（provenance）撞名——不显式改用
    别的结构字段名，save() 会把 provenance 静默覆盖成端点 node id，load()
    回来这条边直接没有 source 键了。"""
    store = NetworkXStore()
    store.add_node("a", node_type="symptom", name="胃脘痛")
    store.add_node("b", node_type="element", name="胃")
    store.add_edge("a", "b", edge_type="indicates", source="gb_standard")

    path = tmp_path / "graph.json"
    store.save(path)
    loaded = NetworkXStore()
    loaded.load(path)

    neighbors = loaded.neighbors("a", edge_type="indicates")
    assert neighbors[0][1]["source"] == "gb_standard"


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


def test_syndrome_definition_accepts_new_source_tiers():
    # 六档可信度分层都要能通过校验，不只是最初的 gb_standard/textbook/manual 三档
    for source in ("gb_standard", "official_consensus", "group_standard", "journal", "secondary_verified", "manual"):
        _sample_definition(source=source)


def test_syndrome_definition_icd11_code_optional():
    without = _sample_definition()
    assert without.icd11_code is None
    with_code = _sample_definition(icd11_code="SF70")
    assert with_code.icd11_code == "SF70"


def test_check_corpus_coverage_strict_only_checks_location_and_cardinal():
    # "呕吐" 只出现在 secondary_symptoms 里，不该被 strict 算命中，
    # 但要出现在 wording_gap_only（宽泛能找到，不是真空白）
    d = _sample_definition(
        location=["脾", "胃"], cardinal_symptoms=["脘腹痞满或疼痛"],
        secondary_symptoms=["恶心或呕吐"],
    )
    coverage = check_corpus_coverage([d], gate_keywords=["呕吐", "痞"])
    assert "呕吐" in coverage["strict_uncovered"]
    assert "呕吐" in coverage["wording_gap_only"]
    assert "痞" in coverage["strict_hits"]  # "脘腹痞满或疼痛" 里有"痞"


def test_check_corpus_coverage_fully_uncovered_keyword():
    d = _sample_definition(
        location=["脾", "胃"], cardinal_symptoms=["脘腹痞满或疼痛"],
        secondary_symptoms=[], nature=["气滞"],
        definition="一个完全不提这个门类的定义。",
    )
    coverage = check_corpus_coverage([d], gate_keywords=["积聚"])
    assert coverage["strict_uncovered"] == ["积聚"]
    assert coverage["broad_uncovered"] == ["积聚"]


def test_check_corpus_coverage_uses_synonyms_not_literal_substring():
    """回归测试：R1 期间的字面子串匹配会把这些全判成未覆盖——"水肿"不是"肿胀"的
    子串、"大便溏稀"不是"泄泻"的子串、"胃脘隐痛"不是"胃痛"的子串。改成走
    core.syndrome_norm 的 SYNONYMS 归一化后，这几条都应该被正确识别。"""
    d = _sample_definition(
        location=["脾", "胃"],
        cardinal_symptoms=["大便溏稀", "胃脘隐痛"],
        secondary_symptoms=["水肿"],
        nature=["气虚"],
        definition="一个不直接提「肿胀」「泄泻」「胃痛」这几个字面词的定义。",
    )
    coverage = check_corpus_coverage([d], gate_keywords=["肿胀", "泄泻", "胃痛"])
    # 泄泻、胃痛在 cardinal_symptoms 里，应该是 strict 命中
    assert "泄泻" in coverage["strict_hits"]
    assert "胃痛" in coverage["strict_hits"]
    # 肿胀只在 secondary_symptoms（水肿）里，strict 对不上、broad 能找到
    assert "肿胀" in coverage["strict_uncovered"]
    assert "肿胀" in coverage["wording_gap_only"]


def test_check_corpus_coverage_gate_keyword_itself_can_be_a_variant():
    """gate_keywords 传进来的写法本身也可能是变体（比如"胃脘痛"而不是"胃痛"），
    要先转成 canonical 名再比较，不能假设传入的关键词已经是 canonical 形式。"""
    d = _sample_definition(
        location=["胃"], cardinal_symptoms=["胃痛明显"],
        secondary_symptoms=[], nature=["气滞"],
    )
    coverage = check_corpus_coverage([d], gate_keywords=["胃脘痛"])
    assert "胃脘痛" in coverage["strict_hits"]
    assert coverage["wording_gap_only"] == []
