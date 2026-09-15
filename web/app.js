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
// layer 4（药材）跟 layer 3（方剂）的 x 只差一小段——compound 子节点要贴着
// 父节点画，隔太远 cytoscape 算出来的方剂包围盒会变成横跨整个画布的细长条，
// 不会像"一个方框里装着自己的药"。
const LAYER_X = { 0: 60, 1: 260, 2: 460, 3: 640, 4: 730 };
// M7：Y_MAX 从 460 提到 620——只调这一个数救不了拥挤（cy.fit() 最后会把全部
// 内容按同一个缩放系数塞进 #cy 容器，光把逻辑坐标范围拉大、节点本身的像素
// 尺寸不变的话，摆放间距和节点尺寸的"比例"没变，缩放后看起来还是一样挤）。
// 真正起作用的是这个比例本身：药材间距（下面 HERB_GAP）从 16 提到 26，
// 明显大于药材节点自身高度（字号 11 + 上下 padding 6，约 23 个逻辑单位），
// 26 提供的间隙足够放开重叠；Y_MAX 一起加大只是给"医家带"之间、方剂与方剂
// 之间腾出更多空间，配合 #cy 容器本身的高度（也在这轮从 460px 提到 540px）
// 一起用，两处缺一处都解决不了 M5 报告标注的"药材纵向堆叠间距小"。
const Y_MIN = 60, Y_MAX = 620;
// 单味药材之间的目标纵向间距（逻辑单位，不是像素——最终经 cy.fit() 统一缩放）。
const HERB_GAP = 26;

let cy = null;

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
}

// §5.1 第 3 条：**超限降级到回放而不是报错**——访问者仍能看到预录主诉的完整
// 效果。所以这一行用 --surface-2 底、不是警告色：降级不是错误，是换了个后端
// 继续跑。原话由服务端给（u.reason），前端不自己编一套——两处措辞不一致时
// 用户不知道信哪个。
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

function switchTab(tab) {
  const toConsult = tab === "consult";
  document.getElementById("tab-btn-consult").classList.toggle("active", toConsult);
  document.getElementById("tab-btn-graph-browser").classList.toggle("active", !toConsult);
  document.getElementById("tab-consult").hidden = !toConsult;
  document.getElementById("tab-graph-browser").hidden = toConsult;
  if (!toConsult && !gbGraphData) loadGraphBrowserData();
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

    put(`syn::${r.physician}`, {
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
      put(`elem::${hit.element}`, {
        title: `证素　${hit.element}`,
        physician_name: r.physician_name,
        color: r.color,
        element_kind: hit.kind === "location" ? "病位" : "病性",
        confidence: hit.confidence,
        supporting: hit.supporting_symptoms || [],
      });
      for (const sym of hit.supporting_symptoms || []) {
        put(`sym::${sym}`, {
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
      put(`formula::${r.physician}::${cand.name}`, {
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
        put(`herb::${r.physician}::${cand.name}::${item.name}`, {
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

function evidenceSectionHtml(s) {
  const rows = [];
  if (s.element_kind) rows.push(`<div class="meta">${s.element_kind}　置信度 ${s.confidence}</div>`);
  if (s.explained_by) rows.push(`<div class="meta">由证素「${escapeHtml(s.explained_by)}」解释　置信度 ${s.confidence}</div>`);
  if (s.supporting && s.supporting.length) {
    rows.push(`<div class="label">支撑症状</div><div>${escapeHtml(s.supporting.join("、"))}</div>`);
  }
  if (s.treatment) rows.push(`<div class="label">治法</div><div>${escapeHtml(s.treatment)}</div>`);
  if (s.formula) rows.push(`<div class="label">方剂</div><div>${escapeHtml(s.formula)}</div>`);

  // M5：方剂节点的候选方元信息（source/confidence/rationale……）。只有
  // formula:: 节点的 payload 才会带 source 字段，症状/证素/证型的 payload
  // 里没有这个键，条件判断天然把两类节点分开，不用额外传节点类型标记。
  if (s.source) {
    const srcLabel = { classic: "经典方", modified: "加减方", composed: "自拟方" }[s.source] || s.source;
    const metaParts = [
      escapeHtml(srcLabel),
      s.base_formula ? "原方：" + escapeHtml(s.base_formula) : "",
      "置信度 " + escapeHtml(s.confidence || ""),
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

async function initDemoModeBanner() {
  try {
    const resp = await fetch("/health");
    if (!resp.ok) { renderOfflineBanner(false); return; }
    const health = await resp.json();
    renderDemoMode(health.demo_mode);
    injectPhysicianColors(health.physicians);
    // 示例主诉和身份色同一趟拿：两者都要在"点第一次辨证之前"就到位。
    EXAMPLE_COMPLAINTS = health.example_complaints || [];
    renderExamples(EXAMPLE_COMPLAINTS);
    renderConsultLambda1Note(health.lambda1_note);
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
function refFoldHtml(refs) {
  const n = (refs || []).length;
  if (!n) {
    return '<div class="col-refs col-refs-empty">无相关医案（相似度均低于阈值）</div>';
  }
  return `<details class="col-refs"><summary>引自 ${n} 条医案</summary>
    <div class="detail-block">${refListHtml(refs)}</div>
  </details>`;
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

function herbRowsHtml(groups) {
  return groups
    .map((g) => {
      const text = g.herbs.map((h) => escapeHtml(herbItemLabel(h))).join("、");
      const inner = g.role === "君" ? `<span class="herb-jun">${text}</span>` : text;
      return `<div class="field"><b>${escapeHtml(g.role)}</b>${inner}</div>`;
    })
    .join("");
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

function initDoctorState(results) {
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
    <div class="label rx-section-title">医生编辑处方——${escapeHtml(state.name)}</div>
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

async function runExport(physician) {
  const state = DOCTOR_STATE[physician];
  if (!state) return;
  const doctorIdInput = document.getElementById("doctor-id-input");
  const doctorId = (doctorIdInput ? doctorIdInput.value : "").trim();
  if (!doctorId) {
    state.exportError = { message: "请先在上方填写医生标识再导出处方。" };
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
      state.exportError = { message: `导出失败（HTTP ${resp.status}）：${JSON.stringify(data)}` };
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
    return "医生模式：处方辅助工具。系统提供的方剂与剂量为建议，最终处方由执业医师" +
      "审核、修改并签发，医师承担全部临床责任。所有导出操作均记录审计日志。";
  }
  return "教学与研究用途，非诊断工具，不能替代执业医师";
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

function renderExamples(examples) {
  const el = document.getElementById("examples");
  if (!el) return;
  el.innerHTML = exampleListHtml(examples);
  for (const btn of el.querySelectorAll(".example")) {
    btn.addEventListener("click", () => {
      document.getElementById("complaint").value = btn.dataset.complaint;
      document.getElementById("complaint").focus();
    });
  }
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
    const name = PHYSICIAN_NAMES[pid] || pid;
    parts.push(`<span class="rx-group" data-physician="${escapeHtml(pid)}">`
      + `<span class="rx-tag">${escapeHtml(name)}独有 ${own.length}</span>`
      + dotsHtml(own.length, "dot-own", `var(--phys-${pid}, var(--ink))`)
      + `</span>`);
  }
  const value = divergence.pairs_mean;
  const seg = bandSegments((divergence.epsilon_for_query || {}).value, value);
  const line = seg
    ? `<div class="rx-line"><span class="rx-seg rx-seg-noise" style="width:${seg.epsPct}%"></span>`
      + `<span class="rx-seg rx-seg-real" style="width:${seg.realPct}%"></span></div>`
    : `<div class="rx-line rx-line-unmeasured"></div>`;
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
  return columnShellHtml(physician, meta.name || PHYSICIAN_NAMES[physician] || physician,
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
      ${doctorSectionHtml(result.physician, mode)}
      ${refFoldHtml(result.refs)}
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
  const name = meta.name || PHYSICIAN_NAMES[pid] || pid;
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

function describeSafetyFlag(flag) {
  // safety_flag 非空 = 服务端开着 EVAL_MODE，这条主诉本该在辨证前被拦下、
  // 不产出任何方药，但评测模式让它跑完了。README 说"不要在对外演示的机器上
  // 打开"，可是之前页面上完全看不出来它开着——结果照常显示、方药照常开。
  // 返回 null 表示不需要横幅。
  if (!flag) return null;
  return `⚠ 评测模式（EVAL_MODE）已开启：这条主诉命中了安全否决（${flag}），正常情况下会在辨证前被拦截、不产出任何方药。下面的结果只用于评测，不要用于演示或作为参考。`;
}

function renderSafetyFlag(flag) {
  const box = document.getElementById("safety-flag-note");
  if (!box) return;
  const text = describeSafetyFlag(flag);
  box.textContent = text || "";
  box.classList.toggle("show", !!text);
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
  return body;
}

function renderConsultResult(data) {
  resetSecondaryPanels();
  hidePatientView();
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
  PHYSICIAN_COLORS = {};
  PHYSICIAN_NAMES = {};
  for (const r of data.results) {
    if (r.color) PHYSICIAN_COLORS[r.physician] = r.color;
    PHYSICIAN_NAMES[r.physician] = r.physician_name;
  }
  // 每次问诊都刷新一次：页面加载时那次 /health 可能失败，而这一条是必须出现的
  renderDemoMode(data.demo_mode);
  const m = data.manifest;
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
  initDoctorState(data.results);
  setConsultState("done");
  // R15：患者模式是**另一种形态**，不是三列的裁剪版（§3.5）。两块互斥——
  // 不是把三列渲染出来再用 CSS 藏起来：藏起来的东西仍然在 DOM 里，而 patient
  // 的字段裁剪是一条安全边界（api/main.py::_filter_s3_for_role），
  // 前端再把裁剪过的空壳摆出来只会让人以为"这次没开出方"。
  if (getSelectedRole() === "patient") {
    document.getElementById("columns").innerHTML = "";
    renderPatientView(data);
  } else {
    hidePatientView();
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
      return `③ 追问结束（${data.rounds} 轮，${data.stopped_by}）`;
    case "residual_done":
      return `④ 残差辨证：补充解释「${(data.newly_explained || []).join("、") || "无"}」`;
    case "physician_start":
      return `▶ ${data.physician_name} 开始辨证…`;
    case "react_step":
      return `　${data.physician_name} 取证第 ${data.step} 步：${data.action}`;
    case "s3_start":
      return `　${data.physician_name} 正在拟定证型与方药…`;
    case "physician_done":
      return `✓ ${data.physician_name} 完成：${data.syndrome}`;
    case "followup_answered":
      return `　已回答「${data.question}」：${data.answer}`;
    default:
      return null; // stream_id / need_input / done / error 各自单独处理，不进日志
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
  renderColumnsPlaceholder((pid) => columnRunningHtml(pid, "s1"));
  currentStreamId = null;

  currentAbort = new AbortController();
  cancelledByUser = false;
  let idleTimedOut = false;
  if (cancelBtn) cancelBtn.classList.remove("is-hidden");

  const t0 = Date.now();
  const ticker = setInterval(() => {
    status.textContent = `已用时 ${Math.round((Date.now() - t0) / 1000)}s`;
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
      } else {
        const line = describeProgressEvent(name, data);
        if (line) appendProgress(line);
        // 三列各自的分步进度。**按 physician 字段路由，不按到达顺序推**——
        // R12 之后三位医家是并发跑的，叶天士的 done 完全可能排在张锡纯的
        // start 后面（docs/DESIGN.md §4.7 的订正）。
        const step = columnStepForEvent(name, data);
        if (step) setColumnStep(step.physician, step.step);
        if (name === "followup_answered") hideNeedInput();
      }
    });

    hideNeedInput();
    if (errorDetail) throw new Error(errorDetail);
    if (!doneData) throw new Error("事件流意外中断，没有收到最终结果");
    renderConsultResult(doneData);
  } catch (err) {
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
  } finally {
    clearInterval(ticker);
    clearTimeout(idleTimer);
    if (cancelBtn) cancelBtn.classList.add("is-hidden");
    currentAbort = null;
    cancelledByUser = false;
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
// 没有图时这两个按钮点了什么都不会发生（lastGraph 为 null），原来仍然是
// 可点的实心按钮，看起来像坏了。
function updateGraphToolbar() {
  for (const id of ["skip-btn", "replay-btn"]) {
    const b = document.getElementById(id);
    if (b) b.disabled = !lastGraph;
  }
}
document.getElementById("skip-btn").addEventListener("click", skipAnimation);
document.getElementById("replay-btn").addEventListener("click", replayGraph);
updateGraphToolbar();
// 保险起见运行时把面板挂到 body 末尾：只要祖先里有任何 transform/filter，
// position:fixed 就会相对该祖先定位而不是视口，面板会跑到页面中间去。
{
  const _p = document.getElementById("evidence-panel");
  if (_p && _p.parentElement !== document.body) document.body.appendChild(_p);
}
document.getElementById("evidence-close").addEventListener("click", closeEvidence);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeEvidence();
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
});
