"""R17：部署层在界面上的三样东西（docs/DESIGN.md §5.1 / §5.2）。

    顶栏右侧：BYOK 收起入口 + 「今日约剩 N 次」
    顶栏下方：演示模式提示 / 降级说明 / 服务未连接

三条都是**只在特定状态下才出现**的东西，而"该出现的时候没出现"不会报错。
演示模式提示尤其如此：它缺席的后果是**观众以为看到的是实时推理**，
而那是这个项目最不能含糊的一件事（README 有一整节叫「诚实标注不是可选项」）。
"""
import json
import subprocess
from pathlib import Path

from tests.web_harness import DOM_STUB, js_tmp, load_app_js, load_css, load_html

ROOT = Path(__file__).resolve().parent.parent


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


def _text(expr: str) -> str:
    return _run(f"process.stdout.write(String({expr}));")


# ---------- §5.1：BYOK 入口 ----------


def test_byok_is_a_collapsed_line_of_small_text_not_a_dialog():
    """§5.1 原话：「放在顶栏右侧，默认收起为一行小字」，**不要做成弹窗**，
    不要在首次进入时拦路。它是给用完额度的人用的后路，不是每个访问者都要先
    过的一道关。"""
    html = load_html()
    topbar = html[html.index('<header id="topbar">'):html.index("</header>")]
    assert 'id="byok-box"' in topbar, "BYOK 不在顶栏里"
    # R47：多了 `class="internal-only"`（§8.2 第 1 条，产品模式下整块不出现）。
    # 判据没变——仍然是"可收起的 details"，只是标签上多了一个属性。
    assert '<details id="byok-box" class="internal-only">' in topbar, "不是可收起的 details"
    assert "用自己的 API key（不限次数）" in topbar
    # details 默认收起：带 open 属性就等于一进来就摊开
    box = topbar[topbar.index('<details id="byok-box"'):]
    assert " open" not in box[:box.index(">")]


def test_the_byok_panel_quotes_the_security_boundary_verbatim():
    """那两句话是 §5.1 的原话。改写会丢掉两件事里的一件：
    "只在本次请求中转发"（不代理别的）和"关闭标签页后即清除"（不持久化）。"""
    html = load_html()
    assert "你的 key 只在本次请求中转发给 DeepSeek，不会存储在服务器上。" in html
    assert "关闭标签页后即清除。" in html


# ---------- §5.1：额度 chip 三档 ----------


def test_the_quota_chip_shows_remaining_consults_not_raw_calls():
    """顶栏那一枚只放次数。"次数"是服务端按 `calls_per_consult` 折算好的，
    前端不再自己算一份——除法写两处，`CALLS_PER_CONSULT` 改一次就会有一处忘了改。"""
    assert _text('quotaChipText({mode: "shared", remaining_consults_estimate: 3})') == "今日约剩 3 次"
    assert _text('quotaChipText({mode: "byok"})') == "自带 key"
    assert _text('quotaChipText({mode: "shared", degraded: true})') == "额度已用完"
    assert _json("quotaChipText(null)") is None


def test_the_three_levels_come_from_the_server_not_a_local_division():
    """80% 预警、100% 降级。**判据全在服务端算好**（`u.warn` / `u.degraded`），
    前端不自己拿 remaining/limit 再除一遍——阈值改一次就会有一处忘了改。"""
    assert _text('quotaChipLevel({mode: "shared"})') == "normal"
    assert _text('quotaChipLevel({mode: "shared", warn: true})') == "warn"
    assert _text('quotaChipLevel({mode: "shared", degraded: true, warn: true})') == "degraded"
    # BYOK 模式下额度跟访问者无关，不该显示成"快没了"
    assert _text('quotaChipLevel({mode: "byok", warn: true})') == "normal"
    src = load_app_js()
    body = src[src.index("function quotaChipLevel"):]
    body = body[:body.index("function renderUsage")]
    assert "/" not in body.replace("//", ""), "前端自己做了除法"


def test_the_chip_uses_caution_then_danger_not_one_colour_for_both():
    """两档颜色对应两件事：「快没了」和「已经换后端了」。合成一种的话
    第二件事就没有提示——而那件事改变了结果的性质（不再是实时推理）。"""
    css = load_css()
    warn = css[css.index("#quota-chip.q-warn"):]
    warn = warn[:warn.index("}")]
    degraded = css[css.index("#quota-chip.q-degraded"):]
    degraded = degraded[:degraded.index("}")]
    assert "var(--caution)" in warn and "var(--danger)" not in warn
    assert "var(--danger)" in degraded


# ---------- §5.1 第 3 条 / §5.2：降级与演示模式 ----------


def test_degradation_says_the_servers_own_words():
    """降级的原话由服务端给（`u.reason`），前端不自己编一套——两处措辞不一致时
    用户不知道信哪个。服务端没给才退回一句兜底。"""
    assert _text('degradeBannerText({degraded: true, reason: "全局额度已用完，已切回放。"})') \
        == "全局额度已用完，已切回放。"
    assert "回放" in _text('degradeBannerText({degraded: true})')
    assert _json("degradeBannerText({degraded: false})") is None
    assert _json("degradeBannerText(null)") is None


def test_the_demo_banner_text_is_assembled_from_health_not_hard_coded():
    """§5.2：「演示模式：结果来自 <日期> 录制的真实推理（<模型>），非实时调用」。
    日期和模型都从 `/health.demo_mode` 来——写死一个日期，换一批 fixture 之后
    页面会理直气壮地报一个错的日期。"""
    out = _text('demoModeText({recorded_at: "2026-09-15T10:00:00Z", model: "deepseek-v4-pro"})')
    assert out == "演示模式：结果来自 2026-09-15 录制的真实推理（deepseek-v4-pro），非实时调用"
    # 实时调用时不显示这一行——没有人需要被告知默认行为
    assert _json("demoModeText(null)") is None


def test_demo_and_degrade_share_the_not_an_error_look_offline_does_not():
    """§5.2：演示模式提示用 `--surface-2` 底、**不是警告色**（它不是错误）。
    降级同理。而"服务未连接"**是**故障——它用朱砂，跟前两条刻意分开。
    三条挤在一起用同一种颜色的话，真出故障时没人分得出来。"""
    css = load_css()

    def block(selector):
        b = css[css.index(selector):]
        return b[:b.index("}")]

    for selector in ("#demo-mode-banner.show", "#degrade-banner.show"):
        b = block(selector)
        assert "var(--danger)" not in b, f"{selector} 用了警告色"
    offline = block("#offline-banner.show")
    assert "var(--danger)" in offline


# ---------- 离线 ----------


def test_health_failure_says_the_service_is_not_connected():
    """`/health` 拿不到 = 后端不在。**要说出来**：一片空白让人以为页面还在加载，
    而它已经加载完了、只是点「辨证」一定会失败。"""
    src = load_app_js()
    assert "function renderOfflineBanner" in src
    body = src[src.index("async function initDemoModeBanner"):]
    body = body[:body.index("// ---------- A2")]
    assert body.count("renderOfflineBanner(false)") == 2, "HTTP 非 200 和 fetch 抛，两条路都要报"
    assert "renderOfflineBanner(true)" in body


def test_cytoscape_falls_back_to_a_local_vendor_copy():
    """断网时图谱要能画。CDN 拿不到就用本地 vendor——这条是"演示模式断网可用"
    的一半（另一半是字体走系统栈）。"""
    src = (ROOT / "web" / "graph.js").read_text(encoding="utf-8")
    assert "vendor/cytoscape" in src


def test_fonts_fall_back_to_the_system_stack():
    """每个 `--font-*` 的栈里，CDN 字体后面都要跟系统栈。判据是**断网时页面
    不得出现空白期**——所以 `@font-face` 还必须带 `font-display: swap`
    （默认的 block 会在取不到时留一段空白）。"""
    css = load_css()
    root = css[css.index(":root {"):css.index("@media")]
    for name in ("--font-classic", "--font-ui", "--font-num"):
        decl = root[root.index(f"{name}:"):]
        decl = decl[:decl.index(";")]
        assert "," in decl, f"{name} 没有回退栈"
    assert css.count("font-display: swap") >= 4
