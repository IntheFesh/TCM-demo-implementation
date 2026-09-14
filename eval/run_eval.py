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

每类指标都设计成接收调用方已经跑好的结果（不在函数内部调 consult()/search()）
——跟 offline/estimate_epsilon.py 的 consult_fn 注入是同一个模式，真实调用和
离线测试用同一套函数，不用为了能测试再写一份假实现。

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
  5. ablation_output_effect（E3/E4/E9 共用）：某个 consult() 开关（refs_mode
     或 use_react）对 S3 输出的真实影响。收集端（collect_ablation_pairs /
     collect_refs_mode_pairs）各跑一次基线设置和一次消融设置，按 (主诉, 医家)
     把两次的用药集合配对；度量端算 Jaccard 距离当"改变率"（E3/E4 闸门
     ≥40%，低于说明这个开关对结论几乎没有实质影响），并跟 eval/epsilon.json
     里同一条主诉、同一位医家的噪声均值逐对比较——不能用全局 ε 做减法，
     ε 按证型强烈分层（同类 ε 可以从 ~0 到 ~0.5），全局一刀切会系统性地
     判错一批样本。
       - E3：refs_mode own vs swapped（换掉参考医案）
       - E4：refs_mode own vs none（去掉参考医案）
       - E9：use_react False vs True（额外还报步数分布、terminated_by
         分布，见 react_process_summary；terminated_by 对 prompt 措辞敏感，
         带 SOURCES.md 第 12 条那条限定）
  6. retriever_mode_output_effect（E8）：四种 RETRIEVER_MODE 下同一批查询
     的 S3 输出差异率。复用 core.setstats 的 pairwise_jaccard_stats/
     aggregate_stats（跟 offline/estimate_epsilon.py 噪声估计同一套统计
     手法：N 个结果两两算距离、取均值，再对多条查询聚合一层）。graph 模式
     依赖 data/element_index.json 的覆盖率不是 100%，报告里必须带这条限定，
     不能让读者把覆盖率问题读成"图检索方法不行"。
  7. divergence_per_query_detail：report.json 里逐条查询的分歧明细
     （query/herb_jaccard/该条 ε/净差异/判定），跟 divergence_vs_epsilon 的
     汇总数字并列存在——汇总回答"总体多少条超阈值"，这里回答"是哪几条、
     差多少"，理由跟 5 一样：全局 ε 一刀切会系统性判错。

用法：
    python -m eval.run_eval --queries-path tests/queries.txt
    python -m eval.run_eval --queries-path tests/queries.txt --e3 --e4 --e8 --e9
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.batch import classify_llm_failure, warn_if_failure_rate_high
from core.progress import Progress
from core.retrieval import MIN_RETRIEVAL_SCORE
# ε 文件的路径和读法只在 core/chain.py 一处：这里之前有一份逐字相同的拷贝
from core.chain import EPSILON_PATH, load_epsilon_online, load_epsilon_online_detail
from core.herbs import normalized_herb_set
from core.physicians import PHYSICIANS
# E8 的四种默认模式取自检索层自己的合法集合，不在这里另抄一份——
# 见 core/chain.py 对 ALLOWED_MODES 的同一条理由。
from core.retrieval_hybrid import ALLOWED_MODES
# E9 dry-run 的调用数上界要按 MAX_STEPS 算，不能只说"可能更高"——
# 从 core/react.py 现读，不在这里另定一个数字（那样 MAX_STEPS 改了这里会
# 悄悄漂移）。
from core.react import MAX_STEPS
# Jaccard 距离/两两统计全项目只在这里实现一处（core/setstats.py 模块文档
# 字符串）；offline/estimate_epsilon.py 的噪声估计也走它，E3/E4/E8/E9
# 跟噪声地板比较、跨模式/跨配置比较用的都是同一把尺子。
from core.setstats import aggregate_stats, jaccard_distance, pairwise_jaccard_stats
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


# ---------- 5. 消融通用机制（E3/E4：refs_mode；E9：use_react）----------

GATE_OUTPUT_CHANGE_RATE = 0.40  # E3/E4 的闸门；E9 只报数、不设闸门（过程性开关，
                                 # "改变了多少"不像"有没有参考医案"那样有天然及格线）

# AutoDL 实测教训：E9 跑到一半，一次 LLM 调用炸了（LLMError）就崩掉整批，前面
# 跑完的全丢——跟 core.chain.consult_many 已经修过的坑同一类。三个收集器
# （collect_ablation_pairs/collect_refs_mode_pairs/collect_retriever_mode_samples）
# 都要单条失败容忍：失败的 (主诉,医家) 记下来、跳过，不崩，但失败率太高时
# 结果本身就不可信了，要有人能看见。失败分类（type(err.__cause__).__name__）
# 和"失败率超阈值打警告"这两件事 estimate_epsilon.py/sdt/run.py 也要做
# 同样的事，按 CLAUDE.md「同一概念的匹配逻辑只能有一处实现」收进
# core/batch.py，这里不再各写一份。


def _call_failed_pair(query: str, error: BaseException) -> dict:
    """collect_ablation_pairs/collect_refs_mode_pairs 里 consult_fn 调用本身
    抛异常时用——形状跟 _herb_pairs_from_outcomes 里"安全否决/信息不足"的跳过
    条目一样（都是 "skipped": True，都不进 change_rate 分母），但 skip_reason
    不同：那边是 consult() 正常返回、只是业务上判定不可比较；这边是 consult()
    本身没跑成。两者必须能区分——ablation_output_effect 的 note 要分别报
    "有多少条被安全否决" vs "有多少条调用失败"，混在一起会让人看错原因去查错
    地方（明明是 API 抖动，却去查安全否决逻辑）。

    "reason" 里的失败类型用 classify_llm_failure（取 __cause__ 的真实异常
    类型名），不是裸的 type(error).__name__——error 这里几乎总是
    core.llm.LLMError，裸打类型名只会看到"LLMError"，看不出是超时、限流
    还是别的，四处失败分类要统一口径（X3 那轮定下的规矩）。
    """
    return {
        "query": query, "skipped": True, "skip_reason": "call_failed",
        "reason": f"consult() 调用失败：{classify_llm_failure(error)}: {error}",
    }


def _herb_pairs_from_outcomes(query: str, baseline: dict, ablated: dict) -> list[dict]:
    """单条主诉的一次基线 consult() 结果和一次消融 consult() 结果，拆成按医家
    配对的用药集合。E3/E4（refs_mode）、E9（use_react）三个消融共用这一份
    配对逻辑——它们的差别只在"跑 consult() 时传什么参数"，"怎么把两次结果
    拆成可比较的 (医家, 用药集合) 对"是同一件事，不该写三份
    （CLAUDE.md「同一概念的匹配逻辑只能有一处实现」）。

    任一侧被安全否决/信息不足时，这条主诉整体跳过，不是在那一侧强行记一个
    空用药集合——那会把"这次被拦截"算成"用药从有变成了无"，是假的改变率。
    两侧分别独立判断，不假设"S1/S2 结果一致就同生同灭"（真实模型在 S1/S2
    上也会抖，见 offline/estimate_epsilon.py 的 epsilon_s2）。
    """
    baseline_ok = not baseline["rejected"] and not baseline["insufficient"]
    ablated_ok = not ablated["rejected"] and not ablated["insufficient"]
    if not (baseline_ok and ablated_ok):
        return [{
            "query": query, "skipped": True, "skip_reason": "safety_or_insufficient",
            "reason": "基线或消融侧被安全否决/信息不足，无法配对比较",
        }]
    baseline_by_physician = {r["physician"]: r for r in baseline["results"]}
    ablated_by_physician = {r["physician"]: r for r in ablated["results"]}
    pairs = []
    for physician in baseline_by_physician:
        if physician not in ablated_by_physician:
            continue
        own = baseline_by_physician[physician]
        ablated_r = ablated_by_physician[physician]
        pairs.append({
            "query": query, "skipped": False, "physician": physician,
            "own_herbs": normalized_herb_set(own["s3"].herbs),
            "ablated_herbs": normalized_herb_set(ablated_r["s3"].herbs),
            # P0-8：逐条明细要看到检索到的医案 id/相似度，不能只有用药集合——
            # own_refs_scores 尤其关键，改变率低时要能分清是"检索质量不够"
            # 还是"prompt 没利用好检索结果"。.get() 兜底是因为一部分老测试
            # 用的假 outcome 字典没有这两个键（不是真实 run_physician() 的产出，
            # 缺了不代表真的没有 refs），不能因为缺键就 KeyError。
            "own_refs_ids": [r["case_id"] for r in own.get("refs") or []],
            "own_refs_scores": [r["score"] for r in own.get("refs") or []],
            "ablated_refs_ids": [r["case_id"] for r in ablated_r.get("refs") or []],
            # P0-7：own 和消融侧两边检索都为空时，两侧看到的输入实际上完全
            # 一样（都走 S3SyndromeUnreferenced），改变率天然是 0——这不是
            # "开关没有效果"，是这条样本压根没有产生对照，ablation_output_effect
            # 要单独识别、排除出 change_rate 的分母。
            "own_refs_empty": bool(own.get("no_reference_cases")),
            "ablated_refs_empty": bool(ablated_r.get("no_reference_cases")),
            # P0-12：这次检索到的候选之间没有真实区分度时 run_physician()
            # 已经把 top-3 收窄成了 top-1——E3 报告要能看到这个标记出现的
            # 比例（LOW_DISCRIMINATION_CUTOFF 默认开，要能跑两遍对比）。
            "own_low_discrimination": bool(own.get("low_discrimination")),
            "ablated_low_discrimination": bool(ablated_r.get("low_discrimination")),
        })
    return pairs


def _default_consult_fn():
    # 惰性 import：真实调用一律注入 consult_fn 才是更常见的路径，这里跟
    # main() 里对 consult_many 的做法一样，不在模块顶层多绑一个
    # core.chain.consult 的名字。
    from core.chain import consult

    return consult


def collect_ablation_pairs(
    queries: list[str], baseline_kwargs: dict, ablated_kwargs: dict, consult_fn=None,
) -> list[dict]:
    """通用单开关消融收集器：每条主诉各跑一次 baseline_kwargs、一次
    ablated_kwargs（都是要透传给 core.chain.consult 的关键字参数），配对
    产出 _herb_pairs_from_outcomes() 的形状。E9（use_react False vs True）
    直接用它；E3/E4 只跑其中一个（不需要跟另一个共享 own）时也能用，两个
    一起跑时改用下面 collect_refs_mode_pairs，省一次重复的 own。

    use_react=False、ask_fn=None 是默认的隔离基线（同一时间只看一个开关的
    效果，混进另一个抖动源就说不清改变率是哪个开关造成的——跟
    estimate_epsilon_online 隔离变量的理由一样），baseline_kwargs/
    ablated_kwargs 里显式传了同名参数会覆盖它——E9 就是要覆盖 use_react。

    某条主诉的 baseline/ablated 两次调用只要有一次抛异常，这条主诉就单独记
    一条 skip_reason="call_failed" 的跳过条目、打到 stderr，不让整批崩掉——
    跟 core.chain.consult_many 对付单条主诉失败是同一个模式（E9 全套要跑
    约 45 分钟，一次真实 LLM 抖动就崩掉损失前面几十条已经花钱跑完的结果，
    代价太大）。
    """
    consult_fn = consult_fn or _default_consult_fn()
    isolating_defaults = {"use_react": False, "ask_fn": None}
    pairs: list[dict] = []
    # 一条主诉要跑两轮（baseline + ablated），每轮十几次 LLM 调用、几十秒——
    # 原来这个循环从头到尾一声不出，E9 全套 45 分钟里有 45 分钟是静默的。
    bar = Progress(total=len(queries) * 2, label="消融（baseline+ablated）", unit="轮")
    for query in queries:
        try:
            baseline = consult_fn(query, **{**isolating_defaults, **baseline_kwargs})
            bar.advance(note=f"「{query[:12]}」baseline")
            ablated = consult_fn(query, **{**isolating_defaults, **ablated_kwargs})
            bar.advance(note=f"「{query[:12]}」ablated")
        except Exception as e:  # noqa: BLE001 - 单条失败不能拖累其余（core.chain.consult_many 同一模式）
            print(f"[collect_ablation_pairs] 「{query}」调用失败：{classify_llm_failure(e)}: {e}", file=sys.stderr)
            bar.note(f"「{query[:16]}」调用失败：{classify_llm_failure(e)}")
            pairs.append(_call_failed_pair(query, e))
            continue
        pairs.extend(_herb_pairs_from_outcomes(query, baseline, ablated))
    bar.close(f"{len(pairs)} 条配对")
    return pairs


def collect_refs_mode_pairs(
    queries: list[str], ablated_modes: list[str], consult_fn=None,
) -> dict[str, list[dict]]:
    """E3/E4 专用：refs_mode="own" 每条主诉只跑一次，各 ablated_mode
    （"swapped"/"none"）各跑一次——E3+E4 一起跑时 own 不会被重复计算两遍
    （每条主诉省下一整轮 consult 调用）。跟 collect_ablation_pairs 共用
    _herb_pairs_from_outcomes 这一份配对逻辑，只是控制流不同：这是刻意的
    效率优化，不是另写一套匹配规则。

    返回 {ablated_mode: pairs}，跟 ablation_output_effect() 一一对应地喂给
    E3/E4 各自的报告条目。

    own 调用失败：这条主诉在所有 ablated_modes 下都记一条 call_failed 跳过
    条目——own 都没跑成，没法跟任何一个消融模式配对，波及范围是本条查询的
    全部模式。某个 ablated_mode 单独调用失败：只影响那一个模式，其余模式
    不受影响，也不拖累下一条查询——两种失败的波及范围不一样，不能共用一段
    except 处理成一样的效果（同 collect_ablation_pairs 的失败容忍，跟
    core.chain.consult_many 是同一个"单条失败不拖累其余"模式）。
    """
    consult_fn = consult_fn or _default_consult_fn()
    pairs_by_mode: dict[str, list[dict]] = {m: [] for m in ablated_modes}
    # **这就是"基线阶段完全静默 5 分钟"那一段**：own 那一轮跑完之前屏幕上一个字
    # 都没有，三次被误判成卡死。每条主诉 1 + len(ablated_modes) 轮。
    bar = Progress(total=len(queries) * (1 + len(ablated_modes)),
                   label=f"E3/E4 消融（own + {'/'.join(ablated_modes)}）", unit="轮")
    for query in queries:
        try:
            baseline = consult_fn(query, refs_mode="own", use_react=False, ask_fn=None)
            bar.advance(note=f"「{query[:12]}」own")
        except Exception as e:  # noqa: BLE001 - own 失败波及本条查询的所有 ablated_mode，不拖累其它查询
            print(f"[collect_refs_mode_pairs] 「{query}」own 调用失败：{classify_llm_failure(e)}: {e}", file=sys.stderr)
            bar.note(f"「{query[:16]}」own 调用失败：{classify_llm_failure(e)}——这条主诉所有模式都跳过")
            for mode in ablated_modes:
                pairs_by_mode[mode].append(_call_failed_pair(query, e))
            continue
        for mode in ablated_modes:
            try:
                ablated = consult_fn(query, refs_mode=mode, use_react=False, ask_fn=None)
                bar.advance(note=f"「{query[:12]}」{mode}")
            except Exception as e:  # noqa: BLE001 - 只影响这一个 mode
                print(f"[collect_refs_mode_pairs] 「{query}」{mode} 调用失败：{classify_llm_failure(e)}: {e}", file=sys.stderr)
                bar.note(f"「{query[:16]}」{mode} 调用失败：{classify_llm_failure(e)}")
                pairs_by_mode[mode].append(_call_failed_pair(query, e))
                continue
            pairs_by_mode[mode].extend(_herb_pairs_from_outcomes(query, baseline, ablated))
    bar.close("、".join(f"{m} {len(v)} 条" for m, v in pairs_by_mode.items()))
    return pairs_by_mode


def ablation_output_effect(
    pairs: list[dict], epsilon_online_detail: dict | None, label: str,
) -> dict:
    """E3（label="swapped"）/ E4（label="none"）/ E9（label="react_on"）
    共用的度量：某个 consult() 开关从基线切到消融设置之后，S3 输出真的变了
    多少。

    pairs：collect_ablation_pairs() 或 collect_refs_mode_pairs()[mode] 的产出。
    change_rate 是基线/消融两次用药集合的 Jaccard 距离均值
    （core.setstats.jaccard_distance：0=完全一致，1=毫无重叠）。
    GATE_OUTPUT_CHANGE_RATE=0.40 是 E3/E4 的及格线（低于它说明这个开关对
    结论几乎没有实质影响）；E9 报同一个字段但不强调"通不通过"——use_react
    是不是该开是权衡调用成本 vs 严谨度的工程选择，不是"这个功能是不是在
    起作用"的是非题。

    **不能只看 change_rate 这一个数就下结论。** eval/epsilon.json 的噪声
    地板按证型强烈分层（同类 ε 可以从 ~0 到 ~0.5），拿一个全局 ε 去减会把
    "这条主诉本来就该有大 ε"的正常抖动错判成开关的效果，也可能把"这条
    主诉 ε 本来就小"时的真实效果错判成噪声。所以按 (主诉, 医家) 去
    epsilon_online_detail["per_query"] 里找同一条主诉、同一位医家的噪声
    均值，逐对比较：距离 > 配对 ε 才算"超出噪声地板的真实差异"；
    epsilon.json 没跑过、或这条主诉/医家组合查不到配对 ε 的样本单独计数，
    不悄悄并进"真实差异"或"噪声"任何一边——那样会让这两个分母失真。

    P0-7 补充：own 和消融侧两边检索都为空的样本（比如医家覆盖窄，这条主诉
    在他库里连一条过线的参考医案都没有），两侧看到的输入实际上完全一样
    （都走 S3SyndromeUnreferenced），改变率天然是 0——这不是"这个开关没有
    效果"，是这条样本压根没有产生对照，混进 change_rate 的分母会系统性把
    整体数字拉低。这类样本单独计数（n_empty_refs），从算 change_rate 的
    分母（n_scored）里剔除，但仍然出现在 per_pair 里
    （verdict="empty_refs_excluded"），不是悄悄消失——CLAUDE.md「任何数字
    都必须带对照」：被排除的样本本身也要留下痕迹，不能只报排除后的数字。

    P0-8：per_pair 是逐条 (主诉,医家) 明细。汇总的 change_rate 一个数字
    看不出重跑不过时该往哪查——per_pair 里的 own_refs_scores 尤其关键：
    如果 top-3 全是刚过 0.6-0.7 这种勉强线的分数，说明改变率低的根因是
    检索质量不够（该查 P0-7 的阈值/P0-6 的编码），不是 prompt 没利用好
    检索结果（该查 P0-1~P0-4）。

    n_failed：consult() 调用本身失败（skip_reason="call_failed"，见
    collect_ablation_pairs/collect_refs_mode_pairs）的样本数——跟被安全否决
    跳过的样本一样不计入 change_rate 分母，但原因不同（一个是模型主动拒答，
    一个是调用没跑成），note 里分开报，不能把两者混成一个"跳过"数字，
    否则看报告的人分不清"这批数字要不要重跑"还是"这批本来就该被拦"。
    """
    n_failed = sum(1 for p in pairs if p.get("skip_reason") == "call_failed")
    usable = [p for p in pairs if not p.get("skipped")]
    n_total = len(pairs)
    n_usable = len(usable)
    if not usable:
        failed_note = (
            f"（其中 {n_failed} 条是 consult() 调用失败，不是被安全否决/信息不足）"
            if n_failed else ""
        )
        return {
            "label": label, "n_total": n_total, "n_usable": 0, "n_failed": n_failed,
            "n_empty_refs": 0, "n_scored": 0,
            "change_rate": None, "gate_threshold": GATE_OUTPUT_CHANGE_RATE,
            "gate_pass": None, "per_pair": [],
            "n_own_low_discrimination": 0, "n_ablated_low_discrimination": 0,
            "own_low_discrimination_rate": None, "ablated_low_discrimination_rate": None,
            "note": f"{label}：没有可用样本（全部被安全否决/信息不足跳过，或调用失败）{failed_note}。",
        }

    epsilon_lookup: dict[tuple[str, str], float] = {}
    for q_record in (epsilon_online_detail or {}).get("per_query", []):
        if q_record.get("skipped"):
            continue
        for physician, stats in (q_record.get("by_physician") or {}).items():
            if stats and stats.get("mean") is not None:
                epsilon_lookup[(q_record["query"], physician)] = stats["mean"]

    per_pair = []
    scored: list[tuple[float, float | None]] = []  # (distance, epsilon)，双侧空引用的样本不进这里
    n_empty_refs = 0
    n_own_low_discrimination = 0
    n_ablated_low_discrimination = 0
    for p in usable:
        distance = jaccard_distance(p["own_herbs"], p["ablated_herbs"])
        eps = epsilon_lookup.get((p["query"], p["physician"]))
        both_empty = bool(p.get("own_refs_empty")) and bool(p.get("ablated_refs_empty"))
        if both_empty:
            n_empty_refs += 1
            verdict = "empty_refs_excluded"
        elif eps is None:
            verdict = "no_epsilon_data"
        elif distance > eps:
            verdict = "above_epsilon"
        else:
            verdict = "within_epsilon"
        own_low_discrimination = bool(p.get("own_low_discrimination"))
        ablated_low_discrimination = bool(p.get("ablated_low_discrimination"))
        if own_low_discrimination:
            n_own_low_discrimination += 1
        if ablated_low_discrimination:
            n_ablated_low_discrimination += 1
        per_pair.append({
            "query": p["query"], "physician": p["physician"],
            "own_refs_ids": p.get("own_refs_ids", []),
            "own_refs_scores": p.get("own_refs_scores", []),
            "ablated_refs_ids": p.get("ablated_refs_ids", []),
            "own_herbs": sorted(p["own_herbs"]),
            "ablated_herbs": sorted(p["ablated_herbs"]),
            "jaccard": round(distance, 4),
            "epsilon": eps,
            "verdict": verdict,
            # P0-12：这次检索的候选是否被判定为"没有真实区分度"而收窄到 top-1。
            "own_low_discrimination": own_low_discrimination,
            "ablated_low_discrimination": ablated_low_discrimination,
        })
        if not both_empty:
            scored.append((distance, eps))

    n_scored = len(scored)
    change_rate = round(sum(d for d, _ in scored) / n_scored, 4) if n_scored else None

    n_above = sum(1 for d, eps in scored if eps is not None and d > eps)
    n_within = sum(1 for d, eps in scored if eps is not None and d <= eps)
    n_no_epsilon = sum(1 for _, eps in scored if eps is None)
    n_paired = n_above + n_within

    # change_rate 是 None（可用样本两侧检索全为空）时闸门本身无法判定——
    # 不能用 `change_rate is not None and ...` 简写，那样 None 会短路成
    # False，跟"跑了、但没通过"混在一起，跟上面"没有可用样本"分支里
    # gate_pass=None 的语义不一致。
    gate_pass = None if change_rate is None else change_rate >= GATE_OUTPUT_CHANGE_RATE
    empty_clause = (
        f"，其中 {n_empty_refs} 条两侧检索都为空（无参考医案可对照，改变率天然为 0），"
        "已从 change_rate 分母剔除" if n_empty_refs else ""
    )
    change_rate_clause = (
        f"{n_scored} 条计入 change_rate，用药 Jaccard 距离均值（改变率）={change_rate}，"
        f"闸门 ≥{GATE_OUTPUT_CHANGE_RATE}（{'通过' if gate_pass else '未通过'}）"
        if change_rate is not None
        else "计入 change_rate 的样本为 0（可用样本两侧检索全为空），无法判定闸门"
    )
    low_discrimination_clause = (
        f" own 侧 {n_own_low_discrimination}/{n_usable}"
        f"（{round(n_own_low_discrimination / n_usable, 3)}）、消融侧 "
        f"{n_ablated_low_discrimination}/{n_usable}"
        f"（{round(n_ablated_low_discrimination / n_usable, 3)}）"
        " 的检索候选被判定为没有真实区分度、收窄到了 top-1（LOW_DISCRIMINATION_CUTOFF）。"
        if n_usable else ""
    )
    failed_clause = (
        f"；另有 {n_failed} 条因 consult() 调用失败被跳过，不计入 change_rate 分母"
        if n_failed else ""
    )
    return {
        "label": label,
        "n_total": n_total, "n_usable": n_usable, "n_failed": n_failed,
        "n_empty_refs": n_empty_refs, "n_scored": n_scored,
        "change_rate": change_rate,
        "gate_threshold": GATE_OUTPUT_CHANGE_RATE,
        "gate_pass": gate_pass,
        "n_above_paired_epsilon": n_above,
        "n_within_paired_epsilon": n_within,
        "n_no_paired_epsilon": n_no_epsilon,
        "rate_above_paired_epsilon": round(n_above / n_paired, 3) if n_paired else None,
        # P0-12：own/消融两侧各自独立统计——它们是两次独立的检索调用，
        # 触发比例不必相同（比如 swapped 检索另一位医家的库，候选分布跟
        # own 不一样，区分度好不好可能也不一样）。
        "n_own_low_discrimination": n_own_low_discrimination,
        "n_ablated_low_discrimination": n_ablated_low_discrimination,
        "own_low_discrimination_rate": (
            round(n_own_low_discrimination / n_usable, 3) if n_usable else None
        ),
        "ablated_low_discrimination_rate": (
            round(n_ablated_low_discrimination / n_usable, 3) if n_usable else None
        ),
        "per_pair": per_pair,
        "note": (
            f"{label}：{n_usable}/{n_total} 条(主诉,医家)样本可用{empty_clause}{failed_clause}。"
            f"{change_rate_clause}。"
            "按主诉+医家配对跟噪声地板逐条比较（不是减一个全局 ε）："
            + (f"{n_above}/{n_paired} 条超出各自的噪声地板、算真实差异，"
               f"{n_within}/{n_paired} 条落在噪声地板以内、不算真实差异"
               if n_paired else "没有可配对的 epsilon_online 数据")
            + (f"；另有 {n_no_epsilon} 条查不到配对 ε，未计入这两个分母。"
               if n_no_epsilon else "。")
            + low_discrimination_clause
        ),
    }


# ---------- 6. 检索模式消融（E8）----------


def collect_retriever_mode_samples(
    queries: list[str], modes: list[str], consult_fn=None,
) -> list[dict]:
    """每条主诉、每种 RETRIEVER_MODE 各跑一次 consult()，按 (主诉, 医家)
    收集各模式的用药集合，供 retriever_mode_output_effect() 用。

    某个模式在这台机器上不可用（retrieval_error 非 None，比如 graph 模式缺
    data/element_index.json）时如实记下"不可用"，不静默跳过、也不拿别的
    模式的结果顶替——否则 E8 的数字悄悄只反映"能跑的那几个模式"，读者却
    以为四个模式都测了。

    consult_fn 调用本身失败（LLMError 等）跟 retrieval_error 是两件不同的
    事——前者是这次调用没跑成，后者是"这台机器上这个模式确实缺数据"的业务
    信号——不能记进同一个 unavailable_modes 列表，那样 retriever_mode_output_
    effect 的"graph 覆盖率不足"这类 caveat 会被"这次调用碰巧抖了几次"污染。
    调用失败单独记进 failed_modes（跟 unavailable_modes 平行的字段），该
    模式这次跳过，不拖累其它模式或其它查询。如果一条主诉所有模式全部失败/
    不可用（by_mode 是空的，没有任何 physician），不能让这条主诉的记录
    整体消失——否则失败率算不出来，读报告的人也看不出这条主诉发生了什么，
    单独补一条 physician=None 的兜底记录，把 failed_modes/unavailable_modes
    带出来；如果只是正常的"这条主诉被拦截/信息不足"（没有失败也没有不可用），
    保持原来的行为，不记录（那是业务上的空，不是需要追踪的异常）。
    """
    consult_fn = consult_fn or _default_consult_fn()
    records: list[dict] = []
    bar = Progress(total=len(queries) * len(modes),
                   label=f"E8 检索模式（{'/'.join(modes)}）", unit="轮")
    for query in queries:
        by_mode: dict[str, dict[str, set]] = {}
        unavailable: list[str] = []
        failed: list[str] = []
        for mode in modes:
            try:
                outcome = consult_fn(query, retriever_mode=mode, use_react=False, ask_fn=None)
                bar.advance(note=f"「{query[:12]}」{mode}")
            except Exception as e:  # noqa: BLE001 - 单个模式失败不拖累其它模式/查询
                print(
                    f"[collect_retriever_mode_samples] 「{query}」{mode} 调用失败："
                    f"{classify_llm_failure(e)}: {e}", file=sys.stderr,
                )
                bar.note(f"「{query[:16]}」{mode} 调用失败：{classify_llm_failure(e)}")
                failed.append(mode)
                continue
            if outcome.get("retrieval_error"):
                unavailable.append(mode)
                continue
            if outcome["rejected"] or outcome["insufficient"]:
                continue
            by_mode[mode] = {
                r["physician"]: normalized_herb_set(r["s3"].herbs) for r in outcome["results"]
            }
        physicians = {p for m in by_mode.values() for p in m}
        if physicians:
            for physician in physicians:
                herb_sets = [
                    by_mode[m][physician] for m in modes
                    if m in by_mode and physician in by_mode[m]
                ]
                records.append({
                    "query": query, "physician": physician,
                    "herb_sets": herb_sets, "n_modes_available": len(herb_sets),
                    "unavailable_modes": list(unavailable), "failed_modes": list(failed),
                })
        elif failed or unavailable:
            records.append({
                "query": query, "physician": None,
                "herb_sets": [], "n_modes_available": 0,
                "unavailable_modes": list(unavailable), "failed_modes": list(failed),
            })
    return records


def _element_index_coverage() -> tuple[int, int] | None:
    """(覆盖数, 总数)，data/element_index.json 不存在时返回 None。

    现算不硬编：之前 syndromes.jsonl 从 17 条扩到 337 条那一轮，写死过一次
    "2 位医家"这类会随数据增长漂移的数字，教训是别把"这台机器现在测出来是
    多少"焊死在代码里——element_index.json 会随 cases.json 重新抽取而变化，
    这里的覆盖数每次都从当前文件现算，caveat 文案永远跟这台机器上的真实
    文件一致。"""
    from core.retrieval_graph import ELEMENT_INDEX_PATH

    if not ELEMENT_INDEX_PATH.exists():
        return None
    index = json.loads(ELEMENT_INDEX_PATH.read_text(encoding="utf-8"))
    covered = sum(1 for v in index.values() if v.get("elements"))
    return covered, len(index)


def _graph_mode_caveat() -> str:
    coverage = _element_index_coverage()
    coverage_clause = (
        f"这台机器上实测 {coverage[0]}/{coverage[1]}（{coverage[0] / coverage[1]:.0%}）"
        if coverage and coverage[1] else "覆盖率未知（data/element_index.json 不存在，先跑 "
        "offline/build_element_index.py）"
    )
    return (
        f"graph 模式依赖 data/element_index.json 把医案连到证素，覆盖率不是 100%（{coverage_clause}）："
        "覆盖不到的医案在 graph 模式下证素集合为空、相似度恒 0，系统性排在检索结果之后。"
        "graph 模式如果输出差异更大/命中率更低，优先怀疑覆盖率而不是图检索方法本身——"
        "不要据此断言\"知识图谱检索不如向量检索\"这个结论。"
    )


def retriever_mode_output_effect(records: list[dict], modes: list[str]) -> dict:
    """E8：四种检索模式下 S3 用药输出的差异率。

    每条 (主诉, 医家) 样本有 N（≤len(modes)）个用药集合——不同检索模式跑出
    来的。用 core.setstats.pairwise_jaccard_stats 算这 N 个集合两两的
    Jaccard 距离均值，再用 aggregate_stats 把所有样本的均值聚合成一个整体
    数——这跟 offline/estimate_epsilon.py 的"N 次重复两两距离再聚合"是完全
    同一个统计手法，只是这里的"N 次重复"换成了"N 种检索模式"，不新写一套。

    failed_modes（consult() 调用失败）跟 unavailable_modes（retrieval_error
    业务信号）分别聚合成 n_failed_by_mode / n_unavailable_by_mode，note 里
    分开报——原因见 collect_retriever_mode_samples 的说明，两者混在一起会让
    "graph 覆盖率不足"这类结论被调用抖动污染。physician=None 的兜底记录
    （某条主诉所有模式全部失败/不可用）天然进不了 usable，单独计进
    n_failed_queries，否则一条主诉整体失败会从这份报告里完全消失。
    """
    usable = [r for r in records if r["n_modes_available"] >= 2]
    n_total = len(records)
    n_usable = len(usable)
    n_failed_queries = sum(1 for r in records if r.get("physician") is None)
    unavailable_counts: dict[str, int] = {m: 0 for m in modes}
    failed_counts: dict[str, int] = {m: 0 for m in modes}
    for r in records:
        for m in r.get("unavailable_modes", []):
            unavailable_counts[m] = unavailable_counts.get(m, 0) + 1
        for m in r.get("failed_modes", []):
            failed_counts[m] = failed_counts.get(m, 0) + 1

    graph_caveat = _graph_mode_caveat() if "graph" in modes else None
    result = {
        "modes": modes, "n_total": n_total, "n_usable": n_usable,
        "n_failed_queries": n_failed_queries,
        "n_unavailable_by_mode": unavailable_counts,
        "n_failed_by_mode": failed_counts,
        "graph_mode_caveat": graph_caveat,
    }
    if not usable:
        result.update({
            "output_difference_rate": None, "p50": None, "p95": None,
            "note": "没有可比较的样本（每条(主诉,医家)至少要 2 种模式都跑出结果）。",
        })
        return result

    per_sample_stats = [pairwise_jaccard_stats(r["herb_sets"]) for r in usable]
    per_sample_means = [s["mean"] for s in per_sample_stats if s]
    overall = aggregate_stats(per_sample_means) or {"mean": None, "p50": None, "p95": None}

    unavailable_note = "；".join(
        f"{m} 在 {c} 条查询上不可用" for m, c in unavailable_counts.items() if c
    )
    failed_note = "；".join(
        f"{m} 在 {c} 条查询上调用失败" for m, c in failed_counts.items() if c
    )
    result.update({
        "output_difference_rate": overall["mean"], "p50": overall["p50"], "p95": overall["p95"],
        "note": (
            f"{n_usable}/{n_total} 条(主诉,医家)样本至少有 2 种模式跑出结果，"
            f"S3 用药输出跨模式差异率（两两 Jaccard 距离均值的均值）="
            f"{overall['mean']}（p50={overall['p50']}, p95={overall['p95']}）。"
            + (f" 另有模式不可用：{unavailable_note}。" if unavailable_note else "")
            + (f" 另有 {n_failed_queries} 条查询所有模式全部失败/不可用（调用失败：{failed_note}）。"
               if n_failed_queries else "")
            + (f" {graph_caveat}" if graph_caveat else "")
        ),
    })
    return result


# ---------- 7. ReAct 消融（E9）----------


_TERMINATED_BY_PROMPT_CAVEAT = (
    "terminated_by 的分布对 prompt 措辞敏感（SOURCES.md 第 12 条）：去掉"
    "prompt 里的剩余步数计数后，max_steps 收尾的比例从 0/5 涨到 4/8——这个"
    "分布会随 prompt 微调而变化，不是模型能力的稳定特征，引用这份分布时要"
    "连带说明当时的 prompt 版本，不能当成跟 prompt 无关的固定指标。"
)


def collect_react_process_samples(queries: list[str], consult_fn=None) -> list[dict]:
    """E9 的过程性统计（不是输出差异）：use_react=True 跑一次，收集每位医家
    react_trace 的步数和 terminated_by。

    跟 collect_ablation_pairs(..., ablated_kwargs={"use_react": True}) 分开跑
    ——那个要基线+消融各一次才能算输出差异率，这个只需要 react=True 那一侧
    的过程数据，两个函数各自负责一件事，不把"顺便再要点别的东西"混进
    收集输出差异率的那个函数里。

    这里跟 collect_ablation_pairs 一样单条失败容忍：这个函数只产出过程性
    统计（步数分布、terminated_by），不进 change_rate 分母，所以失败的
    查询直接跳过、打到 stderr 就够，不需要像另外三个收集器那样记
    skip_reason/算 n_failed——它不是"某个数字的分母需要排除失败样本"这类
    问题，单纯是"这条主诉这次没跑成，别拖累其余主诉"。
    """
    consult_fn = consult_fn or _default_consult_fn()
    records: list[dict] = []
    bar = Progress(total=len(queries), label="E9 过程统计（use_react=True）", unit="条")
    for query in queries:
        try:
            outcome = consult_fn(query, use_react=True, ask_fn=None)
            bar.advance(note=f"「{query[:12]}」")
        except Exception as e:  # noqa: BLE001 - 单条失败不能拖累其余（core.chain.consult_many 同一模式）
            print(f"[collect_react_process_samples] 「{query}」调用失败：{classify_llm_failure(e)}: {e}", file=sys.stderr)
            bar.note(f"「{query[:16]}」调用失败：{classify_llm_failure(e)}")
            continue
        if outcome["rejected"] or outcome["insufficient"]:
            continue
        for r in outcome["results"]:
            trace = r.get("react_trace")
            if trace is None:
                continue
            records.append({
                "query": query, "physician": r["physician"],
                "n_steps": len(trace.steps), "terminated_by": trace.terminated_by,
                "llm_calls": trace.llm_calls, "steps": trace.steps,
            })
    bar.close(f"{len(records)} 条 trace")
    return records


# report.json 里存的动作序列样本数上限——存全部样本会让 report.json 跟着
# 查询数量线性膨胀，10 条足够看清"工具调用分布长什么样"这个问题，不是为了
# 穷举每一条。
_TRACE_SAMPLE_LIMIT = 10
# observation/thought 存进 report.json 时的截断长度——完整 observation
# 有的上千字（search_cases 一次返回三条完整医案），存全量会让 report.json
# 没法直接打开看，只留头部够诊断"这一步查了什么、大致查到了什么"就够。
_ACTION_INPUT_SUMMARY_LEN = 60
_THOUGHT_HEAD_LEN = 100
_OBSERVATION_HEAD_LEN = 100


def _step_sample(step) -> dict:
    return {
        "step": step.step,
        "action": step.action,
        "action_input_summary": str(step.action_input)[:_ACTION_INPUT_SUMMARY_LEN],
        "thought_head": (step.thought or "")[:_THOUGHT_HEAD_LEN],
        "observation_head": (step.observation or "")[:_OBSERVATION_HEAD_LEN],
    }


def react_process_summary(records: list[dict]) -> dict:
    """E9 的步数分布 + terminated_by 分布 + 前 _TRACE_SAMPLE_LIMIT 条完整
    动作序列样本。

    step_distribution 复用 core.setstats.aggregate_stats 算 mean/p50/p95——
    它原本是给"多组已经算好的统计量再聚合一层"设计的，这里直接喂原始步数
    列表：数学上就是同一个 mean/p50/p95 计算，没有必要为了"输入形状不是
    严格意义上的统计量"再写一份一模一样的分位数代码。

    samples 是这轮（P1 ReAct 修复）新加的：光有分布统计看不出"工具调用
    分布 73% 落在国标层"这类问题具体是怎么发生的——上一轮就是靠手写
    python -c 现抓的三条 trace 才诊断出根因，很不方便。records 里的
    "steps" 字段是可选的（不是所有调用方都会附带完整轨迹，见
    test_react_process_summary_distributions 那条不带 steps 的record），
    缺失时该条样本的 steps 就是空列表，不报错。
    """
    n = len(records)
    if not n:
        return {
            "n_samples": 0, "step_distribution": None, "terminated_by_distribution": {},
            "terminated_by_caveat": _TERMINATED_BY_PROMPT_CAVEAT, "samples": [],
            "note": "没有可用样本（全部被拦截/信息不足，或本次没有开 ReAct 的结果）。",
        }
    terminated_by_counts: dict[str, int] = {}
    for r in records:
        terminated_by_counts[r["terminated_by"]] = terminated_by_counts.get(r["terminated_by"], 0) + 1
    step_stats = aggregate_stats([float(r["n_steps"]) for r in records])
    samples = [
        {
            "query": r["query"], "physician": r["physician"], "terminated_by": r["terminated_by"],
            "steps": [_step_sample(s) for s in r.get("steps", [])],
        }
        for r in records[:_TRACE_SAMPLE_LIMIT]
    ]
    return {
        "n_samples": n,
        "step_distribution": step_stats,
        "terminated_by_distribution": terminated_by_counts,
        "terminated_by_caveat": _TERMINATED_BY_PROMPT_CAVEAT,
        "samples": samples,
        "note": (
            f"{n} 条 (主诉,医家) 样本开了 ReAct：步数 mean={step_stats['mean']} "
            f"p50={step_stats['p50']} p95={step_stats['p95']}；"
            f"terminated_by 分布：{terminated_by_counts}。{_TERMINATED_BY_PROMPT_CAVEAT}"
        ),
    }


# ---------- 8. 分歧度 per-query 明细 ----------


def divergence_per_query_detail(
    consult_results: list[dict], epsilon_online_detail: dict | None,
) -> list[dict]:
    """report.json 里逐条查询的分歧明细：query / herb_jaccard / 该条的噪声
    地板 ε / 净差异（herb_jaccard - ε）/ 判定。

    跟 divergence_vs_epsilon 的汇总数字并列存在，不是取代它：汇总回答
    "总体有多少条超过阈值"，这里回答"是哪几条、差多少"——全局 ε 一刀切在
    ε 按证型强烈分层时（同类 ε 可以从 ~0 到 ~0.5）会系统性判错：ε 小的
    主诉上漏判真分歧、ε 大的主诉上把噪声当分歧，逐条摊开才看得出来是哪种。

    每条 consult_results 必须带 "query" 字段（main() 里从 consult_many 的
    结果按位置跟 queries 配对后带出来，不能假设"跟 queries 列表位置对齐"
    ——之前这里就是靠位置对齐的，第 N 条失败被过滤掉之后第 N+1 条就会错位，
    见 main() 里的修复）。没有这个字段的样本判定为 "no_query_tag"，不猜一个
    query 名字。

    ε 取该主诉 by_physician 里所有医家噪声均值的算术平均——divergence 的
    herb_jaccard 本身是跨医家的"全体一致性"量度，不对应某一位医家，配对
    ε 也就只能是该主诉下能拿到的几位医家噪声的一个综合近似值，不是某一位
    医家的精确噪声。这是近似，写清楚比装作精确更诚实。
    """
    per_query_epsilon: dict[str, float] = {}
    for q_record in (epsilon_online_detail or {}).get("per_query", []):
        if q_record.get("skipped"):
            continue
        means = [
            s["mean"] for s in (q_record.get("by_physician") or {}).values()
            if s and s.get("mean") is not None
        ]
        if means:
            per_query_epsilon[q_record["query"]] = round(sum(means) / len(means), 4)

    out = []
    for r in consult_results:
        query = r.get("query")
        if query is None:
            out.append({"query": None, "herb_jaccard": None, "epsilon": None,
                        "net_difference": None, "verdict": "no_query_tag"})
            continue
        hj = None if (r["rejected"] or r["insufficient"]) else (r.get("divergence") or {}).get("herb_jaccard")
        eps = per_query_epsilon.get(query)
        if hj is None:
            verdict = "unusable"
            net = None
        elif eps is None:
            verdict = "no_epsilon_data"
            net = None
        else:
            net = round(hj - eps, 4)
            verdict = "real_divergence" if net > 0 else "within_noise_floor"
        out.append({
            "query": query, "herb_jaccard": hj, "epsilon": eps,
            "net_difference": net, "verdict": verdict,
        })
    return out


# ---------- 汇总与报告 ----------


def school_pair_summary(consult_results: list[dict]) -> dict:
    """1.3（E2）的判据：「跨学派分歧 > 师承内分歧」在多数主诉上成立——报出，
    不是硬闸门。逐条读 divergence 里的 lineage_mean / cross_school_mean
    （core/chain.py::pairwise_divergence 算的），只数两个都有值的主诉；
    被拦截/信息不足/只有两位医家（没有跨学派对）的主诉不计入分母，报在
    n_skipped 里，不让它们把"成立比例"稀释成一个假数。"""
    n_cross_gt = 0
    n_comparable = 0
    n_skipped = 0
    lineage_vals: list[float] = []
    cross_vals: list[float] = []
    for r in consult_results:
        div = None if (r.get("rejected") or r.get("insufficient")) else (r.get("divergence") or {})
        lm = (div or {}).get("lineage_mean")
        cm = (div or {}).get("cross_school_mean")
        if lm is None or cm is None:
            n_skipped += 1
            continue
        n_comparable += 1
        lineage_vals.append(lm)
        cross_vals.append(cm)
        if cm > lm:
            n_cross_gt += 1
    lineage_mean = round(sum(lineage_vals) / len(lineage_vals), 3) if lineage_vals else None
    cross_mean = round(sum(cross_vals) / len(cross_vals), 3) if cross_vals else None
    holds_on_majority = (n_cross_gt * 2 > n_comparable) if n_comparable else None
    return {
        "n_comparable": n_comparable,
        "n_skipped": n_skipped,
        "n_cross_school_gt_lineage": n_cross_gt,
        "lineage_mean": lineage_mean,
        "cross_school_mean": cross_mean,
        "holds_on_majority": holds_on_majority,
        "note": (
            "没有可比较的主诉（全部被拦截/信息不足，或注册表里不足两个学派、"
            "没有跨学派配对）——1.3 的判据无法评估，不是「不成立」。"
            if not n_comparable else
            f"{n_comparable} 条主诉可比较（另 {n_skipped} 条不计入）：其中 "
            f"{n_cross_gt} 条跨学派分歧 > 师承内分歧（{n_cross_gt}/{n_comparable}），"
            f"师承内均值 {lineage_mean} vs 跨学派均值 {cross_mean}。"
            f"判据「多数主诉成立」：{'成立' if holds_on_majority else '不成立'}"
            "（只报出，不作硬闸门；两位医家同为温病学派、第三位衷中参西派，"
            "学派分组来自 core/physicians.py 的 school 字段）。"
        ),
    }


def backend_tags(consult_results: list[dict]) -> dict:
    """这一批数字是**哪个后端、哪个模型**跑出来的。从每条结果自带的 manifest 里抬
    上来，不从 LLM_MODEL 环境变量读（那个变量在 claude_cli / replay 后端下还是
    deepseek-chat，照抄就等于把别的模型跑的结果标成 DeepSeek 跑的）。

    **一份报告里不该混两个后端。** 训练完之后本地模型要重跑这整张表，正确做法是
    跑两次、两份报告并列，不是一次跑里一半 DeepSeek 一半本地——那样出来的每个
    汇总数字都是两个模型的平均，既不能跟论文比也不能跟自己之前比。所以这里把
    `mixed` 和 `note` 一起交出去，混了就在报告顶部写明。

    `replayed_from` 非 None 意味着这批数字是回放录制的结果，不是实时调用（R3）。
    它必须跟着进报告：一个回放出来的数字看起来和实时跑的一模一样，那句「这是我们
    系统跑出来的」就变成了假的。
    """
    # 只认真的带了 model/backend 的 manifest。有 manifest 但里面没有这两个键
    # （老结果、测试里构造的最小结果）算"未标"，不算"后端是 None"——
    # 报一个 None 出去，读的人会以为那是某个真实后端的名字。
    manifests = [m for m in (r.get("manifest") or {} for r in consult_results
                             if isinstance(r, dict))
                 if m.get("model") or m.get("backend")]

    def distinct(key: str) -> list:
        seen: list = []
        for m in manifests:
            v = m.get(key)
            if v not in seen:
                seen.append(v)
        return seen

    models, backends = distinct("model"), distinct("backend")
    replayed = [v for v in distinct("replayed_from") if v]
    warnings = [v for v in distinct("comparability_warning") if v]
    mixed = len(models) > 1 or len(backends) > 1
    if not manifests:
        note = ("⚠ 这批结果里没有 manifest，**后端和模型未知**。没有后端标签的数字"
                "不能跟任何别的数字比——先确认 consult 结果带上 manifest 再报数。")
    elif mixed:
        note = (f"⚠⚠ 这一份报告混了多个后端/模型（模型 {models}，后端 {backends}）。"
                "每个汇总数字都变成了跨模型的平均，既不能跟论文比也不能跟自己之前的"
                "数比。正确做法是每个后端各跑一次、两份报告**并列**，不是混在一次跑里。")
    else:
        note = f"模型 {models[0]}，后端 {backends[0]}。"
        if replayed:
            note += "**这批数字是回放录制结果，不是实时调用**（LLM_MODE=replay）。"
        if warnings:
            note += " ".join(warnings)
    return {
        "models": models, "backends": backends,
        "lora_dirs": distinct("lora_dir"),
        "replayed_from": replayed,
        "comparability_warnings": warnings,
        "mixed": mixed,
        "note": note,
    }


def build_report(
    consult_results: list[dict],
    retrieval_comparisons: list[dict] | None = None,
    ablations: list[dict] | None = None,
    retriever_mode_effect: dict | None = None,
    react_process: dict | None = None,
) -> dict:
    """consult_results 里每条如果带 "query" 字段，divergence_per_query 就能
    逐条对上 epsilon_online_detail；不带就退化成 verdict="no_query_tag"，
    不强行猜一个 query 名字（main() 负责把 query 字段带上，见下面的修复）。
    """
    epsilon_detail = load_epsilon_online_detail()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # 后端标签放在最前面：报告里每一个数字都归这一行管。没有它，
        # 「E3 0.451」这个数不知道是谁跑的，跟训练后本地模型的 0.4x 并列时
        # 分不出是模型变了还是代码变了。
        "backend": backend_tags(consult_results),
        "n_queries": len(consult_results),
        "divergence_vs_epsilon": divergence_vs_epsilon(consult_results, load_epsilon_online()),
        # 逐条明细：跟上面的汇总并列存在，不是取代它——见
        # divergence_per_query_detail 的文档字符串。
        "divergence_per_query": divergence_per_query_detail(consult_results, epsilon_detail),
        # 1.3（E2）：师承内 vs 跨学派的两两配对汇总，判据只报出不硬卡。
        "school_pairs": school_pair_summary(consult_results),
        "hallucination": hallucination_by_reference_availability(consult_results),
        "safety_veto": safety_veto_summary(consult_results),
        "retrieval_mode_comparisons": retrieval_comparisons or [],
        # E3/E4/E9：ablation_output_effect() 的产出列表，跑了哪几个消融就有
        # 几条（"swapped"/"none"/"react_on"），按 main() 的 --e3/--e4/--e9
        # 开关决定，不在这里假设一定跑了哪几个。
        "ablations": ablations or [],
        # E8：retriever_mode_output_effect() 的产出，未跑时为 None（不是
        # 空字典——None 才如实表达"这次没测"，空字典容易被误读成"测了、
        # 没有样本"）。
        "retriever_mode_effect": retriever_mode_effect,
        # E9 的过程性统计（步数/terminated_by 分布），跟上面 ablations 里
        # label="react_on" 的输出差异率并列存在，回答的是不同问题。
        "react_process": react_process,
    }


def render_markdown(report: dict) -> str:
    lines = [
        f"# V1 评测汇总（{report['generated_at']}）",
        "",
        f"**后端**：{report.get('backend', {}).get('note', '未知（旧版报告没有这一行）')}",
        "",
        f"共 {report['n_queries']} 条查询。以下每项数字旁边都带着它的对照基准"
        "（CLAUDE.md：任何数字都必须带对照）。",
        "",
        "## 分歧度 vs 噪声地板 ε",
        report["divergence_vs_epsilon"]["note"],
        "",
        "### 逐条明细",
        "| 主诉 | herb_jaccard | ε | 净差异 | 判定 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for d in report.get("divergence_per_query") or []:
        lines.append(
            f"| {d['query']} | {d['herb_jaccard']} | {d['epsilon']} | "
            f"{d['net_difference']} | {d['verdict']} |"
        )
    lines += [
        "",
        "## 幻觉率（按参考医案可得性分组）",
        report["hallucination"]["note"],
        "",
        "## 安全否决率 + 代价",
        report["safety_veto"]["note"],
        "",
        "## 检索模式对比（top-1 命中率，McNemar）",
    ]
    if report["retrieval_mode_comparisons"]:
        for c in report["retrieval_mode_comparisons"]:
            lines.append(f"- {c['note']}")
    else:
        lines.append("（本次未跑检索模式对比）")
    lines.append("")
    lines.append("## 学派两两配对（E2：师承内 vs 跨学派）")
    lines.append(f"- {report['school_pairs']['note']}")
    lines.append("")
    lines.append("## 消融（E3 own vs swapped / E4 own vs none / E9 react off vs on）")
    if report["ablations"]:
        for a in report["ablations"]:
            lines.append(f"- {a['note']}")
    else:
        lines.append("（本次未跑消融，加 --e3 / --e4 / --e9）")
    lines.append("")
    lines.append("## 检索模式消融（E8，S3 输出差异率）")
    if report["retriever_mode_effect"]:
        lines.append(f"- {report['retriever_mode_effect']['note']}")
    else:
        lines.append("（本次未跑，加 --e8）")
    lines.append("")
    lines.append("## ReAct 过程统计（E9）")
    if report["react_process"]:
        lines.append(f"- {report['react_process']['note']}")
    else:
        lines.append("（本次未跑，加 --e9）")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="V1：评测汇总（需要真实 LLM，跑 core.chain.consult）")
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH)
    ap.add_argument("--out-json", type=Path, default=DEFAULT_REPORT_JSON_PATH)
    ap.add_argument("--out-md", type=Path, default=DEFAULT_REPORT_MD_PATH)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="只打印预估调用数，不真的跑")
    ap.add_argument("--e3", action="store_true", help="另外跑 E3 消融（refs_mode own vs swapped）")
    ap.add_argument("--e4", action="store_true", help="另外跑 E4 消融（refs_mode own vs none）")
    ap.add_argument("--e8", action="store_true", help="另外跑 E8 消融（四种 RETRIEVER_MODE 的输出差异率）")
    ap.add_argument("--e9", action="store_true", help="另外跑 E9 消融（use_react False vs True）")
    args = ap.parse_args(argv)

    queries = [
        line.strip() for line in args.queries_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if args.limit is not None:
        queries = queries[: args.limit]

    # 每条查询的基础调用数：S1+S2 两次，每位医家 S3 通常 1 次、配伍禁忌命中时
    # 可能重开 1 次——不逐条精确统计，给个数量级。**按 len(PHYSICIANS) 动态算，
    # 不写死"2 位医家"**：PHYSICIANS 现在是 2 位，张锡纯加入后这行不用跟着改，
    # 之前写死过一次"2 位医家"就在这类地方漏改过。
    base_calls_per_query = 2 + len(PHYSICIANS) * 2
    if args.dry_run:
        total = len(queries) * base_calls_per_query
        detail = [f"{len(queries)} 条查询 × ~{base_calls_per_query} 次/条（{len(PHYSICIANS)} 位医家 S3、配伍禁忌可能重开）"]
        if args.e3 or args.e4:
            # E3/E4 共享一次 own（collect_refs_mode_pairs），再各跑一次 swapped/none。
            n_ablated_modes = int(args.e3) + int(args.e4)
            e34_total = len(queries) * base_calls_per_query * (1 + n_ablated_modes)
            total += e34_total
            names = "+".join(n for n, on in (("swapped", args.e3), ("none", args.e4)) if on)
            detail.append(f"--e3/--e4 另加 {len(queries)} 条 × ~{base_calls_per_query} 次/条 × "
                           f"{1 + n_ablated_modes}轮（own 共享 1 轮 + {names} 各一轮）")
        if args.e8:
            e8_total = len(queries) * base_calls_per_query * len(ALLOWED_MODES)
            total += e8_total
            detail.append(f"--e8 另加 {len(queries)} 条 × ~{base_calls_per_query} 次/条 × "
                           f"{len(ALLOWED_MODES)}种模式（{sorted(ALLOWED_MODES)}）")
        if args.e9:
            # use_react=False 那一轮跟 base_calls_per_query 同形状；use_react=True
            # 那一轮每位医家最多 MAX_STEPS 次 ReAct 调用 + S3(含可能重开)，这是
            # 真正的调用数上界（不是"量级估计，可能更高"）——ReAct 提前 finish
            # 时实际调用数只会更少，不会超过它。True 那一轮要跑两次：一次算
            # 跟 False 的输出差异率，一次单独收集步数/terminated_by 过程统计。
            react_upper_per_query = 2 + len(PHYSICIANS) * (MAX_STEPS + 2)
            e9_total = len(queries) * (base_calls_per_query + 2 * react_upper_per_query)
            total += e9_total
            detail.append(
                f"--e9 另加 {len(queries)} 条：use_react=False 一轮 ~{base_calls_per_query} 次/条 "
                f"+ use_react=True 两轮（差异率一轮、过程统计一轮）各 ≤{react_upper_per_query} 次/条"
                f"（{len(PHYSICIANS)} 位医家 × 最多 MAX_STEPS={MAX_STEPS} 步 ReAct + S3，"
                "这是真正的调用数上界，ReAct 提前 finish 只会更省）"
            )
        print(f"--dry-run：预估调用数 ≈ {total}（{'；'.join(detail)}），不真的调用")
        return

    from core.chain import consult_many

    results, failures = consult_many(queries)
    # 按位置跟 queries 配对，再过滤失败的——不能先过滤再假设剩下的还跟
    # queries 位置对齐（第 N 条失败被去掉之后，原来的第 N+1 条就错位了）。
    # eval/mes/export.py 一直是这么做的，这里补上同样的修复。
    consult_results = [{**r, "query": q} for q, r in zip(queries, results) if r is not None]
    report = build_report(consult_results)
    # 失败的主诉进报告而不是只打在终端：n_queries 少了几条要能从报告本身看出来
    report["failed_queries"] = failures
    if failures:
        print(f"注意：{len(failures)}/{len(queries)} 条主诉失败，已写进 report.json 的 failed_queries")

    if args.e3 or args.e4:
        ablated_modes = [m for m, on in (("swapped", args.e3), ("none", args.e4)) if on]
        print(f"正在跑 {'/'.join('E3' if m == 'swapped' else 'E4' for m in ablated_modes)}"
              f"（own vs {'/'.join(ablated_modes)}）……")
        epsilon_detail = load_epsilon_online_detail()
        pairs_by_mode = collect_refs_mode_pairs(queries, ablated_modes)
        for mode in ablated_modes:
            effect = ablation_output_effect(pairs_by_mode[mode], epsilon_detail, mode)
            report["ablations"].append(effect)
            tag = "E3" if mode == "swapped" else "E4"
            print(f"{tag}：{effect['note']}")
            warn_if_failure_rate_high(tag, effect["n_failed"], effect["n_total"])

    if args.e8:
        print(f"正在跑 E8（{sorted(ALLOWED_MODES)} 四种检索模式）……")
        records = collect_retriever_mode_samples(queries, sorted(ALLOWED_MODES))
        e8 = retriever_mode_output_effect(records, sorted(ALLOWED_MODES))
        report["retriever_mode_effect"] = e8
        print(f"E8：{e8['note']}")
        warn_if_failure_rate_high("E8", e8["n_failed_queries"], e8["n_total"])

    if args.e9:
        print("正在跑 E9（use_react False vs True）……")
        epsilon_detail = load_epsilon_online_detail()
        pairs = collect_ablation_pairs(
            queries, {"use_react": False}, {"use_react": True},
        )
        e9_effect = ablation_output_effect(pairs, epsilon_detail, "react_on")
        report["ablations"].append(e9_effect)
        print(f"E9（输出差异）：{e9_effect['note']}")
        warn_if_failure_rate_high("E9（输出差异）", e9_effect["n_failed"], e9_effect["n_total"])

        process_records = collect_react_process_samples(queries)
        e9_process = react_process_summary(process_records)
        report["react_process"] = e9_process
        print(f"E9（过程统计）：{e9_process['note']}")

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    args.out_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"已写出 {args.out_json} 和 {args.out_md}")


if __name__ == "__main__":
    main()
