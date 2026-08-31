"""api/main.py 的 to_graph() 离线测试：验证边的两端节点都存在（round 5 的硬性验收点）。"""
from api.main import assert_graph_edges_valid, to_graph
from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome


def _make_results() -> list[dict]:
    s1_symptoms = ["纳差", "乏力", "口苦"]

    s2_ye = S2Elements(
        elements=[
            ElementHit(
                element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high"
            ),
            ElementHit(
                element="气虚", kind="nature", supporting_symptoms=["乏力"], confidence="medium"
            ),
        ],
        unexplained_symptoms=["口苦"],
    )
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力，责之脾虚",
        treatment_principle="健脾益气",
        formula="四君子汤",
        herbs=["党参", "白术", "茯苓", "炙甘草"],
        cited_case_ids=["ye_tianshi-001"],
    )

    s2_wu = S2Elements(
        elements=[
            ElementHit(
                element="胃", kind="location", supporting_symptoms=["纳差"], confidence="high"
            ),
            # 故意让支撑症状带一个 s1.symptoms 里没有的措辞，验证边不会指向不存在的节点
            ElementHit(
                element="热", kind="nature", supporting_symptoms=["口苦口黏"], confidence="low"
            ),
        ],
        unexplained_symptoms=[],
    )
    s3_wu = S3Syndrome(
        syndrome="胃热",
        reasoning="纳差，责之胃热",
        treatment_principle="清胃泄热",
        herbs=["黄连", "黄芩"],
        cited_case_ids=["wu_jutong-001"],
    )

    return [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": s2_ye,
            "s3": s3_ye,
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        },
        {
            "physician": "wu_jutong",
            "physician_name": "吴鞠通",
            "s2": s2_wu,
            "s3": s3_wu,
            "refs": [("wu_jutong-001", 0.8)],
            "hallucinated": [],
        },
    ]


def test_graph_edges_all_point_to_existing_nodes():
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    graph = to_graph(s1, _make_results())
    assert_graph_edges_valid(graph)  # 不抛异常即通过


def test_symptom_not_matched_by_any_element_gets_dropped_edge_not_dangling():
    # s2_wu 的 supporting_symptoms 用了"口苦口黏"而不是 s1 里的"口苦"，
    # 这条边理应被静默丢弃，而不是指向一个不存在的症状节点。
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    graph = to_graph(s1, _make_results())
    node_ids = {n["data"]["id"] for n in graph["nodes"]}
    assert "sym::口苦口黏" not in node_ids
    for e in graph["edges"]:
        assert e["data"]["target"] != "sym::口苦口黏"
        assert e["data"]["source"] != "sym::口苦口黏"


def test_symptom_state_explained_vs_unexplained():
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    graph = to_graph(s1, _make_results())
    state_by_id = {n["data"]["id"]: n["data"]["state"] for n in graph["nodes"] if n["data"]["layer"] == 0}
    assert state_by_id["sym::纳差"] == "explained"
    assert state_by_id["sym::乏力"] == "explained"
    assert state_by_id["sym::口苦"] == "unexplained"  # 没被任何证素的 supporting_symptoms 精确命中


def test_element_nodes_deduplicated_across_physicians():
    # 两位医家都命中了名为"胃"/"脾"这类可能重复的证素时，节点应只出现一次
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    results = [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(
                elements=[
                    ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")
                ]
            ),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=["党参"], cited_case_ids=["ye_tianshi-001"],
            ),
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        },
        {
            "physician": "wu_jutong",
            "physician_name": "吴鞠通",
            "s2": S2Elements(
                elements=[
                    ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")
                ]
            ),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=["白术"], cited_case_ids=["wu_jutong-001"],
            ),
            "refs": [("wu_jutong-001", 0.9)],
            "hallucinated": [],
        },
    ]
    graph = to_graph(s1, results)
    elem_nodes = [n for n in graph["nodes"] if n["data"]["id"] == "elem::脾"]
    assert len(elem_nodes) == 1


def test_max_six_herbs_per_physician():
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    many_herbs = [f"药{i}" for i in range(10)]
    results = [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(elements=[]),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=many_herbs, cited_case_ids=["ye_tianshi-001"],
            ),
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        }
    ]
    graph = to_graph(s1, results)
    herb_nodes = [n for n in graph["nodes"] if n["data"]["id"].startswith("herb::ye_tianshi::")]
    assert len(herb_nodes) == 6
