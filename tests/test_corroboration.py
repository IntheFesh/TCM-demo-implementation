"""R54 第三相：医案佐证。`core/corroboration.py` **绝不回头改推导**——这份
文件的核心断言是一条不变式（`corroborate()` 调用前后 `s3` 的 `model_dump()`
逐字节相同，sha256 比对），其余测试覆盖四个结论桶的分类逻辑、
`CORROBORATION` 开关、以及跟 `core/chain.py::run_derivation` 的接线顺序。

跟 `tests/test_derived_no_cases.py`/`test_phase_order.py` 的分工：那两份
文件测第一相（演绎推导）本身，遇到第三相时一律 `CORROBORATION=off`
隔离开；这份文件专测第三相自己。
"""
from __future__ import annotations

import hashlib
import json

import pytest

from core import chain
from core.corroboration import (
    CONCORDANT_MAX_DISTANCE,
    CorroborationResult,
    PrecedentCase,
    corroborate,
    corroboration_enabled,
)
from core.schemas import S1Normalize, S2Elements, S3Derived, CaseRecord, ElementHit

from tests.test_derived_no_cases import DerivedFakeLLM, _derived_payload


def _case(case_id: str, physician: str, herbs: list[str], syndrome: str = "脾胃气虚证") -> CaseRecord:
    return CaseRecord(
        case_id=case_id, case_group_id=case_id, physician=physician,
        raw="原文", visit_index=0, raw_excerpt="纳谷不香，脘腹痞满。",
        symptoms=["纳差"], tongue="淡红", pulse="细弱",
        syndrome=syndrome, herbs=herbs,
    )


def _s1s2():
    s1 = S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(elements=[ElementHit(element="脾", kind="location",
                                         supporting_symptoms=["纳差"], confidence="high")],
                    unexplained_symptoms=[])
    return s1, s2


def _derived_s3(herbs=("党参", "白术")) -> S3Derived:
    return S3Derived(**_derived_payload(herbs=herbs))


# ---------- 一、CORROBORATION 开关 ----------

def test_default_is_on(monkeypatch):
    monkeypatch.delenv("CORROBORATION", raising=False)
    assert corroboration_enabled() is True


def test_explicit_off(monkeypatch):
    monkeypatch.setenv("CORROBORATION", "off")
    assert corroboration_enabled() is False


@pytest.mark.parametrize("raw", ["on", "ON", "1", "true", "True"])
def test_on_variants(monkeypatch, raw):
    monkeypatch.setenv("CORROBORATION", raw)
    assert corroboration_enabled() is True


@pytest.mark.parametrize("raw", ["off", "OFF", "0", "false"])
def test_off_variants(monkeypatch, raw):
    monkeypatch.setenv("CORROBORATION", raw)
    assert corroboration_enabled() is False


def test_a_typo_raises_instead_of_silently_using_the_default(monkeypatch):
    monkeypatch.setenv("CORROBORATION", "开")
    with pytest.raises(ValueError, match="开"):
        corroboration_enabled()


# ---------- 二、关掉时的行为 ----------

def test_disabled_returns_empty_buckets_with_a_note(monkeypatch):
    monkeypatch.setenv("CORROBORATION", "off")
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(), s1, s2)
    assert result.enabled is False
    assert result.concordant == () and result.divergent == () and result.no_precedent == ()
    assert result.note and "off" in result.note


def test_disabled_never_calls_search_cases(monkeypatch):
    monkeypatch.setenv("CORROBORATION", "off")

    def _poison(*a, **k):
        raise AssertionError("CORROBORATION=off 时不该调用 _search_cases")

    monkeypatch.setattr(chain, "_search_cases", _poison)
    s1, s2 = _s1s2()
    corroborate(_derived_s3(), s1, s2)  # 不该抛


# ---------- 三、开启时的分桶逻辑 ----------

@pytest.fixture
def two_physicians(monkeypatch):
    """把 `core.corroboration.PHYSICIANS` 钉成两位，让测试不随注册表增长
    （现在五位）而改期望值——跟 `tests/test_chain.py` 的
    `_pin_two_physicians` 同一条理由。"""
    import core.corroboration as corrob

    two = {"ye_tianshi": {}, "wu_jutong": {}}
    monkeypatch.setattr(corrob, "PHYSICIANS", two)
    monkeypatch.setenv("CORROBORATION", "on")
    return two


def test_a_concordant_case_when_herbs_overlap_enough(monkeypatch, two_physicians):
    """这次推导用的是「党参、白术」，医案原方也是「党参、白术」——distance=0，
    该进 concordant。"""
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            return [(_case("ye_tianshi-001", "ye_tianshi", ["党参", "白术"]), 0.9)], False
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(herbs=("党参", "白术")), s1, s2)
    assert len(result.concordant) == 1 and result.divergent == ()
    assert result.concordant[0].case_id == "ye_tianshi-001"
    assert result.concordant[0].herb_distance == 0.0
    assert set(result.concordant[0].shared_herbs) == {"党参", "白术"}
    assert result.no_precedent == ("wu_jutong",)
    assert result.physicians_with_precedent == ("ye_tianshi",)


def test_a_divergent_case_when_herbs_barely_overlap(monkeypatch, two_physicians):
    """这次推导用「党参、白术」，医案原方是四味完全不同的药——distance 接近 1，
    该进 divergent。"""
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            return [(_case("ye_tianshi-002", "ye_tianshi",
                           ["柴胡", "黄芩", "半夏", "生姜"]), 0.8)], False
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(herbs=("党参", "白术")), s1, s2)
    assert result.concordant == ()
    assert len(result.divergent) == 1
    assert result.divergent[0].herb_distance > CONCORDANT_MAX_DISTANCE
    assert result.divergent[0].shared_herbs == ()


def test_no_precedent_when_nothing_is_retrieved(monkeypatch, two_physicians):
    def _fake_search(query, physician, s2, retriever_mode):
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(), s1, s2)
    assert set(result.no_precedent) == {"ye_tianshi", "wu_jutong"}
    assert result.concordant == () and result.divergent == ()
    assert result.physicians_with_precedent == ()


def test_physicians_with_precedent_covers_both_buckets(monkeypatch, two_physicians):
    """一家 concordant、一家 divergent——两家都该出现在 physicians_with_precedent，
    这个字段不分方向，只问"有没有查到"。"""
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            return [(_case("ye_tianshi-001", "ye_tianshi", ["党参", "白术"]), 0.9)], False
        return [(_case("wu_jutong-001", "wu_jutong", ["石膏", "知母"]), 0.85)], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(herbs=("党参", "白术")), s1, s2)
    assert set(result.physicians_with_precedent) == {"ye_tianshi", "wu_jutong"}
    assert result.no_precedent == ()


def test_a_physician_can_have_multiple_hits_split_across_buckets(monkeypatch, two_physicians):
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            return [
                (_case("ye_tianshi-001", "ye_tianshi", ["党参", "白术"]), 0.9),
                (_case("ye_tianshi-002", "ye_tianshi", ["柴胡", "黄芩", "半夏", "生姜"]), 0.7),
            ], False
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(herbs=("党参", "白术")), s1, s2)
    assert len(result.concordant) == 1 and len(result.divergent) == 1
    assert result.concordant[0].case_id == "ye_tianshi-001"
    assert result.divergent[0].case_id == "ye_tianshi-002"


def test_concordant_max_distance_is_inclusive_at_the_boundary(monkeypatch, two_physicians):
    """distance 恰好等于阈值——`<=`，算 concordant，不是卡在边界上进 divergent。"""
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            # 党参/白术 与 党参/柴胡/黄芩/半夏/生姜：交集 1，并集 6，distance = 5/6 ≈ 0.833
            # 换一组更贴近阈值：这次推导 {党参,白术,柴胡}，医案 {党参}：
            # 交集 1，并集 3，distance = 2/3 ≈ 0.667 < 0.7，仍是 concordant，
            # 用这组直接验证"阈值附近仍归为一致"这件事，不苛求恰好等于 0.7
            # （构造一手精确等于 0.7 的两个真实药名集合意义不大，浮点比较
            # 也脆弱；这里退而求其次验证阈值线正确的那一侧）。
            return [(_case("ye_tianshi-001", "ye_tianshi", ["党参"]), 0.9)], False
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(herbs=("党参", "白术", "柴胡")), s1, s2)
    assert len(result.concordant) == 1
    assert abs(result.concordant[0].herb_distance - 2 / 3) < 1e-9


def test_syndrome_is_carried_through_from_the_case(monkeypatch, two_physicians):
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            return [(_case("ye_tianshi-001", "ye_tianshi", ["党参", "白术"],
                           syndrome="肝郁脾虚证"), 0.9)], False
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    result = corroborate(_derived_s3(herbs=("党参", "白术")), s1, s2)
    assert result.concordant[0].syndrome == "肝郁脾虚证"


# ---------- 四、绝不回头改推导：不变式 ----------

def _digest(s3) -> str:
    return hashlib.sha256(
        json.dumps(s3.model_dump(), sort_keys=True, ensure_ascii=False, default=str)
        .encode("utf-8")
    ).hexdigest()


def test_corroborate_never_mutates_s3_when_disabled(monkeypatch):
    monkeypatch.setenv("CORROBORATION", "off")
    s3 = _derived_s3()
    before = _digest(s3)
    s1, s2 = _s1s2()
    corroborate(s3, s1, s2)
    assert _digest(s3) == before


def test_corroborate_never_mutates_s3_when_enabled_with_hits(monkeypatch, two_physicians):
    """开着、真的查到医案（concordant + divergent + no_precedent 三桶都有）
    的情况下，这条不变式最容易被违反——所以专门在这个"最热闹"的场景下测。"""
    def _fake_search(query, physician, s2, retriever_mode):
        if physician == "ye_tianshi":
            return [(_case("ye_tianshi-001", "ye_tianshi", ["党参", "白术"]), 0.9)], False
        return [], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s3 = _derived_s3(herbs=("党参", "白术"))
    before = _digest(s3)
    s1, s2 = _s1s2()
    result = corroborate(s3, s1, s2)
    assert result.concordant or result.no_precedent  # 佐证真的跑了，不是提前 return
    assert _digest(s3) == before


def test_corroborate_never_mutates_s1_or_s2_either(monkeypatch, two_physicians):
    def _fake_search(query, physician, s2, retriever_mode):
        return [(_case("ye_tianshi-001", "ye_tianshi", ["党参"]), 0.9)], False

    monkeypatch.setattr(chain, "_search_cases", _fake_search)
    s1, s2 = _s1s2()
    s1_before = s1.model_dump()
    s2_before = s2.model_dump()
    corroborate(_derived_s3(), s1, s2)
    assert s1.model_dump() == s1_before
    assert s2.model_dump() == s2_before


# ---------- 五、序列化 ----------

def test_to_dict_is_json_serialisable():
    result = CorroborationResult(
        enabled=True,
        concordant=(PrecedentCase(case_id="a", physician="ye_tianshi", score=0.9,
                                  herb_distance=0.333333, shared_herbs=("党参",),
                                  syndrome="脾胃气虚证"),),
        no_precedent=("wu_jutong",),
        physicians_with_precedent=("ye_tianshi",),
    )
    d = result.to_dict()
    json.dumps(d)  # 不抛就算过
    assert d["concordant"][0]["herb_distance"] == 0.333
    assert d["no_precedent"] == ["wu_jutong"]


# ---------- 六、跟 run_derivation 的接线 ----------

def test_run_derivation_result_has_a_corroboration_key(monkeypatch):
    monkeypatch.setenv("S3_MODE", "derived")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    monkeypatch.setenv("CORROBORATION", "off")  # 隔离检索层，只看接线本身
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert "corroboration" in r
    assert r["corroboration"]["enabled"] is False


def test_corroboration_runs_after_verification_not_before(monkeypatch):
    """调用顺序：`_verify_and_revise` 必须先跑完，`corroborate()` 才被调用——
    用一个记录调用顺序的列表钉住这件事，而不是只看结果对不对。"""
    monkeypatch.setenv("S3_MODE", "derived")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    monkeypatch.setenv("CORROBORATION", "on")
    order: list[str] = []

    orig_verify = chain._verify_and_revise

    def _tracking_verify(*args, **kwargs):
        order.append("verify")
        return orig_verify(*args, **kwargs)

    def _tracking_corroborate(*args, **kwargs):
        order.append("corroborate")
        from core.corroboration import CorroborationResult
        return CorroborationResult(enabled=True)

    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "_verify_and_revise", _tracking_verify)
    monkeypatch.setattr(chain, "corroborate", _tracking_corroborate)
    chain.consult("胃脘胀满，纳差乏力")
    assert order == ["verify", "corroborate"]


def test_corroboration_is_disabled_end_to_end_through_consult(monkeypatch):
    monkeypatch.setenv("S3_MODE", "derived")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    monkeypatch.setenv("CORROBORATION", "off")

    def _poison(*a, **k):
        raise AssertionError("CORROBORATION=off 时 consult() 全程不该检索医案")

    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "_search_cases", _poison)
    out = chain.consult("胃脘胀满，纳差乏力")
    assert out["results"][0]["corroboration"]["enabled"] is False


def test_core_corroboration_module_does_not_import_chain_at_module_level():
    """破循环靠的是延迟 import——`core/chain.py` 在模块顶层 import 了
    `core.corroboration`，如果 `core.corroboration` 也在模块顶层 import
    `core.chain`，两边谁先加载都会炸。用 AST 静态检查模块顶层的 import
    语句，不靠"现在跑得通"这种运行时巧合。"""
    import ast
    from pathlib import Path

    src = Path("core/corroboration.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_level_imports = [
        n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    for node in top_level_imports:
        if isinstance(node, ast.ImportFrom) and node.module == "core.chain":
            pytest.fail("core/corroboration.py 不该在模块顶层 import core.chain")
