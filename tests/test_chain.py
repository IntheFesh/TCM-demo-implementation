"""core/chain.py 的离线测试：用假 LLM 后端和假检索器，不需要网络。"""
from core import chain
from core.retrieval import Retriever
from core.schemas import CaseRecord, ElementHit, S1Normalize, S2Elements, S3Syndrome


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
