"""core/chain.py 的离线测试：用假 LLM 后端和假检索器，不需要网络。"""
import json
import threading
from core import chain
from core.retrieval import Retriever
from core.schemas import (
    CaseRecord,
    ElementHit,
    S1Normalize,
    S2Elements,
    S3Syndrome,
    S3SyndromeUnreferenced,
    _S3Base,
)


import pytest


@pytest.fixture(autouse=True)
def _pin_two_physicians(monkeypatch):
    """这里的期望值（llm_calls == 4、S3 跑 2 次、FakeLLM 按「叶天士/吴鞠通」分发）都
    写死在两位医家上。注册张锡纯之后（HANDOFF 步骤 3）registry 变成三位，这些测试
    会整批红——那不是 bug，是测试写死了医家数。钉住两位，让 registry 增长与测试解耦。"""
    from core.physicians import physicians_enabled as _enabled

    REG = _enabled()

    two = {k: REG[k] for k in ("ye_tianshi", "wu_jutong")}
    monkeypatch.setattr(chain, "PHYSICIANS", two)


class FakeLLM:
    """按 schema 类型返回预设响应，同时记录调用次数方便断言 S1 只跑一次
    （以及安全否决命中时 S2/S3 一次都不调用）。"""

    def __init__(self, s3_by_physician: dict[str, S3Syndrome], s1: S1Normalize | None = None):
        self.s3_by_physician = s3_by_physician
        self.s1 = s1 or S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
        self.calls: list[str] = []
        self._current_physician: str | None = None

    # manifest 现在从后端问模型名/后端名（不再读 LLM_MODEL 环境变量），
    # 假后端要跟着实现这几个方法，否则 _build_manifest 会 AttributeError。
    def model_name(self) -> str:
        return "fake-model"

    def backend_id(self) -> str:
        return "fake"

    def comparability_warning(self) -> str | None:
        return "后端：fake（离线测试用），不产生任何可用于报告的数字。"

    # 本地模型接入那一轮新加的两个：run_physician 把"这位医家实际挂了哪个
    # LoRA adapter"记进结果、_build_manifest 记 adapter 根目录，两处都要问
    # 后端。假后端跟真后端的默认实现保持一致——没有 adapter 这回事就是 None
    # （core/llm.py::LLMBackend.lora_for/lora_dir 的默认返回值）。
    def lora_for(self, physician: str | None) -> str | None:
        return None

    def lora_dir(self) -> str | None:
        return None

    # R3 录制回放那一轮新加的：manifest 记"这次是不是回放的录制结果"，
    # 所以 _build_manifest 会无条件问后端。假后端跟真后端的默认实现保持一致
    # ——None = 实时调用（core/llm.py::LLMBackend.replay_info 的默认返回值）。
    def replay_info(self) -> dict | None:
        return None

    def generate(self, system: str, user: str, schema, temperature: float = 0.0, **kwargs):
        self.calls.append(schema.__name__)
        if schema is S1Normalize:
            return self.s1
        if schema is S2Elements:
            return S2Elements(
                elements=[
                    ElementHit(
                        element="脾",
                        kind="location",
                        supporting_symptoms=["纳差"],
                        confidence="high",
                    )
                ],
                unexplained_symptoms=[],
            )
        if issubclass(schema, _S3Base):
            # S3Syndrome / S3SyndromeUnreferenced 是同一个 _S3Base 的两个子类，
            # 只在 cited_case_ids 上有无区别（core/schemas.py::_S3Base 的文档
            # 字符串）。chain.py 按该医家检索是否为空动态选 schema——只认
            # S3Syndrome 会在"某位医家检索为空"这条真实会发生的路径上
            # （min_score 卡掉全部结果，之前叶天士就出现过）把假 LLM 自己先
            # 炸掉，而不是暴露产品代码的问题。依赖 system 提示词里包含医家
            # 姓名来区分各位医家的返回值。
            for physician_name, s3 in self.s3_by_physician.items():
                if physician_name in system:
                    return _coerce_s3(s3, schema)
            # 医家不在 s3_by_physician 里（比如注册表里新加的医家、这条测试
            # 没显式配置过）：给个通用兜底响应，不是让整条测试因为"不认识
            # 这个医家"而炸。这些测试大多守的是 SSE 事件序列，不是"每位医家
            # 的内容对不对"。
            return _coerce_s3(None, schema)
        raise AssertionError(f"未预期的 schema: {schema}")


def _coerce_s3(s3: "_S3Base | None", schema):
    """把预设的 S3 响应（或没有预设时的通用兜底内容）转成实际被请求的 schema。

    两个 S3 schema 只在 cited_case_ids 上不同：S3Syndrome 必填，
    S3SyndromeUnreferenced 没有这个字段。假 LLM 不能因为调用方按哪个 schema
    准备了预设值，就在检索状态切换（有检索结果 <-> 检索为空）时把这条区别
    弄反——那正是防幻觉约束要测的东西，弄反了测试会悄悄测出错误的结论
    （比如检索为空却带上了 cited_case_ids，让"检索为空不该有引用"失效）。
    """
    if isinstance(s3, schema):
        return s3
    if s3 is not None:
        data = s3.model_dump(exclude={"cited_case_ids"})
    else:
        data = {
            "syndrome": "脾胃气虚",
            "reasoning": "通用兜底响应（未在 s3_by_physician 中显式配置）",
            "treatment_principle": "健脾益气",
            "formula_candidates": [{
                "name": "四君子汤", "source": "classic", "confidence": "medium",
                "rationale": "兜底响应，供 SSE 事件序列等结构性测试使用，非真实医案推导。",
                "herb_items": [{"name": "党参"}, {"name": "白术"}],
            }],
        }
    if schema is S3Syndrome and not data.get("cited_case_ids"):
        data["cited_case_ids"] = ["fallback-case-001"]
    return schema(**data)


class FakeRetriever(Retriever):
    def __init__(self, cases: list[CaseRecord]):
        self.cases = cases

    def search(self, query, physician, k=3, min_score=0.0, **kwargs):
        # **kwargs 接住但不用：P0-13 起 adaptive_min_score 在 mode="dense"
        # 时会真的探测一次（core/retrieval.py），这个假实现不关心 mode 本身
        # 返回什么结果，只是不能因为多传了这个关键字就报 TypeError——跟
        # tests/test_retriever_mode.py 的 RecordingRetriever 同一条理由。
        hits = [c for c in self.cases if c.physician == physician][:k]
        return [(c, 0.9) for c in hits]


def _fake_cases() -> list[CaseRecord]:
    return [
        CaseRecord(
            case_id="ye_tianshi-001",
            case_group_id="ye_tianshi-001",
            physician="ye_tianshi",
            raw="原文",
            symptoms=["纳差"],
            tongue="淡红",
            pulse="细弱",
            syndrome="脾胃气虚",
            herbs=["党参", "白术"],
        ),
        CaseRecord(
            case_id="wu_jutong-001",
            case_group_id="wu_jutong-001",
            physician="wu_jutong",
            raw="原文",
            symptoms=["纳差"],
            tongue="淡红",
            pulse="细弱",
            syndrome="脾胃气虚",
            herbs=["党参", "白术"],
        ),
    ]


# ---------- V5 P0-1：_format_case_block（原文摘录 + 结构化字段）----------


def _case(**overrides):
    base = dict(
        case_id="ye_tianshi-001", case_group_id="ye_tianshi-001",
        physician="ye_tianshi", raw="原文", visit_index=0,
    )
    base.update(overrides)
    return CaseRecord(**base)


def test_format_case_block_puts_raw_excerpt_before_structured_fields():
    case = _case(raw_excerpt="患者胃脘胀痛，嗳气泛酸，舌淡红苔薄白，脉弦。",
                 symptoms=["胃脘胀痛"], tongue="淡红", herbs=["柴胡"])
    block = chain._format_case_block(case)
    raw_pos = block.index("患者胃脘胀痛")
    structured_pos = block.index("结构化：")
    assert raw_pos < structured_pos, "原文摘录必须排在结构化字段前面（P0-1 的核心改动）"


def test_format_case_block_truncates_long_raw_excerpt():
    long_text = "甲" * 500
    case = _case(raw_excerpt=long_text)
    block = chain._format_case_block(case)
    assert "甲" * chain.CASE_EXCERPT_TRUNCATE_CHARS in block
    assert "甲" * (chain.CASE_EXCERPT_TRUNCATE_CHARS + 1) not in block


def test_format_case_block_missing_raw_excerpt_says_so_explicitly():
    """raw_excerpt 为 None 时不能假装有原文，也不能整段消失——退回结构化
    字段，但要标注原文缺失（用户原话：这 3% 的医案退回结构化字段，标注
    "（原文缺失）"）。"""
    case = _case(raw_excerpt=None, symptoms=["纳差"])
    block = chain._format_case_block(case)
    assert "原文缺失" in block
    assert "结构化：症状=纳差" in block


def test_format_case_block_omits_missing_fields_instead_of_writing_unrecorded():
    """P0 根因修复的核心：缺失字段直接省略，不写"未记"——三个"未记"比什么都
    不写更削弱这条医案的可信度（跟示例的具体内容相比，看起来像"这条参考
    没什么用"）。"""
    case = _case(raw_excerpt="原文内容", symptoms=["纳差"])  # 无舌/脉/证/病机/治法/方/药
    block = chain._format_case_block(case)
    assert "未记" not in block
    assert "结构化：症状=纳差" in block  # 只有症状这一项，其余字段整个不出现


def test_format_case_block_includes_all_present_structured_fields():
    case = _case(
        raw_excerpt="原文", symptoms=["胃脘胀痛"], tongue="淡红", pulse="弦",
        syndrome="肝胃不和证", pathogenesis="肝郁犯胃", treatment_principle="疏肝和胃",
        formula="柴胡疏肝散", herbs=["柴胡", "白芍"],
    )
    block = chain._format_case_block(case)
    for expected in ("症状=胃脘胀痛", "舌=淡红", "脉=弦", "证=肝胃不和证",
                      "病机=肝郁犯胃", "治法=疏肝和胃", "方=柴胡疏肝散", "药=柴胡、白芍"):
        assert expected in block


def test_format_case_block_no_structured_fields_at_all():
    case = _case(raw_excerpt="原文")  # symptoms 默认空列表，其余全 None
    block = chain._format_case_block(case)
    assert "结构化：（无结构化字段）" in block


def test_format_case_block_header_has_case_id_and_visit_label():
    case = _case(case_id="wu_jutong-002-p1-0", visit_index=1, raw_excerpt="原文")
    block = chain._format_case_block(case)
    assert "wu_jutong-002-p1-0" in block
    assert "第2诊" in block


def test_run_physician_embeds_raw_excerpt_in_the_real_prompt_after_the_example(monkeypatch):
    """上面几条 _format_case_block 单测只测格式化函数本身对不对，测不出它的
    输出有没有真的送到模型看得见的地方——P0 要修的根因恰恰是"参考医案的内容
    没进到 prompt 里"。这里跑一次真实的 run_physician()，断言 raw_excerpt
    的内容确实出现在发给 LLM 的 system prompt 里、且排在示例 JSON 之后
    （P0-3 的顺序要求）。E3 在 AutoDL 上重跑不过时，靠这条测试排除"格式化对了
    但没送到模型那里"这种可能——如果这条测试绿了还是不过，问题在别处
    （比如模型没理会 prompt），不在这个环节。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条测的是 prompt 里有没有 raw_excerpt。采 N 次会拿到 N 份一样的 system prompt，
    # 断言 len(s3_systems) == 1 就不再成立，而那跟这条要验的事没关系。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    excerpt = "此患者形瘦神疲，纳谷不香，脘腹痞满，特征串POCKMARK7f3a用于定位"
    case = _case(raw_excerpt=excerpt, symptoms=["纳差"], tongue="淡红", pulse="细弱")
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever([case]))

    s1 = S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(
        elements=[ElementHit(element="脾", kind="location",
                             supporting_symptoms=["纳差"], confidence="high")],
        unexplained_symptoms=[],
    )
    chain.run_physician(s1, s2, "ye_tianshi", "叶天士")

    assert len(fake_llm.s3_systems) == 1
    system = fake_llm.s3_systems[0]
    assert excerpt in system, "raw_excerpt 的内容必须真的出现在发给 LLM 的 system prompt 里"
    assert system.index("病名占位") < system.index(excerpt), (
        "参考医案（含 raw_excerpt）必须排在示例 JSON 之后，不能被示例盖过（P0-3）"
    )


def test_run_physician_flags_low_discrimination_when_retrieval_scores_are_close(monkeypatch):
    """P0-12：检索到的几条候选之间没有真实区分度时，run_physician() 的返回值
    要带 low_discrimination=True、refs 收窄到 1 条——不是只在内部悄悄截断，
    调用方（eval/run_eval.py 的 E3 报告）要能看到这个标记。FakeRetriever
    对每条命中都返回固定相似度 0.9，两条命中分差正好是 0，天然落进
    "没有区分度"这个条件。

    **P0-13**：这条判据只对 dense/graph 模式成立（改动 3），所以这里显式
    传 retriever_mode="dense"——不传的话会走默认（hybrid），这条判据在
    hybrid 下根本不生效，测的就不是这条判据本身了。"""
    cases = [_case(case_id="ye_tianshi-001", raw_excerpt="甲"),
             _case(case_id="ye_tianshi-002", raw_excerpt="乙")]
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))

    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    result = chain.run_physician(s1, s2, "ye_tianshi", "叶天士", retriever_mode="dense")

    assert result["low_discrimination"] is True
    assert len(result["refs"]) == 1


def test_run_physician_no_low_discrimination_flag_when_scores_have_real_spread(monkeypatch):
    class SpreadRetriever(Retriever):
        def __init__(self, cases):
            self.cases = cases

        def search(self, query, physician, k=3, min_score=0.0, **kwargs):
            hits = [c for c in self.cases if c.physician == physician][:k]
            scores = [0.90, 0.60, 0.50]
            return [(c, scores[i]) for i, c in enumerate(hits)]

    cases = [_case(case_id="ye_tianshi-001"), _case(case_id="ye_tianshi-002")]
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: SpreadRetriever(cases))

    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    result = chain.run_physician(s1, s2, "ye_tianshi", "叶天士", retriever_mode="dense")

    assert result["low_discrimination"] is False
    assert len(result["refs"]) == 2


def test_run_physician_low_discrimination_can_be_disabled_via_env(monkeypatch):
    monkeypatch.setenv("LOW_DISCRIMINATION_CUTOFF", "0")
    cases = [_case(case_id="ye_tianshi-001"), _case(case_id="ye_tianshi-002")]
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))

    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    # retriever_mode="dense"：确认关掉的是 LOW_DISCRIMINATION_CUTOFF 这个开关本身
    # 生效，不是巧合落进了"hybrid 模式下这条判据本来就不生效"这条别的路径。
    result = chain.run_physician(s1, s2, "ye_tianshi", "叶天士", retriever_mode="dense")

    assert result["low_discrimination"] is False
    assert len(result["refs"]) == 2


def test_run_physician_hybrid_mode_never_flags_low_discrimination_even_when_scores_tie(monkeypatch):
    """P0-13 契约变更的直接验证：同样的"两条命中分差为 0"场景，默认模式
    （解析成 hybrid）下不该触发——跟上面 test_run_physician_flags_
    low_discrimination_when_retrieval_scores_are_close 是同一份测试数据，
    唯一的区别是这里不传 retriever_mode。"""
    cases = [_case(case_id="ye_tianshi-001", raw_excerpt="甲"),
             _case(case_id="ye_tianshi-002", raw_excerpt="乙")]
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))

    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    result = chain.run_physician(s1, s2, "ye_tianshi", "叶天士")

    assert result["low_discrimination"] is False
    assert len(result["refs"]) == 2


# ---------- P0-13 验证 B：非 dense 模式下不再有多余的探测调用 ----------


class _CountingRetriever(Retriever):
    """记每次 search() 调用（不区分探测还是真正查询——P0-13 改动 2 之后
    非 dense 模式根本不该有探测这一步，调用总数就该等于真正查询的次数）。"""

    def __init__(self, cases):
        self.cases = cases
        self.call_count = 0

    def search(self, query, physician, k=3, min_score=0.0, **kwargs):
        self.call_count += 1
        hits = [c for c in self.cases if c.physician == physician][:k]
        return [(c, 0.9) for c in hits]


def test_search_cases_default_mode_calls_search_exactly_once(monkeypatch):
    """P0-13 验证 B：_search_cases 在默认（不传 retriever_mode，解析成
    hybrid）路径下只调用一次 retriever.search()——P0-7 曾经的探测调用
    （adaptive_min_score）在非 dense 模式下已经被跳过，不是"探测 + 真正
    查询"两次。"""
    r = _CountingRetriever([_case(case_id="ye_tianshi-001")])
    monkeypatch.setattr(chain, "get_retriever", lambda: r)
    s2 = S2Elements(elements=[], unexplained_symptoms=[])

    chain._search_cases("纳差", "ye_tianshi", s2, retriever_mode=None)

    assert r.call_count == 1


@pytest.mark.parametrize("mode", ["bm25", "graph", "hybrid"])
def test_search_cases_non_dense_modes_call_search_exactly_once(monkeypatch, mode):
    r = _CountingRetriever([_case(case_id="ye_tianshi-001")])
    monkeypatch.setattr(chain, "get_retriever", lambda: r)
    s2 = S2Elements(
        elements=[ElementHit(element="脾", kind="location",
                             supporting_symptoms=["纳差"], confidence="high")],
        unexplained_symptoms=[],
    )

    chain._search_cases("纳差", "ye_tianshi", s2, retriever_mode=mode)

    assert r.call_count == 1


def test_search_cases_dense_mode_calls_search_exactly_twice(monkeypatch):
    """对照组：显式请求 dense 模式时，adaptive_min_score 的探测才是真的
    有意义（dense 分支仍然用 min_score 过滤），这里应该是探测 + 真正查询
    两次——不是"P0-13 之后所有模式都只调一次"，只有 dense 该有两次。"""
    r = _CountingRetriever([_case(case_id="ye_tianshi-001")])
    monkeypatch.setattr(chain, "get_retriever", lambda: r)
    s2 = S2Elements(elements=[], unexplained_symptoms=[])

    chain._search_cases("纳差", "ye_tianshi", s2, retriever_mode="dense")

    assert r.call_count == 2


def test_shared_stages_run_once_per_physician_stages_run_twice(monkeypatch):
    """S1 和 S2 全局各只跑一次，S3 每位医家一次。

    S1 只跑一次是硬约束：跑两次会得到两份不同的症状列表，构图时症状节点
    id 对不上，边指向不存在的节点（CLAUDE.md 已知的坑）。

    S2 只跑一次是后来的决定：s2_elements.yaml 里没有 $name 占位符，模型
    不知道自己在为哪位医家推断，temperature=0 下逐医家各跑一次只会得到
    几乎相同的结果。医家条件化发生在 S3（通过检索到的该医家医案），
    S2 是客观的证素抽取，图上证素层本来也是所有医家共享同一批节点。

    这三个数字任何一个变了都要先想清楚为什么，不要直接改期望值。
    """
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条测的是「共享阶段只跑一次、按医家阶段每位一次」。best-of-N 让每位医家的 S3
    # 变成 N 次，会把这条判据的分子改掉——而它要钉的是「S1 有没有被跑两次」。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力，脉细弱",
        treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-001"],
    )
    s3_wu = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力，脉细弱",
        treatment_principle="健脾益气",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert fake_llm.calls.count("S1Normalize") == 1
    assert fake_llm.calls.count("S2Elements") == 1
    assert fake_llm.calls.count("S3Syndrome") == 2
    assert len(outcome["results"]) == 2


def test_divergence_true_when_syndromes_differ(monkeypatch):
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="...",
        treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-001"],
    )
    s3_wu = S3Syndrome(
        syndrome="湿热中阻",
        reasoning="...",
        treatment_principle="清热化湿",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert outcome["divergence"]["same"] is False
    # 1.3（E2）有意的契约变更：method 从过时的 "exact_string_match"（第一版按
    # 证型名字符串比对，早就换成药物集合了，字段一直没跟着改）改成
    # "nway_jaccard+pairwise"——本轮再次修正：之前一度写成 "pairwise_herb_
    # jaccard"，但那只描述了 pairs 的算法，顶层 herb_jaccard 字段本身其实是
    # n 方交并比（set.intersection(*herb_sets)），不是两两配对，标签跟实际
    # 算法对不上会误导只看这个字段的人。见 core/chain.py 里这个字段旁的注释。
    assert outcome["divergence"]["method"] == "nway_jaccard+pairwise"


def test_hallucination_detected_when_cited_id_not_in_refs(monkeypatch):
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="...",
        treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-999"],  # 不在检索结果里
    )
    s3_wu = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="...",
        treatment_principle="健脾益气",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    ye_result = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")
    wu_result = next(r for r in outcome["results"] if r["physician"] == "wu_jutong")
    assert ye_result["hallucinated"] == ["ye_tianshi-999"]
    assert wu_result["hallucinated"] == []


def test_normal_consult_reports_not_rejected(monkeypatch):
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-001"],
    )
    s3_wu = S3Syndrome(
        syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
        cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert outcome["rejected"] is False
    assert outcome["reject_reason"] is None


def test_consult_rejects_before_s2_on_danger_symptoms(monkeypatch):
    """CLAUDE.md 要求安全否决必须发生在 S2 之前：命中危重症状时，S2Elements
    和 S3Syndrome 一次都不应该被调用，results 必须是空的，不产出任何方药。"""
    danger_s1 = S1Normalize(
        symptoms=["胃脘疼痛", "解黑色柏油样便", "头晕心慌"], tongue="淡", pulse="细数", unmapped=[]
    )
    fake_llm = FakeLLM({}, s1=danger_s1)
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("胃脘疼痛数月，近日解黑色柏油样便，头晕心慌，面色苍白，倦怠乏力，舌淡，脉细数。")

    assert outcome["rejected"] is True
    assert "柏油样便" in outcome["reject_reason"]
    assert outcome["results"] == []
    assert outcome["divergence"] is None
    # 一次都不消耗后续的 S2/S3 调用——只应该有 S1Normalize 这一次调用
    assert fake_llm.calls == ["S1Normalize"]


# ---------- X2 输出侧安全：Generate-Verify-Revise 闭环 ----------

class ViolatingThenCleanLLM(FakeLLM):
    """第一次 S3 开出含配伍禁忌的方，收到带【安全问题】的重开提示后改开干净方。
    用来验证重开真的被触发，而不是只把违规标出来就算完。

    契约变更（M2）：重开提示的标记从【配伍禁忌】改成【安全问题】——M2 把重开
    触发条件从"只看十八反十九畏"扩成"十八反十九畏或剂量超限"两种拦截级问题，
    提示语要能同时描述这两种，不能再用只指代配伍禁忌的旧标记。
    """

    def __init__(self, s3_by_physician, clean_by_physician):
        super().__init__(s3_by_physician)
        self.clean_by_physician = clean_by_physician
        self.retry_prompts: list[str] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        # issubclass 而不是恒等比较：某位医家检索为空时 chain.py 重开也会用
        # S3SyndromeUnreferenced（同一个 s3_schema 变量原样带进重开调用），
        # 只认 S3Syndrome 会在这条路径上漏判、把重开当成普通调用处理。
        if issubclass(schema, _S3Base) and "【安全问题】" in system:
            self.calls.append(schema.__name__)
            self.retry_prompts.append(system)
            for name, s3 in self.clean_by_physician.items():
                if name in system:
                    return s3
            raise AssertionError("重开时无法判断医家")
        return super().generate(system, user, schema, temperature, **kwargs)


def _s3(syndrome, herbs, case_id):
    return S3Syndrome(
        syndrome=syndrome, reasoning="...", treatment_principle="健脾益气",
        herbs=herbs, cited_case_ids=[case_id],
    )


# ---------- 本地模型：physician 传下去、实际挂的 adapter 记下来 ----------


def test_physician_id_is_passed_to_generate_not_the_chinese_name(monkeypatch):
    """本地后端（vLLM + LoRA）按 physician 选 adapter，所以 S3 这次调用是替谁
    做的必须传下去——而且传的是**医家 id 不是中文名**：SOURCES.md 第 31 条
    那个坑就是 id/中文名混用，按 id 索引的东西恒空、单元测试全绿。
    S1/S2 跟医家无关，不该带 physician（对应基座模型）。"""
    seen: list[str | None] = []

    class RecordingFakeLLM(FakeLLM):
        def generate(self, system, user, schema, temperature=0.0, physician=None, **kwargs):
            seen.append(physician)
            return super().generate(system, user, schema, temperature, **kwargs)

    fake_llm = RecordingFakeLLM({
        "叶天士": _s3("脾胃气虚", ["党参"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["黄芪"], "wu_jutong-001"),
    })
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    chain.consult("纳差乏力")

    assert seen[:2] == [None, None]  # S1 / S2
    assert set(seen[2:]) == {"ye_tianshi", "wu_jutong"}  # 两位医家的 S3
    assert "叶天士" not in seen and "吴鞠通" not in seen  # 不是中文名


def test_each_physician_result_records_the_adapter_actually_used(monkeypatch, tmp_path):
    """「这位医家用的是他自己的 LoRA」这句声称只有在每位医家的结果上才可验证
    ——manifest 是整次问诊一份，而 adapter 是按医家切的。manifest 只记
    adapter 根目录（"从哪来的"）。"""
    class LoRAAwareFakeLLM(FakeLLM):
        def lora_for(self, physician):
            return physician  # 像配好了 LORA_DIR 的真后端那样按医家回答

        def lora_dir(self):
            return str(tmp_path)

    fake_llm = LoRAAwareFakeLLM({
        "叶天士": _s3("脾胃气虚", ["党参"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["黄芪"], "wu_jutong-001"),
    })
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")
    by_physician = {r["physician"]: r for r in outcome["results"]}
    assert by_physician["ye_tianshi"]["lora"] == "ye_tianshi"
    assert by_physician["wu_jutong"]["lora"] == "wu_jutong"
    assert outcome["manifest"]["lora_dir"] == str(tmp_path)


def test_lora_fields_are_none_on_backends_without_adapters(monkeypatch):
    """默认（云端）后端下这两个字段必须是 None——字段存在不能让读报告的人
    以为挂了 adapter。"""
    fake_llm = FakeLLM({
        "叶天士": _s3("脾胃气虚", ["党参"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["黄芪"], "wu_jutong-001"),
    })
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")
    assert all(r["lora"] is None for r in outcome["results"])
    assert outcome["manifest"]["lora_dir"] is None


def test_incompatible_formula_triggers_one_regeneration(monkeypatch):
    """S3 开出甘草+海藻（十八反）→ 把冲突写进 prompt 重开一次 → 重开后干净。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条数的是「重开了几次」，采样次数会把总调用数抬上去。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    dirty = {
        "叶天士": _s3("脾胃气虚", ["甘草", "海藻", "白术"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["党参", "白术", "茯苓"], "wu_jutong-001"),
    }
    clean = {
        "叶天士": _s3("脾胃气虚", ["甘草", "白术", "茯苓"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["党参", "白术", "茯苓"], "wu_jutong-001"),
    }
    fake = ViolatingThenCleanLLM(dirty, clean)
    monkeypatch.setattr(chain, "get_llm", lambda: fake)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    ye = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")
    wu = next(r for r in outcome["results"] if r["physician"] == "wu_jutong")

    # 叶天士这一支被打回重开，重开后无冲突
    assert ye["safety_output"]["revised"] is True
    assert ye["safety_output"]["incompatible"] == []
    assert "海藻" not in ye["s3"].herbs
    # 吴鞠通那一支本来就干净，不该被重开
    assert wu["safety_output"]["revised"] is False

    # 重开的 prompt 里必须点名具体冲突，否则模型不知道要避开什么
    assert len(fake.retry_prompts) == 1
    assert "甘草" in fake.retry_prompts[0] and "海藻" in fake.retry_prompts[0]

    # llm_calls 要把重开算进去：S1 + S2 + 2×S3 + 1 次重开 = 5
    assert outcome["manifest"]["llm_calls"] == 5


def test_still_incompatible_after_retry_is_reported_not_hidden(monkeypatch):
    """重开之后仍然违规：保留结果但如实标出来，不再重开第二次。
    这条比"修好了"更重要——它是系统承认自己没修好的地方。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 同上：数的是重开，不是采样。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    dirty = {
        "叶天士": _s3("脾胃气虚", ["甘草", "海藻", "白术"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["党参", "白术", "茯苓"], "wu_jutong-001"),
    }
    fake = ViolatingThenCleanLLM(dirty, dirty)  # 重开后还是那张违规方
    monkeypatch.setattr(chain, "get_llm", lambda: fake)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")
    ye = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")

    assert ye["safety_output"]["revised"] is True
    assert ye["safety_output"]["incompatible"] == [("甘草", "海藻")]
    # 只重开一次，不循环——llm_calls 仍然是 5 而不是更多
    assert len(fake.retry_prompts) == 1
    assert outcome["manifest"]["llm_calls"] == 5


def test_clean_formula_does_not_regenerate(monkeypatch):
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 同上：数的是「没重开」。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    clean = {
        "叶天士": _s3("脾胃气虚", ["党参", "白术", "茯苓"], "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃气虚", ["党参", "白术", "甘草"], "wu_jutong-001"),
    }
    fake = ViolatingThenCleanLLM(clean, clean)
    monkeypatch.setattr(chain, "get_llm", lambda: fake)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert all(r["safety_output"]["revised"] is False for r in outcome["results"])
    assert fake.retry_prompts == []
    # 没有重开，S1 + S2 + 2×S3 = 4
    assert outcome["manifest"]["llm_calls"] == 4


def test_thermal_warning_surfaces_without_regeneration(monkeypatch):
    """寒热不一致只警告不打回：revised 必须是 False。"""
    cold_formula = ["黄连", "黄芩", "石膏", "知母", "白术", "茯苓"]
    s3s = {
        "叶天士": _s3("脾胃虚寒证", cold_formula, "ye_tianshi-001"),
        "吴鞠通": _s3("脾胃虚寒证", cold_formula, "wu_jutong-001"),
    }
    fake = ViolatingThenCleanLLM(s3s, s3s)
    monkeypatch.setattr(chain, "get_llm", lambda: fake)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")
    ye = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")

    assert ye["safety_output"]["thermal_warning"] is not None
    assert "寒热方向可能相悖" in ye["safety_output"]["thermal_warning"]
    assert ye["safety_output"]["revised"] is False
    assert fake.retry_prompts == []


# ---------- G2：use_react 开关 ----------

class ReActFakeLLM(FakeLLM):
    """在 FakeLLM 基础上驱动 ReAct 循环：查一次图谱就 finish。
    同时把每次 S3 收到的 system 提示词存下来，用于断言"不开 ReAct 时
    prompt 跟改造前逐字节一致"。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.s3_systems: list[str] = []
        # **每位医家一个步数计数器，不是一个全局的。** R12 三位医家改成并发之后
        # 一个全局计数器会被两个线程交替加：叶天士第二次进来可能看到的是吴鞠通加过
        # 的奇数，于是又查一次图谱、跑出 3 步，而吴鞠通只跑 1 步。这是**假后端**
        # 在建模"一次只有一位医家"——真后端每次调用是无状态的，不存在这个问题。
        # 按 system 里出现的医家名分桶，跟 FakeLLM 分发 S3 用的是同一个判据。
        self._react_step: dict[str, int] = {}
        self._react_lock = threading.Lock()

    def _react_bucket(self, system: str) -> str:
        for physician_name in self.s3_by_physician:
            if physician_name in system:
                return physician_name
        return "<未知医家>"

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        from core.schemas import ReActStep

        if schema is ReActStep:
            self.calls.append("ReActStep")
            bucket = self._react_bucket(system)
            with self._react_lock:
                self._react_step[bucket] = self._react_step.get(bucket, 0) + 1
                step = self._react_step[bucket]
            if step % 2 == 1:
                return ReActStep(thought="先看看证素对应哪些证候",
                                 action="query_graph", action_input={"node": "纳呆"})
            return ReActStep(thought="够了", action="finish")
        if issubclass(schema, _S3Base):
            # 同样不能只认 S3Syndrome：某位医家检索为空时 S3 提示词走的是
            # S3SyndromeUnreferenced，漏记会让"prompt 内容"这类断言对该医家
            # 悄悄失去覆盖，而不是报错——比恒等比较直接崩溃更难发现。
            self.s3_systems.append(system)
        return super().generate(system, user, schema, temperature, **kwargs)


def _react_setup(monkeypatch):
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"], herbs=["党参", "白术"])
    s3_wu = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["wu_jutong-001"], herbs=["党参", "白术"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    import core.react as react_mod
    monkeypatch.setattr(react_mod, "get_llm", lambda: fake_llm)
    return fake_llm


def test_react_receives_physician_id_not_only_chinese_name(monkeypatch):
    """P1-1.1b：run_physician 手里的 physician 已经是 id，必须显式传给
    run_react（prompt 里 $physician_id 的来源）——不能只传中文名让 run_react
    去反查，反查是兜底不是主路径（SOURCES.md 第 31 条）。"""
    _react_setup(monkeypatch)
    seen = []
    real = chain.run_react

    def spy(**kwargs):
        seen.append((kwargs["name"], kwargs.get("physician_id")))
        return real(**kwargs)

    monkeypatch.setattr(chain, "run_react", spy)
    chain.consult("纳差乏力", use_react=True)
    # **R12 起比集合不比列表**：三位医家改成并发之后，谁先进 run_react 是不确定的。
    # 这条钉的是"每位医家都拿到了自己的 id、而且是 id 不是中文名"，跟顺序无关——
    # 原来写成列表只是因为那时是串行的，不是因为顺序是契约。
    assert sorted(seen) == sorted([("叶天士", "ye_tianshi"), ("吴鞠通", "wu_jutong")])


def test_react_off_by_default_leaves_s3_prompt_untouched(monkeypatch):
    """不开 ReAct 时 S3 的提示词里不能多出任何东西——多一个字，
    use_react 的 A/B 就混进了 prompt 变化这个额外变量。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条数的是 ReAct 的调用，采样次数跟它无关。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    fake_llm = _react_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", use_react=False)

    assert fake_llm.calls.count("ReActStep") == 0
    assert all(r["react_trace"] is None for r in outcome["results"])
    assert all("取证过程" not in s for s in fake_llm.s3_systems)
    assert outcome["manifest"]["use_react"] is False
    assert outcome["manifest"]["llm_calls"] == 4  # S1 + S2 + S3×2


def test_react_on_appends_evidence_and_counts_its_calls(monkeypatch):
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 同上：数的是 ReAct 那几步。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    fake_llm = _react_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", use_react=True)

    traces = [r["react_trace"] for r in outcome["results"]]
    assert all(t is not None and t.terminated_by == "finish" for t in traces)
    assert all("取证过程" in s for s in fake_llm.s3_systems)
    assert outcome["manifest"]["use_react"] is True
    # 4 次原有调用 + 每位医家 2 步 ReAct。漏算的话 manifest 报的调用数
    # 会低于实际花费，拿它算成本或比 use_react 的代价就都是错的。
    assert outcome["manifest"]["llm_calls"] == 4 + sum(t.llm_calls for t in traces) == 8


def test_react_does_not_weaken_the_hallucination_check(monkeypatch):
    """ReAct 的取证结果里可能出现别的 case id。引用白名单仍然只认检索到的 refs，
    这条一旦松掉，整个防幻觉设计就从 ReAct 这个新入口漏了。"""
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-999"])
    s3_wu = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["wu_jutong-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    import core.react as react_mod
    monkeypatch.setattr(react_mod, "get_llm", lambda: fake_llm)

    outcome = chain.consult("纳差乏力", use_react=True)
    ye = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")
    assert ye["hallucinated"] == ["ye_tianshi-999"]
    assert "cited_case_ids" in fake_llm.s3_systems[0], "附加证据里要重申引用白名单"


def test_use_react_none_reads_environment(monkeypatch):
    """R21 **有意的契约变更**：`USE_REACT=1` 只在 top3 系里生效。

    原断言没有 `monkeypatch.setenv("RETRIEVER_MODE", "hybrid")` 这一行——那一版
    还没有 full_context。默认模式换成 full_context 之后 ReAct 在默认路径上是关的
    （§1.3：语料已经全在上下文里，工具是冗余的），所以这条测试要显式说明自己
    测的是 top3 系。full_context 下那一半由 tests/test_react.py 的
    test_react_is_off_in_full_context_even_when_asked_for 覆盖。
    """
    _react_setup(monkeypatch)
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    monkeypatch.setenv("USE_REACT", "1")
    assert chain.consult("纳差乏力")["manifest"]["use_react"] is True
    monkeypatch.setenv("USE_REACT", "0")
    assert chain.consult("纳差乏力")["manifest"]["use_react"] is False


# ---------- G3：追问接进 consult ----------

def _followup_setup(monkeypatch, s3=None):
    s3_ye = s3 or S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                             cited_case_ids=["ye_tianshi-001"])
    s3_wu = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                       cited_case_ids=["wu_jutong-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.delenv("FAST_MODE", raising=False)
    return fake_llm


def test_consult_without_ask_channel_is_unchanged(monkeypatch):
    """没有提问渠道就不追问，调用数跟改造前一样。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条数的是「没有提问渠道时调用数不变」。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    fake_llm = _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力")
    assert outcome["followup"].stopped_by == "no_answer"
    assert outcome["manifest"]["llm_calls"] == 4
    assert all("追问结果" not in s for s in fake_llm.s3_systems)


def _affirm_unless_dangerous(question: str) -> str:
    """测试用的"通情达理"患者：什么都说"有"，除非问的是危重症状。

    R2 教材扩表后 Category 2 的修复保证候选池里第一条未问过的安全相关症状
    一定会被问到（core/tools.py::question_candidates），跟诊断信息增益无关。
    这份 fixture 下面几条测试要验证的是"新症状怎么并回 S2"这条跟安全无关的
    路径——如果对安全相关问题也机械地答"有"，会在第一轮就真触发
    check_safety/danger_confirmed_by_answer 而提前终止整个 consult（这是
    正确行为，不是 bug：真答"有便血"就该被拦），但会让这几条测试测不到它们
    本来要测的东西。用 mentions_danger 识别"这一句问的是不是危重症状"
    （core/safety.py 现成的判据，不是新写一套），对危重症状照实答"没有"，
    其余一律"有"。"""
    from core.safety import mentions_danger

    return "没有" if mentions_danger(question) else "有"


def test_followup_costs_one_extra_s2_no_matter_how_many_rounds(monkeypatch):
    """追问每轮 0 次 LLM 调用（规则解析 + 图上贝叶斯更新），只在问出了新症状之后
    整体重跑一次 S2。按轮收费的话这个 demo 就没法用了（G2 实测每次调用 4-8s）。

    这里的 S2 一共 3 次：初次、追问后并入新症状那次、以及残差辨证那次
    （残差是既有行为，跟追问无关——它被触发是因为追问加进来的症状本身
    没被 FakeLLM 的证素解释）。轮数变多时这个数不许跟着涨。
    """
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条数的是追问额外花的那一次 S2。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    fake_llm = _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=_affirm_unless_dangerous)
    assert outcome["followup"].rounds >= 2
    assert fake_llm.calls.count("S2Elements") == 3
    assert fake_llm.calls.count("S1Normalize") == 1, "S1 仍然全局只跑一次"
    # 2(S1+S2) + 1(追问后的 S2) + 2(两位医家 S3) + 1(残差)
    assert outcome["manifest"]["llm_calls"] == 6


def test_asserted_symptoms_are_merged_into_the_symptom_list(monkeypatch):
    _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=_affirm_unless_dangerous)
    assert outcome["followup"].asserted, "全答有没有一条断言，这条测试就测不到并入这一步了"
    for s in outcome["followup"].asserted:
        assert s in outcome["s1"].symptoms


def test_all_denials_do_not_rerun_s2(monkeypatch):
    """全是否定回答时没有新症状可并，不该白花一次 S2。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 同上：数的是 S2 有没有被重跑。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    fake_llm = _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "没有")
    assert outcome["followup"].denied
    assert outcome["followup"].asserted == []
    assert fake_llm.calls.count("S2Elements") == 1
    assert outcome["manifest"]["llm_calls"] == 4


def test_denials_reach_the_s3_prompt(monkeypatch):
    fake_llm = _followup_setup(monkeypatch)
    chain.consult("纳差乏力", ask_fn=lambda q: "没有")
    assert all("患者明确否认" in s for s in fake_llm.s3_systems)
    assert all("阴性证据" in s for s in fake_llm.s3_systems)


def test_dangerous_followup_answer_rejects_and_produces_no_formula(monkeypatch):
    """追问是安全否决层的后门（CLAUDE.md）。问出危重症状要跟初始主诉命中
    同一道否决，S3 一次都不能调。"""
    fake_llm = _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有，昨天开始解黑便")
    assert outcome["rejected"] is True
    assert "黑便" in outcome["reject_reason"]
    assert outcome["results"] == []
    assert fake_llm.calls.count("S3Syndrome") == 0


def test_fast_mode_skips_followup_in_consult(monkeypatch):
    fake_llm = _followup_setup(monkeypatch)
    monkeypatch.setenv("FAST_MODE", "1")
    asked = []
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: asked.append(q) or "有")
    assert outcome["followup"].stopped_by == "fast_mode"
    assert asked == []
    assert fake_llm.calls.count("S2Elements") == 1


def test_max_ask_rounds_zero_reaches_run_followup_and_asks_nothing(monkeypatch):
    """R55：`consult(max_ask_rounds=0)` 必须真的传到 `run_followup`，不是
    只在 `consult()` 自己的签名里加了个没人读的参数——医师角色（算出来是 0）
    传进来之后，一个追问问题都不该发出去。"""
    _followup_setup(monkeypatch)
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    asked = []
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: asked.append(q) or "有",
                            max_ask_rounds=0)
    assert asked == []
    assert outcome["followup"].rounds == 0


def test_max_ask_rounds_none_keeps_the_module_default(monkeypatch):
    """不传（CLI/eval/批跑现状）行为跟改造前逐字节一致——落回
    `core.followup.MAX_ASK_ROUNDS`，不是 0。"""
    from core.followup import MAX_ASK_ROUNDS

    fake_llm = _followup_setup(monkeypatch)
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    asked = []
    chain.consult("纳差乏力", ask_fn=lambda q: (asked.append(q), "没有")[1])
    # 没传 max_ask_rounds：问几轮就该跟模块默认的上限一致（不多问）。
    assert len(asked) <= MAX_ASK_ROUNDS


# ---------- 审查修复 ----------


class EmptyRetriever(Retriever):
    def search(self, query, physician, k=3, min_score=0.0):
        return []


class UnreferencedFakeLLM(ReActFakeLLM):
    """检索为空时 chain 会改用 S3SyndromeUnreferenced，假后端要认得它。"""

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        if schema is S3SyndromeUnreferenced:
            self.calls.append("S3SyndromeUnreferenced")
            self.s3_systems.append(system)
            return S3SyndromeUnreferenced(syndrome="脾胃气虚", reasoning="...",
                                          treatment_principle="健脾益气", herbs=["党参"])
        return super().generate(system, user, schema, temperature, **kwargs)


def test_empty_retrieval_uses_schema_without_cited_case_ids(monkeypatch):
    """一条相关医案都没有时，S3Syndrome 的 min_length=1 会逼模型编一个 id。
    CLAUDE.md 的规定是新建一个不含该字段的 schema，不是放松原来的约束。"""
    # R22：**把 best-of-N 钉成 1**，让这条测试只测它自己那件事。
    # 这条测的是「检索为空时换哪个 schema」，采几次跟它无关。
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    fake_llm = UnreferencedFakeLLM({"叶天士": None, "吴鞠通": None})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: EmptyRetriever())
    outcome = chain.consult("纳差乏力")
    assert fake_llm.calls.count("S3Syndrome") == 0
    assert fake_llm.calls.count("S3SyndromeUnreferenced") == 2
    for r in outcome["results"]:
        assert r["no_reference_cases"] is True
        assert r["hallucinated"] == []
        assert r["s3"].cited_case_ids == []


def test_s3syndrome_min_length_untouched():
    """防幻觉约束本身一个字不能动。"""
    import pytest as _pt
    from pydantic import ValidationError

    with _pt.raises(ValidationError):
        S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=[])


def test_coverage_never_exceeds_one_when_s2_rewrites_symptom_names(monkeypatch):
    """S2 常把「胃脘胀痛」改写成「脘腹胀痛」；改写后的名字不在 s1 里，
    直接拿 supporting_symptoms 当分子，coverage 会算成 1.5。"""
    class RewritingLLM(ReActFakeLLM):
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is S2Elements:
                self.calls.append("S2Elements")
                return S2Elements(elements=[ElementHit(
                    element="脾", kind="location", confidence="high",
                    supporting_symptoms=["脘腹胀痛", "纳差", "舌淡"])])
            return super().generate(system, user, schema, temperature, **kwargs)

    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                    cited_case_ids=["ye_tianshi-001"])
    s3w = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                     cited_case_ids=["wu_jutong-001"])
    fake_llm = RewritingLLM({"叶天士": s3, "吴鞠通": s3w},
                            s1=S1Normalize(symptoms=["胃脘胀痛", "纳差"], unmapped=[]))
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    outcome = chain.consult("胃脘胀痛纳差")
    assert outcome["coverage"] == 0.5
    assert chain.explained_symptoms(outcome["s1"], outcome["s2"]) == {"纳差"}


def test_safety_checks_complaint_and_unmapped_not_only_symptoms(monkeypatch):
    """S1 会把「最近吐了两次血」这类病史归进 unmapped；只查 symptoms 会漏。
    三处（原始主诉、symptoms、unmapped）分别验证。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["ye_tianshi-001"])
    for s1 in [
        S1Normalize(symptoms=["纳差"], unmapped=["最近吐了两次血"]),          # 危重词只在 unmapped
    ]:
        fake_llm = FakeLLM({"叶天士": s3, "吴鞠通": s3}, s1=s1)
        monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
        monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
        assert chain.consult("纳差")["rejected"] is True
    # 危重词只在原始主诉里、S1 一个字都没保留
    fake_llm = FakeLLM({"叶天士": s3, "吴鞠通": s3}, s1=S1Normalize(symptoms=["纳差"], unmapped=[]))
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    assert chain.consult("纳差，昨天解黑便")["rejected"] is True
    assert fake_llm.calls.count("S2Elements") == 0


def test_insufficient_branch_skips_s3_and_pointless_residual(monkeypatch):
    """S2 一个证素都没推出来：不跑 S3；残差的输入跟 S2 一字不差，也不再白跑一次。"""
    class EmptyS2LLM(FakeLLM):
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is S2Elements:
                self.calls.append("S2Elements")
                return S2Elements(elements=[], unexplained_symptoms=["胸闷", "气短"])
            return super().generate(system, user, schema, temperature, **kwargs)

    fake_llm = EmptyS2LLM({}, s1=S1Normalize(symptoms=["胸闷", "气短"], unmapped=[]))
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    outcome = chain.consult("胸闷气短")
    assert outcome["insufficient"] is True
    assert outcome["results"] == []
    assert fake_llm.calls.count("S3Syndrome") == 0
    assert fake_llm.calls.count("S2Elements") == 1
    assert outcome["manifest"]["llm_calls"] == 2


def test_herb_jaccard_is_the_primary_divergence_metric(monkeypatch):
    """分歧主指标是药物集合的 Jaccard 距离（证型名字符串比对无区分度）。
    归一后相同的写法必须算同一味药。"""
    s3_ye = S3Syndrome(syndrome="脾虚", reasoning="x", treatment_principle="健脾",
                       herbs=["党参", "炒白术", "云苓块", "炙甘草"], cited_case_ids=["ye_tianshi-001"])
    s3_wu = S3Syndrome(syndrome="脾虚湿困", reasoning="x", treatment_principle="健脾",
                       herbs=["党参", "白术", "茯苓", "甘草", "陈皮", "半夏"], cited_case_ids=["wu_jutong-001"])
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    div = chain.consult("纳差乏力")["divergence"]
    assert div["shared_herbs"] == ["党参", "甘草", "白术", "茯苓"]
    assert div["herb_jaccard"] == round(1 - 4 / 6, 3)


def test_all_return_paths_share_the_same_keys(monkeypatch):
    """api/前端按同一份契约读三条路径，缺键就是 KeyError。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["ye_tianshi-001"])
    fake_llm = FakeLLM({"叶天士": s3, "吴鞠通": s3})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    normal = chain.consult("纳差乏力")
    rejected = chain.consult("纳差，昨天解黑便")
    followup_rejected = chain.consult("纳差乏力", ask_fn=lambda q: "有，还解了黑便")
    # safety_flag 是 EVAL_MODE 旁路引入的新键（这一轮的有意契约变更）：所有分支
    # 都要带，两种模式的键集才一致，api/前端按同一份契约读。
    expected = {"s1", "results", "divergence", "rejected", "reject_reason", "safety_flag", "s2",
                "residual", "followup", "insufficient", "insufficient_reason", "coverage", "manifest"}
    for outcome in (normal, rejected, followup_rejected):
        assert expected <= set(outcome), expected - set(outcome)


def test_react_ask_user_answer_goes_through_safety_and_reaches_s3(monkeypatch):
    """ReAct 以 ask_user 收尾时问题要真的问出去；回答先过 check_safety，
    危重就整体拒绝，正常就作为证据交给 S3。"""
    from core.schemas import ReActStep

    class AskingLLM(ReActFakeLLM):
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is ReActStep:
                self.calls.append("ReActStep")
                return ReActStep(thought="分不开", action="ask_user",
                                 action_input={"question": "有没有口苦？", "reason": "r"})
            return super().generate(system, user, schema, temperature, **kwargs)

    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["ye_tianshi-001"])
    s3w = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["wu_jutong-001"])
    import core.react as react_mod

    fake_llm = AskingLLM({"叶天士": s3, "吴鞠通": s3w})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(react_mod, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.setenv("FAST_MODE", "1")  # 关掉 G3 追问，只看 ReAct 那条追问路径

    ok = chain.consult("纳差乏力", use_react=True, ask_fn=lambda q: "没有口苦")
    assert ok["rejected"] is False
    assert all("患者答：没有口苦" in s for s in fake_llm.s3_systems)
    assert all(r["react_trace"].pending_answer == "没有口苦" for r in ok["results"])

    fake_llm.s3_systems.clear()
    bad = chain.consult("纳差乏力", use_react=True, ask_fn=lambda q: "有，而且解了黑便")
    assert bad["rejected"] is True and "黑便" in bad["reject_reason"]
    assert bad["results"] == []
    assert fake_llm.s3_systems == [], "被拦截后 S3 一次都不能调"


def test_explained_symptoms_is_the_single_source_of_truth(monkeypatch):
    """同一份 s1/s2 下，coverage、残差的 unexplained、图上的症状 state 必须一致。
    实测过三处各写一套时得出 0.5 / 0.25 / 1-of-4 三个互相矛盾的数。"""
    from api.main import to_graph

    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦", "腹胀"])
    # 模型把「乏力」同时列进 supporting_symptoms 和 unexplained_symptoms（实测常见）
    s2 = S2Elements(
        elements=[ElementHit(element="脾", kind="location",
                             supporting_symptoms=["纳差", "乏力"], confidence="high")],
        unexplained_symptoms=["乏力", "口苦", "腹胀"])
    assert chain.explained_symptoms(s1, s2) == {"纳差"}

    results = [{"physician": "ye_tianshi", "physician_name": "叶天士", "s2": s2,
                "s3": S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                                 cited_case_ids=["ye_tianshi-001"]),
                "refs": [], "hallucinated": []}]
    states = {n["data"]["id"]: n["data"]["state"]
              for n in to_graph(s1, results, s2)["nodes"] if n["data"]["layer"] == 0}
    assert sum(1 for v in states.values() if v == "explained") == 1


def test_batch_runner_handles_insufficient_without_crashing(capsys):
    """批跑器此前只处理 rejected，遇到 insufficient（divergence 为 None）会
    AttributeError，整批跑挂掉、前面几条的结果一起丢。"""
    def fake_consult(complaint, **kw):
        return {
            "s1": S1Normalize(symptoms=["胸闷"]), "results": [], "divergence": None,
            "rejected": False, "reject_reason": None, "insufficient": True,
            "insufficient_reason": "现有症状不足以推断证素", "manifest": {},
        }

    stats = chain.run_batch(["胸闷气短", "乏力"], consult_fn=fake_consult)
    assert stats["insufficient"] == 2 and stats["divergent"] == 0
    out = capsys.readouterr().out
    assert "[信息不足]" in out and "信息不足例数：2/2" in out


def test_batch_runner_counts_normal_and_rejected(capsys):
    """分母要排除被拦截和信息不足的例数，否则分歧率/幻觉率的分母是错的。"""
    s3 = S3Syndrome(syndrome="脾虚", reasoning="x", treatment_principle="健脾",
                    herbs=["党参"], cited_case_ids=["ye_tianshi-001"])

    def fake_consult(complaint, **kw):
        if "黑便" in complaint:
            return {"s1": S1Normalize(symptoms=[]), "results": [], "divergence": None,
                    "rejected": True, "reject_reason": "危重", "manifest": {}}
        return {
            "s1": S1Normalize(symptoms=["纳差"]), "rejected": False, "reject_reason": None,
            "insufficient": False,
            "results": [{"physician_name": "叶天士", "s3": s3, "hallucinated": ["x-999"],
                         "no_reference_cases": False}],
            "divergence": {"same": False, "herb_jaccard": 0.5, "shared_herbs": ["党参"],
                           "treatment_principle_same": True},
            "manifest": {},
        }

    stats = chain.run_batch(["纳差", "解黑便"], consult_fn=fake_consult)
    assert stats == {"total": 2, "rejected": 1, "insufficient": 0, "divergent": 1,
                     "hallucinated": 1, "durations": stats["durations"]}
    assert "分歧例数：1/1" in capsys.readouterr().out


# ---------- 模块 1（E）：divergence.epsilon_online ----------

def test_divergence_epsilon_online_is_none_without_epsilon_json(monkeypatch, tmp_path):
    monkeypatch.setattr(chain, "EPSILON_PATH", tmp_path / "nope.json")
    s3_ye = S3Syndrome(syndrome="脾虚", reasoning="x", treatment_principle="健脾",
                       herbs=["党参"], cited_case_ids=["ye_tianshi-001"])
    s3_wu = S3Syndrome(syndrome="脾虚", reasoning="x", treatment_principle="健脾",
                       herbs=["白术"], cited_case_ids=["wu_jutong-001"])
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    div = chain.consult("纳差乏力")["divergence"]
    assert div["epsilon_online"] is None


def test_divergence_epsilon_online_reads_from_epsilon_json(monkeypatch, tmp_path):
    p = tmp_path / "epsilon.json"
    p.write_text(json.dumps({"epsilon_online": {"mean": 0.22}}), encoding="utf-8")
    monkeypatch.setattr(chain, "EPSILON_PATH", p)
    s3_ye = S3Syndrome(syndrome="脾虚", reasoning="x", treatment_principle="健脾",
                       herbs=["党参"], cited_case_ids=["ye_tianshi-001"])
    s3_wu = S3Syndrome(syndrome="脾虚", reasoning="x", treatment_principle="健脾",
                       herbs=["白术"], cited_case_ids=["wu_jutong-001"])
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    div = chain.consult("纳差乏力")["divergence"]
    assert div["epsilon_online"] == 0.22


def test_divergence_epsilon_online_survives_corrupt_json(monkeypatch, tmp_path):
    """文件存在但不是合法 JSON（比如估算脚本被中断写了一半）不该让 consult 崩掉。"""
    p = tmp_path / "epsilon.json"
    p.write_text("{不是合法 json", encoding="utf-8")
    monkeypatch.setattr(chain, "EPSILON_PATH", p)
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"])
    fake_llm = FakeLLM({"叶天士": s3, "吴鞠通": s3})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    assert chain.consult("纳差乏力")["divergence"]["epsilon_online"] is None


# ---------- M4：病名层接进 S3 ----------


def test_disease_candidates_present_and_scored_from_symptoms_not_from_model(monkeypatch):
    """disease_candidates 是规则算出来的，跟模型填的 disease 字段脱钩——
    即便模型一个病名都没填，disease_candidates 也该照样有值（只要症状/证素
    能匹配上表里的病名）。"""
    class GastralgiaS2LLM(FakeLLM):
        # 基类 FakeLLM 的 S2Elements 分支写死返回 element="脾"，这里覆盖成
        # "胃"——跟本测试传入的胃痛类症状对应，不然 match_disease 拿到的病位
        # 证素跟症状文本对不上，断言会测到 FakeLLM 的默认值而不是真实行为。
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is S2Elements:
                self.calls.append("S2Elements")
                return S2Elements(
                    elements=[
                        ElementHit(
                            element="胃", kind="location",
                            supporting_symptoms=["胃脘胀痛"], confidence="high",
                        )
                    ],
                    unexplained_symptoms=[],
                )
            return super().generate(system, user, schema, temperature, **kwargs)

    s1 = S1Normalize(
        symptoms=["胃脘胀痛", "嗳气泛酸", "情志不畅"], tongue="淡红", pulse="弦", unmapped=[]
    )
    s3_ye = S3Syndrome(
        syndrome="肝胃不和证", reasoning="...", treatment_principle="疏肝和胃",
        herbs=["柴胡"], cited_case_ids=["ye_tianshi-001"],
    )  # 故意不填 disease
    s3_wu = S3Syndrome(
        syndrome="肝胃不和证", reasoning="...", treatment_principle="疏肝和胃",
        herbs=["柴胡"], cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = GastralgiaS2LLM({"叶天士": s3_ye, "吴鞠通": s3_wu}, s1=s1)
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("胃脘胀痛，嗳气泛酸，情志不畅")

    for r in outcome["results"]:
        assert r["s3"].disease is None
        assert r["disease_candidates"], "症状明显指向胃痛，disease_candidates 不该是空的"
        assert r["disease_candidates"][0][0] == "胃痛"


def test_disease_not_in_table_gets_warning_in_note_not_rejected(monkeypatch):
    """模型填的病名不在参考表（含别名）里时，只记 warning 到 note，不拒绝、
    不影响其余字段——CLAUDE.md「追问...安全否决」管的是危重症状拦截，这里
    是另一类判断：病名超出参考表不是安全问题，是"规则校验不了"。"""
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
        disease="这不是一个真实病名", herbs=["党参"], cited_case_ids=["ye_tianshi-001"],
    )
    s3_wu = S3Syndrome(
        syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
        disease="胃痛",  # 表内病名，不该触发 warning
        herbs=["党参"], cited_case_ids=["wu_jutong-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    assert outcome["rejected"] is False
    ye = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")
    wu = next(r for r in outcome["results"] if r["physician"] == "wu_jutong")
    assert "这不是一个真实病名" in ye["s3"].note
    assert "不在病名参考表" in ye["s3"].note
    assert wu["s3"].note is None


def test_disease_via_alias_does_not_get_warning(monkeypatch):
    """模型填的是别名（比如"痞"而不是"痞满"）时不该被当成表外病名——
    get_disease() 本身就支持别名查找，这里复用它，不另写一套判断。"""
    s3 = S3Syndrome(
        syndrome="脾虚气滞证", reasoning="...", treatment_principle="健脾理气",
        disease="痞", herbs=["党参"], cited_case_ids=["ye_tianshi-001"],
    )
    fake_llm = FakeLLM({"叶天士": s3, "吴鞠通": s3})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    outcome = chain.consult("纳差乏力")

    for r in outcome["results"]:
        assert r["s3"].note is None


# ---------- 1.3（E2）：分歧两两配对，师承内 vs 跨学派 ----------
#
# 三家 set.intersection 只有三家都用的药才算共同，jaccard 天然偏向 1.0，
# 分不清师承内（叶×吴，同温病学派）和跨学派（叶×张、吴×张）。pairs 三对
# 分别算，group 从 PHYSICIANS 的 school 字段判，year_gap 从 years 字段算。


def _s3_with_herbs(pid, herbs, tp="健脾益气"):
    return S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle=tp,
                      cited_case_ids=[f"{pid}-001"], herbs=herbs)


def test_pairwise_divergence_three_physicians_separates_lineage_from_cross_school(monkeypatch):
    """对着真实注册表（三位、两个学派）跑：三对各自的 Jaccard、分组、生年差，
    师承内均值 / 跨学派均值并列，判据 cross_school_gt_lineage 报出。"""
    from core.physicians import physicians_enabled as _enabled

    REAL_PHYSICIANS = _enabled()

    assert len(REAL_PHYSICIANS) == 3 and len({i["school"] for i in REAL_PHYSICIANS.values()}) == 2

    herbs = {
        "ye_tianshi": ["党参", "白术", "茯苓", "甘草"],
        "wu_jutong": ["党参", "白术", "茯苓", "陈皮"],          # 跟叶天士 3/5 重合
        "zhang_xichun": ["黄芪", "山药", "党参"],                # 跟两位温病医家只共 1 味
    }
    fake_llm = FakeLLM({info["name"]: _s3_with_herbs(pid, herbs[pid]) for pid, info in REAL_PHYSICIANS.items()})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "PHYSICIANS", REAL_PHYSICIANS)  # 覆盖 autouse 的两位钉死
    cases = [
        CaseRecord(case_id=f"{pid}-001", case_group_id=f"{pid}-001", physician=pid, raw="原文",
                   symptoms=["纳差"], syndrome="脾胃气虚", herbs=["党参"])
        for pid in REAL_PHYSICIANS
    ]
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))

    div = chain.consult("纳差乏力")["divergence"]

    assert div["method"] == "nway_jaccard+pairwise"
    by_pair = {(p["a"], p["b"]): p for p in div["pairs"]}
    assert set(by_pair) == {("ye_tianshi", "wu_jutong"), ("ye_tianshi", "zhang_xichun"),
                            ("wu_jutong", "zhang_xichun")}
    ye_wu = by_pair[("ye_tianshi", "wu_jutong")]
    assert ye_wu["group"] == "lineage"
    assert ye_wu["herb_jaccard"] == round(1 - 3 / 5, 3)
    assert ye_wu["shared_herbs"] == ["党参", "白术", "茯苓"]
    assert ye_wu["name_a"] == "叶天士" and ye_wu["name_b"] == "吴鞠通"
    assert ye_wu["year_gap"] == 1758 - 1667
    ye_zhang = by_pair[("ye_tianshi", "zhang_xichun")]
    assert ye_zhang["group"] == "cross_school"
    assert ye_zhang["herb_jaccard"] == round(1 - 1 / 6, 3)
    assert ye_zhang["year_gap"] == 1860 - 1667
    wu_zhang = by_pair[("wu_jutong", "zhang_xichun")]
    assert wu_zhang["group"] == "cross_school"
    assert wu_zhang["year_gap"] == 1860 - 1758
    # 汇总：师承内 1 对、跨学派 2 对，均值各算各的，判据报出
    assert div["n_lineage_pairs"] == 1 and div["n_cross_school_pairs"] == 2
    assert div["lineage_mean"] == round(1 - 3 / 5, 3)
    assert div["cross_school_mean"] == round((round(1 - 1 / 6, 3) + round(1 - 1 / 6, 3)) / 2, 3)
    assert div["cross_school_gt_lineage"] is True
    # 三家交并比仍然保留（eval/run_eval.py 和前端主行还在读它），且确实比两两的都偏高
    assert div["herb_jaccard"] == round(1 - 1 / 7, 3)
    assert all(div["herb_jaccard"] >= p["herb_jaccard"] for p in div["pairs"])


def test_pairwise_divergence_two_physicians_has_one_pair_and_no_cross_school_means(monkeypatch):
    """两位医家（autouse 钉住的叶/吴，同学派）：只有一对、group=lineage，跨学派
    均值和判据都是 None（不适用），不是 0 或 False。"""
    s3_ye = _s3_with_herbs("ye_tianshi", ["党参", "白术"])
    s3_wu = _s3_with_herbs("wu_jutong", ["党参", "陈皮"])
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    div = chain.consult("纳差乏力")["divergence"]
    assert len(div["pairs"]) == 1
    assert div["pairs"][0]["group"] == "lineage"
    assert div["lineage_mean"] == round(1 - 1 / 3, 3)
    assert div["cross_school_mean"] is None
    assert div["cross_school_gt_lineage"] is None


def test_pairwise_divergence_pure_function_edge_cases(monkeypatch):
    """纯函数直接调：两边都没开药 → jaccard None（不是 0）；医家不在注册表
    → group=unknown、year_gap None；治法是否相同逐对报。"""
    from types import SimpleNamespace

    monkeypatch.setattr(chain, "PHYSICIANS", {
        "ye_tianshi": {"name": "叶天士", "school": "温病", "years": "1667-1746"},
        "wu_jutong": {"name": "吴鞠通", "school": "温病", "years": "1758-1836"},
    })
    results = [
        {"physician": "ye_tianshi", "s3": SimpleNamespace(herbs=[], treatment_principle="健脾")},
        {"physician": "wu_jutong", "s3": SimpleNamespace(herbs=[], treatment_principle="清热")},
        {"physician": "someone_else", "s3": SimpleNamespace(herbs=["党参"], treatment_principle="健脾")},
    ]
    out = chain.pairwise_divergence(results)
    by_pair = {(p["a"], p["b"]): p for p in out["pairs"]}
    assert by_pair[("ye_tianshi", "wu_jutong")]["herb_jaccard"] is None
    assert by_pair[("ye_tianshi", "wu_jutong")]["treatment_principle_same"] is False
    assert by_pair[("ye_tianshi", "someone_else")]["group"] == "unknown"
    assert by_pair[("ye_tianshi", "someone_else")]["year_gap"] is None
    assert by_pair[("ye_tianshi", "someone_else")]["herb_jaccard"] == 1.0  # 一边空一边有：毫无重叠
    assert by_pair[("ye_tianshi", "someone_else")]["treatment_principle_same"] is True
    assert out["lineage_mean"] is None  # 唯一的师承内对没有可比的数
    # R14 新增 pairs_mean（全部配对的均值 = 对照带右端那个"三家平均差异"）。
    # 空输入下是 None 而不是 0，跟这个函数其余字段一个口径：0 会被读成
    # "配对之间毫无差异"，而实际是"没有可比的对"。
    assert chain.pairwise_divergence([]) == {
        "pairs": [], "pairs_mean": None, "lineage_mean": None, "cross_school_mean": None,
        "n_lineage_pairs": 0, "n_cross_school_pairs": 0, "cross_school_gt_lineage": None,
    }


def test_birth_year_parses_registry_format_and_rejects_garbage():
    assert chain._birth_year("1667-1746") == 1667
    assert chain._birth_year(" 1860-1933 ") == 1860
    assert chain._birth_year(None) is None
    assert chain._birth_year("清代") is None


# ---------- registry 增长回归：钉两位是权宜之计，不是终局 ----------

def test_consult_runs_every_registered_physician_not_just_the_pinned_two(monkeypatch):
    """_pin_two_physicians 这个 autouse fixture 让本文件其余测试不受 registry
    增长影响，但代价是全项目没有一条测试真的对着"三位、四位医家"的真实注册表
    跑过 consult()——只靠 pin 意味着"注册第三位医家后 chain.py 还正常"这件事
    没有任何测试守着。这条故意用 monkeypatch 覆盖掉 autouse 的钉法，直接对着
    core.physicians.PHYSICIANS 的真实内容跑，期望值也从它动态算，不写死
    "叶天士/吴鞠通"这两个名字——这样 registry 涨到几位，这条测试都还在真的
    验证 consult() 会不会漏跑或多跑某个医家，而不是永远只测两位那条老路径。"""
    from core.physicians import physicians_enabled as _enabled

    REAL_PHYSICIANS = _enabled()

    assert len(REAL_PHYSICIANS) >= 2  # 这条测试的意义建立在"确实不止一位医家"上

    s3_by_physician = {
        info["name"]: S3Syndrome(
            syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
            cited_case_ids=[f"{pid}-001"], herbs=["党参"],
        )
        for pid, info in REAL_PHYSICIANS.items()
    }
    fake_llm = FakeLLM(s3_by_physician)
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    # 覆盖掉 autouse 的两位钉死——这条测试就是要对着真实注册表跑
    monkeypatch.setattr(chain, "PHYSICIANS", REAL_PHYSICIANS)
    cases = [
        CaseRecord(
            case_id=f"{pid}-001", case_group_id=f"{pid}-001", physician=pid,
            raw="原文", symptoms=["纳差"], tongue="淡红", pulse="细弱",
            syndrome="脾胃气虚", herbs=["党参"],
        )
        for pid in REAL_PHYSICIANS
    ]
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))

    outcome = chain.consult("纳差乏力")

    # 每位注册医家都跑到了，顺序跟注册表一致，不多不少
    assert [r["physician"] for r in outcome["results"]] == list(REAL_PHYSICIANS.keys())
    assert len(outcome["results"]) == len(REAL_PHYSICIANS)
    for r in outcome["results"]:
        assert r["s3"].syndrome == "脾胃气虚"
        assert r["no_reference_cases"] is False
