"""FastAPI 服务：/api/consult 跑推理链并把结果拼成前端可渲染的图数据。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.chain import consult
from core.physicians import PHYSICIANS
from core.schemas import S1Normalize

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="名医辨证对照 demo")


@app.on_event("startup")
def _warmup() -> None:
    """启动时预热检索器，把首请求那几十秒（加载模型 + 编码 839 条医案）
    挪到启动阶段。失败不阻塞启动——没有 cases.json 时服务仍应能起来。"""
    try:
        from core.retrieval import get_retriever

        get_retriever()._ensure_encoded()
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] 检索器预热跳过：{e}")


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

    if outcome["rejected"]:
        # 安全否决命中：S2/S3 从未被调用，没有 results 可以拼图，直接返回空图。
        return {
            "s1": s1.model_dump(),
            "rejected": True,
            "reject_reason": outcome["reject_reason"],
            "results": [],
            "divergence": None,
            "graph": {"nodes": [], "edges": [], "dropped_edges": 0},
            "manifest": outcome.get("manifest"),
        }

    if outcome.get("insufficient"):
        return {
            "s1": outcome["s1"].model_dump(),
            "rejected": False,
            "insufficient": True,
            "insufficient_reason": outcome["insufficient_reason"],
            "coverage": outcome.get("coverage"),
            "s2": outcome["s2"].model_dump() if outcome.get("s2") else None,
            "residual": _serialize_residual(outcome.get("residual")),
            "results": [],
            "divergence": None,
            "graph": {"nodes": [], "edges": [], "dropped_edges": 0},
            "manifest": outcome.get("manifest"),
        }

    results = outcome["results"]
    residual = outcome.get("residual")
    graph = to_graph(s1, results, outcome.get("s2"), residual)
    assert_graph_edges_valid(graph)

    return {
        "s1": s1.model_dump(),
        "rejected": False,
        "reject_reason": None,
        "results": [_serialize_result(r) for r in results],
        "divergence": outcome["divergence"],
        "insufficient": False,
        "insufficient_reason": None,
        "coverage": outcome.get("coverage"),
        "s2": outcome["s2"].model_dump() if outcome.get("s2") else None,
        "residual": _serialize_residual(residual),
        "graph": graph,
        "manifest": outcome.get("manifest"),
    }


def _serialize_residual(residual: dict | None) -> dict | None:
    if not residual:
        return None
    out = dict(residual)
    out["s2"] = residual["s2"].model_dump()
    return out


def _serialize_result(r: dict) -> dict:
    info = PHYSICIANS.get(r["physician"], {})
    return {
        "physician": r["physician"],
        "physician_name": r["physician_name"],
        # 配色从 physicians.py 出，前端不要再写死一份——加第三位医家时
        # 只改注册表一处。
        "color": info.get("color", "#666666"),
        "book": info.get("book"),
        "years": info.get("years"),
        "school": info.get("school"),
        "s2": r["s2"].model_dump(),
        "s3": r["s3"].model_dump(),
        "refs": r["refs"],
        "hallucinated": r["hallucinated"],
    }


# ---------- 图数据 ----------

MAX_HERBS_PER_PHYSICIAN = 6


def to_graph(s1: S1Normalize, results: list[dict], s2=None, residual: dict | None = None) -> dict:
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

    dropped: list[tuple[str, str]] = []

    def add_edge(source: str, target: str, **data) -> None:
        # 已知易错点：只有两端节点都已存在才建边，否则前端渲染会指向空节点。
        # 但静默丢弃会掩盖真实故障——S2 若把 supporting_symptoms 改写了
        # （"胃脘胀痛"->"脘腹胀痛"），边会整批消失，图上只是看起来"稀疏"，
        # 没人发现症状层和证素层已经断开。所以要计数并上报。
        if source not in seen or target not in seen:
            dropped.append((source, target))
            return
        edges.append({"data": {"source": source, "target": target, **data}})

    # layer 0 症状：来自共享的 s1.symptoms，state 取决于是否被任一医家的任一证素解释
    explained_symptoms: set[str] = set()
    for r in results:
        for hit in r["s2"].elements:
            explained_symptoms.update(hit.supporting_symptoms)

    # 两个来源：模型明确列进 unexplained_symptoms 的，和没被任何证素引用的。
    # 取并集——两者不总是一致，宁可多标一个也不要漏掉系统没说清的症状。
    declared_unexplained = set(getattr(s2, "unexplained_symptoms", None) or [])
    residual_explained = set((residual or {}).get("newly_explained") or [])

    for sym in s1.symptoms:
        if sym in explained_symptoms and sym not in declared_unexplained:
            state = "explained"
        elif sym in residual_explained:
            state = "residual"  # 初轮没解释，残差辨证补上了
        else:
            state = "unexplained"
        add_node(f"sym::{sym}", label=sym, layer=0, state=state)

    # 残差辨证新推出的证素，单独标出来（兼夹证的证素）
    if residual:
        for hit in residual["s2"].elements:
            elem_id = f"elem::{hit.element}"
            add_node(elem_id, label=hit.element, layer=1, kind=hit.kind, residual=True)
            for sym in hit.supporting_symptoms:
                add_edge(f"sym::{sym}", elem_id, residual=True)

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

    return {"nodes": nodes, "edges": edges, "dropped_edges": len(dropped)}


def assert_graph_edges_valid(graph: dict) -> None:
    """断言每条边的两端节点都存在于 nodes 里。已知易错点，务必保留这个检查。"""
    # 用 raise 不用 assert：python -O 会把 assert 整个优化掉，
    # 这道检查就在生产模式下静默失效了。
    node_ids = {n["data"]["id"] for n in graph["nodes"]}
    for e in graph["edges"]:
        if e["data"]["source"] not in node_ids:
            raise ValueError(f"边的 source 不存在：{e}")
        if e["data"]["target"] not in node_ids:
            raise ValueError(f"边的 target 不存在：{e}")


# 静态文件挂在 /app，不要挂在根路径——否则会遮蔽上面的 API 路由。
app.mount("/app", StaticFiles(directory=str(WEB_ROOT), html=True), name="web")
