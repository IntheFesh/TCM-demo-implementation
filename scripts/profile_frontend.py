"""R41：前端运行时性能的真浏览器量具。**先测量，后优化。**

    python -m scripts.profile_frontend
    python -m scripts.profile_frontend --only first_paint --repeat 3
    python -m scripts.profile_frontend --compare eval/frontend/R41_baseline.json

## 量哪些数，每个回答什么问题

| 指标 | 怎么来的 | 回答什么 |
|---|---|---|
| `fcp_ms` | `paint` entry `first-contentful-paint` | 屏幕上第一次出现东西要多久 |
| `lcp_ms` | `PerformanceObserver('largest-contentful-paint')` | **主要内容**什么时候到位（人感知的"加载完"） |
| `tbt_ms` | Σ(长任务 − 50ms) | 加载期间主线程被堵了多久（点不动的那段时间） |
| `cls` | `PerformanceObserver('layout-shift')`，排除有用户交互的 | 版面跳不跳（字体、图片、异步插入的块） |
| `inp_ms` | 真点一下，量到**下一帧渲染完** | 点下去到看见变化 |
| `long_tasks` | `PerformanceObserver('longtask')` | 具体哪几段把主线程占了 >50ms |
| `graph_fps` | 连续 rAF 间隔 | 图谱拖动/布局时掉不掉帧 |
| `heap_mb` | `performance.memory.usedJSHeapSize` | 这一页吃多少 JS 堆 |
| `network` | `resource` entries | 请求几个、多少字节、有几个是渲染阻塞的 |

## 三条口径上的讲究

1. **CLS 要排除用户交互引起的位移**（`hadRecentInput`）。点开一个折叠块当然会
   把下面的内容推下去，那不是"版面跳"。不排除的话这个数恒大，等于没有指标。
2. **TBT 不是"长任务总时长"**，是每个长任务超出 50 ms 的部分之和。一个 60 ms
   的任务贡献 10 ms，不是 60 ms——否则一堆刚过线的任务会把这个数吹起来。
3. **INP 量到"下一帧渲染完"，不是"事件回调返回"。** 回调里改完 DOM 就返回，
   人还没看见任何变化；两者能差一整帧（16.7 ms）到几百毫秒。

## 不量什么

真实 LLM 下的问诊时长（那是 R40 的事）。这里所有场景都用**构造好的响应体**
直接喂页面的渲染函数——测的仍然是上线那份 `app.js` + `app.css` + 真浏览器排版，
只是不花钱去要一份形状早就确定的 JSON（跟 `scripts/screenshot_states.py`
同一条理由）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "eval" / "frontend"

#: 长任务的定义（W3C Long Tasks API 的门槛）。**不是可调参数**——改了这个数
#: 报出来的 TBT 就跟任何外部基准都不可比了。
LONG_TASK_MS = 50

#: 视口。跟 `scripts/screenshot_states.py` 的默认值一致，两处量的是同一块版面。
VIEWPORT = {"width": 1440, "height": 900}

#: 注进页面的采集器。**必须在任何页面脚本之前跑**（`add_init_script`）——
#: LCP / CLS / longtask 都是"从现在开始观察"，晚一步就漏掉最早的那几条，
#: 而最早的那几条恰好是加载期最重要的。
COLLECTOR_JS = r"""
(() => {
  const S = {
    lcp: 0, cls: 0, longTasks: [], clsEntries: 0, clsIgnored: 0,
    observersFailed: [],
  };
  window.__perf = S;
  const obs = (type, fn, extra) => {
    try {
      const o = new PerformanceObserver((list) => list.getEntries().forEach(fn));
      o.observe(Object.assign({ type, buffered: true }, extra || {}));
    } catch (e) {
      // 观察器建不起来要**记下来**：静默失败会让那个指标报成 0，
      // 而 0 会被读成"非常好"。
      S.observersFailed.push(type + ": " + e.message);
    }
  };
  obs("largest-contentful-paint", (e) => { S.lcp = Math.max(S.lcp, e.startTime + e.duration); });
  S.shifts = [];
  obs("layout-shift", (e) => {
    // **排除用户交互引起的位移**：点开折叠块把下面推下去不是"版面跳"。
    if (e.hadRecentInput) { S.clsIgnored += 1; return; }
    S.cls += e.value; S.clsEntries += 1;
    // **把肇事元素记下来**：一个 CLS 数字没法指导修改，"哪个元素把什么推下去了"
    // 才能。`sources` 是 LayoutShiftAttribution，Chromium 有，标准里是可选的。
    const who = (e.sources || []).map((src) => {
      const n = src.node;
      if (!n) return "(节点已不在)";
      const id = n.id ? "#" + n.id : "";
      const cls = n.className && typeof n.className === "string"
        ? "." + n.className.trim().split(/\s+/).slice(0, 2).join(".") : "";
      return (n.tagName || "?").toLowerCase() + id + cls;
    });
    S.shifts.push({ value: Math.round(e.value * 10000) / 10000,
                    at: Math.round(e.startTime), who: who.slice(0, 3) });
  });
  obs("longtask", (e) => {
    S.longTasks.push({ start: Math.round(e.startTime), ms: Math.round(e.duration),
                       name: e.name, attribution: (e.attribution || [])
                         .map((a) => a.name + ":" + a.containerType).slice(0, 2) });
  });
})();
"""

#: 读数用的小工具函数，跑在页面里。`measureInteraction` 是 INP 的口径所在。
PROBE_JS = r"""
window.__probe = {
  paint(name) {
    const e = performance.getEntriesByType("paint").find((x) => x.name === name);
    return e ? Math.round(e.startTime) : null;
  },
  nav() {
    const n = performance.getEntriesByType("navigation")[0];
    if (!n) return null;
    return {
      dom_content_loaded_ms: Math.round(n.domContentLoadedEventEnd),
      load_ms: Math.round(n.loadEventEnd),
      transfer_bytes: n.transferSize || 0,
    };
  },
  resources() {
    const rs = performance.getEntriesByType("resource");
    let bytes = 0, blocking = 0;
    const slow = [];
    const all = [];
    for (const r of rs) {
      bytes += r.transferSize || 0;
      all.push({ url: r.name.split("/").pop(), kind: r.initiatorType,
                 bytes: r.transferSize || 0, decoded: r.decodedBodySize || 0,
                 ms: Math.round(r.duration),
                 blocking: r.renderBlockingStatus || null });
      if (r.renderBlockingStatus === "blocking") blocking += 1;
      if (r.duration > 100) slow.push({ url: r.name.split("/").pop(),
                                        ms: Math.round(r.duration),
                                        bytes: r.transferSize || 0,
                                        blocking: r.renderBlockingStatus || null });
    }
    slow.sort((a, b) => b.ms - a.ms);
    all.sort((a, b) => b.decoded - a.decoded);
    return { n: rs.length, bytes, blocking, slow: slow.slice(0, 8), all };
  },
  tbt() {
    // TBT = Σ(长任务 − 50ms)。不是"长任务总时长"。
    return Math.round(window.__perf.longTasks
      .reduce((a, t) => a + Math.max(0, t.ms - 50), 0));
  },
  dom() {
    const all = document.querySelectorAll("*");
    // **最长的那个列表**：虚拟滚动该不该做由这个数决定，不由感觉决定。
    let worst = { selector: null, n: 0 };
    for (const el of document.querySelectorAll("div,ul,ol,tbody,section")) {
      const n = el.children.length;
      if (n > worst.n) {
        worst = { n, selector: (el.tagName || "").toLowerCase()
          + (el.id ? "#" + el.id : "")
          + (el.className && typeof el.className === "string"
             ? "." + el.className.trim().split(/\s+/)[0] : "") };
      }
    }
    return { n_nodes: all.length, longest_list: worst,
             depth_max: (() => {
               let d = 0;
               for (const el of all) {
                 let k = 0, p = el;
                 while (p.parentElement) { k += 1; p = p.parentElement; }
                 if (k > d) d = k;
               }
               return d;
             })() };
  },
  layout() {
    // R41：布局算在哪条线程上、花了多久。`window.__graphPerf` 见 graph.js 末尾。
    return (window.__graphPerf && window.__graphPerf.layoutStats) || null;
  },
  heapMb() {
    const m = performance.memory;
    return m ? Math.round((m.usedJSHeapSize / 1048576) * 10) / 10 : null;
  },
  // **量到"下一帧渲染完"**，不是"回调返回"。两者能差一整帧到几百毫秒。
  measureInteraction(selector) {
    return new Promise((resolve) => {
      const el = document.querySelector(selector);
      if (!el) { resolve({ error: "找不到 " + selector }); return; }
      const t0 = performance.now();
      el.click();
      requestAnimationFrame(() => requestAnimationFrame(() => {
        resolve({ ms: Math.round((performance.now() - t0) * 10) / 10 });
      }));
    });
  },
  // 连续 rAF 间隔 → 帧率。图谱那一页的判据。
  frames(ms) {
    return new Promise((resolve) => {
      const gaps = [];
      let last = performance.now();
      const end = last + ms;
      const step = (now) => {
        gaps.push(now - last); last = now;
        if (now < end) requestAnimationFrame(step);
        else {
          gaps.sort((a, b) => a - b);
          const mid = gaps[Math.floor(gaps.length / 2)] || 0;
          resolve({ n_frames: gaps.length,
                    median_gap_ms: Math.round(mid * 10) / 10,
                    worst_gap_ms: Math.round((gaps[gaps.length - 1] || 0) * 10) / 10,
                    fps: mid > 0 ? Math.round(1000 / mid) : null });
        }
      };
      requestAnimationFrame(step);
    });
  },
};
"""


def _fixtures() -> dict:
    """场景用的响应体。**复用 `scripts/screenshot_states.py` 里那几份**
    ——同一套构造好的 JSON 只能有一处（CLAUDE.md 第 31 条）。那边已经为
    截图验收造了合法的 `S3Structured` 载荷，这里再造一份必然漂。"""
    from scripts import screenshot_states as ss

    return {
        "R37_DONE_PAYLOAD": ss.R37_DONE_PAYLOAD,
        "DONE_PAYLOAD": ss.DONE_PAYLOAD,
        "SIX_LAYER_GRAPH": ss.SIX_LAYER_GRAPH,
        "COMPLAINT": ss.COMPLAINT,
    }


#: 场景。`setup` 跑在页面里（可以 await），`probe` 返回这个场景独有的数。
SCENES: dict[str, dict] = {
    "first_paint": {
        "what": "首屏（只加载页面，不跑任何问诊）",
        "setup": "",
        "interactions": [],
    },
    "chain_done": {
        "what": ("单链九段终态——**直接渲染、没有点击**，所以 CLS 是上界"
                 "（刷新恢复会话那种情况）；真实用户路径看 chain_done_via_click"),
        "setup": ("renderComplaintBody(window.COMPLAINT);"
                  " SERVER_S3_MODE = 'structured';"
                  " renderConsultResult(window.R37_DONE_PAYLOAD);"),
        # 页面里真有的那几个可点元素。**选择器写错的表现是 interactions 为空**，
        # 而空会被读成"没有交互要量"——所以有一条测试钉住每个场景的选择器
        # 在对应的渲染产物里真的存在。
        "interactions": [
            ("展开参考医案", "details.col-refs > summary"),
            ("切换角色下拉", "#role-select"),
            ("点第九段节点", ".chain-sec[data-key='formula'] h3"),
        ],
    },
    "chain_done_via_click": {
        "what": "真实用户路径：点「辨证」→ running → 终态（CLS 的正确口径）",
        # **为什么要单独一个场景**：CLS 排除"用户交互后 500 ms 内"的位移。
        # 真实路径是"点按钮 → 立刻进 running 态（版面大改，在 500 ms 内、被排除）
        # → 几十秒后结果填进已经留好的位置"。上面那个 chain_done 直接从首屏跳到
        # 终态、没有点击，那一大跳整个计进 CLS——量到的是**上界**（刷新页面恢复
        # 会话那种情况），不是常态。两个数都要，且不许混成一个。
        # **必须是 Playwright 真点一下**，不能用 `el.click()`。
        # CLS 排除"用户输入后 500 ms 内"的位移（`hadRecentInput`），而 Chromium
        # 只认**可信事件**——脚本里 `el.click()` 造出来的是不可信事件，不开窗口。
        # 用它量的话那一大跳照样计进 CLS，得到的还是 chain_done 那个上界，
        # 这个场景就白设了。所以这里分三步：
        #   setup_pre  → 把 fetch 挡掉（不发真请求）+ 填好输入框
        #   click      → Playwright 的真实鼠标点击（可信输入，开 500 ms 窗口）
        #   setup_post → 等过 500 ms 再填结果（真实世界里结果是几十秒后到的，
        #                那一跳**是**要算进 CLS 的，不许把它也排除掉）
        "setup_pre": ("window.fetch = () => new Promise(() => {});"
                      " document.getElementById('complaint').value = window.COMPLAINT;"),
        "click": "#submit-btn",
        "setup_post": ("await new Promise((r) => setTimeout(r, 700));"
                       " SERVER_S3_MODE = 'structured';"
                       " renderConsultResult(window.R37_DONE_PAYLOAD);"),
        "setup": "",
        "interactions": [],
    },
    "graph_tab": {
        "what": ("问诊图（六层 + compound）。CLS 同 chain_done 是上界口径"
                 "（这个场景量的是布局线程与帧率，不是 CLS）"),
        "setup": ("renderComplaintBody(window.COMPLAINT);"
                  " renderConsultResult(window.DONE_PAYLOAD);"
                  " await growGraph(window.SIX_LAYER_GRAPH);"),
        "interactions": [],
        "frames": True,
    },
    "repeat_visit": {
        "what": "二次访问（走缓存）——字体与 vendor 的 1.13 MB 应该一个字节都不取",
        "setup": "",
        "interactions": [],
        # 先加载一遍把缓存填上，再重新加载一次量第二次。**同一个 page 上 reload**，
        # 不是新开 page：新 page 共享 browser 的 HTTP 缓存，但 `performance`
        # 条目会重置——两者都要，所以用 reload。
        "reload": True,
    },
    "graph_browser": {
        "what": "图谱浏览器页签（持久知识图谱，分页拉证素）",
        "setup": "switchTab('graph-browser'); await loadGraphBrowserData();",
        "interactions": [("重置视图", "#gb-reset-btn")],
        "frames": True,
    },
}


def _wait_ready(url: str, deadline: float) -> bool:
    import httpx

    while time.monotonic() < deadline:
        try:
            # 就绪闸门：R40 起 /health 在预热完成前回 503。前端量的是**就绪之后**
            # 的页面，预热期间的首屏会把检索器的 IO 算进网络时间。
            if httpx.get(url, timeout=2.0).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)
    return False


def _chromium_path() -> str | None:
    """跟截图脚本同一套找浏览器的办法（一处实现，在 `scripts/screenshot_ui.py`）。"""
    from scripts.screenshot_ui import _chromium_path as find

    return find()


def profile_scene(browser, base_url: str, name: str, *, wait_ms: int) -> dict:
    scene = SCENES[name]
    page = browser.new_page(viewport=VIEWPORT)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.add_init_script(COLLECTOR_JS)
    t0 = time.perf_counter()
    page.goto(f"{base_url}/app/index.html", wait_until="load")
    goto_ms = (time.perf_counter() - t0) * 1000
    page.evaluate(PROBE_JS)
    for var, value in _fixtures().items():
        page.evaluate(f"window.{var} = {json.dumps(value, ensure_ascii=False)};")
    if scene.get("reload"):
        # 第二次加载：HTTP 缓存已经被第一次填上了，`performance` 条目重置。
        # 量的是"回头客要下多少字节"——有了 R41 的 Cache-Control 之后这个数
        # 才有意义（之前浏览器按启发式自己猜新鲜期，猜多久取决于版本）。
        page.reload(wait_until="load")
        page.evaluate(PROBE_JS)
    if scene.get("setup_pre"):
        page.evaluate(f"(async () => {{ {scene['setup_pre']} }})()")
    if scene.get("click"):
        # 真实鼠标点击 → 可信事件 → CLS 的 500 ms 排除窗口才会打开。
        page.click(scene["click"])
    if scene["setup"]:
        page.evaluate(f"(async () => {{ {scene['setup']} }})()")
    if scene.get("setup_post"):
        page.evaluate(f"(async () => {{ {scene['setup_post']} }})()")
    page.wait_for_timeout(wait_ms)

    out: dict = {
        "scene": name,
        "what": scene["what"],
        "goto_ms": round(goto_ms, 1),
        "fcp_ms": page.evaluate("window.__probe.paint('first-contentful-paint')"),
        "fp_ms": page.evaluate("window.__probe.paint('first-paint')"),
        "lcp_ms": round(page.evaluate("window.__perf.lcp") or 0, 1),
        "cls": round(page.evaluate("window.__perf.cls") or 0, 4),
        "cls_entries": page.evaluate("window.__perf.clsEntries"),
        "cls_ignored_user_input": page.evaluate("window.__perf.clsIgnored"),
        # 逐次位移 + 肇事元素。**一个 CLS 数字没法指导修改。**
        "shifts": sorted(page.evaluate("window.__perf.shifts") or [],
                         key=lambda x: -x["value"])[:8],
        "tbt_ms": page.evaluate("window.__probe.tbt()"),
        "long_tasks": page.evaluate("window.__perf.longTasks"),
        "heap_mb": page.evaluate("window.__probe.heapMb()"),
        "dom": page.evaluate("window.__probe.dom()"),
        "layout": page.evaluate("window.__probe.layout()"),
        "navigation": page.evaluate("window.__probe.nav()"),
        "network": page.evaluate("window.__probe.resources()"),
        "observers_failed": page.evaluate("window.__perf.observersFailed"),
        "page_errors": errors,
    }
    out["n_long_tasks"] = len(out["long_tasks"])
    out["worst_long_task_ms"] = max((t["ms"] for t in out["long_tasks"]), default=0)

    interactions = []
    for label, selector in scene["interactions"]:
        res = page.evaluate(f"window.__probe.measureInteraction({json.dumps(selector)})")
        interactions.append({"label": label, "selector": selector, **res})
    out["interactions"] = interactions

    if scene.get("frames"):
        out["frames"] = page.evaluate("window.__probe.frames(1500)")

    page.close()
    return out


def median_of(runs: list[dict]) -> dict:
    """逐场景取**中位数**。不取均值：一次 GC 能把均值拽走。"""
    by_scene: dict[str, list[dict]] = {}
    for r in runs:
        by_scene.setdefault(r["scene"], []).append(r)
    out = {}
    for scene, rows in by_scene.items():
        agg: dict = {"scene": scene, "what": rows[0]["what"], "n_runs": len(rows)}
        for key in ("fcp_ms", "lcp_ms", "tbt_ms", "cls", "heap_mb",
                    "n_long_tasks", "worst_long_task_ms", "goto_ms"):
            vals = [r[key] for r in rows if r.get(key) is not None]
            agg[key] = round(statistics.median(vals), 3) if vals else None
        doms = [r["dom"] for r in rows if r.get("dom")]
        if doms:
            agg["dom"] = {"n_nodes": int(statistics.median(x["n_nodes"] for x in doms)),
                          "longest_list_n": int(statistics.median(
                              x["longest_list"]["n"] for x in doms)),
                          "longest_list": doms[-1]["longest_list"]["selector"],
                          "depth_max": int(statistics.median(x["depth_max"] for x in doms))}
        lays = [r["layout"] for r in rows if r.get("layout") and r["layout"].get("where")]
        if lays:
            agg["layout"] = {"where": lays[-1]["where"],
                             "ms": round(statistics.median(x["ms"] for x in lays), 2),
                             "n_nodes": lays[-1]["n_nodes"],
                             "fallback_reason": lays[-1]["fallback_reason"]}
        net = [r["network"] for r in rows if r.get("network")]
        if net:
            agg["network"] = {
                "n": int(statistics.median(x["n"] for x in net)),
                "bytes": int(statistics.median(x["bytes"] for x in net)),
                "blocking": int(statistics.median(x["blocking"] for x in net)),
            }
        inter = [i for r in rows for i in r.get("interactions", []) if i.get("ms") is not None]
        if inter:
            by_label: dict[str, list[float]] = {}
            for i in inter:
                by_label.setdefault(i["label"], []).append(i["ms"])
            agg["interactions"] = [{"label": k, "ms": round(statistics.median(v), 1)}
                                   for k, v in by_label.items()]
        frames = [r["frames"] for r in rows if r.get("frames")]
        if frames:
            agg["frames"] = {"fps": int(statistics.median(f["fps"] or 0 for f in frames)),
                             "worst_gap_ms": round(statistics.median(
                                 f["worst_gap_ms"] for f in frames), 1)}
        out[scene] = agg
    return out


def compare(old: dict, new: dict) -> list[dict]:
    """逐场景逐指标 diff。**只在一边有的场景如实标出来**（场景表变了和真的
    变快了是两件事）。"""
    o, n = old.get("scenes", {}), new.get("scenes", {})
    rows = []
    for scene in sorted(set(o) | set(n)):
        a, b = o.get(scene), n.get(scene)
        for key in ("fcp_ms", "lcp_ms", "tbt_ms", "cls", "n_long_tasks",
                    "worst_long_task_ms", "heap_mb"):
            av = (a or {}).get(key)
            bv = (b or {}).get(key)
            row = {"scene": scene, "metric": key, "old": av, "new": bv}
            if av not in (None, 0) and bv is not None:
                row["delta"] = round(bv - av, 3)
                row["delta_pct"] = round((bv - av) / av * 100, 1)
            else:
                row["delta"] = row["delta_pct"] = None
                row["note"] = ("只在旧的里有" if a and not b
                               else "这次新增的" if b and not a else "旧值为 0 或缺")
            rows.append(row)
        for key, path in (("net_bytes", "bytes"), ("net_n", "n"), ("net_blocking", "blocking")):
            av = ((a or {}).get("network") or {}).get(path)
            bv = ((b or {}).get("network") or {}).get(path)
            row = {"scene": scene, "metric": key, "old": av, "new": bv}
            if av not in (None, 0) and bv is not None:
                row["delta"] = bv - av
                row["delta_pct"] = round((bv - av) / av * 100, 1)
            else:
                row["delta"] = row["delta_pct"] = None
            rows.append(row)
    return rows


def _cell(value) -> str:
    """表格里一格。**None 显示成「—」而不是 0**：0 会被读成"非常好"。"""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def compare_text(rows: list[dict]) -> str:
    head = f"{'场景':<14s}{'指标':<20s}{'旧':>12s}{'新':>12s}{'差':>12s}{'差%':>9s}"
    lines = [head, "-" * len(head)]
    for r in rows:
        pct = "—" if r["delta_pct"] is None else f"{r['delta_pct']:+.1f}%"
        lines.append(f"{r['scene']:<14s}{r['metric']:<20s}"
                     f"{_cell(r['old']):>12s}{_cell(r['new']):>12s}"
                     f"{_cell(r['delta']):>12s}{pct:>9s}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=list(SCENES), help="只跑一个场景")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--wait-ms", type=int, default=1200,
                    help="setup 之后等多久再读数（让异步渲染与观察器落定）")
    ap.add_argument("--out", default=None)
    ap.add_argument("--compare", default=None, metavar="旧.json",
                    help="跟一份旧 profile 逐指标比（零浏览器，只读两个文件）")
    args = ap.parse_args(argv)

    if args.compare and not args.out:
        latest = sorted(OUT_DIR.glob("frontend_*.json"))
        if not latest:
            print(f"{OUT_DIR} 下没有 profile，先跑一次", file=sys.stderr)
            return 2
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        new = json.loads(latest[-1].read_text(encoding="utf-8"))
        print(compare_text(compare(old, new)))
        print(f"\n（新 = {latest[-1].name}）")
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（浏览器本机已有，"
              "不要跑 playwright install）", file=sys.stderr)
        return 2

    from scripts.live_server import live_server

    names = [args.only] if args.only else list(SCENES)
    runs: list[dict] = []
    with live_server() as base_url:
        if not _wait_ready(f"{base_url}/health", time.monotonic() + 120):
            print("服务没就绪（/health 一直不是 200）", file=sys.stderr)
            return 1
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            try:
                for i in range(max(1, args.repeat)):
                    for name in names:
                        print(f"— 第 {i + 1}/{args.repeat} 次 · {name}")
                        row = profile_scene(browser, base_url, name, wait_ms=args.wait_ms)
                        runs.append(row)
                        print(f"  FCP {row['fcp_ms']} / LCP {row['lcp_ms']} / "
                              f"TBT {row['tbt_ms']} / CLS {row['cls']} / "
                              f"长任务 {row['n_long_tasks']}（最长 "
                              f"{row['worst_long_task_ms']} ms）/ 堆 {row['heap_mb']} MB")
                        if row["page_errors"]:
                            print(f"  ✗ 页面里有 JS 错误：{row['page_errors']}",
                                  file=sys.stderr)
                        if row["observers_failed"]:
                            print(f"  ⚠ 观察器没建起来：{row['observers_failed']}",
                                  file=sys.stderr)
            finally:
                browser.close()

    scenes = median_of(runs)
    report = {
        "kind": "frontend_profile",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "viewport": VIEWPORT,
        "long_task_threshold_ms": LONG_TASK_MS,
        "repeat": args.repeat,
        "runs": runs,
        "scenes": scenes,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else OUT_DIR / f"frontend_{int(time.time())}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")

    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        print()
        print(compare_text(compare(old, report)))
    bad = [r for r in runs if r["page_errors"]]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
