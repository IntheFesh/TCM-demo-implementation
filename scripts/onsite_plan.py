"""上机剧本 `scripts/run_onsite.sh` 段表里「预估调用数」这一格的解析。

## 为什么要一个模块来解析一个数字

段 4（噪声地板 ε）的预估调用数不是拍脑袋的，它等于 `eval/epsilon.json` 里三段
（online / s2 / extract）`llm_calls` 之和。R10 之前这个数以 **215** 的形式同时写在
三处：剧本的段表、段表那一行的说明文字、以及 `tests/test_run_onsite.py` 里拿它跟
文件比的断言。ε 一重跑（AutoDL 实测 212 次）三处全过期，测试变红，而红的那条
测试**本身没错**——它比的两个数一个来自文件、一个写死在剧本里，文件变了剧本没变。

修法不是把 215 换成 212（下次重跑还会红），而是让剧本**现读文件**：段表那一格写
`auto:eval/epsilon.json`，跑的时候解析成当时文件里的真实值。剧本（bash）和测试
（python）都调这里同一个 `resolve_calls`，不各写一套——CLAUDE.md「同一概念的匹配
逻辑只能有一处实现」。

    段号|名称|预估调用数|人工卡点|说明
    "4|R1 验收：噪声地板 ε|auto:eval/epsilon.json|no|..."

bash 侧：`python3 -m scripts.onsite_plan --calls "auto:eval/epsilon.json"`。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
AUTO_PREFIX = "auto:"

# ε 的三个阶段。每个阶段自己记了 llm_calls，段 4 一次跑的是全部三段。
EPSILON_STAGES = ("epsilon_online", "epsilon_s2", "epsilon_extract")


def epsilon_llm_calls(data: dict[str, Any]) -> int:
    """`eval/epsilon.json` 的三段 llm_calls 之和。缺哪段按 0 计——中途挂掉的那次
    跑确实只花了跑到的那几段的钱，按 0 补比按上一次的数补更接近事实。"""
    return sum(int((data.get(k) or {}).get("llm_calls") or 0) for k in EPSILON_STAGES)


# 允许被 `auto:` 引用的文件，以及"从这份文件里数出调用数"的读法。
# 不做成"任意路径 + 任意 jq 表达式"：能被现读的文件就这几份，写死在这里，
# 剧本里拼错路径会立刻报错而不是静默拿到 0。
AUTO_SOURCES: dict[str, Callable[[dict[str, Any]], int]] = {
    "eval/epsilon.json": epsilon_llm_calls,
}


def resolve_calls(cell: str, root: Path | None = None) -> int:
    """段表里那一格 → 整数。普通格子就是它本身的数；`auto:<路径>` 现读文件。"""
    cell = cell.strip()
    if not cell.startswith(AUTO_PREFIX):
        return int(cell)
    rel = cell[len(AUTO_PREFIX):]
    reader = AUTO_SOURCES.get(rel)
    if reader is None:
        raise ValueError(
            f"段表里写了 auto:{rel}，但 scripts/onsite_plan.AUTO_SOURCES 里没有它的读法"
            f"（现有：{sorted(AUTO_SOURCES)}）")
    path = (root or REPO_ROOT) / rel
    if not path.exists():
        raise FileNotFoundError(f"{rel} 不存在——段 {cell} 的预估调用数就是从它现读的")
    return reader(json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--calls", required=True, help="段表里「预估调用数」那一格的原文")
    args = ap.parse_args(argv)
    try:
        print(resolve_calls(args.calls))
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        # **stdout 照样给一个数**：调用方（print_plan）拿它做算术，没数会把整张清单
        # 打坏。给 0 而不是给上一次的值，并且把原因喊到 stderr——清单上那一段显示
        # 0 次调用，人一眼能看出不对。
        print(0)
        print(f"⚠ 段表 {args.calls} 解析失败，这一段的预估按 0 计：{exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
