// ---------- 底层：纯工具 + 宿主钩子 ----------
//
// **依赖方向只能是 app.js → graph.js，反过来不行。** R13 拆分时两边留下了双向的
// 裸全局互调（graph.js 直接调 app.js 的 showError / openEvidence / closeEvidence /
// escapeHtml / sleep），形成循环依赖：谁也不能单独被理解或替换，而 `window.TCM`
// 那两份"公开清单"里 33 个名字**对面一个都没用到**，所以那条"清单齐全"的测试
// 永远绿——删掉 showError 也绿，而浏览器里表现为"点了没反应、不报错"。
//
// 断开的办法分两类，按被调用的东西**是不是宿主的 UI** 来分：
//
//   纯工具（escapeHtml / sleep）：没有任何 UI 归属，搬到这一层来，两边都从这里取。
//   宿主 UI（错误条、证据侧栏）：**不搬**——它们属于问诊页，图谱不该知道页面上
//     有没有错误条。改成钩子：graph.js 只声明"出错时喊一声"，由 app.js 在启动时
//     注册真正的实现。默认实现是无害的兜底（console + 无操作），所以 graph.js
//     单独跑（node 测试、将来嵌到别的页面）也不会炸。

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// 方剂来源三种的中文名。**展示层只认中文名，id 只在数据层**——放在这一层是
// 因为图谱 tooltip 和问诊页的九段都要用，而依赖方向只能 app.js → graph.js
// （见上面那段），共用的东西必须落在被依赖的一侧。
const FORMULA_SOURCE_LABEL = { classic: "经典方", modified: "加减方", composed: "自拟方" };

function formulaSourceLabel(v) {
  // 认不出的值原样回落：显示一个陌生词好过显示空白（至少能搜）。
  return FORMULA_SOURCE_LABEL[v] || v || "";
}

function escapeHtml(str) {
  // 引号也转：这个函数的输出偶尔会被放进属性值里（title="..."），只转尖括号
  // 的话一个带引号的医案 id 就能从属性里逃出来。
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

//: 问诊图的 cytoscape 实例。**R42 从 app.js 搬过来的**——它只有这个文件在用，
//: 而依赖方向只能是 app.js → graph.js（见文件顶部那段）。声明在 app.js 里
//: 意味着 graph.js 单独被加载时它不存在；浏览器里两个 script 共享全局作用域，
//: 所以这个错一直没露出来，直到有一条只加载 graph.js 的 node 测试跑起来。
let cy = null;

// 宿主钩子。默认值故意**不是空函数**：出了错还是要有痕迹，静默才是最坏的情况
// （这正是循环依赖那条假测试造成的事故形状）。
const graphHooks = {
  onError: (message) => { console.error("[graph]", message); },
  onOpenEvidence: () => {},
  onCloseEvidence: () => {},
};

function setGraphHooks(hooks) {
  Object.assign(graphHooks, hooks || {});
}

// ---------- C1：cytoscape 加载兜底 ----------
//
// 图谱库原来只从 cdnjs 取，没有本地副本。CDN 取不到时的真实表现是
// ensureCanvas() 抛 "cytoscape is not defined"，被 submitConsult 的 catch
// 抓成"请求失败"——把一个静态资源问题报成后端问题，现场会照错方向排查。
// 而"断网可用"正是录制回放这条路线存在的理由，图又是演示里最有冲击力的
// 那部分。
//
// 兜底不写成第二个内联脚本标签，因为 12 个前端测试都靠"按开标签切分、取最后
// 一段"的办法抽这份脚本，多一个字面开标签就会静默抽错
// （见 tests/test_web_single_script.py 钉住的那条）。改成用时才动态插入。
//
// R41：**CDN 那一路整个去掉了。** index.html 原来在 `<head>` 里同步加载
// cdnjs 上的 cytoscape，实测 `renderBlockingStatus: "blocking"`、240 ms，
// 而那 373 KB 只有图谱页要用——不开图谱的人白等。加上三甲内网取不到 cdnjs，
// 那条请求会一直挂到超时，而"断网可用"正是录制回放这条路线存在的理由。
// 所以现在**只有这一条加载路径**：用时插入 web/vendor/cytoscape.min.js。
// 下面那句 CYTOSCAPE_MISSING_MSG 里还提 CDN，是因为部署方可能自己改回去；
// 判据在 tests/test_frontend_loading.py。
let _cytoscapePromise = null;
function ensureCytoscape() {
  if (typeof cytoscape !== "undefined") return Promise.resolve(true);
  if (_cytoscapePromise) return _cytoscapePromise;
  _cytoscapePromise = _loadScript("vendor/cytoscape.min.js",
    () => typeof cytoscape !== "undefined");
  return _cytoscapePromise;
}

// R42：dagre 也走同一条路（本地 vendor，无 CDN）。**它是可选的**：
// cytoscape 取不到图整个画不出来（硬失败，弹提示），dagre 取不到只是布局退回
// `fallbackLayout`（等距铺开，可能重叠），图还能看。这个区别必须体现在
// 返回值上——两个都当硬失败的话，一次 dagre 404 会让整页显示"图谱库没加载"，
// 而实际上图是能画的。
//
// **为什么不合并成一个 `ensureLibs`**：两个库的失败后果不同（见上），
// 合并之后调用方拿到的是一个布尔值，没法区分"没有图"和"图不好看"。
let _dagrePromise = null;
function ensureDagre() {
  if (dagreAvailable()) return Promise.resolve(true);
  if (_dagrePromise) return _dagrePromise;
  _dagrePromise = _loadScript(DAGRE_SRC, dagreAvailable);
  return _dagrePromise;
}

//: 两个 vendor 脚本的相对路径。**写在一处**：Worker 的 importScripts 和
//: 主线程的 <script> 插入都要用它，两处各写一份就会在换目录时漏掉一处
//: （而漏掉的表现是"Worker 里 dagre is not defined"，静默回落到主线程）。
const DAGRE_SRC = "vendor/dagre/dagre.min.js";

function _loadScript(src, ready) {
  return new Promise((resolve) => {
    if (typeof document === "undefined") { resolve(false); return; }
    const el = document.createElement("script");
    el.src = src;
    el.onload = () => resolve(!!ready());
    el.onerror = () => resolve(false);
    document.head.appendChild(el);
  });
}

// 两个库一起等。返回的是 **cytoscape 在不在**（硬条件）；dagre 的结果只影响
// `layoutStats.fallback_reason`，不影响能不能画。
async function ensureGraphLibs() {
  const [hasCy] = await Promise.all([ensureCytoscape(), ensureDagre()]);
  return hasCy;
}

const CYTOSCAPE_MISSING_MSG =
  "图谱库没有加载成功：本地副本 web/vendor/cytoscape.min.js 不存在。"
  + "辨证结果本身不受影响，只有图画不出来——断网演示前先把这个文件放好"
  + "（见 README「离线演示」）。";

// ---------- R41：布局算到 Worker 里去 ----------
//
// `computeLayout` 是**纯函数**（只读 nodes/edges，不碰 DOM、不碰 cytoscape），
// 所以它能整段搬到 Worker 线程上算。搬它的理由不是"现在慢"——实测问诊图
// （六层、十几个节点）的布局在主线程上是亚毫秒级，Worker 的收益为 0 甚至为负
// （建 Worker 本身几毫秒）。理由是**图会长大**：R42 要把这一页整页重做成
// dagre 布局的单条诊断链，图谱浏览器那张持久图是几百到几千个节点，
// dagre 在那个规模上是几十到几百毫秒——那正是一个会让点击没反应的长任务。
// 机制先立好、判据先钉住，换算法时不用再动这一层。
//
// ## 一个算法只有一处实现
//
// Worker 里**不重写一份布局**，而是 `importScripts` 把这份 graph.js 原样拉进去，
// 直接调它自己的 `computeLayout`。代价是 graph.js 必须能在没有 `window` 的环境
// 里被 import（见文件末尾那个 `typeof window !== "undefined"` 的护栏）。
//
// ## 三条兜底，缺一条这个优化就变成一个新的故障源
//
// 1. **Worker 建不起来**（老浏览器、CSP 挡了 blob:）→ 主线程算，正常出图；
// 2. **Worker 算不出来**（import 失败、算法抛异常）→ 主线程算；
// 3. **Worker 不回话**（卡死）→ `LAYOUT_WORKER_TIMEOUT_MS` 之后主线程算。
// 任何一条触发都会在 `layoutStats` 里记下来（`fallback_reason`），
// **不静默**：静默回落的表现是"Worker 毫无收益"，而人会以为是 Worker 没用。
const LAYOUT_WORKER_TIMEOUT_MS = 2000;
let _layoutWorker = null;
let _layoutWorkerDead = false;
//: 最近一次布局的统计。profiler 与测试读它。
const layoutStats = { where: null, ms: 0, fallback_reason: null, n_nodes: 0 };

function _graphJsUrl() {
  // 找到自己这份脚本的 URL。`import.meta` 在传统 script 里没有，
  // 所以从 document 里现查——**不要写死 "graph.js"**：部署可能挂在子路径下。
  const el = typeof document !== "undefined"
    ? document.querySelector('script[src*="graph.js"]') : null;
  return el ? new URL(el.getAttribute("src"), document.baseURI).href : null;
}

// R42：Worker 里要先有 dagre 才有意义（没有它 `computeLayout` 会退到
// `fallbackLayout`，等于白起一个线程）。URL 从 graph.js 自己的 URL 推——
// 两个文件都在 web/ 下，**不写死绝对路径**（同 `_graphJsUrl` 那条理由）。
function _dagreUrlFrom(graphJsUrl) {
  if (!graphJsUrl) return null;
  try {
    return new URL(DAGRE_SRC, graphJsUrl).href;
  } catch (e) {
    return null;
  }
}

function _layoutWorker_() {
  if (_layoutWorkerDead || _layoutWorker) return _layoutWorker;
  try {
    const url = _graphJsUrl();
    if (!url || typeof Worker === "undefined" || typeof Blob === "undefined") {
      _layoutWorkerDead = true;
      return null;
    }
    // **dagre 先 import，graph.js 后 import。** 顺序不是随意的：graph.js 顶层
    // 不碰 dagre（只有 `computeLayout` 运行时才查 `typeof dagre`），所以理论上
    // 两种顺序都能跑；但把它写成"先库后用库的人"能让下一个在这里加第三个
    // 依赖的人不必想这个问题。dagre 的 URL 推不出来时**只 import graph.js**
    // ——Worker 仍然起得来，布局退到 fallback，而 `layoutStats.fallback_reason`
    // 会写明；不 import 一个 null 把整个 Worker 弄挂。
    const dagreUrl = _dagreUrlFrom(url);
    const imports = (dagreUrl ? [dagreUrl, url] : [url]).map((u) => JSON.stringify(u));
    const boot = `importScripts(${imports.join(", ")});\n`
      + "self.onmessage = (e) => {\n"
      + "  try { self.postMessage({ ok: true, id: e.data.id,\n"
      + "        positions: computeLayout(e.data.nodes, e.data.edges) }); }\n"
      + "  catch (err) { self.postMessage({ ok: false, id: e.data.id,\n"
      + "        error: String(err && err.message || err) }); }\n"
      + "};\n";
    _layoutWorker = new Worker(URL.createObjectURL(
      new Blob([boot], { type: "text/javascript" })));
    _layoutWorker.onerror = () => { _layoutWorkerDead = true; _layoutWorker = null; };
  } catch (e) {
    _layoutWorkerDead = true;
    _layoutWorker = null;
  }
  return _layoutWorker;
}

let _layoutSeq = 0;

async function layoutAsync(nodes, edges) {
  const t0 = (typeof performance !== "undefined" ? performance.now() : 0);
  const done = (where, positions, reason) => {
    layoutStats.where = where;
    layoutStats.ms = Math.round(
      ((typeof performance !== "undefined" ? performance.now() : 0) - t0) * 100) / 100;
    // R42：**两个回落原因要一起报，不是二选一。**
    //
    // 这里有两层各自独立的回落：Worker 起不来/算不出来（`reason`），以及
    // dagre 没加载上/抛异常（`_computeFallbackReason`）。它们能同时发生，
    // 而且诊断价值完全不同——"Worker 建不起来"说的是线程，"dagre 没加载上"
    // 说的是布局质量（图会重叠）。
    // 写成 `reason || _computeFallbackReason` 的话，Worker 那一条会盖住
    // dagre 那一条，于是"图为什么重叠"这个问题在 layoutStats 里查不到。
    layoutStats.fallback_reason =
      [reason, _computeFallbackReason].filter(Boolean).join("；") || null;
    layoutStats.n_nodes = (nodes || []).length;
    return positions;
  };
  const w = _layoutWorker_();
  if (!w) return done("main", computeLayout(nodes, edges), "Worker 建不起来");
  const id = ++_layoutSeq;
  const positions = await new Promise((resolve) => {
    let settled = false;
    const finish = (value, reason) => {
      if (settled) return;
      settled = true;
      w.removeEventListener("message", onMsg);
      clearTimeout(timer);
      resolve({ value, reason });
    };
    const onMsg = (ev) => {
      // **按 id 认领**：同一个 Worker 会被连续几次布局复用，不认 id 的话
      // 上一次迟到的回复会被当成这一次的结果，图会用错的坐标画出来。
      if (!ev.data || ev.data.id !== id) return;
      if (ev.data.ok) finish(ev.data.positions, null);
      else finish(null, "Worker 报错：" + ev.data.error);
    };
    const timer = setTimeout(() => finish(null, "Worker 超时"), LAYOUT_WORKER_TIMEOUT_MS);
    w.addEventListener("message", onMsg);
    try {
      w.postMessage({ id, nodes, edges });
    } catch (e) {
      finish(null, "postMessage 失败：" + e.message);   // 结构化克隆不了
    }
  });
  if (positions.value) return done("worker", positions.value, null);
  return done("main", computeLayout(nodes, edges), positions.reason);
}

// ---------- R42：节点 id 的构造（前端这一侧的唯一实现） ----------
//
// 后端 `api/main.py::to_graph` 有一张 `LAYER_PREFIX`，这里是它在前端的对面。
// **两处必须说同一套词**：前端的证据链侧栏靠 id 反查（`buildEvidenceIndex`），
// 拼法差一个段，点节点打开的就是空白侧栏——而那看起来像"这个节点没有证据"。
//
// 为什么前端也要拼一遍：侧栏的内容是从 `data.results` 里现算的（后端不为每条
// 证据重复下发一遍 id），所以前端必须能从同一份数据算出同一个 id。
// 收在这一个函数里、并有一条测试**从后端产出的图里取真 id 跟前端拼的比**
// （tests/test_graph.py 那条），是这条契约唯一可核的形式。
//
// R42 去掉了 id 里的医家段（图上不再有医家分带）：`syn::{证型名}`、
// `formula::{方名}`、`herb::{方名}::{药名}`。
const NODE_ID = {
  symptom: (name) => `sym::${name}`,
  // 脏腑与病性是两层，前缀不同——判据跟后端 `_element_layer` 一致：
  // kind === "location" 是病位（脏腑），其余是病性。
  element: (name, kind) => `${kind === "location" ? "organ" : "nature"}::${name}`,
  organ: (name) => `organ::${name}`,
  nature: (name) => `nature::${name}`,
  syndrome: (name) => `syn::${name}`,
  pathogenesis: (text) => `mech::${text}`,
  principle: (text) => `principle::${text}`,
  method: (text) => `method::${text}`,
  formula: (name) => `formula::${name}`,
  herb: (formulaName, herbName) => `herb::${formulaName}::${herbName}`,
};

// ---------- R42：R24 那串"压字"常量退役了 ----------
//
// 这里原来有 `evenY` / `LAYER_X` / `Y_MIN` / `Y_MAX` / `HERB_GAP` 五样东西，
// 是手写分层布局的全部参数（R41 刚把它们从 app.js 搬过来，因为 Worker 只
// import graph.js）。它们连同 R24 为"标签重叠"加的那串上限一起退役，
// 因为它们解决的是同一个问题的**症状**而不是原因：
//
//   手写布局不知道标签有多宽 → 同层节点会挨上 → 加一个"最多展开几个"的上限去
//   减少节点数、加一个 `text-max-width` 去压字 → R37 实测 `text-max-width`
//   断不了中文（只在空白/换行处断行），于是压不动最宽的那个标签，
//   而别的标签白挨一刀（R37 报告里那串 20/19/18/17/16 → 2/1/4/7/6 的对压字数）。
//
// dagre 拿到每个节点的**真实盒子尺寸**，重叠在结构上不可能发生，这些参数就
// 没有存在的理由了。零重叠由 Playwright 用真实包围盒验（那才是最终判据）。
//
// **刻意留下这段注释而不是直接删空**：下一个人看到 `computeLayout` 里没有任何
// 间距常量时会想"是不是漏了"。答案是没漏，是不需要。

// ---------- R42：布局改成 dagre（**结构性**解决标签重叠） ----------
//
// ## 为什么换掉手写的分层布局
//
// 改之前是一套手写的"每层等距铺开 + 重心排序 + 医家分带"。它的问题不是不好看，
// 是**它不知道标签有多宽**：节点的横向位置由 `LAYER_X` 写死，纵向由
// `evenY` 等距分，而一个五个字的证型名比一个两个字的证素宽一倍多。
// R24 因此加了一串"标签上限"常量（`GB_EXPAND_CAP`、`labelMaxFor` 里的
// `text-max-width`）去**压字**——那是在用"少显示几个字"换"不重叠"，
// 而 R37 又实测出 `text-max-width` 断不了中文（只在空白/换行处断行），
// 于是那串常量既压不动最宽的标签、又让别的标签白挨一刀。
//
// dagre 的做法是反过来的：**把每个节点的真实盒子尺寸告诉布局算法**，
// 由它保证同一层的盒子之间留出 `nodesep`、层与层之间留出 `ranksep`。
// 重叠因此是**结构上不可能**，不是靠调常量调出来的。R24 那串压字常量
// 随之退役（`labelMaxFor` 只保留一个很宽的上限当兜底）。
//
// ## 为什么用裸 dagre 而不是 cytoscape-dagre
//
// `cytoscape-dagre` 要在 cy 实例上跑（`cy.layout({name:'dagre'})`），
// 那就没法放进 Worker 里。裸 `dagre` 是纯函数式的图算法库：喂
// (id, width, height) 与边，`dagre.layout(g)` 之后读 `g.node(id).x/y`。
// 所以 R41 建的那条 Worker 通路一行不用改——`computeLayout` 仍然是纯函数。
//
// 代价：节点尺寸要**自己算**（cytoscape-dagre 的 `nodeDimensionsIncludeLabels`
// 是问渲染层要的）。`measureLabel` 用"CJK 一个字一个字宽、ASCII 0.55 倍"
// 估，是个确定性估算——**它必须偏大不偏小**：估小了 dagre 留的间距不够，
// 重叠又回来了。零重叠由 Playwright 用**真实包围盒**验（那才是最终判据）。
const DAGRE_RANKSEP = 70;    // 层间距（横向，rankdir=LR 时）
const DAGRE_NODESEP = 22;    // 同层节点间距（纵向）
const DAGRE_EDGESEP = 12;
const DAGRE_MARGIN = 20;

//: 节点盒子的内边距与最小尺寸。**跟 app.css 里节点样式的 padding 对应**，
//: 估算偏大一点是有意的（见上面注释：宁可松不可紧）。
const NODE_PAD_X = 16;
const NODE_PAD_Y = 10;
const NODE_MIN_W = 44;
const NODE_MIN_H = 28;

//: compound（方剂包着它的药）父节点在 cytoscape 里画出来的内边距，
//: **单位是屏幕像素、不随缩放变化**。样式表里 `node:parent` 的 padding
//: 用的就是它，下面 dagre 的 COMPOUND_PAD 要比它大。
const COMPOUND_RENDER_PAD = 14;

//: dagre 给 compound 父节点留的内边距。**必须 ≥ COMPOUND_RENDER_PAD**：
//: dagre 按这个数留位、cytoscape 按上面那个数画框，留得比画得少 → 相邻两个
//: 候选方的框互相压住。
//:
//: R24 那一轮踩过这个坑：第一版给 20（当时是手写布局里的 FORMULA_MARGIN），
//: Playwright 截图里相邻候选方的框依然互相压住，后来抬到 60 才分开——
//: 那 60 是在**手写布局**下补偿"不知道标签有多宽"用的，dagre 知道每个盒子的
//: 真实尺寸，所以只需要盖住渲染内边距本身再留 4px 余量。
//: 零重叠由 Playwright 用真实包围盒验（`overlapStats`），不靠这个数拍脑袋。
const COMPOUND_PAD = COMPOUND_RENDER_PAD + 4;

function measureLabel(text, fontSize) {
  // CJK 字符按一个字宽算，其余按 0.55 倍。**不用 canvas measureText**：
  // Worker 里要额外建 OffscreenCanvas，而字体那时可能还没加载完，量出来
  // 比真实值小——比估算更危险（估小会重叠）。
  const s = String(text == null ? "" : text);
  let units = 0;
  for (const ch of s) {
    units += /[⺀-鿿豈-﫿＀-￯]/.test(ch) ? 1 : 0.55;
  }
  return { w: Math.max(NODE_MIN_W, units * fontSize + NODE_PAD_X * 2),
           h: Math.max(NODE_MIN_H, fontSize * 1.3 + NODE_PAD_Y * 2) };
}

//: 哪一层用多大的字。**跟 buildStylesheet 里的字号一致**——两处不一致时
//: dagre 按一个尺寸留位、渲染按另一个尺寸画，重叠就回来了。有测试比这两处。
const LAYER_FONT_SIZE = {
  0: 13, 1: 15, 2: 15, 3: 15, 4: 12, 5: 13, 6: 13, 7: 13, 8: 11,
};

function layerFontSize(layer) {
  const n = Number(layer);
  return LAYER_FONT_SIZE[n] == null ? 13 : LAYER_FONT_SIZE[n];
}

function dagreAvailable() {
  // **必须返回真正的布尔值**，不是最后那个真值（`dagre.layout` 是个函数）。
  // 返回函数的表现很隐蔽：`JSON.stringify({avail: dagreAvailable()})` 会把
  // 这个键整个丢掉（JSON 不序列化函数），于是量具/测试拿到的对象里压根没有
  // 那一项——看起来像"没测到"，而实际上是"测到了但值被吞了"。
  return !!(typeof dagre !== "undefined" && dagre && dagre.graphlib && dagre.layout);
}

//: 方剂框里药材竖排的行高与标题高度。
//:
//: **为什么药材不进 dagre**（这一段是 R42 踩出来的，不是设计选择）：
//: dagre 0.8.5 **不能处理"一条边的端点是 compound 父节点"**。方剂节点同时是
//: 两件事——它是 compound 父（里面装着君臣佐使），又是 治则/治法 → 方剂 那条边
//: 的终点。把这两件事同时交给 dagre，它在 `nestingGraph` 那一步抛
//: `TypeError: Cannot set properties of undefined (setting 'rank')`，
//: 而 `computeLayout` 的 try/catch 会**静默**退到 `fallbackLayout`——
//: 图还画得出来，只是等距铺开、可能重叠，没有任何报错。
//: 隔离出来的最小复现（`dagre.version === "0.8.5"`）：
//:   setNode(p) / setNode(f) / setNode(h); setParent(h, f); setEdge(p, f) → 抛
//:   同样三个节点，只把边改成 setEdge(p, h) → 正常
//: 所以做法是**把 compound 整块当一个盒子交给 dagre**：
//:   1. 子节点（君臣佐使）不进 dagre；
//:   2. 父节点（方剂）的尺寸按"能装下它全部子节点"算，dagre 因此给它留够位置；
//:   3. dagre 算完，子节点在父节点那个盒子里竖排。
//: cytoscape 那边本来就是按子节点算父节点的包围盒（所以父节点不给坐标），
//: 两边正好对上。
const COMPOUND_ROW_H = 30;
const COMPOUND_TITLE_H = 20;

//: `computeLayout` 这一次有没有回落、为什么。`layoutAsync` 读它（见那边的注释）。
//: **每次进 computeLayout 先清空**，否则上一次的原因会挂在这一次头上。
let _computeFallbackReason = null;

function computeLayout(nodes, edges) {
  _computeFallbackReason = null;
  if (!dagreAvailable()) {
    _computeFallbackReason = "dagre 没加载上";
    return fallbackLayout(nodes, edges);
  }
  const byId = new Map();
  for (const n of nodes || []) {
    const d = n.data || {};
    if (!d.id) continue;
    byId.set(d.id, d);
  }
  // 子节点按 parent 分组。**顺序就是它们在 nodes 里的顺序**——后端按
  // 君/臣/佐/使 的次序发，图上就该按那个次序竖排（排序换成按 label 会把
  // 君药排到中间去）。
  const childrenOf = new Map();
  for (const d of byId.values()) {
    if (!d.parent || !byId.has(d.parent)) continue;
    if (!childrenOf.has(d.parent)) childrenOf.set(d.parent, []);
    childrenOf.get(d.parent).push(d);
  }

  const g = new dagre.graphlib.Graph({ compound: false, multigraph: false });
  g.setGraph({
    // rankdir=LR：链条从左往右读，跟九段的阅读顺序一致。
    rankdir: "LR",
    ranksep: DAGRE_RANKSEP,
    nodesep: DAGRE_NODESEP,
    edgesep: DAGRE_EDGESEP,
    marginx: DAGRE_MARGIN,
    marginy: DAGRE_MARGIN,
    // network-simplex 是 dagre 的默认，确定性的——同一份输入永远同一个结果。
    ranker: "network-simplex",
  });
  g.setDefaultEdgeLabel(() => ({}));

  //: 一个节点在 dagre 里的盒子。有子节点的按"装得下全部子节点"算。
  const boxOf = (d) => {
    const own = measureLabel(d.label || d.id, layerFontSize(d.layer));
    const kids = childrenOf.get(d.id);
    if (!kids || !kids.length) return own;
    let w = own.w;
    for (const k of kids) {
      w = Math.max(w, measureLabel(k.label || k.id, layerFontSize(k.layer)).w);
    }
    return {
      // 留位**宁可比 cytoscape 画出来的大**：COMPOUND_PAD > COMPOUND_RENDER_PAD
      // （见那两个常量的注释），留少了相邻的候选方框会互相压住。
      w: w + COMPOUND_PAD * 2,
      h: kids.length * COMPOUND_ROW_H + COMPOUND_TITLE_H + COMPOUND_PAD * 2,
    };
  };

  for (const [id, d] of byId) {
    if (d.parent && byId.has(d.parent)) continue;   // 子节点不进 dagre
    const box = boxOf(d);
    g.setNode(id, { width: box.w, height: box.h, layer: Number(d.layer) });
  }
  for (const e of edges || []) {
    const d = e.data || {};
    if (!g.hasNode(d.source) || !g.hasNode(d.target)) continue;
    g.setEdge(d.source, d.target);
  }

  try {
    dagre.layout(g);
  } catch (err) {
    // 布局炸了不能让整页崩：退回等距铺开，图还能看，只是不好看。
    // **这条 catch 曾经掩盖过一个真 bug**（见上面那段 dagre 0.8.5 的记录），
    // 所以它现在会把原因记进 layoutStats——静默回落是最坏的情况。
    _computeFallbackReason = "dagre.layout 抛异常：" + String(err && err.message || err);
    return fallbackLayout(nodes, edges);
  }
  // ---- 把「层号」钉成「第几列」 ----
  //
  // **dagre 按最长路径排 rank，不认我们的 layer 字段。** 实测（R37 那份真
  // payload）：病性(2) 与脏腑(1) 都只有"症状→它"这一条入边，于是同 rank、
  // 挤在同一列（x 都是 403）；症状(0) 自己还分裂成两列（119 / 260）。
  // 而九层图的全部意义就是"第几层 = 第几列"。
  //
  // 试过的做法与为什么不行：加一个隐藏根、向每个节点连 `minlen = layer + 1`
  // 的边。`weight: 0` 时 network-simplex 根本不去拉紧它们（权重为 0 的边对目标
  // 函数没有贡献），层号照样对不上；把权重调上去又会让这个"连到所有节点"的根
  // 参与排序阶段，把纵向次序搅乱。
  //
  // 所以分工改成：**横向（第几列）我们自己定，纵向（同列里的次序）交给 dagre**
  // ——后者才是难的那一半（交叉最小化），前者是一条我们本来就知道答案的规则。
  const colOf = new Map();          // layer → 该列的 x
  const widest = new Map();         // layer → 该列最宽的盒子
  for (const [id, d] of byId) {
    if (d.parent && byId.has(d.parent)) continue;
    const L = Number(d.layer);
    if (!Number.isFinite(L)) continue;
    const n = g.node(id);
    widest.set(L, Math.max(widest.get(L) || 0, (n && n.width) || NODE_MIN_W));
  }
  let cursor = DAGRE_MARGIN;
  for (const L of [...widest.keys()].sort((a, b) => a - b)) {
    const w = widest.get(L);
    colOf.set(L, cursor + w / 2);
    cursor += w + DAGRE_RANKSEP;
  }

  const positions = {};
  //: 同一层里按 dagre 给的 y 排好序，再拉开到至少 `DAGRE_NODESEP` 的间距。
  //:
  //: 为什么还要这一步：dagre 只保证**同一个 rank 内**不重叠，而我们的一层可能
  //: 被它拆到了两个 rank 上（上面症状层那两列就是），那时两个节点的 y 可能相同，
  //: 强行并到一列就压在一起了。**顺序取 dagre 的**（那是交叉最小化的结果），
  //: 只调间距；并列相同时用 id 兜底，保证确定性。
  const byLayer = new Map();
  for (const [id, d] of byId) {
    if (d.parent && byId.has(d.parent)) continue;
    const n = g.node(id);
    if (!n || n.y == null) continue;
    const L = Number(d.layer);
    if (!byLayer.has(L)) byLayer.set(L, []);
    byLayer.get(L).push({ id, y: n.y, h: n.height || NODE_MIN_H });
  }
  for (const [L, items] of byLayer) {
    items.sort((a, b) => (a.y - b.y) || (a.id < b.id ? -1 : 1));
    for (let i = 1; i < items.length; i++) {
      const need = items[i - 1].y + items[i - 1].h / 2 + DAGRE_NODESEP + items[i].h / 2;
      if (items[i].y < need) items[i].y = need;
    }
    const x = colOf.get(L);
    for (const it of items) {
      const kids = childrenOf.get(it.id);
      if (kids && kids.length) {
        // **compound 父节点不给坐标**：cytoscape 自己按子节点算父节点的包围盒，
        // 给了父节点坐标会跟它算出来的打架（父节点被拉走、子节点留在原地）。
        // 子节点在这个盒子里竖排，父节点的框因此落在该落的地方。
        const top = it.y - it.h / 2 + COMPOUND_PAD + COMPOUND_TITLE_H;
        kids.forEach((k, i) => {
          positions[k.id] = { x, y: top + i * COMPOUND_ROW_H + COMPOUND_ROW_H / 2 };
        });
        continue;
      }
      positions[it.id] = { x, y: it.y };
    }
  }
  return positions;
}

//: dagre 不可用（没加载上、或者布局抛异常）时的兜底：**按层等距铺开**。
//: 它不保证不重叠——这是"图还能看"与"整页崩"之间的选择，而不是一个等价实现。
//: `layoutStats.fallback_reason` 会写明走了这条路。
function fallbackLayout(nodes, edges) {
  const positions = {};
  const byLayer = new Map();
  for (const n of nodes || []) {
    const d = n.data || {};
    if (!d.id) continue;
    const L = Number(d.layer) || 0;
    if (!byLayer.has(L)) byLayer.set(L, []);
    byLayer.get(L).push(d);
  }
  const layers = [...byLayer.keys()].sort((a, b) => a - b);
  layers.forEach((L, col) => {
    const items = byLayer.get(L);
    items.forEach((d, i) => {
      positions[d.id] = {
        x: DAGRE_MARGIN + col * (NODE_MIN_W + DAGRE_RANKSEP),
        y: DAGRE_MARGIN + i * (NODE_MIN_H + DAGRE_NODESEP),
      };
    });
  });
  return positions;
}

// ---------- 两张图共用的一份样式表（R16 §3.2 规格 7）----------
//
// R16 之前这里有两份：`buildStylesheet()`（问诊图）和
// `buildGraphBrowserStylesheet()`（图谱浏览器）。基础节点样式、边样式、
// 证素的紫、医案的橙……逐条重复，而两边的值已经开始漂了（问诊图证素用
// 一组淡紫，浏览器同一种节点也写了一遍同样的值——**同样的值写两遍，
// 就是下一次只改一遍的开始**）。CLAUDE.md 第 31 条。
//
// **差异只在一个参数**：问诊图按医家染色（三列的身份色要在图上对得上），
// 浏览器不染（那张图上没有"这是谁的判断"这回事，染色只会误导）。
//
// 共用的前提是两边说同一套词汇。持久图的节点一直带 `node_type`，问诊图只有
// `layer`——R16 让 `to_graph()` 也发 `node_type`（layer 是**布局**，node_type
// 是**这是什么东西**，两件事）。

// cytoscape 读不到 CSS 变量，只能在 JS 里取一次当前计算值。**颜色仍然只在
// app.css 的 :root 里定义一处**（总纲 §7 第 7 条），这里是把它读出来交给
// cytoscape，不是第二处定义。取不到时回落到 fallback：图不至于变透明，
// 而"取不到"本身在断网/测试环境下是正常的（node 里没有 getComputedStyle）。
// **不带兜底色值。** 写一个 fallback 就是把那个颜色在 JS 里又定义了一遍——
// `--verified` 的值恰好等于叶天士的身份色，写成兜底之后
// `test_the_frontend_injects_them_instead_of_hard_coding` 当场红（实测），
// 而那条断言是对的：身份色的唯一来源是 core/physicians.py，语义色的唯一来源
// 是 app.css 的 :root，JS 里一处都不该有。
//
// 取不到就返回 null，调用方用 `pick()` 把这一条样式整个略掉，cytoscape 用它
// 自己的默认值。取不到只发生在没有 CSS 的环境（node 测试），那里本来也不渲染。
function cssVar(name) {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || "").trim() || null;
  } catch (e) {
    return null;
  }
}

// 值为 null 的样式项整个去掉——cytoscape 收到 null 会当成非法值报错。
function pick(styleObj) {
  const out = {};
  for (const [k, v] of Object.entries(styleObj)) {
    if (v !== null && v !== undefined && v !== "") out[k] = v;
  }
  return out;
}

// node_type → 一组颜色。**分色分形的唯一定义**（§3.2 规格 7）：
// 形状在下面的 shape 规则里，颜色在这里，两张图都读这一份。
function graphPalette() {
  return {
    symptom: { bg: cssVar("--surface-2"), border: cssVar("--edge"),
               text: cssVar("--ink") },
    element: { bg: cssVar("--accent-bg"), border: cssVar("--verified"),
               text: cssVar("--verified") },
    syndrome: { bg: cssVar("--surface"), border: cssVar("--ink-2"),
                text: cssVar("--ink") },
    formula: { bg: cssVar("--surface"), border: cssVar("--ink-2"),
               text: cssVar("--ink") },
    herb: { bg: cssVar("--surface-2"), border: cssVar("--edge"),
            text: cssVar("--ink") },
    case: { bg: cssVar("--caution-bg"), border: cssVar("--caution"),
            text: cssVar("--caution") },
    // R42：九层的四族 + 焦点档。**色只有深浅差别，一个新色相都没加**
    // （理由见 app.css 那段令牌注释）。
    input: { bg: cssVar("--node-input-bg"), text: cssVar("--node-input-fg") },
    reason: { bg: cssVar("--node-reason-bg"), text: cssVar("--node-reason-fg") },
    focal: { bg: cssVar("--node-focal-bg"), text: cssVar("--node-focal-fg") },
    treat: { bg: cssVar("--node-treat-bg"), text: cssVar("--node-treat-fg") },
    rx: { bg: cssVar("--node-rx-bg"), text: cssVar("--node-rx-fg") },
  };
}

//: 层号 → 视觉族。**跟 app.css 那段令牌注释是同一张表**（那边是为什么，
//: 这边是怎么做）。第 8 层（君臣佐使）用 rx 族但字号最小、在方剂框内——
//: 它不是独立的一族，是 rx 族里轻的那一档。
const LAYER_FAMILY = {
  0: "input", 1: "reason", 2: "reason", 3: "focal", 4: "reason",
  5: "treat", 6: "treat", 7: "rx", 8: "rx",
};

//: 每一层的字重。焦点层（证型）与方剂层加粗，其余常规——
//: **层级的主要载体是字号（LAYER_FONT_SIZE）与底色，字重只是补足**。
const LAYER_FONT_WEIGHT = { 3: 600, 7: 600 };

//: 每一层的边框粗细。证型层 2px 是"这是结论"的信号；其余 1px。
const LAYER_BORDER_WIDTH = { 3: 2 };

// §3.2 规格 11：节点 label 13px 黑体，证素 15px 宋体 600。
// 证素比别的节点大一号是因为**它是图谱浏览器的枢纽**（首屏只铺证素），
// 问诊图上它也是"症状收敛到哪里"的那一层。
const NODE_FONT_SIZE = 13;
const ELEMENT_FONT_SIZE = 15;

// R37：**两张图的 label 宽度分开定**（`slot` = "consult" | "browser"）。
//
// 为什么必须分开：两张图对同一个数字的诉求相反。问诊图一屏只有一条链、
// label 越完整越好（`病名 · 证型` 拼起来能有十几个字）；图谱浏览器一屏要摆
// 十几二十个证型节点，label 一宽就摆不下——R28 正是因为 label 变成两行、
// 最宽 124px，才把一次展开的上限从 20 砍到 14（见 GB_EXPAND_CAP 的注释）。
// 一个写死的 90px 同时服务两张图，等于让其中一张永远将就另一张。
//
// **但它管不住中文名字的宽度**：`text-wrap: wrap` 只在空白/换行处断行，
// 「疫毒炽盛（急黄）证」里没有断行机会，这个上限对它无效（R37 实测：收到
// 84px 之后最宽标签仍是 125px）。它真正收窄的是重名时补的 `（病名 编码）`
// 那一行——那行里有空格。**不要拿这个令牌当"标签变窄了"的依据**去放宽
// 一次展开的上限，R37 就是这么错过一次的（见 GB_EXPAND_CAP）。
//
// 值从 CSS 令牌取（`--label-max-consult` / `--label-max-browser`），取不到时
// 回落到 R28 之前那个 90px——**回落值写在这里而不是分散在调用点**。
const LABEL_MAX_FALLBACK = "90px";

function labelMaxFor(slot) {
  return cssVar(slot === "browser" ? "--label-max-browser" : "--label-max-consult")
    || LABEL_MAX_FALLBACK;
}

// 字号同理绑到槽位：投影仪上（1280×800）问诊图的字要大一点才看得清，
// 而浏览器那张图节点多、字大了就压字。R37 起两张图各取自己那一档。
function nodeFontFor(slot) {
  const v = cssVar(slot === "browser" ? "--node-font-browser" : "--node-font-consult");
  const n = Number.parseFloat(v || "");
  return Number.isFinite(n) && n > 0 ? n : NODE_FONT_SIZE;
}

function elementFontFor(slot) {
  const v = cssVar(slot === "browser" ? "--element-font-browser" : "--element-font-consult");
  const n = Number.parseFloat(v || "");
  return Number.isFinite(n) && n > 0 ? n : ELEMENT_FONT_SIZE;
}

function buildStylesheet({ physicianColors = null, slot = "consult" } = {}) {
  const pal = graphPalette();
  const style = [
    {
      selector: "node",
      style: pick({
        // is_category（证候表里的类目节点）用菱形，其余圆角矩形。用函数值
        // 而不是选择器匹配布尔字段——避免对 cytoscape 选择器语法里"布尔真值
        // 怎么写"做不必要的猜测。
        shape: (ele) => (ele.data("is_category") ? "diamond" : "round-rectangle"),
        label: "data(label)",
        "text-valign": "center",
        "text-halign": "center",
        "font-size": nodeFontFor(slot),
        "font-family": cssVar("--font-ui"),
        color: pal.symptom.text,
        "background-color": pal.symptom.bg,
        "border-width": 1,
        "border-color": pal.symptom.border,
        padding: "6px",
        width: "label",
        height: "label",
        "text-wrap": "wrap",
        "text-max-width": labelMaxFor(slot),
      }),
    },
    // 按 node_type 分色。**两张图读的是同一组规则**——问诊图的 layer 1 和
    // 浏览器的 element 现在是同一个 node_type，写一遍就够。
    {
      selector: 'node[node_type = "element"]',
      style: pick({
        "background-color": pal.element.bg, "border-color": pal.element.border,
        color: pal.element.text,
        "font-size": elementFontFor(slot),
        "font-family": cssVar("--font-classic"),
        "font-weight": 600,
      }),
    },
    {
      selector: 'node[node_type = "syndrome"]',
      style: pick({ "background-color": pal.syndrome.bg, "border-color": pal.syndrome.border,
                    color: pal.syndrome.text }),
    },
    {
      selector: 'node[node_type = "case"]',
      style: pick({ "background-color": pal.case.bg, "border-color": pal.case.border,
                    color: pal.case.text }),
    },
    // 残差辨证补上的症状/证素，跟初轮推出来的分开标（朱砂虚线）。
    {
      selector: 'node[layer = 0][state = "residual"]',
      style: pick({ "background-color": cssVar("--danger-bg"),
                    "border-color": cssVar("--danger"), "border-width": 2 }),
    },
    {
      selector: "node[residual]",
      style: pick({ "border-color": cssVar("--danger"), "border-width": 2,
                    "border-style": "dashed" }),
    },
    {
      selector: "edge[residual]",
      style: pick({ "line-color": cssVar("--danger"),
                    "target-arrow-color": cssVar("--danger"), "line-style": "dashed" }),
    },
    // 一个症状都没被解释：黄褐虚线。跟残差的朱砂分开——"补上了"和"还没解释"
    // 是两件事。
    {
      selector: 'node[layer = 0][state = "unexplained"]',
      style: pick({ "background-color": cssVar("--caution-bg"),
                    "border-color": cssVar("--caution"),
                    "border-style": "dashed", "border-width": 2 }),
    },
    {
      // M5：方剂（layer 3）是 compound 父节点，:parent 是 cytoscape 内建伪类，
      // 匹配"带子节点的节点"，不用按 layer 另判一次。父节点框要半透明——
      // 不透明会把里面的药材（子节点）整个盖住看不见。
      selector: "node:parent",
      style: {
        "background-opacity": 0.12,
        "border-width": 2,
        "text-valign": "top",
        "text-halign": "center",
        // **跟 dagre 留位用的那个常量绑在一起**（见 COMPOUND_PAD 的注释）：
        // 这里画多少、那边就得至少留多少，写两个字面量就是等着它们漂。
        padding: `${COMPOUND_RENDER_PAD}px`,
      },
    },
    // §3.2 规格 1：方剂框按来源区分边框。**用的是既有的 `source` 字段**
    // （`FormulaCandidate.source` 早就是 Literal["classic","modified","composed"]），
    // 不新建一个同义的 `source_kind`——那会是同一个概念的第二处实现。
    // classic 走默认实线，不用另写规则。
    { selector: 'node[node_type = "formula"][source = "modified"]',
      style: { "border-style": "dashed" } },
    { selector: 'node[node_type = "formula"][source = "composed"]',
      style: { "border-style": "dotted" } },
    { selector: "node.gb-search-hit", style: { "border-width": 3 } },
    // R42：聚焦模式里"被聚焦的那一个"。**用描边加粗，不改填充**——填充色在这张
    // 图上承担的是节点类型，改了就等于把它伪装成另一类节点。
    { selector: "node.gb-focus-root",
      style: pick({ "border-width": 4, "border-color": cssVar("--verified") }) },
    {
      selector: "edge",
      style: pick({
        width: 1.4,
        "line-color": cssVar("--edge-soft"),
        "target-arrow-color": cssVar("--edge-soft"),
        "target-arrow-shape": "triangle",
        // R42：问诊图的边改成 taxi（正交折线），浏览器那张图仍用 bezier。
        //
        // **为什么分开**：taxi 只在"分层有向图"上讲得通——它把边画成"先横走、
        // 到中间转折、再横走"，读起来就是"这一层流到下一层"。dagre 的分层布局
        // 正好是这个形状，两者是一套。而图谱浏览器那张图是同心圆/力导向，
        // 节点没有"层"的概念，taxi 的直角折线在上面会画出一堆莫名的拐弯。
        //
        // 顺带解决一个 bezier 的老毛病：九层之后同一对层之间会有多条平行的边，
        // bezier 把它们画成几条几乎重合的弧，看不出有几条；taxi 的折线会在
        // 中间的转折带上错开（`taxi-turn` 给的就是那条转折带的位置）。
        "curve-style": slot === "browser" ? "bezier" : "taxi",
        // rankdir=LR，所以边的主方向是"向右"。
        "taxi-direction": "rightward",
        // 转折带放在两层之间的正中（50%）。`taxi-turn-min-distance` 太小会让
        // 相邻两层的折线贴到节点边上，2px 是 cytoscape 默认值偏小，抬到 10。
        "taxi-turn": "50%",
        "taxi-turn-min-distance": 10,
        "arrow-scale": 0.7,
        opacity: 0.85,
      }),
    },
  ];

  // R42：九层的视觉层级。**放在 node_type 规则之后**——问诊图的节点同时带
  // `layer` 和 `node_type`，层级规则要盖过按类型的配色；浏览器那张图没有
  // `layer` 字段，这一批规则对它一条都不命中（所以两张图仍然是一份样式表）。
  //
  // 为什么按 layer 而不按 node_type：layer 是**布局位置**，而视觉层级说的正是
  // "这一列在链条上处在哪一步"。按 node_type 写的话，「脏腑」和「病性」是两个
  // 类型但同一族，得写两条一样的规则（同一个值写两遍就是下一次只改一遍的开始）。
  if (slot !== "browser") {
    for (const [layerStr, family] of Object.entries(LAYER_FAMILY)) {
      const layer = Number(layerStr);
      const fam = pal[family] || {};
      style.push({
        selector: `node[layer = ${layer}]`,
        style: pick({
          "background-color": fam.bg,
          color: fam.text,
          "font-size": layerFontSize(layer),
          "font-weight": LAYER_FONT_WEIGHT[layer] || 400,
          "border-width": LAYER_BORDER_WIDTH[layer] || 1,
          "border-color": cssVar("--rule"),
        }),
      });
    }
  }

  // R42：**医家色从"填充"降为"描边"，而且只在只有一位医家时出现。**
  //
  // 改之前是 `node[layer > 1][phys = "…"]` 整块填成医家色——那套规则服务的是
  // 医家分带（三条并行的带子，颜色要跟三列的顶边对得上）。分带去掉之后同名
  // 节点会**合并**，一个证型节点可能是三位医家共同给出的，那时"填成谁的色"
  // 这个问题没有答案。
  //
  // 但"这一步是谁给的"这个信息**不能因此丢掉**（能力不删）。所以：
  //
  //   只有一位贡献者 → 用那位医家的色描边（填充仍归层级族，层级不被打乱）；
  //   多位贡献者     → 双线边框，意思是"几位医家给出了同一个结论"。
  //
  // 两个标记（`contributor_solo` / `multi_contributor`）由后端在建完图之后统一
  // 算（`api.main.to_graph` 的收尾那一段）——**不在前端按 contributors.length
  // 现判**：同一个判断放在两处，改一边的时候另一边不会跟着动。
  for (const [phys, color] of Object.entries(physicianColors || {})) {
    style.push({
      selector: `node[contributor_solo = "${phys}"]`,
      style: { "border-color": color, "border-width": 2 },
    });
  }
  style.push({
    selector: "node[?multi_contributor]",
    style: pick({ "border-style": "double", "border-width": 4,
                  "border-color": cssVar("--ink-2") }),
  });

  // selected/safety_blocking 放在医家配色之后：医家配色也会设 border-color，
  // 样式表按声明顺序层叠，选中框和安全拦截的红框必须始终盖过医家色。
  style.push({
    // [?field] 是 cytoscape 的"布尔真值"选择器，跟内建的 :selected（用户交互
    // 选中状态）是两回事——这里选的是我们自己的数据字段 data.selected。
    selector: "node[?selected]",
    style: { "border-width": 3 },
  });
  style.push({
    selector: "node[?safety_blocking]",
    style: pick({ "border-color": cssVar("--danger"), "border-width": 3 }),
  });

  // 淡化放最后：它的 opacity 必须盖过上面任何规则设的值（§3.4 的 0.25）。
  style.push({ selector: "node.gt-faded", style: { opacity: FADED_OPACITY } });
  style.push({ selector: "edge.gt-faded", style: { opacity: FADED_OPACITY } });
  return style;
}

// ---------- hover tooltip ----------
//
// 跟点击打开的侧栏（openEvidence/EVIDENCE）是两件事：侧栏给的是完整证据链
// （推理过程、引用医案、取证轨迹……），要点开才看；hover 给的是"这个节点/
// 这条边是什么"的一句话速览，划过去就有，不用点。数据不查 EVIDENCE——
// EVIDENCE 是按"这条结论的完整依据"组织的，字段比这里需要的重得多，
// hover 直接读 cytoscape 节点/边自身的 data() 就够，两者故意不复用同一份
// 组装逻辑，因为回答的不是同一个问题（同一条例外，CLAUDE.md 对
// core/safety.py 危重词表不并进 SYNONYMS 是同一个理由）。
//
// R42：**分派顺序反过来了，这是一个 bug 修复。**
//
// 改之前是 `if (data.node_type) { …持久图… } else { …问诊图… }`。但问诊图的节点
// **从 R16 起也带 node_type**（那一轮让两张图共用一份样式表，见 buildStylesheet
// 的注释），于是问诊图上 node_type 撞上持久图那几个值的节点（symptom/syndrome…）
// 全都走进了持久图那一支——表现是证型节点的 tooltip 里没有医家、方剂/药材层
// 压根没有 tooltip 分支可走。单看任一侧的代码都看不出来：两支各自都对。
//
// 现在按 `layer` 在不在分派：**`layer` 是问诊图独有的**（持久图没有层的概念），
// 所以它是这两种数据形状的可靠判据，而 node_type 是两边共有的词汇。
function describeNodeTooltip(data) {
  if (data.layer !== undefined && data.layer !== null) {
    return describeConsultNodeTooltip(data);
  }
  if (data.node_type) {
    switch (data.node_type) {
      case "symptom":
        return `<b>症状</b>　${escapeHtml(data.label)}`;
      case "element": {
        const catLabel = data.category === "location" ? "病位" : data.category === "nature" ? "病性" : (data.category || "");
        return `<b>证素</b>　${escapeHtml(data.label)}<div class="tt-meta">${escapeHtml(catLabel)}</div>`;
      }
      case "syndrome": {
        const lines = [`<b>证型</b>　${escapeHtml(data.label)}${data.is_category ? "　（类目）" : ""}`];
        // 病名单独一行。**174 个证候里 52 个重名**（「肝郁气滞证」分属腹痛/胁痛/
        // 积聚/癃闭，病机各不同），label 里的括号是给图上一眼区分用的，
        // 这一行是给"到底属于哪个病"一个明确的位置。
        // 17 条国标条目没有 disease——那时**不出这一行**，不留一个空的「病名：」。
        if (data.disease) lines.push(`<div class="tt-meta">病名：${escapeHtml(data.disease)}</div>`);
        if (data.definition) lines.push(`<div class="tt-meta">${escapeHtml(data.definition)}</div>`);
        if (data.tongue_pulse) lines.push(`<div class="tt-meta">${escapeHtml(data.tongue_pulse)}</div>`);
        return lines.join("");
      }
      case "case":
        return `<b>医案</b>　${escapeHtml(data.label)}`;
      default:
        return escapeHtml(data.label || data.id || "");
    }
  }
  return escapeHtml(data.label || data.id || "");
}

//: 贡献这个节点的医家名。**R42 之后一个节点可以有多位**（同名证型合并成一个
//: 节点，谁给的记在 `contributors` 里），所以这里返回的是一串，不是一个。
//: 回落到 `pname` 是为了兼容只带单个医家名的旧数据（审计日志回放）。
function contributorNames(data) {
  const ids = data.contributors || [];
  if (ids.length) return ids.map((x) => PHYSICIAN_NAMES[x] || x);
  const one = data.pname || PHYSICIAN_NAMES[data.phys] || data.phys || "";
  return one ? [one] : [];
}

//: 问诊图九层各自的 tooltip 细节。**标题用后端下发的 `layer_label`**
//: （`api.main.CHAIN_LAYERS` 的中文名），所以加一层/改一个层名不用改前端。
//: 这张表只管"除了标题之外还要说什么"，查不到就只显示标题 + 标签。
function describeConsultNodeTooltip(data) {
  const title = data.layer_label || data.node_type || "节点";
  const who = contributorNames(data);
  const meta = [];
  switch (data.node_type) {
    case "symptom": {
      meta.push({ explained: "已解释", residual: "残差辨证补充解释",
                  unexplained: "未解释" }[data.state] || data.state || "");
      break;
    }
    case "organ":
    case "nature": {
      meta.push(data.node_type === "organ" ? "病位" : "病性");
      if (data.residual) meta.push("（残差辨证补充）");
      break;
    }
    case "syndrome": {
      if (data.disease) meta.push(`病名：${data.disease}`);
      break;
    }
    case "pathogenesis": {
      if (data.organ) meta.push(`病位：${data.organ}`);
      break;
    }
    case "formula": {
      meta.push(formulaSourceLabel(data.source));
      if (data.selected) meta.push("★已选");
      if (data.safety_blocking) meta.push("⚠ 安全拦截");
      break;
    }
    case "herb": {
      // 这里的 role 是 HerbItem 自己的字段（君/臣/佐/使），跟角色权限那个 role
      // 只是同名（同 to_graph 里那条注释）。
      if (data.role) meta.push(`${data.role}药`);
      if (data.dose != null) meta.push(`${data.dose}${data.unit || ""}`);
      if (data.decoction) meta.push(data.decoction);
      break;
    }
    default:
      break;
  }
  // 医家放在最后一项：R42 去掉医家分带之后，"谁给的"是节点属性而不是位置，
  // 而**属性里它的分量低于"这是什么"**——放在最前面会让读者又按医家去读图。
  if (who.length) meta.push(who.join("、"));
  const metaLine = meta.filter(Boolean).join("　");
  // 没有 label 时退到 id：**显示一个陌生的 id 好过显示空白**（至少能搜）。
  // 这条兜底是给旧审计记录回放用的——现在的 to_graph 每个节点都有 label。
  const out = [`<b>${escapeHtml(title)}</b>　${escapeHtml(data.label || data.id || "")}`];
  if (metaLine) out.push(`<div class="tt-meta">${escapeHtml(metaLine)}</div>`);
  // 方中作用是一整句自由文本（"疏肝理气，为本方主药"），塞进上面那行会跟
  // 一串短词混在一起看不清断句——单独一行（M7 的原话）。
  if (data.function_in_formula) {
    out.push(`<div class="tt-meta">${escapeHtml(data.function_in_formula)}</div>`);
  }
  return out.join("");
}


function describeEdgeTooltip(edgeData, sourceLabel, targetLabel, currentPhysician) {
  if (edgeData.edge_type) {
    // 持久知识图谱的边：indicates（症状->证素）带 is_cardinal/λ1，
    // composes（证素->证型）不带 per-physician 权重，两者分开描述。
    const lines = [`${escapeHtml(sourceLabel || "?")} → ${escapeHtml(targetLabel || "?")}　<span class="tt-meta">(${escapeHtml(edgeData.edge_type)})</span>`];
    if (edgeData.edge_type === "indicates") {
      lines.push(`<div class="tt-meta">${edgeData.is_cardinal ? "主症" : "次症"}</div>`);
      if (currentPhysician && edgeData.lambda1_by_physician) {
        const l1 = edgeData.lambda1_by_physician[currentPhysician];
        if (l1 !== undefined) {
          lines.push(`<div class="tt-meta">λ1（${escapeHtml(PHYSICIAN_NAMES[currentPhysician] || currentPhysician)}）= ${l1.toFixed(2)}</div>`);
        }
      }
    }
    return lines.join("");
  }
  const physName = edgeData.pname || PHYSICIAN_NAMES[edgeData.phys] || edgeData.phys || "";
  const lines = [`${escapeHtml(sourceLabel || "?")} → ${escapeHtml(targetLabel || "?")}`];
  if (physName) lines.push(`<div class="tt-meta">${escapeHtml(physName)}</div>`);
  if (edgeData.residual) lines.push('<div class="tt-meta">残差辨证补充的连线</div>');
  return lines.join("");
}

// panelId/tooltipId 可传：图谱浏览器（模块7）的画布是另一个 cytoscape 实例、
// 另一个容器，要用同一套定位/显示逻辑但挂在不同的 DOM 节点上，不为它另写
// 一份 showTooltip/hideTooltip——默认值就是原来 per-consult 图那两个 id，
// 老调用方不用改。
// R41 **消除布局抖动**：读、写、再读、再写 → 读完再写、第二次读挪进 rAF。
//
// 改之前这个函数是教科书式的 layout thrashing：
//   写 innerHTML → 写 class → **读 getBoundingClientRect** → 写 style.left/top
// 中间那次读会强制浏览器**同步**跑一遍布局（forced synchronous layout），
// 而这个函数挂在图谱的 mouseover 上——鼠标在图上划一下就是几十次同步布局，
// 每一次都在输入事件的处理里，直接表现为拖动图谱时的手感发涩。
//
// 改之后：
//   1. 先读**不依赖 tooltip 内容**的那几个量（画布的位置与尺寸）；
//   2. 写内容，但先别显示（`.measuring` 让它可量、不可见——`visibility:hidden`
//      仍然参与布局，所以量得到尺寸；`display:none` 量不到）；
//   3. 第二次读（tooltip 自己的尺寸）与最终定位放进 rAF：**从输入事件里搬出去**，
//      事件处理函数立刻返回。
// 代价是 tooltip 晚一帧出现（16.7 ms），换掉的是每次 mouseover 一次同步布局。
// ---------- R42：tooltip 钉住 + 无障碍 ----------
//
// ## 为什么要能钉住
//
// hover tooltip 有三种人用不了它：
//   1. 触屏——没有 hover 这个事件，手指一离开就没了；
//   2. 只用键盘的人——鼠标划过去这件事他做不到；
//   3. 要**照着 tooltip 里的内容打字**的人（把剂量抄进病历）——手一移开就没了，
//      这是三甲实际用法里最常见的一种。
//
// 所以加一个"钉住"：点节点时 tooltip 留在原地，`aria-live` 让读屏软件念出来，
// Esc 或点空白处取消。**钉住状态下 hover 不再改动它**——否则鼠标经过下一个
// 节点就把钉住的内容换掉，"钉住"就没有意义了。
//
// ## 无障碍这三样是最小集，不是装饰
//
//   `role="status"` + `aria-live="polite"`：内容变化时读屏软件会念，
//      polite 而不是 assertive——它不是警报，不该打断正在念的句子。
//   `tabindex="0"` + `aria-label`（画布上）：键盘能聚焦到画布，
//      聚焦后按 Enter 钉住当前节点的释义（见 app.js 的键盘处理）。
//   `aria-hidden` 随显隐切换：隐藏时不该被读屏软件读到（它仍在 DOM 里）。
let _tooltipPinned = false;

function tooltipPinned() {
  return _tooltipPinned;
}

function pinTooltip(tooltipId = "graph-tooltip") {
  _tooltipPinned = true;
  const tip = typeof document !== "undefined" ? document.getElementById(tooltipId) : null;
  if (tip) tip.classList.add("pinned");
}

function unpinTooltip(tooltipId = "graph-tooltip") {
  _tooltipPinned = false;
  const tip = typeof document !== "undefined" ? document.getElementById(tooltipId) : null;
  if (tip) tip.classList.remove("pinned");
}

//: 从 cytoscape 事件里取指针坐标。**`evt.originalEvent` 不保证存在**——
//: 合成事件（以及 `ele.emit("mouseover")` 触发的那一类）没有原生事件对象，
//: 裸读 `.clientX` 当场 TypeError，而它发生在 hover 处理里，表现是"鼠标划过
//: 图的时候整页的 JS 挂了"。两张图（问诊图 / 图谱浏览器）共用这一处。
function eventXY(evt) {
  const e = evt && evt.originalEvent;
  return [(e && e.clientX) || 0, (e && e.clientY) || 0];
}

function showTooltip(html, clientX, clientY, panelId = "graph-panel", tooltipId = "graph-tooltip",
                     { force = false } = {}) {
  // 钉住之后 hover 不许改它（见上面那段）。`force=true` 是点击那条路径用的
  // ——点一个新节点应该换成新节点的内容，那是用户明确的动作。
  if (_tooltipPinned && !force) return;
  const tip = document.getElementById(tooltipId);
  const panel = document.getElementById(panelId);
  // ① 读：这三个量跟 tooltip 的内容无关，所以能在写之前读完。
  const panelRect = panel.getBoundingClientRect();
  const panelW = panel.clientWidth;
  const panelH = panel.clientHeight;
  // ② 写：内容 + "可量但不可见"。
  tip.innerHTML = html;
  tip.classList.add("measuring");
  tip.classList.add("show");
  // 读屏软件要能念到它（role/aria-live 写在 index.html 上，这里只切换可见性）。
  tip.setAttribute("aria-hidden", "false");
  // ③ 下一帧再读自己的尺寸、定位、露出来。
  requestAnimationFrame(() => {
    // 这一帧里 tooltip 可能已经被 hideTooltip 关掉了（鼠标划过去了）。
    if (!tip.classList.contains("show")) { tip.classList.remove("measuring"); return; }
    const tipRect = tip.getBoundingClientRect();
    let left = clientX - panelRect.left + 14;
    let top = clientY - panelRect.top + 14;
    left = Math.min(left, panelW - tipRect.width - 6);
    top = Math.min(top, panelH - tipRect.height - 6);
    tip.style.left = `${Math.max(6, left)}px`;
    tip.style.top = `${Math.max(6, top)}px`;
    tip.classList.remove("measuring");
  });
}

function hideTooltip(tooltipId = "graph-tooltip", { force = false } = {}) {
  // 钉住的 tooltip 不被 mouseout 关掉——那正是"钉住"要解决的事。
  if (_tooltipPinned && !force) return;
  const tip = document.getElementById(tooltipId);
  if (!tip) return;
  tip.classList.remove("show");
  tip.classList.remove("pinned");
  // measuring 也要清：showTooltip 的 rAF 如果还没跑到就被关掉了，
  // 留着这个 class 会让下一次 show 出来是隐形的。
  tip.classList.remove("measuring");
  tip.setAttribute("aria-hidden", "true");
  if (force) _tooltipPinned = false;
}

// ---------- M7：症状→方剂路径高亮 ----------
//
// 纯函数，只读 nodes/edges（跟 computeLayout 同一份数据形状），不碰 cy 实例——
// 离线测试直接喂一份 graph JSON 断言返回的节点/边集合，不用起真实浏览器。
//
// "完整链路"而不是"直连"：起点症状只连着证素（层1），不会直接连到证型或
// 方剂——如果只高亮 sym 节点的直接邻居，学生看到的还是"症状=证素"这一步，
// 看不出这条证素牵动了哪些证型、哪些方剂。所以要顺着边一路往下游走，每一跳
// 都把新到达的节点纳入"已高亮"集合再继续找下一跳的邻居，直到症状(0)->
// 证素(1)->证型(2)->方剂(3) 四层全部走完（三跳）。
//
// 特意停在方剂（层3），不下探到药材（层4）：层4 药材是方剂内部的组成，不是
// "症状牵动的下一个推理结论"，而且层3方剂本身是 compound 父节点，视觉上已经
// 把它的药材整体框在一起了，框被高亮时药材跟着看得见，不需要再单独判它在不在
// 路径上——四步链路的字面意思到"方剂"为止（对齐任务描述原文），不是五步。
// 总纲 §3.4：点症状三跳高亮到方剂层，**其余 0.25 透明度、220ms**。
// 两个数字都只定义一处：cytoscape 的样式表和 CSS 是两套系统，同一个视觉
// 约定在两边各写一遍，改一边另一边不会报错、只会看起来不一样。
const FADED_OPACITY = 0.25;
// 220ms 跟 CSS 的 --t-highlight 是同一个约定。cytoscape 读不到 CSS 变量，
// 所以这里只能是个数字——但它必须跟令牌同值，有一条测试钉住这件事。
const HIGHLIGHT_MS = 220;

//: 高亮**走到方剂层为止**。
//:
//: 这个交互要说明的是"这个症状把几家推到了哪几个方子上"，不是"这个症状连着
//: 哪些药"——所以方剂层进集合、但不从它再往下展开。
//:
//: **判据从"三跳"改成"到这个 node_type 为止"**（R42）：原来写死 `hop < 3`，
//: 那正好是五层时代的 症状→证素→证型→方剂；九层之后同一条链是
//: 症状→脏腑→证型→治则→（治法靶位→）方剂，三跳只到治则，方剂反而被淡掉，
//: 表现是"点了症状，方子暗了"。而 `node_type` 是稳定的词，层号不是——
//: 下一轮再加一层，这条不用改。
//: `HIGHLIGHT_MAX_HOPS` 只是防环的兜底，不是产品判据。
const HIGHLIGHT_STOP_TYPES = new Set(["formula", "herb"]);
const HIGHLIGHT_MAX_HOPS = 24;

function computeHighlightPath(nodes, edges, startId) {
  const typeOf = new Map();
  for (const n of nodes || []) {
    const d = n.data || {};
    if (d.id) typeOf.set(d.id, d.node_type);
  }
  const highlighted = new Set([startId]);
  let frontier = new Set([startId]);
  for (let hop = 0; hop < HIGHLIGHT_MAX_HOPS && frontier.size; hop++) {
    const next = new Set();
    for (const e of edges) {
      if (frontier.has(e.data.source) && !highlighted.has(e.data.target)) {
        next.add(e.data.target);
      }
    }
    for (const id of next) highlighted.add(id);
    // 到方剂层就不再往下展开（进集合，但不做下一跳的起点）。
    frontier = new Set([...next].filter((id) => !HIGHLIGHT_STOP_TYPES.has(typeOf.get(id))));
  }
  const edgeIds = new Set();
  for (const e of edges) {
    if (highlighted.has(e.data.source) && highlighted.has(e.data.target)) {
      edgeIds.add(`${e.data.source}::${e.data.target}`);
    }
  }
  return { nodeIds: highlighted, edgeIds };
}

// 当前处于高亮状态的症状节点 id；null 表示没有高亮。用来实现"再点取消"——
// 再点同一个症状节点时判出这是"关闭"而不是"换一个症状重新高亮"。
let highlightedSymptomId = null;

function applyPathHighlight(nodeIds, edgeIds) {
  if (!cy) return;
  // 淡化要有过渡（§2.4 允许的三处动效之一：路径高亮 220ms）。瞬间切换的话
  // "哪些被淡掉了"这件事没有任何视觉线索，整张图像是换了一张。
  cy.nodes().forEach((n) => {
    n.style("transition-property", "opacity");
    n.style("transition-duration", `${HIGHLIGHT_MS}ms`);
    n.toggleClass("gt-faded", !nodeIds.has(n.id()));
  });
  cy.edges().forEach((e) => {
    const key = `${e.data("source")}::${e.data("target")}`;
    e.style("transition-property", "opacity");
    e.style("transition-duration", `${HIGHLIGHT_MS}ms`);
    e.toggleClass("gt-faded", !edgeIds.has(key));
  });
}

function clearPathHighlight() {
  if (cy) cy.elements().removeClass("gt-faded");
  highlightedSymptomId = null;
}

function handleSymptomClick(nodeId) {
  if (highlightedSymptomId === nodeId) {
    clearPathHighlight();
    return;
  }
  if (!lastGraph) return;
  const { nodeIds, edgeIds } = computeHighlightPath(lastGraph.nodes, lastGraph.edges, nodeId);
  applyPathHighlight(nodeIds, edgeIds);
  highlightedSymptomId = nodeId;
}

function ensureCanvas() {
  // 画布只建一次。生长动画需要 cy.add() 往已有画布上分批追加元素，
  // 每次 destroy 重建就没有"增量"可言了。
  if (cy) return cy;
  cy = cytoscape({
    container: document.getElementById("cy"),
    elements: [],
    // 问诊图按医家染色（三列的身份色要在图上对得上）；浏览器那张不传这个参数。
    style: buildStylesheet({ physicianColors: PHYSICIAN_COLORS }),
    layout: { name: "preset" },
    userZoomingEnabled: true,
    userPanningEnabled: true,
    boxSelectionEnabled: false,
  });
  cy.on("tap", "node", (evt) => {
    const n = evt.target;
    graphHooks.onOpenEvidence(n.id());
    // M7：只有症状节点（层0）触发路径高亮——点其他层级的节点应该只是照旧
    // 打开证据侧栏，不应该顺带清空/改变当前的路径高亮状态（那样点一下证型
    // 节点看侧栏，画面上的高亮却跟着消失，会很意外）。
    if (Number(n.data("layer")) === 0) handleSymptomClick(n.id());
  });
  cy.on("tap", (evt) => {
    if (evt.target === cy) {
      graphHooks.onCloseEvidence();
      clearPathHighlight();
      // 点空白处取消钉住（触屏上这是唯一的"退出"手势）。
      hideTooltip("graph-tooltip", { force: true });
    }
  });
  // R42：**点节点 = 钉住**。触屏没有 hover，键盘用户也到不了 hover；而"照着
  // tooltip 抄剂量"是三甲最常见的用法，手一移开就没了等于不能用。
  // 先 force 显示（点一个新节点要换成新节点的内容），再置钉住位。
  cy.on("tap", "node", (evt) => {
    unpinTooltip();
    showTooltip(describeNodeTooltip(evt.target.data()), ...at(evt),
                "graph-panel", "graph-tooltip", { force: true });
    pinTooltip();
  });
  // R42：`evt.originalEvent` **不保证存在**。cytoscape 的合成事件（以及代码里
  // `ele.emit("mouseover")` 触发的那一类）没有原生事件对象，裸读 `.clientX`
  // 当场 TypeError——而它发生在 hover 处理里，表现是"鼠标划过图的时候整页的
  // JS 挂了"。Playwright 那条钉住 tooltip 的状态第一次跑就撞上了这个。
  // 一处兜底函数（`eventXY`，模块级），不在六个调用点各写一遍 `|| {}`。
  const at = eventXY;
  cy.on("mouseover", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), ...at(evt));
  });
  cy.on("mousemove", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), ...at(evt));
  });
  // 直接把 hideTooltip 当回调传，cytoscape/DOM 会把它们自己的事件对象当第一个
  // 参数传进来，正好落进 hideTooltip(tooltipId=...) 那个默认参数的位置，把
  // "graph-tooltip" 这个默认值顶掉——包一层箭头函数、不透传参数，才是真的调用
  // "无参版本"。
  cy.on("mouseout", "node", () => hideTooltip());
  for (const kind of ["mouseover", "mousemove"]) {
    cy.on(kind, "edge", (evt) => {
      const e = evt.target;
      showTooltip(
        describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label")),
        ...at(evt)
      );
    });
  }
  cy.on("mouseout", "edge", () => hideTooltip());
  // 兜底：鼠标整个离开画布这件事单独用原生 DOM mouseleave 盯一道，不完全
  // 依赖 cytoscape 自己那套"逐帧 hit-test 出有没有悬停元素"的合成事件。
  // 真实验证时发现，鼠标从节点直接"瞬移"到画布外（不经过中间帧）的极端
  // 情况下，cytoscape 的 node mouseout 有几率没触发，tooltip 会卡住不消失
  // ——真实鼠标移动是连续多帧、不会瞬移，用户手动移开基本不会撞上这条，
  // 但原生 mouseleave 是浏览器对"指针真的离开了这个元素"的保证，不依赖
  // canvas 内部逐帧算出来的悬停状态，加一道更保险。
  document.getElementById("cy").addEventListener("mouseleave", () => hideTooltip());
  // R42 无障碍：画布可聚焦（index.html 给了 tabindex/aria-label），
  // **Esc 取消钉住**。Esc 挂在 document 上而不是画布上——钉住之后焦点可能已经
  // 移到 tooltip 里（读屏软件会把它当一个 status 区域读），那时画布上的
  // keydown 收不到。
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && tooltipPinned()) hideTooltip("graph-tooltip", { force: true });
  });
  return cy;
}

// ---------- 模块7：图谱浏览器（持久知识图谱，跟上面 per-consult 图是两回事）----------
//
// GET /api/graph 一次性把整张图（symptom/element/syndrome，这个 sandbox 里没有
// case 层）都发过来。**这个"一次装得下"的前提已经过期**：写这段注释时是 123
// 个节点、377 条边，AutoDL 上的 data/graph.json 现在是 2451 节点 / 4147 边，
// 教材五本扩完（证候 337 -> 1444、症状 1282 -> 约 7000）还要再翻几倍。
// 服务端分页是 api/main.py 的契约变更，留到教材扩充那一轮一起做；这一轮先在
// 前端侧止血（见下面 GB_MAX_NEW_NODES / GB_ELEMENT_LIMIT / GB_COSE_MAX_NODES）。"渐进式展开"因此是纯前端的
// 显示策略：拿到全量数据后先只画证型节点，点开才把它连着的证素/症状加进
// 画布，不是一次性把 123 个节点全铺开——那样会是一团看不出结构的乱线。

let gbCy = null;
let gbGraphData = null;         // 已经从服务端拿到的那部分图（第一页 + 展开/搜索并进来的）
let gbIndex = null;             // { nodeById, edgesByNode }，拿到数据后建一次，避免每次展开都线性扫全部边
let gbVisibleIds = new Set();   // 当前画布上已经显示的节点 id
let gbCurrentPhysician = null;
let gbShowCaseLayer = false;    // 只有 has_case_layer 为真时这个开关才有意义（按钮本身也只在那时才显示）

function ensureGraphBrowserCanvas() {
  if (gbCy) return gbCy;
  gbCy = cytoscape({
    container: document.getElementById("gb-cy"),
    elements: [],
    // 跟问诊图同一份样式表，只是不传 physicianColors、并且槽位是 browser
    // （label 宽度与字号按槽位取，见 labelMaxFor 的注释）
    style: buildStylesheet({ slot: "browser" }),
    // per-consult 图（cy）用 preset 布局：症状/证素/病名证型/方剂/药材天然分层
    // （药材是方剂的 compound 子节点），坐标是 computeLayout() 算好摆的。
    // 这张图没有那种天然分层——点开才逐步长大，
    // 每次新增节点后都要重新摆位，cose 力导向布局适合这种"节点集合会变"的场景。
    layout: { name: "preset" },
    userZoomingEnabled: true,
    userPanningEnabled: true,
    boxSelectionEnabled: false,
  });
  gbCy.on("tap", "node", (evt) => gbExpandNode(evt.target.id()));
  gbCy.on("mouseover", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), ...eventXY(evt), "gb-panel", "gb-tooltip");
  });
  gbCy.on("mousemove", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), ...eventXY(evt), "gb-panel", "gb-tooltip");
  });
  gbCy.on("mouseout", "node", () => hideTooltip("gb-tooltip"));
  gbCy.on("mouseover", "edge", (evt) => {
    const e = evt.target;
    showTooltip(
      describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label"), gbCurrentPhysician),
      ...eventXY(evt), "gb-panel", "gb-tooltip"
    );
  });
  gbCy.on("mousemove", "edge", (evt) => {
    const e = evt.target;
    showTooltip(
      describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label"), gbCurrentPhysician),
      ...eventXY(evt), "gb-panel", "gb-tooltip"
    );
  });
  gbCy.on("mouseout", "edge", () => hideTooltip("gb-tooltip"));
  document.getElementById("gb-cy").addEventListener("mouseleave", () => hideTooltip("gb-tooltip"));
  // R42：**双击 = 聚焦**。单击已经是"展开/收起"（gbExpandNode），不能抢；
  // 双击在这张图上原来没有任何行为，正好空着。
  gbCy.on("dbltap", "node", (evt) => { gbFocus(evt.target.id()); });
  // 面包屑用事件委托挂一次，不给每一格各挂一个——每次聚焦都会换掉整串
  // （同 #columns / 九段那两处 delegation 的理由）。
  const crumbs = document.getElementById("gb-breadcrumb");
  if (crumbs && !crumbs.dataset.bound) {
    crumbs.dataset.bound = "1";
    crumbs.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-gb-crumb]");
      if (btn) gbBreadcrumbTo(Number(btn.dataset.gbCrumb));
    });
  }
  return gbCy;
}

function gbBuildIndex() {
  const nodeById = new Map();
  for (const n of gbGraphData.graph.nodes) nodeById.set(n.data.id, n);
  const edgesByNode = new Map();
  // R43：**边 id 的集合跟着索引一起长期留着**，不在每次合并时重建。
  // 改之前 `gbMergeGraph` 每次都 `new Set(全部边.map(e => e.data.id))`——
  // 那是 O(已累积的边数)，而一次浏览会合并很多次（首屏、每次展开、每次搜索），
  // 合起来是 O(边数 × 合并次数)。本沙盒的持久图是 2356 节点 / 3682 边，
  // 教材扩完还要翻几倍，而这段代码跑在**点击的处理函数里**——正是 INP 的
  // "processing" 那一段。
  const edgeIds = new Set();
  for (const e of gbGraphData.graph.edges) {
    const { source, target } = e.data;
    edgeIds.add(e.data.id);
    if (!edgesByNode.has(source)) edgesByNode.set(source, []);
    if (!edgesByNode.has(target)) edgesByNode.set(target, []);
    edgesByNode.get(source).push(e);
    edgesByNode.get(target).push(e);
  }
  gbIndex = { nodeById, edgesByNode, edgeIds };
}

function gbApplyPhysicianWeighting() {
  if (!gbCy) return;
  gbCy.edges().forEach((ele) => {
    const l1map = ele.data("lambda1_by_physician");
    let opacity = 1;
    if (l1map && gbCurrentPhysician && l1map[gbCurrentPhysician] !== undefined) {
      // λ1 目前恒为 0（原因如实写在 gb-lambda1-note 里）。opacity 直接等于 λ1
      // 的话全部边会变成完全透明，图看起来像渲染坏了——用一个下限 + 线性映射，
      // 数字依然如实反映 λ1（0 时最淡但看得见，1 时最实），不是在掩盖那个 0。
      opacity = 0.25 + 0.75 * l1map[gbCurrentPhysician];
    }
    ele.style("opacity", opacity);
  });
}

// F2/F3/F4：规模上限。这个模块当初是按"123 个节点、377 条边"写的，
// AutoDL 上的 data/graph.json 已经是 2451 节点 / 4147 边，教材五本扩完
// （证候 337 -> 1444、症状 1282 -> 约 7000）还要再翻几倍。三个上限缺一不可：
//   - 一次最多加多少个节点（gbSearch 搜"痛"能命中几百个症状）
//   - 初始铺多少个证型（原来是一次性把全部证型加进画布再跑 cose 力导向，
//     337 个互不相连的节点已经很勉强）
//   - 超过多少个节点就不再跑 cose（力导向对大图是二次复杂度，会卡住浏览器）
const GB_MAX_NEW_NODES = 150;
const GB_COSE_MAX_NODES = 200;
// 证素总数是数据决定的（实测 20），limit 给一个宽裕的上限而不是写死 20——
// 教材扩充后多出来的证素不该静默消失。
const GB_ELEMENT_LIMIT = 200;

// 首屏那批枢纽节点（证素）的 id。concentric 布局按这个集合分内外圈：
// 枢纽在内圈、展开出来的在外圈。**不按 node_type 分圈**——同一个 node_type
// 既可能是枢纽（首屏的证素）也可能是展开出来的（从证型再往外的证素），
// 按"是不是首屏来的"分才对得上"点一层长一圈"这个心智模型。
let gbHubIds = new Set();
// 已经展开过的节点 → 它展开出来的那批 id。再点一次就按这张表收起。
const gbExpanded = new Map();
let gbTotalElements = 0;

function gbSetHint(text) {
  const el = document.getElementById("gb-empty-hint");
  if (el) el.textContent = text;
}

// 新增节点/边只加不减（不做"点一下收起"）：这一轮的范围是"渐进式展开"，
// 没有要求可折叠；加一套折叠状态管理会把这个模块的复杂度顶到跟本身价值
// 不成比例，需要的话留给下一轮。
function gbAddNodes(nodeIds) {
  ensureGraphBrowserCanvas();
  const toAdd = [];
  let truncated = 0;
  for (const id of nodeIds) {
    if (toAdd.length >= GB_MAX_NEW_NODES) { truncated += 1; continue; }
    if (gbVisibleIds.has(id)) continue;
    const n = gbIndex.nodeById.get(id);
    if (!n) continue;
    // 医案层没打开时不显示 case 节点——has_case_layer 为假时按钮本身都不出现
    // （见 loadGraphBrowserData），这里再挡一道是防"从别的节点展开时，边的
    // 另一端恰好是 case 节点"这种间接泄漏。
    if (n.data.node_type === "case" && !gbShowCaseLayer) continue;
    toAdd.push({ data: n.data });
    gbVisibleIds.add(id);
  }
  if (toAdd.length) gbCy.add(toAdd);

  // 两端都已经在画布上的边才补上——判据跟 api/main.py::to_graph() 的
  // add_edge 是同一个道理（两端节点都存在，边才有意义），不是巧合。
  // F4：候选边只从**这次新加的节点**的邻接表里取（gbIndex.edgesByNode 建好
  // 就是干这个的），不再每次都全量扫 graph.edges——原来是 O(每次调用 ×
  // 总边数)，还对每条边做一次 getElementById。
  const edgesToAdd = [];
  const seenEdgeIds = new Set();
  for (const n of toAdd) {
    for (const e of gbIndex.edgesByNode.get(n.data.id) || []) {
      const { id, source, target } = e.data;
      if (seenEdgeIds.has(id)) continue;
      if (!gbVisibleIds.has(source) || !gbVisibleIds.has(target)) continue;
      if (gbCy.getElementById(id).nonempty()) continue;
      seenEdgeIds.add(id);
      edgesToAdd.push({ data: e.data });
    }
  }
  if (edgesToAdd.length) gbCy.add(edgesToAdd);

  if (toAdd.length || edgesToAdd.length) {
    gbApplyPhysicianWeighting();
    gbRelayout();
  }
  if (truncated > 0) {
    const status = document.getElementById("gb-search-status");
    if (status) {
      status.textContent = `本次只加了 ${toAdd.length} 个节点，还有 ${truncated} 个未显示——请把搜索词写得更具体`;
    }
  }
  document.getElementById("gb-empty-hint").hidden = gbVisibleIds.size > 0;
}

// F1：展开改成问服务端。
// 原来是在本地全量邻接表上展开——图一分页，本地就没有全量邻接表了，展开会
// **静默只展开"恰好在本页里"的那部分**。那不是没找到，是没找过，比报错更误导。
//: 请求超过这个时间还没回来，就把状态文案升一级。**不是加载动画**——
//: 300 ms 以下的等待人感觉不到，弹一个转圈反而制造"刚才是不是卡了"的印象；
//: 超过它才需要告诉人"还在等"。300 ms 是 Nielsen 三档里 0.1 s（瞬时）与
//: 1 s（不打断思路）之间的实用分界。
const GB_PENDING_HINT_MS = 300;

//: 每一类请求"正在做什么"的话。**说清在做什么，不写一个笼统的「加载中」**：
//: 三甲内网上这几件事的耗时差一个量级，而人只有看到"在搜索"还是"在展开"
//: 才知道该不该继续等。
const GB_BUSY_TEXT = {
  search: "搜索中…",
  expand: "展开中…",
  layer: "切换图层中…",
};

//: 请求序号。**后发的响应才算数**——连点两次搜索时，先回来的那个不该覆盖
//: 后点的那次的状态文案（同 `openNodeExplain` 的 `NODE_EXPLAIN_SEQ`，
//: 那是同一类竞态的另一个入口，两处都得有）。
let gbFetchSeq = 0;

async function gbFetchInto(url, statusText, pick, busyKey) {
  const status = document.getElementById("gb-search-status");
  const seq = ++gbFetchSeq;
  // R43：**点下去立刻有反馈。** 改之前状态栏要等响应回来才变——在三甲内网上
  // 点「搜索」之后按钮看起来是死的，而"点了没反应"正是这个项目一直在防的
  // 那种失败（只不过这一次不是静默出错，是静默等待）。
  const busy = GB_BUSY_TEXT[busyKey] || "加载中…";
  let slow = null;
  if (status) {
    status.textContent = busy;
    status.setAttribute("aria-busy", "true");
    slow = setTimeout(() => {
      if (seq === gbFetchSeq) status.textContent = `${busy}（这张图比较大，稍候）`;
    }, GB_PENDING_HINT_MS);
  }
  const done = () => {
    if (slow) clearTimeout(slow);
    if (status) status.setAttribute("aria-busy", "false");
  };
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    // `pick` 让调用方在**并进画布之前**先筛一遍（展开时只留前 20 个，见
    // GB_EXPAND_CAP）。做成钩子而不是让调用方自己 fetch：错误处理、状态栏
    // 文案、gbMergeGraph 这三件事只能有一处实现，各写一份的话"加载失败"
    // 会有两种表现。
    const picked = pick ? pick(data.graph, data.page) : {graph: data.graph, dropped: 0};
    gbMergeGraph(picked.graph);
    data.dropped = picked.dropped || 0;
    done();
    // **过期响应不改文案**：连点两次时先回来的那个不该覆盖后点的那次。
    if (status && statusText && seq === gbFetchSeq) {
      status.textContent = statusText(data.page, data.dropped);
    }
    return data;
  } catch (err) {
    done();
    if (status && seq === gbFetchSeq) status.textContent = `加载失败：${err.message || err}`;
    return null;
  }
}

// 把服务端发来的一页并进本地缓存 + 画布。gbIndex 从"全图索引"变成"已加载的
// 那部分的缓存"——语义变了但形状没变，gbBuildIndex() 仍是唯一一处建索引的地方。
function gbMergeGraph(graph) {
  if (!graph) return;
  for (const n of graph.nodes || []) {
    if (!gbIndex.nodeById.has(n.data.id)) gbGraphData.graph.nodes.push(n);
    gbIndex.nodeById.set(n.data.id, n);
  }
  const seen = gbIndex.edgeIds;      // R43：长期留着，不每次重建（见 gbBuildIndex）
  for (const e of graph.edges || []) {
    if (seen.has(e.data.id)) continue;
    seen.add(e.data.id);
    const { source, target } = e.data;
    if (!gbIndex.edgesByNode.has(source)) gbIndex.edgesByNode.set(source, []);
    if (!gbIndex.edgesByNode.has(target)) gbIndex.edgesByNode.set(target, []);
    gbIndex.edgesByNode.get(source).push(e);
    gbIndex.edgesByNode.get(target).push(e);
    gbGraphData.graph.edges.push(e);
  }
  gbAddNodes((graph.nodes || []).map((n) => n.data.id));
}

// R16 §3.2 规格 6：点证素 → 拉它的证型放外圈；点证型 → 拉症状放更外圈；
// **再点收起**。
//
// 收起这件事 R13 那版刻意没做（"加一套折叠状态管理跟本身价值不成比例"）。
// 规格改了之后它是必需的：首屏 20 个证素，每个展开出十几个证型，点开三四个
// 就又变成一屏摊平的方块——跟 F6 要治的是同一个病。
//
// 收起只删**这次展开新加的**那批（gbExpanded 记着），不是删所有邻居：
// 一个证型可能同时挂在两个证素下面，按邻居删会把另一个证素展开的东西也删掉。
// 展开是**分层**的（§3.2 规格 6），不是"把全部邻居倒出来"。这张表是那条
// 规格的唯一定义：证素 → 证型，证型 → 症状，症状 → 证素（从一个症状反查
// "它指向哪些病位病性"，是学生最常问的那个方向）。
//
// 不筛类型的后果实测过：证素「肝」有 **499** 个邻居，其中 61 个证型、其余基本
// 都是症状。点一下就是 150 个症状铺满画布——跟 R16 要治的 F6 是同一个病。
const GB_EXPAND_TARGET = {
  element: "syndrome",
  syndrome: "symptom",
  symptom: "element",
};

const GB_TYPE_LABEL = { element: "证素", syndrome: "证型", symptom: "症状", case: "医案" };

// R24 补丁：**一次展开最多画 20 个**。
//
// 上限不是"服务端给多少就画多少"（那是 GB_MAX_NEW_NODES=150 的止血阀，
// 量级完全不同）。**这个数是画布定的，标签一变大它就得跟着变小**：
//   R24 补丁：20（当时证型标签约 100×50px）
//   R28：**14**——加了病名限定之后标签变成两行、最宽 124px，
//        20 个再也摆不下：实测压字 2~4 对、而且 fit 会把字缩到 11.9px（判据要 ≥12）。
// 少画几个不丢信息：状态栏照旧报"还有 N 个，搜索直达"。
// **宁可少画也不压字**——一张认不出字的图等于没画。
// 实测见 screenshot_states 的 rings 判据（两两比包围盒 + 字号下限）。
//
// 取哪 20 个：**按该证型自己的症状数从多到少**（服务端算好的 `n_symptoms`，
// 见 api/main.py 的 _symptom_counts_by_syndrome_code）。同分按 id 排，
// 保证"同一次点击两次结果一样"——按加载顺序取前 20 看起来也有理由，
// 其实取决于 networkx 的遍历顺序，那不是理由。
//   R37：**试过回到 20，量出来不行，退回 14。** 理由不是"看着挤"：
//        1) `--label-max-browser: 84px` 收不住中文名字（`text-wrap` 只在空白
//           处断行），实测最宽标签仍然是 125px——"标签变窄了"这个前提是假的；
//        2) 1280×800 下真浏览器逐对量包围盒（`--only rings` 的判据）：
//           20 个压字 2 对、19 个 1 对、18 个 4 对、17 个 7 对、16 个 6 对
//           （少反而更差：14 个以上会从两排扇面变成三排，三排的径向间距
//           放不下 125×41 的标签）；14 个 0 对。
//        3) 面积也对得上：这个扇面约 11.2 万 px²，一个位置要 124×62≈7,700px²,
//           理想排布也就 14 个上下——20 个是几何上就放不下，不是参数没调好。
const GB_EXPAND_CAP = 14;

function gbSymptomCount(node) {
  const n = node && node.data ? Number(node.data.n_symptoms) : 0;
  return Number.isFinite(n) ? n : 0;
}

/** 把一页邻居裁到 cap 个。返回 {graph, dropped}，**纯函数**（可在 node 里逐条断言）。 */
function gbCapExpansion(graph, cap) {
  const nodes = (graph && graph.nodes) || [];
  const edges = (graph && graph.edges) || [];
  if (!cap || nodes.length <= cap) return {graph: {nodes, edges}, dropped: 0};
  const sorted = [...nodes].sort((a, b) =>
    gbSymptomCount(b) - gbSymptomCount(a) || String(a.data.id).localeCompare(String(b.data.id)));
  const keep = sorted.slice(0, cap);
  const dropIds = new Set(sorted.slice(cap).map((n) => n.data.id));
  return {
    graph: {
      nodes: keep,
      // 被裁掉的节点的边一起裁掉，否则画布上会留下指向不存在节点的半条边。
      edges: edges.filter((e) => !dropIds.has(e.data.source) && !dropIds.has(e.data.target)),
    },
    dropped: dropIds.size,
  };
}

/** 展开后状态栏那句话。**"还有 N 个"必须说出来**：不说的话用户以为
    这个证素就这 20 个证型，而那是个静默的谎。 */
function gbExpandStatusText(page, dropped, wantLabel) {
  if (!page || page.total === 0) return `这个节点下没有${wantLabel}`;
  if (dropped > 0) {
    return `展开了 ${page.returned - dropped} 个${wantLabel}（按症状数排序取前 ${GB_EXPAND_CAP} 个）`
      + `——还有 ${dropped + (page.total - page.returned)} 个，搜索直达；再点一次收起`;
  }
  if (page.truncated) {
    return `这个节点有 ${page.total} 个${wantLabel}，只展开了前 ${page.returned} 个——再点一次收起`;
  }
  return `展开了 ${page.returned} 个${wantLabel}——再点一次收起`;
}

async function gbExpandNode(nodeId) {
  if (gbExpanded.has(nodeId)) {
    gbCollapseNode(nodeId);
    return;
  }
  const node = gbIndex.nodeById.get(nodeId);
  const want = GB_EXPAND_TARGET[node && node.data.node_type];
  // 医案层节点（或别的没登记的类型）不做分层展开，照旧倒全部邻居——
  // 医案跟国标层之间实测 0 条边，本来也倒不出什么（λ1 恒为 0 那件事）。
  const typeParam = want ? `&node_types=${encodeURIComponent(want)}` : "";
  const wantLabel = GB_TYPE_LABEL[want] || "关联";
  const before = new Set(gbVisibleIds);
  const data = await gbFetchInto(
    `/api/graph/neighbors?node=${encodeURIComponent(nodeId)}&limit=${GB_MAX_NEW_NODES}${typeParam}`,
    (page, dropped) => gbExpandStatusText(page, dropped, wantLabel),
    (graph) => gbCapExpansion(graph, GB_EXPAND_CAP),
    "expand"
  );
  if (!data) return;
  const added = [...gbVisibleIds].filter((id) => !before.has(id));
  // 一个新节点都没加 = 这个节点的邻居早就都在画布上了。不登记，否则下一次点
  // 它会"收起"一批其实不是它展开出来的节点。
  if (added.length) gbExpanded.set(nodeId, added);
  gbRelayout();
}

function gbCollapseNode(nodeId) {
  const added = gbExpanded.get(nodeId) || [];
  gbExpanded.delete(nodeId);
  for (const id of added) {
    // 别人也展开出过它就留着——它现在属于那一支。
    if ([...gbExpanded.values()].some((ids) => ids.includes(id))) continue;
    // 它自己展开过东西，先把那一层收掉（否则会留下一批悬空节点）。
    if (gbExpanded.has(id)) gbCollapseNode(id);
    gbCy.getElementById(id).remove();
    gbVisibleIds.delete(id);
  }
  const status = document.getElementById("gb-search-status");
  if (status) status.textContent = `收起了 ${added.length} 个关联节点`;
  gbRelayout();
}

// R16 §3.2 规格 5：**concentric**——枢纽（首屏那批证素）在内圈，展开出来的
// 在外圈。concentric 的 `concentric` 回调返回的数越大越靠内。
//
// 为什么不是 cose：cose 是力导向，它摆出来的位置取决于连边的拉扯，
// "谁是枢纽"这件事在图上看不出来。而这张图的整个心智模型就是"从证素往外长"。
// 节点多到 cose 会卡的时候仍然退回 grid（那条上限没变）。
//
// **R24：收成正好两环。** 原来是三档（枢纽 3 / 枢纽展开的 2 / 更深的 1），
// 于是屏幕上可能出现三四个半径相近的环——而"这个节点在第几环"本来是要一眼
// 读出"它离枢纽多远"的，环一多就读不出来了，反而像是一团同心圆噪声。
// 现在只有两环：**是枢纽 / 不是枢纽**。代价是"深两层"这个信息不再体现在半径上
// ——它体现在交互里（是你自己一层层点开的，收起按钮也按这个结构给），
// 而交互里的信息比一个读不准的半径可靠。
// 环的含义在画布下方那行图例里写着（`gbRingLegendText`），不靠人猜。
// R24 补丁：两环还是两环，但**位置自己算**（preset），不再交给 concentric。
//
// concentric 的三个毛病在 r24_rings.png 上一次看全了：
//   1. 内圈半径是它按"圈上节点数 × minNodeSpacing"算的，20 个枢纽挤成中间
//      一个点——"谁是枢纽"这件事在图上就不存在了；
//   2. 展开出来的节点摊成**整圆**，于是"这 20 个是从哪个证素点开的"看不出来；
//   3. 画布是 2:1 的横幅，正圆用不掉横向那一半地方，纵向却已经挤不下。
// 这三件都不是参数能调出来的（concentric 不接受半径下限、扇形范围、椭圆），
// 所以位置改成自己算。**布局规则因此变成纯函数，可以在 node 里逐条断言。**
//
// 两环的语义没变：内圈 = 枢纽证素，外圈 = 其余全部（不论展开了几层）。
// 变的是外圈节点的**角度**——它落在"把它展开出来的那个枢纽"的扇面里（±40°）。
const GB_INNER_RADIUS_MIN_RATIO = 0.18;   // 内圈短半轴 ≥ 画布短边的 0.18
const GB_FAN_HALF_DEG = 40;               // 扇面半角
const GB_FAN_ROW_MAX = 7;                 // 一排最多几个，超了往里再起一排
const GB_HUB_ZIG = 1.22;                  // 相邻枢纽交替往外错开的倍数
const GB_OUTER_RATIO = 0.45;              // 外圈椭圆占画布的比例（0.44 时连标签一起算会超出画布，触发 fit 缩小）

/** 内圈椭圆的两个半轴。短半轴卡死 ≥ 0.18×短边——**这是"不许塌成一点"那条规格**。 */
function gbInnerRadii(width, height) {
  const short = Math.max(1, Math.min(width, height));
  // 贴着规格下限一点点（0.19 > 0.18），不往上加：内环每多占 10px，
  // 外圈那 20 个证型标签就少 10px 的活动空间，而挤的是外圈不是内环
  // （证素标签一两个字，证型标签五六个字还会 wrap 成两行）。
  const ry = short * 0.181;
  // 横幅画布上正圆会浪费横向空间，所以按宽高比拉成椭圆；上限 0.28×宽度，
  // 免得内圈顶到外圈上。
  const rx = Math.min(width * 0.28, ry * (width / Math.max(1, height)));
  return {rx: Math.max(rx, ry), ry};
}

/** 枢纽在内圈上的角度。**正在展开的那个转到正右方**（0 弧度）。

    这不是"好看一点"：画布是 2:1 的横幅，横向可用半径是纵向的两倍，
    而一个 ±40° 的扇面能不能放下 20 个带标签的节点**完全取决于它指向哪边**
    ——朝上那个扇面的面积只有朝右的四分之一（算过：3.4 万 px² vs 13 万 px²，
    而 20 个证型标签要 5.7 万）。朝上就必然压字，朝右就绰绰有余。
    没有展开时从正上方起排，跟人读表的顺序一致。 */
function gbHubAngles(hubIds, focusHubId) {
  const n = Math.max(1, hubIds.length);
  const step = (2 * Math.PI) / n;
  const idx = hubIds.indexOf(focusHubId);
  const base = idx >= 0 ? -idx * step : -Math.PI / 2;
  return new Map(hubIds.map((id, i) => [id, base + i * step]));
}

/** 扇面里第 i 个位置的角度 + 半径缩放。**分排 + 按需张开**。

    两个数是量出来的，不是估的（1280×800 下真浏览器量 `renderedBoundingBox`）：
    一个证型标签 wrap 到 90px 上限之后约 100×50px，所以一个位置要留
    106×58 才不压字。

    **±40° 是设计默认值，不是硬上限。** 这一点必须写明白：20 个这么大的标签
    塞进一个 ±40° 的扇面，几何上放不下——那个扇面的面积约 4.8 万 px²，
    而 20 个标签要 10 万。硬守 40° 的结果只有一个：字压字。所以扇面按需张开，
    上限 ±75°（150° < 360°，环上仍然留着一大片空白，"这些是从这个证素点开的"
    这件事照样读得出来）。节点少的时候（≤7 个）它就老老实实是 ±40°。 */
const GB_SLOT_V = 62;              // 一个位置要留多高（R28 实测标签最高 51px：病名另起一行之后是两行）
const GB_SLOT_H = 124;             // 要留多宽（R28 实测最宽 124px：带病名限定的证型）
const GB_FAN_HALF_DEG_MAX = 90;    // 张开的上限：±90° = 半圈，再宽就退化成整圈

/** 一排在给定半径和张角下能放几个。**内排比外排短，就该少放几个**——
    每排一样多是上一版没通过判据的直接原因：外排还宽松，内排已经压字了。 */
function gbRowCapacity(ry, scale, halfRad) {
  return Math.max(1, Math.floor((2 * ry * scale * Math.sin(halfRad)) / GB_SLOT_V) + 1);
}

function gbFanSlots(centerAngle, n, outer, inner, halfDegBase = GB_FAN_HALF_DEG) {
  if (n <= 0) return [];
  const rx = Math.max(1, (outer && outer.rx) || 400);
  const ry = Math.max(1, (outer && outer.ry) || 200);
  // 内环最外那一圈（错开出去的那一半）才是扇面要让开的东西。
  const innerRy = ((inner && inner.ry) || 0) * GB_HUB_ZIG;
  const rows = n <= GB_FAN_ROW_MAX ? 1 : (n <= GB_FAN_ROW_MAX * 2 ? 2 : 3);
  // 最内一排必须让开内环：内环半轴 + 半个位置（内环上是证素，标签一两个字、
  // 只有半个位置高）。这里每多留 10px，排与排之间就少 10px——而实测压字压的是
  // 排与排之间（相邻两排的标签横向撞上），不是内排撞内环。
  const minScale = Math.min(0.92, Math.max(0.5, (innerRy + GB_SLOT_V / 2) / ry));
  const step = rows > 1 ? Math.min(GB_SLOT_H / rx, (1 - minScale) / (rows - 1)) : 0;
  const scales = [];
  for (let r = 0; r < rows; r += 1) scales.push(1 - (rows - 1 - r) * step);
  // 张角：从设计默认值 ±40° 起，不够放就一档档张开到 ±75°。
  let halfRad = (halfDegBase * Math.PI) / 180;
  const maxRad = (GB_FAN_HALF_DEG_MAX * Math.PI) / 180;
  const capacityOf = (h) => scales.reduce((sum, sc) => sum + gbRowCapacity(ry, sc, h), 0);
  while (capacityOf(halfRad) < n && halfRad < maxRad) halfRad = Math.min(maxRad, halfRad + Math.PI / 36);
  // 每排先按容量分，**放不下的那几个加到最外排**（弧最长、最宽松）。
  // 上一版把余数丢给最内排，结果最内排挤成一团——判据当场抓到一对压着 28px
  // 的标签。"余数给谁"这种看起来无所谓的选择，落在最短的那条弧上就是压字。
  const counts = scales.map((sc) => gbRowCapacity(ry, sc, halfRad));
  let left = n - counts.reduce((a, b) => a + b, 0);
  while (left > 0) { counts[counts.length - 1] += 1; left -= 1; }
  let placed = 0;
  const slots = [];
  for (let r = rows - 1; r >= 0 && placed < n; r -= 1) {
    const room = Math.min(counts[r], n - placed);
    // 相邻两排错开**半格**：两排的半径差有限，真正把相邻两排的标签分开的是
    // 这个角度偏移。（试过 0.37 格这种"两两都不对齐"的写法，实测更差：
    // 偏移量一大，最外侧那个就顶到扇面边上去了，反而多压出三对。）
    const jitter = r % 2 && room > 1 ? halfRad / (room - 1) : 0;
    // 错开之后**把这一排的跨度收窄同样多**，让所有位置仍然落在 ±half 之内。
    // 不收窄的话最后一个会被推到扇面外面去——实测扇面跨度因此变成 184°，
    // 判据里那句"永远不摊成整圈"就开始靠运气。
    const spread = 2 * halfRad - jitter;
    for (let i = 0; i < room; i += 1) {
      const t = room === 1 ? 0.5 : i / (room - 1);
      slots.push({angle: centerAngle - halfRad + jitter + spread * t, scale: scales[r]});
    }
    placed += room;
  }
  return slots.slice(0, n);
}

/** 节点 id → 它属于哪个枢纽（顺着展开关系往上走）。找不到返回 null。 */
function gbHubOf(nodeId, parentOf, hubIds) {
  let cur = nodeId;
  for (let i = 0; i < 10 && cur; i += 1) {      // 10 层封顶，防数据成环时死循环
    if (hubIds.has(cur)) return cur;
    cur = parentOf.get(cur);
  }
  return null;
}

/** 算出每个节点的位置。**纯函数**：给定 id 和画布尺寸就能算，不碰 cytoscape。 */
function gbLayoutPositions(opts) {
  const hubIds = opts.hubIds || [];
  const fans = opts.fans || [];               // [{hubId, childIds}]
  const others = opts.others || [];
  const width = opts.width || 1000;
  const height = opts.height || 600;
  const cx = width / 2;
  const cy = height / 2;
  const inner = gbInnerRadii(width, height);
  const outer = {rx: width * GB_OUTER_RATIO, ry: height * GB_OUTER_RATIO};
  const pos = {};
  const hubAngle = gbHubAngles(hubIds, opts.focusHubId);
  hubIds.forEach((id, i) => {
    const a = hubAngle.get(id);
    // 相邻枢纽交替错开半径：切向间距够、但标签宽度不够时，
    // 错开半径是唯一不改变"它属于哪一环"又能让标签让开的办法。
    // **往外错不往内错**（1.22 而不是 0.78）：往内错会让一半的枢纽掉到
    // 「内环半径 ≥ 0.18×短边」这条规格线以下——实测 0.78 那版的最小半径是
    // 0.148×短边，判据当场红。往外错则每一个都 ≥ 基准半径。
    const zig = i % 2 ? GB_HUB_ZIG : 1;
    pos[id] = {x: cx + inner.rx * zig * Math.cos(a), y: cy + inner.ry * zig * Math.sin(a)};
  });
  for (const fan of fans) {
    const center = hubAngle.has(fan.hubId) ? hubAngle.get(fan.hubId) : -Math.PI / 2;
    const slots = gbFanSlots(center, fan.childIds.length, outer, inner);
    fan.childIds.forEach((id, i) => {
      const slot = slots[i];
      pos[id] = {
        x: cx + outer.rx * slot.scale * Math.cos(slot.angle),
        y: cy + outer.ry * slot.scale * Math.sin(slot.angle),
      };
    });
  }
  // 没有主人的外圈节点（搜索命中、按门类加进来的）：均匀铺在外圈上。
  others.forEach((id, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(1, others.length);
    pos[id] = {x: cx + outer.rx * Math.cos(a), y: cy + outer.ry * Math.sin(a)};
  });
  return pos;
}

function gbRelayout() {
  if (!gbCy) return;
  if (gbVisibleIds.size > GB_COSE_MAX_NODES) {
    gbCy.layout({ name: "grid", animate: false, fit: true, padding: 24 }).run();
    return;
  }
  const width = gbCy.width() || 1000;
  const height = gbCy.height() || 600;
  const parentOf = new Map();
  for (const [parent, kids] of gbExpanded.entries()) {
    for (const kid of kids) parentOf.set(kid, parent);
  }
  const visible = [...gbVisibleIds];
  const hubIds = visible.filter((id) => gbHubIds.has(id));
  const hubSet = new Set(hubIds);
  const byHub = new Map(hubIds.map((id) => [id, []]));
  const others = [];
  for (const id of visible) {
    if (hubSet.has(id)) continue;
    const hub = gbHubOf(id, parentOf, hubSet);
    if (hub && byHub.has(hub)) byHub.get(hub).push(id);
    else others.push(id);
  }
  const fans = [...byHub.entries()].map(([hubId, childIds]) => ({hubId, childIds}));
  // 焦点 = 展开出最多节点的那个枢纽（并列时取 id 小的，保证确定性）。
  const focus = fans.filter((f) => f.childIds.length)
    .sort((a, b) => b.childIds.length - a.childIds.length
      || String(a.hubId).localeCompare(String(b.hubId)))[0];
  const positions = gbLayoutPositions({
    hubIds, fans, others, width, height, focusHubId: focus ? focus.hubId : null,
  });
  gbCy.layout({
    name: "preset",
    animate: false,
    fit: false,                 // 位置本来就是按画布尺寸算的，再 fit 一次等于缩小
    positions: (ele) => positions[ele.id()] || {x: width / 2, y: height / 2},
  }).run();
  gbFitIfNeeded(24);
  gbRenderRingLegend();
}

/** 摆得下就 1:1 显示，摆不下才缩。**这一步决定标签能不能读**：
    cytoscape 的 fit 会连字号一起缩，20 个节点本来摆得下、被 fit 缩成 0.7 倍之后
    13px 的字就只剩 9px——判据里那条"字号 ≥ 12px"抓的正是这个。 */
function gbFitIfNeeded(padding) {
  const bb = gbCy.elements().boundingBox();
  const w = gbCy.width();
  const h = gbCy.height();
  if (bb.w + padding * 2 <= w && bb.h + padding * 2 <= h) {
    gbCy.zoom(1);
    gbCy.center();
  } else {
    gbCy.fit(undefined, padding);
  }
}

// R24：两环的图例文字。**环的含义必须写出来**——一张同心圆图上"内圈是什么"
// 如果要靠人猜，那这个布局就只是好看而没有信息。
// 数从画布现算，不另存一份计数（两处各存一份必然有一处忘了更新）。
function gbRingLegendText(nHub, nOuter) {
  if (!nHub && !nOuter) return "";
  return `内圈 ${nHub} 个证素枢纽　外圈 ${nOuter} 个展开出来的节点`
    + `（不论展开了几层都在外圈——深度在交互里，不在半径上）`;
}

function gbRenderRingLegend() {
  const el = document.getElementById("gb-ring-legend");
  if (!el || !gbCy) return;
  let nHub = 0;
  let nOuter = 0;
  gbCy.nodes().forEach((ele) => {
    if (gbHubIds.has(ele.id())) nHub += 1;
    else nOuter += 1;
  });
  el.textContent = gbRingLegendText(nHub, nOuter);
}

async function gbSearch(query) {
  const status = document.getElementById("gb-search-status");
  const q = (query || "").trim();
  if (gbCy) gbCy.nodes().removeClass("gb-search-hit");
  if (!q) { status.textContent = ""; return; }
  // F1：搜索也改成问服务端。本地只有已加载的那部分，在本地搜等于"只在画布上
  // 已经有的东西里找"——搜不到的时候用户会以为图里没有，其实是没搜过。
  const found = await gbFetchInto(
    `/api/graph/search?q=${encodeURIComponent(q)}&limit=${GB_MAX_NEW_NODES}`,
    (page) => page.total === 0
      ? "未找到匹配节点"
      : (page.truncated
          ? `找到 ${page.total} 个匹配节点，只显示前 ${page.returned} 个`
          : `找到 ${page.total} 个匹配节点`),
    null, "search"
  );
  if (!found || !found.page.total) return;
  const matches = found.graph.nodes;
  const matchedIds = new Set(matches.map((n) => n.data.id));
  const eles = gbCy.nodes().filter((ele) => matchedIds.has(ele.id()));
  eles.addClass("gb-search-hit");
  gbCy.animate({ fit: { eles, padding: 40 } }, { duration: 300 });
}

async function gbToggleLayer() {
  gbShowCaseLayer = !gbShowCaseLayer;
  document.getElementById("gb-layer-toggle").textContent =
    gbShowCaseLayer ? "切换到国标层" : "切换到医案层";
  if (!gbShowCaseLayer) {
    const caseIds = [...gbVisibleIds].filter((id) => gbIndex.nodeById.get(id)?.data.node_type === "case");
    for (const id of caseIds) {
      gbCy.getElementById(id).remove();
      gbVisibleIds.delete(id);
    }
    return;
  }
  // 医案层直接按类型取一页，不再"从可见节点展开出 case 邻居"。后者在这个项目上
  // 恒为空：case -evidences-> syndrome 的边实测是 0 条（医案用「胃阳虚」「悬饮」，
  // 国标用「肝胃不和证」，两套术语体系对不上——就是 λ1 恒为 0 那件事）。
  // 按邻居找等于永远显示不出医案层。
  await gbFetchInto(
    `/api/graph?node_types=case&limit=${GB_MAX_NEW_NODES}`,
    (page) => page.next_cursor !== null
      ? `医案层共 ${page.total} 条，显示前 ${page.returned} 条。`
        + `它们跟国标层之间没有边——这正是 λ1 恒为 0 的原因，不是没加载出来。`
      : `医案层 ${page.returned} 条`,
    null, "layer"
  );
}

// ---------- R42：图谱浏览器的「聚焦 + 分层」 ----------
//
// ## 为什么要加这个模式
//
// 同心双环回答的是"这张图上都有什么"——它是一张**概览**。但真正要看一个证型时
// 想问的是另一个问题：**"这一条是怎么连起来的"**（它由哪些证素组成、那些证素
// 又指向哪些症状）。在环上这个问题答不了：相关的节点散在环的不同角度上，
// 边跨过圆心互相交叉，读者得用眼睛把一条链从乱线里挑出来。
//
// 所以加一个聚焦模式：**双击一个节点 → 只看它的 k 跳邻域，按跳距分层**
// （dagre，跟问诊图那张一套）。层号 = 跳距，于是"它 → 它的直接邻居 → 邻居的
// 邻居"就是三列，链条从左往右读，跟九层图的读法一致。
//
// ## 两种布局都留着，按测量选，不是按口味选
//
// 概览留同心环、聚焦用 dagre。**不是"dagre 比环好"**——两者回答的不是同一个
// 问题（见上）。真正要量的是"dagre 在这个规模上够不够快"：dagre 对 N 个节点
// 是 O(V+E) 级的分层 + 排序，而同心环是纯三角函数、几乎不花时间。
// `window.__gbPerf.benchLayouts()` 把同一批节点分别喂给两者计时——
// 数字进报告，**不许只写"改用了 dagre"**（总纲 §12：先测量后优化）。
//
// ## 面包屑
//
// 聚焦会一层层往里走（点证型 → 点它的某个证素 → …），没有面包屑就只能靠
// "重置视图"退回最外层，中间那几步全丢。面包屑存的是**这条聚焦路径**，
// 点任一格回到那一步。
const GB_FOCUS_HOPS = 2;
//: 聚焦邻域的节点上限。**超了就截断并说出来**，不静默少给
//: （同 GB_MAX_NEW_NODES 那条理由）。"肝"这样的枢纽证素两跳能摸到上千个节点。
const GB_FOCUS_MAX_NODES = 120;

//: 聚焦路径（面包屑）。空 = 不在聚焦模式。
let gbFocusStack = [];
//: 进入聚焦前画布上有哪些节点——退出时原样恢复，而不是重铺首屏
//: （重铺会把用户展开了半天的那些节点全丢掉）。
let gbOverviewIds = null;

function gbInFocus() {
  return gbFocusStack.length > 0;
}

/** 以 rootId 为中心的 k 跳邻域。**无向 BFS**：证素→证型和证型→证素在这张图上
    都是"相关"，只走出边的话点一个证型什么都看不到（边的方向是
    症状→证素→证型，证型没有出边）。

    返回 `{ids, hop, truncated}`；`hop` 是 id → 跳距（层号就用它）。 */
function gbNeighborhood(rootId, hops = GB_FOCUS_HOPS, cap = GB_FOCUS_MAX_NODES) {
  const hop = new Map([[rootId, 0]]);
  let frontier = [rootId];
  let truncated = 0;
  for (let d = 1; d <= hops; d++) {
    const next = [];
    for (const id of frontier) {
      for (const e of (gbIndex.edgesByNode.get(id) || [])) {
        for (const other of [e.data.source, e.data.target]) {
          if (other === id || hop.has(other)) continue;
          const n = gbIndex.nodeById.get(other);
          if (!n) continue;
          // 医案层没打开时不显示 case 节点（同 gbAddNodes 那道防间接泄漏）。
          if (n.data.node_type === "case" && !gbShowCaseLayer) continue;
          if (hop.size >= cap) { truncated += 1; continue; }
          hop.set(other, d);
          next.push(other);
        }
      }
    }
    frontier = next;
  }
  return { ids: new Set(hop.keys()), hop, truncated };
}

/** 聚焦邻域的坐标。**层号 = 跳距**，然后交给 `computeLayout`（dagre）——
    跟问诊图**同一个布局函数**，不为浏览器另写一份（那就是同一个算法两处实现）。 */
function gbFocusPositions(nh) {
  const nodes = [...nh.ids].map((id) => {
    const n = gbIndex.nodeById.get(id);
    return { data: Object.assign({}, n ? n.data : { id },
                                 { id, layer: nh.hop.get(id) }) };
  });
  const edges = [];
  const seen = new Set();
  for (const id of nh.ids) {
    for (const e of (gbIndex.edgesByNode.get(id) || [])) {
      const { source, target } = e.data;
      if (!nh.ids.has(source) || !nh.ids.has(target)) continue;
      const key = `${source}>>${target}`;
      if (seen.has(key)) continue;
      seen.add(key);
      edges.push({ data: e.data });
    }
  }
  return { positions: computeLayout(nodes, edges), nodes, edges };
}

function gbFocus(nodeId) {
  if (!gbIndex || !gbIndex.nodeById.has(nodeId)) return null;
  ensureGraphBrowserCanvas();
  if (!gbInFocus()) gbOverviewIds = new Set(gbVisibleIds);
  gbFocusStack.push(nodeId);
  return gbRenderFocus();
}

function gbRenderFocus() {
  const rootId = gbFocusStack[gbFocusStack.length - 1];
  const nh = gbNeighborhood(rootId);
  const { positions, nodes, edges } = gbFocusPositions(nh);
  gbCy.elements().remove();
  gbVisibleIds = new Set(nh.ids);
  gbCy.add(nodes.map((n) => ({ group: "nodes", data: n.data })));
  gbCy.add(edges.map((e) => ({ group: "edges", data: e.data })));
  gbCy.layout({
    name: "preset", animate: false, fit: false,
    positions: (ele) => positions[ele.id()] || { x: 0, y: 0 },
  }).run();
  gbCy.getElementById(rootId).addClass("gb-focus-root");
  gbApplyPhysicianWeighting();
  gbFitIfNeeded(FIT_PADDING);
  renderGbBreadcrumb();
  // 环图例在聚焦模式下没有意义（没有内外圈了）——**换成聚焦自己的说明**，
  // 不是留着一句描述另一种布局的话。
  const legend = document.getElementById("gb-ring-legend");
  if (legend) legend.textContent = gbFocusLegendText(nh);
  gbSetHint("");
  return { n_nodes: nh.ids.size, truncated: nh.truncated,
           layout: dagreAvailable() ? "dagre" : "fallback" };
}

function gbFocusLegendText(nh) {
  const root = gbIndex.nodeById.get(gbFocusStack[gbFocusStack.length - 1]);
  const name = root ? (root.data.label || root.data.id) : "";
  const per = [];
  for (let d = 0; d <= GB_FOCUS_HOPS; d++) {
    per.push(`第 ${d} 列 ${[...nh.hop.values()].filter((x) => x === d).length} 个`);
  }
  return `聚焦「${name}」的 ${GB_FOCUS_HOPS} 跳邻域，按跳距分列（${per.join("　")}）`
    + (nh.truncated ? `；超过 ${GB_FOCUS_MAX_NODES} 个的部分没有显示（截断 ${nh.truncated} 个）` : "")
    + (dagreAvailable() ? "" : "；dagre 没加载上，退回等距铺开（可能重叠）");
}

function gbBreadcrumbTo(index) {
  if (index < 0) { gbExitFocus(); return null; }
  gbFocusStack = gbFocusStack.slice(0, index + 1);
  return gbRenderFocus();
}

function gbExitFocus() {
  gbFocusStack = [];
  renderGbBreadcrumb();
  if (!gbCy) return;
  gbCy.elements().remove();
  gbVisibleIds = new Set();
  // **恢复进聚焦之前那批节点**，不是重铺首屏（重铺会丢掉用户展开的那些）。
  const back = gbOverviewIds ? [...gbOverviewIds] : [];
  gbOverviewIds = null;
  if (back.length) gbAddNodes(back);
  else gbResetView();
}

function gbBreadcrumbHtml(stack) {
  if (!stack.length) return "";
  const crumbs = stack.map((id, i) => {
    const n = gbIndex && gbIndex.nodeById.get(id);
    const label = n ? (n.data.label || id) : id;
    const last = i === stack.length - 1;
    return `<button class="gb-crumb${last ? " is-current" : ""}"`
      + ` data-gb-crumb="${i}"${last ? " aria-current=\"page\"" : ""}>`
      + `${escapeHtml(String(label).replace(/\n/g, " "))}</button>`;
  });
  return '<button class="gb-crumb" data-gb-crumb="-1">全图</button>'
    + crumbs.join('<span class="gb-crumb-sep">›</span>');
}

function renderGbBreadcrumb() {
  const host = typeof document !== "undefined"
    ? document.getElementById("gb-breadcrumb") : null;
  if (!host) return "";
  const html = gbBreadcrumbHtml(gbFocusStack);
  host.innerHTML = html;
  host.hidden = !html;
  return html;
}

function gbResetView() {
  ensureGraphBrowserCanvas();
  gbCy.elements().remove();
  gbVisibleIds = new Set();
  // R42：聚焦态也要一起复位。不清的话点"重置视图"之后面包屑还挂在上面，
  // 而画布已经是全图了——面包屑指向一条不存在的路径。
  gbFocusStack = [];
  gbOverviewIds = null;
  renderGbBreadcrumb();
  document.getElementById("gb-search").value = "";
  document.getElementById("gb-search-status").textContent = "";
  // gbShowCaseLayer 也要一起复位，否则切到医案层之后点"重置视图"，
  // 按钮文字还写着"切换到国标层"，但画布上一个 case 节点都没有。
  gbShowCaseLayer = false;
  const layerBtn = document.getElementById("gb-layer-toggle");
  if (layerBtn) layerBtn.textContent = "切换到医案层";

  // F1：重置 = 重新铺首屏那批枢纽，不是从本地全量里切一刀（本地没有全量）。
  gbCursor = 0;
  gbExpanded.clear();
  gbMergeGraph(gbGraphData.graph);
  gbSetHint(`首屏是 ${gbVisibleIds.size} 个证素——点一个展开它的证型，`
    + `再点证型展开症状；再点一次收起。也可以用「按门类浏览」或上方搜索框。`);
}

// F1：翻页状态。cursor 是位置偏移，服务端按 networkx 的插入顺序切片，
// build_graph 是确定性写入的，所以同一份 graph.json 上这个顺序稳定。
let gbCursor = 0;

// R16 §3.2 规格 9：「加载更多证型」换成「按门类浏览」。
//
// 「加载更多」回答的是"再给我 80 个"——而用户想问的是"脾的证型有哪些"。
// 翻页在一堆互不相连的证型上没有意义：翻到第 3 页看到的还是一堆孤立方块。
//
// 门类 = 证候表的 `location` 字段（脾/胃/肝/肠/中焦……）。**图里已经有这一层**：
// build_graph 把每个 location 建成一个 category="location" 的证素节点，证型挂在
// 它下面。所以「按门类浏览」= 展开那个证素——**复用 gbExpandNode 这一条路径**，
// 不另写一套按门类拉数据的逻辑（CLAUDE.md 第 31 条）。
function populateGbCategorySelect(nodes) {
  const sel = document.getElementById("gb-category-select");
  if (!sel) return;
  sel.innerHTML = '<option value="">按门类浏览…</option>';
  for (const n of nodes || []) {
    if (n.data.category !== "location") continue;
    const opt = document.createElement("option");
    opt.value = n.data.id;
    opt.textContent = n.data.label;
    sel.appendChild(opt);
  }
  // 一个门类都没有时藏起来，不留一个只有占位项的空下拉。
  sel.hidden = sel.options.length <= 1;
  // 选项是刚填进原生元素的，自绘层还停在"空下拉"那一帧——必须通知它。
  refreshSelect(sel);
}

async function gbBrowseCategory(elementId) {
  if (!elementId) return;
  // 门类本身可能还没在画布上（用户先搜索、再选门类），先把它加进去。
  if (!gbVisibleIds.has(elementId)) gbAddNodes([elementId]);
  await gbExpandNode(elementId);
}

function renderGbLambda1Note(note) {
  // 原样展示后端 lambda1_note() 算出来的文字，前端不改写、不精简一个字——
  // 这段话是这个项目的一个真实发现（要么图里压根没挂医案，要么医案证型
  // 体系跟国标对不上），弱化或省略它比图上有 bug 更严重。
  document.getElementById("gb-lambda1-note").textContent = note || "";
}

function renderGbStats(stats) {
  const el = document.getElementById("gb-stats");
  if (!stats) { el.textContent = ""; return; }
  const fmt = (obj) => Object.entries(obj || {}).map(([k, v]) => `${k} ${v}`).join("　");
  el.textContent = `节点：${fmt(stats.node_type_counts)}　|　边：${fmt(stats.edge_type_counts)}`;
}

function populateGbPhysicianSelect(physicians) {
  const sel = document.getElementById("gb-physician-select");
  sel.innerHTML = "";
  // A4：顺手把这两张展示用的映射填上。describeNodeTooltip /
  // describeEdgeTooltip 的 λ1 那行读的就是它们，而原来只有 /api/consult
  // 回来时才填——新开页面直接点"图谱浏览器"，hover 边看到的是
  // "λ1（ye_tianshi）= 0.00" 这种英文 id。/api/graph 的 physicians 里
  // name/color 本来就带着，缺的只是这两行。
  for (const p of physicians || []) {
    if (p.color) PHYSICIAN_COLORS[p.id] = p.color;
    PHYSICIAN_NAMES[p.id] = p.name;
  }
  for (const p of physicians || []) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name;
    sel.appendChild(opt);
  }
  gbCurrentPhysician = (physicians && physicians[0] && physicians[0].id) || null;
  if (gbCurrentPhysician) sel.value = gbCurrentPhysician;
  // 同上：填完选项、选完默认值之后才轮到自绘层重画。
  refreshSelect(sel);
}

async function loadGraphBrowserData() {
  const hint = document.getElementById("gb-empty-hint");
  const errBox = document.getElementById("gb-error-box");
  hint.hidden = false;
  hint.textContent = "加载中…";
  errBox.classList.remove("show");
  if (!(await ensureGraphLibs())) {
    hint.hidden = true;
    errBox.textContent = CYTOSCAPE_MISSING_MSG;
    errBox.classList.add("show");
    return;
  }
  try {
    // R16 §3.2 规格 5：**首屏铺证素，不铺证型。**
    //
    // 之前首屏是 80 个证型方块，互不相连、全同色、cose 摊成几排——总纲 §1
    // 的 F6 点名的就是这个。证型之间本来就没有边，力导向对一堆孤立节点
    // 只能摊平，那张图不传达任何东西。
    //
    // 证素只有 20 个（实测 data/graph.json：element 20 / syndrome 178 /
    // symptom 1087），而且**每个证型都挂在证素下面**——它们是这张图真正的
    // 枢纽。首屏铺证素 = 首屏就有结构：点一个证素，它的证型长在外圈。
    //
    // limit 用 GB_ELEMENT_LIMIT 而不是写 20：证素数量是数据决定的
    // （教材扩充后可能变），写死 20 的话多出来的那几个会静默不显示。
    const resp = await fetch(`/api/graph?node_types=element&limit=${GB_ELEMENT_LIMIT}`);
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`服务返回错误（HTTP ${resp.status}）：${text}`);
    }
    gbGraphData = await resp.json();
    gbCursor = gbGraphData.page ? gbGraphData.page.next_cursor : null;
    gbTotalElements = gbGraphData.page ? gbGraphData.page.total : 0;
    gbHubIds = new Set((gbGraphData.graph.nodes || []).map((n) => n.data.id));
    gbBuildIndex();
    renderGbLambda1Note(gbGraphData.lambda1_note);
    renderGbStats(gbGraphData.stats);
    populateGbPhysicianSelect(gbGraphData.physicians);
    // 医案层是空的（has_case_layer=false）就不显示这个按钮，不是显示一个
    // 点了没反应的——这个 sandbox 里 data/graph.json 只有国标层数据，
    // AutoDL 上跑过 attach_cases 之后 has_case_layer 会是 true，按钮才出现。
    document.getElementById("gb-layer-toggle").hidden = !gbGraphData.has_case_layer;
    populateGbCategorySelect(gbGraphData.graph.nodes);
    gbResetView();
  } catch (err) {
    hint.hidden = true;
    errBox.textContent = `图谱加载失败：${err.message || err}`;
    errBox.classList.add("show");
  }
}




let growToken = 0; // 每次新的生长自增，旧的循环据此提前退出

// 把画布清干净。cy 还没建过时什么都不用做——建一个空的 cytoscape 实例
// 只是为了清空它，没有意义。
function clearGraph() {
  if (cy) cy.elements().remove();
  hideTooltip();
}

//: 每一层入场的间隔（毫秒）。**层号从数据里来，这张表只给间隔**。
//:
//: M5 的教训（CLAUDE.md 那条）直接指向这里：那一轮 `nodesByLayer` 写成
//: `{0:[],1:[],2:[],3:[],4:[]}`，加了 layer 4 之后前端漏了那个 key，
//: 后端 JSON 测试全绿、浏览器里那一层不出现。R42 把层数从 5 改成 9，
//: 同样的坑会再踩一次——所以这一轮把"有哪些层"整个交给后端下发的
//: `graph.layers`，前端**不写任何层号的清单**。这张表查不到就用默认值，
//: 于是加一层不改这里也不会漏掉那一层。
const LAYER_STAGGER_DEFAULT = 45;
const LAYER_STAGGER = { 0: 60, 1: 50, 2: 50, 3: 90, 4: 50, 5: 60, 6: 50, 7: 60, 8: 18 };

//: 画布高度的上下限。上限防止在笔记本屏幕上把输入框挤出首屏，
//: 下限防止只有两三个节点时画布塌成一条。
const CANVAS_MIN_H = 420;
const CANVAS_MAX_H = 900;
const CANVAS_PAD_Y = 80;

//: 图的层序。**只从后端下发的 `layers` 取**，取不到才从节点上现算
//: （旧的审计记录里没有 `layers` 这个键）。
function layerOrder(graph) {
  const declared = (graph.layers || []).map((r) => Number(r.layer))
    .filter((n) => Number.isFinite(n));
  if (declared.length) return [...new Set(declared)].sort((a, b) => a - b);
  const seen = new Set();
  for (const n of graph.nodes || []) {
    const L = Number(n.data.layer);
    if (Number.isFinite(L)) seen.add(L);
  }
  return [...seen].sort((a, b) => a - b);
}

//: 画布高度按**布局算出来的纵向跨度**定。
//:
//: 改之前是 `270 × 医家数`（B1 那一条）——那个公式的前提是医家分带：三位医家
//: 就是三条带子，纵向自然长一半。R42 去掉分带之后节点是**合并**的，两位医家
//: 给出同一个证型时纵向一点都不长；而 `rankdir=LR` 下纵向跨度由"最宽的那一层
//: 有几个节点"决定，跟医家数没有关系了。按医家数算会在合并时留一大片空白，
//: 在同层节点多时反而不够高、被 cy.fit() 缩小到看不清字。
function canvasHeightFor(positions) {
  const ys = Object.values(positions || {}).map((p) => p.y).filter(Number.isFinite);
  if (!ys.length) return CANVAS_MIN_H;
  const span = Math.max(...ys) - Math.min(...ys);
  return Math.min(CANVAS_MAX_H, Math.max(CANVAS_MIN_H, Math.round(span + CANVAS_PAD_Y)));
}

// ---------- R42：层名列头 ----------
//
// 九列摆在一起，不写列头就只能靠猜哪一列是什么。列头的另一半作用是**把缺层
// 说出来**：`missing_layers` 里的层照样出一个列头，标成"本次没有"——
// 后端那边刻意不把缺层的链条悄悄接过去（见 `to_graph` 的文档），前端这边
// 也不能把它悄悄藏掉，否则读图的人会以为九层里本来就只有六层。
function renderLayerBands(graph, hostId = "graph-layers") {
  const host = typeof document !== "undefined" ? document.getElementById(hostId) : null;
  if (!host) return "";
  const missing = new Set((graph && graph.missing_layers) || []);
  const rows = (graph && graph.layers) || [];
  const html = rows.map((r) => {
    const gone = missing.has(Number(r.layer));
    return `<span class="layer-band${gone ? " is-missing" : ""}"`
      + ` data-layer="${escapeHtml(String(r.layer))}"`
      + `>${escapeHtml(r.label || r.node_type || "")}`
      + (gone ? '<i class="layer-band-note">本次没有</i>' : "") + "</span>";
  }).join("");
  host.innerHTML = html;
  return html;
}

// ---------- R42：导出 PNG ----------
//
// 三甲的实际用法是把这张图贴进病历讨论或教学幻灯，所以导出必须是**位图 + 白底
// + 整图**，而不是截屏：
//   `full: true`  —— 导出整张图，不是当前视口（视口里常常只有半条链）；
//   `scale: 2`    —— 投影仪和打印都要 2 倍，1 倍在幻灯上是糊的；
//   `bg`          —— cytoscape 默认透明底，贴进白底幻灯没问题，贴进深色主题
//                    的文档里就成了一片黑；显式给纸色。
// 文件名带主诉与时间戳，否则连导三张图在下载目录里分不清哪张是哪张。
const PNG_SCALE = 2;

function exportGraphPng(cyInstance, filenameHint) {
  const inst = cyInstance || cy;
  if (!inst || typeof inst.png !== "function") {
    graphHooks.onError("图还没画出来，没有可导出的内容。");
    return null;
  }
  // 底色从 CSS 令牌取。**不写兜底色值**——graph.js 里一个十六进制都不许有
  // （tests/test_graph_layout.py 钉住；理由见 cssVar 上面那段）。取不到时
  // 干脆不给 bg，cytoscape 导出透明底；而"取不到"只发生在没有 CSS 的环境
  // （node 测试），那里本来也不导出图。
  const bg = cssVar("--paper");
  const opts = { full: true, scale: PNG_SCALE };
  if (bg) opts.bg = bg;
  const uri = inst.png(opts);
  const name = `辨证链-${(filenameHint || "graph").slice(0, 20)}-${pngStamp()}.png`;
  const a = document.createElement("a");
  a.href = uri;
  a.download = name;
  // 不插进 DOM 也能点（Chrome/Firefox/Safari 都行），插了还得记得删。
  a.click();
  return name;
}

//: 导出文件名里的时间戳。**本地时区、不带冒号**——冒号在 Windows 的文件名里
//: 是非法字符，带冒号的名字在那边会被浏览器静默改名。
function pngStamp(d) {
  const t = d || new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${t.getFullYear()}${p(t.getMonth() + 1)}${p(t.getDate())}`
    + `-${p(t.getHours())}${p(t.getMinutes())}${p(t.getSeconds())}`;
}

// ---------- R42：零重叠 / 零穿越的量具 ----------
//
// 这两个数是这一轮的验收判据，而**它们只能在真实渲染之后量**：dagre 按
// `measureLabel()` 的估算留位，而估算与真实包围盒不是一回事（估小了就会重叠，
// 这正是 `measureLabel` 必须偏大的原因）。所以判据挂在 cy 实例上、由 Playwright
// 在真实浏览器里读——JSON 结构测试测不到这一层（CLAUDE.md 那条硬约定）。
//
// `overlaps`：同一层内两两比真实包围盒，相交面积 > 0 就算一次重叠。
//   只比同层：不同层在 LR 布局下 x 区间本来就不相交，跨层比是白算。
//   compound 父节点（方剂框）与它自己的子节点必然相交——**那是包含不是重叠**，
//   按 parent 关系排除。
// `crossings`：两条边的**中段折线**是否交叉。taxi 边的形状是"横-竖-横"，
//   两条边只可能在中间那段竖线所在的转折带上交叉，所以只比相邻层之间那一段，
//   用线段相交判定。不比 bezier（浏览器那张图不用这个量具）。
function boundingBoxes(cyInstance) {
  const inst = cyInstance || cy;
  if (!inst) return [];
  return inst.nodes().map((n) => {
    const bb = n.renderedBoundingBox();
    return { id: n.id(), layer: Number(n.data("layer")),
             parent: n.data("parent") || null,
             x1: bb.x1, y1: bb.y1, x2: bb.x2, y2: bb.y2 };
  });
}

function overlapStats(cyInstance) {
  const boxes = boundingBoxes(cyInstance);
  const byLayer = new Map();
  for (const b of boxes) {
    if (!Number.isFinite(b.layer)) continue;
    if (!byLayer.has(b.layer)) byLayer.set(b.layer, []);
    byLayer.get(b.layer).push(b);
  }
  const pairs = [];
  for (const [, items] of byLayer) {
    for (let a = 0; a < items.length; a++) {
      for (let b = a + 1; b < items.length; b++) {
        const A = items[a];
        const B = items[b];
        if (A.parent === B.id || B.parent === A.id) continue;   // 包含，不是重叠
        const w = Math.min(A.x2, B.x2) - Math.max(A.x1, B.x1);
        const h = Math.min(A.y2, B.y2) - Math.max(A.y1, B.y1);
        if (w > 0 && h > 0) pairs.push([A.id, B.id, Math.round(w * h)]);
      }
    }
  }
  return { n_nodes: boxes.length, n_overlaps: pairs.length, pairs: pairs.slice(0, 20) };
}

function _seg(p, q, r, s) {
  // 标准的线段相交判定（叉积同异号）。共端点不算交叉——同一个节点出来的
  // 两条边在起点必然共点，那不是"线交叉"。
  const d = (a, b, c) => (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
  const same = (a, b) => Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5;
  if (same(p, r) || same(p, s) || same(q, r) || same(q, s)) return false;
  const d1 = d(p, q, r);
  const d2 = d(p, q, s);
  const d3 = d(r, s, p);
  const d4 = d(r, s, q);
  return ((d1 > 0) !== (d2 > 0)) && ((d3 > 0) !== (d4 > 0));
}

//: **有些交叉是数据逼出来的，不是布局的错。**
//:
//: 两列之间只要出现一个 K₂,₂（两个上游节点各自都连到同样的两个下游节点），
//: 那一对边在**任何**两列直线画法下都必然交叉——换一种排序只是换哪两条交叉，
//: 交叉数不变。所以"零交叉"只能对**单链**（每层一列）要求；多结论并列时
//: 要求的是"交叉数不超过被逼出来的那个下界"。
//:
//: R42 实测：三位医家给出三个不同证型、而证素层是全局共享的那一份，于是
//: 证素→证型是**全连接**——脏腑 2 个 × 证型 3 个 给出 C(2,2)×C(3,2)=3 对，
//: 病性同理 3 对，合计 6 对被逼出来的交叉。量出来正好 6，说明布局一对都没多加。
//: 这个数写在报告里必须带这句话，否则"6 对交叉"会被读成布局没做好。
function forcedCrossings(cyInstance) {
  const inst = cyInstance || cy;
  if (!inst) return 0;
  // 按 (上游层, 下游层) 分组收邻接关系
  const blocks = new Map();
  inst.edges().forEach((e) => {
    const a = Number(e.source().data("layer"));
    const b = Number(e.target().data("layer"));
    if (!Number.isFinite(a) || !Number.isFinite(b)) return;
    const key = `${a}>>${b}`;
    if (!blocks.has(key)) blocks.set(key, new Map());
    const adj = blocks.get(key);
    if (!adj.has(e.source().id())) adj.set(e.source().id(), new Set());
    adj.get(e.source().id()).add(e.target().id());
  });
  let forced = 0;
  for (const [, adj] of blocks) {
    const srcs = [...adj.keys()];
    for (let i = 0; i < srcs.length; i++) {
      for (let j = i + 1; j < srcs.length; j++) {
        const A = adj.get(srcs[i]);
        const B = adj.get(srcs[j]);
        // 共同的下游节点有 k 个 → 这两个上游之间被逼出 C(k,2) 对交叉
        let shared = 0;
        for (const x of A) if (B.has(x)) shared += 1;
        forced += (shared * (shared - 1)) / 2;
      }
    }
  }
  return forced;
}

function crossingStats(cyInstance) {
  const inst = cyInstance || cy;
  if (!inst) return { n_edges: 0, n_crossings: 0, pairs: [] };
  const segs = [];
  inst.edges().forEach((e) => {
    try {
      const s = e.source().renderedPosition();
      const tt = e.target().renderedPosition();
      segs.push({ id: e.id(), p: { x: s.x, y: s.y }, q: { x: tt.x, y: tt.y },
                  a: Number(e.source().data("layer")), b: Number(e.target().data("layer")) });
    } catch (_) { /* 节点还没渲染完，跳过 */ }
  });
  const pairs = [];
  for (let i2 = 0; i2 < segs.length; i2++) {
    for (let j2 = i2 + 1; j2 < segs.length; j2++) {
      const A = segs[i2];
      const B = segs[j2];
      // 只比**同一对相邻层之间**的边：不同层对的边在 x 区间上不重叠。
      if (A.a !== B.a || A.b !== B.b) continue;
      if (_seg(A.p, A.q, B.p, B.q)) pairs.push([A.id, B.id]);
    }
  }
  // `forced` 是"数据逼出来的下界"；`excess = n_crossings - forced` 才是布局
  // 该负责的那部分。判据看 excess，不看 n_crossings（见 forcedCrossings 的注释）。
  const forced = forcedCrossings(inst);
  return { n_edges: segs.length, n_crossings: pairs.length,
           forced, excess: pairs.length - forced, pairs: pairs.slice(0, 20) };
}

async function growGraph(graph, { animate = true } = {}) {
  // graph 为空 = 这一次没有图可画（安全拦截整页替换、或还没问诊）。**清空画布
  // 并返回，不是抛**：R14 的拦截页要求"不显示图谱"，调用方传 null 表达的正是
  // 这件事；这里抛 TypeError 的话拦截页会在控制台留一条错，而页面看起来正常
  // ——正是这个项目一直在防的那种"静默"。
  if (!graph) { clearGraph(); return; }
  if (!(await ensureGraphLibs())) { graphHooks.onError(CYTOSCAPE_MISSING_MSG); return; }

  // R41：**令牌要在 await 之前领，清画布要在 await 之后做。**
  //
  // 布局改成异步（`layoutAsync`，Worker）之后，"清空画布"与"加节点"之间多了一个
  // await，于是两次 growGraph 能交错：两边都先清空、都 await、都恢复、都加节点
  // → cytoscape 抛 `Can not create second element with ID sym::xxx`，整页崩。
  // Playwright 那两个图场景第一次跑就红了（`consult_graph` /
  // `single_chain_graph`），这正是 CLAUDE.md 那条硬约定的意义：改了渲染时序，
  // 单测（纯 JSON 结构）一条都测不出来。
  //
  // 改法是把既有的 `growToken` 机制往前挪：先领号，await 回来发现号过期就
  // **原地退出**（后来的那一次会把画布清干净、重新画），而不是继续往一个
  // 已经被别人接管的画布上加节点。
  const myToken = ++growToken;
  const positions = await layoutAsync(graph.nodes, graph.edges);
  if (myToken !== growToken) return;      // 被后来的一次接管了

  const cyEl = document.getElementById("cy");
  if (cyEl && cyEl.style) {
    cyEl.style.height = `${canvasHeightFor(positions)}px`;
  }
  ensureCanvas();
  if (cy && cy.resize) cy.resize(); // 容器高度改过之后要让 cytoscape 重新量一次
  cy.style(buildStylesheet({ physicianColors: PHYSICIAN_COLORS }));
  renderLayerBands(graph);

  cy.elements().remove();
  // M7：新图跟旧图的节点 id 不一定还对得上（换了个主诉），上一次点开的高亮
  // 状态没有意义了，清掉——不清的话 highlightedSymptomId 会残留一个新图里
  // 可能根本不存在的 id，再点同名症状（如果凑巧还叫这个名）会被误判成
  // "点第二下、取消"，其实用户是第一次点这张新图。
  highlightedSymptomId = null;
  // **层号从数据里来。** 不再有 `{0:[],1:[],...}` 这种写死的清单（见
  // LAYER_STAGGER 上面那段关于 M5 的注释）。
  const nodesByLayer = new Map();
  for (const n of graph.nodes) {
    const L = Number(n.data.layer);
    const key = Number.isFinite(L) ? L : -1;   // 层号缺失/越界：归到 -1，先画，不崩
    if (!nodesByLayer.has(key)) nodesByLayer.set(key, []);
    nodesByLayer.get(key).push(n);
  }
  // 顺序 = 后端声明的层序，再补上数据里出现过而声明里没有的层
  // （**不丢**：丢掉的表现是"那一层的节点不出现"，跟 M5 那次一模一样）。
  const order = layerOrder(graph);
  for (const key of [...nodesByLayer.keys()].sort((a, b) => a - b)) {
    if (!order.includes(key)) order.push(key);
  }

  const addNode = (n) =>
    cy.add({ group: "nodes", data: n.data, position: positions[n.data.id] });

  // 某一层的节点加完之后，把两端都已存在的边补上。
  // grow=true 时用 line-dash-offset 做"生长"：先把边整条画成一段虚线
  // （dash 长度 = 边长），offset 从边长动到 0，看上去就是从起点延伸到终点，
  // 动完再切回 solid。Cytoscape 的元素不是 DOM，只能用 cy.animate()，
  // CSS transition 对它无效。
  const addReadyEdges = (grow = false) => {
    const present = new Set(cy.nodes().map((n) => n.id()));
    // E3：去重改用自己维护的 key 集合，不再把节点 id 拼进 cytoscape 选择器
    // 字符串。节点 id 里的方剂名和药材名**是模型生成的**——出现一个引号或
    // 右方括号就会让整张图渲染失败。
    // 顺带把 O(边数 × 选择器匹配) 降成 O(边数)。
    const seenEdges = new Set(cy.edges().map((e) => `${e.data("source")}::${e.data("target")}`));
    const added = [];
    for (const e of graph.edges) {
      const { source, target } = e.data;
      if (!present.has(source) || !present.has(target)) continue;
      const key = `${source}::${target}`;
      if (seenEdges.has(key)) continue;
      seenEdges.add(key);
      added.push(cy.add({ group: "edges", data: e.data }));
    }
    if (!grow) return added;

    for (const el of added) {
      let len = 260;
      try {
        const s = el.source().position();
        const tt = el.target().position();
        len = Math.max(40, Math.hypot(tt.x - s.x, tt.y - s.y));
      } catch (_) {}
      el.style({
        "line-style": "dashed",
        "line-dash-pattern": [len, len],
        "line-dash-offset": len,
        "target-arrow-shape": "none",
      });
      el.animate(
        { style: { "line-dash-offset": 0 } },
        {
          duration: 420,
          easing: "ease-out",
          complete: () => {
            el.removeStyle("line-style line-dash-pattern line-dash-offset target-arrow-shape");
          },
        }
      );
    }
    return added;
  };

  if (!animate) {
    // 顺序很重要：方剂层（compound 父节点）必须先加完，君臣佐使层（compound
    // 子节点）才能加——cytoscape 的子节点靠 data.parent 引用父节点 id，
    // 父节点不存在时子节点加不上或渲染不出 compound 关系。
    // 升序遍历层号就满足这一条（方剂 7 < 君臣佐使 8），**不用另写一条特例**。
    for (const layer of order) (nodesByLayer.get(layer) || []).forEach(addNode);
    addReadyEdges();
    cy.fit(undefined, FIT_PADDING);
    return;
  }

  for (const layer of order) {
    const stagger = LAYER_STAGGER[layer] === undefined
      ? LAYER_STAGGER_DEFAULT : LAYER_STAGGER[layer];
    for (const n of (nodesByLayer.get(layer) || [])) {
      if (myToken !== growToken) return; // 被新一次生长取代，停掉旧循环
      const el = addNode(n);
      el.style("opacity", 0);
      el.animate({ style: { opacity: 1 } }, { duration: 240, easing: "ease-out" });
      await sleep(stagger);
    }
    if (myToken !== growToken) return;
    addReadyEdges(true);
    await sleep(360); // 让边基本长完再进下一层，否则层层重叠看不清
  }
  cy.fit(undefined, FIT_PADDING);
}

//: `cy.fit()` 的留边。**九层之后这个数要比原来的 24 大**：LR 布局下最左一列
//: （症状）和最右一列（君臣佐使）的标签会贴到画布边上，24px 时药名会被切掉
//: 半个字。36 是实测放得下最长药名的值。
const FIT_PADDING = 36;

// 保留旧名，默认不带动画——改动画开关只在一处，调用方不用改。
function renderGraph(graph) {
  return growGraph(graph, { animate: true });
}

function replayGraph() {
  if (lastGraph) growGraph(lastGraph, { animate: true });
}

function skipAnimation() {
  if (lastGraph) growGraph(lastGraph, { animate: false });
}

let lastGraph = null;


// ---------- 对外接口：window.TCM ----------
//
// **这份清单必须恰好等于 app.js 真正调用到的那些**，不多不少
// （tests/test_web_split.py 从源码算出真实跨文件调用集合来比对——R13 那版是手写的
// 33 个名字、对面一个都没用，那条测试等于没测）。
// 反方向是空的：graph.js 不调用 app.js 的任何东西，宿主 UI 走 setGraphHooks 注入。
//
// R41：**这一句要挡一下 `window` 不存在的情况。** 布局 Worker 用
// `importScripts("graph.js")` 把这份文件原样拉进去（一个算法只能有一处实现，
// 见 layoutAsync 的注释），而 Worker 里没有 `window`——不挡的话 import 那一刻
// 就 ReferenceError，Worker 永远起不来，而 layoutAsync 会静默回落到主线程，
// 看起来像"Worker 没收益"。
if (typeof window !== "undefined") {
window.TCM = Object.assign(window.TCM || {}, {
  // app.js 用到的图谱侧函数
  gbApplyPhysicianWeighting, gbBrowseCategory, gbSearch, hideTooltip, loadGraphBrowserData,
  renderGraph,
  // 两边共用的纯工具（定义在这一层，见文件顶部的依赖方向说明）。
  // sleep 不在清单里：搬过来之后只有 growGraph 用，app.js 一次都没调——
  // 清单只列**对面真的用到的**，多列一个就是给"清单齐全"那条测试留一个假绿点。
  escapeHtml,
  // 方剂来源的中文名（app.js 的九段也要用，见定义处的注释）
  formulaSourceLabel,
  // R42：节点 id 的构造（app.js 的证据链索引要用同一份拼法）
  NODE_ID,
  // R42：导出 PNG（app.js 的工具条按钮调）。
  // **钉住那三个函数刻意不在清单里**：pin/unpin/tooltipPinned 只在 graph.js
  // 内部用（tap 钉住、Esc 取消都在 ensureCanvas 里挂），app.js 一次都不调
  // ——这张清单的契约是"恰好等于 app.js 真正调用到的那些"，多列一个就是给
  // "清单齐全"那条测试留一个假绿点（R13 那版 33 个名字全是这么来的）。
  exportGraphPng,
  // 宿主在启动时注册自己的 UI 实现
  setGraphHooks,
});

// R41：布局的去处与耗时。**刻意不挂在 window.TCM 上**——那张清单的契约是
// "恰好等于 app.js 真正调用到的那些"（有一条测试从源码算真实调用集合来比），
// 而这两样东西 app.js 一次都不用，是给量具（scripts/profile_frontend.py）
// 和测试读的。混进去就等于给"清单齐全"那条测试留一个假绿点。
// R42 加了两把量具：零重叠与零穿越。**它们只能在真实渲染之后量**（dagre 按
// 估算留位，估算不是真实包围盒），所以判据挂在这里由 Playwright 在真实浏览器
// 里读，不进 JSON 结构测试。`bands` 给的是层名列头的 DOM 快照，用来验"缺层
// 如实显示"这一条。
// R42：图谱浏览器两种布局的计时。**"改用了 dagre"不是一个可核的说法**，
// 要有数：同一批节点分别喂给同心环（`gbLayoutPositions`）和 dagre
// （`computeLayout`），各跑 `runs` 遍取中位数。
// 判据是"dagre 在当前规模上是否够快"，不是"哪个更好看"——两者回答的不是
// 同一个问题（见 gbFocus 上面那段）。
window.__gbPerf = {
  focusStack: () => [...gbFocusStack],
  breadcrumb: () => gbBreadcrumbHtml(gbFocusStack),
  focus: (id) => gbFocus(id),
  exitFocus: () => gbExitFocus(),
  neighborhood: (id, hops) => {
    const nh = gbNeighborhood(id, hops);
    return { n: nh.ids.size, truncated: nh.truncated,
             per_hop: [...nh.hop.values()].reduce((acc, d) => {
               acc[d] = (acc[d] || 0) + 1; return acc; }, {}) };
  },
  benchLayouts: (rootId, runs = 7) => {
    if (!gbIndex) return null;
    const nh = gbNeighborhood(rootId);
    const nodes = [...nh.ids].map((id) => ({
      data: Object.assign({}, gbIndex.nodeById.get(id).data, { layer: nh.hop.get(id) }) }));
    const edges = [];
    const seen = new Set();
    for (const id of nh.ids) {
      for (const e of (gbIndex.edgesByNode.get(id) || [])) {
        if (!nh.ids.has(e.data.source) || !nh.ids.has(e.data.target)) continue;
        const k = `${e.data.source}>>${e.data.target}`;
        if (seen.has(k)) continue;
        seen.add(k);
        edges.push({ data: e.data });
      }
    }
    const med = (xs) => xs.slice().sort((a, b) => a - b)[Math.floor(xs.length / 2)];
    const time = (fn) => {
      const out = [];
      for (let i = 0; i < runs; i++) {
        const t0 = performance.now();
        fn();
        out.push(performance.now() - t0);
      }
      return Math.round(med(out) * 1000) / 1000;
    };
    const ids = [...nh.ids];
    const hubIds = ids.filter((id) => gbHubIds.has(id));
    const ring = time(() => gbLayoutPositions({
      hubIds, fans: [], others: ids.filter((id) => !gbHubIds.has(id)),
      width: 1000, height: 600, focusHubId: hubIds[0] || null,
    }));
    const dag = time(() => computeLayout(nodes, edges));
    return { n_nodes: ids.length, n_edges: edges.length, runs,
             ring_ms: ring, dagre_ms: dag,
             dagre_available: dagreAvailable() };
  },
};

window.__graphPerf = {
  layoutStats, layoutAsync, computeLayout,
  overlapStats, crossingStats, forcedCrossings, boundingBoxes,
  dagreAvailable,
  bands: () => {
    const host = document.getElementById("graph-layers");
    return host ? [...host.querySelectorAll(".layer-band")].map((el) => ({
      layer: Number(el.dataset.layer),
      label: el.textContent.replace("本次没有", "").trim(),
      missing: el.classList.contains("is-missing"),
    })) : [];
  },
};
}
