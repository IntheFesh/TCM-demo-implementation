"""offline/build_graph.py 新增的 attach_case_triples()（X3 第二层）的离线测试。

命名带 _triples 后缀跟 build_graph 的骨架建图区分开——那部分（build_graph()/
check_corpus_coverage()）已经在 tests/test_graph_store.py 里间接测过，这里
只测新加的这个函数，不重复整个模块的测试。
"""
import json

from core.graph.store import NetworkXStore
from core.schemas import CaseTriple
from offline.build_graph import attach_case_triples


def _write_triples(tmp_path, triples: list[CaseTriple]):
    path = tmp_path / "case_triples.jsonl"
    path.write_text(
        "\n".join(t.model_dump_json() for t in triples) + "\n", encoding="utf-8"
    )
    return path


def _triple(**overrides):
    base = dict(
        case_id="c1", physician="ye_tianshi",
        subject="syndrome::case::肝胃不和", subject_type="syndrome",
        predicate="treated_by",
        object="therapy::疏肝和胃", object_type="therapy",
        source_span="原文片段",
    )
    base.update(overrides)
    return CaseTriple(**base)


def test_creates_subject_and_object_nodes_with_correct_types(tmp_path):
    store = NetworkXStore()
    path = _write_triples(tmp_path, [_triple()])
    attach_case_triples(store, path)

    assert store.get_node("syndrome::case::肝胃不和")["node_type"] == "syndrome"
    assert store.get_node("therapy::疏肝和胃")["node_type"] == "therapy"


def test_edge_carries_case_id_source_span_and_physician(tmp_path):
    store = NetworkXStore()
    path = _write_triples(tmp_path, [_triple()])
    attach_case_triples(store, path)

    _, attrs = store.neighbors("syndrome::case::肝胃不和", edge_type="treated_by")[0]
    assert attrs["case_id"] == "c1"
    assert attrs["physician"] == "ye_tianshi"
    assert attrs["source_span"] == "原文片段"
    assert attrs["source"] == "case"


def test_two_cases_sharing_same_triple_both_survive_not_overwritten(tmp_path):
    """这是 X3 的核心闸门：不同医案共享同一句治法原文时，第二条不能静默覆盖
    第一条——K1 阶段 indicates 边已经因为漏加 key 丢过 29 条事实，这里必须
    验证同样的坑没有复现。"""
    store = NetworkXStore()
    t1 = _triple(case_id="c1", source_span="第一条医案的原文")
    t2 = _triple(case_id="c2", source_span="第二条医案的原文")
    path = _write_triples(tmp_path, [t1, t2])
    attach_case_triples(store, path)

    edges = store.neighbors("syndrome::case::肝胃不和", edge_type="treated_by")
    assert len(edges) == 2
    case_ids = {attrs["case_id"] for _, attrs in edges}
    assert case_ids == {"c1", "c2"}
    spans = {attrs["source_span"] for _, attrs in edges}
    assert spans == {"第一条医案的原文", "第二条医案的原文"}


def test_two_cases_sharing_same_formula_herb_pair_both_survive(tmp_path):
    """同一坑的第二个实例：formula -contains-> herb，两条医案的方子都含"柴胡"，
    (formula, herb) 这对节点相同，同样必须靠 case_id 区分。"""
    store = NetworkXStore()
    t1 = _triple(
        case_id="c1", subject="formula::柴胡疏肝散", subject_type="formula",
        predicate="contains", object="herb::柴胡", object_type="herb",
    )
    t2 = _triple(
        case_id="c2", subject="formula::柴胡疏肝散", subject_type="formula",
        predicate="contains", object="herb::柴胡", object_type="herb",
    )
    path = _write_triples(tmp_path, [t1, t2])
    attach_case_triples(store, path)

    edges = store.neighbors("formula::柴胡疏肝散", edge_type="contains")
    assert len(edges) == 2


def test_evidences_and_practiced_by_do_not_need_case_scoped_key(tmp_path):
    """case 节点本身已经是 case_id 唯一的，一条医案只产出一条 evidences/
    practiced_by，天然不会跟别的医案撞——这两类边不属于 needs_case_scoped_key，
    这里验证即便如此也照常建边，不是漏加了 key 导致丢边。"""
    store = NetworkXStore()
    t = _triple(
        case_id="c1", subject="case::c1", subject_type="case",
        predicate="evidences", object="syndrome::case::肝胃不和", object_type="syndrome",
    )
    path = _write_triples(tmp_path, [t])
    attach_case_triples(store, path)

    edges = store.neighbors("case::c1", edge_type="evidences")
    assert len(edges) == 1


def test_reuses_existing_case_node_created_by_attach_cases(tmp_path):
    """attach_cases() 先建的 case 节点（带 symptoms 等丰富属性）不该被
    attach_case_triples() 的 add_node 覆盖掉。"""
    store = NetworkXStore()
    store.add_node("case::c1", node_type="case", symptoms=["纳差"], physician="ye_tianshi")
    t = _triple(
        case_id="c1", subject="case::c1", subject_type="case",
        predicate="practiced_by", object="physician::ye_tianshi", object_type="physician",
    )
    path = _write_triples(tmp_path, [t])
    attach_case_triples(store, path)

    node = store.get_node("case::c1")
    assert node["node_type"] == "case"
    assert node["symptoms"] == ["纳差"]  # 没被抹掉


def test_stats_report_triple_count_and_predicate_breakdown(tmp_path):
    store = NetworkXStore()
    triples = [
        _triple(case_id="c1"),
        _triple(case_id="c1", subject="case::c1", subject_type="case",
                predicate="practiced_by", object="physician::ye_tianshi", object_type="physician"),
    ]
    path = _write_triples(tmp_path, triples)
    stats = attach_case_triples(store, path)

    assert stats["triples"] == 2
    assert stats["by_predicate"] == {"treated_by": 1, "practiced_by": 1}


def test_skips_blank_lines(tmp_path):
    store = NetworkXStore()
    path = tmp_path / "case_triples.jsonl"
    path.write_text(_triple().model_dump_json() + "\n\n", encoding="utf-8")
    stats = attach_case_triples(store, path)
    assert stats["triples"] == 1
