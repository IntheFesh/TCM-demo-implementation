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
                    # key 里必须带 code：同一对 (症状, 证素) 会被多条证候定义各写
                    # 一次（「纳呆 提示 胃」SP-02/SP-03/SP-05 都写了），共用
                    # key="indicates" 的话后写的会盖掉先写的，via_syndrome 和
                    # is_cardinal 一起丢。
                    edge_key=f"indicates::{d.code}",
                    edge_type="indicates",
                    source=d.source,
                    via_syndrome=d.code,
                    is_cardinal=is_cardinal,
                )

    return store


# 语料库门类关键词，用来检查 syndromes.jsonl 有没有覆盖到语料库实际涉及的病症范围。
# **从 split_cases.BOOKS 的 gates 派生，不再手抄一份**：手抄的那份已经漂移过——含两本书
# 都没有的门类、缺吴鞠通的「滞下/噎/反胃」和张锡纯的全部门类，覆盖检查报的不是真实
# 语料范围。便血/吐血单独保留：它们对应 tests/queries.txt 里安全否决那条测试主诉。
def _corpus_gate_keywords() -> list[str]:
    from offline.split_cases import BOOKS

    seen: dict[str, None] = {}
    for cfg in BOOKS.values():
        for g in cfg["gates"]:
            seen.setdefault(g, None)
    for extra in ("便血", "吐血"):
        seen.setdefault(extra, None)
    return list(seen)


CORPUS_GATE_KEYWORDS = _corpus_gate_keywords()


def check_corpus_coverage(
    defs: list[SyndromeDefinition], gate_keywords: list[str] = CORPUS_GATE_KEYWORDS
) -> dict:
    """语义匹配，经由 core.syndrome_norm 的 SYNONYMS 表——不是字面子串匹配。
    第一版（R1 期间）直接拿 gate 关键词做字面子串匹配，把"水肿"/"木旺乘土"
    /"土虚木乘"/"大便溏稀"/"胃脘隐痛"这类古籍门类名和标准用语的等价写法全部
    判成未覆盖，报出一堆假阴性。现在两边都先过 SYNONYMS 归一化再比较概念，
    归一化逻辑只在 core/syndrome_norm.py 一处维护，这里不再重复一套字面规则。

    仍然保留 strict/broad 两档：strict 只查 location + cardinal_symptoms
    这两个"定义性"字段，broad 额外查 nature/secondary_symptoms/name/definition。
    两档都要报——用来区分"这门类是真空白"还是"只是在次症或病机描述里提到，
    没写进主症"，不能只报一个让人误判。"""
    from core.syndrome_norm import canonical, normalize

    canonical_keywords = [canonical(kw) for kw in gate_keywords]

    strict_hits: dict[str, list[str]] = {kw: [] for kw in gate_keywords}
    broad_hits: dict[str, list[str]] = {kw: [] for kw in gate_keywords}

    for d in defs:
        strict_text = "".join(d.location + d.cardinal_symptoms)
        broad_text = "".join(
            d.location + d.nature + d.cardinal_symptoms + d.secondary_symptoms
            + [d.name, d.definition]
        )
        strict_concepts = normalize(strict_text)
        broad_concepts = normalize(broad_text)
        for kw, canon in zip(gate_keywords, canonical_keywords):
            if canon in strict_concepts:
                strict_hits[kw].append(d.code)
            if canon in broad_concepts:
                broad_hits[kw].append(d.code)

    return {
        "strict_hits": {kw: v for kw, v in strict_hits.items() if v},
        "strict_uncovered": [kw for kw in gate_keywords if not strict_hits[kw]],
        "broad_uncovered": [kw for kw in gate_keywords if not broad_hits[kw]],
        "wording_gap_only": [
            kw for kw in gate_keywords if not strict_hits[kw] and broad_hits[kw]
        ],
    }


def attach_cases(store: NetworkXStore, cases_path: Path) -> dict:
    """把 cases.json 里的医案作为 case 节点挂进图。

    这是 K2 的另一半——K1 只建了"标准 -> 骨架"（symptom/element/syndrome），
    医案这条管道当时因为 sandbox 没有真实 cases.json 而没写。

    重要：case 节点的价值主要不在 count_support/λ1。实测清代医案的症状表述
    与国标术语字面重合率极低（1433 种表述 vs 93 个标准症状节点，仅 8 种一致，
    且都不是高频词），证型体系同样几乎不相交（839 条医案仅 91 条有证型，
    且多为"胃阳虚""悬饮""关格"这类古籍用词）。所以 evidences 边能建的很少，
    λ1 预期接近 0，这是数据的客观性质，不是代码缺陷。

    case 节点真正要服务的是：检索语料（K3）、ReAct 的 search_cases 工具（G2）、
    前端证据链侧栏（F1）——这些只需要 case 节点存在并按 physician 可查，
    不要求它跟标准证候对齐。
    """
    with cases_path.open("r", encoding="utf-8") as f:
        cases = json.load(f)

    syn_name_to_id = {
        data["name"]: node_id
        for node_id, data in store.g.nodes(data=True)
        if data.get("node_type") == "syndrome"
    }

    stats = {"cases": 0, "evidences_edges": 0, "syndrome_matched": 0, "syndrome_unmatched": 0}
    for c in cases:
        case_id = f"case::{c['case_id']}"
        store.add_node(
            case_id,
            node_type="case",
            physician=c["physician"],
            symptoms=c.get("symptoms") or [],
            syndrome=c.get("syndrome"),
            case_group_id=c.get("case_group_id"),
            visit_index=c.get("visit_index"),
        )
        stats["cases"] += 1

        syn = c.get("syndrome")
        if syn:
            target = syn_name_to_id.get(syn)
            if target:
                store.add_edge(case_id, target, edge_type="evidences", source="case")
                stats["evidences_edges"] += 1
                stats["syndrome_matched"] += 1
            else:
                stats["syndrome_unmatched"] += 1
    return stats


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
    parser.add_argument(
        "--cases-path", type=Path, default=Path("cases.json"),
        help="医案 cases.json 路径；存在则挂入 case 节点，传 --cases-path /dev/null 可跳过",
    )
    args = parser.parse_args(argv)

    defs = load_syndrome_definitions(args.standard_path)
    filtered = filter_by_keywords(defs, args.filter_keywords)
    store = build_graph(filtered)

    # 空文件也算"没有医案"：--help 里写着 `--cases-path /dev/null` 可以跳过挂医案，
    # 但 /dev/null 是存在的，只判 exists() 会走进 attach_cases 然后在 json.load
    # 上崩掉——文档里给的用法直接跑不通。
    if args.cases_path and args.cases_path.is_file() and args.cases_path.stat().st_size > 0:
        cstats = attach_cases(store, args.cases_path)
        print(
            f"挂入医案：{cstats['cases']} 条 case 节点，"
            f"evidences 边 {cstats['evidences_edges']} 条"
            f"（证型可对齐 {cstats['syndrome_matched']}，"
            f"对不齐 {cstats['syndrome_unmatched']}）"
        )
    else:
        print(f"未挂入医案（{args.cases_path} 不存在或为空），图中无 case 节点，λ1 将全为 0")

    # X3 的 data/case_triples.jsonl 不挂进这张图：它的消费方是 core/tools.py 的
    # query_case_graph()，直接读 jsonl 做子串匹配，不经过 NetworkXStore——见
    # offline/extract_case_triples.py 模块文档字符串。两条管道各自独立。

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
