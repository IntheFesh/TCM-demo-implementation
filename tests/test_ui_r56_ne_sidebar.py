"""R56 §6：释义面板从"结果流里的一块"改成桌面端（>768px）吸顶右侧栏——
380–420px、`--t-collapse` 滑入（尊重 prefers-reduced-motion，跟 #evidence-panel
同一个展开/折叠令牌，不为这个侧栏另写一个时长）、切换不关闭、Esc 关闭、
可固定、可滚动；≤768px 仍是底部抽屉（R42 已有，未改）；1366×768 不遮挡
（`body.ne-open main` 让出同宽的右边距）。

这个文件测的是纯函数/DOM 状态判据；真实渲染（侧栏真的贴右边、真的滑入、
1366×768 真的不重叠）要 Playwright 才能验——CLAUDE.md 那条：涉及渲染结构
的改动，JSON/纯函数测试测不出这类问题。
"""
from __future__ import annotations

import json
import subprocess

from pathlib import Path

from tests.web_harness import DOM_STUB, js_tmp, load_app_js, load_css, load_html

ROOT = Path(__file__).resolve().parent.parent
APP = load_app_js()


def _run(js_tail: str) -> str:
    proc = subprocess.run(["node", js_tmp(DOM_STUB + APP + "\n" + js_tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


# ---------- CSS 判据：宽度区间、变换动画、reduced-motion、不遮挡 ----------

CSS = load_css()


def test_the_sidebar_width_is_clamped_between_380_and_420():
    assert "clamp(380px, 32vw, 420px)" in CSS


def test_desktop_breakpoint_uses_a_fixed_right_sidebar():
    assert "@media (min-width: 769px)" in CSS
    idx = CSS.index("@media (min-width: 769px)")
    block = CSS[idx:idx + 2000]
    assert "position: fixed" in block
    assert "right: 0" in block


def test_the_transition_uses_the_shared_collapse_token_not_a_hardcoded_duration():
    """§2.4 只许三处动效各自的时长写死一次（在 `:root` 的令牌定义里），
    别的规则一律引用令牌——之前这里写死 `150ms`，跟 `--t-collapse`
    （180ms，`#evidence-panel` 那种"侧栏滑入滑出"用的同一个语义）不一致，
    还会被 `test_css_tokens.py::test_no_animation_outside_the_three_tokens`
    当场抓到"规则里写死了动效时长"。"""
    assert "150ms" not in CSS
    # 有两处 `#node-explain {`：窄屏之前的 `display: none` 单行规则（第一处），
    # 桌面端 `@media` 里带 transition 的那份（第二处）——按 @media 锚点定位，
    # 不用第一次出现的下标（那会截到 display:none 那一行，永远断言不到 transition）。
    media = CSS.index("@media (min-width: 769px)")
    i = CSS.index("#node-explain {", media)
    block = CSS[i:CSS.index("}", i)]
    assert "transition: transform var(--t-collapse) ease, visibility var(--t-collapse)" in block


def test_reduced_motion_is_handled_by_the_one_existing_global_rule():
    """页面已经有一条全局规则把 `prefers-reduced-motion: reduce` 下所有元素
    的 transition 都压成瞬时（`*, *::before, *::after` + `!important`）——
    侧栏的滑入动画不用再判一次，判一次就是同一件事的第二处实现
    （CLAUDE.md「同一概念的匹配逻辑只能有一处实现」）。这里钉住的是"侧栏
    没有自己另起一份 reduced-motion 判断"，而不是"侧栏尊重它"——后者由
    那条全局规则的既有测试覆盖。"""
    assert CSS.count("prefers-reduced-motion: reduce") == 1
    idx = CSS.index("prefers-reduced-motion: reduce")
    block = CSS[idx:idx + 400]
    assert "transition-duration: .001ms !important" in block


def test_opening_the_sidebar_pushes_main_content_out_of_the_way():
    """1366×768 不遮挡：main 让出跟侧栏一样宽的右边距，同一个自定义属性，
    不是各写各的数字（写两份的话改一处会忘另一处，侧栏就盖住内容了）。"""
    assert "body.ne-open main" in CSS
    idx = CSS.index("body.ne-open main")
    block = CSS[idx:idx + 200]
    assert "var(--ne-w)" in block


def test_mobile_breakpoint_is_untouched_bottom_drawer():
    """≤768px 仍是 R42 那份底部抽屉，两个断点互斥，不该被这次改动波及。"""
    assert "@media (max-width: 768px)" in CSS
    idx = CSS.index("@media (max-width: 768px)")
    block = CSS[idx:idx + 600]
    assert "position: fixed" in block
    assert "bottom: 0" in block


# ---------- JS 判据：切换不关闭、固定、Esc、body 状态 ----------


def test_rendering_a_node_marks_the_body_as_open():
    out = json.loads(_run("""
      globalThis.document = {
        getElementById: (id) => id === "node-explain" ? {
          innerHTML: "", classList: { add(){}, remove(){}, contains(){ return true; } },
          setAttribute(){},
        } : null,
        body: { classList: { _c: new Set(), add(c){ this._c.add(c); }, remove(c){ this._c.delete(c); } } },
      };
      renderNodeExplain({title: "x", kind: "symptom", sections: []});
      process.stdout.write(JSON.stringify([...document.body.classList._c]));
    """))
    assert "ne-open" in out


def test_closing_clears_the_open_marker_and_unpins():
    out = json.loads(_run("""
      NODE_EXPLAIN_PINNED = true;
      globalThis.document = {
        getElementById: (id) => id === "node-explain" ? {
          classList: { add(){}, remove(){}, contains(){ return true; } },
          setAttribute(){}, innerHTML: "",
        } : null,
        body: { classList: { _c: new Set(["ne-open"]), add(c){ this._c.add(c); }, remove(c){ this._c.delete(c); } } },
      };
      closeNodeExplain();
      process.stdout.write(JSON.stringify({
        open: [...document.body.classList._c].includes("ne-open"),
        pinned: NODE_EXPLAIN_PINNED,
      }));
    """))
    assert out["open"] is False
    assert out["pinned"] is False


def test_pinned_panel_ignores_a_new_open_call():
    """固定之后点别的节点不该换内容——`openNodeExplain` 在钉住且面板已开着时
    直接返回，不发新请求、不重渲染。"""
    out = _run("""
      NODE_EXPLAIN_PINNED = true;
      let fetchCalled = false;
      globalThis.fetch = () => { fetchCalled = true; return Promise.reject(new Error("should not fetch")); };
      globalThis.document = {
        getElementById: (id) => id === "node-explain" ? {
          classList: { add(){}, remove(){}, contains(){ return true; } },
          innerHTML: "", setAttribute(){},
        } : null,
        body: { classList: { add(){}, remove(){} } },
      };
      openNodeExplain("elem::脾", "脾").then(() => {
        process.stdout.write(JSON.stringify({ fetchCalled }));
      });
    """)
    assert json.loads(out)["fetchCalled"] is False


def test_unpinned_panel_still_opens_normally():
    """没钉住时 `openNodeExplain` 照常发请求——上一条测试不是把它测死了。"""
    out = _run("""
      NODE_EXPLAIN_PINNED = false;
      let fetchCalled = false;
      globalThis.fetch = () => { fetchCalled = true; return Promise.resolve({
        ok: true, json: () => Promise.resolve({ available: false }),
      }); };
      globalThis.document = {
        getElementById: (id) => id === "node-explain" ? {
          classList: { add(){}, remove(){}, contains(){ return false; } },
          innerHTML: "", setAttribute(){},
        } : null,
        body: { classList: { add(){}, remove(){} } },
      };
      openNodeExplain("elem::脾", "脾").then(() => {
        process.stdout.write(JSON.stringify({ fetchCalled }));
      });
    """)
    assert json.loads(out)["fetchCalled"] is True


def test_the_pin_button_is_in_the_rendered_header():
    out = _run("""
      globalThis.document = {
        getElementById: (id) => id === "node-explain" ? {
          innerHTML: "", classList: { add(){}, remove(){}, contains(){ return true; } },
          setAttribute(){},
        } : null,
        body: { classList: { add(){}, remove(){} } },
      };
      const fake = { innerHTML: "", classList: { add(){}, remove(){} }, setAttribute(){} };
      globalThis.document.getElementById = (id) => id === "node-explain" ? fake : null;
      renderNodeExplain({title: "脾", kind: "element", sections: []});
      process.stdout.write(fake.innerHTML);
    """)
    assert 'data-ne-pin="1"' in out
    assert 'aria-pressed="false"' in out


def test_the_click_delegation_toggles_pin_state():
    src = APP
    assert 'data-ne-pin' in src
    assert "NODE_EXPLAIN_PINNED = !NODE_EXPLAIN_PINNED" in src


# ---------- HTML 判据：面板依然是同一个元素、无障碍属性还在 ----------


def test_the_panel_element_still_exists_with_its_role():
    html = load_html()
    assert 'id="node-explain"' in html
    assert 'role="dialog"' in html
