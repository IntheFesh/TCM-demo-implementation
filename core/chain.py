"""结构化推理链（SRC）：症状标准化 -> 证素推断 -> 检索 -> 证候辨析。全项目核心。

数据流（严格按此，不要改）：

    S1 症状标准化：全局只跑一次，两位医家共用结果
      |
      对每位医家（顺序执行，不用 asyncio）：
        S2 证素推断（注入 $elements）
        检索该医家 top-3 医案
        S3 证候+治法+方（注入 $name 和参考医案）

S1 必须只跑一次：如果对每位医家各跑一次，两次输出的症状列表会不同，
后面构图时症状节点 id 对不上，边会指向不存在的节点。
"""
from __future__ import annotations

import time

from core import herbs as _herbs
from core.elements import ELEMENTS, LOCATIONS, NATURES
from core.llm import get_llm, load_prompt, render
from core.followup import AskFn, format_followup_for_s3, run_followup
from core.physicians import PHYSICIANS
from core.react import format_trace_for_s3, react_enabled, run_react
from core.retrieval import MIN_RETRIEVAL_SCORE, get_retriever
from core.safety import check_safety
from core.safety_output import (
    check_incompatible,
    check_thermal_consistency,
    format_conflicts,
)
from core.schemas import (
    CaseRecord, FollowupResult, S1Normalize, S2Elements, S3Syndrome, S3SyndromeUnreferenced,
)

# MIN_RETRIEVAL_SCORE 挪到 core/retrieval.py 了（ReAct 的 search_cases 工具也要用同一个
# 阈值，而 tools 不能反向 import chain）。这里 re-export，老调用方不受影响。


class SafetyVeto(Exception):
    """推理过程中（不是初始主诉里）冒出危重信号时抛出：ReAct 的 ask_user 追问收到的
    回答命中 check_safety。抛异常而不是返回值，是因为发生点在 run_physician 深处，
    而处理点（不产出任何方药、返回拒绝）只能在 consult 这一层。"""

    def __init__(self, reason: str, llm_calls: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.llm_calls = llm_calls

# 残差辨证触发阈值：未解释症状 >=2 条 且 占比 >=30% 时，用这些症状再跑一轮，
# 看能不能构成兼夹证。S2 共享之后未解释症状是全局唯一一份，所以残差也只跑一次，
# 结果两位医家共用——这比原方案（每位医家各跑一轮）省一半调用，也更一致。
RESIDUAL_MIN_COUNT = 2
RESIDUAL_THRESHOLD = 0.30
RESIDUAL_MAX_ROUNDS = 1


# 药名归一挪到 core/herbs.py 了：core/safety_output.py 也要用它，留在这里会
# 造成 chain ↔ safety_output 循环导入。这里 re-export，老调用方不受影响。
HERB_ALIASES = _herbs.HERB_ALIASES
normalize_herb = _herbs.normalize_herb
strip_dose = _herbs.strip_dose


def _format_case_line(case: CaseRecord) -> str:
    """把一个参考医案压缩成一行，喂给 S3 prompt。"""
    symptoms = "；".join(case.symptoms) if case.symptoms else "无"
    herbs = "、".join(case.herbs) if case.herbs else "无"
    vi = case.visit_index or 0
    visit_desc = "初诊" if vi == 0 else f"第{vi + 1}诊"
    fields = [
        f"id={case.case_id}",
        f"诊次={visit_desc}",
        f"症状={symptoms}",
        f"舌={case.tongue or '未记'}",
        f"脉={case.pulse or '未记'}",
        f"证={case.syndrome or '未记'}",
        f"病机={case.pathogenesis or '未记'}",
        f"治法={case.treatment_principle or '未记'}",
        f"方={case.formula or '未记'}",
        f"药={herbs}",
    ]
    return "；".join(fields)


def _format_elements_summary(s2: S2Elements) -> str:
    """把 S2 的证素命中列表压缩成一段文本，喂给 S3 prompt。"""
    if not s2.elements:
        return "（未推断出明确证素）"
    lines = []
    for hit in s2.elements:
        supporting = "、".join(hit.supporting_symptoms)
        lines.append(
            f"{hit.element}（{hit.kind}，置信度{hit.confidence}，依据：{supporting}）"
        )
    return "；".join(lines)


def explained_symptoms(s1: S1Normalize, s2: S2Elements, residual: dict | None = None) -> set[str]:
    """s1.symptoms 里被 S2（以及残差辨证）的 supporting_symptoms 引用到的那些。

    **只返回 s1.symptoms 的子集。** S2 常把症状名改写（「胃脘胀痛」→「脘腹胀痛」），
    改写后的名字不在 s1 里，直接拿 supporting_symptoms 的集合当分子，coverage 会超过 1
    （实测 1.5）。「哪些症状算已解释」此前在 consult、run_residual、api.to_graph 三处
    各写了一套，这里收成一处——CLAUDE.md「同一概念的匹配逻辑只能有一处实现」。
    """
    referenced = {sym for hit in s2.elements for sym in hit.supporting_symptoms}
    referenced |= set((residual or {}).get("newly_explained") or [])
    return {s for s in s1.symptoms if s in referenced}


def normalize(complaint: str) -> S1Normalize:
    prompt = load_prompt("s1_normalize")
    system = render(prompt["system"], complaint=complaint)
    return get_llm().generate(system=system, user="", schema=S1Normalize)


def infer_elements(s1: S1Normalize) -> S2Elements:
    """S2 证素推断。全局只跑一次，所有医家共用——理由同 S1：

    s2_elements.yaml 的占位符里没有 $name，模型根本不知道自己在为哪位医家推断，
    temperature=0 下对每位医家各跑一次只会得到几乎相同的结果，白花调用。
    设计上医家条件化发生在 S3（通过检索到的该医家医案），S2 是客观的证素抽取。
    图上证素层本来也是所有医家共享同一批节点（api/main.py 的 elem:: 去重）。
    """
    symptoms_text = "；".join(s1.symptoms)
    s2_prompt = load_prompt("s2_elements")
    s2_system = render(
        s2_prompt["system"],
        elements=(
            f"病位证素（kind 填 location）：{'、'.join(LOCATIONS)}\n"
            f"  病性证素（kind 填 nature）：{'、'.join(NATURES)}"
        ),
        symptoms=symptoms_text,
        tongue=s1.tongue or "未记",
        pulse=s1.pulse or "未记",
    )
    return get_llm().generate(system=s2_system, user="", schema=S2Elements)


def run_physician(
    s1: S1Normalize,
    s2: S2Elements,
    physician: str,
    physician_name: str,
    use_react: bool = False,
    followup: FollowupResult | None = None,
    ask_fn: AskFn | None = None,
) -> dict:
    symptoms_text = "；".join(s1.symptoms)

    # 检索该医家 top-3 医案
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"
    hits = get_retriever().search(query, physician, k=3, min_score=MIN_RETRIEVAL_SCORE)
    # 一条相关医案都没有时换用不含 cited_case_ids 的 schema（见 S3SyndromeUnreferenced
    # 的文档字符串）。不是放松 min_length=1，是这个场景下根本没有可引用的东西。
    s3_schema = S3Syndrome if hits else S3SyndromeUnreferenced
    # refs 要给前端证据链侧栏用：只给 (id, score) 的话，用户看到
    # ye_tianshi-0031-p6-0 完全不知道那是什么医案，"可追溯"这个卖点就断在这里。
    refs = [
        {
            "case_id": case.case_id,
            "score": round(score, 3),
            "visit_index": case.visit_index or 0,
            "visit_label": "初诊" if not case.visit_index else f"第{case.visit_index + 1}诊",
            "symptoms": case.symptoms or [],
            "tongue": case.tongue,
            "pulse": case.pulse,
            "syndrome": case.syndrome,
            "treatment_principle": case.treatment_principle,
            "formula": case.formula,
            "herbs": case.herbs or [],
            # 该诊次对应的原文片段（不是整段粗段）
            "excerpt": case.raw_excerpt,
        }
        for case, score in hits
    ]
    refs_text = "\n".join(_format_case_line(case) for case, _ in hits) or "（无可用参考医案）"

    # S3 证候+治法+方
    s3_prompt = load_prompt("s3_syndrome")
    s3_system = render(
        s3_prompt["system"],
        name=physician_name,
        elements_summary=_format_elements_summary(s2),
        symptoms=symptoms_text,
        refs=refs_text,
    )
    # G2：开了 ReAct 就先跑一轮取证，把查到的东西追加到 S3 prompt 后面。
    # 只追加、不改 s3_syndrome.yaml——不开 ReAct 时 prompt 要跟改造前逐字节一致，
    # 否则 use_react 的 A/B 里混进了 prompt 变化这个额外变量。
    # 追问结果接在 S3 提示词后面。否认的那部分尤其重要——肯定的症状会并进症状表
    # 传下去，否认的不会，S3 看不到就照样可能按那条症状去开方。
    if followup is not None:
        s3_system = s3_system + format_followup_for_s3(followup)

    trace = None
    if use_react:
        trace = run_react(
            name=physician_name,
            symptoms=symptoms_text,
            elements_summary=_format_elements_summary(s2),
        )
        # ReAct 用 ask_user 收尾 = 它要追问患者。有提问渠道就真的问，回答先过
        # check_safety 再交给 S3；没有渠道时问题只记录，S3 拿不到答案。
        # 此前这个问题从来没被问出去，S3 却拿着 {"terminate": true} 那条观测继续开方。
        if trace.terminated_by == "ask_user" and trace.pending_question and ask_fn is not None:
            answer = ask_fn(trace.pending_question)
            if answer is not None:
                reject = check_safety([answer])
                if reject is not None:
                    raise SafetyVeto(reject, llm_calls=trace.llm_calls)
                trace.pending_answer = answer
        s3_system = s3_system + format_trace_for_s3(trace)

    s3 = get_llm().generate(system=s3_system, user="", schema=s3_schema)

    # X2 输出侧安全：十八反十九畏命中就把冲突写进 prompt 重开一次。
    # 只重开一次、不循环——循环会让 llm_calls 变成不可预测的数，
    # manifest 里那个调用数就没法用来算成本和比较配置了。
    incompatible = check_incompatible(s3.herbs)
    revised = False
    if incompatible:
        retry_system = s3_system + (
            f"\n\n【配伍禁忌】上一次拟的方中存在中药十八反十九畏配伍禁忌："
            f"{format_conflicts(incompatible)}。请重新拟方避开这些配伍，"
            "其余要求不变。"
        )
        s3 = get_llm().generate(system=retry_system, user="", schema=s3_schema)
        revised = True
        # 重开之后再查一次：还有冲突就保留结果并如实标出来，不再重开。
        incompatible = check_incompatible(s3.herbs)

    # 寒热一致性只警告不打回（寒热错杂本来就寒热并用，打回会改坏正确的方子）
    thermal_warning = check_thermal_consistency(s3.syndrome, s3.herbs)

    # 幻觉检查要放在可能的重开之后——查的是最终留下的那版方子。
    # 白名单 = 本函数检索到的 refs ∪ ReAct 里 search_cases 真实返回过的 id：
    # 后者也是真实医案，工具描述承诺了可以引用，只认 refs 会把模型照做的引用判成幻觉。
    ref_ids = {r["case_id"] for r in refs}
    if trace is not None:
        ref_ids |= set(trace.retrieved_case_ids)
    hallucinated = [cid for cid in s3.cited_case_ids if cid not in ref_ids]

    return {
        "physician": physician,
        "physician_name": physician_name,
        "s2": s2,
        "s3": s3,
        "refs": refs,
        # True = 检索为空，这位医家的结论没有任何医案支撑；前端要明示，不能当成
        # "引用了 0 条"静默过去
        "no_reference_cases": not hits,
        "hallucinated": hallucinated,
        "safety_output": {
            "incompatible": incompatible,
            "thermal_warning": thermal_warning,
            "revised": revised,
        },
        "react_trace": trace,
    }


def _build_manifest(elapsed_ms: int, llm_calls: int, use_react: bool = False) -> dict:
    """跑这一次用的是什么模型、什么 prompt 版本、几次调用。
    竞赛材料里写"我们的结果"时，这几行元数据就是全部的可信度来源。"""
    import hashlib
    from pathlib import Path as _P

    cases_sha = None
    cp = _P(__file__).resolve().parent.parent / "cases.json"
    if cp.exists():
        cases_sha = hashlib.sha256(cp.read_bytes()).hexdigest()[:12]

    # model 从后端问，不从 LLM_MODEL 环境变量读：claude_cli 后端下那个变量
    # 还是 deepseek-chat，照抄就等于把 Claude 跑的结果标成 DeepSeek 跑的。
    llm = get_llm()
    return {
        "model": llm.model_name(),
        "backend": llm.backend_id(),
        # 非默认后端时非 None。带着走，报告里就不会漏标"这个数不可比"。
        "comparability_warning": llm.comparability_warning(),
        "prompt_version": "v1",
        "use_react": use_react,
        "cases_sha256": cases_sha,
        "elapsed_ms": elapsed_ms,
        "llm_calls": llm_calls,
    }


def run_residual(s1: S1Normalize, s2: S2Elements) -> dict | None:
    """残差辨证：拿 S2 明确列出的未解释症状再跑一轮证素推断。

    这是"系统知道自己哪里没说清楚"的落点——不做的话 unexplained_symptoms
    只是个统计数字，界面上看不出系统承认了什么。
    """
    # 不能只信 unexplained_symptoms 字段——模型经常漏填它，
    # 实测有症状明明没被任何证素引用、该字段却是空的。
    # 取并集：字段声明的 + 实际没被任何 supporting_symptoms 提到的。
    # 两边都限定在 s1.symptoms 里：模型 declared 的名字也可能是改写过的，不在 s1 里的
    # 名字算进 unexplained 会让 coverage_before 变成负数。
    declared = {s for s in (s2.unexplained_symptoms or []) if s in s1.symptoms}
    actual = set(s1.symptoms) - explained_symptoms(s1, s2)
    unexplained = sorted(declared | actual, key=s1.symptoms.index)
    total = len(s1.symptoms) or 1
    if len(unexplained) < RESIDUAL_MIN_COUNT:
        return None
    if len(unexplained) / total < RESIDUAL_THRESHOLD:
        return None
    if set(unexplained) == set(s1.symptoms):
        # S2 对全部症状一个证素都没推出来。残差的输入跟 S2 刚才的输入一字不差
        # （同样的症状、舌、脉），temperature=0 下再跑一次必然得到同样的空结果，
        # 白花一次调用；直接交给 consult 的"信息不足"分支。
        return None

    residual_s1 = S1Normalize(
        symptoms=unexplained, tongue=s1.tongue, pulse=s1.pulse, unmapped=[]
    )
    s2r = infer_elements(residual_s1)
    newly = [
        sym
        for hit in s2r.elements
        for sym in hit.supporting_symptoms
        if sym in unexplained
    ]
    return {
        "triggered": True,
        "input_symptoms": unexplained,
        "s2": s2r,
        "newly_explained": sorted(set(newly)),
        "still_unexplained": [s for s in unexplained if s not in set(newly)],
        "coverage_before": round((total - len(unexplained)) / total, 3),
        "coverage_after": round((total - len(unexplained) + len(set(newly))) / total, 3),
    }


def consult(
    complaint: str,
    use_react: bool | None = None,
    ask_fn: AskFn | None = None,
) -> dict:
    """use_react=None 时读环境变量 USE_REACT（默认关）。显式传布尔值优先，
    测试和 A/B 脚本靠它固定条件，不受环境影响。

    ask_fn 是追问的提问渠道（真人命令行、患者模拟器、前端各传各的）。不传就
    不追问——没有提问渠道时静默跳过是对的，不是错误。
    """
    _t0 = time.time()
    if use_react is None:
        use_react = react_enabled()
    s1 = normalize(complaint)

    # 安全否决必须在这里、S2 开始之前——命中就直接返回，S2/S3 一次都不调用，
    # 不产出任何方药。不要把这道检查挪到 run_physician 内部或结果的 note 字段。
    # 三处都要查：S1 可能把"最近吐了两次血"这类病史陈述归进 unmapped
    # （s1_normalize.yaml 明确要求含糊的病史表述放 unmapped），只查 symptoms 会漏。
    reject_reason = check_safety([complaint] + s1.symptoms + s1.unmapped)
    if reject_reason is not None:
        # 键集跟正常路径保持一致：api/前端按同一份契约读，缺键就是 KeyError。
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": True,
            "reject_reason": reject_reason,
            "s2": None, "residual": None, "followup": None,
            "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), 1, use_react),
        }

    s2 = infer_elements(s1)

    # G3 追问：每轮 0 次 LLM 调用（规则解析 + 图上贝叶斯更新），只在问出了新症状
    # 之后重跑一次 S2 把新症状并进证素。
    followup = run_followup(
        s1.symptoms, [h.element for h in s2.elements], ask_fn
    )
    extra_calls = 0
    if followup.stopped_by == "safety":
        # 追问问出危重症状 = 跟初始主诉命中同一道否决，同样不产出任何方药。
        # CLAUDE.md：追问是安全否决层的后门，这里堵上。
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": True,
            "reject_reason": followup.reject_reason,
            "s2": s2,
            "followup": followup,
            "residual": None, "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000), 2, use_react
            ),
        }
    if followup.asserted:
        # 双保险：run_followup 已经把危重症状挡在 asserted 之外，这里再查一次是防
        # 将来有人改了 followup 的判据却没意识到这条症状会一路进 S2/S3。
        reject = check_safety(followup.asserted)
        if reject is not None:
            return {
                "s1": s1, "results": [], "divergence": None,
                "rejected": True, "reject_reason": reject,
                "s2": s2, "followup": followup, "residual": None,
                "insufficient": False, "insufficient_reason": None, "coverage": None,
                "manifest": _build_manifest(int((time.time() - _t0) * 1000), 2, use_react),
            }
        # 追问确认的是国标症状名（来自图谱节点），本身已经是标准表述，不需要再过
        # S1——这不违反"S1 全局只跑一次"，S1 一次也没有多跑。
        s1 = S1Normalize(
            symptoms=s1.symptoms + followup.asserted,
            tongue=s1.tongue, pulse=s1.pulse, unmapped=s1.unmapped,
        )
        s2 = infer_elements(s1)
        extra_calls += 1

    residual = run_residual(s1, s2)

    # 证素层为空 = 结构化推理没有落点。此时若继续跑 S3，模型会绕开证素
    # 直接"看主诉猜证型"（实测「胸闷气短」这类信息量过低的主诉，S2 返回空证素，
    # S3 仍给出完整证型和方药，推理过程里自己写着"证素分析未给出明确结论，
    # 然从症状推之"）。那样 S1->S2->S3 的分步设计就退化成了单步问答，
    # 而且输出的方药没有任何可追溯的依据。宁可如实说信息不足。
    coverage = len(explained_symptoms(s1, s2, residual)) / (len(s1.symptoms) or 1)

    if not s2.elements and not (residual and residual["s2"].elements):
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": False,
            "reject_reason": None,
            "s2": s2,
            "residual": residual,
            "followup": followup,
            "insufficient": True,
            "insufficient_reason": (
                "现有症状不足以推断证素，无法进行有依据的辨证。"
                "请补充更多信息：起病与加重缓解的诱因、疼痛或不适的性质与部位、"
                "饮食与二便情况、寒热喜恶、舌象与脉象。"
            ),
            "coverage": round(coverage, 3),
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000),
                2 + extra_calls + (1 if residual else 0), use_react
            ),
        }
    results = []
    try:
        for physician, info in PHYSICIANS.items():
            results.append(run_physician(
                s1, s2, physician, info["name"], use_react=use_react,
                followup=followup, ask_fn=ask_fn,
            ))
    except SafetyVeto as veto:
        # ReAct 追问问出了危重症状：跟初始主诉命中同一道否决，已经跑完的医家结果
        # 也不返回——被拦截的请求不产出任何方药。
        calls = (
            2 + extra_calls + (1 if residual else 0) + veto.llm_calls
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"])
            + len(results)
            + sum(1 for r in results if r["safety_output"]["revised"])
        )
        return {
            "s1": s1, "results": [], "divergence": None,
            "rejected": True, "reject_reason": veto.reason,
            "s2": s2, "followup": followup, "residual": residual,
            "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), calls, use_react),
        }

    syndromes = {r["physician"]: r["s3"].syndrome for r in results}
    values = list(syndromes.values())
    same = len(set(values)) <= 1

    # 字符串比对会把"脾胃气虚，运化失健"和"脾虚湿困，中焦不运"判为分歧，
    # 哪怕两者治法、方剂一字不差（实测 10 条主诉分歧率 9/9，指标无区分度）。
    # 改用药物集合的 Jaccard 距离作为主指标：用药是医家风格最实在的落点，
    # 而证型命名的差异很大程度上只是措辞。
    herb_sets = [
        {h for h in (normalize_herb(x) for x in (r["s3"].herbs or [])) if h}
        for r in results
    ]
    if len(herb_sets) >= 2 and any(herb_sets):
        inter = set.intersection(*herb_sets)
        union = set.union(*herb_sets)
        herb_jaccard = 1.0 - (len(inter) / len(union)) if union else 0.0
        shared_herbs = sorted(inter)
    else:
        herb_jaccard = None
        shared_herbs = []

    tp_same = len(set(r["s3"].treatment_principle for r in results)) <= 1

    divergence = {
        "same": same,
        "method": "exact_string_match",
        # 0=用药完全一致，1=毫无重叠
        "herb_jaccard": round(herb_jaccard, 3) if herb_jaccard is not None else None,
        "shared_herbs": shared_herbs,
        "treatment_principle_same": tp_same,
    }

    return {
        "s1": s1,
        "results": results,
        "divergence": divergence,
        "rejected": False,
        "reject_reason": None,
        "s2": s2,
        "residual": residual,
        "followup": followup,
        "insufficient": False,
        "insufficient_reason": None,
        "coverage": round(coverage, 3),
        # S1 一次 + S2 一次 + 每位医家 S3 一次 + 残差一次 + 配伍禁忌重开若干次。
        # 重开必须计进来：漏算的话 manifest 报的调用数会低于实际花费，
        # 拿它算成本或比配置就都是错的。
        "manifest": _build_manifest(
            int((time.time() - _t0) * 1000),
            2
            + extra_calls
            + len(results)
            + (1 if residual else 0)
            + sum(1 for r in results if r["safety_output"]["revised"])
            # ReAct 的每一步都是一次真实调用，必须计进来：漏算的话 manifest 报的
            # 调用数会低于实际花费，拿它算成本或比 use_react 开关的代价就都是错的。
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"]),
            use_react,
        ),
    }


if __name__ == "__main__":
    from pathlib import Path

    queries_path = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
    queries = [
        q.strip() for q in queries_path.read_text(encoding="utf-8").splitlines() if q.strip()
    ]

    n_divergent = 0
    n_hallucinated = 0
    n_rejected = 0
    durations = []

    for i, complaint in enumerate(queries, 1):
        t0 = time.time()
        outcome = consult(complaint)
        elapsed = time.time() - t0
        durations.append(elapsed)

        print(f"\n[{i}] 主诉：{complaint}")

        if outcome["rejected"]:
            n_rejected += 1
            print(f"  [安全拦截] {outcome['reject_reason']}")
            print(f"  耗时：{elapsed:.1f}s")
            continue

        for r in outcome["results"]:
            s3 = r["s3"]
            print(
                f"  {r['physician_name']}：证型={s3.syndrome}  "
                f"治法={s3.treatment_principle}  方={s3.formula}  药={'、'.join(s3.herbs)}"
            )
            if r["hallucinated"]:
                n_hallucinated += 1
                print(f"    [幻觉] 引用了检索结果之外的医案 id：{r['hallucinated']}")

        div = outcome["divergence"]
        hj = div.get("herb_jaccard")
        tp = "治法一致" if div.get("treatment_principle_same") else "治法不同"
        shared = div.get("shared_herbs") or []
        print(
            f"  分歧：证型{'不同' if div['same'] is False else '相同'}｜{tp}｜"
            f"药物Jaccard={hj if hj is not None else 'NA'}"
            f"｜共用药={('、'.join(shared[:6]) or '无')}"
        )
        if not div["same"]:
            n_divergent += 1
        print(f"  耗时：{elapsed:.1f}s")

    print("\n=== 统计 ===")
    print(f"安全拦截例数：{n_rejected}/{len(queries)}")
    print(f"分歧例数：{n_divergent}/{len(queries) - n_rejected}（分母排除被拦截的例数）")
    print(f"幻觉例数：{n_hallucinated}/{len(queries) - n_rejected}（分母排除被拦截的例数）")
    if durations:
        print(f"平均耗时：{sum(durations) / len(durations):.1f}s")
