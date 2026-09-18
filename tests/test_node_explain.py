"""R37：节点释义（`core/node_explain.py` + `GET /api/node_explain`）。

**这个文件要钉住的是"零 LLM"和"取不到就说取不到"。** 一个图上点开就能看的解释，
最容易的写法是让模型现编一段——那正是这个项目从头到尾在防的事；第二容易的
写法是查不到时显示"暂无更多信息"，那句话占着位置、看起来像查过了。
"""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import node_explain as ne
from core.node_explain import SECTION_ORDER, explain_node, parse_node_id


# ---------- 一、零 LLM ----------

def test_the_module_never_touches_the_llm():
    """**源码层面的判据**：这个模块里不许出现任何 LLM 调用入口。
    "我记得没调" 不算判据——模块长起来之后没人记得。"""
    src = inspect.getsource(ne)
    for banned in ("get_llm", "generate(", "load_prompt", "OpenAI"):
        assert banned not in src, f"节点释义模块里出现了 {banned}——这一层必须零 LLM"


def test_the_endpoint_does_not_go_through_the_llm(monkeypatch):
    """接口层同样：把 `get_llm` 换成一个会炸的东西，释义照样出得来。"""
    def boom():
        raise AssertionError("节点释义不该调模型")

    monkeypatch.setattr("core.llm.get_llm", boom)
    client = TestClient(api_main.app)
    r = client.get("/api/node_explain", params={"node": "elem::脾"})
    assert r.status_code == 200 and r.json()["available"] is True


# ---------- 二、id 解析 ----------

@pytest.mark.parametrize("node_id,kind,name", [
    ("sym::纳差", "symptom", "纳差"),
    ("elem::脾", "element", "脾"),
    ("syn::ye_tianshi", "syndrome", "ye_tianshi"),
    ("formula::synthesis::四君子汤", "formula", "四君子汤"),
    ("herb::synthesis::四君子汤::党参", "herb", "党参"),
    ("case::ye_tianshi-0001-p0-0", "case", "ye_tianshi-0001-p0-0"),
])
def test_prefixes_map_to_kinds(node_id, kind, name):
    assert parse_node_id(node_id) == (kind, name)


@pytest.mark.parametrize("raw", ["", "  ", "党参", "unknown::x"])
def test_unparseable_ids_are_unknown_not_guessed(raw):
    kind, _name = parse_node_id(raw)
    assert kind == "unknown"


def test_the_name_override_wins_for_syndrome_nodes():
    """问诊图的证型节点 id 是 `syn::{physician}`（那个 id 是证据链反查的键，
    改不得），名字只在 label 里——所以调用方把 label 一起传过来。"""
    r = explain_node("syn::synthesis", name="肝胃不和证")
    assert r["kind"] == "syndrome" and r["title"] == "肝胃不和证"
    assert r["available"] is True


def test_the_override_strips_the_disease_prefix():
    """问诊图 layer 2 的 label 是「病名 · 证型」拼出来的，证候表里存的是证型名。"""
    r = explain_node("syn::synthesis", name="胃痛 · 肝胃不和证")
    assert r["title"] == "肝胃不和证"


def test_the_kind_still_comes_from_the_prefix():
    """名字可以覆盖，"这是什么东西"不行——两件事都让调用方定的话，
    一个写错的前缀会静默走到另一条查询分支上。"""
    r = explain_node("elem::脾", name="党参")
    assert r["kind"] == "element"


# ---------- 三、四节：固定顺序、空节不出现 ----------

def test_sections_are_a_subset_of_the_four_in_order():
    for node_id, name in [("herb::a::b::党参", None), ("elem::脾", None),
                          ("sym::纳差", None), ("syn::x", "肝胃不和证")]:
        r = explain_node(node_id, name=name)
        heads = [s["heading"] for s in r["sections"]]
        assert set(heads) <= set(SECTION_ORDER), heads
        order = [SECTION_ORDER.index(h) for h in heads]
        assert order == sorted(order), f"{node_id} 的四节顺序乱了：{heads}"


def test_every_section_carries_a_source():
    """一节没有出处就是一句没有出处的话——这个项目里那等于没有。"""
    r = explain_node("herb::a::b::党参")
    assert r["sections"]
    for s in r["sections"]:
        assert s.get("source"), s


def test_empty_sections_do_not_appear_as_empty_shells():
    r = explain_node("sym::一个不存在的怪症状")
    for s in r["sections"]:
        assert s["lines"] and all(ln.strip() for ln in s["lines"])


def test_a_case_node_is_declared_unavailable_with_a_reason():
    """医案节点的释义就是医案本文，走证据链侧栏。**说清为什么**，不静默返回空。"""
    r = explain_node("case::ye_tianshi-0001-p0-0")
    assert r["available"] is False and r["sections"] == []
    assert "证据链" in r["note"]


# ---------- 四、覆盖缺口要说成缺口，不说成"不存在" ----------

def test_a_herb_outside_the_ontology_says_the_ontology_is_incomplete():
    """本体覆盖医案语料里的药名约四成（R34b 实测 39.8%）。查不到一味药
    **不代表这味药不存在**，那句话必须写出来。"""
    r = explain_node("herb::a::b::此药本体里必定没有")
    what = [s for s in r["sections"] if s["heading"] == "是什么"][0]
    assert any("不代表" in ln for ln in what["lines"])


def test_a_herb_without_safety_rows_says_not_found_not_safe():
    """"安全表里没有这一条"跟"这味药没有风险"是两件事。"""
    r = explain_node("herb::a::b::此药本体里必定没有")
    notes = [s for s in r["sections"] if s["heading"] == "注意"][0]
    assert any("不是「没有风险」" in ln for ln in notes["lines"])


def test_the_syndrome_provenance_is_not_passed_off_as_a_quotation():
    """证候表的 `source` 是来源标签（official_consensus），不是可以当引文读的原话。"""
    r = explain_node("syn::x", name="肝胃不和证")
    src = [s for s in r["sections"] if s["heading"] == "出处原文"][0]
    assert any("不是逐字引文" in ln for ln in src["lines"])
    assert not any(ln.strip() == "official_consensus" for ln in src["lines"])


# ---------- 五、接口 ----------

def test_the_endpoint_returns_200_even_when_unavailable():
    """"这个节点没有释义"不是错误；4xx 会让前端把它当故障弹红条。"""
    client = TestClient(api_main.app)
    r = client.get("/api/node_explain", params={"node": "case::x"})
    assert r.status_code == 200 and r.json()["available"] is False


def test_the_endpoint_rejects_absurdly_long_input():
    client = TestClient(api_main.app)
    r = client.get("/api/node_explain", params={"node": "x" * 300})
    assert r.status_code == 400
    r2 = client.get("/api/node_explain", params={"node": "elem::脾", "name": "y" * 300})
    assert r2.status_code == 400


def test_the_endpoint_passes_the_name_override_through():
    client = TestClient(api_main.app)
    r = client.get("/api/node_explain",
                   params={"node": "syn::synthesis", "name": "肝胃不和证"})
    assert r.json()["title"] == "肝胃不和证"


def test_health_reports_the_s3_shape():
    """前端要在问诊开始之前就知道骨架是单链还是三列（R37）。"""
    client = TestClient(api_main.app)
    body = client.get("/health").json()
    assert body["s3_mode"] in ("structured", "legacy")


# ---------- 六、同一概念一处实现 ----------

def test_the_syndrome_table_lookup_has_one_implementation():
    """`api/main.py` 的证型标签补编码也要查证候表——**走 node_explain 那一处**，
    不另写一个 for 循环扫 jsonl（第 31 条）。"""
    src = inspect.getsource(api_main)
    assert "from core.node_explain import syndrome_row" in src
    # 判据是"有没有自己去读那个文件"，不是"注释里有没有提到它"——
    # api/main.py 的注释里确实提到 syndromes.jsonl（说 disease 字段的来源），
    # 那不是第二处实现。看的是读文件的动作。
    for bad in ("open(STANDARD_PATH", "syndromes.jsonl\").read", "读 syndromes"):
        assert bad not in src, f"api/main.py 里出现了自己读证候表的痕迹：{bad}"
    assert "_load_standard(" not in src, "api/main.py 不该直连证候表加载器"
