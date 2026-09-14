"""R1-1：`herb_items.role` 在真实 LLM 产出里到底填了多少——**R1-2 分层指标的
前置条件**。

沙盒（改代码的这台机器）没有真实 LLM/语料，测不了这个，所以做成脚本 + 退出码，
跟 scripts/verify_react_tools.py / verify_hybrid_fusion.py 同一个形状。

    python -m scripts.verify_role_fill                 # 默认 tests/queries.txt 前 4 条
    python -m scripts.verify_role_fill --limit 10

退出码：0 = role 填充率 >= 90%；1 = 低于闸门；2 = 没测出来（全部调用失败 /
全被安全否决拦截，连闸门都判不了——这跟"测了没过"是两件事，不要混成同一个码）。

## 为什么这个率是分层指标的前置条件

R1-2 的 core_jaccard（君臣）/ adjunct_jaccard（佐使）全靠 `role` 分层。没标
`role` 的药既不进君臣层也不进佐使层（只留在 herb_jaccard 里），所以填充率就是
"分层指标覆盖了多少用药"这件事本身：填充率 50% 的话，那两个数只是在拿一半的药
说话，**不能当成核心/加减的全貌去读，更不能拿来判断 R1-3 的克制约束有没有效**。
闸门没过时先改 prompt 把 role 填上，再回来看分层的数——顺序反了会拿一个只覆盖
半数用药的指标去验收另一个改动。

顺带报 `function_in_formula` 和 `dose` 的填充率：这两个字段是 M1 加的，到今天
一次都没在真实 DeepSeek 产出上核过（M1 的"填得挺完整"来自一次调用的印象，不是
统计）。它们不参与任何闸门，只是把"这几个字段到底是不是摆设"这件事测出来。

## 只数 selected 那个候选方，不数全部候选方

分层 ε 读的就是 `s3.selected_herb_items`（`formula_candidates[selected]`），
其它候选方的 role 填得再好也不会进那个指标。闸门要盯的是指标真正读的那份数据，
所以这里的分母跟它对齐，不是"所有候选方的所有药"。

西药条目（阿斯匹林等）从三个填充率里一并剔掉：分层指标本来就不把西药算进任何
一层（core/herbs.py 的理由：只有张锡纯用西药，算进去会把跨学派分歧系统性推高），
分母不一致的话这几个率就跟它要验证的那个指标不是在说同一批药。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_QUERIES_PATH = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
# 跟 scripts/verify_react_tools.py 默认值一致：同一批 4 条主诉，两个脚本的
# 数字可以互相对照（queries.txt 第 10 条是黑便，会被安全否决拦掉，默认取前 4
# 条正好避开它）。
DEFAULT_LIMIT = 4
# R1-2 的分层指标要用这份数据说话，覆盖率低于九成就不能当全貌读。
ROLE_FILL_GATE = 0.90


def collect_role_fill_samples(queries: list[str], consult_fn=None) -> dict:
    """跑 consult()，按 (主诉, 医家) 收集 herb_items 的字段填充情况。

    `use_react=False, ask_fn=None` 跟 offline/estimate_epsilon.py 的
    estimate_epsilon_online 显式固定成同一条最基础路径：这里量的是 prompt 让
    模型填了多少字段，ReAct/追问会带进额外变量，混进来就说不清是哪一边的影响。

    单条主诉调用失败不中断整批（真实 API 会抖动/限流），失败计数单独报——跟
    offline/estimate_epsilon.py 同一个模式，分类复用 core/batch.py。
    """
    from core.batch import classify_llm_failure
    from core.chain import consult
    from core.herbs import is_western_drug, role_partitioned_herb_sets

    consult_fn = consult_fn or (lambda c: consult(c, use_react=False, ask_fn=None))

    samples: list[dict] = []
    n_failed = 0
    n_rejected = 0
    n_insufficient = 0
    for complaint in queries:
        try:
            outcome = consult_fn(complaint)
        except Exception as e:  # noqa: BLE001 - 一条主诉失败不能拖累整批
            n_failed += 1
            print(f"[verify_role_fill]「{complaint}」调用失败："
                  f"{classify_llm_failure(e)}: {e}", file=sys.stderr)
            continue
        if outcome["rejected"]:
            n_rejected += 1
            continue
        if outcome.get("insufficient"):
            n_insufficient += 1
            continue
        for r in outcome["results"]:
            s3 = r["s3"]
            items = [i for i in s3.selected_herb_items if not is_western_drug(i.name)]
            parts = role_partitioned_herb_sets(s3.selected_herb_items)
            samples.append({
                "query": complaint,
                "physician": r["physician"],
                "formula": s3.formula,
                "n_items": len(items),
                "n_role": sum(1 for i in items if i.role),
                "n_function": sum(1 for i in items if i.function_in_formula),
                "n_dose": sum(1 for i in items if i.dose is not None),
                # 分层的两个集合大小：R1-3 的"平均药味数"对照就是这三个数的均值，
                # 少了它，"ε 降了"分不清是真稳定还是药开少了碰巧一样
                "n_total_set": len(parts["core"]) + len(parts["adjunct"]) + parts["n_unroled"],
                "n_core_set": len(parts["core"]),
                "n_adjunct_set": len(parts["adjunct"]),
                "roles": [i.role for i in items],
            })
    return {
        "samples": samples,
        "n_failed": n_failed,
        "n_rejected": n_rejected,
        "n_insufficient": n_insufficient,
        "n_queries": len(queries),
    }


def _rate(num: int, den: int) -> float | None:
    """填充率。分母为 0 返回 None 不是 0.0——"一味药都没有"跟"一味都没填"
    是两件事（跟 core/chain.py 分层 Jaccard 的空层返 None 同一条理由）。"""
    return (num / den) if den else None


def summarize(samples: list[dict]) -> dict:
    """按医家 + 总计汇总三个字段的填充率和平均药味数。纯函数，可单测。"""
    fields = ("role", "function", "dose")

    def _agg(rows: list[dict]) -> dict:
        den = sum(r["n_items"] for r in rows)
        out = {f"{f}_fill": _rate(sum(r[f"n_{f}"] for r in rows), den) for f in fields}
        out["n_items"] = den
        out["n_samples"] = len(rows)
        for key, label in (("n_total_set", "mean_herbs"), ("n_core_set", "mean_core"),
                           ("n_adjunct_set", "mean_adjunct")):
            vals = [r[key] for r in rows]
            out[label] = round(sum(vals) / len(vals), 2) if vals else None
        return out

    by_physician = {}
    for r in samples:
        by_physician.setdefault(r["physician"], []).append(r)
    return {
        "overall": _agg(samples),
        "by_physician": {p: _agg(rows) for p, rows in sorted(by_physician.items())},
    }


def _fmt(rate: float | None) -> str:
    return "不适用（没有药材条目）" if rate is None else f"{rate:.1%}"


def _print_table(summary: dict) -> None:
    header = f"{'医家':<14}{'样本':>4}{'药味':>5}{'role':>9}{'function':>10}{'dose':>9}" \
             f"{'均药味':>8}{'均君臣':>8}{'均佐使':>8}"
    print(header)
    print("-" * 80)
    rows = list(summary["by_physician"].items()) + [("合计", summary["overall"])]
    for name, agg in rows:
        print(f"{name:<14}{agg['n_samples']:>4}{agg['n_items']:>5}"
              f"{_fmt(agg['role_fill']):>9}{_fmt(agg['function_fill']):>10}"
              f"{_fmt(agg['dose_fill']):>9}"
              f"{str(agg['mean_herbs']):>8}{str(agg['mean_core']):>8}"
              f"{str(agg['mean_adjunct']):>8}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="R1-1：herb_items.role 的真实填充率（R1-2 分层指标的前置闸门）"
    )
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH,
                    help="一行一条主诉的文本文件")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="只取前几条主诉")
    args = ap.parse_args(argv)

    queries = [
        line.strip() for line in args.queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit is not None:
        queries = queries[: args.limit]

    print(f"主诉数：{len(queries)}（来自 {args.queries_path}）")
    print(f"闸门：role 填充率 >= {ROLE_FILL_GATE:.0%}（只数 selected 候选方、不含西药条目）")
    print()

    collected = collect_role_fill_samples(queries)
    samples = collected["samples"]
    print(f"(主诉,医家) 样本数：{len(samples)}"
          f"（{collected['n_failed']} 条主诉调用失败、"
          f"{collected['n_rejected']} 条被安全否决、"
          f"{collected['n_insufficient']} 条信息不足）")
    print()

    if not samples:
        print("没有任何可用样本，role 填充率无法测定——这不是「闸门没过」，是「没测出来」，"
              "所以退出码是 2。先看上面的失败原因。", file=sys.stderr)
        return 2

    summary = summarize(samples)
    _print_table(summary)

    print("每张方的 role 序列（看的是「填了哪些」，不是「填得对不对」——对不对要人看）：")
    for r in samples:
        roles = "、".join(x or "∅" for x in r["roles"]) or "（无药材条目）"
        print(f"  {r['physician']:<14}{str(r['formula']):<16}{roles}")
    print()

    role_fill = summary["overall"]["role_fill"]
    print("=" * 78)
    if role_fill is not None and role_fill >= ROLE_FILL_GATE:
        print(f"★命中：role 填充率 {role_fill:.1%} >= {ROLE_FILL_GATE:.0%}，"
              "R1-2 的分层指标（core_jaccard / adjunct_jaccard）可以照这份数据读。")
        return 0
    print(f"✗ 未命中：role 填充率 {_fmt(role_fill)} 低于闸门 {ROLE_FILL_GATE:.0%}。",
          file=sys.stderr)
    print("**R1-2 的分层指标要等 prompt 修好再用**：没标 role 的药不进君臣层也不进"
          "佐使层，这个率不到九成的话 core_jaccard / adjunct_jaccard 只覆盖了一部分"
          "用药，拿它去验收 R1-3 的佐使克制约束会得出一个看不出真假的结论。"
          "先在 prompts/v1/s3_syndrome.yaml 里把 role 那条要求写实（现在写的是"
          "「分不清就填 null」，模型可能大面积走了这个出口），再重跑这个脚本。",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
