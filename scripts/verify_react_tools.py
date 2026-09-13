"""P1 ReAct 修复验证：取证清单改成以医案层为主之后，工具调用分布真的
偏过来了没有。

沙盒（本次改代码的环境）没有 cases.json/embedding 模型/网络，跑不了真实
consult()——这个脚本是留给有真实语料和真实 LLM 的机器（AutoDL）跑的，跟
scripts/verify_hybrid_fusion.py 同一个理由、同一个形状：写好参数化，不用
每次现写 python -c 去抓 trace（这轮诊断就是这么抓的，很不方便）。

用法（默认跑 tests/queries.txt 的前 4 条，跟诊断报告用的是同一批数据）：
    python scripts/verify_react_tools.py

也可以换参数：
    python scripts/verify_react_tools.py --limit 10

退出码：医案层工具（search_cases + query_case_graph）占全部动作次数
（含 finish）的比例 > 35% -> 0；否则 -> 1（能直接接进 CI 或 shell 里的
`&&` 判断）。35% 这个数不是拍脑袋定的：是本次诊断的实测起点（12%）和
终点目标（README/CLAUDE.md 里没有单独定过这个数）之间，选一个明显跳出
"国标层占大头"这个失败模式的门槛——真调好的话医案层应该是取证清单里
"必查一次"的第一项，实测占比理应远高于 35%。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_QUERIES_PATH = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
# 诊断报告用的是"4 条主诉 × 3 医家 = 12 个样本、60 次调用"，默认值跟它对齐，
# 方便直接复现同一份诊断；换更大的样本量只需要 --limit。
DEFAULT_LIMIT = 4

CASE_LAYER_TOOLS = {"search_cases", "query_case_graph"}
CASE_LAYER_RATIO_GATE = 0.35
TRACE_SAMPLE_COUNT = 3


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P1 ReAct 修复验证：工具调用分布里医案层占比是否真的上去了"
    )
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH,
                     help="一行一条主诉的文本文件")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="只取前几条主诉")
    args = ap.parse_args(argv)

    from eval.run_eval import collect_react_process_samples, react_process_summary

    queries = [
        line.strip() for line in args.queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit is not None:
        queries = queries[: args.limit]

    print(f"主诉数：{len(queries)}（来自 {args.queries_path}）")
    records = collect_react_process_samples(queries)
    if not records:
        print("没有可用样本（全部被拦截/信息不足，或本次没有跑出任何结果）。", file=sys.stderr)
        return 1

    summary = react_process_summary(records)
    n_physician_samples = summary["n_samples"]
    print(f"(主诉,医家) 样本数：{n_physician_samples}")
    print()

    all_actions = [s.action for r in records for s in r["steps"]]
    total_calls = len(all_actions)
    action_counts = Counter(all_actions)

    print(f"工具调用分布（共 {total_calls} 次）：")
    for action, count in action_counts.most_common():
        ratio = count / total_calls if total_calls else 0.0
        print(f"  {action:<20} {count:>4} 次  {ratio:.1%}")
    print()

    print(f"terminated_by 分布：{summary['terminated_by_distribution']}")
    print()

    case_layer_calls = sum(action_counts.get(t, 0) for t in CASE_LAYER_TOOLS)
    case_layer_ratio = case_layer_calls / total_calls if total_calls else 0.0
    print(f"医案层工具（{sorted(CASE_LAYER_TOOLS)}）占比："
          f"{case_layer_calls}/{total_calls} = {case_layer_ratio:.1%}"
          f"（闸门 > {CASE_LAYER_RATIO_GATE:.0%}）")
    print()

    print(f"前 {TRACE_SAMPLE_COUNT} 条完整 trace：")
    for i, r in enumerate(records[:TRACE_SAMPLE_COUNT], start=1):
        print(f"--- trace {i}：主诉「{r['query']}」/ {r['physician']} / "
              f"terminated_by={r['terminated_by']} ---")
        for s in r["steps"]:
            print(f"  [{s.step}] {s.action}({s.action_input})")
            print(f"      thought: {s.thought}")
            print(f"      observation: {s.observation}")
        print()

    print("=" * 60)
    hit = case_layer_ratio > CASE_LAYER_RATIO_GATE
    if hit:
        print(f"★命中：医案层占比 {case_layer_ratio:.1%} > {CASE_LAYER_RATIO_GATE:.0%}，"
              "取证清单改成以医案层为主的修复生效。")
    else:
        print(f"✗ 未命中：医案层占比 {case_layer_ratio:.1%} 没有超过 "
              f"{CASE_LAYER_RATIO_GATE:.0%} 这道闸门。", file=sys.stderr)
    return 0 if hit else 1


if __name__ == "__main__":
    sys.exit(main())
