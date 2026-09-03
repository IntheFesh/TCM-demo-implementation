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


class S3Syndrome(BaseModel):
    syndrome: str
    reasoning: str
    treatment_principle: str
    formula: str | None = None
    herbs: list[str] = Field(default_factory=list)
    # min_length=1 同理：防幻觉的关键约束，不要改成可选。
    cited_case_ids: list[str] = Field(min_length=1)
    note: str | None = None
