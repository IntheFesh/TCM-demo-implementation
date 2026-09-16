"""沙盒里能真量的那几个性能数，写成一份可机读的文件。

## 为什么要这个脚本

R19 要一张「性能前后对照表」，而这个项目的铁律是**任何数字都必须带对照基准**，
外加 `scripts/collect_results.py --check` 要能核对 RESULTS.md 里的每个数。
在此之前，沙盒里量出来的数（启动 import 耗时、全量测试墙钟）只能手抄进文档
——而这个项目手抄数字漂过三次。

所以：能在沙盒里量的，量完落 `eval/bench/sandbox.json`，进凭据注册表；
量不了的（热启动 ≤ 20s 要真模型、一次问诊 ≤ 90s 要真 LLM）标 ⏳ 附上机命令，
**不在这里编一个**。

## 每个数自带对照，不是单独一个数

- `import_api_main_s`：对照是「四段里 import 占多少」——`scripts/bench_startup.py`
  把启动拆成 import / construct / model_load / encode 四段，这里量的是第一段。
  它是沙盒里唯一跟真机可比的一段（另外三段要 cases.json 和真模型）。
- `health_p50_ms_five` vs `health_p50_ms_three`：**同一个进程、同一份代码**，
  只把注册表从五位改回三位。R18-A 把注册表扩到五位，这一对数回答的是
  「多两位医家让 /health 慢了多少」——单报五位那个数说明不了任何事。
- `pytest_*`：对照是上一轮的条数（写在 RESULTS.md 那一行的正文里）。
- `playwright_*`：16 种状态的墙钟。对照是「它值不值得每轮都跑」——
  47 秒的答案是值得。

用法：
    python -m scripts.bench_sandbox              # 只量快的两项（import + /health）
    python -m scripts.bench_sandbox --all        # 连全量测试和 Playwright 一起量
    python -m scripts.bench_sandbox --show       # 只打印上次量的结果
"""
from __future__ import annotations

import argparse
import re
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "eval" / "bench" / "sandbox.json"
# 冷进程 import 量几次取中位数。3 次够了：实测三次落在 0.53~0.56 之间，
# 再多量几次不会改变这个数的量级，而它的用途是量级对照，不是微基准。
IMPORT_REPEATS = 3
# /health 打多少次。200 次下 p50 稳定在 ±0.5ms，50 次时抖动能到 ±3ms。
HEALTH_REQUESTS = 200
# 对照用的三位医家：R17 及以前的注册表。
BASELINE_PHYSICIANS = ("ye_tianshi", "wu_jutong", "zhang_xichun")


def bench_import() -> dict:
    """冷进程 `import api.main` 的耗时。**必须是子进程**——同进程里第二次 import
    走 sys.modules 缓存，量出来是 0，那个 0 什么也不说明。"""
    ts = []
    for _ in range(IMPORT_REPEATS):
        t0 = time.perf_counter()
        r = subprocess.run([sys.executable, "-c", "import api.main"],
                           cwd=ROOT, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"import api.main 失败：{r.stderr[-400:]}")
        ts.append(time.perf_counter() - t0)
    return {
        "import_api_main_s": round(statistics.median(ts), 3),
        "import_api_main_repeats": IMPORT_REPEATS,
        "import_api_main_all_s": [round(t, 3) for t in ts],
    }


def bench_health() -> dict:
    """/health 的 p50/p95，五位医家 vs 三位医家。

    两次测量在**同一个进程、同一份代码**里跑，只改注册表内容——换进程量会把
    进程启动的抖动混进来，那时两个数之差里有多少是注册表的、说不清。
    """
    from fastapi.testclient import TestClient

    import api.main as main_mod

    client = TestClient(main_mod.app)

    def one_round() -> tuple[float, float]:
        ts = []
        for _ in range(HEALTH_REQUESTS):
            t0 = time.perf_counter()
            client.get("/health")
            ts.append((time.perf_counter() - t0) * 1000)
        return (round(statistics.median(ts), 3),
                round(statistics.quantiles(ts, n=20)[18], 3))

    five_p50, five_p95 = one_round()
    original = dict(main_mod.PHYSICIANS)
    try:
        main_mod.PHYSICIANS.clear()
        main_mod.PHYSICIANS.update(
            {k: v for k, v in original.items() if k in BASELINE_PHYSICIANS})
        three_p50, three_p95 = one_round()
    finally:
        # **一定要还原**：这个脚本可能被别的东西 import，改完不还原会让后面
        # 所有读注册表的代码看到一份被砍掉两位的表。
        main_mod.PHYSICIANS.clear()
        main_mod.PHYSICIANS.update(original)
    return {
        "health_requests": HEALTH_REQUESTS,
        "health_p50_ms_five": five_p50, "health_p95_ms_five": five_p95,
        "health_p50_ms_three": three_p50, "health_p95_ms_three": three_p95,
        "health_n_physicians_five": len(original),
        "health_n_physicians_three": len(BASELINE_PHYSICIANS),
    }


def bench_pytest() -> dict:
    """全量测试的墙钟和条数。条数从 pytest 自己那行输出解析，不另数一遍。"""
    import re

    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                       cwd=ROOT, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    tail = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    passed = re.search(r"(\d+) passed", tail)
    skipped = re.search(r"(\d+) skipped", tail)
    failed = re.search(r"(\d+) failed", tail)
    return {
        "pytest_wall_s": round(wall, 1),
        "pytest_passed": int(passed.group(1)) if passed else None,
        "pytest_skipped": int(skipped.group(1)) if skipped else 0,
        "pytest_failed": int(failed.group(1)) if failed else 0,
        "pytest_summary_line": tail,
    }


def bench_playwright() -> dict:
    """16 种前端状态的真浏览器判据 + 截图，墙钟。

    退出码也记下来：**墙钟本身不说明它过了没有**，一个 5 秒就挂掉的跑法
    看起来比 47 秒"更快"。
    """
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "scripts.screenshot_states"],
                       cwd=ROOT, capture_output=True, text=True)
    wall = time.perf_counter() - t0
    import re
    m = re.search(r"(\d+) 种状态全部通过", r.stdout)
    return {
        "playwright_wall_s": round(wall, 1),
        "playwright_exit_code": r.returncode,
        "playwright_states_passed": int(m.group(1)) if m else None,
    }


def collect(run_all: bool) -> dict:
    out: dict = {}
    print("--- 冷进程 import api.main ---")
    out.update(bench_import())
    print(f"  中位数 {out['import_api_main_s']}s（{out['import_api_main_all_s']}）")
    print("--- /health：五位医家 vs 三位医家（同进程，只改注册表） ---")
    out.update(bench_health())
    print(f"  五位 p50 {out['health_p50_ms_five']}ms / p95 {out['health_p95_ms_five']}ms")
    print(f"  三位 p50 {out['health_p50_ms_three']}ms / p95 {out['health_p95_ms_three']}ms")
    if run_all:
        print("--- 全量测试（几十秒） ---")
        out.update(bench_pytest())
        print(f"  {out['pytest_summary_line']}  墙钟 {out['pytest_wall_s']}s")
        print("--- Playwright 16 种状态（几十秒） ---")
        out.update(bench_playwright())
        print(f"  {out['playwright_states_passed']} 种通过，退出码 "
              f"{out['playwright_exit_code']}，墙钟 {out['playwright_wall_s']}s")
    else:
        print("（没传 --all：全量测试和 Playwright 这两项没量。"
              "eval/bench/sandbox.json 里就不会有它们的键——"
              "不是 0，是**没量**，collect_results 会如实报「键取不到」。）")
    out["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["ran_all"] = run_all
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="连全量测试和 Playwright 一起量（慢，但那两个数才是每轮都在变的）")
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--show", action="store_true", help="只打印上次量的结果，不重量")
    ap.add_argument("--round", dest="round_name", metavar="R23",
                    help="同时写一份这一轮的**不可变快照** eval/bench/rounds/<R>.json。"
                         "sandbox.json 会被下一轮整份覆盖，而每轮报告里的凭据记号"
                         "要永远可核——R21 就是因为缺这一份，让 R19 的五个数当场 "
                         "--check 报错（SOURCES.md 第 64 条第十一点）")
    args = ap.parse_args(argv)
    if args.round_name is not None and not re.fullmatch(r"R\d+", args.round_name):
        # 轮次名的形状被凭据注册表 glob 依赖（round.R23.pytest_passed），
        # 写错一个字母的后果是那一轮的快照谁都不核，而它看起来跟被核过的一样。
        ap.error(f"--round 只接受 R + 数字（比如 R23），给的是 {args.round_name!r}")

    if args.show:
        if not args.out.exists():
            print(f"还没量过：{args.out} 不存在。先跑 python -m scripts.bench_sandbox --all")
            return 1
        print(args.out.read_text(encoding="utf-8"))
        return 0

    # **不合并旧结果。** 合并的话，`--all` 跑过一次之后再跑一次不带 --all 的，
    # 文件里会留着上一次的 pytest_wall_s，而它量的是另一份代码——
    # 一个看起来是这次的、实际是上次的数，正是这个项目漂过三次的那种数。
    data = collect(args.all)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {args.out}")
    if args.round_name:
        snapshot = args.out.parent / "rounds" / f"{args.round_name}.json"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出这一轮的不可变快照 {snapshot}"
              f"（报告里引 bench/rounds/{args.round_name}.json:round.{args.round_name}.* ）")
    print("量不了的那几项（热启动 ≤20s 要真模型、一次问诊 ≤90s 要真 LLM）"
          "在 eval/RESULTS.md 里标 ⏳ 并附上机命令，不在这里编。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
