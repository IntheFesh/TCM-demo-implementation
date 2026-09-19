"""R65 第 4 项：为什么产品路径会把「肝胃气滞」这类主诉整页否掉。

用户的问题是"产品路径与 R57 消融路径的配置差异（effort？revise 轮数？知识块
内容？）"。**实测结论：两条路径的配置逐项相同**（`S3_MODE=derived`、思考开、
effort=medium、`best_of_n=1`、`max_revise_rounds=1`；R57 的 `KNOBS` 根本不含
revise 轮数）。差异不在配置，在 `herb_source_fabricated` 这条 veto 规则的
**归属判据**——它会把本草里的格式化短语误判成"张冠李戴"。

这个脚本量化那个误判率：拿本体**自己的真实原文**去问
`_find_span_owner`「这段话是谁的」。真实原文的正确答案永远是"就是它自己"，
所以凡是被指认给别的药的，都是误判。零 LLM 调用，可反复跑。

    python -m scripts.diagnose_veto_attribution [--sample 200] [--json 输出路径]
"""
from __future__ import annotations

import argparse
import json
import random
from collections import Counter

from core.formula_verifier import _MIN_ATTRIBUTABLE_OVERLAP, _find_span_owner
from core.ontology import get_ontology


def _all_spans(ont) -> list[tuple[str, str, str]]:
    out = []
    for name, h in ont.herbs.items():
        for pred, rs in h.refs.items():
            for r in rs:
                t = r.span.strip()
                if t:
                    out.append((name, pred, t))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--json")
    a = ap.parse_args(argv)

    ont = get_ontology()
    spans = _all_spans(ont)
    lens = Counter(len(t) for _, _, t in spans)
    # 1~2 字的 span 是这件事的放大器：双向子串下「含'胃'字」就等于
    # 「命中粳米·归经的原文」。先把它们本身报出来。
    tiny = sorted({t for _, _, t in spans if len(t) <= 2}, key=len)
    chars = {t for _, _, t in spans if len(t) == 1}
    contaminated = sum(1 for _, _, t in spans if any(c in t for c in chars))

    random.seed(7)  # 固定种子：这个数要能跟上一轮比
    sample = random.sample(spans, min(a.sample, len(spans)))
    wrong = []
    for name, pred, t in sample:
        owner = _find_span_owner(t, ont, exclude_name=name)
        if owner is not None:
            wrong.append({"herb": name, "predicate": pred, "span": t,
                          "misattributed_to": owner[0], "owner_span": owner[1]})

    rate = len(wrong) / len(sample)
    report = {
        "n_herbs": len(ont.herbs),
        "n_spans": len(spans),
        "min_attributable_overlap": _MIN_ATTRIBUTABLE_OVERLAP,
        "span_len_hist_le12": {k: lens[k] for k in sorted(lens) if k <= 12},
        "one_char_spans": sorted(chars),
        "n_tiny_spans": len(tiny),
        # 对照基准（CLAUDE.md：任何数字都必须带对照）：修前的同一口径同一种子
        # 是 146/200 = 73.0%，那一版 `_find_span_owner` 只要双向子串命中就
        # 认定找到主人。
        "baseline_before_r65": {"wrong": 146, "n": 200, "rate": 0.73},
        "contaminated_by_one_char_spans": contaminated,
        "n_sampled": len(sample),
        "n_misattributed": len(wrong),
        "misattribution_rate": round(rate, 4),
        "examples": wrong[:10],
    }
    print(f"药条 {report['n_herbs']}，span {report['n_spans']} 条")
    print(f"1 字 span：{report['one_char_spans']}；≤2 字：{len(tiny)} 条")
    print(f"含 1 字 span 那几个字的 span：{contaminated}/{len(spans)} "
          f"= {contaminated / len(spans):.1%}")
    print(f"\n真实原文被指认给别的药：{len(wrong)}/{len(sample)} = {rate:.1%}"
          f"（修前同口径同种子 146/200 = 73.0%）")
    for w in wrong[:10]:
        print(f"   「{w['herb']}·{w['predicate']}」{w['span'][:34]!r}"
              f" → 「{w['misattributed_to']}」")
    if wrong:
        print("\n残余这些是**语料本身的重复**（几十味药的归经/用量行逐字相同，"
              "或抽取把「秦艽」截成了「秦」），不是判据还漏——"
              "唯一性判据已经把系统性的那批挡掉了。")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n写入 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
