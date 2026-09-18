"""R46 §7.4：中医病历文书自动生成。

三条纪律各有测试钉着：**生成的是草稿**、**医师的修改留痕**、
**措辞守 §0.4 的红线**（不出现指向具体患者的诊疗建议）。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core.emr_writer import (
    DISCLAIMER,
    DRAFT_NOTICE,
    apply_edits,
    build_emr,
    render_emr_text,
    render_prescription_sheet,
)
from core.intake import IntakeForm
from core.schemas import PatientProfile

ROOT = Path(__file__).resolve().parent.parent

FORM = IntakeForm(chief_complaint="胃脘胀痛三月", present_illness="食后加重",
                  tongue="舌淡红苔薄白", pulse="脉弦", sleep="多梦易醒")
S2 = {"elements": [{"element": "肝", "kind": "location"},
                   {"element": "气滞", "kind": "nature"}]}
S3 = {"disease": "胃脘痛", "syndrome": "肝胃不和证", "method": "疏肝和胃",
      "pathogenesis": "肝气犯胃，胃失和降", "reasoning": "由两胁胀满与脉弦推得"}
FORMULA = {"name": "柴胡疏肝散",
           "herb_items": [{"name": "柴胡", "dose": "6", "unit": "g"},
                          {"name": "白芍", "dose": "9", "unit": "g"}]}


def _emr(**kw):
    base = dict(record_id="K7M3QX92", form=FORM, profile=PatientProfile(age_years=45, sex="女"),
                s2=S2, s3=S3, formula=FORMULA, doses=7)
    base.update(kw)
    return build_emr(**base)


# ---------- 字段齐全 ----------

REQUIRED_SECTIONS = ("chief_complaint", "present_illness", "past_history", "allergies",
                     "four_exam", "diagnosis", "analysis", "method", "prescription",
                     "orders", "signature")


@pytest.mark.parametrize("key", REQUIRED_SECTIONS)
def test_every_regulation_section_is_present(key):
    """字段对齐国家中医药管理局《中医病历书写基本规范》。"""
    assert _emr().section(key) is not None, f"少了「{key}」这一段"


def test_the_four_exam_summary_is_grouped_by_the_four_examinations():
    body = _emr().text_of("four_exam")
    assert "望：" in body and "切：" in body
    assert "舌象舌淡红苔薄白" in body


def test_empty_fields_do_not_become_the_words_not_examined():
    """一份满篇「未查」的四诊摘要会让审核的人以为医师什么都没问。"""
    body = build_emr(form=IntakeForm(chief_complaint="纳差")).text_of("four_exam")
    assert "未查" not in body


def test_the_diagnosis_has_both_disease_and_syndrome():
    body = _emr().text_of("diagnosis")
    assert "疾病诊断：胃脘痛" in body and "证候诊断：肝胃不和证" in body


def test_the_analysis_comes_from_the_reasoning_chain():
    """辨证分析直接来自推导链——这是本系统最有价值的输出，
    也是医师手写时最费时间的一段。"""
    body = _emr().text_of("analysis")
    assert "病位：肝" in body and "病性：气滞" in body and "病机：" in body


def test_the_prescription_block_has_name_composition_doses_and_decoction():
    body = _emr().text_of("prescription")
    for must in ("方名：柴胡疏肝散", "组成：", "剂数：7 剂", "煎服法："):
        assert must in body


def test_a_blocked_consult_still_produces_a_document():
    """被安全层拦下的那一次也要能出一份文书——一个"只有开出方来才有病历"
    的实现会让最需要留痕的那一类问诊反而没有记录。"""
    emr = build_emr(record_id="AB12CD34", complaint="呕血")
    assert emr.record_id == "AB12CD34"
    assert emr.section("chief_complaint").body == "呕血"


# ---------- 三种导出 ----------

def test_the_text_export_is_plain_not_markdown():
    """HIS 的病历编辑器多半不认 markdown，粘进去会留下一堆井号和星号。"""
    text = render_emr_text(_emr())
    assert "##" not in text and "**" not in text


def test_the_text_export_carries_the_record_number_and_date():
    text = render_emr_text(_emr())
    assert "记录编号：K7M3QX92" in text and "日期：" in text


def test_the_prescription_sheet_has_a_pharmacy_layout_and_signature_lines():
    sheet = render_prescription_sheet(_emr())
    assert "中医处方笺" in sheet
    assert "医师签名" in sheet and "审核药师" in sheet
    assert "记录编号：K7M3QX92" in sheet


def test_the_json_export_is_the_same_structure():
    d = _emr().model_dump()
    assert d["record_id"] == "K7M3QX92"
    assert {s["key"] for s in d["sections"]} >= set(REQUIRED_SECTIONS)
    json.dumps(d, ensure_ascii=False)  # 可序列化


# ---------- 草稿声明 ----------

@pytest.mark.parametrize("render", [render_emr_text, render_prescription_sheet])
def test_every_export_carries_the_draft_notice(render):
    assert DRAFT_NOTICE in render(_emr())


@pytest.mark.parametrize("render", [render_emr_text, render_prescription_sheet])
def test_every_export_carries_the_disclaimer(render):
    assert DISCLAIMER in render(_emr())


def test_the_draft_notice_says_it_needs_a_physician_review():
    assert "审核" in DRAFT_NOTICE and "正式病历" in DRAFT_NOTICE


# ---------- 医师编辑留痕 ----------

def test_editing_a_section_reports_before_and_after():
    """只写改后的话，"医师把哪一句删了"就查不出来，而那正是质控要看的。"""
    emr = _emr()
    out, changes = apply_edits(emr, {"method": "疏肝理气和胃"})
    assert out.text_of("method") == "疏肝理气和胃"
    assert changes == [{"key": "method", "status": "changed",
                        "before": "疏肝和胃", "after": "疏肝理气和胃"}]


def test_an_unchanged_section_is_not_recorded_as_a_change():
    _, changes = apply_edits(_emr(), {"method": "疏肝和胃"})
    assert changes == []


def test_a_readonly_section_is_rejected_loudly_not_silently():
    """静默丢弃会让医师以为自己改了，而导出的还是原文。"""
    _, changes = apply_edits(_emr(), {"signature": "张三"})
    assert changes and changes[0]["status"] == "rejected"


def test_an_unknown_section_is_rejected_with_a_reason():
    _, changes = apply_edits(_emr(), {"不存在的段": "x"})
    assert changes and changes[0]["why"] == "没有这一段"


def test_edits_go_into_the_audit_chain(monkeypatch):
    seen = {}
    monkeypatch.setattr("core.audit.append_audit", lambda d: seen.update(d))
    from core.emr_writer import record_edits

    emr = _emr()
    out, changes = apply_edits(emr, {"method": "x"})
    record_edits(out, changes, doctor_id="dr_ye")
    assert seen.get("kind") == "emr_edit" and seen.get("doctor_id") == "dr_ye"
    assert seen["changes"][0]["before"] == "疏肝和胃"


# ---------- §0.4 合规措辞 ----------

def test_no_patient_directed_treatment_advice_in_any_export():
    """文书里不得出现"建议患者服用 X"这类指向具体患者的诊疗建议措辞。"""
    for render in (render_emr_text, render_prescription_sheet):
        text = render(_emr())
        for banned in ("建议患者服用", "推荐采用该方治疗", "应当服用", "需服用"):
            assert banned not in text, f"文书里出现了「{banned}」"


def test_the_source_of_the_writer_has_no_such_wording():
    """**文档字符串要摘掉再扫。** 这个模块的文档里正写着"不得出现『建议患者
    服用』"——不摘的话这条测试会红在自己的说明文字上。同一个形状本轮已经在
    JS 禁词扫描和 CSS 关键帧计数上各踩过一次：**扫源码文本的测试，它自己的
    说明文字也在被扫的范围里。**"""
    import ast

    src = (ROOT / "core" / "emr_writer.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            for banned in ("建议患者服用", "推荐采用该方治疗"):
                assert banned not in node.value


def test_the_triage_advice_is_still_allowed():
    """就医指引不在红线内：它说的是**去哪里**，不是吃什么，
    而且是安全边界的一部分（§8.3 第 21 条要求保留且更醒目）。"""
    emr = _emr(triage={"advice": "建议尽快就诊消化内科"})
    assert "建议尽快就诊" in emr.text_of("orders")


# ---------- 接口 ----------

def test_the_draft_endpoint_returns_all_three_formats():
    client = TestClient(api_main.app)
    r = client.post("/api/emr/draft", json={"record_id": "ZZ99YY88",
                                            "complaint": "胃脘胀痛", "s3": S3})
    assert r.status_code == 200
    body = r.json()
    assert body["emr"]["sections"] and body["text"] and body["prescription_sheet"]


def test_fetching_a_missing_record_is_404_not_an_empty_document():
    """空文书会被当成"这次问诊什么都没生成"。"""
    client = TestClient(api_main.app)
    assert client.get("/api/emr/NOSUCHID").status_code == 404
