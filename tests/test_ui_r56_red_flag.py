"""R56 §6 第 4 条：危重症状按角色分流的产品面——顶部红色警示条已有
（`test_describe_safety_flag`），这里测新加的三件：方剂区水印、导出二次
确认、`initDoctorState` 把 `safety_flag` 记成 `redFlag`。

EMR「危重提示」段是后端 `core/emr_writer.py::build_emr()` 的事，测试在
`tests/test_emr_writer.py`；这里只测前端怎么把 `data.safety_flag` 转成
请求体里的 `safety_flag` 字段（见 `test_emr_request_threads_the_safety_flag`）。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js, load_css

APP = load_app_js()
CSS = load_css()


# app.js 加载时会触发一次真实的 `fetch('/health')`（`initDemoModeBanner`），
# 这几条测试要整个替换 `globalThis.document`（DOM_STUB 那个 Proxy 的 set
# 陷阱会把 `document.getElementById = fn` 这种打补丁式赋值悄悄吞掉，
# 见 test_ui_r56_doctor_id.py 里同样踩过的坑），换掉之后那次异步 fetch 的
# 回调会在我们自己的同步代码跑完*之后*才触发，用的是**那时候**的
# `document`——如果只给测试关心的那几个 id 建了假元素、其余 id 一律返回
# null/纯对象，回调里 `el.classList.toggle(...)` 这类调用会在
# 我们已经拿到正确 stdout 之后才抛，让整个进程带着非零退出码收尾。
# `_stub()` 是个"什么都接得住"的假元素，未显式列出的 id 一律退到它。
_STUB_EL_JS = """
function _stub() {
  return {
    classList: { toggle(){}, add(){}, remove(){}, contains(){ return false; } },
    style: {}, dataset: {}, textContent: "", innerHTML: "",
    setAttribute(){}, getAttribute(){ return null; }, addEventListener(){},
    appendChild(){}, insertBefore(){}, remove(){},
  };
}
"""


def _run(js_tail: str) -> str:
    proc = subprocess.run(["node", js_tmp(DOM_STUB + APP + "\n" + _STUB_EL_JS + js_tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


# ---------- 方剂区水印 ----------


def test_the_watermark_is_gated_by_a_body_class_toggled_from_render_safety_flag():
    assert "body.red-flag-active #columns::before" in CSS


def test_render_safety_flag_toggles_the_body_class():
    out = _json("""
      (() => {
        const fake = { classList: { _c: new Set(),
          add(c){ this._c.add(c); }, remove(c){ this._c.delete(c); },
          toggle(c, force){ if (force) this._c.add(c); else this._c.delete(c); } } };
        globalThis.document = {
          getElementById: (id) => id === "safety-flag-note"
            ? { classList: { toggle(){} }, textContent: "" } : _stub(),
          body: fake,
        };
        renderSafetyFlag("柏油样便");
        const on = fake.classList._c.has("red-flag-active");
        renderSafetyFlag(null);
        const off = fake.classList._c.has("red-flag-active");
        return { on, off };
      })()
    """)
    assert out["on"] is True
    assert out["off"] is False


# ---------- initDoctorState 记录 redFlag ----------


def test_init_doctor_state_carries_the_safety_flag_as_red_flag():
    out = _json("""
      (() => {
        const results = [{
          physician: "ye_tianshi",
          s3: { syndrome: "x", disease: null, selected: 0, formula_candidates: [
            { name: "方x", source: "modified", herb_items: [] },
          ] },
        }];
        initDoctorState(results, "柏油样便");
        return DOCTOR_STATE.ye_tianshi.redFlag;
      })()
    """)
    assert out == "柏油样便"


def test_init_doctor_state_leaves_red_flag_null_when_clean():
    out = _json("""
      (() => {
        const results = [{
          physician: "ye_tianshi",
          s3: { syndrome: "x", disease: null, selected: 0, formula_candidates: [
            { name: "方x", source: "modified", herb_items: [] },
          ] },
        }];
        initDoctorState(results, null);
        return DOCTOR_STATE.ye_tianshi.redFlag;
      })()
    """)
    assert out is None


# ---------- 导出二次确认 ----------


def test_export_panel_shows_a_confirm_box_when_red_flag_is_set_and_unconfirmed():
    out = _run("""
      const state = {
        redFlag: "柏油样便", pendingOverrideReason: null, exportResult: null, exportError: null,
      };
      process.stdout.write(doctorExportPanelHtml(state, "ye_tianshi"));
    """)
    assert "rx-override-box" in out
    assert "柏油样便" in out
    assert 'data-rx-confirm-export="ye_tianshi"' in out
    assert 'id="rx-override-input-ye_tianshi"' in out


def test_export_panel_stops_showing_the_confirm_box_once_a_reason_is_recorded():
    out = _run("""
      const state = {
        redFlag: "柏油样便", pendingOverrideReason: "已核实，家属知情，先开方后转诊",
        exportResult: null, exportError: null,
      };
      process.stdout.write(doctorExportPanelHtml(state, "ye_tianshi"));
    """)
    assert out == ""


def test_export_panel_confirm_box_text_is_escaped():
    out = _run("""
      const state = {
        redFlag: "<script>alert(1)</script>", pendingOverrideReason: null,
        exportResult: null, exportError: null,
      };
      process.stdout.write(doctorExportPanelHtml(state, "ye_tianshi"));
    """)
    assert "<script>" not in out


def test_run_export_refuses_to_call_the_server_until_a_reason_is_given():
    """redFlag 命中、还没填理由时，`runExport` 不该真的发请求——直接把确认框
    渲染出来，等医师填了理由再点一次。"""
    out = _run("""
      (async () => {
        DOCTOR_STATE.ye_tianshi = { redFlag: "柏油样便", pendingOverrideReason: null,
          exportResult: null, exportError: null };
        let fetchCalled = false;
        globalThis.fetch = () => { fetchCalled = true; return Promise.reject(new Error("no")); };
        globalThis.document = {
          getElementById: (id) => id === `rx-export-panel-ye_tianshi` ? { innerHTML: "" } : _stub(),
        };
        await runExport("ye_tianshi");
        process.stdout.write(JSON.stringify({ fetchCalled }));
      })();
    """)
    assert json.loads(out)["fetchCalled"] is False


def test_run_export_proceeds_once_a_reason_is_recorded():
    """填了理由之后，`runExport` 应该真的往下走（发请求），不再被这道门槛
    拦住——用 fetch 有没有被调用当探针，不关心导出成功与否（那是另一条
    校验链的事）。"""
    out = _run("""
      (async () => {
        DOCTOR_STATE.ye_tianshi = {
          redFlag: "柏油样便", pendingOverrideReason: "已核实，先开方后转诊",
          exportResult: null, exportError: null, name: "x", source: "modified",
          base_formula: null, confidence: 0.5, rationale: "x", doses_count: 7,
          usage: "x", herb_items: [],
        };
        let fetchCalled = false;
        globalThis.fetch = () => { fetchCalled = true; return Promise.reject(new Error("stop here")); };
        globalThis.document = {
          getElementById: (id) => {
            if (id === "doctor-id-input") return { value: "dr0217" };
            if (id === "patient-ref-input") return { value: "" };
            return _stub();
          },
        };
        await runExport("ye_tianshi");
        process.stdout.write(JSON.stringify({ fetchCalled }));
      })();
    """)
    assert json.loads(out)["fetchCalled"] is True


# ---------- EMR 请求体透传 safety_flag ----------


def test_emr_request_threads_the_safety_flag():
    assert "safety_flag: data.safety_flag" in APP
