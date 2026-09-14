"""R2-1：SDT 失分分析。**对已有的提交文件重新聚合，零 LLM 调用。**

分数已经算出来了，这个模块只是把它拆开看：现在报告里只有一个总分
（chain 22.833 / baseline 22.068，增益 0.77），不知道失分在哪类题上、
也不知道该往哪个方向改 prompt。

聚合出五样东西：

  1. 逐条 T1/T2/T3/T4 得分
  2. **多选率 vs 少选率**，以及两者各自的**边际代价（用官方计分函数实测，
     不是按推测的公式估）**——这是 R2-2 改不改 prompt 的判据
  3. 完全对 / 部分对 / 完全错 的分布
  4. 失分最多的 10 条，模型答案与金标准并排
  5. 按病机 / 证型分组的得分，看有没有某类系统性失分

**关于"多选有惩罚、少选没有"这个前提：这个模块不假设它成立，而是量出来。**
官方 score_proportional 的具体公式在 TCMEval 仓库里，我们不复制它（复制就
不可比了），所以判据不能建立在"我们以为它怎么算"上。做法是反事实：把错选
去掉能捞回多少分、把漏选补上能捞回多少分，两个数都用官方函数算。哪边大，
prompt 就该往那边改；如果两边差不多，就说明选择策略不是主因，该去看别的
环节——这种情况下**不要**为了"做点什么"去改 prompt。
"""
from __future__ import annotations

from pathlib import Path

from eval.sdt.data import load_gold_fields, load_split
from eval.sdt.score import (
    TASK_WEIGHTS, _read_submission, load_official_scorer, per_record_scores,
)

# 判"主因"的两道门槛，两道都要过才算定了主因：
#   比值 1.5×  —— 「显著高于」的操作化定义。1.2× 这种差距在 50 条样本上
#                 随机波动就能造出来，1.5× 才值得据此改 prompt。
#   绝对 0.5 分 —— 当前 chain 相对 baseline 的全部增益只有 0.77 分。能捞回
#                 0.5 分意味着这一个方向就值 65% 的现有增益；低于这个数的
#                 方向，改了也说不清是改动起作用还是噪声。
DOMINANCE_RATIO = 1.5
DOMINANCE_ABS_POINTS = 0.5

# 失分榜列几条、临床资料截多长
TOP_LOSS_N = 10
CLINICAL_DATA_CHARS = 80

# Task2/Task3 在总分里的权重（TASK_WEIGHTS 的第 2、3 项），把"某一项里捞回
# 的分"换算成"总分里捞回的分"要乘它——不换算的话两个任务的数没法相加。
TASK2_WEIGHT, TASK3_WEIGHT = TASK_WEIGHTS[1], TASK_WEIGHTS[2]


def selection_stats(rows: list[dict]) -> dict:
    """多选 / 少选 / 恰好的分布，以及两边的边际代价（已换算成总分里的分）。

    分母是 (记录 × 任务) 对：Task2 和 Task3 都是多选题，同一条记录在两项上
    的选择行为可以不一样，合起来数才是"这个模型的选择习惯"。两项也各自单独
    报——万一只有一项有问题，合并数会把它冲淡。
    """
    per_task: dict[str, dict] = {}
    for task, weight in (("task2", TASK2_WEIGHT), ("task3", TASK3_WEIGHT)):
        cells = [r["choices"][task] for r in rows]
        over = [c for c in cells if c["n_chosen"] > c["n_gold"]]
        under = [c for c in cells if c["n_chosen"] < c["n_gold"]]
        exact = [c for c in cells if c["n_chosen"] == c["n_gold"]]
        # 注意：n_chosen == n_gold 不等于选对了（选了同样多但选错了几个），
        # 所以"恰好"这一列只说数量对得上，不说答案对。
        per_task[task] = {
            "n_cells": len(cells),
            "n_over": len(over), "n_under": len(under), "n_exact": len(exact),
            "over_rate": len(over) / len(cells) if cells else None,
            "under_rate": len(under) / len(cells) if cells else None,
            "n_with_wrong_picks": sum(1 for c in cells if c["wrong"]),
            "n_with_missed_picks": sum(1 for c in cells if c["missed"]),
            "n_wrong_picks": sum(len(c["wrong"]) for c in cells),
            "n_missed_picks": sum(len(c["missed"]) for c in cells),
            "weight": weight,
            # 边际代价：换算成总分里的分（乘任务权重）
            "recoverable_from_dropping_wrong": weight * sum(
                c["score_if_wrong_dropped"] - c["score"] for c in cells),
            "recoverable_from_adding_missed": weight * sum(
                c["score_if_missed_added"] - c["score"] for c in cells),
        }
        # **单个选项的边际代价**：一个错选值多少分 vs 一个漏选值多少分。
        # 总量会被次数带偏（错选 30 个、漏选 3 个，总量当然错选大），这两个数
        # 才直接回答 prompt 该不该说"宁可不选"——如果一个漏选比一个错选更贵，
        # 现在 prompt 里那句「拿不准的宁可不选」就是反着的。
        per_task[task]["cost_per_wrong_pick"] = (
            per_task[task]["recoverable_from_dropping_wrong"] / per_task[task]["n_wrong_picks"]
            if per_task[task]["n_wrong_picks"] else None
        )
        per_task[task]["cost_per_missed_pick"] = (
            per_task[task]["recoverable_from_adding_missed"] / per_task[task]["n_missed_picks"]
            if per_task[task]["n_missed_picks"] else None
        )

    n_cells = sum(t["n_cells"] for t in per_task.values())
    combined = {
        "n_cells": n_cells,
        "n_over": sum(t["n_over"] for t in per_task.values()),
        "n_under": sum(t["n_under"] for t in per_task.values()),
        "n_exact": sum(t["n_exact"] for t in per_task.values()),
        "recoverable_from_dropping_wrong": sum(
            t["recoverable_from_dropping_wrong"] for t in per_task.values()),
        "recoverable_from_adding_missed": sum(
            t["recoverable_from_adding_missed"] for t in per_task.values()),
    }
    combined["over_rate"] = combined["n_over"] / n_cells if n_cells else None
    combined["under_rate"] = combined["n_under"] / n_cells if n_cells else None
    n_wrong = sum(t["n_wrong_picks"] for t in per_task.values())
    n_missed = sum(t["n_missed_picks"] for t in per_task.values())
    combined["n_wrong_picks"], combined["n_missed_picks"] = n_wrong, n_missed
    combined["cost_per_wrong_pick"] = (
        combined["recoverable_from_dropping_wrong"] / n_wrong if n_wrong else None)
    combined["cost_per_missed_pick"] = (
        combined["recoverable_from_adding_missed"] / n_missed if n_missed else None)
    return {"by_task": per_task, "combined": combined,
            "verdict": _selection_verdict(combined)}


def _selection_verdict(combined: dict) -> dict:
    """判主因。**按边际代价（分）判，不按次数判**：多选 20 次但每次只丢 0.01
    分，跟少选 5 次每次丢 0.2 分，该改的不是同一边。次数照样报出来，但它决定
    不了结论。"""
    drop, add = (combined["recoverable_from_dropping_wrong"],
                 combined["recoverable_from_adding_missed"])
    bigger, smaller = max(drop, add), min(drop, add)
    lever = "多选（去掉错选能捞回更多）" if drop >= add else "少选（补上漏选能捞回更多）"
    decided = (
        bigger >= DOMINANCE_ABS_POINTS
        and (smaller <= 0 or bigger / smaller >= DOMINANCE_RATIO)
    )
    return {
        "recoverable_from_dropping_wrong": drop,
        "recoverable_from_adding_missed": add,
        "lever": lever if decided else None,
        "decided": decided,
        "reason": (
            f"{lever}：{bigger:.3f} 分 vs {smaller:.3f} 分，"
            f"既超过 {DOMINANCE_ABS_POINTS} 分的绝对门槛、又达到 {DOMINANCE_RATIO}× 的比值门槛"
            if decided else
            f"两边差距不足以定主因（{bigger:.3f} 分 vs {smaller:.3f} 分；门槛是"
            f"≥{DOMINANCE_ABS_POINTS} 分且 ≥{DOMINANCE_RATIO}×）。"
            "**这种情况下不要改选择策略的 prompt**——改了也分不清是改动起作用"
            "还是噪声，该去看别的环节（逐条失分榜和分组得分）"
        ),
    }


def correctness_distribution(rows: list[dict]) -> dict:
    """完全对 / 部分对 / 完全错。

    判据就是得分本身：1.0 = 完全对，0 < s < 1 = 部分对，0 = 完全错。
    **Task1/Task4 的这三列只能当分布看，不能当"对错"看**：Task1 按字符串
    完全相等评分、Task4 是 ROUGE-L，自由文本拿到整 1.0 本来就近乎不可能，
    那一列的"完全对"恒等于 0 不说明模型答错了。Task2/Task3 是从给定字母里
    多选，这三档才真的对应"对/半对/错"。
    """
    out = {}
    for i in range(4):
        key = f"task{i + 1}"
        scores = [r[key] for r in rows]
        out[key] = {
            "n": len(scores),
            "full": sum(1 for s in scores if s >= 1.0),
            "partial": sum(1 for s in scores if 0.0 < s < 1.0),
            "zero": sum(1 for s in scores if s <= 0.0),
            "mean": sum(scores) / len(scores) if scores else None,
            # 多选题才是真的"对/半对/错"，另两项只是分数分布
            "discrete": key in ("task2", "task3"),
        }
    return out


def group_by_gold_option(rows: list[dict], records: list, task: str) -> list[dict]:
    """按金标准选项的**中文文本**分组报得分，看有没有某类病机/证型系统性失分。

    一条记录的金标准可能有多个选项，**每个选项各自成组**（这条记录同时计入
    几组），不是把"A;J"当成一个组合类别——按组合分组的话 50 条记录能出 40 个
    只含 1 条的组，什么也看不出来。

    每组必须带 n：n=1 的组不是证据，是一条记录。调用方负责把 n 一起显示出来。
    """
    options_attr = "pathogenesis_options" if task == "task2" else "syndrome_options"
    by_id = {r.record_id: r for r in records}
    buckets: dict[str, list[float]] = {}
    for row in rows:
        rec = by_id.get(row["record_id"])
        if rec is None:
            continue
        options = getattr(rec, options_attr)
        for letter in row["choices"][task]["gold"]:
            # 选项文本拿不到（金标准里的字母不在这条记录的选项表里）时用字母
            # 本身当组名并标出来，不要静默丢掉这条——那会让分组的分母悄悄变小
            label = options.get(letter) or f"{letter}（选项表里没有这个字母）"
            buckets.setdefault(label, []).append(row["choices"][task]["score"])
    groups = [
        {"label": label, "n": len(scores), "mean": sum(scores) / len(scores)}
        for label, scores in buckets.items()
    ]
    return sorted(groups, key=lambda g: (g["mean"], -g["n"]))


def analyze(sdt_dir: Path, split: str, submission_path: Path,
            diagnose_bom: bool = False) -> dict:
    """把一份提交文件聚合成失分分析。**不调用任何 LLM**——只读文件 + 调官方
    计分函数。"""
    scorer = load_official_scorer(sdt_dir)
    gold, gold_source = load_gold_fields(sdt_dir, split, strip_bom=diagnose_bom)
    submitted = _read_submission(submission_path)
    records = load_split(sdt_dir, split)
    rows = per_record_scores(scorer, gold, submitted)

    # 官方 automated_score 也跑一次（零额外成本，scorer 已经加载了）：
    # 它是**唯一可以跟论文里那 15 个模型比的数**，而下面那个 weighted_total 是
    # 我们按官方计分函数逐条加权求和得到的。两个数应当很接近，差额只该来自
    # BOM 那条；差得多就是聚合这一层出了问题，必须报出来而不是只报好看的那个。
    gold_path = Path(sdt_dir) / "Results" / f"{split}_data_result.txt"
    official_total = (
        scorer.automated_score(str(gold_path), str(submission_path))
        if gold_path.exists() else None
    )

    unmatched = sorted(set(submitted) - set(gold))
    task_totals = {f"task{i + 1}": sum(r[f"task{i + 1}"] for r in rows) for i in range(4)}
    weighted_total = sum(r["weighted"] for r in rows)
    return {
        "split": split,
        "submission": str(submission_path),
        "gold_source": gold_source,
        "bom_diagnosed": diagnose_bom,
        "n_gold": len(gold),
        "n_submitted": len(submitted),
        "n_scored": len(rows),
        # 提交了但金标准里匹配不上的病案 ID。Validation 上首条会出现在这里，
        # 那不是模型失分，是官方金标准的 BOM（见 data/SOURCES.md 第 14 条）。
        "unmatched": unmatched,
        # 官方口径总分（可跟论文比）；Train 没有 Results 文件时是 None
        "official_total": official_total,
        "task_totals": task_totals,
        "task_per_record": {k: v / len(rows) if rows else None for k, v in task_totals.items()},
        "weighted_total": weighted_total,
        "weighted_per_record": weighted_total / len(rows) if rows else None,
        "rows": rows,
        "selection": selection_stats(rows),
        "correctness": correctness_distribution(rows),
        "top_losses": sorted(rows, key=lambda r: -r["loss"])[:TOP_LOSS_N],
        "groups": {
            "task2": group_by_gold_option(rows, records, "task2"),
            "task3": group_by_gold_option(rows, records, "task3"),
        },
        "_records_by_id": {r.record_id: r for r in records},
    }


# ---------- 打印 ----------
#
# 打印和聚合分开：聚合是纯函数（好单测），这里只负责排版。报告要能直接贴进
# 模块报告里，所以每个数旁边都写清它的分母/对照，不留裸百分比。

def _pct(x: float | None) -> str:
    return "不适用" if x is None else f"{x:.1%}"


BOM_CAVEAT = (
    "【Validation 的 BOM caveat】官方 Results/Validation_data_result.txt 开头有 UTF-8 BOM，"
    "官方 evaluate.py 用默认方式读，BOM 粘在第一条病案 ID 上，那条永远匹配不上、恒得 0 分——"
    "**满分上限是 48.9998/50，不是 50**。这个行为照原样保留（论文里 15 个模型的分大概率"
    "也是在同一份带 BOM 的文件上跑出来的，「修好」它只会让我们的数不可比）。"
    "所以下面 unmatched 里的第一条不是模型失分。见 data/SOURCES.md 第 14 条；"
    "想看修正后的诊断值加 --diagnose-bom。"
)


def print_report(analysis: dict) -> None:
    records_by_id = analysis["_records_by_id"]
    rows = analysis["rows"]
    n = len(rows)

    print(f"提交文件：{analysis['submission']}")
    print(f"split：{analysis['split']}　金标准来源：{analysis['gold_source']}"
          f"{'　（已剥 BOM，诊断值）' if analysis['bom_diagnosed'] else ''}")
    print(f"金标准 {analysis['n_gold']} 条　提交 {analysis['n_submitted']} 条　"
          f"实际计分 {n} 条")
    if analysis["unmatched"]:
        print(f"提交了但金标准匹配不上：{analysis['unmatched']}")
    if analysis["split"] == "Validation":
        print(BOM_CAVEAT)
    print("**本模式不发起任何 LLM 调用**：分数早就算出来了，这里只是重新聚合。")
    print()

    if not rows:
        print("没有任何可计分的记录——提交文件和金标准的病案 ID 完全对不上，"
              "先核对 --split 和提交文件是否配套。")
        return

    official = analysis["official_total"]
    if official is None:
        print("官方 automated_score：不可用（这个 split 没有 Results/*.txt，"
              "金标准在 JSON 里）——下面那个总分是我们按官方计分函数逐条加权算的，"
              "**不能直接跟论文里的 15 个模型比**。")
    else:
        print(f"官方 automated_score：{official:.4f} / {analysis['n_gold']}"
              f"　每条 {official / analysis['n_gold']:.4f}"
              "　← 引用这个数（可跟论文比）")
    print(f"我们逐条加权求和：{analysis['weighted_total']:.4f}"
          f" / {n}　每条 {analysis['weighted_per_record']:.4f}")
    if official is not None:
        delta = analysis["weighted_total"] - official
        note = ("差额与 unmatched 的条数相当，符合预期（BOM）"
                if abs(delta) <= len(analysis["unmatched"]) + 1e-6
                else "**差额超过 unmatched 能解释的范围，聚合这一层可能有问题，先查这个再看下面的分析**")
        print(f"两者差额：{delta:+.4f}（{note}）")
    print("分项总分（权重 T1 0.2 / T2 0.3 / T3 0.4 / T4 0.1）：")
    for i, weight in enumerate(TASK_WEIGHTS, start=1):
        key = f"task{i}"
        print(f"  {key}  总分 {analysis['task_totals'][key]:7.4f}"
              f"　每条 {analysis['task_per_record'][key]:.4f}"
              f"　加权后贡献 {weight * analysis['task_totals'][key]:7.4f}")
    print()

    print("完全对 / 部分对 / 完全错（判据是得分：1.0 / (0,1) / 0）：")
    for key, dist in analysis["correctness"].items():
        tail = "" if dist["discrete"] else "　← 自由文本项，这三档只当分布看，不是对错"
        print(f"  {key}  完全对 {dist['full']:3d}　部分对 {dist['partial']:3d}"
              f"　完全错 {dist['zero']:3d}　均分 {dist['mean']:.4f}{tail}")
    print()

    sel = analysis["selection"]
    print("选择数对比（分母是「记录 × 任务」对；恰好 = 数量对得上，不代表选对）：")
    for key in ("task2", "task3"):
        t = sel["by_task"][key]
        print(f"  {key}  多选 {t['n_over']}/{t['n_cells']}（{_pct(t['over_rate'])}）"
              f"　少选 {t['n_under']}/{t['n_cells']}（{_pct(t['under_rate'])}）"
              f"　恰好 {t['n_exact']}")
        print(f"         错选 {t['n_wrong_picks']} 个（{t['n_with_wrong_picks']} 条记录）"
              f"　漏选 {t['n_missed_picks']} 个（{t['n_with_missed_picks']} 条记录）")
    c = sel["combined"]
    print(f"  合计  多选率 {_pct(c['over_rate'])}　少选率 {_pct(c['under_rate'])}"
          f"（{c['n_over']} / {c['n_under']} / 恰好 {c['n_exact']}，共 {c['n_cells']} 对）")
    print()

    print("边际代价（用官方 score_proportional 实测的反事实，已乘任务权重换算成总分里的分）：")
    for key in ("task2", "task3"):
        t = sel["by_task"][key]
        print(f"  {key}  去掉错选可捞回 {t['recoverable_from_dropping_wrong']:+.4f}"
              f"　补上漏选可捞回 {t['recoverable_from_adding_missed']:+.4f}")
    v = sel["verdict"]
    print(f"  合计  去掉错选 {v['recoverable_from_dropping_wrong']:+.4f}"
          f"　补上漏选 {v['recoverable_from_adding_missed']:+.4f}")
    # 单个选项的代价：总量会被次数带偏，这两个数才直接回答"宁可不选"对不对
    per_wrong, per_missed = c["cost_per_wrong_pick"], c["cost_per_missed_pick"]
    print(f"  单个  一个错选 {'不适用（没有错选）' if per_wrong is None else f'{per_wrong:+.4f}'}"
          f"　一个漏选 {'不适用（没有漏选）' if per_missed is None else f'{per_missed:+.4f}'}"
          f"（{c['n_wrong_picks']} 个错选 / {c['n_missed_picks']} 个漏选）")
    if per_wrong is not None and per_missed is not None and per_missed > per_wrong:
        print("  ⚠ **一个漏选比一个错选更贵** —— prompt 里现在那句「拿不准的宁可不选」"
              "方向是反的，宁可不选反而在丢分。这条要跟上面的主因判据一起看："
              "主因判据说的是总量该往哪边改，这一行说的是那句话本身对不对。")
    print(f"  判据：{v['reason']}")
    print("  （这两个数不是「作弊后能拿多少分」，是定位失分方向的边际量。"
          "前提「多选有惩罚、少选没有」没有被假设成立，是在这里量出来的。）")
    print()

    print(f"失分最多的 {min(TOP_LOSS_N, n)} 条（每条满分 1.0，loss = 1 − 加权得分）：")
    for r in analysis["top_losses"]:
        rec = records_by_id.get(r["record_id"])
        clinical = (rec.clinical_data[:CLINICAL_DATA_CHARS] + "…") if rec else "（记录不在 split 里）"
        print(f"  --- {r['record_id']}　loss {r['loss']:.4f}"
              f"（T1 {r['task1']:.3f} / T2 {r['task2']:.3f} / T3 {r['task3']:.3f}"
              f" / T4 {r['task4']:.3f}）")
        print(f"      临床资料：{clinical}")
        for key, label in (("task2", "病机"), ("task3", "证型")):
            cell = r["choices"][key]
            print(f"      {label}　模型 {cell['chosen'] or '（未选）'}"
                  f"　金标准 {cell['gold'] or '（空）'}"
                  f"　错选 {cell['wrong'] or '无'}　漏选 {cell['missed'] or '无'}")
    print()

    for key, label in (("task2", "病机"), ("task3", "证型")):
        groups = analysis["groups"][key]
        print(f"按{label}分组的得分（每条记录的每个金标准选项各自成组，"
              f"所以同一条会计入多组；**n=1 的组是一条记录、不是证据**）：")
        for g in groups:
            print(f"  {g['mean']:.3f}　n={g['n']:2d}　{g['label']}")
        print()
