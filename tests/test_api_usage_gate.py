"""D1 在 API 层的行为：BYOK / 共享额度 / 降级 / 用量看板。

钉住的判据（都来自交接文档「部署」那一节）：
  · 自带 key 的请求不占站点额度，而且 key 不出现在任何响应里
  · 额度按 llm_calls 结算，不是按请求数
  · 超限**降级到 replay**，不是报 4xx
  · 闸门在调模型之前——被拦的请求一次 LLM 都不调
  · FORCE_REPLAY 是手动熔断，压过一切
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import usage as usage_mod
from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome


def _outcome(llm_calls: int = 5) -> dict:
    s1 = S1Normalize(symptoms=["纳差"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(elements=[
        ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")
    ])
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="纳差", treatment_principle="健脾",
                    herbs=["党参"], cited_case_ids=["ye_tianshi-001"])
    return {
        "s1": s1,
        "results": [{"physician": "ye_tianshi", "physician_name": "叶天士", "s2": s2,
                     "s3": s3, "refs": [], "hallucinated": []}],
        "divergence": {"same": True, "method": "pairwise_herb_jaccard"},
        "rejected": False, "reject_reason": None,
        "manifest": {"llm_calls": llm_calls},
    }


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("FORCE_REPLAY", raising=False)
    usage_mod.reset_ledger()
    yield TestClient(api_main.app)
    usage_mod.reset_ledger()


@pytest.fixture
def seen(monkeypatch):
    """记录每次问诊实际用到的后端类名和调用时的 key。"""
    from core.llm import get_llm

    log: list[dict] = []

    def fake_consult(complaint, **kw):
        backend = get_llm()
        entry = {"backend": type(backend).__name__}
        api_key = getattr(backend, "_api_key", None)
        entry["key"] = api_key() if callable(api_key) else None
        log.append(entry)
        return _outcome()

    monkeypatch.setattr(api_main, "consult", fake_consult)
    return log


def test_usage_endpoint_reports_the_ledger_without_spending_anything(client, seen):
    r = client.get("/api/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "shared"
    assert body["remaining_calls"] == body["ip_limit_calls"]
    assert body["calls_per_consult"] == usage_mod.CALLS_PER_CONSULT
    assert body["remaining_consults_estimate"] >= 1
    assert seen == [], "看板不该触发任何一次问诊"


def test_own_key_is_used_for_the_call_and_never_echoed_back(client, seen):
    r = client.post("/api/consult", json={"complaint": "纳差乏力"},
                    headers={"X-LLM-Key": "sk-visitor-secret"})
    assert r.status_code == 200
    assert seen[0]["backend"] == "ByokBackend"
    assert seen[0]["key"] == "sk-visitor-secret"
    # key 绝不能出现在响应体或响应头里
    assert "sk-visitor-secret" not in r.text
    assert all("sk-visitor-secret" not in v for v in r.headers.values())
    assert r.headers["X-Usage-Mode"] == "byok"
    # 自带 key 不占站点额度
    assert client.get("/api/usage").json()["ip_used_calls"] == 0


def test_shared_pool_charges_the_real_llm_calls_not_the_estimate(client, monkeypatch):
    """按 llm_calls 计量：一次只花了 3 次调用，账上就该记 3，不是预占的估算值。"""
    monkeypatch.setattr(api_main, "consult", lambda c, **kw: _outcome(llm_calls=3))
    client.post("/api/consult", json={"complaint": "纳差乏力"})
    assert client.get("/api/usage").json()["ip_used_calls"] == 3


def test_a_request_that_never_called_the_model_costs_nothing(client, monkeypatch):
    """安全否决走在 S2 之前，一次模型都没调——不该扣额度。"""
    monkeypatch.setattr(api_main, "consult", lambda c, **kw: _outcome(llm_calls=0))
    client.post("/api/consult", json={"complaint": "解黑色柏油样便"})
    assert client.get("/api/usage").json()["ip_used_calls"] == 0


def test_over_quota_degrades_to_replay_instead_of_returning_an_error(client, seen, monkeypatch):
    monkeypatch.setenv("QUOTA_PER_IP_DAILY_CALLS", "4")
    usage_mod.reset_ledger()
    client.post("/api/consult", json={"complaint": "纳差乏力"})   # 记 5 > 4
    r = client.post("/api/consult", json={"complaint": "纳差乏力"})
    assert r.status_code == 200, "超限是降级，不是报错"
    assert r.headers["X-Usage-Mode"] == "replay"
    assert seen[-1]["backend"] == "ReplayBackend"


def test_force_replay_is_a_breaker_that_beats_even_byok(client, seen, monkeypatch):
    monkeypatch.setenv("FORCE_REPLAY", "1")
    usage_mod.reset_ledger()
    r = client.post("/api/consult", json={"complaint": "纳差乏力"},
                    headers={"X-LLM-Key": "sk-visitor"})
    assert seen[-1]["backend"] == "ReplayBackend"
    assert r.headers["X-Usage-Mode"] == "replay"


def test_x_forwarded_for_is_ignored_by_default(client, monkeypatch):
    """**默认不读这个头**。MDN：跟安全相关的 XFF 用法只能用受信代理添加的地址，
    而最左那一跳是客户端自己能写的——取最左等于把限流的 key 交给攻击者：
        for i in $(seq 1 10000); do curl -H "X-Forwarded-For: 随机" …; done
    每条都算成新 IP，按 IP 的额度形同虚设。"""
    monkeypatch.setattr(api_main, "_consult_slots", __import__("threading").BoundedSemaphore(4))
    monkeypatch.setattr(api_main, "consult", lambda c, **kw: _outcome(llm_calls=5))
    monkeypatch.setattr(api_main, "TRUSTED_PROXY_HOPS", 0)
    for fake in ["1.1.1.1", "2.2.2.2", "3.3.3.3"]:
        client.post("/api/consult", json={"complaint": "纳差"}, headers={"X-Forwarded-For": fake})
    # 三次都记在同一个桶上（TCP 对端），而不是三个"新 IP"各记一次
    assert client.get("/api/usage", headers={"X-Forwarded-For": "9.9.9.9"}).json()["ip_used_calls"] == 15


def test_with_trusted_hops_the_client_ip_is_counted_from_the_right(client, monkeypatch):
    """配了 N 层受信反代就从右数第 N 跳取——右边那些是反代自己追加的。"""
    monkeypatch.setattr(api_main, "consult", lambda c, **kw: _outcome(llm_calls=5))
    monkeypatch.setattr(api_main, "TRUSTED_PROXY_HOPS", 1)
    # 攻击者伪造了最左边那个；真正的客户端地址是反代追加的最右边那个
    hdr = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"}
    client.post("/api/consult", json={"complaint": "纳差"}, headers=hdr)
    assert client.get("/api/usage", headers=hdr).json()["ip_used_calls"] == 5
    # 伪造的那个不该有自己的额度桶
    spoof = {"X-Forwarded-For": "6.6.6.6, 198.51.100.1"}
    assert client.get("/api/usage", headers=spoof).json()["ip_used_calls"] == 0


def test_a_forged_value_that_is_not_even_an_ip_falls_back_to_the_peer(client, monkeypatch):
    """MDN 专门提醒过：伪造的值可能根本不是 IP。"""
    monkeypatch.setattr(api_main, "TRUSTED_PROXY_HOPS", 1)
    r = client.get("/api/usage", headers={"X-Forwarded-For": "not-an-ip"})
    assert r.status_code == 200


def test_ipv6_clients_are_bucketed_by_their_64(client, monkeypatch):
    """一个普通家宽用户手上就有整个 /64，不归一等于 IPv6 客户端天然免限流。"""
    monkeypatch.setattr(api_main, "consult", lambda c, **kw: _outcome(llm_calls=5))
    monkeypatch.setattr(api_main, "TRUSTED_PROXY_HOPS", 1)
    client.post("/api/consult", json={"complaint": "纳差"},
                headers={"X-Forwarded-For": "2001:db8:1:2::1"})
    same64 = client.get("/api/usage", headers={"X-Forwarded-For": "2001:db8:1:2::ffff"}).json()
    assert same64["ip_used_calls"] == 5


def test_the_ip_bucket_count_is_capped_so_rotation_cannot_exhaust_memory(client, monkeypatch):
    """MDN 把\"内存耗尽\"跟\"限流被绕过\"并列，是同一个成因。"""
    monkeypatch.setattr(api_main, "TRUSTED_PROXY_HOPS", 1)
    monkeypatch.setattr(api_main, "MAX_TRACKED_IPS", 3)
    for i in range(50):
        client.get("/api/usage", headers={"X-Forwarded-For": f"203.0.113.{i}"})
    assert len(usage_mod.get_ledger()._by_ip) <= 3 + 1  # 3 个真桶 + 一个溢出桶


def test_the_stream_tells_the_client_about_degradation_in_the_first_frame(client, monkeypatch):
    """降级到回放这件事要在推理开始**之前**让人知道，不是等结果出来才发现对不上。"""
    monkeypatch.setenv("FORCE_REPLAY", "1")
    usage_mod.reset_ledger()
    monkeypatch.setattr(api_main, "consult", lambda c, **kw: _outcome())
    with client.stream("POST", "/api/consult/stream", json={"complaint": "纳差乏力"}) as resp:
        first = ""
        for chunk in resp.iter_text():
            first += chunk
            if "\n\n" in first:
                break
    assert "stream_id" in first and '"degraded": true' in first.replace(" ", " ")


def test_the_backend_override_does_not_leak_into_the_next_request(client, seen):
    """BYOK 用的是 ContextVar 逐请求覆盖，不能污染下一个请求。"""
    client.post("/api/consult", json={"complaint": "a"}, headers={"X-LLM-Key": "sk-1"})
    client.post("/api/consult", json={"complaint": "b"})
    assert seen[0]["backend"] == "ByokBackend"
    assert seen[1]["backend"] != "ByokBackend"


def test_a_503_from_the_concurrency_gate_refunds_the_reservation(client, monkeypatch):
    """并发位满 → 503，一次模型都没调。预占不退的话，每一次 503 都把估算永久
    挂在账上，并发一满额度就被慢慢吃光。"""
    import threading

    monkeypatch.setattr(api_main, "_consult_slots", threading.BoundedSemaphore(1))
    api_main._consult_slots.acquire()  # 占满
    r = client.post("/api/consult", json={"complaint": "纳差乏力"})
    assert r.status_code == 503
    assert client.get("/api/usage").json()["ip_used_calls"] == 0


def test_an_invalid_key_is_reported_to_the_visitor_not_hidden_behind_an_error_id(
    client, monkeypatch
):
    """401/402 是访问者自己能修的，不能裹进\"服务端处理失败（错误编号 xxxx）\"。"""
    from core.llm import LLMAuthError

    def boom(complaint, **kw):
        raise LLMAuthError("API key 认证失败（HTTP 401）：这把 key 不正确或已失效。", 401)

    monkeypatch.setattr(api_main, "consult", boom)
    r = client.post("/api/consult", json={"complaint": "纳差乏力"},
                    headers={"X-LLM-Key": "sk-wrong"})
    assert r.status_code == 502
    assert "401" in r.json()["detail"] and "key" in r.json()["detail"]
    assert "错误编号" not in r.json()["detail"]
    assert "sk-wrong" not in r.text


def test_validate_key_uses_the_official_balance_endpoint_and_never_echoes_the_key(
    client, monkeypatch
):
    seen = {}

    def fake_check(api_key, base_url=None, timeout=10.0):
        seen["key"] = api_key
        return {"valid": True, "is_available": True,
                "balances": [{"currency": "CNY", "total_balance": "12.34"}], "reason": ""}

    monkeypatch.setattr(api_main, "check_api_key", fake_check)
    r = client.post("/api/usage/validate-key", headers={"X-LLM-Key": "sk-abc"})
    assert r.status_code == 200 and r.json()["valid"] is True
    assert seen["key"] == "sk-abc"
    assert "sk-abc" not in r.text


def test_validate_key_without_a_key_is_a_400(client):
    assert client.post("/api/usage/validate-key").status_code == 400


def test_a_run_that_started_but_produced_no_manifest_still_gets_charged(client):
    """客户端中途关掉标签页：consult 抛 StreamClosed，outcome 是 None，可 S1/S2
    的钱已经花了。按 0 退的话，开着流跑一半关掉就等于白嫖。"""
    token = usage_mod.get_ledger().reserve("1.2.3.4", 20)
    api_main._settle(token, None)
    assert usage_mod.get_ledger().snapshot("1.2.3.4")["ip_used_calls"] == 20


def test_a_run_that_never_touched_the_model_is_refunded_in_full(client):
    token = usage_mod.get_ledger().reserve("1.2.3.4", 20)
    api_main._refund(token)
    assert usage_mod.get_ledger().snapshot("1.2.3.4")["ip_used_calls"] == 0
