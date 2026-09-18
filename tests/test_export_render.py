"""R62 §6.7 + §12 第 6 项：处方导出三种格式、记录生成一种。

这份测的核心是一条：**三种格式出自同一份 render_model**。三套各自读
`FormulaCandidate` 的渲染器，加一行"署名"要改三处，漏掉的那一处未必每次
都有人看——而它可能正是打印出来交给患者的那张纸。
"""
from __future__ import annotations

import pytest

from core.emr_writer import DISCLAIMER
from core.export_render import (
    PATIENT_NOTICE,
    ExportContext,
    build_render_model,
    render_plain_text,
    render_print_html,
    render_record_text,
)
from core.preferences import DOSAGE_FORMS
from core.schemas import FormulaCandidate

#: 措辞红线（docs/glossary.md）。系统输出的是知识整理与依据呈现，
#: 不是"建议对该患者采用 X 治疗"——后者会让产品落进第三类医疗器械。
BANNED = ("建议服用", "推荐处方", "治疗建议", "建议患者服用", "推荐采用")


@pytest.fixture
def formula() -> FormulaCandidate:
    return FormulaCandidate(
        name="柴胡疏肝散加减", source="modified", base_formula="柴胡疏肝散",
        confidence="high", rationale="疏肝理气", doses_count=7,
        herb_items=[
            {"name": "柴胡", "dose": 10.0, "dose_unit": "g", "processing": "醋炙", "role": "君"},
            {"name": "紫苏叶", "dose": 6.0, "dose_unit": "g", "decoction": "后下", "role": "佐"},
            {"name": "炙甘草", "dose": 3.0, "dose_unit": "g", "role": "使"},
        ])


def _all_three(formula, ctx) -> tuple[dict, str, str]:
    m = build_render_model(formula, ctx)
    return m, render_plain_text(m), render_print_html(m)


def test_changing_one_herb_changes_every_format(formula):
    """三种格式同源的判据：改一味药，三种输出都跟着变。任何一种没变，
    说明它绕过了 render_model 自己去读原始对象。"""
    _, t1, h1 = _all_three(formula, ExportContext())
    formula.herb_items[0].name = "醋北柴胡"
    m2, t2, h2 = _all_three(formula, ExportContext())
    assert "醋北柴胡" in t2 and "醋北柴胡" in h2
    assert m2["herbs"][0]["name"] == "醋北柴胡"
    assert t1 != t2 and h1 != h2


def test_the_printable_page_carries_the_same_disclaimer_as_the_medical_record(formula):
    """免责声明一处定义。抄一份之后改一处漏一处，而漏掉的那一处正是
    打印出来交给患者的那张纸。"""
    _, text, htm = _all_three(formula, ExportContext())
    assert DISCLAIMER in htm and DISCLAIMER in text


def test_a_signature_containing_markup_is_escaped(formula):
    """署名与备注是人填的。一段标记原样印进 HTML 是 XSS。"""
    _, _, htm = _all_three(formula, ExportContext(signature="<script>alert(1)</script>"))
    assert "<script>alert" not in htm
    assert "&lt;script&gt;" in htm


def test_the_printable_page_is_self_contained(formula):
    """诊室那台机器未必连得上外网，而"打印出来没有样式"要等到纸出来才发现。"""
    _, _, htm = _all_three(formula, ExportContext())
    assert "@page" in htm and "size: A4" in htm
    for external in ("<link", "src=\"http", "@import"):
        assert external not in htm


def test_each_dosage_form_gets_its_own_usage(formula):
    """饮片是水煎服、颗粒是开水冲服、膏方另一套。换了剂型用法还写着
    "水煎服"，那张方拿到药房是错的。"""
    seen = set()
    for form in DOSAGE_FORMS:
        m = build_render_model(formula, ExportContext(dosage_form=form))
        usage = next(kv["v"] for kv in m["meta"] if kv["k"] == "用法")
        seen.add(usage)
    assert len(seen) == len(DOSAGE_FORMS), "三种剂型给出了重复的用法"


def test_missing_fields_drop_the_whole_block_and_never_print_a_placeholder(formula):
    """缺字段就整块不出现，**不写"未记"**——一张满篇"未记"的方拿给药房，
    看起来像信息缺失，实际上是这一栏本来就不适用。"""
    formula.doses_count = None
    m, text, htm = _all_three(formula, ExportContext())   # 无署名、无日期、无证型
    assert [kv["k"] for kv in m["meta"]] == ["剂型", "用法"]
    assert m["head"] == [] and m["foot"] == []
    for s in (text, htm):
        assert "未记" not in s and "适量" not in s


def test_no_format_ever_uses_the_banned_wording(formula):
    _, text, htm = _all_three(formula, ExportContext(role="patient"))
    for word in BANNED:
        assert word not in text and word not in htm


def test_the_patient_export_carries_the_mandatory_notice_and_the_doctor_one_does_not(formula):
    """§8.1：患者导出的是教材代表方，顶部必须说清楚"非针对您个人的处方"。
    这段一旦丢掉或改软，同一份文件的性质就变了。"""
    mp, tp, hp = _all_three(formula, ExportContext(role="patient"))
    assert mp["notice"] == PATIENT_NOTICE
    assert "非针对您个人的处方" in tp and "非针对您个人的处方" in hp
    assert "请立即就医" in tp
    md, td, hd = _all_three(formula, ExportContext(role="doctor"))
    assert md["notice"] == "" and "非针对您个人的处方" not in td and "非针对您个人的处方" not in hd


def test_the_default_role_is_the_conservative_one():
    """忘了传 role 时得到的是不带强提示的医师版——漏掉提示的风险在患者
    那一侧，所以默认值刻意不是 patient。这条钉住那个取舍。"""
    assert ExportContext().role == "doctor"


def test_plain_text_reuses_the_pharmacy_formatter_not_a_second_one(formula):
    """药名与剂量的对齐交给 `core/prescription.py::format_pharmacy_text`
    ——它已经处理了东亚宽字符（`len()` 算宽度会让「瓜蒌」和「甘草片」对不齐）。
    判据：三味药名长度不同，对齐之后剂量列的起始位置一致。"""
    from core.prescription import _display_width

    _, text, _ = _all_three(formula, ExportContext())
    rows = [ln for ln in text.splitlines() if ln.startswith("  ") and "g" in ln]
    assert len(rows) == 3
    # **按显示宽度量，不按字符下标**：「炙甘草」是 3 个字符、6 列宽，
    # 「柴胡」是 2 个字符、4 列宽——按 `str.index` 比，对齐好的两行也会得出
    # 不同的数。这正是 `format_pharmacy_text` 存在的理由，也是这条断言
    # 第一次写错的地方。
    ends = {_display_width(ln[: ln.index("g") + 1]) for ln in rows}
    assert len(ends) == 1, f"剂量列没有对齐：{ends}"


def test_dose_and_usage_are_not_printed_twice(formula):
    """同一张纸上出现两次"7 剂"，拿到药房的人会先愣一下再去数哪个对。"""
    _, text, _ = _all_three(formula, ExportContext())
    assert text.count("7 剂") == 1


def test_generating_a_record_goes_through_the_medical_record_writer():
    """§6.7「生成记录」走 `core/emr_writer`，不是另一份模板——两份病历文书
    会在格式与措辞上慢慢分叉，而分叉的那一份迟早被打印出来当正式病历。"""
    text = render_record_text(record_id="AB12CD34", complaint="胃脘胀痛",
                              s3={"syndrome": "肝胃不和证"})
    assert "AB12CD34" in text
    assert DISCLAIMER in text
    # `DRAFT_NOTICE` 是 emr_writer 独有的特征串：另写一份模板不会带上它
    from core.emr_writer import DRAFT_NOTICE
    assert DRAFT_NOTICE in text
