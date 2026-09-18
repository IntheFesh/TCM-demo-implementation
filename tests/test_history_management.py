"""R46 §7.5 第 14 条：问诊历史、收藏与模板、统计。"""
import json

from fastapi.testclient import TestClient

import api.main as api_main
from core import history


def test_a_consult_is_recorded_with_its_record_number(tmp_path):
    p = tmp_path / "h.jsonl"
    row = history.record_consult(doctor_id="dr_a", record_id="AB12CD34",
                                 complaint="胃脘胀痛", syndrome="肝胃不和证",
                                 formula="柴胡疏肝散", path=p)
    assert row["record_id"] == "AB12CD34" and row["at"]
    assert history.list_consults(doctor_id="dr_a", path=p)[0]["formula"] == "柴胡疏肝散"


def test_only_a_summary_is_stored_not_the_whole_prescription(tmp_path):
    """完整轨迹在审计链里，按 `record_id` 回查；两处各存一份会让
    "改了哪一份才算数"没有答案。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="dr_a", record_id="X", complaint="x" * 500, path=p)
    row = json.loads(p.read_text(encoding="utf-8").strip())
    assert len(row["complaint"]) <= 120
    assert "herb_items" not in row


def test_history_filters_by_doctor_syndrome_formula_and_date(tmp_path):
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c",
                           syndrome="肝胃不和证", formula="柴胡疏肝散", path=p)
    history.record_consult(doctor_id="b", record_id="2", complaint="c",
                           syndrome="脾虚证", formula="四君子汤", path=p)
    assert len(history.list_consults(doctor_id="a", path=p)) == 1
    assert len(history.list_consults(syndrome="脾虚", path=p)) == 1
    assert len(history.list_consults(formula="四君子", path=p)) == 1
    assert len(history.list_consults(since="2999-01-01", path=p)) == 0


def test_the_newest_consult_comes_first(tmp_path):
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c",
                           at="2020-01-01T00:00:00+00:00", path=p)
    history.record_consult(doctor_id="a", record_id="2", complaint="c",
                           at="2030-01-01T00:00:00+00:00", path=p)
    assert history.list_consults(doctor_id="a", path=p)[0]["record_id"] == "2"


def test_a_half_written_line_does_not_break_the_whole_history(tmp_path):
    """写到一半掉电：跳过那一行，**不让整份历史读不出来**。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c", path=p)
    with p.open("a", encoding="utf-8") as f:
        f.write('{"record_id": "brok\n')
    assert len(history.list_consults(doctor_id="a", path=p)) == 1


def test_favorites_carry_the_doctors_own_note(tmp_path):
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="我的柴胡疏肝散",
                         herbs=["柴胡", "白芍"], note="脾虚者去枳壳", path=p)
    rows = history.list_favorites(doctor_id="a", path=p)
    assert rows[0]["note"] == "脾虚者去枳壳"


def test_favorites_are_per_doctor(tmp_path):
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="甲", herbs=[], path=p)
    history.add_favorite(doctor_id="b", name="乙", herbs=[], path=p)
    assert len(history.list_favorites(doctor_id="a", path=p)) == 1


def test_every_statistic_carries_its_denominator(tmp_path):
    """「肝胃不和证 3 次」在 5 次里和在 50 次里是两件完全不同的事
    （CLAUDE.md：任何数字都必须带对照）。"""
    p = tmp_path / "h.jsonl"
    for i in range(3):
        history.record_consult(doctor_id="a", record_id=str(i), complaint="c",
                               syndrome="肝胃不和证", path=p)
    s = history.stats(doctor_id="a", path=p)
    assert s["n"] == 3
    assert s["syndromes"][0] == {"name": "肝胃不和证", "count": 3, "of": 3}


def test_the_statistics_say_they_are_not_a_performance_metric(tmp_path):
    """**给医师自己反思用的，不是考核指标**——一个会被拿去考核的统计，
    医师会开始为它而开方。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c", path=p)
    note = history.stats(doctor_id="a", path=p)["note"]
    assert "不是考核" in note and "不排名" in note


def test_statistics_on_an_empty_history_say_so(tmp_path):
    s = history.stats(doctor_id="nobody", path=tmp_path / "h.jsonl")
    assert s["n"] == 0 and "还没有问诊记录" in s["note"]


def test_the_emr_store_returns_the_latest_version(tmp_path):
    p = tmp_path / "e.jsonl"
    history.save_emr("R1", {"v": 1}, path=p)
    history.save_emr("R1", {"v": 2}, path=p)
    assert history.get_emr("R1", path=p)["draft"] == {"v": 2}
    assert len(history.emr_versions("R1", path=p)) == 2


def test_a_missing_emr_is_none_not_an_empty_document(tmp_path):
    assert history.get_emr("NOPE", path=tmp_path / "e.jsonl") is None


def test_the_endpoints_are_reachable():
    client = TestClient(api_main.app)
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/history/favorites").status_code == 200
    assert client.get("/api/history/stats").status_code == 200


def test_a_favorite_without_a_name_is_rejected():
    client = TestClient(api_main.app)
    r = client.post("/api/history/favorite", json={"doctor_id": "a", "name": "  "})
    assert r.status_code == 400
