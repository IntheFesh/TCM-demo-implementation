"""R34a/34b/34c：查不到依据 ≠ 通过；分母说清是哪一层；方剂数先查清再用。

这三条都是"数字的含义"而不是"功能对不对"——所以每条测试钉的都是
**同一个数不会被两种口径混用**。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.formula_verifier import (
    ALL_RULES,
    herbs_grounded_ratio,
    ontology_coverage_of_corpus,
    verify_formula,
)
from core.ontology import Ontology, get_ontology
from core.schemas import S3Structured

ROOT = Path(__file__).resolve().parent.parent


def _row(s, p, o):
    return {"s": s, "p": p, "o": o, "book": "中药学", "source": "modern",
            "source_span": f"【{p}】{o}"}


def _s3(herbs, refs=None, roles=None):
    roles = roles if roles is not None else ["君"] + ["臣"] * (len(herbs) - 1)
    items = [{"name": h, "dose": 9.0, "role": r} for h, r in zip(herbs, roles)]
    return S3Structured(
        organs=[{"organ": "脾", "supporting_symptoms": ["纳差"],
                 "pathogenesis": "脾失健运"}],
        syndrome={"name": "脾胃气虚证", "from_organs": ["脾"], "reasoning": "x",
                  "reasoning_plain": "y"},
        method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                "targets": ["脾失健运"]},
        formula={"from_method": "健脾益气", "candidate": {
            "name": "方", "source": "composed", "confidence": "high",
            "rationale": "x", "herb_items": items}},
        herb_choices=[{"item": it, "for_element": "脾", "effect_cited": "补中益气",
                       "ontology_refs": (refs or {}).get(it["name"], [])}
                      for it in items],
        physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                               "contribution": "x", "cited_case_ids": ["a"]}],
        cited_case_ids=["a"])


# ---------- 34a：只有性味没有归经的药，归经规则必须落进 unverifiable ----------

def test_a_herb_with_nature_but_no_meridian_makes_the_rule_unverifiable():
    """**34a 的判据原文**：构造一味只有性味没有归经的药，`meridian_coverage`
    必须落进 unverifiable 而不是 passed。"""
    ont = Ontology(materia_rows=[
        _row("某药", "性味", "甘，平"),
        _row("某药", "功效", "补中益气"),
    ], formulary_rows=[], patterns=[])
    r = verify_formula(_s3(["某药"]), ontology=ont)
    assert "meridian_coverage" not in {v.rule for v in r.violations}
    assert "meridian_coverage" not in r.checked_rules, "没判就不许记进 checked_rules"
    u = [x for x in r.unverifiable if x.rule == "meridian_coverage"]
    assert len(u) == 1
    assert u[0].missing_predicate == "归经"
    assert "某药" in u[0].reason and "判不了" in u[0].reason
    assert r.passed is False, "判不了就不算通过"
    assert r.status == "partially_verified"


def test_adding_the_meridian_moves_it_from_unverifiable_to_checked():
    """同一味药补上归经，这条规则就从"判不了"变成"判了"——两个状态可切换，
    说明它判的真是"有没有依据"，不是别的。"""
    with_meridian = Ontology(materia_rows=[
        _row("某药", "性味", "甘，平"), _row("某药", "功效", "补中益气"),
        _row("某药", "归经", "归脾、肺经"),
    ], formulary_rows=[], patterns=[])
    r = verify_formula(_s3(["某药"]), ontology=with_meridian)
    assert "meridian_coverage" in r.checked_rules
    assert not [x for x in r.unverifiable if x.rule == "meridian_coverage"]


def test_partially_verified_is_not_the_same_as_verified():
    """这两个 status 必须是两个值——合成一个的话"已验证通过"这句话在
    覆盖率只有一半的本体上依然成立，而那时它是假的。"""
    from core.formula_verifier import Unverifiable, VerificationResult

    assert VerificationResult(checked_rules=ALL_RULES).status == "verified"
    assert VerificationResult(
        unverifiable=(Unverifiable(rule="meridian_coverage", herbs=("x",),
                                   missing_predicate="归经", reason="没有归经"),),
    ).status == "partially_verified"
    assert "verified" != "partially_verified"


def test_the_unverifiable_reason_is_a_full_sentence_for_display():
    """前端与报告要把这句话**原样显示给人看**，所以 reason 是一句完整的话
    而不是一个代号。"""
    ont = Ontology(materia_rows=[_row("某药", "性味", "甘，平")],
                   formulary_rows=[], patterns=[])
    r = verify_formula(_s3(["某药"]), ontology=ont)
    for u in r.unverifiable:
        assert len(u.reason) >= 12, f"{u.rule} 的说明太短，显示出来看不懂：{u.reason}"
        assert u.missing_predicate.strip()


def test_the_result_dict_surfaces_unverifiable_to_the_api_layer():
    ont = Ontology(materia_rows=[_row("某药", "性味", "甘，平")],
                   formulary_rows=[], patterns=[])
    d = verify_formula(_s3(["某药"]), ontology=ont).to_dict()
    assert d["n_unverifiable"] >= 1
    assert d["unverifiable"][0]["missing_predicate"]
    assert d["passed"] is False and d["status"] == "partially_verified"


# ---------- 34b：两个分母 ----------

def test_the_two_ratios_have_different_denominators_and_say_so():
    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    cov = ontology_coverage_of_corpus(ontology=ont)
    refs = {"某药": [{"kind": "herb", "name": "某药", "predicate": "功效",
                      "span": "补中益气"}]}
    ratio = herbs_grounded_ratio(_s3(["某药", "白术"], refs=refs))
    assert ratio == 0.5, "分母是这张方的 2 味药"
    assert cov["n_ontology_herbs"] == len(ont.herbs)
    assert cov["n_corpus_herb_names"] != cov["n_ontology_herbs"]
    # 两个函数的 docstring 都要说清自己的分母
    assert "不是本体总药味数" in (herbs_grounded_ratio.__doc__ or "")
    assert "数据质量指标" in (ontology_coverage_of_corpus.__doc__ or "")


def test_coverage_by_name_and_by_occurrence_are_both_reported():
    cov = ontology_coverage_of_corpus()
    if not cov.get("available"):
        pytest.skip(cov.get("note", "语料不在"))
    assert cov["n_covered_names"] / cov["n_corpus_herb_names"] == pytest.approx(
        cov["coverage_by_name"], abs=1e-4)
    assert cov["n_covered_occurrences"] / cov["n_corpus_occurrences"] == pytest.approx(
        cov["coverage_by_occurrence"], abs=1e-4)


def test_coverage_returns_available_false_when_the_corpus_is_absent(tmp_path):
    """语料不在就说不在，不编一个 0.0 出来——0.0 会被读成"一味都没覆盖"。"""
    cov = ontology_coverage_of_corpus(cases_path=tmp_path / "没有这个文件.json")
    assert cov["available"] is False and "note" in cov
    assert "coverage_by_name" not in cov


def test_the_manifest_keeps_the_two_ratios_apart():
    """manifest 里 `ontology.corpus_coverage`（数据）与
    `synthesis.herbs_grounded_ratio`（模型）分两处，各自注明分母。"""
    from core.chain import _ontology_manifest

    m = _ontology_manifest()
    if not m.get("available"):
        pytest.skip("药理层数据不在")
    assert "corpus_coverage" in m and "missing_predicate_counts" in m
    src = (ROOT / "core" / "chain.py").read_text(encoding="utf-8")
    assert "herbs_grounded_denominator" in src, "synthesis 那边要标出分母"
    assert "本次方的药味数" in src


# ---------- 34c：方剂数先查清 ----------

def test_dump_formulas_prints_the_names_for_inspection():
    """34c 要的那条命令必须存在且能跑——"先看清楚再决定改不改"。"""
    r = subprocess.run([sys.executable, "-m", "core.ontology", "--dump-formulas"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    if "药理层数据不在" in r.stdout:
        pytest.skip("药理层数据不在")
    assert r.returncode == 0
    assert "--- 方名" in r.stdout
    assert len(r.stdout.splitlines()) > 50


def test_no_formula_name_carries_a_chapter_prefix():
    """34c 的两个假设之一：抽取时方名带了章节前缀。**实测不成立**
    ——0 条方名含数字或章节标记。这条测试把那个结论钉住。"""
    import re

    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    bad = [n for n in ont.formulas if re.search(r"[0-9第章节（）()]", n)]
    assert bad == [], f"这些方名带了章节标记：{bad[:10]}"


def test_no_two_formulas_were_merged_into_one_name():
    """34c 的另一个假设：方名归一把不同方并到一起了。**实测也不成立**
    ——名字里出现两个方剂后缀的只有 3 条，且都是正常的长方名
    （麻黄杏仁甘草石膏汤 / 竹叶石膏汤 / 大黄牡丹汤）。"""
    import re

    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    multi = [n for n in ont.formulas if len(re.findall(r"[汤散丸饮丹膏煎]", n)) >= 2]
    assert len(multi) <= 5, f"疑似并方的名字变多了：{multi}"
    for n in multi:
        assert len(n) >= 5, f"「{n}」只有 {len(n)} 字却带两个后缀，可能真的是并了两方"


def test_all_formulary_triples_land_in_a_formula():
    """3184 条三元组**一条不丢**地归到 235 首方——这是"235 不是归并丢了东西"
    的直接证据（34c 的结论就靠这一条）。"""
    import json

    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    path = ROOT / "data" / "standard" / "formulary.jsonl"
    raw = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
           if ln.strip()]
    n_refs = sum(len(rs) for f in ont.formulas.values() for rs in f.refs.values())
    assert n_refs == len(raw), (
        f"落盘 {len(raw)} 条三元组，归并后只剩 {n_refs} 条——有条目被丢掉了"
    )


def test_the_formula_predicates_are_the_eight_expected_ones():
    """谓词只有 8 种。方数少不是因为谓词被切碎，而是源文本里就这些方。"""
    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    preds = {p for f in ont.formulas.values() for p in f.refs}
    assert preds == {"组成", "主治", "功用", "君药", "臣药", "佐药", "使药", "加减"}


def test_the_formula_count_is_not_silently_changed_by_this_round():
    """34c：**只报数、先不改**（改归一会动已测量的数）。这条测试钉住本轮没改归一。

    235 这个数一旦变了，R38 消融里依赖方剂本体的那部分就不可比——
    改它必须是单独一轮，带前后对照。
    """
    ont = get_ontology()
    if not ont.available:
        pytest.skip("药理层数据不在")
    assert len(ont.formulas) == 235, (
        f"方剂数从 235 变成 {len(ont.formulas)}。如果这是有意的改动，"
        "请连同 R38 的可比性说明一起更新这条测试与报告第七节。"
    )
    assert len(ont.herbs) == 1232, f"本草数从 1232 变成 {len(ont.herbs)}"
