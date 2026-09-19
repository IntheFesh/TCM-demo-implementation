"""R63 §2：拖选多行文字卡顿——**先量再改**。

§2.2 原话："用 Performance 面板录一段拖选，不要凭猜改。" 这个脚本就是那段
录制的可复现版本：真浏览器、真 DOM、真的按下-移动-松开，收 `longtask`
（>50ms 的长任务，Core Web Vitals 的判据）与拖选期间的帧时间。

**输出的是改前改后能对照的数字**，不是"感觉快了"。用法：

    python -m scripts.profile_drag_select              # 量当前代码
    python -m scripts.profile_drag_select --json out.json

拖选路径刻意跨多行、跨多个可点术语（`.term`）——§2.2 的第 2 条假设是
"术语可点靠逐字包 span 实现，拖选触发大量节点样式重算"，只有真的从一个
term 拖到另一个 term 才碰得到它。

## 这个脚本自己踩过两个坑，都是"量了个零"

1. **端点取子元素的 `getBoundingClientRect`**：最后那个子元素早滚出视口了，
   `mouse.move` 被夹到视口边界，"跨多行"退化成一行里蹭一下——选中 9~40 个字。
   改成在**视口坐标**里横扫整列，选中字数上到 1013。
2. **不清上一段的选区**：第二段测完选区还在，第三段拖出来的选区跟它一样，
   `selectionchange` 一次都不触发，而"拖选耗时 437ms"和"最长帧 43ms"看着
   像发现了瓶颈——其实那 43ms 是 `classList.add` 的那次重排，跟拖选无关。
   差点据此改了生产 CSS。**所以 `selected_chars` 与 `selectionchange` 两个
   数必须一起看：它们是"这次测量到底测到东西没有"的判据**，不是附加信息。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from scripts.screenshot_ui import _chromium_path, _free_port, _wait_ready
from scripts.verify_r62_ui import _INJECT

#: 拖选的步数。步数太少测不出"每次 mousemove 都干重活"这类毛病
#: ——一次移动做 5ms 的事，10 步看不出来，120 步就是 600ms。
DRAG_STEPS = 120

_OBSERVER = """
(() => {
  window.__perf = {longtasks: [], frames: [], selectionEvents: 0};
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__perf.longtasks.push(Math.round(e.duration));
    }).observe({entryTypes: ["longtask"]});
  } catch (e) { window.__perf.noLongtask = String(e); }
  // selectionchange 有没有人在听、听了干多少活——挂一个自己的计数器当对照
  document.addEventListener("selectionchange", () => { window.__perf.selectionEvents++; }, true);
  let last = performance.now();
  const tick = () => {
    const now = performance.now();
    window.__perf.frames.push(Math.round(now - last));
    last = now;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
})()
"""


#: 压力版：把术语数顶到 R62 的上限、处方表加到 20 行。
#: 用的是页面自己的渲染函数（`__renderInjected` / `__setHerbName`），
#: 不是手搓 HTML——手搓的 DOM 跟上线那份长得不一样，量它没有意义。
_STRESS = """
(() => {
  const herbs = ["柴胡","白芍","甘草","桃仁","香附","川芎","陈皮","枳壳","当归","白术",
                 "茯苓","半夏","黄连","吴茱萸","延胡索","郁金","佛手","木香","砂仁","乌药"];
  const s3s = {
    organs: [{organ: "肝", supporting_symptoms: ["胃脘胀痛","脉弦","嗳气","纳差"],
              pathogenesis: "肝失疏泄，气机郁滞，横逆犯胃，胃失和降", rule_refs: []},
             {organ: "胃", supporting_symptoms: ["食后加重","泛酸"],
              pathogenesis: "胃气壅滞，通降失常", rule_refs: []}],
    syndrome: {name: "肝胃不和证", disease: "胃痛", rule_refs: []},
    method: {principle: "疏肝理气，和胃止痛", targets: ["肝气郁结","胃失和降"], rule_refs: []},
    formula: {candidate: {name: "柴胡疏肝散加减", source: "modified", confidence: "high",
      rationale: "疏肝理气，和胃降逆，佐以活血", doses_count: 7,
      usage: "水煎服，每日1剂，分2次温服",
      herb_items: herbs.map((n, i) => ({
        name: n, dose: 3 + (i % 9), dose_unit: "g",
        role: ["君","臣","佐","使"][i % 4],
        function_in_formula: "疏肝理气，和胃止痛，兼顾脾运"}))}, rule_refs: []},
    herb_choices: herbs.slice(0, 8).map((n) => ({
      herb: n, why_this_one: "性味归经与本证病位相合，功效对得上治法", rule_refs: []})),
    key_points: Array.from({length: 8}, (_, i) => ({
      point: `辨证要点第 ${i + 1} 条：胃脘胀痛，食后加重，嗳气则舒`,
      maps_to: "气滞在胃，食后气机更壅，得嗳气则气机暂通"})),
    differential: Array.from({length: 6}, (_, i) => ({
      syndrome: `鉴别证型${i + 1}`,
      excluded_because: "本例无该证的关键指征，舌脉亦不相符，故不取", rule_refs: []})),
    modifications: Array.from({length: 6}, (_, i) => ({
      if_symptom: `伴随症状${i + 1}`, action: "加",
      item: {name: "煅瓦楞子", dose: 15, dose_unit: "g"},
      why: "制酸止痛，性平不碍气机", rule_refs: []})),
    self_assessment: {weakest_link: "治法到方剂这一步",
                      uncovered_symptoms: ["纳差","夜寐不安"],
                      next_direction: "若三剂无效考虑兼夹湿热"}
  };
  window.__renderInjected({
    record_id: "STRESS01", safety_flag: null, rejected: false,
    explanations: {by_id: {}, terms: []},
    results: [{s3_structured: s3s,
               s3: {syndrome: "肝胃不和证", disease: "胃痛",
                    treatment_principle: "疏肝理气，和胃止痛"},
               corroboration: null}],
    guideline: null
  });
})()
"""


def _stats(name: str, page) -> dict:
    perf = page.evaluate("() => window.__perf")
    frames = [f for f in perf["frames"] if f > 0]
    long_frames = [f for f in frames if f > 50]
    return {
        "where": name,
        "longtasks": len(perf["longtasks"]),
        "longtask_ms_max": max(perf["longtasks"], default=0),
        "longtask_ms_total": sum(perf["longtasks"]),
        "frames": len(frames),
        "frame_ms_max": max(frames, default=0),
        "frames_over_50ms": len(long_frames),
        "selectionchange_events": perf["selectionEvents"],
    }


def _drag_across(page, container: str) -> float:
    """在**视口坐标**里横扫一个容器：左上偏内 → 右下偏内，逐步移动。

    第一版按首尾子元素的 `getBoundingClientRect` 取端点，结果选中的字数是
    9~40 个——最后那个子元素早就滚出视口了，`mouse.move` 会被夹到视口边界，
    于是"跨多行拖选"退化成在一行里蹭一下。**选中字数就是这件事的判据**：
    量不到东西的测量看起来跟"没有卡顿"一模一样。
    """
    box = page.eval_on_selector(container, """e => {
      const r = e.getBoundingClientRect();
      return {l: r.left, t: r.top, r: r.right, b: r.bottom};
    }""")
    vw, vh = page.viewport_size["width"], page.viewport_size["height"]
    x0 = box["l"] + 12
    y0 = max(box["t"] + 12, 12)
    x1 = min(box["r"] - 12, vw - 4)
    y1 = min(box["b"] - 12, vh - 8)
    page.mouse.move(x0, y0)
    page.mouse.down()
    t0 = time.perf_counter()
    for i in range(1, DRAG_STEPS + 1):
        k = i / DRAG_STEPS
        page.mouse.move(x0 + (x1 - x0) * k, y0 + (y1 - y0) * k)
    page.mouse.up()
    return (time.perf_counter() - t0) * 1000


def _selected_chars(page) -> int:
    """**拖完必须真的选中了字**。没选中的话上面量到的是一串空移动
    ——一个测不到任何东西的测量看起来跟"没有卡顿"一模一样。"""
    return page.evaluate("() => (window.getSelection().toString() || '').length")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--port", str(port), "--log-level", "warning"],
        env={**__import__("os").environ, "PRODUCT_MODE": "1"})
    rows: list[dict] = []
    try:
        if not _wait_ready(f"http://127.0.0.1:{port}/health", time.monotonic() + 90):
            print("服务没起来", file=sys.stderr)
            return 1
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.evaluate(_INJECT)
            page.wait_for_timeout(1800)
            n_terms = page.eval_on_selector_all(".term", "els => els.length")
            n_nodes = page.evaluate("() => document.querySelectorAll('*').length")
            print(f"页面里 .term {n_terms} 个，DOM 节点 {n_nodes} 个")

            # ① 中栏结论区：跨多行、跨多个 term
            page.evaluate("() => window.getSelection().removeAllRanges()")
            page.evaluate(_OBSERVER)
            page.wait_for_timeout(400)
            ms = _drag_across(page, "#col-mid")
            page.wait_for_timeout(400)
            row = _stats("中栏（整列横扫）", page)
            row["drag_wall_ms"] = round(ms, 1)
            row["selected_chars"] = _selected_chars(page)
            rows.append(row)

            # ② 右栏释义：文字最密的一块。**从第一段拖到最后一段**——
            # 首尾传同一个选择器会拖出一个退化的 0 字选区，量不到任何东西。
            page.click('#rx-body .term[data-name="柴胡"]')
            page.wait_for_timeout(1500)
            page.evaluate("() => window.getSelection().removeAllRanges()")
            page.evaluate(_OBSERVER)
            page.wait_for_timeout(400)
            ms = _drag_across(page, "#col-right")
            page.wait_for_timeout(400)
            row = _stats("右栏释义（文字最密）", page)
            row["drag_wall_ms"] = round(ms, 1)
            row["selected_chars"] = _selected_chars(page)
            rows.append(row)

            # ③ **压力版**：术语数顶到 R62 的上限（120 个）、处方表 20 行，
            # 然后从中栏第一行拖到最后一行——跨越自动滚动的边界。
            # 注入的那份结果只有 18 个术语、846 个节点，量不出"节点一多就卡"
            # 这类毛病；而用户拖的是一整页真实结果。
            page.evaluate(_STRESS)
            page.wait_for_timeout(1200)
            n2 = page.eval_on_selector_all(".term", "els => els.length")
            nodes2 = page.evaluate("() => document.querySelectorAll('*').length")
            print(f"压力版：.term {n2} 个，DOM 节点 {nodes2} 个")
            page.evaluate("() => window.getSelection().removeAllRanges()")
            page.evaluate(_OBSERVER)
            page.wait_for_timeout(400)
            ms = _drag_across(page, "#col-mid")
            page.wait_for_timeout(500)
            row = _stats(f"压力版中栏（{n2} 个术语 / {nodes2} 节点，跨自动滚动）", page)
            row["drag_wall_ms"] = round(ms, 1)
            row["selected_chars"] = _selected_chars(page)
            row["terms"] = n2
            row["dom_nodes"] = nodes2
            rows.append(row)
            # ④ 急症水印那一层：`.wm::after` 是**带 rotate 的整块覆盖**
            # （`inset: 0` 盖在处方区上），没有 `user-select: none`。
            # §2.2 的第 3、4 条假设正好指着它：旋转的伪元素叠在正文上，
            # 选区一变它就要重画；而它的文字还会跟着一起被选中、被复制。
            page.evaluate("() => document.getElementById('sec-formula').classList.add('wm')")
            page.wait_for_timeout(300)
            page.evaluate("() => window.getSelection().removeAllRanges()")
            page.evaluate(_OBSERVER)
            page.wait_for_timeout(400)
            ms = _drag_across(page, "#col-mid")
            page.wait_for_timeout(500)
            row = _stats("压力版 + 急症水印（.wm 旋转覆盖层）", page)
            row["drag_wall_ms"] = round(ms, 1)
            row["selected_chars"] = _selected_chars(page)
            row["watermark_in_selection"] = page.evaluate(
                "() => (window.getSelection().toString() || '').includes('须在处理急症')")
            rows.append(row)
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print()
    for r in rows:
        print(f"── {r['where']}")
        print(f"   长任务 {r['longtasks']} 个（最长 {r['longtask_ms_max']}ms，"
              f"合计 {r['longtask_ms_total']}ms）")
        print(f"   拖选 {DRAG_STEPS} 步耗时 {r['drag_wall_ms']}ms；"
              f"掉帧（>50ms）{r['frames_over_50ms']}/{r['frames']} 帧，最长 {r['frame_ms_max']}ms")
        print(f"   selectionchange 触发 {r['selectionchange_events']} 次；"
              f"**实际选中 {r['selected_chars']} 个字**")
        if "watermark_in_selection" in r:
            print(f"   水印文字被一起选中：{r['watermark_in_selection']}")
    if args.json:
        __import__("pathlib").Path(args.json).write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写到 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
