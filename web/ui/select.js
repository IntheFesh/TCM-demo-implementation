/* ============================================================================
   R24：自绘下拉（`web/ui/select.js`）。

   ## 为什么自绘

   原生 `<select>` 的弹出层由操作系统画，**不吃页面的字体和颜色**：顶栏用宋体、
   弹出的选项用系统默认字体，在一屏截图里非常扎眼（这个项目的整个视觉主张
   就是"宋体说中医的话、黑体说系统的话"，而下拉是唯一一处说了别人的话的地方）。

   ## 为什么是"包在原生元素外面"而不是换掉它

   **原生 `<select>` 仍然是值的唯一来源**，自绘层只是它的一张皮：
     · 读值的地方（app.js 的 getSelectedRole 等）一行没改，仍然读 `.value`；
     · 写值之后由这里派发 `change`，既有的监听照旧收到；
     · 键盘/无障碍语义有一半是白送的（原生元素还在 DOM 里，只是视觉上藏了）。
   换掉它的话，"这个下拉现在选的是什么"会出现第二个真相（CLAUDE.md 第 31 条），
   而且所有既有测试和 Playwright 的 `select_option` 都要跟着改。

   ## 键盘契约（自绘就必须自己实现这一段）

     Enter / Space / ↓ / ↑     打开
     ↓ / ↑                     在选项间移动（打开时）
     Home / End                首项 / 末项
     Enter                     选中当前高亮项并关闭
     Esc                       关闭且不改值
     Tab / 点击外部            关闭
   ============================================================================ */

// 已经增强过的原生元素集合。**不用 dataset 标记**：dataset 会被写进 HTML 属性，
// 而这个状态是"这一次页面生命周期里已经包过了"，不该出现在 DOM 快照里。
const ENHANCED_SELECTS = new WeakSet();

// 原生元素 → 它那张皮的 refresh()。**必须有这张表**：选项是页面加载之后才由
// fetch 回来的数据填进原生元素的（图谱浏览器那两个下拉就是），
// 而自绘按钮上的字是 enhance 那一刻渲染的——中间没有人通知它，
// 屏幕上就是两个空框（R24 的 r24_rings.png 拍到的正是这个）。
const SELECT_REFRESHERS = new WeakMap();

/** 这个 <select> 现在选中项的文字。空值（"默认"那种）也如实返回它自己的文字。 */
function selectedOptionLabel(sel) {
  const opt = sel.options[sel.selectedIndex];
  return opt ? opt.textContent : "";
}

/** 自绘层的 DOM 结构。**只造 DOM，不绑事件**——绑事件在 enhanceSelect 里，
    分开是为了让这一段能被单独拿来测（node 里只有 DOM 桩，绑不了真事件）。 */
function customSelectMarkup(sel) {
  return {
    button: {
      class: "cs-button",
      type: "button",
      role: "combobox",
      "aria-haspopup": "listbox",
      "aria-expanded": "false",
      // 原生 label 指向的是原生 select，自绘按钮要自己说明它是什么。
      "aria-label": sel.getAttribute("aria-label") || labelTextFor(sel),
    },
    list: {class: "cs-list", role: "listbox", hidden: true},
  };
}

/** 找 `<label for=…>` 的文字。找不到返回空串——**不编一个**：
    空的 aria-label 比一个猜出来的名字好（读屏软件会念元素类型兜底）。 */
function labelTextFor(sel) {
  if (!sel.id) return "";
  const label = document.querySelector(`label[for="${sel.id}"]`);
  return label ? label.textContent.trim() : "";
}

/** 下一个高亮下标。**跳过 disabled 项**，到头不回绕——回绕会让"按住 ↓"
    在最后一项和第一项之间跳，用户以为列表还没到底。 */
function nextEnabledIndex(sel, from, delta) {
  let i = from;
  for (;;) {
    const j = i + delta;
    if (j < 0 || j >= sel.options.length) return i;
    if (!sel.options[j].disabled) return j;
    i = j;
  }
}

/** 键盘事件 → 动作名。**纯函数**，这样键盘契约能在 node 里逐键断言，
    不需要真浏览器（真浏览器那一层由 Playwright 兜）。 */
function selectKeyAction(key, isOpen) {
  if (!isOpen) {
    if (key === "Enter" || key === " " || key === "ArrowDown" || key === "ArrowUp") return "open";
    return null;
  }
  switch (key) {
    case "Escape": return "close";
    case "Enter": case " ": return "commit";
    case "ArrowDown": return "next";
    case "ArrowUp": return "prev";
    case "Home": return "first";
    case "End": return "last";
    case "Tab": return "close";
    default: return null;
  }
}

/** 别处用代码改了这个下拉的**选项或 hidden** 之后，把自绘层重画一遍。

    名字叫 `refreshSelect` 不叫 `refresh`：这几个脚本共用一个全局作用域
    （见 tests/web_harness.py 的说明），一个叫 `refresh` 的全局函数早晚会撞上。

    **没被增强过就返回 false，不报错**：调用方（graph.js 的两个 populate）
    不该先判断"这个下拉被包过没有"——那种判断漏写一处，症状又是一个空按钮。 */
function refreshSelect(sel) {
  const refresh = sel && SELECT_REFRESHERS.get(sel);
  if (!refresh) return false;
  refresh();
  return true;
}

function enhanceSelect(sel) {
  if (!sel || ENHANCED_SELECTS.has(sel)) return null;
  ENHANCED_SELECTS.add(sel);

  const spec = customSelectMarkup(sel);
  const wrap = document.createElement("div");
  wrap.className = "cs-wrap";
  const button = document.createElement("button");
  for (const [k, v] of Object.entries(spec.button)) {
    if (k === "class") button.className = v;
    else if (k === "type") button.type = v;
    else button.setAttribute(k, v);
  }
  const list = document.createElement("div");
  list.className = spec.list.class;
  list.setAttribute("role", "listbox");
  list.hidden = true;

  let highlighted = sel.selectedIndex < 0 ? 0 : sel.selectedIndex;

  function renderButton() {
    button.textContent = selectedOptionLabel(sel);
  }

  /** 重画按钮文字；列表开着就连列表一起重画；**并且把 hidden 同步到壳子上**。

      hidden 这一条不是顺手加的：`populateGbCategorySelect` 在"一个门类都没有"
      时把原生元素 `hidden = true`，而原生元素本来就 `opacity: 0` 看不见——
      真正要藏的是壳子。不同步的话页面上会留下一个点开也没内容的空下拉。 */
  function refresh() {
    renderButton();
    if (!list.hidden) renderList();
    wrap.hidden = !!sel.hidden;
  }

  function renderList() {
    list.textContent = "";
    Array.from(sel.options).forEach((opt, i) => {
      const row = document.createElement("div");
      row.className = "cs-option";
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(i === sel.selectedIndex));
      if (opt.disabled) row.setAttribute("aria-disabled", "true");
      if (i === highlighted) row.classList.add("is-highlighted");
      row.textContent = opt.textContent;
      row.addEventListener("mousedown", (e) => {
        // mousedown 而不是 click：click 之前会先触发 button 的 blur，
        // 那时列表已经关了，click 落在空气上——这是自绘下拉最常见的一个 bug。
        e.preventDefault();
        if (!opt.disabled) commit(i);
      });
      list.appendChild(row);
    });
  }

  function open() {
    highlighted = sel.selectedIndex < 0 ? 0 : sel.selectedIndex;
    renderList();
    list.hidden = false;
    button.setAttribute("aria-expanded", "true");
  }

  function close() {
    list.hidden = true;
    button.setAttribute("aria-expanded", "false");
  }

  function commit(i) {
    if (i !== sel.selectedIndex) {
      sel.selectedIndex = i;
      // **值写回原生元素之后派发 change**：既有的 addEventListener("change") 全都
      // 挂在原生元素上，不派发的话页面看起来变了、实际什么都没发生。
      sel.dispatchEvent(new Event("change", {bubbles: true}));
    }
    renderButton();
    close();
    button.focus();
  }

  function move(action) {
    if (action === "next") highlighted = nextEnabledIndex(sel, highlighted, 1);
    else if (action === "prev") highlighted = nextEnabledIndex(sel, highlighted, -1);
    else if (action === "first") highlighted = nextEnabledIndex(sel, -1, 1);
    else if (action === "last") highlighted = nextEnabledIndex(sel, sel.options.length, -1);
    renderList();
  }

  button.addEventListener("click", () => (list.hidden ? open() : close()));
  button.addEventListener("keydown", (e) => {
    const action = selectKeyAction(e.key, !list.hidden);
    if (!action) return;
    if (action !== "close" || e.key !== "Tab") e.preventDefault();
    if (action === "open") open();
    else if (action === "close") close();
    else if (action === "commit") commit(highlighted);
    else move(action);
  });
  // 点击外部关闭。挂在 document 上而不是 button 的 blur：blur 在点击列表项时
  // 也会触发（见上面 mousedown 那条注释）。
  document.addEventListener("mousedown", (e) => {
    if (!list.hidden && !wrap.contains(e.target)) close();
  });
  // 别处用代码改了值（比如 app.js 从 localStorage 恢复角色）时按钮要跟着变。
  sel.addEventListener("change", renderButton);

  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  wrap.appendChild(button);
  wrap.appendChild(list);
  // 原生元素留在 DOM 里（它是值的唯一来源，Playwright 的 select_option 也要它），
  // 只是视觉上藏起来。**不用 display:none**：那样 Playwright 会判定元素不可交互。
  sel.classList.add("cs-native");
  SELECT_REFRESHERS.set(sel, refresh);
  refresh();
  return {wrap, button, list, refresh};
}

/** 把页面上所有带 `data-custom-select` 的原生下拉包一层。
    **用属性选中而不是写死 id 列表**：图谱浏览器那两个下拉是后来加的，
    写死 id 的话新加的下拉不会被增强，而"少了一个"在截图里很难发现。 */
function enhanceAllSelects(root) {
  const scope = root || document;
  const out = [];
  for (const sel of scope.querySelectorAll("select[data-custom-select]")) {
    const made = enhanceSelect(sel);
    if (made) out.push(made);
  }
  return out;
}
