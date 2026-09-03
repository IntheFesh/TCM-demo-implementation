"""K2 收尾：把 core/graph/weights.py 的四层收缩权重实际写进图，打印图谱统计。

**λ2（学派层）警告——每次输出都必须带，不许省略：**
当前仅 1 个学派（2 位医家），λ2 学派层与医家层高度共线，其数值不构成独立信号，
等 A2 加入第二学派后需重新评估。

**这个 sandbox 里没有真实 cases.json：** offline/extract_cases.py 需要真实 LLM
调用（这里没有网络/API key，只有假 LLM 测试用的 mock），所以图里目前没有真实
node_type="case" 节点。core/graph/weights.py 的 count_support() 对任何医家都会
返回空 dict，λ1（医家层）恒为 0，下面打印的 λ1 分布、λ1>0.5 边数都是这个 sandbox
的真实计算结果，不是估计值——只是这批"真实结果"目前全部退化到标准先验层（λ4=1），
因为真的没有案例数据可数。等真实模型跑完 offline/extract_cases.py 产出 cases.json、
案例节点写入图之后，重跑这个脚本会自动开始出现非零的 λ1。
"""
from __future__ import annotations

import argparse
from pathlib import Path

from core.graph.store import NetworkXStore
from core.graph.weights import apply_weights
from core.physicians import PHYSICIANS, schools

GRAPH_PATH = Path(__file__).resolve().parent.parent / "data" / "graph.json"

LAMBDA2_WARNING = (
    "当前仅 1 个学派（2 位医家），λ2 学派层与医家层高度共线，"
    "其数值不构成独立信号，等 A2 加入第二学派后需重新评估"
)

NO_CASE_DATA_NOTE = (
    "本 sandbox 无真实 cases.json（offline/extract_cases.py 需要真实 LLM，"
    "这里没有网络/API key）。下面的 λ1 分布是真实计算结果，"
    "不是估计值——只是案例节点计数恒为 0，所以真实结果全部退化到标准先验层。"
)


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
    print(f"\n【警告】{LAMBDA2_WARNING}")

    print(f"\n=== λ1（医家层权重）分布，共 {stats['indicates_edge_count']} 条 indicates 边 ===")
    print(f"说明：{NO_CASE_DATA_NOTE}")
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
