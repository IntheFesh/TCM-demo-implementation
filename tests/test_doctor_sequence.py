"""R15：医生模式的九步交互序列（docs/DESIGN.md §3.3）。

九步一步一条断言。**这不是把一个功能测九遍**——序列里每一步都是一个独立的
失效点，而且失效之后前面几步照样正常，演示到第 120 秒才会发现。§3.3 把它
叫作"最有说服力的演示点"，一个需要连着九步都对的演示，值得九条断言。

    1 免责紫底 → 2 剂数/用法 → 3 可编辑表 → 4 改剂量 400ms 防抖校验
    → 5 甘草 + 海藻，配伍红条 160ms 滑入 → 6 含毒性药黄褐条
    → 7 点导出被拒（**按钮不禁用**）→ 8 填理由导出成功
    → 9 药房格式等宽文本 + 审计编号

其中第 4、8、9 步跨到服务端（`/api/prescription/validate`、`/api/prescription/export`），
那几条走真实 TestClient；其余是纯渲染，走 node。
"""
import json
import re
import subprocess

from fastapi.testclient import TestClient

import api.main as api_main
from tests.web_harness import DOM_STUB, js_tmp, load_app_js

APP = load_app_js()


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + APP + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _html(expr: str) -> str:
    return _run(f"process.stdout.write({expr});")


STATE_JS = """
DOCTOR_STATE.ye_tianshi = {
  name: "柴胡疏肝散加减", source: "modified", base_formula: "柴胡疏肝散",
  confidence: 0.8, rationale: "与本证相合", doses_count: 7,
  usage: "水煎服，每日1剂，分2次温服",
  herb_items: [
    {name:"柴胡",dose:6,dose_unit:"g",processing:null,decoction:null,role:"君",function_in_formula:"疏肝"},
    {name:"甘草",dose:3,dose_unit:"g",processing:null,decoction:null,role:"使",function_in_formula:"调和"},
  ],
  safety: null, safetyError: null, exportResult: null, exportError: null,
  syndrome: "肝胃不和证", disease: "胃痛", model_suggestion: {},
};
"""


# ---------- 第 1 步：免责紫底 ----------


def test_step1_the_disclaimer_is_the_first_thing_in_the_doctor_block():
    """§3.3 的框图第一行就是它。文案来自 `describeDisclaimer("doctor")` 这唯一
    一处——这里跟页头那条副标共用同一个字符串来源，不各自硬编码一遍。"""
    out = _html(f'(() => {{{STATE_JS} return doctorSectionHtml("ye_tianshi", "doctor"); }})()')
    assert out.index("doctor-disclaimer") < out.index("rx-table")
    assert "执业医师" in out and "审计" in out


def test_step1_the_disclaimer_uses_the_purple_tokens_not_a_literal_colour():
    css = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "web" / "app.css").read_text(encoding="utf-8")
    block = css[css.index(".doctor-disclaimer {"):]
    block = block[:block.index("}")]
    assert "var(--purple-bg)" in block and "var(--purple-text)" in block
    assert "#" not in block, f"免责条里写死了颜色：{block}"


# ---------- 第 2、3 步：剂数/用法 + 可编辑表 ----------


def test_step2_doses_and_usage_are_editable_fields_bound_to_the_state():
    out = _html(f'(() => {{{STATE_JS} return doctorSectionHtml("ye_tianshi", "doctor"); }})()')
    assert 'data-rx-field="doses_count"' in out and 'value="7"' in out
    assert 'data-rx-field="usage"' in out and "水煎服" in out


def test_step3_every_herb_is_a_row_with_the_seven_columns():
    """§3.3 的表头是药名/剂量/单位/炮制/煎法/作用 + 删除。少一列不会报错，
    只是医生填不了那一项——而"煎法"缺失本身就是安全层要查的东西之一。"""
    out = _html(f'(() => {{{STATE_JS} return doctorSectionHtml("ye_tianshi", "doctor"); }})()')
    for col in ("药名", "剂量", "单位", "炮制", "煎法", "作用"):
        assert f">{col}</th>" in out
    assert out.count("<tr") >= 3  # 表头 + 两味药
    assert "添加药味" in out


# ---------- 第 4 步：400ms 防抖 ----------


def test_step4_validation_is_debounced_at_400ms_not_fired_per_keystroke():
    """不是每敲一个字符就发一次请求。400ms 跟打字节奏对齐——**这个数字只写
    一处**，散成两份的话"防抖多久"这件事就没有答案了。"""
    body = APP[APP.index("function scheduleValidate"):]
    body = body[:body.index("\nasync function runValidate")]
    assert "clearTimeout(RX_VALIDATE_TIMERS[physician])" in body
    assert "400" in body


def test_step4_a_row_with_no_name_is_dropped_before_the_request():
    """刚点了"+添加药味"、名字还没填的那一行送上去会撞 `HerbItem` 的
    `Field(min_length=1)`，整个请求体校验失败，**把已经填好的其他药材的校验
    结果一起吞掉**。这是 M8 踩过的，不许退化。"""
    body = APP[APP.index("async function runValidate"):]
    body = body[:body.index("\nfunction renderDoctorExportPanel")]
    assert 'state.herb_items.filter((h) => (h.name || "").trim())' in body


# ---------- 第 5、6 步：配伍红条滑入 + 毒性药黄褐条 ----------


def test_step5_an_incompatible_pair_renders_a_red_bar_naming_both_herbs():
    safety = json.dumps({"incompatible": [["甘草", "海藻"]], "dose_violations": [],
                         "thermal_warning": None, "decoction_missing": [],
                         "toxic_herbs": [], "blocking": True}, ensure_ascii=False)
    out = _html(f"rxSafetyHtml({safety})")
    assert "safety-incompatible" in out and "甘草" in out and "海藻" in out and "反/畏" in out


def test_step5_the_red_bar_slides_in_over_the_warn_token():
    """§3.3：配伍禁忌红条**从上方滑入 160ms**（§2.4 的第三处动效）。
    为什么要动效：这条红条是在医生打字的**过程中**出现的（400ms 防抖后自动
    校验），没有位移的话它凭空出现在表格下方，而医生的视线还在表格里。

    时长必须是 `--t-warn` 令牌，不能是字面 160ms——写死的那一处不会被
    `prefers-reduced-motion` 关掉。"""
    css = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "web" / "app.css").read_text(encoding="utf-8")
    block = css[css.index(".safety-incompatible {"):]
    block = block[:block.index("\n  }")]
    assert "animation:" in block and "var(--t-warn)" in block
    assert re.search(r"--t-warn:\s*160ms", css), "--t-warn 不是 160ms"
    assert "@keyframes rx-warn-slide" in css


def test_step6_toxic_herbs_get_their_own_bar_separate_from_the_red_one():
    """毒性药材是黄褐（--caution），配伍禁忌是朱砂（--danger）。**两种颜色
    对应两个不同的问题**：一个是"这方子不能这么开"，一个是"这味药要小心用"。
    合成一条的话医生分不出哪条是拦截级的。"""
    safety = json.dumps({"incompatible": [], "dose_violations": [],
                         "thermal_warning": None, "decoction_missing": [],
                         "toxic_herbs": ["半夏"], "blocking": False}, ensure_ascii=False)
    out = _html(f"rxSafetyHtml({safety})")
    assert "safety-thermal" in out and "半夏" in out
    assert "safety-incompatible" not in out


def test_step6_a_clean_formula_says_so_in_the_verified_colour():
    """"未发现问题"走语义色 `--verified`（有出处可核），不另起一个绿。
    R15 之前这里写死 `#2f7d4f`——一个只在这一处出现的绿，没人回答得了它
    跟别处的绿是不是同一件事（总纲 §7 第 7 条：色只承担语义）。"""
    safety = json.dumps({"incompatible": [], "dose_violations": [], "thermal_warning": None,
                         "decoction_missing": [], "toxic_herbs": [], "blocking": False})
    out = _html(f"rxSafetyHtml({safety})")
    assert "rx-safety-ok" in out and "未发现问题" in out
    assert "#" not in out, f"这一行里写死了颜色：{out}"


def test_a_failed_validation_never_looks_like_a_clean_one():
    """A7：校验请求失败原来也走 `safety=null` 这条路、显示成"尚未校验"——
    网络断了、后端 500 了，跟"还没触发校验"长得一模一样。在一个安全闸门上
    这种静默是危险的。"""
    out = _html('rxSafetyHtml(null, "HTTP 500")')
    assert "没能完成" in out and '这不等于"没问题"' in out


# ---------- 第 7 步：导出被拒，按钮不禁用 ----------


def test_step7_the_export_button_is_never_disabled():
    """§3.3 加粗那句：**导出被拒绝时不要禁用按钮**——禁用按钮不告诉人为什么。
    让按钮可点、点了给出明确的拒绝原因和下一步。"""
    out = _html(f'(() => {{{STATE_JS} return doctorSectionHtml("ye_tianshi", "doctor"); }})()')
    assert "rx-export-btn" in out
    assert "disabled" not in out


def test_step7_a_blocked_export_lists_the_problems_and_asks_for_a_reason():
    """拒绝原因 + 「坚持导出的理由」输入框。这条理由会进审计日志，是"明知有
    问题仍坚持"唯一的书面记录，所以文案要说清楚这一点。"""
    out = _html('doctorExportPanelHtml({exportError: {message: "该方存在拦截级安全问题，拒绝导出。",'
                ' problems: ["配伍禁忌：甘草 反/畏 海藻"]}}, "ye_tianshi")')
    assert "拒绝导出" in out and "甘草 反/畏 海藻" in out
    assert "<textarea" in out and "记入审计日志" in out
    assert "data-rx-confirm-export" in out


# ---------- 第 8、9 步：导出成功 + 药房格式 + 审计编号 ----------


def test_step8_export_goes_through_once_a_reason_is_given():
    """服务端那一侧：blocking 的方子带 `override_reason` 才放行。前端那颗
    「坚持导出」按钮点下去走的就是这条。"""
    client = TestClient(api_main.app)
    formula = {
        "name": "试验方", "source": "composed", "base_formula": None,
        "confidence": "medium", "rationale": "测试",
        "herb_items": [
            {"name": "甘草", "dose": 3, "dose_unit": "g"},
            {"name": "海藻", "dose": 9, "dose_unit": "g"},
        ],
        "doses_count": 7, "usage": "水煎服",
    }
    payload = {"formula": formula, "doctor_id": "dr_test", "patient_ref": None,
               "model_suggestion": formula}
    blocked = client.post("/api/prescription/export", json=payload)
    # 422 是这个端点既有的契约（HTTPException(422, detail={...})），不是这一轮
    # 定的——这里只是把它钉住，不顺手改成 400：改了 DEMO.md 第 4 点和既有的
    # 导出测试都要跟着动，而那跟 R15 要做的事没有关系。
    assert blocked.status_code == 422, blocked.text
    assert "甘草" in blocked.text and "海藻" in blocked.text

    ok = client.post("/api/prescription/export",
                     json={**payload, "override_reason": "患者既往耐受，已知情同意"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["audit_id"] and body["text"]


def test_step9_the_pharmacy_text_is_monospace_and_aligned():
    """§3.3：**药房格式文本用等宽对齐**（这是唯一该用等宽字体的地方，
    因为它要对齐）。判据两条：CSS 走 `--font-mono` + `white-space: pre`，
    以及后端真的在右对齐剂量——只设字体不对齐，等宽就白用了。"""
    css = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "web" / "app.css").read_text(encoding="utf-8")
    block = css[css.index(".rx-pharmacy-text {"):]
    block = block[:block.index("\n  }")]
    assert "var(--font-mono)" in block and "white-space: pre" in block

    from core.prescription import format_pharmacy_text
    from core.schemas import FormulaCandidate, HerbItem
    text = format_pharmacy_text(FormulaCandidate(
        name="瓜蒌薤白半夏汤", source="classic", confidence="high", rationale="测试",
        herb_items=[HerbItem(name="瓜蒌", dose=20, dose_unit="g"),
                    HerbItem(name="薤白", dose=9, dose_unit="g")],
        doses_count=7, usage="水煎服，每日1剂"))
    lines = [ln for ln in text.splitlines() if "g" in ln and ("瓜蒌" in ln or "薤白" in ln)]
    assert len(lines) == 2
    assert len(lines[0]) == len(lines[1]), f"剂量没有对齐：{lines!r}"


def test_step9_the_audit_id_is_shown_with_the_pharmacy_text():
    out = _html('doctorExportPanelHtml({exportResult: {text: "瓜蒌薤白半夏汤",'
                ' audit_id: "RX-20260101-0001"}}, "ye_tianshi")')
    assert "rx-pharmacy-text" in out and "RX-20260101-0001" in out and "审计编号" in out


def test_doctor_mode_shows_every_herb_unfolded():
    """折叠是给三列并排读设计的。医生要编辑全量药味，折叠会让"下面还有几味"
    变成一次多余的点击——而漏看一味药在这里是安全问题。"""
    result = json.dumps({
        "physician": "ye_tianshi", "physician_name": "叶天士",
        "s3": {"syndrome": "肝胃不和证", "treatment_principle": "疏肝", "formula": "柴胡疏肝散",
               "herbs": [], "reasoning": "r", "cited_case_ids": [], "selected": 0,
               "formula_candidates": [{"name": "柴胡疏肝散", "rationale": "r", "herb_items": [
                   {"name": n, "role": r, "dose": 9, "dose_unit": "g"}
                   for n, r in [("柴胡", "君"), ("白芍", "臣"), ("陈皮", "佐"),
                                ("枳壳", "佐"), ("川芎", "佐"), ("甘草", "使")]]}]},
        "refs": [], "hallucinated": [],
    }, ensure_ascii=False)
    out = _html(f'(() => {{{STATE_JS} return columnHtml({result}, "doctor"); }})()')
    assert "herb-fold" not in out
    for name in ("柴胡", "白芍", "陈皮", "枳壳", "川芎", "甘草"):
        assert name in out
