"""V1：评测汇总。把已经建好的各条能力（分歧度、幻觉、安全否决、检索模式）
汇总成一份带对照的报告——CLAUDE.md"任何数字都必须带对照"是这里的设计出发点，
不是事后补的检查项：每个 metric 函数的返回值本身就带着它的对照基准，不是
先算数字、再另外拼一段"对照说明"文字。

**V3 计划文档规定的具体指标编号（E1...E13）这一轮没有拿到原文**——早前的
HANDOFF.md 已经写明"这几项的 spec 在 V3 计划文档里，不在代码库里，我这边
没有"。这里实现的是从现有代码已经产出的、有真实含义的信号出发、重新设计
的一套指标（见下面每个函数的文档字符串），不是照抄一份这一轮也拿不准是否
准确的编号清单——写一份看起来对应 E1-E13、实际对不对得上都不知道的东西，
比坦白说"这是我按现有信号设计的"更容易误导人。跟原始编号的对应关系等拿到
可信的原文再核对。

四类指标，每类都设计成接收调用方已经跑好的结果（不在函数内部调 consult()/
search()）——跟 offline/estimate_epsilon.py 的 consult_fn 注入是同一个模式，
真实调用和离线测试用同一套函数，不用为了能测试再写一份假实现。

  1. divergence_vs_epsilon：分歧度 vs ε 噪声地板（复用 offline/estimate_epsilon.py
     的产物 eval/epsilon.json，不重新估计）。
  2. hallucination_by_reference_availability：幻觉率，按"这次问诊有没有真实
     参考医案可引用"分组——两组的对照直接说明"是模型瞎编，还是压根没东西
     可引"，比单独一个幻觉率数字有意义。
  3. safety_veto_summary：安全否决率 + 代价（否决与非否决两组的平均调用数），
     对照就是两组各自的分母。
  4. retrieval_mode_comparison：检索模式两两对比，用 McNemar 检验"top-1 相似度
     达到 MIN_RETRIEVAL_SCORE（复用 core.retrieval 的既有阈值，不新定一个）"
     这个二元结果在两种模式下是否有显著差异。

用法：
    python -m eval.run_eval --queries-path tests/queries.txt
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from core.retrieval import MIN_RETRIEVAL_SCORE
# ε 文件的路径和读法只在 core/chain.py 一处：这里之前有一份逐字相同的拷贝
from core.chain import EPSILON_PATH, load_epsilon_online
from eval.mcnemar import mcnemar_test, paired_outcomes_to_bc

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES_PATH = ROOT / "tests" / "queries.txt"
DEFAULT_REPORT_JSON_PATH = ROOT / "eval" / "report.json"
DEFAULT_REPORT_MD_PATH = ROOT / "eval" / "report.md"


# ---------- 1. 分歧度 vs ε ----------


def divergence_vs_epsilon(consult_results: list[dict], epsilon_online: float | None) -> dict:
    """有效样本 = 未拦截、未信息不足、算出了 herb_jaccard 的那些次问诊。
    ε 未测（epsilon_online 是 None）时不强行给一个"多少算分歧"的结论——
    没有噪声地板，任何阈值都是拍脑袋。"""
    usable = [
        r["divergence"]["herb_jaccard"]
        for r in consult_results
        if not r["rejected"] and not r["insufficient"]
        and r.get("divergence") and r["divergence"].get("herb_jaccard") is not None
    ]
    n_total = len(consult_results)
    n_usable = len(usable)
    if epsilon_online is None:
        return {
            "n_queries": n_total, "n_usable": n_usable,
            "epsilon_online": None,
            "n_above_epsilon": None, "n_within_epsilon": None,
            "available": False,
            "note": f"{EPSILON_PATH} 未生成（先跑 offline/estimate_epsilon.py），"
                    "没有噪声地板就不能判断 herb_jaccard 是不是真分歧。",
        }
    n_above = sum(1 for j in usable if j > epsilon_online)
    n_within = n_usable - n_above
    return {
        "n_queries": n_total, "n_usable": n_usable,
        "epsilon_online": epsilon_online,
        "n_above_epsilon": n_above, "n_within_epsilon": n_within,
        "rate_above_epsilon": round(n_above / n_usable, 3) if n_usable else None,
        "available": True,
        "note": f"{n_above}/{n_usable} 条查询的用药 Jaccard 距离超过噪声地板 "
                f"ε={epsilon_online}，判定为真实分歧而非重复采样噪声；"
                f"{n_within} 条在噪声地板以内，不构成分歧。",
    }


# ---------- 2. 幻觉率（按参考医案可得性分组）----------


def hallucination_by_reference_availability(consult_results: list[dict]) -> dict:
    """幻觉：physician-level 结果里 cited_case_ids 引用了不在检索/ReAct 真实
    返回过的 case_id 里的编号（core/chain.py 的 hallucinated 字段）。按
    no_reference_cases 分组，不是因为幻觉率本身要分子分母各算一次那么简单
    ——是因为"这次问诊压根没有真实医案可引用却还编了一个 id"和"有真实医案
    可引、却引错了"是两种性质不同的失败，混在一起报会把后者的严重程度
    稀释掉。"""
    with_ref = {"n": 0, "n_hallucinated": 0}
    without_ref = {"n": 0, "n_hallucinated": 0}
    for r in consult_results:
        if r["rejected"] or r["insufficient"] or not r.get("results"):
            continue
        for pr in r["results"]:
            bucket = without_ref if pr.get("no_reference_cases") else with_ref
            bucket["n"] += 1
            if pr.get("hallucinated"):
                bucket["n_hallucinated"] += 1

    def _rate(bucket):
        return round(bucket["n_hallucinated"] / bucket["n"], 3) if bucket["n"] else None

    return {
        "with_reference_cases": {**with_ref, "rate": _rate(with_ref)},
        "without_reference_cases": {**without_ref, "rate": _rate(without_ref)},
        "note": "两组对照：有真实医案可引用时仍然幻觉，是模型没有忠实于检索结果；"
                "没有医案可引用时如果还引了一个 id，是更严重的凭空编造。"
                "两组分母/分子都如实报出，不合并成一个数。",
    }


# ---------- 3. 安全否决率 + 代价 ----------


def safety_veto_summary(consult_results: list[dict]) -> dict:
    """代价用两组的平均 llm_calls 对照：否决组因为提前终止，调用数通常明显更低——
    如果否决组反而更高，说明否决判断本身消耗了不合理的额外调用，值得单独排查。"""
    vetoed = [r for r in consult_results if r["rejected"]]
    normal = [r for r in consult_results if not r["rejected"]]

    def _avg_calls(rs):
        calls = [r["manifest"]["llm_calls"] for r in rs if r.get("manifest")]
        return round(sum(calls) / len(calls), 2) if calls else None

    n_total = len(consult_results)
    return {
        "n_queries": n_total,
        "n_vetoed": len(vetoed),
        "n_normal": len(normal),
        "veto_rate": round(len(vetoed) / n_total, 3) if n_total else None,
        "avg_llm_calls_vetoed": _avg_calls(vetoed),
        "avg_llm_calls_normal": _avg_calls(normal),
        "note": f"{len(vetoed)}/{n_total} 条查询触发安全否决，不产出任何方药；"
                "被否决和正常完成两组各自的平均调用数并列报出，用来量化否决的成本，"
                "不是只报一个否决率数字。",
    }


# ---------- 4. 检索模式对比（McNemar） ----------


#  MIN_RETRIEVAL_SCORE 是拿稠密余弦相似度的实测分布校准出来的阈值，graph 模式
# 的 Jaccard 相似度同样落在 [0,1]、语义也是"越大越像"，套用可以成立（K3b 的
# `_graph_ranking` 文档字符串讨论过反方向的问题——阈值本身对小证素集合偏严格，
# 但至少刻度是同一个）。bm25 的展示分是无界的原始分数，10-30 是常态，套用
# 0.70 这个阈值毫无意义——不是"bm25 更自信"，只是数值刻度完全不同。
# 见 core/retrieval_hybrid.py 模块文档字符串"min_score 只作用于稠密分"这条
# 设计的同一个道理。
_BOUNDED_SCORE_MODES = {"dense", "graph", "hybrid"}


def retrieval_mode_comparison(
    per_query_top1_by_mode: dict[str, list[tuple[str, float] | None]],
    mode_a: str,
    mode_b: str,
) -> dict:
    """per_query_top1_by_mode: {mode: [(case_id, score) 或 None, ...]}，
    每个 mode 的列表长度必须一致（同一批查询）——None 表示该模式下这条查询
    没有任何结果（比如检索为空）。

    二元结果的定义分两种情况：
      - 两个 mode 都在 _BOUNDED_SCORE_MODES 里（分数是 [0,1] 有界的真实相似度）：
        用 top-1 相似度是否达到 MIN_RETRIEVAL_SCORE 当"够可信"的判据，复用
        core.retrieval 里已经校准过的阈值，不新定一个。
      - 只要有一个 mode 是 bm25（分数无界）：退化成"这条查询有没有返回任何
        结果"这个更弱但两种刻度下都成立的判据，并在 note 里说明为什么不是
        用阈值判——不能对不可比的分数硬套一个阈值然后假装结果有意义。

    McNemar 检验的是"两种模式在这个二元判据上是否有系统性差异"，不是比较
    两种模式选出的是不是同一条案例（那是描述性统计 top1_differs，附带报出，
    不需要显著性检验）。
    """
    a_list = per_query_top1_by_mode[mode_a]
    b_list = per_query_top1_by_mode[mode_b]
    if len(a_list) != len(b_list):
        raise ValueError(f"两种模式的查询数不一致：{len(a_list)} vs {len(b_list)}")

    both_bounded = mode_a in _BOUNDED_SCORE_MODES and mode_b in _BOUNDED_SCORE_MODES
    if both_bounded:
        criterion = f"相似度达到 MIN_RETRIEVAL_SCORE={MIN_RETRIEVAL_SCORE}"
        outcome_a = [(hit is not None and hit[1] >= MIN_RETRIEVAL_SCORE) for hit in a_list]
        outcome_b = [(hit is not None and hit[1] >= MIN_RETRIEVAL_SCORE) for hit in b_list]
    else:
        criterion = "返回了任意 top-1 结果（分数不可比，退化成有/无结果这个判据）"
        outcome_a = [hit is not None for hit in a_list]
        outcome_b = [hit is not None for hit in b_list]

    b_count, c_count = paired_outcomes_to_bc(outcome_a, outcome_b)
    mcnemar = mcnemar_test(b_count, c_count)

    n = len(a_list)
    top1_differs = sum(
        1 for hit_a, hit_b in zip(a_list, b_list)
        if (hit_a is None) != (hit_b is None)
        or (hit_a is not None and hit_b is not None and hit_a[0] != hit_b[0])
    )

    return {
        "mode_a": mode_a, "mode_b": mode_b, "n_queries": n,
        "criterion": criterion,
        "n_confident_a": sum(outcome_a), "n_confident_b": sum(outcome_b),
        "n_top1_differs": top1_differs,
        "mcnemar": mcnemar,
        "note": f"判据：{criterion}。{mode_a} 命中 {sum(outcome_a)}/{n}，{mode_b} 命中 "
                f"{sum(outcome_b)}/{n}；McNemar p={mcnemar['p_value']}"
                f"（{mcnemar['method']}，n_discordant={mcnemar['n_discordant']}）；"
                f"另有 {top1_differs}/{n} 条查询两种模式选出了不同的 top-1 案例"
                "（描述性统计，不代表其中一个是错的）。",
    }


# ---------- 汇总与报告 ----------


def build_report(
    consult_results: list[dict],
    retrieval_comparisons: list[dict] | None = None,
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_queries": len(consult_results),
        "divergence_vs_epsilon": divergence_vs_epsilon(consult_results, load_epsilon_online()),
        "hallucination": hallucination_by_reference_availability(consult_results),
        "safety_veto": safety_veto_summary(consult_results),
        "retrieval_mode_comparisons": retrieval_comparisons or [],
    }


def render_markdown(report: dict) -> str:
    lines = [
        f"# V1 评测汇总（{report['generated_at']}）",
        "",
        f"共 {report['n_queries']} 条查询。以下每项数字旁边都带着它的对照基准"
        "（CLAUDE.md：任何数字都必须带对照）。",
        "",
        "## 分歧度 vs 噪声地板 ε",
        report["divergence_vs_epsilon"]["note"],
        "",
        "## 幻觉率（按参考医案可得性分组）",
        report["hallucination"]["note"],
        "",
        "## 安全否决率 + 代价",
        report["safety_veto"]["note"],
        "",
        "## 检索模式对比",
    ]
    if report["retrieval_mode_comparisons"]:
        for c in report["retrieval_mode_comparisons"]:
            lines.append(f"- {c['note']}")
    else:
        lines.append("（本次未跑检索模式对比）")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="V1：评测汇总（需要真实 LLM，跑 core.chain.consult）")
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH)
    ap.add_argument("--out-json", type=Path, default=DEFAULT_REPORT_JSON_PATH)
    ap.add_argument("--out-md", type=Path, default=DEFAULT_REPORT_MD_PATH)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="只打印预估调用数，不真的跑")
    args = ap.parse_args(argv)

    queries = [
        line.strip() for line in args.queries_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if args.limit is not None:
        queries = queries[: args.limit]

    if args.dry_run:
        # 每条查询：consult() 至少 2 次（S1+S2），通常每位医家再各 1-2 次，
        # 这里给一个保守估计，不精确统计每条路径——dry-run 只是给个数量级。
        print(f"--dry-run：预估调用数 ≈ {len(queries) * 6}（{len(queries)} 条查询 × ~6 次/条，"
              "含 2 位医家 S3、配伍禁忌可能重开），不真的调用")
        return

    from core.chain import consult_many

    results, failures = consult_many(queries)
    report = build_report([r for r in results if r is not None])
    # 失败的主诉进报告而不是只打在终端：n_queries 少了几条要能从报告本身看出来
    report["failed_queries"] = failures
    if failures:
        print(f"注意：{len(failures)}/{len(queries)} 条主诉失败，已写进 report.json 的 failed_queries")

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    args.out_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"已写出 {args.out_json} 和 {args.out_md}")


if __name__ == "__main__":
    main()
