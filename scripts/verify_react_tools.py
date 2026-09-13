"""P1 ReAct 修复验证：取证清单改成以医案层为主之后，工具调用分布真的
偏过来了没有——而且医案层工具真的拿到数据了没有。

沙盒（本次改代码的环境）没有 cases.json/embedding 模型/网络，跑不了真实
consult()——这个脚本是留给有真实语料和真实 LLM 的机器（AutoDL）跑的，跟
scripts/verify_hybrid_fusion.py 同一个理由、同一个形状：写好参数化，不用
每次现写 python -c 去抓 trace（这轮诊断就是这么抓的，很不方便）。

用法（默认跑 tests/queries.txt 的前 4 条，跟诊断报告用的是同一批数据）：
    python scripts/verify_react_tools.py

也可以换参数：
    python scripts/verify_react_tools.py --limit 10

退出码：**两道闸门都过** -> 0；否则 -> 1（能直接接进 CI 或 shell 里的 `&&`
判断）：
  1. 医案层工具（search_cases + query_case_graph）占全部动作次数（含
     finish）的比例 > 35%
  2. 医案层工具的返回非空率 > 50%

第二道是 1.1b 加的：上一轮只看调用次数，55% 的占比过了闸门，但真实 trace
里 9 次医案层调用全部返回空（physician 参数填的是中文名，医案库按 id 存，
永远匹配不到）——占比高但全返回空，等于模型白花了一半步数。两个数各自的
起点/目标：占比 12% -> 55%（上一轮实测），非空率 0/9 -> 目标 > 50%。
"""
from __future__ import annotations

import argparse
import json
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
CASE_LAYER_NONEMPTY_RATE_GATE = 0.50
TRACE_SAMPLE_COUNT = 3


def _case_layer_returned_data(action: str, observation: str) -> bool | None:
    """这一步医案层工具有没有真的返回数据。None = 不是医案层工具。

    observation 是 core/react.py 把工具返回值 json.dumps 之后的文本，超过
    MAX_OBSERVATION_CHARS 会被截断成非法 JSON——截断只发生在返回很长的时候，
    而返回很长恰恰说明有数据；那种情况按"文本里有没有 case_id 字段"判，
    不按能不能解析判。纯函数，单独可测。"""
    if action not in CASE_LAYER_TOOLS:
        return None
    try:
        payload = json.loads(observation)
    except (json.JSONDecodeError, TypeError):
        return '"case_id"' in (observation or "")
    if not isinstance(payload, dict):
        return False
    key = "cases" if action == "search_cases" else "triples"
    return bool(payload.get(key))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P1 ReAct 修复验证：医案层占比和医案层返回非空率是否都过闸门"
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

    all_steps = [s for r in records for s in r["steps"]]
    all_actions = [s.action for s in all_steps]
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

    nonempty_by_tool: dict[str, int] = {t: 0 for t in sorted(CASE_LAYER_TOOLS)}
    calls_by_tool: dict[str, int] = {t: 0 for t in sorted(CASE_LAYER_TOOLS)}
    for s in all_steps:
        returned = _case_layer_returned_data(s.action, s.observation)
        if returned is None:
            continue
        calls_by_tool[s.action] += 1
        if returned:
            nonempty_by_tool[s.action] += 1
    total_nonempty = sum(nonempty_by_tool.values())
    nonempty_rate = total_nonempty / case_layer_calls if case_layer_calls else 0.0
    print("医案层工具的返回情况：")
    for tool in sorted(CASE_LAYER_TOOLS):
        n, m = calls_by_tool[tool], nonempty_by_tool[tool]
        print(f"  {tool:<20} 调用 {n} 次，其中返回非空 {m} 次（{m}/{n}）")
    print(f"  合计返回非空率：{total_nonempty}/{case_layer_calls} = {nonempty_rate:.1%}"
          f"（闸门 > {CASE_LAYER_NONEMPTY_RATE_GATE:.0%}）")
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
    ratio_ok = case_layer_ratio > CASE_LAYER_RATIO_GATE
    nonempty_ok = nonempty_rate > CASE_LAYER_NONEMPTY_RATE_GATE
    if ratio_ok and nonempty_ok:
        print(f"★命中：医案层占比 {case_layer_ratio:.1%} > {CASE_LAYER_RATIO_GATE:.0%}，"
              f"且返回非空率 {nonempty_rate:.1%} > {CASE_LAYER_NONEMPTY_RATE_GATE:.0%}，"
              "两道闸门都过。")
        return 0
    failed = []
    if not ratio_ok:
        failed.append(f"医案层占比 {case_layer_ratio:.1%} 没有超过 {CASE_LAYER_RATIO_GATE:.0%}")
    if not nonempty_ok:
        failed.append(f"医案层返回非空率 {nonempty_rate:.1%} 没有超过 "
                      f"{CASE_LAYER_NONEMPTY_RATE_GATE:.0%}（占比高但全返回空 = 白花步数）")
    print("✗ 未命中：" + "；".join(failed) + "。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
