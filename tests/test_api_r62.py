"""R62 §12：产品面补齐的那批端点。

全部离线：只有编辑助手与组方检验两条会调模型，这份测试里它们走 fake 后端
（`conftest.py` 的默认），断言的是**外壳行为**——角色 404、规则层结论无论
模型成不成功都带出去、超时不报错——不是模型说了什么。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import history
from core import preferences as prefs


@pytest.fixture
def client():
    return TestClient(api_main.app)


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    """不碰仓库里的 `data/*.jsonl`。三份存储的路径都是模块级常量，
    逐个改掉——漏一份的话测试会在开发者的 data/ 里留下文件，
    而那个文件会被下一次测试读到。"""
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "h.jsonl")
    monkeypatch.setattr(history, "FAVORITES_PATH", tmp_path / "f.jsonl")
    monkeypatch.setattr(prefs, "PREFERENCES_PATH", tmp_path / "p.jsonl")


def _herb(name, dose=9.0, **kw):
    return {"name": name, "dose": dose, "dose_unit": "g", **kw}


# ---------- §12 第 2 项：规则核查 ----------


def test_formula_check_is_pure_rules_and_flags_an_incompatible_pair(client):
    """§7.3 第一层：十八反命中要出红条。这条路上没有任何模型调用。"""
    r = client.post("/api/formula/check", json={
        "herb_items": [_herb("甘草"), _herb("海藻")], "syndrome": "肝胃不和证"})
    assert r.status_code == 200
    body = r.json()
    assert body["blocking"] is True
    assert any("甘草" in "".join(pair) for pair in body["incompatible"])


def test_formula_check_warns_when_the_patient_is_pregnant(client):
    """§5.2 的判据：填了妊娠而方中有妊娠禁忌药必须报警——这是验证
    "人这一维真的接进去了"的那一条。"""
    r = client.post("/api/formula/check", json={
        "herb_items": [_herb("桃仁")], "syndrome": "血瘀证",
        "patient_profile": {"life_stage": "妊娠期", "age_years": 30, "sex": "女"}})
    body = r.json()
    items = body["individualization"]["items"]
    assert any("桃仁" in i["target"] for i in items), "妊娠禁忌没有被报出来"
    assert all(i["basis"] for i in items), "每条调整都要说得出依据"


def test_formula_check_never_hands_a_patient_the_per_herb_warnings(client):
    """个体化提示逐条点名药味，跟 formula_candidates 是同一条安全边界——
    患者角色下这个键**根本不存在**，不是存在但为空。"""
    r = client.post("/api/formula/check", json={
        "herb_items": [_herb("桃仁")], "syndrome": "血瘀证", "role": "patient",
        "patient_profile": {"life_stage": "妊娠期"}})
    assert "individualization" not in r.json()


def test_an_empty_formula_is_not_an_error(client):
    """可编辑处方表从空表开始，医师删到 0 味时前端仍会调一次校验。"""
    r = client.post("/api/formula/check", json={"herb_items": [], "syndrome": ""})
    assert r.status_code == 200 and r.json()["n_herbs"] == 0


# ---------- §12 第 3 项：编辑助手 ----------


def test_advise_computes_the_diff_server_side(client):
    """不让前端传 diff：`compute_herb_diffs` 已经处理了"按药名配对而不是
    按下标配对"这个坑，前端自己算一份就是第二处实现。"""
    r = client.post("/api/formula/advise", json={
        "before": [_herb("柴胡", 10.0)],
        "after": [_herb("柴胡", 10.0), _herb("黄连", 6.0)],
        "syndrome": "肝胃不和证", "principle": "疏肝理气"})
    assert r.status_code == 200
    assert any("黄连" in d for d in r.json()["diff"])


def test_advise_carries_the_rule_layer_out_even_if_the_model_path_fails(client):
    """模型超时了右栏空着，但处方表下面那条红字照样要显示。"""
    r = client.post("/api/formula/advise", json={
        "before": [_herb("甘草")],
        "after": [_herb("甘草"), _herb("海藻")], "syndrome": "痰核"})
    body = r.json()
    assert body["rule_check"]["blocking"] is True
    assert "ok" in body and "timed_out" in body, "成功/超时/出错要分得开"


def test_advise_is_not_offered_to_a_patient(client):
    """§8 那张表：患者角色没有「AI 编辑提示」这一行——方本来就不可编辑。
    404 而不是返回空：一个点了没反应的按钮比一个明确说没有这项功能的
    404 更难查。"""
    r = client.post("/api/formula/advise", json={
        "before": [], "after": [_herb("柴胡")], "role": "patient"})
    assert r.status_code == 404


# ---------- §12 第 4 项：问诊要点 ----------


def test_intake_hints_are_zero_llm_and_explain_why_each_question_matters(client):
    """§5.4：每条要说明为什么问。判据走信息增益，不是让模型凭记忆想
    ——每条提示背后要有可追溯的依据。"""
    r = client.post("/api/intake/hints", json={
        "text": "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。"})
    body = r.json()
    assert body["used_llm"] is False
    assert 1 <= len(body["hints"]) <= 5
    for h in body["hints"]:
        assert h["ask"] and h["why"], "每条提示都要说明为什么问"


def test_intake_hints_on_an_empty_complaint_say_so_instead_of_guessing(client):
    body = client.post("/api/intake/hints", json={"text": "   "}).json()
    assert body["hints"] == [] and body["note"]


def test_intake_hints_stay_well_inside_the_three_second_budget(client):
    """§11 那张表给的是 3 秒。它零 LLM，所以这个预算本来就该有数量级的余量
    ——真的贴着 3 秒说明它偷偷调了什么。"""
    body = client.post("/api/intake/hints", json={"text": "胃脘胀痛，脉弦"}).json()
    assert body["elapsed_s"] < body["budget_s"]


# ---------- §12 第 5 项：组方检验 ----------


def test_compose_verify_returns_the_rule_findings_alongside_the_model_analysis(client):
    """§9.3 的冲突那一栏由规则层给，不问模型——同一张方规则层的结论是
    确定的，换个模型也不变。"""
    r = client.post("/api/compose/verify", json={
        "herb_items": [_herb("甘草"), _herb("海藻")],
        "syndrome": "痰核", "principle": "化痰散结"})
    body = r.json()
    assert "rule_findings" in body
    assert any(f["kind"] == "incompatible" for f in body["rule_findings"])


def test_compose_verify_is_not_offered_to_a_patient(client):
    r = client.post("/api/compose/verify", json={"herb_items": [], "role": "patient"})
    assert r.status_code == 404


# ---------- §12 第 6 项：导出 ----------


def _formula():
    return {"name": "柴胡疏肝散加减", "source": "modified", "base_formula": "柴胡疏肝散",
            "confidence": "high", "rationale": "疏肝理气", "doses_count": 7,
            "herb_items": [_herb("柴胡", 10.0), _herb("白芍", 9.0)]}


@pytest.mark.parametrize("fmt", ["print", "text", "png"])
def test_all_three_export_formats_come_from_one_render_model(client, fmt):
    """三种格式同源。PNG 这一档没有 content——它的"内容"就是 render_model，
    由浏览器 canvas 光栅化（服务端没有字体光栅化能力，见 core/export_render.py）。"""
    r = client.post("/api/export/formula", json={"formula": _formula(), "format": fmt})
    body = r.json()
    assert body["render_model"]["title"] == "柴胡疏肝散加减"
    assert (("content" in body) is (fmt != "png"))


def test_the_patient_export_carries_the_mandatory_notice(client):
    body = client.post("/api/export/formula", json={
        "formula": _formula(), "format": "text", "role": "patient"}).json()
    assert "非针对您个人的处方" in body["content"]


def test_generating_a_record_is_doctor_only(client):
    assert client.post("/api/export/record", json={"role": "student"}).status_code == 404
    r = client.post("/api/export/record", json={
        "role": "doctor", "record_id": "AB12CD34", "complaint": "胃脘胀痛"})
    assert r.status_code == 200 and "AB12CD34" in r.json()["text"]


# ---------- §12 第 7、8、9 项：记录 / 模板 / 设置 ----------


def test_a_saved_visit_comes_back_grouped_by_patient_and_knows_the_visit_number(client):
    """§6.2：左栏按备注分组；复诊时标题要显示第几诊。"""
    payload = {"doctor_id": "dr_a", "patient_ref": "张三", "complaint": "胃脘胀痛",
               "syndrome": "肝胃不和证", "formula": "柴胡疏肝散加减",
               "herb_items": [_herb("柴胡", 10.0, processing="醋炙")],
               "doses_count": 7}
    assert client.post("/api/records", json=payload).json()["next_visit_index"] == 2
    got = client.get("/api/records", params={"doctor_id": "dr_a"}).json()
    group = next(g for g in got["groups"] if g["patient_ref"] == "张三")
    assert group["items"][0]["herb_items"][0]["processing"] == "醋炙"


def test_a_record_can_be_deleted(client):
    client.post("/api/records", json={"record_id": "R1", "doctor_id": "a",
                                      "patient_ref": "张三", "complaint": "c"})
    assert client.delete("/api/records/R1").json()["deleted"] is True
    assert client.get("/api/records", params={"doctor_id": "a"}).json()["n"] == 0


def test_a_template_round_trips_with_its_doses(client):
    client.post("/api/templates", json={
        "doctor_id": "a", "name": "我的疏肝方", "syndrome": "肝胃不和证",
        "herb_items": [_herb("柴胡", 10.0)], "doses_count": 7})
    rows = client.get("/api/templates", params={"doctor_id": "a"}).json()["personal"]
    assert rows[0]["doses_count"] == 7 and rows[0]["herb_items"][0]["dose"] == 10.0
    assert client.delete("/api/templates/我的疏肝方",
                         params={"doctor_id": "a"}).json()["deleted"] is True


def test_a_nameless_template_is_rejected(client):
    assert client.post("/api/templates", json={"name": "  "}).status_code == 400


def test_the_settings_choices_come_from_the_server(client):
    """§4.3：前端不写死可选值——写死的话这里加一种剂型，界面上的下拉
    不会跟着长出来。"""
    body = client.get("/api/preferences").json()
    assert body["preferences"]["doses_count"] == 7
    assert "颗粒" in body["choices"]["dosage_form"]
    assert 14 in body["choices"]["doses_count"]


def test_a_misspelled_or_out_of_range_setting_is_a_400_not_a_silent_write(client):
    assert client.put("/api/preferences", json={"changes": {"defualt_doses": 7}}).status_code == 400
    assert client.put("/api/preferences", json={"changes": {"doses_count": 6}}).status_code == 400
    assert client.get("/api/preferences").json()["preferences"]["doses_count"] == 7


def test_settings_round_trip(client):
    client.put("/api/preferences", json={
        "changes": {"doses_count": 14, "dosage_form": "颗粒", "avoid_herbs": "附子、细辛"}})
    got = client.get("/api/preferences").json()["preferences"]
    assert got["doses_count"] == 14 and got["avoid_herbs"] == ["附子", "细辛"]
    assert "冲服" in got["usage"], "换了剂型，默认煎服法要跟着换"


# ---------- §8.1：患者看到的是教材代表方 ----------


def test_the_textbook_formula_endpoint_separates_two_kinds_of_empty(client):
    """"教材推荐方案表里没有这个证型"和"有这个证型但本体里没有那张方的组成"
    是两件事，而它们在界面上会长成同一个空白。"""
    ok = client.get("/api/textbook_formula", params={"syndrome": "脾胃虚寒证"}).json()
    assert ok["available"] is True and ok["name"] and ok["basis"]
    miss = client.get("/api/textbook_formula", params={"syndrome": "查无此证"}).json()
    assert miss["available"] is False and miss["note"]
