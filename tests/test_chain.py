"""core/chain.py 的离线测试：用假 LLM 后端和假检索器，不需要网络。"""
import json
from core import chain
from core.retrieval import Retriever
from core.schemas import CaseRecord, ElementHit, S1Normalize, S2Elements, S3Syndrome


import pytest


@pytest.fixture(autouse=True)
def _pin_two_physicians(monkeypatch):
    """这里的期望值（llm_calls == 4、S3 跑 2 次、FakeLLM 按「叶天士/吴鞠通」分发）都
    写死在两位医家上。注册张锡纯之后（HANDOFF 步骤 3）registry 变成三位，这些测试
    会整批红——那不是 bug，是测试写死了医家数。钉住两位，让 registry 增长与测试解耦。"""
    from core.physicians import PHYSICIANS as REG

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
    # 假后端要跟着实现这三个方法，否则 _build_manifest 会 AttributeError。
    def model_name(self) -> str:
        return "fake-model"

    def backend_id(self) -> str:
        return "fake"

    def comparability_warning(self) -> str | None:
        return "后端：fake（离线测试用），不产生任何可用于报告的数字。"

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
        if schema is S3Syndrome:
            # 依赖 system 提示词里包含医家姓名来区分两位医家的返回值
            for physician_name, s3 in self.s3_by_physician.items():
                if physician_name in system:
                    return s3
            raise AssertionError("无法从 system 提示词判断当前医家")
        raise AssertionError(f"未预期的 schema: {schema}")


class FakeRetriever(Retriever):
    def __init__(self, cases: list[CaseRecord]):
        self.cases = cases

    def search(self, query, physician, k=3, min_score=0.0):
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
    assert outcome["divergence"]["method"] == "exact_string_match"


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
    """第一次 S3 开出含配伍禁忌的方，收到带【配伍禁忌】的重开提示后改开干净方。
    用来验证重开真的被触发，而不是只把违规标出来就算完。"""

    def __init__(self, s3_by_physician, clean_by_physician):
        super().__init__(s3_by_physician)
        self.clean_by_physician = clean_by_physician
        self.retry_prompts: list[str] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        if schema is S3Syndrome and "【配伍禁忌】" in system:
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


def test_incompatible_formula_triggers_one_regeneration(monkeypatch):
    """S3 开出甘草+海藻（十八反）→ 把冲突写进 prompt 重开一次 → 重开后干净。"""
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
        self._react_step = 0

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        from core.schemas import ReActStep

        if schema is ReActStep:
            self.calls.append("ReActStep")
            self._react_step += 1
            if self._react_step % 2 == 1:
                return ReActStep(thought="先看看证素对应哪些证候",
                                 action="query_graph", action_input={"node": "纳呆"})
            return ReActStep(thought="够了", action="finish")
        if schema is S3Syndrome:
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


def test_react_off_by_default_leaves_s3_prompt_untouched(monkeypatch):
    """不开 ReAct 时 S3 的提示词里不能多出任何东西——多一个字，
    use_react 的 A/B 就混进了 prompt 变化这个额外变量。"""
    fake_llm = _react_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", use_react=False)

    assert fake_llm.calls.count("ReActStep") == 0
    assert all(r["react_trace"] is None for r in outcome["results"])
    assert all("取证过程" not in s for s in fake_llm.s3_systems)
    assert outcome["manifest"]["use_react"] is False
    assert outcome["manifest"]["llm_calls"] == 4  # S1 + S2 + S3×2


def test_react_on_appends_evidence_and_counts_its_calls(monkeypatch):
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
    fake_llm = _react_setup(monkeypatch)
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
    fake_llm = _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力")
    assert outcome["followup"].stopped_by == "no_answer"
    assert outcome["manifest"]["llm_calls"] == 4
    assert all("追问结果" not in s for s in fake_llm.s3_systems)


def test_followup_costs_one_extra_s2_no_matter_how_many_rounds(monkeypatch):
    """追问每轮 0 次 LLM 调用（规则解析 + 图上贝叶斯更新），只在问出了新症状之后
    整体重跑一次 S2。按轮收费的话这个 demo 就没法用了（G2 实测每次调用 4-8s）。

    这里的 S2 一共 3 次：初次、追问后并入新症状那次、以及残差辨证那次
    （残差是既有行为，跟追问无关——它被触发是因为追问加进来的症状本身
    没被 FakeLLM 的证素解释）。轮数变多时这个数不许跟着涨。
    """
    fake_llm = _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有")
    assert outcome["followup"].rounds >= 2
    assert fake_llm.calls.count("S2Elements") == 3
    assert fake_llm.calls.count("S1Normalize") == 1, "S1 仍然全局只跑一次"
    # 2(S1+S2) + 1(追问后的 S2) + 2(两位医家 S3) + 1(残差)
    assert outcome["manifest"]["llm_calls"] == 6


def test_asserted_symptoms_are_merged_into_the_symptom_list(monkeypatch):
    _followup_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有")
    for s in outcome["followup"].asserted:
        assert s in outcome["s1"].symptoms


def test_all_denials_do_not_rerun_s2(monkeypatch):
    """全是否定回答时没有新症状可并，不该白花一次 S2。"""
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


# ---------- 审查修复 ----------

from core.schemas import S3SyndromeUnreferenced


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
