"""K2 收尾：把 core/graph/weights.py 的四层收缩权重实际写进图，打印图谱统计。

**λ2（学派层）警告——每次输出都必须带，不许省略：**
当前仅 1 个学派（2 位医家），λ2 学派层与医家层高度共线，其数值不构成独立信号，
等 A2 加入第二学派后需重新评估。

**λ1（医家层）恒为 0——两种成因，打印时按当前图的实际内容二选一（见 lambda1_note）：**
成因一，图里根本没有 case 节点（sandbox 里没有 cases.json 时就是这种）。
成因二，挂了 839 条真实 case 节点（offline/build_graph.py 的 attach_cases），
count_support() 仍返回空 dict——不是缺数据，是术语体系对不上：
839 条医案仅 91 条标注证型，这 91 条中 0 条能匹配国标证候名；症状端
1433 种表述与国标 93 个症状节点字面重合仅 8 种（0.6%）。因此 evidences
边为 0，无边可数，权重全部退化到标准先验层（λ4=1）。

这是清代医案与现代国标术语体系差异的客观结果，不是实现缺陷，也不会因为
"再跑一次抽取"而改变。要让 λ1 非零，需要建立古籍证型/症状到国标术语的
映射层——那是独立的工作量，不在 K2 范围内。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core.graph.store import NetworkXStore
from core.graph.weights import apply_weights
from core.physicians import PHYSICIANS, schools

GRAPH_PATH = Path(__file__).resolve().parent.parent / "data" / "graph.json"

LAMBDA2_MULTI_SCHOOL_NOTE = (
    "已有 2 个学派，λ2 不再被强制归零。但只要某个学派下只有一位医家，该学派层与那位"
    "医家层统计的仍是同一批数据，λ2 对他不构成独立信号；引用 λ2 前先标注每个学派的医家数"
)

LAMBDA2_WARNING = (
    "当前仅 1 个学派（2 位医家），λ2 学派层与医家层高度共线，"
    "其数值不构成独立信号，等 A2 加入第二学派后需重新评估"
)

# λ1=0 有两种完全不同的成因，说明文字必须跟着当前这张图的实际内容走。
# 原来只有一段文字、且无条件打印，在没挂医案的图上会输出「图中已挂入 839 条真实
# 医案节点」——跟紧挨着它打印的「节点类型分布里没有 case」直接矛盾。一段自相矛盾的
# 说明比没有说明更糟：读报告的人会拿它当"医案与国标对不上"的证据，而这张图里
# 根本没有医案可对。


def _no_case_nodes_note() -> str:
    return (
        "本次加载的图里没有 case 节点（cases.json 不存在或未挂入），"
        "count_support 无边可数，λ1 必然为 0。这不构成「医案与国标术语对不上」的"
        "证据——那个结论来自挂入医案之后的实测，本次运行没有复现它。"
        "要复现：先跑 offline/extract_cases.py 生成 cases.json，"
        "再跑 offline/build_graph.py 让 attach_cases 把医案挂进图。"
    )


def _case_nodes_unaligned_note(n_cases: int, n_evidences: int) -> str:
    return (
        f"λ1 为 0 的原因不是缺少 case 节点——图中已挂入 {n_cases} 条真实医案节点，"
        f"但 case->syndrome 的 evidences 边只有 {n_evidences} 条。"
        "真实原因是 case 无法与标准证候对齐：医案用「胃阳虚」「悬饮」「关格」等"
        "古籍用词，标准侧为「肝胃不和证」「脾胃湿热证」等，命名体系不同；"
        "症状端同样对不上（此前一次 839 条医案的实测：仅 91 条标注证型、其中 0 条"
        "匹配国标证候名，1433 种症状表述与国标 93 个症状节点字面重合仅 8 种）。"
        "evidences 边为 0 时 count_support 无边可数，权重全部退化到标准先验层。"
        "这是清代医案与现代国标术语体系差异的客观结果，不是实现缺陷。"
    )


def lambda1_note(stats: dict) -> str:
    n_cases = stats["node_type_counts"].get("case", 0)
    if n_cases == 0:
        return _no_case_nodes_note()
    return _case_nodes_unaligned_note(n_cases, stats["edge_type_counts"].get("evidences", 0))


def _count_by(items, key_fn) -> dict:
    counts: dict = {}
    for item in items:
        k = key_fn(item)
        counts[k] = counts.get(k, 0) + 1
    return counts


def _lambda1_histogram(lambda1_values: list[float], n_bins: int = 5) -> dict[str, int]:
    """[0,1] 等分 n_bins 档，闭右端点归到最后一档，避免 λ1=1.0 落不进任何区间。"""
    bins = {i: 0 for i in range(n_bins)}
    for v in lambda1_values:
        idx = min(int(v * n_bins), n_bins - 1)
        bins[idx] += 1
    width = 1.0 / n_bins
    return {
        f"[{i * width:.1f}, {(i + 1) * width:.1f}{']' if i == n_bins - 1 else ')'}": bins[i]
        for i in range(n_bins)
    }


def compute_stats(store: NetworkXStore) -> dict:
    g = store.g

    node_type_counts = _count_by(g.nodes(data=True), lambda item: item[1].get("node_type"))
    edge_type_counts = _count_by(g.edges(data=True), lambda item: item[2].get("edge_type"))
    edge_source_counts = _count_by(g.edges(data=True), lambda item: item[2].get("source"))

    isolated = [n for n in g.nodes() if g.degree(n) == 0]

    lambda1_by_physician: dict[str, list[float]] = {pid: [] for pid in PHYSICIANS}
    for _, _, data in g.edges(data=True):
        if data.get("edge_type") != "indicates":
            continue
        for pid, l1 in data.get("lambda1_by_physician", {}).items():
            lambda1_by_physician.setdefault(pid, []).append(l1)

    lambda1_histogram = {
        pid: _lambda1_histogram(values) for pid, values in lambda1_by_physician.items()
    }
    lambda1_gt_half = {
        pid: sum(1 for v in values if v > 0.5) for pid, values in lambda1_by_physician.items()
    }

    return {
        "node_type_counts": node_type_counts,
        "edge_type_counts": edge_type_counts,
        "edge_source_counts": edge_source_counts,
        "isolated_node_count": len(isolated),
        "num_schools": len(schools()),
        "lambda1_histogram": lambda1_histogram,
        "lambda1_gt_half_count": lambda1_gt_half,
        "indicates_edge_count": edge_type_counts.get("indicates", 0),
    }


def print_stats(stats: dict) -> None:
    print("=== 节点类型分布 ===")
    for k, v in sorted(stats["node_type_counts"].items(), key=lambda kv: str(kv[0])):
        print(f"  {k}: {v}")

    print("\n=== 边类型分布 ===")
    for k, v in sorted(stats["edge_type_counts"].items(), key=lambda kv: str(kv[0])):
        print(f"  {k}: {v}")

    print("\n=== 边来源分布 ===")
    for k, v in sorted(stats["edge_source_counts"].items(), key=lambda kv: str(kv[0])):
        print(f"  {k}: {v}")

    print(f"\n=== 孤立节点数（度为 0） ===\n  {stats['isolated_node_count']}")

    print(f"\n=== 当前学派数 ===\n  {stats['num_schools']}")
    if stats["num_schools"] <= 1:
        print(f"\n【警告】{LAMBDA2_WARNING}")
    else:
        # 第二学派已注册，λ2 第一次有真值——但仍不是可信信号：学派数只有 2、其中一个
        # 学派只有一位医家，学派层和医家层对那一位来说还是同一批数据。
        print(f"\n【警告】{LAMBDA2_MULTI_SCHOOL_NOTE}")

    print(f"\n=== λ1（医家层权重）分布，共 {stats['indicates_edge_count']} 条 indicates 边 ===")
    print(f"说明：{lambda1_note(stats)}")
    for pid, histogram in stats["lambda1_histogram"].items():
        name = PHYSICIANS.get(pid, {}).get("name", pid)
        print(f"  {name}（{pid}）：{histogram}")

    print("\n=== λ1>0.5 的边数（这位医家是否有足够病例证据支持"
          "'按医家条件化'这件事本身，而不是权重都退化到标准先验/其他层） ===")
    for pid, count in stats["lambda1_gt_half_count"].items():
        name = PHYSICIANS.get(pid, {}).get("name", pid)
        total = stats["indicates_edge_count"]
        print(f"  {name}（{pid}）：{count}/{total}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="K2 图谱统计：写权重 + 打印分布")
    parser.add_argument("--graph-path", type=Path, default=GRAPH_PATH)
    parser.add_argument(
        "--no-save", action="store_true",
        help="只打印统计，不把算出来的 weight_by_physician/lambda1_by_physician 写回图文件",
    )
    args = parser.parse_args(argv)

    if not args.graph_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.graph_path}，先跑 offline/build_graph.py 建图"
        )

    store = NetworkXStore()
    store.load(args.graph_path)

    apply_weights(store)

    stats = compute_stats(store)
    print_stats(stats)

    if not args.no_save:
        store.save(args.graph_path)
        print(f"\n已把 weight_by_physician/lambda1_by_physician 写回 {args.graph_path}")


if __name__ == "__main__":
    main()
