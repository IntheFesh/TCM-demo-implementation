"""R43：**全站交互延迟**的量具。INP 三段口径，真实输入，真实浏览器。

## 为什么不用 R41 那个 `measureInteraction`

R41 在 `scripts/profile_frontend.py` 里有一个 `measureInteraction(selector)`：
`el.click()` 之后等两帧，报一个总毫秒数。它在当时够用（那一轮量的是首屏与渲染），
但它**不是 INP 的口径**，而且有两个会让数字说谎的地方：

1. **`el.click()` 造的是不可信事件。** Chromium 只对可信输入开某些通路
   （R41 已经在 CLS 那条上踩过一次：`hadRecentInput` 的 500 ms 窗口只认可信事件）。
   这里同理：不可信点击不进 `PerformanceEventTiming`，也就量不到真实的输入延迟。
2. **一个总数掩盖了三件事。** INP 是三段之和，而三段的修法完全不同：

   | 段 | 含义 | 慢了怎么修 |
   |---|---|---|
   | input delay | 事件排队等主线程 | 主线程上有长任务 → 切片 / 挪到 Worker |
   | processing | 事件处理函数本身 | 那个函数太重 → 少做、缓存、批处理 |
   | presentation | 处理完到下一帧画出来 | 改了太多 DOM / 强制同步布局 → 减少写、读写分离 |

   报一个 320 ms 的总数，没人知道该去改哪一处。**所以这一轮三段分开报。**

## 口径

用 `PerformanceObserver({ type: "event", durationThreshold: 16, buffered: true })`
收 `PerformanceEventTiming`，对每一次交互取：

    input_delay   = processingStart - startTime
    processing    = processingEnd   - processingStart
    presentation  = startTime + duration - processingEnd
    inp           = duration          （= 三段之和，向上取整到 8 ms 的倍数）

`duration` 是浏览器自己算的"从输入到下一次绘制"，**它才是 INP**；三段是拆解，
不是另一套测量。两者对不上就说明观察器漏了 entry（会在结果里报 `n_events`）。

输入一律走 Playwright 的真实鼠标/键盘（`page.click` / `page.type`），
**不用 `el.click()`**。

## 预算

Core Web Vitals 的 INP 阈值：≤200 ms 好、≤500 ms 需改进、>500 ms 差。
本项目取 **200 ms** 作为每一个交互的上限（`INP_BUDGET_MS`），另对"必须瞬时"的
那几类（切页签、展开折叠、点节点）取更紧的 **100 ms**。判据进
`tests/test_interaction_budget.py`——**预算进测试**（总纲 §12）。

用法：

    python3 -m scripts.profile_interactions                 # 全部场景
    python3 -m scripts.profile_interactions --only tab_switch
    python3 -m scripts.profile_interactions --out eval/profile/inp_after.json
    python3 -m scripts.profile_interactions --compare eval/profile/inp_before.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.profile_frontend import _chromium_path, _fixtures, _wait_ready  # noqa: E402
from scripts.screenshot_ui import _free_port  # noqa: E402

VIEWPORT = {"width": 1440, "height": 900}

#: Core Web Vitals 的 "good" 阈值。**不是我们定的，是外部基准**——
#: 自己定一个宽松的数然后宣布达标，那个数就没有意义（总纲：任何数字都要有对照）。
INP_BUDGET_MS = 200
#: "必须瞬时"的那几类交互（纯前端、不发请求）。慢于这个数人就会觉得卡。
INP_TIGHT_BUDGET_MS = 100

#: 每次交互重复几遍取中位数。**单次读数在浏览器里没有意义**（GC、字体加载、
#: 首次 JIT 都会让第一次特别慢），而 INP 的定义本身就是"最差的那几次之一"，
#: 所以这里同时报中位数与最大值。
REPEATS = 5

OBSERVER_JS = r"""
(() => {
  window.__inp = { events: [], longTasks: [] };
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        window.__inp.events.push({
          name: e.name,
          start: e.startTime,
          duration: e.duration,
          input_delay: Math.max(0, e.processingStart - e.startTime),
          processing: Math.max(0, e.processingEnd - e.processingStart),
          presentation: Math.max(0, e.startTime + e.duration - e.processingEnd),
          target: e.target ? (e.target.id || e.target.tagName || "") : "",
        });
      }
    }).observe({ type: "event", durationThreshold: 16, buffered: true });
  } catch (err) { window.__inp.unsupported = String(err); }
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        window.__inp.longTasks.push({ start: e.startTime, ms: e.duration });
      }
    }).observe({ type: "longtask", buffered: true });
  } catch (err) { /* 老浏览器没有 longtask */ }
  window.__inpReset = () => { window.__inp.events = []; window.__inp.longTasks = []; };
})();
"""


def _med(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[len(s) // 2]


#: 场景 = 一次交互 + 它需要的准备。
#:
#: `setup` 跑在页面里（可 await），每次重复前都会重跑 `reset`（如果给了），
#: 然后由 Playwright 做**真实输入**：`click` 用鼠标，`type` 用键盘。
#:
#: **覆盖的是"人在这一页上真的会点的每一样东西"**，不是"容易量的那几样"。
SCENES: dict[str, dict] = {
    "tab_switch": {
        "what": "切到图谱浏览器页签（纯前端切换，必须瞬时）",
        "setup": "",
        "click": "#tab-btn-graph-browser",
        "reset": "switchTab('consult');",
        "tight": True,
    },
    "example_pick": {
        "what": "点首屏的示例主诉（填进输入框）",
        "setup": "",
        "click": ".example-item",
        "reset": "document.getElementById('complaint').value = '';",
        "tight": True,
    },
    "typing": {
        "what": "在主诉输入框里连打 20 个字（每个字都是一次交互）",
        "setup": "",
        "type": ("#complaint", "胃脘胀痛食后加重嗳气泛酸每因情志不畅而发纳差"),
        "reset": "document.getElementById('complaint').value = '';",
        "tight": True,
    },
    "chain_section_toggle": {
        "what": "展开/折叠九段里的一段",
        "setup": ("renderComplaintBody(window.COMPLAINT); SERVER_S3_MODE = 'structured';"
                  " renderConsultResult(window.R37_DONE_PAYLOAD);"),
        "click": "#chain-flow .chain-sec .chain-head",
        "tight": True,
    },
    "graph_node_click": {
        "what": "点图上一个节点（钉住 tooltip + 取释义）",
        "setup": ("renderComplaintBody(window.COMPLAINT); SERVER_S3_MODE = 'structured';"
                  " renderConsultResult(window.R37_DONE_PAYLOAD);"
                  " document.getElementById('detail-zone').open = true;"
                  " skipAnimation(); await new Promise(r => setTimeout(r, 800));"),
        "click_canvas": True,
        "tight": False,
    },
    "graph_replay": {
        "what": "重播生长动画（整张图重画一遍）",
        "setup": ("renderComplaintBody(window.COMPLAINT);"
                  " renderConsultResult({...window.DONE_PAYLOAD, graph: window.NINE_LAYER_GRAPH});"
                  " document.getElementById('detail-zone').open = true;"
                  " skipAnimation(); await new Promise(r => setTimeout(r, 600));"),
        "click": "#skip-btn",
        "tight": False,
    },
    "png_export": {
        "what": "导出整图 PNG（2 倍，同步的 canvas 编码）",
        "setup": ("renderComplaintBody(window.COMPLAINT);"
                  " renderConsultResult({...window.DONE_PAYLOAD, graph: window.NINE_LAYER_GRAPH});"
                  " document.getElementById('detail-zone').open = true;"
                  " skipAnimation(); await new Promise(r => setTimeout(r, 600));"),
        "click": "#png-btn",
        "tight": False,
    },
    "role_switch": {
        "what": "切换角色（自绘下拉：打开）",
        "setup": "",
        "click": "#role-select + .sel-btn, .sel-btn",
        "tight": True,
    },
    "browser_expand": {
        "what": "图谱浏览器：展开一个证素（加节点 + 重排）",
        "setup": ("switchTab('graph-browser'); await loadGraphBrowserData();"
                  " await new Promise(r => setTimeout(r, 600));"),
        "click_gb_canvas": True,
        "tight": False,
    },
    "browser_reset": {
        "what": "图谱浏览器：重置视图（清空 + 重铺首屏）",
        "setup": ("switchTab('graph-browser'); await loadGraphBrowserData();"
                  " await new Promise(r => setTimeout(r, 600));"),
        "click": "#gb-reset-btn",
        "tight": False,
    },
}


def _run_scene(browser, base_url: str, name: str) -> dict:
    scene = SCENES[name]
    page = browser.new_page(viewport=VIEWPORT)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(OBSERVER_JS)
    page.goto(f"{base_url}/app/index.html", wait_until="networkidle")
    for var, value in _fixtures().items():
        page.evaluate(f"window.{var} = {json.dumps(value, ensure_ascii=False)};")
    if scene.get("setup"):
        page.evaluate(f"(async () => {{ {scene['setup']} }})()")
        page.wait_for_timeout(400)

    samples: list[dict] = []
    note = None
    for i in range(REPEATS):
        if scene.get("reset") and i:
            page.evaluate(f"(async () => {{ {scene['reset']} }})()")
            page.wait_for_timeout(120)
        page.evaluate("window.__inpReset && window.__inpReset()")
        try:
            if scene.get("type"):
                sel, text = scene["type"]
                page.click(sel)
                page.type(sel, text, delay=30)
            elif scene.get("click_canvas"):
                # cytoscape 画在 canvas 上，点不到"元素"——问它一个节点的屏幕坐标，
                # 然后用**真实鼠标**点那个点。这是唯一能拿到可信事件的办法。
                pt = page.evaluate("""() => {
                    const n = cy.nodes().filter(x => !x.isParent())[0];
                    const p = n.renderedPosition();
                    const b = document.getElementById('cy').getBoundingClientRect();
                    return { x: b.left + p.x, y: b.top + p.y };
                }""")
                page.mouse.click(pt["x"], pt["y"])
            elif scene.get("click_gb_canvas"):
                pt = page.evaluate("""() => {
                    const n = gbCy.nodes()[0];
                    const p = n.renderedPosition();
                    const b = document.getElementById('gb-cy').getBoundingClientRect();
                    return { x: b.left + p.x, y: b.top + p.y };
                }""")
                page.mouse.click(pt["x"], pt["y"])
            else:
                page.click(scene["click"], timeout=5000)
        except Exception as exc:  # noqa: BLE001
            note = f"交互做不成：{exc}"
            break
        page.wait_for_timeout(500)   # 等 observer 把 entry 交付上来
        got = page.evaluate("window.__inp")
        if got.get("unsupported"):
            note = "这个浏览器不支持 event timing：" + got["unsupported"]
            break
        evs = got.get("events") or []
        if not evs:
            # **"没量到"不等于"很快"**（R40 那条纪律）：如实记，不写 0。
            samples.append({"n_events": 0})
            continue
        worst = max(evs, key=lambda e: e["duration"])
        samples.append({
            "n_events": len(evs),
            "inp": round(worst["duration"], 1),
            "input_delay": round(worst["input_delay"], 1),
            "processing": round(worst["processing"], 1),
            "presentation": round(worst["presentation"], 1),
            "long_tasks": len(got.get("longTasks") or []),
            "long_task_ms": round(sum(t["ms"] for t in (got.get("longTasks") or [])), 1),
        })
    page.close()

    got = [s for s in samples if s.get("inp") is not None]
    out = {"scene": name, "what": scene["what"], "repeats": len(samples),
           "n_measured": len(got), "errors": errors[:3], "note": note,
           "budget_ms": INP_TIGHT_BUDGET_MS if scene.get("tight") else INP_BUDGET_MS}
    if got:
        out.update({
            "inp_median_ms": round(_med([s["inp"] for s in got]), 1),
            "inp_max_ms": round(max(s["inp"] for s in got), 1),
            "input_delay_median_ms": round(_med([s["input_delay"] for s in got]), 1),
            "processing_median_ms": round(_med([s["processing"] for s in got]), 1),
            "presentation_median_ms": round(_med([s["presentation"] for s in got]), 1),
            "long_tasks_median": round(_med([s["long_tasks"] for s in got]), 1),
        })
    else:
        # 一次都没量到：观察器的 durationThreshold 是 16 ms，低于它的交互不产出
        # entry——**那是"快到量不出"，不是"失败"**，两者要分开说。
        out["reason"] = out.get("note") or "全部交互都快于 16 ms 的观察阈值（量不到 ≠ 慢）"
    return out


def run(only: str | None) -> dict:
    from playwright.sync_api import sync_playwright

    names = [only] if only else list(SCENES)
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    rows = []
    try:
        if not _wait_ready(f"{base}/health", time.monotonic() + 90):
            raise SystemExit("服务没起来")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            for name in names:
                rows.append(_run_scene(browser, base, name))
                r = rows[-1]
                flag = ""
                if r.get("inp_median_ms") is not None:
                    flag = "  ✗ 超预算" if r["inp_median_ms"] > r["budget_ms"] else "  ✓"
                print(f"{name:22s} "
                      + (f"INP 中位 {r.get('inp_median_ms')} ms "
                         f"（延迟 {r.get('input_delay_median_ms')} / "
                         f"处理 {r.get('processing_median_ms')} / "
                         f"呈现 {r.get('presentation_median_ms')}）"
                         f" 上限 {r['budget_ms']}{flag}"
                         if r.get("inp_median_ms") is not None
                         else f"—— {r.get('reason')}"), flush=True)
            browser.close()
    finally:
        server.terminate()
    return {"budget_ms": INP_BUDGET_MS, "tight_budget_ms": INP_TIGHT_BUDGET_MS,
            "repeats": REPEATS, "scenes": rows}


def compare(old: dict, new: dict) -> str:
    by_old = {r["scene"]: r for r in old.get("scenes", [])}
    lines = ["| 交互 | 改前 INP | 改后 INP | 变化 | 上限 |",
             "|---|---|---|---|---|"]
    for r in new.get("scenes", []):
        o = by_old.get(r["scene"], {})
        a = o.get("inp_median_ms")
        b = r.get("inp_median_ms")
        if a is None or b is None:
            delta = "—"
        else:
            delta = f"{(b - a) / a * 100:+.1f}%" if a else "—"
        lines.append(f"| {r['scene']} | {a if a is not None else '量不到'} | "
                     f"{b if b is not None else '量不到'} | {delta} | {r['budget_ms']} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=list(SCENES))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--compare", type=Path, help="跟这份旧结果比")
    args = ap.parse_args(argv)
    data = run(args.only)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ {args.out}")
    if args.compare and args.compare.exists():
        print("\n" + compare(json.loads(args.compare.read_text(encoding="utf-8")), data))
    over = [r for r in data["scenes"]
            if r.get("inp_median_ms") is not None and r["inp_median_ms"] > r["budget_ms"]]
    if over:
        print(f"\n超预算 {len(over)} 项：" + "、".join(r["scene"] for r in over))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
