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


def load_ui_js() -> str:
    """只要 `ui/select.js` + `graph.js`，**不含 app.js**。

    给 DOM_FAKE 那一路用：app.js 末尾有一批加载即执行的初始化
    （`getElementById("tab-btn-consult").addEventListener(...)` 这种），
    它们要的是一个真实页面，在假 DOM 上会当场抛 TypeError。
    把 app.js 排除掉不是"绕过问题"——这一路测的是自绘下拉这一层的渲染行为，
    页面初始化那一层由 Playwright 兜。
    """
    return "\n".join((WEB / name).read_text(encoding="utf-8")
                     for name in ("ui/select.js", "graph.js"))


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


# **第二个 DOM 桩，回答的不是同一个问题**（CLAUDE.md 第 31 条的例外，走例外要写清区别）：
#
#   DOM_STUB（上面那个 Proxy）回答「被测代码在没有 DOM 的地方跑会不会炸」——
#       它什么都接住、什么都不记录，所以纯函数能测，渲染结果测不了。
#   DOM_FAKE（这个）回答「自绘层真的把东西渲染成了什么」——
#       它记录父子关系、属性、文本和事件，所以 `enhanceSelect` 的行为可以被断言。
#
# 合成一个的话，Proxy 那种"什么都接住"的性质会让所有断言恒真，
# 而恒真的断言正是这一轮要修的那个 bug 当初没被发现的原因。
#
# **它仍然刻意不是一个像样的 DOM**：只实现 select.js 真正用到的那些 API。
# 真实渲染（字号、重叠、能不能点开）由 Playwright 兜，那是另一条路。
DOM_FAKE = r"""
class FakeEvent {
  constructor(type, opts) {
    this.type = type;
    this.bubbles = !!(opts && opts.bubbles);
    this.defaultPrevented = false;
    this.target = null;
  }
  preventDefault() { this.defaultPrevented = true; }
}
globalThis.Event = FakeEvent;

class FakeNode {
  constructor(tag) {
    this.tagName = String(tag || "").toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.id = "";
    this._text = "";
    this._listeners = {};
    const owned = new Set();
    this.classList = {
      add: (c) => owned.add(c),
      remove: (c) => owned.delete(c),
      contains: (c) => owned.has(c),
    };
  }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : this._text;
  }
  set textContent(v) { this.children = []; this._text = String(v); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }
  _detach(n) {
    if (n.parentNode) {
      const i = n.parentNode.children.indexOf(n);
      if (i >= 0) n.parentNode.children.splice(i, 1);
    }
  }
  appendChild(n) { this._detach(n); n.parentNode = this; this.children.push(n); return n; }
  insertBefore(n, ref) {
    this._detach(n);
    n.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(n); else this.children.splice(i, 0, n);
    return n;
  }
  contains(n) {
    if (n === this) return true;
    return this.children.some((c) => c.contains(n));
  }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchEvent(e) {
    e.target = e.target || this;
    for (const fn of this._listeners[e.type] || []) fn(e);
    return !e.defaultPrevented;
  }
  focus() { globalThis.document.activeElement = this; }
  querySelectorAll() { return []; }
}

class FakeOption extends FakeNode {
  constructor(value, label) { super("option"); this.value = value; this.textContent = label; }
}

class FakeSelect extends FakeNode {
  constructor() { super("select"); this.options = []; this.selectedIndex = -1; }
  appendChild(n) {
    super.appendChild(n);
    if (n.tagName === "OPTION") {
      this.options.push(n);
      if (this.selectedIndex < 0) this.selectedIndex = 0;
    }
    return n;
  }
  // graph.js 只用 `sel.innerHTML = '<option value="">…</option>'` 这一种写法
  // （清空 + 放一个占位项），所以只支持这一种。别的写法要用到时再加，
  // 不预先造一个"通用 HTML 解析器"——那会让这个桩自己变成要维护的东西。
  set innerHTML(v) {
    this.children = [];
    this.options = [];
    this.selectedIndex = -1;
    const m = /<option value="([^"]*)">([^<]*)<\/option>/.exec(String(v));
    if (m) this.appendChild(new FakeOption(m[1], m[2]));
  }
  get innerHTML() { return ""; }
  get value() { const o = this.options[this.selectedIndex]; return o ? o.value : ""; }
  set value(v) { const i = this.options.findIndex((o) => o.value === v); if (i >= 0) this.selectedIndex = i; }
}

const FAKE_BY_ID = {};
globalThis.document = {
  activeElement: null,
  createElement: (tag) => (String(tag).toLowerCase() === "select" ? new FakeSelect() : new FakeNode(tag)),
  createTextNode: (t) => { const n = new FakeNode("#text"); n.textContent = t; return n; },
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: (id) => FAKE_BY_ID[id] || null,
  addEventListener: () => {},
  body: new FakeNode("body"),
};
globalThis.window = {addEventListener: () => {}, localStorage: {getItem: () => null, setItem: () => {}}};
globalThis.cytoscape = new Proxy(function () {}, {
  get: () => globalThis.cytoscape, set: () => true,
  apply: () => globalThis.cytoscape, construct: () => globalThis.cytoscape,
});

/** 造一个挂在容器里的原生 select（带 id，可被 getElementById 找到）。 */
function fakeSelect(id, pairs) {
  const host = new FakeNode("div");
  const sel = new FakeSelect();
  sel.id = id;
  sel.setAttribute("data-custom-select", "");
  for (const [value, label] of pairs || []) sel.appendChild(new FakeOption(value, label));
  host.appendChild(sel);
  FAKE_BY_ID[id] = sel;
  return sel;
}
"""
