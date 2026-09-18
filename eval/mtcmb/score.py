"""TCM-PR 的打分：药味集合的 P / R / F1 + 完全命中率。**零 LLM。**

## 为什么这里自己算，而 SDT 那边坚持跑官方脚本

SDT 的分要跟论文里 15 个模型比，唯一的办法是跑他们那份 `evaluate.py`。
MTCMB 的 TCM-PR 如果附了官方打分脚本，**同样以它为准**，这个模块只用来
做本项目内部的对照（A/B 两个 solver 的差）。`score_records()` 因此把
`scorer` 写在返回值里（`"scorer": "eval.mtcmb.score"`），报告里能一眼看出
这个数不是官方分——**没有这个标记，两种来源的分迟早会被并排放进一张表**。

## 三条口径，都写在数里

1. **归一走 `core.herbs.normalize_herb`**，不另写一套字面比较。「炒白术」
   和「白术」是同一味药，按字面比会把它判成两味——这个项目在同一堵墙上
   撞过三次（SOURCES.md「同一概念的匹配逻辑只能有一处实现」）。
2. **P/R/F1 按记录算再取均值（macro）**，不是把所有药味堆一起算（micro）。
   micro 会让开了 20 味药的那条记录说了算。两个都报，`micro` 在返回值里
   单独一栏，读的人自己挑——但**报告里要写清用的是哪一个**。
3. **空方不是 0 分是"没作答"**：安全否决拦下的记录、模型返回空列表的记录，
   分子分母都记在 `n_empty` 里单独报。把它们当 0 分混进均值，等于把
   "系统拒绝作答"和"系统答错了"算成同一件事。
"""
from __future__ import annotations

import statistics


def normalize_set(herbs) -> set[str]:
    """药名列表 → 归一后的集合。空串丢掉（"" 会让交集永远多一个元素）。"""
    from core.herbs import normalize_herb

    return {n for n in (normalize_herb(h) for h in herbs or []) if n}


def score_one(pred, gold) -> dict:
    """一条记录的 P / R / F1。**分母为 0 时报 None 不报 0**：
    没预测出药味和"预测的全错"是两回事。"""
    p, g = normalize_set(pred), normalize_set(gold)
    hit = len(p & g)
    precision = (hit / len(p)) if p else None
    recall = (hit / len(g)) if g else None
    if precision is None or recall is None or (precision + recall) == 0:
        f1 = 0.0 if (p and g) else None
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "n_pred": len(p), "n_gold": len(g), "n_hit": hit,
        "precision": (round(precision, 4) if precision is not None else None),
        "recall": (round(recall, 4) if recall is not None else None),
        "f1": (round(f1, 4) if f1 is not None else None),
        "exact": bool(p and g and p == g),
        "empty_pred": not p,
        "empty_gold": not g,
    }


def score_records(pairs) -> dict:
    """`pairs` 是 [(record_id, pred_herbs, gold_herbs)]。

    返回 macro（逐条算再平均）和 micro（把命中/预测/参考各自求和再算）两套，
    **并把没作答的条数单独列出来**。
    """
    rows = []
    for rid, pred, gold in pairs:
        row = score_one(pred, gold)
        row["record_id"] = rid
        rows.append(row)
    scored = [r for r in rows if not r["empty_pred"] and not r["empty_gold"]]
    n_hit = sum(r["n_hit"] for r in scored)
    n_pred = sum(r["n_pred"] for r in scored)
    n_gold = sum(r["n_gold"] for r in scored)
    micro_p = (n_hit / n_pred) if n_pred else None
    micro_r = (n_hit / n_gold) if n_gold else None
    micro_f1 = (2 * micro_p * micro_r / (micro_p + micro_r)
                if micro_p and micro_r else None)

    def mean(key: str) -> float | None:
        vals = [r[key] for r in scored if r[key] is not None]
        return round(statistics.fmean(vals), 4) if vals else None

    return {
        # **这个数不是官方分**：标在最显眼处，见模块文档字符串
        "scorer": "eval.mtcmb.score",
        "n_records": len(rows),
        "n_scored": len(scored),
        "n_empty_pred": sum(1 for r in rows if r["empty_pred"]),
        "n_empty_gold": sum(1 for r in rows if r["empty_gold"]),
        "macro": {"precision": mean("precision"), "recall": mean("recall"),
                  "f1": mean("f1"),
                  "denominator": "有预测且有参考方的记录数"},
        "micro": {"precision": (round(micro_p, 4) if micro_p is not None else None),
                  "recall": (round(micro_r, 4) if micro_r is not None else None),
                  "f1": (round(micro_f1, 4) if micro_f1 is not None else None),
                  "denominator": "所有被计分记录的药味总数"},
        "exact_match": {"n": sum(1 for r in scored if r["exact"]),
                        "denominator": len(scored),
                        "value": (round(sum(1 for r in scored if r["exact"]) / len(scored), 4)
                                  if scored else None)},
        "rows": rows,
    }
