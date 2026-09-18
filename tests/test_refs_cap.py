"""R40：响应体里的参考医案条数上限。**这一轮最大的一个数就在这里。**

实测（`full_context` 模式、1060 条医案的语料）：一次问诊的响应体
**2,848,127 字节**，其中 `results[0].refs` 占 **1,252,722 字节 / 1060 条**，
是响应体的 **98.9%**。裁到 20 条之后 **83,729 字节（−97.1%）**。

为什么这是个真问题而不是"多传一点没关系"：
  · 前端要 JSON.parse 这 2.8 MB（主线程上一次长任务）
  · 1060 条参考医案没有任何界面能有意义地展示
  · 三甲内网的带宽不是本机回环

这个文件钉住三件事：**被引用的一条都不许丢**、**对照数必须在**、
**裁剪只改"下发多少"不改"验了什么"**。
"""
from __future__ import annotations

import pytest

import api.main as api_main
from api.main import REFS_IN_RESPONSE, _cap_refs


def _refs(n: int, *, prefix: str = "ye_tianshi") -> list[dict]:
    """n 条参考医案，分数递减（第 0 条最高）。"""
    return [{"case_id": f"{prefix}-{i:04d}", "score": round(1.0 - i * 0.001, 4),
             "symptoms": ["纳差"], "syndrome": "脾胃气虚证"} for i in range(n)]


def test_the_default_cap_is_small_enough_to_matter():
    """20 条是"界面摆得下、还能翻一翻"的量级。几百条就等于没裁。"""
    assert 1 <= REFS_IN_RESPONSE <= 50


def test_below_the_cap_nothing_is_touched():
    refs = _refs(5)
    sent, counts = _cap_refs(refs, [], cap=20)
    assert sent == refs
    assert counts == {"refs_total": 5, "refs_sent": 5, "refs_cited": 0,
                      "refs_truncated": False}


def test_above_the_cap_it_keeps_exactly_cap_rows():
    sent, counts = _cap_refs(_refs(1060), [], cap=20)
    assert len(sent) == 20
    assert counts["refs_total"] == 1060
    assert counts["refs_sent"] == 20
    assert counts["refs_truncated"] is True


def test_cited_cases_are_never_dropped_even_if_their_score_is_last():
    """**这一条是这个函数存在的理由。** 被结论引用的医案丢了的话，
    前端"点结论跳到依据"会指向一条不存在的医案——可追溯是这个项目的卖点。"""
    refs = _refs(1060)
    cited = ["ye_tianshi-1059", "ye_tianshi-0900"]      # 分数最低的两条
    sent, counts = _cap_refs(refs, cited, cap=20)
    ids = [r["case_id"] for r in sent]
    assert set(cited) <= set(ids)
    assert counts["refs_cited"] == 2
    assert len(sent) == 20, "被引用的占了名额，总数仍然不超过上限"


def test_cited_rows_come_first_so_the_sidebar_shows_them_without_scrolling():
    refs = _refs(100)
    sent, _ = _cap_refs(refs, ["ye_tianshi-0099"], cap=5)
    assert sent[0]["case_id"] == "ye_tianshi-0099"


def test_the_rest_is_filled_by_score_descending():
    refs = _refs(100)
    sent, _ = _cap_refs(refs, [], cap=3)
    assert [r["case_id"] for r in sent] == ["ye_tianshi-0000", "ye_tianshi-0001",
                                            "ye_tianshi-0002"]


def test_more_cited_rows_than_the_cap_still_keeps_them_all():
    """上限跟"不许丢被引用的"冲突时，**后者赢**。
    宁可多下发几条，不可让追溯断链。"""
    refs = _refs(30)
    cited = [r["case_id"] for r in refs[:25]]
    sent, counts = _cap_refs(refs, cited, cap=5)
    assert len(sent) == 25
    assert counts["refs_cited"] == 25 and counts["refs_sent"] == 25


def test_cap_zero_or_negative_means_no_cap():
    """给评测脚本留的口子：它们要全量。"""
    refs = _refs(50)
    for cap in (0, -1):
        sent, counts = _cap_refs(refs, [], cap=cap)
        assert len(sent) == 50 and counts["refs_truncated"] is False


def test_an_empty_ref_list_is_not_a_special_case():
    sent, counts = _cap_refs([], [], cap=20)
    assert sent == []
    assert counts == {"refs_total": 0, "refs_sent": 0, "refs_cited": 0,
                      "refs_truncated": False}


def test_counts_always_carry_their_own_denominator():
    """项目铁律「任何数字都必须带对照」在这里的落点：只给 `refs` 不给
    `refs_total` 的话，前端会把"下发的条数"当成"检索到的条数"，那是个假数。"""
    _sent, counts = _cap_refs(_refs(1060), ["ye_tianshi-0001"], cap=20)
    assert set(counts) == {"refs_total", "refs_sent", "refs_cited", "refs_truncated"}
    assert counts["refs_total"] > counts["refs_sent"]


def test_cited_ids_that_are_not_in_refs_do_not_crash_or_inflate_the_count():
    """幻觉出来的 case_id（模型编的）不在 refs 里。它不该让 `refs_cited` 变大
    ——那个数说的是"下发的里面有几条是被引用的"。"""
    sent, counts = _cap_refs(_refs(30), ["编出来的-9999"], cap=5)
    assert counts["refs_cited"] == 0
    assert len(sent) == 5


def test_none_scores_do_not_break_the_sort():
    """`score` 缺失的条目（旧数据、别的路径塞进来的）排到最后，不抛。"""
    refs = [{"case_id": "a"}, {"case_id": "b", "score": 0.9}]
    sent, _ = _cap_refs(refs, [], cap=1)
    assert sent[0]["case_id"] == "b"


# ---------- 端到端：真的走一次响应序列化 ----------


def test_the_response_carries_the_counts_next_to_the_refs(monkeypatch):
    """序列化边界上真的挂上去了——单测函数对了但没接线，是这类改动最常见的漏法。"""
    from fastapi.testclient import TestClient

    from tests.test_api import _rich_outcome

    outcome = _rich_outcome()
    outcome["results"][0]["refs"] = _refs(200)
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "胃脘胀痛"}).json()
    row = body["results"][0]
    assert row["refs_total"] == 200
    assert row["refs_sent"] == len(row["refs"]) == REFS_IN_RESPONSE
    assert row["refs_truncated"] is True


def test_capping_does_not_change_what_was_verified(monkeypatch):
    """`hallucinated` 是**服务端**用全集算完的结论。裁剪只改"下发多少"，
    不改"验了什么"——如果裁剪影响了它，幻觉检查就被削弱了。"""
    from fastapi.testclient import TestClient

    from tests.test_api import _rich_outcome

    outcome = _rich_outcome()
    outcome["results"][0]["refs"] = _refs(200)
    outcome["results"][0]["hallucinated"] = ["编出来的-0001"]
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    body = TestClient(api_main.app).post(
        "/api/consult", json={"complaint": "胃脘胀痛"}).json()
    assert body["results"][0]["hallucinated"] == ["编出来的-0001"]


def test_patient_role_still_gets_no_refs_at_all(monkeypatch):
    """裁剪不是角色裁剪的替代品。患者角色一条 refs 都不给（原有边界不许松）。"""
    from fastapi.testclient import TestClient

    from tests.test_api import _rich_outcome

    outcome = _rich_outcome()
    outcome["results"][0]["refs"] = _refs(200)
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: outcome)
    body = TestClient(api_main.app).post(
        "/api/consult", json={"complaint": "胃脘胀痛", "role": "patient"}).json()
    assert body["results"][0]["refs"] == []


def test_the_frontend_shows_both_numbers_not_just_the_sent_one():
    """前端那行小字必须两个数一起说。只显示下发条数就是在界面上给一个假数。"""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "web" / "app.js"
    text = src.read_text(encoding="utf-8")
    assert "refs_total" in text
    assert "refs_truncated" in text
    assert "本次检索到" in text


@pytest.mark.parametrize("n,cap,expect", [(1060, 20, 20), (21, 20, 20), (20, 20, 20),
                                          (19, 20, 19), (0, 20, 0)])
def test_the_boundary_around_the_cap(n, cap, expect):
    sent, _ = _cap_refs(_refs(n), [], cap=cap)
    assert len(sent) == expect
