"""给医案打「含十八反十九畏配伍」的标记。**对任意医案文件通用，不是给某一份
数据写死的。**

动机（总纲 2.5）：李可是敢用反药的名家（海藻与甘草同用，有学术依据），但本
系统的输出侧安全检查 `core.safety_output.check_incompatible` 会拦这类处方。
这批数据进训练集等于**教模型开反药，而输出侧又会拦住它**——模型学到的和系统
允许的自相矛盾，训练出来的东西在自己的安全层面前跑不通。

所以：打标记（这一步），训练导出时默认排除（`offline/export_sft.py` 的
`filter_incompatible_pairs`，加 `--include-incompatible` 才带上）。
**不删数据**——医案本身是真实的、有价值的，将来做"名家为什么敢用反药"这类
研究要用到它；排除只发生在训练导出那一环。

    python -m offline.tag_incompatible_cases --input cases.json            # 就地打标
    python -m offline.tag_incompatible_cases --input x.jsonl --out y.jsonl # 另存
    python -m offline.tag_incompatible_cases --input cases.json --dry-run  # 只报不写

退出码：0 = 跑完（**命中反药不是失败**，是这个脚本要找的东西）；
       1 = 文件读不了 / 格式不认。

## 判定只有一处实现

药对的匹配一律调 `core.safety_output.check_incompatible`——那是全项目唯一的
十八反十九畏实现（它内部再调 `normalize_for_incompat` 归一，认得"制附子"
"黑顺片"这类写法）。这里**不写任何字面比对**：同一个判断有两处实现，
以后改一边就会出现"安全层拦了、标记没标"或者反过来，而这两个结论必须一致
（CLAUDE.md「同一概念的匹配逻辑只能有一处实现」——这个项目在这堵墙上撞过
三次）。

## 支持 .json（数组）和 .jsonl（一行一条）

cases.json 是数组，新接进来的数据可能是 jsonl。按扩展名判断，**不按内容猜**：
猜错了会把一个数组当成一行 jsonl 解析失败，报出来的错跟真正的问题无关。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.safety_output import check_incompatible

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "cases.json"


def herbs_of(row: dict) -> list[str]:
    """一条医案记录里的药物列表。

    `CaseRecord.herbs` 是抽取时落下来的**原始写法**列表，直接喂给
    check_incompatible——它要的就是原始写法（返回冲突对时也用原始写法，
    "制附子"比"乌头"更有信息量，见那个函数的文档字符串）。

    字段缺失/不是列表时当空处理：新接进来的数据字段可能还没对齐，
    这里不该因为一条记录形状不对就崩掉整批。
    """
    herbs = row.get("herbs")
    return [h for h in herbs if isinstance(h, str)] if isinstance(herbs, list) else []


def tag_rows(rows: list[dict]) -> dict:
    """就地给每条记录打 has_incompatible_pair，并把命中的药对写进
    incompatible_pairs（新字段，只在命中时出现）。返回统计。

    `has_incompatible_pair` 是 `CaseRecord` 已有的字段（默认 False），
    `export_sft.filter_incompatible_pairs` 读的就是它——这个脚本只负责把它
    填对，不负责过滤。**具体是哪一对**也要记下来：只有一个布尔值的话，
    人工复核时要重新跑一遍才知道命中了什么，而复核是这批数据能不能用的前提。
    """
    stats = {
        "n_rows": len(rows), "n_tagged": 0, "n_without_herbs": 0,
        "pairs": {}, "tagged_case_ids": [],
    }
    for row in rows:
        herbs = herbs_of(row)
        if not herbs:
            stats["n_without_herbs"] += 1
        found = check_incompatible(herbs)
        row["has_incompatible_pair"] = bool(found)
        if found:
            # 药对排序后当键：同一对在不同医案里出现顺序可能不同（check_incompatible
            # 按方中书写顺序返回），不排序会把同一对统计成两种。
            row["incompatible_pairs"] = [list(p) for p in found]
            stats["n_tagged"] += 1
            stats["tagged_case_ids"].append(row.get("case_id"))
            for pair in found:
                key = " 反 ".join(sorted(pair))
                stats["pairs"][key] = stats["pairs"].get(key, 0) + 1
        else:
            # 之前跑过、这次不再命中（比如药物字段被修正过）要把旧字段清掉，
            # 不然会留下一个跟 has_incompatible_pair=False 矛盾的残留。
            row.pop("incompatible_pairs", None)
    return stats


def load_rows(path: Path) -> list[dict]:
    """.json = 数组，.jsonl = 一行一条。按扩展名判断，不按内容猜。"""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if path.suffix == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path} 是 .json 但顶层不是数组（医案文件应当是一个数组）")
        return data
    raise ValueError(f"不认识的扩展名 {path.suffix!r}：只支持 .json（数组）和 .jsonl（一行一条）")


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jsonl":
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    else:
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def format_report(stats: dict, path: Path) -> str:
    lines = [
        f"{path}：{stats['n_rows']} 条医案，其中 {stats['n_tagged']} 条含十八反/十九畏配伍"
        f"（{stats['n_without_herbs']} 条没有药物字段，按不含处理）",
    ]
    if stats["pairs"]:
        lines.append("命中的药对（按出现次数）：")
        for pair, n in sorted(stats["pairs"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {pair}　{n} 条")
        ids = stats["tagged_case_ids"][:10]
        lines.append(f"命中的 case_id（前 {len(ids)}）：{ids}")
        lines.append(
            "这些医案**不会被删**，只是训练导出时默认排除"
            "（offline/export_sft.py，--include-incompatible 才带上）——"
            "进了训练集等于教模型开反药，而输出侧安全检查又会拦住它，自相矛盾。"
        )
    else:
        lines.append("没有命中任何反药配伍。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给任意医案文件打十八反/十九畏标记")
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out", type=Path, default=None, help="默认就地改写 --input")
    ap.add_argument("--dry-run", action="store_true", help="只统计、不写文件")
    args = ap.parse_args(argv)

    if not args.input.exists():
        print(f"未找到 {args.input}", file=sys.stderr)
        return 1
    try:
        rows = load_rows(args.input)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"读不了 {args.input}：{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    stats = tag_rows(rows)
    print(format_report(stats, args.input))
    if args.dry_run:
        print("--dry-run：不写文件。")
        return 0
    out = args.out or args.input
    write_rows(out, rows)
    print(f"已写出 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
