"""offline/build_element_index.py 的离线测试：手搭一个最小图谱（不需要真实
data/standard/syndromes.jsonl），验证症状->证素的匹配和索引产出。
"""
import json

from core.graph.store import NetworkXStore
from core.schemas import CaseRecord
from offline.build_element_index import build_index, case_elements, main


def _store():
    store = NetworkXStore()
    store.add_node("symptom::胃脘胀满", node_type="symptom", name="胃脘胀满")
    store.add_node("element::脾", node_type="element", name="脾")
    store.add_node("element::气滞", node_type="element", name="气滞")
    store.add_edge("symptom::胃脘胀满", "element::脾", edge_type="indicates", source="gb_standard")
    store.add_edge("symptom::胃脘胀满", "element::气滞", edge_type="indicates", source="gb_standard")
    store.add_node("symptom::纳呆", node_type="symptom", name="纳呆")
    store.add_node("element::胃", node_type="element", name="胃")
    store.add_edge("symptom::纳呆", "element::胃", edge_type="indicates", source="gb_standard")
    return store


def _case(case_id, physician, symptoms):
    return CaseRecord(
        case_id=case_id, case_group_id=case_id, physician=physician,
        raw="x", symptoms=symptoms,
    )


def test_case_elements_matches_via_indicates_edges():
    store = _store()
    elements = case_elements(store, ["胃脘胀满", "纳呆"])
    assert set(elements) == {"脾", "气滞", "胃"}


def test_case_elements_reuses_fragment_matching_not_exact_string():
    """_match_graph_symptoms 支持片段级双向包含匹配，患者写"胃脘胀满疼痛"
    也该匹配上标准症状"胃脘胀满"——这是 core.tools 已经在用的匹配逻辑，
    这里只是验证真的走了这条路径，不是另写一套精确匹配。"""
    store = _store()
    elements = case_elements(store, ["胃脘胀满疼痛，情志不畅"])
    assert "脾" in elements and "气滞" in elements


def test_case_elements_symptom_not_on_graph_returns_empty():
    store = _store()
    assert case_elements(store, ["一个图谱里完全没有的症状表述"]) == []


def test_build_index_covers_all_cases_including_unmatched():
    store = _store()
    cases = [
        _case("a", "ye_tianshi", ["胃脘胀满"]),
        _case("b", "wu_jutong", ["图谱外症状"]),
    ]
    index = build_index(cases, store)
    assert index["a"]["physician"] == "ye_tianshi"
    assert set(index["a"]["elements"]) == {"脾", "气滞"}
    assert index["b"]["elements"] == []


# ---------- CLI ----------


def test_main_writes_element_index_json(tmp_path):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "symptoms": ["胃脘胀满"]},
    ]), encoding="utf-8")

    graph_path = tmp_path / "graph.json"
    _store().save(graph_path)

    out_path = tmp_path / "element_index.json"
    main(["--cases-path", str(cases_path), "--graph-path", str(graph_path), "--out", str(out_path)])

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert set(data["a"]["elements"]) == {"脾", "气滞"}


def test_main_raises_clear_error_when_cases_json_missing(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        main(["--cases-path", str(tmp_path / "nope.json"), "--graph-path", str(tmp_path / "g.json")])


def test_main_raises_clear_error_when_graph_missing(tmp_path):
    import pytest
    cases_path = tmp_path / "cases.json"
    cases_path.write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="build_graph"):
        main(["--cases-path", str(cases_path), "--graph-path", str(tmp_path / "nope_graph.json")])
