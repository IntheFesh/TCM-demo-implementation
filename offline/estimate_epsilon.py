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
from core.progress import Progress
from core.chain import consult, infer_elements, normalize
from core.herbs import normalized_herb_set, role_partitioned_herb_sets
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
# R1 分层：整方 / 君臣 / 佐使。"herbs" 这一层就是 R1 之前的 epsilon_online，
# 字段和数值一个都没变——分层是新增的两层，不是把原来那个数改了（E3/E4/E9 的
# 历史数字都靠 epsilon_online 可比）。
EPSILON_LAYERS = ("herbs", "core", "adjunct")


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

    ## R1：三层一起算（返回值里多一个 layers 子对象）

    返回的 dict 就是 R1 之前的那个 epsilon_online，**字段和数值一个都没变**，
    只多了一个 `layers` 键，里面是君臣层（core）和佐使层（adjunct）两个形状
    完全一样的结果对象——`main()` 把它摘出来平铺成 epsilon.json 顶层的
    `epsilon_core` / `epsilon_adjunct`。三层共用同一批 `consult()` 调用（一次
    跑出的方，切三种集合看），不是跑三遍，所以三层的 `llm_calls` 是同一个数、
    不是三份开销。
    """
    consult_fn = consult_fn or (lambda c: consult(c, use_react=False, ask_fn=None))

    # {层: [距离…]} / {层: {医家: [距离…]}} / {层: [每条主诉一条记录]}
    # 三层的累积结构刻意完全一样：分开写三套平行代码，以后改一处忘另两处
    # 是这个项目撞过好几次的坑。
    all_distances: dict[str, list[float]] = {k: [] for k in EPSILON_LAYERS}
    by_physician_distances: dict[str, dict[str, list[float]]] = {k: {} for k in EPSILON_LAYERS}
    per_query: dict[str, list[dict]] = {k: [] for k in EPSILON_LAYERS}
    # 平均药味数：R1-3 的佐使克制约束会让药味数下降。如果 ε 降了而药味数
    # 大幅下降（9 味 → 5 味），那是"药少了所以碰巧一样"的假改善，不是真的
    # 稳定了——没有这个对照数，ε 单独下降这件事读不出真假。
    set_sizes: dict[str, list[int]] = {k: [] for k in EPSILON_LAYERS}
    # 分层那两个 ε 的可信度全看 role 填充率：没标 role 的药不进任何一层。
    n_herb_items = 0
    n_unroled = 0
    llm_calls = 0
    n_call_failures_total = 0
    # 粒度 = **每一次重复**（一条主诉重复 n_repeats 次，每次一整轮 consult）。
    # 原来这里只有三行阶段级输出，一条主诉跑三遍中间几分钟没声音。
    bar = Progress(total=len(queries) * n_repeats, label="ε_online（逐条重复）", unit="轮")

    for complaint in queries:
        sets_by_layer: dict[str, dict[str, list[set | None]]] = {k: {} for k in EPSILON_LAYERS}
        n_rejected = 0
        n_insufficient = 0
        n_call_failures = 0

        for repeat in range(n_repeats):
            try:
                outcome = consult_fn(complaint)
                bar.advance(note=f"「{complaint[:12]}」第 {repeat + 1}/{n_repeats} 遍")
            except Exception as e:  # noqa: BLE001 - 单次重复失败不能拖累这条主诉的其它重复/其它主诉
                n_call_failures += 1
                bar.note(f"「{complaint[:14]}」一次重复失败：{classify_llm_failure(e)}")
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
                parts = role_partitioned_herb_sets(r["s3"].selected_herb_items)
                n_herb_items += parts["n_items"]
                n_unroled += parts["n_unroled"]
                # 分层的空集**不参与 Jaccard**：传 None 让 pairwise_jaccard_stats
                # 跳过（它本来就有这个约定）。空的君臣层含义是"这张方的药没标
                # role"，不是"这张方没有君臣药"——两个空集算出来的距离是 0.0
                # （"双方都没提到任何东西"视为一致，见 core/setstats.py），
                # 那会让 epsilon_core 被一堆未标注的方压成 0，一个假的好数字。
                # 整方层（herbs）不这么处理：那里的空集是"这位医家真的没开方"，
                # 是真实信息，R1 之前就是这么算的，不动。
                layer_sets: dict[str, set | None] = {
                    "herbs": normalized_herb_set(r["s3"].herbs),
                    "core": parts["core"] or None,
                    "adjunct": parts["adjunct"] or None,
                }
                for layer, st in layer_sets.items():
                    sets_by_layer[layer].setdefault(r["physician"], []).append(st)
                    if st is not None:
                        set_sizes[layer].append(len(st))

        n_call_failures_total += n_call_failures
        n_completed = n_repeats - n_call_failures  # 真正跑完的重复次数，不是 n_repeats
        skip_reason = None
        if n_completed == 0:
            skip_reason = "全部重复调用失败"
        elif n_rejected == n_completed:
            skip_reason = "全部重复被安全否决拦截"
        elif n_insufficient == n_completed:
            skip_reason = "全部重复信息不足未产出结论"
        if skip_reason:
            # 跳过的原因跟分层无关（整条主诉没跑出结论），三层记同一条
            for layer in EPSILON_LAYERS:
                per_query[layer].append({
                    "query": complaint, "skipped": True, "reason": skip_reason,
                    "n_call_failures": n_call_failures,
                })
            continue

        for layer in EPSILON_LAYERS:
            q_record: dict = {
                "query": complaint, "skipped": False,
                "n_rejected": n_rejected, "n_insufficient": n_insufficient,
                "n_call_failures": n_call_failures, "n_completed": n_completed,
                "by_physician": {},
            }
            for physician, sets_ in sets_by_layer[layer].items():
                stats = pairwise_jaccard_stats(sets_)
                q_record["by_physician"][physician] = stats
                if stats:
                    all_distances[layer].extend(stats["values"])
                    by_physician_distances[layer].setdefault(physician, []).extend(stats["values"])
            per_query[layer].append(q_record)

    role_fill_rate = (1 - n_unroled / n_herb_items) if n_herb_items else None
    by_layer: dict[str, dict] = {}
    for layer in EPSILON_LAYERS:
        overall = aggregate_stats(all_distances[layer]) or _empty_stats()
        sizes = set_sizes[layer]
        by_layer[layer] = {
            **overall,
            "by_physician": {
                p: (aggregate_stats(d) or _empty_stats())
                for p, d in by_physician_distances[layer].items()
            },
            "per_query": per_query[layer],
            "n_queries": len(queries),
            "n_queries_used": sum(1 for q in per_query[layer] if not q["skipped"]),
            "n_repeats": n_repeats,
            "n_call_failures": n_call_failures_total,
            "n_attempts": len(queries) * n_repeats,
            # 三层是同一批 consult() 调用切出来的，这个数是那一批的总调用数，
            # 不是"这一层额外花了这么多"
            "llm_calls": llm_calls,
            # 归一去重之后的集合大小，不是 herb_items 的条数——同一味药写两次
            # 不该算两味，而且这样它跟 ε 数的是同一个集合
            "mean_herbs_per_formula": round(sum(sizes) / len(sizes), 2) if sizes else None,
            "n_formulas_counted": len(sizes),
        }
    bar.close(f"{llm_calls} 次调用")
    for layer in ("core", "adjunct"):
        # role 填充率只挂在分层这两个结果上：epsilon_core/epsilon_adjunct 可信到
        # 什么程度全看它，读 JSON 的人应当在同一个对象里就看到，不用去别处找。
        # 判据跟 scripts/verify_role_fill.py 的闸门是同一个量（那个脚本负责在
        # 真机上先把这个率验到 90% 以上，分层指标才算可用）。
        by_layer[layer].update({
            "role_fill_rate": round(role_fill_rate, 4) if role_fill_rate is not None else None,
            "n_herb_items": n_herb_items,
            "n_unroled": n_unroled,
        })

    online = by_layer["herbs"]
    online["layers"] = {"core": by_layer["core"], "adjunct": by_layer["adjunct"]}
    return online


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
    # 粒度 = 每一次 S2 重复（S1 每条只跑一次，不进这个分母）
    s2_bar = Progress(total=len(queries) * n_repeats, label="ε_s2（逐条重复）", unit="轮")

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
        for repeat in range(n_repeats):
            try:
                s2 = infer_elements(s1)
                s2_bar.advance(note=f"「{complaint[:12]}」S2 第 {repeat + 1}/{n_repeats} 遍")
            except Exception as e:  # noqa: BLE001 - 单次 S2 重复失败不能拖累这条主诉的其它重复
                n_call_failures += 1
                s2_bar.note(f"「{complaint[:14]}」一次 S2 重复失败：{classify_llm_failure(e)}")
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

    s2_bar.close(f"{llm_calls} 次调用")
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

    extract_bar = Progress(total=len(sample) * n_repeats, label="ε_extract（逐条重复）", unit="轮")
    for c in sample:
        segment = {
            "seg_id": c["case_id"], "text": c["raw_excerpt"],
            "head_hints": [], "follow_hints": [],
        }
        symptom_sets = []
        for repeat in range(n_repeats):
            try:
                result = extract_segment(segment)
                extract_bar.advance(note=f"{c['case_id']} 第 {repeat + 1}/{n_repeats} 遍")
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

    extract_bar.close(f"{llm_calls} 次调用")
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


def _report_layers(online: dict, layers: dict) -> None:
    """把君臣层/佐使层的数跟整方层并排打出来，并**如实报出观察到的关系**。

    R1 的验收判据是 epsilon_core < epsilon_online < epsilon_adjunct（核心判断
    比整方稳、佐使加减比整方散）。这里只报关系成不成立，不拿它当闸门、更不
    调参去凑：这个关系不成立本身就是一条要留给下一轮的信息（比如 role 标注
    质量不够、或者模型的"君臣"判断本身也在抖）。

    role 填充率一并打出来：填充率低的时候分层那两个数只覆盖了一部分用药，
    读的人必须同时看到这个率，不然会把"只有三成药参与了比较"读成全貌。
    """
    core, adjunct = layers["core"], layers["adjunct"]
    fill = core["role_fill_rate"]
    print(f"  role 填充率 {'未知（没有任何药材条目）' if fill is None else f'{fill:.1%}'}"
          f"（{core['n_herb_items'] - core['n_unroled']}/{core['n_herb_items']} 味标了 role；"
          f"低于 90% 时下面两个分层的数只覆盖了一部分用药，不能当全貌读）")
    for label, layer in (("epsilon_core（君臣）", core), ("epsilon_adjunct（佐使）", adjunct)):
        print(f"  {label:<22} mean={layer['mean']} p50={layer['p50']} p95={layer['p95']}  "
              f"平均药味数={layer['mean_herbs_per_formula']}"
              f"（{layer['n_formulas_counted']} 张方）")
    print(f"  {'epsilon_online（整方）':<22} mean={online['mean']}  "
          f"平均药味数={online['mean_herbs_per_formula']}"
          f"（{online['n_formulas_counted']} 张方）")

    vals = (core["mean"], online["mean"], adjunct["mean"])
    if any(v is None for v in vals):
        missing = [n for n, v in zip(("core", "online", "adjunct"), vals) if v is None]
        print(f"  验收关系无法判定：{'、'.join(missing)} 没有数（这一层没有可比样本）")
        return
    c, o, a = vals
    holds = c < o < a
    print(f"  验收关系 epsilon_core < epsilon_online < epsilon_adjunct："
          f"{c} < {o} < {a} → {'成立' if holds else '不成立'}")
    if not holds:
        print("  （不成立不改参数去凑——如实留给下一轮；先看 role 填充率和"
              "平均药味数这两个对照数是不是解释了它）")


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
    layers = online.pop("layers")  # 摘出来平铺到 epsilon.json 顶层，见 _report_layers
    _report_layers(online, layers)

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
        # 分层的两个 ε 跟 epsilon_online 平级，不藏在它里面：三层是并列的三个
        # 对照基准（core/chain.py 的 load_epsilon_layer_means 按这个形状读），
        # 嵌一层只会让读 JSON 的人多绕一道。
        "epsilon_core": layers["core"],
        "epsilon_adjunct": layers["adjunct"],
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
