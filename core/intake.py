"""R46 §7.1：结构化四诊录入 + 「人」这一维，以及输入侧的合规护栏。

## 为什么要结构化表单，自由文本又为什么必须留着

诊室里两种用法都真实存在：跟着病历模板逐项填（表单快），和把病历原文粘进来
（自由文本快）。所以**两者可互转**，不是二选一：

    form_to_text(IntakeForm)  →  一段可以直接送进 S1 的主诉文本
    text_to_form(str)         →  尽力拆成四诊字段，拆不出的原样留在 free_text

`text_to_form` 是**尽力而为**，不保证拆全：拆不出来的部分留在 `free_text` 里
照样进推理链。一个"拆不出来就丢掉"的实现会静默吃掉病历里最要紧的那句。

## §0.4 的输入侧边界写在代码里，不只写在文档里

《人工智能医用软件产品分类界定指导原则》：对**患者主诉信息或电子病历**进行
推理分析，因为违反了客观数据的定义，**不作为医疗器械管理**。

**一旦引入舌象照片、脉诊仪信号、检验数值等客观数据的分析，产品性质立刻改变，
需要按医疗器械注册。** 所以这个模块只收**文字描述**，并且：

  - `FORBIDDEN_INPUT_KINDS` 明确列出不收什么；
  - `check_input_kinds()` 在边界上拦；
  - `tests/test_intake_form.py` 有一条源码级测试：这个模块和 API 层一旦出现
    图像/信号/检验数值的输入字段，CI 立刻红，并打出"这会改变产品的监管属性"。

这条护栏不是防御攻击，是**防止有人出于好意加个功能**——"顺手支持传张舌象
照片"在工程上是二十行代码，在监管上是换一个产品类别。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.schemas import PatientProfile

#: 四诊的四个部分。**用 Literal 不用自由字符串**：下游按部位分组渲染，
#: 多一个拼错的"問"会长出第五个分组。
Inspection = Literal["望", "闻", "问", "切"]

#: 不收的输入类型。**这张表是合规边界的机读形式**，改它等于改产品的监管属性。
FORBIDDEN_INPUT_KINDS: dict[str, str] = {
    "image": "舌象/面色照片等图像",
    "tongue_image": "舌象照片",
    "face_image": "面色照片",
    "pulse_signal": "脉诊仪信号",
    "lab_value": "检验数值（血常规、肝肾功能等）",
    "ecg": "心电等生理信号",
    "imaging": "影像学检查结果",
}

REGULATORY_WARNING = (
    "本系统只接受主诉与病历的**文字描述**。引入舌象照片、脉诊仪信号、检验数值等"
    "客观数据的分析，会使产品从「不作为医疗器械管理」变为需按医疗器械注册"
    "（《人工智能医用软件产品分类界定指导原则》）。"
)


class InputKindRejected(ValueError):
    """收到了文字之外的输入类型。"""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        what = FORBIDDEN_INPUT_KINDS.get(kind, kind)
        super().__init__(f"不接受「{what}」这类输入。{REGULATORY_WARNING}")


def check_input_kinds(payload: dict) -> None:
    """请求体里出现禁收的输入类型就抛。**在边界上调用一次**，不在各处判。"""
    for key in payload or {}:
        k = str(key).lower()
        for bad in FORBIDDEN_INPUT_KINDS:
            if bad in k:
                raise InputKindRejected(bad)


class TextOnlyInput(BaseModel):
    """接收外部输入的请求体的基类：**多出来的字段不丢，先过一遍合规护栏**。

    为什么不用 `extra="forbid"`：那样任何一个多余字段都变成 422，而 422 的
    正文是 pydantic 的英文校验错误——使用者看不懂，也读不到"这会改变产品的
    监管属性"这句要紧的话。所以是 `allow` + 一条校验：多出来的字段里只要有
    图像/信号/检验数值那几类，就抛 `InputKindRejected`，由 HTTP 层翻成 400
    并带上说明。其余多余字段照旧忽略（老客户端多带一个字段不该被拒）。
    """

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _only_text_input(self):
        check_input_kinds(self.model_extra or {})
        return self


class IntakeForm(TextOnlyInput):
    """结构化四诊 + 人维。全部字段可空——门诊现场未必问得全。

    这里**没有一个 `Field(min_length=1)`**：它不是模型的输出，是人填的表单。
    防幻觉约束管的是"模型说的话要有出处"，跟这张表无关（见 PatientProfile
    的同一条说明）。
    """

    # 望
    tongue: str = ""            # 舌象的**文字描述**
    complexion: str = ""        # 面色
    body_form: str = ""         # 形体
    # 闻
    voice: str = ""
    odor: str = ""
    # 问
    chief_complaint: str = ""   # 主诉
    present_illness: str = ""   # 现病史
    past_history: str = ""      # 既往史
    diet: str = ""
    stool_urine: str = ""
    sleep: str = ""
    # 切
    pulse: str = ""
    abdomen: str = ""
    # 人
    profile: PatientProfile = Field(default_factory=PatientProfile)
    #: 拆不出来的原文。**不丢**——病历里最要紧的那句常常不在任何字段里。
    free_text: str = ""


#: 字段 → (四诊归属, 中文标签)。**一处定义**：表单渲染、文本互转、
#: 病历文书的四诊摘要三处都从这里取。
FIELD_META: dict[str, tuple[Inspection, str]] = {
    "tongue": ("望", "舌象"),
    "complexion": ("望", "面色"),
    "body_form": ("望", "形体"),
    "voice": ("闻", "声音"),
    "odor": ("闻", "气味"),
    "chief_complaint": ("问", "主诉"),
    "present_illness": ("问", "现病史"),
    "past_history": ("问", "既往史"),
    "diet": ("问", "饮食"),
    "stool_urine": ("问", "二便"),
    "sleep": ("问", "睡眠"),
    "pulse": ("切", "脉象"),
    "abdomen": ("切", "腹诊"),
}

#: 常用词一键选。**每一项都是教材里的规范写法**——表单的价值一半在"快"，
#: 一半在"填进去的是规范术语而不是各写各的"。
QUICK_PICKS: dict[str, tuple[str, ...]] = {
    "tongue": ("舌淡红苔薄白", "舌红苔黄", "舌淡胖有齿痕", "舌暗有瘀斑", "舌红少苔"),
    "pulse": ("脉弦", "脉细", "脉滑", "脉数", "脉沉", "脉濡", "脉弦细", "脉沉迟"),
    "complexion": ("面色萎黄", "面色㿠白", "面色晦暗", "面红"),
    "voice": ("声低气怯", "语声重浊", "太息频作"),
    "odor": ("口臭", "无异常气味"),
    "diet": ("纳差", "食后腹胀", "喜热饮", "口干喜冷饮"),
    "stool_urine": ("大便溏", "大便干结", "小便清长", "小便短赤"),
    "sleep": ("入睡困难", "多梦易醒", "眠可"),
    "abdomen": ("腹软无压痛", "上腹压痛", "腹胀满"),
}


def form_to_text(form: IntakeForm) -> str:
    """表单 → 一段可以直接送进 S1 的文本。

    顺序按四诊（望闻问切），不按字段声明顺序——医师读病历就是这个顺序。
    空字段整项不出现，**不写"未记"**：一个满篇"未记"的主诉会把 S1 的
    症状抽取往空里带。
    """
    if form is None:
        return ""
    parts: list[str] = []
    # 主诉单独提到最前：它是这段文本的主语
    if form.chief_complaint.strip():
        parts.append(form.chief_complaint.strip())
    for name, (_part, label) in FIELD_META.items():
        if name == "chief_complaint":
            continue
        val = (getattr(form, name, "") or "").strip()
        if val:
            parts.append(f"{label}：{val}")
    if form.free_text.strip():
        parts.append(form.free_text.strip())
    return "。".join(p.rstrip("。") for p in parts if p) + ("。" if parts else "")


#: 文本 → 表单时认的前缀。**复用 FIELD_META 的标签**，不另列一张表。
_LABEL_TO_FIELD = {label: name for name, (_p, label) in FIELD_META.items()}
#: 常见异写。跟 FIELD_META 的标签合成一张查表，**只此一处**。
_LABEL_ALIASES = {
    "舌": "tongue", "舌苔": "tongue", "舌质": "tongue",
    "脉": "pulse", "脉搏": "pulse",
    "主述": "chief_complaint", "现病史": "present_illness",
    "既往": "past_history", "大便": "stool_urine", "小便": "stool_urine",
}


def text_to_form(text: str) -> IntakeForm:
    """自由文本 → 表单。**尽力而为**：认得出的填进字段，认不出的留在
    `free_text`，一个字都不丢。

    判据是"`形如 标签：内容` 的片段"。不做分词、不猜——猜错会把"脉弦"
    填进面色栏，而那比留在自由文本里糟得多。
    """
    form = IntakeForm()
    if not text:
        return form
    rest: list[str] = []
    for seg in _split_segments(text):
        label, _, value = seg.partition("：")
        if not value:
            label, _, value = seg.partition(":")
        field = _LABEL_TO_FIELD.get(label.strip()) or _LABEL_ALIASES.get(label.strip())
        if field and value.strip():
            cur = getattr(form, field, "")
            setattr(form, field, f"{cur}；{value.strip()}" if cur else value.strip())
        else:
            rest.append(seg)
    if rest:
        # 第一段没有标签时当主诉——病历原文的第一句就是主诉，这是书写规范
        if not form.chief_complaint and rest:
            form.chief_complaint = rest.pop(0)
        form.free_text = "。".join(rest)
    return form


def _split_segments(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        if ch in "。;；\n":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def form_fields_by_part() -> dict[str, list[dict]]:
    """给前端渲染表单用：按望闻问切分组。**结构从 FIELD_META 现算**，
    前端不写死字段表——写死的话这里加一个字段，表单上不会长出来。"""
    out: dict[str, list[dict]] = {"望": [], "闻": [], "问": [], "切": []}
    for name, (part, label) in FIELD_META.items():
        out[part].append({"name": name, "label": label,
                          "quick_picks": list(QUICK_PICKS.get(name, ()))})
    return out
