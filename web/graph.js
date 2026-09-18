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
let _cytoscapePromise = null;
function ensureCytoscape() {
  if (typeof cytoscape !== "undefined") return Promise.resolve(true);
  if (_cytoscapePromise) return _cytoscapePromise;
  _cytoscapePromise = new Promise((resolve) => {
    const el = document.createElement("script");
    el.src = "vendor/cytoscape.min.js";
    el.onload = () => resolve(typeof cytoscape !== "undefined");
    el.onerror = () => resolve(false);
    document.head.appendChild(el);
  });
  return _cytoscapePromise;
}
const CYTOSCAPE_MISSING_MSG =
  "图谱库没有加载成功：CDN 不可达，本地副本 web/vendor/cytoscape.min.js 也不存在。"
  + "辨证结果本身不受影响，只有图画不出来——断网演示前先把这个文件放好"
  + "（见 README「离线演示」）。";

function evenY(index, total) {
  if (total <= 1) return (Y_MIN + Y_MAX) / 2;
  return Y_MIN + (Y_MAX - Y_MIN) * (index / (total - 1));
}

function computeLayout(nodes, edges) {
  const positions = {};
  const byLayer = { 0: [], 1: [], 2: [], 3: [], 4: [] };
  for (const n of nodes) {
    const L = Number(n.data.layer);
    if (!byLayer[L]) byLayer[L] = [];   // 兜底：layer 不在 0-4 时不要整页崩
    byLayer[L].push(n);
  }

  // layer 0 保持原始顺序（症状顺序有语义，是 S1 抽取的顺序）
  (byLayer[0] || []).forEach((n, i) => {
    positions[n.data.id] = { x: LAYER_X[0], y: evenY(i, byLayer[0].length) };
  });

  // layer 1/2 按"上游邻居的平均位置"排序（重心排序），减少边交叉。
  // 不做这一步的话，层内顺序取决于节点数组的原始顺序，
  // 上下游顺序不一致时边会交叉成 X。
  const yOf = (id) => (positions[id] ? positions[id].y : null);
  for (const layer of [1]) {
    const items = byLayer[layer] || [];
    const bary = new Map();
    for (const n of items) {
      const ys = [];
      for (const e of edges) {
        if (e.data.target === n.data.id) {
          const y = yOf(e.data.source);
          if (y !== null) ys.push(y);
        }
      }
      bary.set(n.data.id, ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Number.MAX_SAFE_INTEGER);
    }
    const sorted = [...items].sort((a, b) => bary.get(a.data.id) - bary.get(b.data.id));
    sorted.forEach((n, i) => {
      positions[n.data.id] = { x: LAYER_X[layer], y: evenY(i, sorted.length) };
    });
  }

  // layer 2 证型：按医家分上下两带，与 layer 3 的方剂带对齐。
  // 不能用重心排序——两位医家的证型上游证素高度重叠，重心值接近，
  // 排序结果近乎随机，会把两个证型甩到画布上下两端，边被迫斜穿全图。
  const physOrder = [];
  for (const n of byLayer[2]) {
    if (!physOrder.includes(n.data.phys)) physOrder.push(n.data.phys);
  }
  const formulaByPhys = {};
  for (const n of byLayer[3]) {
    (formulaByPhys[n.data.phys] = formulaByPhys[n.data.phys] || []).push(n);
  }
  // M5：药材（layer 4）现在是方剂（layer 3）的 compound 子节点，按 parent
  // 分组而不是按医家分组——同一位医家的 2-3 个候选方各自带一群不同的药，
  // 混在一个医家带里排布会让不同方剂的药材彼此穿插。
  const herbsByFormula = {};
  for (const n of byLayer[4]) {
    (herbsByFormula[n.data.parent] = herbsByFormula[n.data.parent] || []).push(n);
  }
  const gap = 40;

  // layer 2 按同一套医家带排布，保证证型与它的方剂在同一水平区间
  const synByPhys = {};
  for (const n of byLayer[2]) {
    (synByPhys[n.data.phys] = synByPhys[n.data.phys] || []).push(n);
  }
  const bandCount2 = physOrder.length || 1;
  const bandHeight2 = (Y_MAX - Y_MIN - gap * (bandCount2 - 1)) / bandCount2;
  physOrder.forEach((phys, bandIdx) => {
    const bandTop = Y_MIN + bandIdx * (bandHeight2 + gap);
    const items = synByPhys[phys] || [];
    items.forEach((n, i) => {
      const y = items.length <= 1
        ? bandTop + bandHeight2 / 2
        : bandTop + (bandHeight2 * i) / (items.length - 1);
      positions[n.data.id] = { x: LAYER_X[2], y };
    });
  });

  // layer 3 方剂：跟 layer 2 一样先按医家分带算出一个初始 y。每个方剂的
  // 药材簇宽度（herbSpan）按"同一医家带内相邻候选方的间距"设上限——3 个
  // 候选方紧挨着排，每个方能分到的纵向空间比只有 1 个候选方时更窄，原来
  // 固定 90 的上限在候选方多、间距本来就小时仍然会撞（M5 报告标注的问题）。
  const bandCount = physOrder.length || 1;
  const bandHeight = (Y_MAX - Y_MIN - gap * (bandCount - 1)) / bandCount;
  // 收集初始位置，先不写进 positions——下面还有一道"跨医家带"的碰撞检查，
  // 同一医家带内部间距算得再准，也管不住"上一位医家最后一个候选方"跟
  // "下一位医家第一个候选方"之间只隔着固定的 gap（40）这件事：两者的药材簇
  // 只要分别比 gap 的一半还宽，照样会在带与带的接缝处撞上，这是本轮
  // Playwright 真实截图才照出来的——JSON 结构测试测不出"两个方框画面上
  // 是否互相压住"，只有真的渲染出来看像素位置才看得见（CLAUDE.md 那条
  // "涉及图层结构变更要跑 Playwright"的教训在这里又应验了一次，虽然这次
  // 没有改层结构，但同样是"数据对、布局算法的一个隐藏假设错了，只有真实
  // 渲染能暴露"）。
  const formulaLayout = [];
  physOrder.forEach((phys, bandIdx) => {
    const bandTop = Y_MIN + bandIdx * (bandHeight + gap);
    const items = formulaByPhys[phys] || [];
    const formulaStep = items.length > 1 ? bandHeight / (items.length - 1) : bandHeight;
    const maxHerbSpan = items.length > 1 ? formulaStep * 0.8 : bandHeight * 0.6;
    items.forEach((n, i) => {
      const y = items.length <= 1 ? bandTop + bandHeight / 2 : bandTop + (bandHeight * i) / (items.length - 1);
      const herbs = herbsByFormula[n.data.id] || [];
      const herbSpan = Math.min(maxHerbSpan, HERB_GAP * Math.max(herbs.length - 1, 0));
      formulaLayout.push({ id: n.data.id, y, herbSpan, herbs });
    });
  });

  // 跨医家带的碰撞检查：按 y 从上到下排序后逐个检查相邻两个方剂的药材簇
  // 是否留够间距（各自 herbSpan 的一半 + 一个固定安全边距），不够就把后一个
  // （以及它之后所有已经排定的）整体下移，补齐差额——这是一次性的从上到下
  // 扫描，不是反复迭代到收敛，因为"后面的推前面的"这种情况在这套从上到下的
  // 排布里不会发生（每个方剂的初始 y 已经是它所在医家带内部合理排布的结果，
  // 唯一可能不够的间距就是相邻两个的，处理完这一对，后面的对不会因为这次
  // 调整而重新变得不够）。
  // 这个安全边距不只是"两个方剂中心的间距"，还要盖住 compound 父节点自己的
  // 渲染开销——node:parent 样式给了 14px padding（上下各一份）、"text-valign:
  // top" 的方名标签还要再占一截高度、herbSpan 本身量的是"药材中心到中心"
  // 不含药材节点自己的半个身位。60 是实测出来的：第一版用 20，Playwright
  // 截图里两个相邻候选方的框依然会互相压住（见模块报告），说明"药材中心
  // 间距够了"不等于"方框边缘不重叠"，20 只覆盖了 FORMULA_MARGIN 字面意思
  // 那一层，没算 compound 框自己的额外开销，加到 60 之后截图确认不再重叠。
  const FORMULA_MARGIN = 60;
  formulaLayout.sort((a, b) => a.y - b.y);
  for (let i = 1; i < formulaLayout.length; i++) {
    const prev = formulaLayout[i - 1];
    const cur = formulaLayout[i];
    const minGap = prev.herbSpan / 2 + cur.herbSpan / 2 + FORMULA_MARGIN;
    const actualGap = cur.y - prev.y;
    if (actualGap < minGap) cur.y = prev.y + minGap;
  }

  for (const f of formulaLayout) {
    positions[f.id] = { x: LAYER_X[3], y: f.y };
    // 下属的每味药材（layer 4）紧贴着（可能已经上移调整过的）f.y 纵向小间距
    // 聚成一小簇——cytoscape compound 节点的包围盒是按子节点位置自动算出来
    // 的，子节点越贴近父节点给定的位置，画出来的方剂框就越像"一个方框里
    // 装着自己的药"，而不是横跨整个画布的细长条。
    f.herbs.forEach((h, hi) => {
      const hy = f.herbs.length <= 1
        ? f.y
        : f.y - f.herbSpan / 2 + (f.herbSpan * hi) / (f.herbs.length - 1);
      positions[h.data.id] = { x: LAYER_X[4], y: hy };
    });
  }

  // B1：证型节点跟着它自己的方剂走。
  // 上面那道跨医家带的碰撞检查只推方剂（layer 3），不动证型（layer 2）。
  // 两位医家时每条带 260 个逻辑单位，推得少、看不出来；三位医家时同一个
  // 纵向范围被切成三条、每条只剩 160，而 FORMULA_MARGIN=60 是当年在**两位
  // 医家**上用 Playwright 实测标定的固定值——带一窄，累积推移就把方剂整体
  // 顶下去，证型却还留在原来的带里。合成图上实测最大错位 512 个逻辑单位，
  // 表现为证型节点跟它自己的方剂差了大半个画布、连线斜穿全图。
  // 这里不动任何方剂/药材的坐标（所以整体高度、碰撞结论、已经验过的那份
  // 基线都不变），只在方剂位置定下来之后把证型平移到它那些方剂的形心上。
  const formulaYByPhys = {};
  for (const n of byLayer[3] || []) {
    const pos = positions[n.data.id];
    if (!pos) continue;
    (formulaYByPhys[n.data.phys] = formulaYByPhys[n.data.phys] || []).push(pos.y);
  }
  for (const phys of Object.keys(synByPhys)) {
    const ys = formulaYByPhys[phys];
    if (!ys || !ys.length) continue; // 没有候选方的医家保持按带排布，不凭空移动
    const centre = ys.reduce((a, b) => a + b, 0) / ys.length;
    const items = synByPhys[phys];
    const base = items.map((n) => positions[n.data.id].y);
    const mid = base.reduce((a, b) => a + b, 0) / base.length;
    // 同一位医家有多个证型节点时仍按原来的相对间距摊开，只是整体平移到
    // 形心上——不把它们叠在一个点。
    items.forEach((n, i) => {
      positions[n.data.id] = { x: LAYER_X[2], y: base[i] - mid + centre };
    });
  }

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
  };
}

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
        padding: "14px",
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
    {
      selector: "edge",
      style: pick({
        width: 1.4,
        "line-color": cssVar("--edge-soft"),
        "target-arrow-color": cssVar("--edge-soft"),
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        "arrow-scale": 0.7,
        opacity: 0.85,
      }),
    },
  ];

  // 按医家染色**只有问诊图要**：那张图上每个证型/方剂/药材都属于某一位医家，
  // 颜色要跟三列的顶边对得上。浏览器那张图没有"这是谁的判断"这回事
  // ——染了只会让人以为某个国标证型是某位医家的。
  for (const [phys, color] of Object.entries(physicianColors || {})) {
    style.push({
      selector: `node[layer > 1][phys = "${phys}"]`,
      style: pick({ "background-color": color, "border-color": color,
                    color: cssVar("--paper") }),
    });
    style.push({
      selector: `edge[phys = "${phys}"]`,
      style: { "line-color": color, "target-arrow-color": color },
    });
  }

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
// 五种节点类型（层 0-4：症状/证素/病名证型/方剂/药材，M5 把原来的层 3
// "用药" 拆成方剂+药材两层）各一句 + 边一句，加起来是"6 种可 hover 的
// 东西"——不是又多出一种新节点类型，是 5 类节点 + 边。

// data.node_type 存在 = 持久知识图谱（图谱浏览器，模块7）的节点；
// data.layer 存在 = 单次问诊图（模块6）的节点。两种数据形状不同，但都回答
// "这个节点是什么"这一句话速览，复用同一个函数、同一套 showTooltip/hideTooltip，
// 不为图谱浏览器另起一套 tooltip 逻辑。
function describeNodeTooltip(data) {
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
  const physName = data.pname || PHYSICIAN_NAMES[data.phys] || data.phys || "";
  switch (data.layer) {
    case 0: {
      const stateLabel = { explained: "已解释", residual: "残差辨证补充解释", unexplained: "未解释" }[data.state] || data.state || "";
      return `<b>症状</b>　${escapeHtml(data.label)}<div class="tt-meta">${escapeHtml(stateLabel)}</div>`;
    }
    case 1: {
      const kindLabel = data.kind === "location" ? "病位" : data.kind === "nature" ? "病性" : (data.kind || "");
      return `<b>证素</b>　${escapeHtml(data.label)}<div class="tt-meta">${escapeHtml(kindLabel)}${data.residual ? "　（残差辨证补充）" : ""}</div>`;
    }
    case 2:
      return `<b>证型</b>　${escapeHtml(data.label)}<div class="tt-meta">${escapeHtml(physName)}</div>`;
    case 3: {
      // M5：层 3 从"用药"改成"方剂"，用药下沉到层 4。
      const srcLabel = formulaSourceLabel(data.source);
      const flags = [];
      if (data.selected) flags.push("★已选");
      if (data.safety_blocking) flags.push("⚠ 安全拦截");
      const meta = [physName, srcLabel, flags.join("　")].filter(Boolean).join("　");
      return `<b>方剂</b>　${escapeHtml(data.label)}<div class="tt-meta">${escapeHtml(meta)}</div>`;
    }
    case 4: {
      const parts = [physName];
      if (data.role) parts.push(`${data.role}药`);
      if (data.dose != null) parts.push(`${data.dose}${data.unit || ""}`);
      if (data.decoction) parts.push(data.decoction);
      const metaLine = `<div class="tt-meta">${escapeHtml(parts.filter(Boolean).join("　"))}</div>`;
      // M7：方中作用（function_in_formula）单独一行——跟上面那行不一样，
      // 上面几项都是"一个词/一个数字"，用全角空格拼在一句里；这项是一整句
      // 自由文本（比如"疏肝理气，为本方主药"），塞进同一行会跟前面的短词
      // 混在一起看不清楚断句，单独一行更符合它本身的长度和语义。
      const funcLine = data.function_in_formula
        ? `<div class="tt-meta">${escapeHtml(data.function_in_formula)}</div>` : "";
      return `<b>药材</b>　${escapeHtml(data.label)}${metaLine}${funcLine}`;
    }
    default:
      return escapeHtml(data.label || data.id || "");
  }
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
function showTooltip(html, clientX, clientY, panelId = "graph-panel", tooltipId = "graph-tooltip") {
  const tip = document.getElementById(tooltipId);
  const panel = document.getElementById(panelId);
  const panelRect = panel.getBoundingClientRect();
  tip.innerHTML = html;
  tip.classList.add("show");
  // 先显示、量出真实尺寸，再据此把 tooltip 夹在画布范围内——鼠标贴着
  // 右/下边缘时不这么做的话，tooltip 会被裁出可视区域，看不全。
  const tipRect = tip.getBoundingClientRect();
  let left = clientX - panelRect.left + 14;
  let top = clientY - panelRect.top + 14;
  left = Math.min(left, panel.clientWidth - tipRect.width - 6);
  top = Math.min(top, panel.clientHeight - tipRect.height - 6);
  tip.style.left = `${Math.max(6, left)}px`;
  tip.style.top = `${Math.max(6, top)}px`;
}

function hideTooltip(tooltipId = "graph-tooltip") {
  document.getElementById(tooltipId).classList.remove("show");
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

function computeHighlightPath(nodes, edges, startId) {
  const highlighted = new Set([startId]);
  let frontier = new Set([startId]);
  for (let hop = 0; hop < 3; hop++) {
    const next = new Set();
    for (const e of edges) {
      if (frontier.has(e.data.source) && !highlighted.has(e.data.target)) {
        next.add(e.data.target);
      }
    }
    for (const id of next) highlighted.add(id);
    frontier = next;
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
    }
  });
  cy.on("mouseover", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), evt.originalEvent.clientX, evt.originalEvent.clientY);
  });
  cy.on("mousemove", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), evt.originalEvent.clientX, evt.originalEvent.clientY);
  });
  // 直接把 hideTooltip 当回调传，cytoscape/DOM 会把它们自己的事件对象当第一个
  // 参数传进来，正好落进 hideTooltip(tooltipId=...) 那个默认参数的位置，把
  // "graph-tooltip" 这个默认值顶掉——包一层箭头函数、不透传参数，才是真的调用
  // "无参版本"。
  cy.on("mouseout", "node", () => hideTooltip());
  cy.on("mouseover", "edge", (evt) => {
    const e = evt.target;
    showTooltip(
      describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label")),
      evt.originalEvent.clientX, evt.originalEvent.clientY
    );
  });
  cy.on("mousemove", "edge", (evt) => {
    const e = evt.target;
    showTooltip(
      describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label")),
      evt.originalEvent.clientX, evt.originalEvent.clientY
    );
  });
  cy.on("mouseout", "edge", () => hideTooltip());
  // 兜底：鼠标整个离开画布这件事单独用原生 DOM mouseleave 盯一道，不完全
  // 依赖 cytoscape 自己那套"逐帧 hit-test 出有没有悬停元素"的合成事件。
  // 真实验证时发现，鼠标从节点直接"瞬移"到画布外（不经过中间帧）的极端
  // 情况下，cytoscape 的 node mouseout 有几率没触发，tooltip 会卡住不消失
  // ——真实鼠标移动是连续多帧、不会瞬移，用户手动移开基本不会撞上这条，
  // 但原生 mouseleave 是浏览器对"指针真的离开了这个元素"的保证，不依赖
  // canvas 内部逐帧算出来的悬停状态，加一道更保险。
  document.getElementById("cy").addEventListener("mouseleave", () => hideTooltip());
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
    showTooltip(describeNodeTooltip(evt.target.data()), evt.originalEvent.clientX, evt.originalEvent.clientY, "gb-panel", "gb-tooltip");
  });
  gbCy.on("mousemove", "node", (evt) => {
    showTooltip(describeNodeTooltip(evt.target.data()), evt.originalEvent.clientX, evt.originalEvent.clientY, "gb-panel", "gb-tooltip");
  });
  gbCy.on("mouseout", "node", () => hideTooltip("gb-tooltip"));
  gbCy.on("mouseover", "edge", (evt) => {
    const e = evt.target;
    showTooltip(
      describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label"), gbCurrentPhysician),
      evt.originalEvent.clientX, evt.originalEvent.clientY, "gb-panel", "gb-tooltip"
    );
  });
  gbCy.on("mousemove", "edge", (evt) => {
    const e = evt.target;
    showTooltip(
      describeEdgeTooltip(e.data(), e.source().data("label"), e.target().data("label"), gbCurrentPhysician),
      evt.originalEvent.clientX, evt.originalEvent.clientY, "gb-panel", "gb-tooltip"
    );
  });
  gbCy.on("mouseout", "edge", () => hideTooltip("gb-tooltip"));
  document.getElementById("gb-cy").addEventListener("mouseleave", () => hideTooltip("gb-tooltip"));
  return gbCy;
}

function gbBuildIndex() {
  const nodeById = new Map();
  for (const n of gbGraphData.graph.nodes) nodeById.set(n.data.id, n);
  const edgesByNode = new Map();
  for (const e of gbGraphData.graph.edges) {
    const { source, target } = e.data;
    if (!edgesByNode.has(source)) edgesByNode.set(source, []);
    if (!edgesByNode.has(target)) edgesByNode.set(target, []);
    edgesByNode.get(source).push(e);
    edgesByNode.get(target).push(e);
  }
  gbIndex = { nodeById, edgesByNode };
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
async function gbFetchInto(url, statusText, pick) {
  const status = document.getElementById("gb-search-status");
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
    if (status && statusText) status.textContent = statusText(data.page, data.dropped);
    return data;
  } catch (err) {
    if (status) status.textContent = `加载失败：${err.message || err}`;
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
  const seen = new Set(gbGraphData.graph.edges.map((e) => e.data.id));
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
    (graph) => gbCapExpansion(graph, GB_EXPAND_CAP)
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
          : `找到 ${page.total} 个匹配节点`)
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
      : `医案层 ${page.returned} 条`
  );
}

function gbResetView() {
  ensureGraphBrowserCanvas();
  gbCy.elements().remove();
  gbVisibleIds = new Set();
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
  if (!(await ensureCytoscape())) {
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

async function growGraph(graph, { animate = true } = {}) {
  // graph 为空 = 这一次没有图可画（安全拦截整页替换、或还没问诊）。**清空画布
  // 并返回，不是抛**：R14 的拦截页要求"不显示图谱"，调用方传 null 表达的正是
  // 这件事；这里抛 TypeError 的话拦截页会在控制台留一条错，而页面看起来正常
  // ——正是这个项目一直在防的那种"静默"。
  if (!graph) { clearGraph(); return; }
  if (!(await ensureCytoscape())) { graphHooks.onError(CYTOSCAPE_MISSING_MSG); return; }
  // B1 的另一半：容器高度跟着医家数走。cy.fit() 最后会把全部内容按同一个
  // 缩放系数塞进 #cy，医家从两位变三位、内容纵向长了一半，容器还是 540px
  // 的话缩放系数就变大，节点上的字跟着变小。270 × 医家数：两位时算出来正好
  // 是 540，跟改动前一致；三位是 810，上限 900 防止在笔记本屏幕上把输入框
  // 挤出首屏。
  const physCount = new Set(
    (graph.nodes || []).filter((n) => Number(n.data.layer) === 2).map((n) => n.data.phys)
  ).size;
  const cyEl = document.getElementById("cy");
  if (cyEl && cyEl.style) {
    cyEl.style.height = `${Math.min(900, Math.max(540, 270 * physCount))}px`;
  }
  ensureCanvas();
  if (cy && cy.resize) cy.resize(); // 容器高度改过之后要让 cytoscape 重新量一次
  // 配色随医家变化（physicians.py 是唯一源），每次重新应用样式表
  cy.style(buildStylesheet({ physicianColors: PHYSICIAN_COLORS }));
  cy.elements().remove();
  // M7：新图跟旧图的节点 id 不一定还对得上（换了个主诉），上一次点开的高亮
  // 状态没有意义了，清掉——不清的话 highlightedSymptomId 会残留一个新图里
  // 可能根本不存在的 id，再点同名症状（如果凑巧还叫这个名）会被误判成
  // "点第二下、取消"，其实用户是第一次点这张新图。
  highlightedSymptomId = null;

  const positions = computeLayout(graph.nodes, graph.edges);
  const nodesByLayer = { 0: [], 1: [], 2: [], 3: [], 4: [] };
  for (const n of graph.nodes) {
    // E4：跟 computeLayout() 同一条兜底（那边的注释写着"layer 不在 0-4 时
    // 不要整页崩"）。两处对同一件事的处理原来不一致——这边 layer 越界会直接
    // TypeError 把整页打掉。超出 0-4 的层只是不参与排布，不崩。
    const L = Number(n.data.layer);
    if (!nodesByLayer[L]) nodesByLayer[L] = [];
    nodesByLayer[L].push(n);
  }

  const myToken = ++growToken;
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
    // 字符串。节点 id 形如 herb::ye_tianshi::瓜蒌薤白半夏汤::瓜蒌，其中方剂名
    // 和药材名**是模型生成的**——出现一个引号或右方括号就会让整张图渲染失败。
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
        const t = el.target().position();
        len = Math.max(40, Math.hypot(t.x - s.x, t.y - s.y));
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
    // 顺序很重要：layer 3（方剂，compound 父节点）必须先加完，layer 4
    // （药材，compound 子节点）才能加——cytoscape 的子节点靠 data.parent
    // 引用父节点 id，父节点不存在时子节点加不上或渲染不出 compound 关系。
    for (const layer of [0, 1, 2, 3, 4]) nodesByLayer[layer].forEach(addNode);
    addReadyEdges();
    cy.fit(undefined, 24);
    return;
  }

  const STAGGER = { 0: 60, 1: 50, 2: 90, 3: 60, 4: 20 };
  for (const layer of [0, 1, 2, 3, 4]) {
    for (const n of nodesByLayer[layer]) {
      if (myToken !== growToken) return; // 被新一次生长取代，停掉旧循环
      const el = addNode(n);
      el.style("opacity", 0);
      el.animate({ style: { opacity: 1 } }, { duration: 240, easing: "ease-out" });
      await sleep(STAGGER[layer]);
    }
    if (myToken !== growToken) return;
    addReadyEdges(true);
    await sleep(360); // 让边基本长完再进下一层，否则层层重叠看不清
  }
  cy.fit(undefined, 24);
}

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
  // 宿主在启动时注册自己的 UI 实现
  setGraphHooks,
});
