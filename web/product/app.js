/* R62 产品面：问诊页（§4–§8）。
 *
 * ## 跟 web/app.js 的关系：并存，不继承
 *
 * 旧页面整份留着（`PRODUCT_MODE=0` 的内部研究版还在用它，R1~R61 的截图与
 * 演示脚本也都指着它）。这份是**重写**，不是在那份上打补丁——§4.0 原话
 * 「交付时打开界面应当认不出是同一个产品」。两份共用的只有后端与
 * `web/vendor/`。
 *
 * ## 三条贯穿全文件的约定
 *
 * **一、角色裁剪只信服务端。** 前端的 `data-role` 只管"看不看得到入口"，
 * 不管"拿不拿得到数据"——后者在 `api/main.py::_filter_response_by_role`。
 * 打开 devtools 能绕过的过滤不是过滤。
 *
 * **二、所有插值都转义。** 模型输出、患者备注、本体原文都会进 DOM。
 * `esc()` 是唯一的出口，没有第二处拼 HTML 的地方绕开它。
 *
 * **三、每个异步动作都有"失败了会怎样"。** 编辑助手超时右栏空着但红条还在；
 * 问诊要点取不到就不显示那一块；导出失败给一句话而不是静默。
 * 一个点了没反应的按钮比一个明说"这里没有"的提示更难查。
 */

/* ═════════ 0. 基础工具 ═════════ */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** 节流：给"停止输入 N 毫秒后触发"用（§5.4 的 1.5 秒、§7.3 的 1.5 秒）。 */
function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* 非 JSON 错误体 */ }
    const err = new Error(detail); err.status = r.status; throw err;
  }
  return r.json();
}
const send = (method) => (path, body) => api(path, {
  method, headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const post = send("POST");
// **设置是 PUT，不是 POST。** 用 post 打它会得到 405，而 405 在界面上长成
// "保存失败"——切角色点了没反应，而控制台里那一行没人看。方法写成参数，
// 两个助手出自同一处，不会再出现"这条路径用错动词"。
const put = send("PUT");

/* ═════════ 1. 全局状态 ═════════ */

const S = {
  role: "doctor",
  prefs: null,
  choices: null,
  health: null,
  result: null,          // 最近一次 done 的完整响应
  draft: null,           // s3_draft：推导出来了、还没过核查
  explanations: {},      // id -> 释义（随结果一次性下发，点术语时本地取）
  rx: [],                // 处方表当前内容（可编辑）
  rxBase: [],            // 模型给的原始方，算 diff 用
  pinned: false,
  abort: null,
  recordId: "",
  labRx: [],
};

/* ═════════ 2. 角色与设置 ═════════ */

function setRole(role) {
  S.role = role;
  document.documentElement.dataset.role = role;
  $("role-select").value = role;
  $("set-role").value = role;
  applyRoleFolds();
}

/** §8 那张表的"默认展开/收起"。**只管默认**——人点开之后不再强制。 */
function applyRoleFolds() {
  const open = {
    student: { basis: true, self: true, diff: true, cases: false },
    doctor: { basis: false, self: false, diff: false, cases: false },
    patient: { basis: false, self: false, diff: false, cases: false },
  }[S.role] || {};
  if ($("sec-basis")) $("sec-basis").open = !!open.basis;
  if ($("sec-self")) $("sec-self").open = !!open.self;
  if ($("sec-cases")) $("sec-cases").open = !!open.cases;
  const diff = $("diff-block");
  if (diff) diff.hidden = !open.diff || !(S.result && currentS3S().differential || []).length;
}

function fillSelect(sel, values, current) {
  if (!sel) return;
  sel.innerHTML = values.map((v) =>
    `<option value="${esc(v)}"${String(v) === String(current) ? " selected" : ""}>${esc(v)}</option>`).join("");
}

async function loadPreferences() {
  const got = await api("/api/preferences");
  S.prefs = got.preferences; S.choices = got.choices;
  // **可选值由服务端给**（§4.3）：前端写死一份的话，后端加一种剂型，
  // 这里的下拉不会跟着长出来。
  fillSelect($("set-form"), S.choices.dosage_form, S.prefs.dosage_form);
  fillSelect($("set-doses"), S.choices.doses_count, S.prefs.doses_count);
  fillSelect($("set-herbcount"), S.choices.herb_count_band, S.prefs.herb_count_band);
  fillSelect($("fm-form"), S.choices.dosage_form, S.prefs.dosage_form);
  refreshUsageChoices();
  $("set-avoid").value = (S.prefs.avoid_herbs || []).join("、");
  $("set-signature").value = S.prefs.signature || "";
  $("set-fontsize").value = S.prefs.font_size;
  document.documentElement.dataset.fontsize = S.prefs.font_size;
  setRole(S.prefs.role);
}

function refreshUsageChoices() {
  const form = ($("set-form") && $("set-form").value) || S.prefs.dosage_form;
  fillSelect($("set-usage"), (S.choices.usage[form] || []), S.prefs.usage);
}

async function savePreference(changes) {
  const el = $("set-status");
  try {
    const got = await put("/api/preferences", { changes });
    S.prefs = got.preferences;
    el.textContent = "已保存";
    setTimeout(() => { el.textContent = ""; }, 1500);
    return true;
  } catch (e) {
    // 值不合法时后端 400 带一句中文原因——**原样显示**，不换成
    // "保存失败"：后者不告诉人该改什么。
    el.textContent = e.message;
    return false;
  }
}

/* ═════════ 3. 患者信息 ═════════ */

function patientProfile() {
  const list = (id) => ($(id).value || "").split(/[、,，]/).map((s) => s.trim()).filter(Boolean);
  const age = $("pf-age").value;
  return {
    age_years: age === "" ? null : Number(age),
    sex: $("pf-sex").value || null,
    life_stage: $("pf-stage").value || null,
    constitution: $("pf-const").value || null,
    comorbidities: list("pf-comorb"),
    allergies: list("pf-allergy"),
    current_medications: list("pf-meds"),
  };
}

/** §5.2：年龄与性别必填，不填按钮禁用**并说明缺哪项**。 */
function checkRequired() {
  const missing = [];
  if ($("pf-age").value === "") missing.push("年龄");
  if (!$("pf-sex").value) missing.push("性别");
  const ok = missing.length === 0 && ($("complaint").value || "").trim().length > 0;
  $("btn-go").disabled = !ok;
  $("pf-hint").textContent = missing.length
    ? `还缺：${missing.join("、")}——小儿剂量折算与妊娠禁忌都要靠这两项。`
    : "";
  return ok;
}

/* ═════════ 4. 右栏四态（§7.1） ═════════ */

function rcShow(title, html) {
  if (S.pinned && title !== $("rc-title").textContent) return;  // 钉住时不换
  $("rc-title").textContent = title;
  $("rc-body").innerHTML = html;
}
/** 钉住时也要能强制换（点了别的术语是显式动作，跟自动刷新不同）。 */
function rcForce(title, html) {
  $("rc-title").textContent = title;
  $("rc-body").innerHTML = html;
}

/* ---- 4a. 问诊要点提示（§5.4） ---- */

const askIntakeHints = debounce(async () => {
  const text = ($("complaint").value || "").trim();
  if (!text || S.abort) return;
  try {
    const got = await post("/api/intake/hints", { text });
    if (!(got.hints || []).length) {
      rcShow("问诊要点提示", `<p class="rc-empty">${esc(got.note || "")}</p>`);
      return;
    }
    rcShow("问诊要点提示", `<ul class="hint-list">${got.hints.map((h, i) => `
      <li>
        <button class="hint-ask" data-hint="${i}">· ${esc(h.ask)}</button>
        <span class="hint-why${h.safety_relevant ? " hint-safety" : ""}">—— ${esc(h.why)}</span>
      </li>`).join("")}</ul>`);
    S.hints = got.hints;
  } catch (e) {
    // 取不到就不显示这一块——它是锦上添花，主诉照样能提交。
    rcShow("问诊要点提示", `<p class="rc-empty">${esc(e.message)}</p>`);
  }
}, 1500);

function adoptHint(i) {
  const h = (S.hints || [])[i];
  if (!h) return;
  const box = $("complaint");
  box.value = `${box.value.replace(/\s+$/, "")}\n问：${h.ask}　答：`;
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
}

/* ---- 4b. 术语释义（§7.2） ---- */

function explainTerm(id, name) {
  const d = S.explanations[id];
  if (!d) {
    // 随结果下发时被上限截断的那些：**现查一次**，不是点了没反应。
    api(`/api/node_explain?node=${encodeURIComponent(id)}&name=${encodeURIComponent(name || "")}`)
      .then((got) => { S.explanations[id] = got; renderExplain(got); })
      .catch((e) => rcForce(name || "释义", `<p class="rc-empty">${esc(e.message)}</p>`));
    rcForce(name || "释义", '<p class="rc-empty">查询中…</p>');
    return;
  }
  renderExplain(d);
}

function renderExplain(d) {
  if (!d || !d.available) {
    rcForce(d && d.title || "释义",
      `<p class="rc-empty">${esc((d && d.note) || "本地数据里查不到这一条的释义。")}</p>`);
    return;
  }
  const secs = (d.sections || []).map((s) => `
    <div class="ne-sec"><h4>${esc(s.heading)}</h4>
      ${(s.lines || []).map((l) => `<p>${esc(l)}</p>`).join("")}
      ${s.source ? `<p class="ne-src">——${esc(s.source)}</p>` : ""}
    </div>`).join("");
  rcForce(d.title, `<div class="ne-title">${esc(d.title)}</div>
    <div class="ne-kind">${esc(NODE_KIND_LABEL[d.kind] || "")}</div>${secs}`);
}

const NODE_KIND_LABEL = {
  symptom: "症状", element: "证素", syndrome: "证型", pathogenesis: "病机",
  principle: "治则", method: "治法", formula: "方剂", herb: "药材",
  rule: "医理规则", case: "医案",
};

/** 把一段文字变成可点的术语。`id` 走后端那套前缀，跟图上的节点同一个 id。 */
function term(id, text) {
  return `<span class="term" data-term="${esc(id)}" data-name="${esc(text)}">${esc(text)}</span>`;
}

/* ═════════ 5. 问诊：SSE ═════════ */

/** 后端每一个进度事件都要有这里的一条分支——`tests/test_event_contract.py`
 *  逐个比对两边的名单，少一个红一个。默认分支返回 null（不进日志）不是
 *  兜底，是"这个事件不需要日志"的显式表达。 */
function progressLine(name, d) {
  switch (name) {
    case "s1_done": return { key: "s1", done: true, text: `症状标准化　${(d.symptoms || []).join(" / ")}` };
    case "s2_done": return { key: "s2", done: true, text: `病位病性　${(d.elements || []).map((e) => e.element).join(" · ") || "（未推出）"}` };
    case "followup_done": return { key: "ask", done: true, text: "追问完成" };
    case "residual_done": return { key: "res", done: true, text: "残差辨证完成" };
    case "physician_start": return { key: "s3", done: false, text: "正在推导证型…" };
    case "s3_start": return { key: "s3", done: false, text: "正在推导证型…" };
    case "s3_delta": return null;      // 逐字增量不进日志，会把日志刷没
    case "s3_done": return { key: "s3", done: true, text: "推导完成" };
    case "s3_draft": return { key: "draft", done: true, text: "方已拟出（尚未核查）" };
    case "verify_start": return { key: "chk", done: false, text: "正在逐条核对方药与医理…" };
    case "verify_round": return {
      key: "chk", done: false,
      text: (d.n_veto || d.n_revise)
        ? `第 ${d.round} 轮：${d.n_veto} 条须改、${d.n_revise} 条待商榷`
        : `第 ${d.round} 轮：${(d.checked_rules || []).length} 条规则全部通过`,
    };
    case "verify_revise": return { key: "chk", done: false, text: `按反例重开第 ${d.round} 轮…` };
    case "verify_done": return { key: "chk", done: true, text: d.first_pass ? "核查一次通过" : `核查完成（改了 ${d.revise_calls} 轮）` };
    case "corroborate_start": return { key: "corr", done: false, text: "检索历史上有没有这么治过…" };
    case "corroborate_done": return {
      key: "corr", done: true,
      text: d.enabled === false ? "医案对照：本次未启用"
        : `医案对照：方向一致 ${d.n_concordant} 条`,
    };
    case "physician_done": return null;
    case "early_veto": return { key: "chk", done: false, text: `初步提示：${d.reason || d.rule_label || ""}` };
    case "react_step": return null;
    case "agent_step": return null;
    case "followup_answered": return null;
    case "deltas_dropped": return null;
    case "heartbeat": return null;
    default: return null;
  }
}

const PROGRESS_ORDER = ["s1", "s2", "ask", "res", "s3", "draft", "chk", "corr"];

function renderProgress(state) {
  const rows = PROGRESS_ORDER.filter((k) => state[k]).map((k) => {
    const s = state[k];
    return `<li class="${s.done ? "prog-done" : "prog-now"}">${esc(s.text)}</li>`;
  }).join("");
  // §11.4 明确不显示 token 数、帧数、首字耗时、样本数、模型名、调用次数。
  rcShow("推导进度", `<ul class="prog-list">${rows}</ul>`);
}

/** §11.3 兜底：`s3_done` 之后 60 秒还没等到 `done` 就主动查一次。 */
const DONE_FALLBACK_MS = 60000;

async function runConsult() {
  if (!checkRequired()) return;
  const complaint = $("complaint").value.trim();
  S.abort = new AbortController();
  S.draft = null; S.result = null;
  $("btn-go").disabled = true; $("btn-cancel").hidden = false;
  $("go-status").textContent = "推导中…";
  $("redflag-bar").hidden = true; $("triage-page").hidden = true;
  const progress = {};
  let streamId = ""; let fallbackTimer = null; let resolved = false;

  const finish = (data) => {
    if (resolved) return;
    resolved = true;
    clearTimeout(fallbackTimer);
    renderResult(data);
  };

  try {
    const r = await fetch("/api/consult/stream", {
      method: "POST", headers: { "Content-Type": "application/json" },
      signal: S.abort.signal,
      body: JSON.stringify({
        complaint, role: S.role, patient_profile: patientProfile(),
      }),
    });
    if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, cut); buf = buf.slice(cut + 2);
        let name = ""; let raw = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) name = line.slice(6).trim();
          else if (line.startsWith("data:")) raw += line.slice(5).trim();
        }
        if (!name) continue;
        let data = {};
        try { data = raw ? JSON.parse(raw) : {}; } catch (e) { continue; }

        if (name === "stream_id") { streamId = data.stream_id; continue; }
        if (name === "error") { throw new Error(data.detail || "服务端错误"); }
        if (name === "done") { finish(data); continue; }
        if (name === "s3_draft") {
          // §11.3：②–⑥ 现在就渲染，⑦ 显示"核对中…"，不让整页等到最后。
          S.draft = data.s3_structured;
          renderDraft(data.s3_structured);
        }
        if (name === "s3_done" && streamId) {
          fallbackTimer = setTimeout(async () => {
            try { finish(await api(`/api/consult/stream/${streamId}/result`)); }
            catch (e) { $("go-status").textContent = "结果还没到，请稍候或重试。"; }
          }, DONE_FALLBACK_MS);
        }
        const line = progressLine(name, data);
        if (line) { progress[line.key] = line; renderProgress(progress); }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") $("go-status").textContent = `出错了：${e.message}`;
  } finally {
    S.abort = null;
    $("btn-cancel").hidden = true;
    checkRequired();
    if (!$("go-status").textContent.startsWith("出错")) $("go-status").textContent = "";
  }
}

/* ═════════ 6. 渲染结果 ═════════ */

function currentS3S() {
  const r = (S.result && S.result.results && S.result.results[0]) || null;
  return (r && r.s3_structured) || S.draft || {};
}

/** `s3_draft` 一到就渲染②–⑥，⑦ 标"核对中…"。 */
function renderDraft(s3s) {
  if (!s3s) return;
  $("result-zone").hidden = false;
  renderConclusion(s3s, null);
  renderMechanism(s3s, null);
  renderFormula(s3s, null);
  renderMods(s3s);
  $("rx-check").innerHTML = '<span class="chk-warn">核对中…（方药尚未通过符号核查）</span>';
}

function renderResult(data) {
  S.result = data;
  S.recordId = data.record_id || "";
  $("about-record").textContent = S.recordId || "—";
  S.explanations = (data.explanations && data.explanations.by_id) || {};
  const first = (data.results || [])[0] || {};
  const s3s = first.s3_structured || {};
  const s3 = first.s3 || {};

  // 红旗与拦截（§8.2）
  if (data.rejected || (data.triage && S.role === "patient")) {
    renderTriagePage(data);
    return;
  }
  $("triage-page").hidden = true;
  if (data.safety_flag) {
    $("redflag-bar").hidden = false;
    $("redflag-bar").innerHTML = `<h3>⚠ 本例含危重征象：${esc(data.safety_flag)}</h3>
      <p>提示优先排除相应急症，必要时急诊处理。以下辨证仅供参考，须在处理急症的前提下使用。</p>`;
    $("sec-formula").classList.add("wm");
  } else {
    $("redflag-bar").hidden = true;
    $("sec-formula").classList.remove("wm");
  }

  $("result-zone").hidden = false;
  renderConclusion(s3s, s3);
  renderMechanism(s3s, s3);
  renderFormula(s3s, first, data);
  renderMods(s3s);
  renderGuideline(data.guideline);
  renderBasis(s3s);
  renderCases(first.corroboration);
  renderSelf(s3s);
  applyRoleFolds();
  runRuleCheck();
  rcShow("问诊要点提示", '<p class="rc-empty">结论已出。点中间任意术语，这里给出它的释义与出处。</p>');
}

function renderTriagePage(data) {
  const t = data.triage || {};
  $("result-zone").hidden = true;
  $("triage-page").hidden = false;
  $("triage-page").innerHTML = `<h2 class="card-h">请尽快就医</h2>
    <p>${esc(data.reject_reason || "所述症状中含有需要尽快就医的征象。")}</p>
    ${t.dept ? `<p>建议就诊科室：${esc(t.dept)}</p>` : ""}
    ${(t.red_flags || []).length ? `<p>需要注意的征象：${esc(t.red_flags.join("、"))}</p>` : ""}
    <p>本页不提供方药内容。</p>`;
}

function renderConclusion(s3s, s3) {
  const syn = (s3s.syndrome && s3s.syndrome.name) || (s3 && s3.syndrome) || "";
  const dis = (s3s.syndrome && s3s.syndrome.disease) || (s3 && s3.disease) || "";
  $("cc-syndrome").innerHTML = syn ? term(`syn::${syn}`, syn) : "—";
  $("cc-disease").innerHTML = dis ? term(`syn::${dis}`, dis) : "—";
  const kps = s3s.key_points || [];
  $("kp-list").innerHTML = kps.length ? kps.map((k) => `
    <li><span class="kp-point">${term(`sym::${k.point}`, k.point)}</span>
        <span class="kp-maps">${esc(k.maps_to)}</span></li>`).join("")
    : '<li class="lc-empty">本次未产出辨证要点。</li>';
  const diffs = s3s.differential || [];
  $("diff-list").innerHTML = diffs.length ? diffs.map((d) => `
    <li><span class="diff-syn">${term(`syn::${d.syndrome}`, d.syndrome)}</span>
        <span class="diff-why">${esc(d.excluded_because)}</span>
        ${(d.rule_refs || []).map((r) => term(`rule::${r.rule_id}`, r.rule_id)).join(" ")}</li>`).join("")
    : '<li class="lc-empty">本次未产出鉴别。点证型可以看证候表里的相似证型与鉴别点。</li>';
  $("btn-differential").classList.toggle("is-on", !$("diff-block").hidden);
}

function renderMechanism(s3s, s3) {
  const organs = s3s.organs || [];
  $("mm-patho").innerHTML = organs.length
    ? organs.map((o) => `${term(`organ::${o.organ}`, o.organ)}：${term(`mech::${o.pathogenesis}`, o.pathogenesis)}`).join("；")
    : "—";
  const targets = (s3s.method && s3s.method.targets) || [];
  $("mm-principle").innerHTML = targets.length
    ? targets.map((t) => term(`principle::${t}`, t)).join("、") : "—";
  const method = (s3s.method && s3s.method.principle) || (s3 && s3.treatment_principle) || "";
  $("mm-method").innerHTML = method ? term(`method::${method}`, method) : "—";
}

/* ═════════ 7. 方剂表（可编辑） ═════════ */

function renderFormula(s3s, first, data) {
  const cand = (s3s.formula && s3s.formula.candidate) || null;
  const tb = (data && data.textbook_formula) || null;

  // §8.1 患者模式：看到的是**教材代表方及其组成**，不是模型为他拟的那张。
  if (S.role === "patient") {
    $("fm-name").textContent = tb && tb.available !== false ? tb.name : "—";
    $("fm-textbook").hidden = false;
    $("fm-textbook").innerHTML = tb && tb.available !== false
      ? `<h4>${esc(tb.basis_label || "教材推荐方案")}</h4>
         <p>本证型的教材代表方为「${esc(tb.name)}」${tb.principle ? `，治法为${esc(tb.principle)}` : ""}。</p>
         ${tb.span ? `<p class="ne-src">——${esc(tb.source || "")}${esc(tb.span)}</p>` : ""}
         ${tb.note ? `<p>${esc(tb.note)}</p>` : ""}`
      : `<p>${esc((tb && tb.note) || "教材推荐方案里暂时没有收录这个证型的代表方。")}</p>`;
    S.rx = ((tb && tb.composition) || []).map((c) => ({
      name: c.name, dose: null, dose_unit: "g", processing: null,
      decoction: null, role: null, function_in_formula: c.dose || "",
    }));
    S.rxBase = S.rx.map((x) => ({ ...x }));
    renderRxTable();
    return;
  }

  $("fm-textbook").hidden = true;
  $("fm-name").innerHTML = cand ? term(`formula::${cand.name}`, cand.name) : "—";
  S.rx = (cand && cand.herb_items ? cand.herb_items : []).map((x) => ({ ...x }));
  S.rxBase = S.rx.map((x) => ({ ...x }));
  if (cand && cand.doses_count != null) $("fm-doses").value = cand.doses_count;
  if (cand && cand.usage) $("fm-usage").value = cand.usage;
  renderRxTable();
}

function renderRxTable() {
  const ro = S.role === "patient";
  $("rx-body").innerHTML = S.rx.map((h, i) => `
    <tr data-i="${i}">
      <td class="rx-role">${esc(h.role || "")}</td>
      <td class="rx-name">${term(`herb::${h.name}`, h.name)}</td>
      <td class="rx-dose"><input type="number" step="0.5" min="0" value="${h.dose == null ? "" : esc(h.dose)}"
          data-f="dose" ${ro ? "disabled" : ""}></td>
      <td class="rx-unit">${esc(h.dose_unit || "g")}</td>
      <td class="rx-proc"><input type="text" placeholder="炮制" value="${esc(h.processing || "")}"
          data-f="processing" ${ro ? "disabled" : ""}></td>
      <td class="rx-dec"><input type="text" placeholder="煎法" value="${esc(h.decoction || "")}"
          data-f="decoction" ${ro ? "disabled" : ""}></td>
      <td class="rx-fn">${esc(h.function_in_formula || "")}</td>
      <td>${ro ? "" : `<button class="rx-del" data-del="${i}" title="去掉这一味">✕</button>`}</td>
    </tr>`).join("") || '<tr><td colspan="8" class="lc-empty">方里还没有药。</td></tr>';
}

/** §7.3 第一层：**≤300ms，零 LLM**。改一味药必须立刻有反应。 */
async function runRuleCheck() {
  const box = $("rx-check");
  const hadBad = box.querySelector(".chk-bad") != null;
  try {
    const got = await post("/api/formula/check", {
      herb_items: S.rx, syndrome: currentSyndrome(),
      patient_profile: patientProfile(), role: S.role,
    });
    const bad = []; const warn = []; const ok = [];
    (got.incompatible || []).forEach((p) => bad.push(`${p[0]} 与 ${p[1]} 同用，属十八反十九畏`));
    (got.dose_violations || []).forEach((v) =>
      bad.push(`${v.herb} ${v.dose}${v.unit} 超过常用上限 ${v.limit_g}g`));
    ((got.individualization && got.individualization.items) || []).forEach((it) => {
      (it.kind === "慎用提示" ? warn : bad).push(`${it.target}：${it.adjustment}——${it.reason}`);
    });
    (got.decoction_missing || []).forEach((h) => warn.push(`${h} 需要标注煎法`));
    (got.advice || []).forEach((a) => {
      if (a.severity === "blocking") bad.push(a.reason);
      else if (a.severity === "warning") warn.push(a.reason);
      else warn.push(a.reason);
    });
    if (got.thermal_warning) warn.push(got.thermal_warning);
    if (!bad.length) ok.push("配伍无十八反十九畏", "剂量均在药典范围内");
    box.innerHTML = [
      ...bad.map((t) => `<span class="chk-bad">⚠ ${esc(t)}</span>`),
      ...warn.map((t) => `<span class="chk-warn">· ${esc(t)}</span>`),
      ...ok.map((t) => `<span class="chk-ok">✓ ${esc(t)}</span>`),
    ].join("");
    // §13.2 第 6 条：红条消失时**闪一次绿色「已解决」**——
    // 只在"刚才有红、现在没有"时闪，每次校验都闪等于没闪。
    if (hadBad && !bad.length) {
      box.classList.add("chk-resolved");
      setTimeout(() => box.classList.remove("chk-resolved"), 1200);
    }
  } catch (e) {
    box.innerHTML = `<span class="chk-warn">核查暂时不可用：${esc(e.message)}</span>`;
  }
}

function currentSyndrome() {
  const s = currentS3S();
  return (s.syndrome && s.syndrome.name) || "";
}
function currentMethod() {
  const s = currentS3S();
  return (s.method && s.method.principle) || "";
}

/** §7.3 第二层：停止编辑 1.5 秒后触发。超时只留第一层，不报错。 */
const askEditAdvice = debounce(async () => {
  if (S.role === "patient" || !S.rx.length) return;
  rcShow("AI 提示", '<p class="rc-empty">正在看这次改动…</p>');
  try {
    const got = await post("/api/formula/advise", {
      before: S.rxBase, after: S.rx,
      syndrome: currentSyndrome(), principle: currentMethod(),
      patient_profile: patientProfile(), role: S.role,
    });
    if (!got.comment) {
      rcShow("AI 提示", `<p class="rc-empty">${esc(got.note || (got.timed_out ? "提示超时了，处方表下面的规则核查仍然有效。" : "这次改动没什么要补充的。"))}</p>`);
      return;
    }
    S.adviceOptions = got.options || [];
    rcShow("AI 提示", `<div class="advice-box">
      <p>${esc(got.comment)}</p>
      ${(got.options || []).map((o, i) => `
        <div class="advice-opt">
          <span class="advice-cond">${esc(o.condition)}</span>：${esc(o.action_text)}
          <button class="btn-min" data-adopt="${i}">采纳</button>
          <div class="ne-src">${esc(o.basis)}</div>
        </div>`).join("")}
    </div>`);
  } catch (e) {
    rcShow("AI 提示", `<p class="rc-empty">${esc(e.message)}</p>`);
  }
}, 1500);

/** 采纳一条处置：把 changes 写回处方表。 */
function applyChanges(changes) {
  (changes || []).forEach((c) => {
    const it = c.item || {};
    const at = S.rx.findIndex((h) => h.name === it.name);
    if (c.action === "减") { if (at >= 0) S.rx.splice(at, 1); return; }
    if (c.action === "加") {
      if (at >= 0) S.rx[at] = { ...S.rx[at], ...it };
      else S.rx.push({ dose_unit: "g", ...it });
      return;
    }
    if (at >= 0) S.rx[at] = { ...S.rx[at], ...it };   // 改量/改炮制/改煎法
  });
  renderRxTable();
  runRuleCheck();
  askEditAdvice();
}

/* ═════════ 8. 加减建议 / 教材对照 / 折叠三区 ═════════ */

function renderMods(s3s) {
  const mods = s3s.modifications || [];
  S.mods = mods;
  $("mods-list").innerHTML = mods.length ? mods.map((m, i) => `
    <li>
      <span class="mod-if">若见${esc(m.if_symptom)}</span>
      <span class="mod-do">→ ${esc(m.action)} ${term(`herb::${(m.item || {}).name}`, (m.item || {}).name || "")}
        ${(m.item || {}).dose != null ? esc(`${m.item.dose}${m.item.dose_unit || "g"}`) : ""}</span>
      <span class="mod-why">${esc(m.why)}</span>
      <button class="btn-min" data-mod="${i}">采纳</button>
    </li>`).join("") : '<li class="lc-empty">本次未产出加减建议。</li>';
}

function renderGuideline(g) {
  if (!g) { $("sec-guideline").hidden = true; return; }
  $("sec-guideline").hidden = false;
  $("guideline-body").innerHTML = `
    <p>${esc(g.summary || g.note || "")}</p>
    ${g.recommended_formula ? `<p>${esc(g.basis_label || "教材推荐方案")}：${esc(g.recommended_formula)}</p>` : ""}
    ${(g.herb_diffs || []).length ? `<p>加减 ${g.herb_diffs.length} 处：${esc(g.herb_diffs.join("、"))}</p>` : ""}`;
}

/** ⑨ 推导依据：九步逐条列出用了什么规则，每条可点进右栏。 */
function renderBasis(s3s) {
  const rows = [];
  const push = (step, holder) => {
    ((holder || {}).rule_refs || []).forEach((r) =>
      rows.push(`<div class="basis-row"><span class="basis-step">${esc(step)}</span>
        ${term(`rule::${r.rule_id}`, r.rule_id)}　${esc(r.note || "")}</div>`));
    if ((holder || {}).insufficient) {
      rows.push(`<div class="basis-row"><span class="basis-step">${esc(step)}</span>
        <span class="chk-warn">依据不足：${esc(holder.insufficient.what)}</span></div>`);
    }
  };
  (s3s.organs || []).forEach((o) => push(`病位 ${o.organ}`, o));
  push("证型", s3s.syndrome);
  push("治法", s3s.method);
  push("方剂", s3s.formula);
  (s3s.herb_choices || []).forEach((c) => push(`用药 ${(c.item || {}).name || ""}`, c));
  $("basis-body").innerHTML = rows.join("") || '<p class="lc-empty">本次没有可列出的依据。</p>';
}

function renderCases(corr) {
  if (!corr) { $("cases-body").innerHTML = '<p class="lc-empty">语料中暂无相似处理记录。</p>'; return; }
  const all = [...(corr.concordant || []), ...(corr.divergent || [])];
  $("cases-body").innerHTML = all.length ? all.map((c) => `
    <div class="basis-row">${esc(c.physician || "")}　${esc(c.syndrome || "")}
      ${(c.shared_herbs || []).length ? `　共用：${esc(c.shared_herbs.join("、"))}` : ""}</div>`).join("")
    : '<p class="lc-empty">语料中暂无相似处理记录。</p>';
}

function renderSelf(s3s) {
  const a = s3s.self_assessment;
  $("self-body").innerHTML = a ? `
    <p>依据最弱的一环：${esc(a.weakest_link)}</p>
    ${(a.uncovered_symptoms || []).length ? `<p>本次未解释的症状：${esc(a.uncovered_symptoms.join("、"))}</p>` : ""}
    <p>若三剂无效，下一步方向：${esc(a.next_direction)}</p>`
    : '<p class="lc-empty">本次未产出自评。</p>';
}

/* ═════════ 9. 操作条：导出 / 生成记录 / 保存 / 模板 ═════════ */

function currentFormula() {
  const s3s = currentS3S();
  const cand = (s3s.formula && s3s.formula.candidate) || {};
  return {
    name: cand.name || (S.role === "patient" ? $("fm-name").textContent : "自拟方"),
    source: cand.source || "composed",
    base_formula: cand.base_formula || null,
    confidence: cand.confidence || "medium",
    rationale: cand.rationale || "医师定方",
    herb_items: S.rx,
    doses_count: Number($("fm-doses").value) || null,
    usage: $("fm-usage").value || null,
  };
}

function exportCtx(format) {
  return {
    formula: currentFormula(), format, role: S.role,
    signature: (S.prefs && S.prefs.signature) || "",
    visit_date: new Date().toISOString().slice(0, 10),
    dosage_form: $("fm-form").value, usage: $("fm-usage").value,
    record_id: S.recordId, syndrome: currentSyndrome(),
    disease: (currentS3S().syndrome || {}).disease || "", method: currentMethod(),
  };
}

async function doExport(format) {
  const st = $("op-status");
  try {
    const got = await post("/api/export/formula", exportCtx(format));
    if (format === "print") { openPrintWindow(got.content); }
    else if (format === "text") { await copyText(got.content); st.textContent = "纯文本已复制"; }
    else { drawPng(got.render_model, got.filename); st.textContent = "图片已生成"; }
    setTimeout(() => { st.textContent = ""; }, 2000);
  } catch (e) { st.textContent = `导出失败：${e.message}`; }
}

function openPrintWindow(html) {
  const w = window.open("", "_blank");
  if (!w) { $("op-status").textContent = "浏览器拦住了打印窗口，请允许弹窗后重试。"; return; }
  w.document.write(html); w.document.close();
  // 等字体到位再唤起打印对话框：不等的话第一页会用回退字体排版，
  // 而"预览即所见"正是这一档要的。
  w.addEventListener("load", () => setTimeout(() => w.print(), 120));
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    // 非安全上下文（http 的院内地址）下剪贴板 API 不可用——退到选中即可复制，
    // 而不是让按钮点了没反应。
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
}

/** PNG：**服务端给 render_model，光栅化在这里做**。
 *  仓库里没有字体光栅化能力（只有 woff2 子集），而浏览器字体已经在了——
 *  内容仍由服务端一处生成，这里只负责画（理由见 core/export_render.py）。 */
function drawPng(m, filename) {
  const pad = 40; const lh = 34; const W = 760;
  const lines = [];
  if (m.notice) String(m.notice).split("\n").forEach((t) => lines.push(["notice", t]));
  lines.push(["title", m.title || ""]);
  (m.head || []).forEach((kv) => lines.push(["kv", `${kv.k}：${kv.v}`]));
  lines.push(["gap", ""]);
  (m.herbs || []).forEach((h) => lines.push(["herb", h]));
  lines.push(["gap", ""]);
  (m.meta || []).forEach((kv) => lines.push(["kv", `${kv.k}：${kv.v}`]));
  (m.notes || []).forEach((t) => lines.push(["kv", t]));
  (m.foot || []).forEach((kv) => lines.push(["kv", `${kv.k}：${kv.v}`]));
  lines.push(["gap", ""]);
  lines.push(["disc", m.disclaimer || ""]);

  const dpr = 2;                       // 拍照/转发都要看得清，2 倍够用
  const H = pad * 2 + lines.length * lh;
  const cv = document.createElement("canvas");
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext("2d");
  g.scale(dpr, dpr);
  g.fillStyle = "#fff"; g.fillRect(0, 0, W, H);
  let y = pad + lh;
  for (const [kind, v] of lines) {
    if (kind === "gap") { y += lh * 0.4; continue; }
    if (kind === "title") {
      g.fillStyle = "#1b1b1d"; g.font = '600 26px "Noto Serif SC", serif';
      g.fillText(String(v), pad, y); y += lh; continue;
    }
    if (kind === "notice") {
      g.fillStyle = "#7a1f1f"; g.font = '14px "Noto Sans SC", sans-serif';
      g.fillText(String(v), pad, y); y += lh * 0.7; continue;
    }
    if (kind === "disc") {
      g.fillStyle = "#666"; g.font = '13px "Noto Sans SC", sans-serif';
      g.fillText(String(v), pad, y); y += lh; continue;
    }
    if (kind === "herb") {
      g.fillStyle = "#1b1b1d"; g.font = '18px "Noto Serif SC", serif';
      g.fillText(v.role || "", pad, y);
      g.fillText(v.name || "", pad + 36, y);
      g.textAlign = "right"; g.fillText(v.dose || "", pad + 250, y); g.textAlign = "left";
      g.fillStyle = "#666"; g.font = '14px "Noto Sans SC", sans-serif';
      g.fillText(v.note || "", pad + 270, y);
      y += lh; continue;
    }
    g.fillStyle = "#4a4a50"; g.font = '16px "Noto Sans SC", sans-serif';
    g.fillText(String(v), pad, y); y += lh;
  }
  cv.toBlob((blob) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${filename || "处方"}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  }, "image/png");
}

async function doRecord() {
  const st = $("op-status");
  const t0 = performance.now();
  try {
    const first = ((S.result || {}).results || [])[0] || {};
    const got = await post("/api/export/record", {
      role: S.role, record_id: S.recordId, complaint: $("complaint").value,
      patient_profile: patientProfile(),
      s3: first.s3 || null, formula: currentFormula(),
      doses: Number($("fm-doses").value) || null,
      decoction: $("fm-usage").value,
      safety_flag: (S.result || {}).safety_flag || null,
    });
    await copyText(got.text);
    st.textContent = `记录已生成并复制（${Math.round(performance.now() - t0)} 毫秒）`;
  } catch (e) { st.textContent = `生成记录失败：${e.message}`; }
}

async function doSave() {
  const st = $("op-status");
  try {
    const s3s = currentS3S();
    const got = await post("/api/records", {
      record_id: S.recordId, doctor_id: "", patient_ref: $("pf-ref").value,
      complaint: $("complaint").value, syndrome: currentSyndrome(),
      disease: (s3s.syndrome || {}).disease || "", method: currentMethod(),
      formula: currentFormula().name, herb_items: S.rx,
      doses_count: Number($("fm-doses").value) || null,
      usage: $("fm-usage").value, dosage_form: $("fm-form").value,
    });
    st.textContent = "已保存到既往记录";
    await loadRecords();
    setTimeout(() => { st.textContent = ""; }, 2000);
    return got;
  } catch (e) { st.textContent = `保存失败：${e.message}`; return null; }
}

async function loadRecords() {
  if (S.role === "student") return;
  try {
    const got = await api("/api/records");
    const groups = got.groups || [];
    $("records-list").innerHTML = groups.length ? groups.map((g) => `
      <div class="rec-group">
        <div class="rec-ref">${esc(g.patient_ref || "（未填备注）")}　共 ${g.n} 诊</div>
        ${g.items.map((it) => `
          <div class="rec-item">
            ${esc((it.at || "").slice(0, 10))}　${esc(it.syndrome || "")}　${esc(it.formula || "")}
            <div class="rec-acts">
              <button class="btn-min" data-load="${esc(it.record_id)}">载入此方</button>
              <button class="btn-min" data-drop="${esc(it.record_id)}">删除</button>
            </div>
          </div>`).join("")}
      </div>`).join("") : '<p class="lc-empty">还没有保存过记录。</p>';
    S.records = groups;
  } catch (e) { $("records-list").innerHTML = `<p class="lc-empty">${esc(e.message)}</p>`; }
}

/** §6.2 复诊：载入上次那张方，标题变「复诊调方（第 N 诊）」，
 *  右栏摆出**上次用方对照**。 */
function loadPastFormula(recordId) {
  let row = null; let group = null;
  (S.records || []).forEach((g) => (g.items || []).forEach((it) => {
    if (it.record_id === recordId) { row = it; group = g; }
  }));
  if (!row) return;
  const prev = (row.herb_items || []).map((x) => ({ ...x }));
  S.rx = prev.map((x) => ({ ...x }));
  S.rxBase = prev.map((x) => ({ ...x }));
  S.followUpFrom = prev;
  $("result-zone").hidden = false;
  $("sec-formula").querySelector(".card-h").textContent =
    `⑤ 复诊调方（第 ${group.n + 1} 诊）`;
  if (row.doses_count != null) $("fm-doses").value = row.doses_count;
  if (row.usage) $("fm-usage").value = row.usage;
  if ($("pf-ref")) $("pf-ref").value = row.patient_ref || "";
  $("complaint").placeholder = "服药后症状变化、新出现的症状";
  renderRxTable();
  runRuleCheck();
  showPrevComparison();
}

function showPrevComparison() {
  const prev = S.followUpFrom || [];
  const diffs = [];
  const byName = (arr) => Object.fromEntries(arr.map((h) => [h.name, h]));
  const a = byName(prev); const b = byName(S.rx);
  Object.keys(a).forEach((n) => { if (!(n in b)) diffs.push(`去 ${n}`); });
  Object.keys(b).forEach((n) => {
    if (!(n in a)) diffs.push(`新增 ${n}${b[n].dose != null ? ` ${b[n].dose}${b[n].dose_unit || "g"}` : ""}`);
    else if (a[n].dose !== b[n].dose) diffs.push(`${n} ${a[n].dose}→${b[n].dose}`);
  });
  rcForce("上次用方对照", `
    <p>${prev.map((h) => esc(`${h.name} ${h.dose == null ? "" : `${h.dose}${h.dose_unit || "g"}`}`)).join("　")}</p>
    <p class="hint-why">${diffs.length ? `本次已改：${esc(diffs.join("；"))}` : "本次还没有改动。"}</p>`);
}

async function loadTemplates() {
  try {
    const got = await api("/api/templates");
    S.templates = got.personal || [];
    $("tpl-pop").hidden = false;
    $("tpl-pop").innerHTML = S.templates.length
      ? S.templates.map((t, i) =>
        `<button class="pop-word" data-tpl="${i}">${esc(t.name)}${t.syndrome ? `（${esc(t.syndrome)}）` : ""}</button>`).join("")
      : '<span class="lc-empty">还没有存过模板。</span>';
  } catch (e) { $("tpl-pop").hidden = false; $("tpl-pop").textContent = e.message; }
}

async function saveTemplate() {
  const name = prompt("模板名称（例如：我的疏肝方）");
  if (!name) return;
  const st = $("op-status");
  try {
    await post("/api/templates", {
      name, syndrome: currentSyndrome(), herb_items: S.rx,
      doses_count: Number($("fm-doses").value) || null, usage: $("fm-usage").value,
    });
    st.textContent = "已存为模板";
    setTimeout(() => { st.textContent = ""; }, 2000);
  } catch (e) { st.textContent = `存模板失败：${e.message}`; }
}

/* ═════════ 10. 四诊常用词 / 示例 ═════════ */

async function loadIntakeForm() {
  try {
    const got = await api("/api/intake/form");
    S.intakeParts = got.parts || {};
  } catch (e) { S.intakeParts = {}; }
}

function showFourExam(part) {
  const fields = (S.intakeParts || {})[part] || [];
  const words = fields.flatMap((f) => (f.quick_picks || []).map((w) => ({ w, label: f.label })));
  const pop = $("fx-pop");
  pop.hidden = false;
  pop.innerHTML = words.length
    ? words.map((x) => `<button class="pop-word" data-word="${esc(x.w)}">${esc(x.w)}</button>`).join("")
    : '<span class="lc-empty">这一诊法暂时没有常用词。</span>';
}

function appendWord(w) {
  const box = $("complaint");
  const sep = box.value && !/[，。；\s]$/.test(box.value) ? "，" : "";
  box.value = box.value + sep + w;
  box.focus(); checkRequired(); askIntakeHints();
}

/** §5.5：标题就是「示例」，说明只写临床含义，不出现实现术语。 */
const EXAMPLES = [
  ["肝胃气滞的典型表现", "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。"],
  ["脾胃虚寒的典型表现", "胃痛隐隐，喜温喜按，空腹痛甚，得食则缓，神疲纳呆，四肢倦怠，舌淡苔白，脉细弱。"],
  ["含消化道出血征象", "胃脘疼痛数月，近日解黑色柏油样便，头晕心慌，面色苍白，倦怠乏力，舌淡，脉细数。"],
];

function renderExamples() {
  $("ex-list").innerHTML = EXAMPLES.map(([label, text], i) => `
    <li><button class="ex-item" data-ex="${i}">
      <span class="ex-label">${esc(label)}</span>${esc(text)}
    </button></li>`).join("");
}

/* ═════════ 11. 绑定 ═════════ */

function bindGlobalClicks() {
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-term]");
    if (t) { explainTerm(t.dataset.term, t.dataset.name); return; }
    const hint = ev.target.closest("[data-hint]");
    if (hint) { adoptHint(Number(hint.dataset.hint)); return; }
    const adopt = ev.target.closest("[data-adopt]");
    if (adopt) { applyChanges((S.adviceOptions || [])[Number(adopt.dataset.adopt)].changes); return; }
    const mod = ev.target.closest("[data-mod]");
    if (mod) {
      const m = (S.mods || [])[Number(mod.dataset.mod)];
      if (m) applyChanges([{ action: m.action, item: m.item }]);
      return;
    }
    const del = ev.target.closest("[data-del]");
    if (del) { S.rx.splice(Number(del.dataset.del), 1); renderRxTable(); runRuleCheck(); askEditAdvice(); return; }
    const word = ev.target.closest("[data-word]");
    if (word) { appendWord(word.dataset.word); $("fx-pop").hidden = true; return; }
    const ex = ev.target.closest("[data-ex]");
    if (ex) {
      $("complaint").value = EXAMPLES[Number(ex.dataset.ex)][1];
      checkRequired(); askIntakeHints(); return;
    }
    const load = ev.target.closest("[data-load]");
    if (load) { loadPastFormula(load.dataset.load); return; }
    const drop = ev.target.closest("[data-drop]");
    if (drop) {
      api(`/api/records/${encodeURIComponent(drop.dataset.drop)}`, { method: "DELETE" })
        .then(loadRecords).catch(() => {});
      return;
    }
    const tpl = ev.target.closest("[data-tpl]");
    if (tpl) {
      const t2 = (S.templates || [])[Number(tpl.dataset.tpl)];
      if (t2) {
        S.rx = (t2.herb_items || []).map((x) => ({ ...x }));
        if (t2.doses_count != null) $("fm-doses").value = t2.doses_count;
        if (t2.usage) $("fm-usage").value = t2.usage;
        renderRxTable(); runRuleCheck();
      }
      $("tpl-pop").hidden = true;
      return;
    }
    const fx = ev.target.closest(".fx-btn");
    if (fx) { showFourExam(fx.dataset.part); return; }
    // 点空白处收掉浮层：浮层挡着下面的内容，而它们都不是模态的。
    if (!ev.target.closest("#fx-pop, .fx-btn")) $("fx-pop").hidden = true;
    if (!ev.target.closest("#tpl-pop, #rx-tpl")) $("tpl-pop").hidden = true;
    if (!ev.target.closest("#export-pop, #op-export")) $("export-pop").hidden = true;
    if (!ev.target.closest("#settings-panel, #btn-settings")) $("settings-panel").hidden = true;
    if (!ev.target.closest("#help-panel, #btn-help")) $("help-panel").hidden = true;
  });
}

function bindRxEditing() {
  $("rx-body").addEventListener("input", (ev) => {
    const inp = ev.target.closest("input[data-f]");
    if (!inp) return;
    const i = Number(inp.closest("tr").dataset.i);
    const f = inp.dataset.f;
    S.rx[i][f] = f === "dose" ? (inp.value === "" ? null : Number(inp.value)) : (inp.value || null);
    runRuleCheck();       // 第一层：立刻
    askEditAdvice();      // 第二层：停 1.5 秒
  });
}

function bindSettings() {
  $("btn-settings").onclick = () => {
    $("help-panel").hidden = true;
    $("settings-panel").hidden = !$("settings-panel").hidden;
    $("btn-settings").setAttribute("aria-expanded", String(!$("settings-panel").hidden));
  };
  $("btn-help").onclick = () => {
    $("settings-panel").hidden = true;
    $("help-panel").hidden = !$("help-panel").hidden;
  };
  $("set-close").onclick = () => { $("settings-panel").hidden = true; };
  $("help-close").onclick = () => { $("help-panel").hidden = true; };

  $("set-role").onchange = async (e) => {
    if (await savePreference({ role: e.target.value })) setRole(e.target.value);
  };
  $("role-select").onchange = async (e) => {
    if (await savePreference({ role: e.target.value })) setRole(e.target.value);
  };
  $("set-fontsize").onchange = async (e) => {
    // **立即生效**，不等保存结果：§4.3 的判据是"点了就有效果"。
    document.documentElement.dataset.fontsize = e.target.value;
    await savePreference({ font_size: e.target.value });
  };
  $("set-form").onchange = async (e) => {
    await savePreference({ dosage_form: e.target.value });
    refreshUsageChoices();
    $("fm-form").value = e.target.value;
    $("fm-usage").value = S.prefs.usage;
  };
  $("set-doses").onchange = async (e) => {
    if (await savePreference({ doses_count: Number(e.target.value) })) $("fm-doses").value = e.target.value;
  };
  $("set-usage").onchange = async (e) => {
    if (await savePreference({ usage: e.target.value })) $("fm-usage").value = e.target.value;
  };
  $("set-herbcount").onchange = (e) => savePreference({ herb_count_band: e.target.value });
  $("set-avoid").onchange = (e) => savePreference({ avoid_herbs: e.target.value });
  $("set-signature").onchange = (e) => savePreference({ signature: e.target.value });
}

function bindAll() {
  bindGlobalClicks();
  bindRxEditing();
  bindSettings();

  $("complaint").addEventListener("input", () => { checkRequired(); askIntakeHints(); });
  ["pf-age", "pf-sex"].forEach((id) => $(id).addEventListener("input", checkRequired));
  $("btn-go").onclick = runConsult;
  $("btn-cancel").onclick = () => { if (S.abort) S.abort.abort(); };

  $("btn-differential").onclick = () => {
    $("diff-block").hidden = !$("diff-block").hidden;
    $("btn-differential").classList.toggle("is-on", !$("diff-block").hidden);
  };
  $("rx-add").onclick = () => {
    S.rx.push({ name: "", dose: null, dose_unit: "g" });
    renderRxTable();
  };
  $("rx-tpl").onclick = loadTemplates;
  $("rx-save-tpl").onclick = saveTemplate;
  $("rx-to-lab").onclick = () => { window.TCMLab.openWith(S.rx, currentSyndrome(), currentMethod()); };
  $("op-export").onclick = () => {
    const pop = $("export-pop");
    pop.hidden = !pop.hidden;
    pop.innerHTML = `
      <button class="pop-word" data-fmt="print">可打印页面</button>
      <button class="pop-word" data-fmt="text">纯文本</button>
      <button class="pop-word" data-fmt="png">图片</button>`;
    pop.querySelectorAll("[data-fmt]").forEach((b) => {
      b.onclick = async () => {
        pop.hidden = true;
        // §8：患者导出要二次确认——这张纸会被带走，确认的是"你知道它是什么"。
        if (S.role === "patient" &&
            !confirm("导出的是《方剂学》教材中该证型的代表方及组成，不是针对您个人的处方。用药前请务必经执业中医师当面辨证确认。确定导出吗？")) return;
        if ((S.result || {}).safety_flag &&
            !confirm("本例含危重征象，导出的方须在处理急症的前提下使用。确定导出吗？")) return;
        await doExport(b.dataset.fmt);
      };
    });
  };
  $("op-record").onclick = doRecord;
  $("op-save").onclick = doSave;
  $("fm-form").onchange = () => {
    const usages = (S.choices.usage || {})[$("fm-form").value] || [];
    if (usages.length) $("fm-usage").value = usages[0];
  };
  $("fm-doses").oninput = runRuleCheck;

  $("rc-pin").onchange = (e) => { S.pinned = e.target.checked; };
  $("rc-collapse").onclick = () => {
    $("shell").classList.add("rc-off"); $("rc-expand").hidden = false;
  };
  $("rc-expand").onclick = () => {
    $("shell").classList.remove("rc-off"); $("rc-expand").hidden = true;
  };

  $("tab-consult").onclick = () => showPage("consult");
  $("tab-lab").onclick = () => showPage("lab");
  $("btn-knowledge").onclick = () => window.TCMKnowledge.open(currentSyndrome());
}

function showPage(which) {
  document.documentElement.dataset.page = which;
  $("shell").hidden = which !== "consult";
  $("lab").hidden = which !== "lab";
  $("tab-consult").classList.toggle("is-on", which === "consult");
  $("tab-lab").classList.toggle("is-on", which === "lab");
  $("tab-consult").setAttribute("aria-selected", String(which === "consult"));
  $("tab-lab").setAttribute("aria-selected", String(which === "lab"));
}

/* ═════════ 12. 启动 ═════════ */

async function boot() {
  renderExamples();
  bindAll();
  try {
    S.health = await api("/health");
    $("about-version").textContent = S.health.version || "—";
    // 免责声明与体质表由服务端给，前端不写死（§1.4 那句话是合规文本，
    // 抄一份在前端意味着改一处漏一处）。
    $("ft-disclaimer").textContent = S.health.disclaimer
      || "本系统为中医知识学习与辅助工具，不作为医疗器械管理，不提供诊断结论；所生成内容须由执业医师审核后方可用于临床。";
    $("about-disclaimer").textContent = $("ft-disclaimer").textContent;
  } catch (e) {
    $("ft-disclaimer").textContent =
      "本系统为中医知识学习与辅助工具，不作为医疗器械管理，不提供诊断结论；所生成内容须由执业医师审核后方可用于临床。";
    $("go-status").textContent = "后端还没就绪，稍候再试。";
  }
  fillSelect($("pf-const"), ["", "平和质", "气虚质", "阳虚质", "阴虚质", "痰湿质",
    "湿热质", "血瘀质", "气郁质", "特禀质"], "");
  await loadPreferences().catch(() => {});
  await loadIntakeForm();
  await loadRecords();
  checkRequired();
  rcShow("问诊要点提示",
    '<p class="rc-empty">写下患者说的症状，这里会给出接着还可以问什么。</p>');
  if (window.TCMLab) window.TCMLab.init(S);
  if (window.TCMKnowledge) window.TCMKnowledge.init(S, explainTerm);
}

window.addEventListener("DOMContentLoaded", boot);

/* 给 lab.js / knowledge.js 用的最小接口。**刻意只暴露这几个**：
 * 两个模块要的是"把方送过来/送回去"和"点了术语给我渲染"，
 * 不是整个 S。暴露 S 的话，那两份会开始直接改问诊页的状态。 */
window.TCMApp = {
  esc, post, api, term, explainTerm, rcForce, debounce,
  getRx: () => S.rx.map((x) => ({ ...x })),
  setRx: (rx, syndrome, method) => {
    S.rx = rx.map((x) => ({ ...x }));
    S.rxBase = rx.map((x) => ({ ...x }));
    if (syndrome || method) {
      S.draft = S.draft || {};
      S.draft.syndrome = { name: syndrome || currentSyndrome() };
      S.draft.method = { principle: method || currentMethod() };
    }
    $("result-zone").hidden = false;
    renderRxTable(); runRuleCheck();
  },
  getProfile: patientProfile,
  getRole: () => S.role,
  showPage,
};
