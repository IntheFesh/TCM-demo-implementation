"""offline/extract_case_triples.py 的离线测试：假 LLM 后端，不需要网络。

跟 tests/test_extract_cases.py 一样的假 LLM 模式（monkeypatch get_llm）。
这里要覆盖的核心逻辑有三层防幻觉核验，pydantic schema 都管不了，是
extract_case_triples.py 自己做的：
  1. source_span 的逐字核验（这个模块最初存在的理由）
  2. s/o 不能是"此症""患者"这类指代词占位符（R2 第一轮 --limit 5 试水暴露）
  3. 只用 raw_excerpt 核验/喂给模型，raw_excerpt 缺失就跳过、不退回整段 raw
     （同一轮试水暴露：退回 raw 会让同一病人的多次诊次读到同一段文本、抽出
     不一致甚至跨诊次的内容）
以及第四层——不是"内容有问题"，是"根本没读完"：
  4. 输出被截断（LLMTruncatedError）时跳过这条医案，不重试、不崩批
     （R2 全量跑第一条医案就崩暴露：一张九味药的方子，九条「含」关系的
     source_span 全部重复抄整张方子，约 1800 字纯重复撞上 max_tokens）
谓词的六选一（core.schemas.CaseTriplePredicate）是 schema 层拦的，pydantic
自己会报错，不需要这里再测——CaseTripleItem 的构造失败已经是 pydantic 的
标准行为，这里只补一条确认 Literal 真的生效了。
"""
import json

import pytest
from pydantic import ValidationError

from core.llm import LLMTruncatedError
from core.schemas import CaseRecord, CaseTripleExtraction, CaseTripleItem
from offline import extract_case_triples as ect


def _case(**overrides):
    base = dict(
        case_id="ye_tianshi-1", case_group_id="ye_tianshi-1",
        physician="ye_tianshi", raw="整段原文（不该被用到）",
        raw_excerpt="脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。",
        symptoms=["脘痛"],
    )
    base.update(overrides)
    return CaseRecord.model_validate(base)


class FakeLLM:
    def __init__(self, result: CaseTripleExtraction):
        self.result = result
        self.calls = 0
        self.kwargs_seen: list[dict] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        self.kwargs_seen.append(kwargs)
        assert schema is CaseTripleExtraction
        return self.result


class TruncatingFakeLLM:
    """模拟输出被截断：generate() 直接抛 LLMTruncatedError，就像真实后端
    在 JSON 于 max_tokens 处被砍断时会做的那样。"""

    def __init__(self):
        self.calls = 0

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        raise LLMTruncatedError("疑似输出在 max_tokens 上限处被截断（测试用假错误）")


# 默认值改用受控词表里的"提示"（症状→病机），s/o 都是具体实体，不再用
# "患者"（R2 之后是被禁止的指代词）当主语——旧版 _item() 默认值本身就是
# 这次要修的两个问题（表现为不在六选一里、患者是指代词）的活例子。
def _item(s="脘痛", p="提示", o="肝木犯胃", source_span="脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"):
    return CaseTripleItem(s=s, p=p, o=o, source_span=source_span)


# ---------- extract_case：单条医案 ----------


def test_valid_source_span_survives(monkeypatch):
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is False
    assert len(records) == 1
    assert records[0].s == "脘痛" and records[0].o == "肝木犯胃"
    assert records[0].case_id == "ye_tianshi-1"
    assert records[0].physician == "ye_tianshi"


def test_source_span_not_in_text_is_rejected(monkeypatch):
    """这是这个模块最初存在的核心理由：模型编了一句原文里没有的话，必须被
    丢弃，不能写进 data/case_triples.jsonl——写进去就等于给 query_case_graph
    的调用方一个假的"出处凭据"。"""
    case = _case()
    bad = _item(source_span="这句话原文里根本没有")
    good = _item()
    fake = FakeLLM(CaseTripleExtraction(triples=[bad, good]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert rejected == {"span_not_found": 1, "referential": 0}
    assert truncated is False
    assert len(records) == 1
    assert records[0].s == "脘痛"


def test_referential_subject_is_rejected(monkeypatch):
    """R2 --limit 5 试水实测过的问题：主语是"此症"这类指代词。941 条医案
    如果都抽出"此症"，图谱里会变成共用一个节点，所有医案都连到它上面。
    这条 schema 拦不住（"此症"是合法的非空字符串），核验在这里做。"""
    case = _case()
    bad = _item(s="此症")
    good = _item()
    fake = FakeLLM(CaseTripleExtraction(triples=[bad, good]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 1}
    assert truncated is False
    assert len(records) == 1
    assert records[0].s == "脘痛"


def test_referential_object_is_rejected(monkeypatch):
    """跟上一条对称：指代词出现在宾语位置同样要丢——"用药 用药 患者" 这种
    宾语是"患者"的三元组一样是脏数据。"""
    case = _case()
    bad = _item(o="患者")
    fake = FakeLLM(CaseTripleExtraction(triples=[bad]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 1}
    assert truncated is False
    assert records == []


def test_referential_check_is_exact_match_not_substring(monkeypatch):
    """黑名单是整串相等，不是子串——"患者" 整个词才拦，含"患者"两个字的合法
    医学表述（比如作为更长短语的一部分）不该被误伤。这里用"病家"之外裹了
    别的字的字符串验证不会被拦。"""
    case = _case()
    ok = _item(s="患者自述纳差")  # 不是黑名单里的任何一个整串，应该放行
    fake = FakeLLM(CaseTripleExtraction(triples=[ok]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is False
    assert len(records) == 1


def test_empty_extraction_is_not_an_error(monkeypatch):
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is False


def test_source_span_checked_against_excerpt_raw_never_consulted(monkeypatch):
    """raw_excerpt 存在时，raw 完全不参与——不止是"优先用 excerpt"，是 raw
    里的内容压根不算数。用一个只在 raw 里出现、excerpt 里没有的片段当
    source_span，必须被拒绝，即使 raw 和 excerpt 都不为空。"""
    case = _case(raw="整段原文，另有别的诊次内容", raw_excerpt="这一诊的片段")
    fake = FakeLLM(CaseTripleExtraction(triples=[_item(source_span="这一诊的片段")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is False
    assert len(records) == 1

    fake2 = FakeLLM(CaseTripleExtraction(triples=[_item(source_span="整段原文，另有别的诊次内容")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake2)
    records2, rejected2, truncated2 = ect.extract_case(case)
    assert rejected2 == {"span_not_found": 1, "referential": 0}
    assert truncated2 is False
    assert records2 == []


def test_no_source_text_returns_empty_without_calling_llm(monkeypatch):
    """raw_excerpt 和 raw 都没有——不该发生（raw 是必填字段），但防御性地
    也不该编。"""
    case = _case(raw="", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is False
    assert fake.calls == 0  # 没有原文可核验，不该白烧一次调用


def test_missing_raw_excerpt_is_skipped_even_though_raw_has_real_content(monkeypatch):
    """R2 --limit 5 试水实测出的真实 bug：raw_excerpt 缺失但 raw 有内容时，
    旧版会退回整段 raw 喂模型——wu_jutong-0000 的三诊共享同一段 raw，三次都
    读到整段文本，抽出三套不一致的三元组，其中一次还混进了后面诊次才出现
    的方剂。现在的正确行为是跳过这条医案，不产出任何三元组、不调用 LLM，
    不是退化成用 raw。这条测试是这次修复里最容易被漏掉的一个：如果只测
    "raw_excerpt 和 raw 都空"（上一条测试），测不出"raw_excerpt 空但 raw
    有内容"这条真正触发过 bug 的组合。"""
    case = _case(raw="整段原文，包含这一病人全部诊次的内容", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is False
    assert fake.calls == 0  # 没有 raw_excerpt，不该拿 raw 顶上去调用


def test_extract_case_passes_higher_max_tokens_than_the_backend_default(monkeypatch):
    """R2 全量跑第一条医案就崩：一张九味药的方子，九条「含」关系的
    source_span 各自重抄一遍整张方子，约 1800 字纯重复撞上默认的 8192。
    S5 这一处调用要显式传更大的 max_tokens，不是让它落到后端默认值。"""
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    ect.extract_case(case)
    assert fake.kwargs_seen[0]["max_tokens"] == ect.S5_MAX_TOKENS
    assert ect.S5_MAX_TOKENS > 8192  # 明确大于后端默认值，不是凑巧等于


def test_truncated_output_is_skipped_not_retried_or_raised(monkeypatch):
    """R2 全量跑第一条医案就崩的直接根因：LLMTruncatedError 一路往上抛，
    整批全量跑崩掉。现在这里要捕获它，返回空结果 + truncated=True，让
    调用方（extract_all）跳过这条医案继续跑下一条，而不是让一条医案
    的输出格式问题拖垮整批。"""
    case = _case()
    fake = TruncatingFakeLLM()
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, truncated = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert truncated is True
    assert fake.calls == 1  # 截断不重试——core.llm 的 generate() 已经不重试了，
    # 这里额外确认 extract_case 自己也没有再包一层重试


# ---------- extract_all：统计聚合 ----------


def test_extract_all_aggregates_stats_and_skips_cases_without_excerpt(monkeypatch):
    with_excerpt = _case(case_id="a", case_group_id="a")
    no_excerpt = _case(
        case_id="b", case_group_id="b",
        raw="整段原文，有内容但没有 excerpt", raw_excerpt=None,
    )
    fake = FakeLLM(CaseTripleExtraction(triples=[
        _item(), _item(o="纳差", source_span="脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"),
    ]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, stats = ect.extract_all([with_excerpt, no_excerpt])
    assert stats["cases"] == 2
    assert stats["cases_no_text"] == 1  # no_excerpt 那条：raw 有内容也不算数
    assert stats["llm_calls"] == 1  # 只对有 raw_excerpt 的那条医案调用
    assert len(records) == 2


def test_extract_all_reports_referential_rejections_separately(monkeypatch):
    """跟 span_not_found 分开计数：两类丢弃原因指向不同的 prompt 问题，
    合并成一个数会看不出"改了指代词那条规则有没有生效"。"""
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[
        _item(s="此症"), _item(source_span="这句话原文里根本没有"), _item(),
    ]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, stats = ect.extract_all([case])
    assert stats["triples_rejected_referential"] == 1
    assert stats["triples_rejected_span_not_found"] == 1
    assert stats["triples_extracted"] == 1
    assert len(records) == 1


def test_extract_all_skips_truncated_case_and_keeps_running(monkeypatch):
    """这是这次修复最核心的行为：一条医案输出被截断，不该让整批 extract_all
    崩掉——统计里要能看出"跳过了几条"，其余医案照常处理。用两条医案模拟：
    第一条截断，第二条正常，确认第二条真的被处理了（不是提前 return）。"""
    truncated_case = _case(case_id="a", case_group_id="a")
    ok_case = _case(case_id="b", case_group_id="b")

    class MixedFakeLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LLMTruncatedError("第一条截断（测试用假错误）")
            return CaseTripleExtraction(triples=[_item()])

    fake = MixedFakeLLM()
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, stats = ect.extract_all([truncated_case, ok_case])
    assert stats["cases_truncated"] == 1
    assert stats["llm_calls"] == 2  # 两条都真的调用了一次，截断的那次也算
    assert len(records) == 1  # 第二条正常产出的那一条
    assert records[0].case_id == "b"


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


def test_dry_run_estimate_does_not_count_cases_without_excerpt(tmp_path):
    """dry-run 报的预估调用数要跟实际会调用的次数一致——只数有 raw_excerpt
    的医案，数上 raw 会把成本估计得比实际跑出来的偏高。"""
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "有内容", "raw_excerpt": "有片段"},
        {"case_id": "b", "case_group_id": "b", "physician": "ye_tianshi",
         "raw": "只有整段原文，没有诊次片段", "raw_excerpt": None},
    ]), encoding="utf-8")

    assert ect._estimate_call_count(cases_path, limit=None) == 1


def test_main_writes_jsonl_in_established_format(tmp_path, monkeypatch):
    """字段名必须是 s/p/o，不是 subject/predicate/object——这是
    core/tools.py.query_case_graph() 已经在读的格式，不能各写各的。"""
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ]), encoding="utf-8")
    out_path = tmp_path / "case_triples.jsonl"

    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])

    line = out_path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert set(row) == {"case_id", "physician", "s", "p", "o", "source_span"}
    assert row["s"] == "脘痛" and row["p"] == "提示" and row["o"] == "肝木犯胃"


def test_main_warns_about_truncated_cases(tmp_path, monkeypatch, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ]), encoding="utf-8")

    monkeypatch.setattr(ect, "get_llm", lambda: TruncatingFakeLLM())
    ect.main(["--cases-path", str(cases_path), "--out", str(tmp_path / "out.jsonl")])
    out = capsys.readouterr().out
    assert "截断" in out
    assert "1 条医案" in out


def test_main_raises_clear_error_when_cases_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        ect.main(["--cases-path", str(tmp_path / "nope.json")])


def test_case_triple_item_rejects_empty_source_span():
    with pytest.raises(Exception):
        CaseTripleItem(s="a", p="提示", o="c", source_span="")


# ---------- 谓词受控词表（schema 层用 Literal 强制） ----------


def test_case_triple_item_rejects_predicate_outside_the_controlled_vocabulary():
    """R2 --limit 5 试水实测：不限定谓词时模型吐出 15 种以上写法（起于/表现为/
    诊断为/脉象/病机为/治法/治则……），其中好几个互为同义词，query_case_graph
    的子串匹配对同义词无能为力。收紧成六个固定值，schema 层用 Literal 强制——
    模型给了表外谓词，pydantic 必须直接拒绝，不能指望事后清洗。"""
    with pytest.raises(ValidationError):
        CaseTripleItem(s="脘痛", p="表现为", o="纳差", source_span="x")


def test_case_triple_item_accepts_all_six_controlled_predicates():
    for p in ("提示", "属于", "治以", "用方", "含", "用药"):
        CaseTripleItem(s="a", p=p, o="b", source_span="x")
