"""R56 §6：「相似证型与鉴别点」——「本例知识地图」要回答的第一个问题
（"跟这个证容易混的是哪几个、怎么区分"）。零 LLM，纯集合运算，走的是
`core/node_explain.py::_differentiation_section`，用真实的
`data/standard/syndromes.jsonl`，不造假数据。
"""
from __future__ import annotations

from core.node_explain import SECTION_ORDER, _differentiation_section, explain_node
from core.tools import _load_standard


def _row(name: str, disease: str | None = None) -> dict:
    rows = _load_standard() or []
    for r in rows:
        if r.get("name") == name and r.get("disease") == disease:
            return r
    raise AssertionError(f"证候表里找不到 {name}／{disease}")


def test_same_disease_syndromes_are_preferred_over_location_nature_overlap():
    """临床鉴别诊断真正比的维度是同一个病名下的其他证型——"胃痛"底下有
    ≥4 条，不该退化到病位/病性兜底。"""
    row = _row("寒邪客胃证", "胃痛")
    rows = _load_standard() or []
    sec = _differentiation_section(row, rows)
    assert sec is not None
    assert all("（同病）" in line for line in sec["lines"])
    assert len(sec["lines"]) == 4


def test_differentiation_points_are_the_symmetric_difference_of_cardinal_symptoms():
    row = _row("寒邪客胃证", "胃痛")
    other = _row("饮食伤胃证", "胃痛")
    rows = _load_standard() or []
    sec = _differentiation_section(row, rows)
    line = next(ln for ln in sec["lines"] if ln.startswith("饮食伤胃证"))
    mine = set(row["cardinal_symptoms"])
    theirs = set(other["cardinal_symptoms"])
    for sym in sorted(mine - theirs):
        assert sym in line
    for sym in sorted(theirs - mine):
        assert sym in line


def test_falls_back_to_shared_location_or_nature_when_no_disease_or_too_few():
    """没有病名标注（或同病名条目不够）时退到病位/病性——不是"这个证没有
    可比对象"，是换了一种更弱的相似性判据，措辞里要说清楚是哪一种。"""
    row = {"name": "测试证", "disease": None, "location": ["脾"], "nature": ["气虚"],
          "cardinal_symptoms": ["纳差"]}
    rows = [row, {"name": "另一证", "disease": None, "location": ["脾"], "nature": [],
                 "cardinal_symptoms": ["乏力"]}]
    sec = _differentiation_section(row, rows)
    assert sec is not None
    assert "不同病" in sec["lines"][0]


def test_no_candidates_returns_none_not_an_empty_section():
    row = {"name": "孤证", "disease": None, "location": [], "nature": [],
          "cardinal_symptoms": []}
    assert _differentiation_section(row, [row]) is None


def test_identical_cardinal_symptoms_says_so_instead_of_an_empty_diff():
    row = {"name": "甲证", "disease": "某病", "location": [], "nature": [],
          "cardinal_symptoms": ["纳差"]}
    other = {"name": "乙证", "disease": "某病", "location": [], "nature": [],
            "cardinal_symptoms": ["纳差"]}
    sec = _differentiation_section(row, [row, other])
    assert "区分不出" in sec["lines"][0]


def test_the_section_is_wired_into_syndrome_node_explain():
    out = explain_node("syn::x", name="肝胃不和证")
    heads = [s["heading"] for s in out["sections"]]
    assert "相似证型与鉴别点" in heads
    idx = heads.index("相似证型与鉴别点")
    assert heads.index("病机") < idx < heads.index("药理")


def test_the_section_only_appears_for_syndrome_nodes():
    for node_id, name in [("herb::四君子汤::党参", None), ("formula::四君子汤", None),
                          ("organ::脾", None), ("sym::纳差", None)]:
        out = explain_node(node_id, name=name)
        heads = [s["heading"] for s in out["sections"]]
        assert "相似证型与鉴别点" not in heads, node_id


def test_the_section_is_registered_in_the_shared_order():
    assert "相似证型与鉴别点" in SECTION_ORDER


def test_the_source_string_has_no_path_leak():
    """R56 §6 第 7 条同一条纪律：source= 不带 core/ data/ .jsonl 这类内部路径。"""
    row = _row("寒邪客胃证", "胃痛")
    rows = _load_standard() or []
    sec = _differentiation_section(row, rows)
    for bad in ("core/", "data/", ".jsonl", ".py"):
        assert bad not in sec["source"]
