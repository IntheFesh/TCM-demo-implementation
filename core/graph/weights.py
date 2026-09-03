"""K2：给 indicates（症状->证素）边配医家级权重。

结构（节点、边、is_a/composes/indicates 骨架）来自标准，K2 不改结构、不新增
不存在的 indicates 边——只在已有骨架边上，用真实医案数据算出"这位医家的病例
实际支持这条边的程度"，写成 weight_by_physician。这是 offline/build_graph.py
文档字符串里"结构来自标准，权重来自数据"这句话的落地。

四层收缩：医家层 -> 学派层 -> 全局层 -> 标准先验层。每层用 n/(n+K) 决定
"这层真实证据够不够多、该信多少"；信不过的部分收缩到下一层，标准先验
（来自 syndromes.jsonl 的 is_cardinal/is_secondary，不依赖任何医案数据）
兜底，保证任何数据量下权重都有定义、不会是 NaN。

当前只有 1 个学派（温病，叶天士+吴鞠通）：学派层统计的病例集合跟全局层
完全重合，拆成两个"独立"数字只会制造一个假信号。所以 num_schools<=1 时
λ2 强制为 0、份额并入全局层——不是把学派层直接删掉，是让代码在
num_schools 变成 2（A2 加入第二学派）之后自动切回常规四层公式，不用改
调用方。这个分支现在没有真实多学派数据可测，只能靠 tests/test_weights.py
里的合成数据验证数学本身站得住。
"""
from __future__ import annotations

from core.graph.store import NetworkXStore
from core.physicians import PHYSICIANS, schools

DEFAULT_K = 5


def count_support(store: NetworkXStore, physician: str) -> dict[tuple[str, str], int]:
    """统计这位医家的医案证据里，每条 (症状, 证素) indicates 边被支持了几次。

    只数图里已经存在的 indicates 边——case 节点的症状要落在某条已有边的
    症状端、且该 case 通过 evidences 边指向的证候要落在这条边的证素端
    （经 composes 边），两边都对上才计一次。不会凭空生成标准骨架里没有
    的边，只给已有边配真实使用频率。

    这个函数依赖图里存在 node_type="case"（带 physician 属性）的节点和
    case -> syndrome 的 evidences 边——K1 只从 syndromes.jsonl 建了
    symptom/element/syndrome 三类节点，医案怎么写进图（case/physician
    节点、evidences 边）是 K2 的另一半、这个 sandbox 里还没有真实
    cases.json（offline/extract_cases.py 需要真实 LLM，这里没有网络/key）。
    所以现在对任何 physician 调用这个函数都会返回空 dict——不是逻辑错了，
    是没有真实 case 节点可数，见 offline/graph_stats.py 里对这一点的
    显式警告。
    """
    counts: dict[tuple[str, str], int] = {}
    g = store.g

    element_ids = store.find_nodes("element")
    syndrome_elements: dict[str, list[str]] = {}
    for elem_id in element_ids:
        for dst, attrs in store.neighbors(elem_id, edge_type="composes"):
            syndrome_elements.setdefault(dst, []).append(elem_id)

    for case_id in store.find_nodes("case", physician=physician):
        case_symptoms = g.nodes[case_id].get("symptoms", [])
        case_symptom_ids = {f"symptom::{s}" for s in case_symptoms}

        for syndrome_id, _evidence_attrs in store.neighbors(case_id, edge_type="evidences"):
            for elem_id in syndrome_elements.get(syndrome_id, []):
                for sym_id in case_symptom_ids:
                    has_indicates_edge = any(
                        dst == elem_id for dst, _ in store.neighbors(sym_id, edge_type="indicates")
                    )
                    if not has_indicates_edge:
                        continue
                    sym_name = g.nodes[sym_id]["name"] if sym_id in g.nodes else sym_id
                    elem_name = g.nodes[elem_id]["name"] if elem_id in g.nodes else elem_id
                    key = (sym_name, elem_name)
                    counts[key] = counts.get(key, 0) + 1

    return counts


def shrinkage_weight(
    n_d: int,
    w_d: float,
    n_school: int,
    w_school: float,
    n_global: int,
    w_global: float,
    w_prior: float,
    *,
    num_schools: int,
    K: int = DEFAULT_K,
) -> tuple[float, dict[str, float]]:
    """四层收缩组合。返回 (最终权重, {lambda1..lambda4})，四个 λ 恒和为 1
    （K>0 时；K=0 且 n=0 会除零，调用方不应传 K=0）。

    num_schools<=1 时学派层与医家层统计的是同一批数据，λ2 强制为 0、
    份额并入全局层（见模块文档字符串）；num_schools>=2 时走常规四层：
    λ1 = n_d/(n_d+K)，剩余按同样的 n/(n+K) 逻辑继续往下收缩，最后收缩不完
    的部分给标准先验层（λ4），不额外做 n/(n+K)——标准先验永远"数据充分"
    （它就是标准本身，不依赖案例数量）。
    """
    lambda1 = n_d / (n_d + K)
    remaining = 1 - lambda1

    if num_schools <= 1:
        lambda2 = 0.0
        lambda3 = remaining * (n_global / (n_global + K))
        lambda4 = remaining - lambda3
    else:
        lambda2 = remaining * (n_school / (n_school + K))
        remaining_after_school = remaining - lambda2
        lambda3 = remaining_after_school * (n_global / (n_global + K))
        lambda4 = remaining_after_school - lambda3

    weight = lambda1 * w_d + lambda2 * w_school + lambda3 * w_global + lambda4 * w_prior
    lambdas = {"lambda1": lambda1, "lambda2": lambda2, "lambda3": lambda3, "lambda4": lambda4}
    return weight, lambdas


def _conditional_weight(pair_count: int, symptom_total: int) -> float:
    """P(证素 | 症状, 这一层的数据) 的经验估计。分母为 0（这一层对这个症状
    完全没有数据）时定义为 0.0，不是 NaN——shrinkage_weight 里这一层的 λ
    也会因为 n=0 趋近 0，这个 0.0 的具体取值不会实际影响最终权重。"""
    if symptom_total <= 0:
        return 0.0
    return pair_count / symptom_total


def apply_weights(store: NetworkXStore, K: int = DEFAULT_K) -> None:
    """给图里所有 indicates 边写 weight_by_physician / lambda1_by_physician
    （λ1 本身兼作前端展示的"这条边对这位医家有多少真实病例支持"置信度信号）。

    school_map: 学派 -> 医家 id 列表，来自 core.physicians.schools()——不在
    这里重复写医家分组逻辑。
    """
    school_map = schools()
    num_schools = len(school_map)
    physician_of: dict[str, str] = {}
    for school, pids in school_map.items():
        for pid in pids:
            physician_of[pid] = school

    counts_by_physician = {pid: count_support(store, pid) for pid in PHYSICIANS}

    totals_by_physician: dict[str, dict[str, int]] = {}
    for pid, counts in counts_by_physician.items():
        totals: dict[str, int] = {}
        for (sym, _elem), n in counts.items():
            totals[sym] = totals.get(sym, 0) + n
        totals_by_physician[pid] = totals

    global_counts: dict[tuple[str, str], int] = {}
    global_totals: dict[str, int] = {}
    for counts in counts_by_physician.values():
        for key, n in counts.items():
            global_counts[key] = global_counts.get(key, 0) + n
    for (sym, _elem), n in global_counts.items():
        global_totals[sym] = global_totals.get(sym, 0) + n

    school_counts: dict[str, dict[tuple[str, str], int]] = {s: {} for s in school_map}
    school_totals: dict[str, dict[str, int]] = {s: {} for s in school_map}
    for pid, counts in counts_by_physician.items():
        school = physician_of[pid]
        for key, n in counts.items():
            school_counts[school][key] = school_counts[school].get(key, 0) + n
    for school in school_map:
        for (sym, _elem), n in school_counts[school].items():
            school_totals[school][sym] = school_totals[school].get(sym, 0) + n

    g = store.g
    for u, v, key, data in list(g.edges(keys=True, data=True)):
        if data.get("edge_type") != "indicates":
            continue
        sym_name = g.nodes[u].get("name", u)
        elem_name = g.nodes[v].get("name", v)
        pair = (sym_name, elem_name)
        w_prior = 1.0 if data.get("is_cardinal") else 0.5

        weight_by_physician: dict[str, float] = {}
        lambda1_by_physician: dict[str, float] = {}

        for pid in PHYSICIANS:
            school = physician_of[pid]
            n_d = counts_by_physician[pid].get(pair, 0)
            w_d = _conditional_weight(n_d, totals_by_physician[pid].get(sym_name, 0))

            n_school = school_counts[school].get(pair, 0)
            w_school = _conditional_weight(n_school, school_totals[school].get(sym_name, 0))

            n_global = global_counts.get(pair, 0)
            w_global = _conditional_weight(n_global, global_totals.get(sym_name, 0))

            weight, lambdas = shrinkage_weight(
                n_d, w_d, n_school, w_school, n_global, w_global, w_prior,
                num_schools=num_schools, K=K,
            )
            weight_by_physician[pid] = weight
            lambda1_by_physician[pid] = lambdas["lambda1"]

        g.edges[u, v, key]["weight_by_physician"] = weight_by_physician
        g.edges[u, v, key]["lambda1_by_physician"] = lambda1_by_physician
