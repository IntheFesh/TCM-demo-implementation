"""R56 §6 第 10 条：医师标识 → 医师工号。

三条纪律各自一节：
1. 标签文案改名（不再叫"标识"——那是给系统看的技术词，"工号"是医师自己
   认得出的说法）；
2. 格式校验（挡明显不像工号的输入，不冒充"已对接 HIS 的工号校验"）；
3. HIS/SSO 预填 + 锁定（真实部署里工号来自单点登录，不是手敲）。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js, load_html

APP = load_app_js()


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + APP + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


# ---------- 标签文案 ----------


def test_the_label_says_employee_id_not_identifier():
    html = load_html()
    assert "医师工号" in html
    assert "医师标识" not in html


def test_no_stray_reference_to_the_old_wording_anywhere_in_the_frontend():
    assert "医师标识" not in APP


# ---------- 格式校验 ----------


def test_a_realistic_employee_id_passes():
    assert _json('isValidDoctorId("dr0217")') is True


def test_too_short_is_rejected():
    assert _json('isValidDoctorId("ab")') is False


def test_empty_is_rejected():
    assert _json('isValidDoctorId("")') is False


def test_punctuation_only_is_rejected():
    """随手打的几个字符——明显不是工号，不是"用户没填"那种空值情形。"""
    assert _json('isValidDoctorId("...")') is False


def test_run_export_reports_a_format_error_for_an_invalid_but_nonempty_id():
    """`document` 是 DOM_STUB 里那个"什么都接住"的 Proxy——直接
    `document.getElementById = fn` 会被它的 set 陷阱悄悄吞掉（返回 true 但不
    存），所以这里整个替换 `globalThis.document`，不是打补丁。

    `runExport` 是 `async function`，`JSON.stringify` 不会等 Promise——所以
    这里不走 `_json`（它只是拿表达式直接 stringify），落笔挪到 `.then` 回调
    里、`runExport` 真正跑完之后。
    """
    out = _run("""
      DOCTOR_STATE.ye_tianshi = {name:"x",source:"modified",base_formula:null,
        confidence:0.5,rationale:"x",doses_count:7,usage:"x",herb_items:[],
        safety:null, safetyError:null, exportResult:null, exportError:null};
      globalThis.document = {
        getElementById: (id) => id === "doctor-id-input" ? {value: "!!"} : null,
      };
      runExport("ye_tianshi").then(() => {
        process.stdout.write(JSON.stringify(DOCTOR_STATE.ye_tianshi.exportError));
      });
    """)
    out = json.loads(out)
    assert out and "格式不对" in out["message"]


# ---------- HIS/SSO 预填 + 锁定 ----------


def test_a_doctor_id_in_the_url_prefills_and_locks_the_field():
    out = _json("""
      (() => {
        const fake = {value: "", readOnly: false, title: ""};
        globalThis.window = {location: {search: "?doctor_id=dr0217"}};
        globalThis.document = { getElementById: (id) => id === "doctor-id-input" ? fake : null };
        applyDoctorIdFromQuery();
        return fake;
      })()
    """)
    assert out["value"] == "dr0217"
    assert out["readOnly"] is True


def test_no_query_param_leaves_the_field_untouched():
    out = _json("""
      (() => {
        const fake = {value: "", readOnly: false, title: ""};
        globalThis.window = {location: {search: ""}};
        globalThis.document = { getElementById: (id) => id === "doctor-id-input" ? fake : null };
        applyDoctorIdFromQuery();
        return fake;
      })()
    """)
    assert out["value"] == ""
    assert out["readOnly"] is False
