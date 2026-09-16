"""R24：八项前端改造的离线判据（node + 纯文本），Playwright 那一层单独跑。

**这个文件测的是"纯函数和文件事实"**：HTML 片段对不对、键盘动作映射对不对、
CSS 里有没有留下卡片、字表是怎么收的。真实渲染（自绘下拉真的能点开、两环真的
分得开、题记真的在首屏）由 `scripts/screenshot_states.py` 的 Playwright 那一路
验——CLAUDE.md 那条：涉及渲染结构的改动，JSON/纯函数测试测不出来。
"""
from __future__ import annotations

import json
import re
import subprocess

from tests.web_harness import DOM_STUB, ROOT, js_tmp, load_app_js, load_css, load_html

WEB = ROOT / "web"


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _html(expr: str) -> str:
    return _run(f"process.stdout.write({expr});")


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


# ---------- 第 1 项：自绘下拉 ----------


def test_the_custom_select_lives_in_its_own_file_and_loads_first():
    """`web/ui/select.js` 独立一个文件，且在 app.js **之前**加载——
    app.js 末尾要调 `enhanceAllSelects()`，顺序反了那个函数还不存在。"""
    html = load_html()
    assert (WEB / "ui" / "select.js").exists()
    assert html.index('<script src="ui/select.js">') < html.index('<script src="app.js">')


def test_the_native_select_stays_the_single_source_of_the_value():
    """**自绘层只是原生 `<select>` 的一张皮。** 判据有三条，缺一条就说明
    "现在选的是什么"有了第二个真相（CLAUDE.md 第 31 条）：
      · 原生元素不被移除（代码里不出现 remove/replaceWith）；
      · 值写回原生元素之后要派发 change（既有监听全挂在它上面）；
      · 隐藏用 opacity 而不是 display:none（后者让 Playwright 判定不可交互）。
    """
    src = (WEB / "ui" / "select.js").read_text(encoding="utf-8")
    assert "sel.selectedIndex = i" in src
    assert 'dispatchEvent(new Event("change"' in src
    assert ".remove()" not in src and "replaceWith" not in src
    css = load_css()
    block = css[css.index(".cs-native {"):]
    block = block[:block.index("}")]
    assert "opacity: 0" in block and "display: none" not in block


def test_the_keyboard_contract_is_a_pure_function():
    """键盘契约逐键可测。自绘下拉最容易漏的就是这一段——原生元素白送的东西
    自绘之后全要自己实现。"""
    assert _json('selectKeyAction("Enter", false)') == "open"
    assert _json('selectKeyAction("ArrowDown", false)') == "open"
    assert _json('selectKeyAction("ArrowDown", true)') == "next"
    assert _json('selectKeyAction("ArrowUp", true)') == "prev"
    assert _json('selectKeyAction("Home", true)') == "first"
    assert _json('selectKeyAction("End", true)') == "last"
    assert _json('selectKeyAction("Escape", true)') == "close"
    assert _json('selectKeyAction("Enter", true)') == "commit"
    assert _json('selectKeyAction("Tab", true)') == "close"
    assert _json('selectKeyAction("a", true)') is None


def test_the_highlight_skips_disabled_and_does_not_wrap():
    """到头不回绕——回绕会让"按住 ↓"在首尾之间跳，用户以为列表还没到底。"""
    setup = ("const sel = {options: [{disabled: false}, {disabled: true}, {disabled: false}]};")
    assert _json(f"(() => {{{setup} return nextEnabledIndex(sel, 0, 1); }})()") == 2
    assert _json(f"(() => {{{setup} return nextEnabledIndex(sel, 2, 1); }})()") == 2
    assert _json(f"(() => {{{setup} return nextEnabledIndex(sel, 0, -1); }})()") == 0


def test_all_four_selects_are_marked_for_enhancement():
    """用属性选中而不是写死 id 列表：图谱浏览器那两个下拉是后来加的，
    写死 id 的话新加的下拉不会被增强，而"少了一个"在截图里很难发现。"""
    html = load_html()
    assert html.count("data-custom-select") == 4
    src = (WEB / "ui" / "select.js").read_text(encoding="utf-8")
    assert 'querySelectorAll("select[data-custom-select]")' in src


# ---------- 第 2 项：首屏题记 ----------


def test_the_epigraph_is_three_lines_and_claims_no_classical_source():
    """题记写这个项目自己的话。**不引古人**：一句带「《某书》云」的题记必须能核对到
    原文，而这台机器上核不了（books/ 是 gitignore 的）——编一句假出处比不放题记糟。
    这条判据就是钉住"没有伪造的引文"。"""
    html = load_html()
    block = html[html.index('<div id="epigraph">'):]
    block = block[:block.index("</div>", block.index('eg-3'))]
    assert block.count('class="eg-line') == 3
    for forged in ("云：", "曰：", "《临证指南医案》云", "《温病条辨》云"):
        assert forged not in block, f"题记里出现了引文式表述：{forged}"
    # 三行的分工：这是什么 / 给什么 / 不给什么
    assert "名医" in block and "各自辨证" in block
    # **题记里不许出现医家数**：注册表跑几位是可变的（R18 之后能开到五位），
    # 写死「两位/三位」就是第二处实现，而它在首屏正中央，改了注册表也不会有人想到它。
    for n in ("两位名医", "三位名医", "五位名医"):
        assert n not in block, f"题记里写死了医家数：{n}"
    assert "医案编号" in block and "噪声地板" in block
    assert "不做诊断" in block


def test_the_epigraph_only_shows_on_the_first_screen():
    """输入过一次之后它就该让位给结果——题记回答"这是什么"，
    而一旦有了结果，"这是什么"已经由结果本身回答了。"""
    css = load_css()
    assert "#epigraph { display: none; }" in css
    assert ".state-first #epigraph" in css


# ---------- 第 4 项：对照带 + SVG 斜纹 ----------


def test_the_band_is_svg_with_a_hatch_pattern_for_the_noise_floor():
    """斜纹而不是第二种灰：3px 高的两段纯色在投影和小屏上几乎分不开，
    而这条带子是整页的主角。**纹理不依赖亮度**，投影仪压暗了也还在。"""
    out = _html("rxBandSvgHtml({epsPct: 40, realPct: 60, exceeds: true})")
    assert "<svg" in out and "<pattern" in out
    assert 'fill="url(#rx-hatch)"' in out
    assert 'fill="var(--real)"' in out
    assert 'width="40"' in out and 'width="60"' in out
    # 无障碍：一张图必须有它说的话
    assert 'role="img"' in out and "aria-label" in out


def test_the_band_says_when_nothing_exceeds_the_noise_floor():
    """没超出也要画出来——"没超出"本身就是结论，把带子藏起来等于把结论藏了。"""
    out = _html("rxBandSvgHtml({epsPct: 100, realPct: 0, exceeds: false})")
    assert "噪声地板之内" in out
    unmeasured = _html("rxBandSvgHtml(null)")
    assert "没有噪声地板可比" in unmeasured


def test_the_band_math_is_unchanged():
    """`bandSegments` 一个字没改：R24 改的是画法，不是算法。
    （两段宽度比 = ε : (差异 − ε)，这是 R14 定的。）"""
    seg = _json("bandSegments(0.2, 0.5)")
    assert seg["epsPct"] == 40 and seg["realPct"] == 60 and seg["exceeds"] is True


# ---------- 第 5 项：四级层次 + 君臣佐使两列密排 ----------


def test_the_herb_groups_render_as_a_two_column_grid():
    """左列角色字（注解），右列药味（内容）。两列让四组药味的左边界对齐，
    眼睛竖着扫一遍就知道君臣各几味。"""
    js = """(() => {
      const cand = {herb_items: [
        {name: "党参", dose: 9, dose_unit: "g", role: "君"},
        {name: "白术", dose: 9, dose_unit: "g", role: "臣"},
      ]};
      return herbGroupsHtml(cand, false);
    })()"""
    out = _html(js)
    assert 'class="herb-grid"' in out
    assert out.count('class="hg-role"') == 2
    assert out.count('class="hg-herbs"') == 2
    assert "党参 9g" in out and "白术 9g" in out
    # 君药仍然加粗（M7 的判据没被这次改动弄丢）
    assert 'class="herb-jun"' in out


def test_the_four_levels_each_use_a_different_device():
    """四级各有一种区分手段，不是同一种手段的四个刻度：
    证型宋体大字 / 治法换字族 / 方名加一条上边线 / 药味缩进两列。
    只调字号的话四级会读成"一样的东西四种大小"。"""
    css = load_css()
    syndrome = css[css.index(".col-syndrome { font-size"):]
    syndrome = syndrome[:syndrome.index("}")]
    principle = css[css.index(".col-principle {"):]
    principle = principle[:principle.index("}")]
    formula = css[css.index(".col-formula {"):]
    formula = formula[:formula.index("}")]
    grid = css[css.index(".herb-grid {"):]
    grid = grid[:grid.index("}")]
    assert "17px" in syndrome
    assert "var(--font-ui)" in principle          # 换字族
    assert "border-top" in formula                # 加界线
    assert "grid-template-columns" in grid        # 两列密排


# ---------- 第 6 项：去卡片化 ----------


def test_the_content_blocks_are_no_longer_rounded_boxes():
    """去卡片化：内容块从"圆角边框盒"改成"左侧标记线 / 上分隔线 + 留白"。
    控件（输入框、按钮、chip、浮层）保留圆角——它们本来就该看起来可点。

    判据针对**内容块**逐个查：这几块原来都是 `border: 1px solid` + `--r-lg`。
    """
    css = load_css()
    for sel in ("#divergence-banner {", "#rx-compare.show {", ".triage-card {",
                ".followup-note {", "#input-panel {", ".western-drugs {"):
        block = css[css.index(sel):]
        block = block[:block.index("}")]
        assert "border-radius" not in block, f"{sel} 还是圆角盒"
        assert "border: 0" in block, f"{sel} 还留着整框"


def test_the_controls_keep_their_radius():
    """对照：控件没被顺手去掉圆角。去卡片化针对的是常驻内容块，不是按钮。"""
    css = load_css()
    for sel in ("#submit-btn {", ".cs-button {", "#quota-chip.show {"):
        block = css[css.index(sel):]
        block = block[:block.index("}")]
        assert "border-radius" in block, f"{sel} 的圆角被顺手去掉了"


# ---------- 第 7 项：两环图谱浏览器 ----------


def test_the_browser_has_exactly_two_rings():
    """正好两环：是枢纽 / 不是枢纽。原来三档会在屏幕上出现三四个半径相近的环，
    而"这个节点在第几环"本来是要一眼读出"离枢纽多远"的。

    **有意的契约变更（R24 补丁）**：两环的语义没变，判据从"concentric 回调
    返回 2 还是 1"换成"gbRelayout 仍然按枢纽/非枢纽分两组"。原断言钉的是
    concentric 的参数写法，而那个布局引擎已经被换掉了（理由见
    tests/test_graph_browser.py 同名判据）。**两环真的分得开**由
    tests/test_ui_r24_patch.py 的 `test_layout_positions_put_every_expanded_node_outside_every_hub`
    和 Playwright 的 rings 判据一起管——那两条比数一个字面量强。
    """
    src = (WEB / "graph.js").read_text(encoding="utf-8")
    body = src[src.index("function gbRelayout"):]
    body = body[:body.index("function gbRingLegendText")]
    assert "hubIds = visible.filter((id) => gbHubIds.has(id))" in body
    assert "gbLayoutPositions(" in body
    assert "gbDepthOf" not in src, "三档的旧函数还在（死代码）"


def test_the_rings_say_what_they_are():
    """环的含义必须写出来——一张同心圆图上"内圈是什么"如果要靠人猜，
    这个布局就只是好看而没有信息。"""
    text = _html('gbRingLegendText(20, 61)')
    assert "内圈 20" in text and "外圈 61" in text
    assert "深度在交互里" in text
    assert _html("gbRingLegendText(0, 0)") == ""
    assert 'id="gb-ring-legend"' in load_html()


# ---------- 第 8 项：顶栏折叠 + advice 渲染 + token 面板 ----------


def test_the_topbar_collapses_but_defaults_to_open():
    """**默认展开**：一个默认藏起来的控件区会让人以为功能不存在。"""
    src = load_app_js()
    body = src[src.index("function topbarCollapsed"):]
    body = body[:body.index("function applyTopbarCollapsed")]
    assert 'getItem(TOPBAR_COLLAPSED_KEY) === "1"' in body
    assert "return false" in body, "读不到 localStorage 时要按默认（展开）"
    html = load_html()
    assert 'id="topbar-toggle"' in html and 'aria-expanded="true"' in html
    assert 'aria-controls="topbar-controls"' in html


def test_the_advice_rows_are_split_by_severity():
    """严重度是这一层唯一有信息量的维度，三档画成一样就等于没渲染。"""
    js = """(() => {
      const advice = [
        {kind: "incompatible", herbs: ["甘草", "甘遂"],
         reason: "甘草 与 甘遂 属配伍禁忌，同方相见须改方",
         source_span: "十八反", severity: "blocking"},
        {kind: "thermal_mismatch", herbs: [], reason: "寒热相悖", source_span: null,
         severity: "warning"},
        {kind: "duplicate_effect", herbs: ["白术", "苍术"], reason: "功效重合",
         source_span: null, severity: "suggestion"},
      ];
      return adviceListHtml(advice);
    })()"""
    out = _html(js)
    for cls in ("adv-blocking", "adv-warning", "adv-suggestion"):
        assert cls in out, cls
    assert "十八反" in out
    # **有意的渲染变更**：药名不单独渲染成一个 span。五条规则的 reason 里都已经
    # 点名了涉及的药，前面再重复一遍读起来像数据出错了（截图里当场看出来的，
    # 而"药名在不在"这种断言两种写法都能过）。药名进 data 属性给机器读。
    assert 'data-herbs="甘草、甘遂"' in out
    assert out.count("甘草") == 2, "药名重复渲染了（reason 里一次 + data 属性一次）"
    css = load_css()
    blocking = css[css.index(".adv-row.adv-blocking {"):]
    blocking = blocking[:blocking.index("}")]
    assert "var(--danger)" in blocking


def test_the_skipped_rules_are_shown_not_hidden():
    """「这条规则没给出建议」和「这条规则根本没跑」在界面上长得一样而含义相反
    ——R23 的 skipped 字段存在的全部理由，前端不许把它折叠掉。"""
    js = """(() => {
      const skipped = [
        {rule: "missing_channel_guide", reason: "本草表还没建出来", available: false},
        {rule: "duplicate_effect", reason: "证型里没有病位", available: true},
      ];
      return adviceSkippedHtml(skipped);
    })()"""
    out = _html(js)
    assert out.count("adv-skipped-row") == 2
    assert "缺数据" in out and "不适用" in out
    assert "<details" not in out, "没跑的规则被折叠了"


def test_the_score_carries_its_own_caveat():
    """一个 0~1 的分摆在方子旁边而不说它是什么，读者只会当成"这方有多好"。"""
    out = _html('adviceBlockHtml({advice: [], advice_skipped: [], formula_score: 0.85})')
    assert "0.85" in out
    # 固定两位小数：三列并排时 `String(0)` 出来的「0」跟「0.70」读感不在一个量级
    zero = _html('adviceBlockHtml({advice: [], advice_skipped: [], formula_score: 0})')
    assert "0.00" in zero
    assert "不是疗效评分" in out
    assert "规则层没有发现问题" in out


def test_the_advice_block_is_empty_when_the_role_has_no_data():
    """patient 角色下这三个键根本不在响应里（服务端摘的）。
    前端不再判一遍角色——判两遍的话"谁决定患者能看什么"就有两个答案。"""
    assert _html("adviceBlockHtml({})") == ""
    src = load_app_js()
    body = src[src.index("function adviceBlockHtml"):]
    body = body[:body.index("function columnHtml")]
    assert "patient" not in body, "前端又判了一遍角色"


def test_the_token_panel_separates_this_consult_from_today():
    """两段分开报，因为**分母不同**：这一次问诊 vs 今天累计。
    合成一段会让"这次命中了没有"和"今天整体命中率"混成一个数。"""
    js = """(() => {
      const m = {retriever_mode: "full_context", prefix_tokens_by_section: {"§4 医案全量": 2805},
                 cache_hit_tokens: 179000, cache_miss_tokens: 1200, cache_hit_ratio: 0.993,
                 best_of_n: 3, reasoning_effort: "max", reasoning_tokens: 4096};
      const u = {tokens_today: {cache_hit: 500000, cache_miss: 9000, output: 12000,
                                cache_hit_ratio: 0.982}};
      return tokenPanelHtml(m, u);
    })()"""
    out = _html(js)
    assert "本次前缀各段 token" in out and "今日累计" in out
    assert "2,805" in out and "179,000" in out, "千分位没加——要跟 500,000 预算对着看"
    assert "99.3%" in out and "98.2%" in out
    assert "每位医家采 3 次" in out and "max" in out


def test_an_unreported_hit_ratio_is_not_zero_percent():
    """null 是"这个后端不报这个数"，**不是 0%**（R21 那条：0 会被读成
    "跑了但一次没命中"）。"""
    assert _html("formatHitRatio(null)") == "未报"
    assert _html("formatHitRatio(0)") == "0%"
    assert _html("formatTokenCount(null)") == "—"
    assert _html("formatTokenCount(180000)") == "180,000"


def test_the_token_panel_is_empty_without_any_data():
    """没有 manifest 也没有用量时不显示一个空框。"""
    assert _html("tokenPanelHtml(null, null)") == ""


# ---------- 第 3 项：字体子集化 ----------


def test_the_charset_comes_from_the_repo_not_a_handwritten_list():
    """字表从仓库里真实出现的文本收集。手写字表的失败模式很具体：
    改一句文案之后多出来的那个字没进子集，页面上那一个字掉回系统字体。"""
    from scripts.subset_fonts import TEXT_SOURCES, collect_charset

    chars = collect_charset()
    assert "web/index.html" in TEXT_SOURCES and "core/physicians.py" in TEXT_SOURCES
    # 界面上一定出现的字
    for ch in "名医辨证对照主诉噪声地板":
        assert ch in chars, ch
    # ASCII 与中文标点
    assert "A" in chars and "、" in chars and "《" in chars


def test_the_case_corpus_is_excluded_by_default_and_says_so():
    """医案原文十几万字，收了等于没子集化。**这是有代价的取舍**，
    所以既要有开关（--include-cases）又要在文档里写清代价。"""
    from scripts.subset_fonts import __doc__ as doc
    from scripts.subset_fonts import collect_charset

    small = collect_charset()
    assert "医案原文" in doc and "不进字表" in doc
    assert "--include-cases" in doc
    big = collect_charset(include_cases=True)
    assert len(big) >= len(small)


def test_the_charset_is_deterministic_and_drops_control_chars():
    """两次收集同一个集合；控制字符不进字表（它们不是字形）。"""
    from scripts.subset_fonts import charset_arg, collect_charset

    a, b = collect_charset(), collect_charset()
    assert a == b
    assert charset_arg(a) == charset_arg(b)
    assert not any(ord(c) < 32 for c in a)
    assert re.fullmatch(r"(U\+[0-9A-F]{4,6})(,U\+[0-9A-F]{4,6})*", charset_arg(a))


def test_the_four_faces_match_the_css():
    """四个字面 = CSS 里那四个 @font-face。少一个的后果是某个字重在离线时
    掉回系统字体，而那种差异要盯着看才发现。"""
    from scripts.subset_fonts import FACES

    css = load_css()
    # 数 `@font-face {` 而不是 `@font-face`：文件顶部那段注释里也提到这个词
    # （"除了下面这一段 :root 和 @font-face"），按词数会多出一个。
    assert len(FACES) == css.count("@font-face {")
    for family, weight, _slug in FACES:
        assert f"font-weight: {weight}" in css


def test_the_script_does_not_rewrite_the_css_itself():
    """`app.css` 是设计令牌的唯一定义处。脚本偷偷改它会让"字体在哪定义"
    这件事多出一个不可见的作者——所以只打印该贴的四段。"""
    src = (ROOT / "scripts" / "subset_fonts.py").read_text(encoding="utf-8")
    assert "css_face_block" in src
    assert "app.css" in src
    assert "write_text" not in src.split("def css_face_block")[1].split("def main")[0]


def test_check_deps_exits_nonzero_when_fonttools_is_missing():
    """沙盒里必然缺 fonttools。**退出码要能区分"缺依赖"和"跑完了"**——
    上机脚本靠退出码判断要不要继续。"""
    from scripts.subset_fonts import main, missing_deps

    if missing_deps():
        assert main(["--check-deps"]) == 1
    else:
        assert main(["--check-deps"]) == 0
