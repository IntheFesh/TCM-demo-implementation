"""R18-I：「参考医家」那一栏（后端接口 + 前端渲染）。

前端部分用 tests/web_harness.py 在 Node 里跑 app.js 的纯函数。
**DOM 效果不在这里测**（DOM_STUB 是个万能 Proxy，断言 DOM 只会永远通过）——
真实渲染由 scripts/screenshot_states.py 那一组 Playwright 状态负责。
"""
from __future__ import annotations

import json
import subprocess

from fastapi.testclient import TestClient

from api.main import app
from core.physicians import PHYSICIANS, physicians_all
from core.safety_output import INCOMPATIBLE_TRAINING_NOTE
from tests.web_harness import DOM_STUB, js_tmp, load_app_js

client = TestClient(app)


# ---------- 后端接口 ----------

def test_endpoint_returns_the_registry_colour_and_enabled_flag():
    """身份色和 enabled 都从注册表来（core/physicians.py 唯一来源）。"""
    r = client.get("/api/reference_cases",
                   params={"complaint": "胃脘胀痛", "physician": "li_ke"})
    assert r.status_code == 200
    phys = r.json()["physician"]
    assert phys["id"] == "li_ke"
    assert phys["name"] == physicians_all(PHYSICIANS)["li_ke"]["name"]
    assert phys["color"] == physicians_all(PHYSICIANS)["li_ke"]["color"]
    assert phys["enabled"] is False


def test_endpoint_asks_for_exactly_three_cases():
    r = client.get("/api/reference_cases",
                   params={"complaint": "胃脘胀痛", "physician": "li_ke"})
    assert r.json()["k"] == 3


def test_endpoint_distinguishes_a_bad_physician_from_an_empty_result():
    """三种"空"分开报（SOURCES.md 第 31 条那个坑）：参数错要列出可用值，
    模型/人才能自我纠正；只回一句"没有结果"下一步还是瞎猜。"""
    bad = client.get("/api/reference_cases",
                     params={"complaint": "胃脘胀痛", "physician": "不存在的医家"}).json()
    assert bad["cases"] == [] and bad["error"]
    assert "li_ke" in bad["error"] and "wang_yunqi" in bad["error"]
    ok = client.get("/api/reference_cases",
                    params={"complaint": "胃脘胀痛", "physician": "li_ke"}).json()
    assert "error" not in ok
    # 语料文件在不在，用 available 区分，不跟"参数错"混在一起
    assert "available" in ok


def test_endpoint_rejects_an_empty_or_overlong_complaint():
    assert client.get("/api/reference_cases",
                      params={"complaint": "  ", "physician": "li_ke"}).status_code == 422
    too_long = "胃" * 3000
    assert client.get("/api/reference_cases",
                      params={"complaint": too_long, "physician": "li_ke"}).status_code == 422


def test_endpoint_attaches_the_shared_sentence_only_to_incompatible_cases(monkeypatch):
    """那句话来自 core/safety_output（唯一定义），判定来自 check_incompatible
    （唯一实现）——界面显示的那句必须跟训练样本里的逐字相同。"""
    import api.main as main_mod

    def fake_search(query, physician, k=3):
        return {"available": True, "cases": [
            {"case_id": "li_ke-001", "score": 0.9, "visit_index": 1, "symptoms": [],
             "tongue": None, "pulse": None, "syndrome": "阳虚", "treatment_principle": "温阳",
             "formula": "四逆汤", "herbs": ["炙甘草", "海藻"]},
            {"case_id": "li_ke-002", "score": 0.8, "visit_index": 1, "symptoms": [],
             "tongue": None, "pulse": None, "syndrome": "气虚", "treatment_principle": "补气",
             "formula": "四君子汤", "herbs": ["人参", "白术"]},
        ]}

    monkeypatch.setattr(main_mod, "search_cases", fake_search)
    cases = client.get("/api/reference_cases",
                       params={"complaint": "胃脘胀痛", "physician": "li_ke"}).json()["cases"]
    assert cases[0]["incompatible_pairs"] == ["炙甘草-海藻"]
    assert cases[0]["note"] == INCOMPATIBLE_TRAINING_NOTE
    # 没有反药配对的那条不挂提示——每条都挂会让人习惯性忽略它
    assert cases[1]["incompatible_pairs"] == []
    assert cases[1]["note"] is None


def test_endpoint_is_separate_from_consult():
    """独立接口而不是塞进 /api/consult 的返回：集注是分钟级 LLM 调用，
    检索是毫秒级，合在一起这一栏就得跟三列一起等。"""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "api" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/api/reference_cases")' in src
    # /api/consult 的响应里不该多出这一块
    r = client.get("/health")
    assert "reference_cases" not in json.dumps(r.json())


# ---------- 前端 ----------

def _run(js: str) -> str:
    """在 node 里跑 graph.js + app.js + 一段用例代码，返回 stdout。"""
    path = js_tmp(DOM_STUB + load_app_js() + js)
    r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"node 退出码 {r.returncode}\n{r.stderr}\n脚本：{path}"
    return r.stdout


def test_frontend_reads_who_is_a_reference_physician_from_health():
    """前端不列名单：注册表里开/关一位医家时，写死的名单不会跟着变
    （CLAUDE.md 第 31 条，前端小节：写死的常量也算一处实现）。"""
    out = _run("""
      injectPhysicianColors([
        {id: "ye_tianshi", name: "叶天士", color: "#2C5F5A", enabled: true},
        {id: "li_ke", name: "李可", color: "#4A6B4E", enabled: false},
        {id: "wang_yunqi", name: "王云启", color: "#5B5470", enabled: false},
      ]);
      console.log(JSON.stringify(REFERENCE_PHYSICIANS));
    """)
    assert json.loads(out.strip().splitlines()[-1]) == ["li_ke", "wang_yunqi"]


def test_frontend_only_shows_the_block_for_student_and_researcher():
    """患者模式不显示（不该看到不参与结论的医案），医生模式不显示
    （版面留给可编辑处方）。"""
    out = _run('console.log(JSON.stringify(REFERENCE_ROLES));')
    assert set(json.loads(out.strip().splitlines()[-1])) == {"student", "researcher"}


def test_frontend_uses_the_css_variable_for_the_identity_colour():
    """不把接口返回的色值直接写进 style：变量由 injectPhysicianColors 从同一份
    注册表注入，两条路会漂。"""
    out = _run("""
      const html = referenceBlockHtml("li_ke", {physician: {name: "李可"}, cases: [
        {case_id: "c1", score: 0.9, syndrome: "阳虚", herbs: ["附子"]}]});
      console.log(html.includes("var(--phys-li_ke)"));
      console.log(/#[0-9A-Fa-f]{6}/.test(html));
    """)
    lines = out.strip().splitlines()
    assert lines[-2] == "true", "身份色要走 CSS 变量"
    assert lines[-1] == "false", "HTML 里不该出现写死的色值"


def test_frontend_renders_the_note_verbatim_and_escapes_it():
    out = _run("""
      const html = referenceBlockHtml("li_ke", {physician: {name: "李可"}, cases: [
        {case_id: "c1", score: 0.9, herbs: ["炙甘草", "海藻"],
         incompatible_pairs: ["炙甘草-海藻"], note: "【配伍提示】<b>x</b>"}]});
      console.log(html.includes("炙甘草-海藻"));
      console.log(html.includes("<b>"));
    """)
    lines = out.strip().splitlines()
    assert lines[-2] == "true"
    assert lines[-1] == "false", "note 来自接口，必须转义（XSS）"


def test_frontend_says_which_kind_of_empty_it_is():
    """一律显示"没有结果"会把三件事混成一件。"""
    out = _run("""
      console.log(referenceBlockHtml("li_ke", {error: "参数错了"}).includes("参数错了"));
      console.log(referenceBlockHtml("li_ke", {available: false, note: "cases.json 不存在"})
        .includes("语料未就绪"));
      console.log(referenceBlockHtml("li_ke", {available: true, cases: [], note: "已查 57 条"})
        .includes("已查 57 条"));
    """)
    assert out.strip().splitlines()[-3:] == ["true", "true", "true"]


def test_frontend_block_is_wired_into_the_reset_path():
    """它是上一条主诉的检索结果，留着会跟新主诉的三列并排显示，
    看起来像这一次也检索出了这几条。"""
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "web" / "app.js").read_text(encoding="utf-8")
    reset = src[src.index("function resetSecondaryPanels"):]
    reset = reset[:reset.index("\n}\n")]
    assert "hideReferencePhysicians()" in reset


def test_the_details_block_exists_in_the_html_and_starts_hidden():
    html = (__import__("pathlib").Path(__file__).resolve().parent.parent
            / "web" / "index.html").read_text(encoding="utf-8")
    assert '<details id="reference-physicians" hidden>' in html
    assert 'id="reference-body"' in html
    # 折叠着：它是旁证，不是这次问诊的结论
    assert "参考医家" in html


def test_columns_exclude_reference_physicians():
    """R18-A 把注册表扩到五位之后，非终态一度摆出了**五列**——后端集注只跑
    enabled 那三位，多出来的两列永远停在"辨证中"。

    这是 Playwright 发现的（running / insufficient / followup 三个状态报
    「不是三列，是 5」）；这条纯函数测试是它的回归闸门。
    """
    out = _run("""
      injectPhysicianColors([
        {id: "ye_tianshi", name: "叶天士", color: "#2C5F5A", enabled: true},
        {id: "wu_jutong", name: "吴鞠通", color: "#9C6B16", enabled: true},
        {id: "zhang_xichun", name: "张锡纯", color: "#8A4736", enabled: true},
        {id: "li_ke", name: "李可", color: "#4A6B4E", enabled: false},
        {id: "wang_yunqi", name: "王云启", color: "#5B5470", enabled: false},
      ]);
      console.log(JSON.stringify(physicianOrder()));
    """)
    order = json.loads(out.strip().splitlines()[-1])
    assert order == ["ye_tianshi", "wu_jutong", "zhang_xichun"]
    assert "li_ke" not in order and "wang_yunqi" not in order


def test_columns_still_show_everyone_when_nothing_is_disabled():
    """对照：注册表里全是 enabled 时一个都不该被过滤掉——修复不能变成
    "永远只画前三位"。"""
    out = _run("""
      injectPhysicianColors([
        {id: "a", name: "甲", color: "#111111", enabled: true},
        {id: "b", name: "乙", color: "#222222", enabled: true},
        {id: "c", name: "丙", color: "#333333", enabled: true},
        {id: "d", name: "丁", color: "#444444", enabled: true},
      ]);
      console.log(JSON.stringify(physicianOrder()));
    """)
    assert json.loads(out.strip().splitlines()[-1]) == ["a", "b", "c", "d"]
