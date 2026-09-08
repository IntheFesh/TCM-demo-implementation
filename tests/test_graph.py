"""api/main.py 的 to_graph() 离线测试：验证边的两端节点都存在（round 5 的硬性验收点），
M5 起还验证 compound 节点的 parent 引用有效（同样归 assert_graph_edges_valid 管）。"""
import json
import subprocess
from pathlib import Path

from api.main import assert_graph_edges_valid, to_graph
from core.schemas import (
    ElementHit, FormulaCandidate, HerbItem, S1Normalize, S2Elements, S3Syndrome,
)

ROOT = Path(__file__).resolve().parent.parent


def _make_results() -> list[dict]:
    s2_ye = S2Elements(
        elements=[
            ElementHit(
                element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high"
            ),
            ElementHit(
                element="气虚", kind="nature", supporting_symptoms=["乏力"], confidence="medium"
            ),
        ],
        unexplained_symptoms=["口苦"],
    )
    s3_ye = S3Syndrome(
        syndrome="脾胃气虚",
        reasoning="纳差乏力，责之脾虚",
        treatment_principle="健脾益气",
        formula="四君子汤",
        herbs=["党参", "白术", "茯苓", "炙甘草"],
        cited_case_ids=["ye_tianshi-001"],
    )

    s2_wu = S2Elements(
        elements=[
            ElementHit(
                element="胃", kind="location", supporting_symptoms=["纳差"], confidence="high"
            ),
            # 故意让支撑症状带一个 s1.symptoms 里没有的措辞，验证边不会指向不存在的节点
            ElementHit(
                element="热", kind="nature", supporting_symptoms=["口苦口黏"], confidence="low"
            ),
        ],
        unexplained_symptoms=[],
    )
    s3_wu = S3Syndrome(
        syndrome="胃热",
        reasoning="纳差，责之胃热",
        treatment_principle="清胃泄热",
        herbs=["黄连", "黄芩"],
        cited_case_ids=["wu_jutong-001"],
    )

    return [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": s2_ye,
            "s3": s3_ye,
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        },
        {
            "physician": "wu_jutong",
            "physician_name": "吴鞠通",
            "s2": s2_wu,
            "s3": s3_wu,
            "refs": [("wu_jutong-001", 0.8)],
            "hallucinated": [],
        },
    ]


def test_graph_edges_all_point_to_existing_nodes():
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    graph = to_graph(s1, _make_results())
    assert_graph_edges_valid(graph)  # 不抛异常即通过


def test_symptom_not_matched_by_any_element_gets_dropped_edge_not_dangling():
    # s2_wu 的 supporting_symptoms 用了"口苦口黏"而不是 s1 里的"口苦"，
    # 这条边理应被静默丢弃，而不是指向一个不存在的症状节点。
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    graph = to_graph(s1, _make_results())
    node_ids = {n["data"]["id"] for n in graph["nodes"]}
    assert "sym::口苦口黏" not in node_ids
    for e in graph["edges"]:
        assert e["data"]["target"] != "sym::口苦口黏"
        assert e["data"]["source"] != "sym::口苦口黏"


def test_symptom_state_explained_vs_unexplained():
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    graph = to_graph(s1, _make_results())
    state_by_id = {n["data"]["id"]: n["data"]["state"] for n in graph["nodes"] if n["data"]["layer"] == 0}
    assert state_by_id["sym::纳差"] == "explained"
    assert state_by_id["sym::乏力"] == "explained"
    assert state_by_id["sym::口苦"] == "unexplained"  # 没被任何证素的 supporting_symptoms 精确命中


def test_element_nodes_deduplicated_across_physicians():
    # 两位医家都命中了名为"胃"/"脾"这类可能重复的证素时，节点应只出现一次
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    results = [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(
                elements=[
                    ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")
                ]
            ),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=["党参"], cited_case_ids=["ye_tianshi-001"],
            ),
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        },
        {
            "physician": "wu_jutong",
            "physician_name": "吴鞠通",
            "s2": S2Elements(
                elements=[
                    ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")
                ]
            ),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=["白术"], cited_case_ids=["wu_jutong-001"],
            ),
            "refs": [("wu_jutong-001", 0.9)],
            "hallucinated": [],
        },
    ]
    graph = to_graph(s1, results)
    elem_nodes = [n for n in graph["nodes"] if n["data"]["id"] == "elem::脾"]
    assert len(elem_nodes) == 1


def test_m5_no_longer_truncates_herbs_per_physician():
    """M5 契约变更：改造前"每位医家最多取 6 味"的截断，在候选方结构下不再成立
    ——一个候选方本来就该完整展示它的全部组成，截断会让方子看起来缺药。
    这条测试替换掉改造前的 test_max_six_herbs_per_physician（那条断言的是
    截断行为本身，M5 明确把截断去掉了，不是"改测试让它变绿"，是行为真的变了：
    10 味药的候选方现在应该产出 10 个药材节点，不是 6 个）。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    many_herbs = [HerbItem(name=f"药{i}") for i in range(10)]
    results = [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(elements=[]),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                formula_candidates=[FormulaCandidate(
                    name="十味方", source="composed", confidence="medium",
                    rationale="测试用大方", herb_items=many_herbs,
                )],
                cited_case_ids=["ye_tianshi-001"],
            ),
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        }
    ]
    graph = to_graph(s1, results)
    herb_nodes = [n for n in graph["nodes"] if n["data"].get("layer") == 4]
    assert len(herb_nodes) == 10


# ---------- 药名剥剂量：label 剥、id 不剥 ----------


def _single_physician_result(herbs: list[str], formula: str = "调补方") -> list[dict]:
    return [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(elements=[]),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                formula_candidates=[FormulaCandidate(
                    name=formula, source="composed", confidence="medium",
                    rationale="测试用", herb_items=[HerbItem(name=h) for h in herbs],
                )],
                cited_case_ids=["ye_tianshi-001"],
            ),
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        }
    ]


def test_herb_node_label_strips_dose_but_id_keeps_original_spelling():
    """节点挤成一团的根因：原来 label 直接拿整条"党参三钱"塞进去，90px 的
    text-max-width 一折就是好几行。label 剥掉剂量，但 id 必须原样保留——
    前端 buildEvidenceIndex() 用同样的原始写法拼 id 反查证据，id 一变
    点击侧栏就对不上了（这条不在这个 python 测试的能力范围内，但 id
    不变是它成立的前提，这里钉住前提）。

    M5 契约变更：herb id 从 herb::{phys}::{herb} 两段式变成
    herb::{phys}::{方名}::{herb} 三段式，layer 从 3（药材本身）变成 4
    （药材是方剂 layer 3 的 compound 子节点）——这条药名本身照原样带剂量的
    是 HerbItem.name 这个字段（M1 起本该是干净药名，但向后兼容合成路径/脏
    数据兜底场景下仍可能带着剂量文本，见 core.schemas._S3Base），不是这个
    测试自己要求的，钉住的是"不管 name 里有没有剂量，label 都要剥、id 都
    不能剥"这条规则本身。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, _single_physician_result(["党参三钱", "黄芪一两二钱", "生石膏"]))
    herb_nodes = {n["data"]["id"]: n["data"]["label"]
                  for n in graph["nodes"] if n["data"].get("layer") == 4}

    assert herb_nodes["herb::ye_tianshi::调补方::党参三钱"] == "党参"
    assert herb_nodes["herb::ye_tianshi::调补方::黄芪一两二钱"] == "黄芪"
    # 本来就没剂量的药名原样保留，不因为过了一遍剥离函数而被改写
    assert herb_nodes["herb::ye_tianshi::调补方::生石膏"] == "生石膏"
    # id 端到端保留原始写法（含剂量），前端反查证据靠的就是这个原始拼法不变
    assert set(herb_nodes.keys()) == {"herb::ye_tianshi::调补方::党参三钱",
                                       "herb::ye_tianshi::调补方::黄芪一两二钱",
                                       "herb::ye_tianshi::调补方::生石膏"}


def test_herb_node_label_falls_back_to_raw_when_stripping_empties_it():
    """脏数据兜底：万一某条 herb 整条都是剂量字符串（模型抽取错误），
    strip_dose_and_parens 会把它剥空。节点不能没有 label，退回原文本身
    比显示空白节点更诚实——至少看得出这条数据有问题。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, _single_physician_result(["三钱"]))
    herb_nodes = [n for n in graph["nodes"] if n["data"].get("layer") == 4]
    assert len(herb_nodes) == 1
    assert herb_nodes[0]["data"]["label"] == "三钱"


def _build_evidence_index(data: dict) -> dict:
    """用 node 跑 web/index.html 里真实上线的那份 <script>，调它的
    buildEvidenceIndex(data) 后取 EVIDENCE。测的是真实上线的代码，不是
    在测试里另抄一份反查逻辑——跟 test_western_drugs.py 里 _run_western_drugs_html
    是同一个模式。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    dom_stub = """
    const anyNode = new Proxy(function(){}, {
      get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
    });
    globalThis.document = anyNode;
    globalThis.window = anyNode;
    globalThis.cytoscape = anyNode;
    """
    js = (
        dom_stub
        + script
        + "\nbuildEvidenceIndex(" + json.dumps(data, ensure_ascii=False) + ");"
        + "\nprocess.stdout.write(JSON.stringify(Object.keys(EVIDENCE)));"
    )
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


def test_frontend_evidence_index_key_matches_backend_herb_and_formula_node_id():
    """id 不变这个前提如果被破坏，点击图上的方剂/药材节点会打开空白侧栏——
    这条测试把后端 to_graph() 产出的 id 和前端真实 buildEvidenceIndex() 反查用
    的 id 放在一起比，而不是分别测两边、假设它们拼法一致。

    药名故意带剂量（"党参三钱"），因为 bug 正是剂量还在 id 里的时候才会暴露：
    如果哪天有人图省事在 S3 序列化那层就把剂量剥掉了（id 和 label 一起改），
    这条测试能抓到——那样前后端两边基于同一个"herb"算出的 id 会各自正确但
    彼此对不上。

    frontend_data 的 s3 字段直接用真实 S3Syndrome.model_dump() 的产出（而不是
    手写一份 JSON 形状），跟 api/main.py::_serialize_result() 序列化 s3 的方式
    一字不差——手写形状迟早会跟 schema 真实字段名drift，这条测试要测的是
    "真实序列化产出" 和"真实前端反查逻辑"能不能对上，不是两边各自的想象。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    results = _single_physician_result(["党参三钱"])
    graph = to_graph(s1, results)
    backend_herb_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"].get("layer") == 4}
    backend_formula_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"].get("layer") == 3}

    r = results[0]
    s3 = r["s3"]
    frontend_data = {
        "results": [{
            "physician": r["physician"], "physician_name": r["physician_name"], "color": "#000",
            "s2": {"elements": []},
            "s3": {**s3.model_dump(), "cited_case_ids": list(s3.cited_case_ids)},
            "refs": [], "hallucinated": [],
        }]
    }
    frontend_keys = set(_build_evidence_index(frontend_data))

    assert backend_herb_ids == {"herb::ye_tianshi::调补方::党参三钱"}
    assert backend_formula_ids == {"formula::ye_tianshi::调补方"}
    assert backend_herb_ids <= frontend_keys, (
        f"后端药材节点 id {backend_herb_ids} 在前端 EVIDENCE 索引 {frontend_keys} 里找不到——"
        "点击这个节点会打开空白侧栏"
    )
    assert backend_formula_ids <= frontend_keys, (
        f"后端方剂节点 id {backend_formula_ids} 在前端 EVIDENCE 索引 {frontend_keys} 里找不到——"
        "点击这个节点会打开空白侧栏"
    )


# ---------- M4：layer 2 label 改成「病名 · 证型」，node id 不变 ----------


def test_layer2_label_is_disease_dot_syndrome_when_disease_present():
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    results = _make_results()
    results[0]["s3"].disease = "胃痛"
    graph = to_graph(s1, results)
    syn_node = next(n for n in graph["nodes"] if n["data"]["id"] == "syn::ye_tianshi")
    assert syn_node["data"]["label"] == "胃痛 · 脾胃气虚"


def test_layer2_label_falls_back_to_syndrome_when_disease_is_none():
    # disease 字段允许留空（S3 prompt 明确说"判断不了就填 null，不要硬凑"），
    # label 要退回只显示证型，不能拼出一个悬空的"None · 脾胃气虚"。
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    results = _make_results()
    assert results[0]["s3"].disease is None  # 前提：_make_results() 没有设置 disease
    graph = to_graph(s1, results)
    syn_node = next(n for n in graph["nodes"] if n["data"]["id"] == "syn::ye_tianshi")
    assert syn_node["data"]["label"] == "脾胃气虚"


def test_layer2_node_id_unchanged_by_disease_label():
    # id 是前端证据链侧栏反查的键，M4 只改 label、不能碰 id——跟 M 药名剥剂量
    # 那次「label 剥、id 保原样」是同一条约束。
    s1 = S1Normalize(symptoms=["纳差", "乏力", "口苦"], tongue="淡红", pulse="细弱", unmapped=[])
    results = _make_results()
    results[0]["s3"].disease = "胃痛"
    results[1]["s3"].disease = "胃热"  # 表外病名，label 该照样拼（label 不做校验，note 才做）
    graph = to_graph(s1, results)
    node_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"]["layer"] == 2}
    assert node_ids == {"syn::ye_tianshi", "syn::wu_jutong"}


# ---------- M5：六层图（症状/证素/病名·证型/方剂/药材，方剂-药材是 compound 关系）----------


def _multi_candidate_result(physician: str = "ye_tianshi", physician_name: str = "叶天士") -> dict:
    """一位医家、三个候选方（对齐真实 S3 prompt 要求的 2-3 个），其中两个候选方
    都用了"甘草"——这是本模块要测的关键场景：同一味药出现在不同候选方里，
    herb_id 必须带方剂名，不然会被 add_node 的去重逻辑错误合并。"""
    return {
        "physician": physician,
        "physician_name": physician_name,
        "s2": S2Elements(elements=[
            ElementHit(element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high"),
        ]),
        "s3": S3Syndrome(
            disease="胃痛", syndrome="脾胃气虚", reasoning="...", treatment_principle="健脾益气",
            selected=0,
            formula_candidates=[
                FormulaCandidate(
                    name="四君子汤", source="classic", confidence="high", rationale="经典方",
                    herb_items=[
                        HerbItem(name="党参"), HerbItem(name="白术"),
                        HerbItem(name="茯苓"), HerbItem(name="甘草"),
                    ],
                ),
                FormulaCandidate(
                    name="四君子汤加减", source="modified", base_formula="四君子汤",
                    confidence="medium", rationale="加减方",
                    herb_items=[HerbItem(name="党参"), HerbItem(name="甘草"), HerbItem(name="陈皮")],
                ),
                FormulaCandidate(
                    name="自拟健脾方", source="composed", confidence="low", rationale="自拟方",
                    herb_items=[HerbItem(name="黄芪"), HerbItem(name="山药")],
                ),
            ],
            cited_case_ids=["ye_tianshi-001"],
        ),
        "refs": [("ye_tianshi-001", 0.9)],
        "hallucinated": [],
    }


def test_six_layers_all_produced_with_correct_layer_numbers():
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, [_multi_candidate_result()])
    layers = {n["data"]["layer"] for n in graph["nodes"]}
    # layer 5 不存在——六层的编号是 0-4（第 5 层是"药材"本身，不是再加一层）
    assert layers == {0, 1, 2, 3, 4}


def test_all_three_candidates_produce_formula_nodes_not_just_selected():
    """M5 的核心卖点：图上要能摆出全部候选方，不是只画 selected 那一个——
    只画 selected 的话另外 1-2 个候选方在图上永远不可见，候选方对比就没了。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, [_multi_candidate_result()])
    formula_nodes = {n["data"]["id"]: n["data"] for n in graph["nodes"] if n["data"]["layer"] == 3}
    assert set(formula_nodes) == {
        "formula::ye_tianshi::四君子汤",
        "formula::ye_tianshi::四君子汤加减",
        "formula::ye_tianshi::自拟健脾方",
    }
    assert formula_nodes["formula::ye_tianshi::四君子汤"]["selected"] is True
    assert formula_nodes["formula::ye_tianshi::四君子汤加减"]["selected"] is False
    assert formula_nodes["formula::ye_tianshi::自拟健脾方"]["selected"] is False
    # source 三档如实带出来，前端靠这个字段区分边框
    assert formula_nodes["formula::ye_tianshi::四君子汤"]["source"] == "classic"
    assert formula_nodes["formula::ye_tianshi::四君子汤加减"]["source"] == "modified"
    assert formula_nodes["formula::ye_tianshi::自拟健脾方"]["source"] == "composed"


def test_same_herb_in_two_candidates_gets_two_different_ids_with_different_parents():
    """「甘草」同时出现在候选方一（四君子汤）和候选方二（四君子汤加减）里。
    herb_id 必须带方剂名，不然会被 add_node 的去重逻辑合并成一个节点、同时
    挂在两个 parent 上，cytoscape 会报错——这是 M5 spec 明确点名的易错点。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, [_multi_candidate_result()])
    gancao_nodes = [n["data"] for n in graph["nodes"]
                    if n["data"]["layer"] == 4 and n["data"]["id"].endswith("::甘草")]
    assert len(gancao_nodes) == 2
    ids = {n["id"] for n in gancao_nodes}
    assert ids == {"herb::ye_tianshi::四君子汤::甘草", "herb::ye_tianshi::四君子汤加减::甘草"}
    parents = {n["parent"] for n in gancao_nodes}
    assert parents == {"formula::ye_tianshi::四君子汤", "formula::ye_tianshi::四君子汤加减"}


def test_herb_parent_always_points_to_an_existing_formula_node():
    """parent 指向的方剂节点一定存在，不能有孤儿——这条不是走 edges 数组表达
    的关系，assert_graph_edges_valid() M5 起也检查它（见该函数文档字符串），
    这里额外显式断言一次，把"孤儿 parent"这个失败模式钉在测试名字上。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, [_multi_candidate_result()])
    formula_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"]["layer"] == 3}
    herb_parents = {n["data"]["parent"] for n in graph["nodes"] if n["data"]["layer"] == 4}
    assert herb_parents <= formula_ids
    assert_graph_edges_valid(graph)  # 不抛异常即通过（parent 校验也在这里面）


def test_no_explicit_formula_to_herb_edge_exists():
    """方剂 -> 药材的关系完全由 parent 字段（compound node）表达，不额外画边
    ——画了会在图上出现重复的连线，这是 M5 spec 原文明确警告的一条。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, [_multi_candidate_result()])
    formula_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"]["layer"] == 3}
    herb_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"]["layer"] == 4}
    for e in graph["edges"]:
        assert not (e["data"]["source"] in formula_ids and e["data"]["target"] in herb_ids), (
            f"发现一条方剂->药材的显式边，应该只靠 parent 表达 compound 关系：{e}"
        )


def test_syndrome_to_formula_edge_label_is_treatment_principle():
    """治法不单独成层，挂在 layer2->layer3 这条边的 label 上。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, [_multi_candidate_result()])
    syn_to_formula = [
        e["data"] for e in graph["edges"]
        if e["data"]["source"] == "syn::ye_tianshi"
        and e["data"]["target"].startswith("formula::")
    ]
    assert len(syn_to_formula) == 3  # 三个候选方各一条边
    assert all(e["label"] == "健脾益气" for e in syn_to_formula)


def test_safety_blocking_flag_passed_through_when_candidate_has_safety():
    """safety_blocking 为真时前端要标红——这里只测数据透传对不对，
    FormulaSafety.blocking 本身的判定逻辑是 M2 的范围，这里不重复测。"""
    from core.schemas import DoseViolation, FormulaSafety

    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    result = _multi_candidate_result()
    result["s3"].formula_candidates[0].safety = FormulaSafety(
        dose_violations=[DoseViolation(herb="细辛", dose=10.0, unit="g", limit_g=3.0, reason="细辛不过钱")],
    )
    result["s3"].formula_candidates[1].safety = FormulaSafety()  # 无问题，blocking=False
    graph = to_graph(s1, [result])
    formula_nodes = {n["data"]["id"]: n["data"] for n in graph["nodes"] if n["data"]["layer"] == 3}
    assert formula_nodes["formula::ye_tianshi::四君子汤"]["safety_blocking"] is True
    assert formula_nodes["formula::ye_tianshi::四君子汤加减"]["safety_blocking"] is False
    # 没算过 safety（safety is None）的候选方要如实报 False，不能报 None 或崩
    assert formula_nodes["formula::ye_tianshi::自拟健脾方"]["safety_blocking"] is False


def test_western_drug_herb_item_flagged_is_western():
    from core.schemas import FormulaCandidate as _FC
    from core.schemas import HerbItem as _HI

    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    result = _multi_candidate_result()
    result["s3"].formula_candidates[0] = _FC(
        name="中西合方", source="composed", confidence="low", rationale="张锡纯衷中参西",
        herb_items=[_HI(name="党参"), _HI(name="阿斯匹林")],
    )
    result["s3"].selected = 0
    graph = to_graph(s1, [result])
    herb_by_id = {n["data"]["id"]: n["data"] for n in graph["nodes"] if n["data"]["layer"] == 4}
    assert herb_by_id["herb::ye_tianshi::中西合方::党参"]["is_western"] is False
    assert herb_by_id["herb::ye_tianshi::中西合方::阿斯匹林"]["is_western"] is True
