"""全项目共用的 pydantic 数据模型。离线抽取和在线推理链都从这里取模型，不裸用 dict。"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from core.herbs import split_western_drugs
from core.theory import rule as _theory_rule

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
    # 总纲 2.5：这一诊的处方含十八反十九畏配伍（李可医案那类敢用反药的名家）。
    # 由 offline/extract_cases.py 用 core.safety_output.check_incompatible 在
    # 抽取边界上算好，不靠模型标——它是训练集的排除信号（export_sft.py 据此
    # 过滤），也是 README 里要说明的取舍：系统的安全层会拦这类配伍，数据里
    # 的反药配伍跟 M2 的检查直接冲突，进训练集等于教模型开反药。
    has_incompatible_pair: bool = False
    # R8-2 选项 ②：不在本项目脾胃门定位内的医案（王云启治癌验案录、李可肿瘤医案，
    # 见 data/local_corpora/MANIFEST.json 的 out_of_scope 声明）接进来但打标，
    # 训练导出默认排除（export_sft.py 的 filter_out_of_scope，--include-out-of-scope
    # 才带上）。跟 has_incompatible_pair 同一个形状：标记在数据边界上落好，过滤只
    # 看标记。**不是从证型覆盖率自动推的**——那个数（assess_case_scope）给人看，
    # 决定由人写进 MANIFEST。
    out_of_scope: bool = False
    # R18-G：这一例**讲的是哪个门类**（spleen_stomach / oncology / other，
    # offline/extract_cases_li_ke.classify_scope 从正文判）。
    #
    # 跟上面的 out_of_scope **不是同一个问题**（CLAUDE.md 第 31 条的例外，
    # 走例外必须写清区别）：
    #   out_of_scope 回答「这本书整体要不要进训练集」——人工写在
    #       data/local_corpora/MANIFEST.json 里，粒度是**整本书**；
    #   scope 回答「这一例的病在哪个门类」——从这一例的正文判，粒度是**一例**。
    # 李可那 57 例里有 2 例不是肿瘤，王云启 77 例全是肿瘤：整本书一刀切会把
    # 那 2 例一起切掉，而按 scope 过滤能留下它们。合成一个字段就只能二选一。
    #
    # None = 没判过（叶天士/吴鞠通那批抽取脚本不产出这个字段），**不是
    # "已判定为脾胃门"**——倒填一个猜的值会让 --exclude-scope 把没判过的也算进去。
    scope: str | None = None


# ---------- X3：医案三元组（S5 抽取，LLM 输出） ----------

# 谓词受控词表。R2 --limit 5 试水暴露：不限定谓词时模型实际吐出 15 种以上
# （起于/表现为/诊断为/脉象/病机为/病机/治法/治则/治以/方剂/用药/含/疗效/
# 服药后/后……），其中「病机为」跟「病机」、「治法」「治则」「治以」互为同义词，
# query_case_graph 的子串匹配对同义词无能为力——查「治以」查不到写成「治法」
# 的那些行。原先的设计（见 R2 之前这段文档字符串的历史版本）刻意不限定成
# 固定枚举，理由是"关系比证候-治法-方剂-药物这条链丰富得多"；R2 的真实抽取
# 证明这个理由站不住：模型没有用这份自由去表达更丰富的关系，只是把同一个
# 关系换着说法，外加把叙事/对话也塞进了谓词里（见下面 CaseTripleItem 的
# "只抽本例患者" 那条）。收紧成六个固定谓词后，narrative 那类关系
# （"认为"「用麻黄」"服此方后"）在 schema 层面直接不合法，不需要额外一层
# 叙事过滤器。
CaseTriplePredicate = Literal["提示", "属于", "治以", "用方", "含", "用药"]


class CaseTripleItem(BaseModel):
    """S5（offline/extract_case_triples.py）从一诊原文里抽出的一条三元组。
    s/p/o 全部要求非空——防幻觉约束：抽不出完整的三元组就不该抽这一条，
    不能用空字符串占位凑数。

    p（谓词）限定成 CaseTriplePredicate 六选一，schema 层用 Literal 强制、
    不只是在 prompt 里写"请从这六个里选"——模型给了表外谓词就该让 pydantic
    拒绝、走 core.llm 的重试（校验错误回灌），比抽完之后再清洗可靠：清洗
    只能删，删不掉的话脏数据已经进了 data/case_triples.jsonl，Literal 校验
    在数据落盘之前就拦住。

    s/o 不做 Literal 约束（症状/病机/证型/治法/方名/药名是开放词表，没法枚举），
    但**不能是指代词**（"此症""此病""本例""患者""病家"这类）——这类词在
    941 条医案里字面相同、语义不同，挂进图谱会被当成同一个节点，把所有医案
    的内容都连到它上面。这一条 schema 管不了（"此症"是合法的非空字符串），
    prompt 里明确要求、extract_case_triples.py 里再做一层运行时黑名单兜底
    （跟 source_span 的核验是同一个"prompt 说了不代表模型会听"的道理）。

    source_span 是这条三元组在原文里的出处片段，**必须能在传给模型的原文里
    逐字找到**——这一步不是 pydantic 能校验的（schema 只管字段非空，不知道
    "传给模型的原文"是什么），核验逻辑在 extract_case_triples.py 里，抽取后
    立刻做，验不过的三元组直接丢弃、不写进 data/case_triples.jsonl。
    """

    s: str = Field(min_length=1)
    p: CaseTriplePredicate
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
    唯一的消费者，字段名改了它就读不到数据，不能各写各的。

    p 跟 CaseTripleItem 用同一个 CaseTriplePredicate，不是各自定义一份——
    "谓词只能是这六个之一"是同一条约束，写两处以后改一处会漏。"""

    case_id: str
    physician: str
    s: str = Field(min_length=1)
    p: CaseTriplePredicate
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)


# ---------- 阶段二（药理层）：本草 / 方剂学三元组（S6/S7 抽取，LLM 输出） ----------
#
# 跟 X3 的医案三元组同一套形状（受控谓词 Literal、s/o/source_span 非空、
# source_span 逐字核验在 offline/extract_reference_triples.py 里做），理由也
# 一样：谓词不限定时模型会把同一个关系换着说法（见 CaseTriplePredicate 的
# 文档字符串），query_materia_medica 的字面匹配对同义词无能为力。
#
# 古籍与现代必须分开抽、分开存、分开返回（source: classic | modern）：古籍说
# "细辛，味辛温"，药典说"辛、温，归心肺肾经，1~3g"，术语体系和精度都不同，
# 混在一起会重演 λ1 那个"证型 0/116 对不上"的教训（SOURCES.md 第 7 节）。

MateriaMedicaPredicate = Literal["性味", "归经", "功效", "用量", "禁忌", "炮制"]
ReferenceSource = Literal["classic", "modern"]


class MateriaMedicaItem(BaseModel):
    """S6 从一段本草原文里抽出的一条 (药材, 谓词, 值, 出处)。"""

    s: str = Field(min_length=1)
    p: MateriaMedicaPredicate
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)


class MateriaMedicaExtraction(BaseModel):
    """S6 单次调用的输出。允许空列表——章节引言、目录页这类原文抽不出任何
    药材事实，不能因为"总要抽出点什么"就编。"""

    triples: list[MateriaMedicaItem] = Field(default_factory=list)


class MateriaMedicaRecord(BaseModel):
    """写进 data/materia_medica.jsonl 的最终形态：MateriaMedicaItem 补上
    source（古籍/现代）和 book（出自哪本）。字段名对齐 core/tools.py 的
    query_materia_medica() 读的格式——那份代码是这个格式的唯一消费者。"""

    s: str = Field(min_length=1)
    p: MateriaMedicaPredicate
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)
    source: ReferenceSource
    book: str = Field(min_length=1)


# 君臣佐使从教材来（总纲 2.3）：M1 的 HerbItem.role 现在靠模型标，准确率未知，
# 有了教材的标准答案才有对照。
FormularyPredicate = Literal["组成", "君药", "臣药", "佐药", "使药", "主治", "功用", "加减"]


class FormularyItem(BaseModel):
    """S7 从一段方剂学原文里抽出的一条 (方剂, 谓词, 值, 出处)。「组成」的 o
    写"药+剂量"（如「麻黄三两」），原文没写剂量就只写药名，不补。"""

    s: str = Field(min_length=1)
    p: FormularyPredicate
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)


class FormularyExtraction(BaseModel):
    triples: list[FormularyItem] = Field(default_factory=list)


class FormularyRecord(BaseModel):
    """写进 data/formulary.jsonl 的最终形态，形状同 MateriaMedicaRecord。"""

    s: str = Field(min_length=1)
    p: FormularyPredicate
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)
    source: ReferenceSource
    book: str = Field(min_length=1)


# ---------- R18-D：《脾胃论》立论层（确定性抽取，不是 LLM 输出） ----------

# 谓词受控，六选一，理由跟 CaseTriplePredicate 完全一样：不限定的话同一个关系
# 会有好几种写法（「治以」/「治法」/「当用」），下游的字面匹配对同义词无能为力。
#
# 这一层**不是 LLM 抽的**，跟 MateriaMedicaRecord 那套（S6/S7 真实模型调用）
# 分开看：产出文件落在 data/standard/ 下、要进版本控制，那就必须是任何人在任何
# 机器上重跑都能字字相同的确定性转换——原文里「如脉缓……此湿胜，从平胃散」这类
# 句式本身就是规整的条件-处置句，规则抽取够用，不需要模型，也就没有幻觉风险。
# source_span 仍然强制：它是**逐字**从原文截的那一句，
# offline/extract_rationale_pwl.py 落盘前核验 span 真的出现在原文里，
# 核验不过的整条丢弃并计数。规则抽取不会编造 span，但会因为切句边界写错而
# 截出一段原文里不存在的文字，这道核验拦的是那个。
RationalePredicate = Literal["病机", "治法", "用方", "加药", "去药", "禁忌"]


class RationaleRecord(BaseModel):
    """写进 data/standard/rationale_pwl.jsonl 的一条立论三元组。

    形状对齐 MateriaMedicaRecord（s/p/o/source_span/book），多一个 chapter：
    《脾胃论》同一个论断在不同篇里的分量不同（「脾胃胜衰论」是主论，
    「用药宜禁论」是禁忌专篇），丢掉篇名就没法说这条出自哪里。
    """

    s: str = Field(min_length=1)
    p: RationalePredicate
    o: str = Field(min_length=1)
    source_span: str = Field(min_length=1)
    chapter: str = Field(min_length=1)
    book: str = Field(min_length=1)


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
    # 这条证候是在哪个病名下定义的（比如"胃痛"门下的"脾胃虚寒证"）。R2 教材
    # 扩表之前没有这个字段——17 条手工条目都不绑病名，候选池小，先辨病再辨证
    # 收窄不了太多也不需要收窄。教材来源的条目一律带这个字段：同一个证候名
    # （比如"脾胃虚寒证"）在不同病名下的具体表现不完全一样，各自是独立的
    # (disease, syndrome) 组合，不去重合并——合并会丢掉"这条是哪个病下辨出来
    # 的"这个信息，而这正是 core.tools.syndrome_posterior 的 disease_hint
    # 要用来收窄候选池的锚点。旧的 17 条留 None，不倒填一个猜的病名。
    disease: str | None = None
    source: Literal[
        "gb_standard",
        "official_consensus",
        "group_standard",
        "journal",
        "secondary_verified",
        "manual",
        # 十四五规划教材（《中医内科学》等）。规则脚本从教材原文的"临床表现/
        # 证机概要"四元组抽取，零 LLM 调用——见
        # offline/build_syndrome_textbook.py。教材是公开出版、经同行评审的
        # 权威教学材料，可信度介于 journal 和 secondary_verified 之间，但
        # 这批条目目前没有交叉确认（只有教材这一个来源），不能标
        # secondary_verified（那档要求"内容与另一独立来源交叉确认一致"），
        # 所以单独开一档，不跟 secondary_verified 混在一起。
        "textbook",
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


class S1S2Merged(BaseModel):
    """R36：S1（症状标准化）+ S2（证素推断）合成一次调用的产出。

    **只是形状合一，语义一字未改**：`to_s1()` / `to_s2()` 拆出来的两个对象跟分两次
    调用拿到的逐字段同型，下游（残差、检索、S3、构图、前端）一行都不用改。

    为什么不是"把 S2Elements 塞成 S1Normalize 的一个子字段"：那样 `S1Normalize`
    这个类型就跟"这次合没合"耦合了，而它是全项目最上游的形状，改它会波及所有把
    S1 当参数的函数签名。新建一个只在边界上活的 schema，拆完就扔。

    **刻意不校验 `supporting_symptoms ⊆ symptoms`。** 分两次调用的那条路也不校验：
    模型经常把症状名改写（「胃脘胀痛」→「脘腹胀痛」），那件事由
    `core.chain.explained_symptoms()` 一处处理（见它的文档）。在这里加一条只在新路
    上生效的更严校验，会让两条路的证素质量不可比——而"合一之后证素质量变没变"
    正是 R38 要量的东西，不能先被一条校验改掉一次。
    """

    # 以下四个字段跟 S1Normalize 逐字段同型
    symptoms: list[str] = Field(default_factory=list)
    tongue: str | None = None
    pulse: str | None = None
    unmapped: list[str] = Field(default_factory=list)
    # 以下两个跟 S2Elements 逐字段同型（ElementHit.supporting_symptoms 的
    # min_length=1 防幻觉约束照旧生效——合并不放松任何约束）
    elements: list[ElementHit] = Field(default_factory=list)
    unexplained_symptoms: list[str] = Field(default_factory=list)

    def to_s1(self) -> S1Normalize:
        return S1Normalize(symptoms=list(self.symptoms), tongue=self.tongue,
                           pulse=self.pulse, unmapped=list(self.unmapped))

    def to_s2(self) -> S2Elements:
        return S2Elements(elements=list(self.elements),
                          unexplained_symptoms=list(self.unexplained_symptoms))


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


AdviceKind = Literal[
    "incompatible",            # 十八反十九畏（复用安全层的表）
    "over_dose",               # 超药典常用上限（复用安全层的 DOSE_LIMITS）
    "thermal_mismatch",        # 证型寒热方向与主方药性相悖
    "missing_channel_guide",   # 证型指向的病位上没有一味归该经的药
    "duplicate_effect",        # 两味药性味功效重合过多
]
AdviceSeverity = Literal["blocking", "warning", "suggestion"]


class Advice(BaseModel):
    """R23：对一张方的一条**建议**。产出它的逻辑一律在 core/formula_check.py，
    这里只定义字段（跟 FormulaSafety / DoseViolation 同一条边界，理由见那两处）。

    **这不是 LLM 输出 schema**，是确定性规则算出来的结果——同样的方、同样的
    数据文件，永远产出同样的 advice 列表。这一点决定了下面两个字段的松紧：

    - `reason` 是 `Field(min_length=1)`：一条说不出理由的建议等于没有建议，
      界面上会显示成一个空条目，人只会以为是 bug。
    - `source_span` 是 `str | None`：**不是防幻觉字段**。药理层那四个 schema 里
      `source_span` 必须非空，因为那些值是模型从原文里抽的、必须能回到原文核对；
      这里的值是规则自己算的，十八反/剂量两条能给出表里的出处，寒热/缺引经/
      重复三条**没有原文出处**——那时留 None 是如实，编一句"根据中医理论"才是
      假的（CLAUDE.md「防幻觉约束不许放松」管的是模型填的可验证事实，
      不是规则自己的判据）。

    `herbs` 允许为空：`missing_channel_guide` 说的是"**没有**这样一味药"，
    列不出涉及的药名是这条规则的本来形状，不是数据缺失。
    """

    kind: AdviceKind
    herbs: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    source_span: str | None = None
    severity: AdviceSeverity


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

    @property
    def selected_herb_items(self) -> list["HerbItem"]:
        """这次真正开出的那张方的逐味药条目，已剔除向后兼容合成的占位符。

        `.herbs` 是这同一份条目压平之后的药名列表（且西药已拆走），需要 `role` /
        `dose` / `decoction` 的消费方读这个 property，不要自己去
        `formula_candidates[selected]` 里翻——占位符该不该算一味药这件事只能有
        一处判断（R1 的分层 Jaccard 就踩在这上面：占位符若漏过滤，一张"没给任何
        药材"的方会被报成"有 1 味药、role 未标注"，n_unroled 凭空多一味）。

        西药**不在这里剔**：这个 property 的语义是"这张方的条目原样"，谁要剔谁
        自己剔（core/herbs.py 的分层剔、`.herbs` 由 _derive_flat_fields 剔）——
        在这里剔掉的话 M2 的剂量/煎法安全检查就看不到西药条目了。
        """
        cand = self.formula_candidates[self.selected]
        return [i for i in cand.herb_items if i.name != _LEGACY_HERB_PLACEHOLDER]

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


class DistillStep(BaseModel):
    """蒸馏产物里的一步。形状跟 `offline/export_sft.py` 的 `_step()` 一致——
    步骤名的合法集合由那边的 `CHAIN_STEPS` 管（一处实现），这里只保证非空。

    `output` 是 `str | list`：大多数步是一句结论，方剂→药材那一步是逐味药一项
    （`ITEMIZED_STEPS`）。两种形状共存不是省事，是数据本身的形状。
    """

    step: str = Field(min_length=1)
    output: str | list
    rationale: str | None = None
    source: str = Field(min_length=1)
    rationale_source: str | None = None


class DistillRecord(BaseModel):
    """R26：一条蒸馏样本（教师模型在 `full_context` 下对一条主诉、一位医家的输出）。

    **新建 schema，没有动任何既有字段**——CLAUDE.md 那条铁律要求的正是这个形状：
    新场景要的字段不一样时新建一个，不去放松 `S3Syndrome` 的约束。
    这里的 `Field(min_length=1)` 是同一套防幻觉思路：样本 id、医家、主诉、
    教师模型名、来源标签一个都不许是空串——空串会在训练集里变成"没有出处的样本"，
    而那正是蒸馏最容易悄悄引入的一类脏数据。

    `teacher_saw_source_case`：这条主诉的来源医案是不是就在教师自己的知识前缀里
    （医案那一半恒为 True，SDT 那一半恒为 False）。**下游必须分开统计**：
    教师抄自己语料抄得准，不等于它会推理。
    """

    sample_id: str = Field(min_length=1)
    source: Literal["sdt", "case"]
    physician: str = Field(min_length=1)
    complaint: str = Field(min_length=1)
    steps: list[DistillStep] = Field(min_length=1)
    teacher_model: str = Field(min_length=1)
    teacher_saw_source_case: bool
    case_refs: list[str] = Field(default_factory=list)


# ---------- R33：结构化推理链 S3′（S3Structured） ----------
#
# **为什么新建一整套 schema 而不是给 S3Syndrome 加字段。**
# 用户的要求（§0.1 原话）是「五位医家进行综合分析，**不要给出多个答案**」、
# 「一个专家诊断，给出解决方案、药方」。`S3Syndrome` 的形状恰好相反：它是
# **一位**医家给出 **2–3 个**候选方，三位医家并置成三列由人来比。两者不是同一件
# 产出，不是加几个可选字段能表达的差别——所以按 CLAUDE.md 那条铁律新建，
# `S3Syndrome` 与 `S3SyndromeUnreferenced` 一个字没动，`S3_MODE=legacy` 仍走它们。
#
# **五步链条来自申报书 2.1**：「病变脏腑-证型-治法-方剂-药物组成」。
# 每一步都显式声明自己的输入，由 `model_validator` 检查那个输入确实出现在上一步的
# 输出里——这就是「不可跳步」的可执行形式。**判据在 schema 层而不是 prompt 层**：
# prompt 只能请求模型别跳步，schema 能让跳了步的输出**根本构造不出来**，
# 于是 `generate()` 的两次重试会把校验错误回灌给模型（CLAUDE.md 那条重试约定）。


class OntologyRef(BaseModel):
    """一条本体引用：指向本草/方剂条目的某个谓词，并**带上原文片段**。

    `span` 是 `Field(min_length=1)` 而不是可选：一条"引用"如果说不出原文写了什么，
    它就不是引用，只是又一句模型自己的话。`herb_source_fabricated`（张冠李戴，
    R34 起，R59 从 `herb_grounded` 拆出来）和 `herb_source_paraphrased`（转述
    未照抄，R60 从 `herb_source_fabricated` 再拆出来）都要拿它当反例，空 span
    会让那条规则的反例变成空字符串——等于没有反例。

    `book` 可以为空：本体条目里有 `book` 的话模型应当照填，但模型看到的是知识块里
    的那一段，不一定带书名。**这个字段的真实性由 R34 回查本体核对**，不靠模型自觉，
    所以这里不设 `min_length=1`——设了只会逼模型编一个书名。
    """

    kind: Literal["herb", "formula"]
    name: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    span: str = Field(min_length=1)
    book: str | None = None


class OrganLocus(BaseModel):
    """第 1 步：病变脏腑。

    `supporting_symptoms` 必须非空——"病在脾"这个判断的依据只能是患者的症状，
    说不出依据的脏腑定位是这条链上第一个可以凭空出现的东西。
    """

    organ: str = Field(min_length=1)
    supporting_symptoms: list[str] = Field(min_length=1)
    pathogenesis: str = Field(min_length=1)


class SyndromeStep(BaseModel):
    """第 2 步：证型。`from_organs` 必须全部来自第 1 步（`S3Structured` 校验）。

    `reasoning_plain` 在这里是**必填**，跟 `_S3Base.reasoning_plain` 的可选不同：
    那边的可选是为了不逼几十处旧式构造都补字段（历史包袱），这套 schema 没有
    历史构造点，所以从一开始就要求给——患者模式下前端要显示的就是它。
    """

    name: str = Field(min_length=1)
    disease: str | None = None
    from_organs: list[str] = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    reasoning_plain: str = Field(min_length=1)


class MethodStep(BaseModel):
    """第 3 步：治法。`from_syndrome` 必须**逐字等于**第 2 步的证型名。

    `targets`：这个治法分别针对哪几条病机。第 5 步每味药的 `for_element` 要能在
    「第 1 步的脏腑」∪「这里的 targets」里找到，否则那味药是凭空加的。
    """

    principle: str = Field(min_length=1)
    from_syndrome: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)


class FormulaStep(BaseModel):
    """第 4 步：方剂。**只出一张**（`candidate` 是单个而不是 list）。

    §0.1 的「不要给出多个答案」落在这里：`S3Syndrome.formula_candidates` 是
    `min_length=1, max_length=3`，让模型给 2–3 个候选再由人挑；这套 schema 只收
    一张方，"挑"这件事由模型在第 3→4 步之间做完并在 `rationale` 里说明。
    """

    candidate: FormulaCandidate
    from_method: str = Field(min_length=1)
    ontology_refs: list[OntologyRef] = Field(default_factory=list)


class HerbChoice(BaseModel):
    """第 5 步：一味药**为什么**进这张方。

    **不是 `HerbItem` 的替代**：`item` 直接复用它（剂量/炮制/煎法/君臣佐使那套字段
    以及 M2 的剂量安全检查全部照旧生效，不在这里重写一份）。这个类加的是
    "开它的依据"——针对哪条病机、依据哪条功效、出自本体哪一段、受哪位医家影响。

    `ontology_refs` 默认空而不是 `min_length=1`：药理层数据不在的机器上
    （沙盒、新 clone）模型没有本体可引，要求必填会让 S3 直接跑不起来。
    **"有没有引到本体"是一个要被测量的比率**（R34 的 `herbs_grounded_ratio`），
    不是一个 schema 硬约束——把它设成硬约束，本体缺失时会退化成"模型编 span"，
    那比测出一个低比率糟得多。
    """

    item: HerbItem
    for_element: str = Field(min_length=1)
    effect_cited: str = Field(min_length=1)
    ontology_refs: list[OntologyRef] = Field(default_factory=list)
    physician_source: str | None = None


class PhysicianInfluence(BaseModel):
    """某位医家的思路在这条链的**哪一步**起了作用。

    `cited_case_ids` 是 `min_length=1`：声称"李可的思路影响了这一步"必须指得出
    是他哪一条医案。说不出医案的"influence"就是替那位医家背书他没说过的话
    ——这是整套「综合分析」里最容易出现的一类幻觉，因为它读起来最像学术表述。

    `physician` 存 **id**（`ye_tianshi` 而不是「叶天士」），跟全项目一致；
    边界上由 `core/physicians.py::resolve_physician_id` 解析（CLAUDE.md 那条
    「标识符只有一种规范形式」——ReAct 的 physician 参数已经踩过一次）。
    """

    physician: str = Field(min_length=1)
    step: Literal["organ", "syndrome", "method", "formula", "herbs"]
    contribution: str = Field(min_length=1)
    cited_case_ids: list[str] = Field(min_length=1)


#: 五步链条的步名，**顺序即依赖顺序**。`PhysicianInfluence.step` 的 Literal 用的是
#: 同一组字面量；改这里要同时改那个 Literal（两处写同一组值是 pydantic 的
#: Literal 不能引用变量所致，有一条测试钉住两者一致）。
S3_CHAIN_STEPS: tuple[str, ...] = ("organ", "syndrome", "method", "formula", "herbs")


def _check_no_step_skipping(organs, syndrome, method, formula, herb_choices) -> None:
    """五步链「不可跳步」的四条 + 一条来源校验的唯一实现。

    `_S3StructuredBase`（检索到医案时用）与 `S3Derived`（R52 演绎推导，
    看不到医案）共用这一份——两套 schema 字段形状不同（医案引用 vs 规则引用），
    但"上一步的结论有没有被下一步接住"是同一个问题，答案不能因为问的是哪个
    schema 而不同（CLAUDE.md「同一概念的匹配逻辑只能有一处实现」）。两边的
    字段名刻意保持一致（`organ`/`from_organs`/`from_syndrome`/`principle`/
    `from_method`/`candidate`/`herb_items`/`for_element`/`targets`），
    这份函数靠鸭子类型直接读，不关心传进来的是哪个类。
    """
    organ_names = {o.organ for o in organs}
    missing = [o for o in syndrome.from_organs if o not in organ_names]
    if missing:
        raise ValueError(
            f"第 2 步（证型）的 from_organs 里 {missing} 没有出现在第 1 步的病变脏腑 "
            f"{sorted(organ_names)} 里——证型必须从已经定位的脏腑推出来，不能跳步。"
        )
    if method.from_syndrome != syndrome.name:
        raise ValueError(
            f"第 3 步（治法）的 from_syndrome={method.from_syndrome!r} "
            f"跟第 2 步的证型 {syndrome.name!r} 不一致（要求逐字相同，"
            "不接受「上述证型」这类指代——那样等于没有接住上一步的结论）。"
        )
    if formula.from_method != method.principle:
        raise ValueError(
            f"第 4 步（方剂）的 from_method={formula.from_method!r} "
            f"跟第 3 步的治法 {method.principle!r} 不一致（要求逐字相同）。"
        )
    in_formula = {i.name for i in formula.candidate.herb_items}
    explained = {c.item.name for c in herb_choices}
    unexplained = sorted(in_formula - explained)
    extraneous = sorted(explained - in_formula)
    if unexplained or extraneous:
        # **两个方向一起报**，不是先报一个。这条错误会被 generate() 回灌给模型
        # 重试，只报一半的话它改完一半再撞另一半，白花一次重试——而重试只有两次。
        parts = []
        if unexplained:
            parts.append(
                f"方里有 {unexplained} 但 herb_choices 里没有给出用药理由"
                "（每一味开出去的药都要说得出针对哪条病机、依据哪条功效）")
        if extraneous:
            parts.append(
                f"herb_choices 里的 {extraneous} 并不在这张方的 herb_items 里"
                "（给一味没开的药写理由，说明这两处对不上，不是多写了几句）")
        raise ValueError(
            "herb_choices 与 formula.candidate.herb_items 的药名集合必须完全一致："
            + "；".join(parts) + "。"
        )
    allowed = organ_names | set(method.targets)
    stray = sorted({c.for_element for c in herb_choices} - allowed)
    if stray:
        raise ValueError(
            f"这几味药的 for_element {stray} 既不是第 1 步的病变脏腑、"
            f"也不是第 3 步治法的 targets（可选：{sorted(allowed)}）——"
            "针对一条没被辨出来的病机加药，就是无依据的加减。"
        )


class _S3StructuredBase(BaseModel):
    """`S3Structured` 与 `S3StructuredUnreferenced` 共享的五步链与「不可跳步」校验。

    分基类的形状**照抄 `_S3Base` / `S3Syndrome` / `S3SyndromeUnreferenced`**：
    检索为空时用一个不含引用字段的子类，而不是把 `min_length=1` 放松掉
    （CLAUDE.md 那条铁律）。项目里这个模式已经有一处，这里用同一个形状而不是
    另发明一种——两种写法并存的话，下次改防幻觉约束的人要读懂两套。

    ## 五步链条来自申报书 2.1

    「病变脏腑-证型-治法-方剂-药物组成」。每一步显式声明自己的输入，
    由校验器检查那个输入确实出现在上一步的输出里。

    ## 「不可跳步」的四条 + 一条来源校验

    1. `syndrome.from_organs` ⊆ 第 1 步给出的脏腑名
    2. `method.from_syndrome` == `syndrome.name`（**逐字**）
    3. `formula.from_method` == `method.principle`（**逐字**）
    4. `herb_choices` 的药名集合 == `formula.candidate.herb_items` 的药名集合
       （**双向**：方里有的药必须说得出理由，说了理由的药必须真的在方里）
    5. 每味药的 `for_element` 落在「第 1 步脏腑 ∪ 第 3 步 targets」里

    为什么 2/3 是逐字相等而不是"包含"：允许包含的话模型可以把 `from_syndrome`
    写成「上述证型」，校验照样通过——而那正是跳步，这一步并没有真的接住上一步的
    结论，只是提了一句。

    **判据在 schema 层而不是 prompt 层**：prompt 只能请求模型别跳步，schema 能让
    跳了步的输出**根本构造不出来**，于是 `generate()` 的两次重试会把具体的校验
    错误回灌给模型（CLAUDE.md 那条重试约定）。
    """

    organs: list[OrganLocus] = Field(min_length=1)
    syndrome: SyndromeStep
    method: MethodStep
    formula: FormulaStep
    herb_choices: list[HerbChoice] = Field(min_length=1)
    note: str | None = None

    @model_validator(mode="after")
    def _no_step_skipping(self) -> "_S3StructuredBase":
        _check_no_step_skipping(self.organs, self.syndrome, self.method, self.formula, self.herb_choices)
        return self

    # ---- 派生视图：下游一个调用方都不用改 ----

    @property
    def physicians_cited(self) -> list[str]:
        """这次综合分析里真的说出了贡献的医家 id，按首次出现排序。"""
        out: list[str] = []
        for inf in self.physician_influences:
            if inf.physician not in out:
                out.append(inf.physician)
        return out

    @property
    def ontology_refs(self) -> list["OntologyRef"]:
        """全链条上的本体引用（方级 + 药级），去重保序。R34 回查本体、
        R37 的节点释义都读这个，不各自去遍历一遍嵌套结构。"""
        out: list[OntologyRef] = []
        seen: set[tuple] = set()
        for ref in [*self.formula.ontology_refs,
                    *(r for c in self.herb_choices for r in c.ontology_refs)]:
            key = (ref.kind, ref.name, ref.predicate, ref.span)
            if key not in seen:
                seen.add(key)
                out.append(ref)
        return out

    def herbs_grounded_ratio(self) -> float:
        """带本体引用的药味占比。R34 要报的三个指标之一。

        分母是 `herb_choices` 的条数而不是 `herb_items`——两者在校验通过后必然
        相等（上面第 4 条），用前者是因为"有没有引本体"这件事记在 choice 上。

        **这个比率为 0 有两种完全不同的原因**：药理层数据不在（模型没有本体可引），
        或者本体在、模型就是没引。两者要靠 manifest 的
        `knowledge_entries.available` 分开，光看这个比率分不出来。
        """
        if not self.herb_choices:
            return 0.0
        grounded = sum(1 for c in self.herb_choices if c.ontology_refs)
        return grounded / len(self.herb_choices)

    def to_s3_syndrome(self) -> "_S3Base":
        """转成下游认识的 `S3Syndrome`（或检索为空时的 `S3SyndromeUnreferenced`）。

        **只此一处实现**——api / 前端 / 分歧度 / 安全层读的都是 `S3Syndrome`，
        让每个调用方各自从结构化对象里取字段等于把这一跳抄五遍（第 31 条）。

        引用为空时返回 `S3SyndromeUnreferenced` 而不是给 `S3Syndrome` 塞一个假 id：
        那一步会把「这次没有任何医案支撑」这个信号洗掉，而它正是前端要明示的东西。

        `formula_candidates` 恰好一个元素：结构化模式**只出一张方**，`selected`
        因此恒为 0。`reasoning` 里追加了五步链条与五家影响的摘要——旧界面的证据链
        侧栏读的是 `reasoning`，不追加的话「融合了五家」这件事在 R37 之前
        完全看不见（新字段存在但没有界面读它，等于没做）。
        """
        chain_lines = [
            "病变脏腑：" + "；".join(
                f"{o.organ}（{'、'.join(o.supporting_symptoms)} → {o.pathogenesis}）"
                for o in self.organs),
            f"证型：{self.syndrome.name}（自 {'、'.join(self.syndrome.from_organs)}）",
            f"治法：{self.method.principle}（针对 {'、'.join(self.method.targets)}）",
            f"方剂：{self.formula.candidate.name}",
        ]
        parts = [self.syndrome.reasoning, "", "—— 五步链条 ——", *chain_lines]
        if self.physician_influences:
            parts += ["", "—— 名医思路影响 ——", *(
                f"【{inf.physician}·{inf.step}】{inf.contribution}"
                f"（医案：{'、'.join(inf.cited_case_ids)}）"
                for inf in self.physician_influences)]
        common = dict(
            disease=self.syndrome.disease,
            syndrome=self.syndrome.name,
            reasoning="\n".join(parts),
            reasoning_plain=self.syndrome.reasoning_plain,
            treatment_principle=self.method.principle,
            formula_candidates=[self.formula.candidate],
            selected=0,
            note=self.note,
        )
        if self.cited_case_ids:
            return S3Syndrome(cited_case_ids=list(self.cited_case_ids), **common)
        return S3SyndromeUnreferenced(**common)


class S3Structured(_S3StructuredBase):
    """S3′：五位医家融合后的**一份**结构化诊断。检索到了医案时用这个。

    跟 `S3Syndrome` 的关系是**并列而非继承**：两者字段形状不同（一张方 vs 2–3 个
    候选、五步链 vs 扁平结论），继承会让其中一个的约束污染另一个。

    这个子类比基类多两个字段，各多一条校验：
      - `physician_influences`（`min_length=1`）：一份"五家综合"至少要说清一家的
        贡献。给不出任何一家的影响，这就不是综合分析，是模型自己开了个方。
      - `cited_case_ids`（`min_length=1`）：跟 `S3Syndrome` 同一条防幻觉约束。
      - 校验：每条 influence 引的医案 id 必须落在顶层 `cited_case_ids` 里
        （也就是必须确实被检索到了）。
    """

    physician_influences: list[PhysicianInfluence] = Field(min_length=1)
    cited_case_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _influences_cite_retrieved_cases(self) -> "S3Structured":
        pool = set(self.cited_case_ids)
        for inf in self.physician_influences:
            bad = [cid for cid in inf.cited_case_ids if cid not in pool]
            if bad:
                raise ValueError(
                    f"{inf.physician} 在 {inf.step} 这一步引的医案 {bad} 不在本次的 "
                    f"cited_case_ids 里——医家影响必须指得出医案，且那条医案必须是"
                    "这次真的检索到并引用了的。"
                )
        return self


class S3StructuredUnreferenced(_S3StructuredBase):
    """一条相关医案都检索不到时用的结构化 schema。

    **没有 `cited_case_ids`，也没有 `physician_influences`。** 后者一并去掉的理由
    跟前者是同一条：一条"医家影响"必须指得出那位医家的某条医案（`min_length=1`），
    而这个场景下一条医案都没有——留着这个字段只会逼模型编一个 id 出来，
    那正是 `S3SyndromeUnreferenced` 当初要解决的问题。

    五步链的校验一条没少：没有医案可引，不等于可以跳步。
    """

    @property
    def cited_case_ids(self) -> list[str]:
        """让下游按同一个接口读；这里永远是空——没有可引用的医案。
        是 property 不是字段：`model_dump` 里不会出现。"""
        return []

    @property
    def physician_influences(self) -> list["PhysicianInfluence"]:
        """同上，永远是空——说不出医案的"影响"这个 schema 不收。"""
        return []


# ---------- R52：第一相·演绎推导。看不到任何医案，依据换成医理规则 ----------
#
# `S3Structured`/`S3StructuredUnreferenced` 靠"检索到的医案"防幻觉：说不出
# 医案 id 就不收。这一相反过来——prompt 里从设计上就不给医案，模型只能靠
# R51 医理规则层（`core/theory.py`）与本体（`core/ontology.py`）推导，
# 所以防幻觉换成"说不出规则 id 就不收，说不出规则也可以，但必须显式承认"。
# 这不是把约束改松：`cited_case_ids: Field(min_length=1)` 变成了
# `rule_refs` 非空 **或** `insufficient` 非空的二选一校验，能接受的值集合
# 变小了（多了"必须显式声明缺口"这条），不是变可选。


class TheoryRef(BaseModel):
    """一条医理规则引用：指向 `core/theory.py` 里的一条规则，并带上引用理由。

    `rule_id` 必须真实存在——校验器直接回查 `core.theory.rule()`，查不到
    就在第一次构造时拒绝，不用等到人工审查才发现是编的 id。跟
    `OntologyRef.span` 是同一条防幻觉纪律：一条"依据"如果指不出真实存在的
    东西，就不是依据，是模型自己的话。
    """

    rule_id: str = Field(min_length=1)
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _rule_must_exist(self) -> "TheoryRef":
        if _theory_rule(self.rule_id) is None:
            raise ValueError(
                f"rule_refs 引用的规则 id {self.rule_id!r} 在医理规则层（core/theory.py）"
                "查不到——演绎推导的每一步依据必须指向一条真实存在的规则，不能凭空编一个 id。"
            )
        return self


class InsufficientNote(BaseModel):
    """某一步在医理规则层里确实找不到可引的规则时，显式声明"依据不足"，
    而不是留空或编一条规则凑数。

    `missing_rule_kind` 说清楚缺的是哪一类规则，跟 R51 `core/theory.py` 的
    四个查询接口一一对应——R57 消融实验要按这个字段统计"到底缺什么"，
    不是笼统一句"没查到"。
    """

    what: str = Field(min_length=1)
    missing_rule_kind: Literal[
        "organ_relation", "pathomechanism", "treatment_principle", "compatibility"
    ]


def _require_rule_refs_or_insufficient(step_label: str, rule_refs: list, insufficient) -> None:
    """`rule_refs` 非空 或 `insufficient` 非空——二选一，不能两者都不给。

    五个 Derived 步骤类共用同一条判据（CLAUDE.md「同一概念的匹配逻辑只能有
    一处实现」）：判断"这一步有没有交代依据"跟具体是脏腑/证型/治法/方剂/
    哪一味药无关，只是错误信息里要带上步骤名。
    """
    if not rule_refs and insufficient is None:
        raise ValueError(
            f"{step_label}既没有给 rule_refs 也没有标 insufficient——"
            "演绎推导的每一步要么指得出依据的医理规则，要么显式承认「依据不足」，"
            "不能两者都不给（那样就是凭记忆编一个结论，跟看医案模仿没有区别）。"
        )


class OrganLocusDerived(BaseModel):
    """演绎推导第 1 步：病变脏腑。跟 `OrganLocus` 同形状（`supporting_symptoms`
    仍然必填——脏腑定位必须落在患者症状上，这条跟依据来自哪里无关），
    但换成医理规则（`rule_refs`，通常引藏象关系 `organ_relation`）做依据，
    不是模型看着医案模仿出来的"看起来像"。
    """

    organ: str = Field(min_length=1)
    supporting_symptoms: list[str] = Field(min_length=1)
    pathogenesis: str = Field(min_length=1)
    rule_refs: list[TheoryRef] = Field(default_factory=list)
    insufficient: InsufficientNote | None = None

    @model_validator(mode="after")
    def _cites_or_flags(self) -> "OrganLocusDerived":
        _require_rule_refs_or_insufficient(
            f"第 1 步（病变脏腑 {self.organ!r}）", self.rule_refs, self.insufficient)
        return self


class SyndromeStepDerived(BaseModel):
    """演绎推导第 2 步：证型。`from_organs` 校验跟结构化模式一样（不可跳步），
    依据通常引病机传变 `pathomechanism`。"""

    name: str = Field(min_length=1)
    disease: str | None = None
    from_organs: list[str] = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    reasoning_plain: str = Field(min_length=1)
    rule_refs: list[TheoryRef] = Field(default_factory=list)
    insufficient: InsufficientNote | None = None

    @model_validator(mode="after")
    def _cites_or_flags(self) -> "SyndromeStepDerived":
        _require_rule_refs_or_insufficient("第 2 步（证型）", self.rule_refs, self.insufficient)
        return self


class MethodStepDerived(BaseModel):
    """演绎推导第 3 步：治法。依据通常引治则推导 `treatment_principle`
    （`core/theory.py::principles_for` 按证型的 nature/location 查出来的那批）。"""

    principle: str = Field(min_length=1)
    from_syndrome: str = Field(min_length=1)
    targets: list[str] = Field(min_length=1)
    rule_refs: list[TheoryRef] = Field(default_factory=list)
    insufficient: InsufficientNote | None = None

    @model_validator(mode="after")
    def _cites_or_flags(self) -> "MethodStepDerived":
        _require_rule_refs_or_insufficient("第 3 步（治法）", self.rule_refs, self.insufficient)
        return self


class FormulaStepDerived(BaseModel):
    """演绎推导第 4 步：方剂。`ontology_refs` 照抄 `FormulaStep`（本体引用，
    跟"依据哪条医理规则"是两件事，不合并）；`rule_refs` 通常引配伍理论
    `compatibility`（君臣佐使结构、药对配伍）。

    自拟方（`FormulaCandidate.source="composed"`）不用改 schema 就能表达：
    `name` 仍然必填，模型给一个描述性方名（如"健脾理气方"）即可，
    `base_formula` 留空由 `FormulaCandidate._check_base_formula` 校验。
    """

    candidate: FormulaCandidate
    from_method: str = Field(min_length=1)
    ontology_refs: list[OntologyRef] = Field(default_factory=list)
    rule_refs: list[TheoryRef] = Field(default_factory=list)
    insufficient: InsufficientNote | None = None

    @model_validator(mode="after")
    def _cites_or_flags(self) -> "FormulaStepDerived":
        _require_rule_refs_or_insufficient("第 4 步（方剂）", self.rule_refs, self.insufficient)
        return self


class HerbChoiceDerived(BaseModel):
    """演绎推导第 5 步：一味药为什么进这张方。

    **没有 `physician_source`**——`HerbChoice.physician_source` 是"这味药的
    用法受哪位医家影响"，是这一相要从设计上消除的东西（R52 §0：`cited_case_ids`
    / `physician_influences` / `physician_source` / `dose_evidence` 一起去掉，
    不是留着不填）。`ontology_refs` 保留（本体引用，跟"是不是模仿某位医家"
    是两件事）。
    """

    item: HerbItem
    for_element: str = Field(min_length=1)
    effect_cited: str = Field(min_length=1)
    ontology_refs: list[OntologyRef] = Field(default_factory=list)
    rule_refs: list[TheoryRef] = Field(default_factory=list)
    insufficient: InsufficientNote | None = None

    @model_validator(mode="after")
    def _cites_or_flags(self) -> "HerbChoiceDerived":
        _require_rule_refs_or_insufficient(
            f"第 5 步（用药 {self.item.name!r}）", self.rule_refs, self.insufficient)
        return self


class S3Derived(BaseModel):
    """R52 第一相：演绎推导的结果。**看不到任何医案**——`prompts/v1/s3_derived.yaml`
    全文没有参考医案块，模型只能依据 R51 医理规则层与本体推导；说不出依据
    必须显式 `insufficient`，不能像检索模式那样退回"编一段像医案的话"。

    跟 `_S3StructuredBase` 是**并列而非继承**（CLAUDE.md：不同字段形状继承
    会让一边的校验污染另一边）：这里没有 `cited_case_ids`、没有
    `physician_influences`，替换成 `rule_refs`/`insufficient`。

    五步链「不可跳步」的校验跟结构化模式**共用同一份实现**
    （`_check_no_step_skipping`）：看不到医案不等于可以跳步，这条约束
    跟"依据来自哪里"是两个维度，不能因为换了防幻觉手段就连带放松。

    `to_s3_syndrome()` 恒返回 `S3SyndromeUnreferenced`——这一相的产出天然
    没有 `cited_case_ids`；R54 医案佐证是独立的第三相，事后在旁路补充，
    不回头改这里（"绝不回头改推导"是 R54 的硬约束，这个方法从第一相起
    就没有留一个能被第三相塞值进来的字段）。
    """

    organs: list[OrganLocusDerived] = Field(min_length=1)
    syndrome: SyndromeStepDerived
    method: MethodStepDerived
    formula: FormulaStepDerived
    herb_choices: list[HerbChoiceDerived] = Field(min_length=1)
    note: str | None = None

    @model_validator(mode="after")
    def _no_step_skipping(self) -> "S3Derived":
        _check_no_step_skipping(self.organs, self.syndrome, self.method, self.formula, self.herb_choices)
        return self

    # ---- 派生视图：跟 `_S3StructuredBase` 同名同形状，下游按 hasattr 判断即可 ----

    @property
    def ontology_refs(self) -> list["OntologyRef"]:
        """全链条上的本体引用（方级 + 药级），去重保序。跟
        `_S3StructuredBase.ontology_refs` 是同一段逻辑（字段名相同），
        没有再抽公共函数是因为总共只有这一处重复、且两边就地读 self 的写法
        比额外传参更直接——抽出来反而要多传 5 个位置参数。"""
        out: list[OntologyRef] = []
        seen: set[tuple] = set()
        for ref in [*self.formula.ontology_refs,
                    *(r for c in self.herb_choices for r in c.ontology_refs)]:
            key = (ref.kind, ref.name, ref.predicate, ref.span)
            if key not in seen:
                seen.add(key)
                out.append(ref)
        return out

    @property
    def rule_refs(self) -> list["TheoryRef"]:
        """全链条上引用的医理规则，去重保序。R57 消融实验的 rule_refs 完整度
        指标、前端「本例知识地图」都读这个，不各自遍历一遍嵌套结构。"""
        out: list[TheoryRef] = []
        seen: set[tuple] = set()
        items = [*self.organs, self.syndrome, self.method, self.formula, *self.herb_choices]
        for it in items:
            for ref in it.rule_refs:
                key = (ref.rule_id, ref.note)
                if key not in seen:
                    seen.add(key)
                    out.append(ref)
        return out

    @property
    def insufficient_notes(self) -> list["InsufficientNote"]:
        """哪几条断言标了"依据不足"。前端要如实展示，不是藏起来。"""
        items = [*self.organs, self.syndrome, self.method, self.formula, *self.herb_choices]
        return [it.insufficient for it in items if it.insufficient is not None]

    def derivation_completeness_ratio(self) -> float:
        """演绎链上"给出了 rule_refs 而非 insufficient"的条目占比。

        分母是链上每一条独立断言（每个脏腑定位、证型、治法、方剂、每味药），
        不是固定按 5 步算：脏腑与药味本身是列表，按 5 步算的话一步里有一条
        不够、九条够，也会被算成这步"不够"，会把真实比例压低或抬高，跟
        `herbs_grounded_ratio` 选"按条目而不是按步"是同一个理由。
        """
        items = [*self.organs, self.syndrome, self.method, self.formula, *self.herb_choices]
        if not items:
            return 0.0
        grounded = sum(1 for it in items if it.rule_refs)
        return grounded / len(items)

    def herbs_grounded_ratio(self) -> float:
        """带本体引用的药味占比。跟 `_S3StructuredBase.herbs_grounded_ratio`
        同一段逻辑（字段名相同：`herb_choices[].ontology_refs`），`core/
        formula_verifier.py::herbs_grounded_ratio(s3)` 转发到这个方法，
        `verifier_metrics` 靠它拿 R34 三指标之一——两套 schema 都要有这个方法，
        R53 把符号验证扩到医理一致性时不用再判一次"这是哪种 schema"。
        """
        if not self.herb_choices:
            return 0.0
        grounded = sum(1 for c in self.herb_choices if c.ontology_refs)
        return grounded / len(self.herb_choices)

    def to_s3_syndrome(self) -> "S3SyndromeUnreferenced":
        """转成下游认识的 `S3SyndromeUnreferenced`——只此一处实现
        （第 31 条：同一概念的匹配/转换逻辑只能有一处）。

        恒是 `Unreferenced` 变体，不是 `S3Syndrome`：后者要求非空
        `cited_case_ids`，而演绎推导天生没有——这不是"退化成没有医案"，
        是这一相设计上就不该有。
        """
        chain_lines = [
            "病变脏腑：" + "；".join(
                f"{o.organ}（{'、'.join(o.supporting_symptoms)} → {o.pathogenesis}）"
                for o in self.organs),
            f"证型：{self.syndrome.name}（自 {'、'.join(self.syndrome.from_organs)}）",
            f"治法：{self.method.principle}（针对 {'、'.join(self.method.targets)}）",
            f"方剂：{self.formula.candidate.name}",
        ]
        parts = [self.syndrome.reasoning, "", "—— 五步链条（演绎推导，未参考医案） ——", *chain_lines]
        refs = self.rule_refs
        if refs:
            parts += ["", "—— 医理依据 ——", *(f"【{r.rule_id}】{r.note}" for r in refs)]
        notes = self.insufficient_notes
        if notes:
            parts += ["", "—— 依据不足 ——", *(f"{n.what}（缺 {n.missing_rule_kind}）" for n in notes)]
        return S3SyndromeUnreferenced(
            disease=self.syndrome.disease,
            syndrome=self.syndrome.name,
            reasoning="\n".join(parts),
            reasoning_plain=self.syndrome.reasoning_plain,
            treatment_principle=self.method.principle,
            formula_candidates=[self.formula.candidate],
            selected=0,
            note=self.note,
        )


# ---------- R35：名医用药规律（确定性统计，不是 LLM 输出） ----------
#
# 跟 `RationaleRecord` 同一类：产出文件落 `data/standard/` 要进版本控制，
# 所以必须是任何人在任何机器上重跑都字字相同的确定性转换。这一层**零 LLM 调用**
# ——它是对 `cases.json` 的计数与统计，没有任何生成环节，也就没有幻觉风险。
#
# **但它有另一类风险：把统计巧合说成"名医经验"。** 两味药在 3 张方里一起出现过，
# 不构成"某位医家习惯用这个药对"。所以每一条都必须带：
#   `support`（几张方支持它）和 `case_ids`（**具体是哪几张**）
# 缺了 `case_ids` 的规律无法回查，等于一句没有出处的话——跟 `cited_case_ids`
# 是同一条防幻觉纪律，所以同样是 `Field(min_length=1)`。

PatternKind = Literal["herb", "herb_pair", "dose", "modification"]

#: 规律按什么分组。
#:   physician            这位医家的总体习惯（`group_value` 为空串）
#:   physician_syndrome   这位医家在某个证下的习惯（`group_value` 是证型名）
#: **两档都要有**：`cases.json` 里 1075 诊次只有 116 条标了证型（10.8%），
#: 只按证型分组的话绝大多数医案的信息进不了规律层；只按医家分组则丢掉了
#: "他在这个证下怎么用药"这个更有用的粒度。
PatternGroupBy = Literal["physician", "physician_syndrome"]


class PrescribingPattern(BaseModel):
    """写进 `data/standard/prescribing_patterns.jsonl` 的一条用药规律。

    字段形状对齐 `core/context_prefix.py::_focused_pattern_block` 读的那几个键
    ——知识块要把它渲染给模型看，两处对不上的话规律会渲染成一行空白。

    `dose_*` 三个字段只有 `kind="dose"` 时才有值：`cases.json` 的 `herbs` 是**药名
    列表**，没有结构化剂量，剂量要从 `raw` 原文里按"药名 + 数字 + 单位"抓
    （见 `offline/mine_prescribing_patterns.py`）。抓不到就是 None，**不猜**。

    `has_incompatible_pair`：这条规律涉及的药里有没有十八反十九畏的一对。
    **不是过滤掉而是标出来**——古籍医案里真的有这种配伍（那是历史事实），
    删掉等于篡改语料；标出来才能让下游（知识块、SFT 导出）决定怎么处理。
    判据复用 `core.safety_output.INCOMPATIBLE_PAIRS`，说明文本复用
    `INCOMPATIBLE_TRAINING_NOTE`，不另写一套。
    """

    pattern_id: str = Field(min_length=1)
    kind: PatternKind
    physician: str = Field(min_length=1)
    physician_name: str = Field(min_length=1)
    group_by: PatternGroupBy
    #: 允许空串：`group_by="physician"` 时它就是空的（这位医家的总体习惯）。
    #: 不设 `min_length=1` 是**刻意的**，不是放松约束——它承载的是"分组的值"，
    #: 而"按医家分组"这个分法本来就没有第二层值。
    group_value: str = ""
    #: min_length=1：一条规律至少牵涉一味药。
    herbs: list[str] = Field(min_length=1)
    #: 几张方支持它。**下游引用这条规律时必须同时引这个数**——
    #: 「叶天士常用党参」和「叶天士在 3 张方里用过党参」是两句不同的话。
    support: int = Field(ge=1)
    #: min_length=1：说不出是哪几张方的规律无法回查，等于一句没有出处的话。
    case_ids: list[str] = Field(min_length=1)
    dose_median_g: float | None = None
    dose_min_g: float | None = None
    dose_max_g: float | None = None
    note: str | None = None
    has_incompatible_pair: bool = False

    @model_validator(mode="after")
    def _support_matches_case_ids(self) -> "PrescribingPattern":
        if self.support != len(self.case_ids):
            raise ValueError(
                f"support={self.support} 跟 case_ids 的条数 {len(self.case_ids)} 不一致"
                "——support 就是「有几张方支持它」，两个数对不上说明统计过程里丢了东西"
            )
        if self.kind == "dose" and self.dose_median_g is None:
            raise ValueError(
                'kind="dose" 的规律必须有 dose_median_g，否则它不是一条剂量规律'
            )
        if self.kind == "herb_pair" and len(self.herbs) != 2:
            raise ValueError(
                f'kind="herb_pair" 必须恰好两味药，实际 {len(self.herbs)} 味'
            )
        return self


# ---------- R46 §7.2：「人」这一维 ----------
#
# 对标黄煌的「方—病—人」模式：同一个证，老人/小儿/孕妇/肝肾功能不全者的用药
# 不是同一张方。此前这条链上完全没有"人"——只有症状、证素、证型、方。
#
# **新增 schema，不动既有的任何一个字段**（CLAUDE.md 那条铁律：防幻觉约束不
# 许放松，某个新场景要不同的形状就新建一个 schema，不是把旧的改松）。

#: 体质倾向。**不是自由文本**：九种体质是《中医体质分类与判定》的固定分类，
#: 留成字符串的话模型会写出"偏寒"这种不在任何表里的词，而下游要拿它去查规则。
Constitution = Literal[
    "平和质", "气虚质", "阳虚质", "阴虚质", "痰湿质",
    "湿热质", "血瘀质", "气郁质", "特禀质",
]

#: 生理阶段。剂量折算与禁忌规则按这个分派（儿童折算、妊娠禁忌、老年慎峻药）。
LifeStage = Literal["婴幼儿", "儿童", "青少年", "成人", "老年", "妊娠期", "哺乳期"]


class PatientProfile(BaseModel):
    """患者的「人」维。**全部字段可空**——门诊现场未必问得全，
    而一个"必须填满才能辨证"的表单在诊室里会被绕过去（写个假年龄），
    那比留空更糟。

    这里没有一个 `Field(min_length=1)`：**它不是模型的输出**，是人填的表单，
    防幻觉约束管的是"模型说的话要有出处"，跟这张表无关。
    """

    age_years: int | None = Field(default=None, ge=0, le=130)
    sex: Literal["男", "女"] | None = None
    life_stage: LifeStage | None = None
    constitution: Constitution | None = None
    #: 基础病、过敏史、在服药物：自由文本列表，医师现场写什么就是什么。
    comorbidities: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    current_medications: list[str] = Field(default_factory=list)
    #: 肝肾功能不全：只收「有/无/不详」三态，不收检验数值。
    #: **这是 §0.4 的输入侧边界**——一旦收 ALT/肌酐这类客观数据，
    #: 产品性质从"对患者主诉与病历文本推理"变成"分析客观数据"，
    #: 监管属性随之改变，要按医疗器械注册。
    hepatic_impairment: Literal["有", "无", "不详"] = "不详"
    renal_impairment: Literal["有", "无", "不详"] = "不详"

    def is_empty(self) -> bool:
        """一个字段都没填。调用方据此决定"这一次有没有人维可用"——
        跟"填了但都是不详"是两件事。"""
        return not any([
            self.age_years is not None, self.sex, self.life_stage,
            self.constitution, self.comorbidities, self.allergies,
            self.current_medications,
        ]) and self.hepatic_impairment == "不详" and self.renal_impairment == "不详"


#: 个体化调整的类别。**`Literal` 而不是自由字符串**：下游要按类别分组显示，
#: 也要按类别查"这一类调整有没有本体依据"。
AdjustmentKind = Literal["剂量", "去药", "加药", "换药", "煎服法", "慎用提示"]


class IndividualizationItem(BaseModel):
    """一条针对这位患者的调整。

    **`basis` 是 `Field(min_length=1)`**——跟 `cited_case_ids` 同一条防幻觉纪律：
    一条"孕妇应当减量"的调整，说不出依据就是模型自己想的。取不到依据时正确的
    做法是**不产出这一条**，不是产出一条依据为空的。
    """

    kind: AdjustmentKind
    target: str = Field(min_length=1)          # 哪一味药 / 哪一项
    adjustment: str = Field(min_length=1)      # 怎么调
    reason: str = Field(min_length=1)          # 为什么（针对这位患者的哪一点）
    basis: str = Field(min_length=1)           # 依据（本体条目、药典、教材原文）


class Individualization(BaseModel):
    """一次问诊的全部个体化调整。

    `items` **可以为空**：这位患者没有需要调整的地方，是一个合法且常见的结论，
    强制 `min_length=1` 会逼模型编一条出来。
    `considered` 记的是"看了哪几个维度"——空的 `items` 配上非空的 `considered`
    才说得清"查过了，没有需要调的"，而不是"没查"。
    """

    items: list[IndividualizationItem] = Field(default_factory=list)
    considered: list[str] = Field(default_factory=list)
