"""R59 §0.2 的覆盖率探针固化成可重复跑的脚本。用户真机原来是拿一段一次性
python -c "..." 量出「698 种写法、归一后仍有 367 种（52.6%）查不到」——这个数字
只在那一次终端输出里，改了 HERB_ALIASES 之后没有能重新量一遍的地方。这个脚本
就是把那段一次性代码固化下来，跑法和当初完全一样（读 cases.json，对每种写法
调 core.ontology.Ontology.herb()），可以在任何时候重新验证覆盖率有没有退步。

    python -m scripts.check_herb_coverage                  # 默认读 cases.json
    python -m scripts.check_herb_coverage --max-unresolved 0.20   # 改验收阈值

验收线（R59 §1.1）：查不到的写法占比必须 ≤20%。默认阈值就是 0.20，退出码
非零表示没达标——可以直接接进 CI 或验收脚本。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.ontology import get_ontology

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"


def count_herb_names(records: list[dict]) -> Counter:
    counts: Counter = Counter()
    for r in records:
        for h in r.get("herbs") or []:
            h = (h or "").strip()
            if h:
                counts[h] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--max-unresolved", type=float, default=0.20,
                    help="验收阈值：查不到的写法占比上限，默认 0.20（R59 §1.1 定死的验收线）")
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        print(f"{args.cases_path} 不在——这个探针要读真实医案数据才有意义，"
             "按 README.md「快速开始」第 3 步生成。")
        return 2

    ont = get_ontology()
    if not ont.available:
        print("本体不可用（data/standard/materia_medica.jsonl 不在）——查不到写法"
             "跟本体缺失是两回事，这个探针在本体不可用时量不出真实覆盖率。")
        return 2

    records = json.loads(args.cases_path.read_text(encoding="utf-8"))
    counts = count_herb_names(records)
    total = len(counts)
    if total == 0:
        print(f"{args.cases_path} 里一味药都没抽到，探针量不出比例。")
        return 2

    unresolved_names = [name for name in counts if ont.herb(name) is None]
    resolved = total - len(unresolved_names)
    ratio = len(unresolved_names) / total

    print(f"从 {len(records)} 条诊次里数出 {total} 种药名写法，"
         f"{resolved} 种（{resolved / total:.1%}）本体已能直接查到"
         f"（normalize_herb + HERB_ALIASES），{len(unresolved_names)} 种"
         f"（{ratio:.1%}）查不到。")

    top_unresolved = sorted(unresolved_names, key=lambda n: -counts[n])[:10]
    if top_unresolved:
        print("查不到的写法里出现次数最多的几个（可用 "
             "`python -m scripts.build_herb_aliases` 生成候选表复核）：")
        for name in top_unresolved:
            print(f"  {name}\t{counts[name]} 次")

    passed = ratio <= args.max_unresolved
    verdict = "✅ 达标" if passed else "❌ 未达标"
    print(f"\n验收线：查不到占比 ≤{args.max_unresolved:.0%}。实测 {ratio:.1%}。{verdict}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
