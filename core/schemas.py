"""全项目共用的 pydantic 数据模型。离线抽取和在线推理链都从这里取模型，不裸用 dict。"""
from typing import Literal

from pydantic import BaseModel, Field

# ---------- 离线：医案结构化 ----------


class CaseStructured(BaseModel):
    symptoms: list[str] = Field(default_factory=list)
    tongue: str | None = None
    pulse: str | None = None
    syndrome: str | None = None
    pathogenesis: str | None = None
    treatment_principle: str | None = None
    formula: str | None = None
    herbs: list[str] = Field(default_factory=list)


class VisitStructured(CaseStructured):
    """一诊的结构化结果。CaseSequence.visits 的元素类型。"""

    visit_index: int = 0  # 0=初诊
    visit_marker: str | None = None  # 原文里标识这一诊的字样（「又」「初三日」等），初诊为 None
    visit_date: str | None = None  # 原文日期原样摘录（如「乙酉五月二十一日」），不做归一化解析
    response_to_prior: str | None = None  # 上一诊治疗后的反应（「服七帖而效」），初诊为 None


class CaseSequence(BaseModel):
    """一个病人的完整诊次序列。"""

    visits: list[VisitStructured] = Field(min_length=1)


class SegmentPatients(BaseModel):
    """s0_extract_case 现在的输出形状。R1 的粗段（见 offline/split_cases.py）不保证
    只含一个病人，切分病人边界这件事交给模型做，一段可能有零个、一个或多个病人。

    patients 允许是空列表——粗段有可能整段都是编者按语或纯议论（比如挨着按语被
    粘连进来的情况），这时诚实报告"这段没有病人"是合法输出，跟 S2Elements.elements
    可以为空是同一个道理，不要因为"看起来应该有内容"就诱导模型编一个病人出来。"""

    patients: list[CaseSequence] = Field(default_factory=list)


class CaseRecord(VisitStructured):
    case_id: str
    physician: str
    raw: str
    # 默认公有领域；将来接入现代出版书籍时改为 copyrighted，
    # export_sft.py 会据此过滤，是版权合规的代码级强制点。
    copyright_status: Literal["public_domain", "copyrighted"] = "public_domain"
    # 同一病人的所有诊次共享，格式 {physician}-{案号}
    case_group_id: str
    # 上一诊的 case_id，初诊为 None；诊次内的链式关系靠这个字段还原，不靠 visit_index 推断
    prev_case_id: str | None = None
    # 该诊次在 raw 里对应的片段。raw 是整个粗段（同一段的多个诊次共享，
    # 内容完全相同），前端证据链侧栏需要的是"这一诊对应原文哪几行"。
    raw_excerpt: str | None = None


# ---------- X3：医案三元组（S5 抽取，LLM 输出） ----------


class CaseTripleItem(BaseModel):
    """S5（offline/extract_case_triples.py）从一诊原文里抽出的一条三元组。
    s/p/o 全部要求非空——防幻觉约束：抽不出完整的三元组就不该抽这一条，
    不能用空字符串占位凑数。

    p（谓词）刻意不限定成固定枚举：医案原文里的关系比"证候-治法-方剂-药物"
    这条链丰富得多（症状表现、病机归因、疗效反馈……），限定成小枚举会逼模型
    把不属于任何枚举值的关系硬套进去，反而制造假三元组。core/tools.py 的
    query_case_graph 也是按这个前提设计的：predicate 过滤用的是子串匹配，
    不是枚举相等。

    source_span 是这条三元组在原文里的出处片段，**必须能在传给模型的原文里
    逐字找到**——这一步不是 pydantic 能校验的（schema 只管字段非空，不知道
    "传给模型的原文"是什么），核验逻辑在 extract_case_triples.py 里，抽取后
    立刻做，验不过的三元组直接丢弃、不写进 data/case_triples.jsonl。
    """

    s: str = Field(min_length=1)
    p: str = Field(min_length=1)
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)


class CaseTripleExtraction(BaseModel):
    """S5 单次调用的输出：一段诊次原文里能抽出的全部三元组。允许空列表——
    有的诊次原文过简（"药后知，肿消"），确实抽不出任何结构化关系，不能因为
    "总要抽出点什么"就编。"""

    triples: list[CaseTripleItem] = Field(default_factory=list)


class CaseTripleRecord(BaseModel):
    """写进 data/case_triples.jsonl 的最终形态：CaseTripleItem 补上
    case_id/physician，字段名严格对齐 core/tools.py._load_case_triples() /
    query_case_graph() 已经在读的格式——那份代码是这个格式的第一个、也是
    唯一的消费者，字段名改了它就读不到数据，不能各写各的。"""

    case_id: str
    physician: str
    s: str = Field(min_length=1)
    p: str = Field(min_length=1)
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)


# ---------- 知识图谱：证候定义（人工核对录入，不是 LLM 输出） ----------


class SyndromeDefinition(BaseModel):
    """图谱骨架的数据单元。这不是 LLM 输出 schema，是人工核对录入
    data/standard/syndromes.jsonl 用的，所以没有 min_length=1 这类防幻觉约束——
    防幻觉的关键在录入环节本身"不编造"，不在这里加字段约束。

    source 的六档是按可信度分层，不是按"是不是国标"二分：GB/T 16751.2 全文目前
    拿不到（网页版可在线读、不可批量下载），实际数据来自 WFCMS 等国际标准组织、
    同行评审论文、团体标准公示稿、教材/科普站点等 7 个独立可核验来源交叉确认，
    档次从高到低：
      gb_standard        国标原文（目前没有任何条目用这档，因为拿不到全文）
      official_consensus 国际标准组织 / 国家级专科共识发布，带正式编码
      group_standard     团体标准公示稿
      journal            同行评审期刊论文（可能带 GB/T 15657 官方编码，但没有
                          独立的标准号）
      secondary_verified 教材/科普站点等二手来源，但内容与主流教材交叉确认一致
      manual             最差情况：没有可核实来源支撑的人工最小骨架
    """

    code: str
    name: str
    is_category: bool  # True=类目词，国标明确写了"不适用于临床诊断"，S3 不能输出它
    parent: str | None = None  # is_a 层级的父节点 code
    definition: str
    location: list[str] = Field(default_factory=list)  # 病位证素
    nature: list[str] = Field(default_factory=list)  # 病性证素
    cardinal_symptoms: list[str] = Field(default_factory=list)  # 主症
    secondary_symptoms: list[str] = Field(default_factory=list)  # 次症
    tongue_pulse: str | None = None
    source: Literal[
        "gb_standard",
        "official_consensus",
        "group_standard",
        "journal",
        "secondary_verified",
        "manual",
    ]
    # ICD-11（含 WHO 传统医学模块 TM2）编码。目前唯一能做跨术语体系映射的锚点，
    # 只有极少数条目的来源本身给出了这个编码，绝大多数留空——不要为了填满这个
    # 字段去反查/编一个编码出来。
    icd11_code: str | None = None


# ---------- 在线：结构化推理链 SRC ----------


class S1Normalize(BaseModel):
    symptoms: list[str] = Field(default_factory=list)
    tongue: str | None = None
    pulse: str | None = None
    unmapped: list[str] = Field(default_factory=list)


class ElementHit(BaseModel):
    element: str
    kind: Literal["location", "nature"]
    # min_length=1 是防幻觉的关键约束：证素必须有支撑症状，
    # 空引用在 schema 校验层就被拒绝，不要改成可选。
    supporting_symptoms: list[str] = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class S2Elements(BaseModel):
    # elements 本身可以是空列表——模型确实找不到证素时的合法退路。
    elements: list[ElementHit] = Field(default_factory=list)
    unexplained_symptoms: list[str] = Field(default_factory=list)


# ---------- 在线：ReAct（G2） ----------


class ReActStep(BaseModel):
    """ReAct 单步的 LLM 输出。

    thought 的 min_length=1 是有意的：允许空 thought，循环会退化成一串没有理由的
    工具调用，"可解释"这条就没了，事后也没法判断它为什么选这个工具。

    action 用裸 str 而不是 Literal[工具名...]：模型写了不存在的工具名时，用
    Literal 会让 pydantic 校验失败、走 generate() 的三次重试，一个笔误烧掉 3 次
    调用；用 str 则由 react 循环把"没有这个工具，可用的是……"当成 observation
    回灌，只花 1 次。这跟 core/tools.py "工具永不抛异常给调用方"是同一条思路。
    """

    thought: str = Field(min_length=1)
    action: str = Field(min_length=1)
    action_input: dict = Field(default_factory=dict)


class ReActStepRecord(BaseModel):
    """一步的完整记录。这不是 LLM 输出，是循环自己记的账，所以没有防幻觉约束。"""

    step: int
    thought: str
    action: str
    action_input: dict = Field(default_factory=dict)
    observation: str
    # 这一步发生了什么异常情况：工具名不存在、参数不合法、和前面某步完全重复
    note: str | None = None


class ReActTrace(BaseModel):
    steps: list[ReActStepRecord] = Field(default_factory=list)
    # finish=模型自己说够了；ask_user=它要追问；max_steps=撞上限；
    # no_progress=连续重复同一个调用；error=LLM 调用本身失败。
    # 这五种要分开记：撞 max_steps 说明 prompt 没让模型知道什么时候算够了，
    # 跟"它想清楚了主动收尾"是完全不同的结论，混成一个"结束了"就看不出来。
    terminated_by: Literal["finish", "ask_user", "max_steps", "no_progress", "error"]
    pending_question: str | None = None
    # ask_user 终止后由 run_physician 通过 ask_fn 问出来的回答（先过 check_safety）。
    # 没有提问渠道时为 None——那时问题只是被记录，S3 拿不到答案。
    pending_answer: str | None = None
    # 取证过程中 search_cases 真实返回过的 case_id。它们和 run_physician 自己检索到的
    # refs 一样是真实医案，必须并进引用白名单——否则工具描述里承诺「可以引用这里
    # 返回的 id」，幻觉判定却只认 refs，模型照做反而被判成幻觉。
    retrieved_case_ids: list[str] = Field(default_factory=list)
    llm_calls: int = 0


# ---------- 在线：追问（G3） ----------


class HistoryItem(BaseModel):
    """一轮追问的完整记录。

    四个字段的形状是定死的：`asserted`/`denied` 必须分开存，不能只存 answer
    原文。实测（见 data/SOURCES.md 第 10 条）患者说了「口不渴」，系统只把它当
    成一条普通症状收着，下一轮照样问「有没有口干或口苦」——否定回答不进结构化
    字段，重复提问就堵不掉，而且后验也白丢一半证据。
    """

    question: str
    answer: str
    # 这一问对应的国标症状名。十问歌后备问的是一个话题不是一条症状，此时为 None，
    # 答案也就无法归到 asserted/denied——这是后备模式的固有代价，不要假装能归。
    symptom: str | None = None
    topic: str | None = None
    asserted: list[str] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)
    # check_safety 命中时的拒绝理由。非 None 表示这一轮的回答触发了安全否决，
    # 整个问诊到此为止（CLAUDE.md「追问的回答必须先过 check_safety」）。
    safety_hit: str | None = None


class FollowupResult(BaseModel):
    history: list[HistoryItem] = Field(default_factory=list)
    asserted: list[str] = Field(default_factory=list)
    denied: list[str] = Field(default_factory=list)
    rounds: int = 0
    # max_rounds=问满轮次；converged=再问也问不出信息了；no_candidate=没问题可问；
    # safety=回答触发安全否决；fast_mode=被开关跳过；no_answer=提问方没给回答。
    # 这六种要分开：converged 和 max_rounds 都是"停了"，但前者说明追问设计有效、
    # 后者说明轮次上限卡住了它，混成一个就没法调 MAX_ASK_ROUNDS。
    stopped_by: Literal[
        "max_rounds", "converged", "no_candidate", "safety", "fast_mode", "no_answer"
    ]
    reject_reason: str | None = None


class S3Syndrome(BaseModel):
    syndrome: str
    reasoning: str
    treatment_principle: str
    formula: str | None = None
    herbs: list[str] = Field(default_factory=list)
    # min_length=1 同理：防幻觉的关键约束，不要改成可选。
    cited_case_ids: list[str] = Field(min_length=1)
    note: str | None = None


class S3SyndromeUnreferenced(BaseModel):
    """检索不到任何相关医案（相似度全部低于阈值，或该医家没有医案）时用的 S3 schema。

    **没有 cited_case_ids 字段。** 这是 CLAUDE.md「某个新场景导致校验失败时，新建一个
    不含该字段的 schema，不是放松原来的约束」的落地：白名单为空时 S3Syndrome 的
    min_length=1 会逼模型编一个 id——要么必被判幻觉却照样出方，要么三次校验失败抛
    LLMError 让整个 consult 崩掉。S3Syndrome 本身一个字没动。
    """

    syndrome: str
    reasoning: str
    treatment_principle: str
    formula: str | None = None
    herbs: list[str] = Field(default_factory=list)
    note: str | None = None

    @property
    def cited_case_ids(self) -> list[str]:
        """让下游（幻觉检查、前端）按同一个接口读；这里永远是空——没有可引用的医案。
        是 property 不是字段：model_dump 里不会出现，api 层负责补一个空列表。"""
        return []
