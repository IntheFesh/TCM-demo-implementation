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

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.chain import consult, explained_symptoms
from core.diseases import get_disease, triage_advice
from core.herbs import is_western_drug, strip_dose_and_parens
from core.physicians import PHYSICIANS
from core.schemas import S1Normalize
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


@app.get("/health")
async def health() -> dict:
    """async def 而不是 def：同步端点跑在 anyio 的线程池里（默认 40 个槽），
    几十条并发问诊把槽占满时，存活探针也跟着排队、超时，编排器会把一个其实
    还活着的进程重启掉。这个端点不做任何 IO，直接在事件循环上答。"""
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
    nodes = []
    for node_id, data in store.g.nodes(data=True):
        node_data = {"id": node_id, "label": data.get("name", node_id)}
        for k, v in data.items():
            if k != "name":
                node_data[k] = v
        nodes.append({"data": node_data})

    edges = []
    for src, dst, key, data in store.g.edges(keys=True, data=True):
        edge_data = {"id": f"{src}::{dst}::{key}", "source": src, "target": dst}
        for k, v in data.items():
            edge_data["data_source" if k == "source" else k] = v
        edges.append({"data": edge_data})

    return {"nodes": nodes, "edges": edges}


@app.get("/api/graph")
def api_graph() -> dict:
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
    store = get_graph_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail=_public_text(f"未找到 {GRAPH_PATH}。先跑 offline/build_graph.py 建图谱骨架。"),
        )

    stats = compute_stats(store)
    return {
        "graph": _persistent_graph_to_cytoscape(store),
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
def api_consult(req: ConsultRequest) -> dict:
    slots = _acquire_consult_slot()
    try:
        outcome = consult(req.complaint, retriever_mode=req.retriever_mode)
    except ValueError as e:
        # 模式名不认识 = 请求写错了，是 400 不是 500。只有这一种 ValueError 能
        # 从 consult() 冒到这里（consult 开头就校验了 retriever_mode，其余路径
        # 的检索问题都被包成 RetrievalUnavailable 走返回值，不抛异常）。
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        slots.release()
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
def api_consult_stream(req: ConsultRequest) -> StreamingResponse:
    """retriever_mode 跟 /api/consult 一样逐请求传下去。模式名不认识时这条
    路径不回 400 而是发一个 error 事件——不是漏了，是刻意：模式合法性只在
    core.chain.consult() 一处校验（对着 ALLOWED_MODES），在这里再判一次等于
    把同一个判断连同错误文案实现两遍。worker 里 consult() 抛的 ValueError 会
    被兜成 error 事件，消息跟 400 那条完全一样，前端的 error 分支照样能显示。
    """
    slots = _acquire_consult_slot()
    stream = _ConsultStream(secrets.token_urlsafe(12), slots)
    with _streams_lock:
        _streams[stream.stream_id] = stream

    def worker() -> None:
        try:
            outcome = consult(
                req.complaint,
                ask_fn=stream.ask,
                on_step=stream.emit,
                retriever_mode=req.retriever_mode,
            )
            stream.events_q.put(("done", _consult_response(outcome, role=req.role)))
        except StreamClosed:
            pass  # 客户端已断开，没人读了，正常提前结束
        except ValueError as e:
            # 跟 /api/consult 的 400 同一类：请求本身写错（模式名不认识），
            # 消息是给用户看的、不含内部信息，原样发
            stream.events_q.put(("error", {"detail": str(e)}))
        except Exception as e:  # noqa: BLE001 - 后台线程的异常不会自己冒泡到 HTTP
            # 响应里，必须在这兜住转成一个 error 事件；不然客户端只会看到连接
            # 挂在那不动，什么错误信息都拿不到。
            stream.events_q.put(("error", {"detail": _public_error_detail(e)}))
        finally:
            stream.finish()

    threading.Thread(target=worker, daemon=True).start()

    async def gen() -> AsyncIterator[str]:
        try:
            yield _sse("stream_id", {"stream_id": stream.stream_id})
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


# 静态文件挂在 /app，不要挂在根路径——否则会遮蔽上面的 API 路由。
app.mount("/app", StaticFiles(directory=str(WEB_ROOT), html=True), name="web")
