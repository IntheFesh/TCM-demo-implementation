"""FastAPI 服务：/api/consult 跑推理链并把结果拼成前端可渲染的图数据。"""
from __future__ import annotations

import json
import queue
import secrets
import threading
from pathlib import Path
from typing import Iterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.chain import consult
from core.herbs import strip_dose_and_parens
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


@app.get("/api/trajectories/{physician}")
def api_trajectories(physician: str) -> dict:
    """附属功能：某位医家名下、带复诊序列的病人证素轨迹（core/transition.py，
    trajectory-only，不含转移概率）。cases.json / data/element_index.json
    缺失是这台 demo 环境的正常状态（数据要在有真实 LLM 的机器上生成），
    不是服务器错误——用 503 而不是 500，前端可以据此显示"这项功能待数据
    就绪"而不是当成系统故障。"""
    if physician not in PHYSICIANS:
        raise HTTPException(status_code=404, detail=f"未知医家：{physician}")

    from core.transition import load_trajectories

    try:
        trajectories = load_trajectories()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return {"physician": physician, "trajectories": trajectories.get(physician, [])}


@app.post("/api/consult")
def api_consult(req: ConsultRequest) -> dict:
    return _consult_response(consult(req.complaint))


def _consult_response(outcome: dict) -> dict:
    """把 consult() 的原始返回值拼成前端要的 JSON 形状。

    **全项目"consult() 结果怎么序列化给前端"这件事唯一的实现**：/api/consult
    和 /api/consult/stream 的终值事件都调这一个函数，不是各写一份——两条路径
    对同一份 outcome 必须产出完全相同的 JSON，否则流式端点收到的 done 事件
    和非流式端点的响应体就成了两份分叉的契约，前端的渲染函数没法共用
    （CLAUDE.md 第二次撞墙那条：字面上看着像"抄一份改改"，实际是同一个判断
    在两处实现，改一边会看不出会不会连带影响另一边）。
    """
    s1: S1Normalize = outcome["s1"]

    if outcome["rejected"]:
        # 安全否决命中：S2/S3 从未被调用，没有 results 可以拼图，直接返回空图。
        return {
            "s1": s1.model_dump(),
            "rejected": True,
            "reject_reason": outcome["reject_reason"],
            "safety_flag": outcome.get("safety_flag"),
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
            "safety_flag": outcome.get("safety_flag"),
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
        # EVAL_MODE 下非空 = 这条主诉本该被安全层拦下，但评测模式让它跑完了。
        # demo 模式下这个分支的它恒为 None（命中就走上面 rejected 分支了）。
        "safety_flag": outcome.get("safety_flag"),
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


# ---------- SSE 分步进度 ----------
#
# 跟 /api/consult 是并行的两条路径，不是替代关系——旧接口原样保留，/api/consult
# 从不传 ask_fn/on_step，行为跟改造前逐字节一致，eval/、老测试都还在用它。
# 这条新路径解决两件旧接口做不到的事：(1) 两位医家 + ReAct 加起来能到七八十秒，
# 期间前端只能干等；(2) 旧接口从不传 ask_fn，追问 / ReAct 的 ask_user 问出的
# 问题从来没人真的回答过。
#
# 实现思路：consult() 本来就是同步阻塞函数，不改成异步生成器（改了要动它的全部
# 调用方，CLI/eval/老测试全部要跟着换）。而是让它在后台线程里跑，用两个线程安全
# 的 queue.Queue 搬运数据：
#   events_q：consult() 的 on_step 回调往里塞进度事件，下面的生成器读出来转成
#             SSE 帧发给客户端。
#   answer_q：追问 / ReAct 问出问题时，下面包的 ask_fn 先往 events_q 塞一条
#             need_input 事件，再阻塞在 answer_q.get() 上——真正暂停的是这根
#             后台线程，HTTP 连接本身一直开着，只是暂时没有新事件可读。客户端
#             从 /api/consult/stream/{stream_id}/answer 这个独立端点把答案
#             塞进同一个 answer_q，后台线程就解除阻塞、继续往下跑。
# 一个 stream 同一时刻最多有一个悬而未决的问题（追问和 ReAct 的 ask_user 都在
# consult() 内部顺序执行，不会同时问两件事），所以一个 stream 只用一个答案队列，
# 不必给每个问题各开一个。

ANSWER_TIMEOUT_SECONDS = 300  # 没人回答时的兜底：不能让后台线程无限期挂着
_answer_queues: dict[str, queue.Queue] = {}
_answer_queues_lock = threading.Lock()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/consult/stream")
def api_consult_stream(req: ConsultRequest) -> StreamingResponse:
    stream_id = secrets.token_urlsafe(12)
    events_q: queue.Queue = queue.Queue()
    answer_q: queue.Queue = queue.Queue()
    with _answer_queues_lock:
        _answer_queues[stream_id] = answer_q

    def stream_ask_fn(question: str) -> str | None:
        events_q.put(("need_input", {"question": question}))
        try:
            answer = answer_q.get(timeout=ANSWER_TIMEOUT_SECONDS)
        except queue.Empty:
            # AskFn 的既有契约（core/followup.py）：返回 None = 提问方不打算
            # 回答。这里的"不打算"是等到超时，不是真的有人主动关掉了对话框，
            # 但对下游（run_followup / run_physician）来说是同一件事——这个
            # 问题问不出答案了，不需要为"超时"另开一条分支。
            return None
        events_q.put(("followup_answered", {"question": question, "answer": answer}))
        return answer

    def worker() -> None:
        try:
            outcome = consult(
                req.complaint,
                ask_fn=stream_ask_fn,
                on_step=lambda name, data: events_q.put((name, data)),
            )
            events_q.put(("done", _consult_response(outcome)))
        except Exception as e:  # noqa: BLE001 - 后台线程的异常不会自己冒泡到 HTTP
            # 响应里，必须在这兜住转成一个 error 事件；不然客户端只会看到连接
            # 挂在那不动，什么错误信息都拿不到。
            events_q.put(("error", {"detail": str(e)}))
        finally:
            events_q.put((None, None))  # 哨兵：告诉下面的生成器可以收工了
            with _answer_queues_lock:
                _answer_queues.pop(stream_id, None)

    threading.Thread(target=worker, daemon=True).start()

    def gen() -> Iterator[str]:
        yield _sse("stream_id", {"stream_id": stream_id})
        while True:
            name, data = events_q.get()
            if name is None:
                return
            yield _sse(name, data)

    return StreamingResponse(gen(), media_type="text/event-stream")


class ConsultStreamAnswer(BaseModel):
    answer: str


@app.post("/api/consult/stream/{stream_id}/answer")
def api_consult_stream_answer(stream_id: str, req: ConsultStreamAnswer) -> dict:
    with _answer_queues_lock:
        q = _answer_queues.get(stream_id)
    if q is None:
        # 两种情况都会落到这——stream_id 写错，或者这个 stream 已经跑完/当前
        # 没有待回答的问题。404 而不是静默忽略：前端要知道这次回答没地方接。
        raise HTTPException(status_code=404, detail="stream 不存在，或当前没有待回答的问题")
    q.put(req.answer)
    return {"ok": True}


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
            # id 必须保留原始写法（含剂量）：前端侧栏 buildEvidenceIndex() 用同一个
            # 拼法（herb::{physician}::{原始 herb}）反查证据，id 一变两边就对不上了。
            # label 单独剥掉剂量——"党参三钱""黄芪一两二钱"这种全串塞进节点，
            # text-max-width:90px 一折就是三四行，图挤得看不清药名本身。
            # 剥剂量只用 strip_dose_and_parens，不用 normalize_herb：后者还会查
            # 别名表、剥炮制前缀，会把模型实际写的"广皮"显示成"陈皮"，
            # label 要的是"同一个名字去掉剂量"，不是"归一到另一个名字"。
            herb_id = f"herb::{physician}::{herb}"
            label = strip_dose_and_parens(herb) or herb  # 剥空了（纯剂量字符串之类的脏数据）就退回原文，节点不能没有 label
            add_node(herb_id, label=label, layer=3, phys=physician)
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
