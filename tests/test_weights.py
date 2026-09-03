"""core/graph/weights.py 的离线测试：shrinkage_weight 的数学边界、
count_support 在合成 case 节点上的计数逻辑、apply_weights 端到端写边。
不需要网络、不需要真实 cases.json——K1/K2 阶段这个 sandbox 里没有真实
医案图数据（见 count_support 文档字符串），这里全部用手搭的合成图验证。
"""
from core.graph.store import NetworkXStore
from core.graph.weights import apply_weights, count_support, shrinkage_weight


def test_shrinkage_weight_n_d_zero_gives_lambda1_zero():
    _, lambdas = shrinkage_weight(0, 0.0, 0, 0.0, 0, 0.0, 0.7, num_schools=1)
    assert lambdas["lambda1"] == 0.0


def test_shrinkage_weight_n_d_large_gives_lambda1_near_one():
    _, lambdas = shrinkage_weight(10_000, 0.9, 10_000, 0.9, 10_000, 0.9, 0.7, num_schools=2)
    assert lambdas["lambda1"] > 0.999


def test_shrinkage_weight_lambdas_always_sum_to_one():
    cases = [
        (0, 0, 0, 1),
        (1, 1, 1, 1),
        (5, 3, 100, 1),
        (50, 0, 0, 2),
    ]
    for n_d, n_school, n_global, num_schools in cases:
        _, lambdas = shrinkage_weight(
            n_d, 0.5, n_school, 0.5, n_global, 0.5, 0.6, num_schools=num_schools
        )
        total = sum(lambdas.values())
        assert abs(total - 1.0) < 1e-9, (n_d, n_school, n_global, num_schools, lambdas)


def test_shrinkage_weight_single_school_forces_lambda2_zero():
    _, lambdas = shrinkage_weight(3, 0.5, 3, 0.5, 3, 0.5, 0.6, num_schools=1)
    assert lambdas["lambda2"] == 0.0


def test_shrinkage_weight_multi_school_lambda2_can_be_nonzero():
    _, lambdas = shrinkage_weight(0, 0.0, 20, 0.5, 20, 0.5, 0.6, num_schools=2)
    assert lambdas["lambda2"] > 0.0


def test_shrinkage_weight_zero_everywhere_falls_back_to_prior():
    weight, lambdas = shrinkage_weight(0, 0.0, 0, 0.0, 0, 0.0, 0.7, num_schools=1)
    assert lambdas["lambda4"] == 1.0
    assert weight == 0.7


def _graph_with_one_case(physician: str, symptom: str, element: str, syndrome_code: str = "S001"):
    """搭一个最小合成图：symptom -indicates-> element -composes-> syndrome，
    外加一个这位医家的 case 节点，症状里含 symptom，evidences 指向这个 syndrome。
    用来验证 count_support 真的能在图结构齐全时数出 1 次支持。"""
    store = NetworkXStore()
    sym_id, elem_id, syn_id = f"symptom::{symptom}", f"element::{element}", f"syndrome::{syndrome_code}"
    store.add_node(sym_id, node_type="symptom", name=symptom)
    store.add_node(elem_id, node_type="element", name=element)
    store.add_node(syn_id, node_type="syndrome", name="某证", code=syndrome_code)
    store.add_edge(sym_id, elem_id, edge_type="indicates", source="gb_standard", is_cardinal=True)
    store.add_edge(elem_id, syn_id, edge_type="composes", source="gb_standard")

    case_id = f"case::{physician}::001"
    store.add_node(case_id, node_type="case", physician=physician, symptoms=[symptom])
    store.add_edge(case_id, syn_id, edge_type="evidences", source="case")
    return store, sym_id, elem_id, syn_id, case_id


def test_count_support_counts_matching_case():
    store, _sym_id, _elem_id, _syn_id, _case_id = _graph_with_one_case("ye_tianshi", "纳差", "脾")
    counts = count_support(store, "ye_tianshi")
    assert counts == {("纳差", "脾"): 1}


def test_count_support_ignores_other_physicians_cases():
    store, _sym_id, _elem_id, _syn_id, _case_id = _graph_with_one_case("ye_tianshi", "纳差", "脾")
    assert count_support(store, "wu_jutong") == {}


def test_count_support_ignores_case_symptom_without_indicates_edge():
    """case 记录的症状如果压根没有对应的 indicates 边（不在标准骨架里），
    不能凭空生成一条新的支持计数——K2 只重配已有边的权重，不新增边。"""
    store, sym_id, elem_id, syn_id, case_id = _graph_with_one_case("ye_tianshi", "纳差", "脾")
    store.g.nodes[case_id]["symptoms"] = ["从未定义的症状"]
    assert count_support(store, "ye_tianshi") == {}


def test_count_support_empty_graph_returns_empty_dict():
    store = NetworkXStore()
    assert count_support(store, "ye_tianshi") == {}


def test_apply_weights_writes_weight_and_lambda1_for_both_physicians():
    store, sym_id, elem_id, syn_id, _case_id = _graph_with_one_case("ye_tianshi", "纳差", "脾")
    apply_weights(store)
    edge_data = store.g.get_edge_data(sym_id, elem_id)["indicates"]
    assert set(edge_data["weight_by_physician"]) == {"ye_tianshi", "wu_jutong"}
    assert set(edge_data["lambda1_by_physician"]) == {"ye_tianshi", "wu_jutong"}


def test_apply_weights_no_case_data_falls_back_to_prior_for_everyone():
    """sandbox 里没有真实 case 节点时（当前实际状态），每条边的权重应该
    退化成标准先验本身——λ1 全 0，两位医家权重完全相同，都等于 w_prior。"""
    store = NetworkXStore()
    store.add_node("symptom::纳差", node_type="symptom", name="纳差")
    store.add_node("element::脾", node_type="element", name="脾")
    store.add_edge(
        "symptom::纳差", "element::脾", edge_type="indicates", source="gb_standard", is_cardinal=True
    )
    apply_weights(store)
    edge_data = store.g.get_edge_data("symptom::纳差", "element::脾")["indicates"]
    assert edge_data["lambda1_by_physician"]["ye_tianshi"] == 0.0
    assert edge_data["lambda1_by_physician"]["wu_jutong"] == 0.0
    assert edge_data["weight_by_physician"]["ye_tianshi"] == 1.0  # is_cardinal=True -> w_prior=1.0
    assert edge_data["weight_by_physician"]["wu_jutong"] == 1.0
