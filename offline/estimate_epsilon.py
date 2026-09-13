"""E：噪声地板估算。

现在分歧指标报的是"药物 Jaccard 距离 0.53"，但同一条主诉重复跑 `consult()`，
结果本身就会变（这是模型 temperature=0 之外仍然存在的抖动源：检索到的参考医案
措辞、S2 证素的表述、模型输出的具体用药顺序都可能在多次调用间轻微不同）。
没有噪声地板，任何"这个 Jaccard 数 = 分歧"的说法都可能有一半是模型抖动而不是
真实的辨证差异——这是这个项目所有对外数字里最脆弱的一个（CLAUDE.md「任何数字
都必须带对照」）。

三层噪声，从下游到上游：

  epsilon_s2      S2（证素推断）的抖动。**隔离一个变量**：同一个 S1 反复喂给
                  `infer_elements()`，不重新跑 S1——这样测出来的纯粹是 S2 这一步
                  的噪声。理论上它应当是 epsilon_online 的下界：S2 抖了，S3 必然
                  跟着抖；反过来不一定（S3 自己也会引入额外抖动）。
  epsilon_online  完整 `consult()` 一位医家开出来的方子抖动。这是 divergence 指标
                  真正要拿来做对照的那个数。
  epsilon_extract S0（医案结构化抽取）在同一段原文上重复跑的抖动，独立于
                  consult() 链路，衡量抽取环节本身的噪声——它不影响在线推理，
                  但影响 cases.json 本身的可信度。

三层都用同一个度量：N 次重复的输出集合两两算 Jaccard 距离，取值分布的
均值/p50/p95（`core/setstats.py`，全项目唯一一处 Jaccard 距离实现）。

用法：
    python -m offline.estimate_epsilon --dry-run           # 先看要花多少次调用
    python -m offline.estimate_epsilon                     # 全量：10 条主诉 × 3 次
    python -m offline.estimate_epsilon --limit 2 --n-repeats 2   # 小样本冒烟

产出 eval/epsilon.json，被 core/chain.py 的 divergence 读取（文件不存在时
divergence["epsilon_online"] 就是 None，不抛异常——demo 在没跑过 ε 时也要能用）。
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

from core.batch import classify_llm_failure, warn_if_failure_rate_high
from core.chain import consult, infer_elements, normalize
from core.herbs import normalized_herb_set
from core.llm import get_llm
from core.physicians import PHYSICIANS
from core.safety import check_safety
from core.setstats import aggregate_stats, pairwise_jaccard_stats

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES_PATH = ROOT / "tests" / "queries.txt"
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_OUT_PATH = ROOT / "eval" / "epsilon.json"
DEFAULT_N_REPEATS = 3
DEFAULT_N_SAMPLES = 10
# 固定种子：epsilon_extract 的抽样要可复现（同一份 cases.json 重跑应当抽到同样
# 10 条），不是为了"随机好看"而随机。
SAMPLE_SEED = 20260907


def _empty_stats() -> dict:
    return {"mean": None, "p50": None, "p95": None}


def estimate_epsilon_online(
    queries: list[str], n_repeats: int = DEFAULT_N_REPEATS, consult_fn=None
) -> dict:
    """跑完整 `consult()` N 次，按医家汇总用药集合的两两 Jaccard 距离。

    `use_react=False, ask_fn=None` 显式固定：ReAct 和追问都是额外的抖动源
    （分别在 core/react.py、core/followup.py 里已经有自己的行为记录），这里只
    测最基础路径的噪声，不把它们的抖动混进 epsilon_online。V1 的 E9（ReAct 消融）
    要单独测 use_react 开关本身的影响，跟这里是两个问题。

    被安全否决拦截、或信息不足（S2 没推出任何证素）的重复不产出用药，直接跳过；
    一条主诉的全部重复都被拦截/信息不足时，整条主诉跳过并记录原因——不能当成
    "噪声为 0"。

    单次重复的 consult_fn 调用本身失败（LLMError 等，真实 API 会抖动/限流）
    不能让整条主诉、更不能让整批噪声估算停下——这一层原来没有任何异常捕获，
    跟 estimate_epsilon_extract 早就在用的"单条失败记录、continue，不崩"
    是同一个模式，只是没有补过来。**失败的重复要从这条主诉的有效重复次数里
    剔除，不能当成"这次抖动结果是空集"去参与 Jaccard 计算**：失败的重复
    根本不往 herb_sets_by_physician 里追加，pairwise_jaccard_stats 只在有效
    集合 >=2 时才出数，分母天然只数真正跑成的次数。n_call_failures 单独
    计数，跟"被安全否决"、"信息不足"两种业务性跳过原因分开报——一个是
    consult() 没跑成，一个是 consult() 跑成了、结果如实是"拒答/信息不足"，
    混在一起会让"这条主诉的噪声数据够不够"这件事看不清楚。
    """
    consult_fn = consult_fn or (lambda c: consult(c, use_react=False, ask_fn=None))

    all_distances: list[float] = []
    by_physician_distances: dict[str, list[float]] = {}
    per_query: list[dict] = []
    llm_calls = 0
    n_call_failures_total = 0

    for complaint in queries:
        herb_sets_by_physician: dict[str, list[set]] = {}
        n_rejected = 0
        n_insufficient = 0
        n_call_failures = 0

        for _ in range(n_repeats):
            try:
                outcome = consult_fn(complaint)
            except Exception as e:  # noqa: BLE001 - 单次重复失败不能拖累这条主诉的其它重复/其它主诉
                n_call_failures += 1
                print(
                    f"[estimate_epsilon_online]「{complaint}」一次重复调用失败："
                    f"{classify_llm_failure(e)}: {e}", file=sys.stderr,
                )
                continue
            llm_calls += (outcome.get("manifest") or {}).get("llm_calls", 0)
            if outcome["rejected"]:
                n_rejected += 1
                continue
            if outcome.get("insufficient"):
                n_insufficient += 1
                continue
            for r in outcome["results"]:
                herbs = normalized_herb_set(r["s3"].herbs)
                herb_sets_by_physician.setdefault(r["physician"], []).append(herbs)

        n_call_failures_total += n_call_failures
        n_completed = n_repeats - n_call_failures  # 真正跑完的重复次数，不是 n_repeats
        if n_completed == 0:
            per_query.append({
                "query": complaint, "skipped": True, "reason": "全部重复调用失败",
                "n_call_failures": n_call_failures,
            })
            continue
        if n_rejected == n_completed:
            per_query.append({
                "query": complaint, "skipped": True, "reason": "全部重复被安全否决拦截",
                "n_call_failures": n_call_failures,
            })
            continue
        if n_insufficient == n_completed:
            per_query.append({
                "query": complaint, "skipped": True, "reason": "全部重复信息不足未产出结论",
                "n_call_failures": n_call_failures,
            })
            continue

        q_record: dict = {
            "query": complaint, "skipped": False,
            "n_rejected": n_rejected, "n_insufficient": n_insufficient,
            "n_call_failures": n_call_failures, "n_completed": n_completed,
            "by_physician": {},
        }
        for physician, sets_ in herb_sets_by_physician.items():
            stats = pairwise_jaccard_stats(sets_)
            q_record["by_physician"][physician] = stats
            if stats:
                all_distances.extend(stats["values"])
                by_physician_distances.setdefault(physician, []).extend(stats["values"])
        per_query.append(q_record)

    overall = aggregate_stats(all_distances) or _empty_stats()
    by_physician = {p: (aggregate_stats(d) or _empty_stats()) for p, d in by_physician_distances.items()}
    return {
        **overall,
        "by_physician": by_physician,
        "per_query": per_query,
        "n_queries": len(queries),
        "n_queries_used": sum(1 for q in per_query if not q["skipped"]),
        "n_repeats": n_repeats,
        "n_call_failures": n_call_failures_total,
        "n_attempts": len(queries) * n_repeats,
        "llm_calls": llm_calls,
    }


def estimate_epsilon_s2(queries: list[str], n_repeats: int = DEFAULT_N_REPEATS) -> dict:
    """S2 的抖动，S1 只跑一次（跟 CLAUDE.md「S1 全局只跑一次」的约束一致），
    同一个 S1 反复喂给 `infer_elements()`。

    两处调用都要失败容忍，波及范围不一样：`normalize(complaint)`（S1，只跑
    一次）失败，这条主诉连 S2 重复实验的输入都没有，直接整条跳过，原因单独
    标"S1 调用失败"——跟 check_safety 拦截分开记（一个是调用没跑成，一个是
    调用跑成了、内容触发了安全否决，两件不同的事）。`infer_elements(s1)`
    （S2，重复 n_repeats 次）单次失败：不影响已经算出来的 S1、不影响这条
    主诉的其它重复，失败的这一次直接不进 elem_sets——跟 estimate_epsilon_online
    同一个道理，分母只数真正跑成的次数，不是 n_repeats。全部重复都失败时
    单独标记跳过（而不是留一个 stats=None 混在"跑成了但凑不出两个集合"
    这种正常情况里，看不出是真失败还是数据本来就少）。
    """
    all_distances: list[float] = []
    per_query: list[dict] = []
    llm_calls = 0
    n_call_failures_total = 0

    for complaint in queries:
        try:
            s1 = normalize(complaint)
        except Exception as e:  # noqa: BLE001 - S1 调用失败不能拖累其它主诉
            n_call_failures_total += 1
            print(f"[estimate_epsilon_s2]「{complaint}」S1 调用失败：{classify_llm_failure(e)}: {e}", file=sys.stderr)
            per_query.append({"query": complaint, "skipped": True, "reason": "S1 调用失败"})
            continue
        llm_calls += 1
        reject = check_safety([complaint] + s1.symptoms + s1.unmapped)
        if reject is not None:
            per_query.append({"query": complaint, "skipped": True, "reason": "被安全否决拦截"})
            continue

        elem_sets = []
        n_call_failures = 0
        for _ in range(n_repeats):
            try:
                s2 = infer_elements(s1)
            except Exception as e:  # noqa: BLE001 - 单次 S2 重复失败不能拖累这条主诉的其它重复
                n_call_failures += 1
                print(
                    f"[estimate_epsilon_s2]「{complaint}」一次 S2 重复失败："
                    f"{classify_llm_failure(e)}: {e}", file=sys.stderr,
                )
                continue
            llm_calls += 1
            elem_sets.append({h.element for h in s2.elements})

        n_call_failures_total += n_call_failures
        n_completed = n_repeats - n_call_failures
        if n_completed == 0:
            per_query.append({
                "query": complaint, "skipped": True, "reason": "全部重复调用失败",
                "n_call_failures": n_call_failures,
            })
            continue

        stats = pairwise_jaccard_stats(elem_sets)
        per_query.append({
            "query": complaint, "skipped": False, "stats": stats,
            "n_call_failures": n_call_failures, "n_completed": n_completed,
        })
        if stats:
            all_distances.extend(stats["values"])

    overall = aggregate_stats(all_distances) or _empty_stats()
    return {
        **overall,
        "per_query": per_query,
        "n_queries": len(queries),
        "n_queries_used": sum(1 for q in per_query if not q["skipped"]),
        "n_repeats": n_repeats,
        "n_call_failures": n_call_failures_total,
        "llm_calls": llm_calls,
    }


def estimate_epsilon_extract(
    cases_path: Path = DEFAULT_CASES_PATH,
    n_repeats: int = DEFAULT_N_REPEATS,
    n_samples: int = DEFAULT_N_SAMPLES,
    seed: int = SAMPLE_SEED,
) -> dict:
    """S0 抽取的噪声。挑 `cases.json` 里带 `raw_excerpt` 的初诊记录，把
    `raw_excerpt` 包成 `extract_segment()` 认的 segment 形状
    （`text`/`head_hints`/`follow_hints`），重复调用 N 次，比较抽出来的
    `symptoms` 集合。

    复用 `offline.extract_cases.extract_segment` 的调用路径（同一个 prompt、
    同一个 schema），不是另写一套抽取逻辑——只是喂给它的"段"缩小成了单诊原文，
    `head_hints`/`follow_hints` 留空（那两个字段本来就只是"仅供参考"的提示，
    不是判据）。

    `cases.json` 不存在时返回 `{"available": False, "note": ...}`，不编造数据。
    """
    if not cases_path.exists():
        return {
            "available": False,
            "note": f"{cases_path} 不存在，S0 抽取噪声需要真实医案数据，本机无法估计。"
                    "先跑 offline/split_cases.py + offline/extract_cases.py 生成它。",
        }

    from offline.extract_cases import extract_segment

    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    candidates = [c for c in cases if (c.get("visit_index") or 0) == 0 and c.get("raw_excerpt")]
    if not candidates:
        return {
            "available": False,
            "note": "cases.json 里没有带 raw_excerpt 的初诊记录，无法估计。",
        }

    rng = random.Random(seed)
    sample = rng.sample(candidates, k=min(n_samples, len(candidates)))

    all_distances: list[float] = []
    per_case: list[dict] = []
    llm_calls = 0
    n_extraction_failures = 0

    for c in sample:
        segment = {
            "seg_id": c["case_id"], "text": c["raw_excerpt"],
            "head_hints": [], "follow_hints": [],
        }
        symptom_sets = []
        for _ in range(n_repeats):
            try:
                result = extract_segment(segment)
            except Exception as e:  # noqa: BLE001 - 单条抽取失败（LLMError 等）不该让
                # 整个噪声估算停下；失败的这一次不计入这条 case 的重复，
                # n_extraction_failures 记下来，报告里如实标注不是静默吞掉。
                # 打到 stderr 而不是完全吞掉——原来这里连 print 都没有，出问题
                # 只能看最终计数猜，看不到是哪条 case、什么原因。
                n_extraction_failures += 1
                print(
                    f"[estimate_epsilon_extract] case={c['case_id']} 一次重复抽取失败："
                    f"{classify_llm_failure(e)}: {e}", file=sys.stderr,
                )
                continue
            llm_calls += 1
            if not result.patients or not result.patients[0].visits:
                symptom_sets.append(set())
                continue
            symptom_sets.append(set(result.patients[0].visits[0].symptoms))

        stats = pairwise_jaccard_stats(symptom_sets)
        per_case.append({"case_id": c["case_id"], "stats": stats, "n_ok": len(symptom_sets)})
        if stats:
            all_distances.extend(stats["values"])

    overall = aggregate_stats(all_distances) or _empty_stats()
    return {
        "available": True,
        **overall,
        "per_case": per_case,
        "n_cases": len(sample),
        "n_repeats": n_repeats,
        "n_extraction_failures": n_extraction_failures,
        "llm_calls": llm_calls,
    }


def _estimate_call_counts(n_queries: int, n_repeats: int, n_samples: int,
                          extract_available: bool, n_physicians: int) -> dict:
    """--dry-run 用的调用数估算，不是真跑。每条 consult() 大致 2（S1+S2）+
    每位医家 1 次 S3（追问/残差/重开这些额外调用忽略不计，估算本来就是上界）。"""
    online = n_queries * n_repeats * (2 + n_physicians)
    s2 = n_queries * (1 + n_repeats)  # 1 次 S1 + n_repeats 次 S2
    extract = (n_samples * n_repeats) if extract_available else 0
    return {"online": online, "s2": s2, "extract": extract, "total": online + s2 + extract}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="估算三层噪声地板，写 eval/epsilon.json")
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH)
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--n-repeats", type=int, default=DEFAULT_N_REPEATS)
    ap.add_argument("--n-samples", type=int, default=DEFAULT_N_SAMPLES,
                     help="epsilon_extract 抽样的医案条数")
    ap.add_argument("--limit", type=int, default=0, help="只用前 N 条主诉，先看质量/控制成本")
    ap.add_argument("--skip-extract", action="store_true", help="跳过 epsilon_extract 这一层")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要发起的调用数，不真跑")
    args = ap.parse_args(argv)

    if not args.queries_path.exists():
        raise FileNotFoundError(f"未找到 {args.queries_path}")
    queries = [
        q.strip() for q in args.queries_path.read_text(encoding="utf-8").splitlines() if q.strip()
    ]
    if args.limit:
        queries = queries[: args.limit]

    extract_available = (not args.skip_extract) and args.cases_path.exists()
    est = _estimate_call_counts(
        len(queries), args.n_repeats, args.n_samples, extract_available, len(PHYSICIANS)
    )
    print(f"主诉数：{len(queries)}  重复次数：{args.n_repeats}  已注册医家数：{len(PHYSICIANS)}")
    print(f"预估调用数：epsilon_online≈{est['online']}  epsilon_s2≈{est['s2']}  "
          f"epsilon_extract≈{est['extract']}（{'可用' if extract_available else '不可用：无 cases.json 或 --skip-extract'}）"
          f"  合计≈{est['total']}")

    if args.dry_run:
        print("\n--dry-run：不发起任何调用。确认无误后去掉这个参数重跑。")
        return

    t0 = time.time()
    print("\n=== epsilon_online ===")
    online = estimate_epsilon_online(queries, n_repeats=args.n_repeats)
    print(f"  mean={online['mean']} p50={online['p50']} p95={online['p95']}  "
          f"用了 {online['n_queries_used']}/{online['n_queries']} 条主诉、{online['llm_calls']} 次调用"
          f"（{online['n_call_failures']} 次重复调用失败）")
    warn_if_failure_rate_high("epsilon_online", online["n_call_failures"], online["n_attempts"])

    print("\n=== epsilon_s2 ===")
    s2 = estimate_epsilon_s2(queries, n_repeats=args.n_repeats)
    print(f"  mean={s2['mean']} p50={s2['p50']} p95={s2['p95']}  "
          f"用了 {s2['n_queries_used']}/{s2['n_queries']} 条主诉、{s2['llm_calls']} 次调用"
          f"（{s2['n_call_failures']} 次调用失败）")
    # 分母跟 _estimate_call_counts 的 s2 估算同一个形状：每条主诉 1 次 S1 +
    # n_repeats 次 S2，都是"一次调用"，S1 失败和 S2 单次重复失败在这里
    # 不分开算失败率——都是"这次批处理里的一次调用没跑成"。
    warn_if_failure_rate_high(
        "epsilon_s2", s2["n_call_failures"], len(queries) * (1 + args.n_repeats)
    )

    print("\n=== epsilon_extract ===")
    if extract_available:
        extract = estimate_epsilon_extract(
            args.cases_path, n_repeats=args.n_repeats, n_samples=args.n_samples
        )
        if extract["available"]:
            print(f"  mean={extract['mean']} p50={extract['p50']} p95={extract['p95']}  "
                  f"用了 {extract['n_cases']} 条医案、{extract['llm_calls']} 次调用"
                  f"（{extract['n_extraction_failures']} 次重复抽取失败）")
            warn_if_failure_rate_high(
                "epsilon_extract", extract["n_extraction_failures"],
                extract["n_cases"] * args.n_repeats,
            )
        else:
            print(f"  不可用：{extract['note']}")
    else:
        extract = {"available": False, "note": "跳过（--skip-extract 或 cases.json 不存在）"}
        print(f"  {extract['note']}")

    llm = get_llm()
    out = {
        "epsilon_online": online,
        "epsilon_s2": s2,
        "epsilon_extract": extract,
        "model": llm.model_name(),
        "backend": llm.backend_id(),
        "prompt_version": "v1",
        "cases_sha256": None,
        "comparability_warning": llm.comparability_warning(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_s": round(time.time() - t0, 1),
    }
    if args.cases_path.exists():
        import hashlib

        out["cases_sha256"] = hashlib.sha256(args.cases_path.read_bytes()).hexdigest()[:12]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {args.out}")
    if out["comparability_warning"]:
        print(f"【警告】{out['comparability_warning']}")


if __name__ == "__main__":
    main()
