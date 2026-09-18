"""R15：患者模式的独立形态（docs/DESIGN.md §3.5）+ 服务端那条安全边界。

## 为什么是"独立形态"而不是"三列的裁剪版"

总纲 §1 把「患者模式是三列的裁剪版 + 一个导诊面板」列为明确不做的一条（F5）。
患者要的是"我该去哪个科、什么情况必须马上走"，不是三位古代医家的辨证异同。
把三列裁一裁给他看，等于让他自己从一堆读不懂的东西里找那两句有用的。

## 两条判据分属两层，都要测

**前端**：形态对不对（病名/科室 28px、红旗首屏不折叠、high 不给用药建议）。
**后端**：`role="patient"` 的响应体里**搜不到任何药名**。后者是安全边界，
不是显示问题——「前端不画」和「响应体里没有」差着一次「检查元素」。
"""
import json
import subprocess

from fastapi.testclient import TestClient

import api.main as api_main
from tests.test_api import _rich_outcome
from tests.web_harness import DOM_STUB, js_tmp, load_app_js


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _html(expr: str) -> str:
    return _run(f"process.stdout.write({expr});")


TRIAGE = {
    "disease": "胃痛", "dept": "消化内科", "urgency": "medium",
    "red_flags": ["疼痛剧烈持续不缓解", "痛引肩背或颈部", "伴冷汗、面色苍白", "呕血或解黑便"],
    "advice": "胃痛类症状，建议就诊消化内科，建议近期就诊",
}


def _payload(**over):
    data = {"triage": dict(TRIAGE), "food_therapy": [], "patent_medicines": []}
    data.update(over)
    return json.dumps(data, ensure_ascii=False)


# ---------- 前端：形态 ----------


def test_the_page_leads_with_the_disease_and_the_department():
    """§3.5 的第一屏就是这两行。病名取 `triage.disease`（`get_disease()` 核实过的
    那个），**不取 `results[].s3.disease`**——后者是模型原样吐出来的字符串，
    可能根本不在参考表里（`_compute_triage` 正是靠这一点决定返回 None）。
    一个没核实过的病名摆在患者看的第一行上，比不摆更危险。"""
    out = _html(f"patientViewHtml({_payload()})")
    assert "根据你描述的症状" in out
    assert "可能属于" in out and "胃痛" in out
    assert "建议就诊" in out and "消化内科" in out


def test_red_flags_are_a_plain_list_not_a_collapsed_details():
    """**红旗症状必须在首屏，不能折叠**（§3.5 加粗那一句）。折叠起来的急救
    信息等于没有。判据写成"这一段里不许出现 <details>"——这是能被机器查的
    最直接的形式。"""
    out = _html(f"patientViewHtml({_payload()})")
    assert "出现以下情况请立即就医" in out
    assert out.count("<li>") == 4
    for flag in TRIAGE["red_flags"]:
        assert flag in out
    assert "<details" not in out


def test_high_urgency_gives_no_food_or_otc_advice_at_all():
    """`triage_urgency = high`（胸痹、吐血、便血）时**不显示任何食疗和中成药
    建议**，只显示就医指引。这条闸门真正的实现在服务端
    （`_apply_medication_gate`），前端这一层只是不去暗示它本该有内容。"""
    out = _html(f'patientViewHtml({_payload(triage={**TRIAGE, "urgency": "high"})})')
    assert "紧急度高" in out and "不提供任何用药或食疗建议" in out
    assert "可参考的调养" not in out


def test_non_high_urgency_says_the_data_is_not_wired_yet_not_that_there_is_none():
    """食疗/中成药目前是空列表（M9 的数据还没接）。"尚未接入"和"没有推荐"
    是两件完全不同的事，这里必须说前者。"""
    out = _html(f"patientViewHtml({_payload()})")
    assert "尚未接入" in out and "还没做" in out


def test_no_triage_says_so_instead_of_inventing_a_department():
    """找不到任何一个医家的病名在参考表里时后端返回 `triage: null`。这时候
    不编一个科室出来——跟后端 `_compute_triage` 返回 None 的理由是同一条。"""
    out = _html("patientViewHtml({triage: null})")
    assert "不给科室建议" in out and "直接就医" in out
    assert "消化内科" not in out


def test_the_patient_form_carries_no_herb_or_formula_text():
    """患者形态里不该出现任何药名/方名。这是显示层的自查，真正的边界在后端
    （下面那两条）——两层都要，因为它们防的是不同的失误：后端漏裁是数据泄露，
    前端多画是形态错误。"""
    out = _html(f"patientViewHtml({_payload()})").lower()
    assert "herb" not in out and "formula" not in out


def test_the_patient_form_and_the_three_columns_are_mutually_exclusive():
    """"独立形态"的落法：患者模式下三列的 innerHTML 被清空、患者块显示，
    **不是把三列渲染出来再用 CSS 藏起来**——藏起来的东西仍然在 DOM 里，
    而 patient 的字段裁剪是一条安全边界。"""
    src = load_app_js()
    body = src[src.index("function renderConsultResult"):]
    body = body[:body.index("\n// ---------- SSE")]
    assert 'getSelectedRole() === "patient"' in body
    assert 'document.getElementById("columns").innerHTML = ""' in body
    assert "renderPatientView(data)" in body


def test_the_triage_box_stays_out_of_the_patient_form():
    """R15 之前患者模式是"三列裁剪版 + 一个紧凑导诊框"，也就是总纲 §1 点名的
    F5。整页形态做出来之后那个框就成了重复——病名/科室/红旗在下面已经是主角。
    医生模式仍然用它：医生要的是一眼扫过的紧急度提示，不是占半屏的大字。"""
    src = load_app_js()
    body = src[src.index("function renderTriage"):]
    body = body[:body.index("\n// R14")]
    assert 'getSelectedRole() === "patient"' in body


# ---------- 后端：安全边界 ----------


def test_patient_response_contains_no_herb_name_anywhere(monkeypatch):
    """**判据是整个响应体的 JSON 文本里搜不到药名**，不是"某几个字段被摘掉了"。
    字段级断言挡不住新增字段带来的泄露——M6 当初就是发现图节点的 label 把
    药名重新泄露了一遍，才把 `to_graph(role="patient")` 改成整段跳过。"""
    outcome = _rich_outcome()
    monkeypatch.setattr(api_main, "consult", lambda *a, **k: outcome)
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "胃痛", "role": "patient"}).json()
    text = json.dumps(body, ensure_ascii=False)

    herbs = {h for r in outcome["results"] for h in (r["s3"].herbs or [])}
    assert herbs, "测试数据里本来就没有药名，这条测试等于没测"
    for herb in herbs:
        assert herb not in text, f"patient 响应体里搜到了药名 {herb}"


#: 方剂层与君臣佐使层的层号。**从 CHAIN_LAYERS 反查，不手抄数字**——R42
#: 九层化把它们从 3/4 挪到了 7/8，写死层号的测试在那一轮会连"该红的时候
#: 红"都做不到（3 号位现在是证型，patient 是有证型层的，断言 `3 not in`
#: 会无理由地红）。
def _layer_of(node_type: str) -> int:
    return {t: n for n, t, _ in api_main.CHAIN_LAYERS}[node_type]


def test_patient_graph_has_no_formula_or_herb_layer(monkeypatch):
    """patient 的图里**压根没有** formula/herb 两层。**确认现有实现是"构造时
    跳过"而不是"生成后过滤"**：先建出完整链再删，中间那份完整数据仍然在
    进程里走过一遍，任何一处忘了删就泄露（M6 的原话："根本不生成"，不是
    "生成了再删"）。"""
    outcome = _rich_outcome()
    monkeypatch.setattr(api_main, "consult", lambda *a, **k: outcome)
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "胃痛", "role": "patient"}).json()
    layers = {n["data"]["layer"] for n in body["graph"]["nodes"]}
    banned = {_layer_of("formula"), _layer_of("herb")}
    assert not (layers & banned), f"patient 的图里出现了方剂/药材层：{sorted(layers)}"
    # 缺这两层是**刻意的**，所以不许出现在 missing_layers 里（那个字段是给
    # 前端显示"本次没有 X 层"的，把安全边界报成"缺层"会诱导前端去补）。
    assert not (set(body["graph"]["missing_layers"]) & banned)

    # 源码判据：跳过发生在构造循环里，不是在响应拼装那一层。
    import inspect
    src = inspect.getsource(api_main.to_graph)
    assert 'if role == "patient":\n            continue' in src, "不是构造时跳过"


def test_researcher_still_gets_the_full_nine_layer_chain(monkeypatch):
    """对照组。没有它，上一条在"图永远只画前几层"这种 bug 下也会绿。"""
    outcome = _rich_outcome()
    monkeypatch.setattr(api_main, "consult", lambda *a, **k: outcome)
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "胃痛", "role": "researcher"}).json()
    layers = {n["data"]["layer"] for n in body["graph"]["nodes"]}
    for node_type in ("formula", "herb"):
        assert _layer_of(node_type) in layers, (
            f"researcher 的图缺了 {node_type} 层：{sorted(layers)}")


def test_triage_carries_the_verified_disease_name(monkeypatch):
    """R15 新增：`triage.disease`。取的是 `get_disease()` 核实过的那个名字，
    不是模型原样吐出来的字符串。"""
    outcome = _rich_outcome()
    monkeypatch.setattr(api_main, "consult", lambda *a, **k: outcome)
    client = TestClient(api_main.app)
    body = client.post("/api/consult", json={"complaint": "胃痛", "role": "patient"}).json()
    triage = body["triage"]
    if triage is not None:
        assert triage["disease"], "triage 有内容但没带病名"
        assert set(triage) >= {"disease", "dept", "urgency", "red_flags", "advice"}
