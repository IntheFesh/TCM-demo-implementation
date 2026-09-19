/* R62 §9：组方实验室。
 *
 * ## 它跟问诊不是同一条路
 *
 * 问诊是**从主诉出发**推到方；这里是**从治法或证型出发**，试着组一个方
 * 再检验它站不站得住。用户明确提过而此前一次都没做。学生也用它练组方。
 *
 * ## §9.4：共用的部分一份实现
 *
 * 释义、核查规则、模板、用药习惯**全部复用问诊页那一套**——
 * `window.TCMApp` 暴露的那几个函数就是复用的接口。这里只实现这一页独有的
 * 三件事：从证型带出病位病性治法、从经典方起手、以及 `[检验组方]` 的渲染。
 *
 * 核查那一栏走的是同一个 `/api/formula/check`（§9.4 原话："核查规则复用
 * 同一套实现，不另起一份"）——所以同一张方在两页上得到的红条逐字相同。
 */

(function () {
  const L = { syndromes: [], rx: [], app: null };
  const $ = (id) => document.getElementById(id);

  /** 证候表：从知识查询那一份取，不再单独拉一次。 */
  async function loadSyndromes() {
    try {
      const got = await window.TCMApp.api("/api/graph?node_types=syndrome&limit=400");
      L.syndromes = (got.graph.nodes || [])
        .map((n) => (n.data && n.data.label) || "")
        .filter(Boolean).sort();
    } catch (e) { L.syndromes = []; }
    const sel = $("lab-syndrome");
    sel.innerHTML = '<option value="">—</option>' +
      L.syndromes.map((s) => `<option value="${window.TCMApp.esc(s)}">${window.TCMApp.esc(s)}</option>`).join("");
  }

  /** §9.3 第 1 条：选证型自动带出该证的病位病性与教材治法。 */
  async function onSyndromeChange() {
    const syn = $("lab-syndrome").value;
    if (!syn) { $("lab-locus").textContent = "—"; $("lab-nature").textContent = "—"; return; }
    try {
      const d = await window.TCMApp.api(
        `/api/node_explain?node=${encodeURIComponent(`syn::${syn}`)}`);
      // 病位病性写在「病机」那一节里（core/node_explain.py 的第 2 节）。
      const sec = (d.sections || []).find((s) => s.heading === "病机");
      const lines = (sec && sec.lines) || [];
      $("lab-locus").textContent = pick(lines, "病位") || "—";
      $("lab-nature").textContent = pick(lines, "病性") || "—";
    } catch (e) { /* 取不到就留破折号，不编 */ }
    try {
      const tb = await window.TCMApp.api(
        `/api/textbook_formula?syndrome=${encodeURIComponent(syn)}`);
      if (tb.available && tb.principle && !$("lab-method").value) {
        $("lab-method").value = tb.principle;
      }
    } catch (e) { /* 同上 */ }
  }

  function pick(lines, key) {
    const hit = lines.find((l) => String(l).startsWith(key));
    return hit ? String(hit).slice(key.length).replace(/^[：:]\s*/, "") : "";
  }

  /** §9.3 第 2 条：从经典方起手——调方剂本体里的一张方当底子。 */
  async function fromClassic() {
    const q = $("lab-syndrome").value || $("lab-method").value;
    const pop = $("lab-classic-pop");
    pop.hidden = false;
    pop.innerHTML = '<span class="lc-empty">检索中…</span>';
    try {
      const got = await window.TCMApp.api(
        `/api/knowledge/search?kind=formula&limit=12&q=${encodeURIComponent(q || "汤")}`);
      const items = ((got.groups || [])[0] || {}).items || [];
      pop.innerHTML = items.length
        ? items.map((it, i) => `<button class="pop-word" data-classic="${i}">${window.TCMApp.esc(it.title)}</button>`).join("")
        : `<span class="lc-empty">${window.TCMApp.esc(got.note || "没有查到可用的经典方。")}</span>`;
      L.classicHits = items;
    } catch (e) { pop.innerHTML = `<span class="lc-empty">${window.TCMApp.esc(e.message)}</span>`; }
  }

  async function adoptClassic(i) {
    const hit = (L.classicHits || [])[i];
    if (!hit) return;
    $("lab-classic-pop").hidden = true;
    try {
      const d = await window.TCMApp.api(
        `/api/node_explain?node=${encodeURIComponent(`formula::${hit.title}`)}`);
      const sec = (d.sections || []).find((s) => s.heading === "是什么");
      const compo = pick((sec && sec.lines) || [], "组成");
      // 组成是「柴胡 12g、白芍 9g」这种文本：**只取药名**，剂量留空让人自己定。
      // 教材里的剂量是原方的，照抄进一张正在改的方里会让人以为那是这一次的
      // 判断——而剂量恰恰是最该由医师决定的那一项。
      L.rx = compo.split(/[、，,；;]/).map((s) => s.replace(/[\d.]+\s*(g|克|钱|两|分|枚|片)?/g, "").trim())
        .filter(Boolean).map((name) => ({ name, dose: null, dose_unit: "g" }));
      render();
      check();
    } catch (e) { $("lab-check").innerHTML = `<span class="chk-warn">${window.TCMApp.esc(e.message)}</span>`; }
  }

  function render() {
    const esc = window.TCMApp.esc;
    $("lab-body").innerHTML = L.rx.map((h, i) => `
      <tr data-i="${i}">
        <td class="rx-role">${esc(h.role || "")}</td>
        <td class="rx-name"><input type="text" value="${esc(h.name || "")}" data-f="name" placeholder="药名"></td>
        <td class="rx-dose"><input type="number" step="0.5" min="0"
            value="${h.dose == null ? "" : esc(h.dose)}" data-f="dose"></td>
        <td class="rx-unit">g</td>
        <td class="rx-proc"><input type="text" placeholder="炮制" value="${esc(h.processing || "")}" data-f="processing"></td>
        <td class="rx-dec"><input type="text" placeholder="煎法" value="${esc(h.decoction || "")}" data-f="decoction"></td>
        <td><button class="rx-del" data-labdel="${i}" title="去掉这一味">✕</button></td>
      </tr>`).join("") || '<tr><td colspan="7" class="lc-empty">还没有药。点「+ 加味」或「从经典方开始」。</td></tr>';
  }

  function profile() {
    const age = $("lab-age").value;
    return {
      age_years: age === "" ? null : Number(age),
      sex: $("lab-sex").value || null,
      life_stage: $("lab-stage").value || null,
    };
  }

  /** 第一层规则核查。**跟问诊页同一个端点**，所以同一张方两页的红条一致。 */
  async function check() {
    if (!L.rx.filter((h) => h.name).length) { $("lab-check").innerHTML = ""; return; }
    try {
      const got = await window.TCMApp.post("/api/formula/check", {
        herb_items: L.rx.filter((h) => h.name),
        syndrome: $("lab-syndrome").value, patient_profile: profile(),
        role: window.TCMApp.getRole(),
      });
      const esc = window.TCMApp.esc;
      const bad = []; const warn = [];
      (got.incompatible || []).forEach((p) => bad.push(`${p[0]} 与 ${p[1]} 同用，属十八反十九畏`));
      (got.dose_violations || []).forEach((v) =>
        bad.push(`${v.herb} ${v.dose}${v.unit} 超过常用上限 ${v.limit_g}g`));
      ((got.individualization && got.individualization.items) || []).forEach((it) =>
        bad.push(`${it.target}：${it.adjustment}`));
      (got.advice || []).forEach((a) =>
        (a.severity === "blocking" ? bad : warn).push(a.reason));
      if (got.thermal_warning) warn.push(got.thermal_warning);
      $("lab-check").innerHTML = [
        ...bad.map((t) => `<span class="chk-bad">⚠ ${esc(t)}</span>`),
        ...warn.map((t) => `<span class="chk-warn">· ${esc(t)}</span>`),
        ...(bad.length ? [] : [`<span class="chk-ok">✓ 配伍与剂量核查通过</span>`]),
      ].join("");
    } catch (e) {
      $("lab-check").innerHTML = `<span class="chk-warn">${window.TCMApp.esc(e.message)}</span>`;
    }
  }

  /** §9.3 第 3 条：`[检验组方]` ≤8 秒。 */
  async function verify() {
    const esc = window.TCMApp.esc;
    const herbs = L.rx.filter((h) => h.name);
    if (!herbs.length) {
      $("lab-rc-body").innerHTML = '<p class="rc-empty">先加几味药再检验。</p>';
      return;
    }
    $("lab-rc-body").innerHTML = '<p class="rc-empty">检验中…</p>';
    try {
      const got = await window.TCMApp.post("/api/compose/verify", {
        herb_items: herbs, syndrome: $("lab-syndrome").value,
        principle: $("lab-method").value, patient_profile: profile(),
        role: window.TCMApp.getRole(),
      });
      L.gaps = got.gaps || [];
      const roles = (got.roles || []).map((r) =>
        `<p><b>${esc(r.role || "未定")}</b>　${esc(r.herb)}　<span class="hint-why">${esc(r.reason)}</span></p>`).join("");
      const cover = (got.method_coverage || []).map((c) => `
        <p>${c.herbs && c.herbs.length ? "✓" : "⚠"} ${esc(c.keyword)}　
          <span class="hint-why">${c.herbs && c.herbs.length ? esc(c.herbs.join("、")) : "无药对应"}——${esc(c.reason)}</span></p>`).join("");
      const gaps = (got.gaps || []).map((g, i) => `
        <div class="advice-opt">
          <span class="advice-cond">${esc(g.what)}</span>
          ${(g.changes || []).length ? `<button class="btn-min" data-gap="${i}">采纳</button>` : ""}
          <div class="ne-src">${esc(g.basis)}</div>
        </div>`).join("");
      // 冲突那一栏**摆的是规则层的结论**，不是模型说的（见
      // core/assist.py::compose_verify 的文档字符串）。
      const conflicts = (got.rule_findings || []).map((f) =>
        `<p class="${f.severity === "blocking" ? "chk-bad" : "chk-warn"}">${esc(f.reason)}</p>`).join("");
      $("lab-rc-body").innerHTML = `
        ${got.summary ? `<p>${esc(got.summary)}</p>` : ""}
        ${got.timed_out ? '<p class="chk-warn">分析超时了，下面的规则核查仍然有效。</p>' : ""}
        ${roles ? `<div class="ne-sec"><h4>君臣佐使</h4>${roles}</div>` : ""}
        ${cover ? `<div class="ne-sec"><h4>对治法的覆盖</h4>${cover}</div>` : ""}
        ${gaps ? `<div class="ne-sec"><h4>缺什么</h4>${gaps}</div>` : ""}
        ${conflicts ? `<div class="ne-sec"><h4>冲突</h4>${conflicts}</div>` : ""}`;
    } catch (e) {
      $("lab-rc-body").innerHTML = `<p class="rc-empty">${esc(e.message)}</p>`;
    }
  }

  function applyGap(i) {
    const g = (L.gaps || [])[i];
    if (!g) return;
    (g.changes || []).forEach((c) => {
      const it = c.item || {};
      const at = L.rx.findIndex((h) => h.name === it.name);
      if (c.action === "减") { if (at >= 0) L.rx.splice(at, 1); return; }
      if (at >= 0) L.rx[at] = { ...L.rx[at], ...it };
      else L.rx.push({ dose_unit: "g", ...it });
    });
    render(); check();
  }

  function bind() {
    $("lab-syndrome").onchange = onSyndromeChange;
    $("lab-add").onclick = () => { L.rx.push({ name: "", dose: null, dose_unit: "g" }); render(); };
    $("lab-classic").onclick = fromClassic;
    $("lab-verify").onclick = verify;
    $("lab-to-consult").onclick = () => {
      window.TCMApp.setRx(L.rx.filter((h) => h.name), $("lab-syndrome").value, $("lab-method").value);
      window.TCMApp.showPage("consult");
    };
    $("lab-body").addEventListener("input", (ev) => {
      const inp = ev.target.closest("input[data-f]");
      if (!inp) return;
      const i = Number(inp.closest("tr").dataset.i);
      const f = inp.dataset.f;
      L.rx[i][f] = f === "dose" ? (inp.value === "" ? null : Number(inp.value)) : (inp.value || null);
      check();
    });
    document.addEventListener("click", (ev) => {
      const del = ev.target.closest("[data-labdel]");
      if (del) { L.rx.splice(Number(del.dataset.labdel), 1); render(); check(); return; }
      const cls = ev.target.closest("[data-classic]");
      if (cls) { adoptClassic(Number(cls.dataset.classic)); return; }
      const gap = ev.target.closest("[data-gap]");
      if (gap) { applyGap(Number(gap.dataset.gap)); }
    });
  }

  window.TCMLab = {
    init(app) { L.app = app; bind(); loadSyndromes(); render(); },
    /** 问诊页的 `[送入组方实验室]`。 */
    openWith(rx, syndrome, method) {
      L.rx = (rx || []).map((x) => ({ ...x }));
      if (syndrome) $("lab-syndrome").value = syndrome;
      if (method) $("lab-method").value = method;
      window.TCMApp.showPage("lab");
      render(); check();
    },
  };
}());
