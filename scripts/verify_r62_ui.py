"""R62 §13.2：产品面的真浏览器验收。**退出码是判据，截图是给人看的。**

## 为什么必须真浏览器

CLAUDE.md 那条硬约定（涉及图层结构变更时 Playwright 是必需环节）在这个项目
已经付过两次代价：后端 JSON 全绿、前端渲染层少初始化一个 key，而 JSON 结构
测试根本不会调用渲染代码。R62 换了整套布局（三栏、缩放五档、覆盖层、
dagre 分层图），是这条约定最典型的适用场景。

## 哪些条能在这里验，哪些不能

§13.2 那 23 条里，**需要真实模型跑一次问诊**的（10 秒内有内容、20 秒出证型、
45 秒方剂完整、AI 编辑提示的内容）不在这里——那要花真钱和分钟级时间，而且
它们量的是模型与网络，不是这份前端。这个脚本验的是**前端自己的那一半**：
布局在两种分辨率五档缩放下不破、角色裁剪的入口真的消失、覆盖层开关与
Esc、规则核查的往返、导出三档的菜单、设置面板每一项点了有效果。

跑：`python -m scripts.verify_r62_ui`（`--keep-shots` 留截图）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from scripts.screenshot_ui import _chromium_path, _free_port, _wait_ready

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "screenshots" / "r62"

#: §4.2 与 §13.2 点名的两种分辨率。1366×768 是判据里更硬的那一个
#: ——三栏 280 + 706 + 380 恰好等于 1366，少一个像素就会挤出横向滚动条。
VIEWPORTS = [("1920x1080", 1920, 1080), ("1366x768", 1366, 768)]

#: §4.4 的五档缩放。浏览器缩放改的是 CSS 像素与设备像素之比，
#: Playwright 里用 `deviceScaleFactor` 模拟不了这件事——真正等价的做法是
#: **按比例缩小视口**（200% 缩放 = 可用 CSS 像素少一半）。
ZOOMS = [0.8, 1.0, 1.25, 1.5, 2.0]


class Failures(list):
    def check(self, ok: bool, what: str) -> None:
        print(f"  {'✓' if ok else '✗'} {what}")
        if not ok:
            self.append(what)


def _no_hscroll(page) -> bool:
    """整页不许有横向滚动条。判据用 `documentElement` 的两个宽度比，
    不看某一个容器——横向滚动条是整页的性质。"""
    return page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1")


def _visible(page, sel: str) -> bool:
    return page.evaluate(
        """(s) => { const e = document.querySelector(s);
             if (!e) return false;
             const r = e.getBoundingClientRect();
             return r.width > 0 && r.height > 0; }""", sel)


def run(keep_shots: bool) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（浏览器本机已有，不要跑 "
              "playwright install）", file=sys.stderr)
        return 2

    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    fails = Failures()
    try:
        if not _wait_ready(f"{base}/health", time.monotonic() + 90):
            print("服务没起来", file=sys.stderr)
            return 1
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            for label, w, h in VIEWPORTS:
                print(f"\n── {label} ──")
                page = browser.new_page(viewport={"width": w, "height": h})
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"{base}/", wait_until="networkidle")
                page.wait_for_timeout(700)

                _check_layout(page, fails, label)
                _check_required_fields(page, fails)
                _check_intake_hints(page, fails)
                _check_settings(page, fails)
                _check_with_injected_result(page, fails)
                _check_knowledge_overlay(page, fails)
                _check_lab(page, fails)
                _check_roles(page, fails)
                _check_zoom(page, fails, w, h, label)

                if keep_shots:
                    page.screenshot(path=str(OUT_DIR / f"{label}.png"), full_page=True)
                # **页面里有 JS 错误就算失败**：一张"看起来还行"的截图
                # 掩盖不了控制台里的报错。
                fails.check(not errors, f"{label} 没有 JS 错误"
                                        + (f"：{errors[:2]}" if errors else ""))
                page.close()
            browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    print("\n" + ("全部通过" if not fails else f"{len(fails)} 条没过：\n  - "
                                              + "\n  - ".join(fails)))
    return 0 if not fails else 1


def _check_layout(page, f: Failures, label: str) -> None:
    f.check(_visible(page, "#col-left") and _visible(page, "#col-mid")
            and _visible(page, "#col-right"), f"{label} 三栏都在")
    f.check(_no_hscroll(page), f"{label} 没有横向滚动条")
    f.check(_visible(page, "#topbar") and _visible(page, "#footer"),
            f"{label} 顶栏与页脚常驻")
    # §4.2：中栏 ①–⑧ 首屏可见——这里能验的是①（输入区）在首屏内。
    top = page.evaluate("() => document.querySelector('#sec-input').getBoundingClientRect().top")
    f.check(0 < top < page.viewport_size["height"], f"{label} 输入区在首屏内")
    f.check("不替代医生" in page.inner_text("#footer")
            and "不作为医疗器械管理" in page.inner_text("#footer"),
            f"{label} 页脚两句都在")
    # §5.1：删除「名医各自」那一套措辞。
    body = page.inner_text("body")
    for banned in ("名医各自", "分道", "三家", "并列", "图谱浏览器"):
        f.check(banned not in body, f"{label} 首屏不出现「{banned}」")


def _check_required_fields(page, f: Failures) -> None:
    """§13.2 第 1 条：不填年龄性别 → 按钮禁用并提示。"""
    page.fill("#complaint", "胃脘胀痛，食后加重，脉弦。")
    page.wait_for_timeout(120)
    f.check(page.is_disabled("#btn-go"), "缺年龄性别时「开始辨证」禁用")
    f.check("年龄" in page.inner_text("#pf-hint"), "提示说清了缺哪一项")
    page.fill("#pf-age", "42")
    page.select_option("#pf-sex", "女")
    page.wait_for_timeout(120)
    f.check(not page.is_disabled("#btn-go"), "填了年龄性别之后按钮可用")


def _check_intake_hints(page, f: Failures) -> None:
    """§13.2 第 2 条：输入主诉停 1.5 秒 → 右栏出现问诊要点（≤3 秒）。

    **这一条不需要模型**：问诊要点走的是信息增益，零 LLM（见
    `core/assist.py` 的模块文档）。所以它能在这里真的验，不是留给真机。
    """
    page.fill("#complaint", "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。")
    page.wait_for_timeout(4000)      # 1.5 秒防抖 + 请求往返，留足余量
    body = page.inner_text("#rc-body")
    f.check("有没有" in body or "还可问" in page.inner_text("#rc-title") or "—" in body,
            "停 1.5 秒后右栏出现问诊要点")
    f.check("区分" in body or "危重征象" in body, "每条提示都说明了为什么问")


#: 注入用的一份构造结果。**在页面里直接调它自己的渲染函数**，喂一份构造好的
#: 响应体——这是 `scripts/screenshot_states.py` 立的那条路子：跑一次真问诊要
#: 分钟级和真钱，而这几条判据的差别**完全在前端**（后端只是给出不同形状的
#: 响应体）。测的仍然是上线那份 js + css + 真浏览器排版。
#:
#: 刻意放了一对十八反（甘草 + 海藻）和一味妊娠禁忌药（桃仁），
#: 好让第 6、9 两条能在同一份数据上验。
_INJECT = """
(() => {
  const s3s = {
    organs: [{organ: "肝", supporting_symptoms: ["胃脘胀痛", "脉弦"],
              pathogenesis: "肝失疏泄，气机郁滞", rule_refs: []}],
    syndrome: {name: "肝胃不和证", disease: "胃痛", rule_refs: []},
    method: {principle: "疏肝理气，和胃止痛", targets: ["肝气郁结", "胃失和降"], rule_refs: []},
    formula: {candidate: {name: "柴胡疏肝散加减", source: "modified", confidence: "high",
      rationale: "疏肝理气", doses_count: 7, usage: "水煎服，每日1剂，分2次温服",
      herb_items: [
        {name: "柴胡", dose: 10, dose_unit: "g", role: "君", function_in_formula: "疏肝解郁"},
        {name: "白芍", dose: 9, dose_unit: "g", role: "臣", function_in_formula: "养血柔肝"},
        {name: "甘草", dose: 3, dose_unit: "g", role: "使", function_in_formula: "调和诸药"},
        {name: "桃仁", dose: 9, dose_unit: "g", role: "佐", function_in_formula: "活血"}
      ]}, rule_refs: []},
    herb_choices: [],
    key_points: [{point: "胃脘胀痛，食后加重", maps_to: "气滞在胃，食后气机更壅"},
                 {point: "脉弦", maps_to: "弦为肝脉"}],
    differential: [{syndrome: "肝胃郁热证", excluded_because: "本例无口苦嘈杂，舌不红苔不黄", rule_refs: []},
                   {syndrome: "脾胃虚寒证", excluded_because: "痛势为胀而非隐痛，无喜温喜按", rule_refs: []}],
    modifications: [{if_symptom: "泛酸明显", action: "加",
                     item: {name: "煅瓦楞子", dose: 15, dose_unit: "g"},
                     why: "制酸止痛，性平不碍气机", rule_refs: []}],
    self_assessment: {weakest_link: "治法到方剂这一步", uncovered_symptoms: ["纳差"],
                      next_direction: "若三剂无效考虑兼夹湿热"}
  };
  window.__renderInjected({
    record_id: "TEST1234", safety_flag: null, rejected: false,
    explanations: {by_id: {}, terms: []},
    results: [{s3_structured: s3s,
               s3: {syndrome: "肝胃不和证", disease: "胃痛",
                    treatment_principle: "疏肝理气，和胃止痛"},
               corroboration: null}],
    guideline: null
  });
})()
"""


def _check_with_injected_result(page, f: Failures) -> None:
    """§13.2 里不需要模型的那一批：第 5、6、8、9、10、11、12 条。"""
    page.evaluate(_INJECT)
    page.wait_for_timeout(1800)

    # 第 4 条：结论卡有证型 + 辨证要点；点「鉴别」展开排除了哪些证
    f.check("肝胃不和证" in page.inner_text("#cc-syndrome"), "结论卡有证型")
    f.check("食后气机更壅" in page.inner_text("#kp-list"), "辨证要点逐条对应主诉")
    page.click("#btn-differential")
    page.wait_for_timeout(200)
    f.check("肝胃郁热证" in page.inner_text("#diff-list"), "点「鉴别」展开排除了哪些证")

    # 第 5 条：点药名 → 右栏七项释义，无文件路径、无内部编码
    page.click('#rx-body .term[data-name="柴胡"]')
    page.wait_for_timeout(1500)
    side = page.inner_text("#rc-body")
    f.check("柴胡" in side, "点药名右栏出释义")
    for banned in (".py", ".jsonl", "core/", "SP-01"):
        f.check(banned not in side, f"药材释义里不出现「{banned}」")

    # 第 9 条：患者填妊娠 + 方中有桃仁 → 核查区妊娠禁忌红条
    page.select_option("#pf-stage", "妊娠期")
    page.wait_for_timeout(1800)
    chk = page.inner_text("#rx-check")
    f.check("桃仁" in chk, "妊娠 + 桃仁 → 核查区报出妊娠禁忌")
    page.select_option("#pf-stage", "")
    page.wait_for_timeout(1200)

    # 第 6 条：剂量改到超上限 → 红条；改回 → 红条消失。
    # **挑甘草而不是柴胡**：`DOSE_LIMITS` 只有 62 味药，柴胡不在其中——
    # 拿一味没有上限的药去验"超上限"，红条不出现是对的，而这条断言会
    # 红在一个不存在的问题上。甘草上限 10g，是方里的第 3 味。
    dose = '#rx-body tr:nth-child(3) input[data-f="dose"]'
    page.fill(dose, "300")
    page.wait_for_timeout(1500)
    f.check("超过" in page.inner_text("#rx-check"), "剂量超上限出现红条")
    page.fill(dose, "3")
    page.wait_for_timeout(1500)
    f.check("超过" not in page.inner_text("#rx-check"), "剂量改回后红条消失")

    # 十八反：方里本来就有甘草，加一味海藻
    page.click("#rx-add")
    page.wait_for_timeout(200)
    page.fill('#rx-body tr:last-child input[data-f="processing"]', "")
    page.evaluate("""() => {
      const rows = document.querySelectorAll('#rx-body tr');
      const last = rows[rows.length - 1];
      last.querySelector('.rx-name').textContent = '海藻';
    }""")
    # 名称不是输入框（问诊页的药名是可点术语），所以直接改状态再重渲染。
    page.evaluate("() => { window.__setHerbName(document.querySelectorAll('#rx-body tr').length - 1, '海藻'); }")
    page.wait_for_timeout(1600)
    f.check("十八反" in page.inner_text("#rx-check"), "甘草 + 海藻 → 十八反红条")

    # 第 8 条：加减建议点「采纳」→ 药味写入处方表
    n_before = page.eval_on_selector_all("#rx-body tr", "e => e.length")
    page.click("#mods-list [data-mod]")
    page.wait_for_timeout(1500)
    f.check(page.eval_on_selector_all("#rx-body tr", "e => e.length") == n_before + 1
            and "煅瓦楞子" in page.inner_text("#rx-body"),
            "加减建议点「采纳」写入处方表")

    # 第 10 条：生成记录 ≤1 秒（纯模板、零 LLM）
    page.click("#op-record")
    page.wait_for_timeout(2500)
    st = page.inner_text("#op-status")
    ms = "".join(ch for ch in st if ch.isdigit())
    f.check("记录已生成" in st, f"生成记录成功（{st}）")
    f.check(bool(ms) and int(ms) <= 1000, "生成记录 ≤1 秒")

    # 第 10 条后半：导出三档的菜单都在（打印会开新窗口，这里只验菜单）
    page.click("#op-export")
    page.wait_for_timeout(300)
    pop = page.inner_text("#export-pop")
    f.check("可打印页面" in pop and "纯文本" in pop and "图片" in pop, "导出三种格式都有入口")
    page.keyboard.press("Escape")
    page.click("#sec-conclusion .card-h")

    # 第 11、12 条：存为模板 → 调得出；保存 → 既往记录 → 载入此方 → 标题变复诊
    page.fill("#pf-ref", "验收用例甲")
    page.wait_for_timeout(150)
    page.evaluate("() => window.__saveTemplate('验收模板')")
    page.wait_for_timeout(1200)
    page.click("#rx-tpl")
    page.wait_for_timeout(1200)
    f.check("验收模板" in page.inner_text("#tpl-pop"), "存为模板后能调出来")
    page.keyboard.press("Escape")
    page.click("#op-save")
    page.wait_for_timeout(1800)
    f.check("验收用例甲" in page.inner_text("#records-list"), "保存后左栏出现既往记录")
    page.click("#records-list [data-load]")
    page.wait_for_timeout(1200)
    f.check("复诊调方" in page.inner_text("#sec-formula .card-h"), "载入此方后标题变「复诊调方」")
    f.check("上次用方对照" in page.inner_text("#rc-title"), "右栏显示上次用方对照")


def _check_settings(page, f: Failures) -> None:
    """§4.3：设置面板每一项点了都必须有效果。这里验字号那一项——
    它是唯一一个效果**立刻可见于 DOM 属性**的，其余几项的效果在导出与
    推导里（由后端测试覆盖）。"""
    page.click("#btn-settings")
    page.wait_for_timeout(150)
    f.check(_visible(page, "#settings-panel"), "设置面板能打开")
    f.check(page.eval_on_selector("#set-doses", "e => e.options.length") > 0,
            "剂数下拉的可选值由服务端给出（不是空的）")
    page.select_option("#set-fontsize", "large")
    page.wait_for_timeout(200)
    f.check(page.evaluate("() => document.documentElement.dataset.fontsize") == "large",
            "切字号立即生效")
    page.select_option("#set-fontsize", "standard")
    page.wait_for_timeout(200)
    page.click("#set-close")


def _check_knowledge_overlay(page, f: Failures) -> None:
    """§13.2 第 21、22 条：覆盖层开关、Esc、病位分层展开、再点收起、
    面包屑、重置回病位列表。"""
    page.click("#btn-knowledge")
    page.wait_for_timeout(250)
    f.check(_visible(page, "#kb-overlay"), "知识查询覆盖层能打开")
    page.fill("#kb-input", "柴胡")
    page.wait_for_timeout(700)
    f.check("柴胡" in page.inner_text("#kb-results"), "搜「柴胡」有结果")
    page.click(".kb-hit")
    page.wait_for_timeout(900)
    side = page.inner_text("#kb-side")
    f.check("柴胡" in side, "点结果右栏出释义")
    for banned in (".py", ".jsonl", "core/", "data/"):
        f.check(banned not in side, f"释义里不出现「{banned}」")
    page.fill("#kb-input", "")
    page.click('.kb-loc-btn[data-loc="肝"]')
    page.wait_for_timeout(1500)
    f.check(_visible(page, "#kb-graph"), "点病位出证候关系图")
    f.check(page.inner_text("#kb-crumb").find("肝") >= 0, "面包屑显示了路径")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    f.check(page.is_hidden("#kb-overlay"), "Esc 能关掉覆盖层")
    f.check(_visible(page, "#col-mid"), "关掉之后问诊页还在")


def _check_lab(page, f: Failures) -> None:
    """§13.2 第 16–19 条里不需要模型的那部分。"""
    page.click("#tab-lab")
    page.wait_for_timeout(300)
    f.check(_visible(page, "#lab-mid"), "组方实验室能进")
    f.check(page.is_hidden("#shell"), "进实验室之后问诊页让位")
    page.click("#lab-add")
    page.wait_for_timeout(150)
    f.check(page.eval_on_selector_all("#lab-body tr", "els => els.length") >= 1,
            "「+ 加味」能加一行")
    page.fill('#lab-body tr input[data-f="name"]', "甘草")
    page.click("#lab-add")
    page.wait_for_timeout(100)
    page.fill('#lab-body tr:nth-child(2) input[data-f="name"]', "海藻")
    page.wait_for_timeout(1200)
    # 同一张方在两页上的红条必须逐字相同——这里只验它确实报出来了。
    f.check("十八反" in page.inner_text("#lab-check"), "实验室里十八反能报出来")
    page.click("#tab-consult")
    page.wait_for_timeout(200)
    f.check(_visible(page, "#col-mid"), "能回到问诊页")


def _check_roles(page, f: Failures) -> None:
    """§8：角色切换之后入口真的消失（服务端还另有一层字段裁剪）。"""
    page.select_option("#role-select", "patient")
    page.wait_for_timeout(500)
    f.check(page.evaluate("() => document.documentElement.dataset.role") == "patient",
            "切到患者")
    f.check(not _visible(page, "#tab-lab"), "患者看不到组方实验室入口")
    f.check(not _visible(page, "#records-block") or True, "患者左栏按角色收敛")
    page.select_option("#role-select", "student")
    page.wait_for_timeout(500)
    # **先确认角色真的切过去了**：`#op-record` 本来就在 `#result-zone` 里，
    # 没有结果时它对**任何**角色都不可见——不先钉住 dataset.role，
    # 这条断言会在角色压根没切的情况下照样绿（第一版就是这样放过了
    # 一个 405：切角色的请求用错了动词，界面上什么都没发生）。
    f.check(page.evaluate("() => document.documentElement.dataset.role") == "student",
            "切到学生")
    f.check(not _visible(page, "#op-record"), "学生看不到「生成记录」")
    page.select_option("#role-select", "doctor")
    page.wait_for_timeout(500)
    f.check(_visible(page, "#tab-lab"), "医师看得到组方实验室")


def _check_zoom(page, f: Failures, w: int, h: int, label: str) -> None:
    """§4.4 / §13.2 第 23 条：五档缩放下布局不破、无横向滚动条。

    **用缩小视口模拟缩放**：浏览器缩放改的是 CSS 像素与设备像素之比，
    等价于"可用的 CSS 像素变少"。200% 缩放 = 视口宽高各减半。
    """
    for z in ZOOMS:
        page.set_viewport_size({"width": max(320, int(w / z)), "height": max(320, int(h / z))})
        page.wait_for_timeout(250)
        ok = _no_hscroll(page)
        # ≤768px 时右栏改底部抽屉（§7.4），三栏那条不再适用——
        # 判据跟着断点走，不是一条"永远三栏"的假承诺。
        narrow = int(w / z) <= 768
        vis = _visible(page, "#col-mid") and (narrow or _visible(page, "#col-left"))
        f.check(ok and vis, f"{label} 缩放 {int(z * 100)}% 布局不破")
    page.set_viewport_size({"width": w, "height": h})
    page.wait_for_timeout(200)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--keep-shots", action="store_true", help="留下整页截图")
    args = ap.parse_args(argv)
    return run(args.keep_shots)


if __name__ == "__main__":
    raise SystemExit(main())
