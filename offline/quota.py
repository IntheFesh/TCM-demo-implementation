"""附属：数据配额审计。检查 cases.json 是不是达到了项目自己在
data/SOURCES.md 第 7 节第 1 条已经写明的样本量目标——不是这里新定的数字：

  - 总案例数：每位医家 60-75 案（"正式版需扩到各 60-75 案"），取下限 60。
  - 带复诊序列的案例数：每位医家 >= 50（"原研究方案 K2 门槛是每位医家
    ≥50 例带复诊序列"）。

这台沙箱（30+30，且没有真实 cases.json）明确达不到——这不是这个脚本要
解决的问题，它只负责如实报出差多少，不是想办法把样本量拉上去。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.schemas import CaseRecord

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"

# 见模块文档字符串：两个数字都能在 data/SOURCES.md 第 7 节第 1 条找到出处，
# 不是凭空定的。
MIN_TOTAL_CASES = 60
MIN_MULTI_VISIT_CASES = 50


def audit(
    cases: list[CaseRecord],
    min_total: int = MIN_TOTAL_CASES,
    min_multi_visit: int = MIN_MULTI_VISIT_CASES,
) -> dict:
    """一个病人（case_group_id）只要在这份数据里出现 >=2 条诊次记录，
    组内每一条都算"带复诊序列的案例"——跟 core/transition.py 判断轨迹的
    口径完全一致（组大小 >=2 才算），两处都是同一个判据，不是各自一套。
    """
    group_sizes: dict[tuple[str, str], int] = {}
    for c in cases:
        key = (c.physician, c.case_group_id)
        group_sizes[key] = group_sizes.get(key, 0) + 1

    by_physician: dict[str, dict] = {}
    for c in cases:
        stats = by_physician.setdefault(c.physician, {
            "total_cases": 0, "n_patients": set(), "multi_visit_cases": 0,
        })
        stats["total_cases"] += 1
        stats["n_patients"].add(c.case_group_id)
        if group_sizes[(c.physician, c.case_group_id)] >= 2:
            stats["multi_visit_cases"] += 1

    result = {}
    for physician, stats in by_physician.items():
        result[physician] = {
            "total_cases": stats["total_cases"],
            "n_patients": len(stats["n_patients"]),
            "multi_visit_cases": stats["multi_visit_cases"],
            "total_quota": min_total,
            "multi_visit_quota": min_multi_visit,
            "meets_total_quota": stats["total_cases"] >= min_total,
            "meets_multi_visit_quota": stats["multi_visit_cases"] >= min_multi_visit,
        }
    return result


def format_report(audit_result: dict) -> str:
    lines = []
    for physician, r in sorted(audit_result.items()):
        total_mark = "达标" if r["meets_total_quota"] else "未达标"
        mv_mark = "达标" if r["meets_multi_visit_quota"] else "未达标"
        lines.append(
            f"{physician}：总案例 {r['total_cases']}/{r['total_quota']}（{total_mark}），"
            f"{r['n_patients']} 个病人，带复诊序列的案例 "
            f"{r['multi_visit_cases']}/{r['multi_visit_quota']}（{mv_mark}）"
        )
    return "\n".join(lines) if lines else "cases.json 里没有任何医案"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="附属：审计 cases.json 是否达到样本量门槛")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--min-total", type=int, default=MIN_TOTAL_CASES)
    ap.add_argument("--min-multi-visit", type=int, default=MIN_MULTI_VISIT_CASES)
    ap.add_argument("--out", type=Path, default=None, help="可选：把结果另存一份 JSON")
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.cases_path}。请先运行 `python -m offline.extract_cases` 生成 cases.json。"
        )

    cases = [
        CaseRecord.model_validate(r)
        for r in json.loads(args.cases_path.read_text(encoding="utf-8"))
    ]
    result = audit(cases, args.min_total, args.min_multi_visit)
    print(format_report(result))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写出 {args.out}")


if __name__ == "__main__":
    main()
