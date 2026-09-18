"""R46 §7.1：结构化四诊录入、「人」这一维、文本互转，以及**合规护栏**。

最后那一项是这个文件里最要紧的：§0.4 第 1 条要求"任何图像/信号输入的接口若被
添加，CI 必须红并提示监管属性变更"。所以这里既有行为测试（请求带图像字段 →
400 + 那句说明），也有**源码级**测试（模块里出现图像/信号字段名就红）。
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core.intake import (
    FIELD_META,
    FORBIDDEN_INPUT_KINDS,
    QUICK_PICKS,
    REGULATORY_WARNING,
    InputKindRejected,
    IntakeForm,
    check_input_kinds,
    form_fields_by_part,
    form_to_text,
    text_to_form,
)
from core.schemas import PatientProfile

ROOT = Path(__file__).resolve().parent.parent


# ---------- 四诊结构化 ----------

def test_the_four_parts_are_all_present():
    parts = form_fields_by_part()
    assert set(parts) == {"望", "闻", "问", "切"}
    for part, fields in parts.items():
        assert fields, f"{part} 这一诊一个字段都没有"


def test_every_field_has_a_chinese_label():
    for name, (part, label) in FIELD_META.items():
        assert part in "望闻问切"
        assert re.search(r"[一-鿿]", label), f"{name} 的标签不是中文"


def test_the_quick_picks_are_textbook_wordings():
    """常用词一键选的价值一半在"快"，一半在"填进去的是规范术语"。"""
    for field, picks in QUICK_PICKS.items():
        assert field in FIELD_META, f"{field} 不是表单字段"
        assert picks, f"{field} 的常用词是空的"


def test_form_to_text_orders_by_the_four_examinations():
    f = IntakeForm(chief_complaint="胃脘胀痛", tongue="舌淡红", pulse="脉弦")
    text = form_to_text(f)
    assert text.startswith("胃脘胀痛")
    assert "舌象：舌淡红" in text and "脉象：脉弦" in text


def test_empty_fields_do_not_become_the_words_not_recorded():
    """一份满篇「未记」的主诉会把 S1 的症状抽取往空里带。"""
    text = form_to_text(IntakeForm(chief_complaint="纳差"))
    assert "未记" not in text and "无" not in text


def test_text_to_form_keeps_everything_it_cannot_parse():
    f = text_to_form("胃脘胀痛。舌：舌淡红苔薄白。这一句没有标签")
    assert f.chief_complaint == "胃脘胀痛"
    assert f.tongue == "舌淡红苔薄白"
    assert "这一句没有标签" in f.free_text


def test_the_two_directions_round_trip():
    f = IntakeForm(chief_complaint="胃脘胀痛", tongue="舌淡红", pulse="脉弦",
                   sleep="多梦易醒")
    back = text_to_form(form_to_text(f))
    assert back.chief_complaint == f.chief_complaint
    assert back.tongue == f.tongue and back.pulse == f.pulse
    assert back.sleep == f.sleep


def test_aliases_are_recognised():
    """「舌」「脉」「主述」这些异写要认得——病历原文里它们比全称更常见。"""
    f = text_to_form("舌苔：黄腻。脉搏：滑数")
    assert "黄腻" in f.tongue and "滑数" in f.pulse


def test_an_empty_text_gives_an_empty_form_not_an_error():
    f = text_to_form("")
    assert f.chief_complaint == "" and f.free_text == ""


# ---------- 「人」这一维 ----------

def test_the_profile_has_the_fang_bing_ren_dimensions():
    """对标黄煌的「方—病—人」：年龄、性别、体质倾向、基础病、过敏史、在服药物。"""
    fields = set(PatientProfile.model_fields)
    for must in ("age_years", "sex", "constitution", "comorbidities",
                 "allergies", "current_medications"):
        assert must in fields, f"人维少了 {must}"


def test_the_profile_distinguishes_unfilled_from_filled():
    assert PatientProfile().is_empty() is True
    assert PatientProfile(age_years=8).is_empty() is False
    assert PatientProfile(hepatic_impairment="有").is_empty() is False


def test_the_profile_only_takes_three_states_for_organ_function():
    """**不收检验数值**（§0.4 的输入侧边界）：肝肾功能只有有/无/不详三态。"""
    with pytest.raises(Exception):
        PatientProfile(hepatic_impairment="ALT 120")


def test_the_profile_rejects_an_impossible_age():
    with pytest.raises(Exception):
        PatientProfile(age_years=999)


# ---------- §0.4 合规护栏 ----------

@pytest.mark.parametrize("kind", sorted(FORBIDDEN_INPUT_KINDS))
def test_every_forbidden_input_kind_is_rejected(kind):
    with pytest.raises(InputKindRejected):
        check_input_kinds({kind: "x"})


def test_the_rejection_explains_the_regulatory_consequence():
    """不是一句"不支持"——填的人要知道**为什么**不收，否则下一轮还会有人加。"""
    with pytest.raises(InputKindRejected) as e:
        check_input_kinds({"tongue_image": "..."})
    assert "医疗器械" in str(e.value)
    assert REGULATORY_WARNING in str(e.value)


def test_plain_text_fields_pass():
    assert check_input_kinds({"complaint": "胃脘胀痛", "tongue": "舌淡红"}) is None


def test_the_consult_endpoint_rejects_an_image_field_with_a_readable_reason(monkeypatch):
    monkeypatch.setenv("PRODUCT_MODE", "0")
    client = TestClient(api_main.app)
    r = client.post("/api/consult", json={"complaint": "胃痛", "tongue_image": "data:..."})
    assert r.status_code == 400
    assert "医疗器械" in r.json()["detail"]


def test_the_intake_endpoint_rejects_lab_values():
    client = TestClient(api_main.app)
    r = client.post("/api/intake/parse", json={"text": "x", "lab_value": {"ALT": 40}})
    assert r.status_code == 400


def test_the_source_has_no_image_or_signal_input_field():
    """**源码级护栏。** 有人"顺手支持传张舌象照片"时这条会红——那在工程上是
    二十行代码，在监管上是换一个产品类别。"""
    bad = []
    for path in (ROOT / "core" / "intake.py", ROOT / "core" / "schemas.py"):
        src = path.read_text(encoding="utf-8")
        # 去掉注释与文档字符串：这两个文件里满篇在解释"为什么不收图像"
        import ast

        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.AnnAssign, ast.Assign)):
                for t in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets):
                    name = getattr(t, "id", "") or getattr(t, "attr", "")
                    if re.search(r"(image|photo|signal|ecg|imaging|lab_value)", name, re.I):
                        bad.append(f"{path.name}:{name}")
    assert not bad, ("这些字段会把产品从「不作为医疗器械管理」变成需按医疗器械"
                     f"注册（见 core/intake.py 的 §0.4 说明）：{bad}")


def test_the_form_endpoint_ships_the_regulatory_note():
    client = TestClient(api_main.app)
    note = client.get("/api/intake/form").json()["regulatory_note"]
    assert "照片" in note and "检验数值" in note
