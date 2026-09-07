"""V1 附属：盲评收集。读回 eval/mes/export.py 产出的评分表（评分人已经填完
每条的 winner 字段）和答案表，把 A/B 换回真实医家身份，统计胜负，用
eval/mcnemar.py 检验"某位医家系统性地被评得更好"是不是巧合而不是噪声。

见 eval/mes/export.py 模块文档字符串关于 MES 这个名字的说明。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.mcnemar import mcnemar_test

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ITEMS_PATH = ROOT / "eval" / "mes" / "items.json"
DEFAULT_ANSWER_KEY_PATH = ROOT / "eval" / "mes" / "answer_key.json"
DEFAULT_OUT_PATH = ROOT / "eval" / "mes" / "collected.json"


def collect_ratings(
    items: list[dict], answer_key: dict, physician_a: str, physician_b: str
) -> dict:
    """physician_a/physician_b 是要统计胜负的两位医家 id（不是评分表里的 A/B
    标签——评分表的 A/B 每条都随机对应到不同医家，这里传的是真实医家 id）。

    未评分（winner 不是 "A"/"B"/"tie"）的条目跳过，不强行当平局处理——那会
    把"评分人还没看到"和"评分人看了觉得一样好"混为一谈，稀释真实的平局信号。
    答案表里查不到的 item_id 同样跳过而不是崩掉：评分表和答案表可能来自不同
    批次的部分收集，数据不一致时报出跳过了几条，比让整个统计中断更有用。
    """
    n_total = len(items)
    n_rated = 0
    n_tie = 0
    n_missing_key = 0
    wins = {physician_a: 0, physician_b: 0}
    other_wins = 0  # 答案表里胜者不是这两位（比如以后接入第三位医家）时单独计，不吞掉

    for item in items:
        winner = item.get("winner")
        if winner not in ("A", "B", "tie"):
            continue
        n_rated += 1
        if winner == "tie":
            n_tie += 1
            continue
        key = answer_key.get(item["item_id"])
        if key is None:
            n_missing_key += 1
            continue
        winner_physician = key[winner]
        if winner_physician in wins:
            wins[winner_physician] += 1
        else:
            other_wins += 1

    mcnemar = mcnemar_test(wins[physician_a], wins[physician_b])
    n_unrated = n_total - n_rated
    return {
        "n_total": n_total, "n_rated": n_rated, "n_unrated": n_unrated,
        "n_tie": n_tie, "n_missing_answer_key": n_missing_key,
        "wins": wins, "other_wins": other_wins,
        "mcnemar": mcnemar,
        "note": (
            f"共 {n_total} 条待评，{n_rated} 条已评（{n_unrated} 条未评，不计入统计），"
            f"{n_tie} 条平局。{physician_a} 胜 {wins[physician_a]} 次，"
            f"{physician_b} 胜 {wins[physician_b]} 次。McNemar p={mcnemar['p_value']}"
            f"（{mcnemar['method']}；原假设：两位医家被评为更优的次数在总体上相等，"
            f"p 越小越有证据说明不是巧合）。"
            + (f" 另有 {n_missing_key} 条在答案表里查不到，已跳过。" if n_missing_key else "")
        ),
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="V1 附属：收集盲评结果，算胜负和显著性")
    ap.add_argument("--items-path", type=Path, default=DEFAULT_ITEMS_PATH)
    ap.add_argument("--answer-key-path", type=Path, default=DEFAULT_ANSWER_KEY_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--physician-a", default="ye_tianshi")
    ap.add_argument("--physician-b", default="wu_jutong")
    args = ap.parse_args(argv)

    if not args.items_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.items_path}。先跑 eval/mes/export.py 导出盲评表，"
            "评分人填完 winner 字段后再跑这个脚本。"
        )
    if not args.answer_key_path.exists():
        raise FileNotFoundError(f"未找到 {args.answer_key_path}（export.py 应该跟 items 一起产出它）。")

    items = json.loads(args.items_path.read_text(encoding="utf-8"))
    answer_key = json.loads(args.answer_key_path.read_text(encoding="utf-8"))

    result = collect_ratings(items, answer_key, args.physician_a, args.physician_b)
    print(result["note"])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
