"""G1 工具层的离线测试。不需要网络、不需要 API key、秒级跑完——工具层是
确定性的，这一点本身就是设计约束（见 core/tools.py 模块文档字符串）。

医案三元组和医案检索库这两份数据在这个环境里没有，对应的用例用合成数据
（tmp_path + monkeypatch 路径）覆盖代码路径：读文件、按医家过滤、按症状匹配、
带 source_span 返回。这四条路径跟真实数据长什么样无关。
"""
import json

import pytest

from core import tools
from core.graph.store import NetworkXStore
from core.tools import (
    MIN_INFORMATION_GAIN,
    is_safety_relevant,
    SHIWEN_QUESTIONS,
    TOOLS,
    ask_user,
    check_residual,
    lookup_standard,
    phrase_question,
    query_case_graph,
    query_graph,
    question_candidates,
    run_tool,
    syndrome_posterior,
    tools_manifest,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    tools.reset_tool_caches()
    yield
    tools.reset_tool_caches()


# ---------- 注册表本身 ----------

def test_tools_registry_shape():
    assert set(TOOLS) == {
        "query_graph", "query_case_graph", "search_cases",
        "lookup_standard", "check_residual", "ask_user",
    }
    for key, spec in TOOLS.items():
        assert spec.name == key, "注册表的 key 必须等于 ToolSpec.name，否则模型按名字调不到"
        assert spec.description.strip()
        assert callable(spec.fn)


def test_tools_manifest_carries_parameters():
    manifest = {m["name"]: m for m in tools_manifest()}
    assert len(manifest) == len(TOOLS)
    props = manifest["query_graph"]["parameters"]["properties"]
    assert "node" in props and "edge_type" in props


def test_description_defined_only_in_registry():
    """工具描述只在 TOOLS 里定义一次。prompt 侧一旦手抄一份，模型看到的和代码
    里的就会分叉——这条钉住"prompt 里的工具清单由 tools_manifest() 渲染"。"""
    from pathlib import Path

    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    for yaml_file in prompts_dir.rglob("*.yaml"):
        text = yaml_file.read_text(encoding="utf-8")
        for spec in TOOLS.values():
            assert spec.description not in text, f"{yaml_file.name} 手抄了 {spec.name} 的描述"


# ---------- run_tool 的参数校验 ----------

def test_run_tool_unknown_name():
    out = run_tool("nonexistent_tool", {})
    assert "error" in out
    assert "query_graph" in out["available_tools"]


def test_run_tool_non_dict_args():
    assert "error" in run_tool("query_graph", ["口苦"])


@pytest.mark.parametrize("name,bad_args", [
    ("query_graph", {}),                                  # 缺必填 node
    ("query_graph", {"node": ""}),                        # min_length=1
    ("query_graph", {"node": "口苦", "limit": 0}),         # ge=1
    ("query_graph", {"node": "口苦", "limit": 999}),       # le=200
    ("search_cases", {"query": "胃痛"}),                   # 缺 physician
    ("search_cases", {"query": "胃痛", "physician": "ye_tianshi", "k": 99}),
    ("lookup_standard", {}),
    ("lookup_standard", {"query": ""}),
    ("check_residual", {"symptoms": []}),                 # min_length=1
    ("check_residual", {}),
    ("ask_user", {"question": "有没有口苦？"}),             # 缺 reason
    ("ask_user", {"question": "", "reason": "分不开"}),
    ("query_case_graph", {"limit": -1}),
])
def test_run_tool_rejects_bad_args_without_raising(name, bad_args):
    out = run_tool(name, bad_args)
    assert "error" in out, f"{name} 收到非法参数 {bad_args} 应该返回 error 而不是正常结果"
    assert "expected_parameters" in out


def test_run_tool_happy_path_returns_result_not_error():
    out = run_tool("ask_user", {"question": "有没有口苦？", "reason": "分不开湿热与虚寒"})
    assert out["terminate"] is True


# ---------- query_graph ----------

def test_query_graph_missing_node_returns_empty():
    out = query_graph("这个症状图里绝对没有")
    assert out["found"] is False
    assert out["neighbors"] == []


def test_query_graph_symptom_returns_indicates_out_edges():
    out = query_graph("两胁胀满")
    assert out["found"] is True
    assert out["node_type"] == "symptom"
    kinds = {n["edge_type"] for n in out["neighbors"]}
    assert kinds == {"indicates"}
    assert {n["name"] for n in out["neighbors"]} >= {"肝", "胃", "气滞"}
    assert all(n["weight"] is not None for n in out["neighbors"])


def test_query_graph_syndrome_needs_in_edges():
    """证候节点没有出边（composes 是 element->syndrome）。只返回出边的话
    「这个证候由哪些证素构成」永远是空——这正是 in_neighbors 存在的理由。"""
    out = query_graph("肝胃不和证")
    assert out["found"] is True
    composes = [n for n in out["neighbors"] if n["edge_type"] == "composes"]
    assert {n["name"] for n in composes} == {"肝", "胃", "气滞"}
    assert all(n["direction"] == "in" for n in composes)


def test_query_graph_accepts_raw_node_id():
    assert query_graph("element::脾")["found"] is True


def test_query_graph_edge_type_filter():
    out = query_graph("肝胃不和证", edge_type="indicates")
    assert out["neighbors"] == []


def test_query_graph_limit_truncates_but_reports_total():
    out = query_graph("element::脾", limit=2)
    assert len(out["neighbors"]) == 2
    assert out["total_neighbors"] > 2


def test_query_graph_physician_selects_weight():
    out = query_graph("两胁胀满", physician="ye_tianshi")
    assert all(isinstance(n["weight"], float) for n in out["neighbors"])


# ---------- query_case_graph（合成数据） ----------

TRIPLES = [
    {"case_id": "ye_tianshi-0001-p1-0", "physician": "ye_tianshi",
     "s": "患者", "p": "表现为", "o": "脘痛", "source_span": "脘痛不食，脉弦。"},
    {"case_id": "ye_tianshi-0001-p1-0", "physician": "ye_tianshi",
     "s": "脘痛", "p": "治以", "o": "疏肝和胃", "source_span": "此肝木犯胃，宜苦辛通降。"},
    {"case_id": "wu_jutong-0002-p1-0", "physician": "wu_jutong",
     "s": "患者", "p": "表现为", "o": "脘痛", "source_span": "胃痛甚，喜按。"},
    {"case_id": "wu_jutong-0002-p1-0", "physician": "wu_jutong",
     "s": "疏肝和胃", "p": "用药", "o": "柴胡", "source_span": "柴胡三钱。"},
]


@pytest.fixture
def triples_file(tmp_path, monkeypatch):
    path = tmp_path / "case_triples.jsonl"
    path.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in TRIPLES) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "CASE_TRIPLES_PATH", path)
    tools.reset_tool_caches()
    return path


def test_query_case_graph_missing_file_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "CASE_TRIPLES_PATH", tmp_path / "nope.jsonl")
    tools.reset_tool_caches()
    out = query_case_graph(symptom="脘痛")
    assert out["available"] is False
    assert out["triples"] == []
    assert "X3" in out["note"]


def test_query_case_graph_reads_file(triples_file):
    out = query_case_graph()
    assert out["available"] is True
    assert out["total_matched"] == 4


def test_query_case_graph_filters_by_physician(triples_file):
    out = query_case_graph(physician="wu_jutong")
    assert out["total_matched"] == 2
    assert {t["physician"] for t in out["triples"]} == {"wu_jutong"}


def test_query_case_graph_matches_symptom_in_subject_or_object(triples_file):
    out = query_case_graph(symptom="脘痛")
    assert out["total_matched"] == 3  # 两条宾语命中 + 一条主语命中


def test_query_case_graph_returns_source_span(triples_file):
    out = query_case_graph(symptom="脘痛", physician="ye_tianshi")
    assert all(t["source_span"] for t in out["triples"]), \
        "source_span 是「这条结论出自原文哪一句」的唯一凭据，不能丢"
    assert out["triples"][0]["source_span"].startswith("脘痛不食")


def test_query_case_graph_predicate_and_case_id_filters(triples_file):
    assert query_case_graph(predicate="用药")["total_matched"] == 1
    assert query_case_graph(case_id="ye_tianshi-0001-p1-0")["total_matched"] == 2


def test_query_case_graph_limit(triples_file):
    out = query_case_graph(limit=1)
    assert len(out["triples"]) == 1
    assert out["total_matched"] == 4


def test_query_case_graph_survives_bad_lines(tmp_path, monkeypatch):
    """三元组文件是机器生成的，一行坏了不该让整个工具不可用。"""
    path = tmp_path / "case_triples.jsonl"
    path.write_text(
        json.dumps(TRIPLES[0], ensure_ascii=False) + "\n{ 这不是 json\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "CASE_TRIPLES_PATH", path)
    tools.reset_tool_caches()
    out = query_case_graph()
    assert out["available"] is True
    assert out["total_matched"] == 1
    assert "无法解析" in out["note"]


# ---------- search_cases ----------

def test_search_cases_without_corpus_is_unavailable_not_crash():
    out = run_tool("search_cases", {"query": "胃脘胀痛", "physician": "ye_tianshi"})
    # 这个环境没有 cases.json；有的话就该正常返回
    assert out.get("available") in (True, False)
    if out["available"] is False:
        assert "cases.json" in out["note"]


# ---------- lookup_standard ----------

def test_lookup_standard_by_code_and_name():
    assert lookup_standard("SP-01")["definition"]["name"] == "肝胃不和证"
    assert lookup_standard("肝胃不和证")["definition"]["code"] == "SP-01"


def test_lookup_standard_partial_unique_match():
    out = lookup_standard("胃阴虚")
    assert out["found"] is True
    assert out["definition"]["name"] == "胃阴虚证"


def test_lookup_standard_not_found_lists_candidates():
    out = lookup_standard("完全不存在的证")
    assert out["found"] is False
    assert len(out["candidates"]) == 17


# ---------- check_residual ----------

def test_check_residual_splits_explained_and_unexplained():
    # 大便干结 出自胃阴虚证，指向 胃/阴虚；已知证素里两个都没有，所以未被解释
    out = check_residual(["两胁胀满", "大便干结"], ["肝", "气滞"])
    assert out["explained"] == ["两胁胀满"]
    assert out["unexplained"] == ["大便干结"]
    assert out["coverage"] == 0.5


def test_check_residual_off_graph_not_counted_as_unexplained():
    """图里查无此症 ≠ 没被证素解释。混为一谈会把覆盖率算成假数。"""
    out = check_residual(["两胁胀满", "夜寐多梦纷纭"], ["肝"])
    assert out["off_graph"] == ["夜寐多梦纷纭"]
    assert out["unexplained"] == []
    assert out["coverage"] == 1.0
    assert out["coverage_denominator"] == 1


def test_check_residual_no_elements_explains_nothing():
    out = check_residual(["两胁胀满"], [])
    assert out["explained"] == []
    assert out["coverage"] == 0.0


def test_check_residual_all_off_graph_gives_none_coverage():
    out = check_residual(["完全不着边的一句话"], ["肝"])
    assert out["coverage"] is None


def test_check_residual_matches_symptom_fragment():
    """患者说「胃脘胀满」，标准症状名是「胃脘胀满或疼痛」——不做片段匹配就对不上。"""
    out = check_residual(["胃脘胀满"], ["肝"])
    assert out["explained"] == ["胃脘胀满"]


# ---------- ask_user ----------

def test_ask_user_terminate_marker():
    out = ask_user("有没有口苦？", "湿热与虚寒分不开")
    assert out["terminate"] is True
    assert out["question"] == "有没有口苦？"
    assert out["reason"]


def test_only_ask_user_terminates():
    """终止标记只能由 ask_user 给出。别的工具意外带上 terminate，
    ReAct 循环会在还没查完的时候提前停。"""
    assert query_graph("两胁胀满").get("terminate") is None
    assert check_residual(["两胁胀满"], ["肝"]).get("terminate") is None
    assert lookup_standard("SP-01").get("terminate") is None


# ---------- 提问措辞 ----------

@pytest.mark.parametrize("symptom,expected", [
    ("口臭", "有没有口臭？"),
    ("胃脘胀满或疼痛", "有没有胃脘胀满或疼痛？"),
    ("每因情志不畅而发作或加重", "是否每因情志不畅而发作或加重？"),
    ("腹痛即泻，泻后痛减", "是否腹痛即泻，泻后痛减？"),
    ("腹部积块质软不坚，固定不移", "有没有这样的表现：腹部积块质软不坚，固定不移？"),
])
def test_phrase_question(symptom, expected):
    assert phrase_question(symptom) == expected


# ---------- 后验与信息增益 ----------

def _synthetic_store() -> NetworkXStore:
    """三个证候的小图，用来钉住信息增益的数学性质，不受真实数据变动影响。

      X 证：证素 {甲}，主症 s_x（只有它有）
      Y 证：证素 {甲}，主症 s_y（只有它有）
      Z 证：证素 {乙}，主症 s_z
      s_all：三个证候都列为主症
    """
    store = NetworkXStore()
    for code, elems, syms in [
        ("X", ["甲"], ["s_x", "s_all"]),
        ("Y", ["甲"], ["s_y", "s_all"]),
        ("Z", ["乙"], ["s_z", "s_all"]),
    ]:
        syn_id = f"syndrome::{code}"
        store.add_node(syn_id, node_type="syndrome", name=f"{code}证", code=code,
                       is_category=False)
        for e in elems:
            store.add_node(f"element::{e}", node_type="element", name=e)
            store.add_edge(f"element::{e}", syn_id, edge_type="composes", source="manual")
        for s in syms:
            store.add_node(f"symptom::{s}", node_type="symptom", name=s)
            for e in elems:
                store.add_edge(
                    f"symptom::{s}", f"element::{e}",
                    edge_key=f"indicates::{code}", edge_type="indicates",
                    source="manual", via_syndrome=code, is_cardinal=True,
                    weight_by_physician={"ye_tianshi": 1.0, "wu_jutong": 1.0},
                )
    return store


def test_posterior_uniform_without_elements():
    post = syndrome_posterior([], _synthetic_store())
    assert set(post) == {"X", "Y", "Z"}
    assert all(abs(p - 1 / 3) < 1e-9 for p in post.values())


def test_posterior_concentrates_on_matching_elements():
    post = syndrome_posterior(["甲"], _synthetic_store())
    assert post["X"] == pytest.approx(post["Y"])
    assert post["X"] > post["Z"]


def test_posterior_does_not_penalise_unobserved_elements():
    """追问阶段信息本来就不全，「该证候还要求一个我还没问到的证素」不该扣分——
    那个证素正是接下来要问出来的东西。"""
    store = NetworkXStore()
    store.add_node("syndrome::A", node_type="syndrome", name="A证", code="A", is_category=False)
    store.add_node("syndrome::B", node_type="syndrome", name="B证", code="B", is_category=False)
    for e, syn in [("甲", "A"), ("甲", "B"), ("乙", "B")]:
        store.add_node(f"element::{e}", node_type="element", name=e)
        store.add_edge(f"element::{e}", f"syndrome::{syn}", edge_type="composes", source="manual")
    post = syndrome_posterior(["甲"], store)
    assert post["A"] == pytest.approx(post["B"])


def test_ig_prefers_discriminating_symptom_over_universal_one():
    """s_all 三个证候都有 -> 答案不改变后验 -> 增益应当约等于 0，被过滤掉；
    s_x 只有 X 证有 -> 能真正分叉。"""
    out = question_candidates([], _synthetic_store(), k=5)
    symptoms = [c["symptom"] for c in out]
    assert "s_all" not in symptoms
    assert set(symptoms) == {"s_x", "s_y", "s_z"}


def test_ig_is_higher_when_question_splits_the_live_hypotheses():
    """已知证素「甲」把后验压到 X/Y 两个证候上。此时 s_x 能把它们分开，
    s_z 指向的 Z 证已经几乎不可能——前者的增益必须严格更高。"""
    store = _synthetic_store()
    out = {c["symptom"]: c["information_gain"] for c in question_candidates(["甲"], store, k=5)}
    assert out["s_x"] > out["s_z"]


def test_prior_entropy_drops_as_elements_accumulate():
    """证素越多，"到底是哪个证候"的不确定性越小。

    注意不能顺手断言"最高信息增益也随之下降"——那不成立：均匀分布在 3 个证候上
    时（H=1.585 bit）最好的二元问题只能切出 1:2 的不平衡划分（增益 0.65），
    收缩到 2 个证候后（H≈1.06 bit）同一个问题切的是接近 1:1 的划分（增益 0.71），
    反而更高。二元问题的增益上限是 1 bit，跟先验熵不是同向关系。"""
    store = _synthetic_store()
    assert question_candidates([], store, k=1)[0]["prior_entropy"] > \
        question_candidates(["甲"], store, k=1)[0]["prior_entropy"]


def test_ig_never_exceeds_its_upper_bounds():
    """真正的单调性约束：一个二元问题的信息增益不可能超过先验熵，也不可能
    超过 1 bit（答案只有"有/没有"两种）。任何一条被违反都说明公式写错了。"""
    for elements in ([], ["甲"], ["肝", "胃", "气滞"]):
        store = _synthetic_store() if elements != ["肝", "胃", "气滞"] else None
        for c in question_candidates(elements, store, k=10):
            if c["source"] != "graph_ig":
                continue
            assert 0 < c["information_gain"] <= min(c["prior_entropy"], 1.0) + 1e-9


def test_yes_and_no_branches_point_to_different_syndromes():
    top = question_candidates(["甲"], _synthetic_store(), k=1)[0]
    assert top["if_yes_top"] != top["if_no_top"], "分不了叉的问题不该排在第一"


def test_candidates_respect_k_and_are_sorted():
    out = question_candidates([], _synthetic_store(), k=2)
    assert len(out) == 2
    assert out[0]["information_gain"] >= out[1]["information_gain"]


def test_known_and_asked_symptoms_are_excluded():
    store = _synthetic_store()
    assert "s_x" not in [c["symptom"] for c in question_candidates([], store, k=5,
                                                                  known_symptoms=["s_x"])]
    assert "s_y" not in [c["symptom"] for c in question_candidates([], store, k=5,
                                                                   asked=["s_y"])]


def test_known_symptom_substring_also_excluded():
    """患者说「胃脘胀满」，就不该再问「有没有胃脘胀满或疼痛？」。"""
    out = question_candidates(["肝", "胃", "气滞"], k=5, known_symptoms=["胃脘胀满"])
    assert "胃脘胀满或疼痛" not in [c["symptom"] for c in out]


def test_safety_relevant_flag_uses_the_safety_layer():
    """一旦患者答"有"就会触发 S2 之前安全否决的问题，必须被标出来——G3 收到
    答案后要先跑 check_safety 再更新后验，不能当成普通症状喂回去。判据复用
    core/safety.py 的表，这里只钉住"确实被标出来了"。"""
    assert is_safety_relevant("吐血色红或紫黯，常夹食物残渣") is True
    assert is_safety_relevant("便血") is True
    assert is_safety_relevant("口干或口苦") is False

    out = question_candidates(["胃", "阴虚", "津伤", "热"], k=5)
    flags = {c["symptom"]: c["safety_relevant"] for c in out if c["source"] == "graph_ig"}
    assert any(flags.values()), "胃热壅盛证的吐血主症应当进候选并被标为安全相关"
    assert not all(flags.values())


def test_fallback_entries_carry_no_safety_flag_key_confusion():
    """后备条目没有对应的标准症状，safety_relevant 也就无从谈起——
    保持 graph_ig 分支独有，避免下游拿一个恒为 False 的假字段做判断。"""
    out = question_candidates([], None, k=1)
    assert "safety_relevant" not in out[0] or out[0].get("symptom") is not None


def test_known_symptom_exclusion_reuses_residual_matcher():
    """排除已知症状和 check_residual 用的是同一个片段匹配器。同一个模块里两套
    "这条症状算不算已经知道了"的判断迟早分叉，所以这条钉住它们一致。"""
    known = ["大便溏薄"]
    excluded = {c["symptom"] for c in question_candidates(["脾", "气虚", "湿"], k=20,
                                                          known_symptoms=known)}
    matched = {
        (tools.get_graph_store().get_node(i) or {}).get("name")
        for i in tools._match_graph_symptoms(tools.get_graph_store(), "大便溏薄")
    }
    assert matched, "「大便溏薄」应当能匹配到图里的标准症状节点"
    assert not (matched & excluded), f"匹配器认得的症状不该还出现在候选里：{matched & excluded}"


def test_deterministic():
    a = question_candidates(["肝", "胃", "气滞"], k=3)
    b = question_candidates(["肝", "胃", "气滞"], k=3)
    assert a == b


# ---------- 十问歌后备的 5 条触发条件 ----------

def _is_fallback(out):
    return out and all(c["source"] == "shiwen_fallback" for c in out)


def test_fallback_when_graph_missing(monkeypatch, tmp_path):
    """触发条件 1：图谱文件不存在。"""
    monkeypatch.setattr(tools, "GRAPH_PATH", tmp_path / "nope.json")
    tools.reset_tool_caches()
    out = question_candidates(["肝"], k=3)
    assert _is_fallback(out)
    assert "图谱不可用" in out[0]["fallback_reason"]


def test_fallback_when_no_hypothesis_space():
    """触发条件 2：图里没有非类目证候节点 / 没有 indicates 边。"""
    store = NetworkXStore()
    store.add_node("syndrome::C", node_type="syndrome", name="类目词", code="C",
                   is_category=True)
    out = question_candidates([], store, k=3)
    assert _is_fallback(out)
    assert "假设空间" in out[0]["fallback_reason"]


def test_fallback_when_candidate_pool_exhausted():
    """触发条件 3：图里的标准症状全部已问过或已由患者陈述。"""
    store = _synthetic_store()
    out = question_candidates([], store, k=3, asked=["s_x", "s_y", "s_z", "s_all"])
    assert _is_fallback(out)
    assert "已全部问过" in out[0]["fallback_reason"]


def test_fallback_when_posterior_collapsed():
    """触发条件 4：后验塌缩到单一证候，任何问题增益都是 0。"""
    store = NetworkXStore()
    store.add_node("syndrome::A", node_type="syndrome", name="A证", code="A", is_category=False)
    store.add_node("element::甲", node_type="element", name="甲")
    store.add_edge("element::甲", "syndrome::A", edge_type="composes", source="manual")
    store.add_node("symptom::s1", node_type="symptom", name="s1")
    store.add_edge("symptom::s1", "element::甲", edge_type="indicates", source="manual",
                   via_syndrome="A", is_cardinal=True,
                   weight_by_physician={"ye_tianshi": 1.0, "wu_jutong": 1.0})
    out = question_candidates([], store, k=3)
    assert _is_fallback(out)
    assert "塌缩" in out[0]["fallback_reason"]


def test_fallback_when_all_gains_are_zero():
    """触发条件 5：候选证候的症状集合完全重合，图上区分不了它们。"""
    store = NetworkXStore()
    for code in ("A", "B"):
        store.add_node(f"syndrome::{code}", node_type="syndrome", name=f"{code}证",
                       code=code, is_category=False)
        store.add_node("element::甲", node_type="element", name="甲")
        store.add_edge("element::甲", f"syndrome::{code}", edge_type="composes", source="manual")
        store.add_node("symptom::s1", node_type="symptom", name="s1")
        store.add_edge("symptom::s1", "element::甲", edge_key=f"indicates::{code}",
                       edge_type="indicates", source="manual",
                       via_syndrome=code, is_cardinal=True,
                       weight_by_physician={"ye_tianshi": 1.0, "wu_jutong": 1.0})
    out = question_candidates([], store, k=3)
    assert _is_fallback(out)
    assert "约等于 0" in out[0]["fallback_reason"]


def test_fallback_follows_shiwen_order_and_skips_asked_topics(monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "GRAPH_PATH", tmp_path / "nope.json")
    tools.reset_tool_caches()
    out = question_candidates([], None, k=3, asked=["寒热"])
    assert [c["topic"] for c in out] == ["汗", "头身", "二便"]
    assert [t for t, _ in SHIWEN_QUESTIONS][:2] == ["寒热", "汗"]


def test_fallback_entries_have_no_fake_numbers(monkeypatch, tmp_path):
    """后备问题没有信息增益可言，必须留 None——填一个假数比不填危害大。"""
    monkeypatch.setattr(tools, "GRAPH_PATH", tmp_path / "nope.json")
    tools.reset_tool_caches()
    out = question_candidates([], None, k=3)
    assert all(c["information_gain"] is None and c["p_yes"] is None for c in out)


def test_min_information_gain_is_a_small_positive_threshold():
    assert 0 < MIN_INFORMATION_GAIN < 1e-3
