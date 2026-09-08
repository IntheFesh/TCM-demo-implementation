"""V1 附属：盲评导出。

自动指标（分歧度/幻觉率/否决率，见 eval/run_eval.py）测的是"有没有幻觉""跟
基线比抖动多大"，测不了"这条辨证像不像话"——那需要人读。这个脚本把两位
医家对同一条主诉的 S3 结果匿名成 A/B（隐去医家身份和引用的医案 id——医案 id
前缀会暴露是叶天士还是吴鞠通，见 _s3_view 的取舍），导出成一份待评分表；
`eval/mes/collect.py` 负责在人工填完之后把身份换回来算胜负和显著性。

MES 这个名字沿用这一轮拿到的模块清单里的叫法，具体全称这一轮没有拿到 V3
计划文档原文（跟 X3/K3b 遇到的情况一样），这里是按"盲评导出 + 收集"这个
功能描述自己设计的格式，不是照抄一份不确定的规范。
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_QUERIES_PATH = ROOT / "tests" / "queries.txt"
DEFAULT_ITEMS_PATH = ROOT / "eval" / "mes" / "items.json"
DEFAULT_ANSWER_KEY_PATH = ROOT / "eval" / "mes" / "answer_key.json"

# 固定种子只是为了"同一批 consult 结果重新导出一次，A/B 顺序不变"这个可复现性，
# 不是为了防评分人猜——如果评分人真的想通过内容风格猜出医家身份，种子挡不住，
# 这不是这个脚本要解决的问题。
DEFAULT_SEED = 20260907


def _s3_view(s3) -> dict:
    """给评分人看的字段。不包括 cited_case_ids——引用的医案 id 前缀
    （ye_tianshi-xxx / wu_jutong-xxx）会直接暴露医家身份，盲评就失去意义了。"""
    return {
        "syndrome": s3.syndrome,
        "reasoning": s3.reasoning,
        "treatment_principle": s3.treatment_principle,
        "formula": s3.formula,
        "herbs": s3.herbs,
    }


def build_blind_items(
    queries: list[str], consult_results: list[dict], seed: int = DEFAULT_SEED
) -> tuple[list[dict], dict]:
    """返回 (待评分表, 答案表)。答案表单独存，不跟评分表放一起——评分人不该
    在评分过程中能看到它。

    只有产出了方药、且是两位医家两两对照的那些查询才进盲评表：被安全否决、
    信息不足、或注册医家数不是 2 的（未来接入第三位医家后）都跳过——盲评
    比的是"这两条哪个更好"，没有两条就没什么可比。"""
    if len(queries) != len(consult_results):
        raise ValueError(
            f"查询数和结果数不一致：{len(queries)} vs {len(consult_results)}"
        )
    rng = random.Random(seed)
    items: list[dict] = []
    answer_key: dict = {}
    for i, (query, result) in enumerate(zip(queries, consult_results)):
        if result["rejected"] or result["insufficient"]:
            continue
        physician_results = result.get("results") or []
        if len(physician_results) != 2:
            continue
        item_id = f"item-{i:03d}"
        p0, p1 = physician_results[0], physician_results[1]
        swap = rng.random() < 0.5
        first, second = (p1, p0) if swap else (p0, p1)
        items.append({
            "item_id": item_id,
            "query": query,
            "A": _s3_view(first["s3"]),
            "B": _s3_view(second["s3"]),
            "winner": None,  # 评分人填 "A" / "B" / "tie"
        })
        answer_key[item_id] = {"A": first["physician"], "B": second["physician"]}
    return items, answer_key


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="V1 附属：导出盲评表（需要真实 LLM 跑 consult）")
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH)
    ap.add_argument("--out-items", type=Path, default=DEFAULT_ITEMS_PATH)
    ap.add_argument("--out-answer-key", type=Path, default=DEFAULT_ANSWER_KEY_PATH)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    queries = [
        line.strip() for line in args.queries_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if args.limit is not None:
        queries = queries[: args.limit]

    if args.dry_run:
        print(f"--dry-run：预估调用数 ≈ {len(queries) * 6}（{len(queries)} 条查询），不真的调用")
        return

    from core.chain import consult_many

    results, failures = consult_many(queries)
    if failures:
        print(f"注意：{len(failures)}/{len(queries)} 条主诉失败，盲评表里不含它们")
    ok = [(q, r) for q, r in zip(queries, results) if r is not None]
    items, answer_key = build_blind_items([q for q, _ in ok], [r for _, r in ok], seed=args.seed)

    args.out_items.parent.mkdir(parents=True, exist_ok=True)
    args.out_items.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out_answer_key.write_text(json.dumps(answer_key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"共 {len(queries)} 条查询，{len(items)} 条进入盲评表（其余被拦截/信息不足/医家数不为 2，跳过）")
    print(f"已写出 {args.out_items}（给评分人）和 {args.out_answer_key}（评分完成后给 collect.py，不要给评分人看）")


if __name__ == "__main__":
    main()
