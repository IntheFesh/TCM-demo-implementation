"""offline/extract_case_triples.py 的离线测试：假 LLM 后端，不需要网络。

跟 tests/test_extract_cases.py 一样的假 LLM 模式（monkeypatch get_llm）。
这里额外要覆盖的核心逻辑是 source_span 的逐字核验——这一步 pydantic schema
校验不了，是 extract_case_triples.py 自己做的，也是这个模块存在的意义。
"""
import json

import pytest

from core.schemas import CaseRecord, CaseTripleExtraction, CaseTripleItem
from offline import extract_case_triples as ect


def _case(**overrides):
    base = dict(
        case_id="ye_tianshi-1", case_group_id="ye_tianshi-1",
        physician="ye_tianshi", raw="整段原文（不该被用到）",
        raw_excerpt="脘痛不食，脉弦。此肝木犯胃，宜苦辛通降。",
        symptoms=["脘痛"],
    )
    base.update(overrides)
    return CaseRecord.model_validate(base)


class FakeLLM:
    def __init__(self, result: CaseTripleExtraction):
        self.result = result
        self.calls = 0

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        assert schema is CaseTripleExtraction
        return self.result


def _item(s="患者", p="表现为", o="脘痛", source_span="脘痛不食，脉弦。"):
    return CaseTripleItem(s=s, p=p, o=o, source_span=source_span)


# ---------- extract_case：单条医案 ----------


def test_valid_source_span_survives(monkeypatch):
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, n_rejected = ect.extract_case(case)
    assert n_rejected == 0
    assert len(records) == 1
    assert records[0].s == "患者" and records[0].o == "脘痛"
    assert records[0].case_id == "ye_tianshi-1"
    assert records[0].physician == "ye_tianshi"


def test_source_span_not_in_text_is_rejected(monkeypatch):
    """这是这个模块存在的核心理由：模型编了一句原文里没有的话，必须被丢弃，
    不能写进 data/case_triples.jsonl——写进去就等于给 query_case_graph 的
    调用方一个假的"出处凭据"。"""
    case = _case()
    bad = _item(source_span="这句话原文里根本没有")
    good = _item(source_span="脘痛不食，脉弦。")
    fake = FakeLLM(CaseTripleExtraction(triples=[bad, good]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, n_rejected = ect.extract_case(case)
    assert n_rejected == 1
    assert len(records) == 1
    assert records[0].source_span == "脘痛不食，脉弦。"


def test_empty_extraction_is_not_an_error(monkeypatch):
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, n_rejected = ect.extract_case(case)
    assert records == []
    assert n_rejected == 0


def test_source_span_prefers_raw_excerpt_over_raw(monkeypatch):
    case = _case(raw="整段原文", raw_excerpt="这一诊的片段")
    fake = FakeLLM(CaseTripleExtraction(triples=[_item(source_span="这一诊的片段")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, n_rejected = ect.extract_case(case)
    assert n_rejected == 0
    # 也验证喂给模型的确实是 raw_excerpt，不是 raw：用一个只在 raw 里出现的
    # 片段当 source_span，应该被拒绝，因为核验基准是 raw_excerpt。
    fake2 = FakeLLM(CaseTripleExtraction(triples=[_item(source_span="整段原文")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake2)
    records2, n_rejected2 = ect.extract_case(case)
    assert n_rejected2 == 1
    assert records2 == []


def test_no_source_text_returns_empty_without_calling_llm(monkeypatch):
    case = _case(raw="", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, n_rejected = ect.extract_case(case)
    assert records == []
    assert n_rejected == 0
    assert fake.calls == 0  # 没有原文可核验，不该白烧一次调用


# ---------- extract_all：统计聚合 ----------


def test_extract_all_aggregates_stats_and_skips_cases_without_text(monkeypatch):
    with_text = _case(case_id="a", case_group_id="a")
    no_text = _case(case_id="b", case_group_id="b", raw="", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item(), _item(o="纳差", source_span="脉弦。")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, stats = ect.extract_all([with_text, no_text])
    assert stats["cases"] == 2
    assert stats["cases_no_text"] == 1
    assert stats["llm_calls"] == 1  # 只对有原文的那条医案调用
    assert len(records) == 2


# ---------- CLI ----------


def test_main_dry_run_does_not_call_llm(tmp_path, monkeypatch, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "x"},
    ]), encoding="utf-8")

    def boom():
        raise AssertionError("--dry-run 不该真的调用 LLM")

    monkeypatch.setattr(ect, "get_llm", boom)
    ect.main(["--cases-path", str(cases_path), "--dry-run"])
    out = capsys.readouterr().out
    assert "预估调用数" in out
    assert "--dry-run" in out


def test_main_writes_jsonl_in_established_format(tmp_path, monkeypatch):
    """字段名必须是 s/p/o，不是 subject/predicate/object——这是
    core/tools.py.query_case_graph() 已经在读的格式，不能各写各的。"""
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦。"},
    ]), encoding="utf-8")
    out_path = tmp_path / "case_triples.jsonl"

    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])

    line = out_path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert set(row) == {"case_id", "physician", "s", "p", "o", "source_span"}
    assert row["s"] == "患者" and row["p"] == "表现为" and row["o"] == "脘痛"


def test_main_raises_clear_error_when_cases_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        ect.main(["--cases-path", str(tmp_path / "nope.json")])


def test_case_triple_item_rejects_empty_source_span():
    with pytest.raises(Exception):
        CaseTripleItem(s="a", p="b", o="c", source_span="")
