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


def test_no_hard_coded_colour_outside_root_and_font_face(body):
    """**全部颜色只在 :root 里定义一处。** 散落在规则里的十六进制值没有任何人能
    回答"这个灰跟那个灰是不是同一件事"——而那正是"色只承担语义"（§7 第 7 条）
    唯一可执行的落法。R13 拆分前这个文件里有 62 个。"""
    found = HEX.findall(body)
    assert not found, f"规则里还有写死的颜色：{sorted(set(found))}"


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
