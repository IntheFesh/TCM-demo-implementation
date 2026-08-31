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


class CaseRecord(CaseStructured):
    case_id: str
    physician: str
    raw: str
    # 默认公有领域；将来接入现代出版书籍时改为 copyrighted，
    # export_sft.py 会据此过滤，是版权合规的代码级强制点。
    copyright_status: Literal["public_domain", "copyrighted"] = "public_domain"


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
