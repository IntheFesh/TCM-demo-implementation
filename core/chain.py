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

import contextvars
import hashlib
from functools import lru_cache
import sys
import threading

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
from pathlib import Path

from core import herbs as _herbs
from core.context_prefix import (
    assemble,
    build_focused_knowledge,
    knowledge_in_prompt,
    prefix_tokens_by_section,
)
from core.diseases import get_disease, match_disease
from core.elements import LOCATIONS, NATURES
from core.llm import (
    LLMError,
    current_retry_stats,
    get_llm,
    load_prompt,
    current_usage_stats,
    new_retry_stats,
    new_usage_stats,
    render,
    S3_MODES,
    s3_best_of_n,
    s3_mode,
    s1s2_merged,
    s3_reasoning_effort,
    s3_thinking,
    thinking_by_step,
    thinking_for,
)
from core.followup import (
    AskFn, fast_mode_enabled, format_followup_for_s3, parse_answer, run_followup,
    stop_label,
)
from core.physicians import (
    PHYSICIANS,
    physician_choices_text,
    physicians_enabled,
    physicians_for_synthesis,
)
from core.react import StepFn, format_trace_for_s3, react_enabled, run_react
from core.retrieval import adaptive_min_score, apply_low_discrimination_cutoff, get_retriever
from core.retrieval_hybrid import (
    ALLOWED_MODES,
    DEFAULT_MODE,
    RETRIEVER_MODE_ENV,
    effective_mode,
)
from core.agent import AgentTrace, decide
from core.safety import check_safety, danger_confirmed_by_answer, safety_bypassed
from core.formula_check import advice_dicts, check_formula
from core.formula_verifier import (
    format_violations_for_revise,
    max_revise_rounds,
    verifier_metrics,
    verify_formula,
)
from core.safety_output import assess_formula_safety, format_blocking_issues
from core.schemas import (
    S3_CHAIN_STEPS,
    CaseRecord,
    FollowupResult,
    HerbItem,
    ReActTrace,
    S1Normalize,
    S1S2Merged,
    S2Elements,
    S3Derived,
    S3Structured,
    S3StructuredUnreferenced,
    S3Syndrome,
    S3SyndromeUnreferenced,
)
from core.theory import (
    load_theory,
    organ_relations as theory_organ_relations,
    principles_for as theory_principles_for,
    role_construction_rules as theory_role_construction_rules,
    transitions as theory_transitions,
)

# min_score 不再是 core/retrieval.py 写死的 MIN_RETRIEVAL_SCORE=0.70，改成
# adaptive_min_score() 逐请求算（P0-7）。这个函数也挪在 core/retrieval.py：
# ReAct 的 search_cases 工具也要用同一个函数，而 tools 不能反向 import chain。

# offline/estimate_epsilon.py 的产物。模块级只放路径常量，不在 import 时读文件——
# 惰性初始化的一贯做法，且这里没有单例可惰性化，每次 consult() 直接读（文件几 KB，
# 开销可忽略）。文件不存在时 divergence["epsilon_online"] 就是 None，不抛异常：
# demo 在没跑过 ε 估算时也要能正常用。
EPSILON_PATH = Path(__file__).resolve().parent.parent / "eval" / "epsilon.json"


def _read_epsilon_file() -> dict | None:
    """eval/epsilon.json 的**唯一**读取点。下面三个 load_* 都从这里取，不各自
    open 一次——三层 ε（online / core / adjunct）是同一份文件里的并列字段，
    读文件这件事重复三遍，以后加第四层就会有一处忘了改。

    文件不存在/坏了都返回 None，不抛异常：demo 在没跑过 ε 估算时也要能用。
    """
    if not EPSILON_PATH.exists():
        return None
    try:
        return json.loads(EPSILON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_epsilon_online_detail() -> dict | None:
    """跟 load_epsilon_online 读的是同一份文件——文件读取/解析只在
    _read_epsilon_file 做一次，load_epsilon_online 是对它取 .mean 的薄封装，
    不是另一份独立实现
    （tests/test_dedup_contracts.py::test_epsilon_loader_is_defined_only_in_chain
    钉住"ε 的加载器只能在 core/chain.py 里"这条约束）。

    E3/E4 消融（eval/run_eval.py）要按 (主诉, 医家) 配对去比噪声地板，
    单独一个 mean 标量不够用——这里把完整的 epsilon_online 子对象
    （含 per_query/by_physician）交出去，调用方自己从里面挑要用的字段。
    """
    data = _read_epsilon_file()
    return data.get("epsilon_online") if data else None


def load_epsilon_online() -> float | None:
    detail = load_epsilon_online_detail()
    return detail.get("mean") if detail else None


def load_epsilon_layer_means() -> dict:
    """君臣层 / 佐使层各自的噪声地板均值：{"core": float|None, "adjunct": float|None}。

    分层分歧度（core_jaccard / adjunct_jaccard）跟 herb_jaccard 一样是"没有对照
    就没有意义"的数——CLAUDE.md「任何数字都必须带对照」。前端要把
    core_jaccard 跟 ε_core 并排显示，不能拿 ε_online（整方的地板）去判断君臣层
    那个数超没超出抖动范围：实测佐使层的抖动明显大于整方、君臣层明显小于整方，
    用同一个地板卡三层会把君臣层的一致性低估、佐使层的发散高估。

    还没跑过 offline/estimate_epsilon.py（或跑的是 R1 之前的旧版本、文件里没有
    这两个字段）时两个值都是 None，前端如实展示"未测"。
    """
    data = _read_epsilon_file() or {}
    return {
        layer: ((data.get(f"epsilon_{layer}") or {}).get("mean"))
        for layer in ("core", "adjunct")
    }


def epsilon_values_in_query_record(record: dict) -> list[float]:
    """一条 `per_query` 记录里所有 (医家, 重复对) 的 Jaccard 距离，摊平成一个列表。

    **摊平这件事只写这一处。** 它知道 `per_query[i].by_physician[p].values` 这个
    三层结构；知道这个结构的地方越多，`estimate_epsilon.py` 哪天改字段名就越难
    改干净。`scripts/collect_results.py::epsilon_by_query`（ε 分层那一节的数）和
    下面的 `load_epsilon_for_query`（前端对照带上那个 ε）都从这里取。
    """
    return [
        v
        for bp in (record.get("by_physician") or {}).values()
        for v in (bp.get("values") or [])
    ]


def epsilon_floor_of_query_record(record: dict) -> float | None:
    """一条主诉的噪声地板 = 该条下所有距离的均值，跟 `epsilon_online.mean`
    同一个口径（那个数是全部值的均值），所以逐条的数能直接跟全局均值比大小。
    没有可用值时是 None，不是 0——0 会被读成"这条主诉重复跑毫无抖动"。"""
    values = epsilon_values_in_query_record(record)
    return round(sum(values) / len(values), 4) if values else None


def load_epsilon_for_query(query: str) -> dict:
    """这条主诉自己的噪声地板，取不到就退回全局均值**并说明退了**。

    为什么要逐条而不是一律用全局均值：实测 9 条可用主诉里有 4 条的地板高于全局
    均值，最高的一条是 0.3954 对 0.2611（DEMO.md 第 1 点讲的就是这件事）。拿全局
    均值一刀切，这 4 条主诉上的噪声会被当成真实分歧、另外 5 条上的真实分歧会被
    当成噪声——而前端对照带上那条参考线画的就是这个阈值，画错了整条带子在骗人。

    `scope` 三种取值必须原样传到界面上：
      query  —— 这条主诉测过，用的是它自己的地板
      global —— 没测过这条，用的是全局均值，界面上要标「（全局）」
      none   —— 连 eval/epsilon.json 都没有，界面上写「未测」而不是画一条线

    匹配按主诉原文**逐字相等**：ε 是对某一条特定文本重复跑测出来的，换了标点
    就是另一条主诉、另一个地板。这跟回放按 system 文本哈希索引是同一个道理，
    不做模糊匹配——模糊匹配会把 B 条的地板安到 A 条头上，而这种错不会报任何错。
    """
    detail = load_epsilon_online_detail() or {}
    for record in detail.get("per_query") or []:
        if record.get("query") != query or record.get("skipped"):
            continue
        floor = epsilon_floor_of_query_record(record)
        if floor is not None:
            return {"value": floor, "scope": "query"}
        break
    global_mean = detail.get("mean")
    if global_mean is None:
        return {"value": None, "scope": "none"}
    return {"value": global_mean, "scope": "global"}


# 当前正在提问的医家。**ContextVar 而不是参数**：`AskFn` 的契约是
# `(question) -> str | None`，命令行、患者模拟器、SSE 端点各有一份实现，加一个
# 参数要同时改三处调用方和它们的测试；而这里要传的信息（谁在问）对 ask_fn 的
# 语义没有影响，只是给 SSE 端点用来把 need_input 事件路由到对应那一列。
#
# 并发下仍然正确：每位医家的 worker 跑在 `contextvars.copy_context().run` 里
# （见 `_run_physicians_into` 第四条），各自一份独立的 Context，互不串。
# 全局追问（`run_followup`，在三位医家之前跑）不属于任何一列，取值是 None，
# 前端据此回落到输入区那个问答框。
_ASKING_PHYSICIAN: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar(
    "asking_physician", default=None,
)


def current_asking_physician() -> tuple[str, str] | None:
    """(physician_id, 中文名)，或 None 表示不在某位医家的提问上下文里。"""
    return _ASKING_PHYSICIAN.get()


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


# V5 P0-1：原文摘录的截断长度。医案原文中位数约 1500 字，三条全塞进去会
# 挤占模型注意力；300 字通常够到"证型/治法/方药"那一段——但这个数字没有在
# 真实语料上逐条核验过（这个环境没有 cases.json，抽样核验需要真实医案，
# 见 offline/extract_cases.py 的产出），如实标注为待验证的估计值，不是
# 已经验证过的常量。真实语料上核验时，如果发现大量截断点落在方药描述
# 之前，按用户原话的建议调到 400。
CASE_EXCERPT_TRUNCATE_CHARS = 300


def _format_case_block(case: CaseRecord) -> str:
    """把一个参考医案格式化成两段喂给 S3 prompt：原文摘录在前、结构化字段在后。

    **V5 P0 根因修复**：原来是十个结构化字段压成一行（_format_case_line，
    已删除），大部分医案只有 10.8% 有证型标注，绝大多数字段是"未记"——
    三行几乎全是"未记"的参考，跟 prompt 里紧接着的完整示例相比信息量
    近似于零，模型会转而依赖示例里的具体方药。E3/E4 消融（own vs
    swapped/none 改变率都远低于 0.4 的闸门）证实了这一点：换掉参考医案
    和完全不给参考医案，对输出的影响一样小，说明参考医案的内容根本没被
    利用。真正信息量最丰富的信号——raw_excerpt（97% 的医案有）——反而
    从来没有进过 prompt。

    这一版原文摘录放最前面，结构化字段放后面、且缺失的字段直接省略
    （不写"未记"）——三个"未记"比什么都不写更削弱这条医案的可信度，
    等于在告诉模型"这条参考没什么信息"。
    """
    vi = case.visit_index or 0
    visit_desc = "初诊" if vi == 0 else f"第{vi + 1}诊"
    header = f"【参考医案】{case.case_id}（{visit_desc}）"

    if case.raw_excerpt:
        excerpt = case.raw_excerpt[:CASE_EXCERPT_TRUNCATE_CHARS]
        raw_line = f"原文：{excerpt}"
    else:
        raw_line = "原文：（原文缺失）"

    structured_parts = []
    if case.symptoms:
        structured_parts.append(f"症状={'；'.join(case.symptoms)}")
    if case.tongue:
        structured_parts.append(f"舌={case.tongue}")
    if case.pulse:
        structured_parts.append(f"脉={case.pulse}")
    if case.syndrome:
        structured_parts.append(f"证={case.syndrome}")
    if case.pathogenesis:
        structured_parts.append(f"病机={case.pathogenesis}")
    if case.treatment_principle:
        structured_parts.append(f"治法={case.treatment_principle}")
    if case.formula:
        structured_parts.append(f"方={case.formula}")
    if case.herbs:
        structured_parts.append(f"药={'、'.join(case.herbs)}")
    structured_line = "结构化：" + ("；".join(structured_parts) if structured_parts else "（无结构化字段）")

    return "\n".join([header, raw_line, structured_line])


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
    # 关思考：S1 是结构化抽取，思考对它没有增益却让每次调用从两三秒变成几十秒；
    # 而且思考模式下 temperature 不生效，正是 fixture 与 ε 不可复现的根因。
    return get_llm().generate(system=system, user="", schema=S1Normalize,
                              **thinking_for("s1"))


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
    return get_llm().generate(system=s2_system, user="", schema=S2Elements,
                              **thinking_for("s2"))


def normalize_and_infer_merged(complaint: str) -> tuple[S1Normalize, S2Elements]:
    """R36：S1 + S2 **一次调用**。返回拆好的 `(s1, s2)`，跟分两次拿到的同型。

    ## 这条路默认关着，而且关着是有理由的

    `S1S2_MERGED` 默认 **0**。省下的是一次 2~4 秒的调用，代价是次序：
    CLAUDE.md 那条铁律要求**危重症状的拦截发生在证素推断之前**，而合一之后
    证素推断与症状标准化在同一次调用里完成——拦截最早也只能早到"那一次调用
    之前"，也就是只能拿**原始主诉的字面**去查。S1 归一之后才露出来的危重词
    （原文「呕吐咖啡色物」经 S1 归一成「呕血」）就挡不住证素推断了。

    调用方（`consult`）在合一模式下命中安全否决时**把已经推出来的证素丢掉、
    返回 `s2: None`**，所以对外可见的行为跟分两次那条路逐字段一致（不产出证素、
    不产出方药）。但"丢掉"是流程约定，"没算过"才是结构保证——把一条结构保证换成
    一条流程约定，不该由一个性能开关顺手做掉。

    R36 的调用数验收（一次问诊 ≤4 次）不靠它也达到：S1 + S2 + S3 = 3 次。
    所以这条路完整实现、有测试、`S1S2_MERGED=1` 随时可开（R38 的消融要拿它
    量"合一之后证素质量变没变"），但默认不开。
    """
    prompt = load_prompt("s1s2_merged")
    system = render(
        prompt["system"],
        complaint=complaint,
        elements=(
            f"病位证素（kind 填 location）：{'、'.join(LOCATIONS)}\n"
            f"  病性证素（kind 填 nature）：{'、'.join(NATURES)}"
        ),
    )
    merged = get_llm().generate(system=system, user="", schema=S1S2Merged,
                                **thinking_for("s1s2"))
    return merged.to_s1(), merged.to_s2()


def _search_cases(
    query: str, physician: str, s2: S2Elements, retriever_mode: str | None
) -> tuple[list[tuple[CaseRecord, float]], bool]:
    """检索该医家的 top-3 医案。全项目唯一一处把 retriever_mode 翻译成
    search() 关键字参数的地方。返回 (hits, low_discrimination)。

    两条判断都在这里，不散到调用点：

    1. **不传 mode 时一个额外关键字都不加。** 保持默认路径跟改造前逐字节一致，
       也保证第三方 Retriever 实现（测试里的 FakeRetriever、将来别的后端）不必
       为了这个开关改签名——search() 的抽象基类签名里本来就没有 mode。
    2. **只有 mode="graph" 才传 query_elements。** graph 是唯一必须要证素的模式；
       给 hybrid 也传的话，默认的两路融合会变成三路，那是在没人要求的情况下
       改掉了默认检索行为，也就改掉了 E8 消融的对照基线。三路融合（K3b）目前
       从 consult 走不到，这一轮不顺手打开它，要开该是单独一轮、带对照数字地开。

    min_score 不写死了（P0-7）：先用 adaptive_min_score() 探测这次 (query,
    physician) 该用多严的阈值，再拿这个阈值做真正的检索——探测调用复用同一份
    kwargs（跟真正调用完全一致的 mode/query_elements），不是额外传一个只有
    探测才用的参数。

    P0-12：拿到 top-3 之后再过一次 apply_low_discrimination_cutoff——candidates
    之间没有真实区分度时只留 top-1，避免拿三条弱相关的塞满 prompt 稀释信号。
    只对 dense/graph 模式成立（P0-13 改动 3，见该函数文档字符串）——传
    kwargs.get("mode") 让它自己判断，这里不重复"是不是 dense/graph"这条
    判断（跟 adaptive_min_score 只在 mode="dense" 时探测是同一条规则，
    两处判断都不在 chain.py 里做，chain.py 只转发它已经知道的 mode）。
    """
    kwargs: dict = {}
    if retriever_mode is not None:
        kwargs["mode"] = retriever_mode
    if retriever_mode == "graph":
        kwargs["query_elements"] = [h.element for h in s2.elements]

    try:
        retriever = get_retriever()
        min_score = adaptive_min_score(retriever, query, physician, **kwargs)
        hits = retriever.search(query, physician, k=3, min_score=min_score, **kwargs)
        return apply_low_discrimination_cutoff(hits, mode=kwargs.get("mode"))
    except (ValueError, FileNotFoundError) as e:
        # 只翻译、不吞：检索层照旧大声报错（K3b 的 graph 模式故意不静默降级），
        # 这里把它裹成 RetrievalUnavailable 交给 consult 转成一句人话，避免
        # 500 裸奔到前端。范围收得很窄——只包住这一次 search() 调用，
        # 别处抛的 ValueError（比如 pydantic 校验）不会被误当成检索问题。
        raise RetrievalUnavailable(retriever_mode or "hybrid", str(e)) from e


def _ref_row(case: CaseRecord, score: float) -> dict:
    """一条参考医案在响应 `refs` 里的样子。

    抽成函数是因为 R33 起有两条路径产出 refs（`run_physician` 一家、
    `run_synthesis` 五家）。**前端证据链侧栏读的就是这些键**——两处各拼一份的话，
    加一个键时只改一处，另一条路径上那个键就是 undefined，而界面只会少显示一行，
    不报错（第 31 条：这次的"同一概念"是"一条参考医案对外长什么样"）。

    只给 (id, score) 是不够的：用户看到 `ye_tianshi-0031-p6-0` 完全不知道那是
    什么医案，"可追溯"这个卖点就断在这里。
    """
    return {
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


#: R52：每一类医理规则最多摆几条进演绎推导 prompt。避免 prompt 随症状数线性
#: 膨胀——跟 `FOCUSED_MAX_PATTERNS_PER_PHYSICIAN` 同一条纪律（预算摆在明处，
#: 超出的部分要报"砍了多少"，不是悄悄截断）。
THEORY_RULES_MAX_PER_KIND = 12

_THEORY_KIND_LABELS = {
    "organ_relation": "藏象关系",
    "pathomechanism": "病机传变",
    "treatment_principle": "治则推导",
    "compatibility": "配伍理论（君臣佐使）",
}


def _format_theory_rules(s2: S2Elements) -> tuple[str, dict]:
    """R52：把 S2 证素对应的医理规则渲染成 `s3_derived.yaml` 的 `$theory_rules` 块。

    **这里没有医案，只有规则**——这个函数在演绎推导 prompt 里的位置，就是
    `run_synthesis`/`run_physician` 里 `$refs`（参考医案块）的位置，但内容来源
    完全不同（`core/theory.py`，R51 的四类规则），这正是"检索几位医家再模仿"
    改成"按医理药理演绎推导"的落地点。

    按 S2 给出的脏腑（location）与病性（nature）查，不是把全量规则表塞进去：
    `data/standard/tcm_theory.jsonl` 会随后续轮次继续增补，prompt 不能随着
    规则库增长而线性膨胀——`THEORY_RULES_MAX_PER_KIND` 兜底，超出的部分记进
    `trimmed_sections`（跟 `build_focused_knowledge` 同一条纪律：放了多少、
    砍了什么都要可核）。

    规则库缺文件（`data/standard/tcm_theory.jsonl` 不在）时返回空文本 + `available:
    False`——跟 `build_focused_knowledge` 本体不可用时的处理一致，不假装有规则。
    """
    locations = [h.element for h in s2.elements if h.kind == "location"]
    natures = [h.element for h in s2.elements if h.kind == "nature"]

    if not load_theory():
        return "", {"available": False, "n_rules": 0, "by_kind": {}, "trimmed_sections": []}

    picked: dict[str, list] = {k: [] for k in _THEORY_KIND_LABELS}
    trimmed: list[str] = []
    seen_ids: set[str] = set()

    def _add(kind: str, rules) -> None:
        for r in rules:
            if r.id in seen_ids:
                continue
            if len(picked[kind]) >= THEORY_RULES_MAX_PER_KIND:
                if kind not in trimmed:
                    trimmed.append(kind)
                continue
            seen_ids.add(r.id)
            picked[kind].append(r)

    for element in [*locations, *natures]:
        _add("organ_relation", theory_organ_relations(element))
    _add("treatment_principle", theory_principles_for(natures, locations))
    _add("pathomechanism", theory_transitions([*natures, *locations]))
    _add("compatibility", theory_role_construction_rules())

    lines: list[str] = []
    for kind, label in _THEORY_KIND_LABELS.items():
        rules = picked[kind]
        if not rules:
            continue
        lines.append(f"## {label}")
        for r in rules:
            lines.append(f"- [{r.id}]（{r.confidence}）{r.span}")
    text = "\n".join(lines)
    stats = {
        "available": True,
        "n_rules": sum(len(v) for v in picked.values()),
        "by_kind": {k: len(v) for k, v in picked.items()},
        "trimmed_sections": trimmed,
    }
    return text, stats


# E3/E4 消融（eval/run_eval.py）用的三种取值：
#   own     —— 改造前的默认行为，检索这位医家自己的医案库
#   swapped —— 检索另一位医家的医案库（见 _swap_physician_id），但仍然以这位
#              医家的口吻/prompt 出方——检验"换掉参考医案，结论会不会跟着变"
#   none    —— 不检索任何参考医案（等价于该医家检索为空时的既有路径，
#              S3SyndromeUnreferenced 接管）——检验"有没有参考医案，结论会不会变"
ALLOWED_REFS_MODES = {"own", "swapped", "none"}


def _swap_physician_id(physician: str) -> str:
    """按 PHYSICIANS 的登记顺序做一个环形轮换，取"下一位"医家的 id。
    只有 2 位医家时这就是"对方"；3 位及以上时是真正的环（叶→吴→张→叶……）。
    不写死"三位医家"这个数字——PHYSICIANS 目前还是 2 位，张锡纯加入后
    这里不用改一行代码就能自动变成三向轮换（E3/E4 消融同理，见
    eval/run_eval.py 的 collect_refs_mode_pair）。"""
    ids = list(PHYSICIANS)
    idx = ids.index(physician)
    return ids[(idx + 1) % len(ids)]


def _birth_year(years: str | None) -> int | None:
    """注册表里的 years 是「1667-1746」这种生卒年字符串，取生年。年代探针
    （叶×张 约 190 年 vs 吴×张 约 100 年）按生年差算——两位医家"相隔多久"
    看的是他们各自成长的年代，不是谁先去世。解析不了（None/格式不对）返回
    None，不猜一个数。"""
    if not years:
        return None
    head = years.strip().split("-")[0].strip()
    return int(head) if head.isdigit() else None


def _layered_jaccard(sets: list[set]) -> float | None:
    """君臣层 / 佐使层的 n 方交并比。

    用跟 `herb_jaccard` 同一个 n 方公式（`set.intersection` / `set.union`），好让
    三个数放在一起可比——但**空集的处理刻意不同，这两处回答的不是同一个问题**：
    `herb_jaccard` 只要有一位医家开了药就出数，那里的空集含义是"这位医家真的没
    开方"；分层这里要求**每一位医家在这一层都有药**才出数，因为分层的空集含义是
    "这位医家的药没标 role"，不是"他没开这一类药"。把没标注当成"零条重叠"算下去
    会得出 core_jaccard=1.0，被读成"核心用药毫无重叠"，而事实只是没标注——
    这正是 R1-1 那道 role 填充率闸门要先过的原因。

    所以返回 None 而不是 0.0：**0.0 的意思是"完全相同"，跟"没数据"是两回事**
    （divergence["layer_note"] 把这句话也写给前端和读 JSON 的人）。
    """
    if len(sets) < 2 or not all(sets):
        return None
    inter, union = set.intersection(*sets), set.union(*sets)
    return round(1.0 - len(inter) / len(union), 3)


def pairwise_divergence(results: list[dict]) -> dict:
    """1.3（E2）：医家两两配对的分歧，替代"三家 set.intersection"那种只有三家都
    用的药才算共同的 n 方交并比——那个指标在三位医家时天然偏向 1.0，分不清
    师承内（叶×吴，同为温病学派）和跨学派（叶×张、吴×张）。

    每对给：药物 Jaccard 距离（跟 herb_jaccard 同一把尺子，只是两两算）、
    共用药、治法是否相同、group（lineage=同学派 / cross_school=跨学派，从
    PHYSICIANS 的 school 字段判，任一方没登记学派就是 unknown）、year_gap
    （生年差，年代探针）。汇总给 lineage_mean / cross_school_mean 并列，
    以及 cross_school_gt_lineage——这是 1.3 的判据（"跨学派分歧大于师承内"
    在多数主诉上成立），只报出不硬卡。

    results 少于两位、或某对两边都没开药时，对应的数是 None 不是 0：0 会被
    读成"两边用药完全一致"，而实际是"这个维度不适用"。纯函数，不依赖
    consult() 的其他状态，方便单测。"""
    pairs: list[dict] = []
    for ra, rb in combinations(results, 2):
        a, b = ra["physician"], rb["physician"]
        set_a = _herbs.normalized_herb_set(ra["s3"].herbs)
        set_b = _herbs.normalized_herb_set(rb["s3"].herbs)
        union = set_a | set_b
        inter = set_a & set_b
        jaccard = round(1.0 - len(inter) / len(union), 3) if union else None
        info_a, info_b = PHYSICIANS.get(a, {}), PHYSICIANS.get(b, {})
        school_a, school_b = info_a.get("school"), info_b.get("school")
        if school_a and school_b:
            group = "lineage" if school_a == school_b else "cross_school"
        else:
            group = "unknown"
        birth_a, birth_b = _birth_year(info_a.get("years")), _birth_year(info_b.get("years"))
        pairs.append({
            "a": a, "b": b,
            # 展示层用中文名，数据层用 id——两者都给，前端不用再查一遍注册表
            "name_a": info_a.get("name", a), "name_b": info_b.get("name", b),
            "school_a": school_a, "school_b": school_b,
            "group": group,
            "year_gap": abs(birth_a - birth_b) if birth_a is not None and birth_b is not None else None,
            "herb_jaccard": jaccard,
            "shared_herbs": sorted(inter),
            "treatment_principle_same": ra["s3"].treatment_principle == rb["s3"].treatment_principle,
        })

    def _mean(group: str) -> float | None:
        vals = [p["herb_jaccard"] for p in pairs if p["group"] == group and p["herb_jaccard"] is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    lineage_mean, cross_school_mean = _mean("lineage"), _mean("cross_school")
    # 全部配对的均值 = "三家平均差异"（docs/DESIGN.md §3.1 对照带右端那个数）。
    # **跟 ε 同一把尺子**：ε 也是逐条主诉、同一位医家重复跑之间两两距离的均值，
    # 所以这两个数能直接比大小、能画在同一条参考线上。herb_jaccard 不行——
    # 它是 n 方交并比（只有三家都用的药才算共同），跟 ε 不是一个口径，
    # 拿它去跟 ε 比是在比两种不同的东西。
    all_pair_values = [p["herb_jaccard"] for p in pairs if p["herb_jaccard"] is not None]
    return {
        "pairs": pairs,
        "pairs_mean": (round(sum(all_pair_values) / len(all_pair_values), 3)
                       if all_pair_values else None),
        "lineage_mean": lineage_mean,
        "cross_school_mean": cross_school_mean,
        "n_lineage_pairs": sum(1 for p in pairs if p["group"] == "lineage"),
        "n_cross_school_pairs": sum(1 for p in pairs if p["group"] == "cross_school"),
        # None = 两类里至少一类没有可比的对（比如只有两位医家、或某类全没开药）
        "cross_school_gt_lineage": (
            cross_school_mean > lineage_mean
            if lineage_mean is not None and cross_school_mean is not None else None
        ),
    }


# R22：best-of-N 打分挑选。**评分尺是 R23 的 score_formula，不是这里另算一个**
# ——同一把尺同时给医生看建议、给这里排序，改权重只改一处。
def _as_s3_syndrome(raw):
    """把 S3 这一步的原始产出统一成下游认识的 `S3Syndrome`。

    `S3_MODE=structured` 下 `generate()` 返回的是 `S3Structured`（五步链、一张方），
    `S3_MODE=derived`（R52）下是 `S3Derived`（五步链、无医案），legacy 下返回的
    已经是 `S3Syndrome`。**只此一处转换**——打分、X2 输出侧安全、幻觉检查、
    病名校验、方剂建议、分歧度、api 的角色裁剪、前端，全都只认识 `S3Syndrome`，
    让它们各自判断一遍"这是哪种 schema"等于把这一跳抄七遍（第 31 条）。

    判据是 `hasattr(raw, "to_s3_syndrome")` 而不是 `isinstance(raw, _S3StructuredBase)`
    ——`S3Derived` 跟 `_S3StructuredBase` 是并列关系（R52 的 schema 文档字符串：
    字段形状不同，继承会让案例引用的校验污染进没有医案的 schema），`isinstance`
    检查不出它，鸭子类型检查两边都认。

    转换本身在各自 schema 的 `to_s3_syndrome()` 里，不在这里——这个函数只回答
    "要不要转"，"怎么转"是 schema 自己的事。
    """
    to_s3 = getattr(raw, "to_s3_syndrome", None)
    return to_s3() if callable(to_s3) else raw


def _score_candidate(s3) -> tuple[float, dict]:
    """一次采样的分 + 写进 manifest/响应的那一行。

    分数只看 `selected` 那张方：模型自己挑了一张，我们评的就是它挑的那张。
    评所有候选方再取最高会让"模型挑得对不对"这件事从判据里消失。

    接 `S3Structured` 也接 `S3Syndrome`：开头先过 `_as_s3_syndrome` 合流，
    structured 模式只出一张方，转换之后 `selected` 恒为 0，这段代码一个字不用改。
    """
    s3 = _as_s3_syndrome(s3)
    selected = s3.formula_candidates[s3.selected]
    check = check_formula(s3.syndrome, selected.herb_items)
    return check.score, {
        "score": check.score,
        "syndrome": s3.syndrome,
        "formula": selected.name,
        # 逐类计数而不是整条 advice：这一行是给"为什么选了它"用的，
        # 完整建议在 results[i].advice 里（那是**选中那张**的建议，不重复三份）。
        "advice_kinds": sorted({a.kind for a in check.advice}),
        "n_advice": len(check.advice),
    }


def _streaming_note(n_samples: int) -> str | None:
    """这次 S3 为什么没有增量。能流式就返回 None。

    **三种"没流式"要分开**（后端不支持 / 是模拟的 / best-of-N 这一路不流式），
    合成一句"未启用"的话，前端转着圈等的时候没人知道该修哪儿。
    后端那两种由 `LLMBackend.streaming_note()` 回答（判据在后端自己身上，
    不在这里抄一份）。
    """
    if n_samples > 1:
        return (f"这次 S3 采了 {n_samples} 次（best-of-N），几路同时在飞，"
                "增量混在一条流里没法用，所以这一路不流式。设 S3_BEST_OF_N=1 可开")
    llm = get_llm()
    note = getattr(llm, "streaming_note", None)
    if callable(note):
        return note()
    # 鸭子类型的后端（测试替身、第三方实现）没有这个方法。**不抛异常**——
    # manifest 的一个统计项取不到不该让整次问诊失败（同 `_prefix_tokens_or_none`
    # 那条），而"这个后端没报"跟"报了说不支持"要能分开。
    backend_id = getattr(llm, "backend_id", None)
    who = backend_id() if callable(backend_id) else type(llm).__name__
    return f"后端 {who} 没有实现 streaming_note()，这次有没有流式无从判断"


#: 流式中途从半截 JSON 里扒药名用的。**只扫 `herb_items` 之后那一段**：
#: `"name"` 这个键在方名（`candidate.name`）上也有，整段扫会把方名当药名。
_PARTIAL_ITEMS_ANCHOR = '"herb_items"'
_PARTIAL_NAME_RE = re.compile(r'"name"\s*:\s*"([^"\\]{1,16})"')
_PARTIAL_DOSE_RE = re.compile(r'"dose"\s*:\s*(null|[0-9]+(?:\.[0-9]+)?)')


def scan_partial_herb_items(text: str) -> list[HerbItem]:
    """从**还没输出完**的 S3 JSON 里扒出已经成型的药名与剂量。

    R40 投机执行用。**这是个尽力而为的扫描，不是解析器**：
      · 只取 `"herb_items"` 之后的部分（方名也叫 `name`，见上面那条注释）
      · 每个 `"name"` 往后找到下一个 `"name"` 之前的 `"dose"` 配对，
        找不到就 dose=None（`dose_exceeds` 会把"有上限可比但没写剂量"
        记成 unverifiable，这正是想要的语义）
      · 最后一条可能是半截的（引号还没闭合）→ 正则匹配不上，自然被跳过

    扒错的后果由调用方兜：只有**veto 级**结论才发提示，且措辞写明"初步"，
    最终以完整验证为准。宁可晚报，不可错报——这是临床产品，不是日志。
    """
    if not text:
        return []
    pos = text.find(_PARTIAL_ITEMS_ANCHOR)
    if pos < 0:
        return []
    seg = text[pos + len(_PARTIAL_ITEMS_ANCHOR):]
    out: list[HerbItem] = []
    names = list(_PARTIAL_NAME_RE.finditer(seg))
    for i, m in enumerate(names):
        end = names[i + 1].start() if i + 1 < len(names) else len(seg)
        dm = _PARTIAL_DOSE_RE.search(seg, m.end(), end)
        dose: float | None = None
        if dm and dm.group(1) != "null":
            try:
                dose = float(dm.group(1))
            except ValueError:
                dose = None
        try:
            out.append(HerbItem(name=m.group(1), dose=dose))
        except Exception:  # noqa: BLE001 - 半截的名字过不了 schema 校验，跳过就是
            continue
    return out


class S3DeltaEmitter:
    """R36：把 S3 的流式增量合并成 `s3_delta` 事件。

    **必须合并。** 一个 token 一帧的话，一次 S3 输出几千帧 SSE，前端每帧都要
    JSON.parse + 重排一次；而人眼分辨不出 30ms 和 120ms 的差别。合并判据是
    "攒够 80 字 或 距上次 ≥120ms"，收尾无条件冲一次——不冲的话最后一段永远发不出。

    **思考与正式输出分两路累积。** 合成一路会把思考过程拼进方药文本里
    （`core.llm._delta_texts` 那一层已经把两者分开了，这里不许合回去）。

    计数（`chars` / `events` / `first_delta_s`）是给 bench 与 manifest 用的：
    "首字延迟"这个验收项没有计数就只能靠掐表。
    """

    FLUSH_CHARS = 80
    FLUSH_SECONDS = 0.12

    #: R40 投机执行：正式输出每多这么多字，就拿半截 JSON 里已成型的药名
    #: 跑一次"只看药名"的两条 veto 规则。不是每帧都跑——`verify_incremental`
    #: 本身只要 0.1 ms 级，但正则扫的是**累积全文**，每帧扫一遍是 O(n²)。
    SPECULATIVE_EVERY_CHARS = 400

    def __init__(self, on_step: StepFn | None, physician: str, physician_name: str,
                 *, speculative: bool = True) -> None:
        self._on_step = on_step
        self._physician = physician
        self._physician_name = physician_name
        self._buf: dict[str, str] = {"content": "", "reasoning": ""}
        self._last_flush: dict[str, float] = {"content": 0.0, "reasoning": 0.0}
        self._t0 = time.monotonic()
        self.chars: dict[str, int] = {"content": 0, "reasoning": 0}
        self.events = 0
        self.first_delta_s: float | None = None
        # 投机执行的状态。`_full` 留累积的正式输出（扫描要全文，增量帧不够）。
        self._speculative = speculative
        self._full = ""
        self._next_scan_at = self.SPECULATIVE_EVERY_CHARS
        #: 已经报过的（规则, 药名元组）——**同一条 veto 只报一次**，
        #: 后面每次扫描都会再看见它，重复报会把提示区刷满。
        self._early_reported: set[tuple] = set()
        self.early_vetoes: list[dict] = []

    def __call__(self, text: str, kind: str) -> None:
        if not text:
            return
        if kind not in self._buf:
            # 认不出的种类**当正式输出处理并计数**，不静默丢：丢掉的表现是
            # "前端少了一段"，而那时没人知道少了什么。
            kind = "content"
        if self.first_delta_s is None:
            self.first_delta_s = round(time.monotonic() - self._t0, 4)
        self.chars[kind] += len(text)
        self._buf[kind] += text
        if self._speculative and kind == "content":
            self._full += text
            if self.chars["content"] >= self._next_scan_at:
                self._next_scan_at = self.chars["content"] + self.SPECULATIVE_EVERY_CHARS
                self._speculate()
        now = time.monotonic()
        if (len(self._buf[kind]) >= self.FLUSH_CHARS
                or now - self._last_flush[kind] >= self.FLUSH_SECONDS):
            self._emit(kind)

    def flush(self) -> None:
        for kind in list(self._buf):
            if self._buf[kind]:
                self._emit(kind)

    def _emit(self, kind: str) -> None:
        text, self._buf[kind] = self._buf[kind], ""
        self._last_flush[kind] = time.monotonic()
        if not text:
            return
        self.events += 1
        if self._on_step is not None:
            self._on_step("s3_delta", {
                "physician": self._physician,
                "physician_name": self._physician_name,
                "kind": kind,
                "text": text,
                # 到这一帧为止这一路累计多少字：前端要能判断自己有没有漏帧，
                # 而只发增量的话漏了一帧没人看得出来。
                "chars": self.chars[kind],
                "seq": self.events,
            })

    def _speculate(self) -> None:
        """拿半截输出里已成型的药名跑两条 veto 规则，**命中就当场报一条提示**。

        为什么值得：配伍禁忌和超药典上限是 veto 级——命中这张方根本不会下发。
        真实后端上 S3 要几十秒到几分钟，等输出完再说"这张方作废了"，
        那几十秒白等。药名一出来就能判。

        **一次扫描失败不能影响这次问诊**：这是个尽力而为的旁路，扒错、
        本体不在、schema 拒了半截的名字——任何异常都只意味着"这一次没提示"。
        """
        try:
            items = scan_partial_herb_items(self._full)
            if len(items) < 2:      # 一味药谈不上配伍；剂量那条也要有名字才查得到
                return
            from core.formula_verifier import rule_label, verify_incremental

            result = verify_incremental(items)
            for v in result.vetoes:
                key = (v.rule, v.herbs)
                if key in self._early_reported:
                    return
                self._early_reported.add(key)
                row = {"rule": v.rule, "rule_label": rule_label(v.rule),
                       "herbs": list(v.herbs), "reason": v.reason,
                       "n_herbs_scanned": len(items),
                       # **措辞是这条提示的一半**：半截输出上的结论可能作废，
                       # 说成定论就是在临床界面上撒谎。
                       "note": "初步提示：基于尚未输出完的药味清单，"
                               "最终以完整符号验证为准"}
                self.early_vetoes.append(row)
                if self._on_step is not None:
                    self._on_step("early_veto", {
                        "physician": self._physician,
                        "physician_name": self._physician_name, **row})
        except Exception:  # noqa: BLE001 - 旁路，见文档字符串
            return

    def summary(self) -> dict:
        """写进 `s3_done` 与 manifest 的那几个数。"""
        return {
            "events": self.events,
            "chars_content": self.chars["content"],
            "chars_reasoning": self.chars["reasoning"],
            "first_delta_s": self.first_delta_s,
            # 投机执行提前报了几条。**0 和"没开"要分得开**：`speculative`
            # 一起下发，否则读数的人分不清"没命中"和"没跑"。
            "speculative": self._speculative,
            "n_early_vetoes": len(self.early_vetoes),
        }


def _best_of_n_s3(s3_system: str, s3_schema, physician: str, *, on_delta=None):
    """采 N 次 S3，按 `score_formula` 挑分最高的一次。返回 (s3, candidates_scored)。

    `on_delta`（R36）**只在 N=1 时往下传**。N>1 时几路采样同时在飞，
    把它们的增量混在一条流里发出去，前端拼出来的是几张方交错的乱码——
    与其发一堆没法用的帧，不如这一路不流式（`streaming_skipped_reason`
    会如实说出原因，不让人以为是后端不支持）。

    **N=1 时逐字节走回 R21 及之前的那条路径**（一次 generate、不建线程池、
    candidates_scored 只有一条）——把"关掉 best-of-N"做成一条独立代码路径会让
    两条路径慢慢分叉，而这个旋钮正是要拿来做对照实验的。

    并发：用线程池发 N 次，真正的并发上限由 `LLM_MAX_INFLIGHT` 的信号量管
    （在 core/llm.py 里，跨所有医家共享）。这里**不再设第二个上限**——
    两个闸门管同一件事时，实际生效的是哪个取决于数值大小，那是看不出来的行为。
    每个 worker 跑在 `copy_context().run` 里：重试统计和 usage 统计都在
    ContextVar 上，不拷贝 Context 的话工作线程写进的是一个没人读的地方
    （R21 在 `_complete_within_deadline` 上踩过这个坑，SOURCES.md 第 64 条第九点）。

    平手时取**下标最小**的那次：`max` 对相等的键返回先遇到的那个，而下面是按
    下标顺序遍历的，所以这条是确定的——同一批采样结果永远选出同一张方。
    """
    n = s3_best_of_n()
    thinking = thinking_for("s3")

    def one(stream=False):
        extra = {"on_delta": on_delta} if (stream and on_delta is not None) else {}
        return get_llm().generate(
            system=s3_system, user="", schema=s3_schema, physician=physician,
            **extra, **thinking,
        )

    if n == 1:
        s3 = one(stream=True)
        score, row = _score_candidate(s3)
        return s3, [{**row, "index": 0, "chosen": True}]

    samples: list = [None] * n
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="s3-sample") as pool:
        futures = {pool.submit(contextvars.copy_context().run, one): i for i in range(n)}
        for fut in as_completed(futures):
            i = futures[fut]
            # 一次采样失败不该让整位医家失败：N 次里有一次撞 429/超时是常态，
            # 剩下的仍然能挑出一张。**全部失败才抛**——那时抛的是最后一个异常，
            # 而不是一个"没有候选方"的假结果（后者会在下游变成 IndexError，
            # 离根因十几帧远）。
            try:
                samples[i] = fut.result()
            except Exception as exc:  # noqa: BLE001
                samples[i] = exc

    scored: list[dict] = []
    best_i, best_score, best_s3 = None, -1.0, None
    last_exc: BaseException | None = None
    for i, sample in enumerate(samples):
        if isinstance(sample, BaseException):
            last_exc = sample
            scored.append({"index": i, "score": None, "chosen": False,
                           "error": type(sample).__name__})
            continue
        score, row = _score_candidate(sample)
        scored.append({**row, "index": i, "chosen": False})
        if score > best_score:
            best_i, best_score, best_s3 = i, score, sample
    if best_s3 is None:
        # N 次采样全失败。抛最后那个真实异常，不抛一个"没有候选方"的假结果。
        # **不用 assert 做类型收窄**：`python -O` 会把 assert 整行删掉，
        # 那时 `raise None` 抛的是「exceptions must derive from BaseException」,
        # 把真正的根因（429 / 超时 / 校验失败）盖掉。
        if last_exc is None:
            raise LLMError(
                f"S3 best_of_n 采样 {n} 次，既没有成功的候选也没有记下异常"
                "——这个状态不该出现，请连同 s3_best_of_n() 的取值一起报告。"
            )
        raise last_exc
    scored[best_i]["chosen"] = True
    return best_s3, scored


def run_physician(
    s1: S1Normalize,
    s2: S2Elements,
    physician: str,
    physician_name: str,
    use_react: bool = False,
    followup: FollowupResult | None = None,
    ask_fn: AskFn | None = None,
    refs_mode: str = "own",
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

    refs_mode 见模块里 ALLOWED_REFS_MODES 上面那段注释。默认 "own"，
    行为、调用参数跟改造前逐字节一致——不传就是没有这个开关时的老路。
    """
    if refs_mode not in ALLOWED_REFS_MODES:
        raise ValueError(f"未知的 refs_mode={refs_mode!r}，目前支持 {sorted(ALLOWED_REFS_MODES)}")

    symptoms_text = "；".join(s1.symptoms)
    # ReAct 追问命中危重症状时，demo 模式抛 SafetyVeto 中止；EVAL_MODE 下不中止，
    # 把本该拦截的原因经由返回值带回 consult()（异常没抛，只能走返回值这条路）。
    react_safety_flag: str | None = None

    # 检索该医家 top-3 医案。refs_mode="none" 时连检索都不做（不是查出来再扔掉）
    # ——E4 要测的是"完全没有参考医案"这个条件，真的不检索比检索了再清空更贴近
    # 这个条件本身，也省一次不会被用到的检索调用。
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"
    if refs_mode == "none":
        hits, low_discrimination = [], False
    else:
        search_physician = physician if refs_mode == "own" else _swap_physician_id(physician)
        hits, low_discrimination = _search_cases(query, search_physician, s2, retriever_mode)
    # 一条相关医案都没有时换用不含 cited_case_ids 的 schema（见 S3SyndromeUnreferenced
    # 的文档字符串）。不是放松 min_length=1，是这个场景下根本没有可引用的东西。
    s3_schema = S3Syndrome if hits else S3SyndromeUnreferenced
    # refs 要给前端证据链侧栏用：只给 (id, score) 的话，用户看到
    # ye_tianshi-0031-p6-0 完全不知道那是什么医案，"可追溯"这个卖点就断在这里。
    refs = [_ref_row(case, score) for case, score in hits]
    refs_text = "\n\n".join(_format_case_block(case) for case, _ in hits) or "（无可用参考医案）"

    # S3 证候+治法+方
    #
    # R21：`full_context` 下整份 system prompt 由 core/context_prefix.assemble()
    # 组装（知识速查表 → 指令 → 该医家医案全量 → 药材条目 → 本次问诊），
    # 目的是让前面那几段进 v4-pro 的前缀缓存。**参考医案块仍然走
    # `_format_case_block`**（assemble 内部默认就是它），所以两种模式下医案的
    # 格式逐字节相同——E3/E4 闸门验过的那个格式没有第二份实现。
    # `hits` 直接传进去当 §4：refs_mode 的 own/swapped/none 已经在
    # `_search_cases` 那一步体现在 hits 里了，这里不再判一次。
    mode_eff = effective_mode(retriever_mode)
    # R32：知识块进**所有**检索模式。
    #
    # 在此之前知识速查表只在 `full_context` 下进提示词（走 assemble 的稳定前缀），
    # 而演示与录制跑的是 `hybrid`——"让模型明白药理"在运行配置下从未发生过，
    # 且所有测试全绿（没有一条测试断言"知识块出现在最终 prompt 里"）。
    knowledge_text, knowledge_stats = "", {"available": False, "n_herbs": 0,
                                           "n_formulas": 0, "n_patterns": 0,
                                           "tokens": 0, "trimmed_sections": []}
    knowledge_mode = knowledge_in_prompt(mode_eff)
    if knowledge_mode == "focused":
        knowledge_text, knowledge_stats = build_focused_knowledge(
            s1, s2, hits, list(physicians_enabled(PHYSICIANS)),
            # 候选证型取自本次检索到的医案——S3 还没跑，这是此刻能拿到的最好线索。
            syndromes=[c.syndrome for c, _ in hits if c.syndrome],
        )
    if mode_eff == "full_context":
        s3_system = assemble(
            physician, s1=s1, s2=s2,
            case_block=[case for case, _ in hits],
            elements_summary=_format_elements_summary(s2),
            symptoms=symptoms_text,
        )
    else:
        s3_prompt = load_prompt("s3_syndrome")
        # 知识块插在参考医案块**之前**。`_format_case_block` 的输出与它在 $refs
        # 里的位置一字未动——E3/E4 闸门验过的就是那个格式与那个相对次序。
        # `knowledge_text` 为空时 `refs` **逐字节等于**改造之前（这正是
        # KNOWLEDGE_IN_PROMPT=off 那一档要的"对照组"语义）。
        refs_with_knowledge = (
            f"{knowledge_text}\n\n## 参考医案\n\n{refs_text}" if knowledge_text else refs_text
        )
        s3_system = render(
            s3_prompt["system"],
            name=physician_name,
            elements_summary=_format_elements_summary(s2),
            symptoms=symptoms_text,
            refs=refs_with_knowledge,
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
        s3_system, trace, react_safety_flag = _run_react_round(
            s3_system, s1, s2, physician, physician_name=physician_name,
            ask_fn=ask_fn, bypass_safety=bypass_safety, on_step=on_step,
        )

    if on_step is not None:
        # 没开 ReAct 时这是这位医家唯一一次要等的 LLM 调用；开了 ReAct 也要报——
        # 取证结束不代表马上有结果，S3 本身也要等一次真实调用。
        on_step("s3_start", {"physician": physician, "physician_name": physician_name})
    # R36：流式。`emitter` 在没有 on_step 时也建（它自己判 None），这样
    # `s3_done` 里的计数在 CLI / eval 那条路上照样是真的。
    emitter = S3DeltaEmitter(on_step, physician, physician_name)
    # 混进 herbs 的西药（模型没照 prompt 的要求分开写）在 schema 构造时就已经被
    # core.schemas._S3Base 的 model_validator 挑到 western_drugs 了，这里不用
    # 再包一层 _split_western_into_s3——这一步以前是代码层面的兜底，现在兜底
    # 挪进了 schema 本身，构造完成的这一刻 s3.herbs/s3.western_drugs 就已经是
    # 拆好的（M1 之前这里对 herbs 混西药的拟合方式做了一次真实回归，见 SOURCES.md）。
    # physician 传下去是给本地后端选 LoRA adapter 用的（阶段五每位医家一个
    # adapter，vLLM server 按请求切换）。传的是 id 不是中文名——SOURCES.md
    # 第 31 条那个坑：id 和中文名混用会让按 id 索引的东西恒空。云端后端
    # （DeepSeek）如实忽略它，见 core/llm.py::LLMBackend._complete 的文档。
    # S3 是这条链上唯一真正需要推理的一步，默认开思考（S3_THINKING 可整体关掉，
    # 关掉之后跑出来的数字跟默认配置不可比——manifest 会带上这句话）。
    s3, candidates_scored = _best_of_n_s3(s3_system, s3_schema, physician,
                                          on_delta=emitter)
    emitter.flush()
    if on_step is not None:
        on_step("s3_done", {"physician": physician, "physician_name": physician_name,
                            **emitter.summary(),
                            "streaming_note": _streaming_note(len(candidates_scored))})

    # X2 输出侧安全（M2 起覆盖五条规则，见 core/safety_output.assess_formula_safety
    # 的文档字符串）：给每个候选方都算一份 FormulaSafety，不是只算 selected 那个——
    # M5 的图要在每个候选方节点上标安全状态，选中的和没选中的都要有数据可用。
    # 拦截判据只看 selected 那个：incompatible/dose_violations 命中就把问题写进
    # prompt 重开一次。只重开一次、不循环——循环会让 llm_calls 变成不可预测的数，
    # manifest 里那个调用数就没法用来算成本和比较配置了。
    for cand in s3.formula_candidates:
        cand.safety = assess_formula_safety(s3.syndrome, cand.herb_items)
    selected_safety = s3.formula_candidates[s3.selected].safety
    revised = False
    if selected_safety.blocking:
        retry_system = s3_system + (
            f"\n\n【安全问题】上一次拟的方（当前选中的候选方）存在以下必须修正的"
            f"问题：{format_blocking_issues(selected_safety)}。请重新拟方解决这些"
            "问题，其余要求不变。"
        )
        s3 = get_llm().generate(
            system=retry_system, user="", schema=s3_schema, physician=physician,
            **thinking_for("s3"),
        )
        for cand in s3.formula_candidates:
            cand.safety = assess_formula_safety(s3.syndrome, cand.herb_items)
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

    # M4：病名层。disease_candidates 是规则算出来的对照（跟模型脱钩，同样输入
    # 永远同样输出），前端可以摆出"模型判断 X，规则倾向 Y"这种交叉校验。
    # 只用病位证素（kind="location"）去匹配 Disease.location——nature 证素
    # （气滞/血瘀……）不在 core.elements.LOCATIONS 词表里，传进去也匹配不上，
    # 过滤掉更清楚地表达"这里比的是病位"，不是隐式依赖 match_disease 内部
    # 会自动跳过不认识的词。
    disease_candidates = match_disease(
        s1.symptoms, [h.element for h in s2.elements if h.kind == "location"]
    )
    # 模型填的病名不在参考表里（含别名）时只记 warning，不拒绝——古籍病名可能
    # 超出这 15 个，硬拒绝会丢信息（CLAUDE.md「防幻觉约束不许放松」管的是
    # cited_case_ids 那类可验证事实，病名属于医家的专业判断，不是同一类约束）。
    if s3.disease is not None and get_disease(s3.disease) is None:
        warn = f"病名「{s3.disease}」不在病名参考表（含别名）里，未做规则校验。"
        s3.note = f"{s3.note}；{warn}" if s3.note else warn

    # R23：方剂建议层。跟 safety_output 那三个键**不是同一件事**：那三个回答
    # "这方能不能发出去"，这里回答"这方拟得好不好"——同一张方可以既没有拦截级
    # 问题、又拿到一条"缺引经药"的建议。只算 selected 那一张：其余候选方的
    # 建议没有消费方（R22 的 best-of-N 是在采样出的多张 s3 之间选，
    # 那时每张都是各自的 selected），算了也只是往响应里塞没人读的数据。
    formula_check = check_formula(s3.syndrome, s3.formula_candidates[s3.selected].herb_items)

    return {
        "physician": physician,
        "physician_name": physician_name,
        "s2": s2,
        "s3": s3,
        # R32：这次给这位医家的提示词里放了什么知识块。
        # **记在结果里而不是只记 manifest**：知识块是按医家的 hits 裁剪的，
        # 整次问诊一个数说不清楚谁看到了什么——跟 lora 字段同一个理由。
        "knowledge": {"mode": knowledge_mode, **knowledge_stats},
        # R36：这次 S3 有没有真的流式、发了多少帧、首字多久。
        # **记在结果里而不是只记 manifest**：理由同 knowledge——manifest 一个数
        # 说不清楚哪位医家那一路流了、哪一路没流。
        "streaming": {**emitter.summary(),
                      "note": _streaming_note(len(candidates_scored))},
        "disease_candidates": disease_candidates,
        "refs": refs,
        # E3/E4 消融要按 (主诉, 医家) 配对比较不同 refs_mode 的结果；结果自带
        # 这个字段，eval/run_eval.py 的收集代码不用另外在外层记一份"这条是哪个
        # 模式跑出来的"，也避免两边状态不同步。
        "refs_mode": refs_mode,
        # True = 检索为空，这位医家的结论没有任何医案支撑；前端要明示，不能当成
        # "引用了 0 条"静默过去
        "no_reference_cases": not hits,
        # P0-12：True = 检索到的候选之间没有真实区分度，_search_cases 已经
        # 把 top-3 收窄成了 top-1——不是"检索为空"，是"检索到了但塞三条等于
        # 随机三选三"。eval/run_eval.py 的 E3 报告要能看到这个标记的比例。
        "low_discrimination": low_discrimination,
        # 这位医家这一次实际挂的 LoRA adapter 名，None = 跑的是基座模型。
        # 记在每位医家的结果上而不是只记在 manifest 里：manifest 是整次问诊
        # 一份，而 adapter 是按医家切的——「张锡纯用的是他自己的 LoRA」这句
        # 声称只有在这个粒度上才可验证。非本地后端恒为 None（没有 adapter
        # 这回事），lora_for() 的默认实现就返回 None，不用在这里判后端类型。
        "lora": get_llm().lora_for(physician),
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
        # R23：建议、没跑的规则、粗排序分。三个键一起给——只给 advice 的话，
        # "这条规则没给出建议"和"这条规则因为缺数据没跑"在界面上长得一样。
        "advice": advice_dicts(formula_check),
        "advice_skipped": list(formula_check.skipped),
        "formula_score": formula_check.score,
        # R22：这一位医家这次采了几张方、每张多少分、选了哪张。
        # **安全层的重开发生在挑选之后**，所以这一行记的是"挑选时"的分，
        # 而 formula_score 是最终留下那张方的分——重开过的话两者会不同，
        # 这正是要分两个字段的理由。
        "candidates_scored": candidates_scored,
        "best_of_n": len(candidates_scored),
    }


def _run_react_round(
    s3_system: str, s1: S1Normalize, s2: S2Elements, physician: str | None, *,
    physician_name: str | None = None, ask_fn: AskFn | None,
    bypass_safety: bool, on_step: StepFn | None,
) -> tuple[str, ReActTrace, str | None]:
    """G2：跑一轮 ReAct 取证，把查到的东西**追加**到 S3 prompt 后面。
    返回 `(追加后的 system, trace, 安全标记)`。

    抽成函数是因为 R33 起两条路径都要它（`run_physician` 一家、`run_synthesis`
    五家），而中间那段「追问的回答必须先过 `check_safety`」是 CLAUDE.md 点名的
    一条硬约束——抄两份意味着将来改一边会漏另一边，那正是「安全否决的后门」
    这条约束最怕的事。

    `physician=None` 是 `run_synthesis` 用的：工具层按 physician 过滤医案，
    传五家里的任意一位都是错的（那位的医案库不等于五家的），传 None 表示
    **不按医家过滤**——`resolve_physician_id(None)` 返回 None，过滤处不加条件。
    `physician_name` 同理可空，`run_react` 的 `$name` 那时填一个中性称呼。

    只追加、不改任何 prompt yaml——不开 ReAct 时 prompt 要跟没有这个开关时
    逐字节一致，否则 `use_react` 的 A/B 里混进了 prompt 变化这个额外变量。
    """
    react_safety_flag: str | None = None
    trace = run_react(
        name=physician_name or SYNTHESIS_PHYSICIAN_NAME,
        # physician 在这里已经是 id：prompt 里的 $physician_id 直接用它，
        # 不让 run_react 再从中文名反查一遍（反查是兜底，不是主路径）。
        physician_id=physician,
        symptoms="；".join(s1.symptoms),
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
    return s3_system + format_trace_for_s3(trace), trace, react_safety_flag


def _verify_and_revise(raw, s3_system: str, s3_schema, *, on_step: StepFn | None = None):
    """R34 闭环：验 → 有问题就带着**本体原文反例**重开 → 再验，最多
    `max_revise_rounds()` 轮。返回 `(最终 raw, 最终 s3, 每轮的验证结果, 重开次数)`。

    ## 三条设计决定

    **一、重开时关思考**（`thinking="disabled"`）。这一步不是"再想一遍怎么辨证"
    ——证型、治法、五步链都已经定了，要改的是"把这味药换成归肝经的"这种
    照着反例改的局部修补。开思考在这一步是纯浪费（实测数据见 §0.4：单次 S3
    开思考 260–479 秒），而且**反而更容易把已经对的部分重新想一遍想坏**。

    **二、veto 残余不下发。** 轮数用完还有 veto 级违规就抛 `SymbolicVeto`，
    整次问诊不产出方药——跟安全否决同一条语义（被拦截的请求不产出任何方药），
    但是两个不同的异常（见 `SymbolicVeto` 的文档字符串）。
    revise 级残余**照常下发**并如实标在 `verification` 里：那些是"拟得不够好"，
    不是"不能用"，压着不发等于因为一条归经覆盖建议就不给患者任何东西。

    **三、每一轮的结果都留着。** `verify_rounds` 是 list 而不是只留最后一个：
    `verifier_first_pass_rate` 要的是**第一轮**的状态，而"改了三轮才过"和
    "一次就过"在最终态上看起来一模一样。

    ## unverifiable 不进回灌

    本体缺数据时那条规则判不了（归经缺 49%、用量缺 55%）。把这些写进回灌文本
    只会让模型以为自己错了、去改一个本来可能对的地方——它改方也改不出数据来。
    它们的去处是 `verification` 字段与 manifest（如实显示"这几条判不了"）。
    """
    rounds: list = []
    revise_calls = 0
    s3 = _as_s3_syndrome(raw)
    limit = max_revise_rounds()
    while True:
        result = verify_formula(raw)
        rounds.append(result)
        if not result.violations or revise_calls >= limit:
            break
        feedback = format_violations_for_revise(result)
        if not feedback:            # 理论上不会到：violations 非空时它必非空
            break
        if on_step is not None:
            on_step("verify_revise", {
                "round": revise_calls + 1, "status": result.status,
                "n_veto": len(result.vetoes), "n_revise": len(result.revisables),
                "rules": sorted({v.rule for v in result.violations}),
            })
        raw = get_llm().generate(
            system=s3_system + feedback, user="", schema=s3_schema,
            physician=SYNTHESIS_PHYSICIAN_ID,
            # 关思考：这一步是照着反例做局部修补，不是重新辨一遍证（见上面第一条）。
            thinking="disabled", reasoning_effort=None,
        )
        s3 = _as_s3_syndrome(raw)
        revise_calls += 1
    if rounds[-1].vetoes:
        raise SymbolicVeto(rounds[-1].vetoes, llm_calls=revise_calls,
                           rounds=revise_calls)
    return raw, s3, rounds, revise_calls


class SymbolicVeto(Exception):
    """R34：符号验证器的 veto 级违规在 `MAX_REVISE_ROUNDS` 轮之后仍未消除。

    **跟 `SafetyVeto` 是两件不同的事**，所以是两个异常而不是复用一个：
      - `SafetyVeto`：**输入侧**——主诉或追问的回答里有危重症状，整个请求不该辨证
      - 本异常：**输出侧**——辨完了、方也开了，但方本身有配伍禁忌/超量/编造出处，
        改了三轮还在，这张方不下发
    合并成一个异常的话，前端会把"你的症状需要立刻就医"和"系统改不出一张合规的方"
    显示成同一句话，而这两件事患者该做的完全不同。

    `violations` 带上，好让响应里能如实说出是哪几条——不是一句"验证失败"。
    """

    def __init__(self, violations, llm_calls: int = 0, rounds: int = 0):
        self.violations = tuple(violations)
        self.llm_calls = llm_calls
        self.rounds = rounds
        detail = "；".join(f"[{v.rule}] {v.reason}" for v in self.violations)
        super().__init__(f"符号验证有 {len(self.violations)} 条不可下发的问题"
                        f"（已重开 {rounds} 轮）：{detail}")

    @property
    def reason(self) -> str:
        """给响应用的一句人话。**不含本体原文**——那是给模型看的反例，
        对患者来说是噪音；界面要看细节时读 `verification` 字段。"""
        rules = "、".join(dict.fromkeys(v.rule for v in self.violations))
        return (f"这张方在符号验证中有不可下发的问题（{rules}），"
                f"系统已按本体原文重开 {self.rounds} 轮仍未消除，因此不给出方药。"
                "请换用人工复核，或补充更多症状信息后重试。")


#: 结构化模式下这份结论在 `results` 里的身份。
#:
#: `results` 的元素结构是既有契约（前端、分歧度、eval 收集器都按它读），
#: structured 模式只有**一个**元素，但它仍然需要一个 `physician` 值。
#: 用一个**不在注册表里**的保留 id 而不是随便挑一位医家的 id：挑一位的话
#: 「这份结论是叶天士给的」这句话就是假的，而前端会照着把它显示成叶天士的方。
SYNTHESIS_PHYSICIAN_ID = "synthesis"

#: **R44：显示名从「五家综合」改成「本次辨证」。**
#:
#: 「五家综合」把这份结论说成"几个人拼出来的"——那是**内部机制**，不是产品形态。
#: 总纲 §12 的原话是「能力不删，产品面不露」：五家各出一份再融合这件事照旧
#: 在跑（`run_synthesis` 一行没改），研究面（researcher 角色）照旧拿到三列与
#: 分歧读数；变的是**结论顶上那句话**。
#:
#: 为什么是「本次辨证」而不是某个人名或者某个产品名：
#:   - 人名是假的（这份结论不是哪一位医家给的）；
#:   - 产品名会让这句话变成广告；
#:   - 「本次辨证」如实说出这是什么——**这一次问诊的辨证结论**，
#:     而名老中医经验是它引用的依据（`physician_influences` 逐条标着谁、哪一步）。
#:
#: 这不是把能力藏起来：谁贡献了哪一步仍然在 `physician_influences` 里逐条可查，
#: 九段界面照样显示「叶天士·取象」这种归属。改的是**框架**——从"几个人投票"
#: 改成"一位医师引用了几家的经验"。
SYNTHESIS_PHYSICIAN_NAME = "本次辨证"


def run_synthesis(
    s1: S1Normalize,
    s2: S2Elements,
    use_react: bool = False,
    followup: FollowupResult | None = None,
    ask_fn: AskFn | None = None,
    refs_mode: str = "own",
    bypass_safety: bool = False,
    on_step: StepFn | None = None,
    retriever_mode: str | None = None,
) -> dict:
    """R33：五位医家融合成**一份**结构化诊断（`S3_MODE=structured`）。

    跟 `run_physician` 是**并列的两条路径**，不是它的一个分支：两者检索的范围
    （五家 vs 一家）、prompt（s3_structured vs s3_syndrome）、schema
    （S3Structured vs S3Syndrome）、调用次数（1 vs N 位）全都不同，塞进同一个
    函数里会变成一串 `if mode == "structured"`，而那正是这个项目反复吃过亏的形状。

    **返回值的键跟 `run_physician` 完全一致**，另加两个：
      - `s3_structured`：五步链原件（R34 验证器、R37 单链前端读它）
      - `physician_influences`：哪几家的思路在哪一步起了作用（扁平化好让前端直接渲染）
    `s3` 是 `to_s3_syndrome()` 转出来的 `S3Syndrome`——下游一个调用方都不用改。

    ## 检索：五家各自 top-3，按注册表顺序拼接

    **不是把五家医案混在一起算相似度再取全局 top-3**：那样相似度高的一家会占满
    三个位置，另外四家一条都进不去，而这一轮的全部意义就是五家都参与。
    按注册表顺序拼接还保证了**确定性**——顺序不定 = 缓存前缀 byte 不同 =
    前缀缓存永远不命中（`full_context_hits` 的文档字符串记的是同一条教训）。
    """
    if refs_mode not in ALLOWED_REFS_MODES:
        raise ValueError(f"未知的 refs_mode={refs_mode!r}，目前支持 {sorted(ALLOWED_REFS_MODES)}")

    symptoms_text = "；".join(s1.symptoms)
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"

    roster = physicians_for_synthesis(PHYSICIANS)
    hits: list[tuple[CaseRecord, float]] = []
    low_discrimination = False
    if refs_mode != "none":
        for pid in roster:
            search_pid = pid if refs_mode == "own" else _swap_physician_id(pid)
            got, low = _search_cases(query, search_pid, s2, retriever_mode)
            hits.extend(got)
            # 任一家没有区分度就标上：这个字段的语义是"这次的参考医案里有凑数的"，
            # 五家里有一家凑数也算——按医家分别记的话前端要多一层结构，
            # 而它目前的唯一消费方（E3 报告的比例统计）问的就是"这次有没有"。
            low_discrimination = low_discrimination or low
    s3_schema = S3Structured if hits else S3StructuredUnreferenced

    refs = [_ref_row(case, score) for case, score in hits]
    refs_text = "\n\n".join(_format_case_block(case) for case, _ in hits) or "（无可用参考医案）"

    mode_eff = effective_mode(retriever_mode)
    knowledge_text, knowledge_stats = "", {"available": False, "n_herbs": 0,
                                           "n_formulas": 0, "n_patterns": 0,
                                           "tokens": 0, "trimmed_sections": []}
    knowledge_mode = knowledge_in_prompt(mode_eff)
    if knowledge_mode == "focused":
        knowledge_text, knowledge_stats = build_focused_knowledge(
            s1, s2, hits, list(roster),
            syndromes=[c.syndrome for c, _ in hits if c.syndrome],
        )
    # 知识块插在参考医案之前，跟 legacy 那条路同一个次序与同一个小标题
    # ——`_format_case_block` 的输出与它的相对位置在两条路径上逐字节相同。
    refs_with_knowledge = (
        f"{knowledge_text}\n\n## 参考医案\n\n{refs_text}" if knowledge_text else refs_text
    )
    # full_context 下**不走 assemble()**：那个稳定前缀是按**单个医家**的全量医案
    # 组装的（`assemble(physician, ...)`），五家综合没有"哪一位医家的全量医案"
    # 这个概念。structured + full_context 因此走同一份 s3_structured.yaml，
    # 只是 $refs 里的医案多（五家各自的全量）。**这件事要在 manifest 里看得出来**，
    # 所以 knowledge_mode 照实记（full_context 下它是 "full"，而这条路径没有
    # 前缀缓存可用）——见下面 knowledge 字段里的 prefix_assembled。
    s3_prompt = load_prompt("s3_structured")
    s3_system = render(
        s3_prompt["system"],
        physicians="、".join(info["name"] for info in roster.values()),
        physician_ids=physician_choices_text(),
        elements_summary=_format_elements_summary(s2),
        symptoms=symptoms_text,
        refs=refs_with_knowledge,
    )
    if followup is not None:
        s3_system = s3_system + format_followup_for_s3(followup)

    trace: ReActTrace | None = None
    react_safety_flag: str | None = None
    if use_react:
        # ReAct 取证用哪位医家的身份查医案？**用五家里的第一位是错的**——工具层
        # 的 physician 参数决定它查谁的医案库。这里传 None 让工具层不按医家过滤，
        # 那是"五家一起看"的正确表达。ReAct 那一层本来就允许 physician 为空
        # （`resolve_physician_id(None)` 返回 None，过滤处不加条件）。
        s3_system, trace, react_safety_flag = _run_react_round(
            s3_system, s1, s2, None, physician_name=None, ask_fn=ask_fn,
            bypass_safety=bypass_safety, on_step=on_step,
        )

    if on_step is not None:
        on_step("s3_start", {"physician": SYNTHESIS_PHYSICIAN_ID,
                             "physician_name": SYNTHESIS_PHYSICIAN_NAME})
    emitter = S3DeltaEmitter(on_step, SYNTHESIS_PHYSICIAN_ID, SYNTHESIS_PHYSICIAN_NAME)
    raw, candidates_scored = _best_of_n_s3(s3_system, s3_schema, SYNTHESIS_PHYSICIAN_ID,
                                           on_delta=emitter)
    emitter.flush()
    if on_step is not None:
        on_step("s3_done", {"physician": SYNTHESIS_PHYSICIAN_ID,
                            "physician_name": SYNTHESIS_PHYSICIAN_NAME,
                            **emitter.summary(),
                            "streaming_note": _streaming_note(len(candidates_scored))})
    # R34：符号验证闭环。**这一层取代了 legacy 那条"安全层拦截 → 重开一次"**
    # ——不是两个循环并存：那两条判据（配伍禁忌、超量）现在由验证器的
    # `incompatible_pair` / `dose_exceeds` 两条规则**委托给同一个 safety_output**
    # 去查（见 core/formula_verifier.py 那两条规则的文档字符串）。
    # 两个循环各自决定"要不要重开"的话，同一张方可能被改两遍、llm_calls 不可预测，
    # 而 manifest 里那个数是额度结算与成本比较的依据。
    raw, s3, verify_rounds, revise_calls = _verify_and_revise(
        raw, s3_system, s3_schema, on_step=on_step,
    )
    # `cand.safety` 仍然要填：前端按它挂红/黄标签，M2 那套字段一个没变。
    # 填它跟"要不要重开"是两件事——重开只由验证器决定。
    for cand in s3.formula_candidates:
        cand.safety = assess_formula_safety(s3.syndrome, cand.herb_items)
    selected_safety = s3.formula_candidates[s3.selected].safety
    revised = len(verify_rounds) > 1

    ref_ids = {r["case_id"] for r in refs}
    if trace is not None:
        ref_ids |= set(trace.retrieved_case_ids)
    hallucinated = [cid for cid in s3.cited_case_ids if cid not in ref_ids]
    # 医家影响里引的医案同样要过幻觉检查。schema 的 `_influences_cite_retrieved_cases`
    # 只保证它们在 `cited_case_ids` 里，而 `cited_case_ids` 本身可能是编的
    # ——两道检查管的是不同的事，都要有。
    for inf in raw.physician_influences:
        hallucinated.extend(cid for cid in inf.cited_case_ids if cid not in ref_ids)
    hallucinated = sorted(dict.fromkeys(hallucinated))

    disease_candidates = match_disease(
        s1.symptoms, [h.element for h in s2.elements if h.kind == "location"]
    )
    if s3.disease is not None and get_disease(s3.disease) is None:
        warn = f"病名「{s3.disease}」不在病名参考表（含别名）里，未做规则校验。"
        s3.note = f"{s3.note}；{warn}" if s3.note else warn

    formula_check = check_formula(s3.syndrome, s3.formula_candidates[s3.selected].herb_items)

    return {
        "physician": SYNTHESIS_PHYSICIAN_ID,
        "physician_name": SYNTHESIS_PHYSICIAN_NAME,
        "s2": s2,
        "s3": s3,
        # 五步链原件。**不是 s3 的替代**——`s3` 是下游认识的形状，这个是新增的。
        "s3_structured": raw,
        # 扁平化好让前端直接渲染，不必懂 pydantic 嵌套。
        "physician_influences": [inf.model_dump() for inf in raw.physician_influences],
        # 这次融合真的说出了贡献的医家（id）。**跟名单不是一回事**：名单是五位，
        # 这个可能只有两位——"哪几家真的影响了结论"是要被报出来的数，
        # 不能用"我们接了五家"顶替。
        "physicians_cited": raw.physicians_cited,
        "herbs_grounded_ratio": raw.herbs_grounded_ratio(),
        "n_ontology_refs": len(raw.ontology_refs),
        # R34：符号验证的最终结论 + 三指标。**最后一轮的结果**，不是第一轮——
        # 前端要显示的是"这张方现在的状态"。第一轮的状态在 metrics 里
        # （`verifier_first_pass` / `first_pass_status`），两者都要有：
        # 只报最终态会让"改了三轮才过"和"一次就过"看起来一样。
        "verification": verify_rounds[-1].to_dict(),
        "verifier_metrics": verifier_metrics(verify_rounds, raw),
        "knowledge": {"mode": knowledge_mode, **knowledge_stats,
                      # structured 不走 assemble()，所以没有稳定前缀可缓存。
                      # 如实记一条，别让人看到 mode="full" 就以为缓存命中了。
                      "prefix_assembled": False},
        "streaming": {**emitter.summary(),
                      "note": _streaming_note(len(candidates_scored))},
        "disease_candidates": disease_candidates,
        "refs": refs,
        "refs_mode": refs_mode,
        "no_reference_cases": not hits,
        "low_discrimination": low_discrimination,
        "lora": get_llm().lora_for(SYNTHESIS_PHYSICIAN_ID),
        "hallucinated": hallucinated,
        "safety_flag": react_safety_flag,
        "safety_output": {
            "incompatible": selected_safety.incompatible,
            "thermal_warning": selected_safety.thermal_warning,
            "revised": revised,
        },
        "react_trace": trace,
        "advice": advice_dicts(formula_check),
        "advice_skipped": list(formula_check.skipped),
        "formula_score": formula_check.score,
        "candidates_scored": candidates_scored,
        "best_of_n": len(candidates_scored),
    }


def run_derivation(
    s1: S1Normalize,
    s2: S2Elements,
    followup: FollowupResult | None = None,
    bypass_safety: bool = False,
    on_step: StepFn | None = None,
    refs_mode: str = "own",
) -> dict:
    """R52 第一相：演绎推导，**不检索任何医案**（`S3_MODE=derived`，R52 之后的默认值）。

    跟 `run_synthesis`/`run_physician` 是**并列的第三条路径**，不是它们的分支：
    这条路径的 prompt（s3_derived）、schema（`S3Derived`）从设计上就没有医案
    引用的位置——本函数体内**一次都不调用 `_search_cases`**，不是调用了但没塞进
    prompt。这不是"检索失败退化成没有医案"（那是 `S3StructuredUnreferenced` 的
    场景），是这一相**从不检索**。

    ## 为什么不接 ReAct（`use_react` 不是这个函数的参数）

    `core/react.py` 的工具集里 `search_cases` / `query_case_graph` 直接查医案库
    ——接了 ReAct 等于从工具调用这道后门把医案检索请回来，「看不到任何医案」
    这条约束就名存实亡了。`run_physician`/`run_synthesis` 的 `use_react` 参数
    在这里索性不存在，不是接了但默认关：调用方想给这一相接工具，得先有一套
    只查医理规则、不碰医案库的工具集（不在这一轮范围内）。

    ## refs_mode 为什么还在参数列表里

    单纯为了跟 `run_physician`/`run_synthesis` 保持同一个调用签名，方便
    `consult()` 按 mode 分派时不用为每条路径记一份不同的参数表。这一相没有
    医案检索，这个参数**不产生任何效果**——不是被悄悄忽略，是文档字符串在这里
    明说了它无效，调用方看得到。

    ## 返回值

    跟 `run_synthesis` 同一套键（`results` 的元素结构是既有契约），但没有
    `refs`/`no_reference_cases`/`physician_influences` 这几个案例相关字段的
    真实内容——`refs` 恒为空列表、`no_reference_cases` 恒为 True、
    `physician_influences` 恒为空列表（`hallucinated` 同样恒为空列表：schema
    校验已经把编造的 rule_id 挡在了 `S3Derived` 能被构造出来之前，不会有漏网的）。
    新增三个键（R57 消融实验、R56 前端「本例知识地图」都读这些，不必各自重新
    遍历 `s3_structured` 的嵌套结构）：
      - `theory`：这次进了 prompt 的医理规则统计（`_format_theory_rules` 的第二个返回值）
      - `rule_refs`：全链条引用过的医理规则（扁平化、去重）
      - `insufficient_notes`：哪几步标了"依据不足"
      - `derivation_completeness_ratio`：链上有规则支撑（非 insufficient）的条目占比
    """
    if refs_mode not in ALLOWED_REFS_MODES:
        raise ValueError(f"未知的 refs_mode={refs_mode!r}，目前支持 {sorted(ALLOWED_REFS_MODES)}")

    symptoms_text = "；".join(s1.symptoms)

    theory_text, theory_stats = _format_theory_rules(s2)
    knowledge_text, knowledge_stats = build_focused_knowledge(s1, s2, [], [], syndromes=[])

    s3_prompt = load_prompt("s3_derived")
    s3_system = render(
        s3_prompt["system"],
        elements_summary=_format_elements_summary(s2),
        symptoms=symptoms_text,
        theory_rules=theory_text or "（这次没有查到相关的医理规则，如实在 insufficient 里说明。）",
        knowledge=knowledge_text or "（本体不可用，本草/方剂知识块为空——ontology_refs 留空即可。）",
    )
    if followup is not None:
        s3_system = s3_system + format_followup_for_s3(followup)

    if on_step is not None:
        on_step("s3_start", {"physician": SYNTHESIS_PHYSICIAN_ID,
                             "physician_name": SYNTHESIS_PHYSICIAN_NAME})
    emitter = S3DeltaEmitter(on_step, SYNTHESIS_PHYSICIAN_ID, SYNTHESIS_PHYSICIAN_NAME)
    raw, candidates_scored = _best_of_n_s3(s3_system, S3Derived, SYNTHESIS_PHYSICIAN_ID,
                                           on_delta=emitter)
    emitter.flush()
    if on_step is not None:
        on_step("s3_done", {"physician": SYNTHESIS_PHYSICIAN_ID,
                            "physician_name": SYNTHESIS_PHYSICIAN_NAME,
                            **emitter.summary(),
                            "streaming_note": _streaming_note(len(candidates_scored))})
    # R34（延伸到 R52/R53）：同一套验证闭环，S3Derived 靠字段名跟 S3Structured
    # 保持一致这件事直接免费获得（core/formula_verifier.py 的十一条规则全是
    # 鸭子类型，含 R53 新增的四条医理一致性规则）。
    raw, s3, verify_rounds, revise_calls = _verify_and_revise(
        raw, s3_system, S3Derived, on_step=on_step,
    )
    for cand in s3.formula_candidates:
        cand.safety = assess_formula_safety(s3.syndrome, cand.herb_items)
    selected_safety = s3.formula_candidates[s3.selected].safety
    revised = len(verify_rounds) > 1

    disease_candidates = match_disease(
        s1.symptoms, [h.element for h in s2.elements if h.kind == "location"]
    )
    if s3.disease is not None and get_disease(s3.disease) is None:
        warn = f"病名「{s3.disease}」不在病名参考表（含别名）里，未做规则校验。"
        s3.note = f"{s3.note}；{warn}" if s3.note else warn

    formula_check = check_formula(s3.syndrome, s3.formula_candidates[s3.selected].herb_items)

    return {
        "physician": SYNTHESIS_PHYSICIAN_ID,
        "physician_name": SYNTHESIS_PHYSICIAN_NAME,
        "s2": s2,
        "s3": s3,
        # 五步链原件。**跟 `run_synthesis` 用同一个键**——R37 的单链前端与
        # api/main.py 读的是 organs/syndrome/method/formula/herb_choices 这几个
        # 通用字段名，`S3Derived` 跟 `S3Structured` 字段名相同，键名换了反而要
        # 前端多判一次"这是哪种模式"。
        "s3_structured": raw,
        # 这一相没有案例引用，两个字段恒空——保留键是为了 `results` 的元素结构
        # 跨三条路径一致（前端/eval 收集器按同一套键读）。
        "physician_influences": [],
        "physicians_cited": [],
        "herbs_grounded_ratio": raw.herbs_grounded_ratio(),
        "n_ontology_refs": len(raw.ontology_refs),
        "verification": verify_rounds[-1].to_dict(),
        "verifier_metrics": verifier_metrics(verify_rounds, raw),
        "knowledge": {"mode": "focused" if knowledge_stats.get("available") else "none",
                      **knowledge_stats, "prefix_assembled": False},
        # R52 新增：医理规则层的使用情况，`run_synthesis`/`run_physician` 没有
        # 这个键——那两条路径没有这一层依据。
        "theory": theory_stats,
        "rule_refs": [r.model_dump() for r in raw.rule_refs],
        "insufficient_notes": [n.model_dump() for n in raw.insufficient_notes],
        "derivation_completeness_ratio": raw.derivation_completeness_ratio(),
        "streaming": {**emitter.summary(),
                      "note": _streaming_note(len(candidates_scored))},
        "disease_candidates": disease_candidates,
        # 恒空/恒真：这一相**没有检索**，不是"检索了但没查到"。
        "refs": [],
        "refs_mode": refs_mode,
        "no_reference_cases": True,
        "low_discrimination": False,
        "lora": get_llm().lora_for(SYNTHESIS_PHYSICIAN_ID),
        # schema 校验已经把编造的 rule_id 挡在 S3Derived 能被构造出来之前，
        # 不存在"引了一个不存在的规则却通过了校验"这种情况——恒空列表，
        # 不是没检查。
        "hallucinated": [],
        "safety_flag": None,
        "safety_output": {
            "incompatible": selected_safety.incompatible,
            "thermal_warning": selected_safety.thermal_warning,
            "revised": revised,
        },
        "react_trace": None,
        "advice": advice_dicts(formula_check),
        "advice_skipped": list(formula_check.skipped),
        "formula_score": formula_check.score,
        "candidates_scored": candidates_scored,
        "best_of_n": len(candidates_scored),
    }


def cases_sha256() -> str | None:
    """cases.json 的 sha256 前 12 位，文件不存在时 None。

    抽成函数是因为现在有第三个消费方：manifest（下面）、`offline/estimate_epsilon.py`
    的 epsilon.json，以及 R3 的 fixture 元信息（`core/llm_replay.py`）。同一段
    "这份语料是哪一版"的计算散在三处，改一处（比如换成全长 hash）就会有两处
    对不上——而这个值的全部用途就是跨文件比对。
    """
    cp = Path(__file__).resolve().parent.parent / "cases.json"
    if not cp.exists():
        return None
    return hashlib.sha256(cp.read_bytes()).hexdigest()[:12]


def _comparability_warning(llm, retriever_mode: str | None = None) -> str | None:
    """后端自己的可比性警告 + 思考设置 + 检索模式非默认时的那几句，拼成一条。

    三者是同一类事实——"这次跑的条件跟报告里那些数字的条件不一样"——所以合并成
    一个字段，而不是再加 `thinking_warning` / `retriever_warning` 让引用方记得
    同时看三处。
    """
    parts = [llm.comparability_warning()]
    if s3_thinking() != "enabled":
        parts.append(
            "S3_THINKING=disabled：S3 这一步关掉了思考模式。**关思考跑出来的数字跟"
            "默认配置（S3 开思考 + effort=high）下的不可比**，并列报出，不要相减。")
    mode = effective_mode(retriever_mode)
    if mode != DEFAULT_MODE:
        # R21：检索模式跟换模型同级地改变"模型看到了什么"——full_context 下它看到
        # 该医家全部医案，top3 下看到三条。RESULTS.md 里 top3 那几行是历史行，
        # full_context 另起新行，并列不覆盖。
        parts.append(
            f"{RETRIEVER_MODE_ENV}={mode}（默认是 {DEFAULT_MODE}）：这一轮模型看到的参考"
            "医案跟默认配置不是一回事（top3 系只看三条，full_context 看全量）。"
            "**两系的数不可比**，RESULTS.md 里分行报，不要相减。")
    joined = " ".join(p for p in parts if p)
    return joined or None


def _aggregate_knowledge(results: list[dict] | None) -> dict | None:
    """把各位医家结果里的 `knowledge` 汇成一份给 manifest。

    条目数取**最大值**而不是求和：几位医家的知识块高度重叠（同一批本草条目），
    求和会报出一个比实际放进去的多好几倍的数——而 manifest 里的数字是要被引进
    报告的。token 数同理取最大（"单次调用最多塞了多少"才是成本口径）。
    """
    rows = [r.get("knowledge") for r in (results or []) if isinstance(r.get("knowledge"), dict)]
    if not rows:
        return None
    return {
        "mode": rows[0].get("mode"),
        "available": any(r.get("available") for r in rows),
        "tokens": max((r.get("tokens") or 0) for r in rows),
        "n_herbs": max((r.get("n_herbs") or 0) for r in rows),
        "n_formulas": max((r.get("n_formulas") or 0) for r in rows),
        "n_patterns": max((r.get("n_patterns") or 0) for r in rows),
    }


def _aggregate_streaming(results: list[dict] | None) -> dict | None:
    """把各路 S3 的流式情况汇成一份给 manifest。

    `streamed` 是"**有没有任何一路真的流了**"：一路流了一路没流时，写 True 会
    让人以为整次都是流式的，所以同时报 `n_streamed / n_total`。
    首字延迟取**最大值**——验收要问的是"最慢那一路多久才有字"，取最小值等于
    拿最好看的那个数当结论。`note` 收集所有不同的原因（去重保序），
    一路一个原因合成一句会丢掉信息。
    """
    rows = [r.get("streaming") for r in (results or []) if isinstance(r.get("streaming"), dict)]
    if not rows:
        return None
    streamed = [r for r in rows if (r.get("events") or 0) > 0]
    firsts = [r.get("first_delta_s") for r in streamed if r.get("first_delta_s") is not None]
    notes = list(dict.fromkeys(r.get("note") for r in rows if r.get("note")))
    return {
        "streamed": bool(streamed),
        "n_streamed": len(streamed),
        "n_total": len(rows),
        "events": sum(r.get("events") or 0 for r in rows),
        "chars_content": sum(r.get("chars_content") or 0 for r in rows),
        "chars_reasoning": sum(r.get("chars_reasoning") or 0 for r in rows),
        "first_delta_s_max": max(firsts) if firsts else None,
        "notes": notes,
    }


def _reopen_calls(results: list[dict]) -> int:
    """重开一共花了几次调用。**只此一处实现**——`consult()` 有两处在算 llm_calls
    （安全否决那条早返回路径、正常返回路径），两处各写一遍必然有一处忘了改。

    两种模式的重开次数来源不同：
      - legacy：安全层最多重开**一次**，`safety_output.revised` 是个布尔值，
        计 1 次就是对的；
      - structured（R34）：符号验证闭环最多重开 `MAX_REVISE_ROUNDS` 轮，
        布尔值会把 3 次算成 1 次。

    R34 实测撞到过：加了闭环之后这里仍然按布尔算，manifest 报 4 次而实际花了 6 次
    ——而 manifest 里那个数是额度结算与成本比较的依据，少算不会报错。
    """
    total = 0
    for r in results:
        m = r.get("verifier_metrics")
        if m is not None:
            total += int(m.get("revise_rounds") or 0)
        elif r.get("safety_output", {}).get("revised"):
            total += 1
    return total


def _ontology_manifest() -> dict:
    """本体的规模、缺谓词分布、对医案语料的覆盖率。写进 manifest。

    **为什么要进 manifest 而不是只写在报告里**：引用"符号验证通过"这句话的人
    必须能同时看到"验证依据的那份本体缺了多少"。归经缺 49%、用量缺 55% 的情况下，
    七条规则里有两条在大多数药上判不了——这件事不在同一个地方出现，
    那句话就会被当成"全验过了"。

    本体不可用时只报 `available: False`，不编数。
    """
    from core.formula_verifier import ontology_coverage_of_corpus
    from core.ontology import get_ontology

    ont = get_ontology()
    if not ont.available:
        return {"available": False}
    s = ont.stats()
    return {
        "available": True,
        "n_herbs": s["n_herbs"],
        "n_formulas": s["n_formulas"],
        "n_patterns": s["n_patterns"],
        "missing_predicate_counts": s["missing_predicate_counts"],
        "empty_span_refs": s["empty_span_refs"],
        "corpus_coverage": ontology_coverage_of_corpus(ontology=ont),
    }


def _synthesis_summary(results: list[dict] | None, mode: str) -> dict | None:
    """结构化模式下这一次融合的可核数据。legacy 下返回 **None**。

    None 而不是空字典：「这个模式没跑」和「跑了但一家都没引」是两件事，
    后者是 `physicians_cited: []`，那是一个要被看见的结果（说明"五家综合"
    这次名不副实），前者只是说这次不适用。

    `physicians_available` 与 `physicians_cited` 都要有：前者是"我们接了五家"，
    后者是"这次真的有几家影响了结论"。只报前者就是拿接入数冒充生效数
    ——CLAUDE.md「任何数字都必须带对照」，这里的对照就是分母。
    """
    if mode != "structured" or not results:
        return None
    r = results[0]
    return {
        "physicians_available": len(physicians_for_synthesis(PHYSICIANS)),
        "physicians_cited": r.get("physicians_cited") or [],
        "n_physicians_cited": len(r.get("physicians_cited") or []),
        # R34b：**分母说清楚是哪一层。** 这个比率的分母是**这张方**的药味数，
        # 不是本体总药味数（1232）——两个集合完全不同。数据质量那个比率在
        # `ontology_coverage` 里单独报，见 formula_verifier 那两个函数的文档。
        "herbs_grounded_ratio": r.get("herbs_grounded_ratio"),
        "herbs_grounded_denominator": "本次方的药味数",
        "n_ontology_refs": r.get("n_ontology_refs"),
        "n_herbs": len(r["s3"].herbs),
        "chain_steps": list(S3_CHAIN_STEPS),
        # R34：符号验证的三指标 + 最终状态。
        "verification": r.get("verification"),
        "verifier_metrics": r.get("verifier_metrics"),
    }


def _build_manifest(elapsed_ms: int, llm_calls: int, use_react: bool = False,
                    retriever_mode: str | None = None,
                    knowledge: dict | None = None,
                    s3_mode_used: str | None = None,
                    synthesis: dict | None = None,
                    streaming: dict | None = None) -> dict:
    """跑这一次用的是什么模型、什么 prompt 版本、几次调用。
    竞赛材料里写"我们的结果"时，这几行元数据就是全部的可信度来源。"""
    cases_sha = cases_sha256()

    # model 从后端问，不从 LLM_MODEL 环境变量读：claude_cli 后端下那个变量
    # 还是 deepseek-chat，照抄就等于把 Claude 跑的结果标成 DeepSeek 跑的。
    llm = get_llm()
    return {
        "model": llm.model_name(),
        "backend": llm.backend_id(),
        # 非默认后端时非 None。带着走，报告里就不会漏标"这个数不可比"。
        # **思考设置也算一种"换了实验条件"**：S3 关掉思考会明显更快、结果也会变，
        # 那是另一组数，不能跟默认配置下的数混着引（跟换模型同级）。
        "comparability_warning": _comparability_warning(llm, retriever_mode),
        # 每一步开不开思考。换了这张表 = 数字不可比，所以它跟 model 一样是 manifest
        # 的一等字段，不是可选的调试信息。
        "thinking_by_step": thinking_by_step(),
        # **不写成一个标量**：思考模式下 temperature 不生效，而各步的思考设置不同，
        # 所以"这次跑的 temperature 是多少"本来就没有单一答案。写一个标量就得挑一步
        # 来代表全体，那是在报告里埋一句不准确的话。
        "temperature_effective": {
            step: (None if mode == "enabled" else 0.0)
            for step, mode in thinking_by_step().items()
        },
        # 这次问诊里发生了几次重试、分别是什么原因（429 / 超时 / 其它）。
        # None = 这条路径没开统计（离线脚本直接调 run_physician 之类）。
        "retries": current_retry_stats(),
        # 本地后端 + 配了 LORA_DIR 时是那个目录，否则 None（= 这一轮跑的是
        # 基座模型 / 根本没有 adapter 这回事）。manifest 这一层记"adapter 是
        # 从哪来的"，具体哪位医家实际挂了哪个 adapter 记在各自的结果里
        # （run_physician 的 "lora" 字段）——adapter 是按医家切的，整次问诊
        # 一个值说不清楚。
        "lora_dir": llm.lora_dir(),
        # None = 实时调用；非 None = 这一次是回放录制好的推理（LLM_MODE=replay）。
        # **不许伪装成实时调用**：前端那行"演示模式"小字就从这里取，
        # comparability_warning 也会跟着说明"非实时调用"。
        "replayed_from": llm.replay_info(),
        "prompt_version": "v1",
        "use_react": use_react,
        "cases_sha256": cases_sha,
        "elapsed_ms": elapsed_ms,
        "llm_calls": llm_calls,
        # R21：这四项是"知识怎么进模型"的全部凭据。
        #
        # retriever_mode 跟 model 同级地影响可比性：full_context 下模型看到的是
        # 该医家**全部**医案，top3 下是三条——两组数不可比，所以
        # _comparability_warning 会在它不是默认值时也开口（见那个函数）。
        "retriever_mode": effective_mode(retriever_mode),
        # 每位医家的稳定前缀各段多少 token。full_context 之外恒 None——
        # 不是 0：0 会被读成"算过、是零"，而那几段在 top3 下根本不存在。
        "prefix_tokens_by_section": _prefix_tokens_or_none(retriever_mode),
        # 缓存命中读数，从响应 usage 取（core/llm.py::record_usage）。
        # 非 DeepSeek 后端如实为 None。
        **_cache_usage_fields(),
        # R22：S3 这一步想多久、采几次。两项都直接决定钱和耗时，也都让数字
        # 不可比（effort 从 high 换到 max、N 从 1 换到 3 都是换实验条件），
        # 所以跟 thinking_by_step 一样是 manifest 的一等字段。
        # effort 只在 S3 开着思考时有值——关了思考它不生效，记一个不生效的
        # 实验条件比不记更糟。
        "reasoning_effort": (s3_reasoning_effort() if s3_thinking() == "enabled" else None),
        "best_of_n": s3_best_of_n(),
        # R33：S3 这一步产出的形状。**这一项决定 results 有几个元素**，
        # 引用任何"每位医家……"的数字之前必须先看它——structured 下只有一份结论，
        # 分歧度、ε、三列集注那些数在这个模式下压根不存在（不是 0，是不适用）。
        "s3_mode": s3_mode_used or s3_mode(),
        # 五家融合这一次实际怎么样。structured 之外恒为 None（不是空字典）——
        # "这个模式没跑"和"跑了但一家都没引"是两件事。
        "synthesis": synthesis,
        # R32：知识块怎么进的提示词。**这三项是"让模型明白药理"这件事的凭据**
        # ——在此之前知识速查表只在 full_context 下进提示词，而演示跑的是
        # hybrid，"懂药理"在运行配置下从未发生过且所有测试全绿。
        "knowledge_in_prompt": (knowledge or {}).get("mode")
        or knowledge_in_prompt(effective_mode(retriever_mode)),
        # 这一次实际放进去多少 token。0 且 mode 不是 off = 本体层数据不在
        # （药理层 jsonl 未生成），**跟"放了 0 个 token"是两件事**，
        # 所以 entries 里还带一个 available。
        "knowledge_tokens": (knowledge or {}).get("tokens", 0),
        "knowledge_entries": {
            "available": (knowledge or {}).get("available", False),
            "herbs": (knowledge or {}).get("n_herbs", 0),
            "formulas": (knowledge or {}).get("n_formulas", 0),
            "patterns": (knowledge or {}).get("n_patterns", 0),
        },
        # R34b：本体这份数据本身的质量。**跟 `synthesis.herbs_grounded_ratio`
        # 是两个不同的分母**，放在两处、各自注明，就是为了它们不会被混用：
        #   这里：分母 = 医案语料里出现过的药名种数（数据指标）
        #   那里：分母 = 本次方的药味数（模型指标）
        # 缺谓词计数一起记：归经缺一半的本体上，"归经覆盖规则通过了"这句话
        # 要能被读者自己打折扣。
        "ontology": _ontology_manifest(),
        # R36：这次 S3 有没有真的边生成边吐、首字多久、发了多少帧。
        # None = 这条路径没跑到 S3（被拦截 / 信息不足）——**跟"跑了但没流式"
        # 是两件事**，后者是 `{"streamed": false, "notes": [原因]}`。
        # 首字延迟是 R36 的验收项之一（≤3 秒），没有这个字段就只能掐表。
        "streaming": streaming,
        # 这次 S1/S2 是合成一次调用还是分两次（R36）。它直接改 llm_calls，
        # 不记的话两份 manifest 放一起比时，调用数差 1 看不出是配置还是代码。
        "s1s2_merged": s1s2_merged(),
    }


def _prefix_tokens_or_none(retriever_mode: str | None) -> dict | None:
    """full_context 下报各段 token，其余模式 None。

    取不到（没有 cases.json / 药理层文件）时报出来的是各段**现有内容**的读数
    ——沙盒里医案段会是 0 诊次那一行。不抛异常：manifest 不该因为一个统计项
    取不到就让整次问诊失败。

    **按 cases.json 的 sha 记忆化**：算这个数要把全部医案格式化一遍再数 token，
    941 诊次量级下每次问诊都算一遍是白花几百毫秒，而它在语料不变时恒定。
    sha 变了（重抽了语料）缓存自然失效——这正是它该失效的时机。
    """
    if effective_mode(retriever_mode) != "full_context":
        return None
    return _prefix_tokens_cached(_PREFIX_REPORT_PHYSICIAN, cases_sha256())


@lru_cache(maxsize=8)
def _prefix_tokens_cached(pid: str, cases_sha: str | None) -> dict:
    """`cases_sha` 只用来做缓存键，函数体不读它——它代表"语料这一版"。"""
    try:
        return prefix_tokens_by_section(pid)
    except Exception as e:  # noqa: BLE001 —— 统计项不许拖垮问诊
        return {"error": f"{type(e).__name__}: {e}"}


#: manifest 里那份 prefix_tokens_by_section 按哪位医家报。**一位就够**：
#: 共享段对所有医家相同，医家段的量级三位接近；报三份会让 manifest 膨胀，
#: 而要逐位看有 `python -m core.context_prefix --report`。
_PREFIX_REPORT_PHYSICIAN = "ye_tianshi"


def _cache_usage_fields() -> dict:
    """前缀缓存命中读数。命中率是**算出来的**，不从响应里读——
    响应只给 hit/miss 两个绝对数，率写在两处就会有一处忘了改分母。
    """
    # **不经后端拿**：统计在 ContextVar 里，是"这一次调用链"的属性，不是后端
    # 实例的属性（并发的三位医家共用同一个后端单例）。给后端加一个转发方法
    # 等于给同一件事开第二个入口（CLAUDE.md 第 31 条），而且会让所有测试替身
    # 都得跟着实现那个方法。
    usage = current_usage_stats()
    hit = usage.get("prompt_cache_hit_tokens") if usage else None
    miss = usage.get("prompt_cache_miss_tokens") if usage else None
    total = (hit or 0) + (miss or 0)
    return {
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        # 分母为 0（没有任何 usage）时是 None 而不是 0.0：0.0 会被读成
        # "跑了但一次没命中"，而实际是"这个后端不报这个数"。
        "cache_hit_ratio": (round((hit or 0) / total, 4) if total else None),
        "reasoning_tokens": (usage.get("reasoning_tokens") if usage else None),
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


class _PhysicianCancelled(Exception):
    """某位医家的线程因为别人触发了安全否决而被取消。不是错误，不往上报。"""


def _serialize_ask(ask_fn: AskFn | None) -> AskFn | None:
    """把追问渠道串行化。**并发之后这是必须的**：`ask_fn` 背后是一个人（或患者
    模拟器），同一时刻只可能回答一个问题；`_ConsultStream.ask` 的 `_pending` 也是
    "每个问题一条队列、同一时刻只有一条"的形状——两位医家同时提问，后一位会把前一位
    的队列覆盖掉，前一位于是永远等不到答案、直到 300 秒超时。

    代价是三位医家的追问变成排队（最坏 3×超时），跟并发之前的总时长一样——**追问
    本来就不是能并行的事**，这里并发省的是 LLM 调用的等待，不是人的思考时间。
    """
    if ask_fn is None:
        return None
    lock = threading.Lock()

    def ask(question: str) -> str | None:
        with lock:
            return ask_fn(question)

    return ask


def _run_physicians_into(
    results: list[dict], s1: S1Normalize, s2: S2Elements, *,
    use_react: bool, followup, ask_fn: AskFn | None, bypass: bool,
    on_step: StepFn | None, retriever_mode: str | None, refs_mode: str,
    emit, mode: str,
) -> None:
    """跑 S3 并把结果追加进 `results`。**`mode` 决定跑哪一条路径。**

    `structured`（R33 起的默认）：一次 `run_synthesis`，五家融合成**一份**结论，
    `results` 恰好一个元素。`legacy`：下面那条原路——三位医家并发跑
    `run_physician`，结果按注册表顺序追加。

    分派放在这一个函数里而不是 `consult()` 里：`consult()` 外面包着 SafetyVeto 与
    RetrievalUnavailable 两层处理，那两层对两种模式**完全一样**（被拦截的请求不产出
    任何方药、检索不可用时什么都不给），拆到上面去就得写两遍。

    ---- 以下是 legacy 那条路的四条约束，一条没变 ----

    三位医家**并发**跑 S3，结果按 `PHYSICIANS` 的插入顺序追加进 `results`。

    改并发的理由：一次问诊 6 次调用里有 3 次是各位医家的 S3，串行时它们是
    3×单次耗时（真机实测约 198 秒 / 394.6 秒总耗时的一半）。三位医家之间没有任何
    数据依赖——S1/S2 是全局跑一次、共用的（CLAUDE.md 明令 S1 只能跑一次），
    每位医家各自检索、各自开方。

    四条约束，每一条都有对应的测试：

    **一、`results` 的顺序仍按注册表。** 这是契约：前端三列按它排、分歧度两两配对
    按它取。并发下完成顺序是乱的，所以这里按 `PHYSICIANS` 的顺序去取 future 的结果，
    不用 `as_completed` 的顺序。

    **二、事件会交错，`results` 不会。** `physician_start`/`physician_done` 由各自的
    线程发，叶天士的 done 完全可能排在张锡纯的 start 后面。前端必须按事件里的
    `physician` 字段路由（docs/DESIGN.md §4.7 的订正）。"结果有序"和"事件有序"是
    两件事。

    **三、安全否决要取消其余线程。** ReAct 追问问出危重症状时，被拦截的请求不产出
    任何方药——已经跑完的医家结果也不返回（这是改并发之前就有的语义，不许变松）。
    取消靠一个 `threading.Event` + 包在 `on_step` 外面的一层检查：别的线程在下一次
    发事件时抛 `_PhysicianCancelled` 退出。**不用 `future.cancel()`**——那只能取消
    还没开始跑的任务，而三位医家是同时开跑的，一个都取消不掉。

    **四、ContextVar 必须传进 worker。** `use_llm()` 的逐请求后端覆盖走的是
    ContextVar（BYOK、降级到回放都靠它），而 ContextVar **不会**自动跟着
    `ThreadPoolExecutor` 的线程走——不显式 `copy_context().run` 的话，worker 里
    `get_llm()` 拿到的是进程单例，BYOK 静默失效、访问者的 key 没被用上、额度照扣。
    每个 worker 一份独立的拷贝：一个 `Context` 只能被 `run` 一次。
    """
    if mode == "derived":
        # R52：演绎推导，全程不检索任何医案。跟 structured 分支共用同一个保留
        # 身份（SYNTHESIS_PHYSICIAN_ID/NAME）——对外都是"这次问诊的一份结论，
        # 不挂在某位医家名下"，两条路径的区别在**内部怎么产出**（演绎推导 vs
        # 五家医案融合），不在"这份结论叫什么"。
        if use_react:
            # `run_derivation` 不接 use_react（见它的文档字符串：ReAct 的工具集
            # 里有 search_cases/query_case_graph，接了等于从工具调用这道后门把
            # 医案检索请回来）。这里提前抛，不等到 run_derivation 内部才发现——
            # 跟 retriever_mode/refs_mode 不认识时立刻抛是同一条纪律。
            raise ValueError(
                "S3_MODE=derived 不支持 use_react=True——ReAct 的工具集会重新引入"
                "医案检索，这一相的设计就是不检索任何医案。"
            )
        if on_step is not None:
            on_step("physician_start", {"physician": SYNTHESIS_PHYSICIAN_ID,
                                        "physician_name": SYNTHESIS_PHYSICIAN_NAME})
        r = run_derivation(
            s1, s2, followup=followup, bypass_safety=bypass, on_step=on_step,
            refs_mode=refs_mode,
        )
        if on_step is not None:
            on_step("physician_done", {
                "physician": SYNTHESIS_PHYSICIAN_ID,
                "physician_name": SYNTHESIS_PHYSICIAN_NAME,
                "syndrome": r["s3"].syndrome, "herbs": r["s3"].herbs,
            })
        results.append(r)
        return

    if mode == "structured":
        # 五家融合成一份。事件仍然发 physician_start / physician_done，`physician`
        # 字段是保留 id `synthesis`——前端按这个字段路由（DESIGN §4.7），
        # 换成别的事件名等于让 R37 之前的界面收不到任何进度。
        if on_step is not None:
            on_step("physician_start", {"physician": SYNTHESIS_PHYSICIAN_ID,
                                        "physician_name": SYNTHESIS_PHYSICIAN_NAME})
        r = run_synthesis(
            s1, s2, use_react=use_react, followup=followup, ask_fn=ask_fn,
            bypass_safety=bypass, on_step=on_step,
            retriever_mode=retriever_mode, refs_mode=refs_mode,
        )
        if on_step is not None:
            on_step("physician_done", {
                "physician": SYNTHESIS_PHYSICIAN_ID,
                "physician_name": SYNTHESIS_PHYSICIAN_NAME,
                "syndrome": r["s3"].syndrome, "herbs": r["s3"].herbs,
            })
        results.append(r)
        return

    # R18：只遍历参与集注的那几位。李可/王云启 enabled=False——他们的语料进
    # 检索、进训练、进「参考医家」区，但**不占列**：塞进三列会同时坏掉版面
    # 和对照设计（学派维度被稀释成「每人一个学派」）。
    physicians = list(physicians_enabled(PHYSICIANS).items())
    cancel = threading.Event()
    ask = _serialize_ask(ask_fn)

    def worker_on_step(name: str, data: dict) -> None:
        if cancel.is_set():
            raise _PhysicianCancelled()
        if on_step is not None:
            on_step(name, data)

    def work(physician: str, info: dict) -> dict:
        if cancel.is_set():
            raise _PhysicianCancelled()
        worker_on_step("physician_start",
                       {"physician": physician, "physician_name": info["name"]})
        r = run_physician(
            s1, s2, physician, info["name"], use_react=use_react,
            followup=followup, ask_fn=ask, bypass_safety=bypass,
            on_step=worker_on_step if on_step is not None else None,
            retriever_mode=retriever_mode, refs_mode=refs_mode,
        )
        worker_on_step("physician_done", {
            "physician": physician, "physician_name": info["name"],
            "syndrome": r["s3"].syndrome, "herbs": r["s3"].herbs,
        })
        return r

    futures: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=len(physicians),
                            thread_name_prefix="physician") as pool:
        for physician, info in physicians:
            # 每个 worker 一份独立的 Context 拷贝（见上面第四条）
            futures[physician] = pool.submit(
                contextvars.copy_context().run, work, physician, info)
        # 先用完成顺序扫一遍，只为**尽早**置位取消：否决发生得越早，别人白花的
        # 调用越少。真正的结果收集在下面按注册表顺序做。
        for fut in as_completed(futures.values()):
            if isinstance(fut.exception(), SafetyVeto):
                cancel.set()

    veto: SafetyVeto | None = None
    failure: BaseException | None = None
    for physician, _info in physicians:
        exc = futures[physician].exception()
        if exc is None:
            results.append(futures[physician].result())
        elif isinstance(exc, SafetyVeto):
            veto = veto or exc
        elif isinstance(exc, _PhysicianCancelled):
            continue
        else:
            # 一位医家的真实失败（LLMError 之类）不该被别人的成功盖掉，也不该
            # 被吞掉：按注册表顺序取第一个，跟串行时"第一个失败的抛出来"一致。
            failure = failure or exc
    if veto is not None:
        raise veto
    if failure is not None:
        raise failure


def consult(
    complaint: str,
    use_react: bool | None = None,
    ask_fn: AskFn | None = None,
    eval_mode: bool | None = None,
    on_step: StepFn | None = None,
    retriever_mode: str | None = None,
    refs_mode: str = "own",
    s3_mode_override: str | None = None,
    patient_profile=None,
) -> dict:
    """use_react=None 时读环境变量 USE_REACT（默认关）。显式传布尔值优先，
    测试和 A/B 脚本靠它固定条件，不受环境影响。

    refs_mode 是**逐请求**的参考医案取用方式（own/swapped/none，见
    run_physician 上面 ALLOWED_REFS_MODES 那段注释），只在调用栈里传、
    不接环境变量——跟 retriever_mode 是同一条理由。默认 "own"，行为跟改造前
    逐字节一致。E3/E4 消融（eval/run_eval.py）用它来检验参考医案对结论的
    真实影响。

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
    # 这一次问诊的重试统计。ContextVar 里放一个可变字典，并发的医家线程（走
    # copy_context）加的数父线程读得到；manifest 从 current_retry_stats() 取，
    # 不用把它一路当参数传到每个 _build_manifest 调用点。
    new_retry_stats()
    # R21：缓存命中统计跟重试统计同一处开——两者都是「这一次问诊的」，
    # 开在两个地方就会有一处忘了开，而忘了开的表现是 manifest 里那个数恒 None。
    new_usage_stats()

    def emit(name: str, **data) -> None:
        if on_step is not None:
            on_step(name, data)

    if retriever_mode is not None and retriever_mode not in ALLOWED_MODES:
        # 模式名的合法集合只有 core/retrieval_hybrid.py 那一份，这里 import 常量
        # 复用，不另抄一份字符串列表——抄一份的话加新模式时必然漏改一处。
        raise ValueError(
            f"未知的 retriever_mode={retriever_mode!r}，目前支持 {sorted(ALLOWED_MODES)}"
        )
    if refs_mode not in ALLOWED_REFS_MODES:
        # 同样立刻抛，不等到第一位医家开始检索才失败——run_physician 里也会查
        # 一次（它可以被单独调用，不能假设调用方永远经过 consult 这道校验），
        # 这里提前查是为了避免 S1/S2 两次调用白花。
        raise ValueError(
            f"未知的 refs_mode={refs_mode!r}，目前支持 {sorted(ALLOWED_REFS_MODES)}"
        )

    if use_react is None:
        use_react = react_enabled()
    # R33：S3 这一步产出哪种形状。跟 use_react / bypass 同一条纪律——
    # **一次 consult 里只判一次**，之后一路把这个值传下去，不让下游各自再读一次
    # 环境变量：进程级变量会让两个并发请求互相污染（一个请求的 S3 一半按
    # structured 跑、一半按 legacy 跑，而两种形状的 results 长度不同）。
    mode = s3_mode_override if s3_mode_override is not None else s3_mode()
    if mode not in S3_MODES:
        # 跟 retriever_mode / refs_mode 一样立刻抛，不等到 S3 那一步才失败
        # ——那时 S1/S2 两次调用已经白花了。
        raise ValueError(f"未知的 s3_mode={mode!r}，目前支持 {sorted(S3_MODES)}")
    # 一次 consult 里只判一次，之后一路用这个布尔值：中途有人改环境变量时，
    # 同一个请求的四个中止点也不会一半拦一半不拦。
    bypass = safety_bypassed(eval_mode)
    # demo 模式下这次请求会被拦截的原因（最早触发的那个）。EVAL_MODE 打开时
    # 链路继续往下走，但这个字段仍然如实记着"本来会被拦"，两种模式同一套语义。
    safety_flag: str | None = None
    # R44：这一次代理做过的决策（停/问/取证/验）。规则表在 core/agent.py，
    # 这里只记录——判断仍然在各自的模块里。
    trace = AgentTrace()

    def _stopped(decision, **state) -> dict:
        """**一份**"停下来"的返回值。

        R44 之前这个 dict 在 `consult()` 里有四份拷贝（危重主诉 / 追问命中 /
        追问确认命中 / 证素为空），而且已经开始漂——其中一份带 `"coverage": None`、
        另一份没有，键的顺序也各不相同。前端按同一份契约读，缺一个键就是 KeyError。

        键集跟正常路径保持一致；`state` 里给什么就覆盖什么（被拦时 s2/residual
        有没有值随触发点而定）。
        """
        base = {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": decision.stop_kind == "safety",
            "reject_reason": decision.detail,
            "safety_flag": safety_flag,
            "retrieval_error": None,
            "s2": None,
            "residual": None,
            "followup": None,
            "insufficient": decision.stop_kind == "evidence",
            "insufficient_reason": None,
            "coverage": None,
            "agent_trace": trace.to_list(),
            # R46：**中止的分支也要有这两个键。** 七个返回点守着同一套键这条
            # 纪律不是形式——缺键在前端读到的是 undefined，会悄悄进渲染。
            # 被拦下来的这一次没有方可比、也没有人维可核，所以是 None
            # （"没有可算的"），不是 `{}`（"算了，结果是空的"）。
            "individualization": None,
            "guideline": None,
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000), state.pop("_calls", 1), use_react,
                retriever_mode=retriever_mode, s3_mode_used=mode),
        }
        base.update(state)
        return base
    # R36：S1+S2 合不合**一次 consult 只判一次**（同 use_react / bypass / mode 那条
    # 纪律）：中途有人改环境变量时，同一个请求的调用数结算和实际发生的次数不会错位。
    merged_s1s2 = s1s2_merged()
    if merged_s1s2:
        s1, s2_pending = normalize_and_infer_merged(complaint)
    else:
        s1, s2_pending = normalize(complaint), None
    # 这一段实际花了几次调用。**不问 `core.usage.fixed_steps_per_consult()`**——
    # 那个函数会再读一次环境变量，而本次请求的判断已经落在 merged_s1s2 上了；
    # 两处各读一次就可能一处 1 一处 2，账本跟实际发生的次数错位。
    s1s2_calls = 1 if merged_s1s2 else 2
    emit("s1_done", symptoms=s1.symptoms, tongue=s1.tongue, pulse=s1.pulse, unmapped=s1.unmapped)

    # 安全否决必须在这里、S2 开始之前——命中就直接返回，S2/S3 一次都不调用，
    # 不产出任何方药。不要把这道检查挪到 run_physician 内部或结果的 note 字段。
    # 三处都要查：S1 可能把"最近吐了两次血"这类病史陈述归进 unmapped
    # （s1_normalize.yaml 明确要求含糊的病史表述放 unmapped），只查 symptoms 会漏。
    reject_reason = check_safety([complaint] + s1.symptoms + s1.unmapped)
    safety_flag = safety_flag or reject_reason
    stop = decide("danger_in_complaint", reject_reason, bypass=bypass)
    if stop is not None:
        trace.decisions.append(stop)
        # **合一模式下 `s2_pending` 里已经有证素了，这里把它丢掉、照旧返回
        # `s2: None`。** 对外可见的行为跟分两次那条路逐字段一致（被拦截的请求
        # 不产出证素、不产出方药）。这也正是 `normalize_and_infer_merged` 默认
        # 关着的理由：丢掉是流程约定，没算过才是结构保证。
        return _stopped(stop, _calls=1)

    # 合一模式下这一步不再调模型（证素跟症状是同一次调用的产出）。
    s2 = s2_pending if s2_pending is not None else infer_elements(s1)
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
    # 中文名跟事件一起发：进度日志是给人读的，`max_rounds` 这种 id 印在那里
    # 跟印在结论里一样不可读（`stop_label` 是停因的唯一一张表）。
    emit("followup_done", stopped_by=followup.stopped_by,
         stopped_by_label=stop_label(followup.stopped_by), rounds=followup.rounds,
         asserted=followup.asserted, denied=followup.denied)
    # R44：**问过就记一笔**（问了 0 轮不记——那时这个能力没上场）。
    if followup.rounds:
        trace.record("ask_for_missing_symptoms",
                     f"问了 {followup.rounds} 轮，确认 {len(followup.asserted)} 条、"
                     f"否认 {len(followup.denied)} 条；停因：{stop_label(followup.stopped_by)}")
    extra_calls = 0
    if followup.stopped_by == "safety":
        # 追问问出危重症状 = 跟初始主诉命中同一道否决，同样不产出任何方药。
        # CLAUDE.md：追问是安全否决层的后门，这里堵上。
        safety_flag = safety_flag or followup.reject_reason
    stop = decide("danger_in_followup_answer",
                  followup.reject_reason if followup.stopped_by == "safety" else None,
                  bypass=bypass)
    if stop is not None:
        trace.decisions.append(stop)
        return _stopped(stop, s2=s2, followup=followup, _calls=s1s2_calls)
    if followup.asserted:
        # 双保险：run_followup 已经把危重症状挡在 asserted 之外，这里再查一次是防
        # 将来有人改了 followup 的判据却没意识到这条症状会一路进 S2/S3。
        reject = check_safety(followup.asserted)
        safety_flag = safety_flag or reject
        stop = decide("danger_in_asserted", reject, bypass=bypass)
        if stop is not None:
            trace.decisions.append(stop)
            return _stopped(stop, s2=s2, followup=followup, _calls=s1s2_calls)
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

    stop = decide("no_elements", not s2.elements and not (residual and residual["s2"].elements))
    if stop is not None:
        trace.decisions.append(stop)
        return _stopped(
            stop, s2=s2, residual=residual, followup=followup,
            insufficient_reason=(
                "现有症状不足以推断证素，无法进行有依据的辨证。"
                "请补充更多信息：起病与加重缓解的诱因、疼痛或不适的性质与部位、"
                "饮食与二便情况、寒热喜恶、舌象与脉象。"
            ),
            coverage=round(coverage, 3),
            _calls=s1s2_calls + extra_calls + (1 if residual else 0),
        )
    results = []
    try:
        _run_physicians_into(
            results, s1, s2, use_react=use_react, followup=followup, ask_fn=ask_fn,
            bypass=bypass, on_step=on_step, retriever_mode=retriever_mode,
            refs_mode=refs_mode, emit=emit, mode=mode,
        )
    except SafetyVeto as veto:
        # ReAct 追问问出了危重症状：跟初始主诉命中同一道否决，已经跑完的医家结果
        # 也不返回——被拦截的请求不产出任何方药。
        calls = (
            s1s2_calls + extra_calls + (1 if residual else 0) + veto.llm_calls
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"])
            # R22：**不是 len(results)**。每位医家的 S3 采了 N 次（best-of-N），
            # 按医家数计等于漏掉 N−1 次真实调用——manifest 的调用数是额度结算的
            # 依据，漏算的表现是账本持续少扣，而少扣不会报错。
            # 用每位医家自己报的 N（`_best_of_n_s3` 返回几条就是采了几次），
            # 不是全局 s3_best_of_n()：中途改环境变量、或某位医家采样部分失败时，
            # 全局那个数跟实际发生的次数会不一致。
            + sum(len(r["candidates_scored"]) for r in results)
            + _reopen_calls(results)
        )
        safety_flag = safety_flag or veto.reason
        stop = trace.record("danger_in_react_answer", veto.reason)
        return _stopped(
            stop, s2=s2, followup=followup, residual=residual,
            reject_reason=veto.reason, safety_flag=safety_flag,
            manifest=_build_manifest(int((time.time() - _t0) * 1000), calls, use_react,
                                     retriever_mode=retriever_mode, s3_mode_used=mode,
                                     knowledge=_aggregate_knowledge(results),
                                     streaming=_aggregate_streaming(results)),
        )
    except SymbolicVeto as veto:
        # R34：符号验证的 veto 级违规改了 MAX_REVISE_ROUNDS 轮还在——这张方不下发。
        # **跟 SafetyVeto 分两个分支而不是合成一个**：两者该对用户说的话不同
        # （见 SymbolicVeto 的文档字符串），而合并之后响应里只能说一句
        # "被拦截了"，患者分不出是"你该立刻就医"还是"系统改不出合规的方"。
        calls = (
            s1s2_calls + extra_calls + (1 if residual else 0)
            + sum(len(r["candidates_scored"]) for r in results)
            + veto.llm_calls
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"])
        )
        stop = trace.record("symbolic_veto", veto.reason)
        return _stopped(
            stop, s2=s2, followup=followup, residual=residual,
            # **`rejected` 仍然是 True**：方不下发这件事对调用方来说跟安全拦截
            # 一样是"这次没有方"。区别在 `safety_flag`（留给安全层，不复用——
            # 它的语义是"危重症状"，而这里的原因是"方不合规"）与
            # `verification_veto`（只有这一条路径有），前端按这两个字段走
            # 不同的提示文案。
            rejected=True,
            reject_reason=veto.reason,
            safety_flag=safety_flag,
            verification_veto=[
                {"rule": v.rule, "herbs": list(v.herbs), "reason": v.reason,
                 "counterexample": v.counterexample} for v in veto.violations
            ],
            manifest=_build_manifest(int((time.time() - _t0) * 1000), calls, use_react,
                                     retriever_mode=retriever_mode, s3_mode_used=mode,
                                     knowledge=_aggregate_knowledge(results),
                                     streaming=_aggregate_streaming(results)),
        )
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
            # 键集跟其余返回点一致（这一条不是"代理的决策"，是环境缺数据，
            # 所以 agent_trace 里照实是这一次已经发生过的那些决策，可能为空）。
            "agent_trace": trace.to_list(),
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
            # R46：同上——检索跑不起来时没有结论可比对。
            "individualization": None,
            "guideline": None,
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000),
                s1s2_calls + extra_calls + (1 if residual else 0), use_react,
                retriever_mode=retriever_mode, s3_mode_used=mode,
            ),
        }

    syndromes = {r["physician"]: r["s3"].syndrome for r in results}
    values = list(syndromes.values())
    same = len(set(values)) <= 1
    pairwise = pairwise_divergence(results)

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

    # R1 分层：君臣（核心判断）和佐使（加减）各算一个 Jaccard，跟上面那个
    # herb_jaccard 并列。**上面那段一个字没动**：E3/E4/E9 的历史数字都基于
    # herb_jaccard，改了就不可比（tests/test_role_layers.py 里有一条测试钉住
    # "加不加 role 标注，herb_jaccard 都是同一个数"）。分层是新增的两个数，
    # 回答的是 herb_jaccard 回答不了的问题：0.53 这个数里有多少是核心判断
    # 不一致、有多少只是佐使加减不同。
    role_parts = [
        _herbs.role_partitioned_herb_sets(r["s3"].selected_herb_items)
        for r in results
    ]
    core_sets = [p["core"] for p in role_parts]
    adjunct_sets = [p["adjunct"] for p in role_parts]
    core_jaccard = _layered_jaccard(core_sets)
    adjunct_jaccard = _layered_jaccard(adjunct_sets)

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
        # 1.3（E2）：主指标是两两配对的药物 Jaccard 距离（pairs），不再是过时的
        # "exact_string_match"（那是第一版按证型名字符串比对的写法，早就换成
        # 药物集合了，字段一直没跟着改）。herb_jaccard 保留：它是"三家共用"的
        # n 方交并比，三位医家时只有三家都用的药才算共同，天然偏向 1.0，
        # 分不清师承内（叶×吴）和跨学派（叶×张、吴×张）——pairs 才分得清。
        #
        # method 的值要如实描述这两个字段各自的算法，不能只报一半：曾经写成
        # "pairwise_herb_jaccard"，但那只是 pairs 的算法——herb_jaccard 本身
        # 用的是上面几行的 set.intersection(*herb_sets)/set.union(*herb_sets)，
        # 是 n 方交并比，不是两两配对。读这个 dict 的人只看 method 字段会以为
        # herb_jaccard 也是两两算的，被这个标签本身带偏——不能改 herb_jaccard
        # 的算法本身（E3/E4/E9 的历史数字都基于它，改了就不可比），只改
        # 这个描述性标签，让它跟两个字段的真实算法对得上。
        "method": "nway_jaccard+pairwise",
        # 0=用药完全一致，1=毫无重叠（n 方交并比，见上）
        "herb_jaccard": round(herb_jaccard, 3) if herb_jaccard is not None else None,
        "shared_herbs": shared_herbs,
        # R14 对照带要画"共用 ● + 各家独有 ●"。**独有集合在后端算**：判定两味药
        # 是不是同一味走 core/herbs.py::normalized_herb_set（"炙甘草三钱"和"甘草"
        # 是同一味），前端拿 s3.herbs 自己做集合差就是第二套匹配实现，
        # CLAUDE.md 第 31 条撞过三次的正是这件事。
        "unique_herbs": {
            r["physician"]: sorted(hs - set.union(*(other for j, other in enumerate(herb_sets) if j != i)))
            if len(herb_sets) >= 2 else sorted(hs)
            for i, (r, hs) in enumerate(zip(results, herb_sets))
        },
        # R1 分层：君臣 = 这个证的核心判断，佐使 = 针对兼夹症状的加减。两个数
        # 分开报才能区分"核心判断一致、只是加减用药不同"（合理的用药灵活性）和
        # "连君臣都对不上"（真分歧）。None = 这一层没数据，见 layer_note。
        "core_jaccard": core_jaccard,
        "adjunct_jaccard": adjunct_jaccard,
        "shared_core_herbs": sorted(set.intersection(*core_sets)) if core_jaccard is not None else [],
        "shared_adjunct_herbs": (
            sorted(set.intersection(*adjunct_sets)) if adjunct_jaccard is not None else []
        ),
        # 按医家报"有几味药没标 role"——分层的数可信到什么程度全看这个：
        # 没标注的药既不在君臣层也不在佐使层（只在 herb_jaccard 里），
        # 这个数大就说明分层指标只覆盖了一部分用药，不能当全貌读。
        "n_unroled": {r["physician"]: p["n_unroled"] for r, p in zip(results, role_parts)},
        "layer_note": (
            "core_jaccard/adjunct_jaccard 为 null 表示这一层没有可比数据"
            "（至少一位医家在这一层没有标注 role 的药），不是 0——"
            "0 的意思是「两边完全相同」，跟「没数据」是两回事。"
            "role 未标注的药不进任何一层，只计入 n_unroled，但仍照旧计入 herb_jaccard。"
        ),
        **pairwise,
        "treatment_principle_same": tp_same,
        # None = 两位医家都没开西药，这个维度不适用（不是"完全一致"）
        "western_drug_overlap": western_overlap,
        # 噪声地板：herb_jaccard 本身没有意义，除非知道"同一设定重复跑，本来就会
        # 抖多少"。None = 还没跑过 offline/estimate_epsilon.py，前端要如实展示
        # "未测"，不能假装这个数已经有对照（CLAUDE.md「任何数字都必须带对照」）。
        "epsilon_online": load_epsilon_online(),
        # 分层的数同样必须带自己的对照基准：ε_online 是整方的地板，拿它去卡
        # 君臣层会低估一致性、卡佐使层会高估发散（实测佐使层抖动大于整方、
        # 君臣层小于整方）。None = 还没跑过带分层的 estimate_epsilon。
        **{f"epsilon_{k}": v for k, v in load_epsilon_layer_means().items()},
        # R14：对照带上那条参考线画的就是这个阈值，所以它必须是**这条主诉自己的**
        # 地板，不是全局均值（实测 9 条可用主诉里 4 条的地板高于全局均值，最高的
        # 一条 0.3954 对 0.2611）。取不到时退回全局均值、把 scope 标成 "global"，
        # 界面上显示「（全局）」——一个没说明来源的对照基准跟没有对照一样。
        "epsilon_for_query": load_epsilon_for_query(complaint),
    }

    # R44：**取证与自验这两条能力发生过就记一笔。** 从 results 里现算，
    # 不在各处埋点——埋点必然漏一处，而漏掉的表现是"那一步好像没做"。
    n_react = sum(1 for r in results if r.get("react_trace"))
    if n_react:
        steps = sum(len(r["react_trace"].steps) for r in results if r.get("react_trace"))
        trace.record("gather_evidence", f"取证 {steps} 步（{n_react} 条推理链）")
    n_verified = sum(1 for r in results if r.get("verification"))
    if n_verified:
        reopened = _reopen_calls(results)
        trace.record("verify_and_revise",
                     f"验了 {n_verified} 份处方" + (f"，重开 {reopened} 次" if reopened else "，一次通过"))

    # R46 §7.2/§7.3：「人」维核查与循证对照。**两者都是确定性计算，零 LLM 调用**
    # ——个体化的每一条要指得出本草原文（`basis` 是 `Field(min_length=1)`），
    # 让模型生成的话那个字段只能是编的；循证对照比的是教材条目，更没有理由
    # 去问模型。所以它们放在这里而不是进 prompt：**算得出来的东西不问模型。**
    individualization, guideline = _patient_and_guideline(results, patient_profile)
    if individualization is not None:
        trace.record(
            "verify_patient_fit",
            (f"按「人」维核出 {len(individualization.items)} 条调整提示"
             if individualization.items
             else f"按「人」维查了 {len(individualization.considered)} 项，没有需要调整的"))

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
        "agent_trace": trace.to_list(),
        # R46：个体化调整与循证对照。None = 这一次没有可算的（没填人维 /
        # 没有证型），**跟"算了但是空的"是两件事**，前端据此显示不同的话。
        "individualization": (individualization.model_dump()
                              if individualization is not None else None),
        "guideline": guideline,
        # S1/S2 这一段（合一 1 次、分开 2 次，见 s1s2_calls）+ 每位医家 S3 一次
        # + 残差一次 + 配伍禁忌重开若干次。重开必须计进来：漏算的话 manifest 报的
        # 调用数会低于实际花费，拿它算成本或比配置就都是错的。
        "manifest": _build_manifest(
            int((time.time() - _t0) * 1000),
            s1s2_calls
            + extra_calls
            # R22：每位医家采了 N 次 S3，见上面 SafetyVeto 分支里那段注释。
            + sum(len(r["candidates_scored"]) for r in results)
            + (1 if residual else 0)
            + _reopen_calls(results)
            # ReAct 的每一步都是一次真实调用，必须计进来：漏算的话 manifest 报的
            # 调用数会低于实际花费，拿它算成本或比 use_react 开关的代价就都是错的。
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"]),
            use_react,
            retriever_mode=retriever_mode,
            s3_mode_used=mode,
            knowledge=_aggregate_knowledge(results),
            streaming=_aggregate_streaming(results),
            synthesis=_synthesis_summary(results, mode),
        ),
    }


def _step_name(step) -> str:
    """五步链的一步 → 它的名字。裸字符串原样返回，对象取 `name`，
    其余（None、数字）返回空串。**一处实现**：证型、治法、方名三处都走它。"""
    if isinstance(step, str):
        return step.strip()
    if hasattr(step, "model_dump"):
        step = step.model_dump()
    if isinstance(step, dict):
        return str(step.get("name") or step.get("principle") or "").strip()
    return ""


def _patient_and_guideline(results: list[dict], patient_profile):
    """「人」维核查 + 循证对照。两者都从**第一条结论**取方与证型。

    为什么只看第一条：structured 模式下 `results` 恰好一条（那是融合出的
    结论）；legacy 三列模式下三条方各不相同，对每一条各算一份会在界面上摆出
    三份对照——而 R44 刚把"几份并列"从产品面上消除。legacy 是研究面，
    那里看的是三列本身，不需要这一层。
    """
    from core.guideline_compare import compare
    from core.individualize import individualize

    r = (results or [{}])[0]
    st = r.get("s3_structured")
    st = st.model_dump() if hasattr(st, "model_dump") else (st or {})
    flat = r.get("s3")
    flat = flat.model_dump() if hasattr(flat, "model_dump") else (flat or {})
    # **五步链里每一步都是一个对象**（`{"name": ..., "from_...": ...}`），
    # 扁平的那份 `s3` 才是裸字符串。两种形状都要认——只按其中一种写，
    # 另一种走到这里是 `AttributeError: 'dict' object has no attribute 'strip'`，
    # 而那会把一次本来跑成了的问诊整个打断。
    syndrome = _step_name(st.get("syndrome")) or _step_name(flat.get("syndrome"))
    method = _step_name(st.get("method")) or _step_name(flat.get("method"))
    formula = st.get("formula") or {}
    if not isinstance(formula, dict):
        formula = formula.model_dump() if hasattr(formula, "model_dump") else {}
    # 五步链的 `formula` 是 `{"candidate": {...}, "from_method": ...}`，
    # 扁平那份是 `{"name": ..., "herb_items": [...]}`——同样两种形状都认。
    cand = formula.get("candidate") if isinstance(formula.get("candidate"), dict) else formula
    fname = (cand or {}).get("name") or formula.get("name") or ""
    herbs = [it.get("name", "") for it in ((cand or {}).get("herb_items") or [])
             if isinstance(it, dict)]
    if not herbs:
        herbs = [h for h in (flat.get("herbs") or []) if isinstance(h, str)]

    individualization = None
    if patient_profile is not None:
        individualization = individualize(patient_profile, herbs, syndrome)
    guideline = compare(syndrome, str(method), fname, herbs) if syndrome else None
    return individualization, guideline


def consult_many(queries: list[str], consult_fn=None) -> tuple[list[dict | None], list[dict]]:
    """逐条跑 consult，**一条挂了不拖累其余**。返回 (与 queries 对齐的结果列表，
    失败记录)；失败的位置是 None。

    eval/run_eval.py 和 eval/mes/export.py 之前各自写的是 `[consult(q) for q in
    queries]`：第 9 条主诉的 LLMError 会把前 8 条已经花钱跑完的结果一起丢掉。
    run_batch 的文档记过同一个坑（insufficient 分支 AttributeError 整批挂掉），
    教训没有传到后来的两个批处理入口——所以抽成一处，两边都调它。
    """
    from core.parallel import run_indexed, worker_count
    from core.progress import Progress

    fn = consult_fn or consult
    failures: list[dict] = []
    # R40：**并发跑**。一条主诉十几次 LLM 调用、几十秒到几分钟，其中绝大部分
    # 时间在等 socket——串行跑 10 条 = 10 倍的等待。E1/E2 与 MES 导出是这个
    # 函数的主干，它们批量跑几十条，省下的是小时级的墙钟。
    #
    # 默认并发度 `CONSULT_MANY_WORKERS`（默认 4）而不是"能开多少开多少"：
    #   · 上游 API 有速率限制，一次把 50 条打出去会整批 429；
    #   · 本地 vLLM 后端的显存是硬上限，并发过高直接 OOM；
    #   · 4 是"明显比 1 快、又不至于触发限流"的保守值，现场可调。
    # 串行（=1）时的顺序、异常路径与并发路径完全一致（见 `run_indexed`）。
    workers = worker_count("CONSULT_MANY_WORKERS", 4)
    bar = Progress(total=len(queries), label=f"consult 批量（并发 {workers}）", unit="条")

    def _done(i: int, _result) -> None:
        bar.advance(note=f"第 {i + 1} 条「{queries[i][:12]}」")

    def _failed(i: int, e: BaseException) -> None:
        # 一条主诉的失败不能把整批已完成的结果一起丢掉。
        print(f"[consult_many] 第 {i + 1} 条失败：{type(e).__name__}: {e}", file=sys.stderr)
        bar.note(f"第 {i + 1} 条失败：{type(e).__name__}")
        failures.append({"index": i + 1, "query": queries[i],
                         "error": f"{type(e).__name__}: {e}"})

    results = run_indexed(list(queries), fn, workers=workers,
                          on_done=_done, on_error=_failed,
                          thread_name_prefix="consult")
    # 失败记录按 index 排序：并发下回调的到达顺序不确定，而 failures 是要写进
    # 报告的——同一批输入必须给出同一份报告，不能因为线程调度而变。
    failures.sort(key=lambda f: f["index"])
    bar.close(f"{len(queries) - len(failures)} 条成功，{len(failures)} 条失败")
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
