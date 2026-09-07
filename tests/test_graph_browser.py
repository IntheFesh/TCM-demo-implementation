"""web/index.html 里图谱浏览器页签（模块7）前端代码的离线测试。

跑真实上线的那份 <script>（跟别的前端测试同一个模式），只测不需要真实
cytoscape 就能验证的部分：gbBuildIndex 的索引构建。像 gbAddNodes/gbExpandNode/
gbSearch/gbToggleLayer/gbApplyPhysicianWeighting 这些函数内部都会调
真实 cytoscape 实例的方法（cy.add/cy.getElementById/cy.nodes().filter(...)/
ele.style(...)），DOM 代理桩测不出来——桩对象对任何属性访问、任何调用都返回
自己，`cy.nodes().filter(...)` 这类链式调用不会抛异常也不会报出任何有意义的
错误，测出来的只是"没崩"，测不出"filter 出来的到底是不是我要的那几个节点"。
这部分交给真实 Playwright + 真实 cytoscape 验证（见模块7报告），不在这里
用桩硬凑一份看起来测了、实际什么都没测到的测试。
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


def _sample_graph_data() -> dict:
    return {
        "graph": {
            "nodes": [
                {"data": {"id": "a", "label": "A", "node_type": "symptom"}},
                {"data": {"id": "b", "label": "B", "node_type": "element"}},
                {"data": {"id": "c", "label": "C", "node_type": "syndrome"}},
            ],
            "edges": [
                {"data": {"id": "a::b::indicates", "source": "a", "target": "b", "edge_type": "indicates"}},
                {"data": {"id": "b::c::composes", "source": "b", "target": "c", "edge_type": "composes"}},
            ],
        },
    }


def test_gb_build_index_maps_all_nodes():
    js = f"""
    gbGraphData = {json.dumps(_sample_graph_data(), ensure_ascii=False)};
    gbBuildIndex();
    process.stdout.write(JSON.stringify([...gbIndex.nodeById.keys()].sort()));
    """
    assert json.loads(_run_node(js)) == ["a", "b", "c"]


def test_gb_build_index_edges_are_bidirectional():
    """节点 b 既是 a->b 这条边的终点、又是 b->c 这条边的起点——两条边都要能
    从 b 这一侧查到，展开 b 节点时才能同时露出 a 和 c 这两个方向的邻居。"""
    js = f"""
    gbGraphData = {json.dumps(_sample_graph_data(), ensure_ascii=False)};
    gbBuildIndex();
    const out = {{
      edgesOfA: gbIndex.edgesByNode.get("a").map((e) => e.data.id),
      edgesOfB: gbIndex.edgesByNode.get("b").map((e) => e.data.id).sort(),
      edgesOfC: gbIndex.edgesByNode.get("c").map((e) => e.data.id),
    }};
    process.stdout.write(JSON.stringify(out));
    """
    out = json.loads(_run_node(js))
    assert out["edgesOfA"] == ["a::b::indicates"]
    assert out["edgesOfB"] == ["a::b::indicates", "b::c::composes"]
    assert out["edgesOfC"] == ["b::c::composes"]


def test_gb_build_index_node_with_no_edges_has_no_entry():
    """孤立节点（假设图里存在）在 edgesByNode 里不该有条目——gbExpandNode 对
    这种节点会用 `|| []` 兜底，这里钉住"没有条目"这个前提本身是真的，不是
    凭空假设。"""
    graph_data = {
        "graph": {
            "nodes": [{"data": {"id": "lonely", "label": "孤立", "node_type": "symptom"}}],
            "edges": [],
        },
    }
    js = f"""
    gbGraphData = {json.dumps(graph_data, ensure_ascii=False)};
    gbBuildIndex();
    process.stdout.write(JSON.stringify(gbIndex.edgesByNode.has("lonely")));
    """
    assert json.loads(_run_node(js)) is False
