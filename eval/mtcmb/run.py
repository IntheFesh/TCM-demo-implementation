"""跑 MTCMB 的 TCM-PR 并打分。需要真实 LLM，所以在 `eval/` 不在 `tests/`。

    # 上机第一步（零 LLM、零打分）：看清楚数据长什么样
    python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --probe

    # 两组都要跑（项目规则：任何数字都必须带对照）
    python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --solver baseline --out out/pr_baseline.json
    python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --solver chain    --out out/pr_chain.json

    # 已有两份产出之后，零 LLM 地把它们并排放
    python -m eval.mtcmb.run --compare out/pr_baseline.json out/pr_chain.json

成本：baseline 1 次调用/条，chain 3 次/条（多 S1+S2）。

**`--probe` 不是可选的。** 这台机器上没有 MTCMB 的数据，字段映射没有实测过；
一个字段名猜错的后果不是报错，是一份"所有人都得 0 分"的漂亮报告。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from core.batch import warn_if_failure_rate_high
from core.progress import Progress
from core.safety import safety_bypassed
from eval.mtcmb.adapter import SOLVERS
from eval.mtcmb.data import load_records, probe
from eval.mtcmb.score import score_records


def _print_probe(report: dict) -> int:
    print(f"目录 {report['dir']}　文件 {report['n_files']} 份：{report['files']}")
    print(f"记录 {report.get('n_rows', 0)} 条　带参考方 {report.get('n_with_answer', 0)} 条"
          f"（{report.get('answer_coverage')}）")
    print(f"文件里出现过的键：{report.get('keys')}")
    for s in report.get("samples", []):
        print(f"  题面字段 {s['question_field']}　答案字段 {s['answer_field']}"
              f"　参考方 {s['n_gold_herbs']} 味 {s['gold_head']}")
        print(f"    题面开头：{s['question_head']}")
    for p in report.get("problems", []):
        print(f"⚠ {p}", file=sys.stderr)
    return 1 if report.get("problems") else 0


def run_split(records, solver_name: str, *, ignore_safety_veto: bool) -> dict:
    solver = SOLVERS[solver_name]()
    answers = []
    failures: list[str] = []
    bar = Progress(total=len(records), label=f"MTCMB TCM-PR（{solver_name}）", unit="条")
    for record in records:
        ans = solver.solve(record, ignore_safety_veto=ignore_safety_veto)
        if ans.error:
            failures.append(ans.error_kind or "Unknown")
            bar.note(f"{record.record_id} 失败：{ans.error_kind}")
        answers.append(ans)
        bar.advance(note=record.record_id)
    bar.close()
    warn_if_failure_rate_high("MTCMB TCM-PR", len(failures), len(records))
    scored = score_records([(a.record_id, a.herbs, r.gold_herbs)
                            for a, r in zip(answers, records, strict=True)])
    return {
        "kind": "mtcmb_tcm_pr",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "solver": solver_name,
        "n_records": len(records),
        "n_safety_rejected": sum(1 for a in answers if a.safety_rejected),
        "n_errors": len(failures),
        # 失败按种类计数：全是 TimeoutError 和五花八门，该做的事完全不同
        "failure_kinds": {k: failures.count(k) for k in sorted(set(failures))},
        "ignore_safety_veto": ignore_safety_veto,
        "llm_calls": sum(a.llm_calls for a in answers),
        "score": scored,
        "answers": [{"record_id": a.record_id, "herbs": a.herbs,
                     "reasoning": a.reasoning, "safety_rejected": a.safety_rejected,
                     "error": a.error} for a in answers],
    }


def compare(baseline: dict, chain: dict) -> str:
    """两份产出并排。**差值只在两边口径一致时才算**：条数、打分器、
    是否旁路安全层，任何一项不同就不给差，只并排列出来。"""
    lines = ["# MTCMB TCM-PR：chain vs baseline", ""]
    same_n = baseline["n_records"] == chain["n_records"]
    same_scorer = baseline["score"]["scorer"] == chain["score"]["scorer"]
    same_safety = baseline["ignore_safety_veto"] == chain["ignore_safety_veto"]
    lines.append(f"记录数 {baseline['n_records']} vs {chain['n_records']}　"
                 f"打分器 `{baseline['score']['scorer']}`　"
                 f"安全层旁路 {baseline['ignore_safety_veto']} vs {chain['ignore_safety_veto']}")
    lines += ["", "| 指标 | baseline | chain | 差 |", "|---|---|---|---|"]
    comparable = same_n and same_scorer and same_safety
    for label, path in (("macro F1", ("macro", "f1")),
                        ("macro P", ("macro", "precision")),
                        ("macro R", ("macro", "recall")),
                        ("micro F1", ("micro", "f1")),
                        ("完全命中率", ("exact_match", "value"))):
        b = baseline["score"][path[0]][path[1]]
        c = chain["score"][path[0]][path[1]]
        delta = (round(c - b, 4) if comparable and isinstance(b, (int, float))
                 and isinstance(c, (int, float)) else None)
        lines.append(f"| {label} | {b} | {c} | {'—' if delta is None else delta} |")
    lines += ["", f"调用数：baseline {baseline['llm_calls']}　chain {chain['llm_calls']}",
              f"安全否决：baseline {baseline['n_safety_rejected']} 条　"
              f"chain {chain['n_safety_rejected']} 条"
              "（**按「没作答」记，不按 0 分记**）"]
    if not comparable:
        lines += ["", "> ⚠ **两边口径不一致，差值一栏留空**："
                  + "；".join(x for x in [
                      None if same_n else "记录数不同",
                      None if same_scorer else "打分器不同",
                      None if same_safety else "一边旁路了安全层",
                  ] if x)]
    lines += ["", "> 这里的分由 `eval.mtcmb.score` 算出，**不是官方分**。"
              "MTCMB 若附官方打分脚本，以它为准；这一份只用于本项目内部的"
              "chain vs baseline 对照。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", help="MTCMB 的 TCM-PR 数据目录")
    ap.add_argument("--pattern", default="*", help="只读匹配这个 glob 的文件")
    ap.add_argument("--probe", action="store_true", help="只看数据长什么样（零 LLM）")
    ap.add_argument("--solver", choices=sorted(SOLVERS), default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", nargs=2, metavar=("BASELINE", "CHAIN"),
                    help="把两份已有产出并排（零 LLM）")
    ap.add_argument("--ignore-safety-veto", action="store_true",
                    help="量化「安全层花了多少分」用。**用它跑出来的数必须单独标注**")
    args = ap.parse_args(argv)

    if args.compare:
        b = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        c = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        print(compare(b, c))
        return 0
    if not args.dir:
        print("要么 --dir，要么 --compare", file=sys.stderr)
        return 2
    if args.probe:
        return _print_probe(probe(args.dir, pattern=args.pattern))
    if not args.solver:
        print("--solver 必须指定（baseline / chain）。**两组都要跑才有对照**",
              file=sys.stderr)
        return 2

    records = load_records(args.dir, pattern=args.pattern, limit=args.limit)
    if not records:
        print("一条记录都没读到——先跑 --probe", file=sys.stderr)
        return 2
    bypass = safety_bypassed(args.ignore_safety_veto or None)
    t0 = time.perf_counter()
    report = run_split(records, args.solver, ignore_safety_veto=bypass)
    report["wall_s"] = round(time.perf_counter() - t0, 2)
    out = Path(args.out) if args.out else Path(f"out/mtcmb_pr_{args.solver}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    s = report["score"]
    print(f"{args.solver}：macro F1 {s['macro']['f1']}　micro F1 {s['micro']['f1']}　"
          f"完全命中 {s['exact_match']['value']}　"
          f"（计分 {s['n_scored']}/{s['n_records']} 条，"
          f"安全否决 {report['n_safety_rejected']} 条、失败 {report['n_errors']} 条）")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
