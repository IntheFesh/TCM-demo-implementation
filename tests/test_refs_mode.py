"""E3/E4 消融要用的 refs_mode（own/swapped/none）离线测试。

跟 tests/test_retriever_mode.py 是同一类问题的姊妹模块——那边测的是"检索
用哪种算法"，这边测的是"检索谁的医案库"，两个开关相互独立、不该有前一个
的测试漏了后一个。复用同一个 RecordingRetriever，不再另写一个假实现。
"""
import pytest

from core import chain
from core.schemas import S3Syndrome
from tests.test_chain import FakeLLM, _fake_cases
from tests.test_retriever_mode import RecordingRetriever


def _setup(monkeypatch, physicians=("ye_tianshi", "wu_jutong")):
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in physicians})
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=["ye_tianshi-001"], herbs=["党参"])
    fake_llm = FakeLLM({name: s3 for name in [REG[p]["name"] for p in physicians]})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    r = RecordingRetriever(_fake_cases())
    monkeypatch.setattr(chain, "get_retriever", lambda: r)
    return r, fake_llm


# ---------- own（默认，改造前的行为）----------


def test_own_mode_is_the_default_and_searches_each_physicians_own_corpus(monkeypatch):
    r, _ = _setup(monkeypatch)
    chain.consult("纳差乏力")
    assert sorted(c["physician"] for c in r.calls) == ["wu_jutong", "ye_tianshi"]


def test_own_mode_explicit_matches_default(monkeypatch):
    r, _ = _setup(monkeypatch)
    chain.consult("纳差乏力", refs_mode="own")
    assert sorted(c["physician"] for c in r.calls) == ["wu_jutong", "ye_tianshi"]


# ---------- swapped ----------


def test_swapped_mode_searches_the_other_physicians_corpus(monkeypatch):
    """两位医家时 swapped 就是"对方"：叶天士的检索请求应该打到吴鞠通的医案库，
    反过来也一样——这正是 E3 要制造的反事实条件。"""
    r, _ = _setup(monkeypatch)
    chain.consult("纳差乏力", refs_mode="swapped")
    called_physicians = {c["physician"] for c in r.calls}
    # 两次调用打到的都是"对方"的库，而不是自己的库
    assert called_physicians == {"wu_jutong", "ye_tianshi"}
    assert len(r.calls) == 2


def test_swap_physician_id_cycles_through_registry_order(monkeypatch):
    """三位（及以上）医家时是真正的环，不是简单的"对方"。用一个假的三人登记表
    直接测 _swap_physician_id，不依赖真的张锡纯数据落地——PHYSICIANS 扩到
    三位后这个函数不用改一行代码就该自动正确。"""
    monkeypatch.setattr(chain, "PHYSICIANS", {"a": {}, "b": {}, "c": {}})
    assert chain._swap_physician_id("a") == "b"
    assert chain._swap_physician_id("b") == "c"
    assert chain._swap_physician_id("c") == "a"


# ---------- none ----------


def test_none_mode_skips_search_entirely(monkeypatch):
    r, _ = _setup(monkeypatch)
    outcome = chain.consult("纳差乏力", refs_mode="none")
    assert r.calls == [], "refs_mode=none 不该发起任何检索调用"
    for pr in outcome["results"]:
        assert pr["no_reference_cases"] is True
        assert pr["refs"] == []
        assert pr["s3"].cited_case_ids == []


# ---------- refs_mode 回填进结果，供 E3/E4 收集代码配对用 ----------


def test_refs_mode_is_recorded_on_each_physician_result(monkeypatch):
    _setup(monkeypatch)
    outcome = chain.consult("纳差乏力", refs_mode="swapped")
    for pr in outcome["results"]:
        assert pr["refs_mode"] == "swapped"


# ---------- 非法值 ----------


def test_unknown_refs_mode_fails_fast_before_any_llm_call(monkeypatch):
    r, fake_llm = _setup(monkeypatch)
    with pytest.raises(ValueError, match="未知的 refs_mode"):
        chain.consult("纳差乏力", refs_mode="没有这个模式")
    assert fake_llm.calls == [], "校验必须发生在任何 LLM 调用之前"
    assert r.calls == []


def test_run_physician_itself_also_validates_refs_mode(monkeypatch):
    """run_physician 可以被单独调用（比如 eval 脚本直接拼装），不能假设永远
    经过 consult() 那道校验。"""
    from core.schemas import S1Normalize, S2Elements

    _setup(monkeypatch)
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    with pytest.raises(ValueError, match="未知的 refs_mode"):
        chain.run_physician(s1, s2, "ye_tianshi", "叶天士", refs_mode="没有这个模式")
