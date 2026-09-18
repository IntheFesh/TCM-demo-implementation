"""R37/R42：节点释义（`core/node_explain.py` + `GET /api/node_explain`）。

**这个文件要钉住的是"零 LLM"和"取不到就说取不到"。** 一个图上点开就能看的解释，
最容易的写法是让模型现编一段——那正是这个项目从头到尾在防的事；第二容易的
写法是查不到时显示"暂无更多信息"，那句话占着位置、看起来像查过了。

R42 把四节扩成八节，并把三张"节点 id 前缀"表放在一起比。前缀表那条是这一轮
新加的：R42 之前 `_PREFIX_KIND` 只认问诊图那一套，持久图的 `element::` /
`syndrome::` / `symptom::` 一个都不认——于是在图谱浏览器里点一个节点，
`parse_node_id` 判成 unknown、面板整块隐藏，而"隐藏"跟"这个节点没有释义"
在界面上长得一模一样。**这类 bug 单看任何一侧的代码都看不出来**，
只有把两张表摆在一起才看得出。
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
    # R42 九层：证素拆成病位(organ)/病性(nature) 两层，两个前缀同归 element
    # ——"这是什么东西"没变，变的只是它摆在第几层。
    ("organ::脾", "element", "脾"),
    ("nature::气虚", "element", "气虚"),
    ("syn::肝胃不和证", "syndrome", "肝胃不和证"),
    ("mech::脾失健运", "pathogenesis", "脾失健运"),
    ("principle::健脾化湿", "principle", "健脾化湿"),
    ("method::益气健脾", "method", "益气健脾"),
    ("formula::四君子汤", "formula", "四君子汤"),
    ("herb::四君子汤::党参", "herb", "党参"),
    # 持久知识图谱那一套前缀（offline/build_graph.py 写进 graph.json 的）
    ("element::肝", "element", "肝"),
    ("syndrome::TB-001", "syndrome", "TB-001"),
    ("symptom::纳呆", "symptom", "纳呆"),
    ("case::ye_tianshi-0001-p0-0", "case", "ye_tianshi-0001-p0-0"),
])
def test_prefixes_map_to_kinds(node_id, kind, name):
    assert parse_node_id(node_id) == (kind, name)


def test_the_consult_graph_prefix_table_is_fully_covered():
    """**三张表比一遍。** `api.main.LAYER_PREFIX` 里的每个前缀都必须在
    `_PREFIX_KIND` 里有一条——漏一个的表现是那一层的节点点开一片空白。

    不反向要求相等：`_PREFIX_KIND` 是这两套 id 的并集（还含持久图那一套
    和已退役的 `elem`），本来就更大。"""
    import api.main as m

    missing = [pfx for pfx in m.LAYER_PREFIX.values() if pfx not in ne._PREFIX_KIND]
    assert not missing, f"问诊图这些前缀在 _PREFIX_KIND 里查不到：{missing}"


def test_the_persistent_graph_prefix_table_is_fully_covered():
    """持久图那一套同理。前缀从 data/graph.json 的真实 id 里现取，
    **不手抄**——手抄的表跟生成物漂了就查不出来。"""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "data" / "graph.json"
    if not path.exists():
        pytest.skip("这个仓库应该带 data/graph.json，没有就跳过")
    data = json.loads(path.read_text(encoding="utf-8"))
    prefixes = {str(n.get("id", "")).split("::")[0]
                for n in data["nodes"] if "::" in str(n.get("id", ""))}
    missing = sorted(p for p in prefixes if p not in ne._PREFIX_KIND)
    assert not missing, f"持久图这些前缀在 _PREFIX_KIND 里查不到：{missing}"


def test_every_kind_has_a_builder():
    """`NodeKind` 多一个值、`explain_node` 的 builders 表忘了加，表现是 KeyError
    ——那是 500，前端会弹红条。**这条比"手动测几个节点"可靠**。"""
    import typing

    kinds = set(typing.get_args(ne.NodeKind)) - {"unknown", "case"}
    # builders 是函数体里的局部变量，从源码取它的键（这一层没必要为了可测把它提到模块级）
    src = inspect.getsource(ne.explain_node)
    for k in kinds:
        assert f'"{k}": lambda' in src, f"explain_node 的 builders 里没有 {k}"


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
    r = explain_node("organ::脾", name="党参")
    assert r["kind"] == "element"


# ---------- 三、四节：固定顺序、空节不出现 ----------

#: R42 要求的那五节。**这张清单写在测试里，不是写在实现里**——
#: 实现里那张 `SECTION_ORDER` 是"允许出现哪些节"，这张是"这一轮必须有哪些节"，
#: 两者混成一个常量的话，删掉一节实现会连测试一起变绿。
R42_REQUIRED_SECTIONS = ("病机", "药理", "名老中医经验", "验证结果", "循证对照")


def test_sections_are_a_subset_of_the_eight_in_order():
    for node_id, name in [("herb::b::党参", None), ("organ::脾", None),
                          ("sym::纳差", None), ("syn::x", "肝胃不和证"),
                          ("formula::四君子汤", None), ("mech::脾失健运", None),
                          ("principle::健脾化湿", None), ("method::益气健脾", None)]:
        r = explain_node(node_id, name=name)
        heads = [s["heading"] for s in r["sections"]]
        assert set(heads) <= set(SECTION_ORDER), heads
        order = [SECTION_ORDER.index(h) for h in heads]
        assert order == sorted(order), f"{node_id} 的八节顺序乱了：{heads}"


def test_the_five_r42_sections_all_really_show_up_somewhere():
    """R42 要的五节**每一节都得在某类节点上真的出现过**。
    只在 SECTION_ORDER 里加一个字符串、没有任何 builder 产出它，
    上一条（子集 + 顺序）照样绿。"""
    seen: set[str] = set()
    for node_id, name in [("herb::b::党参", None), ("organ::脾", None),
                          ("nature::气虚", None), ("sym::纳差", None),
                          ("syn::x", "肝胃不和证"), ("formula::四君子汤", None),
                          ("mech::脾失健运，湿浊内生", None),
                          ("principle::健脾化湿", None), ("method::益气健脾", None)]:
        seen |= {s["heading"] for s in explain_node(node_id, name=name)["sections"]}
    missing = [h for h in R42_REQUIRED_SECTIONS if h not in seen]
    assert not missing, f"R42 要的这几节一个节点上都没出现：{missing}"


def test_the_renamed_section_is_the_old_one_not_a_new_one():
    """「名医怎么用」→「名老中医经验」是**改名**，不是新增一节。
    两个标题同时存在就说明有人把旧的留下、又加了一个新的（同一概念两处实现）。"""
    assert "名老中医经验" in SECTION_ORDER
    assert "名医怎么用" not in SECTION_ORDER
    # **只看会产出一节的那行代码**，不看文档字符串——模块文档里必须能提到旧名
    # （改名这件事本身要留下痕迹），而 `_section("名医怎么用"` 才是"还留着旧的
    # 那一节"的判据。
    src = inspect.getsource(ne)
    assert '_section("名医怎么用"' not in src, "实现里还留着旧标题那一节"


def test_every_section_carries_a_source():
    """一节没有出处就是一句没有出处的话——这个项目里那等于没有。
    **八类节点都要过一遍**：R42 新增的三类（病机/治则/治法）最容易漏。"""
    for node_id, name in [("herb::b::党参", None), ("organ::脾", None),
                          ("sym::纳差", None), ("syn::x", "肝胃不和证"),
                          ("formula::四君子汤", None), ("mech::脾失健运", None),
                          ("principle::健脾化湿", None), ("method::益气健脾", None)]:
        r = explain_node(node_id, name=name)
        assert r["sections"], node_id
        for s in r["sections"]:
            assert s.get("source"), (node_id, s)


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
    r = explain_node("herb::b::此药本体里必定没有")
    what = [s for s in r["sections"] if s["heading"] == "是什么"][0]
    assert any("不代表" in ln for ln in what["lines"])


def test_a_herb_without_safety_rows_says_not_found_not_safe():
    """"安全表里没有这一条"跟"这味药没有风险"是两件事。"""
    r = explain_node("herb::b::此药本体里必定没有")
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
    assert body["s3_mode"] in ("derived", "structured", "legacy")


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
