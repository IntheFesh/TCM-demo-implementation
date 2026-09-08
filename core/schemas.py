"""全项目共用的 pydantic 数据模型。离线抽取和在线推理链都从这里取模型，不裸用 dict。"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from core.herbs import split_western_drugs

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
    # A2 张锡纯「衷中参西」：方中会出现阿斯匹林、硫酸镁这类西药。跟 herbs 互斥，
    # 由 core.herbs.split_western_drugs 在抽取边界上强制拆开——混进 herbs 会污染
    # herb_jaccard，把跨学派分歧系统性推高，而那个推高是假的（两位温病医家不可能
    # 开阿斯匹林）。叶天士/吴鞠通两本书实测一个西药词都没有，这个字段对他们恒为空。
    western_drugs: list[str] = Field(default_factory=list)


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


# ---------- 在线：病名层（M4） ----------


class Disease(BaseModel):
    """`data/standard/diseases.jsonl` 里的一条：中医内科病名参考表。M4 起给 S3 的
    `disease` 字段做交叉校验（`core.diseases.match_disease`）、给图 layer2
    拼 `病名 · 证型` label、给 M6 患者模式的导诊输出提供数据。

    不是 LLM 输出 schema——这是人工整理的静态参考表，加载后不再做任何
    生成式校验，`min_length=1` 只用在 `name` 上（防"表里混进一条空名称的
    脏数据"，不是防模型幻觉，来源不同但约束该一样严）。

    `triage_dept` / `triage_urgency` / `red_flags` 三个字段现在就要跟
    `core.safety_output.DOSE_LIMITS` 一样的严格度：有出处、不确定就留 None
    /空列表，不为了让表看起来完整而编一个等级——这三个字段虽然要到 M6
    患者模式才真正用上，但它们是安全相关信息（导诊结果直接决定"要不要
    建议立刻就医/叫救护车"），错误的严重度跟填错一味药的剂量上限是同一
    量级，不能因为"暂时用不上"就放松核实标准。
    """

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    location: list[str] = Field(default_factory=list)  # 病位，取值应落在 core.elements.LOCATIONS 内，否则 match_disease 的病位匹配永远命中不了
    cardinal: list[str] = Field(default_factory=list)  # 主症关键词，match_disease 用来算命中数
    common_syndromes: list[str] = Field(default_factory=list)
    corpus_gate: list[str] = Field(default_factory=list)  # 语料库门类关键词，取 core.syndrome_norm 的 canonical 名；语料里没有对应医案的病名此项如实留空
    triage_dept: str | None = None
    triage_urgency: Literal["low", "medium", "high"] | None = None
    red_flags: list[str] = Field(default_factory=list)


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


class HerbItem(BaseModel):
    """处方里的一味药。名字之外的字段都可能缺失（古籍医案常常不写剂量），
    缺失一律 None，不要填默认值——"没写"和"写了 0"是两回事。

    role 不做枚举以外的约束（比如"一个方最多一个君药"）：君臣佐使的分配是
    模型自己判断的，这里只承接结果，不裁判它对不对——M7 的报告会把填充率
    如实报出来，判断准不准是后续要看真实产出才能下的结论，不是 schema 该管的事。
    """

    name: str = Field(min_length=1)
    dose: float | None = None
    dose_unit: Literal["g", "钱", "两", "分", "枚", "片"] = "g"
    processing: str | None = None  # 炮制：醋制/煅/蜜炙/生/炒/姜制/酒制
    decoction: str | None = None  # 煎法：先煎/后下/包煎/烊化/冲服/另煎
    role: Literal["君", "臣", "佐", "使"] | None = None
    function_in_formula: str | None = None
    dose_evidence: list[str] = Field(default_factory=list)


class DoseViolation(BaseModel):
    """一味药剂量超过 core.safety_output.DOSE_LIMITS 里的常用上限。

    这不是 LLM 输出 schema——`core.safety_output.check_dose_limits()` 用确定性
    规则算出来的结果，M2 把它做成 pydantic 模型（而不是像 check_incompatible
    继续返回裸 tuple）是因为它要装进 FormulaSafety，被 FormulaCandidate.safety
    这个真实字段携带，FormulaSafety 又是这个模块里的类型——三者必须在同一处
    定义才不会出现循环依赖（见 FormulaSafety 的文档字符串）。
    """

    herb: str
    dose: float
    unit: str
    limit_g: float
    reason: str


class FormulaSafety(BaseModel):
    """一个候选方的 X2 输出侧安全汇总：十八反十九畏、寒热方向、剂量上限、
    必要煎法、毒性标记。字段本身在这里定义，但**产出这个对象的逻辑一律在
    core/safety_output.py**（check_incompatible/check_thermal_consistency/
    check_dose_limits/check_required_decoction/check_toxic_herbs 五个函数 +
    assess_formula_safety 这一个组装点）——这条边界很重要：

    这个类本该跟着 check_dose_limits 等函数放在 core/safety_output.py（M2 的
    任务描述原文就是这么写的），但 `FormulaCandidate.safety: FormulaSafety
    | None` 必须是一个真实的 pydantic 字段类型（不是裸 dict，"所有结构化数据
    都用 pydantic 承接"是这个项目从第一个模块就守的规矩），而 FormulaCandidate
    定义在这个文件里——如果 FormulaSafety 留在 core/safety_output.py，
    core.schemas 要 import core.safety_output 来拿类型，core.safety_output
    本来就要 import core.schemas 拿 HerbItem，两边互相导入会在模块加载时炸。
    放在这里，core.safety_output 单向依赖 core.schemas（导入 HerbItem/
    DoseViolation/FormulaSafety），跟这份文件已有的方向（core.schemas 依赖
    core.herbs、不依赖任何业务逻辑模块）一致，不新开一条依赖边。

    blocking 分两级：incompatible（十八反十九畏）和 dose_violations（剂量超限）
    是拦截级——命中就该让 run_physician 重开一次；thermal_warning（寒热方向）/
    decoction_missing（缺必要煎法）/ toxic_herbs（含毒性药材）是警告级，只展示
    不打回——寒热错杂本来就寒热并用、毒性药材的常规用量本就贴着上限、煎法漏标
    不代表方子本身有问题，这三类逼模型重开只会把本来对的方子改坏。
    """

    incompatible: list[tuple[str, str]] = Field(default_factory=list)
    thermal_warning: str | None = None
    dose_violations: list[DoseViolation] = Field(default_factory=list)
    decoction_missing: list[str] = Field(default_factory=list)
    toxic_herbs: list[str] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.incompatible) or bool(self.dose_violations)


class FormulaCandidate(BaseModel):
    """一个候选方。三种来源的可信度不同，前端必须视觉区分（M6/M7）：
      classic  —— 现有经典方，原方名照写
      modified —— 在经典方基础上加减，必须能追溯到 base_formula
      composed —— 根据药性药理自组方，没有"原方"这个概念

    base_formula 的约束用 model_validator 强制而不是留给调用方记得填：
    加减方不写原方就无法追溯改了什么，这条防线不能是"建议"。
    """

    name: str = Field(min_length=1)
    source: Literal["classic", "modified", "composed"]
    base_formula: str | None = None
    confidence: Literal["high", "medium", "low"]
    rationale: str = Field(min_length=1)
    # min_length=1：一个"候选方"至少要有一味药，否则不构成方。
    herb_items: list[HerbItem] = Field(min_length=1)
    doses_count: int | None = None  # 剂数
    usage: str | None = None  # 用法，如"水煎服，每日1剂，分2次温服"
    # M2：X2 输出侧安全汇总。不由模型生成——安全判定必须是确定性规则，不能让
    # 模型自己说"我这个方是安全的"。默认 None，S3 生成后由
    # core.safety_output.assess_formula_safety() 填充（core/chain.py 里做）。
    safety: FormulaSafety | None = None

    @model_validator(mode="after")
    def _check_base_formula(self) -> "FormulaCandidate":
        if self.source == "modified":
            if not self.base_formula:
                raise ValueError(
                    "source='modified'（加减方）必须填 base_formula——"
                    "不写原方就无法追溯改了什么，这条约束不能省。"
                )
        elif self.base_formula:
            raise ValueError(
                f"source={self.source!r} 时 base_formula 必须为空："
                "只有 modified（加减方）才有『原方』这个概念，classic/composed 硬填一个会误导。"
            )
        return self


# 向后兼容合成用的占位符：只在"旧式调用只给了 herbs/formula/western_drugs、
# 完全没给 formula_candidates"且连一味药都没给时才可能出现在 herb_items[0].name /
# formula_candidates[0].name 里，_S3Base._derive_flat_fields 会把它从派生结果里
# 过滤掉，好让 .herbs == [] / .formula is None 这两条旧默认值原样保留
# （见 core/schemas.py 的 M1 迁移设计——不能让"没提供任何药材"被合成成一味假药，
# 那会把 herb_jaccard 从 None 悄悄变成 0.0，一个没人要求过的行为变化）。
_LEGACY_HERB_PLACEHOLDER = "（占位·未提供药材）"
_LEGACY_FORMULA_PLACEHOLDER = "（占位·未提供方名）"


class _S3Base(BaseModel):
    """`S3Syndrome` 与 `S3SyndromeUnreferenced` 共享的字段与派生逻辑。

    两个子类唯一的区别本该只在 cited_case_ids（一个必填、一个恒空的
    property）——这个基类的存在就是不让这个"唯一的区别"之外的东西被复制
    两份、以后改一边忘了改另一边（CLAUDE.md「同一概念只能有一处实现」，
    这次撞的不是匹配逻辑，是 schema 定义本身）。

    ## herbs / formula / western_drugs 为什么还在，为什么变成"派生"

    这三个字段这一轮改造前是可以独立赋值的普通字段。保留它们**不是**为了兼容
    调用方少写代码，是因为三个真实消费方到今天还在直接读它们：
    herb_jaccard（分歧指标）、check_incompatible/check_thermal_consistency
    （X2 输出侧安全）、前端证据链侧栏。逼这三处都改成读
    `formula_candidates[selected].herb_items` 是这一轮不该碰的范围（M2/M5 才会
    真的用上 herb_items 的 dose/role/decoction），所以让新旧两种形状共存，
    但**只能有一份真相**：新形状（formula_candidates）永远是权威来源，旧形状
    在构造完成后立即从它派生，调用方不用（也不能）让两者手动保持同步。

    ## 两条 model_validator 各管一个方向

    `_synthesize_formula_candidates_from_legacy_fields`（before）：输入侧没给
    `formula_candidates` 时，从 `formula`/`herbs`/`western_drugs` 合成恰好一个
    候选方——这不是"让旧代码继续绕过新约束"，是给 M1 之前所有已存在的构造点
    （测试 fixture、CLI）一条不用逐个改写就能继续工作的迁移路径。真实 LLM 调用
    在 M3 改完 prompt 前也会走这条路：模型仍按老 schema 吐 herbs/formula，
    这里补一层，consult() 端到端行为在 M3 之前不变。

    `_derive_flat_fields`（after）：不管 formula_candidates 是怎么来的（LLM
    真输出的，还是上面合成的），一律从 `formula_candidates[selected]` 重新算出
    `formula`/`herbs`/`western_drugs`，覆盖掉调用方可能传入的任何旧值——
    "不要靠调用方记得同步"就是这条的字面意思。
    """

    disease: str | None = None  # 病名（M4 起才真正校验/匹配，这里先只是字段）
    syndrome: str
    reasoning: str
    # M6：患者模式用的通俗语言版推理——不能直接给患者看"肝木乘土，中焦气机
    # 不利"这种专业表述。在 prompt 里让模型跟 reasoning 一起生成（而不是
    # 事后再调一次 LLM 翻译）：模型生成时就知道要给两个版本，比事后翻译准，
    # 也省一次调用。
    #
    # 定义成 `str | None = None` 而不是必填：必填会让全项目现存的几十处
    # `S3Syndrome(...)` 旧式构造（测试 fixture、eval/、CLI）全部要补这个字段，
    # 而这个字段的安全属性不靠 schema 层的"必填"来保证——真正兜底的是
    # `api/main.py::_filter_s3_for_role`：patient 角色下，`reasoning_plain`
    # 为空时展示的是一句明确的占位说明，不会退回显示原始的 `reasoning`
    # 专业文本（那样会让"选做可选字段"这个决定悄悄破坏掉 M6 要守的安全
    # 边界）。真实 S3 prompt 会要求模型每次都给出这个字段（见
    # prompts/v1/s3_syndrome.yaml），schema 层的"可选"只是不逼旧构造点
    # 都跟着改，不是说这个字段在真实产出里可以随意缺失。
    reasoning_plain: str | None = None
    treatment_principle: str
    formula_candidates: list[FormulaCandidate] = Field(min_length=1, max_length=3)
    selected: int = 0  # 默认选第几个候选方；下面的 after 校验器负责越界检查
    # 以下三个保留，向后兼容，从 formula_candidates[selected] 派生——见类文档字符串
    formula: str | None = None
    herbs: list[str] = Field(default_factory=list)
    # 医家开方时也可能用西药（不只是医案原文里有），同 CaseStructured.western_drugs
    western_drugs: list[str] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _synthesize_formula_candidates_from_legacy_fields(cls, data):
        if not isinstance(data, dict) or "formula_candidates" in data:
            # 后一个条件是"键存在"，不是"值非空"：显式传 formula_candidates=[]
            # 必须让 pydantic 自己的 min_length=1 去拒绝它（这正是防幻觉约束要测的
            # 那种输入），不能被这里的合成逻辑悄悄补上一个候选方，把校验绕过去。
            # 前半个条件覆盖"根本不是 dict"（比如已构造好的实例）。
            return data
        herbs = list(data.get("herbs") or [])
        western = list(data.get("western_drugs") or [])
        names = herbs + western
        herb_items = [{"name": n} for n in names] or [{"name": _LEGACY_HERB_PLACEHOLDER}]
        data = dict(data)
        data["formula_candidates"] = [{
            "name": data.get("formula") or _LEGACY_FORMULA_PLACEHOLDER,
            "source": "composed",
            "confidence": "medium",
            "rationale": "由旧式扁平字段（herbs/formula/western_drugs）自动合成，"
                         "构造时未提供 formula_candidates。",
            "herb_items": herb_items,
        }]
        data.setdefault("selected", 0)
        return data

    @model_validator(mode="after")
    def _derive_flat_fields(self) -> "_S3Base":
        n = len(self.formula_candidates)
        if not (0 <= self.selected < n):
            raise ValueError(
                f"selected={self.selected} 越界：formula_candidates 共 {n} 个，"
                f"合法范围是 [0, {n - 1}]。"
            )
        cand = self.formula_candidates[self.selected]
        # 过滤掉合成占位符，让"完全没给任何药材/方名"时 .herbs == [] / .formula
        # is None 这两条旧默认值原样保留，见 _LEGACY_HERB_PLACEHOLDER 的注释。
        names = [item.name for item in cand.herb_items if item.name != _LEGACY_HERB_PLACEHOLDER]
        kept, moved = split_western_drugs(names)
        self.formula = None if cand.name == _LEGACY_FORMULA_PLACEHOLDER else cand.name
        self.herbs = kept
        self.western_drugs = moved
        return self


class S3Syndrome(_S3Base):
    # min_length=1：防幻觉的关键约束，不要改成可选。
    cited_case_ids: list[str] = Field(min_length=1)


class S3SyndromeUnreferenced(_S3Base):
    """检索不到任何相关医案（相似度全部低于阈值，或该医家没有医案）时用的 S3 schema。

    **没有 cited_case_ids 字段。** 这是 CLAUDE.md「某个新场景导致校验失败时，新建一个
    不含该字段的 schema，不是放松原来的约束」的落地：白名单为空时 S3Syndrome 的
    min_length=1 会逼模型编一个 id——要么必被判幻觉却照样出方，要么三次校验失败抛
    LLMError 让整个 consult 崩掉。S3Syndrome 本身一个字没动。
    """

    @property
    def cited_case_ids(self) -> list[str]:
        """让下游（幻觉检查、前端）按同一个接口读；这里永远是空——没有可引用的医案。
        是 property 不是字段：model_dump 里不会出现，api 层负责补一个空列表。"""
        return []
