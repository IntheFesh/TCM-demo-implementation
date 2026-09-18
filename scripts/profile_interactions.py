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

#: "点到结果出现"的上限。**这是跟 INP 并列的第二个数，不是替代**。
#:
#: R43 的基线跑完发现一件事：全部 12 个交互的 INP `processing` 段都是 ~0 ms，
#: 因为这个应用的点击处理函数**都是立刻返回的**——真正的工作（fetch、合并、
#: 重排）发生在 await 之后，落在 INP 的观测窗口之外。
#: 也就是说**光看 INP 会得出"这个站一点延迟都没有"的结论，而用户明明在等**。
#:
#: 所以再量一个"从点下去到结果真的出现在屏幕上"（settle）。两个数各自回答：
#:   INP    —— 点下去有没有立刻响应（界面卡不卡）
#:   settle —— 这件事总共等了多久（快不快）
#: 两个都要，缺一个就会把"卡"和"慢"混成一件事。
#:
#: 1000 ms 这个上限的来处：Nielsen 的经典阈值——**1 秒是"思路不被打断"的界**
#: （0.1 秒=瞬时、1 秒=不打断思路、10 秒=注意力流失）。不是自己拍的数。
SETTLE_BUDGET_MS = 1000

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
    // "点到结果出现"的起点。**用 pointerdown 而不是 click**：click 在鼠标抬起
  // 之后才派发，中间那 ~50 ms 是 Playwright 的按下-抬起间隔，不该算进等待。
  window.__clickAt = null;
  document.addEventListener("pointerdown", () => { window.__clickAt = performance.now(); },
                            { capture: true });
  window.__inpReset = () => {
    window.__inp.events = []; window.__inp.longTasks = []; window.__clickAt = null;
  };
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
        # 切到图谱页 = 拉全量证素 + 建索引 + 首次布局。**这是全站最重的一次点击。**
        # **只量第一次（冷态）**：第二次起数据已经在内存里，`gbCy.nodes()` 一开始
        # 就非空，判据立刻成立——量出来是 9 ms，而那 9 ms 什么都没等。
        # 报一个"什么都没等"的数比不报更糟。
        "settle": "() => gbCy && gbCy.nodes().length > 0",
        "settle_first_only": True,
    },
    "example_pick": {
        "what": "点首屏的示例主诉（填进输入框）",
        "setup": "",
        "click": "#examples .example",
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
        "click": "#chain-flow .chain-sec",
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
        "settle": "() => document.getElementById('graph-tooltip').classList.contains('show')",
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
        "click": ".cs-wrap button",
        "tight": True,
    },
    "browser_expand": {
        "what": "图谱浏览器：展开一个证素（加节点 + 重排）",
        "setup": ("switchTab('graph-browser'); await loadGraphBrowserData();"
                  " await new Promise(r => setTimeout(r, 600));"),
        "click_gb_canvas": True,
        "tight": False,
    },
    # ---- 规模场景：**真实规模才找得到延迟**（本沙盒持久图 2356 节点 / 3682 边）----
    "browser_search_broad": {
        "what": "图谱浏览器：搜一个宽词（「痛」命中上百条，走服务端 + 合并 + 重排）",
        "setup": ("switchTab('graph-browser'); await loadGraphBrowserData();"
                  " await new Promise(r => setTimeout(r, 600));"
                  " document.getElementById('gb-search').value = '痛';"),
        "click": "#gb-search-btn",
        "tight": False,
        # 状态栏每次先清空再等它填上——不清的话上一轮留下的字会让判据立刻成立。
        "reset": "document.getElementById('gb-search-status').textContent = '';",
        "settle": ("() => (document.getElementById('gb-search-status').textContent || '')"
                   ".includes('找到')"),
    },
    "browser_expand_repeat": {
        "what": ("图谱浏览器：连展开 5 个证素之后再展一个"
                 "——**合并与索引的累积成本在这里才看得出来**"),
        "setup": ("switchTab('graph-browser'); await loadGraphBrowserData();"
                  " await new Promise(r => setTimeout(r, 600));"
                  " const ids = gbCy.nodes().map(n => n.id()).slice(0, 5);"
                  " for (const id of ids) { await gbExpandNode(id);"
                  "   await new Promise(r => setTimeout(r, 150)); }"),
        "click_gb_canvas": True,
        "tight": False,
        "settle": "() => gbCy.nodes('[node_type = \"syndrome\"]').length > 0",
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
                #
                # **先把画布滚进视口**：图在折叠区里、位置很靠下，
                # `getBoundingClientRect()` 给的是视口坐标，画布在折叠区里时
                # y 会超出视口高度，`mouse.click` 点到的是页面外——表现是
                # "交互做了但什么都没发生"，而那跟"功能坏了"长得一模一样。
                pt = page.evaluate("""() => {
                    const el = document.getElementById('cy');
                    el.scrollIntoView({ block: 'center' });
                    const n = cy.nodes().filter(x => !x.isParent())[0];
                    const p = n.renderedPosition();
                    const b = el.getBoundingClientRect();
                    return { x: b.left + p.x, y: b.top + p.y,
                             ok: b.top >= 0 && b.bottom <= window.innerHeight };
                }""")
                if not pt.get("ok"):
                    raise RuntimeError(f"画布滚不进视口：{pt}")
                page.mouse.click(pt["x"], pt["y"])
            elif scene.get("click_gb_canvas"):
                pt = page.evaluate("""() => {
                    const el = document.getElementById('gb-cy');
                    el.scrollIntoView({ block: 'center' });
                    const n = gbCy.nodes()[0];
                    const p = n.renderedPosition();
                    const b = el.getBoundingClientRect();
                    return { x: b.left + p.x, y: b.top + p.y };
                }""")
                page.mouse.click(pt["x"], pt["y"])
            else:
                page.click(scene["click"], timeout=5000)
        except Exception as exc:  # noqa: BLE001
            note = f"交互做不成：{exc}"
            break
        settle_ms = None
        if scene.get("settle") and not (scene.get("settle_first_only") and i):
            # **从点下去开始算**，不是从这一行开始算：上面那几句 Playwright 调用
            # 本身有几毫秒的 IPC，算进去会让这个数虚高。所以用页面里的时钟：
            # `__clickAt` 由 `add_init_script` 里的 pointerdown 监听打点。
            try:
                page.wait_for_function(scene["settle"], timeout=8000)
                settle_ms = page.evaluate(
                    "() => window.__clickAt ? Math.round(performance.now() - window.__clickAt) : null")
            except Exception as exc:  # noqa: BLE001
                note = f"等不到结果：{str(exc)[:80]}"
        page.wait_for_timeout(500)   # 等 observer 把 entry 交付上来
        got = page.evaluate("window.__inp")
        if got.get("unsupported"):
            note = "这个浏览器不支持 event timing：" + got["unsupported"]
            break
        evs = got.get("events") or []
        if not evs:
            # **"没量到"不等于"很快"**（R40 那条纪律）：如实记，不写 0。
            samples.append({"n_events": 0, "settle": settle_ms})
            continue
        worst = max(evs, key=lambda e: e["duration"])
        samples.append({
            "n_events": len(evs),
            "settle": settle_ms,
            "inp": round(worst["duration"], 1),
            "input_delay": round(worst["input_delay"], 1),
            "processing": round(worst["processing"], 1),
            "presentation": round(worst["presentation"], 1),
            "long_tasks": len(got.get("longTasks") or []),
            "long_task_ms": round(sum(t["ms"] for t in (got.get("longTasks") or [])), 1),
        })
    page.close()

    got = [s for s in samples if s.get("inp") is not None]
    settles = [s["settle"] for s in samples if s.get("settle") is not None]
    out = {"scene": name, "what": scene["what"], "repeats": len(samples),
           "n_measured": len(got), "errors": errors[:3], "note": note,
           "budget_ms": INP_TIGHT_BUDGET_MS if scene.get("tight") else INP_BUDGET_MS}
    if settles:
        out["settle_median_ms"] = round(_med(settles), 1)
        out["settle_max_ms"] = round(max(settles), 1)
        out["settle_budget_ms"] = SETTLE_BUDGET_MS
        out["settle_n"] = len(settles)
        out["settle_cold_only"] = bool(scene.get("settle_first_only"))
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
                settle = (f"  ｜ 点到出结果 {r['settle_median_ms']} ms"
                          f"（上限 {r['settle_budget_ms']}）"
                          + ("  ✗" if r["settle_median_ms"] > r["settle_budget_ms"] else "")
                          if r.get("settle_median_ms") is not None else "")
                print(f"{name:22s} "
                      + (f"INP 中位 {r.get('inp_median_ms')} ms "
                         f"（延迟 {r.get('input_delay_median_ms')} / "
                         f"处理 {r.get('processing_median_ms')} / "
                         f"呈现 {r.get('presentation_median_ms')}）"
                         f" 上限 {r['budget_ms']}{flag}"
                         if r.get("inp_median_ms") is not None
                         else f"—— {r.get('reason')}") + settle, flush=True)
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
