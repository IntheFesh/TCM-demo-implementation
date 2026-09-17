"""R35：规律层的消费端（`Ontology.patterns_for` → 知识块规律段）。

**这一组测的是"永不裁"和"无上限"不是一回事。** 规律段不进裁剪循环
（砍掉它等于回到"只有教材、没有这五位医家"），但 R35 实测：1924 条规律里
1869 条是医家档（`group_value=""`），而空串是任何证名的子串——
`patterns_for` 对任何证型都会把这 1869 条全返回。全放进知识块，光规律段
就吃掉整个 3 万 token 预算，本草与方剂一条都放不下，而"永不裁"又让
裁剪循环救不了它。结论：取前 N 条（按医家），并把"放了多少 / 一共多少"
都记进 stats 与段标题。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.context_prefix import (
    FOCUSED_MAX_CASE_IDS,
    FOCUSED_MAX_PATTERNS_PER_PHYSICIAN,
    build_focused_knowledge,
    count_tokens,
)
from core.ontology import Ontology, sort_patterns
from core.schemas import ElementHit, S1Normalize, S2Elements

ROOT = Path(__file__).resolve().parent.parent


# ---------- 夹具 ----------

def _pat(pid: str, physician: str, *, support: int, group_value: str = "",
         group_by: str = "physician", herbs: list[str] | None = None,
         case_ids: list[str] | None = None, kind: str = "herb") -> dict:
    return {
        "pattern_id": pid, "kind": kind, "physician": physician,
        "physician_name": {"ye_tianshi": "叶天士", "wu_jutong": "吴鞠通"}.get(
            physician, physician),
        "group_by": group_by, "group_value": group_value,
        "herbs": herbs or ["茯苓"], "support": support,
        "case_ids": case_ids or [f"{physician}-{i:04d}" for i in range(support)],
    }


def _row(s, p, o, *, book="中药学"):
    return {"s": s, "p": p, "o": o, "book": book, "source": "modern",
            "source_span": f"{s}，{p}：{o}"}


def _ont(patterns: list[dict]) -> Ontology:
    return Ontology(
        materia_rows=[_row("茯苓", "性味", "甘，平"), _row("茯苓", "功效", "利水渗湿")],
        formulary_rows=[],
        patterns=patterns,
    )


@pytest.fixture
def s1s2():
    s1 = S1Normalize(symptoms=["纳差"], tongue="淡红", pulse="细弱", unmapped=[])
    s2 = S2Elements(
        elements=[ElementHit(element="脾", kind="location",
                             supporting_symptoms=["纳差"], confidence="high")],
        unexplained_symptoms=[])
    return s1, s2


def _many(physician: str, n: int, *, group_value: str = "") -> list[dict]:
    return [_pat(f"{physician}-p{i:03d}", physician, support=100 - i,
                 group_value=group_value,
                 group_by="physician_syndrome" if group_value else "physician",
                 herbs=[f"药{i:03d}"]) for i in range(n)]


# ---------- 排序只有一处实现 ----------

def test_sort_puts_syndrome_level_first_then_support_then_id():
    a = _pat("a", "ye_tianshi", support=3, group_value="脾胃气虚",
             group_by="physician_syndrome")
    b = _pat("b", "ye_tianshi", support=200)
    c = _pat("c", "ye_tianshi", support=200)
    d = _pat("d", "ye_tianshi", support=5)
    assert [p["pattern_id"] for p in sort_patterns([d, c, b, a])] == ["a", "b", "c", "d"]


def test_sort_is_deterministic_for_equal_support():
    rows = [_pat(x, "ye_tianshi", support=7) for x in ("z", "m", "a")]
    assert ([p["pattern_id"] for p in sort_patterns(rows)]
            == [p["pattern_id"] for p in sort_patterns(list(reversed(rows)))]
            == ["a", "m", "z"])


def test_patterns_for_returns_them_sorted():
    ont = _ont([_pat("low", "ye_tianshi", support=3),
                _pat("high", "ye_tianshi", support=99)])
    got = ont.patterns_for("脾胃气虚", physician="ye_tianshi")
    assert [p["pattern_id"] for p in got] == ["high", "low"]


def test_only_one_pattern_sorting_implementation_exists():
    """第 31 条：同一概念的匹配/排序逻辑只能有一处实现。知识块合并多个证型的
    结果之后要重排，必须调 `sort_patterns`，不许自己再写一个 sorted(key=support)。"""
    src = Path(ROOT / "core" / "context_prefix.py").read_text(encoding="utf-8")
    assert "sort_patterns" in src
    assert 'key=lambda p: (\n' not in src
    for line in src.splitlines():
        if "sorted(" in line and "support" in line:
            raise AssertionError(f"context_prefix 里又写了一套规律排序：{line}")


# ---------- patterns_for 的两档语义 ----------

def test_the_physician_level_bucket_matches_any_syndrome_on_purpose():
    """`group_value=""` 恒命中：1075 诊次只有 116 条标了证型，只放证型档
    等于九成语料进不了知识块。这是有意的，代价由调用方的上限来兜。"""
    ont = _ont([_pat("p1", "ye_tianshi", support=9)])
    assert len(ont.patterns_for("随便什么证", physician="ye_tianshi")) == 1
    assert len(ont.patterns_for("", physician="ye_tianshi")) == 1


def test_patterns_for_filters_by_physician_id_not_name():
    ont = _ont([_pat("p1", "ye_tianshi", support=9), _pat("p2", "wu_jutong", support=9)])
    assert [p["pattern_id"] for p in ont.patterns_for("", physician="wu_jutong")] == ["p2"]
    assert ont.patterns_for("", physician="叶天士") == []


# ---------- 知识块里的上限 ----------

def test_each_physician_gets_at_most_the_cap(s1s2):
    s1, s2 = s1s2
    ont = _ont(_many("ye_tianshi", 400))
    _text, stats = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                           syndromes=["脾胃气虚"])
    assert FOCUSED_MAX_PATTERNS_PER_PHYSICIAN == 12
    assert stats["n_patterns"] == 12
    assert stats["n_patterns_available"] == 400


def test_the_cap_is_per_physician_so_the_case_rich_one_cannot_crowd_out_the_others(s1s2):
    """叶天士 495 诊次、吴鞠通 221 诊次。按总数取前 N 条会让案多的医家把案少的
    挤干净，"融合五家"就名存实亡。"""
    s1, s2 = s1s2
    ont = _ont(_many("ye_tianshi", 400) + _many("wu_jutong", 20))
    text, stats = build_focused_knowledge(
        s1, s2, [], ["ye_tianshi", "wu_jutong"], ontology=ont, syndromes=["脾胃气虚"])
    assert stats["n_patterns"] == 24
    assert "叶天士" in text and "吴鞠通" in text
    assert text.count("### 吴鞠通") == 12


def test_taking_the_first_n_is_recorded_not_silent(s1s2):
    """少放了东西必须可核。它跟被预算循环砍掉的段性质不同（上游选取 vs
    超预算裁剪），但都要出现在 trimmed_sections 里。"""
    s1, s2 = s1s2
    ont = _ont(_many("ye_tianshi", 50))
    _text, stats = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                          syndromes=["脾胃气虚"])
    assert "patterns_per_physician_cap" in stats["trimmed_sections"]
    assert stats["n_patterns"] < stats["n_patterns_available"]


def test_nothing_is_recorded_as_capped_when_nothing_was_dropped(s1s2):
    s1, s2 = s1s2
    ont = _ont(_many("ye_tianshi", 3))
    _text, stats = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                          syndromes=["脾胃气虚"])
    assert stats["n_patterns"] == stats["n_patterns_available"] == 3
    assert "patterns_per_physician_cap" not in stats["trimmed_sections"]


def test_the_section_heading_carries_both_numbers(s1s2):
    """任何数字都必须带对照：模型不能以为它看到的是全部规律。"""
    s1, s2 = s1s2
    ont = _ont(_many("ye_tianshi", 40))
    text, _ = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                     syndromes=["脾胃气虚"])
    assert "本次放入 12 条" in text
    assert "共 40 条" in text


def test_case_ids_are_capped_but_their_total_is_stated(s1s2):
    """support 上限 223，全列出来一条规律就吃掉 2 千 token。列前 8 条即可回查，
    但**总数必须写出来**。"""
    s1, s2 = s1s2
    ont = _ont([_pat("p1", "ye_tianshi", support=223)])
    text, _ = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                     syndromes=["脾胃气虚"])
    assert FOCUSED_MAX_CASE_IDS == 8
    assert text.count("ye_tianshi-00") == 8
    assert "共 223 条，此处列前 8 条" in text


def test_the_physician_level_bucket_is_labelled_not_left_blank(s1s2):
    s1, s2 = s1s2
    ont = _ont([_pat("p1", "ye_tianshi", support=9),
                _pat("p2", "ye_tianshi", support=8, group_value="脾胃气虚",
                     group_by="physician_syndrome")])
    text, _ = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                     syndromes=["脾胃气虚"])
    assert "### 叶天士·不分证型（该医家全部医案）" in text
    assert "### 叶天士·脾胃气虚" in text


def test_the_cap_can_be_set_by_env(monkeypatch, s1s2):
    s1, s2 = s1s2
    monkeypatch.setenv("FOCUSED_MAX_PATTERNS_PER_PHYSICIAN", "2")
    ont = _ont(_many("ye_tianshi", 40))
    _text, stats = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                          syndromes=["脾胃气虚"])
    assert stats["n_patterns"] == 2


def test_patterns_survive_a_one_token_budget_even_when_capped(s1s2):
    """"永不裁"这条没有变：预算再小也不砍规律段，变的只是上游取多少条。"""
    s1, s2 = s1s2
    ont = _ont(_many("ye_tianshi", 40))
    text, stats = build_focused_knowledge(s1, s2, [], ["ye_tianshi"], ontology=ont,
                                         syndromes=["脾胃气虚"], budget=1)
    assert stats["n_patterns"] == 12
    assert "名医用药规律" in text


# ---------- 真数据：整块还在预算里 ----------

def test_the_real_pattern_file_keeps_the_block_inside_the_budget(s1s2):
    """钉住本轮那个缺陷的实际后果：改之前这一块会把 3 万 token 预算吃光。"""
    from core.ontology import get_ontology

    ont = get_ontology()
    if not ont.available or ont.stats()["n_patterns"] == 0:
        pytest.skip("沙盒里没有药理层/规律层数据")
    s1, s2 = s1s2
    from core.physicians import PHYSICIANS, physicians_all

    pids = list(physicians_all(PHYSICIANS))
    text, stats = build_focused_knowledge(s1, s2, [], pids, ontology=ont,
                                         syndromes=["脾胃气虚", "湿热"])
    assert stats["n_patterns"] <= FOCUSED_MAX_PATTERNS_PER_PHYSICIAN * len(pids)
    assert stats["n_patterns_available"] > stats["n_patterns"]
    assert count_tokens(text) < 30_000
    assert stats["n_herbs"] > 0, "规律段没有把本草段挤掉"
