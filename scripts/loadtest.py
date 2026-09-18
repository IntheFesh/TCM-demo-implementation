"""R40：并发压测。**真服务器、真 HTTP、真并发闸**，不是 `TestClient` 里串行跑 N 次。

    python -m scripts.loadtest --backend fake --concurrency 8 --requests 40
    python -m scripts.loadtest --backend fake --sweep 1,2,4,8,16
    python -m scripts.loadtest --backend fake --concurrency 32 --requests 64 --expect-503

## 这个脚本回答什么，不回答什么

回答的：
  · 并发 N 时的 p50 / p90 / p95 / p99 与吞吐（req/s）
  · 并发闸（`MAX_CONCURRENT_CONSULTS`）在什么并发度上开始回 503，以及回得对不对
    （**503 必须带 `Retry-After`**，且这条路上一次模型都没调、预占要退还）
  · 错误分类：连接层 / 4xx / 5xx / 超时，分开计数

不回答的：
  · **真实 LLM 下的延迟**。`--backend fake` 把模型调用换成本地函数，量到的是
    "除模型之外这套东西能扛多少"。真实后端下的数由 `--backend real` 跑，
    但那时 p95 基本等于模型的 p95，这个脚本的信息量很低——两种口径
    在报告里必须分开写，混成一张表是 R38 立过规矩的那类错。
  · 前端。那是 R41 的 `scripts/profile_frontend.py`。

## 为什么 p99 要单独看

问诊是**分钟级**的请求，一个患者等 480 秒是正常的、等 900 秒不是。均值在这种
分布上没有意义（一条超时能把均值拽走 10%，而它对应的是一个真的失败了的患者）。
所以这里报分位数，并且**把超时单独列出来**，不混进延迟分布。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "eval" / "bench"


def rss_mb() -> float | None:
    """本进程的常驻内存（MB）。**没有 psutil 也要能出数**——`/proc/self/status`
    是 Linux 上的直接来源，部署目标就是 Linux（`core/audit.py` 的 fcntl 已经
    把平台钉死了）。读不到就返回 None，不返回 0：0 会被读成"没占内存"。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _pct(values: list[float], q: float) -> float | None:
    """分位数。**不用 statistics.quantiles**：它在 n < 2 时抛异常，而压测跑
    一两条的情况（冒烟）恰好要能出数。"""
    if not values:
        return None
    xs = sorted(values)
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return round(xs[i], 1)


def one_request(base_url: str, complaint: str, timeout: float) -> dict:
    """一次问诊。**失败要分类**：连接不上、超时、4xx、5xx 是四件不同的事。"""
    import httpx

    t0 = time.perf_counter()
    try:
        resp = httpx.post(f"{base_url}/api/consult", timeout=timeout,
                          json={"complaint": complaint, "role": "doctor"})
    except httpx.TimeoutException:
        return {"kind": "timeout", "ms": (time.perf_counter() - t0) * 1000}
    except httpx.TransportError as e:
        return {"kind": "transport", "ms": (time.perf_counter() - t0) * 1000,
                "error": f"{type(e).__name__}: {e}"}
    ms = (time.perf_counter() - t0) * 1000
    row = {"ms": ms, "status": resp.status_code, "bytes": len(resp.content)}
    if resp.status_code == 200:
        row["kind"] = "ok"
    elif resp.status_code == 503:
        # 并发闸。**Retry-After 必须在**：没有它客户端只能瞎重试，
        # 而瞎重试会把一次尖峰变成持续过载。
        row["kind"] = "gate_503"
        row["retry_after"] = resp.headers.get("Retry-After")
    elif 400 <= resp.status_code < 500:
        row["kind"] = "client_error"
        row["error"] = resp.text[:200]
    else:
        row["kind"] = "server_error"
        row["error"] = resp.text[:200]
    return row


def run_level(base_url: str, complaints: list[str], *, concurrency: int,
              n_requests: int, timeout: float) -> dict:
    """一个并发档。`n_requests` 条请求由 `concurrency` 个线程发完。"""
    rows: list[dict] = []
    rss_before = rss_mb()
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [pool.submit(one_request, base_url, complaints[i % len(complaints)], timeout)
                for i in range(n_requests)]
        for f in cf.as_completed(futs):
            rows.append(f.result())
    wall = time.perf_counter() - t0

    ok_ms = [r["ms"] for r in rows if r["kind"] == "ok"]
    kinds: dict[str, int] = {}
    for r in rows:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    gates = [r for r in rows if r["kind"] == "gate_503"]
    rss_after = rss_mb()
    return {
        "concurrency": concurrency,
        "n_requests": n_requests,
        "wall_s": round(wall, 3),
        # 内存：并发闸的取值靠这两个数说话，不靠感觉。
        # **压测客户端与服务端在同一个进程里**（live_server 就在本进程），
        # 所以这是两者之和——比较的是"并发档之间的增量"，不是绝对值。
        "rss_before_mb": rss_before,
        "rss_after_mb": rss_after,
        "rss_delta_mb": (round(rss_after - rss_before, 1)
                         if (rss_before is not None and rss_after is not None) else None),
        # 吞吐按**成功的**请求算：把 503 算进吞吐会让"闸门把请求全挡掉"
        # 看起来像"吞吐很高"。
        "throughput_rps": round(len(ok_ms) / wall, 3) if wall else None,
        "kinds": kinds,
        "n_ok": len(ok_ms),
        "p50_ms": _pct(ok_ms, 0.50), "p90_ms": _pct(ok_ms, 0.90),
        "p95_ms": _pct(ok_ms, 0.95), "p99_ms": _pct(ok_ms, 0.99),
        "max_ms": round(max(ok_ms), 1) if ok_ms else None,
        "mean_ms": round(statistics.mean(ok_ms), 1) if ok_ms else None,
        "response_bytes": max((r.get("bytes") or 0) for r in rows) if rows else 0,
        # 闸门回 503 时 Retry-After 缺了就是个 bug，这里当场判
        "gate_503_without_retry_after": sum(1 for r in gates if not r.get("retry_after")),
        "errors": [r for r in rows if r["kind"] in
                   ("transport", "client_error", "server_error", "timeout")][:5],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="fake", choices=["fake", "real"])
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--requests", type=int, default=None,
                    help="默认 = concurrency × 3（每个线程至少发三条，避免只量到冷的那一条）")
    ap.add_argument("--sweep", default=None,
                    help="逗号分隔的并发档，逐档跑（例：1,2,4,8,16）。给这个就忽略 --concurrency")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--expect-503", action="store_true",
                    help="断言这一跑确实触发了并发闸（否则退出码非 0）。"
                         "**闸门要验它真的会挡**，不是只验它没挡")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    complaints = [ln.strip() for ln in (ROOT / "tests" / "queries.txt")
                  .read_text(encoding="utf-8").splitlines() if ln.strip()]

    from core.retrieval import cases_available
    from scripts.bench_consult import (
        AUTO_FAKE_CASES_PER_PHYSICIAN, build_backend, install_fake_cases,
    )
    from scripts.live_server import live_server

    backend = build_backend(args.backend, 0.0, False)
    if args.backend == "fake" and not cases_available():
        install_fake_cases(AUTO_FAKE_CASES_PER_PHYSICIAN)

    # 后端钉死的方式跟 profiler 同一套（`use_llm` 是 ContextVar，跨不过
    # uvicorn 的 threadpool，更跨不过 SSE 的裸线程）——**同一个问题只有一处解法**。
    from scripts.profile_consult import _ForceBackend

    import api.main as api_main

    levels = ([int(x) for x in args.sweep.split(",") if x.strip()]
              if args.sweep else [args.concurrency])
    results = []
    with _ForceBackend(backend), live_server() as base_url:
        for c in levels:
            n = args.requests if args.requests else c * 3
            print(f"— 并发 {c}，共 {n} 条")
            row = run_level(base_url, complaints, concurrency=c,
                            n_requests=n, timeout=args.timeout)
            results.append(row)
            print(f"  p50 {row['p50_ms']} / p95 {row['p95_ms']} / p99 {row['p99_ms']} ms，"
                  f"吞吐 {row['throughput_rps']} req/s，"
                  f"RSS {row['rss_before_mb']}→{row['rss_after_mb']} MB，"
                  f"分类 {row['kinds']}")

    report = {
        "kind": "loadtest",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": {"id": backend.backend_id(), "model": backend.model_name()},
        "backend_caveat": ("fake 后端：量到的是「除模型之外这套东西能扛多少」，"
                           "不是真实 LLM 下的延迟——两种口径不许并进一张表"
                           if args.backend == "fake" else None),
        "max_concurrent_consults": api_main.MAX_CONCURRENT_CONSULTS,
        "levels": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"loadtest_{int(time.time())}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}")

    bad = 0
    for row in results:
        if row["gate_503_without_retry_after"]:
            print(f"✗ 并发 {row['concurrency']}：{row['gate_503_without_retry_after']} 条 "
                  "503 没带 Retry-After", file=sys.stderr)
            bad += 1
        for k in ("transport", "server_error", "timeout"):
            if row["kinds"].get(k):
                print(f"✗ 并发 {row['concurrency']}：{row['kinds'][k]} 条 {k}"
                      f"（样例 {row['errors'][:1]}）", file=sys.stderr)
                bad += 1
    if args.expect_503 and not any(r["kinds"].get("gate_503") for r in results):
        print(f"✗ --expect-503：没有一条被并发闸挡下（闸门 "
              f"{api_main.MAX_CONCURRENT_CONSULTS}，本次最高并发 {max(levels)}）",
              file=sys.stderr)
        bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
