"""web/index.html 里 M7 新增的 computeHighlightPath() 的离线测试：给定一个
起点症状节点 id，算出"症状->证素->证型->方剂"完整链路上该高亮的节点/边集合。

纯函数，只读一份 cytoscape 风格的 {nodes, edges} JSON（跟 api/main.py::to_graph()
返回的形状一致），不碰真实 cytoscape 实例——这条正是 M7 闸门要求的测试："路径
高亮的节点集合计算（纯函数：给定起点症状，返回应高亮的节点 id 集）"。

真实浏览器里点击生效、淡化样式真的应用到了 DOM 上的验证是另一件事（M7 闸门
要求的 Playwright 验证，见模块报告），这里只测算法本身对不对。
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


def _highlight(nodes, edges, start_id):
    js = f"""
    const r = computeHighlightPath({json.dumps(nodes, ensure_ascii=False)},
                                    {json.dumps(edges, ensure_ascii=False)},
                                    {json.dumps(start_id)});
    process.stdout.write(JSON.stringify({{ nodeIds: [...r.nodeIds].sort(), edgeIds: [...r.edgeIds].sort() }}));
    """
    return json.loads(_run_node(js))


def _e(source, target, **extra):
    return {"data": {"source": source, "target": target, **extra}}


def _n(node_id, **extra):
    return {"data": {"id": node_id, **extra}}


# ---------- 单医家、单候选方：最小的完整四步链路 ----------


def test_single_physician_single_formula_full_chain_highlighted():
    nodes = [
        _n("sym::胃脘胀痛", layer=0), _n("elem::气滞", layer=1),
        _n("syn::ye_tianshi", layer=2), _n("formula::ye_tianshi::柴胡疏肝散", layer=3),
        _n("herb::ye_tianshi::柴胡疏肝散::柴胡", layer=4, parent="formula::ye_tianshi::柴胡疏肝散"),
    ]
    edges = [
        _e("sym::胃脘胀痛", "elem::气滞"),
        _e("elem::气滞", "syn::ye_tianshi", phys="ye_tianshi"),
        _e("syn::ye_tianshi", "formula::ye_tianshi::柴胡疏肝散", phys="ye_tianshi"),
    ]
    result = _highlight(nodes, edges, "sym::胃脘胀痛")
    assert result["nodeIds"] == sorted([
        "sym::胃脘胀痛", "elem::气滞", "syn::ye_tianshi", "formula::ye_tianshi::柴胡疏肝散",
    ])
    # 药材层（layer 4）不在高亮集合里——四步链路到方剂为止，见函数文档字符串。
    assert "herb::ye_tianshi::柴胡疏肝散::柴胡" not in result["nodeIds"]
    assert len(result["edgeIds"]) == 3


def test_unrelated_symptom_and_its_downstream_are_not_highlighted():
    """另一个症状（走不同的证素）不应该被牵连进来——高亮必须是"这个症状的
    推理网"，不是"整张图"。"""
    nodes = [
        _n("sym::胃脘胀痛", layer=0), _n("sym::口苦", layer=0),
        _n("elem::气滞", layer=1), _n("elem::湿热", layer=1),
        _n("syn::ye_tianshi", layer=2), _n("syn::wu_jutong", layer=2),
        _n("formula::ye_tianshi::柴胡疏肝散", layer=3), _n("formula::wu_jutong::连朴饮", layer=3),
    ]
    edges = [
        _e("sym::胃脘胀痛", "elem::气滞"),
        _e("elem::气滞", "syn::ye_tianshi", phys="ye_tianshi"),
        _e("syn::ye_tianshi", "formula::ye_tianshi::柴胡疏肝散", phys="ye_tianshi"),
        _e("sym::口苦", "elem::湿热"),
        _e("elem::湿热", "syn::wu_jutong", phys="wu_jutong"),
        _e("syn::wu_jutong", "formula::wu_jutong::连朴饮", phys="wu_jutong"),
    ]
    result = _highlight(nodes, edges, "sym::胃脘胀痛")
    assert set(result["nodeIds"]) == {
        "sym::胃脘胀痛", "elem::气滞", "syn::ye_tianshi", "formula::ye_tianshi::柴胡疏肝散",
    }
    assert "sym::口苦" not in result["nodeIds"]
    assert "formula::wu_jutong::连朴饮" not in result["nodeIds"]


def test_two_physicians_multiple_candidates_fan_out_to_a_dozen_or_so_nodes():
    """M7 提前说的那句话的字面验证："如果模型给了两位医家、每位三个候选方，
    一个症状可能高亮出十几个节点——这是对的"。这里构造一个共享证素、两位
    医家各三个候选方的图，断言高亮集合确实覆盖了两位医家的全部候选方
    （不是只覆盖第一个/被选中的那个）。"""
    nodes = [_n("sym::胃脘胀痛", layer=0), _n("elem::气滞", layer=1)]
    edges = [_e("sym::胃脘胀痛", "elem::气滞")]
    physicians = ["ye_tianshi", "wu_jutong"]
    formula_ids = []
    for phys in physicians:
        syn_id = f"syn::{phys}"
        nodes.append(_n(syn_id, layer=2, phys=phys))
        edges.append(_e("elem::气滞", syn_id, phys=phys))
        for i in range(3):
            fid = f"formula::{phys}::候选{i}"
            formula_ids.append(fid)
            nodes.append(_n(fid, layer=3, phys=phys))
            edges.append(_e(syn_id, fid, phys=phys))

    result = _highlight(nodes, edges, "sym::胃脘胀痛")
    # 1 症状 + 1 证素 + 2 证型 + 6 候选方 = 10 个节点，"十几个"这个量级成立
    # （量级判断不是要求精确到某个数字，是"确实比单条直连要多得多"）。
    assert len(result["nodeIds"]) == 10
    for fid in formula_ids:
        assert fid in result["nodeIds"], f"{fid} 应该被高亮（不只是被选中的候选方）"


def test_start_node_itself_is_included():
    nodes = [_n("sym::孤立症状", layer=0)]
    edges = []
    result = _highlight(nodes, edges, "sym::孤立症状")
    assert result["nodeIds"] == ["sym::孤立症状"]
    assert result["edgeIds"] == []


def test_residual_edges_are_followed_same_as_normal_edges():
    """残差辨证补充的连线（edge.data.residual=true）在路径高亮里跟普通连线
    一视同仁——高亮回答的是"这条推理链走到哪"，不区分是初轮辨证还是残差
    补充推出来的，两者对学生来说都是"这个症状牵动的推理"。"""
    nodes = [_n("sym::兼症", layer=0), _n("elem::残差证素", layer=1)]
    edges = [_e("sym::兼症", "elem::残差证素", residual=True)]
    result = _highlight(nodes, edges, "sym::兼症")
    assert set(result["nodeIds"]) == {"sym::兼症", "elem::残差证素"}
