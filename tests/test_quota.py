"""offline/quota.py 的离线测试。"""
import json

import pytest

from core.schemas import CaseRecord
from offline import quota


def _case(case_id, group_id, physician, visit_index=0):
    return CaseRecord(
        case_id=case_id, case_group_id=group_id, physician=physician,
        raw="x", visit_index=visit_index,
    )


def test_audit_counts_multi_visit_cases_by_group_size_ge_two():
    cases = [
        _case("a0", "g1", "ye_tianshi", 0), _case("a1", "g1", "ye_tianshi", 1),  # 复诊组，2 条都算
        _case("b0", "g2", "ye_tianshi", 0),  # 单诊，不算
    ]
    r = quota.audit(cases, min_total=10, min_multi_visit=1)
    assert r["ye_tianshi"]["total_cases"] == 3
    assert r["ye_tianshi"]["n_patients"] == 2
    assert r["ye_tianshi"]["multi_visit_cases"] == 2


def test_audit_meets_flags_reflect_thresholds():
    cases = [_case(f"a{i}", f"g{i}", "ye_tianshi") for i in range(5)]
    r = quota.audit(cases, min_total=10, min_multi_visit=1)
    assert r["ye_tianshi"]["meets_total_quota"] is False
    assert r["ye_tianshi"]["meets_multi_visit_quota"] is False

    r2 = quota.audit(cases, min_total=5, min_multi_visit=0)
    assert r2["ye_tianshi"]["meets_total_quota"] is True
    assert r2["ye_tianshi"]["meets_multi_visit_quota"] is True


def test_audit_defaults_match_documented_sources_thresholds():
    """两个默认门槛写在 data/SOURCES.md 第 7 节第 1 条，这里钉住数值本身，
    防止以后有人顺手改了却忘记同步文档（或者反过来）。"""
    assert quota.MIN_TOTAL_CASES == 60
    assert quota.MIN_MULTI_VISIT_CASES == 50


def test_audit_partitions_by_physician_independently():
    cases = [
        _case("a0", "g1", "ye_tianshi"),
        _case("b0", "g1", "wu_jutong"),  # 撞了 case_group_id 但医家不同，不该混
    ]
    r = quota.audit(cases, min_total=10, min_multi_visit=1)
    assert r["ye_tianshi"]["n_patients"] == 1
    assert r["wu_jutong"]["n_patients"] == 1


def test_audit_empty_input():
    assert quota.audit([]) == {}


def test_format_report_shows_pass_and_fail():
    cases = [_case(f"a{i}", f"g{i}", "ye_tianshi") for i in range(3)]
    r = quota.audit(cases, min_total=10, min_multi_visit=1)
    report = quota.format_report(r)
    assert "未达标" in report
    assert "ye_tianshi" in report


def test_format_report_empty():
    assert "没有任何医案" in quota.format_report({})


# ---------- CLI ----------


def test_main_raises_clear_error_when_cases_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        quota.main(["--cases-path", str(tmp_path / "nope.json")])


def test_main_writes_out_json_when_requested(tmp_path, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        _case("a0", "g1", "ye_tianshi").model_dump(),
    ]), encoding="utf-8")
    out_path = tmp_path / "quota_report.json"

    quota.main(["--cases-path", str(cases_path), "--out", str(out_path), "--min-total", "1", "--min-multi-visit", "0"])
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["ye_tianshi"]["total_cases"] == 1
    assert "ye_tianshi" in capsys.readouterr().out
