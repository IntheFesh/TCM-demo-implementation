"""web/index.html 里 hover tooltip 这部分前端代码的离线测试。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_stream_frontend.py /
test_western_drugs.py 同一个模式），测 describeNodeTooltip / describeEdgeTooltip
这两个纯函数：给一份 cytoscape 节点/边的 data()，应该出什么文字。不测真实
鼠标 hover 事件本身（cytoscape 的 mouseover/mousemove/mouseout 绑定逻辑）——
那要么得起真浏览器（Playwright），要么得深度 mock cytoscape 的事件系统，
两者都测不出比"这几行 cy.on(...) 调用对不对"更多的信息，而这几行本身很短、
读代码就能确认对不对，不值得为它单独搭一套 cytoscape 事件模拟。
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
                   physician_names: dict | None = None) -> str:
    js = (
        f"PHYSICIAN_NAMES = {json.dumps(physician_names or {}, ensure_ascii=False)};\n"
        f'process.stdout.write(describeEdgeTooltip({json.dumps(data, ensure_ascii=False)}, '
        f'{json.dumps(source_label, ensure_ascii=False)}, {json.dumps(target_label, ensure_ascii=False)}));'
    )
    return _run_node(js)


# ---------- 四种节点类型（对应 to_graph() 的 layer 0-3）----------


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


def test_herb_node_falls_back_to_physician_names_map():
    """herb:: 节点的 data 里没有 pname（to_graph() 没存这份），必须靠前端自己
    从 PHYSICIAN_NAMES（renderConsultResult 里从 data.results 建的）查——
    这条测试钉住这条回退路径，不是钉住"节点数据恰好带全了"这个巧合。"""
    out = _describe_node({"id": "herb::wu_jutong::党参", "label": "党参", "layer": 3, "phys": "wu_jutong"},
                         physician_names={"wu_jutong": "吴鞠通"})
    assert "用药" in out and "党参" in out and "吴鞠通" in out


def test_herb_node_without_physician_names_map_falls_back_to_raw_id():
    """PHYSICIAN_NAMES 还没建好（比如图还没渲染过就被 hover，理论上不会发生，
    但代码不能因为查不到就崩），退到显示医家 id 本身，好歹不是空白。"""
    out = _describe_node({"id": "herb::wu_jutong::党参", "label": "党参", "layer": 3, "phys": "wu_jutong"})
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
