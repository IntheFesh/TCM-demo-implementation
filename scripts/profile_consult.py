"""R40：一次完整问诊的分段计时（函数级）。**先测量，后优化。**

    python -m scripts.profile_consult --backend fake --repeat 3
    python -m scripts.profile_consult --backend real --complaint "…"
    python -m scripts.profile_consult --compare eval/profile/consult_旧.json

## 插桩方式：外挂包装，不改热路径

21 个阶段里 17 个是既有函数，**用 monkeypatch 在跑之前把它们包一层**，跑完还原；
另外 4 个（`import` / `serialize` / `sse_flush` / `audit_write` 的调用点）由这个
脚本自己掐表。

为什么不在 `core/chain.py` 里写 `with stage(...)`：那条链上有三千多条测试，
为了量一次耗时去改它等于拿正确性换测量。**外挂包装量的是同一个函数的同一次调用**，
没有精度损失，代价只是这张 `TARGETS` 表要跟着重构走——所以它有一条测试
钉住"表里每个目标都还存在"（改名了就红，而不是默默少一段）。

## 输出

  · `eval/profile/consult_<ts>.json`：逐段 wall/cpu/blocked/alloc + 汇总
  · 终端缩进火焰图（不依赖外部工具）
  · `--compare 旧.json`：逐段改前改后 diff 表
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from core.profiling import STAGES, Profiler, compare

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "eval" / "profile"

#: (模块, 属性, 阶段名)。**顺序无关**，但每一项都必须存在——有一条测试查这个。
#: 改名/搬家时这张表要跟着改，红一次好过少一段。
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("core.llm", "get_backend", "config_load"),
    ("core.ontology", "get_ontology", "ontology_load"),
    # 同一个"包在被用的地方"的坑，第二次踩到：`core/formula_verifier.py` 顶上写的是
    # `from core.ontology import Ontology, get_ontology`，只包 `core.ontology` 那一份
    # 的话，验证器里那次调用走不到包装——于是 2.8 秒的本体层首次加载被记到了
    # `verify` 头上，读起来像"符号验证器慢"。其余调用方（chain / node_explain /
    # context_prefix）都是**函数内 import**，调用时才从 core.ontology 取，包定义处就够。
    ("core.formula_verifier", "get_ontology", "ontology_load"),
    ("core.retrieval", "get_retriever", "retriever_load"),
    ("core.chain", "get_retriever", "retriever_load"),
    ("core.tools", "get_graph_store", "graph_load"),
    # **包在"被用的地方"，不是"被定义的地方"**：`core/chain.py` 用的是
    # `from core.formula_verifier import verify_formula` 这种直接导入，
    # 它在 import 时就把函数对象绑进了 chain 的命名空间——只包定义处那一份，
    # chain 里那次调用根本走不到包装。实测就是这么发现的：第一版 `verify`
    # 那一段恒为空，而它明明跑了。
    ("core.chain", "normalize", "s1"),
    ("core.chain", "infer_elements", "s2"),
    ("core.chain", "normalize_and_infer_merged", "s2"),
    ("core.chain", "run_followup", "followup"),
    ("core.chain", "_search_cases", "retrieve"),
    # knowledge_build = **提示词上下文组装**这一段，三种模式下是三个函数：
    # focused → build_focused_knowledge；legacy full_context → assemble（单医家
    # 稳定前缀）；structured full_context → render（五家综合没有"某一位医家的
    # 全量医案"这个概念，chain.py 那处注释说明了为什么不走 assemble）。
    # 三个都包上，缺哪个都会让这一段在对应模式下变成"没量到"。
    ("core.chain", "build_focused_knowledge", "knowledge_build"),
    ("core.chain", "assemble", "knowledge_build"),
    ("core.chain", "render", "knowledge_build"),
    ("core.chain", "run_synthesis", "s3_generate"),
    ("core.chain", "run_physician", "s3_generate"),
    ("core.chain", "verify_formula", "verify"),
    ("core.chain", "_verify_and_revise", "revise"),
    ("core.chain", "check_safety", "safety"),
    ("core.chain", "check_formula", "advice"),
    # **包 `_filter_response_by_role` 而不是 `_filter_s3_for_role`**：后者只在
    # patient/student 角色下被调到，doctor 角色跑一遍这一段会是空的——
    # 而"空"会被读成"角色裁剪不花时间"。前者是四个返回分支都过的那一道。
    ("api.main", "_filter_response_by_role", "role_slice"),
    ("api.main", "to_graph", "graph_build"),
    ("api.main", "append_audit", "audit_write"),
    ("api.main", "_sse", "sse_flush"),
)

#: 挂在类方法上的那几段（不是模块级函数）。
#: `_ensure_encoded` 在没装 sentence_transformers 的机器上自然缺席，
#: `missing_stages` 报得出来。
#: `JSONResponse.render` 留着是为了**错误响应**那条路（HTTPException 与 404
#: 走的是它）；正常 200 响应不经过它，见下面 `serialize` 那条注释。
METHOD_TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("core.retrieval", "DenseRetriever", "_ensure_encoded", "embedding_encode"),
    ("fastapi.responses", "JSONResponse", "render", "serialize"),
)

#: 这条路上**没跑到**的段，每一段写明属于哪一类。三类不许合并：
#:
#:  ① `not_on_this_path`：这次这条路本来就不经过它（合法，换条路就量到了）
#:  ② `bypassed_by_profiler`：量具自己旁路掉了（合法但要说明，否则读成"不花时间"）
#:  ③ 表里没有的段 → **插桩掉了**（bug，报告里必须红着写）
#:
#: 为什么要这张表：`missing_stages` 只给一串名字，而"没量到"有上面三种完全
#: 不同的含义。R38 消融那一轮已经在同一件事上立过规矩（三种空格子不许合并成
#: 一个"—"），这里是同一条纪律在性能表上的落点。
MISSING_REASONS: dict[str, tuple[str, str]] = {
    "config_load": ("bypassed_by_profiler",
                    "profiler 用 _ForceBackend 把后端钉死了，请求路径上 "
                    "get_backend() 不会再跑；这一段单独掐表（见 config_load_ms）"),
    "embedding_encode": ("not_on_this_path",
                         "默认 full_context 模式用全量医案，不做向量编码；"
                         "跑 --retriever-mode hybrid 才量得到"),
    "graph_load": ("not_on_this_path",
                   "全局图谱只有 ReAct 工具层会读（query_graph/check_residual），"
                   "结构化单链这条路不读它"),
    "knowledge_build": ("not_on_this_path",
                        "focused 档才调 build_focused_knowledge；full_context 档"
                        "由 assemble/render 承担，已分别包上"),
    "audit_write": ("not_on_this_path",
                    "审计落盘在 /api/pharmacy/export 那个端点，不在问诊路径上；"
                    "它的耗时用 --probe-audit 单独量"),
    "sse_flush": ("not_on_this_path", "只有 --stream 那条路上才有"),
    "serialize": ("not_on_this_path",
                  "FastAPI 0.141 起有一条快路径：response_class 是默认占位且存在"
                  "response_field 时，serialize_response(dump_json=True) 直接产 "
                  "bytes 再包一个裸 Response，**不经过 JSONResponse.render**。"
                  "所以真正的序列化点是 fastapi.routing.serialize_response，"
                  "已在 ASYNC_TARGETS 里包上；这里只会在错误响应那条路上被记到"),
    "followup": ("not_on_this_path", "没开追问（默认不开）"),
    "revise": ("not_on_this_path", "验证器一轮就过，没触发修订"),
}

#: `async def` 的插桩点。**必须单独一张表**：用同步 wrapper 包协程函数，
#: 量到的只是"创建协程对象"那几微秒，真正的 await 在包装之外——读起来像
#: "序列化不花时间"，而它恰恰是这条路上唯一的序列化点。
ASYNC_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("fastapi.routing", "serialize_response", "serialize"),
)


class _Patcher:
    """把 TARGETS 逐个包一层，`restore()` 还原。**还原是必须的**：
    这个脚本可能被测试 import，包装残留会让后面的用例量到别人的耗时。"""

    def __init__(self, prof: Profiler) -> None:
        self.prof = prof
        self._undo: list = []
        self.wrapped: list[str] = []
        self.missing: list[str] = []

    def _wrap(self, fn, stage: str):
        prof = self.prof

        if inspect.iscoroutinefunction(fn):
            async def wrapper(*args, **kwargs):      # noqa: RUF029 - 就是要 await
                with prof.stage(stage):
                    return await fn(*args, **kwargs)
        else:
            def wrapper(*args, **kwargs):
                with prof.stage(stage):
                    return fn(*args, **kwargs)

        wrapper.__name__ = getattr(fn, "__name__", stage)
        wrapper.__doc__ = getattr(fn, "__doc__", None)
        wrapper.__wrapped_stage__ = stage
        return wrapper

    def apply(self) -> None:
        for mod_name, attr, stage in TARGETS + ASYNC_TARGETS:
            try:
                mod = importlib.import_module(mod_name)
                fn = getattr(mod, attr)
            except (ImportError, AttributeError):
                self.missing.append(f"{mod_name}.{attr}")
                continue
            setattr(mod, attr, self._wrap(fn, stage))
            self._undo.append((mod, attr, fn))
            self.wrapped.append(f"{mod_name}.{attr}→{stage}")
        for mod_name, cls_name, attr, stage in METHOD_TARGETS:
            try:
                mod = importlib.import_module(mod_name)
                cls = getattr(mod, cls_name)
                fn = getattr(cls, attr)
            except (ImportError, AttributeError):
                self.missing.append(f"{mod_name}.{cls_name}.{attr}")
                continue
            setattr(cls, attr, self._wrap(fn, stage))
            self._undo.append((cls, attr, fn))
            self.wrapped.append(f"{mod_name}.{cls_name}.{attr}→{stage}")

    def restore(self) -> None:
        for holder, attr, original in reversed(self._undo):
            setattr(holder, attr, original)
        self._undo.clear()


class _ForceBackend:
    """把后端钉成给定的那个，**进程级**而不是 ContextVar 级。

    为什么不用 `use_llm`：它是 ContextVar，而 FastAPI 的同步端点跑在
    threadpool 的另一个线程里，ContextVar 传不过去——于是走 TestClient 那条
    路径时 `use_llm` 形同虚设，量到的是"没有 key，第一次调用就抛"。
    这里直接换掉三处 `get_llm` 的绑定（定义处 + 两个直接 import 它的模块），
    跑完还原。
    """

    HOLDERS = (("core.llm", "get_llm"), ("core.chain", "get_llm"), ("api.main", "get_llm"))

    def __init__(self, backend) -> None:
        self.backend = backend
        self._undo: list = []

    def __enter__(self):
        for mod_name, attr in self.HOLDERS:
            try:
                mod = importlib.import_module(mod_name)
                original = getattr(mod, attr)
            except (ImportError, AttributeError):
                continue
            setattr(mod, attr, lambda *_a, **_k: self.backend)
            self._undo.append((mod, attr, original))
        return self

    def __exit__(self, *exc):
        for mod, attr, original in reversed(self._undo):
            setattr(mod, attr, original)
        self._undo.clear()
        return False


def _probe_config_load(prof: Profiler) -> str | None:
    """单独量一次真实的配置解析（`get_backend()` 读环境变量、挑后端、建 client）。

    请求路径上量不到它：profiler 用 `_ForceBackend` 把后端钉死了。留着"没量到"
    比给个数更糟——读报告的人会当成"配置解析不花时间"。
    """
    import core.llm as core_llm

    t0 = time.perf_counter()
    c0 = time.thread_time()
    try:
        core_llm.get_backend()
    except Exception as e:  # noqa: BLE001 - 没 key 的机器上它本来就会抛
        return f"{type(e).__name__}: {e}"
    prof.record("config_load", wall_ms=(time.perf_counter() - t0) * 1000,
                cpu_ms=(time.thread_time() - c0) * 1000)
    return None


def _probe_audit_write(prof: Profiler) -> str | None:
    """单独量一次审计落盘（哈希链读尾行 + flock + 追加写）。

    它在 `/api/pharmacy/export` 那个端点上，不在问诊路径上。**写到临时文件**
    而不是真的 data/audit.jsonl——量耗时不该往审计链里塞假记录。
    """
    import tempfile

    import core.audit as audit

    original = audit.AUDIT_PATH
    with tempfile.TemporaryDirectory() as d:
        audit.AUDIT_PATH = Path(d) / "audit.jsonl"
        try:
            payload = {"doctor_id": "profiler", "patient_ref": None,
                       "model_suggestion": {}, "final": {}, "diffs": [],
                       "safety_at_export": {}, "override_reason": None}
            t0 = time.perf_counter()
            c0 = time.thread_time()
            audit.append_audit(payload)
            prof.record("audit_write", wall_ms=(time.perf_counter() - t0) * 1000,
                        cpu_ms=(time.thread_time() - c0) * 1000)
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"
        finally:
            audit.AUDIT_PATH = original
    return None


def _finish_run(run: dict, import_ms: float, runs: list[dict]) -> None:
    """把 `import` 那一段补进去并重算缺失表，然后收进 runs。

    重算是必须的：**不重算 `import` 会永远挂在"没量到"里**，而那张表正是
    给人看"哪一段插桩掉了"的，一个假阳性就会让人不再信它。
    """
    run["import_note"] = ("import 这一段量的是 api.main 的 import，"
                          "**不是冷进程启动**（那个走 bench_startup）")
    run["stages"].insert(0, {"name": "import", "wall_ms": round(import_ms, 3),
                             "cpu_ms": round(import_ms, 3), "blocked_ms": 0.0,
                             "alloc_mb": 0.0, "n_calls": 1,
                             "parent": None, "n_parents": 0})
    have = {s["name"] for s in run["stages"]}
    run["missing_stages"] = [n for n in STAGES if n not in have]
    run["missing_reasons"] = missing_reason_rows(run["missing_stages"])
    runs.append(run)


def missing_reason_rows(missing: list[str]) -> list[dict]:
    """把 `missing_stages` 变成带分类的表。表外的段归第三类——**插桩掉了**。"""
    rows = []
    for name in missing:
        kind, why = MISSING_REASONS.get(
            name, ("instrumentation_gap",
                   "MISSING_REASONS 里没有这一段的说明 → 多半是插桩掉了，"
                   "不是它没跑"))
        rows.append({"stage": name, "kind": kind, "why": why})
    return rows


def profile_once(complaint: str, backend, *, trace_alloc: bool = True,
                 stream: bool = False, role: str = "doctor",
                 retriever_mode: str | None = None,
                 probes: bool = True, wait_ready: bool = True) -> dict:
    """跑一次**完整的 HTTP 问诊**并返回 profile。

    走 `TestClient` 而不是直接调 `consult()`：`role_slice` / `graph_build` /
    `serialize` / `audit_write` / `sse_flush` 这五段都在 API 层，
    直接调链路的话它们永远量不到——而"量不到"会被读成"这几段不花时间"。
    """
    from fastapi.testclient import TestClient

    import api.main as api_main

    prof = Profiler(trace_alloc=trace_alloc)
    prof.start()
    patcher = _Patcher(prof)
    patcher.apply()
    error = None
    n_bytes = 0
    manifest: dict = {}
    probe_errors: dict[str, str] = {}
    if probes:
        # 两个旁路段**先量**：它们要跑在 _ForceBackend 之外（config_load 量的
        # 就是"没被钉死时的配置解析"），所以不能挪到 with 里面。
        for name, fn in (("config_load", _probe_config_load),
                         ("audit_write", _probe_audit_write)):
            err = fn(prof)
            if err:
                probe_errors[name] = err
    body: dict = {"complaint": complaint, "role": role}
    if retriever_mode:
        body["retriever_mode"] = retriever_mode
    try:
        with _ForceBackend(backend):
            if stream:
                from scripts.live_server import live_server

                with live_server() as base_url:
                    n_bytes, manifest, error = _drive_stream(base_url, body)
            else:
                # **必须用 with**：TestClient 只有当上下文管理器用时才跑 lifespan，
                # 而 `_warmup()`（本体层 + 检索器预热）就挂在 lifespan 上。
                # 不进 with 的话预热根本没发生，那 2.8 秒的本体层加载会落在
                # 第一个请求里、被记到 `verify` 头上——**profiler 第一版就是
                # 这样把"没预热"读成了"验证器慢"**，见 R40 报告第一节那张表。
                with TestClient(api_main.app) as client:
                    if wait_ready:
                        _wait_ready(client)
                    resp = client.post("/api/consult", json=body)
                    n_bytes = len(resp.content)
                    if resp.status_code != 200:
                        error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    else:
                        manifest = (resp.json() or {}).get("manifest") or {}
    except Exception as e:  # noqa: BLE001 - 一次跑挂不该把 profile 丢掉
        error = f"{type(e).__name__}: {e}"
    finally:
        patcher.restore()

    prof.stop()
    out = prof.to_dict()
    out.update({
        "complaint": complaint,
        "stream": stream,
        "ok": error is None,
        "error": error,
        "response_bytes": n_bytes,
        "wrapped": patcher.wrapped,
        "unwrappable": patcher.missing,
        "llm_calls": manifest.get("llm_calls"),
        "retriever_mode": retriever_mode,
        "wait_ready": wait_ready,
        "probe_errors": probe_errors,
    })
    out["missing_reasons"] = missing_reason_rows(out["missing_stages"])
    return out


def _drive_stream(base_url: str, body: dict, *, answer: str = "无"
                  ) -> tuple[int, dict, str | None]:
    """按真实客户端的样子把 SSE 跑完：**追问要回答**。

    不回答就会挂死：`stream.ask` 把 need_input 推给客户端后阻塞在 `answer_q.get()`
    上，没人送答案，worker 线程就停在那儿——`--stream` 第一版就是这样卡住的，
    看上去像"SSE 端点慢"，实际是量具自己没扮好客户端这个角色。

    答案固定一句话：这里量的是 SSE 推送与序列化的耗时，不是追问质量；
    回答内容一变，后面的证素推断就变，逐段耗时也就不可比了。

    走真服务器（`scripts.live_server`）而不是 TestClient——理由见那个模块的
    文档字符串（全缓冲传输层下这条路必死锁）。
    """
    import json as _json

    import httpx

    n_bytes = 0
    manifest: dict = {}
    error: str | None = None
    stream_id: str | None = None
    event = None
    with httpx.stream("POST", f"{base_url}/api/consult/stream", json=body,
                      timeout=600.0) as resp:
        if resp.status_code != 200:
            resp.read()
            return 0, {}, f"HTTP {resp.status_code}"
        for line in resp.iter_lines():
            n_bytes += len(line) + 1
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                try:
                    payload = _json.loads(line[5:].strip())
                except ValueError:
                    continue
                if event == "stream_id":
                    stream_id = payload.get("stream_id")
                elif event == "need_input" and stream_id:
                    httpx.post(f"{base_url}/api/consult/stream/{stream_id}/answer",
                               json={"answer": answer}, timeout=30.0)
                elif event == "done":
                    manifest = payload.get("manifest") or {}
                elif event == "error":
                    error = str(payload)[:200]
    return n_bytes, manifest, error


def _wait_ready(client, *, budget: float | None = None) -> dict:
    """等 `/health` 回 200 再发问诊——**真实客户端就是这么做的**。

    R40 把启动改成"先监听再预热"之后，服务在预热完成前就开始应答，`/health`
    回 503 + 进度。编排器据此不放流量进来。量具如果无视这个闸门，量到的是
    "在知识库还没加载完时硬发一条问诊"——那条请求会阻塞在本体层的加载锁上，
    实测 `verify` 一段就是 2220 ms（本体层 4790 ms 的一部分），
    而**没有任何真实部署会处在这个状态**。

    `--no-wait-ready` 保留那条路：它量的是"无视就绪闸门的最坏情况"，
    是个有意义的数，但必须跟稳态分开写（两种口径不许并进一张表）。
    """
    import api.main as api_main

    budget = budget if budget is not None else api_main.WARMUP_TIMEOUT_SECONDS
    deadline = time.monotonic() + budget
    last: dict = {}
    while time.monotonic() < deadline:
        resp = client.get("/health")
        last = resp.json().get("warmup") or {}
        if resp.status_code == 200:
            return last
        time.sleep(0.05)
    return last


def _jsonable(obj):
    """pydantic 模型 → dict。序列化这一段要量的就是真实响应的体积与耗时，
    所以不能只 dump 一个壳。"""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    return obj


def median_profile(runs: list[dict]) -> dict:
    """多次跑取**逐段中位数**。不取均值：一次 GC 或一次网络抖动会把均值拽走，
    而我们要的是"典型的一次"。"""
    ok = [r for r in runs if r.get("ok")]
    if not ok:
        return {"kind": "profile_median", "n_runs": len(runs), "n_ok": 0, "stages": []}
    names: list[str] = []
    for r in ok:
        for s in r["stages"]:
            if s["name"] not in names:
                names.append(s["name"])
    names.sort(key=lambda n: STAGES.index(n) if n in STAGES else 999)
    stages = []
    for name in names:
        vals = {k: [s[k] for r in ok for s in r["stages"] if s["name"] == name]
                for k in ("wall_ms", "cpu_ms", "blocked_ms", "alloc_mb")}
        stages.append({"name": name, "n_runs_with_stage": len(vals["wall_ms"]),
                       **{k: round(statistics.median(v), 3) if v else None
                          for k, v in vals.items()}})
    total = sum(s["wall_ms"] or 0 for s in stages)
    blocked = sum(s["blocked_ms"] or 0 for s in stages)
    return {
        "kind": "profile_median",
        "n_runs": len(runs), "n_ok": len(ok),
        "total_stage_wall_ms": round(total, 3),
        "total_blocked_ms": round(blocked, 3),
        "blocked_ratio": round(blocked / total, 4) if total else None,
        "stages": stages,
        "missing_stages": ok[-1].get("missing_stages", []),
    }


def compare_text(rows: list[dict]) -> str:
    head = f"{'阶段':<18s}{'旧 wall':>10s}{'新 wall':>10s}{'差':>10s}{'差%':>8s}  说明"
    lines = [head, "-" * len(head)]
    for r in rows:
        old = "—" if r["old_wall_ms"] is None else f"{r['old_wall_ms']:.1f}"
        new = "—" if r["new_wall_ms"] is None else f"{r['new_wall_ms']:.1f}"
        d = "—" if r["delta_ms"] is None else f"{r['delta_ms']:+.1f}"
        p = "—" if r["delta_pct"] is None else f"{r['delta_pct']:+.1f}%"
        lines.append(f"{r['name']:<18s}{old:>10s}{new:>10s}{d:>10s}{p:>8s}  {r['note'] or ''}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--complaint", default=None, help="默认用 tests/queries.txt 第一条")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--backend", default="fake", choices=["fake", "real"])
    ap.add_argument("--no-alloc", dest="alloc", action="store_false",
                    help="不跑 tracemalloc（它自己有 2~3 倍内存开销）")
    ap.set_defaults(alloc=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", default=None, metavar="旧.json",
                    help="跟一份旧 profile 逐段比（零调用，只读两个文件）")
    ap.add_argument("--stream", action="store_true",
                    help="走 SSE 端点（sse_flush 这一段只有这条路上量得到）")
    ap.add_argument("--retriever-mode", default=None,
                    help="检索模式（embedding_encode 只有 hybrid/dense 这条路上量得到）")
    ap.add_argument("--role", default="doctor", choices=["doctor", "patient",
                                                        "student", "researcher"])
    ap.add_argument("--no-probes", dest="probes", action="store_false",
                    help="不单独量 config_load / audit_write 这两个旁路段")
    ap.set_defaults(probes=True)
    ap.add_argument("--no-wait-ready", dest="wait_ready", action="store_false",
                    help="不等 /health 回 200 就发问诊。量的是「无视就绪闸门的"
                         "最坏情况」——首个请求会阻塞在本体层加载锁上，"
                         "实测比稳态慢两个数量级。跟稳态数**不许并进一张表**")
    ap.set_defaults(wait_ready=True)
    ap.add_argument("--warm", action="store_true",
                    help="先跑一次不计入的问诊，让本体层/检索器就位——**量稳态**。"
                         "不加这个开关量到的是冷启动（本体层 2.4s、编码 8.8s 全落在"
                         "第一次里），两种数不可混在一张表里")
    ap.add_argument("--complaints-file", default=None,
                    help="逐行一条主诉，每条各跑 --repeat 次（基线表用）")
    args = ap.parse_args(argv)

    if args.compare and not args.out:
        # 只比不跑：`--compare A --out B` 是"跑一次再跟 A 比"，
        # 只给 `--compare A` 是"拿最新那份跟 A 比"
        latest = sorted(OUT_DIR.glob("consult_*.json"))
        if not latest:
            print(f"{OUT_DIR} 下没有 profile，先跑一次", file=sys.stderr)
            return 2
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        new = json.loads(latest[-1].read_text(encoding="utf-8"))
        print(compare_text(compare(old, new)))
        print(f"\n（新 = {latest[-1].name}）")
        return 0

    complaint = args.complaint
    if not complaint:
        lines = [ln.strip() for ln in (ROOT / "tests" / "queries.txt")
                 .read_text(encoding="utf-8").splitlines() if ln.strip()]
        complaint = lines[0]

    # `import` 这一段：**冷 import 只能量一次**（第二次全在 sys.modules 里）。
    # 这个进程里它已经发生过了（脚本顶上 import 了 core.profiling），所以这里
    # 量的是"把 api.main 拉起来"这一段，并在输出里写明口径。
    t0 = time.perf_counter()
    importlib.import_module("api.main")
    import_ms = (time.perf_counter() - t0) * 1000

    from scripts.bench_consult import (
        AUTO_FAKE_CASES_PER_PHYSICIAN, build_backend, install_fake_cases,
    )
    from core.retrieval import cases_available

    backend = build_backend(args.backend, 0.0, False)
    if args.backend == "fake" and not cases_available():
        install_fake_cases(AUTO_FAKE_CASES_PER_PHYSICIAN)

    if args.warm:
        # 预热跑**不进 runs**：它量的是冷启动，跟后面几次不可比。
        print("— 预热（不计入）")
        profile_once(complaint, backend, trace_alloc=False, stream=args.stream,
                     role=args.role, retriever_mode=args.retriever_mode, probes=False,
                     wait_ready=args.wait_ready)

    complaints = [complaint]
    if args.complaints_file:
        complaints = [ln.strip() for ln
                      in Path(args.complaints_file).read_text(encoding="utf-8").splitlines()
                      if ln.strip()]

    runs = []
    for ci, one in enumerate(complaints):
        for i in range(max(1, args.repeat)):
            print(f"— 主诉 {ci + 1}/{len(complaints)} 第 {i + 1}/{args.repeat} 次")
            run = profile_once(one, backend, trace_alloc=args.alloc,
                               stream=args.stream, role=args.role,
                               retriever_mode=args.retriever_mode,
                               probes=args.probes, wait_ready=args.wait_ready)
            _finish_run(run, import_ms, runs)
            if not run["ok"]:
                print(f"  ✗ {run['error']}", file=sys.stderr)

    report = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": {"id": backend.backend_id(), "model": backend.model_name()},
        "complaint": complaint,
        "complaints": complaints,
        "warm": args.warm,
        "wait_ready": args.wait_ready,
        "repeat": args.repeat,
        "runs": runs,
        **median_profile(runs),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"consult_{int(time.time())}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(runs[-1].get("flamegraph") or _flame(runs[-1]))
    print()
    print(f"逐段墙钟合计 {report['total_stage_wall_ms']:.1f} ms，"
          f"其中等待 {report['total_blocked_ms']:.1f} ms（{report['blocked_ratio']:.0%}）")
    rows = missing_reason_rows(report["missing_stages"])
    report["missing_reasons"] = rows
    if rows:
        print("\n没量到的段（三类，不合并）：")
        for r in rows:
            mark = "✗" if r["kind"] == "instrumentation_gap" else "·"
            print(f"  {mark} {r['stage']:<18s}[{r['kind']}] {r['why']}")
        gaps = [r["stage"] for r in rows if r["kind"] == "instrumentation_gap"]
        if gaps:
            print(f"⚠ 疑似插桩掉了：{gaps}", file=sys.stderr)
    if runs[-1].get("probe_errors"):
        print(f"旁路段没量到：{runs[-1]['probe_errors']}")
    if runs[-1]["unwrappable"]:
        print(f"⚠ 包不上的插桩点：{runs[-1]['unwrappable']}", file=sys.stderr)
    print(f"→ {out}")
    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print()
        print(compare_text(compare(old, report)))
    return 0 if all(r["ok"] for r in runs) else 1


def _flame(run: dict) -> str:
    """从落盘的 stages 重建火焰图文本（`profile_once` 之后 Profiler 已经不在了）。"""
    rows = run.get("stages") or []
    if not rows:
        return "（一段都没记到）"
    top = max(r["wall_ms"] for r in rows) or 1.0
    by_parent: dict = {}
    for r in rows:
        by_parent.setdefault(r.get("parent"), []).append(r)
    lines: list[str] = []

    seen: set[str] = set()

    def emit(rec: dict, depth: int) -> None:
        if rec["name"] in seen:
            return
        seen.add(rec["name"])
        bar = "█" * max(1, int(round(rec["wall_ms"] / top * 48)))
        wait = f" 等 {rec['blocked_ms'] / rec['wall_ms']:.0%}" if rec["wall_ms"] else ""
        # **多个父段要标出来**：`render` 这种被 s1/s2/s3 都调到的函数只会挂在
        # 第一个父段下面，不标的话读起来像"它只在 s1 里跑过"。
        multi = f" ⚠挂在 {rec['n_parents']} 个父段下" if rec.get("n_parents", 0) > 1 else ""
        lines.append(f"{'  ' * depth}{rec['name']:<16s} {bar} "
                     f"{rec['wall_ms']:8.1f}ms{wait}{multi}")
        for child in by_parent.get(rec["name"], []):
            emit(child, depth + 1)

    for root in by_parent.get(None, []):
        emit(root, 0)
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
