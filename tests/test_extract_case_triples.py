"""offline/extract_case_triples.py 的离线测试：确定性转换，不需要 LLM/网络。"""
import json

import pytest

from core.schemas import CaseRecord, CaseTriple
from offline import extract_case_triples as ect


def _case(**overrides):
    base = dict(
        case_id="ye_tianshi-1", case_group_id="ye_tianshi-1",
        physician="ye_tianshi", raw="原文全文", raw_excerpt="原文片段",
        symptoms=["纳差"],
    )
    base.update(overrides)
    return CaseRecord.model_validate(base)


def test_full_chain_produces_all_five_predicates():
    case = _case(
        syndrome="肝胃不和", treatment_principle="疏肝和胃",
        formula="柴胡疏肝散", herbs=["柴胡", "白芍", "炒枳壳"],
    )
    triples = ect.case_to_triples(case)
    predicates = [t.predicate for t in triples]
    assert predicates == [
        "practiced_by", "evidences", "treated_by", "realized_by",
        "contains", "contains", "contains",
    ]


def test_chain_breaks_at_missing_syndrome():
    case = _case(treatment_principle="疏肝和胃", formula="柴胡疏肝散", herbs=["柴胡"])
    triples = ect.case_to_triples(case)
    assert [t.predicate for t in triples] == ["practiced_by"]


def test_chain_breaks_at_missing_treatment_principle():
    case = _case(syndrome="肝胃不和", formula="柴胡疏肝散", herbs=["柴胡"])
    triples = ect.case_to_triples(case)
    assert [t.predicate for t in triples] == ["practiced_by", "evidences"]


def test_chain_breaks_at_missing_formula():
    case = _case(syndrome="肝胃不和", treatment_principle="疏肝和胃")
    triples = ect.case_to_triples(case)
    assert [t.predicate for t in triples] == ["practiced_by", "evidences", "treated_by"]


def test_chain_with_formula_but_no_herbs_stops_before_contains():
    case = _case(syndrome="肝胃不和", treatment_principle="疏肝和胃", formula="柴胡疏肝散", herbs=[])
    triples = ect.case_to_triples(case)
    assert [t.predicate for t in triples] == ["practiced_by", "evidences", "treated_by", "realized_by"]


def test_herbs_are_normalized_via_core_herbs_not_a_new_matcher():
    """CLAUDE.md：同一概念的匹配逻辑只能有一处实现——药名归一必须复用
    core.herbs.normalize_herb，不能在这里另写一套。"""
    case = _case(
        syndrome="肝胃不和", treatment_principle="疏肝和胃",
        formula="柴胡疏肝散", herbs=["炒枳壳"],
    )
    triples = ect.case_to_triples(case)
    contains = [t for t in triples if t.predicate == "contains"]
    assert contains[0].object == "herb::枳壳"  # 炒 前缀被剥掉，跟 core.herbs 的行为一致


def test_case_local_syndrome_node_namespaced_away_from_gb_standard():
    """证候节点用 syndrome::case:: 前缀，不能跟 offline/build_graph.py 里
    国标证候的 syndrome::{code} 撞命名空间——两套术语体系本来就没对齐，
    节点 id 也不该看起来像对齐了。"""
    case = _case(syndrome="胃阳虚")
    triples = ect.case_to_triples(case)
    evidences = [t for t in triples if t.predicate == "evidences"][0]
    assert evidences.object == "syndrome::case::胃阳虚"


def test_source_span_prefers_raw_excerpt_over_raw():
    case = _case(raw="整段原文", raw_excerpt="这一诊的片段", syndrome="x")
    triples = ect.case_to_triples(case)
    assert all(t.source_span == "这一诊的片段" for t in triples)


def test_source_span_falls_back_to_raw_when_no_excerpt():
    case = _case(raw="整段原文", raw_excerpt=None, syndrome="x")
    triples = ect.case_to_triples(case)
    assert all(t.source_span == "整段原文" for t in triples)


def test_no_source_span_available_produces_no_triples():
    """raw 是必填字段，正常不会为空；但防御一下——没有可核验出处就不产出，
    不能编一个 source_span 出来。"""
    case = _case(raw="", raw_excerpt=None, syndrome="x")
    triples = ect.case_to_triples(case)
    assert triples == []


def test_multiple_cases_sharing_therapy_produce_shared_node_id():
    """治法/方剂/药物节点故意设计成跨医案共享——同一句治法原文不同医案各说
    一次，应该指向同一个节点，不是各自建一个。"""
    c1 = _case(case_id="a", case_group_id="a", syndrome="肝胃不和",
               treatment_principle="疏肝和胃", formula="柴胡疏肝散", herbs=["柴胡"])
    c2 = _case(case_id="b", case_group_id="b", syndrome="肝胃不和",
               treatment_principle="疏肝和胃", formula="柴胡疏肝散", herbs=["白芍"])
    triples = ect.extract_all([c1, c2])
    therapy_objects = {t.object for t in triples if t.predicate == "treated_by"}
    formula_objects = {t.object for t in triples if t.predicate == "realized_by"}
    assert therapy_objects == {"therapy::疏肝和胃"}
    assert formula_objects == {"formula::柴胡疏肝散"}


def test_case_triple_rejects_empty_source_span():
    with pytest.raises(Exception):
        CaseTriple(
            case_id="a", physician="ye_tianshi", subject="case::a", subject_type="case",
            predicate="practiced_by", object="physician::ye_tianshi", object_type="physician",
            source_span="",
        )


# ---------- CLI ----------


def test_main_dry_run_does_not_write_file(tmp_path, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi", "raw": "x",
         "raw_excerpt": "x", "syndrome": "肝胃不和"},
    ]), encoding="utf-8")
    out_path = tmp_path / "case_triples.jsonl"

    ect.main(["--cases-path", str(cases_path), "--out", str(out_path), "--dry-run"])
    assert not out_path.exists()


def test_main_writes_jsonl(tmp_path):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi", "raw": "x",
         "raw_excerpt": "x", "syndrome": "肝胃不和"},
        {"case_id": "b", "case_group_id": "b", "physician": "wu_jutong", "raw": "y",
         "raw_excerpt": "y"},
    ]), encoding="utf-8")
    out_path = tmp_path / "case_triples.jsonl"

    ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])

    lines = out_path.read_text(encoding="utf-8").splitlines()
    triples = [CaseTriple.model_validate_json(l) for l in lines]
    assert len(triples) == 3  # a: practiced_by+evidences，b: 只有 practiced_by


def test_main_limit_only_processes_first_n(tmp_path):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": f"c{i}", "case_group_id": f"c{i}", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "x"}
        for i in range(5)
    ]), encoding="utf-8")
    out_path = tmp_path / "case_triples.jsonl"

    ect.main(["--cases-path", str(cases_path), "--out", str(out_path), "--limit", "2"])

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2  # 每条只有 practiced_by，5 条限到 2 条就是 2 行


def test_main_raises_clear_error_when_cases_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        ect.main(["--cases-path", str(tmp_path / "nope.json")])
