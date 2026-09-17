"""R32：知识块到底有没有进最终提示词。

**这个文件存在的理由本身就是一条教训。** 在 R32 之前，知识速查表（本草 9776 +
方剂 3184）只在 `full_context` 模式下进提示词（走 `context_prefix.assemble()` 的
稳定前缀），而演示与录制跑的是 `hybrid`——「让模型明白药理」在运行配置下**从未
发生过**，而且整套测试全绿：没有一条测试断言"知识块出现在发给 LLM 的那段文本里"。
测了组装函数的输出、测了 token 预算、测了缓存命中率，就是没测**最终 prompt**。

所以这里的每一条都断言"发给 LLM 的 system 字符串里有/没有什么"，不是断言
某个中间函数返回了什么。
"""
from __future__ import annotations

import pytest

from core import chain
from core.context_prefix import (
    FOCUSED_CUT_ORDER,
    FOCUSED_KNOWLEDGE_MAX_TOKENS,
    KNOWLEDGE_MODES,
    build_focused_knowledge,
    knowledge_in_prompt,
)
from core.ontology import Ontology
from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome

from tests.test_chain import FakeRetriever, ReActFakeLLM, _case


# ---------- 夹具 ----------

def _row(s, p, o, *, book="中药学"):
    return {"s": s, "p": p, "o": o, "book": book, "source": "modern",
            "source_span": f"{s}，{p}：{o}"}


@pytest.fixture
def ont() -> Ontology:
    return Ontology(
        materia_rows=[
            _row("柴胡", "性味", "苦、辛，微寒"),
            _row("柴胡", "归经", "归肝、胆经"),
            _row("柴胡", "功效", "疏肝解郁、和解表里"),
            _row("柴胡", "用量", "3~10g"),
            _row("党参", "性味", "甘，平"),
            _row("党参", "归经", "归脾、肺经"),
            _row("党参", "功效", "补中益气、健脾益肺"),
            _row("白术", "性味", "苦、甘，温"),
            _row("白术", "功效", "健脾益气、燥湿利水"),
            _row("白术", "炮制", "麸炒"),
        ],
        formulary_rows=[
            _row("四君子汤", "组成", "党参 9g、白术 9g、茯苓 9g、甘草 6g", book="方剂学"),
            _row("四君子汤", "君药", "党参", book="方剂学"),
            _row("四君子汤", "主治", "脾胃气虚", book="方剂学"),
            _row("四君子汤", "功用", "益气健脾", book="方剂学"),
        ],
        patterns=[{
            "pattern_id": "pat-1", "physician": "ye_tianshi", "physician_name": "叶天士",
            "group_value": "脾胃气虚", "kind": "herb_pair", "support": 6,
            "herbs": ["党参", "白术"], "case_ids": ["ye_tianshi-001", "ye_tianshi-007"],
        }],
    )


@pytest.fixture
def s1s2():
    s1 = S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(
        elements=[ElementHit(element="脾", kind="location",
                             supporting_symptoms=["纳差"], confidence="high")],
        unexplained_symptoms=[],
    )
    return s1, s2


def _run(monkeypatch, ont, s1, s2, *, mode="hybrid", knowledge_env=None):
    """真的跑一次 run_physician，返回 (发给 LLM 的 system, 结果 dict)。"""
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    if knowledge_env is None:
        monkeypatch.delenv("KNOWLEDGE_IN_PROMPT", raising=False)
    else:
        monkeypatch.setenv("KNOWLEDGE_IN_PROMPT", knowledge_env)
    case = _case(case_id="ye_tianshi-001", raw_excerpt="纳谷不香，脘腹痞满。",
                 symptoms=["纳差"], tongue="淡红", pulse="细弱",
                 syndrome="脾胃气虚", herbs=["党参", "白术"])
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
                    cited_case_ids=["ye_tianshi-001"])
    fake_llm = ReActFakeLLM({"叶天士": s3})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever([case]))
    monkeypatch.setattr("core.ontology.get_ontology", lambda: ont)
    result = chain.run_physician(s1, s2, "ye_tianshi", "叶天士", retriever_mode=mode)
    assert len(fake_llm.s3_systems) == 1
    return fake_llm.s3_systems[0], result


# ---------- 这一轮要修的那个缺陷本身 ----------

@pytest.mark.parametrize("mode", ["hybrid", "dense", "graph", "bm25"])
def test_the_knowledge_block_reaches_the_llm_in_every_top3_mode(monkeypatch, ont, s1s2, mode):
    """**这是本轮的核心断言。** 改造之前这四种模式下知识块一个字都不会出现在
    system 里，而所有测试依然全绿。"""
    s1, s2 = s1s2
    system, result = _run(monkeypatch, ont, s1, s2, mode=mode)
    assert "柴胡" in system or "党参" in system, f"{mode} 模式下本草条目没进提示词"
    assert "疏肝解郁" in system or "补中益气" in system, f"{mode} 模式下功效没进提示词"
    assert result["knowledge"]["mode"] == "focused"
    assert result["knowledge"]["available"] is True
    assert result["knowledge"]["n_herbs"] > 0


def test_the_knowledge_block_sits_before_the_case_block(monkeypatch, ont, s1s2):
    """次序是定死的：知识块在参考医案**之前**。`_format_case_block` 的输出与它在
    `$refs` 里的相对次序一字未动——E3/E4 闸门验过的就是那个格式与那个次序。"""
    s1, s2 = s1s2
    system, _ = _run(monkeypatch, ont, s1, s2)
    assert "## 参考医案" in system
    knowledge_pos = min(system.index(x) for x in ("柴胡", "党参") if x in system)
    assert knowledge_pos < system.index("## 参考医案")
    assert system.index("## 参考医案") < system.index("纳谷不香")


def test_the_case_block_format_is_untouched(monkeypatch, ont, s1s2):
    """§0.6 明确不做：不改 `_format_case_block`。这条钉的就是它。"""
    s1, s2 = s1s2
    case = _case(case_id="ye_tianshi-001", raw_excerpt="纳谷不香，脘腹痞满。",
                 symptoms=["纳差"], tongue="淡红", pulse="细弱",
                 syndrome="脾胃气虚", herbs=["党参", "白术"])
    block = chain._format_case_block(case)
    system, _ = _run(monkeypatch, ont, s1, s2)
    assert block in system, "医案块必须逐字原样出现，知识块不许插进它内部"


# ---------- off 档：逐字节等于改造之前 ----------

def test_off_produces_a_prompt_byte_identical_to_before_the_change(monkeypatch, ont, s1s2):
    """off 档是消融实验的 A 组。**逐字节等于改前**，否则 A 组就不是对照组
    ——多一个换行都会让"知识块带来的差异"混进"prompt 变了"这个额外变量。"""
    s1, s2 = s1s2
    with_knowledge, _ = _run(monkeypatch, ont, s1, s2, knowledge_env=None)
    without, result = _run(monkeypatch, ont, s1, s2, knowledge_env="off")
    assert result["knowledge"]["mode"] == "off"
    assert "## 参考医案" not in without, "off 档不该多出这个小标题"
    assert "柴胡" not in without and "党参条目" not in without
    assert without != with_knowledge

    # 跟一个"本体压根不可用"的跑法比：两者必须逐字节相同——off 的语义正是
    # "就当没有本体这回事"。
    empty = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    as_if_absent, _ = _run(monkeypatch, empty, s1, s2, knowledge_env=None)
    assert without == as_if_absent


def test_when_the_ontology_is_absent_the_prompt_is_unchanged_and_the_manifest_says_so(
        monkeypatch, s1s2):
    """药理层 jsonl 不在（沙盒常态）：prompt 照旧、**不假装放了知识块**，
    manifest 里 available=False 与 tokens=0 两件事分开记。"""
    s1, s2 = s1s2
    empty = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    system, result = _run(monkeypatch, empty, s1, s2)
    assert "## 参考医案" not in system
    assert result["knowledge"]["mode"] == "focused"      # 选了 focused
    assert result["knowledge"]["available"] is False      # 但本体不在
    assert result["knowledge"]["tokens"] == 0


# ---------- 三档选择 ----------

def test_full_context_keeps_using_the_stable_prefix(monkeypatch, ont, s1s2):
    """`full_context` 已经把全量速查表放进稳定前缀了（前缀缓存命中实测 0.989），
    再塞一份裁剪版是纯浪费。"""
    assert knowledge_in_prompt("full_context") == "full"
    for m in ("hybrid", "dense", "graph", "bm25"):
        assert knowledge_in_prompt(m) == "focused"


def test_the_env_var_overrides_the_default_and_rejects_typos(monkeypatch):
    for m in KNOWLEDGE_MODES:
        monkeypatch.setenv("KNOWLEDGE_IN_PROMPT", m.upper())   # 大小写不敏感
        assert knowledge_in_prompt("hybrid") == m
    monkeypatch.setenv("KNOWLEDGE_IN_PROMPT", "focussed")      # 拼错
    with pytest.raises(ValueError) as e:
        knowledge_in_prompt("hybrid")
    assert "focussed" in str(e.value)
    for m in KNOWLEDGE_MODES:
        assert m in str(e.value), "报错要列出可用值，让人自己改对"


# ---------- build_focused_knowledge 本身 ----------

def test_focused_knowledge_reports_what_it_put_in(ont, s1s2):
    s1, s2 = s1s2
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["党参", "白术"])
    text, stats = build_focused_knowledge(
        s1, s2, [(case, 0.9)], ["ye_tianshi"], ontology=ont, syndromes=["脾胃气虚"])
    assert stats["available"] is True
    assert stats["n_herbs"] >= 2 and stats["n_formulas"] >= 1 and stats["n_patterns"] == 1
    assert stats["tokens"] > 0
    assert stats["trimmed_sections"] == []
    assert "四君子汤" in text and "党参" in text


def test_patterns_carry_their_case_ids(ont, s1s2):
    """规律块必须带 case_ids——它是"这条规律有据可查"的凭据，也是 S3 的
    `physician_influences` 能回指到医案的依据。没有它，规律就是一句没有出处的话。"""
    s1, s2 = s1s2
    text, _ = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                      syndromes=["脾胃气虚"])
    assert "ye_tianshi-001" in text and "ye_tianshi-007" in text
    assert "支持案数：6" in text


def test_patterns_are_never_trimmed_even_at_a_tiny_budget(ont, s1s2):
    """裁剪顺序里没有 patterns：砍掉它等于回到"只有教材、没有这五位医家"，
    而"融合五家"正是这一轮要在知识层做到的事。"""
    assert "patterns" not in FOCUSED_CUT_ORDER
    s1, s2 = s1s2
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["党参", "白术"])
    text, stats = build_focused_knowledge(
        s1, s2, [(case, 0.9)], ["ye_tianshi"], ontology=ont,
        syndromes=["脾胃气虚"], budget=1)
    assert "ye_tianshi-001" in text, "预算再小也不许砍掉用药规律"
    assert stats["n_patterns"] == 1
    assert stats["trimmed_sections"], "砍了什么必须报出来，不能静默少放"
    assert stats["n_formulas"] == 0, "方剂是第一个被砍的"


def test_the_trim_order_is_the_declared_one(ont, s1s2):
    assert FOCUSED_CUT_ORDER == ("formulary", "materia_detail", "materia_entries")
    s1, s2 = s1s2
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["党参", "白术"])
    _text, stats = build_focused_knowledge(
        s1, s2, [(case, 0.9)], ["ye_tianshi"], ontology=ont,
        syndromes=["脾胃气虚"], budget=1)
    order = [x for x in FOCUSED_CUT_ORDER if x in stats["trimmed_sections"]]
    assert stats["trimmed_sections"][:len(order)] == order


def test_the_budget_can_be_set_by_env(monkeypatch, ont, s1s2):
    s1, s2 = s1s2
    assert FOCUSED_KNOWLEDGE_MAX_TOKENS == 30_000
    monkeypatch.setenv("FOCUSED_KNOWLEDGE_MAX_TOKENS", "1")
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["党参", "白术"])
    _text, stats = build_focused_knowledge(
        s1, s2, [(case, 0.9)], ["ye_tianshi"], ontology=ont, syndromes=["脾胃气虚"])
    assert stats["trimmed_sections"], "环境变量没生效"


def test_detail_trimming_drops_preparation_first(ont, s1s2):
    """炮制/别名对"这味药该不该用"没有判据价值，是超预算时第一批该砍的。"""
    s1, s2 = s1s2
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["白术"])
    full, _ = build_focused_knowledge(s1, s2, [(case, 0.9)], [], ontology=ont)
    assert "麸炒" in full
    thin, stats = build_focused_knowledge(s1, s2, [(case, 0.9)], [], ontology=ont, budget=1)
    assert "materia_detail" in stats["trimmed_sections"]
    assert "麸炒" not in thin


# ---------- manifest ----------

def test_the_manifest_records_the_three_knowledge_fields():
    """manifest 是"我们的结果"这句话的全部可信度来源。知识块进没进提示词、
    进了多少 token、本体在不在，三件事分开记。"""
    m = chain._build_manifest(
        1, 1, False, retriever_mode="hybrid",
        knowledge={"mode": "focused", "available": True, "tokens": 1234,
                   "n_herbs": 7, "n_formulas": 2, "n_patterns": 1})
    assert m["knowledge_in_prompt"] == "focused"
    assert m["knowledge_tokens"] == 1234
    assert m["knowledge_entries"] == {"available": True, "herbs": 7,
                                      "formulas": 2, "patterns": 1}


def test_the_manifest_falls_back_to_the_mode_when_no_physician_ran():
    """安全否决那条早返回路径上没有 results，知识字段也得有值——
    manifest 里缺一个 key 比写 0 更难查。"""
    m = chain._build_manifest(1, 0, False, retriever_mode="hybrid", knowledge=None)
    assert m["knowledge_in_prompt"] in KNOWLEDGE_MODES
    assert m["knowledge_tokens"] == 0
    assert m["knowledge_entries"]["available"] is False


def test_aggregate_takes_the_max_not_the_sum():
    """几位医家的知识块高度重叠（同一批本草条目），求和会报出一个比实际放进去的
    多好几倍的数——而这个数字是要被引进报告的。"""
    rows = [{"knowledge": {"mode": "focused", "available": True, "tokens": 100,
                           "n_herbs": 5, "n_formulas": 2, "n_patterns": 1}},
            {"knowledge": {"mode": "focused", "available": True, "tokens": 120,
                           "n_herbs": 6, "n_formulas": 2, "n_patterns": 1}}]
    agg = chain._aggregate_knowledge(rows)
    assert agg["tokens"] == 120 and agg["n_herbs"] == 6
    assert agg["n_formulas"] == 2, "两位医家各 2 首、大量重叠，不该变成 4"
    assert chain._aggregate_knowledge([]) is None
    assert chain._aggregate_knowledge(None) is None
