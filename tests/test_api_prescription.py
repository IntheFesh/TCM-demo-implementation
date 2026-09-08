"""api/main.py 里 M8 新增的两条端点（/api/prescription/validate、
/api/prescription/export）的离线测试。不调 LLM——validate 是纯规则接口，
export 只走确定性的 diff/格式化/哈希链，不需要 mock core.chain.consult。
"""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
import core.audit as audit


@pytest.fixture(autouse=True)
def _isolated_audit_path(tmp_path, monkeypatch):
    """跟 tests/test_audit.py 同一条理由：不碰真实 data/audit.jsonl。
    api/main.py 的 append_audit 是 `from core.audit import append_audit`
    直接导入的函数名（不是模块引用），所以要 patch core.audit.AUDIT_PATH
    ——append_audit 函数体内部读的是它自己模块里的 AUDIT_PATH 全局名，
    不受 api.main 这边 import 方式的影响。"""
    monkeypatch.setattr(audit, "AUDIT_PATH", tmp_path / "audit.jsonl")


@pytest.fixture
def client():
    return TestClient(api_main.app)


def _herb(name, dose=9.0, dose_unit="g", **kw):
    return {"name": name, "dose": dose, "dose_unit": dose_unit, **kw}


def _formula(herb_items, **overrides):
    base = dict(name="测试方", source="composed", confidence="medium", rationale="r")
    base.update(overrides)
    return {**base, "herb_items": herb_items}


# ---------- /api/prescription/validate：五类问题各一条 ----------


def test_validate_reports_incompatible_pair(client):
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb("甘草"), _herb("海藻")],
        "syndrome": "脾胃气虚",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["incompatible"] == [["甘草", "海藻"]]
    assert body["blocking"] is True


def test_validate_reports_dose_violation(client):
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb("附子", dose=20)],
        "syndrome": "脾胃气虚",
    })
    body = resp.json()
    assert len(body["dose_violations"]) == 1
    assert body["dose_violations"][0]["herb"] == "附子"
    assert body["blocking"] is True


def test_validate_reports_decoction_missing(client):
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb("附子", dose=5, decoction=None)],
        "syndrome": "脾胃气虚",
    })
    body = resp.json()
    assert body["decoction_missing"] == ["附子"]
    # 缺煎法是警告级，不拦截——跟剂量超限（拦截级）不是同一档
    assert body["blocking"] is False


def test_validate_reports_toxic_herb(client):
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb("附子", dose=5, decoction="先煎")],
        "syndrome": "脾胃气虚",
    })
    body = resp.json()
    assert body["toxic_herbs"] == ["附子"]
    assert body["blocking"] is False  # 毒性提示同样是警告级


def test_validate_reports_thermal_warning(client):
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb(n) for n in ["黄连", "黄芩", "石膏", "知母", "白术", "茯苓"]],
        "syndrome": "脾胃虚寒证",
    })
    body = resp.json()
    assert body["thermal_warning"] is not None and "脾胃虚寒证" in body["thermal_warning"]
    assert body["blocking"] is False  # 寒热警告也是警告级


def test_validate_clean_formula_has_no_problems(client):
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb("党参"), _herb("白术")],
        "syndrome": "脾胃气虚",
    })
    body = resp.json()
    assert body == {
        "incompatible": [], "thermal_warning": None, "dose_violations": [],
        "decoction_missing": [], "toxic_herbs": [], "blocking": False,
    }


def test_validate_accepts_empty_herb_items():
    """可编辑处方表从空表开始，医生删到只剩 0 味药时前端仍可能调一次
    校验——不该被拒绝，应该诚实返回一个全空的结果。"""
    client = TestClient(api_main.app)
    resp = client.post("/api/prescription/validate", json={"herb_items": [], "syndrome": "脾胃气虚"})
    assert resp.status_code == 200
    assert resp.json()["blocking"] is False


def test_validate_rejects_empty_syndrome(client):
    resp = client.post("/api/prescription/validate", json={"herb_items": [], "syndrome": ""})
    assert resp.status_code == 422


def test_validate_does_not_call_llm(client, monkeypatch):
    """纯规则、不调 LLM——把 get_llm 换成一个调用即报错的桩，跑一次 validate
    如果真的触发了 LLM 调用，测试会因为这个桩爆炸而失败。"""
    import core.llm as llm_mod

    def _boom():
        raise AssertionError("validate 不应该调 LLM")

    monkeypatch.setattr(llm_mod, "get_llm", _boom)
    resp = client.post("/api/prescription/validate", json={
        "herb_items": [_herb("党参")], "syndrome": "脾胃气虚",
    })
    assert resp.status_code == 200


# ---------- /api/prescription/export ----------


def test_export_succeeds_for_clean_formula_and_records_audit(client):
    model_suggestion = _formula([_herb("党参", dose=9)], name="四君子汤")
    final = _formula([_herb("党参", dose=15)], name="四君子汤加减")
    resp = client.post("/api/prescription/export", json={
        "formula": final, "doctor_id": "dr_ye", "patient_ref": "patient-001",
        "model_suggestion": model_suggestion,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "党参" in body["text"] and "15g" in body["text"]
    assert body["audit_id"] == "1"

    ok, problems = audit.verify_audit_chain()
    assert ok is True, problems


def test_export_blocking_without_override_reason_is_rejected(client):
    """safety.blocking 为真时拒绝导出，返回 422 并列出问题。"""
    formula = _formula([_herb("甘草", dose=5), _herb("海藻", dose=5)])
    resp = client.post("/api/prescription/export", json={
        "formula": formula, "doctor_id": "dr_ye", "patient_ref": None,
        "model_suggestion": formula,
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any("配伍禁忌" in p for p in detail["problems"])
    assert detail["safety"]["blocking"] is True

    # 拒绝导出不应该留下审计记录——被拒的请求不构成一次真实导出。
    ok, problems = audit.verify_audit_chain()
    assert ok is True and problems == []
    assert not audit.AUDIT_PATH.exists() or audit.AUDIT_PATH.read_text(encoding="utf-8").strip() == ""


def test_export_blocking_with_override_reason_succeeds_and_is_recorded(client):
    """医生要坚持导出，必须传非空 override_reason，这条理由要进审计日志
    ——它是"医生明知有问题仍坚持导出"唯一的书面记录。"""
    formula = _formula([_herb("甘草", dose=5), _herb("海藻", dose=5)])
    resp = client.post("/api/prescription/export", json={
        "formula": formula, "doctor_id": "dr_ye", "patient_ref": "patient-002",
        "model_suggestion": formula,
        "override_reason": "医生临床判断该配伍禁忌在此证型下可控，坚持使用",
    })
    assert resp.status_code == 200

    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    import json
    record = json.loads(lines[0])
    assert record["override_reason"] == "医生临床判断该配伍禁忌在此证型下可控，坚持使用"
    assert record["safety_at_export"]["blocking"] is True


def test_export_blocking_with_whitespace_only_override_reason_is_still_rejected(client):
    """override_reason 传了但只有空白字符——跟没传等价，不能靠传几个空格
    绕过拦截。"""
    formula = _formula([_herb("甘草", dose=5), _herb("海藻", dose=5)])
    resp = client.post("/api/prescription/export", json={
        "formula": formula, "doctor_id": "dr_ye", "patient_ref": None,
        "model_suggestion": formula, "override_reason": "   ",
    })
    assert resp.status_code == 422


def test_export_ignores_client_supplied_safety_and_recomputes_server_side(client):
    """客户端在 formula 里带一个自己编的、说"没有任何问题"的 safety 字段，
    不能凭这个绕过拦截——安全判定必须是服务端权威重新算出来的，这条测试
    钉住"客户端自己说安全"不作数。"""
    formula = _formula(
        [_herb("甘草", dose=5), _herb("海藻", dose=5)],
        safety={"incompatible": [], "thermal_warning": None, "dose_violations": [],
                "decoction_missing": [], "toxic_herbs": [], "blocking": False},
    )
    resp = client.post("/api/prescription/export", json={
        "formula": formula, "doctor_id": "dr_ye", "patient_ref": None,
        "model_suggestion": formula,
    })
    assert resp.status_code == 422


def test_export_computes_diffs_between_model_suggestion_and_final(client):
    model_suggestion = _formula([_herb("附子", dose=10)])
    final = _formula([_herb("附子", dose=15)])
    resp = client.post("/api/prescription/export", json={
        "formula": final, "doctor_id": "dr_ye", "patient_ref": None,
        "model_suggestion": model_suggestion,
    })
    assert resp.status_code == 200
    lines = audit.AUDIT_PATH.read_text(encoding="utf-8").strip().split("\n")
    import json
    record = json.loads(lines[0])
    assert "附子 10g→15g" in record["diffs"]


def test_export_rejects_missing_doctor_id(client):
    formula = _formula([_herb("党参")])
    resp = client.post("/api/prescription/export", json={
        "formula": formula, "doctor_id": "", "patient_ref": None,
        "model_suggestion": formula,
    })
    assert resp.status_code == 422
