"""web/index.html 里 M8 医生模式可编辑处方表这部分前端代码的离线测试：
describeDisclaimer/blankHerbItem/rxSafetyHtml/rxRowHtml/doctorSectionHtml
几个纯函数（或"给定 DOCTOR_STATE 全局态就是纯函数"的准纯函数）。用 node
跑 index.html 里真实上线的那份 <script>，跟 tests/test_herb_grouping.py /
tests/test_hover_tooltip.py 同一个模式。

真实点击"+添加药味"/编辑单元格/导出这几步交互（含防抖校验请求真的打到
/api/prescription/validate、事件委托真的接住了动态加进来的行）留给
Playwright（见模块报告的截图）——这里测的是"给定状态，渲染出来的 HTML/
纯函数结果对不对"，不测真实 DOM 事件流转。
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
"""


def _run_node(js_tail: str) -> str:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    proc = subprocess.run(
        ["node", "-e", DOM_STUB + script + "\n" + js_tail],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


# ---------- describeDisclaimer：定位表述分模式 ----------


def test_disclaimer_doctor_mode_uses_prescription_tool_wording():
    js = 'process.stdout.write(describeDisclaimer("doctor"));'
    out = _run_node(js)
    assert "处方辅助工具" in out
    assert "医师承担全部临床责任" in out
    assert "审计日志" in out


def test_disclaimer_other_modes_keep_original_wording():
    for mode in ["researcher", "student", "patient", None]:
        js = f'process.stdout.write(describeDisclaimer({json.dumps(mode)}));'
        out = _run_node(js)
        assert out == "教学与研究用途，非诊断工具，不能替代执业医师"


# ---------- blankHerbItem ----------


def test_blank_herb_item_shape():
    js = "process.stdout.write(JSON.stringify(blankHerbItem()));"
    item = json.loads(_run_node(js))
    assert item == {
        "name": "", "dose": None, "dose_unit": "g", "processing": None,
        "decoction": None, "role": None, "function_in_formula": None,
    }


# ---------- rxSafetyHtml ----------


def _safety_html(safety) -> str:
    js = f"process.stdout.write(rxSafetyHtml({json.dumps(safety, ensure_ascii=False)}));"
    return _run_node(js)


def test_rx_safety_html_not_yet_validated_when_null():
    assert "尚未校验" in _safety_html(None)


def test_rx_safety_html_clean_shows_no_problems():
    clean = {"incompatible": [], "thermal_warning": None, "dose_violations": [],
             "decoction_missing": [], "toxic_herbs": [], "blocking": False}
    out = _safety_html(clean)
    assert "未发现问题" in out
    assert "⚠" not in out


def test_rx_safety_html_shows_incompatible_pair():
    safety = {"incompatible": [["甘草", "海藻"]], "thermal_warning": None,
              "dose_violations": [], "decoction_missing": [], "toxic_herbs": [], "blocking": True}
    out = _safety_html(safety)
    assert "配伍禁忌" in out and "甘草" in out and "海藻" in out


def test_rx_safety_html_shows_dose_violation_with_limit():
    safety = {"incompatible": [], "thermal_warning": None,
              "dose_violations": [{"herb": "附子", "dose": 20.0, "unit": "g", "limit_g": 15.0, "reason": "常用上限"}],
              "decoction_missing": [], "toxic_herbs": [], "blocking": True}
    out = _safety_html(safety)
    assert "附子" in out and "20" in out and "15" in out


def test_rx_safety_html_shows_warning_level_issues_without_blocking_styling():
    """煎法缺失/毒性提示/寒热警告是警告级，不用跟拦截级同一个红色
    safety-incompatible 类——用的是 safety-thermal（橙色）。"""
    safety = {"incompatible": [], "thermal_warning": "证型「脾胃虚寒证」属寒，但主方偏温热",
              "dose_violations": [], "decoction_missing": ["附子"], "toxic_herbs": ["附子"], "blocking": False}
    out = _safety_html(safety)
    assert "safety-incompatible" not in out
    assert out.count("safety-thermal") == 3  # 寒热 + 煎法缺失 + 毒性，各一条


# ---------- rxRowHtml：一行的可编辑单元格 ----------


def test_rx_row_html_carries_all_seven_columns():
    """任务描述原文的七列：药名/剂量/单位/炮制/煎法/作用/删除。"""
    h = {"name": "瓜蒌", "dose": 15, "dose_unit": "g", "processing": "蜜炙",
         "decoction": "先煎", "role": "君", "function_in_formula": "开胸涤痰"}
    js = f'process.stdout.write(rxRowHtml("ye_tianshi", 0, {json.dumps(h, ensure_ascii=False)}));'
    out = _run_node(js)
    assert 'value="瓜蒌"' in out
    assert 'value="15"' in out
    assert '<option value="g" selected>' in out
    assert 'value="蜜炙"' in out
    assert 'value="先煎"' in out
    assert 'value="开胸涤痰"' in out
    assert "rx-del-btn" in out
    # role（君臣佐使）不是这张表的可编辑列——任务描述原文的七列里没有它，
    # 这条断言钉住"没有悄悄多加一列"。
    assert "君" not in out


def test_rx_row_html_handles_null_dose_without_crashing_or_showing_none():
    h = {"name": "甘草", "dose": None, "dose_unit": "g", "processing": None,
         "decoction": None, "role": None, "function_in_formula": None}
    js = f'process.stdout.write(rxRowHtml("ye_tianshi", 0, {json.dumps(h, ensure_ascii=False)}));'
    out = _run_node(js)
    assert 'data-rx-field="dose" value=""' in out
    assert "None" not in out


def test_rx_row_html_escapes_herb_name():
    h = {"name": '<script>alert(1)</script>', "dose": None, "dose_unit": "g",
         "processing": None, "decoction": None, "role": None, "function_in_formula": None}
    js = f'process.stdout.write(rxRowHtml("ye_tianshi", 0, {json.dumps(h, ensure_ascii=False)}));'
    out = _run_node(js)
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


# ---------- doctorSectionHtml：只在 doctor 模式渲染 ----------


def _with_doctor_state(physician: str, state: dict, tail_js: str) -> str:
    js = f"""
    DOCTOR_STATE = {{ {json.dumps(physician)}: {json.dumps(state, ensure_ascii=False)} }};
    {tail_js}
    """
    return _run_node(js)


def _sample_state(**overrides) -> dict:
    base = dict(
        syndrome="肝胃不和证", disease="胃痛", name="柴胡疏肝散", source="classic",
        base_formula=None, confidence="high", rationale="经典方",
        doses_count=7, usage="水煎服，每日1剂",
        herb_items=[{"name": "柴胡", "dose": 9, "dose_unit": "g", "processing": None,
                     "decoction": None, "role": "君", "function_in_formula": None}],
        safety=None, exportResult=None, exportError=None, pendingOverrideReason=None,
    )
    base.update(overrides)
    return base


def test_doctor_section_html_empty_for_non_doctor_modes():
    for mode in ["researcher", "student", "patient"]:
        out = _with_doctor_state("ye_tianshi", _sample_state(),
                                 f'process.stdout.write(doctorSectionHtml("ye_tianshi", {json.dumps(mode)}));')
        assert out == ""


def test_doctor_section_html_empty_when_no_state_for_physician():
    """没有 formula_candidates 的角色（比如 patient）走不到这里，但函数
    本身要对"这位医家没有可编辑状态"这件事诚实地返回空，不是崩溃或者
    渲染一个指向 undefined 的表格。"""
    out = _with_doctor_state("ye_tianshi", _sample_state(),
                             'process.stdout.write(doctorSectionHtml("wu_jutong", "doctor"));')
    assert out == ""


def test_doctor_section_html_renders_disclaimer_and_table_for_doctor_mode():
    out = _with_doctor_state("ye_tianshi", _sample_state(),
                             'process.stdout.write(doctorSectionHtml("ye_tianshi", "doctor"));')
    assert "处方辅助工具" in out
    assert "柴胡疏肝散" in out
    assert "柴胡" in out
    assert "+ 添加药味" in out
    assert "导出处方" in out
    assert 'data-rx-add="ye_tianshi"' in out
    assert 'data-rx-export="ye_tianshi"' in out


def test_doctor_section_html_formula_level_fields_prefilled():
    out = _with_doctor_state("ye_tianshi", _sample_state(doses_count=5, usage="每日一剂"),
                             'process.stdout.write(doctorSectionHtml("ye_tianshi", "doctor"));')
    assert 'data-rx-field="doses_count" value="5"' in out
    assert 'data-rx-field="usage" value="每日一剂"' in out
