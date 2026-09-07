"""api/main.py 的 to_graph() 离线测试：验证边的两端节点都存在（round 5 的硬性验收点）。"""
import json
import subprocess
from pathlib import Path

from api.main import assert_graph_edges_valid, to_graph
from core.schemas import ElementHit, S1Normalize, S2Elements, S3Syndrome

ROOT = Path(__file__).resolve().parent.parent


def _make_results() -> list[dict]:
    s1_symptoms = ["纳差", "乏力", "口苦"]

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


def test_max_six_herbs_per_physician():
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    many_herbs = [f"药{i}" for i in range(10)]
    results = [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(elements=[]),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=many_herbs, cited_case_ids=["ye_tianshi-001"],
            ),
            "refs": [("ye_tianshi-001", 0.9)],
            "hallucinated": [],
        }
    ]
    graph = to_graph(s1, results)
    herb_nodes = [n for n in graph["nodes"] if n["data"]["id"].startswith("herb::ye_tianshi::")]
    assert len(herb_nodes) == 6


# ---------- 药名剥剂量：label 剥、id 不剥 ----------


def _single_physician_result(herbs: list[str]) -> list[dict]:
    return [
        {
            "physician": "ye_tianshi",
            "physician_name": "叶天士",
            "s2": S2Elements(elements=[]),
            "s3": S3Syndrome(
                syndrome="脾虚", reasoning="...", treatment_principle="健脾",
                herbs=herbs, cited_case_ids=["ye_tianshi-001"],
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
    不变是它成立的前提，这里钉住前提）。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, _single_physician_result(["党参三钱", "黄芪一两二钱", "生石膏"]))
    herb_nodes = {n["data"]["id"]: n["data"]["label"]
                  for n in graph["nodes"] if n["data"].get("layer") == 3}

    assert herb_nodes["herb::ye_tianshi::党参三钱"] == "党参"
    assert herb_nodes["herb::ye_tianshi::黄芪一两二钱"] == "黄芪"
    # 本来就没剂量的药名原样保留，不因为过了一遍剥离函数而被改写
    assert herb_nodes["herb::ye_tianshi::生石膏"] == "生石膏"
    # id 端到端保留原始写法（含剂量），前端反查证据靠的就是这个原始拼法不变
    assert set(herb_nodes.keys()) == {"herb::ye_tianshi::党参三钱",
                                       "herb::ye_tianshi::黄芪一两二钱",
                                       "herb::ye_tianshi::生石膏"}


def test_herb_node_label_falls_back_to_raw_when_stripping_empties_it():
    """脏数据兜底：万一某条 herb 整条都是剂量字符串（模型抽取错误），
    strip_dose_and_parens 会把它剥空。节点不能没有 label，退回原文本身
    比显示空白节点更诚实——至少看得出这条数据有问题。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, _single_physician_result(["三钱"]))
    herb_nodes = [n for n in graph["nodes"] if n["data"].get("layer") == 3]
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


def test_frontend_evidence_index_key_matches_backend_herb_node_id():
    """id 不变这个前提如果被破坏，点击图上的药物节点会打开空白侧栏——这条测试
    把后端 to_graph() 产出的 herb id 和前端真实 buildEvidenceIndex() 反查用的 id
    放在一起比，而不是分别测两边、假设它们拼法一致。

    药名故意带剂量（"党参三钱"），因为 bug 正是剂量还在 id 里的时候才会暴露：
    如果哪天有人图省事在 S3 序列化那层就把剂量剥掉了（id 和 label 一起改），
    这条测试能抓到——那样前后端两边基于同一个"herb"算出的 id 会各自正确但
    彼此对不上。"""
    s1 = S1Normalize(symptoms=["纳差"], tongue=None, pulse=None, unmapped=[])
    graph = to_graph(s1, _single_physician_result(["党参三钱"]))
    backend_herb_ids = {n["data"]["id"] for n in graph["nodes"] if n["data"].get("layer") == 3}

    frontend_data = {
        "results": [{
            "physician": "ye_tianshi", "physician_name": "叶天士", "color": "#000",
            "s2": {"elements": []},
            "s3": {"herbs": ["党参三钱"], "syndrome": "脾虚"},
            "refs": [], "hallucinated": [],
        }]
    }
    frontend_keys = set(_build_evidence_index(frontend_data))

    assert backend_herb_ids == {"herb::ye_tianshi::党参三钱"}
    assert backend_herb_ids <= frontend_keys, (
        f"后端节点 id {backend_herb_ids} 在前端 EVIDENCE 索引 {frontend_keys} 里找不到——"
        "点击这个节点会打开空白侧栏"
    )
