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

function buildStylesheet() {
  const style = [
    {
      selector: "node",
      style: {
        shape: "round-rectangle",
        label: "data(label)",
        "text-valign": "center",
        "text-halign": "center",
        "font-size": 11,
        color: "#24262b",
        "background-color": "#f4f4f6",
        "border-width": 1,
        "border-color": "#c7c9d1",
        padding: "6px",
        width: "label",
        height: "label",
        "text-wrap": "wrap",
        "text-max-width": "90px",
      },
    },
    {
      selector: 'node[layer = 0][state = "residual"]',
      style: {
        "background-color": "#fdf1ea",
        "border-color": "#C2410C",
        "border-width": 2,
      },
    },
    {
      selector: "node[residual]",
      style: { "border-color": "#C2410C", "border-width": 2, "border-style": "dashed" },
    },
    {
      selector: "edge[residual]",
      style: { "line-color": "#C2410C", "target-arrow-color": "#C2410C", "line-style": "dashed" },
    },
    {
      selector: 'node[layer = 0][state = "unexplained"]',
      style: {
        "background-color": "#fff4e5",
        "border-color": "#e08a2b",
        "border-style": "dashed",
        "border-width": 2,
      },
    },
    {
      selector: "node[layer = 1]",
      style: {
        "background-color": "#efe7fb",
        "border-color": "#8b6fd9",
        color: "#4b2e9e",
      },
    },
    {
      // M5：方剂（layer 3）现在是 compound 父节点，:parent 是 cytoscape 内建的
      // 伪类选择器，匹配"带子节点的节点"，不用按 layer 另判一次。父节点框要
      // 半透明——不透明会把里面的药材（子节点）整个盖住看不见。
      selector: "node:parent",
      style: {
        "background-opacity": 0.12,
        "border-width": 2,
        "text-valign": "top",
        "text-halign": "center",
        padding: "14px",
      },
    },
    {
      // source 三档的边框区分：classic 用默认实线不用另写规则，
      // modified 虚线，composed 点线。
      selector: 'node[layer = 3][source = "modified"]',
      style: { "border-style": "dashed" },
    },
    {
      selector: 'node[layer = 3][source = "composed"]',
      style: { "border-style": "dotted" },
    },
    {
      selector: "edge",
      style: {
        width: 1.4,
        "line-color": "#d3d5db",
        "target-arrow-color": "#d3d5db",
        "target-arrow-shape": "triangle",
        "curve-style": "bezier",
        "arrow-scale": 0.7,
        opacity: 0.85,
      },
    },
    // M7：症状->方剂路径高亮用的淡化样式。放在样式表末尾（selector 顺序即
    // 层叠顺序，医家配色规则在下面的循环里还会追加），这样 .gt-faded 的
    // opacity 会盖过医家配色规则设的任何值——同一条"选中/拦截红框必须盖过
    // 医家色"的道理（见下面 node[?selected] 的注释），淡化同样必须盖过颜色。
    { selector: "node.gt-faded", style: { opacity: 0.15 } },
    { selector: "edge.gt-faded", style: { opacity: 0.06 } },
  ];
  for (const [phys, color] of Object.entries(PHYSICIAN_COLORS)) {
    style.push({
      selector: `node[layer > 1][phys = "${phys}"]`,
      style: { "background-color": color, "border-color": color, color: "#ffffff" },
    });
    style.push({
      selector: `edge[phys = "${phys}"]`,
      style: { "line-color": color, "target-arrow-color": color },
    });
  }
  // selected/safety_blocking 要放在医家配色循环之后：医家配色规则也会设
  // border-color，样式表按声明顺序层叠（后面的覆盖前面同名属性），选中框和
  // 安全拦截的红框必须始终盖过医家色，不能被医家配色循环反过来盖掉。
  style.push({
    // [?field] 是 cytoscape 的"布尔真值"选择器，跟内建的 :selected（用户
    // 交互选中状态）是两回事——这里选的是我们自己的数据字段
    // data.selected（这位医家当前选中的候选方），别选错成 :selected。
    selector: "node[?selected]",
    style: { "border-width": 3 },
  });
  style.push({
    selector: "node[?safety_blocking]",
    style: { "border-color": "#dc2626", "border-width": 3 },
  });
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
      const srcLabel = { classic: "经典方", modified: "加减方", composed: "自拟方" }[data.source] || data.source || "";
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
  cy.nodes().forEach((n) => n.toggleClass("gt-faded", !nodeIds.has(n.id())));
  cy.edges().forEach((e) => {
    const key = `${e.data("source")}::${e.data("target")}`;
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
    style: buildStylesheet(),
    layout: { name: "preset" },
    userZoomingEnabled: true,
    userPanningEnabled: true,
    boxSelectionEnabled: false,
  });
  cy.on("tap", "node", (evt) => {
    const n = evt.target;
    openEvidence(n.id());
    // M7：只有症状节点（层0）触发路径高亮——点其他层级的节点应该只是照旧
    // 打开证据侧栏，不应该顺带清空/改变当前的路径高亮状态（那样点一下证型
    // 节点看侧栏，画面上的高亮却跟着消失，会很意外）。
    if (Number(n.data("layer")) === 0) handleSymptomClick(n.id());
  });
  cy.on("tap", (evt) => {
    if (evt.target === cy) {
      closeEvidence();
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
// 前端侧止血（见下面 GB_MAX_NEW_NODES / GB_INITIAL_SYNDROMES / GB_COSE_MAX_NODES）。"渐进式展开"因此是纯前端的
// 显示策略：拿到全量数据后先只画证型节点，点开才把它连着的证素/症状加进
// 画布，不是一次性把 123 个节点全铺开——那样会是一团看不出结构的乱线。

let gbCy = null;
let gbGraphData = null;         // 已经从服务端拿到的那部分图（第一页 + 展开/搜索并进来的）
let gbIndex = null;             // { nodeById, edgesByNode }，拿到数据后建一次，避免每次展开都线性扫全部边
let gbVisibleIds = new Set();   // 当前画布上已经显示的节点 id
let gbCurrentPhysician = null;
let gbShowCaseLayer = false;    // 只有 has_case_layer 为真时这个开关才有意义（按钮本身也只在那时才显示）

function buildGraphBrowserStylesheet() {
  return [
    {
      selector: "node",
      style: {
        // is_category 决定形状用函数值而不是 CSS 选择器匹配布尔字段——避免
        // 对 cytoscape 选择器语法里"布尔真值怎么写"这件事做不必要的猜测，
        // 函数值直接读 JS 里的布尔值，语义没有歧义。
        shape: (ele) => (ele.data("is_category") ? "diamond" : "round-rectangle"),
        label: "data(label)", "text-valign": "center", "text-halign": "center",
        "font-size": 11, color: "#24262b", "background-color": "#f4f4f6",
        "border-width": 1, "border-color": "#c7c9d1", padding: "6px",
        width: "label", height: "label", "text-wrap": "wrap", "text-max-width": "90px",
      },
    },
    { selector: 'node[node_type = "element"]', style: { "background-color": "#efe7fb", "border-color": "#8b6fd9", color: "#4b2e9e" } },
    { selector: 'node[node_type = "syndrome"]', style: { "background-color": "#e8f4f8", "border-color": "#2f7d95", color: "#1a4652" } },
    { selector: 'node[node_type = "case"]', style: { "background-color": "#fdf1ea", "border-color": "#C2410C", color: "#7a2c07" } },
    { selector: "node.gb-search-hit", style: { "border-width": 3 } },
    {
      selector: "edge",
      style: {
        width: 1.4, "line-color": "#d3d5db", "target-arrow-color": "#d3d5db",
        "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.7,
      },
    },
  ];
}

function ensureGraphBrowserCanvas() {
  if (gbCy) return gbCy;
  gbCy = cytoscape({
    container: document.getElementById("gb-cy"),
    elements: [],
    style: buildGraphBrowserStylesheet(),
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
const GB_INITIAL_SYNDROMES = 80;
const GB_COSE_MAX_NODES = 200;

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
    // 大图不跑力导向：cose 对几百个节点会把浏览器卡住，grid 是 O(n) 的。
    const layoutName = gbVisibleIds.size > GB_COSE_MAX_NODES ? "grid" : "cose";
    gbCy.layout({ name: layoutName, animate: false, fit: true, padding: 24 }).run();
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
async function gbFetchInto(url, statusText) {
  const status = document.getElementById("gb-search-status");
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    gbMergeGraph(data.graph);
    if (status && statusText) status.textContent = statusText(data.page);
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

async function gbExpandNode(nodeId) {
  await gbFetchInto(
    `/api/graph/neighbors?node=${encodeURIComponent(nodeId)}&limit=${GB_MAX_NEW_NODES}`,
    (page) => page.truncated
      ? `这个节点有 ${page.total} 个关联，只展开了前 ${page.returned} 个`
      : `展开了 ${page.returned} 个关联节点`
  );
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

  // F1：重置 = 重新取第一页，不是从本地全量里切一刀（本地已经没有全量了）。
  gbCursor = 0;
  gbMergeGraph(gbGraphData.graph);
  gbRenderMoreButton();
  gbSetHint(gbTotalSyndromes > gbVisibleIds.size
    ? `证型共 ${gbTotalSyndromes} 条，先显示 ${gbVisibleIds.size} 条——用搜索框定位、点节点展开关联，或点"加载更多证型"`
    : "点击节点展开关联的证素/症状，或用上方搜索框定位");
}

// F1：翻页状态。cursor 是位置偏移，服务端按 networkx 的插入顺序切片，
// build_graph 是确定性写入的，所以同一份 graph.json 上这个顺序稳定。
let gbCursor = 0;
let gbTotalSyndromes = 0;

function gbRenderMoreButton() {
  const btn = document.getElementById("gb-more");
  if (!btn) return;
  const more = gbTotalSyndromes > 0 && gbCursor !== null && gbCursor < gbTotalSyndromes;
  btn.hidden = !more;
  if (more) btn.textContent = `加载更多证型（还有 ${gbTotalSyndromes - gbCursor} 条）`;
}

async function gbLoadMoreSyndromes() {
  if (gbCursor === null) return;
  const data = await gbFetchInto(
    `/api/graph?node_types=syndrome&limit=${GB_INITIAL_SYNDROMES}&cursor=${gbCursor}`,
    (page) => `又加载了 ${page.returned} 条证型`
  );
  if (!data) return;
  gbCursor = data.page.next_cursor;
  gbTotalSyndromes = data.page.total;
  gbRenderMoreButton();
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
    // F1：**不再一次拿全图**。原来这里拉的是整张 data/graph.json——注释当年
    // 写的是"123 个节点、377 条边，一次装得下"，AutoDL 上现在是 2256/3771，
    // 教材五本扩完还要再翻几倍。现在只取第一页证型，其余靠展开/搜索/加载更多。
    const resp = await fetch(`/api/graph?node_types=syndrome&limit=${GB_INITIAL_SYNDROMES}`);
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`服务返回错误（HTTP ${resp.status}）：${text}`);
    }
    gbGraphData = await resp.json();
    gbCursor = gbGraphData.page ? gbGraphData.page.next_cursor : null;
    gbTotalSyndromes = gbGraphData.page ? gbGraphData.page.total : 0;
    gbBuildIndex();
    renderGbLambda1Note(gbGraphData.lambda1_note);
    renderGbStats(gbGraphData.stats);
    populateGbPhysicianSelect(gbGraphData.physicians);
    // 医案层是空的（has_case_layer=false）就不显示这个按钮，不是显示一个
    // 点了没反应的——这个 sandbox 里 data/graph.json 只有国标层数据，
    // AutoDL 上跑过 attach_cases 之后 has_case_layer 会是 true，按钮才出现。
    document.getElementById("gb-layer-toggle").hidden = !gbGraphData.has_case_layer;
    gbResetView();
  } catch (err) {
    hint.hidden = true;
    errBox.textContent = `图谱加载失败：${err.message || err}`;
    errBox.classList.add("show");
  }
}




let growToken = 0; // 每次新的生长自增，旧的循环据此提前退出

async function growGraph(graph, { animate = true } = {}) {
  if (!(await ensureCytoscape())) { showError(CYTOSCAPE_MISSING_MSG); return; }
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
  cy.style(buildStylesheet());
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
// index.html 里 graph.js 和 app.js 是两个 <script src>，**共用同一个全局作用域**，
// 所以严格说不需要命名空间就能互相调用。仍然显式挂一份的理由是**可读性和可测性**：
// 拆成两个文件之后，"哪些函数是给另一个文件用的"必须能一眼看出来，否则两边会
// 越缠越紧、下次再拆就拆不动了。tests/test_web_split.py 把这份清单写死，
// 少一个名字就红——那种改动在浏览器里表现为"点了没反应"，不报任何错。
window.TCM = Object.assign(window.TCM || {}, {
  ensureCytoscape, computeLayout, buildStylesheet, growGraph, renderGraph,
  replayGraph, skipAnimation, computeHighlightPath, applyPathHighlight,
  clearPathHighlight, handleSymptomClick, describeNodeTooltip,
  describeEdgeTooltip, showTooltip, hideTooltip, loadGraphBrowserData,
  gbExpandNode, gbSearch, gbResetView, gbToggleLayer,
});
