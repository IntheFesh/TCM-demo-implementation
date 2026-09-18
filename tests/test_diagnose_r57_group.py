"""R57 C 组不达标诊断（`scripts/diagnose_r57_group.py`）。`format_report()`
用合成数据测（零 LLM）；`diagnose()` 本身跑一遍 `--backend fake`（零网络，
跟 `tests/test_ablation_r57.py` 的假后端冒烟测试同一类——只验证管道通不通、
字段形状对不对，不验证假数据的内容）。
"""
from __future__ import annotations

from scripts.diagnose_r57_group import diagnose, format_report

KIND_LABELS = {
    "organ_relation": "藏象关系", "pathomechanism": "病机传变",
    "treatment_principle": "治则推导", "compatibility": "配伍理论（君臣佐使）",
}


def _fixture(**overrides) -> dict:
    base = {
        "group": "C", "n_queries": 20, "n_ok": 19, "n_with_output": 19,
        "insufficient_by_kind": {}, "insufficient_examples": {k: [] for k in KIND_LABELS},
        "cited_by_kind": {}, "unresolvable_rule_ids": [], "kind_labels": KIND_LABELS,
    }
    base.update(overrides)
    return base


def test_zero_insufficient_points_at_the_verifier_not_the_rule_library():
    out = format_report(_fixture())
    assert "问题大概率不在规则库覆盖面" in out
    assert "core/formula_verifier.py" in out


def test_the_thinnest_kind_is_named_explicitly():
    out = format_report(_fixture(
        insufficient_by_kind={"pathomechanism": 7, "compatibility": 1},
        insufficient_examples={**{k: [] for k in KIND_LABELS},
                               "pathomechanism": ["脾虚夹湿证的传变链条查不到"]}))
    assert "最薄的一类：病机传变" in out
    assert "脾虚夹湿证的传变链条查不到" in out
    # 零引用的类别不该出现在「1. 依据不足」表里，只在下面的对照表里
    assert "藏象关系（`organ_relation`） | 0 |" not in out.split("## 2.")[0]


def test_cited_by_kind_table_lists_all_four_kinds_even_at_zero():
    out = format_report(_fixture(cited_by_kind={"organ_relation": 12}))
    table = out.split("## 2.")[1]
    for label in KIND_LABELS.values():
        assert label in table


def test_unresolvable_rule_ids_are_flagged_as_more_severe_than_thin_coverage():
    out = format_report(_fixture(unresolvable_rule_ids=["ZX-999", "BJ-888"]))
    assert "ZX-999" in out
    assert "比规则库覆盖面不够更严重" in out


def test_diagnose_runs_end_to_end_against_the_fake_backend():
    """零网络。只验证管道通不通、返回的字典形状对不对——假后端产出的是
    schema-valid 的固定假数据，insufficient_notes/rule_refs 的具体内容
    没有信息量，跟 test_ablation_r57.py 里其余假后端测试同一条边界。"""
    from scripts.bench_consult import build_fake_backend

    backend = build_fake_backend(0.0)
    complaints = [{"record_id": "q1", "syndrome": None, "complaint": "纳差乏力"}]
    result = diagnose("C", complaints, backend)
    assert result["group"] == "C"
    assert result["n_queries"] == 1
    assert set(result["kind_labels"]) == set(KIND_LABELS)
    report = format_report(result)
    assert "R57 C 组诊断" in report
