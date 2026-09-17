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
    s3_best_of_n,
    s3_reasoning_effort,
    s3_thinking,
    thinking_by_step,
    thinking_for,
)
from core.followup import (
    AskFn, fast_mode_enabled, format_followup_for_s3, parse_answer, run_followup,
)
from core.physicians import PHYSICIANS, physicians_enabled
from core.react import StepFn, format_trace_for_s3, react_enabled, run_react
from core.retrieval import adaptive_min_score, apply_low_discrimination_cutoff, get_retriever
from core.retrieval_hybrid import (
    ALLOWED_MODES,
    DEFAULT_MODE,
    RETRIEVER_MODE_ENV,
    effective_mode,
)
from core.safety import check_safety, danger_confirmed_by_answer, safety_bypassed
from core.formula_check import advice_dicts, check_formula
from core.safety_output import assess_formula_safety, format_blocking_issues
from core.schemas import (
    CaseRecord, FollowupResult, S1Normalize, S2Elements, S3Syndrome, S3SyndromeUnreferenced,
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
def _score_candidate(s3) -> tuple[float, dict]:
    """一次采样的分 + 写进 manifest/响应的那一行。

    分数只看 `selected` 那张方：模型自己挑了一张，我们评的就是它挑的那张。
    评所有候选方再取最高会让"模型挑得对不对"这件事从判据里消失。
    """
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


def _best_of_n_s3(s3_system: str, s3_schema, physician: str):
    """采 N 次 S3，按 `score_formula` 挑分最高的一次。返回 (s3, candidates_scored)。

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

    def one():
        return get_llm().generate(
            system=s3_system, user="", schema=s3_schema, physician=physician,
            **thinking,
        )

    if n == 1:
        s3 = one()
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
        trace = run_react(
            name=physician_name,
            # physician 在这里已经是 id：prompt 里的 $physician_id 直接用它，
            # 不让 run_react 再从中文名反查一遍（反查是兜底，不是主路径）。
            physician_id=physician,
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
    # physician 传下去是给本地后端选 LoRA adapter 用的（阶段五每位医家一个
    # adapter，vLLM server 按请求切换）。传的是 id 不是中文名——SOURCES.md
    # 第 31 条那个坑：id 和中文名混用会让按 id 索引的东西恒空。云端后端
    # （DeepSeek）如实忽略它，见 core/llm.py::LLMBackend._complete 的文档。
    # S3 是这条链上唯一真正需要推理的一步，默认开思考（S3_THINKING 可整体关掉，
    # 关掉之后跑出来的数字跟默认配置不可比——manifest 会带上这句话）。
    s3, candidates_scored = _best_of_n_s3(s3_system, s3_schema, physician)

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


def _build_manifest(elapsed_ms: int, llm_calls: int, use_react: bool = False,
                    retriever_mode: str | None = None,
                    knowledge: dict | None = None) -> dict:
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
    emit,
) -> None:
    """三位医家**并发**跑 S3，结果按 `PHYSICIANS` 的插入顺序追加进 `results`。

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
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), 1, use_react, retriever_mode=retriever_mode),
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
                int((time.time() - _t0) * 1000), 2, use_react,
                retriever_mode=retriever_mode,
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
                "manifest": _build_manifest(int((time.time() - _t0) * 1000), 2, use_react, retriever_mode=retriever_mode),
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
                2 + extra_calls + (1 if residual else 0), use_react,
                retriever_mode=retriever_mode,
            ),
        }
    results = []
    try:
        _run_physicians_into(
            results, s1, s2, use_react=use_react, followup=followup, ask_fn=ask_fn,
            bypass=bypass, on_step=on_step, retriever_mode=retriever_mode,
            refs_mode=refs_mode, emit=emit,
        )
    except SafetyVeto as veto:
        # ReAct 追问问出了危重症状：跟初始主诉命中同一道否决，已经跑完的医家结果
        # 也不返回——被拦截的请求不产出任何方药。
        calls = (
            2 + extra_calls + (1 if residual else 0) + veto.llm_calls
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"])
            # R22：**不是 len(results)**。每位医家的 S3 采了 N 次（best-of-N），
            # 按医家数计等于漏掉 N−1 次真实调用——manifest 的调用数是额度结算的
            # 依据，漏算的表现是账本持续少扣，而少扣不会报错。
            # 用每位医家自己报的 N（`_best_of_n_s3` 返回几条就是采了几次），
            # 不是全局 s3_best_of_n()：中途改环境变量、或某位医家采样部分失败时，
            # 全局那个数跟实际发生的次数会不一致。
            + sum(len(r["candidates_scored"]) for r in results)
            + sum(1 for r in results if r["safety_output"]["revised"])
        )
        return {
            "s1": s1, "results": [], "divergence": None,
            "rejected": True, "reject_reason": veto.reason,
            "safety_flag": safety_flag or veto.reason, "retrieval_error": None,
            "s2": s2, "followup": followup, "residual": residual,
            "insufficient": False, "insufficient_reason": None, "coverage": None,
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), calls, use_react,
                                        retriever_mode=retriever_mode,
                                        knowledge=_aggregate_knowledge(results)),
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
                retriever_mode=retriever_mode,
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
            # R22：每位医家采了 N 次 S3，见上面 SafetyVeto 分支里那段注释。
            + sum(len(r["candidates_scored"]) for r in results)
            + (1 if residual else 0)
            + sum(1 for r in results if r["safety_output"]["revised"])
            # ReAct 的每一步都是一次真实调用，必须计进来：漏算的话 manifest 报的
            # 调用数会低于实际花费，拿它算成本或比 use_react 开关的代价就都是错的。
            + sum(r["react_trace"].llm_calls for r in results if r["react_trace"]),
            use_react,
            retriever_mode=retriever_mode,
            knowledge=_aggregate_knowledge(results),
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
    from core.progress import Progress

    fn = consult_fn or consult
    results: list[dict | None] = []
    failures: list[dict] = []
    # 一条主诉十几次 LLM 调用、几十秒；这个循环是 E1/E2 和 MES 导出的主干，
    # 原来从头到尾只在失败时才出声（R9：静默和卡死不能长得一样）。
    bar = Progress(total=len(queries), label="consult 批量", unit="条")
    for i, complaint in enumerate(queries, 1):
        try:
            results.append(fn(complaint))
            bar.advance(note=f"第 {i} 条「{complaint[:12]}」")
        except Exception as e:  # noqa: BLE001 - 一条主诉的失败不能把整批已完成的结果一起丢掉
            print(f"[consult_many] 第 {i} 条失败：{type(e).__name__}: {e}", file=sys.stderr)
            bar.note(f"第 {i} 条失败：{type(e).__name__}")
            results.append(None)
            failures.append({"index": i, "query": complaint, "error": f"{type(e).__name__}: {e}"})
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
