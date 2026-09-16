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

    段号|执行序|检索模式|名称|预估调用数|人工卡点|说明
    "4|5|top3|R1 验收：噪声地板 ε|auto:eval/epsilon.json|no|..."

bash 侧：`python3 -m scripts.onsite_plan --calls "auto:eval/epsilon.json"`。

## R28：这个模块又多管两件事，理由是同一条

段表里新增的 `执行序` 和 `检索模式` 两格，以及"这一段多少钱"的算法，全都放在这里，
**因为 bash 和 python 两侧都要用**：剧本按它排序、按它 export、按它估价；
测试按它断言"段 9 真的排在段 3 之前""段 7 真的钉成 top3"。各写一套的后果这一轮
刚撞过一次真的——R21 把 `effective_mode()` 的默认值换成 `full_context`，
剧本里没有任何一段钉模式，于是段 4/6/7/8 全部继承新默认，
而两套单价差 29 倍（¥0.0055 vs ¥0.16），段 7 的 1200 次从清单标的 ¥6.6 变成约 ¥192。
**改一个默认值就打穿了预算表**，因为"这一段用哪个模式"这件事当时没有任何地方写着。
"""
from __future__ import annotations

import argparse
import json
import re
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


# ---------------------------------------------------------------------------
# R28：段表的结构（执行序 / 检索模式 / 单价）
# ---------------------------------------------------------------------------

#: 段表一行有几格。多一格少一格都要在这里改，解析和剧本各自散着数格子迟早对不上。
SEGMENT_FIELDS = ("num", "order", "mode", "name", "calls", "gate", "note")

#: 一段可以声明的检索模式。
#:
#:   top3          —— R21 之前的四种检索（dense/bm25/graph/hybrid）的统称。
#:                    这几段的历史数据、E3/E4 旧基线、录好的 fixture 全是这一系的。
#:   full_context  —— R21 之后的默认：该医家全部医案进前缀缓存。
#:   n/a           —— 这一段不走检索层（离线抽取、蒸馏、纯本地校验）。
#:
#: **n/a 不是"随便哪个模式都行"**：它是"这一段跑的时候要把 RETRIEVER_MODE 清掉"。
#: 留着上一段的值等于让一段不该受模式影响的活儿，悄悄依赖上一段的设置。
SEGMENT_MODES = ("top3", "full_context", "n/a")

#: `top3` 是一系四种的统称，真要 export 的是其中一个具体模式。**映射只有这一处**。
#: 选 hybrid 而不是别的三种：RESULTS.md 里 top3 系的历史数字是它跑出来的。
RETRIEVER_MODE_BY_SEGMENT_MODE: dict[str, str | None] = {
    "top3": "hybrid",
    "full_context": "full_context",
    "n/a": None,
}

#: top3 系每次调用的均价。来源是本仓库唯一一次成本记录（README 里 record_fixtures
#: 那条「约 272 次 ¥1.5」）。⚠ 量级估算、不是账单。
TOP3_YUAN_PER_CALL = 0.0055

#: full_context 下一次 S3 调用要带多少 token 的知识前缀（单医家全量医案，R21 实测量级）。
FULL_CONTEXT_PREFIX_TOKENS = 180_000


def _out_tokens_per_call() -> int:
    """一次 S3 调用的输出 token 估算。**问 R26 那个常量，不再估一遍**——
    它旁边写着"这是假设不是实测"以及怎么按真机值替掉，两处各写一个数的话，
    真机回填的时候一定只回填一处。"""
    from offline.distill_from_v4 import OUT_TOKENS_PER_CALL_ESTIMATE

    return OUT_TOKENS_PER_CALL_ESTIMATE


OUT_TOKENS_PER_CALL = _out_tokens_per_call()


def retriever_mode_for(segment_mode: str) -> str | None:
    """段表里的模式 → 要 export 的 `RETRIEVER_MODE`；`n/a` 返回 None（表示清掉）。"""
    if segment_mode not in RETRIEVER_MODE_BY_SEGMENT_MODE:
        raise ValueError(f"未知的段模式 {segment_mode!r}，可用：{SEGMENT_MODES}")
    return RETRIEVER_MODE_BY_SEGMENT_MODE[segment_mode]


def unit_price_cny(segment_mode: str) -> float:
    """这个模式下每次调用多少钱（高峰价）。**两套单价只在这里定义。**

    full_context 那个不是拍的：命中输入 18 万 token + 一次输出，按
    `core/usage.py` 的价目表（全项目唯一的价格表）算出来。价目表一改它跟着改。

    **`n/a` 按 top3 的均价算，不是 0。** 这一条是写的时候先写错、
    `--dry-run` 一跑就看出来的：段 5（药理层抽取，2181 次）和段 10（蒸馏，350 次）
    的检索模式确实是 n/a，但它们照样在调模型——按 0 算出来的清单会告诉人
    "这两段不要钱"，而段 5 本来就是全剧本最贵的一段。
    **"不走检索层"和"不花钱"是两件事**：前者说的是这一段的输入怎么拼，
    后者说的是它调不调模型。两者共用一个字段就会把人读错，所以价格在这里
    按"带不带知识前缀"分档，而不是按检索模式分档。
    """
    if segment_mode == "full_context":
        from core.usage import cost_cny

        return cost_cny(hit_tokens=FULL_CONTEXT_PREFIX_TOKENS,
                        out_tokens=OUT_TOKENS_PER_CALL, peak=True)
    if segment_mode in ("top3", "n/a"):
        return TOP3_YUAN_PER_CALL
    raise ValueError(f"未知的段模式 {segment_mode!r}，可用：{SEGMENT_MODES}")


def segment_cost_cny(segment_mode: str, calls: int) -> float:
    return unit_price_cny(segment_mode) * max(0, int(calls))


_SEGMENT_ROW_RE = re.compile(r'^\s*"([^"]+)"\s*$')


def parse_segments(script_text: str) -> list[dict[str, str]]:
    """把剧本里的 `SEGMENTS=( ... )` 解析成一串字典。

    **剧本和测试读同一个解析器**：以前每个测试文件各写一个正则去抠段表，
    加一格就要同时改四处正则，而漏改的那处不会报错、只会少断言一件事。
    """
    start = script_text.index("SEGMENTS=(")
    block = script_text[start:script_text.index("\n)", start)]
    rows = []
    for line in block.splitlines()[1:]:
        m = _SEGMENT_ROW_RE.match(line)
        if not m:
            continue
        parts = m.group(1).split("|")
        if len(parts) != len(SEGMENT_FIELDS):
            raise ValueError(
                f"段表这一行有 {len(parts)} 格，应该是 {len(SEGMENT_FIELDS)} 格"
                f"（{'|'.join(SEGMENT_FIELDS)}）：{m.group(1)[:60]}…")
        rows.append(dict(zip(SEGMENT_FIELDS, parts)))
    return rows


def segments_in_execution_order(script_text: str) -> list[dict[str, str]]:
    """按执行序排好的段。**段号不参与排序**——那正是这一轮要解耦的东西。"""
    return sorted(parse_segments(script_text), key=lambda r: int(r["order"]))


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
    ap.add_argument("--calls", help="段表里「预估调用数」那一格的原文")
    ap.add_argument("--retriever-mode", metavar="SEGMENT_MODE",
                    help="段模式 → 要 export 的 RETRIEVER_MODE（n/a 打印空串）")
    ap.add_argument("--unit-price", metavar="SEGMENT_MODE",
                    help="这个模式下每次调用多少钱")
    ap.add_argument("--cost", nargs=2, metavar=("SEGMENT_MODE", "CALLS"),
                    help="这一段多少钱")
    args = ap.parse_args(argv)
    # 三个查询型开关走同一条路：算不出来就非 0 退出并喊到 stderr，
    # 不给一个看起来正常的默认值（一个编出来的模式会让整段按错的口径跑）。
    try:
        if args.retriever_mode is not None:
            print(retriever_mode_for(args.retriever_mode) or "")
            return 0
        if args.unit_price is not None:
            print(f"{unit_price_cny(args.unit_price):.4f}")
            return 0
        if args.cost is not None:
            print(f"{segment_cost_cny(args.cost[0], int(args.cost[1])):.1f}")
            return 0
        if args.calls is None:
            ap.error("要 --calls / --retriever-mode / --unit-price / --cost 之一")
        print(resolve_calls(args.calls))
    except (ValueError, FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        # **stdout 照样给一个数**：调用方（print_plan）拿它做算术，没数会把整张清单
        # 打坏。给 0 而不是给上一次的值，并且把原因喊到 stderr——清单上那一段显示
        # 0 次调用，人一眼能看出不对。
        print(0)
        print(f"⚠ 段表 {args.calls} 解析失败，这一段的预估按 0 计：{exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
