"""量化 `herb_source_fabricated` 这条 veto 规则的归属误判率。

formula_verifier 用 `_find_span_owner` 判断一段依据原文"属于哪味药"，以发现把别的药
的原文张冠李戴的情况。本草里有大量格式化短语（归经、用量行等），归属判据太宽时，
真实的依据也会被指认给别的药，导致整页被否决。

这个脚本拿本体**自己的真实原文**去问 `_find_span_owner`「这段话是谁的」。真实原文的
正确答案永远是"就是它自己"，所以凡是被指认给别的药的，都是误判。零 LLM 调用，可反复运行。

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

    random.seed(7)  # 固定种子：同一份数据上多次运行的结果可比
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
    print(f"\n真实原文被指认给别的药：{len(wrong)}/{len(sample)} = {rate:.1%}")
    for w in wrong[:10]:
        print(f"   「{w['herb']}·{w['predicate']}」{w['span'][:34]!r}"
              f" → 「{w['misattributed_to']}」")
    if wrong:
        print("\n逐条核对上面的例子：语料本身的重复（多味药的归经/用量行逐字相同，"
              "或抽取截断了药名）也会表现为误判，这类不是归属判据的问题。")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n写入 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
