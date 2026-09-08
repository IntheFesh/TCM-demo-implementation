"""web/index.html 里 hover tooltip 这部分前端代码的离线测试。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_stream_frontend.py /
test_western_drugs.py 同一个模式），测 describeNodeTooltip / describeEdgeTooltip
这两个纯函数：给一份 cytoscape 节点/边的 data()，应该出什么文字。这两个函数
同时服务两种数据形状——per-consult 图（模块6，靠 layer 字段分支）和图谱浏览器
的持久知识图谱（模块7，靠 node_type/edge_type 字段分支），复用同一个函数、
同一份测试文件，不为图谱浏览器另起一套 tooltip 逻辑或另一个测试文件。

不测真实鼠标 hover 事件本身（cytoscape 的 mouseover/mousemove/mouseout 绑定
逻辑）——那要么得起真浏览器（Playwright），要么得深度 mock cytoscape 的事件
系统，两者都测不出比"这几行 cy.on(...) 调用对不对"更多的信息，而这几行本身
很短、读代码就能确认对不对，不值得为它单独搭一套 cytoscape 事件模拟。
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
"""


def _run_node(js_tail: str) -> str:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    proc = subprocess.run(
        ["node", "-e", DOM_STUB + script + "\n" + js_tail],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _describe_node(data: dict, physician_names: dict | None = None) -> str:
    js = (
        f"PHYSICIAN_NAMES = {json.dumps(physician_names or {}, ensure_ascii=False)};\n"
        f'process.stdout.write(describeNodeTooltip({json.dumps(data, ensure_ascii=False)}));'
    )
    return _run_node(js)


def _describe_edge(data: dict, source_label: str, target_label: str,
                   physician_names: dict | None = None, current_physician: str | None = None) -> str:
    js = (
        f"PHYSICIAN_NAMES = {json.dumps(physician_names or {}, ensure_ascii=False)};\n"
        f'process.stdout.write(describeEdgeTooltip({json.dumps(data, ensure_ascii=False)}, '
        f'{json.dumps(source_label, ensure_ascii=False)}, {json.dumps(target_label, ensure_ascii=False)}, '
        f'{json.dumps(current_physician, ensure_ascii=False)}));'
    )
    return _run_node(js)


# ---------- 五种节点类型（对应 to_graph() 的 layer 0-4，M5 把原来的层 3
# "用药" 拆成方剂(3)+药材(4) 两层）----------


def test_symptom_node_shows_label_and_state():
    for state, expect in [("explained", "已解释"), ("residual", "残差辨证补充解释"), ("unexplained", "未解释")]:
        out = _describe_node({"id": "sym::纳差", "label": "纳差", "layer": 0, "state": state})
        assert "症状" in out
        assert "纳差" in out
        assert expect in out


def test_element_node_shows_location_vs_nature_and_residual_flag():
    out = _describe_node({"id": "elem::脾", "label": "脾", "layer": 1, "kind": "location"})
    assert "证素" in out and "脾" in out and "病位" in out
    assert "残差" not in out

    out = _describe_node({"id": "elem::气虚", "label": "气虚", "layer": 1, "kind": "nature", "residual": True})
    assert "病性" in out
    assert "残差辨证补充" in out


def test_syndrome_node_shows_physician_name_from_pname_field():
    """syn:: 节点的 data 里 to_graph() 本来就带了 pname，直接用，不用查
    PHYSICIAN_NAMES 这张前端补的表。"""
    out = _describe_node({"id": "syn::ye_tianshi", "label": "脾胃气虚", "layer": 2,
                          "phys": "ye_tianshi", "pname": "叶天士"})
    assert "证型" in out and "脾胃气虚" in out and "叶天士" in out


def test_formula_node_shows_source_label_and_selected_and_safety_flags():
    """M5：layer 3 从"用药"改成"方剂"，source 三档要翻成中文，selected/
    safety_blocking 两个布尔标记要各自出一句人话，不是原样打印 true/false。"""
    out = _describe_node({
        "id": "formula::ye_tianshi::柴胡疏肝散", "label": "柴胡疏肝散", "layer": 3,
        "phys": "ye_tianshi", "pname": "叶天士", "source": "classic",
        "selected": True, "safety_blocking": False,
    })
    assert "方剂" in out and "柴胡疏肝散" in out and "叶天士" in out
    assert "经典方" in out
    assert "已选" in out
    assert "安全拦截" not in out

    out2 = _describe_node({
        "id": "formula::ye_tianshi::柴胡疏肝散加减", "label": "柴胡疏肝散加减", "layer": 3,
        "phys": "ye_tianshi", "pname": "叶天士", "source": "modified",
        "selected": False, "safety_blocking": True,
    })
    assert "加减方" in out2
    assert "已选" not in out2
    assert "安全拦截" in out2


def test_herb_node_falls_back_to_physician_names_map():
    """herb:: 节点的 data 里没有 pname（to_graph() 没存这份），必须靠前端自己
    从 PHYSICIAN_NAMES（renderConsultResult 里从 data.results 建的）查——
    这条测试钉住这条回退路径，不是钉住"节点数据恰好带全了"这个巧合。
    M5：id 从 herb::{phys}::{herb} 两段式变成 herb::{phys}::{方名}::{herb}
    三段式（同一味药可能出现在多个候选方里，不带方名会撞节点），layer 3 的
    "用药"变成 layer 4 的"药材"。"""
    out = _describe_node(
        {"id": "herb::wu_jutong::甘草泻心汤::党参", "label": "党参", "layer": 4,
         "phys": "wu_jutong", "role": "臣", "dose": 9.0, "unit": "g"},
        physician_names={"wu_jutong": "吴鞠通"},
    )
    assert "药材" in out and "党参" in out and "吴鞠通" in out
    assert "臣药" in out


def test_herb_node_without_physician_names_map_falls_back_to_raw_id():
    """PHYSICIAN_NAMES 还没建好（比如图还没渲染过就被 hover，理论上不会发生，
    但代码不能因为查不到就崩），退到显示医家 id 本身，好歹不是空白。"""
    out = _describe_node(
        {"id": "herb::wu_jutong::甘草泻心汤::党参", "label": "党参", "layer": 4, "phys": "wu_jutong"}
    )
    assert "wu_jutong" in out


def test_unknown_layer_falls_back_to_label_or_id():
    assert "神秘节点" in _describe_node({"id": "x::神秘节点", "label": "神秘节点", "layer": 99})
    assert "x::无标签" in _describe_node({"id": "x::无标签", "layer": 99})


# ---------- 边 ----------


def test_edge_shows_source_arrow_target():
    out = _describe_edge({}, "纳差", "脾")
    assert "纳差" in out and "脾" in out and "→" in out


def test_edge_with_phys_shows_physician_name():
    out = _describe_edge({"phys": "ye_tianshi"}, "脾", "脾胃气虚",
                         physician_names={"ye_tianshi": "叶天士"})
    assert "叶天士" in out


def test_edge_with_residual_flag_says_so():
    out = _describe_edge({"residual": True}, "乏力", "气虚")
    assert "残差辨证补充的连线" in out


def test_edge_without_phys_or_residual_shows_only_the_arrow():
    """症状->证素这条边（S2 全局共享）不带 phys、不带 residual，tooltip 不该
    凭空印出"undefined"或空的 meta 行。"""
    out = _describe_edge({}, "纳差", "脾")
    assert "undefined" not in out
    assert "tt-meta" not in out


# ---------- HTML 转义（沿用项目里既有的 escapeHtml，这里只验证真的被调用了）----------


def test_labels_are_html_escaped():
    out = _describe_node({"id": "sym::<script>", "label": "<script>x</script>", "layer": 0, "state": "explained"})
    assert "<script>x" not in out
    assert "&lt;script&gt;" in out


# ---------- 模块7：图谱浏览器节点（node_type，跟 per-consult 图的 layer 是两种数据形状）----------


def test_persistent_symptom_node_shows_label_only():
    out = _describe_node({"id": "symptom::纳呆", "label": "纳呆", "node_type": "symptom"})
    assert "症状" in out and "纳呆" in out


def test_persistent_element_node_shows_location_vs_nature():
    out = _describe_node({"id": "element::脾", "label": "脾", "node_type": "element", "category": "location"})
    assert "证素" in out and "病位" in out
    out = _describe_node({"id": "element::气虚", "label": "气虚", "node_type": "element", "category": "nature"})
    assert "病性" in out


def test_persistent_syndrome_node_shows_definition_and_category_flag():
    out = _describe_node({
        "id": "syndrome::SP-01", "label": "肝胃不和证", "node_type": "syndrome",
        "is_category": False, "definition": "肝气犯胃，胃失和降", "tongue_pulse": "舌淡红，苔薄白",
    })
    assert "证型" in out and "肝胃不和证" in out
    assert "肝气犯胃，胃失和降" in out
    assert "舌淡红，苔薄白" in out
    assert "类目" not in out

    out_cat = _describe_node({
        "id": "syndrome::CAT-01", "label": "脾胃病类", "node_type": "syndrome", "is_category": True,
    })
    assert "（类目）" in out_cat


def test_persistent_syndrome_node_without_definition_does_not_print_undefined():
    """definition/tongue_pulse 都是可选字段（合成图里可能不填），tooltip 不该
    因为缺了它们就印出"undefined"这种明显的渲染事故。"""
    out = _describe_node({"id": "syndrome::SP-01", "label": "肝胃不和证", "node_type": "syndrome"})
    assert "undefined" not in out


def test_persistent_case_node_shows_label():
    out = _describe_node({"id": "case::ye_tianshi-001", "label": "ye_tianshi-001", "node_type": "case"})
    assert "医案" in out


# ---------- 模块7：图谱浏览器的边（edge_type，跟 per-consult 图的 phys/residual 是两种数据形状）----------


def test_persistent_indicates_edge_shows_cardinal_and_lambda1_for_selected_physician():
    edge = {
        "edge_type": "indicates", "is_cardinal": True,
        "lambda1_by_physician": {"ye_tianshi": 0.0, "wu_jutong": 0.5},
    }
    out = _describe_edge(edge, "纳呆", "脾", physician_names={"ye_tianshi": "叶天士"},
                         current_physician="ye_tianshi")
    assert "纳呆" in out and "脾" in out and "indicates" in out
    assert "主症" in out
    assert "叶天士" in out and "0.00" in out


def test_persistent_indicates_edge_secondary_symptom_label():
    out = _describe_edge({"edge_type": "indicates", "is_cardinal": False}, "口苦", "热")
    assert "次症" in out


def test_persistent_indicates_edge_without_selected_physician_omits_lambda1_line():
    """没选医家（比如页面刚加载、还没触发 change 事件）时不该报 undefined 或
    随便挑一个医家的 λ1 出来充数——没选就不显示这一行。"""
    out = _describe_edge(
        {"edge_type": "indicates", "is_cardinal": True, "lambda1_by_physician": {"ye_tianshi": 0.0}},
        "纳呆", "脾", current_physician=None,
    )
    assert "λ1" not in out
    assert "undefined" not in out


def test_persistent_composes_edge_shows_only_arrow_and_type():
    """composes 边（证素->证型）不带 per-physician 权重，不该硬造一行"主症/次症"
    或 λ1 出来。"""
    out = _describe_edge({"edge_type": "composes"}, "脾", "脾胃气虚证")
    assert "脾" in out and "脾胃气虚证" in out and "composes" in out
    assert "主症" not in out and "次症" not in out
    assert "λ1" not in out
