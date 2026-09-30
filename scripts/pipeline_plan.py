"""`scripts/run_pipeline.sh` 的步骤表解析、检索模式映射与成本估算。

流水线脚本（bash）和测试（python）都要用这几样东西：脚本按它 export
`RETRIEVER_MODE`、按它估价；测试按它断言步骤顺序和每一步的检索模式。所以它们
只在这里实现一次，bash 通过命令行来问：

    python3 -m scripts.pipeline_plan --calls "auto:eval/epsilon.json"   # 预估调用数
    python3 -m scripts.pipeline_plan --retriever-mode top3              # 要 export 的模式
    python3 -m scripts.pipeline_plan --unit-price full_context          # 每次调用多少钱
    python3 -m scripts.pipeline_plan --cost top3 1200                   # 这一步多少钱

## 预估调用数可以运行时现读

步骤表里「预估调用数」一格可以写 `auto:<文件>`，运行时从那份文件读出当前的值。
噪声地板 ε 那一步就是这样：它的调用数等于 `eval/epsilon.json` 里三段 `llm_calls`
之和，ε 每重跑一次这个数都会变；写死在步骤表里的数迟早跟文件脱节，而脱节不会报错。

    步骤号|检索模式|名称|预估调用数|人工确认|说明
    "5|top3|噪声地板 ε|auto:eval/epsilon.json|no|..."

## 每一步钉死检索模式

两套检索模式的单价相差一个数量级以上（top3 只带几条参考医案，full_context 带单医家
全量医案的前缀）。一步不声明模式，就会继承 `effective_mode()` 的默认值；默认值一变，
所有没钉模式的步骤都按另一套单价花钱，估算表却看不出任何变化。所以每一步都在步骤表
里声明自己的模式，运行时 export，成本也按它算。
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

# ε 的三个阶段。每个阶段各自记了 llm_calls，噪声地板那一步一次跑全部三段。
EPSILON_STAGES = ("epsilon_online", "epsilon_s2", "epsilon_extract")


def epsilon_llm_calls(data: dict[str, Any]) -> int:
    """`eval/epsilon.json` 的三段 llm_calls 之和。缺哪段按 0 计：中途中断的那次运行
    只花了已完成那几段的调用，按 0 补比按上一次的数补更接近事实。"""
    return sum(int((data.get(k) or {}).get("llm_calls") or 0) for k in EPSILON_STAGES)


# 允许被 `auto:` 引用的文件，以及从这份文件里数出调用数的读法。
# 不做成"任意路径 + 任意表达式"：能现读的文件就这几份，写死在这里，
# 步骤表里拼错路径会立刻报错，而不是静默拿到 0。
AUTO_SOURCES: dict[str, Callable[[dict[str, Any]], int]] = {
    "eval/epsilon.json": epsilon_llm_calls,
}


# ---------------------------------------------------------------------------
# 步骤表的结构（检索模式 / 单价）
# ---------------------------------------------------------------------------

#: 步骤表一行有几格。增减格子只改这里，解析器和流水线脚本共用这一处定义。
STEP_FIELDS = ("num", "mode", "name", "calls", "gate", "note")

#: 一步可以声明的检索模式。
#:
#:   top3          —— 检索少量参考医案进提示词的四种模式（dense/bm25/graph/hybrid）
#:                    的统称。E3/E4 的对照值与录制的 fixture 都在这一系下产生。
#:   full_context  —— 该医家全部医案进前缀缓存（产品默认）。
#:   n/a           —— 这一步不走检索层（离线抽取、蒸馏、纯本地校验）。
#:
#: n/a 的意思是"运行这一步时清掉 RETRIEVER_MODE"，不是"哪个模式都行"：
#: 留着上一步的值，等于让一步本不受模式影响的作业悄悄依赖上一步的设置。
STEP_MODES = ("top3", "full_context", "n/a")

#: `top3` 是一系四种模式的统称，真要 export 的是其中一个具体模式。映射只有这一处。
#: 选 hybrid：eval/RESULTS.md 里 top3 系的对照值是它跑出来的。
RETRIEVER_MODE_BY_STEP_MODE: dict[str, str | None] = {
    "top3": "hybrid",
    "full_context": "full_context",
    "n/a": None,
}

#: top3 系每次调用的估算均价（元）。量级估算，不是账单；实际花费以服务商账单为准。
TOP3_YUAN_PER_CALL = 0.0055

#: 估算参数：full_context 下一次 S3 调用带多少 token 的知识前缀（单医家全量医案的量级）。
#: 实际规模用 `python -m core.context_prefix --report` 查看。
FULL_CONTEXT_PREFIX_TOKENS = 180_000


def _out_tokens_per_call() -> int:
    """一次 S3 调用的输出 token 估算。复用蒸馏脚本的同名常量：同一个估算只定义一处，
    按实际用量校准时也只需要改那一处。"""
    from offline.distill_from_v4 import OUT_TOKENS_PER_CALL_ESTIMATE

    return OUT_TOKENS_PER_CALL_ESTIMATE


OUT_TOKENS_PER_CALL = _out_tokens_per_call()


def retriever_mode_for(step_mode: str) -> str | None:
    """步骤表里的模式 → 要 export 的 `RETRIEVER_MODE`；`n/a` 返回 None（表示清掉）。"""
    if step_mode not in RETRIEVER_MODE_BY_STEP_MODE:
        raise ValueError(f"未知的步骤模式 {step_mode!r}，可用：{STEP_MODES}")
    return RETRIEVER_MODE_BY_STEP_MODE[step_mode]


def unit_price_cny(step_mode: str) -> float:
    """这个模式下每次调用多少钱（高峰价）。两套单价只在这里定义。

    full_context 的单价是算出来的：命中缓存的知识前缀 + 一次输出，按 `core/usage.py`
    的价目表（全项目唯一的价格表）折算，价目表一改它跟着改。

    `n/a` 按 top3 的均价算，不是 0：药理层抽取和蒸馏不走检索层，但照样调用模型。
    "不走检索层"说的是这一步的输入怎么拼，"花不花钱"说的是它调不调模型，
    所以单价按"带不带知识前缀"分档，而不是按检索模式分档。
    """
    if step_mode == "full_context":
        from core.usage import cost_cny

        return cost_cny(hit_tokens=FULL_CONTEXT_PREFIX_TOKENS,
                        out_tokens=OUT_TOKENS_PER_CALL, peak=True)
    if step_mode in ("top3", "n/a"):
        return TOP3_YUAN_PER_CALL
    raise ValueError(f"未知的步骤模式 {step_mode!r}，可用：{STEP_MODES}")


def step_cost_cny(step_mode: str, calls: int) -> float:
    return unit_price_cny(step_mode) * max(0, int(calls))


_STEP_ROW_RE = re.compile(r'^\s*"([^"]+)"\s*$')


def parse_steps(script_text: str) -> list[dict[str, str]]:
    """把流水线脚本里的 `STEPS=( ... )` 解析成一串字典。

    流水线脚本和所有测试都读这一个解析器：各自写正则去抠步骤表的话，
    表里加一格就要同时改好几处，漏改的那处不会报错、只会少断言一件事。
    """
    start = script_text.index("STEPS=(")
    block = script_text[start:script_text.index("\n)", start)]
    rows = []
    for line in block.splitlines()[1:]:
        m = _STEP_ROW_RE.match(line)
        if not m:
            continue
        parts = m.group(1).split("|")
        if len(parts) != len(STEP_FIELDS):
            raise ValueError(
                f"步骤表这一行有 {len(parts)} 格，应该是 {len(STEP_FIELDS)} 格"
                f"（{'|'.join(STEP_FIELDS)}）：{m.group(1)[:60]}…")
        rows.append(dict(zip(STEP_FIELDS, parts)))
    return rows


def steps_in_order(script_text: str) -> list[dict[str, str]]:
    """步骤表按表中顺序（即执行顺序）返回；步骤号从 0 连续编号。"""
    return parse_steps(script_text)


def resolve_calls(cell: str, root: Path | None = None) -> int:
    """步骤表里那一格 → 整数。普通格子就是它本身的数；`auto:<路径>` 现读文件。"""
    cell = cell.strip()
    if not cell.startswith(AUTO_PREFIX):
        return int(cell)
    rel = cell[len(AUTO_PREFIX):]
    reader = AUTO_SOURCES.get(rel)
    if reader is None:
        raise ValueError(
            f"步骤表里写了 auto:{rel}，但 scripts/pipeline_plan.AUTO_SOURCES 里没有它的读法"
            f"（现有：{sorted(AUTO_SOURCES)}）")
    path = (root or REPO_ROOT) / rel
    if not path.exists():
        raise FileNotFoundError(f"{rel} 不存在——预估调用数 {cell} 要从它现读")
    return reader(json.loads(path.read_text(encoding="utf-8")))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--calls", help="步骤表里「预估调用数」那一格的原文")
    ap.add_argument("--retriever-mode", metavar="STEP_MODE",
                    help="步骤模式 → 要 export 的 RETRIEVER_MODE（n/a 打印空串）")
    ap.add_argument("--unit-price", metavar="STEP_MODE",
                    help="这个模式下每次调用多少钱")
    ap.add_argument("--cost", nargs=2, metavar=("STEP_MODE", "CALLS"),
                    help="这一步多少钱")
    args = ap.parse_args(argv)
    # 几个查询开关走同一条路：算不出来就非 0 退出并把原因打到 stderr，
    # 不给一个看起来正常的默认值（一个编出来的模式会让整步按错的口径跑）。
    try:
        if args.retriever_mode is not None:
            print(retriever_mode_for(args.retriever_mode) or "")
            return 0
        if args.unit_price is not None:
            print(f"{unit_price_cny(args.unit_price):.4f}")
            return 0
        if args.cost is not None:
            print(f"{step_cost_cny(args.cost[0], int(args.cost[1])):.1f}")
            return 0
        if args.calls is None:
            ap.error("要 --calls / --retriever-mode / --unit-price / --cost 之一")
        print(resolve_calls(args.calls))
    except (ValueError, FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        # stdout 照样给一个数：调用方（print_plan）拿它做算术，没有数会把整张清单
        # 打坏。给 0 而不是上一次的值，并把原因打到 stderr——清单上那一步显示
        # 0 次调用，一眼能看出不对。
        print(0)
        print(f"注意：步骤表 {args.calls} 解析失败，这一步的预估按 0 计：{exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
