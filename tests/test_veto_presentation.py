"""R65：验证否决的呈现。

用户真机截图里，**医师**角色在验证否决后看到的是整页红：

    请尽快就医
    …（herb_source_fabricated）
    本页不提供方药内容。

三件事同时错了，这份测试逐条钉住修法：

1. 它借用了红旗拦截的样式与措辞。红旗是"你的症状危险，别在这儿看方了"，
   验证否决是"方没通过系统核查"——**两套完全不同的呈现**。
2. `herb_source_fabricated` 直接印在了屏幕上（R62 §7.2 之后第 N 次），
   说明错误文案的拼装路径绕过了产品文案过滤。
3. 最根本的：**医师模式下把整张方废掉是设计错了**。医师是专业人员，需要
   完整分析 + 指出哪一味有问题，自己判断要不要用。

全部离线，不调模型。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import veto_text
from core.formula_verifier import ALL_RULES, REVISE_RULES, VETO_RULES
from tests.test_revise_loop import ont, run  # noqa: F401  （夹具，真跑一次推理链）

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    return TestClient(api_main.app)


# ---------- 1. 每条规则都要有人话版本 ----------


def test_every_rule_has_plain_language_text():
    """十三条规则一条不漏。少一条就意味着那条规则触发时，界面只能退回去
    印规则 id——那正是这轮要修的事，所以缺口必须是 0，不是"大部分有"。"""
    assert veto_text.coverage_gap() == (), (
        f"这些规则没有人话文案：{veto_text.coverage_gap()}")
    assert set(veto_text.RULE_TEXT) >= set(ALL_RULES)


def test_an_unknown_rule_still_never_leaks_its_id():
    """以后加新规则时，加规则的人可能忘了同步文案表。那种情况下也不能把
    id 漏到界面上——兜底文案必须是人话，且不含规则名。"""
    text = veto_text.describe("some_future_rule", ["柴胡"])
    assert "some_future_rule" not in text
    assert "柴胡" in text


def test_no_plain_text_entry_contains_an_underscore_rule_name():
    """文案表自己也不能把 id 抄进正文里——那样过滤了等于没过滤。"""
    for rule, text in veto_text.RULE_TEXT.items():
        assert rule not in text, f"{rule} 的文案里含它自己的 id"
        assert not re.search(r"[a-z]+_[a-z_]+", text), f"{rule} 的文案含下划线标识符：{text}"


# ---------- 2. 红旗措辞收归专用（源码级） ----------

#: 「请尽快就医」「本页不提供方药内容」这两句**只属于红旗拦截**。
#: 这条测试扫源码而不是扫响应：响应级测试只覆盖被构造到的那几条分支，
#: 而这两句话一旦被写进验证否决的文案表或渲染分支，下一个人复制粘贴
#: 就会再出一次同样的事故。扫源码把"不许写"本身钉死。
_REDFLAG_ONLY = veto_text.RED_FLAG_ONLY_PHRASES


def test_red_flag_phrases_never_appear_in_the_verification_copy_module():
    """`core/veto_text.py` 是验证否决全部产品文案的唯一来源。这两句话在这个
    模块里只允许出现在注释/文档字符串里（说明"不许用"），不能出现在任何
    会被渲染的字符串常量里。"""
    for phrase in _REDFLAG_ONLY:
        assert phrase not in veto_text.DOCTOR_BANNER
        assert phrase not in veto_text.PATIENT_NOTICE
        for rule, text in veto_text.RULE_TEXT.items():
            assert phrase not in text, f"{rule} 的文案用了红旗专用语「{phrase}」"
        for rule, short in veto_text.SHORT_REASON.items():
            assert phrase not in short


def _triage_body(*, strip_comments: bool = True) -> str:
    """`renderTriagePage` 的函数体。默认剥掉 `//` 注释——注释里**要**写明
    「这两句是红旗专用」，那是这条约束的说明文字，不是会被渲染的文案。
    扫源码的测试如果连注释一起扫，就会把"写清楚为什么"惩罚成红。"""
    src = (ROOT / "web" / "product" / "app.js").read_text(encoding="utf-8")
    fn = src.index("function renderTriagePage")
    body = src[fn:src.index("\nfunction ", fn + 10)]
    if strip_comments:
        body = "\n".join(re.sub(r"//.*$", "", ln) for ln in body.split("\n"))
    return body


def test_the_frontend_gates_the_red_flag_phrases_behind_the_red_flag_branch():
    """前端 `renderTriagePage` 是两条路唯一的汇合点（红旗与验证否决都会走到
    这个函数），所以这两句话在 `web/product/app.js` 里必须**只**出现在
    `verification` 分支已经 return 之后的那一段。"""
    body = _triage_body()
    guard = body.index('vb.kind === "verification"')
    ret = body.index("return;", guard)
    for phrase in _REDFLAG_ONLY:
        assert phrase in body, f"红旗分支本身还得说「{phrase}」"
        for m in re.finditer(re.escape(phrase), body):
            assert m.start() > ret, (
                f"「{phrase}」出现在验证否决分支 return 之前（偏移 {m.start()} ≤ {ret}）"
                "——验证否决会看到这句红旗专用语")


def test_the_verification_branch_uses_the_no_prescription_wording():
    body = _triage_body()
    assert "本次未能给出可供参考的方剂" in body
    assert body.index("本次未能给出可供参考的方剂") < body.index("请尽快就医")


def test_the_two_bars_are_separate_dom_elements():
    """R65 之前两条路合成了一条 DOM 元素，措辞才会互相串味。分成两个
    元素，样式与文案就不可能再共用。"""
    html = (ROOT / "web" / "product" / "index.html").read_text(encoding="utf-8")
    assert 'id="redflag-bar"' in html
    assert 'id="veto-bar"' in html


# ---------- 3. 规则 id 不到产品面 ----------
#
# 下面这些用**真的推理链**跑出 veto outcome，再喂给 `_consult_response`——
# 手搓 outcome dict 会漏掉 schema 对象（`s1.model_dump()`），而且手搓的那份
# 形状一旦跟真实 outcome 分叉，测试就开始测一个不存在的契约。


@pytest.fixture
def veto_outcome(run):
    """甘草 + 甘遂 = 十八反，回灌轮数用尽后残留 veto。"""
    out, _llm = run([dict(herbs=("甘草", "甘遂"))])
    assert out["verification_veto"], "夹具本身要真的产出 veto"
    return out


def _with_rule(outcome, rule):
    """把 veto 的规则名换掉，逐条规则复用同一份真实 outcome。"""
    veto = [{**v, "rule": rule, "reason": f"内部日志的话，带规则名 {rule}"}
            for v in outcome["verification_veto"]]
    return {**outcome, "verification_veto": veto,
            "reject_reason": f"符号验证未通过（{rule}）"}


@pytest.mark.parametrize("role", ["doctor", "student", "patient"])
@pytest.mark.parametrize("rule", ALL_RULES)
def test_no_rule_id_reaches_any_product_role(veto_outcome, role, rule):
    """产品三角色的**整个**响应（不只是某个字段）里都不许出现规则 id。
    整体序列化后扫一遍——单字段断言挡不住"换个键继续漏"。"""
    out = api_main._consult_response(_with_rule(veto_outcome, rule), role=role)
    blob = json.dumps(out, ensure_ascii=False, default=str)
    assert rule not in blob, f"role={role} 的响应里漏出了规则 id {rule}"


def test_the_internal_role_still_gets_the_technical_detail(veto_outcome):
    """「查看详情」要给得出技术细节（规则 id、本体原文、模型原文），否则
    研究模式与排障没法用。产品面过滤 ≠ 信息消失。"""
    out = api_main._consult_response(veto_outcome, role="researcher")
    detail = out["verification_block"]["detail"]
    assert detail and detail[0]["rule"] == "incompatible_pair"
    assert detail[0]["counterexample"]


# ---------- 4. 分角色行为 ----------


@pytest.mark.parametrize("role", ["doctor", "student"])
def test_doctor_and_student_keep_the_whole_chain_and_formula(veto_outcome, role):
    """这一条是这轮的核心：**不整页拦截**。医师/学生照常拿到推导与方剂，
    另外拿到"哪一味有问题"和"导出被禁"。"""
    out = api_main._consult_response(veto_outcome, role=role)
    assert out["rejected"] is False, "整页拦截是红旗专属，验证否决不许整页拦"
    assert out["results"], "方药与推导要照常给出"
    assert out["results"][0]["s3_structured"], "医师要看的是五步链，不只是结论卡"
    vb = out["verification_block"]
    assert vb["kind"] == "verification"
    assert vb["export_blocked"] is True, "不可下发要表达成禁用导出，不是抹掉内容"
    assert set(vb["flagged_herbs"]) == {"甘草", "甘遂"}, "要指出是哪几味"
    assert "甘草" in vb["herb_reasons"], "每一味要带它自己的原因"
    assert vb["banner"], "黄条要有话说"


def test_the_doctor_banner_is_not_a_red_flag_banner(veto_outcome):
    out = api_main._consult_response(veto_outcome, role="doctor")
    banner = out["verification_block"]["banner"]
    for phrase in _REDFLAG_ONLY:
        assert phrase not in banner
    assert "导出" in banner, "要说清为什么不能用，而不是只说失败"


def test_patient_gets_no_prescription_and_never_the_red_flag_wording(veto_outcome):
    out = api_main._consult_response(veto_outcome, role="patient")
    assert out["rejected"] is True, "患者这一侧仍然不给方"
    assert out["results"] == []
    assert "本次未能给出可供参考的方剂" in out["reject_reason"]
    for phrase in _REDFLAG_ONLY:
        assert phrase not in out["reject_reason"]
    vb = out["verification_block"]
    assert vb["detail"] == [] and vb["flagged_herbs"] == [] and vb["herb_reasons"] == {}


def test_a_safety_veto_still_blocks_the_whole_page(veto_outcome):
    """反向确认：红旗那条路一点没动——安全否决仍然整页拦截、仍然说
    「请尽快就医」（措辞由安全层给，这里只确认它没被验证否决那套接管）。"""
    out = api_main._consult_response({
        **veto_outcome, "verification_veto": None, "results": [],
        "rejected": True, "safety_flag": "黑便",
        "reject_reason": "所述症状含危重征象，请尽快就医。",
    }, role="patient")
    assert out["rejected"] is True
    assert out["verification_block"] is None, "安全否决不该带验证否决块"
    assert "请尽快就医" in out["reject_reason"]


# ---------- 5. 措辞本身读得通 ----------


def test_an_incompatible_pair_reads_as_two_herbs_meeting():
    """十八反是"两味药同方相见"，文案必须读得出这层意思——单味药的句式
    套上去会变成"甘草属十八反"，那是错的。"""
    text = veto_text.describe("incompatible_pair", ["甘草", "甘遂"])
    assert "甘草" in text and "甘遂" in text
    assert "十八反" in text or "十九畏" in text


def test_the_summary_merges_several_rules_without_listing_ids():
    veto = [{"rule": "herb_source_fabricated", "herbs": ["蜜麻黄"],
             "reason": "r", "counterexample": "c"},
            {"rule": "dose_exceeds", "herbs": ["附子"],
             "reason": "r", "counterexample": "c"}]
    s = veto_text.summarize(veto)
    assert "蜜麻黄" in s and "附子" in s
    assert "herb_source_fabricated" not in s and "dose_exceeds" not in s


def test_veto_and_revise_rules_stay_disjoint():
    """文案表覆盖两级规则，但两级不许重叠——重叠意味着同一条规则既能
    veto 又能 revise，那是 `Violation.__post_init__` 明确禁止的。"""
    assert set(VETO_RULES) & set(REVISE_RULES) == set()
    assert veto_text.veto_rules() == tuple(VETO_RULES)
