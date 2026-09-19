"""R63 §3：本草缺谓词的三选一归因。零 LLM，全部现算。

## 要回答的问题

释义面板里"某一项空着"绝大部分出自本草层缺谓词（实测：缺归经 598 味、
缺用量 672 味、缺禁忌 770 味、缺炮制 790 味）。**跟医案数量无关**——医案
1075 诊次、证候 337 条、方剂 235 首都够用，瓶颈在本草这一层。

缺一个谓词可能是三种完全不同的事，修法也完全不同：

  (a) 三元组里根本没有这条          → 抽取时就没抽到，要花钱重抽
  (b) 三元组里有，但主语跟正名对不上 → 归并漏了，**免费修**（扩 HERB_ALIASES）
  (c) 三元组里有，但谓词写法不同     → 建谓词同义表，**免费修**

**这个脚本的价值就是把三者分开。** 分不开的话，"扩别名表"这种免费修法会被
当成"要重抽 9776 条"，或者反过来，明明只能重抽却去改归并逻辑
——后者更糟：那会动到所有正在跑的释义。

用法：`python -m scripts.diagnose_ontology_gaps`（`--json out.json` 存结果）
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from core.context_prefix import build_entry_index
from core.herbs import normalize_herb
from core.ontology import _rows, get_ontology

#: 本体认的六个谓词。**顺序跟 `Ontology.stats()` 的报表一致**，好逐行对照。
PREDICATES = ("性味", "归经", "功效", "用量", "禁忌", "炮制")

#: 每一类举几个例子。§3.3 第 1 步要求 10 个。
N_EXAMPLES = 10


def diagnose() -> dict:
    ont = get_ontology()
    if not ont.available:
        return {"available": False,
                "note": "本草层数据文件不在，无法归因（这本身不是缺谓词）。"}
    rows = _rows("materia_medica")
    index = build_entry_index("materia_medica", rows)

    # 原始三元组里，每个**归一后**的药名各自有哪些谓词——归并之前的真相。
    raw_preds: dict[str, set[str]] = collections.defaultdict(set)
    raw_spellings: dict[str, set[str]] = collections.defaultdict(set)
    for raw_name, preds in index.items():
        name = normalize_herb(raw_name) or raw_name
        raw_preds[name] |= set(preds)
        raw_spellings[name].add(raw_name)

    # 三元组里出现过的谓词写法全集——(c) 类要靠它发现"性味 vs 药性"这种。
    all_predicate_spellings = collections.Counter(r.get("p") or "" for r in rows)
    unknown_predicates = {p: n for p, n in all_predicate_spellings.items()
                          if p not in PREDICATES}

    out: dict = {"available": True,
                 "n_rows": len(rows),
                 "n_herbs": len(ont.herbs),
                 "n_raw_subjects": len(index),
                 "predicate_spellings": dict(all_predicate_spellings),
                 "unknown_predicate_spellings": unknown_predicates,
                 "by_predicate": {}}

    for p in PREDICATES:
        missing = [h.name for h in ont.herbs.values() if not h.has(p)]
        # (b)：本体里这一味缺这个谓词，但三元组里**某个写法**有它
        #      ——归并把它丢了。R60 修过一次（refs 被覆盖而不是累加）。
        b = [n for n in missing if p in raw_preds.get(n, set())]
        # (c)：这一味在三元组里完全没有这个谓词，但**有一个本体不认的谓词写法**
        #      ——同义谓词没归并。
        c = [n for n in missing
             if p not in raw_preds.get(n, set())
             and (raw_preds.get(n, set()) - set(PREDICATES))]
        # (a)：剩下的——抽取时就没抽到。
        a = [n for n in missing if n not in set(b) | set(c)]
        out["by_predicate"][p] = {
            "n_missing": len(missing),
            "a_not_extracted": {"n": len(a), "examples": a[:N_EXAMPLES]},
            "b_name_mismatch": {
                "n": len(b),
                "examples": [{"herb": n, "spellings": sorted(raw_spellings[n])}
                             for n in b[:N_EXAMPLES]]},
            "c_predicate_variant": {
                "n": len(c),
                "examples": [{"herb": n,
                              "predicates_present": sorted(raw_preds.get(n, set()))}
                             for n in c[:N_EXAMPLES]]},
        }
    return out


def _print(d: dict) -> None:
    if not d.get("available"):
        print(d["note"])
        return
    print(f"本草三元组 {d['n_rows']} 条，原始主语 {d['n_raw_subjects']} 个，"
          f"归并后 {d['n_herbs']} 味")
    print("\n三元组里出现过的谓词写法：")
    for p, n in sorted(d["predicate_spellings"].items(), key=lambda kv: -kv[1]):
        mark = "" if p in PREDICATES else "  ← 本体不认这个写法"
        print(f"  {p}\t{n}{mark}")
    if not d["unknown_predicate_spellings"]:
        print("  （没有本体不认的写法——(c) 类在源头就是 0，"
              "抽取 schema 把谓词卡成了六个 Literal）")

    print(f"\n{'谓词':<6}{'缺':>6}{'(a) 没抽到':>12}{'(b) 名字对不上':>16}{'(c) 谓词写法':>14}")
    for p in PREDICATES:
        r = d["by_predicate"][p]
        print(f"{p:<6}{r['n_missing']:>6}{r['a_not_extracted']['n']:>12}"
              f"{r['b_name_mismatch']['n']:>16}{r['c_predicate_variant']['n']:>14}")

    for p in PREDICATES:
        r = d["by_predicate"][p]
        for key, label in (("b_name_mismatch", "(b) 名字对不上"),
                           ("c_predicate_variant", "(c) 谓词写法不同")):
            if r[key]["n"]:
                print(f"\n{p} 的 {label} 例子：")
                for e in r[key]["examples"]:
                    print(f"  {e}")
    print("\n各谓词 (a) 类的例子（这一类只能重抽，本轮不做）：")
    for p in PREDICATES:
        ex = d["by_predicate"][p]["a_not_extracted"]["examples"]
        if ex:
            print(f"  {p}：{'、'.join(ex)}")

    total_free = sum(d["by_predicate"][p]["b_name_mismatch"]["n"]
                     + d["by_predicate"][p]["c_predicate_variant"]["n"]
                     for p in PREDICATES)
    total_missing = sum(d["by_predicate"][p]["n_missing"] for p in PREDICATES)
    print(f"\n合计缺 {total_missing} 个 (药, 谓词) 槽位，其中**免费能修的 "
          f"{total_free} 个**（(b)+(c)），只能重抽的 {total_missing - total_free} 个。")
    if total_free == 0:
        print("免费的那两类是 0 ——归并没有漏，谓词写法也没有分歧。"
              "\n**缺口全部是 (a)：抽取时就没抽到那句原文。**"
              "\n所以 §3.3 第 3 步的验收目标（归经 598→<300）**改归并改不出来**，"
              "\n如实报出而不是硬凑。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    d = diagnose()
    _print(d)
    if args.json:
        Path(args.json).write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n写到 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
