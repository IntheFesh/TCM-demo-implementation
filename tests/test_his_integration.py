"""R46 §7.5 第 15 条：HIS 集成接口（两个端点 + OpenAPI + 鉴权 + 字段对齐）。

对标电子病历系统功能应用水平分级评价 4/5 级：全院统一知识库、集成展示、
决策支持、病历结构化智能化书写。**接口必须能被 HIS 集成，不能是孤岛。**
"""
import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import history
from core.integration_auth import (
    API_KEYS_ENV,
    IP_ALLOWLIST_ENV,
    IntegrationDenied,
    check,
    status,
)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PRODUCT_MODE", "0")
    return TestClient(api_main.app)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(API_KEYS_ENV, "k-good,k-other")
    monkeypatch.delenv(IP_ALLOWLIST_ENV, raising=False)


# ---------- 鉴权 ----------

def test_the_default_is_deny_not_allow(monkeypatch):
    """**默认拒绝。** 一个默认放行的鉴权在内网里等于没有鉴权，
    而医院内网并不比公网干净。"""
    monkeypatch.delenv(API_KEYS_ENV, raising=False)
    with pytest.raises(IntegrationDenied):
        check("anything", "127.0.0.1")


def test_a_good_key_passes(keyed):
    assert check("k-good", "127.0.0.1") is None


def test_a_bad_key_is_rejected(keyed):
    with pytest.raises(IntegrationDenied):
        check("k-wrong", "127.0.0.1")


def test_a_missing_key_is_rejected(keyed):
    with pytest.raises(IntegrationDenied):
        check("", "127.0.0.1")


def test_the_ip_allowlist_narrows_further(keyed, monkeypatch):
    monkeypatch.setenv(IP_ALLOWLIST_ENV, "10.0.0.1")
    assert check("k-good", "10.0.0.1") is None
    with pytest.raises(IntegrationDenied):
        check("k-good", "10.0.0.2")


def test_an_empty_ip_allowlist_means_no_ip_restriction(keyed):
    """key 是必配的，IP 是加固项——**两者的默认值方向不同**，理由见模块文档。"""
    assert check("k-good", "8.8.8.8") is None


def test_the_key_comparison_is_constant_time():
    """字符串相等是短路比较，逐字节的耗时差可以被用来把 key 试出来。"""
    from pathlib import Path

    src = Path("core/integration_auth.py").read_text(encoding="utf-8")
    assert "compare_digest" in src


def test_the_status_never_echoes_the_key(keyed):
    s = status()
    assert s == {"keys_configured": 2, "ip_allowlist": 0, "enabled": True}
    assert "k-good" not in str(s)


def test_the_http_reason_does_not_tell_the_prober_what_to_try_next(client, keyed):
    """「key 不对」和「IP 不在白名单」的区别会告诉试探者下一步该试什么。"""
    r1 = client.post("/api/integration/consult", json={"chief_complaint": "x"},
                     headers={"X-API-Key": "k-wrong"})
    r2 = client.post("/api/integration/consult", json={"chief_complaint": "x"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["detail"] == r2.json()["detail"] == "鉴权失败。"


# ---------- 两个端点 ----------

def test_the_consult_endpoint_needs_a_key(client):
    assert client.post("/api/integration/consult",
                       json={"chief_complaint": "胃脘胀痛"}).status_code == 401


def test_the_emr_endpoint_needs_a_key(client):
    assert client.get("/api/integration/emr/ANY").status_code == 401


def test_the_emr_endpoint_returns_the_structured_document(client, keyed, tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(history, "EMR_PATH", tmp_path / "e.jsonl")
    history.save_emr("ZZ11YY22", {"record_id": "ZZ11YY22", "sections": []})
    r = client.get("/api/integration/emr/ZZ11YY22", headers={"X-API-Key": "k-good"})
    assert r.status_code == 200
    assert r.json()["record_id"] == "ZZ11YY22"


def test_the_request_fields_align_with_his_terms():
    """字段命名与常用 HIS 术语对齐：患者主索引、就诊流水号、主诉、现病史。"""
    fields = set(api_main.IntegrationConsultRequest.model_fields)
    for must in ("patient_id", "visit_id", "chief_complaint", "present_illness"):
        assert must in fields


def test_the_integration_request_also_takes_the_person_dimension():
    fields = set(api_main.IntegrationConsultRequest.model_fields)
    assert "patient_profile" in fields and "intake" in fields


def test_the_integration_request_rejects_objective_data(client, keyed):
    """§0.4 的输入侧边界对集成接口同样成立——HIS 那头最容易顺手把检验值带过来。"""
    r = client.post("/api/integration/consult",
                    json={"chief_complaint": "x", "lab_value": {"ALT": 40}},
                    headers={"X-API-Key": "k-good"})
    assert r.status_code == 400
    assert "医疗器械" in r.json()["detail"]


# ---------- OpenAPI 文档 ----------

def test_the_openapi_document_is_version_3():
    spec = api_main.app.openapi()
    assert spec["openapi"].startswith("3.")


def test_both_integration_paths_are_documented():
    paths = api_main.app.openapi()["paths"]
    assert "/api/integration/consult" in paths
    assert "/api/integration/emr/{record_id}" in paths


def test_the_openapi_title_carries_the_product_name_and_version():
    from core.version import PRODUCT_NAME, VERSION

    info = api_main.app.openapi()["info"]
    assert info["title"] == PRODUCT_NAME and info["version"] == VERSION
