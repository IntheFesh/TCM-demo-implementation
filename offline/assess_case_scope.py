"""接新一批医案之前，**先量一遍它落在现有证候表的覆盖范围里没有**。零 LLM 调用。

动机：用户手上那份《王云启治癌验案录》是肿瘤科医案，而这个项目现有的语料、
标准证候表、以及 demo 的定位都在**脾胃门**（胃痛/痞满/呕吐/泄泻/肿胀……）。
肿瘤科医案进来意味着扩大范围，而"要不要扩"是一个需要数据支撑的决定，不是
一句"应该可以吧"。这个脚本给那个决定提供数：

  - 这批医案里出现的证型/病名，在 `data/standard/syndromes.jsonl`（337 条）
    里能匹配上多少
  - 匹配不上的都列出来——那些就是"扩大范围"要补的表

    python -m offline.assess_case_scope --input 王云启.jsonl
    python -m offline.assess_case_scope --input cases.json --show 30

退出码：0 = 跑完（**覆盖率低不是失败**，是这个脚本要报的事实）；1 = 文件读不了。

## 为什么不自动判"能不能进"

覆盖率低有两种完全不同的含义：(a) 这批医案讲的是另一个科，现有证候表确实
不覆盖 → 要扩表，或者不收这批数据；(b) 只是证型名写法不同（"脾虚湿困"vs
"脾虚湿阻"）→ 补别名就行。**代码分不出这两种**，所以这个脚本只报数和明细，
判断留给人。给它设一个阈值然后自动 PASS/FAIL，等于假装这件事能自动化。

## 匹配复用 core/tools.py 的 lookup_standard

"这个证型名在标准表里有没有"这个判断**已经有实现了**：
`core.tools.lookup_standard`（精确名 / 编码 / 「编码+名称」合写 / 唯一部分匹配
四种都认，那几种写法是真实模型产出里实际出现过的）。这里直接调它，
不另写一套字面比对——"覆盖检查用字面子串比「水肿」和「肿胀」，把等价写法全判成
未覆盖"是 CLAUDE.md 记着的第一次撞墙，而"同一个词两个工具给出相反答案"是第三次。

标准表的路径因此也只有一处：`core.tools.STANDARD_PATH`。这个脚本不提供
`--syndromes-path`——提供了就等于在这里开第二个入口，而 lookup_standard 读的
还是它自己那一个。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNDROMES_PATH = ROOT / "data" / "standard" / "syndromes.jsonl"
DEFAULT_SHOW = 20

# 判"这批数据像不像肿瘤科"的关键词。**只用来给报告分类，不参与任何过滤**——
# 关键词命中不代表这条医案是肿瘤科的，只代表值得人看一眼。
ONCOLOGY_HINTS = ("癌", "瘤", "恶性", "转移", "化疗", "放疗", "术后", "肿块", "痞块", "积聚")


def n_standard_syndromes() -> int:
    """标准表里有多少条。只为了在报告里给覆盖率一个分母的量级参照，
    判断"有没有"一律走 lookup_standard。"""
    from core.tools import _load_standard

    return len(_load_standard())


def load_rows(path: Path) -> list[dict]:
    """跟 offline/tag_incompatible_cases.py 同一个约定：按扩展名判断，不猜。"""
    from offline.tag_incompatible_cases import load_rows as _load

    return _load(path)


def assess(rows: list[dict]) -> dict:
    """统计这批医案的证型在标准表里的覆盖情况。判"有没有"走
    core.tools.lookup_standard（全项目唯一实现）。"""
    from core.tools import lookup_standard

    # 同一个证型名在一批医案里会重复出现很多次，查一次记住结果——lookup_standard
    # 对每次调用都要遍历整张表，337 条 × 几百条医案是白跑的开销。
    verdict: dict[str, bool] = {}
    covered: dict[str, int] = {}
    uncovered: dict[str, int] = {}
    n_without_syndrome = 0
    oncology_hits: dict[str, int] = {}
    for row in rows:
        text = " ".join(str(row.get(k) or "") for k in ("raw", "raw_excerpt", "syndrome", "disease"))
        for hint in ONCOLOGY_HINTS:
            if hint in text:
                oncology_hits[hint] = oncology_hits.get(hint, 0) + 1
        name = (row.get("syndrome") or "").strip()
        if not name:
            n_without_syndrome += 1
            continue
        if name not in verdict:
            verdict[name] = bool(lookup_standard(name).get("found"))
        bucket = covered if verdict[name] else uncovered
        bucket[name] = bucket.get(name, 0) + 1
    n_with = len(rows) - n_without_syndrome
    return {
        "n_rows": len(rows),
        "n_with_syndrome": n_with,
        "n_without_syndrome": n_without_syndrome,
        "n_covered_records": sum(covered.values()),
        "n_uncovered_records": sum(uncovered.values()),
        "coverage_rate": round(sum(covered.values()) / n_with, 4) if n_with else None,
        "covered": covered,
        "uncovered": uncovered,
        "oncology_hits": oncology_hits,
        "n_standard_syndromes": n_standard_syndromes(),
    }


def format_report(result: dict, show: int) -> str:
    lines = [
        f"医案 {result['n_rows']} 条，其中 {result['n_with_syndrome']} 条有证型字段"
        f"（{result['n_without_syndrome']} 条没有，未抽取或字段缺失）",
        f"标准证候表：{result['n_standard_syndromes']} 条（data/standard/syndromes.jsonl）",
    ]
    rate = result["coverage_rate"]
    lines.append(
        f"证型覆盖率：{result['n_covered_records']}/{result['n_with_syndrome']} = "
        + ("不适用（没有带证型的记录）" if rate is None else f"{rate:.1%}")
    )
    if result["uncovered"]:
        top = sorted(result["uncovered"].items(), key=lambda kv: -kv[1])[:show]
        lines.append(f"表里没有的证型（{len(result['uncovered'])} 种，列前 {len(top)}）：")
        lines.extend(f"  {name}　{n} 条" for name, n in top)
        lines.append("  → 这些是「扩大范围」要补进 data/standard/syndromes.jsonl 的，"
                     "或者说明这批数据不属于当前门类。**代码分不出是哪一种**："
                     "可能是另一个科，也可能只是写法不同（补别名就行），要人看。")
    if result["oncology_hits"]:
        hits = "、".join(f"{k}×{v}" for k, v in
                        sorted(result["oncology_hits"].items(), key=lambda kv: -kv[1]))
        lines.append(f"肿瘤科相关词命中（只作提示，不参与任何过滤）：{hits}")
        lines.append("  → 这个项目现有语料与定位都在脾胃门。肿瘤科医案进来是扩大范围，"
                     "涉及：证候表要不要扩、demo 的定位表述要不要改、"
                     "安全否决的危重词表是否覆盖肿瘤急症。**这是产品决定，不是脚本能定的。**")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="接新医案前评估它在现有证候表的覆盖范围（零 LLM 调用）")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--show", type=int, default=DEFAULT_SHOW, help="未覆盖证型列前几种")
    args = ap.parse_args(argv)

    if not args.input.exists():
        print(f"未找到 {args.input}", file=sys.stderr)
        return 1
    try:
        rows = load_rows(args.input)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"读不了 {args.input}：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print("**零 LLM 调用**：只比对证型名。覆盖率低不是失败，是要报的事实。")
    print()
    if not SYNDROMES_PATH.exists():
        print(f"未找到标准证候表 {SYNDROMES_PATH}（core.tools.STANDARD_PATH）",
              file=sys.stderr)
        return 1
    print(format_report(assess(rows), args.show))
    return 0


if __name__ == "__main__":
    sys.exit(main())
