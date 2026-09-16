"""R23：建议层在 HTTP 边界上的形状与那道角色边界。

两件事分开测：
1. **形状**：`/api/prescription/validate` 多了三个键，安全层原有的键一个没动。
2. **边界**：`role="patient"` 的响应体里**搜不到 advice、也搜不到建议里的药名**。
   后者是安全边界不是显示问题——「前端不画」和「响应体里没有」差着一次
   「检查元素」（跟 R15 患者模式那条判据同一个理由）。

⚠ 命名上的一个坑：患者导诊那个面板里也有一个 `advice`（`triage.advice`，
一句就诊指引文本）。两者不在同一层——导诊那个在响应顶层的 `triage` 里，
建议层这个在 `results[i]` 里。测试里都写全路径，不写裸 `advice`。
"""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient

import api.main as api_main
from core.formula_check import check_formula
from core.schemas import HerbItem
from tests.test_api import _rich_outcome

BODY = {
    "syndrome": "脾胃虚寒证",
    "herb_items": [{"name": "甘草"}, {"name": "甘遂"}, {"name": "附子", "dose": 30.0}],
}
ADVICE_FIELDS = ("advice", "advice_skipped", "formula_score")
# 安全层在 R23 之前就有的键。R23 是**新增三个键**，不是改写这几个——
# 前端和 /export 的 422 detail 都已经在消费它们。
SAFETY_KEYS = {"incompatible", "thermal_warning", "dose_violations",
               "decoction_missing", "toxic_herbs", "blocking"}


def _client() -> TestClient:
    return TestClient(api_main.app)


def test_validate_returns_the_three_advice_fields():
    resp = _client().post("/api/prescription/validate", json=BODY)
    assert resp.status_code == 200
    body = resp.json()
    for key in ADVICE_FIELDS:
        assert key in body, key
    kinds = [a["kind"] for a in body["advice"]]
    assert "incompatible" in kinds and "over_dose" in kinds


def test_validate_keeps_every_pre_r23_safety_key():
    """建议层是新增的一层。少一个安全键就是把调用方弄坏了。"""
    body = _client().post("/api/prescription/validate", json=BODY).json()
    assert SAFETY_KEYS <= set(body), SAFETY_KEYS - set(body)
    assert body["blocking"] is True


def test_validate_score_is_not_recomputed_at_the_http_layer():
    """接口里的分必须等于 `check_formula` 算出来的那个——HTTP 层自己再算一遍
    权重，就有了第二处实现。"""
    body = _client().post("/api/prescription/validate", json=BODY).json()
    expected = check_formula(BODY["syndrome"], [HerbItem(**h) for h in BODY["herb_items"]])
    assert body["formula_score"] == expected.score


def test_validate_strips_the_advice_layer_for_the_patient_role():
    """三个键**一个都不下发**（不是下发三个空值）：空列表读起来是"查过了，
    没有建议"，而真相是"这个角色不给这一层"。"""
    body = _client().post("/api/prescription/validate", json={**BODY, "role": "patient"}).json()
    for key in ADVICE_FIELDS:
        assert key not in body, key
    assert SAFETY_KEYS <= set(body), "安全键照旧给（患者那边由 _simplify_safety_output 再裁）"


def test_validate_gives_the_advice_layer_to_doctor_student_and_researcher():
    client = _client()
    for role in ("doctor", "student", "researcher"):
        body = client.post("/api/prescription/validate", json={**BODY, "role": role}).json()
        for key in ADVICE_FIELDS:
            assert key in body, f"role={role} 少了 {key}"


def test_validate_defaults_to_doctor_so_the_editor_gets_advice_without_asking():
    """默认 doctor：这条接口是医生端可编辑处方表在用的。
    判据写成"不传 role 和传 doctor 的响应一致"，而不是去读默认值字面量。"""
    client = _client()
    without = client.post("/api/prescription/validate", json=BODY).json()
    with_doctor = client.post("/api/prescription/validate", json={**BODY, "role": "doctor"}).json()
    assert without == with_doctor


def test_an_empty_formula_still_returns_200_with_a_clean_score():
    """0 味药本来就查不出十八反/剂量超限，返回全空 + 满分是诚实的结果，
    不该被 422 拒绝（这条判据在 R23 之前就写在请求体的注释里，这里把它延伸到
    新增的三个键上）。"""
    body = _client().post("/api/prescription/validate",
                          json={"syndrome": "脾胃虚寒证", "herb_items": []}).json()
    assert body["advice"] == []
    assert body["formula_score"] == 1.0
    assert body["blocking"] is False


def test_consult_carries_advice_per_physician(monkeypatch):
    outcome = copy.deepcopy(_rich_outcome())
    outcome["results"][0].update({
        "advice": [{"kind": "duplicate_effect", "herbs": ["白术", "苍术"],
                    "reason": "白术 与 苍术 性味功效重合 100%（健脾益气），考虑去其一",
                    "source_span": None, "severity": "suggestion"}],
        "advice_skipped": [{"rule": "missing_channel_guide", "reason": "缺表", "available": False}],
        "formula_score": 0.9,
    })
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    resp = _client().post("/api/consult", json={"complaint": "胸闷胸痛", "role": "researcher"})
    assert resp.status_code == 200
    r = resp.json()["results"][0]
    assert r["formula_score"] == 0.9
    assert r["advice"][0]["kind"] == "duplicate_effect"
    assert r["advice_skipped"][0]["available"] is False


def test_the_patient_response_contains_no_advice_and_no_herb_name_from_it(monkeypatch):
    """安全边界：患者响应体里既不能有这三个键，也不能出现建议文案里的药名。
    整个响应体序列化成一个字符串来搜——这样"药名从某个没想到的字段漏出去"
    也会被抓到，而不是只检查我们记得去检查的那几个键。"""
    outcome = copy.deepcopy(_rich_outcome())
    outcome["results"][0].update({
        "advice": [{"kind": "incompatible", "herbs": ["甘草", "甘遂"],
                    "reason": "甘草 与 甘遂 属配伍禁忌，同方相见须改方",
                    "source_span": "十八反", "severity": "blocking"}],
        "advice_skipped": [],
        "formula_score": 0.0,
    })
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    resp = _client().post("/api/consult", json={"complaint": "胸闷胸痛", "role": "patient"})
    assert resp.status_code == 200
    r = resp.json()["results"][0]
    for key in ADVICE_FIELDS:
        assert key not in r, key
    blob = resp.text
    for herb in ("甘遂", "十八反"):
        assert herb not in blob, f"患者响应体里泄露了「{herb}」"


def test_doctor_keeps_the_advice_layer_in_consult(monkeypatch):
    """医生要的正是这一层——patient 的裁剪不许顺手把 doctor 也裁掉。"""
    outcome = copy.deepcopy(_rich_outcome())
    outcome["results"][0].update({
        "advice": [{"kind": "over_dose", "herbs": ["附子"], "reason": "附子 30.0g 超过常用上限 15.0g",
                    "source_span": "含乌头碱", "severity": "blocking"}],
        "advice_skipped": [], "formula_score": 0.5,
    })
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    resp = _client().post("/api/consult", json={"complaint": "胸闷胸痛", "role": "doctor"})
    r = resp.json()["results"][0]
    assert r["advice"][0]["herbs"] == ["附子"]
    assert r["formula_score"] == 0.5


def test_the_role_predicate_is_the_single_place_that_decides(monkeypatch):
    """把 `_role_gets_advice` 换掉，两处（validate 和 consult 裁剪）都要跟着变。
    这条抓的是"两处各写一遍 if role == 'patient'"那种退化。"""
    monkeypatch.setattr(api_main, "_role_gets_advice", lambda role: False)
    body = _client().post("/api/prescription/validate", json=BODY).json()
    for key in ADVICE_FIELDS:
        assert key not in body, f"{key} 仍在——validate 没走那个判据"
