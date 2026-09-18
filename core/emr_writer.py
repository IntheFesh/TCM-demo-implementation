"""R46 §7.4：中医病历文书自动生成。

**对标灵枢云列出的行业痛点第二条：病历书写耗时。** 这个系统已经把辨证过程
一步步算出来了——病位、病性、病机演变、治法、方药、每一步的依据——而这些
正好就是中医病历里最费时间的「辨证分析」那一段。不生成一份文书，等于让医师
对着屏幕上的推导链再手打一遍。

## 三条纪律

1. **生成的是草稿，不是病历。** 每一份导出都带那句
   「本文书为辅助生成的草稿，须经执业医师审核修改后方可作为正式病历」。
2. **医师的每一次修改都留痕。** `record_edits()` 把改前改后写进审计链
   （`core.audit.append_audit`），改了什么、谁改的、什么时候改的可回查。
3. **措辞守 §0.4 的红线。** 文书里不出现"建议患者服用 X"这类指向具体患者的
   诊疗建议，改为客观陈述（「本证型的教材治法为…」）。就医指引不在红线内。

## 字段对齐国家中医药管理局《中医病历书写基本规范》

主诉 / 现病史 / 既往史 / 过敏史 / 四诊摘要（望闻问切）/ 中医诊断（疾病诊断 +
证候诊断）/ 辨证分析 / 治法 / 处方（方名、组成、剂量、剂数、煎服法）/ 医嘱 /
医师签名栏 / 日期。

## 三种导出，一个来源

`build_emr()` 产出一份结构，三个 `render_*` 各自把它排成一种形状：

    render_prescription_sheet()  A4 处方笺（药房格式，含记录编号与声明）
    render_emr_text()            病历文本（可直接粘进 HIS 的病历编辑器）
    emr.model_dump()             结构化 JSON（供 HIS 接口导入）

**药房格式那一段复用 `core.prescription.format_pharmacy_text`**，不另写一套
对齐逻辑——CLAUDE.md「同一概念的匹配逻辑只能有一处实现」。
"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from core.intake import FIELD_META, IntakeForm
from core.schemas import Individualization, PatientProfile

DRAFT_NOTICE = ("本文书为辅助生成的草稿，须经执业医师审核修改后方可作为正式病历。")

DISCLAIMER = ("本系统为中医知识辅助与教学工具，不作为医疗器械管理，不提供诊断结论；"
              "生成内容须由执业医师审核后方可使用。")

#: 医嘱的三类。**每一类都是客观陈述或就医指引，不是"建议患者服用"**（§0.4）。
ORDER_SECTIONS = ("饮食起居", "复诊", "注意事项")


class EMRSection(BaseModel):
    """文书的一段。`editable` 决定前端给不给编辑框——签名栏与日期不给。"""

    key: str
    title: str
    body: str = ""
    editable: bool = True


class EMRDraft(BaseModel):
    """一份中医病历文书草稿。

    `record_id` 是产品面上那个「本次记录编号」——文书与那一次问诊靠它对上，
    医院质控调阅时报这个号。
    """

    record_id: str = ""
    visit_date: str = ""
    sections: list[EMRSection] = Field(default_factory=list)
    draft_notice: str = DRAFT_NOTICE
    disclaimer: str = DISCLAIMER

    def section(self, key: str) -> EMRSection | None:
        return next((s for s in self.sections if s.key == key), None)

    def text_of(self, key: str) -> str:
        s = self.section(key)
        return s.body if s else ""


def _four_exam_summary(form: IntakeForm | None) -> str:
    """四诊摘要：按望闻问切分四行，每行列这一诊填了的项。

    空项整项不出现，**不写"未查"**——一份满篇"未查"的四诊摘要会让审核的人
    以为医师什么都没问，而实际情况多半是这一项跟本次主诉无关。
    """
    if form is None:
        return ""
    buckets: dict[str, list[str]] = {"望": [], "闻": [], "问": [], "切": []}
    for name, (part, label) in FIELD_META.items():
        if name == "chief_complaint":
            continue  # 主诉单独成段
        val = (getattr(form, name, "") or "").strip()
        if val:
            buckets[part].append(f"{label}{val}")
    lines = [f"{part}：{'，'.join(vals)}" for part, vals in buckets.items() if vals]
    return "\n".join(lines)


def _profile_line(profile: PatientProfile | None) -> str:
    if profile is None or profile.is_empty():
        return ""
    bits: list[str] = []
    if profile.age_years is not None:
        bits.append(f"{profile.age_years} 岁")
    if profile.sex:
        bits.append(profile.sex)
    if profile.life_stage:
        bits.append(profile.life_stage)
    if profile.constitution:
        bits.append(f"体质倾向{profile.constitution}")
    if profile.comorbidities:
        bits.append("基础病：" + "、".join(profile.comorbidities))
    if profile.current_medications:
        bits.append("在服药物：" + "、".join(profile.current_medications))
    for organ, flag in (("肝", profile.hepatic_impairment), ("肾", profile.renal_impairment)):
        if flag == "有":
            bits.append(f"{organ}功能不全")
    return "，".join(bits)


def _analysis(s2: dict | None, s3: dict | None) -> str:
    """辨证分析：病位、病性、病机演变。**直接来自推导链**——这是本系统最有
    价值的输出，也是医师手写时最费时间的一段。"""
    lines: list[str] = []
    elements = (s2 or {}).get("elements") or []
    loc = [e.get("element") for e in elements if e.get("kind") == "location"]
    nat = [e.get("element") for e in elements if e.get("kind") == "nature"]
    if loc:
        lines.append("病位：" + "、".join(x for x in loc if x))
    if nat:
        lines.append("病性：" + "、".join(x for x in nat if x))
    st = s3 or {}
    if st.get("pathogenesis"):
        lines.append("病机：" + str(st["pathogenesis"]))
    if st.get("reasoning"):
        lines.append("辨证推导：" + str(st["reasoning"]))
    return "\n".join(lines)


def _orders(individualization: Individualization | None, triage: dict | None) -> str:
    """医嘱。三类，每类都是客观陈述或就医指引。"""
    lines: list[str] = []
    if triage and triage.get("advice"):
        lines.append(f"复诊：{triage['advice']}")
    if individualization and individualization.items:
        for it in individualization.items:
            lines.append(f"注意事项：{it.target} —— {it.adjustment}（{it.reason}；依据：{it.basis}）")
    if not lines:
        lines.append("注意事项：按医师面嘱执行。")
    return "\n".join(lines)


def _prescription_block(formula: dict | None, doses: int | None, decoction: str) -> str:
    if not formula:
        return ""
    lines = [f"方名：{formula.get('name') or '（未命名）'}"]
    items = formula.get("herb_items") or []
    if items:
        comp = "、".join(
            f"{it.get('name', '')}{it.get('dose', '')}{it.get('unit', '')}".strip()
            for it in items if it.get("name"))
        lines.append(f"组成：{comp}")
    if doses:
        lines.append(f"剂数：{doses} 剂")
    lines.append(f"煎服法：{decoction or '水煎服，日一剂，分两次温服'}")
    return "\n".join(lines)


def build_emr(
    *,
    record_id: str = "",
    complaint: str = "",
    form: IntakeForm | None = None,
    profile: PatientProfile | None = None,
    s2: dict | None = None,
    s3: dict | None = None,
    formula: dict | None = None,
    individualization: Individualization | None = None,
    triage: dict | None = None,
    guideline: dict | None = None,
    visit_date: str = "",
    doses: int | None = None,
    decoction: str = "",
) -> EMRDraft:
    """把一次问诊拼成一份文书草稿。

    **全部入参可空**：被安全层拦下的那一次也要能出一份文书（记下"因何中止"），
    一个"只有开出方来才有病历"的实现会让最需要留痕的那一类问诊反而没有记录。
    """
    form = form or IntakeForm()
    chief = (complaint or form.chief_complaint or "").strip()
    st = s3 or {}
    sections = [
        EMRSection(key="chief_complaint", title="主诉", body=chief),
        EMRSection(key="present_illness", title="现病史",
                   body=form.present_illness or chief),
        EMRSection(key="past_history", title="既往史",
                   body=form.past_history or _profile_line(profile)),
        EMRSection(key="allergies", title="过敏史",
                   body="、".join((profile or PatientProfile()).allergies) or "否认药物及食物过敏史"),
        EMRSection(key="four_exam", title="四诊摘要", body=_four_exam_summary(form)),
        EMRSection(key="diagnosis", title="中医诊断",
                   body="\n".join(x for x in (
                       f"疾病诊断：{st.get('disease')}" if st.get("disease") else "",
                       f"证候诊断：{st.get('syndrome')}" if st.get("syndrome") else "",
                   ) if x)),
        EMRSection(key="analysis", title="辨证分析", body=_analysis(s2, st)),
        EMRSection(key="method", title="治法", body=str(st.get("method") or "")),
        EMRSection(key="prescription", title="处方",
                   body=_prescription_block(formula, doses, decoction)),
        EMRSection(key="orders", title="医嘱", body=_orders(individualization, triage)),
    ]
    if guideline and guideline.get("summary"):
        # 循证对照进文书：医师需要知道这一次的推导跟教材差在哪。
        # **这一段是客观陈述**，不带"所以应当采用哪一个"。
        lines = [guideline["summary"]]
        for d in guideline.get("deviations") or []:
            lines.append(f"- {d.get('what')}：教材为「{d.get('recommended')}」，"
                         f"本次为「{d.get('ours')}」（{d.get('note')}；出处：{d.get('source')}）")
        sections.append(EMRSection(key="guideline", title="与教材推荐方案的对照",
                                   body="\n".join(lines), editable=False))
    sections.append(EMRSection(key="signature", title="医师签名", body="", editable=False))
    return EMRDraft(
        record_id=record_id,
        visit_date=visit_date or date.today().isoformat(),
        sections=sections,
    )


def render_emr_text(emr: EMRDraft) -> str:
    """病历文本：可直接粘进 HIS 的病历编辑器。**纯文本、不带 markdown 记号**
    ——HIS 的编辑器多半不认，粘进去会留下一堆井号和星号。"""
    out = [f"【{DRAFT_NOTICE}】", ""]
    for s in emr.sections:
        if not s.body.strip() and s.key != "signature":
            continue
        out.append(f"{s.title}：")
        out.append(s.body.strip() or "＿＿＿＿＿＿＿＿＿＿")
        out.append("")
    out.append(f"日期：{emr.visit_date}")
    out.append(f"记录编号：{emr.record_id or '（未记录）'}")
    out.append(DISCLAIMER)
    return "\n".join(out)


def render_prescription_sheet(emr: EMRDraft, formula_obj=None) -> str:
    """A4 处方笺（药房格式）。

    药材那一段**复用 `core.prescription.format_pharmacy_text`**——那套对齐
    逻辑（按显示宽度补空格，中日韩字符算两格）已经存在，抄第二份的结果是
    两张处方笺排版不一样。
    """
    from core.prescription import format_pharmacy_text

    head = [
        "中医处方笺",
        f"日期：{emr.visit_date}　　记录编号：{emr.record_id or '（未记录）'}",
        "-" * 40,
        f"诊断：{emr.text_of('diagnosis').replace(chr(10), '；') or '＿＿＿'}",
        f"治法：{emr.text_of('method') or '＿＿＿'}",
        "-" * 40,
    ]
    body = format_pharmacy_text(formula_obj) if formula_obj is not None else emr.text_of("prescription")
    tail = [
        "-" * 40,
        f"医嘱：{emr.text_of('orders')}",
        "",
        "医师签名：＿＿＿＿＿＿　　审核药师：＿＿＿＿＿＿",
        "",
        DRAFT_NOTICE,
        DISCLAIMER,
    ]
    return "\n".join([*head, body, *tail])


def apply_edits(emr: EMRDraft, edits: dict[str, str]) -> tuple[EMRDraft, list[dict]]:
    """医师改了哪几段。返回改后的文书和**逐段的改前改后**。

    不可编辑的段（签名栏、循证对照）收到修改时**拒绝并记一条**，不是静默丢弃
    ——静默丢弃会让医师以为自己改了，而导出的还是原文。
    """
    changes: list[dict] = []
    out = emr.model_copy(deep=True)
    for key, new in (edits or {}).items():
        sec = out.section(key)
        if sec is None:
            changes.append({"key": key, "status": "rejected", "why": "没有这一段"})
            continue
        if not sec.editable:
            changes.append({"key": key, "status": "rejected", "why": "这一段不可编辑"})
            continue
        if sec.body == new:
            continue
        changes.append({"key": key, "status": "changed", "before": sec.body, "after": new})
        sec.body = new
    return out, changes


def record_edits(emr: EMRDraft, changes: list[dict], doctor_id: str = "") -> None:
    """把修改写进审计链。**改前改后都写**——只写改后的话，"医师把哪一句删了"
    这件事就查不出来，而那正是质控要看的。"""
    if not changes:
        return
    from core.audit import append_audit

    append_audit({
        "kind": "emr_edit",
        "record_id": emr.record_id,
        "doctor_id": doctor_id,
        "changes": changes,
    })
