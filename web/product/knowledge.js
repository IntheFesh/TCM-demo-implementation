/* R62 §10：知识查询覆盖层。
 *
 * ## 为什么重做，判据是什么
 *
 * 旧的「图谱浏览器」回答的是"知识库里有什么"，而使用者真正会问的是三个问题：
 *   1. 这个证跟哪些证容易混淆，怎么分？
 *   2. 这个证有几种治法，各适合什么情形？
 *   3. 这味药还能用在哪、有什么类似药可替代？
 * §10.1 原话：**做不到就没有存在价值**。所以这一版的结构直接照着这三问搭：
 * 左边病位 → 中间该病位下按病性分组的证候（相邻证候之间画鉴别连线）→
 * 证候节点下挂治法与代表方；药物搜索结果带同类可替代。
 *
 * ## 为什么是覆盖层不是标签页
 *
 * §10.2：Esc 或点外部关闭，**问诊结果原样保留**。医师是在看结论的过程中
 * 顺手查一个词，查完要回到原来那一屏——一个标签页会把他挤走。
 *
 * ## 图怎么画
 *
 * cytoscape + dagre 从 `../vendor/` 懒加载（跟旧页面用的是同一份文件，
 * 不再下一份）。**分层布局而不是放射状**：放射状的那一版点开两层就成了
 * 一团线，而这里的结构本来就是"病位 → 病性 → 证候 → 治法/方"这种层级。
 *   · `nodeDimensionsIncludeLabels: true` —— 结构性地避免标签重叠，
 *     不是事后调 padding（§10.4）。
 *   · 边用 `taxi`（正交折线）不用 bezier —— 层级图上 bezier 会在层间
 *     绕出交叉，而正交折线的走向跟层级一致。
 */

(function () {
  const K = { app: null, explain: null, locus: "", stack: [], cy: null, loaded: false };
  const $ = (id) => document.getElementById(id);

  /** 病位清单。**跟 `core/elements.py::LOCATIONS` 同一套词**——
   *  那边是证素表，这里是导航，用词不一致会让点「大肠」查不到东西。 */
  const LOCI = ["肝", "胆", "心", "小肠", "脾", "胃", "肺", "大肠", "肾", "膀胱", "三焦"];

  function loadScript(src) {
    return new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = src; s.onload = res; s.onerror = () => rej(new Error(`加载不了 ${src}`));
      document.head.appendChild(s);
    });
  }

  async function ensureLibs() {
    if (K.loaded) return true;
    try {
      if (!window.cytoscape) await loadScript("../vendor/cytoscape.min.js");
      if (!window.dagre) await loadScript("../vendor/dagre/dagre.min.js");
      if (window.cytoscape && window.dagre && !window.cytoscapeDagre) {
        await loadScript("../vendor/dagre/cytoscape-dagre.js");
      }
      K.loaded = !!window.cytoscape;
      return K.loaded;
    } catch (e) { return false; }
  }

  /* ---------- 证候关系图（答问题 1 与 2） ---------- */

  async function showLocus(loc) {
    K.locus = loc;
    K.stack = [loc];
    $("kb-results").hidden = true;
    $("kb-graph").hidden = false;
    document.querySelectorAll(".kb-loc-btn").forEach((b) =>
      b.classList.toggle("is-on", b.dataset.loc === loc));
    renderCrumb();
    $("kb-graph-note").textContent = "载入中…";
    try {
      const got = await window.TCMApp.api(
        `/api/graph/search?q=${encodeURIComponent(loc)}&node_types=syndrome&limit=120`);
      const names = (got.graph.nodes || [])
        .map((n) => (n.data && n.data.label) || "").filter(Boolean);
      await drawSyndromeGraph(loc, names);
      $("kb-graph-note").textContent = names.length
        ? "相邻证候之间的连线是鉴别关系，悬停看鉴别要点；点证候展开它的治法与代表方，再点一次收起。"
        : `证候表里没有归在「${loc}」下的证候。`;
    } catch (e) { $("kb-graph-note").textContent = e.message; }
  }

  async function drawSyndromeGraph(loc, names) {
    if (!(await ensureLibs())) {
      $("kb-graph-note").textContent = "这台机器上加载不了图形库，请用上面的搜索框查。";
      return;
    }
    // 按病性分组：证型名里出现哪个病性词就归哪一组，都不含就归「其他」。
    // **这个分组是导航，不是诊断**——它只决定节点挂在哪一支下面。
    const NATURES = ["气滞", "气虚", "阳虚", "阴虚", "血虚", "血瘀", "痰", "湿", "热", "寒", "食积"];
    const groups = {};
    names.forEach((n) => {
      const nat = NATURES.find((x) => n.includes(x)) || "其他";
      (groups[nat] = groups[nat] || []).push(n);
    });
    const els = [{ data: { id: `loc:${loc}`, label: loc, kind: "locus" } }];
    Object.entries(groups).forEach(([nat, list]) => {
      els.push({ data: { id: `nat:${nat}`, label: nat, kind: "nature" } });
      els.push({ data: { id: `e:${loc}:${nat}`, source: `loc:${loc}`, target: `nat:${nat}` } });
      list.forEach((n) => {
        els.push({ data: { id: `syn:${n}`, label: n, kind: "syndrome" } });
        els.push({ data: { id: `e:${nat}:${n}`, source: `nat:${nat}`, target: `syn:${n}` } });
      });
      // 同一病性组内相邻的两个证候之间画鉴别连线（§10.3）——
      // 同组才连：不同病性的两个证不容易混，连起来只会让图变密。
      for (let i = 0; i + 1 < list.length; i += 1) {
        els.push({ data: {
          id: `d:${list[i]}:${list[i + 1]}`, source: `syn:${list[i]}`,
          target: `syn:${list[i + 1]}`, kind: "differential",
          label: `${list[i]} vs ${list[i + 1]}`,
        } });
      }
    });

    if (K.cy) { K.cy.destroy(); K.cy = null; }
    K.cy = window.cytoscape({
      container: $("kb-graph"),
      elements: els,
      style: [
        { selector: "node", style: {
          label: "data(label)", "font-family": '"Noto Serif SC", serif', "font-size": 13,
          "text-valign": "center", "text-halign": "center", shape: "round-rectangle",
          "background-color": "#fff", "border-width": 1, "border-color": "#cfcfd6",
          color: "#1b1b1d", padding: "8px", width: "label", height: "label",
        } },
        { selector: 'node[kind="locus"]', style: {
          "background-color": "#7a5b3a", color: "#fff", "border-color": "#7a5b3a",
          "font-size": 16 } },
        { selector: 'node[kind="nature"]', style: {
          "background-color": "#f2ece4", "border-color": "#e0d3c0" } },
        { selector: "edge", style: {
          width: 1, "line-color": "#d5d5db", "curve-style": "taxi",
          "taxi-direction": "downward", "target-arrow-shape": "none" } },
        { selector: 'edge[kind="differential"]', style: {
          "line-color": "#c9a24a", "line-style": "dashed", "curve-style": "bezier" } },
        { selector: ".dim", style: { opacity: 0.25 } },
      ],
      layout: dagreLayout(),
      // 标签参与布局尺寸计算：**结构性地避免重叠**，不是事后调 padding。
      wheelSensitivity: 0.2,
    });

    K.cy.on("tap", 'node[kind="syndrome"]', (ev) => toggleSyndrome(ev.target));
    K.cy.on("tap", 'node[kind="locus"], node[kind="nature"]', (ev) => {
      K.explainInSide(`syn::${ev.target.data("label")}`, ev.target.data("label"));
    });
    K.cy.on("mouseover", 'edge[kind="differential"]', (ev) => {
      const [a, b] = ev.target.data("label").split(" vs ");
      $("kb-graph-note").textContent = `${a} 与 ${b}：点任一个看它的辨证要点与鉴别点。`;
    });
  }

  function dagreLayout() {
    return window.dagre && window.cytoscape().layout
      ? { name: "dagre", rankDir: "LR", nodeDimensionsIncludeLabels: true,
          rankSep: 70, nodeSep: 12, animate: false }
      : { name: "breadthfirst", directed: true, spacingFactor: 1.2,
          nodeDimensionsIncludeLabels: true, animate: false };
  }

  /** §10.4：展开的节点**再点一次收起**。 */
  async function toggleSyndrome(node) {
    const name = node.data("label");
    const kids = K.cy.nodes(`[parentSyn = "${name}"]`);
    if (kids.length) { kids.remove(); relayout(); K.explainInSide(`syn::${name}`, name); return; }
    K.explainInSide(`syn::${name}`, name);
    try {
      const tb = await window.TCMApp.api(
        `/api/textbook_formula?syndrome=${encodeURIComponent(name)}`);
      const add = [];
      if (tb.available !== false) {
        if (tb.principle) {
          add.push({ data: { id: `m:${name}`, label: tb.principle, kind: "method", parentSyn: name } });
          add.push({ data: { id: `em:${name}`, source: `syn:${name}`, target: `m:${name}` } });
        }
        if (tb.name) {
          add.push({ data: { id: `f:${name}`, label: tb.name, kind: "formula", parentSyn: name } });
          add.push({ data: { id: `ef:${name}`, source: `syn:${name}`, target: `f:${name}` } });
        }
      }
      if (!add.length) {
        $("kb-graph-note").textContent = `${name}：教材推荐方案里暂时没有收录它的治法与代表方。`;
        return;
      }
      K.cy.add(add);
      K.cy.nodes('[kind="method"], [kind="formula"]').forEach((n) => {
        n.on("tap", () => K.explainInSide(
          `${n.data("kind") === "formula" ? "formula" : "method"}::${n.data("label")}`, n.data("label")));
      });
      relayout();
      K.stack = [K.locus, name];
      renderCrumb();
    } catch (e) { $("kb-graph-note").textContent = e.message; }
  }

  function relayout() { if (K.cy) K.cy.layout(dagreLayout()).run(); }

  /** §10.4：面包屑显示路径，可逐级回退；重置回病位列表。 */
  function renderCrumb() {
    const esc = window.TCMApp.esc;
    const parts = ["<button data-crumb=\"-1\">全部病位</button>"];
    K.stack.forEach((s, i) => parts.push(`<span>›</span><button data-crumb="${i}">${esc(s)}</button>`));
    $("kb-crumb").innerHTML = parts.join(" ");
  }

  /* ---------- 搜索（答问题 3） ---------- */

  const doSearch = (q) => {
    if (!q.trim()) { $("kb-results").hidden = true; $("kb-graph").hidden = false; return; }
    $("kb-status").textContent = "查询中…";
    window.TCMApp.api(`/api/knowledge/search?limit=8&q=${encodeURIComponent(q)}`)
      .then((got) => {
        const esc = window.TCMApp.esc;
        $("kb-graph").hidden = true;
        $("kb-results").hidden = false;
        const groups = (got.groups || []).filter((g) => g.n);
        $("kb-results").innerHTML = groups.length ? groups.map((g) => `
          <div><h4 class="kb-group-h">${esc(g.label)}</h4>
            ${g.items.map((it) => `
              <button class="kb-hit" data-kind="${esc(it.kind)}" data-title="${esc(it.title)}">
                ${esc(it.title)}<span class="kb-hit-sum">${esc(it.summary || "")}</span>
              </button>`).join("")}
          </div>`).join("") : `<p class="rc-empty">${esc(got.note || "没有查到。")}</p>`;
        $("kb-status").textContent = "";
      })
      .catch((e) => { $("kb-status").textContent = e.message; });
  };

  /** 药物结果点开时，右栏除释义外多一段「同类可替代」（§10.3 第 5 条）。 */
  async function showHerbWithAlternatives(name) {
    K.explainInSide(`herb::${name}`, name);
    try {
      const d = await window.TCMApp.api(
        `/api/node_explain?node=${encodeURIComponent(`herb::${name}`)}`);
      const sec = (d.sections || []).find((s) => s.heading === "药理");
      const eff = ((sec && sec.lines) || []).find((l) => String(l).startsWith("功效"));
      if (!eff) return;
      const key = String(eff).replace(/^功效[：:]\s*/, "").split(/[、，,]/)[0];
      if (!key) return;
      const got = await window.TCMApp.api(
        `/api/knowledge/search?kind=herb&limit=8&q=${encodeURIComponent(key)}`);
      const alts = (((got.groups || [])[0] || {}).items || [])
        .filter((it) => it.title !== name).slice(0, 6);
      if (!alts.length) return;
      const esc = window.TCMApp.esc;
      $("kb-side").insertAdjacentHTML("beforeend", `
        <div class="ne-sec"><h4>同类可替代</h4>
          ${alts.map((a) => `<p><span class="term" data-term="herb::${esc(a.title)}"
             data-name="${esc(a.title)}">${esc(a.title)}</span>
             <span class="hint-why">${esc(a.summary || "")}</span></p>`).join("")}
          <p class="ne-src">按「${esc(key)}」这一条功效检索到的同类药，差异见各自的性味归经。</p>
        </div>`);
    } catch (e) { /* 取不到就只显示释义，不报错 */ }
  }

  /* ---------- 侧栏释义（复用问诊页那一套） ---------- */

  K.explainInSide = async (id, name) => {
    const esc = window.TCMApp.esc;
    $("kb-side").innerHTML = '<p class="rc-empty">查询中…</p>';
    try {
      const d = await window.TCMApp.api(
        `/api/node_explain?node=${encodeURIComponent(id)}&name=${encodeURIComponent(name || "")}`);
      if (!d.available) {
        $("kb-side").innerHTML = `<p class="rc-empty">${esc(d.note || "查不到这一条的释义。")}</p>`;
        return;
      }
      $("kb-side").innerHTML = `<div class="ne-title">${esc(d.title)}</div>` +
        (d.sections || []).map((s) => `
          <div class="ne-sec"><h4>${esc(s.heading)}</h4>
            ${(s.lines || []).map((l) => `<p>${esc(l)}</p>`).join("")}
            ${s.source ? `<p class="ne-src">——${esc(s.source)}</p>` : ""}</div>`).join("");
    } catch (e) { $("kb-side").innerHTML = `<p class="rc-empty">${esc(e.message)}</p>`; }
  };

  /* ---------- 开关与绑定 ---------- */

  function open(syndrome) {
    $("kb-overlay").hidden = false;
    $("kb-locus").innerHTML = LOCI.map((l) =>
      `<button class="kb-loc-btn" data-loc="${l}">${l}</button>`).join("");
    // §10.5：问诊有结果时默认定位到本次证型，周围是它的鉴别证候。
    if (syndrome) {
      const loc = LOCI.find((l) => syndrome.includes(l));
      showLocus(loc || LOCI[0]).then(() => K.explainInSide(`syn::${syndrome}`, syndrome));
    } else {
      $("kb-graph-note").textContent = "点左边的病位，展开它下面的证候。";
    }
    setTimeout(() => $("kb-input").focus(), 30);
  }

  function close() {
    $("kb-overlay").hidden = true;
    // **不销毁 cy、不清空结果**：§10.2 要求关掉之后问诊结果原样保留，
    // 而下次打开时上一次查到哪儿也该还在——这两件事是同一个体验。
  }

  function bind() {
    $("kb-close").onclick = close;
    $("kb-overlay").addEventListener("click", (ev) => {
      if (ev.target === $("kb-overlay")) close();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && !$("kb-overlay").hidden) close();
    });
    $("kb-input").addEventListener("input",
      window.TCMApp.debounce((ev) => doSearch(ev.target.value), 250));
    document.addEventListener("click", (ev) => {
      const loc = ev.target.closest(".kb-loc-btn");
      if (loc) { showLocus(loc.dataset.loc); return; }
      const hit = ev.target.closest(".kb-hit");
      if (hit) {
        const kind = hit.dataset.kind; const title = hit.dataset.title;
        if (kind === "herb") showHerbWithAlternatives(title);
        else K.explainInSide(`${kind === "formula" ? "formula" : "syn"}::${title}`, title);
        return;
      }
      const crumb = ev.target.closest("[data-crumb]");
      if (crumb) {
        const i = Number(crumb.dataset.crumb);
        if (i < 0) {
          // 重置视图回到病位列表，**不是一团放射线**（§10.4）。
          K.stack = []; K.locus = "";
          if (K.cy) { K.cy.destroy(); K.cy = null; }
          $("kb-graph").innerHTML = "";
          $("kb-graph-note").textContent = "点左边的病位，展开它下面的证候。";
          document.querySelectorAll(".kb-loc-btn").forEach((b) => b.classList.remove("is-on"));
          renderCrumb();
        } else { showLocus(K.stack[0]); }
      }
    });
  }

  window.TCMKnowledge = {
    init(app, explain) { K.app = app; K.explain = explain; bind(); },
    open,
    close,
  };
}());
