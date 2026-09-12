"""V1 附属：盲评导出。

自动指标（分歧度/幻觉率/否决率，见 eval/run_eval.py）测的是"有没有幻觉""跟
基线比抖动多大"，测不了"这条辨证像不像话"——那需要人读。这个脚本把各位
医家对同一条主诉的 S3 结果匿名成 A/B/C/...（隐去医家身份和引用的医案 id——
医案 id 前缀会暴露是哪位医家，见 _s3_view 的取舍），导出成一份待评分表；
`eval/mes/collect.py` 负责在人工填完之后把身份换回来算胜负和显著性。

**列数不写死。** 早前只有叶天士/吴鞠通两位医家时这里是 A/B 两列；张锡纯
注册进 core.physicians.PHYSICIANS 之后（bef7415），三位医家的盲评完全可以
做，只是这个脚本原来写死了"len(physician_results) != 2 就跳过"——同一类
问题这个项目已经在 _pin_two_physicians、eval/run_eval.py 的 dry-run 估算
上踩过，这次改成从 len(PHYSICIANS) 现读，下次再加医家不用再改这个文件。

MES 这个名字沿用这一轮拿到的模块清单里的叫法，具体全称这一轮没有拿到 V3
计划文档原文（跟 X3/K3b 遇到的情况一样），这里是按"盲评导出 + 收集"这个
功能描述自己设计的格式，不是照抄一份不确定的规范。

**评分维度**：现在的评分表每条只有一个整体 winner（A/B/.../tie），没有
拆成"辨证合理/方药对证/推理可信"这类分维度打分——仓库里从当初到现在都是
这一个字段，不是这一轮改动改掉的（改动前后逐字比对过）。如果需要分维度
评分，得新增字段（比如 winner 改成 {dimension: choice} 的字典），这是一个
需要单独确认的格式变更，这一轮没有擅自加。
"""
from __future__ import annotations

import argparse
import json
import random
import string
from pathlib import Path

from core.physicians import PHYSICIANS

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_QUERIES_PATH = ROOT / "tests" / "queries.txt"
DEFAULT_ITEMS_PATH = ROOT / "eval" / "mes" / "items.json"
DEFAULT_ANSWER_KEY_PATH = ROOT / "eval" / "mes" / "answer_key.json"

# 固定种子只是为了"同一批 consult 结果重新导出一次，A/B/... 顺序不变"这个
# 可复现性，不是为了防评分人猜——如果评分人真的想通过内容风格猜出医家身份，
# 种子挡不住，这不是这个脚本要解决的问题。
DEFAULT_SEED = 20260907


def _s3_view(s3) -> dict:
    """给评分人看的字段。不包括 cited_case_ids——引用的医案 id 前缀
    （ye_tianshi-xxx / wu_jutong-xxx / zhang_xichun-xxx）会直接暴露医家身份，
    盲评就失去意义了。"""
    return {
        "syndrome": s3.syndrome,
        "reasoning": s3.reasoning,
        "treatment_principle": s3.treatment_principle,
        "formula": s3.formula,
        "herbs": s3.herbs,
    }


def build_blind_items(
    queries: list[str], consult_results: list[dict], seed: int = DEFAULT_SEED,
) -> tuple[list[dict], dict, dict]:
    """返回 (待评分表, 答案表, 跳过原因计数)。答案表单独存，不跟评分表放在
    一起——评分人不该在评分过程中能看到它。

    只有产出了方药、且医家数正好等于当前注册表 len(PHYSICIANS) 的查询才进
    盲评表：被安全否决、信息不足、或某次 consult 返回的医家数跟注册表对不上
    （比如检索/生成中途出了别的问题，实际只拿到部分医家的结果）的都跳过——
    盲评比的是"这几条哪个更好"，缺一个就不是完整的对照组，硬凑会让"A 组
    永远是那几位医家"这种偏差混进去。

    跳过原因分三类分别计数（拦截 / 信息不足 / 医家数不对），不合并成一句
    "其余跳过"——医家数不对这一类此前只可能来自测试构造的边界情况，现在
    医家数从 2 变 3 之后，如果哪个环节没跟着适配（比如某个检索模式只支持
    两位医家），这一类计数会不会长成非零，是能不能及时发现类似 bug 的唯一
    信号，合并掉就看不出来了。
    """
    if len(queries) != len(consult_results):
        raise ValueError(
            f"查询数和结果数不一致：{len(queries)} vs {len(consult_results)}"
        )
    expected_n = len(PHYSICIANS)
    letters = string.ascii_uppercase[:expected_n]

    rng = random.Random(seed)
    items: list[dict] = []
    answer_key: dict = {}
    skip_counts = {"rejected": 0, "insufficient": 0, "wrong_physician_count": 0}

    for i, (query, result) in enumerate(zip(queries, consult_results)):
        if result["rejected"]:
            skip_counts["rejected"] += 1
            continue
        if result["insufficient"]:
            skip_counts["insufficient"] += 1
            continue
        physician_results = result.get("results") or []
        if len(physician_results) != expected_n:
            skip_counts["wrong_physician_count"] += 1
            continue

        item_id = f"item-{i:03d}"
        order = list(range(expected_n))
        rng.shuffle(order)
        shuffled = [physician_results[j] for j in order]

        item = {"item_id": item_id, "query": query}
        key_entry = {}
        for letter, pr in zip(letters, shuffled):
            item[letter] = _s3_view(pr["s3"])
            key_entry[letter] = pr["physician"]
        item["winner"] = None  # 评分人填字母（A/B/.../这批的最后一个字母）之一，或 "tie"
        items.append(item)
        answer_key[item_id] = key_entry

    return items, answer_key, skip_counts


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
        # S1+S2 两次 + 每位医家 S3 一次（配伍禁忌可能重开）的量级估计，
        # 跟 eval/run_eval.py 的 base_calls_per_query 同一个公式——按
        # len(PHYSICIANS) 现算，不写死"2 位医家"（同一类教训见该文件）。
        per_query = 2 + len(PHYSICIANS) * 2
        print(f"--dry-run：预估调用数 ≈ {len(queries) * per_query}"
              f"（{len(queries)} 条查询 × ~{per_query} 次/条，{len(PHYSICIANS)} 位医家），不真的调用")
        return

    from core.chain import consult_many

    results, failures = consult_many(queries)
    if failures:
        print(f"注意：{len(failures)}/{len(queries)} 条主诉失败，盲评表里不含它们")
    ok = [(q, r) for q, r in zip(queries, results) if r is not None]
    items, answer_key, skip_counts = build_blind_items(
        [q for q, _ in ok], [r for _, r in ok], seed=args.seed
    )

    args.out_items.parent.mkdir(parents=True, exist_ok=True)
    args.out_items.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    args.out_answer_key.write_text(json.dumps(answer_key, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"共 {len(queries)} 条查询，{len(items)} 条进入盲评表（{len(PHYSICIANS)} 位医家并排）。"
        f"跳过：因安全拦截 {skip_counts['rejected']} 条，因信息不足 {skip_counts['insufficient']} 条，"
        f"因医家数不等于 {len(PHYSICIANS)} {skip_counts['wrong_physician_count']} 条。"
    )
    print(f"已写出 {args.out_items}（给评分人）和 {args.out_answer_key}（评分完成后给 collect.py，不要给评分人看）")


if __name__ == "__main__":
    main()
