"""offline/extract_case_triples.py 的离线测试：假 LLM 后端，不需要网络。

跟 tests/test_extract_cases.py 一样的假 LLM 模式（monkeypatch get_llm）。
这里要覆盖的核心逻辑有三层防幻觉核验，pydantic schema 都管不了，是
extract_case_triples.py 自己做的：
  1. source_span 的逐字核验（这个模块最初存在的理由）
  2. s/o 不能是"此症""患者"这类指代词占位符（R2 第一轮 --limit 5 试水暴露）
  3. 只用 raw_excerpt 核验/喂给模型，raw_excerpt 缺失就跳过、不退回整段 raw
     （同一轮试水暴露：退回 raw 会让同一病人的多次诊次读到同一段文本、抽出
     不一致甚至跨诊次的内容）
以及批处理本身要扛得住单条失败——两种失败分开处理：
  4. 输出被截断（LLMTruncatedError）：重试没有意义（同样的输入会在同一处
     再次被截断），跳过、计数，不建议重跑
  5. 其余 LLMError（网络抖动/超时/限流/非截断的格式错误，core.llm.generate()
     已经重试 3 次仍失败）：值得重跑，记下 case_id，支持 --only-ids 只重跑
     这几条，结果按 case_id 合并回主输出文件

谓词的六选一（core.schemas.CaseTriplePredicate）是 schema 层拦的，pydantic
自己会报错，不需要这里再测——CaseTripleItem 的构造失败已经是 pydantic 的
标准行为，这里只补一条确认 Literal 真的生效了。
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.llm import LLMError, LLMTruncatedError
from core.schemas import CaseRecord, CaseTripleExtraction, CaseTripleItem
from offline import extract_case_triples as ect


def _write_cases_json(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


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


class FailingFakeLLM:
    """总是抛通用 LLMError（不是 LLMTruncatedError）——模拟网络抖动/超时/限流，
    core.llm.generate() 已经重试 3 次仍失败后才会走到调用方手里。用 `from`
    保留一个真实的底层异常类型，模拟 core.llm 那句 `raise LLMError(...) from
    last_error`。"""

    def __init__(self, cause: Exception | None = None):
        self.calls = 0
        self.cause = cause if cause is not None else TimeoutError("网络超时（测试用假错误）")

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        raise LLMError("LLM 调用在 3 次尝试后仍失败（测试用假错误）") from self.cause


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

    records, rejected, failure = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure is None
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

    records, rejected, failure = ect.extract_case(case)
    assert rejected == {"span_not_found": 1, "referential": 0}
    assert failure is None
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

    records, rejected, failure = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 1}
    assert failure is None
    assert len(records) == 1
    assert records[0].s == "脘痛"


def test_referential_object_is_rejected(monkeypatch):
    """跟上一条对称：指代词出现在宾语位置同样要丢——"用药 用药 患者" 这种
    宾语是"患者"的三元组一样是脏数据。"""
    case = _case()
    bad = _item(o="患者")
    fake = FakeLLM(CaseTripleExtraction(triples=[bad]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 1}
    assert failure is None
    assert records == []


def test_referential_check_is_exact_match_not_substring(monkeypatch):
    """黑名单是整串相等，不是子串——"患者" 整个词才拦，含"患者"两个字的合法
    医学表述（比如作为更长短语的一部分）不该被误伤。"""
    case = _case()
    ok = _item(s="患者自述纳差")  # 不是黑名单里的任何一个整串，应该放行
    fake = FakeLLM(CaseTripleExtraction(triples=[ok]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure is None
    assert len(records) == 1


def test_empty_extraction_is_not_an_error(monkeypatch):
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure is None


def test_source_span_checked_against_excerpt_raw_never_consulted(monkeypatch):
    """raw_excerpt 存在时，raw 完全不参与——不止是"优先用 excerpt"，是 raw
    里的内容压根不算数。用一个只在 raw 里出现、excerpt 里没有的片段当
    source_span，必须被拒绝，即使 raw 和 excerpt 都不为空。"""
    case = _case(raw="整段原文，另有别的诊次内容", raw_excerpt="这一诊的片段")
    fake = FakeLLM(CaseTripleExtraction(triples=[_item(source_span="这一诊的片段")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure is None
    assert len(records) == 1

    fake2 = FakeLLM(CaseTripleExtraction(triples=[_item(source_span="整段原文，另有别的诊次内容")]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake2)
    records2, rejected2, failure2 = ect.extract_case(case)
    assert rejected2 == {"span_not_found": 1, "referential": 0}
    assert failure2 is None
    assert records2 == []


def test_no_source_text_returns_empty_without_calling_llm(monkeypatch):
    """raw_excerpt 和 raw 都没有——不该发生（raw 是必填字段），但防御性地
    也不该编。"""
    case = _case(raw="", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure is None
    assert fake.calls == 0  # 没有原文可核验，不该白烧一次调用


def test_missing_raw_excerpt_is_skipped_even_though_raw_has_real_content(monkeypatch):
    """R2 --limit 5 试水实测出的真实 bug：raw_excerpt 缺失但 raw 有内容时，
    旧版会退回整段 raw 喂模型。现在的正确行为是跳过这条医案，不产出任何
    三元组、不调用 LLM，不是退化成用 raw。"""
    case = _case(raw="整段原文，包含这一病人全部诊次的内容", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure is None
    assert fake.calls == 0  # 没有 raw_excerpt，不该拿 raw 顶上去调用


def test_extract_case_passes_higher_max_tokens_than_the_backend_default(monkeypatch):
    """一张九味药的方子，九条「含」关系的 source_span 各自重抄一遍整张方子，
    约 1800 字纯重复撞上默认的 8192。S5 这一处调用要显式传更大的
    max_tokens，不是让它落到后端默认值。"""
    case = _case()
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    ect.extract_case(case)
    assert fake.kwargs_seen[0]["max_tokens"] == ect.S5_MAX_TOKENS
    assert ect.S5_MAX_TOKENS > 8192  # 明确大于后端默认值，不是凑巧等于


def test_truncated_output_is_skipped_not_retried_or_raised(monkeypatch):
    """LLMTruncatedError 一路往上抛，整批全量跑崩掉——这里要捕获它，返回空
    结果 + failure="truncated"，让调用方（extract_all）跳过这条医案继续跑
    下一条，而不是让一条医案的输出格式问题拖垮整批。"""
    case = _case()
    fake = TruncatingFakeLLM()
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure == "truncated"
    assert fake.calls == 1  # 截断不重试——core.llm 的 generate() 已经不重试了，
    # 这里额外确认 extract_case 自己也没有再包一层重试


def test_generic_llm_error_is_caught_and_classified_by_cause_type(monkeypatch):
    """新增：LLMTruncatedError 是子类，普通 LLMError（网络抖动/超时/限流/
    非截断的格式错误）在上一轮没有被捕获，会让整批崩掉——这跟截断当初的
    问题是同一类。这里用一个带真实底层异常（TimeoutError）的 LLMError 验证
    extract_case 能接住它，并且失败原因用 type(err.__cause__).__name__ 分类，
    不是恒定的一个固定字符串（不然"失败原因的分布"这个要求就没有意义了）。"""
    case = _case()
    fake = FailingFakeLLM(cause=TimeoutError("测试用假超时"))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, rejected, failure = ect.extract_case(case)
    assert records == []
    assert rejected == {"span_not_found": 0, "referential": 0}
    assert failure == "TimeoutError"
    assert fake.calls == 1  # 不在这里再包一层重试——core.llm.generate() 已经重试过了


def test_generic_llm_error_without_cause_falls_back_to_its_own_type_name(monkeypatch):
    """理论上 LLMError 也可能没有 __cause__（比如被别的代码直接构造而不是
    经 core.llm.generate() 的 from last_error 那条路径）——这种情况下用
    LLMError 自己的类型名兜底，不该崩在这里。"""
    class BareErrorLLM:
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            raise LLMError("没有 __cause__ 的假错误")

    case = _case()
    monkeypatch.setattr(ect, "get_llm", lambda: BareErrorLLM())

    records, rejected, failure = ect.extract_case(case)
    assert records == []
    assert failure == "LLMError"


def test_truncated_error_is_not_caught_by_the_generic_error_branch(monkeypatch):
    """顺序敏感：LLMTruncatedError 是 LLMError 的子类，except 顺序反了会让
    截断也被通用错误分支接住，丢失"截断不值得重跑"这条区分——failed_case_ids
    会混进本来不该重跑的 case_id。"""
    case = _case()
    fake = TruncatingFakeLLM()
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    _, _, failure = ect.extract_case(case)
    assert failure == "truncated"
    assert failure != "LLMTruncatedError"  # 不会被当成"普通失败"的一种


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
    """一条医案输出被截断，不该让整批 extract_all 崩掉——统计里要能看出
    "跳过了几条"，其余医案照常处理。"""
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


def test_extract_all_survives_scattered_llm_errors_across_20_cases(monkeypatch):
    """验收场景：20 条医案，一部分抛通用 LLMError（不是截断），断言不崩、
    失败计入 stats、失败的 case_id 被记录、失败原因的分布可读。

    失败下标用固定集合而不是真的 random.random()——固定下标能让 CI 稳定
    复现，效果（失败散布在批次中间，前后都有成功的）跟"随机"是一回事，
    真随机只会让这条测试偶发失败，测不出更多东西。"""
    n = 20
    fail_at_calls = {3, 7, 12, 18}  # 第几次调用失败（1-based），散布在各处
    cases = [_case(case_id=f"c{i}", case_group_id=f"c{i}") for i in range(n)]

    class ScatteredFailureLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            if self.calls in fail_at_calls:
                raise LLMError("模拟网络抖动（测试用假错误）") from ConnectionError("连接被重置")
            return CaseTripleExtraction(triples=[_item()])

    fake = ScatteredFailureLLM()  # 同一个实例贯穿全部调用——lambda 里现建
    # 会让 self.calls 每次都从 0 开始，fail_at_calls 永远命中不了
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    records, stats = ect.extract_all(cases)  # 能跑到这里、不抛异常就是"不崩"

    assert stats["cases_failed"] == len(fail_at_calls)
    assert stats["llm_calls"] == n  # 20 条全部真的调用过，失败的也算一次调用
    expected_failed_ids = {f"c{i - 1}" for i in fail_at_calls}  # calls 是 1-based
    assert set(stats["failed_case_ids"]) == expected_failed_ids
    assert stats["failure_reasons"] == {"ConnectionError": len(fail_at_calls)}
    assert len(records) > 0  # 没失败的那些正常产出，不是被牵连成全灭


# ---------- extract_all：进度回调 ----------


def test_on_progress_fires_every_n_cases_and_at_the_end(monkeypatch):
    """941 条要跑十几分钟，中途崩了要知道跑到哪——这条钉住"每 PROGRESS_EVERY
    条报一次，外加处理完最后一条必报一次"这个节奏，不满一个整数倍的尾巴
    不能被吃掉（比如 130 条：报在 50、100、130，不是只报 50、100 然后
    静默结束）。"""
    fake = FakeLLM(CaseTripleExtraction(triples=[]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)
    cases = [_case(case_id=str(i), case_group_id=str(i)) for i in range(130)]

    calls = []
    ect.extract_all(cases, on_progress=lambda done, total, stats: calls.append((done, total)))

    assert calls == [(50, 130), (100, 130), (130, 130)]


def test_on_progress_not_called_when_omitted(monkeypatch):
    """默认不传就是 None，不该强迫所有调用方（包括现有测试）都提供一个回调。"""
    fake = FakeLLM(CaseTripleExtraction(triples=[]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)
    ect.extract_all([_case()])


def test_on_progress_receives_a_snapshot_not_a_live_reference(monkeypatch):
    """传给回调的 stats 必须是那一刻的快照，不能是后续还会被原地修改的同一个
    dict——包括嵌套的 list/dict 字段（failed_case_ids/failure_reasons），
    浅拷贝保护不了它们，得单独拷一份。"""
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)
    cases = [_case(case_id=str(i), case_group_id=str(i)) for i in range(60)]

    snapshots = []
    ect.extract_all(cases, on_progress=lambda done, total, stats: snapshots.append(stats))

    assert snapshots[0]["triples_extracted"] == 50  # 第 50 条时已抽出 50 条
    assert snapshots[1]["triples_extracted"] == 60  # 第 60 条时已抽出 60 条
    assert snapshots[0]["triples_extracted"] == 50  # 没被第二次回调悄悄改掉


def test_on_progress_snapshot_failed_case_ids_not_shared_across_snapshots(monkeypatch):
    """failed_case_ids 是列表，专门验证它不是同一个引用——第一份快照存下的
    列表内容不该因为后续处理往同一个列表里 append 而跟着变长。"""
    cases = [_case(case_id=str(i), case_group_id=str(i)) for i in range(60)]

    class AllFailLLM:
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            raise LLMError("全部失败（测试用假错误）") from RuntimeError("x")

    monkeypatch.setattr(ect, "get_llm", lambda: AllFailLLM())
    snapshots = []
    ect.extract_all(cases, on_progress=lambda done, total, stats: snapshots.append(stats))

    assert len(snapshots[0]["failed_case_ids"]) == 50
    assert len(snapshots[1]["failed_case_ids"]) == 60
    assert len(snapshots[0]["failed_case_ids"]) == 50  # 第一份没被追加到 60


def test_on_progress_snapshot_reflects_truncated_and_failed_counts(monkeypatch):
    cases = [_case(case_id=str(i), case_group_id=str(i)) for i in range(3)]
    cases[1] = _case(case_id="1", case_group_id="1", raw="有内容", raw_excerpt=None)

    class MixedFakeLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LLMTruncatedError("测试用假错误")
            return CaseTripleExtraction(triples=[_item()])

    fake = MixedFakeLLM()  # 同一个实例贯穿多次调用——lambda 里现建会让 calls
    # 计数器每次都从 0 开始，两次调用各自都撞上 self.calls == 1 的截断分支
    monkeypatch.setattr(ect, "get_llm", lambda: fake)
    snapshots = []
    ect.extract_all(cases, on_progress=lambda done, total, stats: snapshots.append(stats))

    final = snapshots[-1]
    assert final["cases_truncated"] == 1
    assert final["cases_no_text"] == 1
    assert final["triples_extracted"] == 1


# ---------- extract_all：on_case_done 回调 ----------


def test_on_case_done_fires_only_for_cases_with_text(monkeypatch):
    """没有 raw_excerpt、根本没尝试抽取的医案不该触发这个回调——没有内容
    可落盘，触发了也没有信息量，调用方（比如 main() 的增量落盘）不需要
    为这种情况做任何事。"""
    with_excerpt = _case(case_id="a", case_group_id="a")
    no_excerpt = _case(case_id="b", case_group_id="b", raw="x", raw_excerpt=None)
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    seen = []
    ect.extract_all(
        [with_excerpt, no_excerpt],
        on_case_done=lambda case, records, failure: seen.append(case.case_id),
    )
    assert seen == ["a"]


def test_on_case_done_receives_records_and_failure_reason(monkeypatch):
    truncated_case = _case(case_id="a", case_group_id="a")
    ok_case = _case(case_id="b", case_group_id="b")

    class MixedFakeLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise LLMTruncatedError("测试用假错误")
            return CaseTripleExtraction(triples=[_item()])

    fake = MixedFakeLLM()  # 同一个实例贯穿两次调用，见上面同类修复的理由
    monkeypatch.setattr(ect, "get_llm", lambda: fake)
    seen = {}
    ect.extract_all(
        [truncated_case, ok_case],
        on_case_done=lambda case, records, failure: seen.__setitem__(case.case_id, (records, failure)),
    )
    assert seen["a"] == ([], "truncated")
    ok_records, ok_failure = seen["b"]
    assert ok_failure is None
    assert len(ok_records) == 1


# ---------- 落盘合并：_load_existing_rows / _write_rows ----------


def test_load_existing_rows_returns_empty_dict_when_file_missing(tmp_path):
    assert ect._load_existing_rows(tmp_path / "nope.jsonl") == {}


def test_load_existing_rows_groups_by_case_id(tmp_path):
    out_path = tmp_path / "out.jsonl"
    out_path.write_text(
        json.dumps({"case_id": "a", "physician": "ye_tianshi", "s": "x", "p": "提示", "o": "y", "source_span": "z"})
        + "\n"
        + json.dumps({"case_id": "a", "physician": "ye_tianshi", "s": "x2", "p": "含", "o": "y2", "source_span": "z2"})
        + "\n"
        + json.dumps({"case_id": "b", "physician": "wu_jutong", "s": "x3", "p": "治以", "o": "y3", "source_span": "z3"})
        + "\n",
        encoding="utf-8",
    )
    grouped = ect._load_existing_rows(out_path)
    assert set(grouped) == {"a", "b"}
    assert len(grouped["a"]) == 2
    assert len(grouped["b"]) == 1


def test_write_rows_then_load_round_trips(tmp_path):
    out_path = tmp_path / "out.jsonl"
    grouped = {
        "a": [{"case_id": "a", "physician": "ye_tianshi", "s": "脘痛", "p": "提示", "o": "肝木犯胃", "source_span": "x"}],
    }
    ect._write_rows(out_path, grouped)
    assert ect._load_existing_rows(out_path) == grouped


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
    assert ect._estimate_call_count(_write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "有内容", "raw_excerpt": "有片段"},
        {"case_id": "b", "case_group_id": "b", "physician": "ye_tianshi",
         "raw": "只有整段原文，没有诊次片段", "raw_excerpt": None},
    ]), limit=None) == 1


def test_main_writes_jsonl_in_established_format(tmp_path, monkeypatch):
    """字段名必须是 s/p/o，不是 subject/predicate/object——这是
    core/tools.py.query_case_graph() 已经在读的格式，不能各写各的。"""
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ])
    out_path = tmp_path / "case_triples.jsonl"

    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])

    line = out_path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert set(row) == {"case_id", "physician", "s", "p", "o", "source_span"}
    assert row["s"] == "脘痛" and row["p"] == "提示" and row["o"] == "肝木犯胃"


def test_main_warns_about_truncated_cases(tmp_path, monkeypatch, capsys):
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ])

    monkeypatch.setattr(ect, "get_llm", lambda: TruncatingFakeLLM())
    ect.main(["--cases-path", str(cases_path), "--out", str(tmp_path / "out.jsonl")])
    out = capsys.readouterr().out
    assert "截断" in out
    assert "1 条医案" in out


def test_main_warns_about_failed_cases_and_suggests_only_ids(tmp_path, monkeypatch, capsys):
    """新增：非截断失败要打印出来，并且给出可以直接照抄的 --only-ids 命令，
    不是让人自己去日志里翻 case_id。"""
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ])

    monkeypatch.setattr(ect, "get_llm", lambda: FailingFakeLLM())
    ect.main(["--cases-path", str(cases_path), "--out", str(tmp_path / "out.jsonl")])
    out = capsys.readouterr().out
    assert "调用失败" in out
    assert "--only-ids a" in out
    assert "TimeoutError" in out  # 失败原因分布里能看到真实的异常类型


def test_main_prints_progress_for_a_batch_crossing_the_report_boundary(tmp_path, monkeypatch, capsys):
    """941 条这种长跑批次最需要的就是这行输出——这里用 PROGRESS_EVERY + 1 条
    医案确认真的打了两次进度（一次在整数倍处、一次是收尾），不是只在全部
    跑完后才输出一次汇总。"""
    n = ect.PROGRESS_EVERY + 1
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": str(i), "case_group_id": str(i), "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"}
        for i in range(n)
    ])

    monkeypatch.setattr(ect, "get_llm", lambda: FakeLLM(CaseTripleExtraction(triples=[])))
    ect.main(["--cases-path", str(cases_path), "--out", str(tmp_path / "out.jsonl")])
    out = capsys.readouterr().out
    assert f"进度 {ect.PROGRESS_EVERY}/{n}" in out
    assert f"进度 {n}/{n}" in out


def test_main_raises_clear_error_when_cases_json_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="extract_cases"):
        ect.main(["--cases-path", str(tmp_path / "nope.json")])


def test_case_triple_item_rejects_empty_source_span():
    with pytest.raises(Exception):
        CaseTripleItem(s="a", p="提示", o="c", source_span="")


# ---------- CLI：--only-ids ----------


def test_only_ids_rejects_unknown_case_id(tmp_path):
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "x"},
    ])
    with pytest.raises(SystemExit, match="不在"):
        ect.main(["--cases-path", str(cases_path), "--only-ids", "nonexistent"])


def test_only_ids_filters_to_just_the_requested_cases(tmp_path, monkeypatch):
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
        {"case_id": "b", "case_group_id": "b", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ])
    fake = FakeLLM(CaseTripleExtraction(triples=[_item()]))
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    ect.main(["--cases-path", str(cases_path), "--out", str(tmp_path / "out.jsonl"), "--only-ids", "a"])
    assert fake.calls == 1  # 只处理了 a，没有处理 b


def test_only_ids_rerun_merges_new_result_and_preserves_untouched_case_ids(tmp_path, monkeypatch):
    """核心场景：第一轮 a 成功、b 失败；用 --only-ids b 单独重跑，这次 b 成功。
    最终文件要同时有 a（第一轮的旧结果，原样保留）和 b（重跑的新结果）——
    不是清空重来，也不是只有 b。"""
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
        {"case_id": "b", "case_group_id": "b", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ])
    out_path = tmp_path / "out.jsonl"

    class FirstRunLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            if self.calls == 1:  # a
                return CaseTripleExtraction(triples=[_item()])
            raise LLMError("b 失败（测试用假错误）") from TimeoutError("x")  # b

    fake = FirstRunLLM()  # 同一个实例贯穿两次调用——lambda 里现建会让
    # self.calls 每次都从 0 开始，两条都各自撞上 calls == 1 的成功分支
    monkeypatch.setattr(ect, "get_llm", lambda: fake)
    ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])

    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert {r["case_id"] for r in rows} == {"a"}  # b 失败，没有写入

    # 第二轮：只重跑 b，这次成功
    monkeypatch.setattr(
        ect, "get_llm", lambda: FakeLLM(CaseTripleExtraction(triples=[_item(o="纳差")]))
    )
    ect.main(["--cases-path", str(cases_path), "--out", str(out_path), "--only-ids", "b"])

    rows2 = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert {r["case_id"] for r in rows2} == {"a", "b"}
    a_rows = [r for r in rows2 if r["case_id"] == "a"]
    b_rows = [r for r in rows2 if r["case_id"] == "b"]
    assert a_rows[0]["o"] == "肝木犯胃"  # a 的旧结果原样保留
    assert b_rows[0]["o"] == "纳差"  # b 是重跑后的新结果


def test_failed_rerun_does_not_wipe_previously_successful_result(tmp_path, monkeypatch):
    """重跑一条失败的医案，如果这次还是失败，不该把这条医案之前成功的旧结果
    抹掉——这里模拟"a 先成功，后来因为别的原因又被 --only-ids a 重跑，但
    这次网络又抖了"，a 的旧结果必须还在文件里。"""
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": "a", "case_group_id": "a", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"},
    ])
    out_path = tmp_path / "out.jsonl"

    monkeypatch.setattr(ect, "get_llm", lambda: FakeLLM(CaseTripleExtraction(triples=[_item()])))
    ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])
    rows = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1

    monkeypatch.setattr(ect, "get_llm", lambda: FailingFakeLLM())
    ect.main(["--cases-path", str(cases_path), "--out", str(out_path), "--only-ids", "a"])
    rows2 = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
    assert rows2 == rows  # 重跑失败，旧结果原样还在


# ---------- CLI：中途落盘（崩溃/中断不丢全部结果）----------


def test_main_persists_partial_results_when_process_crashes_mid_run(tmp_path, monkeypatch):
    """941 条要跑十几分钟，中途真的崩了（这里用一个不被 core.llm 包装过的
    裸异常模拟——比如进程被杀、或者代码本身有别的 bug），已经跑完的结果不该
    全部丢掉。第 50 条落盘检查点已经过了之后再崩，文件里就该已经有前 50 条
    的内容，不用等到第 53 条彻底跑完才有数据。"""
    n = ect.PROGRESS_EVERY + 5
    cases_path = _write_cases_json(tmp_path, [
        {"case_id": f"c{i}", "case_group_id": f"c{i}", "physician": "ye_tianshi",
         "raw": "x", "raw_excerpt": "脘痛不食，脉弦，此肝木犯胃，治以疏肝和胃。"}
        for i in range(n)
    ])
    out_path = tmp_path / "out.jsonl"

    class CrashingLLM:
        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            if self.calls == ect.PROGRESS_EVERY + 3:
                # 裸 RuntimeError，不是 LLMError/LLMTruncatedError——模拟
                # extract_case 接不住的真正崩溃，不是"抽取失败"这种可以
                # 优雅处理的情况
                raise RuntimeError("模拟进程崩溃（测试用假错误，不经过 core.llm 包装）")
            return CaseTripleExtraction(triples=[_item()])

    fake = CrashingLLM()  # 同一个实例贯穿全部调用，见上面同类修复的理由——
    # 否则 self.calls 永远重置成 1，撞不上第 PROGRESS_EVERY+3 次那个崩溃点
    monkeypatch.setattr(ect, "get_llm", lambda: fake)

    with pytest.raises(RuntimeError, match="模拟进程崩溃"):
        ect.main(["--cases-path", str(cases_path), "--out", str(out_path)])

    assert out_path.exists()
    rows = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) >= ect.PROGRESS_EVERY  # 第 50 条那次落盘检查点已经写过


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
