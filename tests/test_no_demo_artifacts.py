"""R47 §8.2 + §8.5：全仓扫描用户可见文本里的 demo/测试痕迹。

**判据分两层，这个文件是静态那一层。**

静态层（这里）：扫源码里会变成界面文字的那些串。好处是跑得快、每次 pytest
都跑；短板是它判不出"这段文案在产品模式下到底会不会被渲染出来"。
运行层（`scripts/screenshot_states.py --product`）：真的在产品模式下把页面
渲染出来，扫 `document.body.innerText`。那一层才是终审。
**两层都要**——只有静态层会漏掉运行时拼出来的字符串，只有运行层会让这条
约束一年只被检查几次（截图不是每次提交都跑的）。

## 白名单机制

`internal-only` 那些块的文案**本来就该带研究词**（用药对照带就是要写"噪声
地板"）。所以：
  - `index.html`：解析 DOM，标了 `internal-only` 的子树整个跳过；
  - `app.js` / `graph.js`：按顶层函数切块，含研究词的函数必须登记在
    `INTERNAL_RENDERERS` 里，并注明它对应 §8.2 的哪一条。
登记表比"整个文件豁免"严：加一个新的、没登记的产品面函数写了"ε"，这里会红。

## 只看含中文的串

纯 ASCII 的字符串在这个前端里几乎全是 DOM id、CSS 选择器和事件名
（`"demo-mode-banner"`、`"hybrid"` 作为 option 的 value），它们不是界面文字。
把它们算进来会逼着测试维护一张"这个 id 不算"的例外表，而例外表一长，
这条约束就废了。**代价说清楚**：纯英文的界面文案（如果有）这一层扫不到，
由运行层兜底。
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.web_harness import load_app_js, load_html

ROOT = Path(__file__).resolve().parent.parent

#: §8.2 的禁词表。每条后面是它属于清单第几项。
#: 中文词直接比；ASCII 词要求它出现在**含中文的串**里（见模块文档）。
BANNED = {
    "噪声地板": 7, "分歧度": 7, "Jaccard": 7, "ε": 7,
    "演示模式": 3, "评测": 15, "SDT": 15, "E3": 15, "E4": 15, "凭据记号": 15,
    "traceback": 12, "Traceback": 12, "LLMError": 12, "exit code": 12,
    "实验性": 16, "beta": 16, "原型": 16, "demo": 16, "Demo": 16,
    "待跑": 11, "未做": 11, "TODO": 11, "⏳": 11,
}

#: 内部轮次编号：`R32` / `R33` 这种。docs/ 里可以留，用户可见处不行。
ROUND_RE = re.compile(r"\bR\d{1,2}\b")

#: §8.2 第 14 条：检索方式的名字不上产品面。它们是 option 的 value
#: （纯 ASCII，按上面的规则本来就扫不到），这里单列是为了**中文文案里
#: 提到它们**的情况——"当前用的是 hybrid 检索"这种句子。
RETRIEVER_WORDS = ("hybrid", "dense", "bm25", "full_context")

#: 含研究词的渲染函数登记表：函数名 → 它属于 §8.2 的哪一条。
#: **登记不等于放行**：这些函数渲染的 DOM 必须挂在 `internal-only` 的块里，
#: 那一条由 tests/test_product_mode.py 的标记表钉着。
INTERNAL_RENDERERS = {
    # 第 7 条：用药对照带、噪声地板、分层读数。它们渲染进 #rx-compare 与
    # #divergence-detail，两块都标了 internal-only。
    "rxCompareHtml": 7, "rxBandSvgHtml": 7, "epsilonLabel": 7,
    "divergenceBannerText": 7, "layerText": 7,
    # 第 3 条：回放提示。渲染进 #demo-mode-banner；产品模式下这台服务若真
    # 在回放，走的是 §6 的降级机制，不用"演示模式"这个词。
    "demoModeText": 3,
}

CJK = re.compile(r"[一-鿿]")
STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"' + r"|'(?:[^'\\\n]|\\.)*'"
                       + r"|`(?:[^`\\]|\\.)*`", re.DOTALL)


def strip_js_comments(src: str) -> str:
    r"""去掉 `//` 与 `/* */`。**这一步不能省**：这个仓库的注释里到处在解释
    "为什么不用噪声地板这个词"，不去掉的话这条测试会红在自己的说明文字上
    （R42 起已经踩过四次，每次都是同一个形状）。

    **为什么是一个状态机而不是两条正则。** `(?m)^\s*//` 只去行首注释，
    行尾的 `const x = 1; // 解释噪声地板` 留着；而放开成 `//[^\n]*` 会把
    `"https://..."` 里的两个斜杠当注释开头，从那里一直吃到行尾——字符串
    被截断之后，后面的引号配对全乱，扫出来的"文案"是一堆碎片。
    所以按字符走一遍，记住自己在不在字符串里。正则在这件事上没有正确解。"""
    out: list[str] = []
    i, n = 0, len(src)
    quote = ""       # 当前所在字符串的引号（空 = 不在字符串里）
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


class ProductTextParser(HTMLParser):
    """收集 `internal-only` 子树**之外**的文字。

    用真的 HTML 解析器而不是正则：`internal-only` 的块里嵌着别的标签，
    正则切不准，而切不准的方向恰好是"少扫一块"——那正是这条测试最不能出
    的错。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0          # 当前在几层 internal-only 里
        self.stack: list[bool] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class") or ""
        internal = "internal-only" in cls
        self.stack.append(internal)
        if internal:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.stack:
            if self.stack.pop():
                self.depth -= 1

    def handle_data(self, data):
        if self.depth == 0:
            self.text.append(data)


def product_visible_html_text() -> str:
    p = ProductTextParser()
    p.feed(load_html())
    return "\n".join(p.text)


def js_chunks(src: str) -> dict[str, str]:
    """按顶层 `function name(` 把文件切成块。切不到函数里的那部分算
    `"<module>"`。"""
    src = strip_js_comments(src)
    out: dict[str, str] = {}
    marks = [(m.start(), m.group(1)) for m in re.finditer(r"(?m)^function\s+(\w+)\s*\(", src)]
    if not marks:
        return {"<module>": src}
    out["<module>"] = src[: marks[0][0]]
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
        out[name] = out.get(name, "") + src[pos:end]
    return out


def chinese_strings(chunk: str) -> list[str]:
    return [m.group(0) for m in STRING_RE.finditer(chunk) if CJK.search(m.group(0))]


# ---------- index.html：产品可见的文字 ----------

HTML_TEXT = product_visible_html_text()


@pytest.mark.parametrize("word", sorted(BANNED))
def test_index_html_product_text_has_no_banned_word(word):
    assert word not in HTML_TEXT, (
        f"index.html 的产品可见文字里出现了禁词「{word}」"
        f"（§8.2 第 {BANNED[word]} 条）")


def test_index_html_product_text_has_no_internal_round_number():
    hits = ROUND_RE.findall(HTML_TEXT)
    assert not hits, f"产品可见文字里有内部轮次编号：{hits}"


@pytest.mark.parametrize("word", RETRIEVER_WORDS)
def test_index_html_product_text_does_not_name_a_retrieval_mode(word):
    assert word not in HTML_TEXT


def test_the_internal_only_subtree_is_actually_skipped():
    """**这条测试是上面那几条的自检。** 解析器要是把 internal-only 子树也
    收进来了，上面全会红；要是它把整个文件都跳过了，上面全会假绿。
    所以这里正反各钉一次：研究面的词在整份 HTML 里**有**，在产品可见文字里
    **没有**。"""
    raw = load_html()
    assert "噪声地板" in raw, "整份 HTML 里本来就该有研究面的措辞"
    assert "噪声地板" not in HTML_TEXT
    assert "推导链" in HTML_TEXT or "辨证" in HTML_TEXT, "产品可见文字被整个跳过了"


# ---------- app.js：中文串 + 函数级白名单 ----------

APP_CHUNKS = js_chunks(load_app_js())


def _offending(chunks: dict[str, str]) -> list[str]:
    bad = []
    for name, chunk in chunks.items():
        if name in INTERNAL_RENDERERS:
            continue
        for lit in chinese_strings(chunk):
            for word in BANNED:
                if word in lit:
                    bad.append(f"{name}: 「{word}」 in {lit[:60]}")
    return bad


def test_app_js_product_facing_strings_have_no_banned_word():
    bad = _offending(APP_CHUNKS)
    assert not bad, ("这些函数的中文文案里有禁词，而它们不在 INTERNAL_RENDERERS "
                     "登记表里：\n  " + "\n  ".join(bad))


def test_the_whitelist_is_not_a_blanket_exemption():
    """登记表里每一项都要对得上 §8.2 的一条。写一个不在清单里的条号 = 拿
    白名单当垃圾桶。"""
    valid = set(BANNED.values()) | {2, 3, 7, 8}
    for fn, item in INTERNAL_RENDERERS.items():
        assert item in valid, f"{fn} 登记的 §8.2 条号 {item} 不在清单里"


def test_the_whitelist_has_no_dead_entries():
    """登记表里的函数得真的存在。函数改名之后留在表里的那一条会永远放行
    一个不存在的名字，而真正该被扫的新函数没人登记。"""
    names = set(APP_CHUNKS)
    dead = [fn for fn in INTERNAL_RENDERERS if fn not in names]
    assert not dead, f"登记表里这些函数已经不存在了：{dead}"


def test_app_js_has_no_round_number_in_chinese_strings():
    bad = []
    for name, chunk in APP_CHUNKS.items():
        for lit in chinese_strings(chunk):
            if ROUND_RE.search(lit):
                bad.append(f"{name}: {lit[:60]}")
    assert not bad, "中文文案里有内部轮次编号：\n  " + "\n  ".join(bad)


def test_the_comment_stripper_actually_strips():
    """自检：注释里满是"噪声地板"这四个字，去不掉的话上面那条恒红。"""
    src = 'const a = 1; // 这里解释噪声地板\n/* 也讲 ε */\nconst b = "正文";'
    out = strip_js_comments(src)
    assert "噪声地板" not in out and "ε" not in out and "正文" in out


def test_the_chunker_attributes_strings_to_the_right_function():
    chunks = js_chunks('function alpha() { return "甲"; }\nfunction beta() { return "乙"; }')
    assert "甲" in chunks["alpha"] and "甲" not in chunks["beta"]
    assert "乙" in chunks["beta"]


# ---------- 后端：面向使用者的错误文案 ----------

API_SRC = (ROOT / "api" / "main.py").read_text(encoding="utf-8")


def test_http_error_details_are_chinese_not_exception_names():
    """§8.2 第 12 条：英文技术词与报错原文不上产品面。
    扫 `HTTPException(... detail=...)` 里的字面量。"""
    bad = []
    for m in re.finditer(r'detail=(?:_public_text\()?(["\'])(.*?)\1', API_SRC, re.DOTALL):
        text = m.group(2)
        if not CJK.search(text):
            continue
        for word in ("Traceback", "LLMError", "schema", "null", "exit code"):
            if word in text:
                bad.append(f"{word} in {text[:50]}")
    assert not bad, "错误文案里有英文技术词：" + "；".join(bad)


def test_the_product_name_has_no_demo_word():
    from core.version import PRODUCT_NAME

    assert "demo" not in PRODUCT_NAME.lower()
    assert CJK.search(PRODUCT_NAME)


def test_the_page_title_matches_the_product_name():
    """§8.4 第 32 条：页面标题与产品名统一。两处不一致时，浏览器标签页上
    是一个名字、页脚上是另一个——那正是"拼起来的"观感。"""
    from core.version import PRODUCT_NAME

    m = re.search(r"<title>(.*?)</title>", load_html())
    assert m and m.group(1).strip() == PRODUCT_NAME


def test_the_h1_matches_the_product_name():
    from core.version import PRODUCT_NAME

    m = re.search(r"<h1>(.*?)</h1>", load_html())
    assert m and m.group(1).strip() == PRODUCT_NAME
