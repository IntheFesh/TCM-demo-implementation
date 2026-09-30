"""core/transition.py 的离线测试：不需要真实 cases.json/element_index.json。"""
import json

import pytest

from core.schemas import CaseRecord
from core.transition import build_trajectories, load_trajectories


def _case(case_id, group_id, physician, visit_index, symptoms=None, syndrome=None):
    return CaseRecord(
        case_id=case_id, case_group_id=group_id, physician=physician,
        raw="x", visit_index=visit_index, symptoms=symptoms or [], syndrome=syndrome,
    )


def test_single_visit_group_is_excluded():
    """单诊不是"轨迹"，是一个点——过滤掉，不是漏做。"""
    cases = [_case("a", "g1", "ye_tianshi", 0)]
    trajectories = build_trajectories(cases, {})
    assert trajectories == {}


def test_multi_visit_group_is_included_and_sorted_by_visit_index():
    cases = [
        _case("a2", "g1", "ye_tianshi", 2),
        _case("a0", "g1", "ye_tianshi", 0),
        _case("a1", "g1", "ye_tianshi", 1),
    ]
    trajectories = build_trajectories(cases, {})
    assert list(trajectories.keys()) == ["ye_tianshi"]
    traj = trajectories["ye_tianshi"][0]
    assert traj["case_group_id"] == "g1"
    assert traj["n_visits"] == 3
    assert [v["case_id"] for v in traj["visits"]] == ["a0", "a1", "a2"]


def test_elements_come_from_element_index_not_reinferred():
    cases = [
        _case("a0", "g1", "ye_tianshi", 0),
        _case("a1", "g1", "ye_tianshi", 1),
    ]
    element_index = {
        "a0": {"physician": "ye_tianshi", "elements": ["脾", "气虚"]},
        "a1": {"physician": "ye_tianshi", "elements": ["脾", "湿"]},
    }
    trajectories = build_trajectories(cases, element_index)
    visits = trajectories["ye_tianshi"][0]["visits"]
    assert visits[0]["elements"] == ["脾", "气虚"]
    assert visits[1]["elements"] == ["脾", "湿"]


def test_case_missing_from_element_index_gets_empty_elements_not_crash():
    cases = [
        _case("a0", "g1", "ye_tianshi", 0),
        _case("a1", "g1", "ye_tianshi", 1),
    ]
    trajectories = build_trajectories(cases, {})  # 空索引
    visits = trajectories["ye_tianshi"][0]["visits"]
    assert visits[0]["elements"] == []


def test_groups_partitioned_by_physician_independently():
    """不同医家的 case_group_id 恰好撞名（不太可能但不该互相污染）。"""
    cases = [
        _case("a0", "g1", "ye_tianshi", 0),
        _case("a1", "g1", "ye_tianshi", 1),
        _case("b0", "g1", "wu_jutong", 0),
    ]
    trajectories = build_trajectories(cases, {})
    assert "ye_tianshi" in trajectories
    assert "wu_jutong" not in trajectories  # wu_jutong 那组只有 1 诊，被过滤


def test_multiple_patients_sorted_by_group_id():
    cases = [
        _case("z0", "z", "ye_tianshi", 0), _case("z1", "z", "ye_tianshi", 1),
        _case("a0", "a", "ye_tianshi", 0), _case("a1", "a", "ye_tianshi", 1),
    ]
    trajectories = build_trajectories(cases, {})
    assert [t["case_group_id"] for t in trajectories["ye_tianshi"]] == ["a", "z"]


# ---------- load_trajectories ----------


def test_load_trajectories_raises_when_cases_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        load_trajectories(
            cases_path=tmp_path / "nope.json",
            element_index_path=tmp_path / "nope2.json",
        )


def test_load_trajectories_raises_when_element_index_missing(tmp_path):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text("[]", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="build_element_index"):
        load_trajectories(cases_path=cases_path, element_index_path=tmp_path / "nope.json")


def test_load_trajectories_end_to_end(tmp_path):
    cases_path = tmp_path / "cases.json"
    cases = [
        _case("a0", "g1", "ye_tianshi", 0).model_dump(),
        _case("a1", "g1", "ye_tianshi", 1).model_dump(),
    ]
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    index_path = tmp_path / "element_index.json"
    index_path.write_text(json.dumps({
        "a0": {"physician": "ye_tianshi", "elements": ["脾"]},
    }), encoding="utf-8")

    trajectories = load_trajectories(cases_path=cases_path, element_index_path=index_path)
    assert trajectories["ye_tianshi"][0]["visits"][0]["elements"] == ["脾"]
