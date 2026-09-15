"""R18-D：《脾胃论》立论三元组抽取。

这些测试**不需要真实语料**（`data/local_corpora/脾胃论.txt` 在版本控制里，
但测试不依赖它的具体内容），除了两条标了 real_corpus 的对账测试——它们钉的是
实测数字，语料缺失时跳过而不是假装通过。
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from core.schemas import RationaleRecord
from offline.extract_rationale_pwl import (
    DEFAULT_INPUT,
    DEFAULT_OUTPUT,
    TREATMENTS,
    _chapter_title,
    drop_front_matter,
    extract,
    extract_from_sentence,
    looks_like_herb_list,
    probe,
    split_sentences,
    verify_spans,
)


# ---------- 篇名识别 ----------

def test_chapter_title_squeezes_inline_spaces():
    """原书篇名有四个是跨行排的，转 txt 后留下行内空格。不抹空白会少 4 篇。"""
    assert _chapter_title("大肠小肠五脏皆属于 胃胃虚则俱病论") == "大肠小肠五脏皆属于胃胃虚则俱病论"
    assert _chapter_title("长夏湿热胃困尤甚用 清暑益气汤论") == "长夏湿热胃困尤甚用清暑益气汤论"


def test_chapter_title_rejects_dose_lines():
    """药量行整行无标点、长度也够短，不认剂量就会被当成篇名。

    判据复用 pharmacology_sources.has_classic_dose，不是本模块另写的正则。
    """
    assert _chapter_title("黄丹二钱定粉舶上硫黄陀僧已上各三钱轻粉少许") is None
    assert _chapter_title("人参茯苓白术甘草橘皮已上各五分") is None


def test_chapter_title_keeps_junchenzuoshi_chapter_but_drops_the_label_line():
    """「君臣佐使法」是真篇名，「白术君人参臣……桑白皮佐」是标注行。

    只数角色字出现次数会把前者一起误杀（它四个角色字全有），所以判据是
    **行尾是角色字**。
    """
    assert _chapter_title("君臣佐使法") == "君臣佐使法"
    assert _chapter_title("白术君人参臣甘草佐芍药佐黄连使黄芪臣桑白皮佐") is None


def test_chapter_title_rejects_volume_lines():
    """卷次行也是一整行纯汉字，不排掉会变成一篇，把它到第一个真篇名之间的行吃进去。"""
    assert _chapter_title("脾胃论卷上") is None
    assert _chapter_title("脾胃论卷下") is None
    assert _chapter_title("脾胃虚实传变论") == "脾胃虚实传变论"


# ---------- 目录块切除 ----------

def test_drop_front_matter_ends_toc_at_the_first_punctuated_line():
    """目录里混着 `_chapter_title` 认不出来的行（「摄 养」两个字）。

    逐行要求"像篇名"的话目录会在这些行上提前结束，后面剩下的目录行全部变成
    假篇名——修复前实测多出 3 个空篇。判据改成"第一行带句读的行"。
    """
    lines = [
        "版权页",
        "Table of Contents 脾胃论卷上 脾胃虚实传变论",
        "脾胃胜衰论",
        "摄 养",          # 两个字，不像篇名，但仍是目录
        "省 言 箴",
        "如果你不知道读什么书，",   # 第一行带句读 → 目录到此结束
        "脾胃虚实传变论",
        "夫饮食不节则胃病。",
    ]
    kept = drop_front_matter(lines)
    assert kept[0] == "如果你不知道读什么书，"
    assert "脾胃胜衰论" not in kept


def test_drop_front_matter_without_toc_mark_keeps_everything():
    """没有目录标记的语料（测试用的小样本）整份都当正文，不静默返回空。"""
    lines = ["脾胃虚实传变论", "夫饮食不节则胃病。"]
    assert drop_front_matter(lines) == lines


# ---------- 切句 ----------

def test_split_sentences_cuts_on_semicolons_too():
    """本书的条件-处置句大量用分号并列，不切分号会让一句里有两条处置。"""
    line = "腹中痛者，加甘草、白芍药；腹痛兼发热，加黄芩；恶寒或腹中觉寒，加桂。"
    sents = split_sentences([line])
    assert len(sents) == 3
    assert sents[1] == "腹痛兼发热，加黄芩"


# ---------- 六个谓词各自的抽取 ----------

def test_use_formula_triple():
    got = extract_from_sentence("如脉缓，病怠惰嗜卧，四肢不收，或大便泄泻，此湿胜，从平胃散", "脾胃胜衰论")
    assert [(r.p, r.o) for r in got] == [("用方", "平胃散")]
    # 条件取的是处置之前**整**一截，不是最后一个逗号之后那一小段：
    # 只取最后一段会丢掉「脉缓」这个真正的证候。
    assert got[0].s.startswith("脉缓")
    assert "湿胜" in got[0].s


def test_add_and_drop_herb_triples():
    add = extract_from_sentence("如肺气短促，或不足者，加人参、白芍药", "脾胃胜衰论")
    assert [(r.p, r.o) for r in add] == [("加药", "人参、白芍药")]
    drop = extract_from_sentence("气短小便利者，四君子汤中去茯苓", "脾胃胜衰论")
    assert ("去药", "茯苓") in [(r.p, r.o) for r in drop]


def test_add_rule_rejects_non_herb_objects():
    """「加」后面跟的不一定是药名。仓库里没有药名词表，判据只能是形状。"""
    assert looks_like_herb_list("人参、白芍药")
    assert looks_like_herb_list("防风")
    assert not looks_like_herb_list("正药中")     # 是"加到哪里"
    assert not looks_like_herb_list("一分可也")   # 是剂量
    assert not looks_like_herb_list("芍药收之")   # 药名后面挂了动词
    # 整句走一遍：这三种都不该产出「加药」
    for sent in ("或渴，从五苓散去桂，摘一二味加正药中",
                 "于甘草五分中加一分可也",
                 "腹中夯闷，乃散而不收，可加芍药收之"):
        assert "加药" not in [r.p for r in extract_from_sentence(sent, "x")]


def test_forbid_triple():
    got = extract_from_sentence("腹满气不转者，勿加", "脾胃胜衰论")
    assert [(r.p, r.o) for r in got] == [("禁忌", "勿加")]


def test_treatment_rule_does_not_fire_on_ze():
    """「脾病则下流乘肾」里的「下」是"下流"的下，不是下法。

    第一版触发字含「则」，这一句抽出了 (脾病)-[治法]->(下) 这条错的。
    """
    assert "治法" not in [r.p for r in extract_from_sentence("脾病则下流乘肾，土克水", "x")]
    got = extract_from_sentence("若用辛甘之药滋胃，当升当浮，使生长之气旺", "x")
    assert ("治法", "升") in [(r.p, r.o) for r in got]


def test_mechanism_takes_the_shortest_valid_conclusion():
    """「则」后面整截拿来当 o 得到的是半句原文，不是一个结论。"""
    got = extract_from_sentence("形体劳役则脾病，脾病则怠惰嗜卧，四肢不收，大便泄泻", "x")
    mech = [(r.s, r.o) for r in got if r.p == "病机"]
    assert mech == [("形体劳役", "脾病")]


def test_mechanism_requires_an_element_word_in_the_conclusion():
    """「胆气春升则万化安」的结论里没有证素词，不是病机结论，不抽。

    证素判据复用 core.elements 的 LOCATIONS/NATURES（CLAUDE.md 第 31 条），
    不是本模块另写的字面表。
    """
    assert "病机" not in [r.p for r in extract_from_sentence("胆气春升则万化安", "x")]


def test_one_sentence_can_yield_several_triples():
    """「四君子汤中去茯苓」既有去药也有方——返回列表不是 0/1。

    这一条最初是红的：`_USE_FORMULA_RE` 只认「从/用/服/投 + 方名」，而原文里
    「在某方基础上加减」写作「方名 + 中」，没有处置动词，实测漏 10 条。
    """
    got = extract_from_sentence("气短小便利者，四君子汤中去茯苓", "x")
    assert len(got) >= 2
    assert {"用方", "去药"} <= {r.p for r in got}


def test_sentences_outside_the_length_window_are_skipped():
    """太短抽不出条件-处置对；太长的是引《内经》的整段论述，抽出来的 s 是一大段原文。"""
    assert extract_from_sentence("亦加之", "x") == []
    long_sent = "夫" + "饮食不节则胃病" * 30
    assert extract_from_sentence(long_sent, "x") == []


# ---------- source_span 核验 ----------

def test_verify_spans_drops_records_whose_span_is_not_in_the_source():
    """规则抽取不会编造 span，但会因为切句边界算错而截出原文里不存在的文字。"""
    good = RationaleRecord(s="a", p="病机", o="b", source_span="夫饮食不节则胃病",
                           chapter="x", book="脾胃论")
    bad = good.model_copy(update={"source_span": "这句话原文里没有"})
    kept, dropped = verify_spans([good, bad], "……夫饮食不节则胃病，胃病则气短……")
    assert [r.source_span for r in kept] == ["夫饮食不节则胃病"]
    assert dropped == 1


def test_every_extracted_span_is_verbatim_in_the_source_text():
    text = "脾胃胜衰论\n夫饮食不节则胃病，胃病则气短精神少而生大热。\n腹中急缩，或脉弦，加防风。\n"
    records, stats = extract(text)
    assert records, "小样本也该抽出东西来，抽不出说明规则坏了"
    assert stats["n_span_dropped"] == 0
    for r in records:
        assert r.source_span in text


# ---------- schema 约束 ----------

def test_predicate_is_a_closed_set():
    """谓词受控，六选一。不限定的话同一个关系会有好几种写法。"""
    with pytest.raises(ValidationError):
        RationaleRecord(s="a", p="治则", o="b", source_span="c", chapter="d", book="脾胃论")


def test_record_rejects_empty_source_span():
    """防幻觉约束：source_span 不许空（CLAUDE.md「防幻觉约束不许放松」）。"""
    with pytest.raises(ValidationError):
        RationaleRecord(s="a", p="病机", o="b", source_span="", chapter="d", book="脾胃论")


def test_treatments_are_disjoint_from_the_element_table():
    """治法词表跟 ELEMENTS 回答的不是同一个问题，但也不该互相污染。

    「湿」既是证素又可能被当成治法动作的话，(脾虚)-[治法]->(湿) 这种条目
    就会出现——那不是治法。
    """
    from core.elements import ELEMENTS
    assert not (set(TREATMENTS) & set(ELEMENTS))


# ---------- 真实语料对账（缺语料就跳过，不假装通过） ----------

@pytest.mark.skipif(not DEFAULT_INPUT.exists(), reason="没有 data/local_corpora/脾胃论.txt")
def test_real_corpus_probe_numbers():
    """先探再切（R18-C 立的规矩）。这几个数是实测值，改了切分逻辑要么这里跟着改、
    要么说明为什么变——不能悄悄变。"""
    p = probe(DEFAULT_INPUT.read_text(encoding="utf-8"))
    assert p["n_lines"] == 659
    assert p["n_chapters"] == 97
    assert p["n_sentences"] == 1570
    # 第一篇是正文第一篇，不是目录里的任何一条
    assert p["chapters"][0] == "脾胃虚实传变论"
    assert p["chapters"][-1] == "省言箴"


@pytest.mark.skipif(not DEFAULT_INPUT.exists(), reason="没有 data/local_corpora/脾胃论.txt")
def test_real_corpus_triple_counts():
    records, stats = extract(DEFAULT_INPUT.read_text(encoding="utf-8"))
    assert stats["n_triples"] == 278
    assert stats["n_span_dropped"] == 0
    assert stats["by_predicate"] == {
        "禁忌": 111, "加药": 81, "用方": 32, "病机": 28, "治法": 21, "去药": 5,
    }
    # 禁忌最多不是巧合：《脾胃论》有「用药宜禁论」整整一篇。
    assert max(stats["by_predicate"], key=lambda k: stats["by_predicate"][k]) == "禁忌"


@pytest.mark.skipif(not DEFAULT_OUTPUT.exists(), reason="还没跑 extract_rationale_pwl")
def test_committed_jsonl_matches_a_fresh_run():
    """进版本控制的生成物必须能被重跑复现——这是它可以进 data/standard/ 的前提。"""
    on_disk = [json.loads(ln) for ln in DEFAULT_OUTPUT.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not DEFAULT_INPUT.exists():
        pytest.skip("没有源语料，只能校验落盘格式")
    fresh, _ = extract(DEFAULT_INPUT.read_text(encoding="utf-8"))
    assert on_disk == [r.model_dump() for r in fresh]
