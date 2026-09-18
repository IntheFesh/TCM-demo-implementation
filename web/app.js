// 配色由 /api/consult 响应里的 result.color 提供（源头是 core/physicians.py）。
// 这里只保留图渲染用的运行时缓存，每次响应回来时填充。
let PHYSICIAN_COLORS = {};
// hover tooltip 用：herb 节点的 data 里只带 phys（id），不带 pname（syn 节点带了，
// herb 没有——to_graph() 没为 herb 节点重复存一份，这里在前端补一份映射，不是
// 因为后端漏了字段，是没必要为一个纯展示用途改后端契约（模块6"不改后端"）。
let PHYSICIAN_NAMES = {};

// 三列表头要显示的年代与学派（"叶天士 / 1667-1746 / 温病"）。跟身份色同一批
// 数据、同一个来源（core/physicians.py → /health → injectPhysicianColors），
// 不在前端另抄一张表：注册表加第四位医家时这里自动跟上。
let PHYSICIAN_META = {};
// R18-I：注册表里 enabled=false 的那几位（李可、王云启）。由 injectPhysicianColors
// 从 /health 算出来，同时决定「参考医家」那一栏列谁、三列**不**列谁。
let REFERENCE_PHYSICIANS = [];

// 首屏三条示例主诉。同样从 /health 下发（core/examples.py），不写死在这里——
// DEMO.md 和录制清单里已经各有一份，第三份副本漂一个标点就是演示当场 LLMError。
let EXAMPLE_COMPLAINTS = [];

// R42：`let cy` 搬到 graph.js 去了。**这是 R41 那条反向依赖的第二例**：
// 问诊图的 cytoscape 实例只有 graph.js 在用（app.js 从声明它的那天起一次都
// 没读过），而声明在这里意味着 graph.js 单独被加载时 `cy` 不存在。
// 浏览器里两个 script 共享全局作用域，所以从来没报错过——是 R42 给
// `exportGraphPng` 写只加载 graph.js 的 node 测试时，第一次跑就
// `ReferenceError: cy is not defined`。跟 `LAYER_X` 那四个常量同一个形状、
// 同一个修法（搬到被依赖的那一侧）。

// ---------- D1：BYOK + 共享额度 + 用量看板 ----------
//
// key 存 sessionStorage 不存 localStorage：关掉标签页就没了。localStorage 会一直
// 留在这台机器上，而这是个公开 demo，很可能有人在共用的电脑上填过 key。
// 传输走 X-LLM-Key 请求头不走请求体：请求体在很多地方会被整条记进日志。
//
// ⚠ **但 sessionStorage 不是安全存储**：同源的任何脚本都读得到它，页面上只要
// 出现一次 XSS，这把 key 就跟着走了。选它是因为在"不落服务端"和"刷新页面不用
// 重填"之间它是最不坏的一档，不是因为它安全。输入框旁边的提示原话照实说
// "只存在本标签页"，不写"安全保存"这类会让人放心的措辞。真正的防线是
// **这把 key 应该是访问者专门为试用这个 demo 建的、额度有限的 key**。
const BYOK_STORAGE_KEY = "tcm_byok_key";

function getByokKey() {
  try { return sessionStorage.getItem(BYOK_STORAGE_KEY) || ""; } catch (e) { return ""; }
}
function setByokKey(v) {
  try {
    if (v) sessionStorage.setItem(BYOK_STORAGE_KEY, v);
    else sessionStorage.removeItem(BYOK_STORAGE_KEY);
  } catch (e) { /* 隐私模式下 sessionStorage 可能不可用，退化成这次不带 key */ }
}
function authHeaders(base) {
  const h = Object.assign({}, base || {});
  const k = getByokKey();
  if (k) h["X-LLM-Key"] = k;
  return h;
}

// R24：token 面板要同时用到"这一次问诊的 manifest"和"今天的用量快照"，
// 而这两份数据由两条不同的路径刷新（问诊回来 / `/api/usage` 轮询）。
// 各存一份最近值，谁回来谁刷自己那段——不存的话另一段会在刷新时被清空。
let lastManifest = null;
let lastUsage = null;

function usageText(u) {
  if (!u) return "";
  if (u.mode === "byok") return "正在使用你自己的 API key，不占用站点额度。";
  // 降级的原话由服务端给，前端不自己编一套——两处措辞不一致时用户不知道信哪个。
  if (u.degraded) return u.reason;
  // "次数"是服务端按 calls_per_consult 折算好的，前端不再自己算一份。
  return `站点共享额度：今日约剩 ${u.remaining_consults_estimate} 次问诊`
    + `（${u.remaining_calls}/${u.ip_limit_calls} 次模型调用；开 ReAct 约消耗 3 倍）。`
    + `额度自 ${String(u.since || "").slice(0, 16).replace("T", " ")} 起算，服务重启会重置。`;
}

// R17 §5.1：顶栏右侧那一枚「今日约剩 N 次」。
//
// **只有次数，没有句子**：顶栏那一行装不下 usageText() 那三句话，而那三句话
// 是点开 BYOK 之后要看的（"额度怎么算的、什么时候重置"）。两处各答各的问题，
// 不是同一份文案裁两次。
//
// 返回 null 表示不显示这枚 chip（BYOK 模式下额度跟访问者无关）。
function quotaChipText(u) {
  if (!u) return null;
  if (u.mode === "byok") return "自带 key";
  if (u.degraded) return "额度已用完";
  const n = u.remaining_consults_estimate;
  return n === null || n === undefined ? null : `今日约剩 ${n} 次`;
}

// 三档状态：normal / warn（80%）/ degraded（100%）。**判据全在服务端算好**
// （u.warn / u.degraded），前端不自己拿 remaining/limit 再除一遍——除法写两处，
// 阈值改一次就会有一处忘了改。
function quotaChipLevel(u) {
  if (!u || u.mode === "byok") return "normal";
  if (u.degraded) return "degraded";
  return u.warn ? "warn" : "normal";
}

function renderUsage(u) {
  lastUsage = u || null;
  const chip = document.getElementById("quota-chip");
  const text = document.getElementById("usage-text");
  if (u && text) text.textContent = usageText(u);
  if (chip) {
    const label = quotaChipText(u);
    chip.textContent = label || "";
    chip.classList.toggle("show", !!label);
    for (const level of ["normal", "warn", "degraded"]) {
      chip.classList.toggle(`q-${level}`, quotaChipLevel(u) === level);
    }
  }
  renderDegradeBanner(u);
  // 今日累计那一段只在 /api/usage 回来时才会变；本次那几段由
  // renderConsultResult 传 manifest 进来。两个来源各刷自己那部分，
  // 所以这里带上 lastManifest（没有问诊过就是 null，面板只有"今日累计"）。
  renderTokenPanel(lastManifest, u);
}

// §5.1 第 3 条：**超限降级到回放而不是报错**——访问者仍能看到预录主诉的完整
// 效果。所以这一行用 --surface-2 底、不是警告色：降级不是错误，是换了个后端
// 继续跑。原话由服务端给（u.reason），前端不自己编一套——两处措辞不一致时
// 用户不知道信哪个。
// R24：token 面板（R21 的数据）。两段分开报，**因为它们的分母不同**：
//   · 这一次问诊：前缀各段 token（prefix_tokens_by_section）+ 本次命中率
//   · 今天累计：命中/未命中/输出三项 + 今日命中率（/api/usage 的 tokens_today）
// 合成一段会让"这次命中了没有"和"今天整体命中率"混成一个数，而前者是要看的，
// 后者是要报的。
function tokenSectionRowsHtml(sections) {
  if (!sections) return "";
  const rows = Object.entries(sections)
    .map(([name, n]) => `<tr><td>${escapeHtml(name)}</td>`
      + `<td class="tp-num">${escapeHtml(formatTokenCount(n))}</td></tr>`).join("");
  return `<table class="tp-table"><tbody>${rows}</tbody></table>`;
}

// 千分位。**不缩写成 k/M**：前缀 token 数是要跟 500,000 预算对着看的，
// "180k" 和 "18万" 在同一页里混着出现过一次就会有人算错一个数量级。
function formatTokenCount(n) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("en-US");
}

// 命中率显示成百分比；null 是"这个后端不报这个数"，**不是 0%**
// （R21 那条：0 会被读成"跑了但一次没命中"）。
function formatHitRatio(r) {
  return (r === null || r === undefined) ? "未报" : `${Math.round(r * 1000) / 10}%`;
}

function tokenPanelHtml(manifest, usage) {
  const m = manifest || {};
  const parts = [];
  if (m.prefix_tokens_by_section) {
    parts.push(`<div class="tp-block"><div class="tp-title">本次前缀各段 token`
      + `（${escapeHtml(m.retriever_mode || "?")}）</div>`
      + tokenSectionRowsHtml(m.prefix_tokens_by_section) + `</div>`);
  }
  if (m.cache_hit_tokens !== undefined || m.cache_miss_tokens !== undefined) {
    parts.push(`<div class="tp-block"><div class="tp-title">本次前缀缓存</div>`
      + `<div class="tp-line">命中 ${escapeHtml(formatTokenCount(m.cache_hit_tokens))}`
      + `　未命中 ${escapeHtml(formatTokenCount(m.cache_miss_tokens))}`
      + `　命中率 ${escapeHtml(formatHitRatio(m.cache_hit_ratio))}</div></div>`);
  }
  if (m.best_of_n) {
    parts.push(`<div class="tp-block"><div class="tp-title">本次采样</div>`
      + `<div class="tp-line">每位医家采 ${escapeHtml(String(m.best_of_n))} 次`
      + `　推理档 ${escapeHtml(String(m.reasoning_effort || "未开思考"))}`
      + `　推理 token ${escapeHtml(formatTokenCount(m.reasoning_tokens))}</div></div>`);
  }
  const td = usage && usage.tokens_today;
  if (td) {
    parts.push(`<div class="tp-block"><div class="tp-title">今日累计</div>`
      + `<div class="tp-line">命中 ${escapeHtml(formatTokenCount(td.cache_hit))}`
      + `　未命中 ${escapeHtml(formatTokenCount(td.cache_miss))}`
      + `　输出 ${escapeHtml(formatTokenCount(td.output))}`
      + `　命中率 ${escapeHtml(formatHitRatio(td.cache_hit_ratio))}</div></div>`);
  }
  if (!parts.length) return "";
  return `<details class="token-panel-details"><summary>token 与缓存</summary>`
    + parts.join("") + `</details>`;
}

function renderTokenPanel(manifest, usage) {
  const box = document.getElementById("token-panel");
  if (!box) return;
  box.innerHTML = tokenPanelHtml(manifest, usage);
}

// R24：顶栏折叠。**默认展开**——一个默认藏起来的控件区会让人以为功能不存在。
const TOPBAR_COLLAPSED_KEY = "tcm.topbarCollapsed";

function topbarCollapsed() {
  try {
    return localStorage.getItem(TOPBAR_COLLAPSED_KEY) === "1";
  } catch (e) {
    // 隐私模式/禁用存储时读会抛。折叠状态是个便利，读不到就按默认（展开）来，
    // 不是让页面炸掉。
    return false;
  }
}

function applyTopbarCollapsed(collapsed) {
  const box = document.getElementById("topbar-controls");
  const btn = document.getElementById("topbar-toggle");
  if (box) box.classList.toggle("is-collapsed", !!collapsed);
  if (btn) btn.setAttribute("aria-expanded", String(!collapsed));
}

function toggleTopbar() {
  const next = !topbarCollapsed();
  try {
    localStorage.setItem(TOPBAR_COLLAPSED_KEY, next ? "1" : "0");
  } catch (e) { /* 存不了就只在本次生效，不影响功能 */ }
  applyTopbarCollapsed(next);
}

function degradeBannerText(u) {
  if (!u || !u.degraded) return null;
  return u.reason || "站点共享额度已用完，已切换到回放模式：结果来自预先录制的真实推理。";
}

function renderDegradeBanner(u) {
  const el = document.getElementById("degrade-banner");
  if (!el) return;
  const text = degradeBannerText(u);
  el.textContent = text || "";
  el.classList.toggle("show", !!text);
}

// /health 拿不到 = 后端不在。**要说出来**：一片空白让人以为页面还在加载，
// 而它已经加载完了，只是点「辨证」会失败。
function renderOfflineBanner(connected) {
  const el = document.getElementById("offline-banner");
  if (!el) return;
  el.textContent = connected ? "" : "服务未连接：页面已加载，但后端没有响应，现在点「辨证」会失败。";
  el.classList.toggle("show", !connected);
}

async function refreshUsage() {
  try {
    const resp = await fetch("/api/usage", { headers: authHeaders() });
    if (!resp.ok) return;
    renderUsage(await resp.json());
  } catch (e) { /* 看板拿不到不影响问诊本身，静默 */ }
}

// R56 §6：本例知识地图。有活跃问诊（LAST_RESULT 非空）时切到图谱页要直接
// 定位到这次的证型，不是回退到脾胃门首屏——首屏那套"证素在内圈"的布局
// 回答的是另一个问题（这个知识库整体长什么样），医师这时候要的是"这一次
// 这个证，跟哪些证容易混、还有什么别的治法方剂、名老中医怎么用"。
//
// 只在证型**变了**的时候才重新聚焦（`gbAutoFocusedSyndrome` 记的是上一次
// 自动聚焦过的证型名）——不然医师在图谱页手动探索到别处、切回问诊页看一眼
// 结果、再切回来，会被强制拉回原地，那不是"回到本例地图"，是打断医师
// 自己的浏览。
let gbAutoFocusedSyndrome = null;

//: 当前这次问诊结论的证型名。**只取 results[0]**——图谱页一次只能聚焦一个
//: 节点，legacy 模式下三位医家各有各的证型时，取第一个作为代表足够回答
//: "这次大概在看哪个证"，不必也不该在这里另起一套"三证怎么合成一个"的判断
//: （那件事 renderDivergence 已经做过了，产品面看分歧度就是看它）。
function currentSyndromeName() {
  const r = (LAST_RESULT && LAST_RESULT.results || [])[0];
  if (!r) return null;
  const st = r.s3_structured;
  return (st ? st.syndrome.name : (r.s3 || {}).syndrome) || null;
}

function switchTab(tab) {
  const toConsult = tab === "consult";
  document.getElementById("tab-btn-consult").classList.toggle("active", toConsult);
  document.getElementById("tab-btn-graph-browser").classList.toggle("active", !toConsult);
  document.getElementById("tab-consult").hidden = !toConsult;
  document.getElementById("tab-graph-browser").hidden = toConsult;
  if (toConsult) return;
  const syndrome = currentSyndromeName();
  if (syndrome && syndrome !== gbAutoFocusedSyndrome) {
    gbAutoFocusedSyndrome = syndrome;
    gbFocusOnNodeId(NODE_ID.syndrome(syndrome)).then((found) => {
      // 找不到（证候表覆盖不到这个证型）就不强行打开一个空释义面板——
      // 那会让医师以为点错了，实际是数据覆盖缺口，跟证候表本身"没有这一条
      // 就如实说"是同一条纪律。画布仍然留在首屏，不是留一片空白。
      if (found) openNodeExplain(NODE_ID.syndrome(syndrome), syndrome);
    });
    return;
  }
  if (!gbGraphData) loadGraphBrowserData();
}

document.getElementById("tab-btn-consult").addEventListener("click", () => switchTab("consult"));
document.getElementById("tab-btn-graph-browser").addEventListener("click", () => switchTab("graph-browser"));
document.getElementById("gb-category-select").addEventListener("change", (e) => {
  gbBrowseCategory(e.target.value);
});
document.getElementById("gb-search-btn").addEventListener("click", () => gbSearch(document.getElementById("gb-search").value));
document.getElementById("gb-search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") gbSearch(e.target.value);
});
document.getElementById("gb-physician-select").addEventListener("change", (e) => {
  gbCurrentPhysician = e.target.value;
  gbApplyPhysicianWeighting();
});
document.getElementById("gb-layer-toggle").addEventListener("click", gbToggleLayer);
document.getElementById("gb-reset-btn").addEventListener("click", gbResetView);


// 节点 id -> 该节点对应的证据。图上只有 id 和 label，医家的 refs 在
// data.results 里，点击时要能反查，所以每次响应回来先建一张索引。
let EVIDENCE = {};

function buildEvidenceIndex(data) {
  EVIDENCE = {};
  const put = (id, payload) => {
    if (!EVIDENCE[id]) EVIDENCE[id] = { title: payload.title, sections: [] };
    EVIDENCE[id].sections.push(payload);
  };

  for (const r of data.results || []) {
    const s2 = r.s2 || {};
    const s3 = r.s3 || {};

    put(NODE_ID.syndrome(s3.syndrome || ""), {
      title: `${s3.syndrome || "证型"}　·　${r.physician_name}`,
      physician_name: r.physician_name,
      color: r.color,
      reasoning: s3.reasoning,
      note: s3.note,
      treatment: s3.treatment_principle,
      formula: s3.formula,
      refs: r.refs || [],
      // 侧栏只把模型真正引用了的医案标成"引用"；检索到但没引用的单独列，
      // 否则幻觉引用在卡片上有红标、侧栏里却把全部检索结果当成结论的依据
      cited: new Set(s3.cited_case_ids || []),
      hallucinated: r.hallucinated || [],
      no_reference_cases: !!r.no_reference_cases,
      react_trace: r.react_trace || null,
    });

    for (const hit of s2.elements || []) {
      put(NODE_ID.element(hit.element, hit.kind), {
        title: `证素　${hit.element}`,
        physician_name: r.physician_name,
        color: r.color,
        element_kind: hit.kind === "location" ? "病位" : "病性",
        confidence: hit.confidence,
        supporting: hit.supporting_symptoms || [],
      });
      for (const sym of hit.supporting_symptoms || []) {
        put(NODE_ID.symptom(sym), {
          title: `症状　${sym}`,
          physician_name: r.physician_name,
          color: r.color,
          explained_by: hit.element,
          confidence: hit.confidence,
        });
      }
    }

    // M5：方剂(layer 3)/药材(layer 4) 都要建反查索引——层 3 从"用药"改成
    // "方剂"之后不再有旧式 herb::{physician}::{herb} 这种节点，图上出现的
    // 是每个候选方各一个 formula:: 节点、各自带自己的 herb:: 子节点，全部
    // 候选方都建索引（不是只建 selected 那个），否则点未选中的候选方节点
    // 侧栏是空的。
    (s3.formula_candidates || []).forEach((cand, i) => {
      put(NODE_ID.formula(cand.name), {
        title: `${cand.name}　·　${r.physician_name}`,
        physician_name: r.physician_name,
        color: r.color,
        source: cand.source,
        base_formula: cand.base_formula,
        confidence: cand.confidence,
        rationale: cand.rationale,
        selected: i === s3.selected,
        doses_count: cand.doses_count,
        usage: cand.usage,
        safety: cand.safety || null,
      });
      for (const item of cand.herb_items || []) {
        put(NODE_ID.herb(cand.name, item.name), {
          title: `${item.name}　·　${cand.name}`,
          physician_name: r.physician_name,
          color: r.color,
          formula: cand.name,
          dose: item.dose,
          dose_unit: item.dose_unit,
          processing: item.processing,
          decoction: item.decoction,
          role: item.role,
          function_in_formula: item.function_in_formula,
          refs: (r.refs || []).filter((x) => (x.herbs || []).some((h) => h.includes(item.name) || item.name.includes(h))),
        });
      }
    });
  }
}

// 置信度三档的中文名，**只有这一处**。
// 它跟 `URGENCY_LABEL` 的键长得一样（high/medium/low），但回答的**不是同一个
// 问题**——那张回答"这次要不要催病人马上就医"（导诊分级），这张回答"这条证素
// 推得有多稳"（模型自评）。合并成一张，以后改一边会连带动另一边，而两边的
// 阈值语义根本不同（CLAUDE.md 那条例外：不同问题就该是两张表，并写清区别）。
//
// 方剂来源那张（经典方/加减方/自拟方）在 graph.js：两张图都要用，而**依赖方向
// 只能 app.js → graph.js**（R13/R14 断开的那个循环），所以共用的东西放那边。
const CONFIDENCE_LABEL = { high: "高", medium: "中", low: "低" };

function confidenceLabel(v) { return CONFIDENCE_LABEL[v] || v || ""; }

function evidenceSectionHtml(s) {
  const rows = [];
  if (s.element_kind) rows.push(`<div class="meta">${s.element_kind}　置信度 ${escapeHtml(confidenceLabel(s.confidence))}</div>`);
  if (s.explained_by) rows.push(`<div class="meta">由证素「${escapeHtml(s.explained_by)}」解释　置信度 ${escapeHtml(confidenceLabel(s.confidence))}</div>`);
  if (s.supporting && s.supporting.length) {
    rows.push(`<div class="label">支撑症状</div><div>${escapeHtml(s.supporting.join("、"))}</div>`);
  }
  if (s.treatment) rows.push(`<div class="label">治法</div><div>${escapeHtml(s.treatment)}</div>`);
  if (s.formula) rows.push(`<div class="label">方剂</div><div>${escapeHtml(s.formula)}</div>`);

  // M5：方剂节点的候选方元信息（source/confidence/rationale……）。只有
  // formula:: 节点的 payload 才会带 source 字段，症状/证素/证型的 payload
  // 里没有这个键，条件判断天然把两类节点分开，不用额外传节点类型标记。
  if (s.source) {
    const srcLabel = formulaSourceLabel(s.source);
    const metaParts = [
      escapeHtml(srcLabel),
      s.base_formula ? "原方：" + escapeHtml(s.base_formula) : "",
      "置信度 " + escapeHtml(confidenceLabel(s.confidence)),
      s.selected ? "★已选" : "",
    ].filter(Boolean);
    rows.push(`<div class="meta">${metaParts.join("　")}</div>`);
  }
  if (s.rationale) rows.push(`<div class="label">立方思路</div><div>${escapeHtml(s.rationale)}</div>`);
  if (s.doses_count || s.usage) {
    const usageParts = [
      s.doses_count ? `${s.doses_count} 剂` : "",
      s.usage ? escapeHtml(s.usage) : "",
    ].filter(Boolean);
    rows.push(`<div class="meta">${usageParts.join("　")}</div>`);
  }
  if (s.safety) {
    const sf = s.safety;
    const blocking = [];
    if (sf.incompatible && sf.incompatible.length) {
      blocking.push("配伍禁忌：" + sf.incompatible.map((p) => p.join("与")).join("；"));
    }
    if (sf.dose_violations && sf.dose_violations.length) {
      blocking.push("剂量超限：" + sf.dose_violations.map((v) => `${v.herb} ${v.dose}${v.unit}`).join("；"));
    }
    const warn = [];
    if (sf.thermal_warning) warn.push(sf.thermal_warning);
    if (sf.decoction_missing && sf.decoction_missing.length) warn.push("煎法缺失：" + sf.decoction_missing.join("、"));
    if (sf.toxic_herbs && sf.toxic_herbs.length) warn.push("含毒性药材：" + sf.toxic_herbs.join("、"));
    if (blocking.length) rows.push(`<div class="label">⚠ 安全拦截</div><div class="meta">${escapeHtml(blocking.join("；"))}</div>`);
    if (warn.length) rows.push(`<div class="label">安全提示</div><div class="meta">${escapeHtml(warn.join("；"))}</div>`);
  }

  // M5：药材节点的剂量/炮制/煎法/君臣佐使/方中作用。
  if (s.dose != null || s.processing || s.decoction || s.role) {
    const doseParts = [
      s.dose != null ? `剂量 ${s.dose}${escapeHtml(s.dose_unit || "")}` : "",
      s.processing ? "炮制：" + escapeHtml(s.processing) : "",
      s.decoction ? "煎法：" + escapeHtml(s.decoction) : "",
      s.role ? escapeHtml(s.role) + "药" : "",
    ].filter(Boolean);
    rows.push(`<div class="meta">${doseParts.join("　")}</div>`);
  }
  if (s.function_in_formula) rows.push(`<div class="label">方中作用</div><div>${escapeHtml(s.function_in_formula)}</div>`);

  if (s.reasoning) rows.push(`<div class="label">推理过程</div><div>${escapeHtml(s.reasoning)}</div>`);
  if (s.note) rows.push(`<div class="label">备注</div><div>${escapeHtml(s.note)}</div>`);

  if (s.no_reference_cases) {
    rows.push(`<div class="label">引用医案</div><div class="meta">未检索到相关医案（相似度均低于阈值），本结论没有医案支撑。</div>`);
  }
  if (s.hallucinated && s.hallucinated.length) {
    rows.push(`<div class="label">⚠ 幻觉引用</div><div class="meta">模型引用了检索结果之外的 id：${escapeHtml(s.hallucinated.join("、"))}</div>`);
  }
  if (s.react_trace && s.react_trace.steps && s.react_trace.steps.length) {
    const steps = s.react_trace.steps.map((st) =>
      `<div class="meta">第${st.step}步 ${escapeHtml(st.action)}${st.note ? "（" + escapeHtml(st.note) + "）" : ""}：${escapeHtml((st.thought || "").slice(0, 80))}</div>`).join("");
    rows.push(`<div class="label">取证过程（${escapeHtml(s.react_trace.terminated_by)}，${s.react_trace.llm_calls} 次调用）</div>${steps}`);
  }
  if (s.refs && s.refs.length) {
    const citedRefs = s.refs.filter((r) => s.cited && s.cited.has(r.case_id));
    const otherRefs = s.refs.filter((r) => !(s.cited && s.cited.has(r.case_id)));
    const caseHtml = (r, i) => {
        const raw = r.excerpt || "";
        const long = raw.length > 110;
        return `<div class="ev-case">
          <div class="cid">${escapeHtml(r.case_id)}</div>
          <div class="meta">${escapeHtml(r.visit_label)}　相似度 ${escapeHtml(r.score)}${r.syndrome ? "　证：" + escapeHtml(r.syndrome) : ""}</div>
          <div>${escapeHtml((r.symptoms || []).slice(0, 5).join("；"))}</div>
          ${raw ? `<div class="ev-raw">${escapeHtml(raw)}</div>
            ${long ? '<span class="ev-more" onclick="this.previousElementSibling.classList.toggle(\'expanded\'); this.textContent = this.textContent === \'展开全文\' ? \'收起\' : \'展开全文\';">展开全文</span>' : ""}` : ""}
        </div>`;
      };
    if (s.cited) {
      if (citedRefs.length) rows.push(`<div class="label">引用医案（${citedRefs.length}）</div>${citedRefs.map(caseHtml).join("")}`);
      if (otherRefs.length) rows.push(`<div class="label">检索到但未引用（${otherRefs.length}）</div>${otherRefs.map(caseHtml).join("")}`);
    } else {
      rows.push(`<div class="label">相关医案（${s.refs.length}）</div>${s.refs.map(caseHtml).join("")}`);
    }
  }

  return `<div class="ev-sec">
    <div class="ev-phys" style="color: ${escapeHtml(s.color || "var(--ink)")};">${escapeHtml(s.physician_name)}</div>
    ${rows.join("")}
  </div>`;
}

// 打开侧栏前记住焦点在哪，关闭时还回去——否则用键盘操作的人在关掉面板之后
// 焦点落在 body 上，得重新 tab 一遍才能回到图上。
let _evidenceReturnFocus = null;

function openEvidence(nodeId) {
  const e = EVIDENCE[nodeId];
  if (!e) return;
  document.getElementById("evidence-title").textContent = e.title;
  document.getElementById("evidence-body").innerHTML = e.sections.map(evidenceSectionHtml).join("");
  const panel = document.getElementById("evidence-panel");
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  _evidenceReturnFocus = document.activeElement || null;
  const closeBtn = document.getElementById("evidence-close");
  if (closeBtn && closeBtn.focus) closeBtn.focus();
}

function closeEvidence() {
  const panel = document.getElementById("evidence-panel");
  panel.classList.remove("open");
  panel.setAttribute("aria-hidden", "true");
  if (_evidenceReturnFocus && _evidenceReturnFocus.focus) _evidenceReturnFocus.focus();
  _evidenceReturnFocus = null;
}

// R1 分层：君臣（核心判断）/ 佐使（加减）各自的文案。每一层都跟**自己那一层的**
// ε 比，不跟 ε_online 比——整方的地板拿来卡君臣层会低估一致性、卡佐使层会高估
// 发散（CLAUDE.md「任何数字都必须带对照」，对照要对上是同一层的）。
const LAYER_LABELS = { core: "君臣", adjunct: "佐使" };

function layerText(divergence, key) {
  const j = divergence[key + "_jaccard"];
  const eps = divergence["epsilon_" + key];
  const label = LAYER_LABELS[key];
  if (j === null || j === undefined) {
    // null 不是 0：0 的意思是"两边完全相同"，null 是"没数据"（至少一位医家
    // 这一层没有标注 role 的药）。把它显示成 0 会凭空报出一个"完全一致"。
    return `${label} 不适用（至少一位医家这一层没有标注 role 的药）`;
  }
  if (eps === null || eps === undefined) {
    return `${label} ${j}（ε_${key} 未测，此数暂无参照）`;
  }
  return `${label} ${j}（ε_${key}=${eps}，${j <= eps ? "在抖动范围内" : "已超出"}）`;
}

function physicianNameMap(divergence) {
  // divergence 的数据字段一律按 id 存（n_unroled、western_drug_overlap.by_physician），
  // 展示层要中文名——CLAUDE.md「数据文件里存 id，展示层用中文名」。名字从 pairs
  // 里取（每对都带 name_a/name_b，两位医家时那一对就覆盖了全部），查不到就退回
  // id 原样显示，不编一个名字出来。
  const map = {};
  for (const p of divergence.pairs || []) {
    if (p.a) map[p.a] = p.name_a || p.a;
    if (p.b) map[p.b] = p.name_b || p.b;
  }
  return map;
}

function layerVerdict(divergence) {
  const c = divergence.core_jaccard, a = divergence.adjunct_jaccard;
  const ec = divergence.epsilon_core, ea = divergence.epsilon_adjunct;
  // 四个数缺任何一个就不给结论：这句话的全部意义在于"哪一层超出了自己的地板"，
  // 缺了地板就只是在比两个没有参照的数。
  if ([c, a, ec, ea].some((v) => v === null || v === undefined)) return "";
  if (c <= ec && a > ea) return "；核心判断一致、加减用药不同";
  if (c > ec && a > ea) return "；核心判断与加减用药都有分歧";
  if (c > ec && a <= ea) return "；核心判断有分歧、加减用药一致";
  return "；核心判断与加减用药都在抖动范围内";
}

function divergenceBannerText(divergence) {
  if (divergence && divergence.herb_jaccard !== null && divergence.herb_jaccard !== undefined) {
    const j = divergence.herb_jaccard;
    const eps = divergence.epsilon_online;
    const shared = divergence.shared_herbs || [];
    const tp = divergence.treatment_principle_same ? "治法一致" : "治法不同";
    // 三档判定，按 offline/estimate_epsilon.py 产出的噪声地板分档：
    //   没测过 ε —— 退回旧的三段式粗判，并如实标注"未测噪声地板"
    //   jaccard <= ε —— 差异在同一设定重复跑本来就会有的抖动范围内，不算分歧
    //   ε < jaccard —— 按原来 0.3/0.7 两档粗分程度
    let level, epsNote;
    if (eps === null || eps === undefined) {
      level = j <= 0.3 ? "用药高度重合" : j <= 0.7 ? "用药部分重合" : "用药基本不重合";
      epsNote = "；对照基准 ε 尚未估算，此数暂无参照";
    } else if (j <= eps) {
      level = "用药差异在模型抖动范围内，不构成分歧";
      epsNote = `；ε=${eps}`;
    } else {
      level = j <= 0.5 ? "用药部分重合" : "用药基本不重合";
      epsNote = `；ε=${eps}（已超出噪声范围）`;
    }
    const sharedText = shared.length ? `，共用 ${shared.join("、")}` : "";
    let text = `${tp}；${level}（药物 Jaccard 距离 ${j}${sharedText}${epsNote}）`;
    // 1.3（E2）：三位医家时 herb_jaccard 是三家交并比（只有三家都用的药才算
    // 共同，天然偏向 1.0），分不清师承内和跨学派——第二行按两两配对列出来，
    // 师承内均值 / 跨学派均值并列。只有两位医家时 pairs 只有一对，不重复报。
    const pairs = divergence.pairs || [];
    if (pairs.length >= 2) {
      const groupLabel = { lineage: "师承内", cross_school: "跨学派", unknown: "学派未知" };
      const pairText = pairs.map((p) => {
        const jac = p.herb_jaccard === null || p.herb_jaccard === undefined ? "不适用" : p.herb_jaccard;
        const gap = p.year_gap === null || p.year_gap === undefined ? "" : `，相隔 ${p.year_gap} 年`;
        return `${p.name_a}×${p.name_b} ${jac}（${groupLabel[p.group] || p.group}${gap}）`;
      }).join("　");
      const lm = divergence.lineage_mean, cm = divergence.cross_school_mean;
      const means = (lm !== null && lm !== undefined && cm !== null && cm !== undefined)
        ? `；师承内均值 ${lm} vs 跨学派均值 ${cm}（${cm > lm ? "跨学派分歧更大" : "跨学派分歧未大于师承内"}）`
        : "";
      text += `\n两两配对：${pairText}${means}`;
    }
    // R1 第三行：分层。整方那个数（herb_jaccard）说不清 0.53 里有多少是核心
    // 判断不一致、多少只是佐使加减不同——这一行就是把它拆开。
    if ("core_jaccard" in divergence || "adjunct_jaccard" in divergence) {
      const unroled = divergence.n_unroled || {};
      const names = physicianNameMap(divergence);
      const unroledText = Object.keys(unroled).length
        ? `　未标注 role：${Object.entries(unroled)
            .map(([p, n]) => `${names[p] || p} ${n} 味`)
            .join("　")}`
        : "";
      text += `\n分层：${layerText(divergence, "core")}　`
        + `${layerText(divergence, "adjunct")}${layerVerdict(divergence)}${unroledText}`;
    }
    return text;
  }
  if (divergence && divergence.same === false) {
    // A3：医家数从 pairs 推，不写死"两位"——注册张锡纯之后这句话就是错的。
    // 推不出来（pairs 缺失）时退回不带数字的说法，不编一个数出来。
    const n = Object.keys(physicianNameMap(divergence)).length;
    const who = n >= 2 ? `${n} 位医家` : "各位医家";
    return `${who}的辨证结论不同（按证型名称精确比对，为粗略判定）`;
  }
  return null;
}

// R3 演示模式那行小字。文案由服务端给（api/main.py::demo_mode_info 的 notice），
// 前端不自己拼措辞——措辞是诚实性的一部分，不该有两个版本；服务端漏给 notice
// 时才用这里的兜底句，兜底句也必须说清"非实时调用"这件事。
function demoModeText(demoMode) {
  if (!demoMode) return null;
  if (demoMode.notice) return demoMode.notice;
  const date = (demoMode.recorded_at || "").slice(0, 10) || "未记录日期";
  const model = demoMode.model || "未记录模型";
  return `演示模式：结果来自 ${date} 录制的真实推理（${model}），非实时调用`;
}

function renderDemoMode(demoMode) {
  const banner = document.getElementById("demo-mode-banner");
  if (!banner) return;
  const text = demoModeText(demoMode);
  if (text === null) {
    // 实时调用：不显示这行字（也不显示"这是实时调用"——没有人需要被告知默认行为）
    banner.textContent = "";
    banner.classList.remove("show");
    return;
  }
  banner.textContent = text;
  banner.classList.add("show");
}

// 页面一加载就问一次：访问者应该在点第一次问诊**之前**就知道这是演示模式，
// 不是跑完才被告知"刚才那个不是现场跑的"。/health 拿不到就静默跳过——
// 这行提示的缺失不该让整个页面起不来，而问诊响应里还会再带一次（每次都带）。
// 把三家身份色注入成 CSS 变量。**唯一来源是 core/physicians.py**
// （docs/DESIGN.md §2.1 的订正 + CLAUDE.md 第 31 条前端小节：写死的常量也算一处
// 实现）。app.css 的 :root 里这几个变量只写了兜底值，真值在这里灌进去——注册表加
// 第四位医家时，这里按 id 自动多注入一组，CSS 一个字都不用改。
//
// id → CSS 变量名的映射也不写死：`--phys-<id>` 是规则，`--ye/--wu/--zhang` 是给
// 既有规则用的别名。两者一起注入，新医家走前者，老规则走后者。
const PHYSICIAN_CSS_ALIAS = { ye_tianshi: "ye", wu_jutong: "wu", zhang_xichun: "zhang" };

function injectPhysicianColors(physicians) {
  if (!Array.isArray(physicians)) return;
  // R18-I：谁算「参考医家」由注册表的 enabled 说了算，前端不列名单。
  REFERENCE_PHYSICIANS = physicians
    .filter((p) => p && p.id && p.enabled === false)
    .map((p) => p.id);
  const root = document.documentElement;
  for (const p of physicians) {
    if (!p || !p.id) continue;
    PHYSICIAN_COLORS[p.id] = p.color;
    PHYSICIAN_NAMES[p.id] = p.name;
    PHYSICIAN_META[p.id] = { years: p.years, school: p.school, name: p.name };
    const names = [`--phys-${p.id}`];
    if (PHYSICIAN_CSS_ALIAS[p.id]) names.push(`--${PHYSICIAN_CSS_ALIAS[p.id]}`);
    for (const name of names) {
      if (p.color) root.style.setProperty(name, p.color);
      if (p.color_bg) root.style.setProperty(`${name}-bg`, p.color_bg);
    }
  }
}

// R40：预热进度条。后端改成「先监听再预热」之后，`/health` 在知识库加载完
// 之前回 **503 带进度**——503 不是"后端不在"，响应体是完整的。
//
// 这块横幅是动态建的、不写进 index.html：它是**瞬时状态**，不是页面结构，
// 而 index.html 那份契约（≤250 行）守的是结构文件不该越长。
function renderWarmupBanner(warmup) {
  let el = document.getElementById("warmup-banner");
  if (!warmup || warmup.ready) { if (el) el.remove(); return; }
  if (!el) {
    el = document.createElement("div");
    el.id = "warmup-banner";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.className = "show";
    const anchor = document.getElementById("offline-banner");
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(el, anchor);
    else document.body.prepend(el);
  }
  // 逐项列出来而不是只给一个百分比：哪一项还没好决定了现在能做什么
  // （本体层没好 → 符号验证判不了；检索器没好 → 医案检索是空的）。
  const pending = (warmup.steps || [])
    .filter((s) => s.status === "pending" || s.status === "running")
    .map((s) => s.label || s.step);
  el.textContent = `正在加载知识库（${warmup.progress}）：${pending.join("、")}` +
    "——现在可以浏览页面，点「辨证」会等它加载完。";
}

// 预热还没完就隔一会儿再问一次。**不轮询到永远**：到了上限就停下并如实说
// 「预热没能在 N 秒内完成」，而不是让一个转圈的横幅永远挂着。
const WARMUP_POLL_MS = 1000;
const WARMUP_POLL_MAX = 180;
let warmupPolls = 0;

function scheduleWarmupRecheck() {
  if (warmupPolls >= WARMUP_POLL_MAX) {
    const el = document.getElementById("warmup-banner");
    if (el) el.textContent = `知识库预热超过 ${WARMUP_POLL_MAX} 秒还没完成，` +
      "服务仍可用但首次辨证会更慢；请查看服务端日志。";
    return;
  }
  warmupPolls += 1;
  setTimeout(pollWarmupOnly, WARMUP_POLL_MS);
}

// R41：轮询**只更新那条横幅**，不再走整个 initDemoModeBanner()。
// 后者会把身份色重新注一遍 CSS 变量、把三条示例主诉重新 innerHTML 一遍、
// 把 λ₁ 说明重新渲染一遍——每秒一次、最多 180 次，全是白做的 DOM 工作
// （身份色、示例、s3_mode 在预热期间不会变）。这是 R40 加轮询时引入的浪费，
// R41 的前端 profile 才把它显出来。
async function pollWarmupOnly() {
  try {
    const resp = await fetch("/health");
    if (!resp.ok && resp.status !== 503) return;
    const health = await resp.json();
    renderWarmupBanner(health.warmup);
    if (health.warmup && !health.warmup.ready) scheduleWarmupRecheck();
  } catch (e) {
    // 预热期间后端被重启之类：停止轮询，横幅留在最后一次的状态。
  }
}

async function initDemoModeBanner() {
  try {
    const resp = await fetch("/health");
    // **503 要放过**：那是"预热中"，响应体完整。当成 !ok 处理的话预热那十几秒
    // 里页面会说"服务未连接"，而服务其实在正常应答——比晚几秒着色糟得多。
    if (!resp.ok && resp.status !== 503) { renderOfflineBanner(false); return; }
    const health = await resp.json();
    renderWarmupBanner(health.warmup);
    if (health.warmup && !health.warmup.ready) scheduleWarmupRecheck();
    renderDemoMode(health.demo_mode);
    injectPhysicianColors(health.physicians);
    // 示例主诉和身份色同一趟拿：两者都要在"点第一次辨证之前"就到位。
    EXAMPLE_COMPLAINTS = health.example_complaints || [];
    renderExamples(EXAMPLE_COMPLAINTS);
    renderConsultLambda1Note(health.lambda1_note);
    // R37：这台服务的 S3 形状。**在点「辨证」之前就要到位**——它决定骨架是
    // 单链九段还是三列集注。拿不到（老服务、/health 失败）时保持 null，
    // isSingleChain() 会回落到 legacy 三列，跟 R36 及以前的行为一致。
    SERVER_S3_MODE = health.s3_mode || null;
    // R47：产品形态、版本、本体可用性。**先于渲染任何结果**——产品模式要藏的
    // 十六处必须在访问者看到页面之前就定下来。
    applyProductMode(health);
    renderFooterVersion(health);
    KNOWLEDGE_BASE_AVAILABLE = !(health.knowledge_base
      && health.knowledge_base.available === false);
    renderOfflineBanner(true);
  } catch (e) {
    // 网络不通时 fetch 会抛。身份色有 CSS 兜底、示例主诉不显示——都不致命，
    // 但**这件事本身要说出来**：页面看起来正常，而点「辨证」一定会失败。
    renderOfflineBanner(false);
  }
}

// ---------- A2：患者模式导诊面板 ----------
//
// 后端在 role=patient/doctor 时下发 triage（dept/urgency/red_flags/advice）与
// food_therapy/patent_medicines，之前前端一个字段都没渲染，所以即使把 patient
// 选项加回来，DEMO.md 第 5 点要的"病名 + 建议科室 + 红旗症状"页面上也看不到。
//
// 食疗/中成药目前是空列表（M9 的数据还没接），这里如实说明"尚未接入"，
// 不显示成"没有推荐"——两者意思不同。urgency=high 时后端 _apply_medication_gate
// 无论如何都返回空列表，这条闸门在服务端，这里只是不去暗示它本该有内容。
const URGENCY_LABEL = { high: "紧急", medium: "较急", low: "常规" };

function triageBoxHtml(data) {
  const t = data.triage;
  if (!t) {
    return `<div class="triage-card"><div class="t-none">本次没有导诊依据：模型给出的病名不在参考病名表里，不编造科室建议。</div></div>`;
  }
  const urgency = t.urgency || "low";
  const rows = [];
  rows.push(`<div><span class="t-label">紧急度</span>${escapeHtml(URGENCY_LABEL[urgency] || urgency)}</div>`);
  if (t.dept) rows.push(`<div><span class="t-label">建议科室</span>${escapeHtml(t.dept)}</div>`);
  if (t.advice) rows.push(`<div><span class="t-label">建议</span>${escapeHtml(t.advice)}</div>`);
  if ((t.red_flags || []).length) {
    rows.push(`<div><span class="t-label">需要警惕</span><span class="t-flags">${escapeHtml(t.red_flags.join("、"))}</span></div>`);
  }
  const food = data.food_therapy || [];
  const otc = data.patent_medicines || [];
  const gated = urgency === "high";
  rows.push(`<div class="t-none">${gated
    ? "紧急度为「紧急」，按安全规则不提供任何用药或食疗建议，请尽快就医。"
    : (food.length || otc.length)
      ? `食疗 ${food.length} 条　中成药 ${otc.length} 条`
      : "食疗与 OTC 中成药数据尚未接入（M9），此处暂无内容。"}</div>`);
  return `<div class="triage-card urgency-${escapeHtml(urgency)}">${rows.join("")}</div>`;
}

function renderTriage(data) {
  const box = document.getElementById("triage-box");
  if (!box) return;
  // triage 这个键只在 patient/doctor 角色下存在。researcher/student 拿不到，
  // 这时整个面板不出现——不是显示一个空框。
  //
  // R15：**患者模式不走这个面板**。§3.5 的整页形态里病名/科室/红旗已经是主角，
  // 再在上面顶一个紧凑版导诊框，就又变回"三列的裁剪版 + 一个导诊面板"
  // ——那正是总纲 §1 点名的 F5。医生模式仍然用它：医生要的是一眼扫过的
  // 紧急度提示，不是占半屏的大字。
  if (getSelectedRole() === "patient") {
    box.innerHTML = "";
    box.classList.remove("show");
    return;
  }
  if (!data || !("triage" in data)) {
    box.innerHTML = "";
    box.classList.remove("show");
    return;
  }
  box.innerHTML = triageBoxHtml(data);
  box.classList.add("show");
}

// R14：分歧不再是一整段文字横幅（§1 的 F3），主角换成上面那条点阵对照带。
// **但这段文字一个字都没删**：分层读数（君臣/佐使各自的 Jaccard 与各自的 ε）、
// 未标注 role 的药味数、两两配对的师承内/跨学派均值——这些都是对照带画不出来、
// 又确实有意义的数，收进对照带下面一个默认折叠的 <details> 里。
// 把它整段去掉才是"为了版面好看牺牲信息"，那正是这个项目一直在避免的事。
function renderDivergence(divergence) {
  const banner = document.getElementById("divergence-banner");
  if (!banner) return;
  const wrap = document.getElementById("divergence-detail");
  const text = divergenceBannerText(divergence);
  if (text === null) {
    banner.textContent = "";
    banner.classList.remove("show");
    if (wrap) wrap.hidden = true;
    return;
  }
  banner.textContent = text;
  banner.classList.add("show");
  if (wrap) wrap.hidden = false;
}

function refListHtml(refs) {
  if (!refs || refs.length === 0) {
    return '<div class="ref-item">（无相关医案，相似度均低于阈值）</div>';
  }
  return refs
    .map((r) => {
      const syn = r.syndrome ? `　证：${escapeHtml(r.syndrome)}` : "";
      const sym = (r.symptoms || []).slice(0, 4).join("；");
      const ex = r.excerpt
        ? `<div class="ref-excerpt">${escapeHtml(r.excerpt.slice(0, 120))}${r.excerpt.length > 120 ? "…" : ""}</div>`
        : "";
      return `<div class="ref-item">
        <b>${escapeHtml(r.case_id)}</b>　${escapeHtml(r.visit_label)}　相似度 ${escapeHtml(r.score)}${syn}
        <div class="ref-sym">${escapeHtml(sym)}</div>
        ${ex}
      </div>`;
    })
    .join("");
}

// R14 §3.1 第九条：引用收成一行「引自 N 条医案 ▾」，展开后才是原文块。
// N 从 refs.length 现算，不另存一个计数——两处各存一份必然有一处忘了更新。
//
// R40：后端只下发前 REFS_IN_RESPONSE 条（实测 1060 条 = 1.25 MB / 响应体的
// 98.9%）。这时 `refs.length` 是**下发的条数**，不是语料里检索到的条数——
// 光显示它就是个假数。所以有 `refs_total` 时两个数一起显示：
// 「引自 20 条医案（本次检索到 1060 条，下发前 20 条）」。
// 项目规则「任何数字都必须带对照」在界面上的落点。
function refFoldHtml(refs, counts) {
  const n = (refs || []).length;
  if (!n) {
    return '<div class="col-refs col-refs-empty">无相关医案（相似度均低于阈值）</div>';
  }
  const total = counts && counts.refs_total;
  const note = (counts && counts.refs_truncated && total > n)
    ? `（本次检索到 ${total} 条，按相似度下发前 ${n} 条；被结论引用的一条不漏）`
    : "";
  // R41：超过阈值才开窗口化渲染（见 VIRTUAL_LIST_THRESHOLD 那一段）。
  // 默认 REFS_IN_RESPONSE=20，所以默认这条分支**不会走**——DOM 一个字节没变。
  const body = n > VIRTUAL_LIST_THRESHOLD
    ? virtualRefsHtml(refs)
    : refListHtml(refs);
  return `<details class="col-refs"><summary>引自 ${n} 条医案${escapeHtml(note)}</summary>
    <div class="detail-block">${body}</div>
  </details>`;
}

// ---------- R41：窗口化渲染（虚拟滚动） ----------
//
// ## 先说清楚这一项在当前配置下不生效，以及为什么还要有
//
// R41 实测的 DOM 规模：整页 154~297 个节点，**最长的列表 14 个子节点**。
// 参考医案列表在默认配置下是 20 条（`REFS_IN_RESPONSE`）。这个量级上做虚拟
// 滚动是纯亏：多 80 行代码、多一个剪裁/滚动跳动的失败模式，换 0 收益。
//
// 但 `REFS_IN_RESPONSE` 是**环境变量**。医院把它调到 500 是完全合法的配置
// （"我们要看全部候选"），那时 500 个 `.ref-item`（每个 4~5 个子节点 =
// 2000+ 节点）会让展开那一下变成一个长任务。所以机制建好、按阈值启用：
// 默认那条路径的 DOM 逐字节不变，超过阈值才换。
//
// ## 为什么是"固定行高 + 只渲染可见窗口"，不是"滚到底再追加"
//
// 追加式（分块渲染）DOM 会随滚动一直长，滚到底跟一次全渲染一样——**它解决的
// 是首次渲染，不是 DOM 规模**。真正的窗口化要求行高可预测，所以这条路径下
// 每行收成**一行**（`.ref-row`，CSS 定死高度、超出省略号），完整内容点开进
// 证据侧栏看。这是一个**取舍**：>阈值时列表从"每条三行摘要"变成"每条一行"。
// 阈值以下不受影响，所以默认界面一个像素都没动。
const VIRTUAL_LIST_THRESHOLD = 50;
//: 一行的高度（px）。**必须跟 app.css 里 .ref-row 的 height 一致**——
//: 两处不一致时滚动位置会越滚越偏。有一条测试比这两个数。
const VIRTUAL_ROW_HEIGHT = 26;
//: 窗口外上下各多渲染几行，避免快速滚动时露白。
const VIRTUAL_OVERSCAN = 6;

function virtualRowText(r) {
  const sym = (r.symptoms || []).slice(0, 3).join("；");
  return `${r.case_id}　${r.visit_label || ""}　相似度 ${r.score}`
    + (r.syndrome ? `　证：${r.syndrome}` : "") + (sym ? `　${sym}` : "");
}

function virtualRefsHtml(refs) {
  const n = refs.length;
  // 外层固定高度 + 内层撑满总高：滚动条的长度必须跟"全部 n 行"一致，
  // 否则用户看到的滚动比例是假的。
  return `<div class="virtual-list" data-count="${n}" style="height:${
    Math.min(12, n) * VIRTUAL_ROW_HEIGHT}px">`
    + `<div class="virtual-spacer" style="height:${n * VIRTUAL_ROW_HEIGHT}px">`
    + `<div class="virtual-window"></div></div></div>`;
}

// 把 refs 挂到 DOM 上并接上滚动。**分两步**（先出 HTML 再挂数据）是因为
// 上层 `columnHtml` 是纯字符串拼接的（有一批纯函数测试靠这个性质），
// 数据不能塞进字符串里。
function mountVirtualLists(root, refsByIndex) {
  const lists = (root || document).querySelectorAll(".virtual-list");
  lists.forEach((el, i) => {
    const refs = refsByIndex[i] || [];
    if (!refs.length) return;
    const win = el.querySelector(".virtual-window");
    const draw = () => {
      const first = Math.max(0, Math.floor(el.scrollTop / VIRTUAL_ROW_HEIGHT) - VIRTUAL_OVERSCAN);
      const visible = Math.ceil(el.clientHeight / VIRTUAL_ROW_HEIGHT) + VIRTUAL_OVERSCAN * 2;
      const rows = refs.slice(first, first + visible);
      win.style.transform = `translateY(${first * VIRTUAL_ROW_HEIGHT}px)`;
      win.innerHTML = rows.map((r) => `<div class="ref-row" title="${
        escapeHtml(virtualRowText(r))}">${escapeHtml(virtualRowText(r))}</div>`).join("");
    };
    // 滚动回调走 rAF 合并：scroll 事件一秒能来上百次，每次都重排一遍 DOM
    // 就是自己造抖动（R41 的"消除布局抖动"那一条）。
    let queued = false;
    el.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; draw(); });
    });
    draw();
  });
}

// ---------- M7：君臣佐使分组 ----------
//
// 纯函数：给一份 herb_items（core.schemas.HerbItem 的列表），按 role 分组，
// 固定顺序 君/臣/佐/使，group 为空的角色不出现在返回结果里（没有臣药的方子
// 不该显示一行空的"臣"）。role 为 null／不是这四个字之一的，一律归进
// "未标注"组——不强行塞进某一档，模型没标注就是没标注，伪造一个猜测出来的
// 君臣佐使比"照实说不知道"更误导人。
function groupHerbsByRole(herbItems) {
  const order = ["君", "臣", "佐", "使"];
  const byRole = { 君: [], 臣: [], 佐: [], 使: [] };
  const unlabeled = [];
  for (const item of herbItems || []) {
    if (item && order.includes(item.role)) byRole[item.role].push(item);
    else unlabeled.push(item);
  }
  const groups = order.filter((r) => byRole[r].length).map((r) => ({ role: r, herbs: byRole[r] }));
  if (unlabeled.length) groups.push({ role: "未标注", herbs: unlabeled });
  return groups;
}

// 一味药的展示文本：名字 + 剂量（有就带上，古籍医案常常没写剂量，见
// core.schemas.HerbItem 的文档字符串）。炮制信息放进括号里跟在后面，跟
// evidenceSectionHtml 里"炮制：xxx"单独一行的详细展示是两个不同的场景——
// 这里是卡片正文里一行多味药紧凑罗列，不适合每味药都占一整行。
function herbItemLabel(item) {
  const dose = item.dose != null ? `${item.dose}${item.dose_unit || ""}` : "";
  const base = [item.name, dose].filter(Boolean).join(" ");
  return item.processing ? `${base}（${item.processing}）` : base;
}

// R14 §3.1 第四条：**默认只显示君臣 + 前两味佐药**，其余折叠成"⋯ 展开 N 味"。
// 理由是三列并排时垂直空间是稀缺资源，而君臣决定了"这一路是什么打法"，佐使是加减。
// VISIBLE_ADJUNCT_HERBS 是那个"两味"的唯一定义——写死在两个地方（渲染一处、
// 计数一处）的话，展开按钮上的 N 迟早跟实际折叠的味数对不上。
const VISIBLE_ADJUNCT_HERBS = 2;

// 把分好组的药按"默认可见 / 折叠"切成两半。纯函数，单独提出来是因为
// "折叠了几味"这个数要在两个地方用（按钮文案、测试断言），算两遍必然漂。
function splitHerbGroupsForFold(groups) {
  const visible = [];
  const folded = [];
  let adjunctBudget = VISIBLE_ADJUNCT_HERBS;
  for (const g of groups) {
    if (g.role === "君" || g.role === "臣") {
      visible.push(g);
      continue;
    }
    // 佐/使/未标注：从预算里取，取完的进折叠区。**不是按组折叠而是按味折叠**
    // ——佐药一组就有六味时，整组留下等于没折叠。
    const head = g.herbs.slice(0, adjunctBudget);
    const tail = g.herbs.slice(adjunctBudget);
    adjunctBudget -= head.length;
    if (head.length) visible.push({ role: g.role, herbs: head });
    if (tail.length) folded.push({ role: g.role, herbs: tail });
  }
  return { visible, folded, nFolded: folded.reduce((n, g) => n + g.herbs.length, 0) };
}

// R24：君臣佐使改成**两列密排注解**——左列一个角色字（窄，像批注的鱼尾号），
// 右列这一组的药味。原来是 `<div class="field"><b>君</b>…</div>` 的逐行流式排版，
// 三列并排时每行都要重新找"角色字在哪儿结束、药名从哪儿开始"。
//
// 两列网格让四组的药味**左边界对齐**，眼睛竖着扫一遍就知道君臣各几味；
// 角色字变小变灰，因为它是注解不是内容（内容是药名）。
// 结构用 grid 而不是 table：table 在窄屏上不会换行，而这一块要跟着列宽走。
function herbRowsHtml(groups) {
  const rows = groups
    .map((g) => {
      const text = g.herbs.map((h) => escapeHtml(herbItemLabel(h))).join("、");
      const inner = g.role === "君" ? `<span class="herb-jun">${text}</span>` : text;
      return `<div class="hg-role">${escapeHtml(g.role)}</div>`
        + `<div class="hg-herbs">${inner}</div>`;
    })
    .join("");
  return rows ? `<div class="herb-grid">${rows}</div>` : "";
}

// fold=false 给医生模式/导出这类"要看全量"的场景留的口子：折叠是阅读密度的
// 手段，不是信息裁剪，任何时候都必须有一条路看到全部药味。
function herbGroupsHtml(cand, fold = true) {
  if (!cand || !cand.herb_items || !cand.herb_items.length) return "";
  const groups = groupHerbsByRole(cand.herb_items);
  if (!fold) return herbRowsHtml(groups);
  const { visible, folded, nFolded } = splitHerbGroupsForFold(groups);
  const more = nFolded
    ? `<details class="herb-fold"><summary>⋯ 展开 ${nFolded} 味</summary>${herbRowsHtml(folded)}</details>`
    : "";
  return herbRowsHtml(visible) + more;
}

// ---------- M7：卡片详情默认展开/折叠 ----------
//
// researcher 模式（默认）保持改造前的行为——推理过程/引用详情、ReAct 取证
// 过程默认折叠，跟这一轮之前完全一样；student 模式默认展开，学生不用先点开
// 才能看到完整推理链和取证过程，并排对照两位医家更直接。doctor/patient 目前
// 没有专属界面（见 role-select 的注释），跟 researcher 一样按"默认折叠"处理
// ——不是刻意把它们当 researcher 看待，只是还没有为它们设计过展开状态，
// 折叠是更安全的缺省值。
function defaultDetailsOpenForMode(mode) {
  return mode === "student";
}

// ReAct 取证过程单独一个 <details>，不再嵌在"推理过程与引用详情"里面——
// 这样它的默认展开状态能跟外层详情框分别控制（虽然这一轮两者的开合逻辑给的
// 值相同，都是 defaultDetailsOpenForMode(mode)，但任务描述原文把"三医家默认
// 展开"和"ReAct 默认显示"列成两条独立的验收项，说明它们在概念上是两个可以
// 分别调节的开关，不是同一个东西的两次描述——现在拆开，以后要让两者展开
// 状态不同（比如再增加一种模式）不用重新拆结构）。
function reactTraceDetailsHtml(rt, openAttr) {
  if (!rt || !rt.steps || !rt.steps.length) return "";
  const steps = rt.steps
    .map((st) => `<div>第${st.step}步 ${escapeHtml(st.action)}${st.note ? "（" + escapeHtml(st.note) + "）" : ""}：${escapeHtml((st.thought || "").slice(0, 100))}</div>`)
    .join("");
  const pending = rt.pending_question
    ? `<div>追问患者：${escapeHtml(rt.pending_question)}${rt.pending_answer ? "　答：" + escapeHtml(rt.pending_answer) : "（未获回答）"}</div>`
    : "";
  return `<details ${openAttr}>
    <summary>取证过程（ReAct，${escapeHtml(rt.terminated_by)}，${rt.llm_calls} 次调用）</summary>
    <div class="detail-block">${steps}${pending}</div>
  </details>`;
}

// ---------- M8：医生模式可编辑处方表 ----------
//
// DOCTOR_STATE：physician -> 这位医家当前的编辑状态。跟 EVIDENCE/lastGraph
// 一样是"上一次问诊响应"派生出的运行时状态，每次新响应回来时重建
// （initDoctorState），不是持久化的——医生编辑到一半切换到别的患者，
// 之前编辑的内容本来就不该带过去。
//
// medical_suggestion（发给 /export 做 diff 基准的那份）在 initDoctorState
// 时深拷贝一次锁死，之后医生怎么编辑 herb_items 都不会改到它——
// compute_herb_diffs() 比的就是"模型当初给的" vs "医生最终定的"，如果
// model_suggestion 也跟着编辑走，diff 永远是空的，审计就失去意义。
let DOCTOR_STATE = {};

function blankHerbItem() {
  return { name: "", dose: null, dose_unit: "g", processing: null, decoction: null, role: null, function_in_formula: null };
}

function initDoctorState(results, safetyFlag) {
  DOCTOR_STATE = {};
  for (const r of results) {
    const s3 = r.s3;
    const cand = (s3.formula_candidates && s3.formula_candidates.length)
      ? s3.formula_candidates[s3.selected] : null;
    if (!cand) continue; // patient 角色下 formula_candidates 整个不存在，医生模式不会走到这里，但兜底不崩
    DOCTOR_STATE[r.physician] = {
      syndrome: s3.syndrome,
      disease: s3.disease || null,
      name: cand.name,
      source: cand.source,
      base_formula: cand.base_formula,
      confidence: cand.confidence,
      rationale: cand.rationale,
      doses_count: cand.doses_count,
      usage: cand.usage,
      herb_items: (cand.herb_items || []).map((h) => ({ ...h })),
      model_suggestion: JSON.parse(JSON.stringify(cand)),
      safety: null,
      safetyError: null,
      exportResult: null,
      exportError: null,
      pendingOverrideReason: null,
      // R56 §6：这次问诊命中过危重信号（safety_flag 非空）。导出这张方之前
      // 要跟 safety.blocking（方剂本身超量/配伍禁忌）一样走"坚持导出"那条
      // 二次确认——两者问的不是同一个问题（一个问"这张方本身有没有问题"，
      // 一个问"这条主诉本身危不危重"），但都需要医师明确写一句理由才能导出，
      // 所以复用同一套确认 UI，不另起一套弹窗。
      redFlag: safetyFlag || null,
    };
  }
}

function rxRowHtml(physician, i, h) {
  const doseUnits = ["g", "钱", "两", "分", "枚", "片"];
  const unitOptions = doseUnits
    .map((u) => `<option value="${u}" ${h.dose_unit === u ? "selected" : ""}>${u}</option>`)
    .join("");
  return `<tr>
    <td><input type="text" data-rx-phys="${physician}" data-rx-idx="${i}" data-rx-field="name" value="${escapeHtml(h.name || "")}" /></td>
    <td><input type="number" step="0.1" data-rx-phys="${physician}" data-rx-idx="${i}" data-rx-field="dose" value="${h.dose != null ? h.dose : ""}" /></td>
    <td><select data-rx-phys="${physician}" data-rx-idx="${i}" data-rx-field="dose_unit">${unitOptions}</select></td>
    <td><input type="text" data-rx-phys="${physician}" data-rx-idx="${i}" data-rx-field="processing" value="${escapeHtml(h.processing || "")}" /></td>
    <td><input type="text" data-rx-phys="${physician}" data-rx-idx="${i}" data-rx-field="decoction" value="${escapeHtml(h.decoction || "")}" /></td>
    <td><input type="text" data-rx-phys="${physician}" data-rx-idx="${i}" data-rx-field="function_in_formula" value="${escapeHtml(h.function_in_formula || "")}" /></td>
    <td><button type="button" class="rx-del-btn" data-rx-del="${physician}" data-rx-idx="${i}" title="删除">×</button></td>
  </tr>`;
}

// 跟 api/main.py::_safety_dict() 是同一份数据形状（FormulaSafety 的字段 +
// blocking），这里只是把它渲染成人看的文字——判断"有没有问题"这件事本身
// 不在前端重新做一遍，safety.blocking 和各个数组是否非空都是后端算好直接
// 传过来的。
// error 参数可选：不传时行为跟改动前完全一致（tests/test_prescription_frontend.py
// 就是单参数调用的）。
function rxSafetyHtml(safety, error) {
  // A7：校验请求失败原来也走 safety=null 这条路，显示成"尚未校验"——网络断了、
  // 后端 500 了，跟"还没触发校验"长得一模一样。在一个安全闸门上这种静默是危险的。
  if (error) {
    return `<div class="safety-incompatible">⚠ 安全校验没能完成（${escapeHtml(error)}）：这不等于"没问题"，请重试或检查服务端。</div>`;
  }
  if (!safety) return `<div class="rx-safety-idle">尚未校验</div>`;
  const blocks = [];
  if (safety.incompatible && safety.incompatible.length) {
    blocks.push(`<div class="safety-incompatible">⚠ 配伍禁忌：${safety.incompatible.map((p) => `${escapeHtml(p[0])} 反/畏 ${escapeHtml(p[1])}`).join("；")}</div>`);
  }
  if (safety.dose_violations && safety.dose_violations.length) {
    blocks.push(`<div class="safety-incompatible">⚠ 剂量超限：${safety.dose_violations.map((v) => `${escapeHtml(v.herb)} ${v.dose}${escapeHtml(v.unit)}（上限 ${v.limit_g}g）`).join("；")}</div>`);
  }
  if (safety.thermal_warning) {
    blocks.push(`<div class="safety-thermal">⚠ ${escapeHtml(safety.thermal_warning)}</div>`);
  }
  if (safety.decoction_missing && safety.decoction_missing.length) {
    blocks.push(`<div class="safety-thermal">煎法缺失：${escapeHtml(safety.decoction_missing.join("、"))}</div>`);
  }
  if (safety.toxic_herbs && safety.toxic_herbs.length) {
    blocks.push(`<div class="safety-thermal">含毒性药材：${escapeHtml(safety.toxic_herbs.join("、"))}</div>`);
  }
  // 「未发现问题」是**语义色 --verified**（有出处可核），不是另起一个绿。
  // R15 之前这里写死 #2f7d4f——一个只在这一处出现的绿，没人回答得了它跟
  // 别处的绿是不是同一件事（总纲 §7 第 7 条：色只承担语义）。
  if (!blocks.length) blocks.push(`<div class="rx-safety-ok">✓ 未发现问题</div>`);
  return blocks.join("");
}

function doctorSectionHtml(physician, mode) {
  if (mode !== "doctor") return "";
  const state = DOCTOR_STATE[physician];
  if (!state) return "";
  // 免责声明文案只在 describeDisclaimer() 里写一份——这里跟页头那条横幅
  // 共用同一个字符串来源，不各自硬编码一遍。收尾阶段核对 README「定位」
  // 那节时发现这里曾经复制过一份，跟 describeDisclaimer() 各自维护，
  // 改一处忘另一处的风险是真实存在的（CLAUDE.md「同一概念只能有一处
  // 实现」，这次撞的是文案而不是逻辑，但道理一样），这里改成调用它。
  return `
    <div class="doctor-disclaimer">${escapeHtml(describeDisclaimer("doctor"))}</div>
    <div class="label rx-section-title">医师编辑处方——${escapeHtml(state.name)}</div>
    <div class="rx-formula-fields">
      <label>剂数　<input type="number" class="rx-doses" data-rx-phys="${physician}" data-rx-field="doses_count" value="${state.doses_count != null ? state.doses_count : ""}" /></label>
      <label>用法　<input type="text" class="rx-usage" data-rx-phys="${physician}" data-rx-field="usage" value="${escapeHtml(state.usage || "")}" /></label>
    </div>
    <div class="rx-table-wrap">
    <table class="rx-table">
      <thead><tr><th scope="col">药名</th><th scope="col">剂量</th><th scope="col">单位</th><th scope="col">炮制</th><th scope="col">煎法</th><th scope="col">作用</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead>
      <tbody id="rx-table-body-${physician}">${state.herb_items.map((h, i) => rxRowHtml(physician, i, h)).join("")}</tbody>
    </table>
    </div>
    <button type="button" class="rx-add-btn" data-rx-add="${physician}">+ 添加药味</button>
    <div id="rx-safety-${physician}">${rxSafetyHtml(state.safety)}</div>
    <button type="button" class="rx-export-btn" data-rx-export="${physician}">导出处方</button>
    <div id="rx-export-panel-${physician}"></div>
  `;
}

function renderDoctorTable(physician) {
  const el = document.getElementById(`rx-table-body-${physician}`);
  const state = DOCTOR_STATE[physician];
  if (!el || !state) return;
  el.innerHTML = state.herb_items.map((h, i) => rxRowHtml(physician, i, h)).join("");
}

function renderDoctorSafety(physician) {
  const el = document.getElementById(`rx-safety-${physician}`);
  const state = DOCTOR_STATE[physician];
  if (!el || !state) return;
  el.innerHTML = rxSafetyHtml(state.safety, state.safetyError);
}

const RX_VALIDATE_TIMERS = {};

// 每次编辑后调一次校验接口，但不是每敲一个字符就发一次请求——防抖
// 400ms，跟用户打字节奏对齐，不会打字打到一半疯狂发请求。
function scheduleValidate(physician) {
  clearTimeout(RX_VALIDATE_TIMERS[physician]);
  RX_VALIDATE_TIMERS[physician] = setTimeout(() => runValidate(physician), 400);
}

async function runValidate(physician) {
  const state = DOCTOR_STATE[physician];
  if (!state) return;
  // 名字还没填的行（刚点了"+添加药味"、还没来得及输入）先过滤掉再送——
  // 送一个空名字上去会撞 HerbItem 的 Field(min_length=1)，整个请求体
  // 校验失败，会把已经填好的其他药材的校验结果也一起吞掉。
  const herbItems = state.herb_items.filter((h) => (h.name || "").trim());
  if (!herbItems.length) {
    state.safety = null;
    state.safetyError = null;
    renderDoctorSafety(physician);
    return;
  }
  try {
    const resp = await fetch("/api/prescription/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ herb_items: herbItems, syndrome: state.syndrome, disease: state.disease }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    state.safety = await resp.json();
    state.safetyError = null;
  } catch (err) {
    console.error("处方校验失败：", err);
    state.safety = null;
    state.safetyError = err.message || String(err);
  }
  renderDoctorSafety(physician);
}

// 拼 HTML 和写 DOM 拆开：这一段是九步序列里最后两步（拒绝 → 填理由 → 成功），
// 而它原来只有"写进 DOM"这一种形态，测试没法看到它拼出了什么
// （`DOM_STUB` 是个 Proxy，写进去的 innerHTML 读不回来）。
// 拆成纯函数之后跟本文件其余部分（columnHtml / rxSafetyHtml / rxCompareHtml）
// 一个形状，测试直接对返回值断言。
function doctorExportPanelHtml(state, physician) {
  if (!state) return "";
  if (state.exportResult) {
    return `<div class="rx-pharmacy-text">${escapeHtml(state.exportResult.text)}</div>
      <div class="rx-audit-id">审计编号：${escapeHtml(state.exportResult.audit_id)}</div>`;
  }
  // R56 §6 第 4 条：这次问诊命中过危重信号——导出前先要求医师写一句理由，
  // 跟 safety.blocking（方剂本身超量/配伍禁忌）复用同一套"填理由才能坚持
  // 导出"的 UI（同一个 rx-override-input / rx-confirm-export 判据，见
  // runExport 里的门槛）。这个分支必须排在 exportError 检查之前——还没点过
  // 导出按钮时（exportError 还是 null）也要先看到这道确认，不能等医师点了
  // 才发现要多填一步。
  if (state.redFlag && !state.pendingOverrideReason && !state.exportResult) {
    return `<div class="rx-override-box">
      <div class="rx-reject-title">⚠ 本次问诊命中危重症状信号（${escapeHtml(state.redFlag)}）</div>
      <div class="rx-reject-item">· 导出前请确认已结合临床实际判断，必要时已提醒患者立即就医或转诊/急诊处理</div>
      <textarea id="rx-override-input-${physician}" placeholder="填写确认理由（必填，将原样记入审计日志，医师对该理由负责）"></textarea>
      <button type="button" class="rx-export-btn" data-rx-confirm-export="${physician}">确认并导出</button>
    </div>`;
  }
  if (state.exportError) {
    const message = state.exportError.message || "该方存在拦截级安全问题，拒绝导出。";
    const problems = state.exportError.problems || null;
    if (problems) {
      // safety.blocking 为真：给出问题清单 + override_reason 输入框，医生填了
      // 非空理由才能"坚持导出"——这条理由会进审计日志，是"明知有问题仍坚持"
      // 唯一的书面记录，所以文案要说清楚这一点，不能只是一个不起眼的确认框。
      return `<div class="rx-override-box">
        <div class="rx-reject-title">⚠ ${escapeHtml(message)}</div>
        ${problems.map((x) => `<div class="rx-reject-item">· ${escapeHtml(x)}</div>`).join("")}
        <textarea id="rx-override-input-${physician}" placeholder="填写坚持导出的理由（必填，将原样记入审计日志，医师对该理由负责）"></textarea>
        <button type="button" class="rx-export-btn" data-rx-confirm-export="${physician}">坚持导出</button>
      </div>`;
    }
    return `<div class="rx-override-box"><div class="rx-reject-title">⚠ ${escapeHtml(message)}</div></div>`;
  }
  return "";
}

function renderDoctorExportPanel(physician) {
  const el = document.getElementById(`rx-export-panel-${physician}`);
  const state = DOCTOR_STATE[physician];
  if (!el || !state) return;
  el.innerHTML = doctorExportPanelHtml(state, physician);
}

// R56 §6 第 10 条：医师工号格式校验。三甲的工号是 HIS/SSO 发的，不是自由
// 文本——至少 3 位字母数字，挡掉"随手打几个字"这类肯定不是工号的输入。
// 真正的工号规则（几位、有没有院区前缀）要接哪家医院的 HIS 才能定，这里
// 只挡明显不像工号的输入，不冒充"已对接 HIS 的工号校验"。
const DOCTOR_ID_FORMAT_RE = /^[A-Za-z0-9]{3,}$/;

function isValidDoctorId(id) {
  return DOCTOR_ID_FORMAT_RE.test(id);
}

async function runExport(physician) {
  const state = DOCTOR_STATE[physician];
  if (!state) return;
  // R56 §6 第 4 条：危重信号命中时，导出前必须先拿到医师写的确认理由——
  // 门槛跟 doctorExportPanelHtml() 里那个判据是同一条（redFlag 非空、
  // pendingOverrideReason 还没填），不发请求、直接把确认框渲染出来。
  if (state.redFlag && !state.pendingOverrideReason) {
    renderDoctorExportPanel(physician);
    return;
  }
  const doctorIdInput = document.getElementById("doctor-id-input");
  const doctorId = (doctorIdInput ? doctorIdInput.value : "").trim();
  if (!doctorId) {
    state.exportError = { message: "请先在上方填写医师工号再导出处方。" };
    state.exportResult = null;
    renderDoctorExportPanel(physician);
    return;
  }
  if (!isValidDoctorId(doctorId)) {
    state.exportError = { message: "医师工号格式不对：至少 3 位字母或数字（HIS/SSO 登录后会自动填入，无需手填）。" };
    state.exportResult = null;
    renderDoctorExportPanel(physician);
    return;
  }
  const patientRefInput = document.getElementById("patient-ref-input");
  const patientRef = (patientRefInput ? patientRefInput.value : "").trim() || null;

  const body = {
    formula: {
      name: state.name, source: state.source, base_formula: state.base_formula,
      confidence: state.confidence, rationale: state.rationale,
      herb_items: state.herb_items, doses_count: state.doses_count, usage: state.usage,
    },
    doctor_id: doctorId,
    patient_ref: patientRef,
    model_suggestion: state.model_suggestion,
    override_reason: state.pendingOverrideReason || null,
  };

  try {
    const resp = await fetch("/api/prescription/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.status === 422) {
      state.exportError = data.detail;
      state.exportResult = null;
    } else if (!resp.ok) {
      // R56 §6 第 1 条：不把整个响应体 JSON.stringify 印给用户——`data.detail`
      // 是后端已经写好的人话错误，没有的话才退到一句通用说明，不裸露原始 JSON。
      state.exportError = {
        message: `导出失败（HTTP ${resp.status}）：${
          typeof data.detail === "string" ? data.detail : "服务器未给出可读的失败原因"}`,
      };
      state.exportResult = null;
    } else {
      state.exportResult = data;
      state.exportError = null;
      state.pendingOverrideReason = null;
    }
  } catch (err) {
    state.exportError = { message: `导出失败：${err.message || err}` };
    state.exportResult = null;
  }
  renderDoctorExportPanel(physician);
}

// 事件委托绑在 #columns 这个容器本身（每次响应回来只会替换它的子树，容器
// 节点自己一直存在），不是每次重新渲染表格后逐个 input 重新 addEventListener
// ——那样容易漏绑，而且用户正在打字的输入框如果整表重渲染会丢失光标位置，
// 委托模式下"编辑单元格内容"根本不触发整表重渲染（见下面 input 分支只改
// DOCTOR_STATE、不碰 DOM），只有加行/删行才会。
//
// R14 容器从 #results 改名成 #columns。委托的好处在这里反过来咬了一口：
// 容器不在了 getElementById 返回 null，**整份 app.js 在加载时就抛**，
// 页面上什么都不会发生——而 node 测试里 DOM_STUB 是个什么都接住的 Proxy，
// 一个字都测不出来。这就是 CLAUDE.md 那条"Playwright 是必需环节"的又一例。
document.getElementById("columns").addEventListener("input", (e) => {
  const t = e.target;
  const phys = t.dataset.rxPhys;
  if (!phys || !DOCTOR_STATE[phys]) return;
  const field = t.dataset.rxField;
  if (!field) return;
  const idx = t.dataset.rxIdx;
  if (idx !== undefined && idx !== "") {
    const item = DOCTOR_STATE[phys].herb_items[Number(idx)];
    if (!item) return;
    item[field] = field === "dose" ? (t.value === "" ? null : Number(t.value)) : (t.value === "" ? null : t.value);
  } else {
    DOCTOR_STATE[phys][field] = field === "doses_count" ? (t.value === "" ? null : Number(t.value)) : (t.value === "" ? null : t.value);
  }
  scheduleValidate(phys);
});

document.getElementById("columns").addEventListener("click", (e) => {
  const addPhys = e.target.dataset.rxAdd;
  if (addPhys) {
    DOCTOR_STATE[addPhys].herb_items.push(blankHerbItem());
    renderDoctorTable(addPhys);
    return;
  }
  const delPhys = e.target.dataset.rxDel;
  if (delPhys) {
    DOCTOR_STATE[delPhys].herb_items.splice(Number(e.target.dataset.rxIdx), 1);
    renderDoctorTable(delPhys);
    scheduleValidate(delPhys);
    return;
  }
  const exportPhys = e.target.dataset.rxExport;
  if (exportPhys) {
    DOCTOR_STATE[exportPhys].pendingOverrideReason = null;
    runExport(exportPhys);
    return;
  }
  const confirmPhys = e.target.dataset.rxConfirmExport;
  if (confirmPhys) {
    const textarea = document.getElementById(`rx-override-input-${confirmPhys}`);
    DOCTOR_STATE[confirmPhys].pendingOverrideReason = textarea ? textarea.value : "";
    runExport(confirmPhys);
  }
});

// ---------- M8：定位表述分模式 + role-select 联动 ----------

function describeDisclaimer(mode) {
  if (mode === "doctor") {
    // R47 §0.4 第 2 条：输出侧的措辞决定这个产品算不算"辅助决策"。
    // 「系统提供的方剂与剂量为建议」是指向具体患者的诊疗建议式措辞；
    // 改成客观陈述——系统陈述的是**教材与医案里的记载**，用不用、怎么用
    // 是医师的判断。这不是文字游戏，是产品定位（不作为医疗器械管理的前提）。
    return "医师模式：知识辅助工具。系统列出的方剂与剂量出自教材与医案记载，" +
      "不构成对该患者的用药建议；处方由执业医师审核、修改并签发，" +
      "医师承担全部临床责任。所有导出操作均记录审计日志。";
  }
  return "中医知识辅助与教学工具，不作为医疗器械管理，不提供诊断结论";
}

function updateDisclaimer() {
  const el = document.getElementById("disclaimer-text");
  if (el) el.textContent = describeDisclaimer(getSelectedRole());
}

function updateDoctorFieldsVisibility() {
  const row = document.getElementById("doctor-id-row");
  // classList 而不是 style.display："显示成什么"（flex）归 CSS 管，这里只说
  // "要不要显示"。R14 之前这行写死 flex，改版面时要同时改 JS 才生效。
  if (row) row.classList.toggle("is-hidden", getSelectedRole() !== "doctor");
}

// R56 §6 第 10 条：真实部署里工号来自 HIS/SSO 单点登录，不是护士站手敲。
// 这里没有真的 HIS 网关可接，用 URL 参数模拟"上级系统已经把身份传进来"这
// 一步——`?doctor_id=dr0217` 预填并锁定输入框，跟真实 SSO 回跳时后端把
// 工号写进页面的效果一致。没带这个参数时保持原样（手填，未锁定）。
function applyDoctorIdFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const injected = (params.get("doctor_id") || "").trim();
  if (!injected) return;
  const input = document.getElementById("doctor-id-input");
  if (!input) return;
  input.value = injected;
  input.readOnly = true;
  input.title = "由 HIS/SSO 登录自动填入，不可手改";
}

function validateDoctorIdLive() {
  const input = document.getElementById("doctor-id-input");
  const errEl = document.getElementById("doctor-id-format-error");
  const errRow = document.getElementById("doctor-id-error-row");
  if (!input || !errEl || !errRow) return;
  const v = input.value.trim();
  const bad = v.length > 0 && !isValidDoctorId(v);
  errEl.textContent = bad ? "格式不对：至少 3 位字母或数字" : "";
  errRow.classList.toggle("is-hidden", !bad);
}

document.getElementById("doctor-id-input").addEventListener("input", validateDoctorIdLive);
applyDoctorIdFromQuery();

// R15 第四条：**角色切换不刷新页面、不重新问诊，但已有结果要按新角色重新
// 请求后端。**
//
// 为什么不能前端切换显示：patient 的字段裁剪是一条安全边界
// （api/main.py::_filter_s3_for_role + to_graph(role="patient") 整段跳过方剂/
// 药材层）。researcher 那份响应体里带着全部药名，切到 patient 只是"前端不画"
// ——一次「检查元素」就能把它们全看回来，这个角色的意义就没了。
//
// 为什么不是"什么都不做、等用户自己重新点辨证"：那是 R15 之前的行为，
// DEMO.md 第 4 点为此专门写了一句"切下拉不会就地生效，必须重新提交"——
// 一个需要靠文档解释的交互就是设计没做完。
//
// 代价说清楚：重新请求 = 重新走一次完整推理 = 真实 LLM 调用。**没有偷偷花钱**
// ——切换后页面立刻进 running 状态、进度条走起来、取消按钮出现，跟手动点
// 「辨证」看到的完全一样。replay 模式下这一次是命中 fixture、零调用。
let LAST_COMPLAINT = "";
//: R46：最近一次问诊的完整响应。病历文书按它排版（`buildEmrFromLastResult`）。
let LAST_RESULT = null;

document.getElementById("role-select").addEventListener("change", () => {
  updateDisclaimer();
  updateDoctorFieldsVisibility();
  // 还没问过、或正在跑：没有"已有结果"可重取，什么都不做。
  if (!LAST_COMPLAINT || currentAbort) return;
  document.getElementById("complaint").value = LAST_COMPLAINT;
  submitConsult();
});
updateDisclaimer();
updateDoctorFieldsVisibility();

// ============================================================================
// R14：问诊页的五种状态（docs/DESIGN.md §3.1 状态设计表）
// ============================================================================
//
// 五种状态各有各的形态，不是"同一个页面加个 loading 遮罩"：
//   first        首次进入——输入区居中放大，下面三条示例可点击填入
//   running      辨证中——三列各自走 S1 → S2 → S3 的分步进度，不是一个转圈
//   insufficient 信息不足——三列位置各一句话 + 该补什么，不留空白
//   followup     追问——问题弹在提问那位医家的列里，其余两列"等待中"
//   done         有结果
// 第六种「安全拦截」不在这个类名里：它是**整页替换**，#safety-block 跟
// #consult-page 是互斥的两个兄弟（§3.1 "打断必须彻底"）。
const CONSULT_STATES = ["first", "running", "insufficient", "followup", "done"];

function setConsultState(state) {
  const page = document.getElementById("consult-page");
  if (!page) return;
  for (const s of CONSULT_STATES) page.classList.remove(`state-${s}`);
  page.classList.add(`state-${state}`);
  // R47 §8.4 第 26 条：预计剩余时间的采样点挂在**状态机这一处**，不散到
  // 各个调用点——"这一次跑了多久"的起止就是 running → done 这两次转移，
  // 在别处计时早晚会漏掉某条分支（追问、信息不足、被拦截）。
  if (typeof startEta !== "function") return;
  if (state === "running") startEta();
  // done 才计入样本：追问/信息不足/被拦截都没跑完整条链，把它们的历时
  // 混进中位数会让预计时间越来越短，而那不是它变快了。
  else stopEta(state === "done");
}

// 安全拦截：整页替换。**不是隐藏，是清空**——被拦截的请求不产出任何方药，
// 页面上就不该留着上一次的三列在那儿等着被滚出来（DOM 里搜不到药名这件事
// 有一条测试钉着）。
function showSafetyBlock(reason) {
  const page = document.getElementById("consult-page");
  const block = document.getElementById("safety-block");
  clearColumns();
  hidePatientView();
  document.getElementById("rx-compare").innerHTML = "";
  if (typeof renderGraph === "function") renderGraph(null);
  block.innerHTML = safetyBlockHtml(reason);
  bindSafetyBlockBack();
  block.hidden = false;
  if (page) page.hidden = true;
}

function hideSafetyBlock() {
  const block = document.getElementById("safety-block");
  const page = document.getElementById("consult-page");
  if (block) { block.hidden = true; block.innerHTML = ""; }
  if (page) page.hidden = false;
}

// 就医指引是固定文案，不是模型生成的——拦截的整个意义就是"不让模型继续说话"。
function safetyBlockHtml(reason) {
  return `<div class="sb-inner">
    <div class="sb-title">这条主诉需要先去医院，不适合在这里继续辨证</div>
    <div class="sb-reason">${escapeHtml(reason || "命中危重症状")}</div>
    <div class="sb-advice">
      <div>· 立即前往急诊，或拨打 120。</div>
      <div>· 携带既往病历与正在服用的药物清单。</div>
      <div>· 不要自行服药、进食或饮水，以免影响后续检查。</div>
    </div>
    <div class="sb-note">本次已在证素推断之前中止，系统不会给出任何处方建议。这是设计如此，不是出错。</div>
    <button type="button" id="sb-back">换一条主诉</button>
  </div>`;
}

// 整页替换之后必须有路走回去。没有这颗按钮的话，拦截页是个死胡同——唯一的
// 出路是刷新整个页面，而那会把 BYOK、角色、检索模式一起清掉。
// "打断必须彻底"说的是不给方药，不是把人困在这一页。
function bindSafetyBlockBack() {
  const btn = document.getElementById("sb-back");
  if (!btn) return;
  btn.addEventListener("click", () => {
    hideSafetyBlock();
    setConsultState("first");
    renderComplaintBody("");
    document.getElementById("complaint").value = "";
    document.getElementById("complaint").focus();
  });
}

// R16 §3.2 规格 2：问诊图上的 λ1 说明。**原样显示后端 lambda1_note() 那段话**
// ——跟图谱浏览器那张图读的是同一处（offline/graph_stats.py），前端不改写、
// 不精简一个字。那段话是这个项目的一个真实发现，弱化它比图上有 bug 更严重。
// 拿不到（这台机器没建过图谱）就不显示这一行，不是显示一句"未知"。
function renderConsultLambda1Note(note) {
  const el = document.getElementById("cy-lambda1-note");
  if (!el) return;
  el.textContent = note || "";
  el.classList.toggle("show", !!note);
}

// ---------- 主诉正文（28px 宋体，行宽 ≤ 38 汉字） ----------

function renderComplaintBody(text) {
  const el = document.getElementById("complaint-body");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("show", !!text);
}

// ---------- 首屏三条示例 ----------

function exampleListHtml(examples) {
  if (!examples || !examples.length) return "";
  const items = examples.map((e) => `<button type="button" class="example" data-complaint="${escapeHtml(e.text)}">
      <span class="ex-label">${escapeHtml(e.label)}</span>
      <span class="ex-text">${escapeHtml(e.text)}</span>
      <span class="ex-hint">${escapeHtml(e.hint || "")}</span>
    </button>`).join("");
  return `<div class="ex-title">或从这三条开始</div><div class="ex-list">${items}</div>`;
}

// R41 事件委托：**一次绑在容器上**，不随每次渲染逐个按钮重绑。
// 三个按钮的绑定本身很便宜，改它是为了两件别的事：
//   · `renderExamples` 可能被调多次（重连、角色切换），逐个重绑的写法要靠
//     "innerHTML 换了节点、旧监听器跟着走"这个副作用才不泄漏——依赖副作用的
//     正确性是看不出来的正确性；
//   · 委托之后 `renderExamples` 变成纯粹的"填 HTML"，没有副作用，
//     `tests/test_consult_layout.py` 那类纯函数测试能直接用。
let _examplesDelegated = false;

function delegateExamples() {
  if (_examplesDelegated) return;
  const el = document.getElementById("examples");
  if (!el) return;
  _examplesDelegated = true;
  el.addEventListener("click", (e) => {
    const btn = e.target.closest(".example");
    if (!btn || !el.contains(btn)) return;
    const box = document.getElementById("complaint");
    box.value = btn.dataset.complaint;
    box.focus();
  });
}

function renderExamples(examples) {
  const el = document.getElementById("examples");
  if (!el) return;
  el.innerHTML = exampleListHtml(examples);
  delegateExamples();
}

// ---------- 用药对照带（§3.1 第三条：页面的主角） ----------
//
// 不是"大数字配小标签"（那是默认做法），是一条点阵：共用药实心 --ink 点、
// 各家独有药按各自身份色。一眼看出"共同的少、各自的多"——比 0.53 这个数直观。
//
// 集合本身在后端算（divergence.shared_herbs / unique_herbs），这里只负责画：
// 判定两味药是不是同一味走的是 core/herbs.py::normalized_herb_set，
// 前端拿 s3.herbs 自己做集合差就是第二套匹配实现（CLAUDE.md 第 31 条）。
function dotsHtml(n, cls, color) {
  const style = color ? ` style="background: ${color};"` : "";
  return Array.from({ length: n }, () => `<span class="dot ${cls}"${style}></span>`).join("");
}

// ε 段 --noise 灰、超出段 --real 黑。两段的宽度比 = ε : (差异 - ε)，
// 差异没超过 ε 时超出段宽度为 0，并且左右两个读数照旧都显示——
// "没超出"本身就是结论，把带子藏起来等于把结论也藏了。
function bandSegments(epsilon, divergenceValue) {
  if (epsilon === null || epsilon === undefined || divergenceValue === null
      || divergenceValue === undefined) {
    return null;
  }
  const total = Math.max(epsilon, divergenceValue);
  if (total <= 0) return { epsPct: 100, realPct: 0, exceeds: false };
  const epsPct = Math.min(100, (epsilon / total) * 100);
  return {
    epsPct: Math.round(epsPct * 10) / 10,
    realPct: Math.round((100 - epsPct) * 10) / 10,
    exceeds: divergenceValue > epsilon,
  };
}

// R24：对照带改成 SVG，并把 ε 那一段画成**斜纹**而不是一块灰。
//
// 为什么斜纹：3px 高的两段纯色带，在投影和小屏上"灰"和"黑"的区别几乎看不出，
// 而这条带子是整页的主角（§3.1 第三条）——它要回答的是"差异有没有超出噪声"。
// 斜纹和实心的区别是**纹理**，不依赖亮度，投影仪压暗了也还在。
// 带高从 3px 提到 18px：这是页面上唯一需要被隔着几米看清的东西。
//
// pattern 的 id 写成常量：整页只有一条带，每次重渲染时整个 SVG 被替换，
// 不会出现两个同 id 的 pattern。
const RX_HATCH_ID = "rx-hatch";
const RX_BAND_HEIGHT = 18;

function rxBandSvgHtml(seg) {
  if (!seg) {
    return `<div class="rx-line rx-line-unmeasured" role="img"`
      + ` aria-label="没有噪声地板可比"></div>`;
  }
  const label = seg.exceeds
    ? `斜纹段是噪声地板，实心段是超出噪声的真实分歧`
    : `整条都在噪声地板之内`;
  // width 用百分比、height 用固定值：百分比宽让它跟着栏宽走，
  // 固定高让斜纹的角度在任何宽度下都一样（斜纹的可读性靠角度）。
  return `<svg class="rx-band" viewBox="0 0 100 ${RX_BAND_HEIGHT}" preserveAspectRatio="none"
       height="${RX_BAND_HEIGHT}" role="img" aria-label="${escapeHtml(label)}">
    <defs>
      <pattern id="${RX_HATCH_ID}" width="4" height="4" patternUnits="userSpaceOnUse"
               patternTransform="rotate(45)">
        <rect width="4" height="4" fill="var(--surface-2)"></rect>
        <line x1="0" y1="0" x2="0" y2="4" stroke="var(--noise)" stroke-width="2"></line>
      </pattern>
    </defs>
    <rect class="rx-band-noise" x="0" y="0" width="${seg.epsPct}" height="${RX_BAND_HEIGHT}"
          fill="url(#${RX_HATCH_ID})"></rect>
    <rect class="rx-band-real" x="${seg.epsPct}" y="0" width="${seg.realPct}"
          height="${RX_BAND_HEIGHT}" fill="var(--real)"></rect>
  </svg>`;
}

function epsilonLabel(epsForQuery) {
  const e = epsForQuery || {};
  if (e.value === null || e.value === undefined) return "噪声地板 未测";
  // scope=global 必须标出来：一个没说明来源的对照基准跟没有对照一样。
  const scope = e.scope === "global" ? "（全局）" : "";
  return `噪声地板 ε=${e.value}${scope}`;
}

function rxCompareHtml(divergence, results) {
  if (!divergence) return "";
  const shared = divergence.shared_herbs || [];
  const unique = divergence.unique_herbs || {};
  const order = (results || []).map((r) => r.physician);
  const parts = [`<span class="rx-group"><span class="rx-tag">共用 ${shared.length}</span>${dotsHtml(shared.length, "dot-shared")}</span>`];
  for (const pid of order) {
    const own = unique[pid] || [];
    const name = physicianName(pid);
    parts.push(`<span class="rx-group" data-physician="${escapeHtml(pid)}">`
      + `<span class="rx-tag">${escapeHtml(name)}独有 ${own.length}</span>`
      + dotsHtml(own.length, "dot-own", `var(--phys-${pid}, var(--ink))`)
      + `</span>`);
  }
  const value = divergence.pairs_mean;
  const seg = bandSegments((divergence.epsilon_for_query || {}).value, value);
  const line = rxBandSvgHtml(seg);
  const right = value === null || value === undefined
    ? "三家平均差异 未算出"
    : `三家平均差异 ${value}`;
  const verdict = seg
    ? (seg.exceeds ? "超出噪声地板的部分是真实分歧" : "差异未超出噪声地板，这一条上三家实质一致")
    : "没有噪声地板可比，这个数单独看没有意义";
  return `<div class="rx-title">用药对照</div>
    <div class="rx-dots">${parts.join("")}</div>
    ${line}
    <div class="rx-scale"><span>${escapeHtml(epsilonLabel(divergence.epsilon_for_query))}</span><span>${escapeHtml(right)}</span></div>
    <div class="rx-verdict">${escapeHtml(verdict)}</div>`;
}

function renderRxCompare(divergence, results) {
  const el = document.getElementById("rx-compare");
  if (!el) return;
  el.innerHTML = rxCompareHtml(divergence, results);
  el.classList.toggle("show", !!el.innerHTML);
}

// ---------- 三列的非终态：进度 / 等待 / 信息不足 / 追问 ----------
//
// 三列的**列表**不依赖结果：还没有结果时也要摆出三列（表头着色靠 /health 下发的
// 身份色，不用等问诊回来）。顺序按 PHYSICIAN_META 的键序——那是 /health 下发的
// 顺序，而 /health 遍历的是 PHYSICIANS 注册表，跟后端 results 的顺序是同一个。
//
// **只算 enabled 的那几位。** R18-A 把注册表从三位扩到五位（加了李可、王云启，
// 两位 enabled=false 的参考医家），而 /health 下发的是**全部**五位——前端得知道
// 李可存在才画得出「参考医家」那一栏。这里不过滤的话非终态会摆出五列，其中两列
// 永远停在"辨证中"，因为后端集注只跑 physicians_enabled 那三位。
// Playwright 实测就是这么发现的：running/insufficient/followup 三个状态的
// 「不是三列，是 5」。纯函数测试测不出来——它们要么不调这个函数，要么自己喂
// 一份三人的 PHYSICIAN_META。
// 展示层只认中文名，id 只在数据层出现（CLAUDE.md「标识符只有一种规范形式」）。
// **一处解析**：先看这次问诊的结果（`physician_name`），再回落到 /health 那份
// 注册表，最后才是 id 本身。为什么必须有第二级：`renderConsultResult()` 每次
// 都把 `PHYSICIAN_NAMES` 清空重填，而 structured 模式下 `results` 只有
// 「五家综合」一条——§⑧ 的用药归属和 §⑨ 的「引到的医家」要显示叶天士、李可，
// 只查那份映射就会把 `ye_tianshi` 这个 id 直接印到界面上（R37 实测截图上
// 就是这么印的）。这是 SOURCES.md 第 31 条那件事的显示层版本。
function physicianName(pid) {
  if (!pid) return "";
  return PHYSICIAN_NAMES[pid]
    || (PHYSICIAN_META[pid] && PHYSICIAN_META[pid].name)
    || pid;
}

function physicianOrder() {
  return Object.keys(PHYSICIAN_META).filter((pid) => !REFERENCE_PHYSICIANS.includes(pid));
}

// S1/S2 是全局跑一次、三列共用的（CLAUDE.md：S1 全局只跑一次），所以三列的前两步
// 永远同时亮；只有 S3 是各跑各的。这不是偷懒，是如实反映后端的执行形状。
const COLUMN_STEPS = [
  { key: "s1", label: "症状标准化" },
  { key: "s2", label: "证素推断" },
  { key: "s3", label: "证型与方药" },
];

function columnStepsHtml(reached) {
  return `<ol class="col-steps">` + COLUMN_STEPS.map((s) => {
    const idx = COLUMN_STEPS.findIndex((x) => x.key === s.key);
    const at = COLUMN_STEPS.findIndex((x) => x.key === reached);
    const state = idx < at ? "done" : idx === at ? "active" : "todo";
    return `<li class="step step-${state}" data-step="${s.key}">${escapeHtml(s.label)}</li>`;
  }).join("") + `</ol>`;
}

function columnRunningHtml(physician, reached) {
  const meta = PHYSICIAN_META[physician] || {};
  return columnShellHtml(physician, meta.name || physicianName(physician),
                         meta, "running", columnStepsHtml(reached));
}

function columnWaitingHtml(physician) {
  const meta = PHYSICIAN_META[physician] || {};
  return columnShellHtml(physician, meta.name || physician, meta, "waiting",
    `<div class="col-wait">等待中——另一位医家正在向患者确认信息</div>`);
}

function columnFollowupHtml(physician, question) {
  const meta = PHYSICIAN_META[physician] || {};
  const body = `<div class="col-ask">
      <div class="ask-q">${escapeHtml(question)}</div>
      <div class="input-row input-row-left">
        <input type="text" class="ask-input" placeholder="请输入回答，例如：有 / 没有" />
        <button type="button" class="ask-submit">回答</button>
      </div>
    </div>`;
  return columnShellHtml(physician, meta.name || physician, meta, "asking", body);
}

// 信息不足不是错误：系统承认没法有依据地辨证。三列位置各一句话 + 该补什么，
// 不留空白——留空白的话页面看起来像坏了，而它其实是好好地拒绝了。
function columnInsufficientHtml(physician, reason) {
  const meta = PHYSICIAN_META[physician] || {};
  return columnShellHtml(physician, meta.name || physician, meta, "insufficient",
    `<div class="col-insufficient">
       <div>现有症状不足以推断证素，这一列没有结论。</div>
       <div class="ins-reason">${escapeHtml(reason || "请补充舌象、脉象、寒热喜恶、二便等信息。")}</div>
     </div>`);
}

function clearColumns() {
  const el = document.getElementById("columns");
  if (el) el.innerHTML = "";
}

// ---------- R37：单链问诊流程（九段） ----------
//
// **结构化模式只有一份结论，三列等宽的版面在这里没有意义**（空着两列比摆一列
// 更像"坏了"）。九段是一条链，纵向排、段号显式——链条这件事在 schema 层就是
// 真的（`from_organs` / `from_syndrome` / `from_method` 逐字校验，见
// core/schemas.py::_S3StructuredBase），界面只是把它显示出来。
//
// 九段的顺序 = 申报书 2.1 的五步链 + 它前面的三步（主诉/证素/追问）
// + 后面的一步（校验与出处）。**不许重排**：顺序本身是"先说依据、再说结论"。
const CHAIN_SECTIONS = [
  { key: "complaint", no: "①", title: "主诉与标准化症状", step: "s1" },
  { key: "elements", no: "②", title: "证素", step: "s2" },
  { key: "followup", no: "③", title: "追问", step: "s2" },
  { key: "organs", no: "④", title: "病变脏腑", step: "s3" },
  { key: "syndrome", no: "⑤", title: "证型", step: "s3" },
  { key: "method", no: "⑥", title: "治法", step: "s3" },
  { key: "formula", no: "⑦", title: "方剂", step: "s3" },
  { key: "herbs", no: "⑧", title: "药物组成", step: "s3" },
  { key: "checks", no: "⑨", title: "校验与出处", step: "s3" },
];

// 这台服务的 S3 形状。**问诊开始之前就要知道**（/health 下发），
// 否则跑的那几十秒里只能先摆一个可能是错的骨架、再当场换掉。
// 一次问诊结束后以 manifest.s3_mode 为准（那份记的是真的跑了哪一条）。
let SERVER_S3_MODE = null;

function isSingleChain(manifest) {
  if (manifest && manifest.s3_mode) return manifest.s3_mode === "structured";
  return SERVER_S3_MODE === "structured";
}

// 这一份**响应**该不该画成单链。跟 `isSingleChain()` 分开是因为两者回答的不是
// 同一个问题：那个回答"这台服务/这次问诊跑的是哪条链"（模式），这个回答"眼前这
// 份数据能不能按一条链画"（形状）。`renderChainFlow()` 只读 `results[0]`，所以
// 一份多医家的响应走进单链分支 = 另外几位医家的结论被静默丢掉——而模式在
// manifest 缺 `s3_mode` 时（回放的老响应、fixture）是**回落猜的**，结论条数是
// 事实。事实优先于猜测。
function isSingleChainResult(data) {
  if (((data || {}).results || []).length > 1) return false;
  return isSingleChain((data || {}).manifest);
}

// 「某位医家影响了哪一步」里那个 step 的中文名。**复用九段自己的段名**，
// 不另建一张表——影响的就是这几段，页面上两处出现同一个概念时必须同名
// （`PhysicianInfluence.step` 的枚举是 organ/syndrome/method/formula/herbs，
// 而段的 key 是 `organs`——schema 用单数，这一处是唯一要照顾的差别）。
function influenceStepLabel(step) {
  const key = step === "organ" ? "organs" : step;
  const sec = CHAIN_SECTIONS.find((x) => x.key === key);
  return sec ? sec.title : (step || "");
}

function chainLine(key, value) {
  if (value === null || value === undefined || value === "") return "";
  return `<div class="chain-line"><span class="chain-key">${escapeHtml(key)}</span>${value}</div>`;
}

// R56 §6 第 9 条：「轮数：3（max_rounds）」是给自己看的统计口径，医师要的是
// "系统问了什么、病人怎么答的"这段对话本身——`f.history` 每一轮都带
// `question`/`answer`（core/schemas.py::HistoryItem），原样摆成一问一答的
// 气泡，比一行统计更快让医师确认"该问的都问了、答案也对"。
// 轮数/停因挪到气泡下面一行小结，不再是唯一的呈现方式，而是补充。
function followupChatHtml(f) {
  const turns = (f.history || []).map((h) => `
    <li class="followup-turn">
      <div class="followup-bubble followup-q">${escapeHtml(h.question || "")}</div>
      <div class="followup-bubble followup-a">${escapeHtml(h.answer || "")}</div>
    </li>`).join("");
  const parts = [];
  if (turns) {
    parts.push(`<div class="followup-chat" aria-label="补充问诊">`
      + `<div class="followup-chat-title">补充问诊</div>`
      + `<ul class="followup-turns">${turns}</ul>`
      + `</div>`);
  }
  // 小结：轮数/停因 + 汇总出的确认/排除项，回答的是"结果"，气泡回答的是
  // "过程"——两者都要，读者可以先扫小结、要细节再看气泡。
  parts.push(chainLine("小结", `共 ${escapeHtml(String(f.rounds))} 轮，`
    + `${escapeHtml(f.stopped_by_label || f.stopped_by || "")}`));
  if ((f.asserted || []).length) {
    parts.push(chainLine("问出的症状", escapeHtml((f.asserted || []).join("、"))));
  }
  if ((f.denied || []).length) {
    parts.push(chainLine("已排除", escapeHtml((f.denied || []).join("、"))));
  }
  return parts.join("");
}

// 可点开释义的词。`data-node` 是节点 id、`data-name` 是显示名——
// 证型节点的 id 是 `syn::{physician}`（那个 id 是证据链反查的键，改不得），
// 名字只在 label 里，所以两个都带上（见 core/node_explain.explain_node 的文档）。
function explainable(nodeId, text, name) {
  return `<span class="explainable" data-node="${escapeHtml(nodeId)}"`
    + ` data-name="${escapeHtml(name || text)}" role="button" tabindex="0">`
    + `${escapeHtml(text)}</span>`;
}

function chainSectionHtml(sec, state, body) {
  return `<section class="chain-sec" data-key="${sec.key}" data-state="${state}">
    <h3><span class="chain-no">${sec.no}</span>${escapeHtml(sec.title)}</h3>
    <div class="chain-body">${body || ""}</div>
  </section>`;
}

// R55：第九段「校验与出处」原来跟④-⑧共用 sec.step === "s3" 这一个粗桶，
// 于是 s3_done 之后进入验证/佐证/个体化这几步的等待期间，⑨看起来跟④-⑧
// 流式输出中一模一样——都是 state="active" + 固定文案「推理中…」。用户在
// 这段时间里唯一能看到变化的地方是底部一行行滚动的进度日志，主状态区完全
// 不动，286 秒卡在「推理中」正是这个空档（见本轮报告 §5.2 的真机实证）。
// 独立跟踪⑨自己的状态，不再从 reached 推：pending（还没到）→ active
// （S3 已完，正在验证/佐证，还没有 verify_revise）→ verifying（正在回灌
// 重开，带轮次）。"done" 不需要在这里出现——终值由 renderChainFlow() 整体
// 替换掉骨架，不经过这个函数。
let chainChecksState = { phase: "pending" };

function resetChainChecksState() {
  chainChecksState = { phase: "pending" };
}

function chainChecksBodyHtml() {
  if (chainChecksState.phase === "verifying") {
    return `<span class="chain-key">核对中（第 ${chainChecksState.round} 轮）</span>`;
  }
  if (chainChecksState.phase === "active") {
    return "<span class=\"chain-key\">推理中…</span>";
  }
  return "";
}

// 跑到哪一步了 → 九段各自的状态。step 的取值跟 COLUMN_STEPS 同一套
// （s1/s2/s3），**复用那套而不是另编一套**：两套的话进度会各走各的。
// ⑨是例外（见上面 chainChecksState 的注释）：它不跟着 reached 走。
function chainStateFor(sec, reached) {
  if (sec.key === "checks") {
    return chainChecksState.phase === "pending" ? "todo" : "active";
  }
  const order = COLUMN_STEPS.map((x) => x.key);
  const at = order.indexOf(reached);
  const mine = order.indexOf(sec.step);
  if (mine < at) return "done";
  if (mine === at) return "active";
  return "todo";
}

function renderChainSkeleton(reached) {
  const el = document.getElementById("chain-flow");
  if (!el) return;
  // S3 这一步一旦被触达（s3_start/s3_delta/s3_done 都传 "s3"），⑨从
  // pending 变成 active——不用等 s3_done：S3 一开始验证器就已经"在排队"了，
  // 摆一个「推理中…」比让它继续显示"还没轮到"更诚实。
  if (reached === "s3" && chainChecksState.phase === "pending") {
    chainChecksState = { phase: "active" };
  }
  el.innerHTML = CHAIN_SECTIONS.map((sec) => chainSectionHtml(
    sec, chainStateFor(sec, reached),
    sec.key === "checks" ? chainChecksBodyHtml()
      : (chainStateFor(sec, reached) === "todo" ? "" : "<span class=\"chain-key\">推理中…</span>"),
  )).join("");
  showChainFlow();
}

function showChainFlow() {
  const flow = document.getElementById("chain-flow");
  const cols = document.getElementById("columns");
  if (flow) flow.classList.add("show");
  // **三列不是藏起来，是清空**：留在 DOM 里的话"这次跑的是哪一条"看不出来，
  // 而且上一次 legacy 的三列会被滚出来（同 showSafetyBlock 那条理由）。
  if (cols) cols.innerHTML = "";
}

function hideChainFlow() {
  const flow = document.getElementById("chain-flow");
  if (flow) { flow.classList.remove("show"); flow.innerHTML = ""; }
  closeNodeExplain();
}

function chainHerbRow(choice, pid) {
  const item = (choice && choice.item) || {};
  const nodeId = `herb::${pid}::x::${item.name || ""}`;
  const dose = item.dose ? `${item.dose}${item.dose_unit || "g"}` : "";
  const bits = [
    explainable(nodeId, stripDose(item.name || ""), stripDose(item.name || "")),
    item.role ? `<span class="chain-key">${escapeHtml(item.role)}</span>` : "",
    dose ? escapeHtml(dose) : "",
    choice.for_element ? `→ ${escapeHtml(choice.for_element)}` : "",
    choice.effect_cited ? `（${escapeHtml(choice.effect_cited)}）` : "",
    choice.physician_source
      ? `【${escapeHtml(physicianName(choice.physician_source))}】` : "",
  ].filter(Boolean);
  return `<div class="chain-line">${bits.join(" ")}</div>`;
}

function stripDose(name) {
  // 药名剥剂量：跟后端 label 的做法对齐（id 保原样、label 剥）。
  return String(name || "").replace(/[（(][^）)]*[）)]/g, "").replace(/\d+(\.\d+)?\s*[gG克钱两]/g, "").trim();
}

// 九段的填充。**每一段的数据来源写在对应分支的注释里**——一段填不出来时
// 显示的是"这一步没有产出"而不是空白（空白看起来像坏了）。
//: 代理决策的一行。**`why` 是制度、`detail` 是这一次的证据，两行分开**
//: ——合成一句的话，读者分不清"系统一向这么做"和"这次是因为你说了这句话"。
function agentTraceHtml(trace) {
  const rows = (trace || []).filter((d) => d && d.capability);
  if (!rows.length) return "";
  const items = rows.map((d) => {
    const kind = d.stop_kind_label
      ? `<span class="agent-kind">${escapeHtml(d.stop_kind_label)}</span>` : "";
    return `<li class="agent-step" data-capability="${escapeHtml(d.capability)}">
      <span class="agent-cap">${escapeHtml(d.capability_label || d.capability)}</span>${kind}
      <span class="agent-why">${escapeHtml(d.why || "")}</span>
      ${d.detail ? `<span class="agent-detail">${escapeHtml(d.detail)}</span>` : ""}
    </li>`;
  }).join("");
  return `<section class="agent-trace" aria-label="本次辨证的处理决策">
      <div class="agent-trace-title">本次处理经过</div>
      <ol class="agent-steps">${items}</ol>
    </section>`;
}

// R56 §6 第 13 条：⑨「校验与出处」原来是"违规 veto N / revise N" + "判不了
// N（...）"两行统计，医师读不出"到底是哪几条规则的事"。改成逐条核对清单——
// 每条规则一行，✓ 通过 / ✗ 不通过（附原因） / — 判不了（附缺什么）。
//
// 一条规则可能同时落在 violations 与 checked_rules 里（`check_meridian_
// coverage` 那类：有违规时 checked 依然带它自己的名字，因为"判过"这件事跟
// "判的结果是不是违规"是两回事，见 core/formula_verifier.py 的模块注释）——
// 所以这里按 violations > checked_rules（通过） > unverifiable 的优先级取
// 每条规则的显示状态：出了问题最要紧，其次是判过通过，最后才是判不了。
function verificationChecklistHtml(v) {
  const byRule = {};
  for (const rule of (v.checked_rules || [])) {
    byRule[rule] = { state: "pass", label: (v.checked_rule_labels || {})[rule] || rule };
  }
  for (const u of (v.unverifiable || [])) {
    if (!byRule[u.rule]) {
      byRule[u.rule] = { state: "unverifiable", label: u.rule_label || u.rule, detail: u };
    }
  }
  for (const viol of (v.violations || [])) {
    byRule[viol.rule] = { state: "fail", label: viol.rule_label || viol.rule, detail: viol };
  }
  const rules = Object.keys(byRule);
  if (!rules.length) return "";
  const marks = { pass: "✓", fail: "✗", unverifiable: "—" };
  const items = rules.map((rule) => {
    const e = byRule[rule];
    const reason = e.state === "fail" ? (e.detail && e.detail.reason)
      : e.state === "unverifiable" ? `判不了：${(e.detail && e.detail.reason) || ""}` : "";
    return `<li class="verify-item verify-${e.state}">`
      + `<span class="verify-label">${escapeHtml(e.label)}</span>`
      + `<span class="verify-mark">${marks[e.state]}</span>`
      + (reason ? `<div class="verify-reason">${escapeHtml(reason)}</div>` : "")
      + `</li>`;
  });
  return `<ul class="verify-checklist" aria-label="逐条校验">${items.join("")}</ul>`;
}

function renderChainFlow(data) {
  const el = document.getElementById("chain-flow");
  if (!el) return;
  const r = (data.results || [])[0] || {};
  const st = r.s3_structured || null;
  const s3 = r.s3 || {};
  const pid = r.physician || "synthesis";
  const s1 = data.s1 || {};
  const s2 = data.s2 || {};
  const parts = [];

  // **这条链是谁的**：三列有列头，单链没有——名字要是不写在链顶上，页面上就只有
  // §⑨ 的归因里能翻出来。而「本次辨证」（structured：融合出的一份结论）和
  // 「叶天士一家」（legacy：单跑一家）是两种完全不同的结论，读的人必须一眼看见
  // 自己在看哪一种。引用了几家的经验照实数（`physicians_cited`），
  // 不拿配置里的家数当数——名字是配置，数是这次真跑出来的。
  const cited = new Set([...(r.physicians_cited || []),
                         ...(r.physician_influences || []).map((x) => x.physician)]);
  parts.push(`<header class="chain-head">`
    + `<span class="chain-who">${escapeHtml(r.physician_name || pid)}</span>`
    + (r.school ? `<span class="chain-school">${escapeHtml(r.school)}</span>` : "")
    + (cited.size
        // R44：措辞从「引到 N 位医家」改成「引用名老中医经验 N 家」。
        // 前者读起来像"有 N 个人参与了这次判断"（投票），后者说的是
        // "这一份判断引用了 N 家的经验"（依据）。同一个数、同一份数据，
        // 改的是它在产品面上表达的关系。
        ? `<span class="chain-note">引用名老中医经验 ${cited.size} 家</span>` : "")
    + `</header>`);
  // R44：代理这一次做过的决策（停/问/取证/验）。**摆在链顶下面、九段上面**：
  // 它回答的是"系统在这一步做了什么、凭什么"，而三甲的主治医师问的正是这个。
  // 中文名由后端下发（`capability_label` / `stop_kind_label`），前端不写死。
  parts.push(agentTraceHtml(data.agent_trace));

  for (const sec of CHAIN_SECTIONS) {
    let body = "";
    if (sec.key === "complaint") {
      body = chainLine("主诉", escapeHtml(LAST_COMPLAINT || ""))
        + chainLine("症状", (s1.symptoms || []).map((x) =>
            explainable(`sym::${x}`, x)).join("、"))
        + chainLine("舌", escapeHtml(s1.tongue || "未记"))
        + chainLine("脉", escapeHtml(s1.pulse || "未记"))
        + ((s1.unmapped || []).length
            ? chainLine("未归类表述", escapeHtml((s1.unmapped || []).join("、"))) : "");
    } else if (sec.key === "elements") {
      const hits = (s2.elements || []).map((h) =>
        `${explainable(`elem::${h.element}`, h.element)}`
        + `<span class="chain-key">${escapeHtml(h.kind === "location" ? "病位" : "病性")}</span>`
        + `${escapeHtml(confidenceLabel(h.confidence))}`
        + `（依据：${escapeHtml((h.supporting_symptoms || []).join("、"))}）`);
      body = hits.length ? hits.map((x) => `<div class="chain-line">${x}</div>`).join("")
                         : chainLine("证素", "（这一步没有推出证素）");
      if ((s2.unexplained_symptoms || []).length) {
        body += chainLine("未被解释的症状",
          escapeHtml((s2.unexplained_symptoms || []).join("、")));
      }
    } else if (sec.key === "followup") {
      const f = data.followup;
      body = f
        ? followupChatHtml(f)
        : chainLine("追问", "（这一次没有追问：没有提问渠道或 FAST_MODE）");
    } else if (sec.key === "organs") {
      body = st && (st.organs || []).length
        ? (st.organs || []).map((o) => `<div class="chain-line">`
            + `${explainable(`elem::${o.organ}`, o.organ)} `
            + `${escapeHtml(o.pathogenesis || "")}`
            + `（依据：${escapeHtml((o.supporting_symptoms || []).join("、"))}）</div>`).join("")
        : chainLine("病变脏腑", "（legacy 模式不产出这一步，见第⑤段的证型推理）");
    } else if (sec.key === "syndrome") {
      const name = st ? st.syndrome.name : s3.syndrome;
      const disease = st ? st.syndrome.disease : s3.disease;
      body = chainLine("证型", explainable(`syn::${pid}`,
                disease ? `${disease} · ${name}` : (name || ""), name || ""))
        + (st ? chainLine("从哪些脏腑推出",
              escapeHtml((st.syndrome.from_organs || []).join("、"))) : "")
        + chainLine("推理", escapeHtml((st ? st.syndrome.reasoning : s3.reasoning) || ""))
        + (st && st.syndrome.reasoning_plain
            ? chainLine("白话", escapeHtml(st.syndrome.reasoning_plain)) : "");
    } else if (sec.key === "method") {
      body = chainLine("治法", escapeHtml((st ? st.method.principle : s3.treatment_principle) || ""))
        + (st ? chainLine("接住的证型", escapeHtml(st.method.from_syndrome || "")) : "")
        + (st ? chainLine("针对", escapeHtml((st.method.targets || []).join("、"))) : "");
    } else if (sec.key === "formula") {
      const cand = st ? st.formula.candidate
                      : ((s3.formula_candidates || [])[s3.selected || 0] || {});
      body = chainLine("方剂", explainable(`formula::${pid}::${cand.name || ""}`, cand.name || ""))
        + (st ? chainLine("接住的治法", escapeHtml(st.formula.from_method || "")) : "")
        + chainLine("来源", escapeHtml(formulaSourceLabel(cand.source)))
        + chainLine("理由", escapeHtml(cand.rationale || ""))
        + (st && (st.formula.ontology_refs || []).length
            ? chainLine("本体出处", (st.formula.ontology_refs || []).map((x) =>
                escapeHtml(`${x.predicate}：${x.span}`)).join("；")) : "");
    } else if (sec.key === "herbs") {
      const choices = st ? (st.herb_choices || []) : [];
      if (choices.length) {
        body = choices.map((c) => chainHerbRow(c, pid)).join("");
      } else {
        const cand = (s3.formula_candidates || [])[s3.selected || 0] || {};
        body = (cand.herb_items || []).map((it) =>
          chainHerbRow({ item: it }, pid)).join("")
          || chainLine("药物组成", "（这一步没有产出）");
      }
    } else if (sec.key === "checks") {
      const v = r.verification || null;
      const m = r.verifier_metrics || null;
      body = v
        ? chainLine("符号验证", escapeHtml(v.status_label || v.status || ""))
          + verificationChecklistHtml(v)
        : chainLine("符号验证", "（legacy 模式不跑符号验证器）");
      if (m && m.revise_rounds !== undefined) {
        body += chainLine("重开轮数", escapeHtml(String(m.revise_rounds)));
      }
      if (r.herbs_grounded_ratio !== null && r.herbs_grounded_ratio !== undefined) {
        // 这个数必须带口径：分母是**这一方的药味数**，不是本体总药味
        // （R34b 那两个分母）。少了这句话它会被当成"本体覆盖率"。
        body += chainLine("带本体出处的药味占比",
          `${Math.round(r.herbs_grounded_ratio * 100)}%`
          + `<span class="chain-key">分母＝本方药味数</span>`);
      }
      if ((r.physicians_cited || []).length) {
        body += chainLine("引用的名老中医经验", (r.physicians_cited || []).map((x) =>
          escapeHtml(physicianName(x))).join("、"));
      }
      for (const inf of (r.physician_influences || [])) {
        body += chainLine(`${physicianName(inf.physician)}·${influenceStepLabel(inf.step)}`,
          `${escapeHtml(inf.contribution || "")}`
          + `（医案 ${escapeHtml((inf.cited_case_ids || []).join("、"))}）`);
      }
      if ((r.hallucinated || []).length) {
        body += chainLine("⚠ 编造的医案号", escapeHtml((r.hallucinated || []).join("、")));
      }
    }
    parts.push(chainSectionHtml(sec, "done", body));
  }
  el.innerHTML = parts.join("");
  showChainFlow();
}

// ---------- R37/R42：节点释义面板（零 LLM，八节，取不到就整块隐藏） ----------
//
// R42：八节（是什么/病机/药理/出处原文/名老中医经验/验证结果/循证对照/注意）。
// **这一层不写死节的清单**——标题与顺序全由后端下发，前端只负责按顺序渲染。
// 写死一份的话，后端加一节前端不显示，而"不显示"看起来跟"这一节没内容"一样。

let NODE_EXPLAIN_SEQ = 0;
// R56 §6：吸顶侧栏的「固定」——钉住之后点图上/正文里别的节点不再换内容，
// 医师可以带着这份释义到处看图，不会被自己下一次点击顶掉。跟 graph.js 的
// `.gt-tooltip.pinned` 是两个不同的东西（那个钉的是悬浮 tooltip，这个钉的
// 是侧栏面板），故意不共用一个状态名，避免以后有人以为改一个另一个也变了。
let NODE_EXPLAIN_PINNED = false;

async function openNodeExplain(nodeId, name) {
  const el = document.getElementById("node-explain");
  if (!el) return;
  if (NODE_EXPLAIN_PINNED && el.classList.contains("show")) return;
  const seq = ++NODE_EXPLAIN_SEQ;
  // R43：**点下去立刻有反馈。** 改之前面板要等响应回来才出现——点一个节点之后
  // 屏幕上什么都不变，人会以为"点了没反应"再点一下（于是又发一次请求）。
  // 先摆一个带节点名的骨架，拿到内容再替换；`available=false` 时整块隐藏的
  // 语义没变（那一条在下面）。
  showNodeExplainPending(name || nodeId);
  try {
    const qs = `node=${encodeURIComponent(nodeId)}`
      + (name ? `&name=${encodeURIComponent(name)}` : "");
    const resp = await fetch(`/api/node_explain?${qs}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    // **过期响应不渲染**：连点两个节点时先回来的那个不该覆盖后点的那个。
    if (seq !== NODE_EXPLAIN_SEQ) return;
    if (!data.available) { closeNodeExplain(); return; }
    renderNodeExplain(data);
  } catch (err) {
    // 释义取不到不该弹红条打断问诊——它是附加信息。整块隐藏即可。
    if (seq === NODE_EXPLAIN_SEQ) closeNodeExplain();
  }
}

//: 等待态的骨架。**不是一个转圈图标**：这里能立刻说出"正在查哪个节点"，
//: 那比一个匿名的加载动画有用得多（人据此确认自己点对了）。
//: `aria-busy` 让读屏软件知道这一块还在变。
//: 面板头部那两个按钮，钉/关闭两处共用同一个片段——按钮的判据只有一处。
function neHeaderButtonsHtml() {
  return `<button type="button" class="ne-pin" data-ne-pin="1" `
    + `aria-pressed="${NODE_EXPLAIN_PINNED ? "true" : "false"}" `
    + `title="固定：点图上/正文里别的节点不再换内容">${NODE_EXPLAIN_PINNED ? "📌 已固定" : "📌 固定"}</button>`
    + `<span class="ne-close" data-ne-close="1">×</span>`;
}

function showNodeExplainPending(title) {
  const el = document.getElementById("node-explain");
  if (!el) return;
  el.innerHTML = `<div>${neHeaderButtonsHtml()}
      <span class="ne-title">${escapeHtml(String(title || ""))}</span>
      <span class="ne-kind">查询中…</span></div>`;
  el.setAttribute("aria-busy", "true");
  el.classList.add("show");
  document.body.classList.add("ne-open");
}

function renderNodeExplain(data) {
  const el = document.getElementById("node-explain");
  if (!el) return;
  // R56 §6 第 7 条：证候编码（SP-01/B04.xxx）不进正文——`s.codes` 是后端
  // 单独带出来的编码列表，只挂成这一节标题的 title= 属性（鼠标悬停才看
  // 得到），正文 `lines` 里已经不含编码。
  const secs = (data.sections || []).map((s) => `<div class="ne-sec">
      <div class="ne-head"${(s.codes || []).length
        ? ` title="编码：${escapeHtml(s.codes.join("、"))}"` : ""}>${escapeHtml(s.heading)}</div>
      ${(s.lines || []).map((ln) => `<div class="ne-line">${escapeHtml(ln)}</div>`).join("")}
      ${s.source ? `<div class="ne-src">出处：${escapeHtml(s.source)}</div>` : ""}
    </div>`).join("");
  el.innerHTML = `<div>${neHeaderButtonsHtml()}
      <span class="ne-title">${escapeHtml(data.title || "")}</span>
      <span class="ne-kind">${escapeHtml(NODE_KIND_LABEL[data.kind] || data.kind || "")}</span>
    </div>${secs}`;
  el.setAttribute("aria-busy", "false");
  el.classList.add("show");
  document.body.classList.add("ne-open");
}

//: 节点种类的中文名。**R42 补齐三类新节点**（病机/治则/治法）——漏一个的
//: 表现是面板标题旁边冒出一个英文 id（`pathogenesis`），跟 R37 截图上那个
//: 「meridian_coverage缺归经」是同一类事故。
const NODE_KIND_LABEL = {
  symptom: "症状", element: "证素", syndrome: "证型",
  pathogenesis: "病机", principle: "治则", method: "治法",
  formula: "方剂", herb: "药材", case: "医案",
  // R62 §7.2：医理规则也能点开（内容 / 出处 / 用在哪一步）。这一行是
  // `tests/test_chain_flow_frontend.py` 那条"前后端种类清单必须一致"的
  // 测试逼出来的——后端 NodeKind 加一种，这里不加就会在面板标题后面
  // 印出一个英文单词。
  rule: "医理规则",
};

function closeNodeExplain() {
  const el = document.getElementById("node-explain");
  if (el) {
    el.classList.remove("show");
    el.innerHTML = "";
    el.setAttribute("aria-busy", "false");
  }
  // Esc/关闭键无条件解除固定：固定是"面板开着的时候别换"，面板都关了就没有
  // "别换"这回事，留着这个状态会让下次打开面板时无缘无故就是钉住的。
  NODE_EXPLAIN_PINNED = false;
  document.body.classList.remove("ne-open");
}

// 事件委托挂在 document 上一次，不给每个 .explainable 各挂一个——
// 九段每次重渲染都会换掉所有节点，逐个挂等于每次泄一批监听器
// （同 #columns 那处 delegation 的理由）。
document.addEventListener("click", (e) => {
  const closer = e.target.closest("[data-ne-close]");
  if (closer) { closeNodeExplain(); return; }
  const pinner = e.target.closest("[data-ne-pin]");
  if (pinner) {
    NODE_EXPLAIN_PINNED = !NODE_EXPLAIN_PINNED;
    pinner.setAttribute("aria-pressed", NODE_EXPLAIN_PINNED ? "true" : "false");
    pinner.textContent = NODE_EXPLAIN_PINNED ? "📌 已固定" : "📌 固定";
    return;
  }
  const hit = e.target.closest(".explainable");
  if (hit) openNodeExplain(hit.dataset.node, hit.dataset.name);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const hit = e.target.closest && e.target.closest(".explainable");
  if (hit) { e.preventDefault(); openNodeExplain(hit.dataset.node, hit.dataset.name); }
});

function renderColumnsPlaceholder(builder) {
  const el = document.getElementById("columns");
  if (!el) return;
  el.innerHTML = physicianOrder().map(builder).join("");
}

// ---------- 辨证中：按 physician 路由，不假设顺序 ----------
//
// **R12 之后三位医家是并发跑的**，physician_start / s3_start / physician_done
// 的到达顺序完全是乱的（docs/DESIGN.md §4.7 的订正）。所以这里按事件里的
// physician 字段更新对应那一列，绝不按到达次序去推"现在轮到谁了"。
let COLUMN_PROGRESS = {};

function resetColumnProgress() {
  COLUMN_PROGRESS = {};
  for (const pid of physicianOrder()) COLUMN_PROGRESS[pid] = "s1";
}

function setColumnStep(physician, step) {
  if (!physician) {
    // S1/S2 是全局的，三列一起走。
    for (const pid of Object.keys(COLUMN_PROGRESS)) COLUMN_PROGRESS[pid] = step;
  } else {
    COLUMN_PROGRESS[physician] = step;
  }
  renderColumnsPlaceholder((pid) => columnRunningHtml(pid, COLUMN_PROGRESS[pid] || "s1"));
}

// ============================================================================
// R15：患者模式的独立形态（docs/DESIGN.md §3.5）
// ============================================================================
//
// **整个界面形态不同，不是三列的裁剪版。** 患者要的是"我该去哪个科、什么情况
// 必须马上走"，不是三位古代医家的辨证异同——把三列裁一裁给他看，等于让他自己
// 从一堆他读不懂的东西里找那两句有用的。
//
// 三条硬规矩：
//   一、**红旗症状必须在首屏、不能折叠**。折叠起来的急救信息等于没有。
//   二、`triage_urgency = high` 时不显示任何食疗与中成药建议。这条闸门在服务端
//       （`_apply_medication_gate`），这里只是不去暗示它本该有内容。
//   三、病名取 `triage.disease`（get_disease 核实过的那个），**不取
//       `results[].s3.disease`**——后者是模型原样吐出来的字符串，可能根本不在
//       参考表里。一个没核实过的病名摆在患者看的第一行上，比不摆更危险。

function patientRowHtml(label, value) {
  return `<div class="pv-row"><span class="pv-label">${escapeHtml(label)}</span>`
    + `<span class="pv-value">${escapeHtml(value)}</span></div>`;
}

function patientViewHtml(data) {
  const t = (data && data.triage) || null;
  if (!t) {
    // 没有导诊依据时如实说，不编一个科室出来。这跟后端 _compute_triage
    // 返回 None 的理由是同一条：宁可说"不知道"，不给一个猜的科室。
    return `<div class="pv-inner">
      <div class="pv-lead">根据你描述的症状</div>
      <div class="pv-none">系统没能把这些症状对应到参考病名表里的任何一条，所以这里不给科室建议——不是没有问题，是这套系统这次判断不了。症状持续或加重请直接就医。</div>
      <div class="pv-note">这不是诊断。中医辨证需要面诊，这里只帮你判断该看哪个科。</div>
    </div>`;
  }
  const flags = t.red_flags || [];
  const flagList = flags.length
    ? `<div class="pv-flags-title">出现以下情况请立即就医：</div>
       <ul class="pv-flags">${flags.map((f) => `<li>${escapeHtml(f)}</li>`).join("")}</ul>`
    : "";
  const gated = t.urgency === "high";
  const food = (data.food_therapy || []).length;
  const otc = (data.patent_medicines || []).length;
  const care = gated
    ? `<div class="pv-gated">这类症状紧急度高，按安全规则这里不提供任何用药或食疗建议，请尽快就医。</div>`
    : (food || otc)
      ? `<div class="pv-care">可参考的调养：食疗 ${food} 条、中成药 ${otc} 条</div>`
      : `<div class="pv-care pv-care-empty">食疗与中成药数据尚未接入，此处暂无内容——不是"没有可用的"，是这一块还没做。</div>`;
  return `<div class="pv-inner urgency-${escapeHtml(t.urgency || "low")}">
    <div class="pv-lead">根据你描述的症状</div>
    ${patientRowHtml("可能属于", t.disease || "（未能对应到参考病名）")}
    ${patientRowHtml("建议就诊", t.dept || "具体科室建议现场分诊")}
    <hr class="pv-rule" />
    ${flagList}
    <hr class="pv-rule" />
    ${care}
    <div class="pv-note">这不是诊断。中医辨证需要面诊，这里只帮你判断该看哪个科。</div>
  </div>`;
}

function renderPatientView(data) {
  const el = document.getElementById("patient-view");
  if (!el) return;
  el.innerHTML = patientViewHtml(data);
  el.hidden = false;
}

function hidePatientView() {
  const el = document.getElementById("patient-view");
  if (el) { el.hidden = true; el.innerHTML = ""; }
}

// ---------- R14：一列集注 ----------
//
// **不是卡片。** 三家是对同一段主诉的三种读法（docs/DESIGN.md §3.1 第一条），
// 卡片会暗示它们是三个独立的东西。列与列之间只有一条 --rule-soft 细线。
//
// **身份色只用在顶部三行 + 3px 顶边**（第二条）。列内正文一律 --ink：整列染色
// 会让人看颜色而不看内容。色值走 `var(--phys-<id>)`，由 /health 注入——
// 这里不写任何十六进制（CLAUDE.md 第 31 条前端小节）。
//
// 函数名从 cardHtml 改成 columnHtml：R13 之前它渲染的确实是一张卡片，现在不是了，
// 留着旧名字会让下一个人以为外面还包着 .card。
function columnHeadHtml(physician, name, meta) {
  const m = meta || {};
  const years = m.years ? `<div class="col-years">${escapeHtml(m.years)}</div>` : "";
  const school = m.school ? `<div class="col-school">${escapeHtml(m.school)}</div>` : "";
  return `<div class="col-head">
    <div class="col-name">${escapeHtml(name || physician)}</div>
    ${years}${school}
  </div>`;
}

// 一列的外壳。五种状态共用它，区别只在 body 和 data-state——外壳统一，
// 三列在任何状态下都等宽等高，不会因为某一列还在跑就把版面挤歪。
function columnShellHtml(physician, name, meta, state, body) {
  return `<div class="col" data-physician="${escapeHtml(physician)}" data-state="${escapeHtml(state)}"
       style="--col: var(--phys-${escapeHtml(physician)}, var(--ink));">
    ${columnHeadHtml(physician, name, meta)}
    <div class="col-body">${body}</div>
  </div>`;
}

// R24：R23 的建议层渲染。**按 severity 分三档视觉**，不是一串同样的灰字：
// blocking（配伍禁忌/超剂量）用 --danger、warning（寒热）用 --caution、
// suggestion（缺引经/重复）用普通正文色——严重度是这一层唯一有信息量的维度，
// 把三档画成一样就等于没渲染。
//
// **patient 角色下这三个键根本不在响应里**（api/main.py::_role_gets_advice
// 服务端就摘掉了），所以这里不需要再判角色：没有数据就渲染不出东西。
// 前端再判一遍角色的话，"谁决定患者能看什么"就有了两个答案。
function adviceListHtml(advice) {
  if (!advice || !advice.length) return "";
  const rows = advice.map((a) => {
    // **不单独渲染 herbs**：`check_formula` 五条规则的 reason 里**都已经点名了
    // 涉及的药**（「甘草 与 甘遂 属配伍禁忌」「白术 与 苍术 性味功效重合 100%」），
    // 前面再重复一遍读起来像是数据出错了——这是截图里当场看出来的，
    // 而 DOM 断言只查"药名在不在"，两种写法都能过。
    // herbs 仍然进 data 属性：它是给机器读的（比如以后要点药名跳到药材层）。
    const herbsAttr = (a.herbs || []).length
      ? ` data-herbs="${escapeHtml((a.herbs || []).join("、"))}"` : "";
    const span = a.source_span
      ? `<span class="adv-source">${escapeHtml(a.source_span)}</span>` : "";
    return `<li class="adv-row adv-${escapeHtml(a.severity || "suggestion")}"${herbsAttr}>`
      + `<span class="adv-reason">${escapeHtml(a.reason || "")}</span>`
      + span + "</li>";
  }).join("");
  return `<ul class="advice-list">${rows}</ul>`;
}

// 没跑的规则如实列出来。**不折叠、不省略**：「这条规则没给出建议」和
// 「这条规则根本没跑」在界面上长得一模一样，而含义相反（R23 的 skipped 字段
// 存在的全部理由）。
function adviceSkippedHtml(skipped) {
  if (!skipped || !skipped.length) return "";
  const rows = skipped.map((s) => {
    const flag = s.available === false ? "缺数据" : "不适用";
    return `<li class="adv-skipped-row"><span class="adv-skipped-flag">${escapeHtml(flag)}</span>`
      + `<span>${escapeHtml(s.reason || "")}</span></li>`;
  }).join("");
  return `<ul class="advice-skipped">${rows}</ul>`;
}

// 一整块：分 + 建议 + 没跑的规则。分数**带口径说明**（"粗排序尺，不是疗效评分"）
// ——一个 0~1 的分摆在方子旁边而不说它是什么，读者只会当成"这方有多好"。
function adviceBlockHtml(result) {
  const advice = result.advice;
  const skipped = result.advice_skipped;
  const score = result.formula_score;
  if (!advice && !skipped && (score === undefined || score === null)) return "";
  const scoreHtml = (score === undefined || score === null)
    ? ""
    // 固定两位小数：0 / 0.7 / 0.9 三列并排时，`String(0)` 出来是「0」，
    // 跟「0.7」不在同一个数量级的读感上——三列对照页里这种不齐会被读成别的意思。
    : `<span class="adv-score" title="1.0 − Σ规则权重，粗排序尺，不是疗效评分">`
      + `方剂评分 <b>${escapeHtml(Number(score).toFixed(2))}</b></span>`;
  const bodyHtml = adviceListHtml(advice) + adviceSkippedHtml(skipped);
  const emptyHtml = (advice && advice.length) ? "" :
    `<div class="adv-none">规则层没有发现问题</div>`;
  return `<div class="advice-block">`
    // R47 §0.4：区块名从「方剂建议」改成「方剂核查」。这一层给的是对**方子
    // 本身**的核查结论（配伍禁忌、剂量上限、归经覆盖），不是对这位患者的
    // 用药建议——名字叫「建议」会把一层客观校验说成一条诊疗意见。
    + `<div class="advice-head"><span class="advice-title">方剂核查</span>${scoreHtml}</div>`
    + bodyHtml + emptyHtml + `</div>`;
}

function columnHtml(result, mode = "researcher") {
  const s3 = result.s3;
  const hallucinationHtml = result.hallucinated && result.hallucinated.length > 0
    ? `<div class="hallucination-warning">⚠ 检测到可能的幻觉引用：模型引用了检索结果之外的医案 id（${escapeHtml(result.hallucinated.join("、"))}），请勿采信该结论中的引用。</div>`
    : "";

  // X2：incompatible 非空说明重开一次之后仍有禁忌，这时红标签必须显示——
  // 它代表"系统尝试修正但没修好"，比没检查更需要让人看到。
  const so = result.safety_output || {};
  const incompatibleHtml = so.incompatible && so.incompatible.length > 0
    ? `<div class="safety-incompatible">⚠ 配伍禁忌：${so.incompatible.map(pair => `${escapeHtml(pair[0])} 反/畏 ${escapeHtml(pair[1])}`).join("；")}</div>`
    : "";
  const thermalHtml = so.thermal_warning
    ? `<div class="safety-thermal">⚠ ${escapeHtml(so.thermal_warning)}</div>`
    : "";
  const revisedHtml = so.revised
    ? `<div class="safety-revised">已因配伍禁忌自动重开一次方</div>`
    : "";
  const westernHtml = westernDrugsHtml(s3);
  const noRefHtml = result.no_reference_cases
    ? `<div class="safety-thermal">⚠ 未检索到相关医案（相似度均低于阈值）：本结论没有医案支撑，仅供参考</div>`
    : "";
  const openAttr = defaultDetailsOpenForMode(mode) ? "open" : "";
  const reactHtml = reactTraceDetailsHtml(result.react_trace, openAttr);

  // patient 角色下 formula_candidates/formula/herbs 三个键被后端整个摘掉
  // （api/main.py::_filter_s3_for_role，安全边界不是前端藏起来）。"这个角色
  // 本来就拿不到"跟"这次没开出方"意思完全不同，必须分开说。
  const selectedCand = (s3.formula_candidates && s3.formula_candidates.length)
    ? s3.formula_candidates[s3.selected] : null;
  const isPatient = mode === "patient";
  // 医生模式要编辑全量药味，折叠会让"下面还有几味"变成一次多余的点击。
  const herbsHtml = isPatient
    ? `<div class="doctor-disclaimer">患者模式不提供具体方剂与药材——这是服务端的字段裁剪（响应体里根本没有这几个键），不是本次没开出方。具体用药请咨询执业医师。</div>`
    : selectedCand
      ? herbGroupsHtml(selectedCand, mode !== "doctor")
      : `<div class="field"><b>用药</b>${escapeHtml((s3.herbs || []).join("、") || "（无）")}</div>`;
  const formulaRow = isPatient
    ? ""
    : `<div class="col-formula">${escapeHtml(s3.formula || "（未明确）")}</div>`;

  const body = `
      <div class="col-syndrome">${escapeHtml(s3.syndrome)}</div>
      <div class="col-principle">${escapeHtml(s3.treatment_principle)}</div>
      ${formulaRow}
      ${herbsHtml}
      ${westernHtml}
      ${incompatibleHtml}
      ${thermalHtml}
      ${revisedHtml}
      ${noRefHtml}
      ${hallucinationHtml}
      ${adviceBlockHtml(result)}
      ${doctorSectionHtml(result.physician, mode)}
      ${refFoldHtml(result.refs, result)}
      <details class="col-reasoning" ${openAttr}>
        <summary>推理过程</summary>
        <div class="detail-block">
          <div>${escapeHtml(s3.reasoning)}</div>
          <div class="label">引用医案 id</div>
          <div>${escapeHtml((s3.cited_case_ids || []).join("、") || "（无）")}</div>
          ${s3.note ? `<div class="label">备注</div><div>${escapeHtml(s3.note)}</div>` : ""}
        </div>
      </details>
      ${reactHtml}
  `;
  return columnShellHtml(result.physician, result.physician_name,
                         PHYSICIAN_META[result.physician], "done", body);
}

// 「参西用药」栏。张锡纯「衷中参西」会在方里用阿斯匹林这类西药，是他最有辨识度的
// 特征，单独一栏展示；叶天士/吴鞠通两本书实测一个西药词都没有，他们的 western_drugs
// 恒为空，此时返回空串——不显示一个空栏。提成顶层纯函数是为了能被测试直接调用
// （tests/test_western_drugs.py 用 node 跑的就是这里这份代码，不是另抄一份。
// 原来这行写的是 tests/test_web_render.py，那个文件不存在）。
function westernDrugsHtml(s3) {
  const drugs = (s3 && s3.western_drugs) || [];
  if (!drugs.length) return "";
  return `<div class="western-drugs"><b>参西用药</b>　${escapeHtml(drugs.join("、"))}</div>`;
}


// ---------- R18-I：参考医家（enabled=false 的那几位） ----------
//
// 谁算「参考医家」不由前端判断，读 /health 下发的 enabled 字段
// （源头 core/physicians.py → physicians_all）。前端自己列一份名单就是第二处实现，
// 注册表里开/关一位医家时这份名单不会跟着变（CLAUDE.md 第 31 条）。
// 声明在文件顶部（跟 PHYSICIAN_META 一起）：physicianOrder() 要用它过滤三列，
// 而那个函数在这一段之前定义——`let` 的 TDZ 会让"先调用后声明"直接抛。
// 哪些查看模式显示这一栏。患者模式不显示（不该看到不参与结论的医案），
// 医生模式不显示（版面留给可编辑处方）。
const REFERENCE_ROLES = ["student", "researcher"];

function referenceCaseHtml(c) {
  const parts = [];
  parts.push(`<div class="ref-case-head">`
    + `<span class="ref-case-id">${escapeHtml(c.case_id || "")}</span>`
    + `<span class="ref-case-score">相似度 ${escapeHtml(String(c.score ?? ""))}</span>`
    + (c.visit_index ? `<span class="ref-case-visit">第 ${escapeHtml(String(c.visit_index))} 诊</span>` : "")
    + `</div>`);
  const line = (label, val) => val
    ? `<div class="ref-line"><span class="ref-label">${label}</span>${escapeHtml(val)}</div>` : "";
  parts.push(line("证型", c.syndrome));
  parts.push(line("治法", c.treatment_principle));
  parts.push(line("方", c.formula));
  if (Array.isArray(c.herbs) && c.herbs.length) {
    parts.push(line("药", c.herbs.join("、")));
  }
  // 反药配对**只在真有的时候**才出现，并且原样显示后端给的那句话
  // （core/safety_output.INCOMPATIBLE_TRAINING_NOTE，跟训练样本里的逐字相同）。
  // 前端不自己拼这句话：拼了就是界面在替模型背书它没学过的话。
  if (Array.isArray(c.incompatible_pairs) && c.incompatible_pairs.length) {
    parts.push(`<div class="ref-incompat">`
      + `<span class="ref-pairs">${escapeHtml(c.incompatible_pairs.join("、"))}</span>`
      + `<span class="ref-note">${escapeHtml(c.note || "")}</span></div>`);
  }
  return `<div class="ref-case">${parts.join("")}</div>`;
}

function referenceBlockHtml(pid, data) {
  const meta = (data && data.physician) || {};
  const name = meta.name || physicianName(pid);
  // 身份色走 CSS 变量，不把 /api 返回的色值直接写进 style——变量由
  // injectPhysicianColors 从同一份注册表注入，两条路会漂。
  const head = `<div class="ref-head" style="border-left-color: var(--phys-${pid})">`
    + `<span class="ref-name">${escapeHtml(name)}</span>`
    + `<span class="ref-tag">参考</span></div>`;
  // 三种"空"分开说，跟后端 search_cases 的三分法一一对应（SOURCES.md 第 31 条）：
  // 参数错 / 数据文件不存在 / 真没匹配。一律显示"没有结果"会把三件事混成一件。
  if (data && data.error) {
    return `<div class="ref-phys">${head}<div class="ref-empty ref-error">${escapeHtml(data.error)}</div></div>`;
  }
  if (data && data.available === false) {
    return `<div class="ref-phys">${head}<div class="ref-empty">语料未就绪：${escapeHtml(data.note || "")}</div></div>`;
  }
  const cases = (data && data.cases) || [];
  if (!cases.length) {
    return `<div class="ref-phys">${head}<div class="ref-empty">${escapeHtml((data && data.note) || "没有相似度达标的医案")}</div></div>`;
  }
  return `<div class="ref-phys">${head}${cases.map(referenceCaseHtml).join("")}</div>`;
}

function hideReferencePhysicians() {
  const box = document.getElementById("reference-physicians");
  if (!box) return;
  box.hidden = true;
  const body = document.getElementById("reference-body");
  if (body) body.innerHTML = "";
}

// 拉取并渲染。**独立于 /api/consult**：集注是分钟级的 LLM 调用，检索是毫秒级的，
// 合在一起会让这一栏跟着三列一起等。
async function renderReferencePhysicians(complaint, role) {
  const box = document.getElementById("reference-physicians");
  const body = document.getElementById("reference-body");
  if (!box || !body) return;
  if (!REFERENCE_ROLES.includes(role) || !REFERENCE_PHYSICIANS.length || !complaint) {
    hideReferencePhysicians();
    return;
  }
  box.hidden = false;
  body.innerHTML = `<div class="ref-empty">正在检索参考医家的医案…</div>`;
  const blocks = [];
  for (const pid of REFERENCE_PHYSICIANS) {
    let data = null;
    try {
      const resp = await fetch("/api/reference_cases?complaint="
        + encodeURIComponent(complaint) + "&physician=" + encodeURIComponent(pid));
      data = resp.ok ? await resp.json() : { error: `接口返回 ${resp.status}` };
    } catch (e) {
      // 网络抛了要说出来：静默留空会让人以为这几位医家没有相似医案。
      data = { error: "检索请求失败（服务未连接？）" };
    }
    blocks.push(referenceBlockHtml(pid, data));
  }
  body.innerHTML = blocks.join("");
}

function renderColumns(results, mode = "researcher") {
  document.getElementById("columns").innerHTML = results.map((r) => columnHtml(r, mode)).join("");
  // R41：窗口化列表要在 HTML 落进 DOM 之后挂数据与滚动（`columnHtml` 是纯字符串
  // 拼接，数据塞不进字符串里）。顺序跟 `results` 一致——`.virtual-list` 出现的
  // 顺序就是列的顺序，第 i 个列表拿第 i 列的 refs。
  // 条数没超阈值时页面里一个 `.virtual-list` 都没有，这一行是空转。
  mountVirtualLists(document.getElementById("columns"),
                    results.map((r) => r.refs || []));
}

// role-select 的当前取值。跟 retriever-mode 一样"逐请求参数、不落进程状态"，
// 这里只是读 DOM 当前值，不额外维护一份全局变量去跟 DOM 手动同步。
function getSelectedRole() {
  const sel = document.getElementById("role-select");
  return sel ? sel.value : "researcher";
}

// 被拦截/信息不足的请求也要把上一次正常问诊留下的东西清掉：残差提示、页脚、
// 证据索引、上一张图。否则「重播」会重绘上一位患者的图。
function resetSecondaryPanels() {
  lastGraph = null;
  if (typeof updateGraphToolbar === "function") updateGraphToolbar();
  EVIDENCE = {};
  DOCTOR_STATE = {};
  closeEvidence();
  hideTooltip();
  for (const id of ["residual-note", "graph-note", "followup-note"]) {
    const el = document.getElementById(id);
    if (el) { el.textContent = ""; el.style.display = "none"; }
  }
  renderSafetyFlag(null);
  renderTriage(null);
  // 参考医案也归零：它是上一条主诉的检索结果，留着会跟新主诉的三列并排显示，
  // 看起来像是这一次也检索出了这几条。
  hideReferencePhysicians();
  const footer = document.getElementById("manifest-footer");
  if (footer) footer.textContent = "";
  // R14：对照带和分层读数也是"上一次问诊留下的东西"。不清的话，被拦截的
  // 那一次页面上还挂着上一位患者的用药分歧——比留一张旧图更容易被误读成
  // "这次的结果"。
  const rx = document.getElementById("rx-compare");
  if (rx) { rx.innerHTML = ""; rx.classList.remove("show"); }
  renderDivergence(null);
}

// G3 追问记录。HTTP 路径上现在没有提问渠道（api_consult 不传 ask_fn），所以正常情况
// 下 followup.stopped_by 是 no_answer、什么都不显示；一旦接上 SSE/表单式追问，
// 这里就会把问答和否认的症状渲染出来。
function renderFollowup(followup) {
  let el = document.getElementById("followup-note");
  if (!el) {
    el = document.createElement("div");
    el.id = "followup-note";
    // 原来挂的是 residual-note，而全文件只有 #residual-note 这个 id 选择器，
    // 同名的类选择器从来不存在——这个框一直没有任何样式。
    el.className = "followup-note";
    const detail = document.getElementById("detail-body");
    if (detail) detail.insertBefore(el, detail.firstChild);
  }
  if (!followup || !followup.history || followup.history.length === 0) {
    el.style.display = "none";
    return;
  }
  const qa = followup.history.map((h) =>
    `<div>问：${escapeHtml(h.question)}　答：${escapeHtml(h.answer)}${h.safety_hit ? "　<b>⚠ 触发安全否决</b>" : ""}</div>`).join("");
  const denied = (followup.denied || []).length ? `<div>患者明确否认：${escapeHtml(followup.denied.join("、"))}</div>` : "";
  el.innerHTML = `<div><b>追问（${followup.rounds} 轮，${escapeHtml(followup.stopped_by)}）</b></div>${qa}${denied}`;
  el.style.display = "block";
}

function showError(message) {
  const box = document.getElementById("error-box");
  box.textContent = message;
  box.classList.add("show");
}

// R56 §6：危重症状按角色分流。patient 角色命中即整页拦截（不会走到这个
// 函数——`showSafetyBlock` 那条分支），doctor/student/researcher 命中之后
// 拿完整推理，`flag` 非空就是这件事的唯一信号（见 core.safety.
// role_sees_full_reasoning_on_red_flag 的文档字符串：api/main.py 现在按角色
// 显式传 eval_mode，服务端全局 EVAL_MODE 不再能通过 HTTP 接口影响这个字段，
// 所以这里不用再区分"服务端配置"还是"角色策略"——走到这条 HTTP 接口，
// 非 patient 角色下 flag 非空恒等于角色策略在生效）。
function describeSafetyFlag(flag) {
  if (!flag) return null;
  return `⚠ 危重信号提示（${flag}）：这条主诉命中了危重症状信号，`
    + `以下推理结果按角色权限完整给出，不做删减。请结合临床实际判断，`
    + `必要时提醒患者立即就医或转诊/急诊处理。`;
}

function renderSafetyFlag(flag) {
  const box = document.getElementById("safety-flag-note");
  if (!box) return;
  const text = describeSafetyFlag(flag);
  box.textContent = text || "";
  box.classList.toggle("show", !!text);
  // 方剂区水印：有危重信号时，处方表格本身要带着这个提示，不能只在页面顶部
  // 出现一条横幅——医师滚动到处方区域时横幅可能已经不在视口里了。
  document.body.classList.toggle("red-flag-active", !!text);
}

function clearError() {
  const box = document.getElementById("error-box");
  box.classList.remove("show");
  box.textContent = "";
}

// data 的形状是 api/main.py::_consult_response() 的返回值——不管是走
// /api/consult 的普通响应体，还是走 /api/consult/stream 的 done 事件，
// 两条路径共用同一个后端函数拼出这份 JSON，这里也只用一份渲染逻辑接。
// 检索模式跟着请求走：空值表示"用服务端默认"，这时候整个字段都不带，
// 而不是传一个空字符串（空字符串不在 ALLOWED_MODES 里，会被当成非法模式名）。
function buildConsultRequestBody(complaint) {
  const body = { complaint };
  const sel = document.getElementById("retriever-mode");
  const mode = sel ? sel.value : "";
  if (mode) body.retriever_mode = mode;
  // M7：role 跟 retriever_mode 一样逐请求带上，不是进程级设置。这里始终显式
  // 传（不像 retriever_mode 那样"空值就不传这个字段"），因为 role-select
  // 永远有一个合法选中值（默认 researcher），没有"不传等于用服务端默认"这种
  // 需要区分的场景。
  body.role = getSelectedRole();
  // R46：结构化四诊与「人」维**只在填了的时候带**。带一份全空的表单会让
  // 后端把"没填"当成"填了但都是空的"，而这两者在个体化那一层是不同的结论
  // （见 core/individualize.py 对 `profile.is_empty()` 的处理）。
  const intake = collectIntake();
  if (Object.keys(intake).length) body.intake = intake;
  const profile = collectProfile();
  if (profile) body.patient_profile = profile;
  return body;
}

function renderConsultResult(data) {
  resetSecondaryPanels();
  hidePatientView();
  hideChainFlow();
  if (data.rejected) {
    // 安全拦截：**整页替换**（§3.1）。不是在三列上面加个红横幅——总纲的原话是
    // "什么都标红会训练用户忽略标红"，这个系统只在真危重时打断，所以打断必须
    // 彻底：三列清空、图不画、对照带不画，页面上不留任何可以被误读成"结论"的东西。
    showSafetyBlock(data.reject_reason);
    renderDivergence(null);
    renderFollowup(data.followup);
    return;
  }
  if (data.retrieval_error) {
    // 选的检索模式这台机器上跑不起来。这是服务端能力问题，不是"你给的信息不够"，
    // 所以跟 insufficient 分开显示；后端也没有偷偷换个模式跑完再假装成功，
    // 这里如实把原因显示出来（graph 模式的静默降级会让 E8 消融失去意义）。
    showError(`⚠ ${data.retrieval_error}`);
    renderDivergence(null);
    clearColumns();
    renderFollowup(data.followup);
    renderGraph(data.graph);
    return;
  }
  if (data.insufficient) {
    // 信息不足不是错误：系统承认没法有依据地辨证。§3.1 要求"三列位置显示一句话
    // + 建议补充的信息，不留空白"——留空白的话页面看起来像坏了，而它其实是
    // 好好地拒绝了。理由原样来自后端 insufficient_reason，不在前端编一句。
    setConsultState("insufficient");
    const reason = data.insufficient_reason || "";
    renderColumnsPlaceholder((pid) => columnInsufficientHtml(pid, reason));
    renderDivergence(null);
    renderFollowup(data.followup);
    renderGraph(data.graph);
    return;
  }
  renderSafetyFlag(data.safety_flag);
  // R47：产品面的三件——依据强度（替代用药对照带）、本次记录编号、空结果。
  // 三个函数各自判断该不该显示，这里不加 if：判据散到调用点就是十六处
  // 零散判断的开头。
  renderEvidenceStrength(data);
  renderRecordId(data);
  renderEmptyResult(data);
  // R46：循证对照、个体化调整、病历文书草稿。三块都各自判断该不该显示。
  // `LAST_RESULT` 给病历文书用——它是"把这一次的结果排成一份文书"，
  // 所以要拿得到这一次的结果。
  LAST_RESULT = data;
  renderGuideline(data.guideline);
  renderIndividualization(data.individualization);
  // R46 §7.4：医师模式下把病历文书草稿也生成出来。**这一趟不调模型**
  // （把已经算好的结果排版而已），所以可以跟结果一起出来，不用让医师再点一次
  // ——"生成病历"如果要额外一次点击，它就还是一件额外的事，而这一层的全部
  // 意义正是"病历书写不该再是一件额外的事"。
  if (getSelectedRole() === "doctor" && (data.results || []).length) {
    buildEmrFromLastResult(data).then((out) => { if (out) renderEMR(out.emr); })
      .catch(() => { /* 生成失败不影响结果页；按钮上还能再试一次 */ });
  } else {
    renderEMR(null);
  }
  PHYSICIAN_COLORS = {};
  PHYSICIAN_NAMES = {};
  for (const r of data.results) {
    if (r.color) PHYSICIAN_COLORS[r.physician] = r.color;
    PHYSICIAN_NAMES[r.physician] = r.physician_name;
  }
  // 每次问诊都刷新一次：页面加载时那次 /health 可能失败，而这一条是必须出现的
  renderDemoMode(data.demo_mode);
  const m = data.manifest;
  lastManifest = m || null;
  renderTokenPanel(m, lastUsage);
  document.getElementById("manifest-footer").textContent = m
    ? `${m.model}　prompt ${m.prompt_version}　语料 ${m.cases_sha256 || "?"}　${m.llm_calls} 次调用　${(m.elapsed_ms / 1000).toFixed(1)}s`
    : "";
  buildEvidenceIndex(data);
  const res = data.residual;
  const rnote = document.getElementById("residual-note");
  if (rnote) {
    if (res && res.triggered) {
      const before = Math.round(res.coverage_before * 100);
      const after = Math.round(res.coverage_after * 100);
      const gained = (res.newly_explained || []).join("、");
      rnote.innerHTML = `残差辨证：初轮解释率 ${before}% → ${after}%` +
        (gained ? `，补充解释「${escapeHtml(gained)}」` : "") +
        (res.still_unexplained && res.still_unexplained.length
          ? `；仍未解释「${escapeHtml(res.still_unexplained.join("、"))}」` : "");
      rnote.style.display = "block";
    } else {
      rnote.style.display = "none";
    }
  }
  lastGraph = data.graph;
  updateGraphToolbar();
  const dropped = data.graph.dropped_edges || 0;
  const note = document.getElementById("graph-note");
  if (dropped > 0) {
    note.textContent = `有 ${dropped} 条推理连线因症状表述不一致未能显示`;
    note.style.display = "block";
  } else {
    note.style.display = "none";
  }
  renderDivergence(data.divergence);
  renderRxCompare(data.divergence, data.results);
  renderTriage(data);
  // M8：医生模式的可编辑处方状态要在渲染卡片之前建好——cardHtml() 里的
  // doctorSectionHtml() 读的是 DOCTOR_STATE，不是 data.results 本身
  // （医生编辑的是自己的一份拷贝，不直接改问诊响应）。
  initDoctorState(data.results, data.safety_flag);
  setConsultState("done");
  // R15：患者模式是**另一种形态**，不是三列的裁剪版（§3.5）。两块互斥——
  // 不是把三列渲染出来再用 CSS 藏起来：藏起来的东西仍然在 DOM 里，而 patient
  // 的字段裁剪是一条安全边界（api/main.py::_filter_s3_for_role），
  // 前端再把裁剪过的空壳摆出来只会让人以为"这次没开出方"。
  if (getSelectedRole() === "patient") {
    document.getElementById("columns").innerHTML = "";
    hideChainFlow();
    renderPatientView(data);
  } else if (isSingleChainResult(data)) {
    // R37：结构化模式 = 单链九段。**patient 角色仍然走患者视图**（那是另一种
    // 形态，不是这条链的裁剪版），所以这个分支在它后面。
    hidePatientView();
    renderChainFlow(data);
  } else {
    hidePatientView();
    hideChainFlow();
    renderColumns(data.results, getSelectedRole());
  }
  // R18-I：参考医家那一栏不等三列、也不阻塞后面的渲染——它自己 fetch，
  // 回来了再填。await 它会让图谱和追问提示跟着一次检索的往返延迟。
  renderReferencePhysicians(LAST_COMPLAINT, getSelectedRole());
  renderFollowup(data.followup);
  renderGraph(data.graph);
}

// ---------- SSE 分步进度 ----------

function appendProgress(line) {
  const log = document.getElementById("progress-log");
  log.classList.add("show");
  log.textContent += (log.textContent ? "\n" : "") + line;
  log.scrollTop = log.scrollHeight;
}

function clearProgress() {
  const log = document.getElementById("progress-log");
  log.textContent = "";
  log.classList.remove("show");
  clearS3Stream();
}

// ---------- R36：S3 流式增量 ----------
//
// 服务端已经把增量合并过了（core/chain.py 的 S3DeltaEmitter，80 字或 120ms 一帧），
// 所以这里**不需要再攒一层**；要做的是限制重排频率。
//
// **R37 把节流从 requestAnimationFrame（≈16ms）改成 ≥50ms 的时间闸。** rAF 的
// 节奏跟屏幕刷新率绑，60Hz 下是 16ms、120Hz 的屏上是 8ms——这个区域的内容是
// 模型吐的 JSON，人眼在 50ms 和 8ms 之间分辨不出差别，而每次重排都要把这一大段
// 文本重新排版。50ms（20 次/秒）是"看起来连续"的下限，再快只是白烧 CPU；
// 而 1280×800 的投影仪那台机器上，白烧的那部分会让整页跟着掉帧。
const S3_STREAM_RENDER_MS = 50;
const s3Stream = { text: new Map(), pending: false, order: [], lastRender: 0 };

function clearS3Stream() {
  s3Stream.text.clear();
  s3Stream.order.length = 0;
  s3Stream.lastRender = 0;
  const box = document.getElementById("s3-stream");
  if (box) { box.textContent = ""; box.classList.remove("show"); }
}

function onS3Delta(data) {
  const who = data.physician_name || data.physician || "";
  const kind = data.kind === "reasoning" ? "reasoning" : "content";
  const key = `${data.physician || ""}:${kind}`;
  if (!s3Stream.text.has(key)) {
    s3Stream.order.push({ key, who, kind });
    s3Stream.text.set(key, "");
  }
  s3Stream.text.set(key, s3Stream.text.get(key) + (data.text || ""));
  if (s3Stream.pending) return;
  s3Stream.pending = true;
  const since = Date.now() - (s3Stream.lastRender || 0);
  // 距上次渲染够久就立刻画（第一帧不该等 50ms），否则排到 50ms 边界上。
  const wait = since >= S3_STREAM_RENDER_MS ? 0 : S3_STREAM_RENDER_MS - since;
  setTimeout(renderS3Stream, wait);
}

function renderS3Stream() {
  s3Stream.pending = false;
  s3Stream.lastRender = Date.now();
  const box = document.getElementById("s3-stream");
  if (!box) return;
  box.classList.add("show");
  // **只留尾部 1200 字**：模型吐的是几千字的 JSON，全留着 DOM 越来越大而人
  // 只看得见最后几行。截断这件事要让人看出来（前面加省略号），不能悄悄丢。
  box.textContent = "";
  for (const { key, who, kind } of s3Stream.order) {
    const full = s3Stream.text.get(key) || "";
    const tail = full.length > 1200 ? "…" + full.slice(-1200) : full;
    const head = document.createElement("span");
    head.className = "s3-who";
    head.textContent = `${who}${kind === "reasoning" ? "（思考）" : ""}：`;
    const body = document.createElement("span");
    if (kind === "reasoning") body.className = "s3-reasoning";
    // textContent 而不是 innerHTML：这段文本直接来自模型输出
    body.textContent = tail + "\n";
    box.appendChild(head);
    box.appendChild(body);
  }
  box.scrollTop = box.scrollHeight;
}

let currentStreamId = null;

// R14 §3.1 追问状态：**问题弹在提问那位医家的列里，其余两列显示"等待中"**。
//
// 谁在问由 need_input 事件的 physician 字段说了算（后端 core/chain.py 的
// ContextVar → api/main.py::_ConsultStream.ask）。physician 为空 = 全局追问
// （run_followup 在三位医家之前跑，不属于任何一列），回落到输入区那个问答框
// ——**不是随便挑一列塞进去**：挑一列会让人以为是那位医家在问。
function showNeedInput(question, physician) {
  if (physician && PHYSICIAN_META[physician]) {
    setConsultState("followup");
    renderColumnsPlaceholder((pid) => pid === physician
      ? columnFollowupHtml(pid, question)
      : columnWaitingHtml(pid));
    bindColumnAsk();
    return;
  }
  document.getElementById("need-input-question").textContent = `❓ ${question}`;
  const input = document.getElementById("need-input-answer");
  input.value = "";
  document.getElementById("need-input-box").classList.add("show");
  input.focus();
}

// 列内问答框的事件在每次重渲染之后重新绑：这几个节点是 innerHTML 生成的，
// 上一批节点连同它们的监听器一起被丢掉了。答案照旧走同一个提交函数
// （submitNeedInputAnswer 读的是那个隐藏输入框），不另开一条提交路径——
// 两条路径迟早在"流已经结束了还能不能提交"这种细节上分叉。
function bindColumnAsk() {
  const box = document.querySelector(".col-ask");
  if (!box) return;
  const input = box.querySelector(".ask-input");
  const submit = () => {
    document.getElementById("need-input-answer").value = input.value;
    submitNeedInputAnswer();
  };
  box.querySelector(".ask-submit").addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  input.focus();
}

function hideNeedInput() {
  document.getElementById("need-input-box").classList.remove("show");
}

async function submitNeedInputAnswer() {
  const input = document.getElementById("need-input-answer");
  const answer = input.value.trim();
  // 没有 currentStreamId 说明流已经结束（比如用户手滑点了两下）——这时候
  // 提交答案没有地方能接住，后端会回 404，不如干脆不发这个请求。
  if (!answer || !currentStreamId) return;
  hideNeedInput();
  try {
    const resp = await fetch(`/api/consult/stream/${currentStreamId}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`回答提交失败（HTTP ${resp.status}）：${text}`);
    }
    // 不在这里手动往 progress-log 加"已回答"——服务端会在真正收到、真正
    // 让 consult() 里的 ask_fn 返回之后回发一条 followup_answered 事件，
    // 那条才是唯一真相；这里手动加一条的话，等那条事件到达时就会重复一遍。
  } catch (err) {
    showError(`回答提交失败：${err.message || err}`);
  }
}

// 把一条 SSE 进度事件翻成人看得懂的一行日志。事件名和数据形状是
// core/chain.py::consult() 的 on_step 回调定义的契约——那边新增一种事件，
// 这里要跟着加一个 case，不加也不会报错，只是进度条上少一行，不算严重故障，
// 所以没有钉一条"事件名必须穷举"的断言，交给人工核对事件名对不对得上。
// 事件 → 该把哪一列推到哪一步。纯函数：**这张表是"事件怎么映射成进度"的
// 唯一定义**，散在 switch 里的话，加一种事件时三列的进度和左下角的日志会
// 各更新各的、然后对不上。physician 为 null 表示三列一起走（S1/S2 是全局
// 跑一次、三列共用的，CLAUDE.md 明令 S1 只能跑一次）。
function columnStepForEvent(name, data) {
  switch (name) {
    case "s1_done":
      return { physician: null, step: "s2" };
    case "s2_done":
      return { physician: null, step: "s3" };
    case "physician_start":
    case "react_step":
    case "s3_start":
    // R36：增量与 s3 结束都仍然属于"这一列在跑 S3"这一步——
    // 漏了它们的话，流式期间那一列的进度指示会退回上一步。
    case "s3_delta":
    case "s3_done":
      return { physician: data.physician, step: "s3" };
    default:
      return null;
  }
}

function describeProgressEvent(name, data) {
  switch (name) {
    case "s1_done":
      return `① 症状标准化：${(data.symptoms || []).join("、")}`;
    case "s2_done":
      return `② 证素推断${data.after_followup ? "（追问后重跑）" : ""}：` +
        ((data.elements || []).map((e) => e.element).join("、") || "（未推出证素）");
    case "followup_done":
      if (data.stopped_by === "no_answer") return "③ 追问：无提问渠道，跳过";
      if (data.stopped_by === "fast_mode") return "③ 追问：FAST_MODE 已跳过";
      return `③ 追问结束（${data.rounds} 轮，`
        + `${data.stopped_by_label || data.stopped_by}）`;
    case "residual_done":
      return `④ 残差辨证：补充解释「${(data.newly_explained || []).join("、") || "无"}」`;
    case "physician_start":
      return `▶ ${data.physician_name} 开始辨证…`;
    case "react_step":
      return `　${data.physician_name} 取证第 ${data.step} 步：${data.action}`;
    case "s3_start":
      return `　${data.physician_name} 正在拟定证型与方药…`;
    case "s3_done": {
      // R56 §6 第 3 条：「N 帧流式 / 首字 Xs / 思考 N 字」是给排查问题看的
      // 底层遥测（流式帧数、思考 token 字数这类实现细节），医师读不出信息、
      // 只会觉得系统在讲他听不懂的话。产品面只报"完成"这件事本身；
      // 研究模式保留完整数字（调优、排查卡顿就靠它）。
      if (!isProductMode()) {
        if (!data.events) {
          return `　${data.physician_name} 输出完成（无增量${data.streaming_note ? "：" + data.streaming_note : ""}）`;
        }
        const first = data.first_delta_s == null ? "—" : `${data.first_delta_s}s`;
        return `　${data.physician_name} 输出完成：${data.events} 帧流式，首字 ${first}，` +
          `正文 ${data.chars_content} 字${data.chars_reasoning ? `，思考 ${data.chars_reasoning} 字` : ""}`;
      }
      return `　${data.physician_name} 输出完成`;
    }
    case "physician_done":
      return `✓ ${data.physician_name} 完成：${data.syndrome}`;
    case "followup_answered":
      return `　已回答「${data.question}」：${data.answer}`;
    // R55：这三个事件之前后端发了、这里一个 case 都没有——不是"少一行日志"
    // 这么轻，是⑨（校验与出处）那一段在这段时间里完全没有任何可见变化，
    // 用户会把"S3 早就输出完了、还在走验证/佐证"读成"卡住了"（见本轮报告）。
    case "verify_revise":
      return `　⑨ 校验：第 ${data.round} 轮核对未通过（${data.rules.join("、")}），正在按反例重开…`;
    // R62 §11.3：`s3_done` 之后还有核查与佐证两段，此前这两段一个事件都不发。
    // 这里补上的是**边界**（开始/每轮结论/结束），不是把 verify_revise 说第二遍：
    // verify_revise 只在"要回炉重开"时发，而第一轮就通过的那条路径此前
    // 在界面上完全看不见，恰恰是最常见的那条。
    case "s3_draft":
      // 内容事件，不是进度事件：五步链已经推出来了，但**还没过符号验证**。
      // 这一版旧界面按 `done` 一次性渲染（R62 的三栏新界面才用它提前渲染
      // ②–⑥），所以这里只记一行——但必须有这一行：它标出"推导结束、核查
      // 开始"这个时刻，没有它，s3_done 到 verify_start 之间看起来像空白。
      return "　推导完成，方已拟出（尚未通过符号核查）";
    case "verify_start":
      return "　⑨ 校验：开始逐条核对方药与医理…";
    case "verify_round":
      return `　⑨ 校验：第 ${data.round} 轮 —— ` +
        (data.n_veto || data.n_revise
          ? `${data.n_veto} 条须改、${data.n_revise} 条待商榷（${(data.rules || []).join("、")}）`
          : `${(data.checked_rules || []).length} 条规则全部通过`);
    case "verify_done":
      return `　⑨ 校验完成：${data.first_pass ? "一次通过" : `改了 ${data.revise_calls} 轮`}`;
    case "corroborate_start":
      return "　⑩ 医案对照：检索历史上有没有这么治过…";
    case "corroborate_done":
      if (data.enabled === false) return "　⑩ 医案对照：本次未启用";
      return `　⑩ 医案对照：方向一致 ${data.n_concordant} 条、不一致 ${data.n_divergent} 条`;
    case "early_veto":
      return `　⚠ 初步提示：${data.reason || data.rule_label}（基于尚未输出完的药味清单，最终以完整校验为准）`;
    case "agent_step":
      return `　${data.capability_label}：${data.why}${data.detail ? "——" + data.detail : ""}`;
    default:
      return null; // stream_id / need_input / done / error / heartbeat 各自单独处理，不进日志
  }
}

// 手写 SSE 帧解析而不是用浏览器原生 EventSource：EventSource 只能发 GET，
// 这里要在打开流的同一个请求里带上 complaint（POST body），只能自己用
// fetch + ReadableStream 读。SSE 帧格式是"若干 field: value 行 + 一个空行"，
// 这里只用到 event/data 两个字段。
async function readSSE(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let eventName = "message";
      const dataLines = [];
      for (const line of frame.split("\n")) {
        // 冒号后的空格按 SSE 规范是可选的，只认带空格的写法太脆。
        if (line.startsWith("event:")) eventName = line.slice(6).replace(/^ /, "");
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
      }
      if (dataLines.length) {
        // E2：一条畸形事件不该毁掉整次问诊。原来 JSON.parse 抛出来会一路冒泡到
        // submitConsult 的 catch，已经跑完的推理全部作废、钱白花。
        let payload;
        try {
          payload = JSON.parse(dataLines.join("\n"));
        } catch (parseErr) {
          console.error("SSE 帧解析失败，已跳过这一帧：", frame, parseErr);
          continue;
        }
        onEvent(eventName, payload);
      }
    }
  }
}

// ---------- E1：取消、空闲超时、已用时 ----------
//
// 一次问诊在 v4-pro + ReAct 下是分钟级，而后端 R9 刚加的墙钟（API 后端 180 秒
// ×3 次重试）前端一点都没接上：原来点下"辨证"就只能等，流要是卡住（段 6 那种
// 挂死在演示现场就是这个表现）唯一的出路是刷新整个页面。
//
// 三件事：
//   1. AbortController，取消按钮和空闲看门狗共用同一个
//   2. 看门狗按"距上一条事件多久"算，不是按总时长——ReAct 取证本来就慢，
//      按总时长会把正常的长问诊误杀
//   3. #status-text 原来是个永远为空的 span（只被写过空字符串），改成显示
//      已用时。"静默很久是正常的"这件事，有个走字的秒数比一句文档管用
let currentAbort = null;
let cancelledByUser = false;
const SSE_IDLE_TIMEOUT_MS = 300000;

// R55：s3_done 之后 120 秒还没等到 done 的兜底。**不是整体看门狗
// （SSE_IDLE_TIMEOUT_MS=300s）的替代**——那个按"距上一条事件多久"算，
// heartbeat 每 15 秒一条，只要连接还活着它就不会触发；这个专门针对
// "s3_done 已经发生、后面的验证/佐证/收尾阶段这条连接却再也没有任何一帧
// 新事件"这一种更窄的失败模式（真机上更常见：nginx/CDN 截断了 done 那一帧，
// 或者后端 worker 早就跑完但那一帧在网络上丢了），不用干等到 300 秒才有
// 反应。
const S3_DONE_FALLBACK_MS = 120000;
let s3DoneFallbackTimer = null;
let resolvedByFallback = false;

function clearS3DoneFallback() {
  if (s3DoneFallbackTimer) {
    clearTimeout(s3DoneFallbackTimer);
    s3DoneFallbackTimer = null;
  }
}

function armS3DoneFallback(streamId) {
  clearS3DoneFallback();
  if (!streamId) return; // 理论上到不了：stream_id 事件总在 s3_done 之前到
  s3DoneFallbackTimer = setTimeout(() => fetchDoneFallback(streamId), S3_DONE_FALLBACK_MS);
}

async function fetchDoneFallback(streamId) {
  // 只是一次尝试，不是主流程——失败（还没跑完 / stream_id 已过期 / 网络
  // 也不通）就什么都不做，继续按原计划让主看门狗（300 秒）兜底，不额外报错。
  try {
    const resp = await fetch(`/api/consult/stream/${streamId}/result`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (currentStreamId !== streamId) return; // 这期间用户已经开始了下一次问诊
    resolvedByFallback = true;
    appendProgress("　长时间没有收到最终结果，已通过兜底接口直接取回（本次结果不受影响）");
    if (currentAbort) currentAbort.abort();
    renderConsultResult(data);
  } catch {
    // 静默：见上面的注释。
  }
}

async function submitConsult() {
  const complaint = document.getElementById("complaint").value.trim();
  if (!complaint) {
    showError("请先输入患者主诉。");
    return;
  }

  const btn = document.getElementById("submit-btn");
  const cancelBtn = document.getElementById("cancel-btn");
  const status = document.getElementById("status-text");
  btn.disabled = true;
  btn.textContent = "推理中…";
  status.textContent = "";
  clearError();
  clearProgress();
  hideNeedInput();
  // R14：主诉从"输入框里的字"升格成页面正文（§3.1 布局图最上面那一段），
  // 输入区同时从居中放大收到底部。整页替换的拦截页如果还开着，先收起来。
  hideSafetyBlock();
  hidePatientView();
  LAST_COMPLAINT = complaint;
  renderComplaintBody(complaint);
  setConsultState("running");
  resetColumnProgress();
  resetChainChecksState();
  if (isSingleChain(null)) {
    // R37：structured 下**问诊一开始就摆九段骨架**。/health 已经告诉了前端
    // 这台服务的形状，所以不必先摆三列再当场换掉——那一下闪烁正是"界面在猜"。
    renderChainSkeleton("s1");
  } else {
    hideChainFlow();
    renderColumnsPlaceholder((pid) => columnRunningHtml(pid, "s1"));
  }
  currentStreamId = null;

  currentAbort = new AbortController();
  cancelledByUser = false;
  resolvedByFallback = false;
  let idleTimedOut = false;
  if (cancelBtn) cancelBtn.classList.remove("is-hidden");

  const t0 = Date.now();
  const ticker = setInterval(() => {
    // R56 §6 第 4 条：跟 #eta-note 同一个格式函数（formatElapsed），
    // 超过一分钟显示"X 分 Y 秒"，不是一直显示三位数的秒数。
    status.textContent = `已用时 ${formatElapsed(Date.now() - t0)}`;
  }, 1000);

  let idleTimer = null;
  const armWatchdog = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idleTimedOut = true;
      if (currentAbort) currentAbort.abort();
    }, SSE_IDLE_TIMEOUT_MS);
  };
  armWatchdog();

  try {
    const resp = await fetch("/api/consult/stream", {
      method: "POST",
      signal: currentAbort.signal,
      headers: authHeaders({ "Content-Type": "application/json" }),
      // 检索模式跟着这一次请求走，不去调什么"服务端设置"接口——服务端的
      // RETRIEVER_MODE 是进程级的，改它会影响同时在跑的别人的请求。
      // 空字符串 = 用默认，这时候不带这个字段（不是传 ""，那是个非法模式名）。
      body: JSON.stringify(buildConsultRequestBody(complaint)),
    });
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`服务返回错误（HTTP ${resp.status}）：${text}`);
    }

    let doneData = null;
    let errorDetail = null;
    await readSSE(resp, (name, data) => {
      armWatchdog(); // 每收到一条事件就把空闲计时归零
      // R55：s3_done 后 120 秒还没等到 done 的兜底（见 armS3DoneFallback 的
      // 文档）。放在这里、路由逻辑之前——不管下面按哪个分支处理这条事件，
      // 计时器的起止都不该漏。
      if (name === "s3_done") armS3DoneFallback(currentStreamId);
      if (name === "done" || name === "error") clearS3DoneFallback();
      if (name === "usage") { renderUsage(data); return; }
      if (name === "stream_id") {
        // 额度状态搭在第一帧里：降级到回放要在推理开始之前让人知道。
        if (data.usage) renderUsage(data.usage);
        currentStreamId = data.stream_id;
      } else if (name === "need_input") {
        showNeedInput(data.question, data.physician);
      } else if (name === "error") {
        errorDetail = data.detail;
      } else if (name === "done") {
        doneData = data;
      } else if (name === "verify_revise") {
        // R55：⑨（校验与出处）的状态**只**由这个事件（进行中）和 done（定稿）
        // 推进，不再跟④-⑧共用 reached==="s3" 这个粗桶——那正是"S3 早输出完、
        // 界面还显示推理中"这条真机 bug 的根因。
        chainChecksState = { phase: "verifying", round: data.round };
        if (isSingleChain(null)) renderChainSkeleton("s3");
        const line = describeProgressEvent(name, data);
        if (line) appendProgress(line);
      } else if (name === "s3_delta") {
        // R36：增量**不进日志**（几十帧会把日志顶得看不见），只刷流式区。
        onS3Delta(data);
      } else if (name === "deltas_dropped") {
        // R40 背压：后端因为 SSE 队列满丢了 N 条逐字增量。**要说出来**——
        // 打字机效果中间缺一段而界面一声不响，会被读成"模型就是这么写的"。
        // 终值不受影响（s3_done / done 兜底），所以这是一条说明，不是错误。
        appendProgress(`网络较慢，已跳过 ${data.n} 条逐字增量（最终结果不受影响）`);
      } else if (name === "heartbeat") {
        // 心跳的全部作用是"别把这条连接当空闲连接掐掉"（见 api/main.py
        // _HEARTBEAT_SECONDS）。armWatchdog() 上面已经调过了，这里什么都不做
        // ——写进日志会每 15 秒刷一行噪音。
      } else {
        const line = describeProgressEvent(name, data);
        if (line) appendProgress(line);
        // 三列各自的分步进度。**按 physician 字段路由，不按到达顺序推**——
        // R12 之后三位医家是并发跑的，叶天士的 done 完全可能排在张锡纯的
        // start 后面（docs/DESIGN.md §4.7 的订正）。
        const step = columnStepForEvent(name, data);
        if (step) {
          if (isSingleChain(null)) renderChainSkeleton(step.step);
          else setColumnStep(step.physician, step.step);
        }
        if (name === "followup_answered") hideNeedInput();
      }
    });

    hideNeedInput();
    if (errorDetail) throw new Error(errorDetail);
    if (!doneData) throw new Error("事件流意外中断，没有收到最终结果");
    renderConsultResult(doneData);
  } catch (err) {
    if (resolvedByFallback) {
      // R55：120 秒兜底已经拿到结果并渲染过了（fetchDoneFallback 里主动
      // abort 了这次 fetch 换来的就是这里的 AbortError）——这不是失败，
      // 不该再走下面任何一条错误提示。
      console.warn("consult stream 被兜底接口接管:", err);
    } else {
      console.error("consult failed:", err);
      if (cancelledByUser) {
        showError("已取消本次辨证。服务端可能还在跑完当前这一步，费用已经发生的部分不会退回。");
      } else if (idleTimedOut || (err && err.name === "AbortError")) {
        showError(
          `等待服务端新进度超过 ${Math.round(SSE_IDLE_TIMEOUT_MS / 1000)} 秒，已中断。`
          + "服务端可能仍在重试（API 后端墙钟 180 秒 × 3 次）——先看服务端日志，"
          + "不要先怀疑网络。"
        );
      } else {
        showError(`请求失败：${err.message || err}\n${(err.stack || "").split("\n").slice(0, 3).join("\n")}`);
      }
    }
  } finally {
    clearInterval(ticker);
    clearTimeout(idleTimer);
    clearS3DoneFallback();
    if (cancelBtn) cancelBtn.classList.add("is-hidden");
    currentAbort = null;
    cancelledByUser = false;
    resolvedByFallback = false;
    btn.disabled = false;
    btn.textContent = "辨证";
    status.textContent = "";
    currentStreamId = null;
    refreshUsage();
  }
}

initDemoModeBanner();
document.getElementById("submit-btn").addEventListener("click", submitConsult);
// 存之前先验一把：走服务端的 /api/usage/validate-key，它用 DeepSeek 官方的
// 「查询余额」接口（GET /user/balance）核，**零 token 消耗**。没有这一步的话，
// 填错 key 的人只能靠跑一次问诊才知道——而那一次可能已经走完 S1/S2 才失败。
document.getElementById("byok-save").addEventListener("click", async () => {
  const input = document.getElementById("byok-key");
  const key = (input.value || "").trim();
  const text = document.getElementById("usage-text");
  if (!key) { setByokKey(""); refreshUsage(); return; }
  text.textContent = "正在验证这把 key（查余额，不消耗 token）…";
  let res = null;
  try {
    const resp = await fetch("/api/usage/validate-key", {
      method: "POST",
      headers: { "X-LLM-Key": key },
    });
    res = await resp.json();
  } catch (e) {
    res = { valid: null, reason: `验证请求没发出去：${e.message || e}` };
  }
  if (res && res.valid === false) {
    // 无效就不存——存下去只会让下一次问诊在 S1 那里失败。
    text.textContent = `${res.reason} 没有保存这把 key。`;
    return;
  }
  setByokKey(key);
  input.value = "";
  if (res && res.valid === null) {
    // 验不了不等于 key 不对（可能是站点到 DeepSeek 的网络问题），如实说，照存。
    text.textContent = `key 已保存，但没能验证：${res.reason}`;
    return;
  }
  if (res && res.is_available === false) {
    text.textContent = `${res.reason} key 已保存，但现在调用会返回 402。`;
    return;
  }
  const b = (res && res.balances && res.balances[0]) || null;
  await refreshUsage();
  if (b) {
    document.getElementById("usage-text").textContent +=
      `　（key 可用，余额 ${b.total_balance} ${b.currency}）`;
  }
});
document.getElementById("byok-clear").addEventListener("click", () => {
  setByokKey("");
  document.getElementById("byok-key").value = "";
  refreshUsage();
});
document.getElementById("cancel-btn").addEventListener("click", () => {
  if (!currentAbort) return;
  cancelledByUser = true;
  currentAbort.abort();
});
document.getElementById("need-input-submit").addEventListener("click", submitNeedInputAnswer);
document.getElementById("need-input-answer").addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitNeedInputAnswer();
});
// 没有图时这三个按钮点了什么都不会发生（lastGraph 为 null），原来仍然是
// 可点的实心按钮，看起来像坏了。
// R55：`#graph-panel` 原来在没有图的时候也照常显示 540px 高的空画布 + 一整条
// 工具栏——展开「推理链·图谱·取证过程」那个 details 之后，问诊开始之前看到
// 的就是一个空白大框加三个按钮，会被当成"坏了"（见本轮报告 §5.2）。
// `.has-graph` 这个类才是真正的开关：CSS 那边（app.css）用它决定画布/图层
// 列头显示还是一段"还没有图"的说明，这里只负责按 `lastGraph` 切它——跟
// 下面禁用三个按钮同一处判据，不再另写一份"有没有图"的逻辑。
function updateGraphToolbar() {
  const panel = document.getElementById("graph-panel");
  if (panel) panel.classList.toggle("has-graph", !!lastGraph);
  for (const id of ["skip-btn", "replay-btn", "png-btn"]) {
    const b = document.getElementById(id);
    if (b) b.disabled = !lastGraph;
  }
}
document.getElementById("skip-btn").addEventListener("click", skipAnimation);
document.getElementById("replay-btn").addEventListener("click", replayGraph);
// R42：导出 PNG。文件名带当前主诉的前几个字——连导三张图在下载目录里
// 要能分清哪张是哪张（文件名的拼装在 graph.js 那一侧，这里只给"提示"）。
document.getElementById("png-btn").addEventListener("click", () => {
  TCM.exportGraphPng(null, (document.getElementById("complaint") || {}).value || "");
});
updateGraphToolbar();
// 保险起见运行时把面板挂到 body 末尾：只要祖先里有任何 transform/filter，
// position:fixed 就会相对该祖先定位而不是视口，面板会跑到页面中间去。
// R42：**释义面板也要**——它在窄屏（≤768px）下是底部抽屉（position: fixed），
// 挂在 main 里时实测贴不到屏底（差 8px，正好是祖先那一层的偏移）。
// 同一个理由、同一处修法，所以放在同一个块里。
for (const _id of ["evidence-panel", "node-explain"]) {
  const _p = document.getElementById(_id);
  if (_p && _p.parentElement !== document.body) document.body.appendChild(_p);
}
document.getElementById("evidence-close").addEventListener("click", closeEvidence);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeEvidence();
});
// R56 §6：吸顶侧栏要能用 Esc 关——键盘用户不该被逼着去找那个 × 按钮。
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  const el = document.getElementById("node-explain");
  if (el && el.classList.contains("show")) closeNodeExplain();
});


// ---------- 把宿主 UI 注册给 graph.js ----------
//
// **app.js 不往 window.TCM 上挂任何东西。** 依赖方向是单向的：app.js → graph.js。
// 图谱要用到问诊页的错误条和证据侧栏，走这三个钩子注入，而不是反过来直接调
// app.js 的全局函数——那样两个文件谁也不能单独被理解或替换（R13 拆分时留下的
// 循环依赖就是这个形状）。
setGraphHooks({
  onError: showError,
  onOpenEvidence: openEvidence,
  onCloseEvidence: closeEvidence,
  checkProductMode: isProductMode,
});


// ---------- R24：自绘下拉 ----------
//
// 放在最后：`enhanceAllSelects()` 要在两个下拉的 change 监听器都绑好之后再包，
// 顺序反了的话自绘层派发的第一次 change 会落在空气上。
// 图谱浏览器那两个下拉的选项是运行时填的，包一层不影响后填选项——
// 列表每次打开时现渲染（见 select.js 的 open()）。
enhanceAllSelects();

// R24：顶栏折叠。绑事件 + 按上次的选择恢复。
{
  const _tb = document.getElementById("topbar-toggle");
  if (_tb) _tb.addEventListener("click", toggleTopbar);
  applyTopbarCollapsed(topbarCollapsed());
}


// ============================================================================
// R47 产品化收口
// ============================================================================
//
// **能力不删，产品面不露。** 这一段不删任何既有渲染函数——它只决定"这一次
// 运行要不要把它们摆出来"，以及产品面用什么替代物。分派点在后端
// （core/product_mode.py → /health），这里是唯一的应用点。
//
// 为什么应用点只能有一个：要藏的东西有十六条，散着判断的话以后加第十七条
// 会漏掉其中几处，而漏掉的那几处恰恰是没人盯着的角落（顶栏折叠起来的那一块、
// 某个角色下才出现的面板）。跟 CLAUDE.md「同一概念的匹配逻辑只能有一处实现」
// 是同一条。

//: /health 下发的那一份。默认按产品模式算——拿不到 /health 时（后端不在）
//: 页面应该是正式版形态，而不是把内部面板露出来。
let PRODUCT_FLAGS = { product_mode: true, roles: null, default_role: null };

function isProductMode() {
  return document.documentElement.dataset.productMode !== "0";
}

function applyProductMode(health) {
  PRODUCT_FLAGS = {
    product_mode: health.product_mode !== false,
    roles: health.roles || null,
    default_role: health.default_role || null,
  };
  document.documentElement.dataset.productMode = PRODUCT_FLAGS.product_mode ? "1" : "0";
  const sel = document.getElementById("role-select");
  if (!sel) return;
  // **研究者这一档从 DOM 里摘掉，不是用 CSS 藏。** 一个被 CSS 藏起来的
  // `<option>` 仍然是 select 的 value：页面一加载就会以研究者身份去请求，
  // 而后端在产品模式下对这个角色回 404——界面看起来好好的，一点就报错。
  const allowed = PRODUCT_FLAGS.roles
    ? new Set(PRODUCT_FLAGS.roles.map((r) => r.id)) : null;
  if (allowed) {
    for (const opt of [...sel.options]) {
      if (opt.dataset.internalRole && !allowed.has(opt.value)) opt.remove();
    }
  }
  const want = PRODUCT_FLAGS.default_role;
  if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
  // 自绘下拉的按钮文字要跟着换：只改原生 select 的 value 的话，按钮上留着
  // 上一个选项的名字（R24 那层包装记的是自己渲染过的文本）。
  if (typeof refreshSelect === "function") refreshSelect(sel);
  updateDisclaimer();
  updateDoctorFieldsVisibility();
}

// ---------- §8.2 第 7 条：产品面的「本方的依据强度」 ----------
//
// 用药对照带与噪声地板是**研究指标**：它们回答"几家之间差多少"，而主治医师
// 要的是"这一条结论我能不能核对"。同一份数据，换一个问题来问。
//
// **一个字的研究词都不许出现**（ε、Jaccard、噪声地板、分歧度）：
// tests/test_no_demo_artifacts.py 扫源码钉这一条。

//: 药味可核对比例的三档。分档不是为了给一个分数，是为了让"要不要逐味核对"
//: 这个决定有个起点——高档可以抽查，低档必须逐味看。
const GROUNDED_STRONG = 0.8;
const GROUNDED_MEDIUM = 0.5;

function groundedLabel(ratio) {
  if (ratio >= GROUNDED_STRONG) return "强";
  if (ratio >= GROUNDED_MEDIUM) return "中";
  return "弱";
}

function evidenceStrengthHtml(data) {
  const r = (data.results || [])[0];
  if (!r) return "";
  const cited = new Set([...(r.physicians_cited || []),
                         ...(r.physician_influences || []).map((x) => x.physician)]);
  const nRefs = (r.refs || []).length;
  const ratio = r.herbs_grounded_ratio;
  const parts = [];
  if (ratio === null || ratio === undefined) {
    // **不编**：这一次没有算出这个比例（legacy 路径、参考医家引用区），
    // 就说没算，不拿 0 充数——0 在这里意味着"一味都没查到"，是另一回事。
    parts.push("本次未统计药味出处覆盖。");
  } else if (!KNOWLEDGE_BASE_AVAILABLE) {
    // 0 的两种含义必须分清（见 /health 的 knowledge_base 注释）
    parts.push("这台部署未装载本草与方剂本体，药味出处无从核对——"
      + "这不是「查不到出处」，是没有可查的底本。");
  } else {
    const pct = Math.round(ratio * 100);
    parts.push(`依据强度<b>${groundedLabel(ratio)}</b>：本方 ${pct}% 的药味`
      + "可在本草与方剂本体中查到性味归经与功效的原文出处。");
  }
  if (nRefs) parts.push(`另引医案原文 ${nRefs} 条`);
  if (cited.size) parts.push(`名老中医经验 ${cited.size} 家`);
  return parts.join("，").replace(/，$/, "") + "。逐条出处见下方推导链，点任一节点展开。";
}

//: /health 下发的本体可用性。默认 true——拿不到 /health 时不该反过来
//: 断言"本体不在"，那是把一个不知道说成了一个结论。
let KNOWLEDGE_BASE_AVAILABLE = true;

function renderEvidenceStrength(data) {
  const el = document.getElementById("evidence-strength");
  if (!el) return;
  const html = data && (data.results || []).length ? evidenceStrengthHtml(data) : "";
  el.innerHTML = html;
  el.classList.toggle("show", !!html);
}

// ---------- §8.4 第 31 条：页脚的版本与本次记录编号 ----------

function renderFooterVersion(health) {
  const el = document.getElementById("footer-version");
  if (!el) return;
  const name = health.product_name || "";
  const ver = health.version || "";
  el.textContent = name && ver ? `${name} ${ver}` : "";
}

function renderRecordId(data) {
  const el = document.getElementById("footer-record");
  if (!el) return;
  // §8.2 第 9 条：叫「本次记录编号」，不叫 trace_id，也不解释它是什么技术。
  el.textContent = data && data.record_id ? `本次记录编号 ${data.record_id}` : "";
}

// ---------- §8.4 第 28 条：空结果 ----------

function renderEmptyResult(data) {
  const el = document.getElementById("empty-result");
  if (!el) return;
  const s1 = data && data.s1;
  const unmapped = (s1 && s1.unmapped) || [];
  const symptoms = (s1 && s1.symptoms) || [];
  // 判据是"一个症状都没归类出来"，不是"结果为空"——后者还可能是被安全层
  // 拦下、或者信息不足，那两种各有各的界面，不该被这一块盖住。
  const show = !!s1 && symptoms.length === 0 && !data.rejected && !data.insufficient;
  if (!show) { el.classList.remove("show"); el.innerHTML = ""; return; }
  el.innerHTML = "<b>这段描述没有落进本系统的语料范围。</b>"
    + "<div>当前覆盖：内科杂病为主的教材证候与历代医案，"
    + "输入需要是症状与病史的<b>文字描述</b>（含舌象脉象的文字记录）。</div>"
    + (unmapped.length
        ? `<div>未能归类的表述：${escapeHtml(unmapped.join("、"))}</div>` : "")
    + "<div class=\"er-retry\">建议：按病历原文补充主诉、现病史与舌脉描述后重试；"
    + "若属外科、骨伤、儿科专科病证，本系统当前语料未覆盖。</div>";
  el.classList.add("show");
}

// ---------- §8.4 第 26 条：基于实测 p50 的预计剩余时间 ----------
//
// **为什么不写死一个秒数**：这台部署用的是哪个后端、哪块卡、开不开 ReAct，
// 决定了一次问诊是十几秒还是几分钟。写死的数在另一台机器上就是错的，
// 而一个错的倒计时比没有倒计时更伤信任。
//
// **为什么不取 R40 压测里的 p50**：那一组是 `--backend fake` 跑出来的，
// 量的是服务框架的开销，不是真实推理的历时。拿它当预计等于用一个不相干的
// 数冒充实测（「任何数字都必须带对照」那条铁律的反面用法）。
//
// 所以：**这台部署自己跑过的历时**，取中位数。样本不足时如实说没有。
const DURATION_KEY = "tcm.consultDurations";
const DURATION_KEEP = 20;
const DURATION_MIN_SAMPLES = 3;
let etaStartedAt = 0;
let etaTimer = null;

function readDurations() {
  try {
    const raw = JSON.parse(localStorage.getItem(DURATION_KEY) || "[]");
    return Array.isArray(raw) ? raw.filter((n) => typeof n === "number" && n > 0) : [];
  } catch (e) { return []; }
}

function recordDuration(ms) {
  if (!(ms > 0)) return;
  const next = [...readDurations(), ms].slice(-DURATION_KEEP);
  try { localStorage.setItem(DURATION_KEY, JSON.stringify(next)); } catch (e) { /* 隐私模式 */ }
}

function durationP50() {
  const xs = readDurations().slice().sort((a, b) => a - b);
  if (xs.length < DURATION_MIN_SAMPLES) return null;
  const mid = Math.floor(xs.length / 2);
  return xs.length % 2 ? xs[mid] : Math.round((xs[mid - 1] + xs[mid]) / 2);
}

// R56 §6 第 4 条：无历时样本时**不出现"样本"这个词**——「这台服务还没有
// 足够的历时样本（0/3）」读起来像在说"这台服务没人用过"，不是给患者/医师
// 看的产品文案。改成按 S3 推理档位给一个预计区间。
//
// **这两个数是占位的经验估计，不是真机实测**：R55 把 S3_REASONING_EFFORT_
// TOP3 降到 low、FULL_CONTEXT 降到 medium 之后，2026-09-17 那次真机三档
// 墙钟（264.6/276.3/208.9 秒）落在测量噪声内、顺序还乱——档位越高越久这个
// 排序本身都还没坐实，这里只给"大致要多久"的粗略量级，不假装是精确预测。
// 真实的按档位耗时表要等 R57/R58 在用户 AutoDL 上重新跑出来才能替换
// （scripts/compare_reasoning_tiers.py），到那时这两个数字要用实测值覆盖。
const ETA_PRESET_SECONDS_TOP3 = 45;
const ETA_PRESET_SECONDS_FULL_CONTEXT = 75;

function presetEtaSeconds() {
  const sel = document.getElementById("retriever-mode");
  const mode = (sel && sel.value) || "";
  // 空值或显式 "full_context" 都是 full_context 档（core.retrieval_hybrid.
  // DEFAULT_MODE），其余（dense/bm25/graph/hybrid）是 top3 系——跟
  // core.llm.s3_reasoning_effort() 判定档位的口径同一条，不在前端另写一份。
  return (mode === "" || mode === "full_context")
    ? ETA_PRESET_SECONDS_FULL_CONTEXT : ETA_PRESET_SECONDS_TOP3;
}

function formatElapsed(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
}

function etaText(elapsedMs) {
  const elapsed = formatElapsed(elapsedMs);
  const p50 = durationP50();
  if (p50 === null) {
    const left = Math.round(presetEtaSeconds() - elapsedMs / 1000);
    return left > 0
      ? `已用 ${elapsed}，预计还需约 ${left} 秒（按当前档位的经验估计）。`
      : `已用 ${elapsed}，复杂主诉会更久，可随时取消。`;
  }
  const left = Math.round((p50 - elapsedMs) / 1000);
  if (left > 0) {
    return `已用 ${elapsed}，预计还需约 ${left} 秒`
      + `（按本机近 ${readDurations().length} 次的中位数）。`;
  }
  return `已用 ${elapsed}，已超过本机中位数`
    + `（${Math.round(p50 / 1000)} 秒）；复杂主诉会更久，可随时取消。`;
}

function tickEta() {
  const el = document.getElementById("eta-note");
  if (!el || !etaStartedAt) return;
  el.textContent = etaText(Date.now() - etaStartedAt);
}

function startEta() {
  etaStartedAt = Date.now();
  tickEta();
  if (etaTimer) clearInterval(etaTimer);
  etaTimer = setInterval(tickEta, 1000);
}

function stopEta(completed) {
  if (etaTimer) { clearInterval(etaTimer); etaTimer = null; }
  if (completed && etaStartedAt) recordDuration(Date.now() - etaStartedAt);
  etaStartedAt = 0;
}

// ---------- §8.4 第 35 条：帮助入口 ----------
//
// 一处 DOM、一张文案表。每条是"一句话说明 + 一个例子"——没有例子的说明
// 等于把术语换了一个说法。
const HELP_TEXT = {
  complaint: {
    title: "该输入什么",
    body: "患者的主诉与四诊文字描述，越接近病历原文越好。",
    eg: "例：胃脘胀痛三月，食后加重，嗳气泛酸，纳差，舌淡红苔薄白，脉弦。",
  },
  "detail-zone": {
    title: "这一区是什么",
    body: "本次推导的完整过程：症状如何归类、证素如何推出、查了哪些医案与本草、"
      + "验证器改过什么。图上每个节点都可点开看它的依据原文。",
    eg: "例：点「肝郁气滞」可以看到它由哪几个症状支持、出自哪一条教材证候定义。",
  },
};

function hideHelp() {
  const pop = document.getElementById("help-pop");
  if (pop) pop.hidden = true;
}

function showHelp(btn) {
  const pop = document.getElementById("help-pop");
  const item = HELP_TEXT[btn.dataset.help];
  if (!pop || !item) return;
  pop.innerHTML = `<b>${escapeHtml(item.title)}</b><div>${escapeHtml(item.body)}</div>`
    + `<span class="hp-eg">${escapeHtml(item.eg)}</span>`;
  pop.hidden = false;
  // 定位在按钮下方，右侧不出屏。`pageYOffset` 而不是 `getBoundingClientRect`
  // 的裸值：气泡是 absolute（跟着文档走），滚动之后裸值会偏。
  const box = btn.getBoundingClientRect();
  const w = pop.getBoundingClientRect().width;
  const left = Math.max(8, Math.min(box.left + window.pageXOffset,
                                    document.documentElement.clientWidth - w - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${box.bottom + window.pageYOffset + 4}px`;
}

document.addEventListener("click", (evt) => {
  const btn = evt.target.closest && evt.target.closest(".help-btn");
  if (btn) {
    evt.preventDefault();
    // `<summary>` 里那个按钮：点它不该把折叠区一起展开/收起
    evt.stopPropagation();
    const pop = document.getElementById("help-pop");
    if (pop && !pop.hidden && pop.dataset.forKey === btn.dataset.help) { hideHelp(); return; }
    if (pop) pop.dataset.forKey = btn.dataset.help;
    showHelp(btn);
    return;
  }
  if (!(evt.target.closest && evt.target.closest("#help-pop"))) hideHelp();
});
document.addEventListener("keydown", (evt) => { if (evt.key === "Escape") hideHelp(); });

// ---------- §8.4 第 34 条：三步引导 ----------
//
// **只在首次进入时自动弹一次**。每次都弹的引导是最快被训练成"闭眼点掉"的
// 东西——弹第三次的时候没有人还在读它。看过就记在 localStorage，
// 页脚留一个「使用引导」随时可以再开。
const ONBOARDING_KEY = "tcm.onboardingSeen";

function onboardingSeen() {
  try { return localStorage.getItem(ONBOARDING_KEY) === "1"; } catch (e) { return true; }
}

function openOnboarding() {
  const el = document.getElementById("onboarding");
  if (el) el.hidden = false;
}

function closeOnboarding() {
  const el = document.getElementById("onboarding");
  if (el) el.hidden = true;
  try { localStorage.setItem(ONBOARDING_KEY, "1"); } catch (e) { /* 隐私模式 */ }
}

{
  const ob = document.getElementById("onboarding");
  if (ob) {
    for (const id of ["ob-skip", "ob-done"]) {
      const b = document.getElementById(id);
      if (b) b.addEventListener("click", closeOnboarding);
    }
    // 遮罩点击也关：一个只能靠按钮关的对话框在平板上很容易变成死路
    ob.addEventListener("click", (e) => { if (e.target === ob) closeOnboarding(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !ob.hidden) closeOnboarding();
    });
    if (!onboardingSeen()) openOnboarding();
  }
  const g = document.getElementById("guide-open-btn");
  if (g) g.addEventListener("click", openOnboarding);
}


// ============================================================================
// R46 临床工作流闭环：采集 → 诊断 → 方案 → 检索 → 管理
// ============================================================================

// ---------- §7.1 结构化四诊录入 ----------
//
// **字段表由后端下发**（`/api/intake/form`），前端不写死。写死的话
// core/intake.py 加一个字段，表单上不会长出来——跟示例主诉、身份色、
// 九层层名是同一条（"写死的常量也算一处实现"）。

let INTAKE_FIELDS = null;

const LIFE_STAGES = ["", "婴幼儿", "儿童", "青少年", "成人", "老年", "妊娠期", "哺乳期"];
const CONSTITUTIONS = ["", "平和质", "气虚质", "阳虚质", "阴虚质", "痰湿质",
                       "湿热质", "血瘀质", "气郁质", "特禀质"];

function fillSelect(id, values) {
  const sel = document.getElementById(id);
  if (!sel) return;
  sel.innerHTML = values.map((v) =>
    `<option value="${escapeHtml(v)}">${escapeHtml(v || "—")}</option>`).join("");
}

function intakeFieldHtml(f) {
  const picks = (f.quick_picks || []).map((p) =>
    `<button type="button" class="qp" data-field="${escapeHtml(f.name)}"`
    + ` data-value="${escapeHtml(p)}">${escapeHtml(p)}</button>`).join("");
  return `<div class="intake-field">`
    + `<label for="if-${escapeHtml(f.name)}">${escapeHtml(f.label)}</label>`
    + `<input type="text" id="if-${escapeHtml(f.name)}" class="text-field" data-intake="${escapeHtml(f.name)}" />`
    + (picks ? `<div class="qp-row">${picks}</div>` : "")
    + `</div>`;
}

function renderIntakeForm(data) {
  const box = document.getElementById("intake-fields");
  if (!box) return;
  INTAKE_FIELDS = data.parts || {};
  box.innerHTML = Object.entries(INTAKE_FIELDS).map(([part, fields]) =>
    `<fieldset class="intake-part"><legend>${escapeHtml(part)}</legend>`
    + fields.map(intakeFieldHtml).join("") + `</fieldset>`).join("");
  const note = document.getElementById("intake-note");
  // §0.4 的输入侧边界**摆在表单上**，不只写在文档里：填表的人要在填之前
  // 就知道这个系统不收照片和检验数值。
  if (note) note.textContent = data.regulatory_note || "";
  // 常用词一键选：点一下填进对应字段（已有内容就追加，不覆盖——
  // 覆盖会把医师刚打的那句话吃掉）
  box.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".qp");
    if (!b) return;
    const input = box.querySelector(`[data-intake="${b.dataset.field}"]`);
    if (!input) return;
    input.value = input.value ? `${input.value}；${b.dataset.value}` : b.dataset.value;
  });
}

function collectIntake() {
  const form = {};
  // **页面上没有这张表单时返回空对象，不抛。** 跟 `getSelectedRole()` 的
  // `sel ? sel.value : ...` 是同一条兜底：一个精简页面（或还没加载完的
  // 页面）缺了这一块，不该让整个提交流程断在这里。
  const all = document.querySelectorAll ? document.querySelectorAll("[data-intake]") : [];
  Array.prototype.forEach.call(all, (el) => {
    if (el.value && el.value.trim()) form[el.dataset.intake] = el.value.trim();
  });
  return form;
}

function splitList(v) {
  return (v || "").split(/[、,，;；]/).map((x) => x.trim()).filter(Boolean);
}

function collectProfile() {
  const val = (id) => (document.getElementById(id) || {}).value || "";
  const age = val("pf-age");
  const p = {
    age_years: age === "" ? null : Number(age),
    sex: val("pf-sex") || null,
    life_stage: val("pf-stage") || null,
    constitution: val("pf-const") || null,
    comorbidities: splitList(val("pf-comorb")),
    allergies: splitList(val("pf-allergy")),
    current_medications: splitList(val("pf-meds")),
    hepatic_impairment: val("pf-hep") || "不详",
    renal_impairment: val("pf-ren") || "不详",
  };
  const empty = p.age_years === null && !p.sex && !p.life_stage && !p.constitution
    && !p.comorbidities.length && !p.allergies.length && !p.current_medications.length
    && p.hepatic_impairment === "不详" && p.renal_impairment === "不详";
  // **一个字段都没填就不传**：传一份全空的 profile 会让后端把"没填"
  // 当成"填了但都是不详"，两者在个体化那一层是不同的结论。
  return empty ? null : p;
}

async function intakeToText() {
  const resp = await fetch("/api/intake/parse", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ form: collectIntake() }),
  });
  if (!resp.ok) return;
  const data = await resp.json();
  const ta = document.getElementById("complaint");
  if (ta) ta.value = data.text || "";
}

async function intakeFromText() {
  const ta = document.getElementById("complaint");
  const resp = await fetch("/api/intake/parse", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: (ta && ta.value) || "" }),
  });
  if (!resp.ok) return;
  const data = await resp.json();
  for (const [k, v] of Object.entries(data.form || {})) {
    const el = document.querySelector(`[data-intake="${k}"]`);
    if (el && typeof v === "string") el.value = v;
  }
}

// ---------- §7.3 循证对照 ----------
//
// **这一层只呈现差异，不参与选择。** 没有分数、没有排序、不回答"哪个更好"
// ——那会把它变成另一种投票。文案里的口径名（「教材推荐方案」）由后端下发，
// 前端不写死"指南"两个字：底本是什么，说的就是什么。

function guidelineHtml(g) {
  if (!g) return "";
  const label = g.basis_label || "";
  if (!g.covered) {
    return `<div class="gl-line gl-none">${escapeHtml(g.summary || "")}`
      + `<div class="gl-why">${escapeHtml(g.not_covered || "")}</div></div>`;
  }
  const rows = [];
  for (const a of g.aligned || []) {
    rows.push(`<li class="gl-ok"><b>${escapeHtml(a.what)}</b>${escapeHtml(a.detail)}`
      + `<span class="gl-src">出处：《${escapeHtml(a.source || "")}》${escapeHtml(a.span || "")}</span></li>`);
  }
  for (const d of g.deviations || []) {
    rows.push(`<li class="gl-diff"><b>${escapeHtml(d.what)}</b>`
      + `${escapeHtml(label)}为「${escapeHtml(d.recommended || "")}」，`
      + `本次为「${escapeHtml(d.ours || "")}」`
      + `<div class="gl-note">${escapeHtml(d.note || "")}</div>`
      + `<span class="gl-src">出处：《${escapeHtml(d.source || "")}》${escapeHtml(d.span || "")}</span></li>`);
  }
  return `<details class="gl-box"><summary>${escapeHtml(g.summary || "")}（点击查看）</summary>`
    + `<ul class="gl-list">${rows.join("")}</ul>`
    + `<div class="gl-why">这一层只摆出异同供医师判断，不用于择优。</div></details>`;
}

function renderGuideline(g) {
  const el = document.getElementById("guideline-box");
  if (!el) return;
  el.innerHTML = guidelineHtml(g);
  el.classList.toggle("show", !!(g && (g.covered || g.summary)));
}

// ---------- §7.2 个体化调整 ----------

function individualizationHtml(ind) {
  if (!ind) return "";
  const items = (ind.items || []).map((it) =>
    `<li class="iv-item" data-kind="${escapeHtml(it.kind)}">`
    + `<span class="iv-kind">${escapeHtml(it.kind)}</span>`
    + `<b>${escapeHtml(it.target)}</b>${escapeHtml(it.adjustment)}`
    + `<div class="iv-why">${escapeHtml(it.reason)}</div>`
    + `<div class="iv-basis">依据：${escapeHtml(it.basis)}</div></li>`).join("");
  const considered = (ind.considered || []).map((c) =>
    `<li>${escapeHtml(c)}</li>`).join("");
  // **空的 items 配非空的 considered** 才说得清"查过了，没有需要调的"，
  // 而不是"没查"。所以两块都渲染，不因为 items 是空就整块不出现。
  const head = items
    ? `按患者情况需要注意 ${(ind.items || []).length} 处`
    : "按患者情况核查完毕，没有需要调整的地方";
  return `<details class="iv-box"${items ? " open" : ""}>`
    + `<summary>${escapeHtml(head)}</summary>`
    + (items ? `<ul class="iv-list">${items}</ul>` : "")
    + (considered ? `<div class="iv-considered">已核查：<ul>${considered}</ul></div>` : "")
    + `</details>`;
}

function renderIndividualization(ind) {
  const el = document.getElementById("individualization-box");
  if (!el) return;
  el.innerHTML = individualizationHtml(ind);
  el.classList.toggle("show", !!ind);
}

// ---------- §7.4 病历文书 ----------

let LAST_EMR = null;

function emrSectionHtml(s) {
  const body = s.editable
    ? `<textarea class="emr-input" data-emr="${escapeHtml(s.key)}"`
      + ` rows="${Math.max(2, (s.body || "").split("\n").length)}">${escapeHtml(s.body || "")}</textarea>`
    : `<div class="emr-ro">${escapeHtml(s.body || "＿＿＿＿＿＿")}</div>`;
  return `<div class="emr-sec"><label>${escapeHtml(s.title)}</label>${body}</div>`;
}

function renderEMR(emr) {
  const box = document.getElementById("emr-box");
  if (!box) return;
  LAST_EMR = emr;
  const show = !!emr && getSelectedRole() === "doctor";
  box.hidden = !show;
  if (!show) return;
  const notice = document.getElementById("emr-notice");
  if (notice) notice.textContent = emr.draft_notice || "";
  const secs = document.getElementById("emr-sections");
  if (secs) secs.innerHTML = (emr.sections || []).map(emrSectionHtml).join("");
}

function collectEmrEdits() {
  const edits = {};
  document.querySelectorAll("[data-emr]").forEach((el) => {
    const sec = (LAST_EMR && (LAST_EMR.sections || []).find((s) => s.key === el.dataset.emr));
    if (sec && sec.body !== el.value) edits[el.dataset.emr] = el.value;
  });
  return edits;
}

async function buildEmrFromLastResult(data) {
  const r = (data.results || [])[0] || {};
  const st = r.s3_structured || r.s3 || {};
  const formula = st.formula || (st.formula_candidates || [])[0] || null;
  const resp = await fetch("/api/emr/draft", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      record_id: data.record_id || "",
      complaint: LAST_COMPLAINT || "",
      intake: collectIntake(),
      patient_profile: collectProfile(),
      s2: data.s2 || null,
      s3: st,
      formula,
      triage: data.triage || null,
      guideline: data.guideline || null,
      doctor_id: (document.getElementById("doctor-id-input") || {}).value || "",
      edits: collectEmrEdits(),
      // R56 §6：危重信号按角色分流的产品面第 3 条——EMR「危重提示」段，
      // 原样透传这次响应的 safety_flag，不在前端重新判断。
      safety_flag: data.safety_flag || null,
    }),
  });
  if (!resp.ok) return null;
  return resp.json();
}

function emrStatus(text) {
  const el = document.getElementById("emr-status");
  if (el) el.textContent = text;
}

async function copyText(text, okMsg) {
  try {
    await navigator.clipboard.writeText(text);
    emrStatus(okMsg);
  } catch (e) {
    // 剪贴板在非安全上下文里不可用（内网 http 就是这种情况）：
    // **说出来并给出路**，不是静默失败
    emrStatus("这个浏览器不允许自动复制，请手动选中下方文本复制。");
  }
}

// ---------- §7.5 知识速查（Ctrl/⌘ + K） ----------

let kpSeq = 0;

function kpResultHtml(groups) {
  return (groups || []).map((g) => {
    if (!g.n) return "";
    const items = g.items.map((it) =>
      `<li><b>${escapeHtml(it.title)}</b><div class="kp-sum">${escapeHtml(it.summary || "")}</div>`
      + (it.source ? `<div class="kp-src">出处：《${escapeHtml(it.source)}》${escapeHtml((it.span || "").slice(0, 60))}</div>` : "")
      + `</li>`).join("");
    return `<section class="kp-group"><h3>${escapeHtml(g.label)}（${g.n}）</h3><ul>${items}</ul></section>`;
  }).join("");
}

async function kpSearch(q) {
  const seq = ++kpSeq;
  const status = document.getElementById("kp-status");
  const out = document.getElementById("kp-results");
  if (!q.trim()) { if (out) out.innerHTML = ""; if (status) status.textContent = ""; return; }
  if (status) status.textContent = "查询中…";
  const t0 = performance.now();
  const resp = await fetch(`/api/knowledge/search?q=${encodeURIComponent(q)}`);
  // 输入框打字快过网络时会有多个在飞：**只认最后一次**，否则前一次的结果
  // 会盖住后一次的（R43 那条 `gbFetchSeq` 是同一个形状）
  if (seq !== kpSeq) return;
  if (!resp.ok) { if (status) status.textContent = "查询失败"; return; }
  const data = await resp.json();
  const ms = Math.round(performance.now() - t0);
  if (out) out.innerHTML = kpResultHtml(data.groups) || `<div class="kp-none">${escapeHtml(data.note || "")}</div>`;
  if (status) status.textContent = `${ms} ms`;
}

function kpOpen() {
  const el = document.getElementById("kp-overlay");
  if (!el) return;
  el.hidden = false;
  const input = document.getElementById("kp-input");
  if (input) { input.focus(); input.select(); }
}

function kpClose() {
  const el = document.getElementById("kp-overlay");
  if (el) el.hidden = true;
}

// ---------- 接线 ----------

{
  const to = document.getElementById("intake-to-text");
  if (to) to.addEventListener("click", intakeToText);
  const from = document.getElementById("intake-from-text");
  if (from) from.addEventListener("click", intakeFromText);
  fillSelect("pf-stage", LIFE_STAGES);
  fillSelect("pf-const", CONSTITUTIONS);
  fetch("/api/intake/form").then((r) => (r.ok ? r.json() : null))
    .then((d) => { if (d) renderIntakeForm(d); })
    .catch(() => { /* 后端不在：表单空着，自由文本那条路照走 */ });

  const kpIn = document.getElementById("kp-input");
  if (kpIn) {
    let timer = null;
    kpIn.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => kpSearch(kpIn.value), 120);
    });
  }
  const kpX = document.getElementById("kp-close");
  if (kpX) kpX.addEventListener("click", kpClose);
  const kpOv = document.getElementById("kp-overlay");
  if (kpOv) kpOv.addEventListener("click", (e) => { if (e.target === kpOv) kpClose(); });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      kpOpen();
      return;
    }
    if (e.key === "Escape" && kpOv && !kpOv.hidden) kpClose();
  });

  const copyBtn = document.getElementById("emr-copy-text");
  if (copyBtn) copyBtn.addEventListener("click", async () => {
    const out = await buildEmrFromLastResult(LAST_RESULT || {});
    if (!out) { emrStatus("生成失败"); return; }
    renderEMR(out.emr);
    await copyText(out.text, "病历文本已复制，粘进病历编辑器后请逐段核对。");
  });
  const jsonBtn = document.getElementById("emr-copy-json");
  if (jsonBtn) jsonBtn.addEventListener("click", async () => {
    const out = await buildEmrFromLastResult(LAST_RESULT || {});
    if (!out) { emrStatus("生成失败"); return; }
    await copyText(JSON.stringify(out.emr, null, 2), "结构化 JSON 已复制。");
  });
  const printBtn = document.getElementById("emr-print");
  if (printBtn) printBtn.addEventListener("click", async () => {
    const out = await buildEmrFromLastResult(LAST_RESULT || {});
    if (!out) { emrStatus("生成失败"); return; }
    renderEMR(out.emr);
    emrStatus("已生成处方笺，使用浏览器打印（A4 纵向）。");
    window.print();
  });
}
