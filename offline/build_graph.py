"""从 data/standard/syndromes.jsonl 建知识图谱骨架。

第一批数据（13 条脾胃相关证候，见 data/standard/syndromes.jsonl）不是 GB/T
16751.2 原文——那份文件目前仍拿不到全文（网页版可在线读、不可批量下载）——而是
7 个独立、可核验、彼此不矛盾的来源交叉确认后录入的：WFCMS 国际标准、两篇同行评审
论文（各带 GB/T 15657 官方编码）、一份团体标准公示稿、医学教育网、39健康网、
百度百科。source 字段按可信度分六档如实标注（见 core.schemas.SyndromeDefinition
的文档字符串），没有一条标 "gb_standard"。

**不编造证候定义。** 这个项目的地基是"从真实来源忠实抽取"，图谱骨架尤其如此——
它是后面 K2 权重、K3 检索、G1-G3 智能体全部依赖的结构，编造的定义会让下游所有
"图谱证实了 XX"的结论都变成幻觉，比某一条案例数据出错严重得多。

结构来自标准，权重来自数据：这一步只建 symptom / element / syndrome 三类节点和
indicates / composes / is_a 三类边，全部标 source=定义本身的 source
（一般是 "gb_standard"）。therapy / formula / herb / case / physician 节点和
treated_by / realized_by / contains / evidences / practiced_by 边留给 K2 及以后，
它们的数据来源是医案和治法国标，不是这份 syndromes.jsonl。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.graph.store import NetworkXStore
from core.schemas import SyndromeDefinition

STANDARD_PATH = Path(__file__).resolve().parent.parent / "data" / "standard" / "syndromes.jsonl"
GRAPH_OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "graph.json"

DEFAULT_FILTER_KEYWORDS = ["脾", "胃", "肝", "肠", "中焦"]


def load_syndrome_definitions(path: Path = STANDARD_PATH) -> list[SyndromeDefinition]:
    if not path.exists():
        raise FileNotFoundError(
            f"未找到 {path}。图谱骨架的数据来自人工核对过的 GB/T 16751.2 脾胃相关子集"
            "（次选中医诊断学教材，最差情况退到人工最小骨架），这个文件需要先准备好——"
            "每行一个 SyndromeDefinition 的 JSON。不能用编造的证候定义代替：这个项目"
            "的防幻觉设计要求图谱骨架和医案数据一样，必须来自可核实的真实来源。"
        )
    defs = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                defs.append(SyndromeDefinition.model_validate_json(line))
            except Exception as e:  # noqa: BLE001 - 报出具体哪一行坏了，方便人工核对时定位
                raise ValueError(f"{path}:{lineno} 不是合法的 SyndromeDefinition：{e}") from e
    return defs


def filter_by_keywords(
    defs: list[SyndromeDefinition], keywords: list[str] | None
) -> list[SyndromeDefinition]:
    """按病位关键词缩小到脾胃相关子集（K1 覆盖范围：60-80 条叶节点 + 相关类目词，
    不做全部 2060 条）。keywords 为空/None 时不过滤。"""
    if not keywords:
        return defs
    return [
        d
        for d in defs
        if any(
            kw in d.name or kw in d.definition or kw in d.location
            for kw in keywords
        )
    ]


def build_graph(defs: list[SyndromeDefinition]) -> NetworkXStore:
    """把证候定义列表建成图。symptom<->element 的 indicates 边是从"同一条证候定义
    里症状和证素共同出现"这个结构关系里合理推出来的（国标本身没有给症状到证素的
    直接映射表，只给了"这个证候的主症/次症是什么"+"这个证候的病位/病性是什么"），
    是一个粗粒度但站得住脚的骨架级默认值——K2 会用医案的真实共现数据在它上面
    做加权，届时精度会远好于这里"同证候内全连接"的粗略处理。"""
    store = NetworkXStore()

    for d in defs:
        syn_id = f"syndrome::{d.code}"
        store.add_node(
            syn_id,
            node_type="syndrome",
            name=d.name,
            code=d.code,
            is_category=d.is_category,
            definition=d.definition,
            tongue_pulse=d.tongue_pulse,
        )

        if d.parent:
            store.add_edge(
                syn_id, f"syndrome::{d.parent}", edge_type="is_a", source=d.source
            )

        elements = [(loc, "location") for loc in d.location] + [
            (nat, "nature") for nat in d.nature
        ]
        for elem_name, category in elements:
            elem_id = f"element::{elem_name}"
            store.add_node(elem_id, node_type="element", name=elem_name, category=category)
            store.add_edge(elem_id, syn_id, edge_type="composes", source=d.source)

        symptoms = [(s, True) for s in d.cardinal_symptoms] + [
            (s, False) for s in d.secondary_symptoms
        ]
        for sym_name, is_cardinal in symptoms:
            sym_id = f"symptom::{sym_name}"
            store.add_node(sym_id, node_type="symptom", name=sym_name)
            for elem_name, _category in elements:
                elem_id = f"element::{elem_name}"
                store.add_edge(
                    sym_id,
                    elem_id,
                    edge_type="indicates",
                    source=d.source,
                    via_syndrome=d.code,
                    is_cardinal=is_cardinal,
                )

    return store


# 语料库（data/ye_tianshi、data/wu_jutong）的门类关键词，用来检查
# syndromes.jsonl 有没有覆盖到语料库实际涉及的病症范围。
CORPUS_GATE_KEYWORDS = [
    "肿胀", "痰饮", "木乘土", "噎膈反胃", "呕吐", "泄泻",
    "便血", "吐血", "痞", "胃痛", "积聚",
]


def check_corpus_coverage(
    defs: list[SyndromeDefinition], gate_keywords: list[str] = CORPUS_GATE_KEYWORDS
) -> dict:
    """字面子串匹配，不是语义匹配——故意保守：只查 location + cardinal_symptoms
    这两个"定义性"字段（strict），宁可多报"未覆盖"让人工核实，也不要因为模糊
    匹配漏报真正的空白。同时给一个 broad 版本（额外查 nature/secondary_symptoms/
    name/definition）作对照，用来区分"这门类是真空白"还是"只是措辞差异、其实
    在次症或病机描述里提到了"——两者都要报，不能只报一个让人误判。"""
    strict_hits: dict[str, list[str]] = {kw: [] for kw in gate_keywords}
    broad_hits: dict[str, list[str]] = {kw: [] for kw in gate_keywords}

    for d in defs:
        strict_text = "".join(d.location + d.cardinal_symptoms)
        broad_text = "".join(
            d.location + d.nature + d.cardinal_symptoms + d.secondary_symptoms
            + [d.name, d.definition]
        )
        for kw in gate_keywords:
            if kw in strict_text:
                strict_hits[kw].append(d.code)
            if kw in broad_text:
                broad_hits[kw].append(d.code)

    return {
        "strict_hits": {kw: v for kw, v in strict_hits.items() if v},
        "strict_uncovered": [kw for kw in gate_keywords if not strict_hits[kw]],
        "broad_uncovered": [kw for kw in gate_keywords if not broad_hits[kw]],
        "wording_gap_only": [
            kw for kw in gate_keywords if not strict_hits[kw] and broad_hits[kw]
        ],
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="从 syndromes.jsonl 建知识图谱骨架")
    parser.add_argument(
        "--filter-keywords",
        nargs="*",
        default=DEFAULT_FILTER_KEYWORDS,
        help="按病位关键词过滤，默认脾/胃/肝/肠/中焦；传空列表不过滤",
    )
    parser.add_argument("--standard-path", type=Path, default=STANDARD_PATH)
    parser.add_argument("--out", type=Path, default=GRAPH_OUT_PATH)
    args = parser.parse_args(argv)

    defs = load_syndrome_definitions(args.standard_path)
    filtered = filter_by_keywords(defs, args.filter_keywords)
    store = build_graph(filtered)
    store.save(args.out)

    print(f"读入 {len(defs)} 条证候定义，过滤后 {len(filtered)} 条")
    print(f"图谱节点数：{store.g.number_of_nodes()}  边数：{store.g.number_of_edges()}")

    node_type_counts: dict[str, int] = {}
    for _, data in store.g.nodes(data=True):
        node_type_counts[data.get("node_type")] = node_type_counts.get(data.get("node_type"), 0) + 1
    print(f"节点类型分布：{node_type_counts}")

    edge_type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for _, _, data in store.g.edges(data=True):
        edge_type_counts[data.get("edge_type")] = edge_type_counts.get(data.get("edge_type"), 0) + 1
        source_counts[data.get("source")] = source_counts.get(data.get("source"), 0) + 1
    print(f"边类型分布：{edge_type_counts}")
    print(f"边来源分布：{source_counts}")

    import networkx as nx

    is_a_edges = [
        (u, v) for u, v, d in store.g.edges(data=True) if d.get("edge_type") == "is_a"
    ]
    is_a_subgraph = nx.DiGraph(is_a_edges)
    print(f"is_a 子图无环：{nx.is_directed_acyclic_graph(is_a_subgraph)}")

    coverage = check_corpus_coverage(filtered)
    print("\n=== 语料库门类覆盖检查（对 syndromes.jsonl 的 location/cardinal_symptoms） ===")
    print(f"严格匹配命中：{coverage['strict_hits']}")
    print(f"严格未覆盖（location/cardinal_symptoms 都对不上）：{coverage['strict_uncovered']}")
    print(f"仅措辞差异（严格对不上，但在 nature/次症/病机描述里提到过，不是真空白）：{coverage['wording_gap_only']}")
    print(f"完全未覆盖（严格 + 宽泛都对不上）：{coverage['broad_uncovered']}")

    print(f"\n已写出 {args.out}")


if __name__ == "__main__":
    main()
