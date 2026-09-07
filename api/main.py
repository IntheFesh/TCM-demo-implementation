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
            # 拒绝也要带追问记录：被拦下来的原因可能正是追问问出来的，
            # 只回一句"检测到危重症状"而不显示是哪一问问出来的，用户无从判断。
            "followup": _serialize_followup(outcome.get("followup")),
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
            "followup": _serialize_followup(outcome.get("followup")),
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
        "followup": _serialize_followup(outcome.get("followup")),
        "graph": graph,
        "manifest": outcome.get("manifest"),
    }


def _serialize_followup(followup) -> dict | None:
    return followup.model_dump() if followup is not None else None


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
        # 检索为空时 s3 是 S3SyndromeUnreferenced，model_dump 里没有 cited_case_ids，
        # 前端按同一份契约读，这里补成空列表
        "s3": {**r["s3"].model_dump(), "cited_case_ids": list(r["s3"].cited_case_ids)},
        "no_reference_cases": r.get("no_reference_cases", False),
        "refs": r["refs"],
        "hallucinated": r["hallucinated"],
        # X2 输出侧安全校验结果，前端据此挂红/黄标签
        "safety_output": r.get("safety_output"),
        # G2 取证轨迹。不开 ReAct 时是 None；开了要如实带出来——ReAct 的卖点
        # 就是"能看见它查了什么"，只把结论传出去等于白跑。
        "react_trace": r["react_trace"].model_dump() if r.get("react_trace") else None,
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

    # layer 0 症状：「已解释」用 core.chain.explained_symptoms 这一处实现——S2 全局共享，
    # 各医家的 r["s2"] 是同一份，这里不再各自汇总一遍
    from core.chain import explained_symptoms as _explained

    if s2 is None and results:
        s2 = results[0]["s2"]  # S2 全局共享，各医家拿到的是同一份
    explained_symptoms: set[str] = _explained(s1, s2) if s2 is not None else set()

    # 「已解释」的判据只有 core.chain.explained_symptoms 一处（上面），这里不再
    # 叠一层对 unexplained_symptoms 的处理——叠了就会跟 coverage、残差报的数打架。
    residual_explained = set((residual or {}).get("newly_explained") or [])

    for sym in s1.symptoms:
        if sym in explained_symptoms:
            state = "explained"
        elif sym in residual_explained:
            state = "residual"  # 初轮没解释，残差辨证补上了
        else:
            state = "unexplained"
        add_node(f"sym::{sym}", label=sym, layer=0, state=state)

    # layer 1 证素与 症状->证素 边：S2 全局共享，只发一遍，不按医家重复。
    # 原来每位医家各发一遍完全相同的边并打上 phys 标签，前端按 (source,target) 去重
    # 只画第一条、颜色永远是第一位医家的——第三位医家加入后重复更多、含义更误导。
    if s2 is not None:
        for hit in s2.elements:
            elem_id = f"elem::{hit.element}"
            add_node(elem_id, label=hit.element, layer=1, kind=hit.kind)
            for sym in hit.supporting_symptoms:
                add_edge(f"sym::{sym}", elem_id)

    # 残差辨证新推出的证素，单独标出来（兼夹证的证素）。必须在主证素之后加：
    # add_node 先到先得，先加残差会把主路径里同名的证素整个标成 residual=True。
    if residual:
        for hit in residual["s2"].elements:
            elem_id = f"elem::{hit.element}"
            add_node(elem_id, label=hit.element, layer=1, kind=hit.kind, residual=True)
            for sym in hit.supporting_symptoms:
                add_edge(f"sym::{sym}", elem_id, residual=True)

    for r in results:
        physician = r["physician"]
        pname = r["physician_name"]

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
