"""FastAPI 服务：/api/consult 跑推理链并把结果拼成前端可渲染的图数据。"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import secrets
import sys
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Header, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.audit import append_audit
from core.chain import consult, explained_symptoms
from core.diseases import get_disease, triage_advice
from core.herbs import is_western_drug, strip_dose_and_parens
from core.llm import ByokBackend, LLMAuthError, check_api_key, get_llm, use_llm
from core.react import react_enabled
from core import usage as usage_mod
from core.physicians import PHYSICIANS, resolve_physician_id
from core.prescription import compute_herb_diffs, format_pharmacy_text
from core.safety_output import assess_formula_safety
from core.schemas import FormulaCandidate, FormulaSafety, HerbItem, S1Normalize
from core.tools import GRAPH_PATH, get_graph_store
from offline.graph_stats import compute_stats, lambda1_note

# M6：四种角色。前端按角色显示不同的 UI，但**字段裁剪在这里做，不在前端做**
# ——前端过滤等于把汤剂处方发到客户端再藏起来，患者打开 devtools 照样能看到。
Role = Literal["patient", "doctor", "student", "researcher"]

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"

# 同时在跑的问诊数上限（/api/consult 与 /api/consult/stream 合计）。每条问诊
# 占一根线程、十几次 LLM 调用、几十秒到几分钟；不设上限的话一个 for 循环里的
# curl 就能开出几千根线程、把 API key 的额度烧光。这是部署侧的进程级设置
# （跟 retriever_mode 那种逐请求的行为开关不是一回事），所以读环境变量没问题。
MAX_CONCURRENT_CONSULTS = int(os.environ.get("MAX_CONCURRENT_CONSULTS", "4"))
_consult_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CONSULTS)

# 主诉/追问回答的长度上限。主诉超过这个数几乎肯定是整篇病历粘进来了——
# 一整段原样进 S1 的 prompt，费用随长度线性涨；而且 uvicorn 会把整个请求体
# 读进内存，没有上限的话一个几百 MB 的 complaint 字段能直接把进程 OOM。
MAX_COMPLAINT_CHARS = 2000
MAX_ANSWER_CHARS = 500


def _warmup() -> None:
    """启动时预热检索器，把首请求那几十秒（加载模型 + 编码 839 条医案）
    挪到启动阶段。失败不阻塞启动——没有 cases.json 时服务仍应能起来。"""
    try:
        from core.retrieval import get_retriever

        get_retriever()._ensure_encoded()
    except Exception as e:  # noqa: BLE001 - 预热失败只是没有预热，服务照常起
        print(f"[warmup] 检索器预热跳过：{e}", file=sys.stderr)


# 预热最多等这么久，超过就先开始服务。真实冒烟里踩到的：有 cases.json 但连不上
# huggingface 的机器，预热卡在模型下载的重试上，服务一分多钟都不监听端口，存活探针
# 一直连不上——编排器会把它当成起不来。预热线程超时后不杀（也杀不了），在后台
# 继续；首个问诊会在 _encode_lock 上等它，而 /health 这时已经能答。
WARMUP_TIMEOUT_SECONDS = float(os.environ.get("WARMUP_TIMEOUT_SECONDS", "120"))


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI 已把 on_event 标成 deprecated，改成 lifespan。预热是同步的
    重 IO（加载模型、编码语料），放在自己的线程里而不是直接在事件循环上跑——
    直接阻塞循环会让 uvicorn 的信号处理一起卡住，这段时间 Ctrl-C 都停不下来。
    不走线程池：anyio 的 to_thread 默认等不到就取消不了，有超时也没法真的
    "先开始服务"。"""
    t = threading.Thread(target=_warmup, name="warmup", daemon=True)
    t.start()
    deadline = time.monotonic() + WARMUP_TIMEOUT_SECONDS
    while t.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    if t.is_alive():
        print(f"[warmup] 预热 {WARMUP_TIMEOUT_SECONDS:.0f} 秒还没完成，先开始服务；"
              "预热在后台继续，首个问诊会等它", file=sys.stderr)
    yield


app = FastAPI(title="名医辨证对照 demo", lifespan=_lifespan)


def _public_text(text: str) -> str:
    """把要发给客户端的文字里的项目绝对路径抹掉。core 层的报错（`未找到
    /home/xxx/data/element_index.json`）对命令行用户是有用信息，对匿名的 HTTP
    调用方就是在泄露部署布局。只抹路径，文件名留着——用户要知道缺的是哪个文件。"""
    return text.replace(str(ROOT) + "/", "").replace(str(ROOT), "<项目目录>")


def _public_error_detail(exc: Exception) -> str:
    """后台线程里的未预期异常，给客户端的只有异常类型和一个错误编号；完整内容
    （LLMError 带着后端名、模型名、模型原始输出前 500 字，claude_cli 后端还带
    子进程的整段 stderr）只打到服务端 stderr。之前是 str(e) 原样下发。
    编号是为了让用户报障时能对上服务端那一行，不是安全措施。"""
    error_id = secrets.token_hex(4)
    print(f"[consult-error {error_id}] {exc!r}", file=sys.stderr)
    traceback.print_exception(exc, file=sys.stderr)
    return f"服务端处理失败（{type(exc).__name__}，错误编号 {error_id}），详细原因见服务端日志。"


class ConsultRequest(BaseModel):
    complaint: str = Field(min_length=1, max_length=MAX_COMPLAINT_CHARS)
    # 逐请求的检索模式。**刻意不做成服务端的全局设置**：RETRIEVER_MODE 那个
    # 环境变量是进程级的，一个请求设了它，同一进程里并发的另一个请求就跟着变了。
    # 这个字段一路作为函数参数传到检索层，任何时候都不写进程状态。
    # 合法值不在这里用 Literal 卡：校验只在 core.chain.consult() 一处
    # （对着 ALLOWED_MODES），这里卡一遍等于把同一个判断实现两遍，
    # 加新模式时必然漏改一处。
    retriever_mode: str | None = None
    # M6：逐请求的显示角色，**不是服务端全局设置**——跟 retriever_mode 同一条
    # 理由：role 决定的是"这次响应给谁看"，不同请求可能是不同角色的人在用，
    # 写进程状态会互相污染。这里用 Literal 卡合法值（跟 retriever_mode 故意
    # 不卡是两回事）：非法角色应该在请求校验阶段就拒绝，不该让它混进
    # core.chain.consult()——consult() 本身完全不知道 role 这个概念，
    # 字段裁剪只发生在 _consult_response() 这一层，role 传得太深只会让
    # consult() 背上一个它不需要关心的参数。
    role: Role = "researcher"


def demo_mode_info() -> dict | None:
    """这台服务是不是在回放录制好的推理；None = 实时调用。

    **全项目唯一一处组装这个字段的地方**：/health、问诊响应、SSE 的 done 事件
    三个消费方都从这里取。散成三份的话，改一处漏两处，而漏掉的那两处就是
    "页面上没有那行小字"——这一条是诚实性要求，不能靠"记得三处都改"。

    刻意**不放进 manifest**：manifest 只下发给 researcher 角色
    （`_filter_response_by_role` 把它 pop 掉了），而演示给谁看就是给
    patient/doctor/student 看的——挂在 manifest 上等于对真正的观众隐身。
    """
    info = get_llm().replay_info()
    if not info:
        return None
    return {
        "recorded_at": info["recorded_at"],
        "model": info["model"],
        "n_fixtures": info["n_fixtures"],
        # 一句现成的话，前端直接显示，不在前端拼措辞——措辞是诚实性的一部分，
        # 不该有两个版本
        "notice": (f"演示模式：结果来自 {info['recorded_at'][:10]} 录制的真实推理"
                   f"（{info['model']}），非实时调用"),
    }


@app.get("/health")
async def health() -> dict:
    """async def 而不是 def：同步端点跑在 anyio 的线程池里（默认 40 个槽），
    几十条并发问诊把槽占满时，存活探针也跟着排队、超时，编排器会把一个其实
    还活着的进程重启掉。这个端点不做任何 IO，直接在事件循环上答。"""
    # demo_mode 非 None = 这台服务在回放录制好的推理（LLM_MODE=replay）。
    # **放在 /health 而不是只放在问诊响应里**：前端一加载就该看到那行小字，
    # 不该等到跑完一次问诊才告诉访问者"刚才那个不是现场跑的"。
    # 这个端点不做 IO 的性质没变——replay_info() 读的是已经装载好的 fixture
    # 元信息（fixtures 惰性加载，健康探针不会触发扫目录：探针在服务起来后
    # 第一次被调时若还没装载，装载的是几百个小 JSON，一次性的）。
    # physicians：**身份色的唯一来源是 core/physicians.py**（docs/DESIGN.md §2.1 的
    # 订正 + CLAUDE.md 第 31 条前端小节）。前端一加载就从这里取，注入成 CSS 变量，
    # CSS 里不写死——写死的话注册表加第四位医家时那份副本不会跟着长出来，新医家在
    # 界面上就没有颜色（这个坑已经踩过一次）。
    # 放 /health 而不是等第一次问诊：三列的顶边和姓名行在**还没有结果时**就要着色。
    return {
        "status": "ok",
        "demo_mode": demo_mode_info(),
        "physicians": [
            {"id": pid, "name": info["name"], "years": info["years"],
             "school": info["school"], "color": info["color"],
             "color_bg": info["color_bg"]}
            for pid, info in PHYSICIANS.items()
        ],
    }


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
    # 路径参数是外部输入，过唯一的解析入口（id 或中文名都认），不在这里直接
    # 拿字符串跟注册表比——CLAUDE.md「标识符只有一种规范形式，边界上统一解析」。
    physician_id = resolve_physician_id(physician)
    if physician_id is None:
        raise HTTPException(status_code=404, detail=f"未知医家：{physician}")
    physician = physician_id

    from core.transition import load_trajectories

    try:
        trajectories = load_trajectories()
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=_public_text(str(e))) from e

    return {"physician": physician, "trajectories": trajectories.get(physician, [])}


def _persistent_graph_to_cytoscape(store) -> dict:
    """把 core.graph.store.NetworkXStore 转成 Cytoscape 的 {nodes, edges} 形状，
    给图谱浏览器页签用。

    跟 to_graph() 是两个不同的函数、不能合并：那个函数把一次 consult() 的
    结果拼成"这次辨证走了哪条推理链"，输入是 S1/S2/results；这个函数把
    持久知识图谱（data/graph.json，K1/K2 建的国标结构层）转成同一种前端
    图形状，输入是 NetworkXStore。两者只是"目的地格式恰好一样"，源头的
    数据和语义完全不同，硬合并成一个函数只会让两边的调用方都要小心避开
    对方的参数。

    边端点字段名要小心：core/graph/store.py 的 save()/load() 已经踩过一次
    这个坑——每条边自带的 provenance 属性也叫 source（gb_standard/case/…），
    直接 **data 展开会把 cytoscape 期待的边端点字段 source 覆盖掉。这里改名
    成 data_source，把 source/target 这两个字段名让给端点。
    """
    nodes = [_node_payload(nid, d) for nid, d in store.g.nodes(data=True)]
    edges = [_edge_payload(u, v, k, d) for u, v, k, d in store.g.edges(keys=True, data=True)]
    return {"nodes": nodes, "edges": edges}


def _node_payload(node_id: str, data: dict) -> dict:
    node_data = {"id": node_id, "label": data.get("name", node_id)}
    for k, v in data.items():
        if k != "name":
            node_data[k] = v
    return {"data": node_data}


def _edge_payload(src: str, dst: str, key, data: dict) -> dict:
    edge_data = {"id": f"{src}::{dst}::{key}", "source": src, "target": dst}
    for k, v in data.items():
        edge_data["data_source" if k == "source" else k] = v
    return {"data": edge_data}


def _require_store():
    store = get_graph_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=_public_text(f"未找到 {GRAPH_PATH}。先跑 offline/build_graph.py 建图谱骨架。"),
        )
    return store


def _node_ids_of_types(store, node_types: str | None) -> list[str]:
    """按 node_type 过滤并**保持 networkx 的插入顺序**——分页游标是位置偏移，
    顺序一变，翻页就会漏掉或重复。build_graph 是确定性写入的，所以这个顺序
    在同一份 graph.json 上是稳定的。"""
    wanted = {t.strip() for t in (node_types or "").split(",") if t.strip()}
    out = []
    for nid, d in store.g.nodes(data=True):
        if wanted and d.get("node_type") not in wanted:
            continue
        out.append(nid)
    return out


@app.get("/api/graph")
def api_graph(
    node_types: str | None = None,
    limit: int = 0,
    cursor: int = 0,
) -> dict:
    """图谱浏览器页签用的持久知识图谱（data/graph.json 的国标结构层，跟
    /api/consult 里 to_graph() 产出的单次问诊图是两回事）。

    has_case_layer 如实反映这个 sandbox 的数据现状：这里没有 cases.json，
    data/graph.json 只挂了国标层，没有 case 节点，所以是 False——前端据此
    不显示"国标层/医案层"切换按钮，而不是显示一个点了没反应的（AutoDL 上
    跑过 attach_cases 之后这里会变 True）。

    lambda1_note 直接复用 offline/graph_stats.py 的 lambda1_note()，不在这里
    重新写一遍或者精简一遍——那段话是这个项目的一个真实发现（λ1 恒为 0，
    要么是压根没挂医案，要么是挂了医案但证型体系跟国标对不上），只有一处
    实现，前端原样显示，不弱化也不省略。
    """
    store = _require_store()
    stats = compute_stats(store)

    # limit=0 = 全量，**跟分页之前逐字节一样**（旧客户端和
    # test_graph_endpoint_matches_real_store_counts 都依赖这个默认）。
    # 分页是显式 opt-in：前端传 limit，服务端才切页。
    if limit and limit > 0:
        ids = _node_ids_of_types(store, node_types)
        total = len(ids)
        offset = max(int(cursor), 0)
        page_ids = ids[offset:offset + limit]
        keep = set(page_ids)
        graph = {
            "nodes": [_node_payload(nid, store.g.nodes[nid]) for nid in page_ids],
            # 只发两端都在本页里的边——跟前端 gbAddNodes 的规则同一条，
            # 不在这里另写一套"半条边"的语义。
            "edges": [
                _edge_payload(u, v, k, d)
                for u, v, k, d in store.g.edges(keys=True, data=True)
                if u in keep and v in keep
            ],
        }
        nxt = offset + len(page_ids)
        page = {"limit": limit, "cursor": offset, "returned": len(page_ids),
                "total": total, "next_cursor": nxt if nxt < total else None,
                "node_types": node_types}
    else:
        graph = _persistent_graph_to_cytoscape(store)
        page = {"limit": 0, "cursor": 0, "returned": len(graph["nodes"]),
                "total": len(graph["nodes"]), "next_cursor": None, "node_types": None}

    return {
        "graph": graph,
        "page": page,
        "has_case_layer": stats["node_type_counts"].get("case", 0) > 0,
        "lambda1_note": lambda1_note(stats),
        "physicians": [
            {"id": pid, "name": info["name"], "color": info["color"]}
            for pid, info in PHYSICIANS.items()
        ],
        "stats": {
            "node_type_counts": stats["node_type_counts"],
            "edge_type_counts": stats["edge_type_counts"],
        },
    }


@app.get("/api/graph/neighbors")
def api_graph_neighbors(node: str, limit: int = 200) -> dict:
    """展开一个节点的邻居。

    分页之后**必须有这个端点**：原来前端一次拿全图、在本地邻接表上展开，
    图一分页本地就没有全量邻接表了，展开会静默只展开"恰好在本页里"的那部分。
    无向看待（进边出边都算）——图谱浏览器展示的是关联，不是流向。
    """
    store = _require_store()
    if not store.g.has_node(node):
        raise HTTPException(status_code=404, detail=_public_text(f"图里没有这个节点：{node}"))

    seen: dict[str, dict] = {}
    edges = []
    for u, v, k, d in store.g.edges(node, keys=True, data=True):
        edges.append(_edge_payload(u, v, k, d))
        seen.setdefault(v, store.g.nodes[v])
    for u, v, k, d in store.g.in_edges(node, keys=True, data=True):
        edges.append(_edge_payload(u, v, k, d))
        seen.setdefault(u, store.g.nodes[u])
    seen.pop(node, None)

    total = len(seen)
    picked = list(seen.items())[:max(limit, 0)] if limit and limit > 0 else list(seen.items())
    keep = {nid for nid, _ in picked} | {node}
    return {
        "graph": {
            "nodes": [_node_payload(nid, d) for nid, d in picked],
            "edges": [e for e in edges
                      if e["data"]["source"] in keep and e["data"]["target"] in keep],
        },
        "page": {"limit": limit, "returned": len(picked), "total": total,
                 "truncated": len(picked) < total},
    }


@app.get("/api/graph/search")
def api_graph_search(q: str, limit: int = 100, node_types: str | None = None) -> dict:
    """按标签子串搜节点。

    跟 neighbors 同一条理由：分页之后前端手里没有全量 label 了，本地搜只能搜到
    已经画出来的那些——那不是"没找到"，是"没找过"，比报错更误导。
    `total` 如实报命中总数，`returned` 是实际发回的条数，前端据此显示
    "命中 N 条，只显示前 M 条"。
    """
    store = _require_store()
    needle = (q or "").strip()
    if not needle:
        return {"graph": {"nodes": [], "edges": []},
                "page": {"limit": limit, "returned": 0, "total": 0, "truncated": False}}

    wanted = {t.strip() for t in (node_types or "").split(",") if t.strip()}
    hits = []
    for nid, d in store.g.nodes(data=True):
        if wanted and d.get("node_type") not in wanted:
            continue
        if needle in str(d.get("name", nid)):
            hits.append((nid, d))

    total = len(hits)
    picked = hits[:max(limit, 0)] if limit and limit > 0 else hits
    keep = {nid for nid, _ in picked}
    return {
        "graph": {
            "nodes": [_node_payload(nid, d) for nid, d in picked],
            "edges": [_edge_payload(u, v, k, d)
                      for u, v, k, d in store.g.edges(keys=True, data=True)
                      if u in keep and v in keep],
        },
        "page": {"limit": limit, "returned": len(picked), "total": total,
                 "truncated": len(picked) < total},
    }


# 受信反代跳数。**默认 0 = 完全不读 X-Forwarded-For**，只用 TCP 对端地址。
# MDN 写得很明确：任何跟安全相关的 XFF 用法（限流、基于 IP 的访问控制）只能用
# 受信代理添加的那些地址，用不可信的值会导致限流被绕过、内存耗尽等后果
# （https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Forwarded-For）。
# 而**最左那一跳恰恰是客户端自己能写的**——取最左等于把限流的 key 交给攻击者：
#     for i in $(seq 1 10000); do curl -H "X-Forwarded-For: $RANDOM.$RANDOM.1.1" …; done
# 每一条都算成新 IP，按 IP 的额度形同虚设。
# 部署在 N 层受信反代后面时把这个值设成 N，代码从**右**数第 N 跳取。
TRUSTED_PROXY_HOPS = max(int(os.environ.get("TRUSTED_PROXY_HOPS", "0") or 0), 0)

# 不同 IP 桶的上限。同样来自上面那条 MDN 警告里的"内存耗尽"：轮换 IP 的请求会让
# 账本里的 dict 一直长。超过上限之后新来的都归进一个共用桶——宁可让少数人互相
# 挤额度，也不让进程被撑爆。
MAX_TRACKED_IPS = max(int(os.environ.get("QUOTA_MAX_TRACKED_IPS", "5000") or 0), 1)


def _normalize_ip(raw: str) -> str | None:
    """校验并归一化。伪造的值**可能根本不是 IP**（MDN 专门提醒过这点），
    所以先 parse；IPv6 归一到 /64——一个普通家宽用户手上就有整个 /64，
    不归一等于 IPv6 客户端天然免限流。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _client_ip(request: Request) -> str:
    """额度按 IP 分的 key。

    默认只用 TCP 对端地址（`request.client.host`）。部署在反代后面时对端是反代，
    所有访问者会被算成同一个人——那时候才设 `TRUSTED_PROXY_HOPS=N`，从右数第 N 跳取。
    **绝不取最左跳**，理由见上面 TRUSTED_PROXY_HOPS 的注释。

    即便配对了跳数，按 IP 限额也只是**防误伤**（让正常访问者各自计数），
    不是不可绕过的安全边界（同一个人换 IP 就是新额度）。真正兜底的是全局限额，
    那一条伪造不了。这句话要留着，不要让人误以为按 IP 限额是安全边界。
    """
    peer = _normalize_ip(request.client.host) if request.client else None
    if TRUSTED_PROXY_HOPS <= 0:
        return peer or "unknown"
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    # 从右数第 TRUSTED_PROXY_HOPS 跳：右边那些是受信反代自己追加的，再往左就是
    # 客户端能写的部分。链比预期短说明请求没经过那么多层反代（可能是直连），
    # 这时候退回对端地址，而不是去信一个位置对不上的值。
    idx = len(hops) - TRUSTED_PROXY_HOPS
    if idx < 0 or idx >= len(hops):
        return peer or "unknown"
    return _normalize_ip(hops[idx]) or peer or "unknown"


def _byok_key(raw: str | None) -> str | None:
    """访问者自带的 key 从请求头来，**不从请求体来**：请求体在很多地方会被
    完整记进日志/追踪，头字段至少不会跟着 payload 一起被打出来。"""
    if not raw:
        return None
    key = raw.strip()
    return key or None


def _gate(request: Request, raw_key: str | None):
    """闸门：决定这一次用哪个后端，并预占额度。

    **只读账本、不碰模型**，所以被拦下来的请求是零成本的——这是 D1 的核心判据，
    不是把请求跑完再看花了多少。返回 (decision, backend, token)；backend 为 None
    表示用进程默认后端（共享额度那条路），token 为 None 表示不需要结算。
    """
    ledger = usage_mod.get_ledger()
    ip = ledger.bucket_for(_client_ip(request), MAX_TRACKED_IPS)
    key = _byok_key(raw_key)
    decision = ledger.decide(ip, has_own_key=key is not None)

    if decision.mode == "byok":
        return decision, ByokBackend(key), None
    if decision.mode == "replay":
        from core.llm_replay import ReplayBackend

        return decision, ReplayBackend(), None

    estimate = usage_mod.estimate_calls(react_enabled(), len(PHYSICIANS))
    return decision, None, ledger.reserve(ip, estimate)


def _settle(token: int | None, outcome: dict | None) -> None:
    """按 manifest 里的真实 llm_calls 结算。

    outcome 为 None 分两种情况，**不能混为一谈**：
      · 根本没跑起来（并发位满、闸门本身抛了）→ `_refund()`，一次调用都没发生
      · 跑起来了但拿不到 manifest（客户端中途断开、中途抛异常）→ 走这里的
        `release()`：钱已经花了，只是数不清，按预占记账，不退。
        按 0 退的话，开着流跑一半关掉就等于白嫖。
    """
    if token is None:
        return
    ledger = usage_mod.get_ledger()
    if not outcome:
        ledger.release(token)
        return
    calls = int((outcome.get("manifest") or {}).get("llm_calls") or 0)
    ledger.settle(token, calls)


def _refund(token: int | None) -> None:
    """一次模型都没调，预占全额退还。"""
    if token is not None:
        usage_mod.get_ledger().settle(token, 0)


def _usage_block(request: Request, decision) -> dict:
    ledger = usage_mod.get_ledger()
    snap = ledger.snapshot(ledger.bucket_for(_client_ip(request), MAX_TRACKED_IPS))
    snap["mode"] = decision.mode
    snap["reason"] = decision.reason
    snap["degraded"] = decision.degraded
    return snap


@app.post("/api/usage/validate-key")
def api_validate_key(x_llm_key: str | None = Header(default=None)) -> dict:
    """用 DeepSeek 官方的「查询余额」接口验一把访问者填的 key，**零 token 消耗**。

    没有这个端点的话，填错 key 的人只能靠跑一次问诊才知道——而那一次可能已经
    走完 S1/S2。返回里不回显 key。
    """
    key = _byok_key(x_llm_key)
    if not key:
        raise HTTPException(status_code=400, detail="没有收到 key。")
    return check_api_key(key)


@app.get("/api/usage")
def api_usage(request: Request, x_llm_key: str | None = Header(default=None)) -> dict:
    """用量看板。**不消耗任何额度**（decide 只读账本），前端可以随时轮询。"""
    ledger = usage_mod.get_ledger()
    decision = ledger.decide(
        ledger.bucket_for(_client_ip(request), MAX_TRACKED_IPS),
        has_own_key=_byok_key(x_llm_key) is not None,
    )
    return _usage_block(request, decision)


def _acquire_consult_slot() -> threading.BoundedSemaphore:
    """拿不到就 503 + Retry-After，不排队：排队的请求照样占着连接和线程池的槽，
    客户端也不知道自己在等什么。返回拿到的那把信号量，调用方 release 的必须是
    同一把（测试里会整个换掉模块级的那把）。"""
    slots = _consult_slots
    if not slots.acquire(blocking=False):
        raise HTTPException(
            status_code=503,
            detail=f"同时进行的问诊已达上限（{MAX_CONCURRENT_CONSULTS}），请稍后再试。",
            headers={"Retry-After": "10"},
        )
    return slots


@app.post("/api/consult")
def api_consult(
    req: ConsultRequest,
    request: Request,
    response: Response,
    x_llm_key: str | None = Header(default=None),
) -> dict:
    decision, backend, token = _gate(request, x_llm_key)
    try:
        slots = _acquire_consult_slot()
    except HTTPException:
        # 并发位满 → 503。这条路上一次模型都没调，预占必须退还，否则每一次
        # 503 都会把估算永久挂在账上，并发一满额度就被慢慢吃光。
        _refund(token)
        raise
    outcome = None
    try:
        with use_llm(backend):
            outcome = consult(req.complaint, retriever_mode=req.retriever_mode)
    except LLMAuthError as e:
        # 见 stream 里那条注释：这一类要说给访问者听。
        raise HTTPException(status_code=502, detail=str(e)) from e
    except ValueError as e:
        # 模式名不认识 = 请求写错了，是 400 不是 500。只有这一种 ValueError 能
        # 从 consult() 冒到这里（consult 开头就校验了 retriever_mode，其余路径
        # 的检索问题都被包成 RetrievalUnavailable 走返回值，不抛异常）。
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        slots.release()
        _settle(token, outcome)
    # 额度状态走**响应头**，不进响应体：响应体的形状有一条逐字节契约测试守着
    # （test_researcher_role_response_matches_pre_m6_shape_byte_for_byte），
    # 而且额度是"站点计量"、不是"这次问诊的结果"，混进结果体会让两件事纠缠。
    # 完整看板在 GET /api/usage。
    snap = _usage_block(request, decision)
    response.headers["X-Usage-Mode"] = decision.mode
    response.headers["X-Usage-Remaining-Calls"] = str(snap["remaining_calls"])
    return _consult_response(outcome, role=req.role)


def _consult_response(outcome: dict, role: Role = "researcher") -> dict:
    """把 consult() 的原始返回值拼成前端要的 JSON 形状，按 role 裁剪。

    **全项目"consult() 结果怎么序列化给前端"这件事唯一的实现**：/api/consult
    和 /api/consult/stream 的终值事件都调这一个函数，不是各写一份——两条路径
    对同一份 outcome 必须产出完全相同的 JSON，否则流式端点收到的 done 事件
    和非流式端点的响应体就成了两份分叉的契约，前端的渲染函数没法共用
    （CLAUDE.md 第二次撞墙那条：字面上看着像"抄一份改改"，实际是同一个判断
    在两处实现，改一边会看不出会不会连带影响另一边）。

    role 裁剪统一走 `_filter_response_by_role()`，在函数末尾**每一个**分支
    返回前都过一遍——包括 rejected/retrieval_error/insufficient 这几个空
    results 的分支：role 是"这次响应给谁看"，跟这次辨证有没有产出结果是
    两个维度，不能因为没有结果就跳过角色裁剪，那样 patient 角色在这几个
    分支下会拿到跟 researcher 一样的（虽然当下是空的）响应形状，字段存在性
    本身就不该因为分支不同而不一致。
    """
    s1: S1Normalize = outcome["s1"]
    # 四个分支返回**同一套键**：前端按同一份契约读，缺键就是 undefined 悄悄进渲染。
    # core/chain.py 的七个返回点守着 14 键一致，这里是它上面那一层，同一条纪律。
    base = {
        "s1": s1.model_dump(),
        "rejected": False,
        "reject_reason": None,
        # EVAL_MODE 下非空 = 这条主诉本该被安全层拦下，但评测模式让它跑完了。
        # demo 模式下正常分支的它恒为 None（命中就走 rejected 分支了）。
        "safety_flag": outcome.get("safety_flag"),
        "retrieval_error": None,
        "insufficient": False,
        "insufficient_reason": None,
        "coverage": outcome.get("coverage"),
        "s2": outcome["s2"].model_dump() if outcome.get("s2") else None,
        "residual": _serialize_residual(outcome.get("residual")),
        # 拒绝也要带追问记录：被拦下来的原因可能正是追问问出来的，
        # 只回一句"检测到危重症状"而不显示是哪一问问出来的，用户无从判断。
        "followup": _serialize_followup(outcome.get("followup")),
        "results": [],
        "divergence": None,
        "graph": {"nodes": [], "edges": [], "dropped_edges": 0},
        "manifest": outcome.get("manifest"),
        # 非 None = 这次结果是回放的录制推理。跟 manifest 分开放：manifest 只
        # 给 researcher，而这行提示要给所有角色看（见 demo_mode_info 的注释）。
        "demo_mode": demo_mode_info(),
    }

    if outcome["rejected"]:
        # 安全否决命中：S2/S3 从未被调用，没有 results 可以拼图，直接返回空图。
        return _filter_response_by_role(
            {**base, "rejected": True, "reject_reason": outcome["reject_reason"]}, role, []
        )

    if outcome.get("retrieval_error"):
        # 选的检索模式这台机器上没有对应数据。单独一个分支而不是混进
        # insufficient：那个字段的意思是"你给的信息不够辨证"，这里是
        # "服务端这条检索路跑不起来"，混成一个会把服务端的问题说成用户的问题。
        return _filter_response_by_role(
            {**base, "retrieval_error": _public_text(outcome["retrieval_error"])}, role, []
        )

    if outcome.get("insufficient"):
        return _filter_response_by_role(
            {**base, "insufficient": True, "insufficient_reason": outcome["insufficient_reason"]},
            role, [],
        )

    results = outcome["results"]
    # role 传进 to_graph()：patient 角色从图构造这一步起就不生成方剂/药材层
    # （layer 3/4），不是先生成完整图再事后过滤掉那两层——图节点本身就带着
    # 药名，事后过滤等于先把处方发出去一半再藏起来，跟"裁剪必须在后端做"
    # 是同一条安全边界、同一个理由。
    graph = to_graph(s1, results, outcome.get("s2"), outcome.get("residual"), role=role)
    assert_graph_edges_valid(graph)
    full = {
        **base,
        "results": [_serialize_result(r) for r in results],
        "divergence": outcome["divergence"],
        "graph": graph,
    }
    return _filter_response_by_role(full, role, results)


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
_STREAM_POLL_SECONDS = 0.05  # 生成器轮询事件队列的间隔，见 _ConsultStream 文档


class StreamClosed(Exception):
    """客户端已经断开，后台线程没必要再往下跑。在 on_step/ask_fn 里抛出，
    consult() 不认识它、不会捕获，会一路冒到 worker 的兜底 except——那里把它
    当"正常提前结束"处理，不发 error 事件（也没人读了）。取消的粒度是
    "下一次回调"，即最多再跑完当前这一次 LLM 调用；线程没法从外面杀。"""


class _ConsultStream:
    """一次 SSE 问诊的后台线程和 HTTP 生成器之间的桥。三样东西：

    events_q  ——后台线程往里塞进度事件，生成器读出来转成 SSE 帧。
    cancel    ——生成器一结束（客户端断开、或正常收尾）就置位；后台线程的每次
                回调都先看它，置位就抛 StreamClosed 提前结束，不再花 LLM 调用。
                之前没有这条路：标签页一关，后台线程照样把 S1→S3 全跑完、
                每次调用照样计费，遇到追问还要傻等满 300 秒。
    pending   ——**只在有一个问题正在等回答时**才非 None 的答案队列，容量 1，
                每个问题一条新队列。之前是整条流共用一条、流一开就登记，带来
                两个问题：(1) 还没提问就能往里塞答案，下一个问题一问出来立刻
                被这个预先塞的答案"回答"了；(2) 上一问超时之后迟到的答案会
                留在队列里，喂给下一问——追问「有没有便血」如果吃到上一问的
                「有」，会凭空触发一次安全否决。

    生成器是 async 的、用 get_nowait + sleep 轮询而不是 iterate_in_threadpool
    阻塞在 events_q.get() 上：后者会让每条开着的流长期占一个线程池的槽（默认
    40 个），几十条流就把 /health 一起饿死；而且 to_thread 里的阻塞 get 不可
    取消，客户端断开后 Starlette 的取消要等到下一个事件才生效。轮询 50ms 的
    延迟对人看进度没有区别。
    """

    def __init__(self, stream_id: str, slots: threading.BoundedSemaphore) -> None:
        self.stream_id = stream_id
        self.events_q: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self._slots = slots
        self._pending: queue.Queue | None = None
        self._pending_lock = threading.Lock()

    # ---- 后台线程侧（consult 的回调）----

    def emit(self, name: str, data: dict) -> None:
        if self.cancel.is_set():
            raise StreamClosed()
        self.events_q.put((name, data))

    def ask(self, question: str) -> str | None:
        answer_q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending = answer_q
        try:
            # 先登记再发 need_input：客户端收到事件时答案一定已经有地方接
            self.emit("need_input", {"question": question})
            deadline = time.monotonic() + ANSWER_TIMEOUT_SECONDS
            while True:
                if self.cancel.is_set():
                    raise StreamClosed()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # AskFn 的既有契约（core/followup.py）：返回 None = 提问方不打算
                    # 回答。这里的"不打算"是等到超时，对下游来说是同一件事——
                    # 这个问题问不出答案了，不需要为"超时"另开一条分支。
                    return None
                try:
                    answer = answer_q.get(timeout=min(1.0, remaining))
                    break
                except queue.Empty:
                    continue  # 每秒醒一次看 cancel，不然断开后要等满 300 秒
        finally:
            with self._pending_lock:
                self._pending = None
        self.emit("followup_answered", {"question": question, "answer": answer})
        return answer

    # ---- HTTP 侧 ----

    def deliver_answer(self, answer: str) -> bool:
        """False = 当前没有问题在等回答（还没问、已超时、或已经答过）。"""
        with self._pending_lock:
            q = self._pending
            if q is None:
                return False
            try:
                q.put_nowait(answer)
            except queue.Full:
                return False
            return True

    def finish(self) -> None:
        self.events_q.put((None, None))  # 哨兵：告诉生成器可以收工了
        self._slots.release()
        with _streams_lock:
            _streams.pop(self.stream_id, None)


_streams: dict[str, _ConsultStream] = {}
_streams_lock = threading.Lock()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/api/consult/stream")
def api_consult_stream(
    req: ConsultRequest,
    request: Request,
    x_llm_key: str | None = Header(default=None),
) -> StreamingResponse:
    """retriever_mode 跟 /api/consult 一样逐请求传下去。模式名不认识时这条
    路径不回 400 而是发一个 error 事件——不是漏了，是刻意：模式合法性只在
    core.chain.consult() 一处校验（对着 ALLOWED_MODES），在这里再判一次等于
    把同一个判断连同错误文案实现两遍。worker 里 consult() 抛的 ValueError 会
    被兜成 error 事件，消息跟 400 那条完全一样，前端的 error 分支照样能显示。
    """
    decision, backend, token = _gate(request, x_llm_key)
    usage_snapshot = _usage_block(request, decision)
    try:
        slots = _acquire_consult_slot()
    except HTTPException:
        _refund(token)
        raise
    stream = _ConsultStream(secrets.token_urlsafe(12), slots)
    with _streams_lock:
        _streams[stream.stream_id] = stream

    def worker() -> None:
        outcome = None
        try:
            # use_llm 必须在**这个线程里**进——ContextVar 会被 anyio 的线程池
            # 复制，但裸 threading.Thread 不继承；在外面进、这里就是默认后端，
            # BYOK 的 key 和超额降级都会静默失效。
            with use_llm(backend):
                outcome = consult(
                    req.complaint,
                    ask_fn=stream.ask,
                    on_step=stream.emit,
                    retriever_mode=req.retriever_mode,
                )
            stream.events_q.put(("done", _consult_response(outcome, role=req.role)))
        except StreamClosed:
            pass  # 客户端已断开，没人读了，正常提前结束
        except LLMAuthError as e:
            # 401/402 是访问者自己能修的（key 不对、余额不足），必须说清楚，
            # 不能裹进"服务端处理失败（错误编号 xxxx）"里让人去找站点管理员。
            # 异常里不含 key（SDK 不把 Authorization 头放进异常）。
            stream.events_q.put(("error", {"detail": str(e)}))
        except ValueError as e:
            # 跟 /api/consult 的 400 同一类：请求本身写错（模式名不认识），
            # 消息是给用户看的、不含内部信息，原样发
            stream.events_q.put(("error", {"detail": str(e)}))
        except Exception as e:  # noqa: BLE001 - 后台线程的异常不会自己冒泡到 HTTP
            # 响应里，必须在这兜住转成一个 error 事件；不然客户端只会看到连接
            # 挂在那不动，什么错误信息都拿不到。
            stream.events_q.put(("error", {"detail": _public_error_detail(e)}))
        finally:
            _settle(token, outcome)
            stream.finish()

    threading.Thread(target=worker, daemon=True).start()

    async def gen() -> AsyncIterator[str]:
        try:
            # 额度状态**搭在第一帧里**发，不另起一帧：超额降级到 replay 时，
            # 用户该在推理开始之前就知道自己看到的是回放，而不是等结果出来才
            # 发现对不上；而另起一帧会改事件顺序，那个顺序有契约测试守着
            # （test_stream_id_arrives_first_then_progress_then_done）。
            yield _sse("stream_id", {"stream_id": stream.stream_id, "usage": usage_snapshot})
            while True:
                try:
                    name, data = stream.events_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(_STREAM_POLL_SECONDS)
                    continue
                if name is None:
                    return
                yield _sse(name, data)
        finally:
            # 正常收尾时后台线程早已结束，置位无害；客户端断开时 Starlette 取消
            # 这个生成器、走到这里，后台线程下一次回调就会看到并退出。
            stream.cancel.set()

    return StreamingResponse(gen(), media_type="text/event-stream")


class ConsultStreamAnswer(BaseModel):
    answer: str = Field(max_length=MAX_ANSWER_CHARS)


@app.post("/api/consult/stream/{stream_id}/answer")
def api_consult_stream_answer(stream_id: str, req: ConsultStreamAnswer) -> dict:
    with _streams_lock:
        stream = _streams.get(stream_id)
    if stream is None or not stream.deliver_answer(req.answer):
        # 三种情况都落到这——stream_id 写错、这个 stream 已经跑完、或者当前
        # 没有待回答的问题（还没问 / 已超时 / 刚才已经答过）。404 而不是静默
        # 忽略：前端要知道这次回答没地方接。
        raise HTTPException(status_code=404, detail="stream 不存在，或当前没有待回答的问题")
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
        # M4：规则算出来的病名候选（跟模型脱钩），前端可以摆出"模型判断 X，
        # 规则倾向 Y"这种交叉校验，不是要替代模型的判断。
        "disease_candidates": r.get("disease_candidates", []),
        "no_reference_cases": r.get("no_reference_cases", False),
        "refs": r["refs"],
        "hallucinated": r["hallucinated"],
        # X2 输出侧安全校验结果，前端据此挂红/黄标签
        "safety_output": r.get("safety_output"),
        # G2 取证轨迹。不开 ReAct 时是 None；开了要如实带出来——ReAct 的卖点
        # 就是"能看见它查了什么"，只把结论传出去等于白跑。
        "react_trace": r["react_trace"].model_dump() if r.get("react_trace") else None,
    }


# ---------- M6：role 字段裁剪 ----------
#
# 四种角色的字段表（见模块报告，逐条对齐 M6 任务描述原文的表格）：
#   字段              | patient | doctor | student | researcher
#   disease/syndrome  |    ✅   |   ✅   |   ✅    |    ✅
#   triage            |    ✅   |   ✅   |   —     |    —
#   formula_candidates|    ❌   |   ✅   |   ✅    |    ✅
#   食疗/中成药        |    ✅   |   ✅   |   —     |    —
#   reasoning         |  简化   |   ✅   |   ✅    |    ✅
#   react_trace       |    ❌   |   ❌   |   ✅    |    ✅
#   refs              |    ❌   |   ✅   |   ✅    |    ✅
#   divergence        |    ❌   |   ❌   |   ✅    |    ✅
#   manifest          |    ❌   |   ❌   |   —     |    ✅
#   safety 详情        |  简化   |   ✅   |   ✅    |    ✅
# student 在这张表里跟 researcher 唯一的差别是 manifest（技术/后端元数据，
# 跟教学用途无关）——这也是下面的实现顺着这张表从 researcher 开始逐步收窄
# 的原因：先剥 manifest（覆盖 student），再剥 divergence/react_trace（覆盖
# patient+doctor），最后 patient 单独再剥一层跟处方直接相关的字段。


def _apply_medication_gate(items: list, urgency: str | None) -> list:
    """urgency=high 时无条件不给任何用药相关建议——包括食疗，哪怕将来 M9
    接入了真实食疗/中成药数据源，这个函数都是唯一要改的地方。高危症状类别
    下，任何看起来"温和"的建议都可能让患者放松警惕、延误真正需要的急诊
    处置，这条边界故意写得比"看起来过度保守"更硬，不因为"食疗数据这轮还是
    空的、看起来测不出差别"就不实现——数据源接上的那天，这道闸门必须已经
    在这里等着，不能指望 M9 的人记得回来加。"""
    if urgency == "high":
        return []
    return items


def _compute_triage(results: list[dict]) -> dict | None:
    """患者导诊：取所有医家诊断病名里紧急度最高的一个作为总体建议——错过
    真实红旗症状比给一个偏保守的建议危险得多，宁可保守，不因为"多数医家
    没那么紧急"压低。找不到任何一个医家的病名在参考表里（包括模型没填
    disease、或填了但不在 M4 建的 15 条参考表里）时返回 None，如实说明
    没有导诊依据，不伪造一个。"""
    urgency_rank = {"high": 2, "medium": 1, "low": 0}
    candidates = []
    for r in results:
        s3 = r["s3"]
        d = get_disease(s3.disease) if s3.disease else None
        if d is not None:
            candidates.append(d)
    if not candidates:
        return None
    chosen = max(candidates, key=lambda d: urgency_rank.get(d.triage_urgency, -1))
    return {
        "dept": chosen.triage_dept,
        "urgency": chosen.triage_urgency,
        "red_flags": list(chosen.red_flags),
        "advice": triage_advice(chosen),
    }


def _simplify_safety_output(safety_output: dict | None) -> dict | None:
    """patient 角色的"安全详情简化"：原始 safety_output 里 incompatible
    （配伍禁忌的药对）、thermal_warning（寒热警告文本）都会提到具体药材
    名——而 patient 角色本来就看不到 formula_candidates，如果 safety_output
    原样下发，等于从这条后门把药名重新泄露回去。压成一个不含药名的布尔
    摘要，但不整个丢掉：'方子有没有被系统标记过问题'这件事本身对患者是
    有意义的信息（比如可以提示"医生模式能看到更详细的提示"）。"""
    if not safety_output:
        return safety_output
    return {
        "has_safety_note": bool(
            safety_output.get("incompatible") or safety_output.get("thermal_warning")
        ),
        "revised": safety_output.get("revised", False),
    }


def _filter_s3_for_role(s3: dict, role: Role) -> dict:
    """按角色裁剪单个 s3 字典。只有 patient 角色需要动这一层——doctor/
    student/researcher 三者的 s3 内容完全一致（表里 disease/syndrome/
    formula_candidates/reasoning 四行这三者都是 ✅/✅/full）。"""
    if role != "patient":
        return s3
    s3 = dict(s3)
    # reasoning 换成通俗版：reasoning_plain 缺失时**不退回显示 reasoning**
    # ——那样会让"这个字段允许为空"这个 schema 层的宽松决定，悄悄破坏掉
    # patient 角色不该看到专业推理文本这条安全边界。缺失时给一句明确的
    # 占位说明，不是伪造内容也不是泄露原文。
    s3["reasoning"] = s3.get("reasoning_plain") or "（本次未生成通俗版说明，具体病机建议咨询医师）"
    s3.pop("reasoning_plain", None)
    # 候选方/药材相关字段全部摘掉——这几个字段本身就会泄露具体方名药名，
    # 不是"藏起来"，是压根不下发。selected 是候选方数组的下标，没有
    # formula_candidates 时这个数字没有意义，一并摘掉避免误导。
    for key in ("formula_candidates", "formula", "herbs", "western_drugs", "selected"):
        s3.pop(key, None)
    return s3


def _filter_response_by_role(response: dict, role: Role, results: list[dict]) -> dict:
    """把 _consult_response() 拼好的完整响应按角色裁剪。**全项目角色裁剪
    唯一的实现**——前端不做任何字段过滤，过滤在这里一次性做完，服务端
    就不下发患者不该看到的字段（不是发了再让前端藏起来，那样打开
    devtools 照样能看到）。

    results 是这次 consult() 的原始 per-physician 结果（不是已经序列化过的
    response["results"]）——_compute_triage 需要读 r["s3"].disease（pydantic
    对象上的字段），序列化后的 dict 也有同名字段，但直接传原始对象更清楚
    "这里读的是模型的真实判断，不是已经被裁剪过的展示层数据"。
    """
    if role == "researcher":
        # 默认角色，闸门要求跟改造前逐字节一致——不做任何处理、连 dict() 拷贝
        # 都不做，返回的就是 _consult_response() 刚拼好的那个对象本身。
        return response

    response = dict(response)
    response.pop("manifest", None)  # 除 researcher 外，manifest 一律不下发

    if role == "student":
        # student 相对 researcher 唯一的差别就是 manifest，到这里就结束了。
        return response

    # 走到这里说明 role 是 "patient" 或 "doctor"。
    response.pop("divergence", None)

    triage = _compute_triage(results)
    response["triage"] = triage
    urgency = triage.get("urgency") if triage else None
    # 食疗/中成药需要 data/patent_medicines.jsonl（M9 才建），这一轮先把字段
    # 留好、返回空列表——但空列表不是这里的安全边界，_apply_medication_gate
    # 才是：urgency=high 时无论 M9 接了什么内容都必须继续返回空列表。
    response["food_therapy"] = _apply_medication_gate([], urgency)
    response["patent_medicines"] = _apply_medication_gate([], urgency)

    new_results = []
    for r in response["results"]:
        r = dict(r)
        r.pop("react_trace", None)  # patient/doctor 都拿不到取证轨迹
        if role == "patient":
            r["s3"] = _filter_s3_for_role(r["s3"], role)
            r["refs"] = []
            r["safety_output"] = _simplify_safety_output(r.get("safety_output"))
        new_results.append(r)
    response["results"] = new_results

    return response


# ---------- 图数据 ----------


def to_graph(
    s1: S1Normalize, results: list[dict], s2=None, residual: dict | None = None,
    role: Role = "researcher",
) -> dict:
    """构造 Cytoscape 格式的图：{nodes: [{"data": {...}}], edges: [{"data": {...}}]}。

    六层（M5）：症状(0) -> 证素(1) -> 病名·证型(2) -> 方剂(3) -> 药材(4)。
    治法不单独成层，做成 layer2 -> layer3 边的 label（六层已经够宽，七层会挤到
    看不清）。方剂(3)/药材(4) 是 compound 关系：药材节点的 `parent` 字段指向
    它所属的方剂节点，父子关系由 cytoscape 内建机制表达，**不额外画一条
    formula->herb 的边**——画了会在图上出现重复的连线。

    节点去重用 seen 集合，同 id 只加一次。

    M6：role="patient" 时压根不产出方剂(3)/药材(4) 层——图节点本身就带着
    真实药名（label/id 都是），如果先建出完整六层图、再在 `_consult_response`
    那层把 `results[].s3.formula_candidates` 摘掉，图里这两层节点依然会把
    同样的药名重新泄露给前端。跟"字段裁剪必须在后端做、不能指望前端藏起来"
    是同一条安全边界：这里的做法是"根本不生成"，不是"生成了再删"。
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
    if s2 is None and results:
        s2 = results[0]["s2"]  # S2 全局共享，各医家拿到的是同一份
    explained: set[str] = explained_symptoms(s1, s2) if s2 is not None else set()

    # 「已解释」的判据只有 core.chain.explained_symptoms 一处（上面），这里不再
    # 叠一层对 unexplained_symptoms 的处理——叠了就会跟 coverage、残差报的数打架。
    residual_explained = set((residual or {}).get("newly_explained") or [])

    for sym in s1.symptoms:
        if sym in explained:
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

        # layer 2 病名·证型（M4）。node id 不变——仍是 syn::{physician}，只改
        # label：id 是前端证据链侧栏 buildEvidenceIndex() 反查的键，改了就断链，
        # 跟 M 药名剥剂量那次「label 剥、id 保原样」是同一条理由。disease 为
        # None（病名判断不了，S3 prompt 允许留空）时退回只显示证型，不显示
        # 一个悬空的"· 证型"。
        syn_id = f"syn::{physician}"
        s3 = r["s3"]
        label = f"{s3.disease} · {s3.syndrome}" if s3.disease else s3.syndrome
        add_node(syn_id, label=label, layer=2, phys=physician, pname=pname)

        for hit in r["s2"].elements:
            elem_id = f"elem::{hit.element}"
            add_edge(elem_id, syn_id, phys=physician)

        # layer 3 方剂 + layer 4 药材（M5）：每个候选方都出节点，不是只画
        # selected 那一个——前端要能摆出 2-3 个方框各自装着自己的药，
        # 「点哪个方剂看哪些药」是候选方对比的核心卖点，只画 selected 会把
        # 另外 1-2 个候选方在图上变得不可见。
        #
        # M6：role="patient" 时整段跳过——不生成方剂/药材层，不是生成了再
        # 从响应里摘掉（见函数文档字符串）。
        if role == "patient":
            continue
        for i, cand in enumerate(s3.formula_candidates):
            # 同一位医家的多个候选方可能撞同一个方名（真实产出里少见，但不能假设
            # 不会发生）——formula_id 只按 physician+name 拼，重名候选方会被
            # add_node 的去重逻辑合并成一个节点，这是已知的、可接受的边界情况
            # （见 tests/test_graph.py 的对应测试）：图上没有"同名候选方各画一份"
            # 的必要，两个同名候选方本来就该被当成同一个方剂节点。
            formula_id = f"formula::{physician}::{cand.name}"
            add_node(
                formula_id, label=cand.name, layer=3, phys=physician,
                # 前端按 source 区分边框（classic 实线/modified 虚线/composed
                # 点线）、selected 高亮选中的那个、safety_blocking 为真时标红。
                source=cand.source, confidence=cand.confidence,
                selected=(i == s3.selected),
                safety_blocking=cand.safety.blocking if cand.safety else False,
            )
            # 边 label 用 treatment_principle：治法不单独成层，挂在这条边上。
            add_edge(syn_id, formula_id, phys=physician, label=s3.treatment_principle)

            for item in cand.herb_items:
                # herb_id 必须带方剂名：同一味药可能出现在这位医家的多个候选方里
                # （比如"甘草"作为使药几乎每个方都有），不带方名会被 add_node 的
                # 去重逻辑合并成一个节点、同时挂在两个 parent 上，cytoscape 会报错。
                # id 用 item.name 原始写法（旧式合成路径下可能仍带剂量文本，见
                # core.schemas._S3Base 的向后兼容合成），label 单独剥剂量——
                # 跟"药名剥剂量"那次「label 剥、id 保原样」是同一条理由，前端
                # buildEvidenceIndex() 用同一个拼法反查证据，id 一变就断链。
                herb_id = f"herb::{physician}::{cand.name}::{item.name}"
                label = strip_dose_and_parens(item.name) or item.name
                add_node(
                    herb_id, label=label, layer=4, phys=physician,
                    parent=formula_id,
                    dose=item.dose, unit=item.dose_unit,
                    processing=item.processing, decoction=item.decoction,
                    # 这里的 role（君/臣/佐/使）是 HerbItem 自己的字段，跟本函数
                    # 参数 role（patient/doctor/...角色）只是同名，语义完全不同，
                    # 不要看到 role= 就以为在传角色参数。
                    role=item.role, function_in_formula=item.function_in_formula,
                    is_western=is_western_drug(item.name),
                )
                # 方剂 -> 药材的关系由上面的 parent 字段（compound node）表达，
                # 这里不额外画边——画了会在图上出现重复的连线，这是这个模块
                # 最容易漏改的一条。

    return {"nodes": nodes, "edges": edges, "dropped_edges": len(dropped)}


def assert_graph_edges_valid(graph: dict) -> None:
    """断言每条边的两端节点都存在于 nodes 里，以及（M5 起）每个 compound
    子节点的 `parent` 也指向一个真实存在的节点。已知易错点，务必保留这个检查
    ——`parent` 不是走 edges 数组表达的关系，跟"边两端都存在"是同一类"不能有
    悬空引用"的问题，放进同一个函数里查，不另开一个只测 parent 的检查点。"""
    # 用 raise 不用 assert：python -O 会把 assert 整个优化掉，
    # 这道检查就在生产模式下静默失效了。
    node_ids = {n["data"]["id"] for n in graph["nodes"]}
    for e in graph["edges"]:
        if e["data"]["source"] not in node_ids:
            raise ValueError(f"边的 source 不存在：{e}")
        if e["data"]["target"] not in node_ids:
            raise ValueError(f"边的 target 不存在：{e}")
    for n in graph["nodes"]:
        parent = n["data"].get("parent")
        if parent is not None and parent not in node_ids:
            raise ValueError(f"节点的 parent 不存在：{n}")


# ---------- M8：医生模式——处方校验 / 导出 / 审计 ----------


class PrescriptionValidateRequest(BaseModel):
    # 不设 min_length=1：可编辑处方表从空表开始，医生删到只剩 0 味药时
    # 前端仍可能调一次校验（"每次编辑后调一次"）——0 味药本来就查不出十八反/
    # 剂量超限，返回一个全空的 FormulaSafety 是诚实的结果，不该被 422 拒绝。
    herb_items: list[HerbItem] = Field(default_factory=list)
    syndrome: str = Field(min_length=1)
    # disease 字段任务描述原文的 body 形状里给了，但 assess_formula_safety
    # 五条规则（十八反/寒热/剂量/煎法/毒性）没有一条读病名——寒热一致性查的是
    # 证型名里的关键词，不是病名。这里原样接住这个字段（跟请求契约保持一致，
    # 医生端可能想传），但目前确实没有消费它，如实留着不裁掉，也不假装用了它。
    disease: str | None = None


def _safety_dict(safety: FormulaSafety) -> dict:
    """FormulaSafety.blocking 是 @property（schemas.py 里定义），
    model_dump() 不会带出计算属性——三处（/validate 响应、/export 拒绝时的
    422 detail、写进审计记录的 safety_at_export）都要把 blocking 一起带上，
    不然调用方（前端，或者以后读审计日志的人）得自己重新判断"incompatible
    或 dose_violations 非空就是 blocking"，这条判断 core/safety_output.py
    已经有唯一实现，不该被逼着在第二处（第三处、第四处……）重新写一遍
    （CLAUDE.md「同一概念只能有一处实现」）。三处调用同一个函数，不是三处
    各自拼一遍 {**safety.model_dump(), "blocking": ...}。"""
    return {**safety.model_dump(), "blocking": safety.blocking}


@app.post("/api/prescription/validate")
def api_prescription_validate(req: PrescriptionValidateRequest) -> dict:
    """纯规则校验，不调 LLM，毫秒级返回。独立于 /api/consult——医生可能在
    完全不同的场景下想校验一张手写/临时改动的方（不是从某次问诊来的），
    这条接口不依赖任何问诊上下文。"""
    safety = assess_formula_safety(req.syndrome, req.herb_items)
    return _safety_dict(safety)


class PrescriptionExportRequest(BaseModel):
    formula: FormulaCandidate
    doctor_id: str = Field(min_length=1)
    patient_ref: str | None = None
    model_suggestion: FormulaCandidate
    # 任务描述原文展示的 body 形状里没列这个字段，但紧接着那句"医生要坚持
    # 导出，必须传 override_reason: str（非空）"没有别的地方能装这个值——
    # 这里补上，属于把隐含在文字里的字段显式化，不是新加一条没来由的契约。
    override_reason: str | None = None


@app.post("/api/prescription/export")
def api_prescription_export(req: PrescriptionExportRequest) -> dict:
    """`formula.safety`（如果客户端带了）**不作数**——安全阻断的判定必须是
    服务端权威计算的，不能信任客户端上报的安全结果，跟 X2 输出侧安全检查
    "不能让模型自己说安全"是同一条原则，这里换成"不能让客户端自己说安全"。
    重新算的时候 syndrome 传空字符串——`assess_formula_safety` 只有
    thermal_warning（寒热警告，警告级，不影响 blocking）依赖 syndrome，
    incompatible/dose_violations（两项拦截级判据）完全不看 syndrome，
    空字符串不会让 blocking 的判定失真，只是这次重算不会产出寒热警告文案
    （这条接口的请求体本来就没有 syndrome 可用，见 PrescriptionExportRequest
    的字段选择）。

    `safety.blocking` 为真且没有非空 `override_reason` 时拒绝导出（422，
    列出具体问题）；医生传了非空 override_reason 就放行——这条理由连同
    完整的 safety_at_export 一起写进审计记录，是"医生明知有问题仍坚持导出"
    唯一的书面记录。
    """
    safety = assess_formula_safety("", req.formula.herb_items)
    override_reason = (req.override_reason or "").strip()
    if safety.blocking and not override_reason:
        problems = []
        if safety.incompatible:
            problems.append("配伍禁忌：" + "、".join(f"{a}与{b}" for a, b in safety.incompatible))
        if safety.dose_violations:
            problems.append("剂量超限：" + "、".join(
                f"{v.herb} {v.dose}{v.unit}（上限 {v.limit_g}g，{v.reason}）" for v in safety.dose_violations
            ))
        raise HTTPException(
            status_code=422,
            detail={
                "message": "该方存在拦截级安全问题，拒绝导出。如需坚持导出，"
                            "请传非空 override_reason 并对该理由负责。",
                "problems": problems,
                "safety": _safety_dict(safety),
            },
        )

    diffs = compute_herb_diffs(req.model_suggestion, req.formula)
    text = format_pharmacy_text(req.formula)
    record = append_audit({
        "doctor_id": req.doctor_id,
        "patient_ref": req.patient_ref,
        "model_suggestion": req.model_suggestion.model_dump(),
        "final": req.formula.model_dump(),
        "diffs": diffs,
        "safety_at_export": _safety_dict(safety),
        "override_reason": override_reason or None,
    })
    return {"text": text, "audit_id": str(record.seq)}


# 静态文件挂在 /app，不要挂在根路径——否则会遮蔽上面的 API 路由。
app.mount("/app", StaticFiles(directory=str(WEB_ROOT), html=True), name="web")
