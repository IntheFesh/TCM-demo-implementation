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

import hashlib
import sys

import json
import time
from pathlib import Path

from core import herbs as _herbs
from core.elements import LOCATIONS, NATURES
from core.llm import get_llm, load_prompt, render
from core.followup import (
    AskFn, fast_mode_enabled, format_followup_for_s3, parse_answer, run_followup,
)
from core.physicians import PHYSICIANS
from core.react import StepFn, format_trace_for_s3, react_enabled, run_react
from core.retrieval import MIN_RETRIEVAL_SCORE, get_retriever
from core.retrieval_hybrid import ALLOWED_MODES
from core.safety import check_safety, danger_confirmed_by_answer, safety_bypassed
from core.safety_output import assess_formula_safety, format_blocking_issues
from core.schemas import (
    CaseRecord, FollowupResult, S1Normalize, S2Elements, S3Syndrome, S3SyndromeUnreferenced,
)

# MIN_RETRIEVAL_SCORE 挪到 core/retrieval.py 了（ReAct 的 search_cases 工具也要用同一个
# 阈值，而 tools 不能反向 import chain）。这里 re-export，老调用方不受影响。

# offline/estimate_epsilon.py 的产物。模块级只放路径常量，不在 import 时读文件——
# 惰性初始化的一贯做法，且这里没有单例可惰性化，每次 consult() 直接读（文件几 KB，
# 开销可忽略）。文件不存在时 divergence["epsilon_online"] 就是 None，不抛异常：
# demo 在没跑过 ε 估算时也要能正常用。
EPSILON_PATH = Path(__file__).resolve().parent.parent / "eval" / "epsilon.json"


def load_epsilon_online() -> float | None:
    if not EPSILON_PATH.exists():
        return None
    try:
        data = json.loads(EPSILON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get("epsilon_online") or {}).get("mean")


class SafetyVeto(Exception):
    """推理过程中（不是初始主诉里）冒出危重信号时抛出：ReAct 的 ask_user 追问收到的
    回答命中 check_safety。抛异常而不是返回值，是因为发生点在 run_physician 深处，
    而处理点（不产出任何方药、返回拒绝）只能在 consult 这一层。"""

    def __init__(self, reason: str, llm_calls: int = 0):
        super().__init__(reason)
        self.reason = reason
        self.llm_calls = llm_calls


class RetrievalUnavailable(Exception):
    """请求的检索模式这台机器上跑不起来（graph 模式缺 data/element_index.json、
    或者缺 query_elements）。形状照抄 SafetyVeto：发生点在 run_physician 深处，
    处理点只能在 consult 这一层。

    **检索层照旧大声报错，这里只负责把它翻译成用户看得懂的话，不做静默降级。**
    K3b 的 graph 模式明确设计成"拿不到证素就报错"而不是悄悄退回 hybrid——
    退回去的话调用方以为自己拿到的是证素路的结果，E8 消融比的就不再是
    "graph vs 别的"，那组数字直接失去意义。所以这个异常存在的意义是"把 500
    变成一句人话"，不是"把错误吞掉继续跑"。
    """

    def __init__(self, mode: str, detail: str):
        super().__init__(detail)
        self.mode = mode
        self.detail = detail

# 残差辨证触发阈值：未解释症状 >=2 条 且 占比 >=30% 时，用这些症状再跑一轮，
# 看能不能构成兼夹证。S2 共享之后未解释症状是全局唯一一份，所以残差也只跑一次，
# 结果两位医家共用——这比原方案（每位医家各跑一轮）省一半调用，也更一致。
RESIDUAL_MIN_COUNT = 2
RESIDUAL_THRESHOLD = 0.30


# 药名归一挪到 core/herbs.py 了：core/safety_output.py 也要用它，留在这里会
# 造成 chain ↔ safety_output 循环导入。这里只留本模块真正用到的一个名字。
# split_western_drugs 曾经也在这里再导出（给 _split_western_into_s3 用），M1
# 把西药拆分挪进 core.schemas._S3Base 的 model_validator 之后不再需要——
# S3Syndrome/S3SyndromeUnreferenced 构造完成的那一刻，.herbs/.western_drugs
# 就已经是拆好的，这里不用也不该再拆一次。
normalize_herb = _herbs.normalize_herb


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
    """s1.symptoms 里已被解释的那些。**这是全项目唯一的判据**，coverage、残差触发、
    前端图上的症状 state 三处都调它——CLAUDE.md「同一概念的匹配逻辑只能有一处实现」。

    两条规则，缺一处就会出现三个互相矛盾的数（实测过：同一份 s1/s2 下
    consult.coverage=0.5、residual.coverage_before=0.25、图上 1/4）：

    1. **只返回 s1.symptoms 的子集。** S2 常把症状名改写（「胃脘胀痛」→「脘腹胀痛」），
       改写后的名字不在 s1 里，拿 supporting_symptoms 当分子会让 coverage 超过 1。
    2. **模型自己声明未解释的，即使被某个证素引用了也算未解释。** 实测模型经常两边
       都列（「乏力」同时出现在 supporting_symptoms 和 unexplained_symptoms），
       以它自己承认的为准更保守。
    """
    referenced = {sym for hit in s2.elements for sym in hit.supporting_symptoms}
    declared_unexplained = set(s2.unexplained_symptoms or [])
    explained = {s for s in s1.symptoms if s in referenced and s not in declared_unexplained}
    # 残差补上的那些无条件算已解释：残差本来就是针对未解释症状再跑的一轮
    explained |= {s for s in s1.symptoms if s in set((residual or {}).get("newly_explained") or [])}
    return explained


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


def _search_cases(
    query: str, physician: str, s2: S2Elements, retriever_mode: str | None
) -> list[tuple[CaseRecord, float]]:
    """检索该医家的 top-3 医案。全项目唯一一处把 retriever_mode 翻译成
    search() 关键字参数的地方。

    两条判断都在这里，不散到调用点：

    1. **不传 mode 时一个额外关键字都不加。** 保持默认路径跟改造前逐字节一致，
       也保证第三方 Retriever 实现（测试里的 FakeRetriever、将来别的后端）不必
       为了这个开关改签名——search() 的抽象基类签名里本来就没有 mode。
    2. **只有 mode="graph" 才传 query_elements。** graph 是唯一必须要证素的模式；
       给 hybrid 也传的话，默认的两路融合会变成三路，那是在没人要求的情况下
       改掉了默认检索行为，也就改掉了 E8 消融的对照基线。三路融合（K3b）目前
       从 consult 走不到，这一轮不顺手打开它，要开该是单独一轮、带对照数字地开。
    """
    kwargs: dict = {}
    if retriever_mode is not None:
        kwargs["mode"] = retriever_mode
    if retriever_mode == "graph":
        kwargs["query_elements"] = [h.element for h in s2.elements]

    try:
        return get_retriever().search(
            query, physician, k=3, min_score=MIN_RETRIEVAL_SCORE, **kwargs
        )
    except (ValueError, FileNotFoundError) as e:
        # 只翻译、不吞：检索层照旧大声报错（K3b 的 graph 模式故意不静默降级），
        # 这里把它裹成 RetrievalUnavailable 交给 consult 转成一句人话，避免
        # 500 裸奔到前端。范围收得很窄——只包住这一次 search() 调用，
        # 别处抛的 ValueError（比如 pydantic 校验）不会被误当成检索问题。
        raise RetrievalUnavailable(retriever_mode or "hybrid", str(e)) from e


def run_physician(
    s1: S1Normalize,
    s2: S2Elements,
    physician: str,
    physician_name: str,
    use_react: bool = False,
    followup: FollowupResult | None = None,
    ask_fn: AskFn | None = None,
    bypass_safety: bool = False,
    on_step: StepFn | None = None,
    retriever_mode: str | None = None,
) -> dict:
    """bypass_safety 由 consult() 一次算好后传进来，不在这里各自读一次环境变量
    ——同一个请求的几个中止点必须用同一个判断，不能一半拦一半不拦。

    retriever_mode 同理由 consult() 逐请求传进来，**不读也不写任何全局状态**：
    RETRIEVER_MODE 那个环境变量是进程级的，两个并发请求各选一种模式会互相
    污染（跟 safety_bypassed() 拒绝"接到环境变量"是同一条理由）。不传就完全
    走改造前的老路——连 mode 关键字都不传给 search()，行为逐字节一致。
    """
    symptoms_text = "；".join(s1.symptoms)
    # ReAct 追问命中危重症状时，demo 模式抛 SafetyVeto 中止；EVAL_MODE 下不中止，
    # 把本该拦截的原因经由返回值带回 consult()（异常没抛，只能走返回值这条路）。
    react_safety_flag: str | None = None

    # 检索该医家 top-3 医案
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"
    hits = _search_cases(query, physician, s2, retriever_mode)
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
            on_step=on_step,
        )
        # ReAct 用 ask_user 收尾 = 它要追问患者。有提问渠道就真的问，回答先过
        # check_safety 再交给 S3；没有渠道时问题只记录，S3 拿不到答案。
        # 此前这个问题从来没被问出去，S3 却拿着 {"terminate": true} 那条观测继续开方。
        if trace.terminated_by == "ask_user" and trace.pending_question and ask_fn is not None:
            answer = ask_fn(trace.pending_question)
            if answer is not None:
                # 回答先过 check_safety；问的本身是危重症状而患者没有明确否认时也拦
                # （「有没有便血？」→「有」）。后一条判据跟 G3 追问共用
                # core.safety.danger_confirmed_by_answer，不在这里另写一套。
                reject = check_safety([answer]) or danger_confirmed_by_answer(
                    trace.pending_question, parse_answer(answer)
                )
                if reject is not None:
                    if not bypass_safety:
                        raise SafetyVeto(reject, llm_calls=trace.llm_calls)
                    react_safety_flag = reject
                trace.pending_answer = answer
        s3_system = s3_system + format_trace_for_s3(trace)

    if on_step is not None:
        # 没开 ReAct 时这是这位医家唯一一次要等的 LLM 调用；开了 ReAct 也要报——
        # 取证结束不代表马上有结果，S3 本身也要等一次真实调用。
        on_step("s3_start", {"physician": physician, "physician_name": physician_name})
    # 混进 herbs 的西药（模型没照 prompt 的要求分开写）在 schema 构造时就已经被
    # core.schemas._S3Base 的 model_validator 挑到 western_drugs 了，这里不用
    # 再包一层 _split_western_into_s3——这一步以前是代码层面的兜底，现在兜底
    # 挪进了 schema 本身，构造完成的这一刻 s3.herbs/s3.western_drugs 就已经是
    # 拆好的（M1 之前这里对 herbs 混西药的拟合方式做了一次真实回归，见 SOURCES.md）。
    s3 = get_llm().generate(system=s3_system, user="", schema=s3_schema)

    # X2 输出侧安全（M2 起覆盖五条规则，见 core/safety_output.assess_formula_safety
    # 的文档字符串）：给每个候选方都算一份 FormulaSafety，不是只算 selected 那个——
    # M5 的图要在每个候选方节点上标安全状态，选中的和没选中的都要有数据可用。
    # 拦截判据只看 selected 那个：incompatible/dose_violations 命中就把问题写进
    # prompt 重开一次。只重开一次、不循环——循环会让 llm_calls 变成不可预测的数，
    # manifest 里那个调用数就没法用来算成本和比较配置了。
    for cand in s3.formula_candidates:
        cand.safety = assess_formula_safety(s3.syndrome, cand)
    selected_safety = s3.formula_candidates[s3.selected].safety
    revised = False
    if selected_safety.blocking:
        retry_system = s3_system + (
            f"\n\n【安全问题】上一次拟的方（当前选中的候选方）存在以下必须修正的"
            f"问题：{format_blocking_issues(selected_safety)}。请重新拟方解决这些"
            "问题，其余要求不变。"
        )
        s3 = get_llm().generate(system=retry_system, user="", schema=s3_schema)
        for cand in s3.formula_candidates:
            cand.safety = assess_formula_safety(s3.syndrome, cand)
        revised = True
        # 重开之后再查一次：还有问题就保留结果并如实标出来，不再重开。
        selected_safety = s3.formula_candidates[s3.selected].safety

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
        # 只在 EVAL_MODE 下可能非空：demo 模式命中这里就抛 SafetyVeto 了，走不到返回。
        "safety_flag": react_safety_flag,
        # 只保留 incompatible/thermal_warning/revised 三个键，跟 M2 之前的形状
        # 一字不差——api/main.py 和 web/index.html 已经在消费这三个键，M2 不碰
        # 那一层。dose_violations/decoction_missing/toxic_herbs 不在这里重复一份，
        # 它们已经在 s3.formula_candidates[i].safety 里，读那边就有，不用两处维护
        # 同一份数据（这正是 CLAUDE.md「同一概念只能有一处实现」要防的重复）。
        "safety_output": {
            "incompatible": selected_safety.incompatible,
            "thermal_warning": selected_safety.thermal_warning,
            "revised": revised,
        },
        "react_trace": trace,
    }


def _build_manifest(elapsed_ms: int, llm_calls: int, use_react: bool = False) -> dict:
    """跑这一次用的是什么模型、什么 prompt 版本、几次调用。
    竞赛材料里写"我们的结果"时，这几行元数据就是全部的可信度来源。"""
    cases_sha = None
    cp = Path(__file__).resolve().parent.parent / "cases.json"
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
    # FAST_MODE：残差辨证整体关闭。它是三处降级里最贵的一处——每触发一次就是
    # 一次完整的 S2 调用，而它本来就是"锦上添花"（把没解释的症状再推一轮），
    # 不是产出方药的必经步骤。判断放在这里而不是 consult 的调用点：只在调用方
    # 生效的开关是半吊子。
    if fast_mode_enabled():
        return None

    # 不能只信 unexplained_symptoms 字段——模型经常漏填它，
    # 实测有症状明明没被任何证素引用、该字段却是空的。
    # 取并集：字段声明的 + 实际没被任何 supporting_symptoms 提到的。
    # 未解释 = s1.symptoms 减去 explained_symptoms 的结果。declared 已经在
    # explained_symptoms 里处理过了（模型声明未解释的不算已解释），这里不再叠一层，
    # 否则同一份 s1/s2 会得出跟 coverage 不一致的数。
    unexplained = sorted(set(s1.symptoms) - explained_symptoms(s1, s2), key=s1.symptoms.index)
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
    eval_mode: bool | None = None,
    on_step: StepFn | None = None,
    retriever_mode: str | None = None,
) -> dict:
    """use_react=None 时读环境变量 USE_REACT（默认关）。显式传布尔值优先，
    测试和 A/B 脚本靠它固定条件，不受环境影响。

    ask_fn 是追问的提问渠道（真人命令行、患者模拟器、前端各传各的）。不传就
    不追问——没有提问渠道时静默跳过是对的，不是错误。

    eval_mode=None 时读环境变量 EVAL_MODE（默认关），形状跟 use_react 一致。
    打开之后，**安全检查照跑、命中原因照记进返回值的 safety_flag，但不再中止
    链路**——评测要量化"安全否决花了多少分"，就得让被拦的那些主诉也走完一遍
    拿到分数，否则那个代价算不出来。默认关，demo 的拦截红线不受影响。

    on_step 是 SSE 分步进度用的回调，(事件名, 数据字典) -> None。不传（CLI、
    eval/、批跑现状）就完全不影响这个函数原来的行为——每一处 emit 之前都判了
    `if on_step is not None`。传了之后在 S1/S2/追问/残差/每位医家开始与结束
    这几个自然边界各上报一次；医家内部更细的 ReAct 单步进度由 run_physician
    透传给 run_react（同一个回调对象，不是另起一套）。**不在这里处理"追问 /
    ReAct 追问需要用户回答"这件事**——那仍然是 ask_fn 的职责：SSE 端点想在
    追问时推 need_input 事件、暂停等回答，只需要传一个自己包了一层的 ask_fn，
    不需要 consult() 或 core/followup.py 知道"上面接的是不是 SSE"。

    retriever_mode 是**逐请求**的检索模式（dense/bm25/graph/hybrid），不传就走
    检索层自己的默认。**这里刻意不去设 RETRIEVER_MODE 环境变量**：那个变量是
    进程级的，一个请求设了它，同一进程里并发的另一个请求就跟着变了——跟
    safety_bypassed() 当初拒绝"把开关接到环境变量上"是同一条理由。这个参数
    从头到尾只在调用栈里传，任何时候都不写进程状态。

    模式不认识时**立刻抛 ValueError**，不往下跑：不然要等到第一位医家开始检索
    才失败，S1/S2 两次 LLM 调用已经白花了。跑得起来但这台机器上没有对应数据
    （graph 模式缺 element_index.json）是另一回事，那走 RetrievalUnavailable，
    返回值里带一句人话的 retrieval_error，不是异常也不是 500。
    """
    _t0 = time.time()

    def emit(name: str, **data) -> None:
        if on_step is not None:
            on_step(name, data)

    if retriever_mode is not None and retriever_mode not in ALLOWED_MODES:
        # 模式名的合法集合只有 core/retrieval_hybrid.py 那一份，这里 import 常量
        # 复用，不另抄一份字符串列表——抄一份的话加新模式时必然漏改一处。
        raise ValueError(
            f"未知的 retriever_mode={retriever_mode!r}，目前支持 {sorted(ALLOWED_MODES)}"
        )

    if use_react is None:
        use_react = react_enabled()
    # 一次 consult 里只判一次，之后一路用这个布尔值：中途有人改环境变量时，
    # 同一个请求的四个中止点也不会一半拦一半不拦。
    bypass = safety_bypassed(eval_mode)
    # demo 模式下这次请求会被拦截的原因（最早触发的那个）。EVAL_MODE 打开时
    # 链路继续往下走，但这个字段仍然如实记着"本来会被拦"，两种模式同一套语义。
    safety_flag: str | None = None
    s1 = normalize(complaint)
    emit("s1_done", symptoms=s1.symptoms, tongue=s1.tongue, pulse=s1.pulse, unmapped=s1.unmapped)

    # 安全否决必须在这里、S2 开始之前——命中就直接返回，S2/S3 一次都不调用，
    # 不产出任何方药。不要把这道检查挪到 run_physician 内部或结果的 note 字段。
    # 三处都要查：S1 可能把"最近吐了两次血"这类病史陈述归进 unmapped
    # （s1_normalize.yaml 明确要求含糊的病史表述放 unmapped），只查 symptoms 会漏。
    reject_reason = check_safety([complaint] + s1.symptoms + s1.unmapped)
    safety_flag = safety_flag or reject_reason
    if reject_reason is not None and not bypass:
        # 键集跟正常路径保持一致：api/前端按同一份契约读，缺键就是 KeyError。
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": True,
            "reject_reason": reject_reason,
            "safety_flag": safety_flag, "retrieval_error": None,
            "s2": None, "residual": None, "followup": None,
            "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), 1, use_react),
        }

    s2 = infer_elements(s1)
    emit("s2_done", elements=[
        {"element": h.element, "kind": h.kind, "confidence": h.confidence} for h in s2.elements
    ], unexplained_symptoms=s2.unexplained_symptoms)

    # G3 追问：每轮 0 次 LLM 调用（规则解析 + 图上贝叶斯更新），只在问出了新症状
    # 之后重跑一次 S2 把新症状并进证素。
    # 追问过程中每一问/每一答的进度不在这里上报——那是 ask_fn 的职责（SSE 端点
    # 想要 need_input 事件，自己包一层传进来的 ask_fn，不需要 run_followup 或
    # 这里知道调用方是不是 SSE）。这里只上报"追问这一整段结束了"。
    followup = run_followup(
        s1.symptoms, [h.element for h in s2.elements], ask_fn
    )
    emit("followup_done", stopped_by=followup.stopped_by, rounds=followup.rounds,
         asserted=followup.asserted, denied=followup.denied)
    extra_calls = 0
    if followup.stopped_by == "safety":
        # 追问问出危重症状 = 跟初始主诉命中同一道否决，同样不产出任何方药。
        # CLAUDE.md：追问是安全否决层的后门，这里堵上。
        safety_flag = safety_flag or followup.reject_reason
    if followup.stopped_by == "safety" and not bypass:
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": True,
            "reject_reason": followup.reject_reason,
            "safety_flag": safety_flag, "retrieval_error": None,
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
        safety_flag = safety_flag or reject
        if reject is not None and not bypass:
            return {
                "s1": s1, "results": [], "divergence": None,
                "rejected": True, "reject_reason": reject,
                "safety_flag": safety_flag, "retrieval_error": None,
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
        emit("s2_done", elements=[
            {"element": h.element, "kind": h.kind, "confidence": h.confidence} for h in s2.elements
        ], unexplained_symptoms=s2.unexplained_symptoms, after_followup=True)

    residual = run_residual(s1, s2)
    if residual:
        emit("residual_done", newly_explained=residual["newly_explained"],
             still_unexplained=residual["still_unexplained"],
             coverage_before=residual["coverage_before"], coverage_after=residual["coverage_after"])

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
            "safety_flag": safety_flag, "retrieval_error": None,
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000),
                2 + extra_calls + (1 if residual else 0), use_react
            ),
        }
    results = []
    try:
        for physician, info in PHYSICIANS.items():
            emit("physician_start", physician=physician, physician_name=info["name"])
            r = run_physician(
                s1, s2, physician, info["name"], use_react=use_react,
                followup=followup, ask_fn=ask_fn, bypass_safety=bypass, on_step=on_step,
                retriever_mode=retriever_mode,
            )
            results.append(r)
            emit("physician_done", physician=physician, physician_name=info["name"],
                 syndrome=r["s3"].syndrome, herbs=r["s3"].herbs)
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
            "safety_flag": safety_flag or veto.reason, "retrieval_error": None,
            "s2": s2, "followup": followup, "residual": residual,
            "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), calls, use_react),
        }
    except RetrievalUnavailable as e:
        # 选的检索模式这台机器上没有对应数据（graph 缺 element_index.json 之类）。
        # 已经跑完的医家结果也不返回：一半医家用了这个模式、另一半没有的话，
        # 那份对照本身就是错的，不如干净地什么都不给、把原因说清楚。
        # **不在这里静默降级回 hybrid**——那样用户以为自己看到的是 graph 模式的
        # 结果，E8 消融的数字也就没有意义了（这一条是这个模块的硬约束）。
        return {
            "s1": s1, "results": [], "divergence": None,
            "rejected": False, "reject_reason": None,
            "safety_flag": safety_flag,
            "retrieval_error": (
                f"检索模式「{e.mode}」在这台机器上不可用：{e.detail} "
                # 真实冒烟里踩到的：默认模式本身跑不了（没有 cases.json）时还建议
                # "换用默认模式"，等于指一条不存在的路。显式选了别的模式才这么说。
                + ("换用默认模式可以正常辨证；" if retriever_mode is not None
                   else "这台机器还没有生成检索数据，哪个模式都跑不了；")
                + "本次没有降级到别的模式跑，是为了不让你以为看到的是这个模式的结果。"
            ),
            "s2": s2, "followup": followup, "residual": residual,
            "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000),
                2 + extra_calls + (1 if residual else 0), use_react,
            ),
        }

    syndromes = {r["physician"]: r["s3"].syndrome for r in results}
    values = list(syndromes.values())
    same = len(set(values)) <= 1

    # EVAL_MODE 下 ReAct 的追问可能问出危重症状而没有中止（见 run_physician），
    # 把那个本该拦截的原因收上来。demo 模式走不到这里——那条路会抛 SafetyVeto。
    safety_flag = safety_flag or next(
        (r["safety_flag"] for r in results if r["safety_flag"]), None
    )

    # 字符串比对会把"脾胃气虚，运化失健"和"脾虚湿困，中焦不运"判为分歧，
    # 哪怕两者治法、方剂一字不差（实测 10 条主诉分歧率 9/9，指标无区分度）。
    # 改用药物集合的 Jaccard 距离作为主指标：用药是医家风格最实在的落点，
    # 而证型命名的差异很大程度上只是措辞。
    # s3.herbs 在 S3Syndrome/S3SyndromeUnreferenced 构造完成的那一刻已经被
    # core.schemas._S3Base 的 model_validator 清过西药，这里不用再滤一次
    # ——清洗只在那一处做，下游全都看到干净数据。
    herb_sets = [
        _herbs.normalized_herb_set(r["s3"].herbs)
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

    # 西药单独报，不混进 herb_jaccard 这个主指标：只有张锡纯会用西药，把它算进
    # 药物集合的话，"叶天士没开阿斯匹林"会被当成一条真实的用药分歧计入，
    # 跨学派分歧被系统性推高——而那个推高只是学派不同带来的记录体例差异，
    # 不是辨证思路的差异。双方都没有西药时为 None（不是 0）：0 会被读成
    # "两边西药完全一致"，而实际是"这个维度不适用"。
    western_sets = [
        {w for w in (x.strip() for x in (r["s3"].western_drugs or [])) if w}
        for r in results
    ]
    if len(western_sets) >= 2 and any(western_sets):
        w_inter = set.intersection(*western_sets)
        w_union = set.union(*western_sets)
        western_overlap = {
            "jaccard": round(1.0 - len(w_inter) / len(w_union), 3) if w_union else 0.0,
            "shared": sorted(w_inter),
            "by_physician": {
                r["physician"]: sorted(ws) for r, ws in zip(results, western_sets)
            },
        }
    else:
        western_overlap = None

    divergence = {
        "same": same,
        "method": "exact_string_match",
        # 0=用药完全一致，1=毫无重叠
        "herb_jaccard": round(herb_jaccard, 3) if herb_jaccard is not None else None,
        "shared_herbs": shared_herbs,
        "treatment_principle_same": tp_same,
        # None = 两位医家都没开西药，这个维度不适用（不是"完全一致"）
        "western_drug_overlap": western_overlap,
        # 噪声地板：herb_jaccard 本身没有意义，除非知道"同一设定重复跑，本来就会
        # 抖多少"。None = 还没跑过 offline/estimate_epsilon.py，前端要如实展示
        # "未测"，不能假装这个数已经有对照（CLAUDE.md「任何数字都必须带对照」）。
        "epsilon_online": load_epsilon_online(),
    }

    return {
        "s1": s1,
        "results": results,
        "divergence": divergence,
        "rejected": False,
        "reject_reason": None,
        "safety_flag": safety_flag, "retrieval_error": None,
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


def consult_many(queries: list[str], consult_fn=None) -> tuple[list[dict | None], list[dict]]:
    """逐条跑 consult，**一条挂了不拖累其余**。返回 (与 queries 对齐的结果列表，
    失败记录)；失败的位置是 None。

    eval/run_eval.py 和 eval/mes/export.py 之前各自写的是 `[consult(q) for q in
    queries]`：第 9 条主诉的 LLMError 会把前 8 条已经花钱跑完的结果一起丢掉。
    run_batch 的文档记过同一个坑（insufficient 分支 AttributeError 整批挂掉），
    教训没有传到后来的两个批处理入口——所以抽成一处，两边都调它。
    """
    fn = consult_fn or consult
    results: list[dict | None] = []
    failures: list[dict] = []
    for i, complaint in enumerate(queries, 1):
        try:
            results.append(fn(complaint))
        except Exception as e:  # noqa: BLE001 - 一条主诉的失败不能把整批已完成的结果一起丢掉
            print(f"[consult_many] 第 {i} 条失败：{type(e).__name__}: {e}", file=sys.stderr)
            results.append(None)
            failures.append({"index": i, "query": complaint, "error": f"{type(e).__name__}: {e}"})
    return results, failures


def run_batch(queries: list[str], consult_fn=None) -> dict:
    """把 tests/queries.txt 逐条跑一遍并打印结果，返回统计。

    抽成函数而不是留在 `__main__` 里：`__main__` 块没法被测试覆盖，而这里的
    分支处理正好出过 bug——原来只处理 rejected，遇到 insufficient（divergence 是
    None）直接 AttributeError，整批跑挂掉、前面几条的结果一起丢。
    """
    fn = consult_fn or consult
    stats = {"total": len(queries), "rejected": 0, "insufficient": 0,
             "divergent": 0, "hallucinated": 0, "durations": []}

    for i, complaint in enumerate(queries, 1):
        t0 = time.time()
        outcome = fn(complaint)
        elapsed = time.time() - t0
        stats["durations"].append(elapsed)

        print(f"\n[{i}] 主诉：{complaint}")

        if outcome["rejected"]:
            stats["rejected"] += 1
            print(f"  [安全拦截] {outcome['reject_reason']}")
            print(f"  耗时：{elapsed:.1f}s")
            continue

        if outcome.get("insufficient"):
            stats["insufficient"] += 1
            print(f"  [信息不足] {outcome['insufficient_reason']}")
            print(f"  耗时：{elapsed:.1f}s")
            continue

        for r in outcome["results"]:
            s3 = r["s3"]
            print(
                f"  {r['physician_name']}：证型={s3.syndrome}  "
                f"治法={s3.treatment_principle}  方={s3.formula}  药={'、'.join(s3.herbs)}"
            )
            if r["no_reference_cases"]:
                print("    [无参考医案] 检索不到相关医案，本结论没有医案支撑")
            if r["hallucinated"]:
                stats["hallucinated"] += 1
                print(f"    [幻觉] 引用了检索结果之外的医案 id：{r['hallucinated']}")

        div = outcome["divergence"] or {}
        hj = div.get("herb_jaccard")
        tp = "治法一致" if div.get("treatment_principle_same") else "治法不同"
        shared = div.get("shared_herbs") or []
        print(
            f"  分歧：证型{'不同' if div.get('same') is False else '相同'}｜{tp}｜"
            f"药物Jaccard={hj if hj is not None else 'NA'}"
            f"｜共用药={('、'.join(shared[:6]) or '无')}"
        )
        if div.get("same") is False:
            stats["divergent"] += 1
        print(f"  耗时：{elapsed:.1f}s")

    denom = stats["total"] - stats["rejected"] - stats["insufficient"]
    print("\n=== 统计 ===")
    print(f"安全拦截例数：{stats['rejected']}/{stats['total']}")
    print(f"信息不足例数：{stats['insufficient']}/{stats['total']}")
    print(f"分歧例数：{stats['divergent']}/{denom}（分母排除被拦截和信息不足的例数）")
    print(f"幻觉例数：{stats['hallucinated']}/{denom}（分母排除被拦截和信息不足的例数）")
    if stats["durations"]:
        print(f"平均耗时：{sum(stats['durations']) / len(stats['durations']):.1f}s")
    return stats


if __name__ == "__main__":
    from pathlib import Path

    queries_path = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
    run_batch([
        q.strip() for q in queries_path.read_text(encoding="utf-8").splitlines() if q.strip()
    ])
