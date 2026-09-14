"""调官方 evaluate.py 打分。**不重新实现计分逻辑。**

分数要和论文里 15 个模型可比，唯一的办法是跑他们那份脚本本身。这里做的只有
两件事：把 evaluate.py 动态 import 进来，以及用它里面的四个计分函数额外算一份
分项明细（`automated_score` 只返回加权总分，看不出是哪一项拉胯）。分项也用
他们的函数算，不是我们另写一套。

**读数注意（两条都要写进报告）：**

1. `automated_score` 返回的是**求和**不是均值，量纲随记录数走。50 条的满分是
   50.0（每条四项加权和为 1.0）。报告里同时给原始总分和 总分/条数，并写明
   条数——只报一个 32.7 别人不知道分母是 50 还是 200。

2. 官方 Validation 金标准文件开头有 UTF-8 BOM，`evaluate.py` 用默认方式读，
   BOM 会粘在第一条病案 ID 上导致那条恒 0 分。**默认保留这个行为**（论文里的
   分大概率也是这么跑出来的，"修好"它反而不可比）。想知道它值多少分，用
   score_submission(..., diagnose_bom=True) 另算一份诊断值，单独标注。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from eval.sdt.data import read_gold

TASK_WEIGHTS = (0.2, 0.3, 0.4, 0.1)


def load_official_scorer(sdt_dir: Path):
    """从 TCMEval 仓库里动态加载 evaluate.py。找不到就直接报错——
    静默退回自己实现的计分会产出一个看起来正常、实际不可比的数。"""
    path = Path(sdt_dir) / "evaluate.py"
    if not path.exists():
        raise FileNotFoundError(
            f"未找到官方评分脚本 {path}。先 clone github.com/zhuyan166/TCMEval，"
            "把 evaluation/TCMEval-SDT 的路径传给 --sdt-dir。不要用自己实现的计分"
            "代替它——那样算出来的分跟论文里的 15 个模型不可比。"
        )
    spec = importlib.util.spec_from_file_location("tcmeval_official_evaluate", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _letters(field: str) -> list[str]:
    """把选项字段切成**干净的**字母列表：空串给 `[]` 而不是 `[""]`。

    只用于集合运算（谁选了、谁漏了、反事实要保留哪几个字母）——`""` 在集合里
    没有意义。**打分时不用这个**：那里一律把 `field.split(";")` 原样交给官方的
    score_proportional，连尾随分隔符带出来的空元素都照传，这样算出来的分项
    才跟 automated_score 逐位可比。两处刻意不统一，理由写在 per_record_scores
    里（一个回答"哪些字母参与集合运算"，一个回答"官方那把尺子怎么量"）。
    """
    return [x for x in (field or "").split(";") if x.strip()]


def per_record_scores(scorer, gold: dict[str, list[str]],
                      submitted: dict[str, list[str]]) -> list[dict]:
    """**逐条**四项得分 + Task2/3 的选择数对比 + 两个反事实得分。

    计分一律调官方脚本里的函数，不自己实现——包括反事实：R2-1 要回答"多选和
    少选各值多少分"，那两个数必须跟总分同一把尺子算出来，自己按推测的公式
    算一遍只会得到一个看起来合理、实际跟官方分不可比的数。

    两个反事实（只对 Task2/3 这两个多选项有意义）：
      `score_if_wrong_dropped`  把选了但不在金标准里的字母去掉 = 只保留交集。
                                它减去实际得分 = **多选的代价**。
      `score_if_missed_added`   把漏选的金标准字母补上 = 取并集。
                                它减去实际得分 = **少选的代价**。
    两者都不是"作弊后的分"，是用来定位失分来源的边际量：哪一边能捞回的分多，
    prompt 就该往那一边改。

    `automated_score` 只给一个加权总分，看不出任何这些——这个函数存在的理由。
    """
    rows: list[dict] = []
    for rid, sub in submitted.items():
        ref = gold.get(rid)
        if ref is None:
            continue  # 金标准里没有这条（Validation 首条被 BOM 粘住时就是这样）
        scores = [0.0, 0.0, 0.0, 0.0]
        if sub[0]:
            scores[0] = scorer.clinical_info_extraction_eval(sub[0], ref[0].split(";"))
        if sub[3]:
            scores[3] = scorer.rouge_l(sub[3], ref[3])
        # 多选项的明细放在 choices 下面，不跟 task2/task3 这两个**标量得分**
        # 抢同一个键名——抢了之后 row["task2"] 到底是分数还是明细，取决于哪一行
        # 先跑，那种 bug 只会在读的时候炸。
        row: dict = {"record_id": rid, "choices": {}}
        for idx, task in ((1, "task2"), (2, "task3")):
            # 打分用官方口径：原样 split，参照集也原样 split。反事实跟实际得分
            # 必须共用同一个参照集，否则两个数不在同一把尺子上、相减没有意义。
            gold_ref = ref[idx].split(";")
            if sub[idx]:
                scores[idx] = scorer.score_proportional(sub[idx].split(";"), gold_ref, 1)
            chosen, gold_letters = _letters(sub[idx]), _letters(ref[idx])
            chosen_set, gold_set = set(chosen), set(gold_letters)
            wrong, missed = sorted(chosen_set - gold_set), sorted(gold_set - chosen_set)
            kept, union = sorted(chosen_set & gold_set), sorted(chosen_set | gold_set)
            row["choices"][task] = {
                "chosen": chosen, "gold": gold_letters,
                "n_chosen": len(chosen_set), "n_gold": len(gold_set),
                "wrong": wrong, "missed": missed,
                "score": scores[idx],
                # 反事实：去掉错选之后、补上漏选之后，各能拿多少分
                "score_if_wrong_dropped": (
                    scorer.score_proportional(kept, gold_ref, 1) if kept else 0.0
                ),
                "score_if_missed_added": (
                    scorer.score_proportional(union, gold_ref, 1) if union else 0.0
                ),
            }
        row.update({f"task{i + 1}": scores[i] for i in range(4)})
        # 每条记录四项加权和的满分是 1.0，所以 loss 就是 1 − weighted
        row["weighted"] = sum(w * scores[i] for i, w in enumerate(TASK_WEIGHTS))
        row["loss"] = 1.0 - row["weighted"]
        rows.append(row)
    return rows


def _task_breakdown(scorer, gold: dict[str, list[str]], submitted: dict[str, list[str]]) -> dict:
    """分项明细，用的是官方脚本里的四个计分函数本身。

    从 per_record_scores 求和而不是自己再循环一遍：同一套分项得分有两处实现的
    话，逐条明细和总分迟早对不上（CLAUDE.md「同一概念只能有一处实现」）。
    """
    rows = per_record_scores(scorer, gold, submitted)
    return {f"task{i + 1}": sum(r[f"task{i + 1}"] for r in rows) for i in range(4)}


def _read_submission(path: Path) -> dict[str, list[str]]:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        f = line.split("@")
        if len(f) >= 5 and f[0] not in out:  # 官方按首次出现去重，这里保持一致
            out[f[0]] = f[1:5]
    return out


def score_submission(sdt_dir: Path, split: str, submission_path: Path,
                     diagnose_bom: bool = False) -> dict:
    scorer = load_official_scorer(sdt_dir)
    gold_path = Path(sdt_dir) / "Results" / f"{split}_data_result.txt"
    total = scorer.automated_score(str(gold_path), str(submission_path))

    gold = read_gold(sdt_dir, split, strip_bom=diagnose_bom)
    submitted = _read_submission(submission_path)
    n = len(gold)
    breakdown = _task_breakdown(scorer, gold, submitted)
    weighted = sum(w * breakdown[f"task{i + 1}"] for i, w in enumerate(TASK_WEIGHTS))

    return {
        "split": split,
        "n_records": n,
        "n_submitted": len(submitted),
        # 官方脚本原样跑出来的数，报告里引用这个
        "official_total": total,
        "official_per_record": total / n if n else 0.0,
        # 分项用官方的计分函数算，加权后应当与 official_total 接近；
        # 差异只可能来自 BOM 那条（diagnose_bom=True 时会显出来）
        "task_totals": breakdown,
        "task_per_record": {k: v / n if n else 0.0 for k, v in breakdown.items()},
        "weighted_from_breakdown": weighted,
        "bom_diagnosed": diagnose_bom,
    }
