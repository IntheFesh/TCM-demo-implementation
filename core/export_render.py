"""R62 §6.7 + §12 第 6 项：处方与记录的导出。

## 三种格式，**一个内容来源**

`[导出药方]` 要出可打印页面（A4）、纯文本、图片（PNG）三种。三种全部从
`build_render_model()` 产出的那一份 `render_model` 派生——**不是三套各自读
`FormulaCandidate` 的渲染器**。三套的话，加一行"署名"要改三处，漏掉一处
在那一种格式里就没有署名，而那种格式未必每次都有人看。

## PNG 为什么在浏览器里光栅化

这个仓库里没有 Pillow，`web/vendor/fonts/` 只有 woff2 子集（Pillow 喂不进
去）。为 PNG 引入 Pillow 加一套中文 TTF 是一个新依赖加几 MB 字体，
CLAUDE.md 明确说 demo 阶段不要过度设计。

**所以光栅化放在浏览器 canvas 上**：那里字体已经加载好了，画出来的 PNG
跟打印页逐像素同源。但**内容仍然由服务端一处生成**——前端 canvas 画的是
这个模块产出的同一份 `render_model`，没有另编一份。

这不是降级：使用者点「图片」照样得到 PNG，而光栅化本来就该在有字体的那一层
做。降级的做法是"PNG 这一档不做了"或者"PNG 里只画英文"，两者都没有发生。

## 措辞红线

`docs/glossary.md`：不许写"建议服用/推荐处方/治疗建议"。免责声明直接引
`core/emr_writer.DISCLAIMER`，**不在这里抄一份**——抄一份之后改一处漏一处，
而漏掉的那一处正是打印出来交给患者的那张纸。
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from core.emr_writer import DISCLAIMER, build_emr, render_emr_text
from core.prescription import format_pharmacy_text
from core.preferences import DOSAGE_FORMS, default_usage_for
from core.schemas import FormulaCandidate

#: 患者角色导出时顶部固定的那一段（§8.1 原文）。**一处定义，三种格式共用**。
#:
#: 它不是免责话术，是这个产品能把方摆给患者看的**前提**：摆出来的是教材上
#: 该证型的代表方（公开知识），不是给这个人开的方。这段话一旦被删掉或改软，
#: 同一份文件的性质就变了。
PATIENT_NOTICE = (
    "以下为《方剂学》教材中该证型的代表方及组成，非针对您个人的处方。\n"
    "中药需辨证使用，同一症状可能属于不同证型，用药前请务必经执业中医师当面辨证确认。\n"
    "若症状加重或出现新的不适，请立即就医。"
)


@dataclass(frozen=True)
class ExportContext:
    """导出这一次的外部信息。**方本身不在这里**——方是 `build_render_model`
    的第一个参数，这里只放"这一次是谁、什么时候、按什么剂型导的"。

    `role` 决定要不要加 §8.1 那段强提示。**默认 doctor**：忘了传 role 时
    得到的是不带强提示的医师版，而那是更保守的那一边——患者版多一段提示，
    漏掉提示的风险在患者那一侧。所以默认值刻意不是 patient。
    """

    role: str = "doctor"
    signature: str = ""
    visit_date: str = ""
    dosage_form: str = "饮片"
    record_id: str = ""
    syndrome: str = ""
    disease: str = ""
    method: str = ""
    #: 界面上让人改过的用法。空的话按剂型取默认（`core/preferences.py` 那张表）。
    usage: str = ""
    extra_notes: tuple[str, ...] = field(default_factory=tuple)


def _dose_text(item) -> str:
    """一味药的剂量文本。剂量缺失时是空串，**不写"适量"**——
    "适量"是一个临床判断，编一个出来比留白危险得多。"""
    d = item if isinstance(item, dict) else item.model_dump()
    if d.get("dose") is None:
        return ""
    return f"{float(d['dose']):g}{d.get('dose_unit') or 'g'}"


def _note_text(item) -> str:
    d = item if isinstance(item, dict) else item.model_dump()
    return " ".join(x for x in (d.get("processing"), d.get("decoction")) if x)


def build_render_model(formula: FormulaCandidate | dict, ctx: ExportContext) -> dict:
    """**三种格式唯一的内容来源。**

    结构刻意是"标题 + 若干分区 + 若干行"这种平的形状，不是嵌套的业务对象：
    前端 canvas 要照着它一行行画，一个需要理解业务语义才能渲染的结构会逼
    canvas 那一侧重新实现一遍"哪些字段该显示、显示成什么样"。

    缺字段就整块不出现（`sections` 里没有这一项），**不写"未记"**——
    跟 `core/intake.py::form_to_text` 同一条纪律：一张满篇"未记"的方拿给
    药房，看起来像信息缺失，实际上是这一栏本来就不适用。
    """
    f = formula if isinstance(formula, dict) else formula.model_dump()
    items = list(f.get("herb_items") or [])
    form = ctx.dosage_form if ctx.dosage_form in DOSAGE_FORMS else DOSAGE_FORMS[0]
    usage = (ctx.usage or "").strip() or (f.get("usage") or "").strip() or default_usage_for(form)

    meta: list[tuple[str, str]] = [("剂型", form)]
    if f.get("doses_count") is not None:
        meta.append(("剂数", f"{int(f['doses_count'])} 剂"))
    meta.append(("用法", usage))

    head: list[tuple[str, str]] = []
    for label, value in (("证型", ctx.syndrome), ("病名", ctx.disease), ("治法", ctx.method)):
        if (value or "").strip():
            head.append((label, value.strip()))

    foot: list[tuple[str, str]] = []
    if ctx.visit_date:
        foot.append(("日期", ctx.visit_date))
    if ctx.signature:
        foot.append(("署名", ctx.signature))
    if ctx.record_id:
        foot.append(("本次记录编号", ctx.record_id))

    return {
        "title": f.get("name") or "",
        # §8.1：患者角色顶部那一段。其余角色**这个键是空串**，不是一段
        # 空白——前端按真假决定画不画这一块。
        "notice": PATIENT_NOTICE if ctx.role == "patient" else "",
        "head": [{"k": k, "v": v} for k, v in head],
        "herbs": [{
            "name": (i if isinstance(i, dict) else i.model_dump()).get("name") or "",
            "dose": _dose_text(i),
            "note": _note_text(i),
            "role": (i if isinstance(i, dict) else i.model_dump()).get("role") or "",
        } for i in items],
        "meta": [{"k": k, "v": v} for k, v in meta],
        "foot": [{"k": k, "v": v} for k, v in foot],
        "notes": [n for n in ctx.extra_notes if (n or "").strip()],
        "disclaimer": DISCLAIMER,
    }


def render_plain_text(model: dict) -> str:
    """纯文本（可复制到任何地方）。

    药名与剂量那一段**交给 `core/prescription.py::format_pharmacy_text`**
    ——它已经处理了东亚宽字符的对齐（`len()` 算宽度会让「瓜蒌」和「甘草片」
    对不齐）。在这里再写一个排版器就是同一件事的第二份实现。
    """
    lines: list[str] = []
    if model.get("notice"):
        lines += [model["notice"], ""]
    for kv in model.get("head") or []:
        lines.append(f"{kv['k']}：{kv['v']}")
    if lines and lines[-1]:
        lines.append("")
    lines.append(format_pharmacy_text(_as_formula(model)))
    # 剂数与用法已经由 `format_pharmacy_text` 印在方头与末行了，这里不再印
    # 第二遍——同一张纸上出现两次"7 剂"，拿到药房的人会先愣一下再去数哪个对。
    for kv in model.get("meta") or []:
        if kv["k"] not in ("用法", "剂数"):
            lines.append(f"{kv['k']}：{kv['v']}")
    for n in model.get("notes") or []:
        lines.append(n)
    if model.get("foot"):
        lines.append("")
        lines += [f"{kv['k']}：{kv['v']}" for kv in model["foot"]]
    lines += ["", model.get("disclaimer") or ""]
    return "\n".join(x for x in lines if x is not None).rstrip() + "\n"


def _as_formula(model: dict) -> FormulaCandidate:
    """把 render_model 还原成 `format_pharmacy_text` 认的形状。

    **这里不重新读原始的 `FormulaCandidate`**：那样纯文本这一档就绕过了
    render_model，"三种格式同源"这条就只剩两种了——而分叉正是要防的事。
    """
    doses = next((kv["v"] for kv in model.get("meta") or [] if kv["k"] == "剂数"), "")
    usage = next((kv["v"] for kv in model.get("meta") or [] if kv["k"] == "用法"), "")
    items = []
    for h in model.get("herbs") or []:
        dose, unit = _split_dose(h.get("dose") or "")
        proc, dec = _split_note(h.get("note") or "")
        items.append({"name": h.get("name") or "", "dose": dose, "dose_unit": unit or "g",
                      "processing": proc, "decoction": dec, "role": h.get("role") or None})
    return FormulaCandidate(
        name=model.get("title") or "（未命名）", source="composed", confidence="medium",
        rationale="导出渲染用的中间形状", herb_items=items or [{"name": "（空方）"}],
        doses_count=int(doses.rstrip(" 剂")) if doses else None,
        usage=usage or None,
    )


_UNITS = ("g", "钱", "两", "分", "枚", "片")
#: 煎法的全集。**跟 `core/schemas.py::HerbItem.decoction` 的注释里列的是同一批**
#: ——这里只做"这个词是炮制还是煎法"的切分，不判断该不该标。
_DECOCTIONS = ("先煎", "后下", "包煎", "烊化", "冲服", "另煎")


def _split_dose(text: str) -> tuple[float | None, str]:
    for u in _UNITS:
        if text.endswith(u):
            try:
                return float(text[: -len(u)]), u
            except ValueError:
                return None, u
    return None, ""


def _split_note(text: str) -> tuple[str | None, str | None]:
    parts = [p for p in text.split(" ") if p]
    dec = next((p for p in parts if p in _DECOCTIONS), None)
    proc = next((p for p in parts if p not in _DECOCTIONS), None)
    return proc, dec


_PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm; }
* { box-sizing: border-box; }
body { font-family: "Noto Serif SC", "Songti SC", serif; color: #1a1a1a;
       font-size: 12pt; line-height: 1.7; margin: 0; }
h1 { font-size: 18pt; margin: 0 0 6mm; letter-spacing: .05em; }
.notice { border: 1pt solid #a33; padding: 3mm 4mm; margin: 0 0 5mm;
          white-space: pre-line; font-size: 10.5pt; color: #7a1f1f; }
.kv { margin: 0 0 4mm; }
.kv div { margin: 0 0 1mm; }
table.rx { width: 100%; border-collapse: collapse; margin: 0 0 5mm; }
table.rx td { padding: 1.4mm 2mm; border-bottom: .3pt solid #ddd; vertical-align: baseline; }
td.n { width: 34%; } td.d { width: 16%; text-align: right; }
td.r { width: 8%; color: #666; } td.x { color: #666; font-size: 10.5pt; }
.meta div, .foot div { margin: 0 0 1mm; }
.foot { margin-top: 8mm; }
.notes { margin: 0 0 4mm; font-size: 10.5pt; }
.disc { margin-top: 6mm; padding-top: 3mm; border-top: .3pt solid #bbb;
        font-size: 9.5pt; color: #555; }
@media screen { body { max-width: 190mm; margin: 12mm auto; padding: 0 6mm; } }
"""


def render_print_html(model: dict) -> str:
    """A4 打印页。**自包含**：内联样式、不引外部资源——诊室的那台机器未必
    连得上外网，而"打印出来没有样式"要等到纸出来才发现。

    全部插值走 `html.escape`：署名与备注是人填的，一段 `<script>` 原样印进
    去是 XSS。
    """
    e = html.escape

    def kv_block(rows, cls):
        if not rows:
            return ""
        inner = "".join(f"<div>{e(r['k'])}：{e(r['v'])}</div>" for r in rows)
        return f'<div class="{cls}">{inner}</div>'

    herb_rows = "".join(
        f'<tr><td class="r">{e(h.get("role") or "")}</td>'
        f'<td class="n">{e(h["name"])}</td>'
        f'<td class="d">{e(h.get("dose") or "")}</td>'
        f'<td class="x">{e(h.get("note") or "")}</td></tr>'
        for h in (model.get("herbs") or []))
    notice = (f'<div class="notice">{e(model["notice"])}</div>'
              if model.get("notice") else "")
    notes = ("".join(f"<div>{e(n)}</div>" for n in (model.get("notes") or [])))
    notes = f'<div class="notes">{notes}</div>' if notes else ""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{e(model.get('title') or '处方')}</title>"
        f"<style>{_PRINT_CSS}</style></head><body>"
        f"{notice}"
        f"<h1>{e(model.get('title') or '')}</h1>"
        f"{kv_block(model.get('head'), 'kv')}"
        f'<table class="rx">{herb_rows}</table>'
        f"{kv_block(model.get('meta'), 'meta')}"
        f"{notes}"
        f"{kv_block(model.get('foot'), 'foot')}"
        f'<div class="disc">{e(model.get("disclaimer") or "")}</div>'
        "</body></html>"
    )


def render_record_text(*, record_id: str = "", complaint: str = "",
                       form=None, profile=None, s2: dict | None = None,
                       s3: dict | None = None, formula: dict | None = None,
                       individualization=None, triage: dict | None = None,
                       guideline: dict | None = None, visit_date: str = "",
                       doses: int | None = None, decoction: str = "",
                       safety_flag: str | None = None) -> str:
    """§6.7「生成记录」：**纯模板填充、零 LLM、≤1 秒**。

    整段交给 `core/emr_writer.build_emr` + `render_emr_text`——那是这个项目
    里"一次问诊拼成一份文书"的唯一实现（R46 建的，带草稿声明、危重信号留痕、
    按角色的医嘱分段）。在这里另写一份模板会让两份病历文书在格式与措辞上
    慢慢分叉，而分叉的那一份迟早被打印出来当正式病历。
    """
    return render_emr_text(build_emr(
        record_id=record_id, complaint=complaint, form=form, profile=profile,
        s2=s2, s3=s3, formula=formula, individualization=individualization,
        triage=triage, guideline=guideline, visit_date=visit_date,
        doses=doses, decoction=decoction, safety_flag=safety_flag,
    ))
