"""R62 §12 第 1 项：`explanations` 块——本次结果里全部可点术语的释义。

这份测的是"**哪些术语要释义**"这一件事（`core/explanations.py` 的全部职责）。
释义本身长什么样由 `tests/test_node_explain*.py` 测——两个问题分开，因为它们
变化的原因不同：这里变是因为界面上多了一种可点的东西，那里变是因为释义里
多了一节。
"""
from __future__ import annotations

import pytest

from core import theory
from core.explanations import MAX_TERMS, build_explanations, collect_terms
from core.ontology import Ontology


@pytest.fixture(scope="module")
def rule_id() -> str:
    rules = theory.load_theory()
    assert rules, "医理规则层没加载出来，这份测试的前提不成立"
    return rules[0].id


@pytest.fixture
def s3s(rule_id) -> dict:
    return {
        "organs": [
            {"organ": "肝", "supporting_symptoms": ["胃脘胀痛", "脉弦"],
             "pathogenesis": "肝失疏泄，气机郁滞",
             "rule_refs": [{"rule_id": rule_id, "note": "凭这条定位"}]},
            {"organ": "胃", "supporting_symptoms": ["嗳气泛酸"],
             "pathogenesis": "胃失和降", "rule_refs": []},
        ],
        "syndrome": {"name": "肝胃不和证", "disease": "胃痛", "rule_refs": []},
        "method": {"principle": "疏肝理气，和胃止痛",
                   "targets": ["肝气郁结", "胃失和降"], "rule_refs": []},
        "formula": {"candidate": {
            "name": "柴胡疏肝散加减",
            "herb_items": [{"name": n} for n in ("柴胡", "白芍", "枳壳")]}, "rule_refs": []},
        "herb_choices": [{"rule_refs": []}],
        "key_points": [{"point": "胃脘胀痛，食后加重", "maps_to": "气滞在胃"}],
        "differential": [
            {"syndrome": "肝胃郁热证", "excluded_because": "本例无口苦", "rule_refs": []},
            {"syndrome": "脾胃虚寒证", "excluded_because": "痛势为胀非隐痛", "rule_refs": []},
        ],
        "modifications": [{"if_symptom": "泛酸明显", "action": "加",
                           "item": {"name": "煅瓦楞子"}, "why": "制酸止痛",
                           "rule_refs": []}],
    }


@pytest.fixture
def s3() -> dict:
    return {"syndrome": "肝胃不和证", "disease": "胃痛",
            "treatment_principle": "疏肝理气，和胃止痛"}


def _no_ontology() -> Ontology:
    """本体文件不在的那台机器。

    **用真的 `Ontology` 喂空行，不是手写一个桩类**：桩类要照着
    `core/node_explain.py` 今天调了哪几个方法去补，那边多调一个方法这里就
    `AttributeError`——而那是"桩跟不上"，不是被测代码有问题。空行构造出来的
    `Ontology.available` 就是 `False`，跟 `get_ontology()` 在缺文件的机器上
    返回的东西**逐字段相同**。
    """
    return Ontology(materia_rows=[], formulary_rows=[])


def _ids(terms) -> list[str]:
    return [t["id"] for t in terms]


def test_every_clickable_thing_in_the_spec_shows_up(s3s, s3):
    """§7.1 原话：中栏所有专业术语都可点——证型、病名、辨证要点、鉴别证型、
    病机、治则、治法、方名、每一味药名、每一条依据。漏一种就有一块区域
    点了没反应，而那在界面上跟"这里不可点"长得一模一样。"""
    wheres = {t["where"] for t in collect_terms(s3s, s3)}
    assert wheres >= {"syndrome", "disease", "organ", "pathogenesis", "method",
                      "principle", "formula", "herb", "key_point", "differential",
                      "modification", "symptom", "rule"}


def test_a_herb_only_in_the_modification_list_is_still_clickable(s3s, s3):
    """加减建议里的药不在处方表里，但它照样显示在界面上、照样该能点开看
    性味归经——医师正是要凭这个判断采不采纳。"""
    assert "herb::煅瓦楞子" in _ids(collect_terms(s3s, s3))


def test_the_excluded_syndromes_are_clickable_too(s3s, s3):
    """§8 那张表把「鉴别」列成学生模式的教学核心。一条"排除了肝胃郁热证"
    而点不开"肝胃郁热证是什么"的鉴别，教不了任何东西。"""
    ids = _ids(collect_terms(s3s, s3))
    assert "syn::肝胃郁热证" in ids and "syn::脾胃虚寒证" in ids


def test_terms_are_deduped_and_keep_reading_order(s3s, s3):
    """同一个词在多处出现只留第一处——释义是同一份。顺序是界面上从上到下
    的阅读顺序，截断时留下的才是先读到的那些。"""
    s3s["modifications"].append({"if_symptom": "同一味药再出现一次", "action": "加",
                                 "item": {"name": "柴胡"}, "why": "x", "rule_refs": []})
    ids = _ids(collect_terms(s3s, s3))
    assert len(ids) == len(set(ids))
    assert ids.index("syn::肝胃不和证") < ids.index("formula::柴胡疏肝散加减")
    assert ids.index("formula::柴胡疏肝散加减") < ids.index("herb::柴胡")


def test_the_rule_ids_from_every_step_are_collected(s3s, s3, rule_id):
    """⑨「推导依据」那一栏每一条都可点。漏一条就有一行点了没反应。"""
    assert f"rule::{rule_id}" in _ids(collect_terms(s3s, s3))


def test_an_empty_result_yields_no_terms_and_does_not_crash():
    assert collect_terms(None, None) == []
    out = build_explanations(None, None)
    assert out["terms"] == [] and out["by_id"] == {} and out["truncated"] is False


def test_entries_without_an_explanation_are_still_delivered(s3s, s3):
    """前端点了要能显示"这一条查不到释义"，不是点了没反应——后者跟
    "这个词不可点"在界面上长得一模一样。"""
    out = build_explanations(s3s, s3, ontology=_no_ontology())
    for t in out["terms"]:
        assert t["id"] in out["by_id"], f"{t['id']} 在术语表里却没有对应条目"
    assert out["n"] == len(out["terms"])
    assert out["n_available"] <= out["n"]


def test_truncation_is_reported_with_real_numbers(s3s, s3):
    """静默截断会让某个词点了没反应，而那看起来像 bug 不像"太多了"。"""
    out = build_explanations(s3s, s3, limit=3)
    assert out["truncated"] is True and out["n"] == 3
    assert str(out["n_total"]) in out["note"] and "3" in out["note"]


def test_the_default_cap_leaves_room_for_the_worst_realistic_case(s3s, s3):
    """上限是按最坏情况估的：16 味药 + 加减 + 九层链 + 要点 + 鉴别 + 规则。
    一个典型结果远在上限之下，说明这个数不是随手填的。"""
    assert build_explanations(s3s, s3)["truncated"] is False
    assert MAX_TERMS >= 100


def test_building_explanations_never_calls_the_model(s3s, s3, monkeypatch):
    """**零 LLM** 是这一层的硬约束：释义随结果一次性下发，而问诊已经跑完了，
    这里再调一次模型等于在"结果已经出来了"之后又加一段等待。"""
    import core.llm as llm

    def _boom(*a, **k):
        raise AssertionError("这一层不许调模型")

    monkeypatch.setattr(llm, "get_llm", _boom)
    out = build_explanations(s3s, s3)
    assert out["n"] > 0
