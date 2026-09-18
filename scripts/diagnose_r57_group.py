"""R57 某一组（默认 C）不达标时，缺的是哪一类医理规则——这条诊断当前**没有
任何官方报告在报**：`eval/ablation/r57.py::metrics_from_result` 只提取
`derivation_completeness_ratio`（一个汇总比率），从不读
`results[0]["insufficient_notes"]`——而 `core/schemas.py::InsufficientNote`
的文档字符串原话是"R57 消融实验要按这个字段统计到底缺什么，不是笼统一句
'没查到'"——这个字段是 R51/R52 就为这件事准备好的，只是没有工具真的用过它。

跑法（重跑指定组，不依赖已有的 `eval/report_ablation_r57.json`，因为那份
报告里 `rows[*].metrics` 已经把 `insufficient_notes`/完整的 `rule_refs`
丢了，只留了一个比率）：

    python -m scripts.diagnose_r57_group --group C \\
        --sdt-dir <TCMEval>/evaluation/TCMEval-SDT

输出两张表：
1. 哪个演绎步骤（脏腑/证型/治法/方剂/用药）标了"依据不足"，按
   `missing_rule_kind`（藏象/病机/治则/配伍）分类计数，附前几条 `what`
   原文——这是"缺哪一类规则"的直接证据，不是猜。
2. 全部 `rule_refs` 实际引用了哪些规则 id，按 `kind` 分类计数——跟上一张表
   对照着看：如果某一类规则"insufficient 很多、引用很少"，说明规则库这一
   类是真的薄；如果"insufficient 不多但完整率还是低于 0.9"，问题更可能出在
   校验器判据本身，不是规则库覆盖面，诊断方向不一样。

只诊断，不改动任何生产代码——发现规则库某一类确实薄了，回 R51 按
`offline/extract_tcm_theory.py` 的既有生成方式补规则，不是这个脚本的职责。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def diagnose(group_key: str, complaints: list[dict], backend) -> dict:
    from core.chain import _THEORY_KIND_LABELS
    from core.theory import rule as theory_rule
    from eval.ablation.r57 import run_group
    from eval.ablation.spec import group_by_key

    group = group_by_key(group_key)
    rows = run_group(group, complaints, backend, keep_raw_results=True,
                     progress=lambda line: print(line, file=sys.stderr))

    insufficient_by_kind: Counter = Counter()
    insufficient_examples: dict[str, list[str]] = {k: [] for k in _THEORY_KIND_LABELS}
    cited_by_kind: Counter = Counter()
    unresolvable_ids: list[str] = []
    n_ok = n_with_output = 0

    for row in rows:
        if not row.get("ok"):
            continue
        n_ok += 1
        result = row.get("result") or {}
        results = result.get("results") or []
        r = results[0] if results else {}
        if not r or r.get("s3") is None:
            continue
        n_with_output += 1
        for note in r.get("insufficient_notes") or []:
            kind = note.get("missing_rule_kind")
            insufficient_by_kind[kind] += 1
            if len(insufficient_examples.get(kind, [])) < 3:
                insufficient_examples.setdefault(kind, []).append(note.get("what", ""))
        for ref in r.get("rule_refs") or []:
            rid = ref.get("rule_id") if isinstance(ref, dict) else None
            if not rid:
                continue
            tr = theory_rule(rid)
            if tr is None:
                unresolvable_ids.append(rid)
            else:
                cited_by_kind[tr.kind] += 1

    return {
        "group": group_key, "n_queries": len(complaints), "n_ok": n_ok,
        "n_with_output": n_with_output,
        "insufficient_by_kind": dict(insufficient_by_kind),
        "insufficient_examples": insufficient_examples,
        "cited_by_kind": dict(cited_by_kind),
        "unresolvable_rule_ids": unresolvable_ids,
        "kind_labels": dict(_THEORY_KIND_LABELS),
    }


def format_report(d: dict) -> str:
    labels = d["kind_labels"]
    lines = [f"# R57 {d['group']} 组诊断：缺的是哪一类医理规则", "",
            f"{d['n_ok']}/{d['n_queries']} 次问诊正常完成，其中 {d['n_with_output']} 条"
            "有完整的演绎结果（其余是安全否决或失败，不计入下面的统计）。", ""]

    lines.append("## 1. 「依据不足」按缺的规则类别计数")
    lines.append("")
    if not d["insufficient_by_kind"]:
        lines.append("零——这一批主诉没有任何一步标过 insufficient，"
                     "如果完整率仍然不达标，问题大概率不在规则库覆盖面，"
                     "去查符号验证器本身的判据（`core/formula_verifier.py`）。")
    else:
        lines.append("| 规则类别 | insufficient 次数 | 举例（what） |")
        lines.append("|---|---:|---|")
        for kind, label in labels.items():
            n = d["insufficient_by_kind"].get(kind, 0)
            if n == 0:
                continue
            examples = "；".join(d["insufficient_examples"].get(kind, [])[:2]) or "—"
            lines.append(f"| {label}（`{kind}`） | {n} | {examples} |")
        top_kind = max(d["insufficient_by_kind"], key=d["insufficient_by_kind"].get)
        lines.append("")
        lines.append(f"**最薄的一类：{labels.get(top_kind, top_kind)}**——回 R51 补这一类"
                     "规则，参照 `offline/extract_tcm_theory.py` 里同类规则的既有生成"
                     "方式（曲线不是新写一套抽取逻辑，是给同一个脚本加条目）。")

    lines.append("")
    lines.append("## 2. 实际引用的规则按类别计数（对照用）")
    lines.append("")
    lines.append("| 规则类别 | 被引用次数 |")
    lines.append("|---|---:|")
    for kind, label in labels.items():
        lines.append(f"| {label}（`{kind}`） | {d['cited_by_kind'].get(kind, 0)} |")
    if d["unresolvable_rule_ids"]:
        lines.append("")
        lines.append(f"⚠ {len(d['unresolvable_rule_ids'])} 处 `rule_refs` 引用的 id 在"
                     f"`core/theory.py` 里查不到（{', '.join(d['unresolvable_rule_ids'][:5])}"
                     f"{'…' if len(d['unresolvable_rule_ids']) > 5 else ''}）——这不该发生，"
                     "schema 校验器本该在生成时就拒绝不存在的 id，出现即是防幻觉约束被"
                     "绕过，比规则库覆盖面不够更严重，优先查这个。")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--group", default="C", choices=["A", "B", "C", "D"],
                    help="诊断哪一组，默认 C（核心组，R57 三条闸门里两条直接盯着它）")
    ap.add_argument("--sdt-dir", type=Path, default=None)
    ap.add_argument("--queries-path", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--backend", default="real", choices=["fake", "real"])
    args = ap.parse_args(argv)

    from eval.ablation.r57 import load_complaints

    try:
        complaints = load_complaints(args.sdt_dir, args.queries_path, args.limit)
    except SystemExit as e:
        print(e.code, file=sys.stderr)
        return 2

    if args.backend == "fake":
        print("⚠ --backend fake：insufficient_notes/rule_refs 是假后端的固定假数据，"
             "这份诊断只验证脚本本身能跑通，不代表真实规则库覆盖情况。",
             file=sys.stderr)

    from scripts.bench_consult import build_backend

    backend = build_backend(args.backend, 0.0, False)
    result = diagnose(args.group, complaints, backend)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
