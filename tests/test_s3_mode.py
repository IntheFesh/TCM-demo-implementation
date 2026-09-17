"""R33 `S3_MODE`：两档的分派、单元素 results、折算系数、五位医家参与综合。

**这个文件是"演示跑的那个配置有没有被测过"的答案。** R32 的教训是知识块只在
一种模式下进提示词而演示跑另一种，整套测试全绿。所以这里的每一条都显式
`S3_MODE=structured`（产品默认），走真的结构化路径，断言的是**发给 LLM 的
system 出自 s3_structured.yaml**、**results 恰好一个元素**这些能被看见的事实。

`tests/conftest.py` 的全局夹具把 `S3_MODE` 钉成 `legacy`（那批为 legacy 形状写的
测试继续测它们本来要测的东西）。这里每条都自己 setenv 覆盖掉那个钉子。
"""
from __future__ import annotations

import pytest

from core import chain
from core.llm import S3_MODE_DEFAULT, S3_MODES, s3_mode
from core.physicians import (
    PHYSICIANS,
    physicians_enabled,
    physicians_for_mode,
    physicians_for_synthesis,
)
from core.schemas import (
    CaseRecord,
    ElementHit,
    S1Normalize,
    S2Elements,
    S3Structured,
    S3StructuredUnreferenced,
)
from core.usage import CALLS_PER_CONSULT_FIXED_STEPS, calls_per_consult

from tests.test_chain import FakeLLM, FakeRetriever


# ---------- 假后端：按 schema 返回 S3Structured ----------

def _structured_payload(*, case_ids: list[str], herbs: list[str]) -> dict:
    return {
        "organs": [{"organ": "脾", "supporting_symptoms": ["纳差"],
                    "pathogenesis": "脾失健运"}],
        "syndrome": {"name": "脾胃气虚证", "disease": "痞满", "from_organs": ["脾"],
                     "reasoning": "纳差乏力，脾失健运",
                     "reasoning_plain": "消化功能弱了"},
        "method": {"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                   "targets": ["脾失健运"]},
        "formula": {"from_method": "健脾益气",
                    "candidate": {"name": "四君子汤", "source": "classic",
                                  "confidence": "high", "rationale": "对应脾胃气虚",
                                  "herb_items": [{"name": h, "dose": 9.0, "role": "君"}
                                                 for h in herbs]}},
        "herb_choices": [{"item": {"name": h, "dose": 9.0, "role": "君"},
                          "for_element": "脾", "effect_cited": "补中益气"}
                         for h in herbs],
        "physician_influences": [{"physician": "ye_tianshi", "step": "formula",
                                  "contribution": "用药轻灵",
                                  "cited_case_ids": case_ids[:1]}],
        "cited_case_ids": case_ids,
    }


class StructuredFakeLLM(FakeLLM):
    """S1/S2 沿用 `FakeLLM`，S3 这一步按传进来的 schema 返回结构化产出。

    继承而不是另写一个：`FakeLLM` 已经实现了 manifest 要问的那几个方法
    （model_name / backend_id / lora_for / comparability_warning），
    另写一份必然漏一个，而漏了的表现是 `_build_manifest` AttributeError。
    """

    def __init__(self, *args, case_ids=None, herbs=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.case_ids = case_ids if case_ids is not None else ["ye_tianshi-001"]
        self.herbs = herbs if herbs is not None else ["党参"]
        self.s3_systems: list[str] = []
        self.s3_schemas: list[type] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        if schema in (S3Structured, S3StructuredUnreferenced):
            self.calls.append(schema.__name__)
            self.s3_systems.append(system)
            self.s3_schemas.append(schema)
            d = _structured_payload(case_ids=self.case_ids, herbs=self.herbs)
            if schema is S3StructuredUnreferenced:
                del d["cited_case_ids"], d["physician_influences"]
            return schema(**d)
        return super().generate(system, user, schema, temperature=temperature, **kwargs)


def _case(pid: str, n: int = 1) -> CaseRecord:
    return CaseRecord(
        case_id=f"{pid}-{n:03d}", case_group_id=f"{pid}-{n:03d}", physician=pid,
        raw="原文", visit_index=0, raw_excerpt="纳谷不香，脘腹痞满。",
        symptoms=["纳差"], tongue="淡红", pulse="细弱",
        syndrome="脾胃气虚", herbs=["党参", "白术"],
    )


@pytest.fixture
def s1s2():
    s1 = S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(elements=[ElementHit(element="脾", kind="location",
                                         supporting_symptoms=["纳差"],
                                         confidence="high")],
                    unexplained_symptoms=[])
    return s1, s2


@pytest.fixture
def structured(monkeypatch):
    """钉成 structured + best_of_n=1，装好五家医案的假检索器与结构化假后端。"""
    monkeypatch.setenv("S3_MODE", "structured")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    cases = [_case(pid) for pid in physicians_for_synthesis(PHYSICIANS)]
    llm = StructuredFakeLLM({}, case_ids=[c.case_id for c in cases])
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
    return llm, cases


# ---------- 模式本身 ----------

def test_the_product_default_is_structured(monkeypatch):
    """**这一条是那个钉子的对立面。** conftest 把 `S3_MODE` 钉成 legacy 好让
    上百条老测试继续测 legacy；这里把环境变量清掉，断言产品默认是 structured
    ——用户的要求是「五位医家进行综合分析，不要给出多个答案」。"""
    monkeypatch.delenv("S3_MODE", raising=False)
    assert s3_mode() == "structured"
    assert S3_MODE_DEFAULT == "structured"
    assert S3_MODES == ("structured", "legacy")


def test_the_mode_is_case_insensitive(monkeypatch):
    for raw in ("LEGACY", " legacy ", "Legacy"):
        monkeypatch.setenv("S3_MODE", raw)
        assert s3_mode() == "legacy"


def test_a_typo_raises_instead_of_silently_using_the_default(monkeypatch):
    """`S3_THINKING` / `S3_REASONING_EFFORT` 拼错只打一句 stderr 走默认，
    因为它们错了只影响成本与质量；这一个错了下游拿到的是**另一种 schema**，
    悄悄回到 legacy 的表现是「怎么又出了五份答案」，而那时人会去找前端的 bug。"""
    monkeypatch.setenv("S3_MODE", "structred")
    with pytest.raises(ValueError) as e:
        s3_mode()
    assert "structred" in str(e.value)
    for m in S3_MODES:
        assert m in str(e.value), "报错要列出可用值"


def test_consult_rejects_an_unknown_override_before_spending_any_call(monkeypatch):
    """跟 retriever_mode / refs_mode 一样立刻抛——不然要等到 S3 那一步才失败，
    S1/S2 两次调用已经白花了。"""
    llm = FakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    with pytest.raises(ValueError) as e:
        chain.consult("胃脘胀痛", s3_mode_override="structred")
    assert "s3_mode" in str(e.value)
    assert llm.calls == [], "一次调用都不该发生"


# ---------- 名单：五位 vs 三位 ----------

def test_all_five_physicians_take_part_in_the_synthesis():
    """用户原话：五位医家（叶天士、吴鞠通、张锡纯、李可、王云启）进行综合分析。"""
    roster = physicians_for_synthesis()
    assert list(roster) == ["ye_tianshi", "wu_jutong", "zhang_xichun",
                            "li_ke", "wang_yunqi"]
    assert all(info.get("in_synthesis") for info in PHYSICIANS.values())


def test_enabled_was_not_flipped_and_the_reason_is_documented():
    """`enabled` 回答的是"谁算三列集注的一员"，`in_synthesis` 回答"谁参与综合分析"
    ——两个不同的问题，一个字段答两个正是第 31 条要防的形状。

    翻 `enabled` 的代价是量过的：李可与王云启的 `school` 都是 None，五位两两配对
    共 10 对，其中 7 对（70%）的学派判定会变成 unknown——而 λ2 与「跨学派分歧大于
    师承内」这条对照正是 §0.6 要保留的东西。
    """
    import itertools

    assert list(physicians_enabled()) == ["ye_tianshi", "wu_jutong", "zhang_xichun"]
    assert PHYSICIANS["li_ke"]["enabled"] is False
    assert PHYSICIANS["wang_yunqi"]["enabled"] is False

    doc = physicians_for_synthesis.__doc__ or ""
    assert "第 31 条" in doc and "70%" in doc

    # 把那个代价当场算一遍，别让文档里的数字变成一句没人核过的话
    five = {pid: info["school"] for pid, info in physicians_for_synthesis().items()}
    pairs = list(itertools.combinations(sorted(five), 2))
    unknown = [p for p in pairs if five[p[0]] is None or five[p[1]] is None]
    assert (len(pairs), len(unknown)) == (10, 7)


def test_physicians_for_mode_is_the_single_dispatch_point():
    assert list(physicians_for_mode("structured")) == list(physicians_for_synthesis())
    assert list(physicians_for_mode("legacy")) == list(physicians_enabled())
    with pytest.raises(ValueError):
        physicians_for_mode("structred")


def test_a_new_physician_joins_the_synthesis_by_default():
    """默认 True：新注册一位医家自动参与综合分析，要排除得显式写 False。"""
    reg = {**PHYSICIANS, "新医家": {"name": "新", "school": None, "enabled": False}}
    assert "新医家" in physicians_for_synthesis(reg)


# ---------- 折算系数 ----------

def test_calls_per_consult_drops_the_physician_factor_in_structured_mode():
    """五位 × N=3 是 15 次 S3，融合成一次之后是 3 次。这是这一轮最大的一笔省。"""
    assert calls_per_consult(5, 3, "structured") == CALLS_PER_CONSULT_FIXED_STEPS + 3
    assert calls_per_consult(5, 3, "legacy") == CALLS_PER_CONSULT_FIXED_STEPS + 15
    # 医家数在 structured 下不进公式——传几位都一样
    assert calls_per_consult(1, 3, "structured") == calls_per_consult(99, 3, "structured")


def test_calls_per_consult_reads_the_roster_per_mode(monkeypatch):
    """默认名单**按模式取**：structured 下是五位、legacy 下是三位。
    写死任一个数都会在另一种模式下虚报。"""
    monkeypatch.setenv("S3_BEST_OF_N", "3")
    monkeypatch.setenv("S3_MODE", "legacy")
    assert calls_per_consult() == CALLS_PER_CONSULT_FIXED_STEPS + 3 * 3
    monkeypatch.setenv("S3_MODE", "structured")
    assert calls_per_consult() == CALLS_PER_CONSULT_FIXED_STEPS + 3


def test_calls_per_consult_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        calls_per_consult(3, 1, "structred")


# ---------- 端到端：真的跑一次 consult ----------

def test_structured_consult_returns_exactly_one_result(structured, monkeypatch):
    llm, cases = structured
    out = chain.consult("胃脘胀痛，纳差乏力", retriever_mode="hybrid")
    assert len(out["results"]) == 1, "五家融合只出一份答案"
    r = out["results"][0]
    assert r["physician"] == chain.SYNTHESIS_PHYSICIAN_ID == "synthesis"
    assert r["physician_name"] == "五家综合"
    assert r["s3"].syndrome == "脾胃气虚证"
    assert len(r["s3"].formula_candidates) == 1


def test_the_synthesis_id_is_not_a_real_physician():
    """用保留 id 而不是随便挑一位医家：挑一位的话「这份结论是叶天士给的」
    就是假的，而前端会照着把它显示成叶天士的方。"""
    assert chain.SYNTHESIS_PHYSICIAN_ID not in PHYSICIANS
    from core.physicians import resolve_physician_id

    assert resolve_physician_id(chain.SYNTHESIS_PHYSICIAN_ID) is None


def test_the_prompt_sent_to_the_llm_comes_from_s3_structured_yaml(structured):
    """R32 的教训：断言**发给 LLM 的那段字符串**，不是断言生成它的函数的返回值。"""
    llm, _cases = structured
    chain.consult("胃脘胀痛，纳差乏力", retriever_mode="hybrid")
    assert len(llm.s3_systems) == 1, "S3 只调一次，不是每位医家一次"
    system = llm.s3_systems[0]
    assert "五步推理链，一步都不许跳" in system
    assert "ye_tianshi(叶天士)" in system, "$physician_ids 真的填进去了"
    assert "叶天士、吴鞠通、张锡纯、李可、王云启" in system, "$physicians 是五位"
    assert llm.s3_schemas == [S3Structured]


def test_all_five_corpora_are_retrieved_not_just_the_three_enabled(structured):
    """检索是五家各自 top-3 拼接，不是只查参与集注的三位。"""
    llm, cases = structured
    out = chain.consult("胃脘胀痛，纳差乏力", retriever_mode="hybrid")
    got = {r["case_id"].rsplit("-", 1)[0] for r in out["results"][0]["refs"]}
    assert got == {f"{pid}-001".rsplit("-", 1)[0]
                   for pid in physicians_for_synthesis(PHYSICIANS)}
    assert len(out["results"][0]["refs"]) == 5


def test_the_manifest_records_the_mode_and_the_synthesis_summary(structured):
    llm, cases = structured
    m = chain.consult("胃脘胀痛，纳差乏力", retriever_mode="hybrid")["manifest"]
    assert m["s3_mode"] == "structured"
    syn = m["synthesis"]
    assert syn["physicians_available"] == 5
    assert syn["physicians_cited"] == ["ye_tianshi"]
    assert syn["n_physicians_cited"] == 1
    assert syn["chain_steps"] == list(chain.S3_CHAIN_STEPS)
    assert syn["herbs_grounded_ratio"] == 0.0, "沙盒里没有本体可引"


def test_the_synthesis_summary_reports_both_available_and_cited(structured):
    """只报"我们接了五家"就是拿接入数冒充生效数。分母与分子都要有
    ——CLAUDE.md「任何数字都必须带对照」。"""
    llm, cases = structured
    syn = chain.consult("胃脘胀痛", retriever_mode="hybrid")["manifest"]["synthesis"]
    assert syn["physicians_available"] == 5 and syn["n_physicians_cited"] == 1
    assert syn["n_physicians_cited"] < syn["physicians_available"], (
        "这次只有一家真的影响了结论——这正是要被看见的那种差距"
    )


def test_legacy_mode_still_produces_three_results(monkeypatch, s1s2):
    """§0.6 保留：legacy 一个字没变。"""
    from core.schemas import S3Syndrome

    monkeypatch.setenv("S3_MODE", "legacy")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    cases = [_case(pid) for pid in physicians_enabled(PHYSICIANS)]
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=[cases[0].case_id])
    llm = FakeLLM({info["name"]: s3 for info in physicians_enabled(PHYSICIANS).values()})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
    out = chain.consult("胃脘胀痛，纳差乏力", retriever_mode="hybrid")
    assert len(out["results"]) == 3
    assert out["manifest"]["s3_mode"] == "legacy"
    assert out["manifest"]["synthesis"] is None, (
        "legacy 下是 None 不是空字典——「这个模式没跑」和「跑了但一家都没引」是两件事"
    )


def test_the_override_beats_the_env_var(structured, monkeypatch):
    """`s3_mode_override` 是逐请求的，不读进程状态——两个并发请求各选一种模式
    不会互相污染（跟 retriever_mode 同一条纪律）。"""
    monkeypatch.setenv("S3_MODE", "structured")
    from core.schemas import S3Syndrome

    cases = [_case(pid) for pid in physicians_enabled(PHYSICIANS)]
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=[cases[0].case_id])
    llm = FakeLLM({info["name"]: s3 for info in physicians_enabled(PHYSICIANS).values()})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
    out = chain.consult("胃脘胀痛", retriever_mode="hybrid", s3_mode_override="legacy")
    assert len(out["results"]) == 3
    assert out["manifest"]["s3_mode"] == "legacy"


def test_empty_retrieval_switches_to_the_unreferenced_schema(structured, monkeypatch):
    """一条医案都检索不到时换用不含引用字段的 schema——不是放松 min_length=1。"""
    llm, _cases = structured
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever([]))
    out = chain.consult("胃脘胀痛", retriever_mode="hybrid")
    r = out["results"][0]
    assert llm.s3_schemas == [S3StructuredUnreferenced]
    assert r["no_reference_cases"] is True
    assert r["s3"].cited_case_ids == []
    assert r["physician_influences"] == []
    assert out["manifest"]["synthesis"]["n_physicians_cited"] == 0


def test_a_fabricated_case_id_is_reported_as_hallucinated(monkeypatch, structured):
    llm, cases = structured
    llm.case_ids = ["ye_tianshi-001", "编造的-999"]
    out = chain.consult("胃脘胀痛", retriever_mode="hybrid")
    assert "编造的-999" in out["results"][0]["hallucinated"]


def test_an_influence_citing_a_fabricated_id_is_also_caught(monkeypatch, structured):
    """schema 的校验只保证 influence 引的 id 在 `cited_case_ids` 里，
    而 `cited_case_ids` 本身可能是编的——两道检查管不同的事，都要有。"""
    llm, cases = structured
    llm.case_ids = ["编造的-888"]
    out = chain.consult("胃脘胀痛", retriever_mode="hybrid")
    assert out["results"][0]["hallucinated"] == ["编造的-888"]


def test_the_structured_result_carries_the_chain_original(structured):
    """`s3_structured` 是新增字段，不是 `s3` 的替代——R34 验证器与 R37 单链前端读它。"""
    llm, cases = structured
    r = chain.consult("胃脘胀痛", retriever_mode="hybrid")["results"][0]
    assert isinstance(r["s3_structured"], S3Structured)
    assert r["s3_structured"].organs[0].organ == "脾"
    assert r["physician_influences"][0]["physician"] == "ye_tianshi"
    assert r["n_ontology_refs"] == 0
    # `s3` 仍然是下游认识的形状
    from core.schemas import S3Syndrome as _S3

    assert isinstance(r["s3"], _S3)


def test_the_result_dict_has_the_same_keys_as_the_legacy_path(structured, monkeypatch):
    """契约：前端、eval 收集器、分歧度按这些键读。structured 只多两个键、不少键。"""
    llm, cases = structured
    got = set(chain.consult("胃脘胀痛", retriever_mode="hybrid")["results"][0])

    from core.schemas import S3Syndrome

    monkeypatch.setenv("S3_MODE", "legacy")
    lcases = [_case(pid) for pid in physicians_enabled(PHYSICIANS)]
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=[lcases[0].case_id])
    llm2 = FakeLLM({i["name"]: s3 for i in physicians_enabled(PHYSICIANS).values()})
    monkeypatch.setattr(chain, "get_llm", lambda: llm2)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(lcases))
    legacy = set(chain.consult("胃脘胀痛", retriever_mode="hybrid")["results"][0])

    assert legacy - got == set(), f"structured 少了这些键：{legacy - got}"
    assert got - legacy == {"s3_structured", "physician_influences",
                            "physicians_cited", "herbs_grounded_ratio",
                            "n_ontology_refs",
                            # R34 加的两个：符号验证的最终结论 + 三指标
                            "verification", "verifier_metrics"}


def test_stream_events_still_route_by_physician_field(structured):
    """事件名不变、`physician` 字段是保留 id——前端按这个字段路由（DESIGN §4.7），
    换成别的事件名等于让 R37 之前的界面收不到任何进度。"""
    llm, cases = structured
    seen: list[tuple[str, dict]] = []
    chain.consult("胃脘胀痛", retriever_mode="hybrid",
                  on_step=lambda n, d: seen.append((n, d)))
    names = [n for n, _ in seen]
    assert "physician_start" in names and "physician_done" in names
    starts = [d for n, d in seen if n == "physician_start"]
    assert len(starts) == 1 and starts[0]["physician"] == "synthesis"
    done = [d for n, d in seen if n == "physician_done"][0]
    assert done["syndrome"] == "脾胃气虚证" and done["herbs"] == ["党参"]
