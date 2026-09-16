"""前端 node 测试的共用入口：怎么拿到那份脚本、拿什么 DOM 桩去跑它。

## 为什么要这个文件

R13 之前，12 个前端测试各自写 `html.split("<script>")[-1].split("</script>")[0]`
去抽 `web/index.html` 里的内联脚本，而 `DOM_STUB` 在 9 个文件里各抄了一份
（其中 8 份完全相同、1 份多一行 fetch 桩——**已经开始分叉了**）。
R13 把前端拆成 `index.html` + `app.css` + `app.js` + `graph.js` 之后，
那个 split 抽法一个字符都抽不到东西，正好把这两件事一起收口：

- **脚本从文件读**，不再从 HTML 里切字符串；
- **DOM 桩只此一处**（CLAUDE.md 第 31 条）。

## 为什么两个 js 文件拼起来跑

浏览器里它们是两个 `<script src>`，**共用同一个全局作用域**——`function` 声明
互相可见，顶层的 `let`/`const` 也在同一个全局词法环境里。node 里把两份文本拼起来
跑，语义跟浏览器一致；分成两个文件反而要自己造模块边界，那才是跟线上不一样。

拼接顺序跟 `index.html` 里的 `<script src>` 顺序一致：**ui/select.js → graph.js →
app.js**（app.js 末尾有一批加载时就执行的初始化，其中包括调用 select.js 的
`enhanceAllSelects()`——顺序反了那个函数还不存在）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

# 浏览器里有、node 里没有的那几样东西。`anyNode` 是个什么都接住的 Proxy：
# 被测代码在顶层绑事件、查元素、调 cytoscape 都不会炸，而真正要断言的纯函数
# 照常可用。**故意不做成"像样的 DOM"**——做得越像，测试越容易在"其实没渲染"
# 的情况下绿。要验真实渲染请用 Playwright（那是另一条路，见 R13/R14 的截图）。
DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
"""

# 额外把 fetch 也堵掉的版本：测"网络不可用时前端怎么表现"的用例用它。
# 单独一个常量而不是给 DOM_STUB 加参数——两者回答的是不同的问题
# （"有没有 DOM" vs "有没有网"），合成一个开关以后会看不出哪条测试依赖哪一条。
DOM_STUB_OFFLINE = DOM_STUB + 'globalThis.fetch = () => Promise.reject(new Error("no net"));\n'

# 拼接顺序 = index.html 里 <script src> 的顺序。两者不一致的话，node 里跑得通的
# 代码在浏览器里可能因为初始化顺序而炸。
SCRIPT_FILES = ("ui/select.js", "graph.js", "app.js")


def load_app_js() -> str:
    """`graph.js` + `app.js` 拼成一份，跟浏览器里的全局作用域等价。"""
    return "\n".join((WEB / name).read_text(encoding="utf-8") for name in SCRIPT_FILES)


def load_css() -> str:
    return (WEB / "app.css").read_text(encoding="utf-8")


def load_html() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")


def js_tmp(source: str) -> str:
    """把脚本写进临时文件，返回路径，给 subprocess 当 node 的参数用。

    不用 `node -e`：`-e` 是把整份脚本当**一个命令行参数**交出去，而 Linux 上单个
    参数的上限（MAX_ARG_STRLEN）是 128KB。前端脚本加上 DOM 桩和用例自己的尾巴，
    在 D1/F1 那一轮越过了这条线，症状是 `OSError: [Errno 7] Argument list too long`
    ——看起来像 node 崩了，其实是 `exec` 根本没起来，跟被测代码毫无关系。

    **文件刻意不删**：临时目录本来就会被系统清理，留着的好处是测试挂掉时可以
    直接 `node /tmp/xxx.js` 原样复现，不用再去拼一遍脚本。
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(source)
        return f.name
