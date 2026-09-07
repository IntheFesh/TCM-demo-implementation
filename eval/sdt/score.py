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


def _task_breakdown(scorer, gold: dict[str, list[str]], submitted: dict[str, list[str]]) -> dict:
    """分项明细，用的是官方脚本里的四个计分函数本身。"""
    totals = [0.0, 0.0, 0.0, 0.0]
    for rid, sub in submitted.items():
        ref = gold.get(rid)
        if ref is None:
            continue
        if sub[0]:
            totals[0] += scorer.clinical_info_extraction_eval(sub[0], ref[0].split(";"))
        if sub[1]:
            totals[1] += scorer.score_proportional(sub[1].split(";"), ref[1].split(";"), 1)
        if sub[2]:
            totals[2] += scorer.score_proportional(sub[2].split(";"), ref[2].split(";"), 1)
        if sub[3]:
            totals[3] += scorer.rouge_l(sub[3], ref[3])
    return {f"task{i + 1}": totals[i] for i in range(4)}


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
