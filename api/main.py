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
from core.chain import (
    SYNTHESIS_PHYSICIAN_ID,
    consult,
    current_asking_physician,
    explained_symptoms,
)
from core.diseases import get_disease, triage_advice
from core.examples import EXAMPLE_COMPLAINTS
from core.followup import stop_label
from core.herbs import is_western_drug, strip_dose_and_parens
from core.llm import (
    ByokBackend,
    LLMAuthError,
    check_api_key,
    get_llm,
    s3_mode,
    use_llm,
)
from core.node_explain import syndrome_row
from core.react import react_enabled
from core import usage as usage_mod
from core.physicians import (
    PHYSICIANS,
    synthesis_display,
    physicians_all,
    physicians_enabled,
    resolve_physician_id,
)
from core.prescription import compute_herb_diffs, format_pharmacy_text
from core.formula_check import advice_dicts, check_formula
from core.safety_output import (
    INCOMPATIBLE_TRAINING_NOTE,
    assess_formula_safety,
    check_incompatible,
)
from core.schemas import FormulaCandidate, FormulaSafety, HerbItem, S1Normalize
from core.tools import GRAPH_PATH, get_graph_store, search_cases
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

#: SSE 事件队列的上限。R40 背压。取 2000 的依据：一次问诊的增量事件实测在
#: 千条量级（`S3DeltaEmitter` 每积累到一定字数发一条），2000 给了一倍余量，
#: 而每条事件的字典很小（几十到几百字节），2000 条 ≈ 几百 KB/流。
#: 上限太小会让正常的快客户端也开始丢增量；太大就退化成无上限。
SSE_QUEUE_MAXSIZE = int(os.environ.get("SSE_QUEUE_MAXSIZE", "2000"))

#: 非增量事件最多等多久。超过就按"客户端不读了"收尾。
#: 生成器每 50ms 轮询一次队列，正常情况下这个等待是微秒级；30 秒还塞不进去
#: 说明连接真的死了（TCP 窗口关死、对端不再 ack）。
SSE_PUT_TIMEOUT_SECONDS = float(os.environ.get("SSE_PUT_TIMEOUT_SECONDS", "30"))

#: 队列满时**可以丢**的事件名。只有"同一段文字的逐步生成"属于这一类：
#: 它们的终值由 `s3_done` / `done` 兜底，丢掉不影响结果的正确性。
#: **这张表只许收窄，不许扩张**——把 `need_input` 或 `done` 放进来就等于
#: 允许静默丢结果。
SSE_DROPPABLE_EVENTS = frozenset({"s3_delta"})


def _warmup() -> None:
    """启动时预热，把首请求那几十秒挪到启动阶段。**两项并行**，每一项失败
    都不阻塞启动——数据不全时服务仍应能起来，预热不是前置条件。

    实现整个在 `api/warmup.py`（状态机 + 并行 + 进度快照），这里只是一层壳：
    `/health` 要报进度，进度就得有个地方存，而那份状态跟"跑预热"是同一件事的
    两面，分在两个模块里会各存一份（R40 之前 `/health` 根本报不出进度，
    因为预热没有状态，只有 stderr 上两行 print）。

    两项为什么无依赖、为什么并行省得下来：见 `api/warmup.py` 的模块文档
    （实测本体层 2417 ms、检索器 8757 ms，串行 11174 ms）。
    """
    from api.warmup import run_warmup

    run_warmup()


# 预热最多等这么久——**现在这个数只用于"等预热完成"的工具（压测、冒烟脚本）**，
# 不再是"服务什么时候开始监听"的闸门：R40 起 startup 阶段立刻 yield，服务先
# 监听、预热在后台跑、`/health` 在就绪前回 503 带进度。
#
# 旧行为踩到的坑留在这里当反面教材：有 cases.json 但连不上 huggingface 的机器，
# 预热卡在模型下载的重试上，ASGI startup 走不完，uvicorn **端口开着但一个请求
# 都不答**，存活探针连得上却等不到响应——编排器会把一个其实正常的进程判死。
WARMUP_TIMEOUT_SECONDS = float(os.environ.get("WARMUP_TIMEOUT_SECONDS", "120"))


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """**先监听，再预热。** startup 阶段不等预热——ASGI 的 startup 没走完
    uvicorn 就不会开始处理请求，等在这里等于"端口开着但不答"。

    预热是同步的重 IO（加载模型、编码语料），放在自己的线程里而不是直接在
    事件循环上跑：直接阻塞循环会让 uvicorn 的信号处理一起卡住，那段时间
    Ctrl-C 都停不下来。不走 anyio 的线程池——那里的线程等不到就取消不了。
    """
    from api.warmup import TRACKER

    # **同步登记再起线程**：lifespan 起完线程立刻 yield，第一个 readiness 探针
    # 可能比线程的第一行还早。见 `WarmupTracker.begin` 的文档字符串。
    TRACKER.begin()
    t = threading.Thread(target=_warmup, name="warmup", daemon=True)
    t.start()
    yield


app = FastAPI(title="名医辨证对照 demo", lifespan=_lifespan)


# ---------- R43：响应压缩（**选择性**，不是无脑全开） ----------
#
# 实测（R43 基线）：`/api/graph?limit=200` 的响应体 **352 KB，一个字节都没压**
# ——这是点开「图谱」页签之后用户在等的那一段。三甲内网的带宽不是问题，但
# 教材扩完之后这张图要翻几倍（证候 337 → 1444、症状 1282 → 约 7000），
# 而 JSON 是压缩率最高的那一类数据。
#
# **两类必须跳过，无脑全开会出事：**
#
# 1. **SSE（`text/event-stream`）。** starlette 的 GZipMiddleware 对流式响应是
#    逐块写进 gzip 缓冲再发，而 gzip 在攒够一个块之前不产出任何字节——于是
#    "一边推理一边出字"会变成"憋一会儿吐一大段"。R36 花了一整轮把流式做出来，
#    不能在这里被压缩缓冲抵消掉。
# 2. **已经压过的二进制**（woff2 / png / 图片）。再压一遍省不下几个字节，
#    却要为每个请求付一次 CPU；字体那 1.13 MB 是首屏的大头，白烧 CPU 会
#    直接体现在首屏时间上。
#
# 判据按**路径**定而不是按 content-type：中间件在响应头出来之前就要决定走不走
# 压缩，按路径是确定的、可测的；按 content-type 要先等响应开始、逻辑绕一圈，
# 而这两条规则本来就跟路径一一对应。
GZIP_MIN_BYTES = 1024
#: 不压的路径前缀。SSE 那条见上；`/app/vendor/fonts` 与图片同理。
GZIP_SKIP_PREFIXES = ("/api/consult/stream",)
#: 不压的扩展名（已经是压缩格式）。
GZIP_SKIP_SUFFIXES = (".woff2", ".woff", ".png", ".jpg", ".jpeg", ".webp", ".gz")


def _gzip_skip(path: str) -> bool:
    return (path.startswith(GZIP_SKIP_PREFIXES)
            or path.endswith(GZIP_SKIP_SUFFIXES))


class SelectiveGZipMiddleware:
    """按路径决定要不要走 gzip。**压缩本身复用 starlette 的实现**，
    这里只负责"走不走"——自己写一遍 gzip 响应器就是同一件事的第二处实现。"""

    def __init__(self, app, minimum_size: int = GZIP_MIN_BYTES) -> None:
        from starlette.middleware.gzip import GZipMiddleware

        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or _gzip_skip(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


app.add_middleware(SelectiveGZipMiddleware)


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


def _lambda1_note_or_none() -> str | None:
    """图谱没建过（这台机器上没有 data/graph.json）时返回 None 而不是抛：
    /health 是存活探针，不该因为一个可选的数据文件缺失就报 503。"""
    store = get_graph_store()
    if store is None:
        return None
    return lambda1_note(compute_stats(store))


@app.get("/health/live")
async def health_live() -> dict:
    """**存活**探针：只要进程在跑就 200，预热到哪一步都不影响它。

    跟 `/health`（就绪）分开，因为两个探针问的不是同一个问题——
    存活答"要不要重启我"，就绪答"能不能把流量放进来"。合成一个的代价是
    真实的：R40 之前只有一个端点，预热期间编排器分不清"还在热"和"已经死"，
    只能靠调长探针超时来将就。
    """
    from api.warmup import TRACKER

    return {"status": "alive", "warmup": TRACKER.snapshot()}


@app.get("/health")
async def health(response: Response) -> dict:
    """**就绪**探针 + 前端启动所需的那几份配置。

    async def 而不是 def：同步端点跑在 anyio 的线程池里（默认 40 个槽），
    几十条并发问诊把槽占满时，存活探针也跟着排队、超时，编排器会把一个其实
    还活着的进程重启掉。这个端点不做任何 IO，直接在事件循环上答。

    **预热没完成时回 503**，响应体照样完整（外加 `warmup` 进度块）：
    编排器看状态码，前端读响应体。只回一个空 503 的话前端在预热那几秒里
    连医家身份色都拿不到，页面是一片没有颜色的骨架——比"晚几秒着色"更糟。
    """
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
    from api.warmup import TRACKER

    warmup_block = TRACKER.snapshot()
    if not warmup_block["ready"]:
        # 503 而不是 200+标记：编排器只看状态码，一个 200 会让流量在知识库
        # 还没加载完时就被放进来，首个患者等的是那 11 秒。
        response.status_code = 503
    return {
        "status": "ok" if warmup_block["ready"] else "warming",
        "demo_mode": demo_mode_info(),
        "physicians": [
            {"id": pid, "name": info["name"], "years": info["years"],
             "school": info["school"], "color": info["color"],
             "color_bg": info["color_bg"], "enabled": info.get("enabled", True)}
            # **全部**医家，带 enabled 标记：三列只摆 enabled 的，而「参考医家」
            # 引用区要摆 enabled=False 的——前端两处各取所需，但只拉一次。
            # 只下发 enabled 的话前端就没法给李可的引用块上色、显示姓名。
            for pid, info in physicians_all(PHYSICIANS).items()
        ],
        # example_complaints：R14 首屏的三条可点击示例。**下发而不是写死在
        # app.js 里**——CLAUDE.md 第 31 条前端小节写明"写死的常量也算一处实现"，
        # 而这三条已经在 DEMO.md 和录制清单里各有一份。回放按主诉原文的哈希索引，
        # 三份副本漂了一个标点，演示当场 LLMError（core/examples.py 的由来）。
        "example_complaints": [dict(e) for e in EXAMPLE_COMPLAINTS],
        # lambda1_note：R16 §3.2 规格 2 要求**问诊图上也有那一行说明**
        # （"当前语料中医案术语与国标不对齐，医家层权重为 0，边权重显示的是
        # 标准先验层"）。之前只有图谱浏览器那张图有——而问诊图的边同样是
        # 等透明度的，看图的人同样会以为图没建好。
        #
        # 文字来自 offline/graph_stats.lambda1_note() 这唯一一处，前端原样显示、
        # 不改写不精简一个字：那段话是这个项目的一个真实发现，弱化它比图上有
        # bug 更严重。
        #
        # 放 /health 而不是问诊响应里：这行说明跟"这一次问诊"无关，它描述的是
        # 图谱本身，而且页面一加载就该能显示。图谱没建过时是 None，前端不显示
        # 这一行——不是显示一句"未知"。
        "lambda1_note": _lambda1_note_or_none(),
        # R37：这台服务的 S3 形状（structured / legacy）。**前端要在问诊开始之前
        # 就知道它**：structured 是单链九段、legacy 是三列集注，两种形态的骨架
        # 完全不同。等到 done 事件里的 manifest 才知道的话，跑的那几十秒里只能
        # 先摆一个可能是错的骨架，然后当场换掉——那一下闪烁正是"界面在猜"的表现。
        #
        # 这是**服务端配置的默认值**，不是某一次问诊的结果：`consult()` 支持
        # 逐请求覆盖（`s3_mode_override`），但界面上没有这个开关，所以这里报
        # 默认值是准确的。每次问诊结束仍然以 `manifest.s3_mode` 为准（那一份
        # 记的是真的跑了哪一条），两处不一致时前端信 manifest。
        "s3_mode": s3_mode(),
        # R40：预热进度。`ready=false` 时上面那个 503 才有可读的原因，
        # 前端据此显示"正在加载知识库（1/2）"而不是干等。
        "warmup": warmup_block,
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
    ambiguous = ambiguous_syndrome_keys(store)
    nodes = [_node_payload(nid, d, ambiguous) for nid, d in store.g.nodes(data=True)]
    edges = [_edge_payload(u, v, k, d) for u, v, k, d in store.g.edges(keys=True, data=True)]
    return {"nodes": nodes, "edges": edges}


def _node_payload(node_id: str, data: dict,
                  ambiguous: set[tuple[str, str]] | None = None) -> dict:
    node_data = {"id": node_id, "label": _display_label(node_id, data, ambiguous)}
    for k, v in data.items():
        if k != "name":
            node_data[k] = v
    return {"data": node_data}


def ambiguous_syndrome_pairs(pairs) -> set[tuple[str, str]]:
    """哪些 `(证型名, 病名)` 组合在这一批里**不止一条**。

    **两张图共用这一处**（R37）：图谱浏览器扫的是持久图的证型节点，问诊图扫的是
    这一次几位医家给出的证型——问的是同一个问题（"这个标签在这张图上分得清吗"），
    所以判断只有一处。摆成一行还是两行是排版，不是同一个问题，各图自己决定。
    """
    seen: dict[tuple[str, str], int] = {}
    for name, disease in pairs:
        key = (name or "", (disease or "").strip())
        seen[key] = seen.get(key, 0) + 1
    return {k for k, n in seen.items() if n > 1}


def syndrome_code_suffix(*, ambiguous: bool, code: str | None) -> str:
    """撞名时补的那截编码。**补不补这件事只有这一处判断。**

    只在**确实还撞着**且**真有编码**时补：给每条都挂编码会让图上全是 TB-xxx
    的噪音，而没有编码时补一个空括号比不补更糟（那是"查过了、没有"和"没查"
    分不开的经典形状）。
    """
    return f"（{code}）" if (ambiguous and code) else ""


def ambiguous_syndrome_keys(store) -> set[tuple[str, str]]:
    """哪些 (证型名, 病名) 组合在图里**不止一条**。

    加病名限定之后仍有 15 组撞在一起（「热证（胃痛）」有三条、「肝火犯肺证（咳嗽）」
    有两条），而它们的 definition 各不相同且**对不上自己的名字**
    （TB-114「热证」的定义是「肝郁化火，横逆犯胃」，TB-115 的是「脾胃虚寒，胃失和降」
    ——脾胃虚寒挂在"热证"名下）。

    **根因分五类，R29 查清前四类、R31 修掉主导那一类**（R28 原来写的"名字列跟
    定义列错位"描述错了机制）：扫描件里证型标题有四种写法，R31 之前只认两种，
    认不出 `7.痰火扰心`（丢了行首 `#`）这种，上一条证型的名字就被下一条沿用。
    R31 统一判定之后这个数从 15 掉到 3，剩下 3 组是**块边界配错位**，
    是另一个根因（见 tests/test_syndrome_disease_label.py 最后那条判据）。
    显示层能做的是**不装作它们一样**：这几组再补一个 code。
    """
    return ambiguous_syndrome_pairs(
        (d.get("name") or "", d.get("disease") or "")
        for _nid, d in store.g.nodes(data=True)
        if d.get("node_type") == "syndrome")


def _display_label(node_id: str, data: dict,
                   ambiguous: set[tuple[str, str]] | None = None) -> str:
    """节点在图上显示什么。证型带病名限定，其余原样。

    **为什么必须带**：174 个证候节点里 52 个重名（21 个名字重复 2–4 次；
    R28 178/66/25 → R29 177/67/26 → R31 174/52/21，降的两轮分别是
    OCR 修正表覆盖 name 列、和把扫描件里四种证型标题写法统一成一处判定）。
    「肝郁气滞证」分属腹痛/胁痛/积聚/癃闭四个病名，**病机各不同**——
    数据是对的（`data/standard/syndromes.jsonl` 有 disease 字段，build_graph 也
    把它写进了节点），丢信息的是显示层：图上并排四个一模一样的方块，
    点开才知道不是同一个证。

    **只改 label，不动 `name`**：`/api/graph/search` 匹配的是 `name`
    （见 api_graph_search），跟着改会让搜「肝郁气滞证」因为多出括号而漏掉全部条目。
    17 条国标条目没有 disease，那时不加括号——一个空括号比没有更糟。

    **病名另起一行**（`\n`，cytoscape 的 `text-wrap: wrap` 认它）。写成一行的
    代价是量出来的：加了病名之后节点最宽到 **169px**（原来约 100px），
    图谱浏览器那 20 个证型当场压字 8 对——`docs/screenshots/r24_rings.png` 的判据
    立刻红了。换行之后宽度回到"名字和病名里较长的那个"，高度多一行。
    """
    name = data.get("name", node_id)
    if data.get("node_type") != "syndrome":
        return name
    disease = (data.get("disease") or "").strip()
    # 病名 + 编码都齐时才补编码，而且只在这一组确实还撞着的时候补——
    # 给每条都挂编码会让图上全是 TB-xxx 的噪音。
    suffix = syndrome_code_suffix(
        ambiguous=bool(ambiguous and (name, disease) in ambiguous),
        code=data.get("code"))
    if suffix:
        inner = f"{disease} {data['code']}" if disease else str(data["code"])
        return f"{name}\n（{inner}）"
    return f"{name}\n（{disease}）" if disease else name


def _symptom_counts_by_syndrome_code(store) -> dict[str, int]:
    """证型编码 → 挂在它上面的症状数。

    **症状不直接连证型**：这张图里症状连的是证素（`indicates` 边），
    是哪个证型把它们串起来记在**边的 `via_syndrome` 属性**上（build_graph 的形状）。
    所以"这个证型有多少症状"数不出来自节点的邻居，只能扫边。
    第一版写成数 symptom 邻居，实测 61 个证型**全部返回 0**——一个恒为 0 的
    排序键跟没有排序一样，而它不会报错。
    """
    by_code: dict[str, set] = {}
    for u, _v, _k, d in store.g.edges(keys=True, data=True):
        if d.get("edge_type") != "indicates":
            continue
        code = d.get("via_syndrome")
        if not code:
            continue
        by_code.setdefault(code, set()).add(u)
    return {code: len(srcs) for code, srcs in by_code.items()}


def _symptom_count(store, node_id: str, data: dict, by_code: dict[str, int]) -> int:
    """挂在这个节点上的症状数。**一个问题，两种编码**：证型走边上的
    `via_syndrome`，其余节点（证素等）走直接邻居。合成一个函数是因为
    调用方问的是同一句话，分两处会让"证型怎么数"散到调用点上去。
    """
    if data.get("node_type") == "syndrome":
        return by_code.get(data.get("code"), 0)
    g = store.g
    return len({nb for nb in (list(g.successors(node_id)) + list(g.predecessors(node_id)))
                if g.nodes[nb].get("node_type") == "symptom"})


def _node_with_symptom_count(store, node_id: str, data: dict, by_code: dict[str, int],
                             ambiguous: set[tuple[str, str]] | None = None) -> dict:
    payload = _node_payload(node_id, data, ambiguous)
    payload["data"]["n_symptoms"] = _symptom_count(store, node_id, data, by_code)
    return payload


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
        # **在循环外算一次。** `ambiguous_syndrome_keys` 要扫一遍全图的节点，
        # 写在推导式里就是"每个节点扫一遍全图"——200 个节点的一页会扫
        # 200 × 1312 = 26 万次，而它的结果跟节点无关。
        ambiguous = ambiguous_syndrome_keys(store)
        graph = {
            "nodes": [_node_payload(nid, store.g.nodes[nid], ambiguous)
                      for nid in page_ids],
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
            # 图谱浏览器的 λ1 医家下拉：全部医家。李可/王云启的医案也挂在图上，
            # 下拉里没有他们等于看不到他们那部分边的权重。
            for pid, info in physicians_all(PHYSICIANS).items()
        ],
        "stats": {
            "node_type_counts": stats["node_type_counts"],
            "edge_type_counts": stats["edge_type_counts"],
        },
    }


@app.get("/api/graph/neighbors")
def api_graph_neighbors(node: str, limit: int = 200, node_types: str | None = None) -> dict:
    """展开一个节点的邻居。

    分页之后**必须有这个端点**：原来前端一次拿全图、在本地邻接表上展开，
    图一分页本地就没有全量邻接表了，展开会静默只展开"恰好在本页里"的那部分。
    无向看待（进边出边都算）——图谱浏览器展示的是关联，不是流向。

    R16 加 `node_types`：图谱浏览器的展开是**分层**的（§3.2 规格 6：点证素出
    证型、点证型出症状），不是"把全部邻居倒出来"。实测「肝」这个证素有 499 个
    邻居，其中绝大多数是症状——不筛类型的话点一下就是 150 个症状铺满画布，
    那张图跟 R16 要治的 F6（一屏互不相连的方块）是同一个病。

    **筛在服务端**：前端筛意味着先把 499 个全拉回来再扔掉 480 个，而且
    `limit` 会先在服务端把想要的那些截掉——截断发生在筛之前，结果是
    "限 150 个邻居里恰好有几个证型就显示几个"，而那个数完全取决于
    networkx 的遍历顺序。这种错不会报错，只会让人以为脾没几个证型。
    """
    store = _require_store()
    if not store.g.has_node(node):
        raise HTTPException(status_code=404, detail=_public_text(f"图里没有这个节点：{node}"))

    wanted = {x.strip() for x in (node_types or "").split(",") if x.strip()}
    seen: dict[str, dict] = {}
    edges = []
    for u, v, k, d in store.g.edges(node, keys=True, data=True):
        edges.append(_edge_payload(u, v, k, d))
        seen.setdefault(v, store.g.nodes[v])
    for u, v, k, d in store.g.in_edges(node, keys=True, data=True):
        edges.append(_edge_payload(u, v, k, d))
        seen.setdefault(u, store.g.nodes[u])
    seen.pop(node, None)
    if wanted:
        seen = {nid: d for nid, d in seen.items() if d.get("node_type") in wanted}

    total = len(seen)
    picked = list(seen.items())[:max(limit, 0)] if limit and limit > 0 else list(seen.items())
    keep = {nid for nid, _ in picked} | {node}
    symptom_counts = _symptom_counts_by_syndrome_code(store)
    ambiguous = ambiguous_syndrome_keys(store)   # 循环外算一次，见 /api/graph 那一处的注释
    return {
        "graph": {
            # `n_symptoms` 是**给前端排序用的派生字段**：图谱浏览器一次只画得下
            # 14 个（GB_EXPAND_CAP），"是哪 14 个"得有依据，按症状数取前 14 是
            # 那个依据。前端算不了——它手里只有这一页，证型→症状那一层还没加载。
            "nodes": [_node_with_symptom_count(store, nid, d, symptom_counts, ambiguous)
                      for nid, d in picked],
            "edges": [e for e in edges
                      if e["data"]["source"] in keep and e["data"]["target"] in keep],
        },
        "page": {"limit": limit, "returned": len(picked), "total": total,
                 "truncated": len(picked) < total},
    }


#: 节点释义接口的入参长度上限。**不是怕慢，是怕日志/错误信息里被塞长串**
#: （同 MAX_COMPLAINT_CHARS 那条理由）。R42 之后节点 id 最长的形状是
#: `herb::{方名}::{药名}`（去掉了医家段），200 字绰绰有余。
MAX_NODE_ID_CHARS = 200

#: 这个接口的耗时预算。**性能预算进测试**（总纲 §12）：R42 把四节扩成八节，
#: 新增的三节要读方剂本体、功效同义表、规律层——都是惰性初始化的全量表，
#: 第一次点开会把它们全加载一遍。预算按**热态**定（本体已加载），
#: 判据在 tests/test_node_explain_perf.py：p95 ≤ 50 ms。
NODE_EXPLAIN_BUDGET_MS = 50


@app.get("/api/node_explain")
def api_node_explain(node: str, name: str | None = None) -> dict:
    """R37/R42：图上一个节点的**八节**释义。**零 LLM 调用**，
    判据全在 core/node_explain.py。

    八节：是什么 / 病机 / 药理 / 出处原文 / 名老中医经验 / 验证结果 / 循证对照 / 注意。
    R42 新增的四节（病机、药理、验证结果、循证对照）对应九层图新增的节点类型
    （病机、治则、治法）和"每个数字都要有对照基准"那条铁律。

    `name` 是显示名覆盖：问诊图证型节点的 label 是「病名 · 证型」拼出来的，
    而证候表里存的是证型名；节点 id 里那一段还可能带方名。所以前端把 label
    一起传来。

    取不到时返回 `available=False` + 一句 `note`，**HTTP 仍然是 200**：
    "这个节点没有释义"不是错误，而 4xx 会让前端把它当故障弹红条。
    前端据 `available` 整块隐藏这个面板，不显示"暂无信息"的空壳。
    """
    if len(node or "") > MAX_NODE_ID_CHARS or len(name or "") > MAX_NODE_ID_CHARS:
        raise HTTPException(status_code=400,
                            detail=f"node/name 超过 {MAX_NODE_ID_CHARS} 字")
    from core.node_explain import explain_node

    return explain_node(node, name=name)


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
    ambiguous = ambiguous_syndrome_keys(store)   # 循环外算一次，见 /api/graph 那一处的注释
    return {
        "graph": {
            "nodes": [_node_payload(nid, d, ambiguous)
                      for nid, d in picked],
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

    # 额度预扣按**参与集注**的医家数算：一次问诊只跑他们。
    estimate = usage_mod.estimate_calls(react_enabled(), len(physicians_enabled(PHYSICIANS)))
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
    manifest = outcome.get("manifest") or {}
    calls = int(manifest.get("llm_calls") or 0)
    ledger.settle(token, calls)
    # R21：token 用量与高峰调用数在结算的同一处记——记在别处就会有一条路径
    # （断流、异常）漏记，而漏记的表现是用量面板上的数偏小、看不出漏了哪次。
    # 从 manifest 取而不是再问一次 ContextVar：那份统计是**这一次问诊**的，
    # 而 _settle 可能在别的线程/更晚的时刻跑，ContextVar 那时已经不是同一份了。
    ledger.record_tokens(
        {
            "prompt_cache_hit_tokens": manifest.get("cache_hit_tokens"),
            "prompt_cache_miss_tokens": manifest.get("cache_miss_tokens"),
            # 输出 token manifest 里没有单列（它在 usage 统计里），命中/未命中
            # 两项才是这一轮要看的；输出量用 0 占位会让面板上那个数假装是真的，
            # 所以干脆不传这一项（record_tokens 用 or 0，缺键就是不加）。
        } if manifest.get("cache_hit_tokens") is not None else None,
        calls=calls,
    )


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


# R18-I：「参考医家」——注册表里 enabled=False 的那几位（李可、王云启）。
#
# 他们**不参加集注**（那是 physicians_enabled 的事），但语料在库里，学生/研究者
# 模式下应该能看到"同一条主诉，这几位的医案里最像的是哪三条"。做成一个独立
# 接口而不是塞进 /api/consult 的返回：集注是分钟级的 LLM 调用，检索是毫秒级的，
# 合在一起会让这一栏跟着三列一起等。
REFERENCE_CASES_K = 3


@app.get("/api/reference_cases")
def api_reference_cases(complaint: str, physician: str) -> dict:
    """某位医家的医案里跟这条主诉最像的前三条（默认 k=3）。

    三种"空"分开报，沿用 core/tools.py::search_cases 的分法（SOURCES.md 第 31 条
    那个坑）：参数错 → `error` 并列出可用值；数据文件不存在 → `available: false`；
    真没匹配 → `note` 带"已查 N 条"。前端据此显示不同的话，而不是一律"没有结果"。

    每条医案带 `incompatible_pairs` 和 `note`：判定走 core.safety_output 的
    check_incompatible（唯一实现），提示语走同一个模块的
    INCOMPATIBLE_TRAINING_NOTE（唯一定义）——**界面显示的那句话必须跟训练样本
    里的那句逐字相同**，各写一句的话界面就在替模型背书它没学过的话。
    """
    text = (complaint or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="complaint 不能为空")
    if len(text) > MAX_COMPLAINT_CHARS:
        raise HTTPException(status_code=422,
                            detail=f"complaint 超过 {MAX_COMPLAINT_CHARS} 字")
    out = search_cases(text, physician, k=REFERENCE_CASES_K)
    pid = resolve_physician_id(physician)
    info = physicians_all(PHYSICIANS).get(pid or "", {})
    for c in out.get("cases") or []:
        pairs = check_incompatible(c.get("herbs") or [])
        c["incompatible_pairs"] = [f"{a}-{b}" for a, b in pairs]
        # 有反药配对才带这句话；没有就不带，不要每条都挂一句让人习惯性忽略它。
        c["note"] = INCOMPATIBLE_TRAINING_NOTE if pairs else None
    out["physician"] = {
        "id": pid, "name": info.get("name"),
        "color": info.get("color"),
        "enabled": info.get("enabled", True),
        "source": info.get("source"),
    }
    out["k"] = REFERENCE_CASES_K
    return out


@app.post("/api/usage/validate-key")
def api_validate_key(x_llm_key: str | None = Header(default=None)) -> dict:
    """用 DeepSeek 官方的「查询余额」接口验一把访问者填的 key，**零 token 消耗**。

    没有这个端点的话，填错 key 的人只能靠跑一次问诊才知道——而那一次可能已经
    走完 S1/S2。返回里不回显 key。
    """
    key = _byok_key(x_llm_key)
    if not key:
        raise HTTPException(status_code=400, detail="没有收到 key。")
    out = dict(check_api_key(key))
    # R21：BYOK 的第一次问诊要把 50–90 万 token 的知识前缀送上去（未命中价），
    # 之后每次几乎全命中。**验 key 的时候就说**——等他跑完第一次看到账单
    # 再说就晚了。两个数从 context_prefix 现算，不写死：前缀变大它们就跟着变。
    out["prefix_warmup_note"] = _prefix_warmup_note()
    return out


#: 价格表和 token→人民币的折算都在 `core/usage.py`（R26 搬过去的，第 31 条：
#: 蒸馏脚本要算同一件事）。这里只是把名字引过来，方便本模块和既有调用方读。
PRICE_USD_PER_MTOK_MISS = usage_mod.PRICE_USD_PER_MTOK_MISS
PRICE_USD_PER_MTOK_HIT = usage_mod.PRICE_USD_PER_MTOK_HIT
PRICE_USD_PER_MTOK_OUT = usage_mod.PRICE_USD_PER_MTOK_OUT
USD_TO_CNY = usage_mod.USD_TO_CNY


def _prefix_warmup_note() -> str:
    """「首次问诊会预热知识前缀，约 ¥X；之后每次约 ¥Y」。数字现算。

    取不到前缀大小（没有 cases.json / 药理层文件）时**说取不到**，
    不给一个编的数——一个编出来的成本数比不给更糟。
    """
    try:
        from core.context_prefix import budget_plan

        plan = budget_plan()
        per_phys = [sum(v.values()) for v in plan.tokens_by_physician_after.values()]
    except Exception:  # noqa: BLE001 —— 提示语不该让验 key 失败
        return ("首次问诊会预热知识前缀（这台机器上算不出它有多大：缺 cases.json "
                "或药理层文件），之后每次几乎全部命中缓存、便宜一个数量级。")
    total = sum(per_phys)
    # 高峰价报，谷段五折在下面那句话里说——报低的那个数会让人以为随时都这么便宜。
    first = usage_mod.cost_cny(miss_tokens=total, peak=True)
    later = usage_mod.cost_cny(hit_tokens=total, peak=True)
    return (f"首次问诊会预热知识前缀（{total:,} token，{len(per_phys)} 位医家合计），"
            f"约 ¥{first:.1f}；之后每次约 ¥{later:.2f}（缓存命中价差 30 倍）。"
            "谷段（北京 12–14、18–09）再打五折。")


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
        # R44：这一次代理做过的决策（停/问/取证/验）。**给所有角色**——
        # 患者最需要知道的正是"为什么让我去急诊"，而那句话就在这里。
        # 规则表在 core/agent.py，这里只是原样下发。
        "agent_trace": outcome.get("agent_trace") or [],
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
#: R36：多久没有事件就发一帧心跳。
#:
#: 为什么要它：一次问诊里 S3 那一步要等几十秒，这期间**一个字节都不发**。
#: nginx 的 `proxy_read_timeout` 默认 60 秒、多数 CDN / 反代在 30~120 秒之间掐
#: 空闲连接——掐掉的表现是浏览器那边流突然结束、没有 error 事件、没有 done 事件，
#: 前端只能显示"转圈转到底"。15 秒是最紧的那个默认值（30 秒）的一半，留一倍余量。
#:
#: 发的是**一个真事件**而不是 SSE 注释行（`: ping`）：注释行前端看不见，
#: "还在跑"这件事就只能靠转圈暗示；而 heartbeat 事件带着已等待秒数，
#: 界面能说"已等待 42 秒"。老前端不认这个事件名也无害
#: （`describeProgressEvent` 对未知事件返回 null，不进日志、不报错）。
_HEARTBEAT_SECONDS = 15.0


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
        # R40 **背压**：有上限的队列。之前是 `queue.Queue()`（无上限）——
        # 客户端读得慢或者卡住时，后台线程照样按 token 频率往里塞 `s3_delta`，
        # 队列只涨不降。一条流的增量事件是**几千条**（每 N 个 token 一条），
        # 几十条慢连接就能把进程的内存吃掉，而这中间没有任何一处会报错。
        self.events_q: queue.Queue = queue.Queue(maxsize=SSE_QUEUE_MAXSIZE)
        # 被丢掉的增量条数。**丢了必须数出来**，不能静默——前端据此显示
        # "网络较慢，已跳过 N 条增量"，而不是让用户看到一段缺字的推理过程。
        self.dropped_deltas = 0
        self.cancel = threading.Event()
        self._slots = slots
        self._pending: queue.Queue | None = None
        self._pending_lock = threading.Lock()

    # ---- 后台线程侧（consult 的回调）----

    def emit(self, name: str, data: dict) -> None:
        """往流里塞一个事件。**两类事件两种背压策略**，不能合并成一种：

        · 增量事件（`SSE_DROPPABLE_EVENTS`）：队列满就**丢**，并计数。它们是
          "同一段文字的逐步生成"，丢掉几条只是打字机效果卡一下，终值由
          `s3_done` / `done` 兜底——而为它们阻塞后台线程，等于让一条慢连接
          把这次问诊整体拖慢。
        · 其余事件（阶段完成、需要追问、终值、错误）：**阻塞等**，让生产端
          慢到消费端的速度上。这才是真正的背压。丢掉任何一条都会让前端
          缺一段状态（`need_input` 丢了 = 追问永远等不到回答）。

        阻塞不是无限等：`SSE_PUT_TIMEOUT_SECONDS` 之后按"客户端已经不读了"
        处理，抛 `StreamClosed`——跟客户端断开走同一条收尾路径。
        """
        if self.cancel.is_set():
            raise StreamClosed()
        if name in SSE_DROPPABLE_EVENTS:
            try:
                self.events_q.put_nowait((name, data))
            except queue.Full:
                self.dropped_deltas += 1
            return
        try:
            self.events_q.put((name, data), timeout=SSE_PUT_TIMEOUT_SECONDS)
        except queue.Full as e:
            # 队列满了这么久 = 没人在读。跟客户端断开是同一件事，走同一条路。
            self.cancel.set()
            raise StreamClosed() from e

    def ask(self, question: str) -> str | None:
        answer_q: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending = answer_q
        try:
            # 先登记再发 need_input：客户端收到事件时答案一定已经有地方接。
            # physician：R14 的三列集注要把问题弹在**提问那位医家的列里**，其余
            # 两列显示"等待中"。谁在问由 core.chain 的 ContextVar 传过来——
            # AskFn 的契约是 (question) -> str|None，三处实现（命令行、患者
            # 模拟器、这里）都按这个签名写，加参数要同时改三处。
            # None 表示全局追问（run_followup 在三位医家之前跑，不属于任何一列），
            # 前端据此回落到输入区那个问答框，不是随便挑一列塞进去。
            asking = current_asking_physician()
            self.emit("need_input", {
                "question": question,
                "physician": asking[0] if asking else None,
                "physician_name": asking[1] if asking else None,
            })
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
        """收尾。哨兵告诉生成器可以退出了。

        **有超时**：队列有上限之后，一个没人读的满队列会让这里永远阻塞，
        后台线程于是永远不退出（daemon=True 只保证进程能退，不保证线程能回收
        它占的内存和那个信号量槽）。塞不进去就说明没人读，哨兵本身也没意义。
        """
        try:
            self.events_q.put((None, None), timeout=SSE_PUT_TIMEOUT_SECONDS)
        except queue.Full:
            pass
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
            # R40 背压：丢过增量就**说出来**，紧挨在 done 之前。
            # 单独一个事件而不是塞进 done 的载荷：done 的形状跟 /api/consult
            # 的响应体是同一份契约（`_consult_response` 是唯一实现），
            # 往里加一个只有流式路径才有的键会让那份契约分叉。
            if stream.dropped_deltas:
                stream.emit("deltas_dropped", {"n": stream.dropped_deltas})
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
            t_open = time.monotonic()
            last_sent = t_open
            while True:
                try:
                    name, data = stream.events_q.get_nowait()
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_sent >= _HEARTBEAT_SECONDS:
                        # 心跳只在**真的没别的东西可发**的时候发：它的作用是"别把
                        # 这条连接当空闲连接掐掉"，不是定时汇报。
                        last_sent = now
                        yield _sse("heartbeat", {"elapsed_s": round(now - t_open, 1)})
                    await asyncio.sleep(_STREAM_POLL_SECONDS)
                    continue
                if name is None:
                    return
                last_sent = time.monotonic()
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
    if followup is None:
        return None
    out = followup.model_dump()
    # 中文名跟着结论一起下发（同 `VerificationResult.to_dict` 的 `status_label`）：
    # 前端只负责显示，不再自己攒一张停因表。
    out["stopped_by_label"] = stop_label(out.get("stopped_by", ""))
    return out


def _refs_block(r: dict) -> dict:
    """`refs` + 三个对照数。抽成函数因为 `_serialize_result` 里那个字典字面量
    已经很长，而这几个键必须**一起**出现——只有 refs 没有 refs_total 时，
    前端会把"下发的条数"当成"语料的条数"，那是个假数。"""
    cited = (r.get("s3").cited_case_ids if r.get("s3") is not None
             and hasattr(r.get("s3"), "cited_case_ids") else ())
    sent, counts = _cap_refs(list(r.get("refs") or []), cited)
    return {"refs": sent, **counts}


def _serialize_residual(residual: dict | None) -> dict | None:
    if not residual:
        return None
    out = dict(residual)
    out["s2"] = residual["s2"].model_dump()
    return out


#: 一次响应里最多下发多少条参考医案。R40 实测：`full_context` 模式下
#: `refs` 是**整个语料**——一条问诊的响应体 2,848,127 字节，其中
#: `results[0].refs` 占 1,252,722 字节 / 1060 条（98.9%）。
#:
#: 为什么这是个真问题而不是"多传一点没关系"：
#:   · 前端要 JSON.parse 这 2.8 MB（主线程上一次长任务，R41 量到的 TBT 来源之一）
#:   · 1060 条参考医案没有任何界面能有意义地展示
#:   · 三甲内网的带宽不是本机回环
#:
#: 为什么**不是**在 `core/chain.py` 那一层砍：`refs` 在链内部还有别的用途
#: （幻觉检查要拿全集比 `cited_case_ids`）。砍在序列化边界上，链的语义一个字不动。
REFS_IN_RESPONSE = int(os.environ.get("REFS_IN_RESPONSE", "20"))


def _cap_refs(refs: list[dict], cited_ids, cap: int = REFS_IN_RESPONSE) -> tuple[list[dict], dict]:
    """下发的参考医案裁到 `cap` 条，**被引用的一条都不许丢**。

    顺序上的取舍：先放这次真的被引用的（`cited_case_ids`），再按分数补满。
    被引用的条目丢了的话前端的"点结论跳到依据"就会指向一条不存在的医案
    ——那比传得多严重得多（可追溯是这个项目的卖点）。

    `cap <= 0` 表示不裁（给需要全量的评测脚本留口）。

    返回的第二项是**对照数**（CLAUDE.md「任何数字都必须带对照」）：
    下发几条、这次引用了几条、语料里一共几条。前端显示"下发 20 / 共 1060"，
    不显示成"共 20"——后者是个假数。
    """
    total = len(refs)
    cited = set(cited_ids or ())
    if cap <= 0 or total <= cap:
        return refs, {"refs_total": total, "refs_sent": total,
                      "refs_cited": sum(1 for x in refs if x.get("case_id") in cited),
                      "refs_truncated": False}
    must = [x for x in refs if x.get("case_id") in cited]
    rest = [x for x in refs if x.get("case_id") not in cited]
    rest.sort(key=lambda x: -(x.get("score") or 0))
    sent = must + rest[:max(0, cap - len(must))]
    return sent, {"refs_total": total, "refs_sent": len(sent),
                  "refs_cited": len(must), "refs_truncated": True}


def _serialize_result(r: dict) -> dict:
    # 按 id 查元数据（姓名/配色）用全表：结果里只会有 enabled 的医家，
    # 但这个函数也被「参考医家」引用区的序列化复用。
    # R33：结构化模式那份结论的 id 是保留值 `synthesis`，**不在注册表里**
    # （见 core/physicians.py::SYNTHESIS_DISPLAY 的注释——它不是一位医家）。
    # 查不到就退到那份展示元数据，而不是退到灰色兜底：灰色在前端表示"未知医家"。
    info = physicians_all(PHYSICIANS).get(r["physician"]) or (
        synthesis_display() if r["physician"] == SYNTHESIS_PHYSICIAN_ID else {})
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
        # R40：下发的 refs 裁到 REFS_IN_RESPONSE 条，被引用的全留。
        # `hallucinated` 是**服务端**用全集算完的结论，不受这里裁剪影响——
        # 裁剪只改"下发多少"，不改"验了什么"。
        **_refs_block(r),
        "hallucinated": r["hallucinated"],
        # X2 输出侧安全校验结果，前端据此挂红/黄标签
        "safety_output": r.get("safety_output"),
        # G2 取证轨迹。不开 ReAct 时是 None；开了要如实带出来——ReAct 的卖点
        # 就是"能看见它查了什么"，只把结论传出去等于白跑。
        "react_trace": r["react_trace"].model_dump() if r.get("react_trace") else None,
        # R23：建议层三个键。`.get` 带默认值而不是 `r["advice"]`——这个函数也
        # 被「参考医家」引用区复用，那条路径的结果不是 run_physician 产出的，
        # 没有这三个键；而缺键会让前端读到 undefined 悄悄进渲染。
        # 患者角色的摘除在 _filter_response_by_role 里做，不在这儿：
        # 这一层只负责"怎么序列化"，角色判据只有那一处。
        "advice": r.get("advice", []),
        "advice_skipped": r.get("advice_skipped", []),
        "formula_score": r.get("formula_score"),
        # R33：结构化模式多出来的四项。**`.get` 带默认值**——legacy 那条路和
        # 「参考医家」引用区都没有这几个键，而缺键会让前端读到 undefined 悄悄进渲染。
        # `s3_structured` 是五步链原件（R37 的单链问诊界面读它）；`s3` 那一份
        # 仍然是下游既有契约的形状，两者并存不是重复——一个给新界面、一个给旧界面。
        "s3_structured": (r["s3_structured"].model_dump()
                          if r.get("s3_structured") is not None else None),
        "physician_influences": r.get("physician_influences", []),
        "physicians_cited": r.get("physicians_cited", []),
        # 带本体引用的药味占比。**0 有两种原因**（本体不在 / 本体在但模型没引），
        # 前端要显示这个数时必须同时看 manifest 的 knowledge_entries.available。
        "herbs_grounded_ratio": r.get("herbs_grounded_ratio"),
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
        # R15：病名要下发。§3.5 的患者形态第一行就是「可能属于 <病名>」，
        # 而这个名字是 get_disease() 核实过、在 M4 参考表里的那个——前端
        # 从 results[].s3.disease 自己取会拿到模型原样吐出来的字符串，
        # 那个字符串可能根本不在表里（_compute_triage 正是靠这一点决定
        # 返回 None 的）。一个没核实过的病名摆在患者看的第一行上，
        # 比不摆更危险。
        "disease": chosen.name,
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
        if not _role_gets_advice(role):
            # 判据跟 /api/prescription/validate 是同一个函数，见 ADVICE_FIELDS。
            for key in ADVICE_FIELDS:
                r.pop(key, None)
        new_results.append(r)
    response["results"] = new_results

    return response


# ---------- 图数据 ----------


#: R42：**单一诊断链的九层。** 一张表定死层号、层的机器名、层的中文名。
#:
#: 层号是布局（第几列），`node_type` 是"这是什么东西"——两件事分开
#: （R16 那条：混在一个字段上，样式表就只能有两份）。中文名由后端下发，
#: 前端不写死：加层/改名时前端跟着长，不需要改两处。
#:
#: 为什么是九层而不是原来的五层：原来「治法」挂在 证型→方剂 那条边的 label 上、
#: 「脏腑」和「病性」挤在一个「证素」层里、「病机」根本没有位置。那张图看得出
#: "从症状到方"，看不出**为什么是这个证、为什么是这个治法**——而那正是辨证
#: 这件事本身。九层把推理链的每一步摆成一层，图与 `S3Structured` 的九段一一对应。
CHAIN_LAYERS: tuple[tuple[int, str, str], ...] = (
    (0, "symptom", "症状"),
    (1, "organ", "脏腑"),
    (2, "nature", "病性"),
    (3, "syndrome", "证型"),
    (4, "pathogenesis", "病机"),
    (5, "principle", "治则"),
    (6, "method", "治法靶位"),
    (7, "formula", "方剂"),
    (8, "herb", "君臣佐使"),
)

#: 层号 → node_type / 中文名。从上面那张表派生，**不另写一份**。
LAYER_NODE_TYPE: dict[int, str] = {n: t for n, t, _ in CHAIN_LAYERS}
LAYER_LABEL: dict[int, str] = {n: z for n, _, z in CHAIN_LAYERS}

#: 节点 id 的前缀 → 层号。前缀是 `core/node_explain.py::parse_node_id` 的输入，
#: 两边必须说同一套词（那边有一张 `_PREFIX_KIND`，有测试比这两张表）。
LAYER_PREFIX: dict[int, str] = {
    0: "sym", 1: "organ", 2: "nature", 3: "syn",
    4: "mech", 5: "principle", 6: "method", 7: "formula", 8: "herb",
}


def to_graph(
    s1: S1Normalize, results: list[dict], s2=None, residual: dict | None = None,
    role: Role = "researcher",
) -> dict:
    """构造 Cytoscape 格式的图：{nodes: [{"data": {...}}], edges: [...], ...}。

    **R42：九层单链，图上没有医家分带。**

    症状(0) → 脏腑(1) → 病性(2) → 证型(3) → 病机(4) → 治则(5) → 治法靶位(6)
    → 方剂(7) → 君臣佐使(8)

    第 6 层叫「治法靶位」而不是「治法」：它的内容是 `MethodStep.targets`，
    schema 里写明那是"这个治法分别针对哪几条病机"——是**靶位**（肝、胃…），
    不是治法本身（治法在第 5 层的 `principle` 里）。叫「治法」会让图上出现
    一个写着「肝」的治法节点，那是错的。

    ## 为什么去掉医家分带

    改之前证型/方剂/药材的 node id 里带 physician（`syn::ye_tianshi`），于是
    三位医家在图上是三条并行的带子。那张图回答的是"三个人各自怎么想"，而
    产品要回答的是"**这一个**诊断是怎么推出来的"——分带把一条推理链切成三条，
    每条都缺上游（症状与证素是共享的），读图的人得自己在脑子里把它们并起来。

    改之后同名节点**合并成一个**，谁贡献的记在 `contributors` 里（节点属性，
    不是空间位置）。legacy 三列模式下三位医家给出同一个证型时图上就是一个
    证型节点、`contributors` 三个人；给出不同证型时是三个证型节点并列在
    同一层——**并列不等于分带**：它们在同一列上，上游连回同一批证素。

    ## 缺层如实报，不伪造

    `病机(4)` 与 `治法靶位(6)` 只有结构化 S3（`S3Structured`）才有
    （`organs[].pathogenesis` / `method.targets`）。legacy `S3Syndrome` 没有
    这两样，**这时那两层就是空的**，链条直接从证型接到治则、从治则接到方剂，
    并把层号记进 `missing_layers`。
    从 `reasoning` 里切一句话当病机是**伪造**——那段文字是模型的自由叙述，
    不是它标定的病机。

    ## role=patient 的边界没变

    仍然**压根不生成**方剂(7)/君臣佐使(8) 层，不是生成了再从响应里摘掉
    （图节点的 label/id 本身就是真实药名）。
    """
    nodes: list[dict] = []
    edges: list[dict] = []
    seen: set[str] = set()
    #: 已经出现过的层号——`missing_layers` 从它算，不另维护一份。
    layers_present: set[int] = set()

    def add_node(node_id: str, layer: int, **data) -> None:
        """同 id 只加一次。**重复时把 contributor 并进去**，不是丢掉——
        三位医家给出同一个证型时，那个节点要记得是三个人给的。"""
        layers_present.add(layer)
        if node_id in seen:
            if data.get("contributor"):
                for n in nodes:
                    if n["data"]["id"] == node_id:
                        who = n["data"].setdefault("contributors", [])
                        if data["contributor"] not in who:
                            who.append(data["contributor"])
                        break
            return
        seen.add(node_id)
        contributor = data.pop("contributor", None)
        data["layer"] = layer
        data["node_type"] = LAYER_NODE_TYPE[layer]
        data["layer_label"] = LAYER_LABEL[layer]
        if contributor:
            data["contributors"] = [contributor]
        nodes.append({"data": {"id": node_id, **data}})

    dropped: list[tuple[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()

    # 这一次几位医家的 (证型, 病名) 里哪些撞了。**先算好再进循环**：
    # 边画边判会让第一个撞上的那位医家不带编码（它那时还不知道后面有人重名）。
    ambiguous_syn = ambiguous_syndrome_pairs(
        (r["s3"].syndrome, r["s3"].disease or "") for r in results)

    def add_edge(source: str, target: str, **data) -> None:
        # 已知易错点：只有两端节点都已存在才建边，否则前端渲染会指向空节点。
        # 但静默丢弃会掩盖真实故障——S2 若把 supporting_symptoms 改写了
        # （"胃脘胀痛"->"脘腹胀痛"），边会整批消失，图上只是看起来"稀疏"，
        # 没人发现症状层和证素层已经断开。所以要计数并上报。
        if source not in seen or target not in seen:
            dropped.append((source, target))
            return
        # R42：去掉医家分带之后，同一条 (source, target) 会被几位医家各贡献一次。
        # **在这里去重**，不是让前端按 (source,target) 去重——前端去重只画第一条，
        # 而"第一条"取决于医家顺序，颜色/标签就成了随机的那一位（R16 踩过）。
        key = (source, target)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({"data": {"id": f"e::{source}>>{target}",
                               "source": source, "target": target, **data}})

    # ---- layer 0 症状 ----
    # 「已解释」用 core.chain.explained_symptoms 这一处实现——S2 全局共享，
    # 各医家的 r["s2"] 是同一份，这里不再各自汇总一遍
    if s2 is None and results:
        s2 = results[0]["s2"]
    explained: set[str] = explained_symptoms(s1, s2) if s2 is not None else set()
    residual_explained = set((residual or {}).get("newly_explained") or [])

    for sym in s1.symptoms:
        if sym in explained:
            state = "explained"
        elif sym in residual_explained:
            state = "residual"  # 初轮没解释，残差辨证补上了
        else:
            state = "unexplained"
        add_node(f"sym::{sym}", 0, label=sym, state=state)

    # ---- layer 1 脏腑 / layer 2 病性 ----
    # 原来这两样挤在一个「证素」层里。它们回答的不是同一个问题：脏腑是
    # **病位**（病在哪），病性是**病的性质**（寒热虚实）。摆成两层之后，
    # 「脾 + 气虚 → 脾胃气虚证」这条推理在图上是两条边汇进一个节点，
    # 而不是两个同色方块并排。
    def _element_layer(kind: str) -> int:
        return 1 if kind == "location" else 2

    def _element_id(hit) -> str:
        return f"{LAYER_PREFIX[_element_layer(hit.kind)]}::{hit.element}"

    if s2 is not None:
        for hit in s2.elements:
            layer = _element_layer(hit.kind)
            add_node(_element_id(hit), layer, label=hit.element, kind=hit.kind)
            for sym in hit.supporting_symptoms:
                add_edge(f"sym::{sym}", _element_id(hit))

    # 残差辨证新推出的证素，单独标出来（兼夹证的证素）。必须在主证素之后加：
    # add_node 先到先得，先加残差会把主路径里同名的证素整个标成 residual=True。
    if residual:
        for hit in residual["s2"].elements:
            layer = _element_layer(hit.kind)
            add_node(_element_id(hit), layer, label=hit.element, kind=hit.kind,
                     residual=True)
            for sym in hit.supporting_symptoms:
                add_edge(f"sym::{sym}", _element_id(hit), residual=True)

    for r in results:
        physician = r["physician"]
        pname = r["physician_name"]
        s3 = r["s3"]
        # **病机(4) 与治法(6) 的原件在 `s3_structured` 里，不在 `s3` 里。**
        #
        # `consult()` 给下游的 `s3` 是 `to_s3_syndrome()` **扁平化之后**的那一份
        # ——那次转换把 `organs[]`（病机）和 `method.targets`（治法）丢掉了，
        # 只留下 `treatment_principle` 这一个字符串。所以只读 `s3` 的话，
        # 这两层在生产里**永远**是空的，而 `missing_layers` 会如实把它们报成
        # "本次没有"——看起来像"这一轮的模型没产出病机"，实际上是读错了字段。
        # 这个 bug 只有把真 payload 喂进浏览器才看得见（Playwright 的
        # `single_chain_graph` 第一次跑就红了：第 4 层一个节点都没有），
        # 后端的 JSON 结构测试全绿——**又一次 CLAUDE.md 那条硬约定的例子**。
        st = r.get("s3_structured") or s3

        # ---- layer 3 证型（含病名） ----
        # **id 按证型名，不按医家**（R42 去分带）。撞名补证候编码那一条不变。
        label = f"{s3.disease} · {s3.syndrome}" if s3.disease else s3.syndrome
        suffix = syndrome_code_suffix(
            ambiguous=(s3.syndrome, (s3.disease or "").strip()) in ambiguous_syn,
            code=(syndrome_row(s3.syndrome) or {}).get("code"))
        syn_id = f"syn::{s3.syndrome}"
        add_node(syn_id, 3, label=label + suffix, syndrome=s3.syndrome,
                 disease=s3.disease, contributor=physician, pname=pname)

        # 证素 → 证型。**只连这一位医家真的用到的证素**（r["s2"] 是全局共享的
        # 那一份，所以实际上是全部证素——这跟改动前一致，不在这一轮改语义）。
        for hit in r["s2"].elements:
            add_edge(_element_id(hit), syn_id)

        # ---- layer 4 病机（只有结构化 S3 有） ----
        upstream_of_principle = [syn_id]
        organs = list(getattr(st, "organs", ()) or ())
        if organs:
            upstream_of_principle = []
            for o in organs:
                organ_name = getattr(o, "organ", None)
                mech = (getattr(o, "pathogenesis", "") or "").strip()
                # 脏腑节点：结构化 S3 自己标了病位，它可能不在 S2 的证素表里
                # （模型从症状直接判的）。**补进来而不是丢掉**：丢掉的话
                # 病机会悬空，而"悬空"在图上看起来只是"这一段没画出来"。
                if organ_name:
                    add_node(f"organ::{organ_name}", 1, label=organ_name,
                             kind="location")
                    for sym in (getattr(o, "supporting_symptoms", ()) or ()):
                        add_edge(f"sym::{sym}", f"organ::{organ_name}")
                    add_edge(f"organ::{organ_name}", syn_id)
                if not mech:
                    continue
                mech_id = f"mech::{mech}"
                add_node(mech_id, 4, label=mech, organ=organ_name,
                         contributor=physician)
                add_edge(syn_id, mech_id)
                upstream_of_principle.append(mech_id)
            if not upstream_of_principle:
                upstream_of_principle = [syn_id]

        # ---- layer 5 治则 ----
        # 结构化：`method.principle`；legacy：`treatment_principle`。
        # 两处取值一个函数，不在这里 if/else 两遍（那是两处实现）。
        principle = _s3_principle(st) or _s3_principle(s3)
        principle_targets = list(getattr(getattr(st, "method", None), "targets", ()) or ())
        upstream_of_formula: list[str] = []
        if principle:
            principle_id = f"principle::{principle}"
            add_node(principle_id, 5, label=principle, contributor=physician)
            for up in upstream_of_principle:
                add_edge(up, principle_id)
            upstream_of_formula = [principle_id]
            # ---- layer 6 治法靶位（只有结构化 S3 有 targets） ----
            method_ids = []
            for t in principle_targets:
                t = (t or "").strip()
                if not t:
                    continue
                method_id = f"method::{t}"
                add_node(method_id, 6, label=t, contributor=physician)
                add_edge(principle_id, method_id)
                method_ids.append(method_id)
            if method_ids:
                upstream_of_formula = method_ids
        else:
            upstream_of_formula = upstream_of_principle

        # M6：role="patient" 时整段跳过——不生成方剂/药材层，不是生成了再
        # 从响应里摘掉（见函数文档字符串）。
        if role == "patient":
            continue

        # ---- layer 7 方剂 + layer 8 君臣佐使 ----
        for i, cand in enumerate(s3.formula_candidates):
            formula_id = f"formula::{cand.name}"
            add_node(
                formula_id, 7, label=cand.name,
                # 前端按 source 区分边框（classic 实线/modified 虚线/composed
                # 点线）、selected 高亮选中的那个、safety_blocking 为真时标红。
                source=cand.source, confidence=cand.confidence,
                selected=(i == s3.selected),
                safety_blocking=cand.safety.blocking if cand.safety else False,
                contributor=physician,
            )
            for up in upstream_of_formula:
                add_edge(up, formula_id)

            for item in cand.herb_items:
                # herb_id 带方名：同一味药会出现在多个候选方里（"甘草"作为使药
                # 几乎每个方都有），不带方名会被去重合并成一个节点、同时挂在
                # 两个 parent 上，cytoscape 会报错。
                # id 用 item.name 原始写法（旧式合成路径下可能仍带剂量文本），
                # label 单独剥剂量——「label 剥、id 保原样」，前端
                # buildEvidenceIndex() 用同一个拼法反查证据，id 一变就断链。
                herb_id = f"herb::{cand.name}::{item.name}"
                add_node(
                    herb_id, 8, label=strip_dose_and_parens(item.name) or item.name,
                    parent=formula_id,
                    dose=item.dose, unit=item.dose_unit,
                    processing=item.processing, decoction=item.decoction,
                    # 这里的 role（君/臣/佐/使）是 HerbItem 自己的字段，跟本函数
                    # 参数 role（patient/doctor/...角色）只是同名，语义完全不同。
                    role=item.role, function_in_formula=item.function_in_formula,
                    is_western=is_western_drug(item.name),
                    contributor=physician,
                )
                # 方剂 → 药材的关系由 parent 字段（compound node）表达，
                # 这里不额外画边——画了会在图上出现重复的连线。

    # R42 收尾：把"谁贡献的"从列表压成两个可选择的标记。
    #
    # **为什么在这里算而不在前端按 contributors.length 现判**：这是同一个判断
    # （"这个结论是一个人给的还是几个人给的"），放在两处就会在改一边时漏掉
    # 另一边（CLAUDE.md 第 31 条）。cytoscape 的选择器也做不到按数组长度选，
    # 前端真要判就得在 JS 里再遍历一遍节点——那正是第二处实现。
    for n in nodes:
        who = n["data"].get("contributors") or []
        if len(who) == 1:
            n["data"]["contributor_solo"] = who[0]
        elif len(who) > 1:
            n["data"]["multi_contributor"] = True

    expected = {n for n, _, _ in CHAIN_LAYERS}
    if role == "patient":
        expected -= {7, 8}     # 这两层是刻意不生成的，不算"缺"
    return {
        "nodes": nodes,
        "edges": edges,
        "dropped_edges": len(dropped),
        # R42：层的元信息**由后端下发**，前端不写死（加层时前端跟着长）。
        "layers": [{"layer": n, "node_type": t, "label": z} for n, t, z in CHAIN_LAYERS],
        # **缺哪一层要说出来。** 空层有两种来路：legacy S3 不产出病机/治法
        # （合法），和上游数据出了问题（要看一眼）。前端照实显示"本次没有 X 层"，
        # 不是悄悄把链条接过去。
        "missing_layers": sorted(expected - layers_present),
    }


def _s3_principle(s3) -> str:
    """治则。**结构化与 legacy 两种 S3 取同一个概念的唯一入口。**

    结构化是 `method.principle`，legacy 是 `treatment_principle`。散在两处 if
    的话，将来加第三种 S3 形状就会漏掉其中一处（而漏掉的表现是图上少一层，
    不报错）。
    """
    method = getattr(s3, "method", None)
    if method is not None:
        p = (getattr(method, "principle", "") or "").strip()
        if p:
            return p
    return (getattr(s3, "treatment_principle", "") or "").strip()


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
    # R23：这条接口是医生端的可编辑处方表在用的，所以默认 doctor。
    # 带 role 而不是"这条接口本来就只有医生在调、不用判"——**建议里带着具体
    # 药名**，patient 拿到 advice 等于绕过 _filter_s3_for_role 摘掉 formula/herbs
    # 的那道边界。裁剪在服务端做（跟 /api/consult 同一条原则：不是发了再让前端
    # 藏起来，那样打开 devtools 照样能看到）。
    role: Role = "doctor"


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


# R23：哪些角色能看到建议层。**全项目唯一的判据**——/api/prescription/validate
# 和 results[i] 两处都问这一个函数，不是各写一遍 `if role == "patient"`。
# patient 拿不到的理由：每条建议的 reason 里都带着具体药名（「甘草 与 甘遂 属
# 配伍禁忌」），下发 advice 等于把处方内容从另一个字段漏出去，
# _filter_s3_for_role 摘掉 formula/herbs 就白做了。formula_score 虽然只是个数，
# 但它是"这张处方拟得好不好"的分，对患者没有意义，一起摘。
ADVICE_FIELDS = ("advice", "advice_skipped", "formula_score")


def _role_gets_advice(role: Role) -> bool:
    return role != "patient"


def _advice_fields_for_role(check, role: Role) -> dict:
    """建议层那三个键。角色不该看到时返回 `{}`（不是三个空值）——
    空列表读起来是"查过了，没有建议"，而真相是"这个角色不给这一层"。"""
    if not _role_gets_advice(role):
        return {}
    return {
        "advice": advice_dicts(check),
        "advice_skipped": list(check.skipped),
        "formula_score": check.score,
    }


@app.post("/api/prescription/validate")
def api_prescription_validate(req: PrescriptionValidateRequest) -> dict:
    """纯规则校验，不调 LLM，毫秒级返回。独立于 /api/consult——医生可能在
    完全不同的场景下想校验一张手写/临时改动的方（不是从某次问诊来的），
    这条接口不依赖任何问诊上下文。

    R23 起除了 FormulaSafety 那几个键，还带 advice（建议）/ advice_skipped
    （因为缺数据没跑的规则）/ formula_score（粗排序分）。安全层那几个键**一个
    没动**：调用方已经在消费它们，建议层是新增的三个键，不是改写原有的。
    """
    safety = assess_formula_safety(req.syndrome, req.herb_items)
    check = check_formula(req.syndrome, req.herb_items)
    return {**_safety_dict(safety), **_advice_fields_for_role(check, req.role)}


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


#: R41：静态资源的缓存策略。**两类资源两种，不能给同一个值。**
#:
#: | 类 | 谁 | 策略 | 为什么 |
#: |---|---|---|---|
#: | 不变的第三方产物 | `vendor/`（字体 1.13 MB + cytoscape 373 KB） | `max-age=1 年, immutable` | 内容跟文件名绑定（字体子集是 `scripts/subset_fonts.py` 的产物、cytoscape 带版本），换内容必然换文件名。二次访问一个字节都不用取——这 1.5 MB 是首屏字节数的 84% |
#: | 会改的自家代码 | `index.html` / `app.js` / `app.css` / `graph.js` | `no-cache` | **必须每次问服务器**。`max-age` 一给，升级之后医生刷新页面还是旧的 JS 配新的后端，而那是一类最难查的故障（R45 的升级回滚要靠这条）。`no-cache` 不是"不缓存"，是"缓存但每次带 ETag 问一句"——304 只有几十字节 |
#:
#: 为什么不靠 StaticFiles 的默认值：它只给 ETag / Last-Modified，没有
#: `Cache-Control`。浏览器于是按启发式自己猜一个新鲜期——猜多久取决于浏览器版本，
#: 而"取决于浏览器版本"意味着现场表现不可复现。
CACHE_IMMUTABLE_SECONDS = 31536000       # 1 年
CACHE_IMMUTABLE_PREFIXES = ("vendor/",)


class _CachingStatic(StaticFiles):
    """给静态响应补 `Cache-Control`。**只补，不改别的**——ETag 与
    Last-Modified 仍由 StaticFiles 处理，304 的逻辑一行没动。"""

    async def get_response(self, path: str, scope):  # noqa: ANN001 - 跟基类签名一致
        resp = await super().get_response(path, scope)
        if resp.status_code in (200, 304):
            if path.startswith(CACHE_IMMUTABLE_PREFIXES):
                resp.headers["Cache-Control"] = (
                    f"public, max-age={CACHE_IMMUTABLE_SECONDS}, immutable")
            else:
                resp.headers["Cache-Control"] = "no-cache"
        return resp


# 静态文件挂在 /app，不要挂在根路径——否则会遮蔽上面的 API 路由。
app.mount("/app", _CachingStatic(directory=str(WEB_ROOT), html=True), name="web")
