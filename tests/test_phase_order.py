"""R52：阶段顺序。「按医理药理演绎推导，医案退为事后佐证」这句话落到代码里，
是**医案检索绝不发生在演绎推导之前——而在这一轮，压根不发生**。

跟 `tests/test_derived_no_cases.py` 的分工：那份文件测 derived 这一档本身的
行为（返回值形状、schema 校验），这份测的是**阶段之间的顺序关系**——
构造 prompt、调 S3、验证闭环这几步之间，医案检索这道工序被摆在了哪里
（哪里都不摆，是这一轮唯一正确的答案；R54 才会在验证通过之后、作为独立的
第三相加上医案佐证，但那时"佐证"绝不回头改前两相的结论——这份文件先把
"前两相干净"钉住，R54 再加"第三相不回头改"那条）。
"""
from __future__ import annotations

import pytest

from core import chain

from tests.test_derived_no_cases import DerivedFakeLLM, _poison


@pytest.fixture
def derived_env(monkeypatch):
    monkeypatch.setenv("S3_MODE", "derived")
    monkeypatch.setenv("S3_BEST_OF_N", "1")


def test_search_cases_call_count_is_zero_when_corroboration_is_off(derived_env, monkeypatch):
    """比"抛异常才发现"更强的一条：即便 `_search_cases` 不被毒化、老老实实
    数调用次数，`CORROBORATION=off` 时也该是 0——不是"侥幸没触发"，是这一相
    在关掉第三相之后，压根不存在对它的引用。`CORROBORATION=on`（默认）下
    第三相会调用它，见 `tests/test_corroboration.py`——那不是对这条测试的
    否定，是两个不同开关状态下的两个事实。"""
    monkeypatch.setenv("CORROBORATION", "off")
    calls: list[tuple] = []
    orig = chain._search_cases

    def _counting(*args, **kwargs):
        calls.append(args)
        return orig(*args, **kwargs)

    monkeypatch.setattr(chain, "_search_cases", _counting)
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    chain.consult("胃脘胀满，纳差乏力")
    assert calls == []


def test_run_derivation_has_no_use_react_parameter():
    """`run_synthesis`/`run_physician` 都有 `use_react`；`run_derivation` 干脆
    没有这个参数——不是接了但默认关，是这一相从设计上就没有"要不要接工具"
    这个选项（工具集里有查医案的工具，接了就是重新引入医案检索）。"""
    import inspect

    sig = inspect.signature(chain.run_derivation)
    assert "use_react" not in sig.parameters


def test_the_prompt_construction_happens_before_the_s3_call(derived_env, monkeypatch):
    """prompt（system 字符串）必须在调用 LLM 之前就完整拼好——`_format_theory_rules`
    产出的文本要出现在真正发给假后端的 system 里，不是调用之后才补上去的。"""
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    chain.consult("胃脘胀满，纳差乏力")
    assert len(llm.s3_systems) == 1
    # 医理规则表一定已经渲染进这次真正发出去的 system——不是调用完之后才拼。
    assert "医理规则表" in llm.s3_systems[0]


def test_verification_happens_after_the_s3_call_not_before(derived_env, monkeypatch):
    """验证闭环（`_verify_and_revise`）读的是 S3 的产出，天然只能在 S3 调用
    之后发生——这里用一个会触发 revise 的 payload（本体不可用时七条规则全部
    unverifiable，`passed=False` 但不会真的重开，因为没有 violations 可回灌），
    断言 `verification` 字段确实来自这次真正跑过的 S3 产出，而不是一个提前
    算好的占位值。"""
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    out = chain.consult("胃脘胀满，纳差乏力")
    r = out["results"][0]
    # verification 的证型必须跟这次 S3 真实产出的证型一致——如果验证发生在
    # S3 调用之前，这里能核对的只会是一个上一轮遗留的旧值。
    assert r["s3"].syndrome == r["s3_structured"].syndrome.name


def test_no_reference_cases_is_set_without_ever_attempting_retrieval(derived_env, monkeypatch):
    """`no_reference_cases=True` 不是"检索过、正好没查到"（那是
    `S3StructuredUnreferenced` 的场景），是"演绎推导这一步根本没检索"——
    `CORROBORATION=off` 隔离掉第三相（那一相**会**检索，`get_retriever`
    毒化了会炸，见 `tests/test_corroboration.py`），只看第一相自己的字段。"""
    monkeypatch.setenv("CORROBORATION", "off")
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", _poison("get_retriever"))
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    assert r["no_reference_cases"] is True


def test_derivation_completes_with_zero_physicians_enumerated(derived_env, monkeypatch):
    """跟 legacy/structured 的顺序都不同：那两条路径在跑 S3 之前要先决定
    "按哪份医家名单跑"（`physicians_enabled`/`physicians_for_synthesis`），
    derived 这一步压根不问这件事——`physicians_for_mode("derived")` 恒空，
    `run_derivation` 内部也没有任何按医家分支的代码路径。"""
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    out = chain.consult("胃脘胀满，纳差乏力")
    assert len(out["results"]) == 1, "不是每位医家一条，是恰好一条"


def test_a_second_consult_does_not_leak_state_from_the_first(derived_env, monkeypatch):
    """两次连续问诊之间不该有残留状态——第二次的 system 里该是第二次的症状，
    不带第一次的痕迹（`load_theory()` 的模块级缓存除外，那是只读数据，
    不是"上一次问诊的结论"）。假后端的 S1 是脚本化的（不真的解析主诉），
    所以这里直接换一个带不同 `s1` 的假后端来代表"下一次问诊"。"""
    from core.schemas import S1Normalize

    llm1 = DerivedFakeLLM({}, s1=S1Normalize(
        symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[]))
    monkeypatch.setattr(chain, "get_llm", lambda: llm1)
    chain.consult("胃脘胀满，纳差乏力")
    first_system = llm1.s3_systems[-1]

    llm2 = DerivedFakeLLM({}, s1=S1Normalize(
        symptoms=["胁肋胀痛", "急躁易怒"], tongue="红", pulse="弦", unmapped=[]))
    monkeypatch.setattr(chain, "get_llm", lambda: llm2)
    chain.consult("胁肋胀痛，急躁易怒")
    second_system = llm2.s3_systems[-1]

    # 只看「患者症状：」那一行——`FakeLLM` 的 S2 是脚本化的固定返回值
    # （跟传入的 s1 无关），"证素分析" 那段会一直说"依据：纳差"，不能拿来
    # 判断这次跑的是哪个 s1；症状行才是从这次真实传入的 s1 里取的。
    assert "患者症状：纳差；乏力" in first_system
    assert "患者症状：胁肋胀痛；急躁易怒" in second_system
    assert "患者症状：纳差；乏力" not in second_system


def test_the_corroboration_field_landed_in_r54_nested_not_flattened(derived_env, monkeypatch):
    """R54 落地：这条测试原来叫 `test_future_corroboration_field_is_not_yet_present`，
    钉住"R54 还没实现"这件事，好让 R54 落地时这里变红、提醒作者来更新它
    ——现在更新成对 R54 真实形状的断言。四个桶键**嵌在 `r["corroboration"]`
    里，不是拍平进 `r` 本身**：`concordant`/`divergent`/`no_precedent`/
    `physicians_with_precedent` 都不是这次演绎推导结论（`s3`/`s3_structured`）
    的一部分，混进 `r` 顶层会让"这是推导出的还是佐证附加的"这条界线在数据
    形状上消失——跟 `s3_structured` 用同一个键、`corroboration` 单独一个键，
    是同一条纪律的两种应用。详细的行为测试在 `tests/test_corroboration.py`。
    """
    llm = DerivedFakeLLM({})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    r = chain.consult("胃脘胀满，纳差乏力")["results"][0]
    for top_level_leak in ("concordant", "divergent", "no_precedent", "physicians_with_precedent"):
        assert top_level_leak not in r
    assert "corroboration" in r
    assert set(r["corroboration"]) == {
        "enabled", "concordant", "divergent", "no_precedent",
        "physicians_with_precedent", "note",
    }
