"""集合相似度的通用统计量：Jaccard 距离，以及它在重复采样上的均值/分位数。

**这是全项目里 Jaccard 距离公式唯一的实现**——CLAUDE.md「同一概念的匹配逻辑只能
有一处实现」。`offline/estimate_epsilon.py` 的三层噪声估算、`eval/run_eval.py`
的 E2（按医家配对分组的分歧度）都调用这里的 `jaccard_distance`，不要在任何一处
另写一套除法。

跟 `core/chain.py` 的 `divergence["herb_jaccard"]` 是两个不同的问题，不是重复：
divergence 回答"K 位医家这一次的结论有多不一致"（K 个集合的全局交集/并集，
K=2 时与两两距离等价，K>2 时是一个更严格的"全体共识"式度量，`chain.py` 里的
实现照旧不动）；这里的 `pairwise_jaccard_stats` 回答"同一个设定重复跑 N 次，
输出本身抖动多大"（N 次重复两两取平均），是两件事，只是共用同一个距离公式。
"""
from __future__ import annotations

from itertools import combinations


def jaccard_distance(a: set, b: set) -> float:
    """两个集合的 Jaccard 距离：0 = 完全相同，1 = 毫无重叠。

    两者都为空时定义为 0——"没有可比较的内容"不等于"完全不同"，
    等价于把"双方都没提到任何东西"当成一致，而不是当成最大分歧。
    """
    union = a | b
    if not union:
        return 0.0
    return 1.0 - len(a & b) / len(union)


def _percentile(sorted_values: list[float], q: float) -> float:
    """最近邻插值分位数。不引入 numpy/scipy：这个项目的 requirements.txt 里没有
    它们（SDT 评测的 pandas 是评测环境的隐式依赖，不是我们主动要的），
    为了算一个分位数不值得加一个新依赖。"""
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


def pairwise_jaccard_stats(sets: list[set]) -> dict | None:
    """N 个集合两两算 `jaccard_distance`，返回 {mean, p50, p95, n_pairs, values}。

    这是"重复采样噪声"的标准估计方式：N 次独立重复的输出应当彼此接近，
    两两距离的分布就是噪声的经验分布。跳过输入里的 None（某次重复失败/跳过
    时上游会传 None 占位，不是抛异常——见 estimate_epsilon.py 的处理）。

    有效集合少于 2 个时返回 None：少于两次重复，没有"两两"可言，估不出噪声。
    调用方要把 None 当成"数据不够"处理，不能当成"噪声为 0"。
    """
    valid = [s for s in sets if s is not None]
    if len(valid) < 2:
        return None
    dists = [jaccard_distance(a, b) for a, b in combinations(valid, 2)]
    dists_sorted = sorted(dists)
    return {
        "mean": round(sum(dists) / len(dists), 4),
        "p50": round(_percentile(dists_sorted, 0.50), 4),
        "p95": round(_percentile(dists_sorted, 0.95), 4),
        "n_pairs": len(dists),
        "values": [round(d, 4) for d in dists],
    }


def aggregate_stats(values: list[float]) -> dict | None:
    """多组 pairwise_jaccard_stats 的 mean 值再汇总一层（比如"10 条主诉各自的
    epsilon，汇总成一个总的 epsilon_online"）。值本身已经是统计量、不是原始距离，
    所以这里只做简单的 mean/p50/p95，不重新展开成两两对。"""
    if not values:
        return None
    sorted_values = sorted(values)
    return {
        "mean": round(sum(values) / len(values), 4),
        "p50": round(_percentile(sorted_values, 0.50), 4),
        "p95": round(_percentile(sorted_values, 0.95), 4),
        "n": len(values),
    }
