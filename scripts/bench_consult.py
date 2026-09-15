"""一次完整问诊的性能基准：总耗时、每一步耗时、每次 LLM 调用的参数与 usage。

## 为什么要这个脚本

R12 要动三个杠杆（三医家并发、embedding 缓存、思考模式按步控制），每一个都必须
有**改动前的数字**才谈得上"提升了多少"（CLAUDE.md「任何数字都必须带对照」）。
没有基线的"感觉快了"在这个项目里等于没有意义。

## 两条设计决定

**一、不改业务代码，只在边界上观测。** 每一步的耗时从 `consult(on_step=...)` 已有的
SSE 事件里拿（`s1_done` / `s2_done` / `physician_start` / `physician_done` / …），
不往 `core/chain.py` 里塞计时代码。每次调用的参数从**包住 `_complete` 的一层**拿
——`_complete` 是 `LLMBackend` 契约里"单次原始调用"的唯一入口，包住它就等于包住了
所有后端的所有调用，不需要为每种后端各写一份。

**二、报告的是观测到的东西，不是假设的东西。** `thinking` / `reasoning_effort` /
`max_tokens` 都从那一次调用**实际收到的参数**里读：R11 还没做按步控制，所以这几个
字段现在是 `null`——那是事实，不是缺失。R12 把它们传下去之后，这个脚本不用改一行
就会报出真值。

## usage（completion_tokens / reasoning_tokens）

`_complete` 只返回正文，拿不到 `resp.usage`。所以再包一层 **SDK 调用点**
（`backend.client.chat.completions.create`），只对 OpenAI 兼容后端有效；别的后端
（fake / replay / 进程内 vLLM）如实报 `usage_available: false` 并写明原因，不编数。
墙钟超时后 `abort_in_flight()` 会把客户端丢掉重建，那之后的调用取不到 usage
——这种情况同样按"这一次没有"如实记，不回填上一次的值。

    python -m scripts.bench_consult --backend fake --repeat 1
    python -m scripts.bench_consult --repeat 3 --no-react        # 真实后端，要 key
    python -m scripts.bench_consult --backend fake --fake-latency 1.0
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "eval" / "bench"

# 默认主诉取 eval 用的第一条（tests/queries.txt 的首行同一条）：基线和评测用同一条
# 输入，两边的数字才能互相印证。
DEFAULT_COMPLAINT = "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。"

# 假后端给字符串字段填的内容。**刻意是一句能看懂的中文**：这段文本会出现在
# 假跑的结果里，写成无意义的 "x" 会让人分不清"这是假数据"还是"模型真答了这个"。
FAKE_TEXT = "基准测试假数据"

# 自动装合成医案时每位医家给几条。3 条够让检索返回 top-3、S3 有东西可引，
# 再多只是拖慢构造，对耗时没有影响（检索在这条路径上是毫秒级的内存计算）。
AUTO_FAKE_CASES_PER_PHYSICIAN = 3


# ---------- 从 pydantic schema 造一个最小合法实例 ----------


def minimal_payload(schema: type[BaseModel]) -> dict:
    """按 JSON Schema 造一个字段齐全的最小合法实例。

    **按 schema 造而不是给每个 schema 手写一份**：链路上的 schema 有六七个
    （S1/S2/S3 两种/追问/ReAct 步），手写一份就得跟着 `core/schemas.py` 一起改，
    漏一个就是假后端在某条路径上突然抛 AssertionError——而那条路径往往正是
    `--react` 才会走到的那条。
    """
    js = schema.model_json_schema()
    return _minimal(js, js.get("$defs", {}))


def _minimal(node: dict, defs: dict) -> Any:
    if "$ref" in node:
        return _minimal(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    for branch_key in ("anyOf", "oneOf"):
        if branch_key in node:
            # 可选字段是 `X | None`，JSON Schema 里是 anyOf[X, null]。取第一个非 null
            # 分支：填上真值比填 null 更能走到下游逻辑（null 会让一半分支被跳过，
            # 那样测出来的耗时不是完整链路的耗时）。
            for branch in node[branch_key]:
                if branch.get("type") != "null":
                    return _minimal(branch, defs)
            return None
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    node_type = node.get("type")
    if node_type == "object":
        props = node.get("properties", {})
        required = node.get("required", list(props))
        return {k: _minimal(v, defs) for k, v in props.items() if k in required}
    if node_type == "array":
        count = max(1, int(node.get("minItems", 1)))
        return [_minimal(node.get("items") or {"type": "string"}, defs)] * count
    if node_type == "integer":
        return 1
    if node_type == "number":
        return 1.0
    if node_type == "boolean":
        return True
    if node_type == "null":
        return None
    return FAKE_TEXT


# ---------- 假后端 ----------


def build_fake_backend(latency: float):
    """每次调用睡 latency 秒、返回最小合法实例的后端。

    latency 是并发正确性的机器可验依据：三位医家串行跑 3 次调用要 3×latency，
    并发之后应该 ≈1×latency（R12 的验收判据之一）。
    """
    from core.llm import LLMBackend

    class FakeBenchBackend(LLMBackend):
        def __init__(self) -> None:
            self.n_calls = 0

        def model_name(self) -> str:
            return f"fake-bench(latency={latency}s)"

        def backend_id(self) -> str:
            return "fake"

        def comparability_warning(self) -> str | None:
            return ("后端：fake（基准测试用，不发任何网络请求）。这一轮的耗时只反映"
                    "链路自身的开销和人为设定的 latency，**不可用于报告里的任何数字**。")

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kwargs) -> str:
            self.n_calls += 1
            if latency > 0:
                time.sleep(latency)
            if schema is None:
                return FAKE_TEXT
            return json.dumps(minimal_payload(schema), ensure_ascii=False)

    return FakeBenchBackend()


# ---------- 合成语料（沙盒里没有 cases.json 时用） ----------


def install_fake_cases(n_per_physician: int) -> int:
    """给每位医家造 n 条合成医案，装成检索单例。返回总条数。

    存在的理由：`cases.json` 是生成物、不进版本控制，沙盒里没有它——检索层一开口
    就 `RetrievalUnavailable`，三位医家的 S3 一次都不跑，基准量到的只有 S1/S2。
    而 R12 要验的正是"三位医家并发之后 S3 那一段的墙钟从 3×latency 降到 ≈1×latency"，
    没有 S3 就没有可验的东西。

    **合成语料只影响能不能跑到 S3，不影响耗时的含义**：检索本身在这条路径上是纯
    内存计算（毫秒级），耗时几乎全在 LLM 调用上。报告里 `fake_cases` 非空就说明
    这一轮用的是合成语料，不会被当成真机数字。
    """
    from core import retrieval
    from core.physicians import PHYSICIANS
    from core.retrieval import Retriever
    from core.schemas import CaseRecord

    cases: list[CaseRecord] = []
    for pid in PHYSICIANS:
        for i in range(n_per_physician):
            cases.append(CaseRecord(
                case_id=f"{pid}-bench-{i:03d}", case_group_id=f"{pid}-bench-g{i:03d}",
                physician=pid, visit_index=0,
                raw=f"{FAKE_TEXT}：胃脘胀痛，嗳气泛酸。",
                symptoms=["胃脘胀痛", "嗳气"], herbs=["柴胡", "白芍"],
                raw_excerpt=f"{FAKE_TEXT}：胃脘胀痛，嗳气泛酸，脉弦。",
            ))

    class BenchRetriever(Retriever):
        def search(self, query, physician, k=3, min_score=0.0, **kwargs):
            hits = [c for c in cases if c.physician == physician][:k]
            return [(c, 0.9) for c in hits]

    retrieval._retriever_singleton = BenchRetriever()
    return len(cases)


# ---------- 观测层 ----------


class CallRecorder:
    """包住 `_complete` 记每次调用的参数与耗时；能包到 SDK 时顺带记 usage。"""

    def __init__(self, backend) -> None:
        self.backend = backend
        self.calls: list[dict] = []
        self.usage_available = False
        self.usage_note = ""
        self._pending_usage: dict | None = None
        self._orig_complete = backend._complete
        self._orig_create = None
        self._completions = None
        backend._complete = self._wrapped_complete
        self._hook_usage()

    def restore(self) -> None:
        """把包的那两层拆掉。**必须拆**：`--repeat 3` 会建三个 CallRecorder，不拆的话
        第二次的包会套在第一次的包外面，同一次调用被记进两个 recorder——第一次那轮的
        `calls` 会在它早已结束之后继续变长，每次跑的调用数全是错的。"""
        self.backend._complete = self._orig_complete
        if self._completions is not None and self._orig_create is not None:
            self._completions.create = self._orig_create

    def _wrapped_complete(self, messages, temperature, max_tokens=None, schema=None,
                          physician=None, **kwargs):
        self._pending_usage = None
        t0 = time.perf_counter()
        error = None
        try:
            return self._orig_complete(messages, temperature, max_tokens=max_tokens,
                                       schema=schema, physician=physician, **kwargs)
        except Exception as e:  # noqa: BLE001 - 失败的调用也要记进基准，它照样花了时间
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            self.calls.append({
                "seq": len(self.calls) + 1,
                "schema": schema.__name__ if schema is not None else None,
                "physician": physician,
                "elapsed_s": round(time.perf_counter() - t0, 4),
                # 下面四个读的是这次调用**实际收到的参数**。R11 还没做按步控制，
                # thinking/reasoning_effort 现在恒为 None——那是事实不是缺失。
                "max_tokens": max_tokens,
                "temperature": temperature,
                "thinking": kwargs.get("thinking"),
                "reasoning_effort": kwargs.get("reasoning_effort"),
                "usage": self._pending_usage,
                "error": error,
            })

    def _hook_usage(self) -> None:
        """包住 OpenAI SDK 的 create()。拿不到就说清楚为什么，不编数。"""
        client = getattr(self.backend, "client", None)
        if client is None:
            self.usage_note = (f"后端 {self.backend.backend_id()} 不走 OpenAI 兼容 SDK，"
                               "没有 usage 可读（completion_tokens / reasoning_tokens 恒为 null）")
            return
        try:
            completions = client.chat.completions
            orig_create = completions.create
        except AttributeError as e:
            self.usage_note = f"SDK 结构不是预期的 client.chat.completions.create（{e}）"
            return

        def create(**kwargs):
            resp = orig_create(**kwargs)
            usage = getattr(resp, "usage", None)
            if usage is not None:
                details = getattr(usage, "completion_tokens_details", None)
                self._pending_usage = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "reasoning_tokens": getattr(details, "reasoning_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
            return resp

        completions.create = create
        self._completions = completions
        self._orig_create = orig_create
        self.usage_available = True
        self.usage_note = ("usage 从 SDK 响应现读。墙钟超时后 abort_in_flight() 会重建"
                           "客户端，那之后的调用 usage 为 null——不回填上一次的值。")


class StepTimer:
    """把 consult 的 SSE 事件变成"这一步花了多久"。

    每个事件只带"发生了"，不带耗时，所以耗时是**相邻两个事件之间的墙钟差**。
    R12 三医家并发之后 physician_start/physician_done 会交错，所以医家那几步
    单独按 (start, done) 配对算，不用相邻差——不然并发下会算出负数或串起来的值。
    """

    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.events: list[dict] = []
        self._last = self.t0
        self._physician_start: dict[str, float] = {}
        self.by_physician: dict[str, float] = {}
        # S3 这一段的**墙钟**：第一位医家开始到最后一位医家结束。串行时它等于三位
        # 之和，并发时等于最慢的那一位——R12 三医家并发的验收判据就是这两个数的比。
        self._s3_first_start: float | None = None
        self._s3_last_done: float | None = None

    def __call__(self, event: str, data: dict) -> None:
        now = time.perf_counter()
        physician = data.get("physician")
        if event == "physician_start" and physician:
            self._physician_start[physician] = now
            if self._s3_first_start is None:
                self._s3_first_start = now
        elif event == "physician_done" and physician:
            started = self._physician_start.pop(physician, None)
            if started is not None:
                self.by_physician[physician] = round(now - started, 4)
            self._s3_last_done = now
        self.events.append({
            "event": event,
            "physician": physician,
            "t_s": round(now - self.t0, 4),
            "dt_s": round(now - self._last, 4),
        })
        self._last = now

    def by_step(self) -> dict:
        """链路上几个自然边界各自的耗时。缺的步（没追问、没残差）不出现在结果里
        ——填 0 会让"这一步没跑"和"这一步跑了 0 秒"分不开。"""
        out: dict[str, Any] = {}
        for ev in self.events:
            if ev["event"] in ("s1_done", "s2_done", "followup_done", "residual_done"):
                out.setdefault(ev["event"].removesuffix("_done"), ev["dt_s"])
        if self.by_physician:
            out["s3_by_physician"] = dict(self.by_physician)
            # 两个数放一起才有意义：sum 是"总共干了多少活"，wall 是"这一段占了多少
            # 墙钟"。串行时 wall ≈ sum；并发之后 wall 应该掉到 max(各位医家) 附近，
            # 而 sum 不变——sum 不变正是"没有偷偷少干活"的证据。
            out["s3_sum"] = round(sum(self.by_physician.values()), 4)
            out["s3_slowest"] = round(max(self.by_physician.values()), 4)
            if self._s3_first_start is not None and self._s3_last_done is not None:
                out["s3_wall"] = round(self._s3_last_done - self._s3_first_start, 4)
        return out


# ---------- 主流程 ----------


def invalid_reason(result: dict | None) -> str | None:
    """这次 consult 的结果能不能当性能基准用。不能就返回一句话说明为什么。

    **存在的理由**：`consult()` 把"检索不可用"放进返回值的 `retrieval_error` 而不是
    抛异常（那是它对的设计——HTTP 调用方要拿到一句人话，不是 500），于是 `run_once`
    的 try/except 什么都接不住：三位医家一个都没跑，基准照样打印「1/1 次跑成功」。
    一份 `ok: true` 的报告会被直接引用，比崩掉危险得多。

    三种"跑了但不能用"：检索不可用、被安全否决、信息不足——它们都不产生 S3，而 S3
    正是这个基准要量的那一段。
    """
    if result is None:
        return "consult() 没有返回结果"
    if result.get("retrieval_error"):
        return f"检索不可用，三位医家都没跑：{result['retrieval_error']}"
    if result.get("rejected"):
        return f"被安全否决，不产生方药，不能当性能基准：{result.get('reject_reason')}"
    if result.get("insufficient"):
        return f"信息不足，没跑到 S3：{result.get('insufficient_reason')}"
    from core.physicians import PHYSICIANS

    got = len(result.get("results") or [])
    if got != len(PHYSICIANS):
        return f"只有 {got}/{len(PHYSICIANS)} 位医家跑出了结果"
    return None


def run_once(complaint: str, use_react: bool, retriever_mode: str | None,
             backend) -> dict:
    from core.chain import consult
    from core.llm import use_llm

    recorder = CallRecorder(backend)
    timer = StepTimer()
    t0 = time.perf_counter()
    result: dict | None = None
    error = None
    try:
        with use_llm(backend):
            result = consult(complaint, use_react=use_react, on_step=timer,
                             retriever_mode=retriever_mode)
    except Exception as e:  # noqa: BLE001 - 一次跑挂掉不该把整批基准丢掉
        error = f"{type(e).__name__}: {e}"
    finally:
        recorder.restore()
    elapsed = time.perf_counter() - t0
    if error is None:
        error = invalid_reason(result)
    manifest = (result or {}).get("manifest") or {}
    return {
        "ok": error is None,
        "error": error,
        "elapsed_s": round(elapsed, 4),
        "llm_calls": manifest.get("llm_calls", len(recorder.calls)),
        "n_raw_calls": len(recorder.calls),
        "by_step": timer.by_step(),
        "events": timer.events,
        "calls": recorder.calls,
        "usage_available": recorder.usage_available,
        "usage_note": recorder.usage_note,
    }


def summarize(runs: list[dict]) -> dict:
    ok = [r for r in runs if r["ok"]]
    def stat(values: list[float]) -> dict | None:
        if not values:
            return None
        return {"mean": round(statistics.fmean(values), 4),
                "min": round(min(values), 4), "max": round(max(values), 4)}
    step_keys = {k for r in ok for k in r["by_step"] if isinstance(r["by_step"][k], (int, float))}
    return {
        "n_runs": len(runs),
        "n_ok": len(ok),
        "elapsed_s": stat([r["elapsed_s"] for r in ok]),
        "llm_calls": stat([float(r["llm_calls"]) for r in ok]),
        "by_step_mean": {k: round(statistics.fmean(
            [r["by_step"][k] for r in ok if k in r["by_step"]]), 4) for k in sorted(step_keys)},
        "usage_available": any(r["usage_available"] for r in ok),
        # **按 schema 汇总 token 用量**：R13 把开思考那一步的默认上限从 16384 提到
        # 32768，"S3 还会不会截断"要靠 completion_tokens + reasoning_tokens 贴着
        # 上限没有来判断，而不是靠"这次没报错"。没有 usage 的后端这里是空字典。
        "usage_by_schema": _usage_by_schema(ok),
    }


def _usage_by_schema(runs: list[dict]) -> dict:
    """每种 schema 的 token 用量均值与最大值。看最大值而不是只看均值——
    截断是被最长的那一次触发的，均值会把它抹平。"""
    buckets: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        for call in run["calls"]:
            usage = call.get("usage")
            if not usage or not call.get("schema"):
                continue
            bucket = buckets.setdefault(call["schema"], {})
            for field in ("prompt_tokens", "completion_tokens",
                          "reasoning_tokens", "total_tokens"):
                value = usage.get(field)
                if value is not None:
                    bucket.setdefault(field, []).append(float(value))
    return {
        schema: {field: {"mean": round(statistics.fmean(values), 1),
                         "max": max(values), "n": len(values)}
                 for field, values in fields.items()}
        for schema, fields in buckets.items()
    }


def build_backend(kind: str, fake_latency: float):
    if kind == "fake":
        return build_fake_backend(fake_latency)
    from core.llm import get_llm

    return get_llm()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--complaint", default=DEFAULT_COMPLAINT)
    ap.add_argument("--repeat", type=int, default=1)
    react = ap.add_mutually_exclusive_group()
    react.add_argument("--react", dest="react", action="store_true")
    react.add_argument("--no-react", dest="react", action="store_false")
    ap.set_defaults(react=False)
    ap.add_argument("--backend", default="fake", choices=["fake", "real"],
                    help="fake=不发网络请求的假后端（验脚本、量并发）；real=走 LLM_MODE 配的那个")
    ap.add_argument("--fake-latency", type=float, default=0.0,
                    help="假后端每次调用睡多少秒——串行/并发的差就是靠它量出来的")
    ap.add_argument("--retriever-mode", default=None)
    ap.add_argument("--fake-cases", type=int, default=0, metavar="N",
                    help="没有 cases.json 时给每位医家造 N 条合成医案，好让 S3 真的跑起来")
    ap.add_argument("--no-auto-fake-cases", dest="auto_fake_cases", action="store_false",
                    help="关掉「假后端 + 没有 cases.json 时自动装合成医案」这个默认行为")
    ap.set_defaults(auto_fake_cases=True)
    ap.add_argument("--out", default=None, help="默认 eval/bench/<时间戳>.json")
    args = ap.parse_args(argv)

    if args.repeat < 1:
        print("--repeat 至少是 1", file=sys.stderr)
        return 2

    backend = build_backend(args.backend, args.fake_latency)
    # 假后端 + 仓库里没有 cases.json = 沙盒里的常态。这时自动装合成医案，比让人拿到
    # 一份"只跑了 S1/S2 却标着成功"的报告好——但**必须在输出里标出来**（fake_cases_auto），
    # 不然这份报告跟真语料跑出来的长得一模一样。
    from core.retrieval import CASES_PATH

    auto = (args.fake_cases == 0 and args.auto_fake_cases
            and args.backend == "fake" and not CASES_PATH.exists())
    n_per_physician = args.fake_cases or (AUTO_FAKE_CASES_PER_PHYSICIAN if auto else 0)
    n_fake_cases = install_fake_cases(n_per_physician) if n_per_physician > 0 else 0
    runs = [run_once(args.complaint, args.react, args.retriever_mode, backend)
            for _ in range(args.repeat)]
    report = {
        "kind": "consult",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": {
            "complaint": args.complaint,
            "repeat": args.repeat,
            "use_react": args.react,
            "retriever_mode": args.retriever_mode,
            "backend_arg": args.backend,
            "fake_latency_s": args.fake_latency,
            "fake_cases": n_fake_cases,
            "fake_cases_auto": auto,
        },
        "backend": {
            "id": backend.backend_id(),
            "model": backend.model_name(),
            "comparability_warning": backend.comparability_warning(),
        },
        "runs": runs,
        "summary": summarize(runs),
    }
    # 文件名带后端：`consult_fake_*.json` 被 .gitignore 挡掉，`consult_real_*.json`
    # 不挡。理由是项目纪律「花过 API 钱的产物进版本控制，纯本地能重算的不进」——
    # 而假跑的秒数一旦混进仓库，下一个人没法从文件名看出它不是真的。
    out = (Path(args.out) if args.out
           else BENCH_DIR / f"consult_{args.backend}_{int(time.time())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = report["summary"]
    print(f"后端 {report['backend']['id']}（{report['backend']['model']}）　"
          f"{s['n_ok']}/{s['n_runs']} 次跑成功")
    if s["elapsed_s"]:
        print(f"总耗时　mean {s['elapsed_s']['mean']}s　min {s['elapsed_s']['min']}s　"
              f"max {s['elapsed_s']['max']}s")
        print(f"LLM 调用　mean {s['llm_calls']['mean']}")
        for step, value in s["by_step_mean"].items():
            print(f"  {step:16s} {value}s")
    for r in runs:
        if not r["ok"]:
            print(f"✗ 有一次跑失败：{r['error']}", file=sys.stderr)
    print(f"→ {out}")
    return 0 if s["n_ok"] == s["n_runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
