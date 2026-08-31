"""FastAPI 服务：/api/consult 跑推理链并把结果拼成前端可渲染的图数据。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.chain import consult
from core.schemas import S1Normalize

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="名医辨证对照 demo")


class ConsultRequest(BaseModel):
    complaint: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/app/index.html")


@app.post("/api/consult")
def api_consult(req: ConsultRequest) -> dict:
    outcome = consult(req.complaint)
    s1: S1Normalize = outcome["s1"]
    results = outcome["results"]

    graph = to_graph(s1, results)
    assert_graph_edges_valid(graph)

    return {
        "s1": s1.model_dump(),
        "results": [_serialize_result(r) for r in results],
        "divergence": outcome["divergence"],
        "graph": graph,
    }


def _serialize_result(r: dict) -> dict:
    return {
        "physician": r["physician"],
        "physician_name": r["physician_name"],
        "s2": r["s2"].model_dump(),
        "s3": r["s3"].model_dump(),
        "refs": r["refs"],
        "hallucinated": r["hallucinated"],
    }


# ---------- 图数据 ----------

MAX_HERBS_PER_PHYSICIAN = 6


def to_graph(s1: S1Normalize, results: list[dict]) -> dict:
    """构造 Cytoscape 格式的图：{nodes: [{"data": {...}}], edges: [{"data": {...}}]}。

    四层：症状(0) -> 证素(1) -> 证型(2) -> 药物(3)。
    节点去重用 seen 集合，同 id 只加一次。
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()

    def add_node(node_id: str, **data) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        nodes.append({"data": {"id": node_id, **data}})

    def add_edge(source: str, target: str, **data) -> None:
        # 已知易错点：只有两端节点都已存在才建边，否则前端渲染会指向空节点。
        if source not in seen or target not in seen:
            return
        edges.append({"data": {"source": source, "target": target, **data}})

    # layer 0 症状：来自共享的 s1.symptoms，state 取决于是否被任一医家的任一证素解释
    explained_symptoms: set[str] = set()
    for r in results:
        for hit in r["s2"].elements:
            explained_symptoms.update(hit.supporting_symptoms)

    for sym in s1.symptoms:
        state = "explained" if sym in explained_symptoms else "unexplained"
        add_node(f"sym::{sym}", label=sym, layer=0, state=state)

    for r in results:
        physician = r["physician"]
        pname = r["physician_name"]

        # layer 1 证素：两位医家共用同一节点（去重）
        for hit in r["s2"].elements:
            elem_id = f"elem::{hit.element}"
            add_node(elem_id, label=hit.element, layer=1, kind=hit.kind)

            for sym in hit.supporting_symptoms:
                sym_id = f"sym::{sym}"
                add_edge(sym_id, elem_id, phys=physician)

        # layer 2 证型
        syn_id = f"syn::{physician}"
        add_node(syn_id, label=r["s3"].syndrome, layer=2, phys=physician, pname=pname)

        for hit in r["s2"].elements:
            elem_id = f"elem::{hit.element}"
            add_edge(elem_id, syn_id, phys=physician)

        # layer 3 药物：每位医家最多取 6 味
        for herb in r["s3"].herbs[:MAX_HERBS_PER_PHYSICIAN]:
            herb_id = f"herb::{physician}::{herb}"
            add_node(herb_id, label=herb, layer=3, phys=physician)
            add_edge(syn_id, herb_id, phys=physician)

    return {"nodes": nodes, "edges": edges}


def assert_graph_edges_valid(graph: dict) -> None:
    """断言每条边的两端节点都存在于 nodes 里。已知易错点，务必保留这个检查。"""
    node_ids = {n["data"]["id"] for n in graph["nodes"]}
    for e in graph["edges"]:
        assert e["data"]["source"] in node_ids, f"边的 source 不存在：{e}"
        assert e["data"]["target"] in node_ids, f"边的 target 不存在：{e}"


# 静态文件挂在 /app，不要挂在根路径——否则会遮蔽上面的 API 路由。
app.mount("/app", StaticFiles(directory=str(WEB_ROOT), html=True), name="web")
