"""R13：设计令牌与「明确不做的」六条（docs/DESIGN.md §1、§2.1–2.4）的静态断言。

**为什么把"不做什么"写成测试**：总纲 §1 列的六条全是"AI 生成设计最典型的样子"，
它们不会因为某次改动而报错，只会一点点渗回来——全大写英文标签、按钮文字后面加
箭头、所有元素一个圆角。没有断言的话，三轮之后页面又长成通用模板，而每一次改动
单看都合理。
"""
import re

import pytest

from tests.web_harness import load_css, load_html

# 令牌只允许在这两个地方定义具体颜色值：:root 和 @font-face。
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


@pytest.fixture(scope="module")
def css() -> str:
    return load_css()


@pytest.fixture(scope="module")
def body(css: str) -> str:
    """:root 与 @font-face 之外的部分——真正写规则的那 600 多行。"""
    return css[css.index("/* §2.4：尊重 prefers-reduced-motion"):]


# ---------- §2.1–2.3：令牌齐全 ----------


def test_every_token_in_the_design_doc_is_defined(css):
    """R13 的要求是"逐字按 §2.1–2.4 落令牌"。缺一个，用到它的规则就会静默回退到
    继承值——页面不报错，只是颜色不对。"""
    for name in ("--paper", "--surface", "--surface-2", "--ink", "--ink-2", "--muted",
                 "--rule", "--rule-soft", "--danger", "--danger-bg", "--caution",
                 "--caution-bg", "--verified", "--noise", "--real",
                 "--font-classic", "--font-ui", "--font-num",
                 "--r-sm", "--r-md", "--r-lg", "--edge", "--edge-soft"):
        assert f"{name}:" in css, f"令牌 {name} 没定义"
    assert len(re.findall(r"--s[1-8]:", css)) == 8, "间距应该是 8 档"


def test_the_token_values_match_the_design_doc(css):
    """值也要对得上——只查"有没有这个变量名"的话，把 --paper 写成 #fff 照样绿，
    而那正好是总纲 §1 第一条要避开的米白。"""
    for name, value in (("--paper", "#F2F3EF"), ("--ink", "#1E211C"),
                        ("--danger", "#B3261E"), ("--caution", "#8A6D1F"),
                        ("--verified", "#2C5F5A"), ("--noise", "#B9BDB3")):
        assert re.search(rf"{name}:\s*{value}", css, re.I), f"{name} 不是 {value}"


def test_verified_is_documented_as_coincidentally_equal_to_ye_tianshi(css):
    """`--verified` 跟叶天士同值（总纲 §2.1："复用青黛"），但叶天士的色现在从
    `core/physicians.py` 注入、`--verified` 写死在 CSS——**将来会分叉**。

    **不改成绑定**：语义色不该跟着某位医家走。哪天叶天士换个色，"有出处可核"这个
    语义没有任何理由跟着变；反过来也一样。所以处理方式是在那一行写清楚"同值是巧合
    不是绑定"，让下一个看到这里的人不会顺手把它们接起来。
    """
    line = next(ln for ln in css.splitlines() if "--verified:" in ln)
    assert "巧合" in line or "不是绑定" in line, f"那一行没说明同值的性质：{line.strip()}"


def test_no_hard_coded_colour_outside_root_and_font_face(body):
    """**全部颜色只在 :root 里定义一处。** 散落在规则里的十六进制值没有任何人能
    回答"这个灰跟那个灰是不是同一件事"——而那正是"色只承担语义"（§7 第 7 条）
    唯一可执行的落法。R13 拆分前这个文件里有 62 个。"""
    found = HEX.findall(body)
    assert not found, f"规则里还有写死的颜色：{sorted(set(found))}"


def test_no_hard_coded_colour_in_the_html_either():
    """**扫描范围包括 index.html。** R13 只扫了 app.css，于是第 8 行那个内联 SVG
    favicon 里的旧模板蓝 `%233b5bdb` 一路留到了 R14——而那正好是总纲第一部分点名
    要避开的那个蓝，出现在浏览器标签页上，比页面里任何一处都显眼。

    HTML 里的颜色必须是总纲里的值（favicon 是内联 data URI，没法引用 CSS 变量，
    所以这里只能比对"是不是总纲定义过的颜色"，不能像 CSS 那样要求零十六进制）。
    """
    html = load_html()
    allowed = {"#2C5F5A", "#9C6B16", "#8A4736", "#F2F3EF", "#1E211C", "#B3261E"}
    found = {m.upper().replace("%23", "#") for m in re.findall(r"(?:%23|#)[0-9a-fA-F]{6}", html)}
    extra = found - allowed
    assert not extra, f"index.html 里有总纲之外的颜色：{sorted(extra)}"
    assert "3B5BDB" not in "".join(found), "旧模板蓝还在（总纲第一部分点名要避开它）"


def test_inline_style_attributes_carry_no_colour_of_their_own():
    """**颜色藏在 `style="..."` 属性里才是上一条漏掉两个色值的真正原因。**
    R13 的 `test_index_html_has_no_inline_script_or_style` 只查 `<style>` 块，
    `style=` 属性不算内联样式；而 `test_no_hard_coded_colour_*` 只扫 app.css。
    两条断言各自都通过，两条角标的橙和褐就从这道缝里活了下来。

    这里不禁止 `style=` 本身（排版微调放在结构文件里可读性更好），只禁止它带
    自己的颜色：属性里出现的任何颜色都必须是 `var(--令牌)`。
    """
    html = load_html()
    decls = re.findall(r'style="([^"]*)"', html)
    offenders = []
    for decl in decls:
        for prop, value in re.findall(r"([a-z-]*(?:color|background)[a-z-]*)\s*:\s*([^;]+)", decl):
            if "var(--" not in value:
                offenders.append(f"{prop}: {value.strip()}")
    assert not offenders, f"内联 style 属性里写死了颜色：{offenders}"


def test_identity_colours_are_not_written_into_the_css(css, body):
    """**三家身份色的唯一来源是 core/physicians.py**（§2.1 的订正）。CSS 里出现
    具体色值 = 第二处实现，注册表加第四位医家时它不会跟着长出来。

    查的是 `:root` **之外**：`--verified: #2C5F5A` 在 :root 里是合法的——总纲 §2.1
    写明"有出处可核（**复用青黛**）"，那是一个语义色恰好跟叶天士同值，不是身份色的
    第二处定义。三个 `--ye/--wu/--zhang` 的声明里则不许出现任何十六进制。"""
    for value in ("#2C5F5A", "#9C6B16", "#8A4736"):
        assert value not in body, f"规则里写死了身份色 {value}"
    for name in ("--ye", "--wu", "--zhang"):
        decl = re.search(rf"{name}:\s*([^;]+);", css)
        assert decl, f"{name} 要有兜底声明（接口挂了不至于变透明色）"
        assert not HEX.search(decl.group(1)), f"{name} 的兜底值里写死了颜色：{decl.group(1)}"


# ---------- §1：明确不做的六条 ----------


def test_no_uppercase_english_labels(css, body):
    """§1：全大写英文标签（`SYMPTOMS`）是通用模板最扎眼的特征之一。"""
    assert "text-transform: uppercase" not in css
    html = load_html()
    shouty = re.findall(r">\s*([A-Z][A-Z ]{3,})\s*<", html)
    assert not shouty, f"页面上有全大写英文标签：{shouty}"


def test_no_wide_letter_spacing(body):
    """§1：宽字距 + 全大写是同一套模板妆。中文本来就不该拉字距。"""
    spacing = [s for s in re.findall(r"letter-spacing:\s*([\d.]+)(?:px|em)", body)
               if float(s) > 0.5]
    assert not spacing, f"有明显的宽字距：{spacing}"


def test_no_arrow_suffixed_buttons():
    """§1：按钮文字后面加 `→`。"""
    html = load_html()
    arrows = re.findall(r">\s*[^<>]*→\s*<", html)
    assert not arrows, f"按钮文案带箭头：{arrows}"


def test_shadows_are_only_used_for_floating_layers(body):
    """§2.3：**不用阴影做层次**，用底色和边框。唯一允许的阴影是浮层
    （tooltip、侧栏）——所以整份 CSS 里 box-shadow 的出现次数要很少，
    且只能引用 --shadow-float 那一个令牌。"""
    shadows = re.findall(r"box-shadow:\s*([^;]+);", body)
    offenders = [s.strip() for s in shadows
                 if "var(--shadow-float)" not in s and s.strip() != "none"]
    assert not offenders, f"用阴影做层次了：{offenders}"


def test_border_radius_has_exactly_three_levels(body):
    """§2.3：**圆角按层级区分，不是所有元素一个值**——所有元素同一个圆角是
    通用模板最明显的特征之一。所以规则里只许引用三个令牌，不许写具体像素。"""
    radii = re.findall(r"border-radius:\s*([^;]+);", body)
    literal = [r.strip() for r in radii
               if not r.strip().startswith("var(--r-") and r.strip() not in ("0", "50%", "999px")]
    assert not literal, f"规则里写死了圆角：{sorted(set(literal))}"


def test_monospace_is_only_for_the_pharmacy_text(css):
    """§3.3：等宽字体唯一该用的地方是药房格式文本（它要对齐）。
    §1：小号数据标签用等宽是模板特征。判据：等宽只通过 --font-mono 这一个令牌用。"""
    body_css = css[css.index("/* §2.4："):]
    raw_mono = [m for m in re.findall(r"font-family:\s*([^;]+);", body_css)
                if "monospace" in m and "var(--font-mono)" not in m]
    assert not raw_mono, f"直接写了等宽字体栈：{raw_mono}"


# ---------- §2.2 / §2.4 ----------


def test_webfonts_have_a_system_fallback_and_swap(css):
    """断网演示是回放模式存在的全部意义。`font-display: swap` 少一条，取不到字体时
    会留最多 3 秒空白；字体栈里少了系统兜底，断网时直接没有中文字形。"""
    faces = re.findall(r"@font-face\s*\{[^}]*\}", css, re.S)
    assert len(faces) >= 4, "至少要有 Noto Serif/Sans 各两档"
    for face in faces:
        assert "font-display: swap" in face, face[:80]
    assert "Songti SC" in css and "PingFang SC" in css, "字体栈里要有系统兜底"


def test_webfont_urls_are_pinned_to_a_version(css):
    """**字体版本不许浮动。** 同一个项目里 cytoscape 钉死 3.30.2 且有本地 vendor 兜底，
    字体却写 `@latest` —— 上游一改版字形就变，而这个项目对"两次跑出来的不一样"
    特别敏感（ε、fixture 回放、截图对比都建立在"同样输入同样输出"上）。

    字形变了不会报任何错，只会让上周的截图跟这周的对不上，而没人会想到是字体。
    """
    urls = re.findall(r'src:\s*url\("([^"]+)"\)', css)
    assert urls, "一个 @font-face 的 URL 都没抓到"
    floating = [u for u in urls if "@latest" in u or "/latest/" in u]
    assert not floating, f"字体 URL 版本浮动：{floating}"
    for url in urls:
        assert re.search(r"@\d+\.\d+\.\d+", url), f"URL 里没有具体版本号：{url}"


def test_no_animation_outside_the_three_tokens(css, body):
    """§2.4 只允许三处动效。**判据是"规则里不许出现具体时长"**——写成
    `.22s` 的那一处不会被 `prefers-reduced-motion` 那一块关掉（那一块重定义的
    是令牌），于是"尊重系统的减弱动效设置"只对一半的动效成立，而页面上
    看不出任何异常。

    R14 抓到的就是这一处：证据侧栏的滑入写死 `.22s`，值恰好等于 --t-highlight
    但语义是"展开/折叠"，两边都说得通——这正是为什么要用令牌而不是数值。
    """
    durations = re.findall(r"transition[^;{]*?:\s*[^;{]*?(\d*\.?\d+m?s)", body)
    hard = [d for d in durations if not d.startswith(".001")]
    assert not hard, f"规则里写死了动效时长（应当用 --t-* 令牌）：{sorted(set(hard))}"
    assert "@keyframes" not in css, "§2.4 之外不许再加 keyframes 动画"


def test_reduced_motion_is_respected(css):
    """§2.4：`prefers-reduced-motion: reduce` 时全部动效改为瞬时。"""
    assert "prefers-reduced-motion: reduce" in css
    block = css[css.index("prefers-reduced-motion: reduce"):]
    assert "--t-collapse: 0ms" in block
    assert "transition-duration: .001ms !important" in block


def test_the_three_allowed_transitions_have_tokens(css):
    """§2.4 只允许三处动效：展开/折叠 180ms、路径高亮 220ms、处方校验滑入 160ms。
    做成令牌是为了 reduced-motion 能一处关掉，也为了"第四种动效"必须显式加一个
    令牌才写得出来。"""
    for name, value in (("--t-collapse", "180ms"), ("--t-highlight", "220ms"),
                        ("--t-warn", "160ms")):
        assert re.search(rf"{name}:\s*{value}", css), f"{name} 不是 {value}"
