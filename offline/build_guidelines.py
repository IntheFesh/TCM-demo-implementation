"""R46 §7.3：生成 `data/standard/guidelines.jsonl`——循证对照层的底本。

## 这一层为什么是「教材推荐方案」而不是「指南」

任务书原文要的是《中医药循证临床实践指南》的条目。**那份指南的全文不在这个
项目里，也没有可公开获取的机读版本**；照抄一份"指南说什么"等于编造出处，
而这个项目每条结论都要能核对到原文（防幻觉设计的地基）。

所以本轮按用户的降级指示做**教材推荐方案对照**：底本是
`data/standard/formulary.jsonl`（《方剂学》，3184 条三元组、235 首方），
每一条对照都带：
  - `recommended_formula`：方名
  - `recommended_principle`：该方的「功用」原文
  - `source` / `span`：书名与抽取时留下的原文片段——**取不到 span 的不收**

界面上如实称「与教材推荐方案的对照」，**不称"指南"**。将来拿到指南全文时，
这个文件多一批 `source` 不同的行即可，`core/guideline_compare.py` 一行不用改。

## 证型 → 方 的匹配走既有实现

`Ontology.formulas_for_syndrome()` 已经做了这件事（主治里去掉尾「证」再比）。
**不在这里另写一套字面匹配**——CLAUDE.md 那条"同一概念的匹配逻辑只能有一处
实现"，这个项目已经在同一堵墙上撞过三次。

用法：

    python -m offline.build_guidelines            # 写 data/standard/guidelines.jsonl
    python -m offline.build_guidelines --stats    # 只打统计，不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "data" / "standard" / "guidelines.jsonl"

#: 证据等级。**这一版只有一档**：教材推荐方案。指南条目进来时会有
#: `guideline_1a` 之类的分级——那时候这个常量表会长出新值，不是改写旧值。
EVIDENCE_TEXTBOOK = "textbook"


def _syndromes() -> list[dict]:
    from core.tools import _load_standard

    return _load_standard() or []


def build_rows() -> list[dict]:
    """337 条证候 × 教材方，能对上且**有出处原文**的才出一行。"""
    from core.ontology import get_ontology

    onto = get_ontology()
    out: list[dict] = []
    for row in _syndromes():
        name = (row.get("name") or "").strip()
        if not name or row.get("is_category"):
            continue
        for formula in onto.formulas_for_syndrome(name):
            refs = formula.refs.get("功用") or formula.refs.get("主治") or ()
            span = next((r.span for r in refs if r.span), "")
            if not span:
                # 取不到原文就不收。一条没有出处的"推荐方案"在这个项目里
                # 等于没有——它会被当成可核对的依据显示出来。
                continue
            book = next((r.book for r in refs if r.span), "")
            out.append({
                "syndrome": name,
                "syndrome_code": row.get("code") or "",
                "disease": row.get("disease") or "",
                "recommended_principle": "；".join(formula.functions),
                "recommended_formula": formula.name,
                "evidence_level": EVIDENCE_TEXTBOOK,
                "source": book,
                "span": span,
            })
    return out


def stats(rows: list[dict]) -> dict:
    syn = {r["syndrome"] for r in rows}
    total = sum(1 for r in _syndromes() if not r.get("is_category"))
    return {
        "rows": len(rows),
        "syndromes_covered": len(syn),
        "syndromes_total": total,
        "coverage": round(len(syn) / total, 4) if total else 0.0,
        "formulas": len({r["recommended_formula"] for r in rows}),
        "books": sorted({r["source"] for r in rows}),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stats", action="store_true", help="只打统计，不写文件")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    rows = build_rows()
    s = stats(rows)
    print(json.dumps(s, ensure_ascii=False, indent=2))
    if s["rows"] == 0:
        print("一条都没生成：检查 data/standard/formulary.jsonl 在不在", file=sys.stderr)
        return 1
    if args.stats:
        return 0
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"→ {path}（{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
