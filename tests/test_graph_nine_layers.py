"""R42：问诊图从五层改成**九层单链**，并且去掉医家分带。

## 这个文件钉住什么

三件事，而第三件是这一轮最容易出事的那件：

1. **九层的契约**：层号、层的机器名、层的中文名只有一张表（`CHAIN_LAYERS`），
   节点 id 前缀只有一张表（`LAYER_PREFIX`），缺层如实报（`missing_layers`）。
2. **去分带**：同名节点合并成一个，谁给的记在 `contributors` 里，
   `contributor_solo` / `multi_contributor` 由后端统一算。
3. **前端不写死层号的清单**。CLAUDE.md 那条 M5 教训原文：`to_graph()` 的
   Python 单测全绿，而前端 `growGraph()` 的 `nodesByLayer` 初始化漏了新加的
   layer 4 这个 key——数据是对的，渲染层的初始化没跟上，JSON 结构测试测不出。
   R42 把层数从 5 改成 9，同一个坑会再踩一次，所以这一轮的做法是**前端一个
   层号清单都不写**：层序全从后端下发的 `graph.layers` 取。
   下面那几条源码判据盯的正是这件事——它们不能替代 Playwright（真实渲染），
   但能在改坏的**那一刻**红，而不是等到跑浏览器。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import api.main as api_main
from api.main import CHAIN_LAYERS, LAYER_LABEL, LAYER_NODE_TYPE, LAYER_PREFIX, to_graph
from core.schemas import (
    ElementHit,
    FormulaCandidate,
    HerbItem,
    S1Normalize,
    S2Elements,
    S3Syndrome,
)

ROOT = Path(__file__).resolve().parent.parent
GRAPH_JS = (ROOT / "web" / "graph.js").read_text(encoding="utf-8")


def _s2():
    return S2Elements(elements=[
        ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"],
                   confidence="high"),
        ElementHit(element="气虚", kind="nature", supporting_symptoms=["乏力"],
                   confidence="high"),
    ])


def _s3(syndrome="脾胃气虚证", formula="四君子汤"):
    return S3Syndrome(
        syndrome=syndrome, reasoning="脾失健运", treatment_principle="健脾益气",
        cited_case_ids=["ye_tianshi-0001-p0-0"],
        formula_candidates=[FormulaCandidate(
            name=formula, source="classic", confidence="high", rationale="主治脾胃气虚",
            herb_items=[HerbItem(name="党参", dose=9.0, dose_unit="g", role="君")],
        )],
        selected=0,
    )


def _result(physician="ye_tianshi", pname="叶天士", **kw):
    return {"physician": physician, "physician_name": pname, "s2": _s2(),
            "s3": kw.pop("s3", None) or _s3(), "refs": [], "hallucinated": []}


def _graph(results=None, **kw):
    results = results or [_result()]
    return to_graph(S1Normalize(symptoms=["纳差", "乏力"]), results, _s2(), **kw)


# ---------- 一、九层的契约只有一张表 ----------

def test_there_are_exactly_nine_layers_numbered_zero_to_eight():
    assert [n for n, _, _ in CHAIN_LAYERS] == list(range(9))


def test_the_layer_tables_are_all_derived_from_one_source():
    """`LAYER_NODE_TYPE` / `LAYER_LABEL` / `LAYER_PREFIX` 三张表的键必须完全
    对得上 `CHAIN_LAYERS`——任一张漏一层，那一层的节点就带着 KeyError 崩掉
    （`add_node` 里直接下标取）。"""
    keys = {n for n, _, _ in CHAIN_LAYERS}
    assert set(LAYER_NODE_TYPE) == keys
    assert set(LAYER_LABEL) == keys
    assert set(LAYER_PREFIX) == keys


def test_every_layer_has_a_distinct_node_type_and_prefix():
    """两层共用一个 node_type 或一个前缀 = 那两层的节点在前端分不开
    （样式表按 node_type 选、释义面板按前缀判种类）。"""
    assert len(set(LAYER_NODE_TYPE.values())) == 9
    assert len(set(LAYER_PREFIX.values())) == 9


def test_the_backend_ships_the_layer_metadata_so_the_frontend_need_not_hardcode_it():
    g = _graph()
    assert [r["layer"] for r in g["layers"]] == list(range(9))
    for row in g["layers"]:
        assert row["label"] and row["node_type"]


def test_every_node_carries_layer_node_type_and_layer_label():
    for n in _graph()["nodes"]:
        d = n["data"]
        assert d["node_type"] == LAYER_NODE_TYPE[d["layer"]]
        assert d["layer_label"] == LAYER_LABEL[d["layer"]]


# ---------- 二、层的归属 ----------

def test_location_elements_go_to_the_organ_layer_and_natures_to_the_nature_layer():
    """原来两样挤在一个「证素」层里。它们回答的不是同一个问题：
    脏腑是病位（病在哪），病性是病的性质（寒热虚实）。"""
    g = _graph()
    by_id = {n["data"]["id"]: n["data"] for n in g["nodes"]}
    assert by_id["organ::脾"]["layer"] == 1
    assert by_id["nature::气虚"]["layer"] == 2


def test_the_treatment_principle_is_a_layer_not_an_edge_label():
    """改之前治法挂在 证型→方剂 那条边的 label 上——那条边因此同时表达
    "这个证用这个方"和"用什么治法"两件事，而图上看不出第二件。"""
    g = _graph()
    principles = [n["data"] for n in g["nodes"] if n["data"]["layer"] == 5]
    assert [p["label"] for p in principles] == ["健脾益气"]
    # 那条边上不许再有 label 把同一件事说第二遍
    for e in g["edges"]:
        assert "label" not in e["data"], e["data"]


def test_legacy_s3_reports_the_missing_layers_instead_of_faking_them():
    """legacy `S3Syndrome` 没有病机(4)和治法(6)——**那两层就是空的**，
    链条直接从证型接到治则、从治则接到方剂，并把层号记进 `missing_layers`。
    从 `reasoning` 里切一句话当病机是伪造。"""
    g = _graph()
    assert g["missing_layers"] == [2, 4, 6] or g["missing_layers"] == [4, 6], g["missing_layers"]
    assert 4 in g["missing_layers"] and 6 in g["missing_layers"]
    labels = [n["data"]["label"] for n in g["nodes"]]
    assert "脾失健运" not in labels, "reasoning 被当成病机塞进图里了"


def test_the_chain_is_connected_across_the_missing_layers():
    """缺层不能把链条断开：证型(3) 缺了病机(4) 之后要直接连到治则(5)。"""
    g = _graph()
    edges = {(e["data"]["source"], e["data"]["target"]) for e in g["edges"]}
    assert ("syn::脾胃气虚证", "principle::健脾益气") in edges
    assert ("principle::健脾益气", "formula::四君子汤") in edges


def test_herbs_are_compound_children_of_their_formula_not_separate_edges():
    g = _graph()
    herb = next(n["data"] for n in g["nodes"] if n["data"]["layer"] == 8)
    assert herb["parent"] == "formula::四君子汤"
    assert not any(e["data"]["source"] == "formula::四君子汤"
                   and e["data"]["target"] == herb["id"] for e in g["edges"]), (
        "方剂→药材既画了 parent 又画了边，图上会出现重复连线")


# ---------- 三、去分带：合并 + contributors ----------

def test_two_physicians_giving_the_same_syndrome_share_one_node():
    g = _graph([_result("ye_tianshi", "叶天士"), _result("wu_jutong", "吴鞠通")])
    syn = [n["data"] for n in g["nodes"] if n["data"]["layer"] == 3]
    assert len(syn) == 1, "同名证型没有合并——医家分带还在"
    assert set(syn[0]["contributors"]) == {"ye_tianshi", "wu_jutong"}


def test_different_syndromes_stand_side_by_side_in_the_same_layer():
    """并列不等于分带：它们在同一列上，上游连回同一批证素。"""
    g = _graph([_result("ye_tianshi", "叶天士"),
                _result("wu_jutong", "吴鞠通", s3=_s3("肝胃不和证", "柴胡疏肝散"))])
    syn = [n["data"] for n in g["nodes"] if n["data"]["layer"] == 3]
    assert len(syn) == 2
    for s in syn:
        ups = {e["data"]["source"] for e in g["edges"] if e["data"]["target"] == s["id"]}
        assert "organ::脾" in ups and "nature::气虚" in ups


def test_no_node_id_contains_a_physician_segment():
    """`syn::ye_tianshi` 那种 id 就是分带的形状。id 里带医家 = 同名节点必然分裂。"""
    for n in _graph([_result("ye_tianshi"), _result("wu_jutong", "吴鞠通")])["nodes"]:
        for seg in n["data"]["id"].split("::")[1:]:
            assert seg not in ("ye_tianshi", "wu_jutong"), n["data"]["id"]


def test_the_solo_and_multi_contributor_flags_are_computed_by_the_backend():
    """cytoscape 的选择器选不了"数组长度 > 1"，所以这两个标记必须后端算。
    前端按 contributors.length 现判就是同一个判断的第二处实现。"""
    g = _graph([_result("ye_tianshi", "叶天士"), _result("wu_jutong", "吴鞠通")])
    syn = next(n["data"] for n in g["nodes"] if n["data"]["layer"] == 3)
    assert syn.get("multi_contributor") is True
    assert "contributor_solo" not in syn
    single = _graph([_result("ye_tianshi", "叶天士")])
    syn1 = next(n["data"] for n in single["nodes"] if n["data"]["layer"] == 3)
    assert syn1["contributor_solo"] == "ye_tianshi"
    assert "multi_contributor" not in syn1


def test_the_shared_upstream_edges_are_emitted_once_not_per_physician():
    g = _graph([_result("ye_tianshi"), _result("wu_jutong", "吴鞠通")])
    keys = [(e["data"]["source"], e["data"]["target"]) for e in g["edges"]]
    assert len(keys) == len(set(keys)), "同一条边被几位医家各画了一遍"


def test_edge_ids_are_derived_from_their_endpoints():
    for e in _graph()["edges"]:
        d = e["data"]
        assert d["id"] == f"e::{d['source']}>>{d['target']}"


# ---------- 四、前端一个层号清单都不写（M5 那条教训的直接判据）----------

def _code_only(js: str) -> str:
    """去掉 `//` 行注释再搜。

    **跟 CLAUDE.md 那条数 `Field(min_length=1)` 的规矩同一个道理**：注释里提到
    一个形状（"M5 那一轮写成 {0:[],1:[]…}"）是在解释为什么不该这么写，
    连注释一起搜的话，写清楚原因反而会让判据变红——那会逼下一个人删注释。
    """
    return "\n".join(ln.split("//")[0] for ln in js.split("\n"))


def test_the_frontend_never_hardcodes_a_layer_list():
    """M5 的事故形状：`{0:[],1:[],2:[],3:[],4:[]}` 漏了新加的 key。
    R42 的做法是层序全从后端下发的 `layers` 取，所以这几个形状**一个都不许有**。"""
    code = _code_only(GRAPH_JS)
    banned = [
        r"\{\s*0:\s*\[\]",                 # nodesByLayer = {0:[],1:[]...}
        r"for\s*\(const layer of \[0, 1, 2, 3, 4\]\)",
        r"\[0,\s*1,\s*2,\s*3,\s*4\]\.forEach",
    ]
    for pat in banned:
        assert not re.search(pat, code), f"前端又写死了层号清单：{pat}"


def test_the_layer_order_comes_from_the_backend_payload():
    assert "function layerOrder(graph)" in GRAPH_JS
    assert "graph.layers || []" in GRAPH_JS
    body = GRAPH_JS.split("function layerOrder(graph)")[1][:700]
    assert "declared.length" in body, "没有「后端给了就用它」这一步"
    assert "n.data.layer" in body, "后端没给时不会从节点上现算，旧记录会一层都画不出"


def test_unknown_layer_numbers_do_not_crash_the_render():
    """层号缺失/越界只该"不参与排布"，不该整页崩（E4 那条）。"""
    body = GRAPH_JS.split("async function growGraph")[1][:4000]
    assert "Number.isFinite(L) ? L : -1" in body


def test_the_stagger_table_has_a_default_so_a_new_layer_cannot_be_dropped():
    assert "const LAYER_STAGGER_DEFAULT" in GRAPH_JS
    body = GRAPH_JS.split("async function growGraph")[1][:6000]
    assert "LAYER_STAGGER[layer] === undefined" in body, (
        "查不到间隔时没有回落到默认值——新加的层会在动画里被跳过")


def test_compound_parents_are_added_before_their_children():
    """方剂(7) 必须在君臣佐使(8) 之前加：cytoscape 的子节点靠 data.parent
    引用父节点 id，父节点不存在时 compound 关系渲染不出来。
    升序遍历层号就满足这一条——判据是"没有另写一条特例"。"""
    assert LAYER_NODE_TYPE[7] == "formula" and LAYER_NODE_TYPE[8] == "herb"
    body = GRAPH_JS.split("async function growGraph")[1][:6000]
    assert "sort((a, b) => a - b)" in body


def test_the_layer_bands_show_the_missing_layers_instead_of_hiding_them():
    assert "function renderLayerBands" in GRAPH_JS
    body = GRAPH_JS.split("function renderLayerBands")[1][:900]
    assert "missing_layers" in body and "is-missing" in body
    assert "本次没有" in body


# ---------- 五、patient 边界没变 ----------

@pytest.mark.parametrize("layer", [7, 8])
def test_patient_role_never_generates_the_prescription_layers(layer):
    g = _graph(role="patient")
    assert layer not in {n["data"]["layer"] for n in g["nodes"]}
    assert layer not in g["missing_layers"], (
        "刻意不生成的层被报成「缺层」，前端会去补")


def test_patient_role_still_gets_the_whole_upstream_chain():
    """不给方药**不等于**不给推理过程。"""
    g = _graph(role="patient")
    layers = {n["data"]["layer"] for n in g["nodes"]}
    assert {0, 1, 2, 3, 5} <= layers, sorted(layers)


def test_the_node_id_prefixes_are_the_same_words_the_explainer_uses():
    """`api.main.LAYER_PREFIX` 与 `core.node_explain._PREFIX_KIND` 必须说同一套词
    ——不一致的表现是点开那一层的节点一片空白（而"空白"跟"这个节点没有释义"
    在界面上长得一模一样）。"""
    from core.node_explain import _PREFIX_KIND

    missing = [p for p in LAYER_PREFIX.values() if p not in _PREFIX_KIND]
    assert not missing, missing


def test_to_graph_does_not_silently_drop_edges():
    """边被丢掉要计数上报——S2 改写了 supporting_symptoms 时边会整批消失，
    图上只是看起来"稀疏"，没人发现症状层和证素层已经断开。"""
    g = _graph()
    assert g["dropped_edges"] == 0
    src = api_main.to_graph.__doc__ or ""
    assert "patient" in src
