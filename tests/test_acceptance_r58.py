"""R58：真机验收脚本的判据。**零 LLM、零网络**——真跑在
`scripts/acceptance_r58.py` 里，这里只测那几件"错了也不会报错"的纯函数：
角色形状契约、原文摘录溯源正则、R57 闸门三分返回值。

`audit_case_excerpt_grounding` 与它配套的 `CASE_BLOCK_RE` 两处都是脚本
自己在第一次用真实 cases.json 跑通之后才改对的（先是正则只抓一行、把多行
摘录误判成"编造"，再是"没有摘录的医案"被误判成"cases.json 里没有这个
id"）——这几条回归测试钉的就是这两个真实踩过的坑，不是假设出来的边界。
"""
from __future__ import annotations

import json

from scripts.acceptance_r58 import (
    ROLE_SHAPE,
    assert_response_shape,
    audit_case_excerpt_grounding,
    check_r57_gate_passed,
)


# ---------- 角色形状契约 ----------

def _response(**overrides) -> dict:
    base = {"manifest": {"x": 1}, "divergence": {"y": 2},
           "results": [{"refs": ["r1"], "react_trace": [], "advice": "x"}],
           "individualization": {}, "guideline": {},
           "triage": {}, "food_therapy": [], "patent_medicines": []}
    base.update(overrides)
    return base


def test_researcher_gets_everything_untouched():
    assert assert_response_shape("researcher", _response()) == []


def test_student_loses_only_manifest():
    resp = _response()
    del resp["manifest"]
    assert assert_response_shape("student", resp) == []


def test_student_flags_a_leaked_manifest():
    problems = assert_response_shape("student", _response())
    assert any("manifest" in p for p in problems)


def test_doctor_loses_manifest_divergence_and_react_trace():
    resp = _response()
    del resp["manifest"]
    del resp["divergence"]
    del resp["results"][0]["react_trace"]
    assert assert_response_shape("doctor", resp) == []


def test_doctor_flags_a_leaked_react_trace():
    resp = _response()
    del resp["manifest"]
    del resp["divergence"]
    problems = assert_response_shape("doctor", resp)
    assert any("react_trace" in p for p in problems)


def test_patient_loses_manifest_divergence_individualization_guideline_and_advice():
    resp = _response()
    for k in ("manifest", "divergence", "individualization", "guideline"):
        del resp[k]
    del resp["results"][0]["react_trace"]
    del resp["results"][0]["advice"]
    resp["results"][0]["refs"] = []
    assert assert_response_shape("patient", resp) == []


def test_patient_flags_non_empty_refs():
    resp = _response()
    for k in ("manifest", "divergence", "individualization", "guideline"):
        del resp[k]
    del resp["results"][0]["react_trace"]
    del resp["results"][0]["advice"]
    problems = assert_response_shape("patient", resp)
    assert any("refs" in p for p in problems)


def test_an_unknown_role_is_reported_not_silently_skipped():
    problems = assert_response_shape("nobody", _response())
    assert problems and "nobody" in problems[0]


def test_every_registered_role_has_a_non_empty_contract():
    for role, exp in ROLE_SHAPE.items():
        assert exp.must_have or exp.must_not_have, f"{role} 的契约是空的"


# ---------- cases.json 原文摘录溯源 ----------

def _prompt_block(case_id: str, excerpt: str, *, structured: str = "症状=胃痛") -> str:
    return f"【参考医案】{case_id}（初诊）\n原文：{excerpt}\n结构化：{structured}"


def test_a_genuine_verbatim_excerpt_is_grounded():
    real = "钱 胃虚少纳。土不生金。音低气馁。当与清补。"
    prompt = _prompt_block("ye_tianshi-0001-p0-0", real)
    out = audit_case_excerpt_grounding(prompt, {"ye_tianshi-0001-p0-0": real})
    assert out == {"n_refs": 1, "n_grounded": 1, "ungrounded": []}


def test_a_multiline_excerpt_is_not_mistaken_for_truncated_garbage():
    """回归测试：`CASE_BLOCK_RE` 第一版只抓「原文：」后面那一行，真实
    `raw_excerpt` 带换行时会把完整摘录切短，切短的结果天然对不上
    `real[:300]`，被误判成"编造"——这条钉住多行摘录必须原样抓全。"""
    real = "钱 胃虚少纳。土不生金。\n麦冬 生扁豆 玉竹\n王 数年病伤不复。"
    prompt = _prompt_block("ye_tianshi-0002-p0-0", real)
    out = audit_case_excerpt_grounding(prompt, {"ye_tianshi-0002-p0-0": real})
    assert out["n_refs"] == 1
    assert out["ungrounded"] == []


def test_a_fabricated_excerpt_is_caught():
    real = "这是真实原文，用于对照。"
    prompt = _prompt_block("wu_jutong-0001-p0-0", "这是模型编出来的一段完全不同的话")
    out = audit_case_excerpt_grounding(prompt, {"wu_jutong-0001-p0-0": real})
    assert out["n_refs"] == 1 and out["n_grounded"] == 0
    assert out["ungrounded"][0]["case_id"] == "wu_jutong-0001-p0-0"


def test_a_truncated_prefix_is_grounded_not_flagged_as_fabricated():
    real = "甲" * 500  # 超过 300 字截断长度
    prompt = _prompt_block("zhang_xichun-0001-p0-0", real[:300])
    out = audit_case_excerpt_grounding(prompt, {"zhang_xichun-0001-p0-0": real})
    assert out["ungrounded"] == []


def test_a_case_with_no_real_excerpt_correctly_renders_the_missing_sentinel():
    prompt = _prompt_block("li_ke-0001-p0-0", "（原文缺失）")
    out = audit_case_excerpt_grounding(prompt, {"li_ke-0001-p0-0": None})
    assert out["ungrounded"] == []


def test_a_case_with_no_real_excerpt_but_prompt_shows_real_text_is_flagged():
    """一条医案没有 raw_excerpt，prompt 里却出现了具体原文——
    这才是真正可疑的情况，不是"没有摘录"那条的对称面被漏判。"""
    prompt = _prompt_block("li_ke-0002-p0-0", "这段文字不该出现")
    out = audit_case_excerpt_grounding(prompt, {"li_ke-0002-p0-0": None})
    assert len(out["ungrounded"]) == 1


def test_a_case_id_that_does_not_exist_in_cases_json_at_all_is_flagged():
    """**回归测试**：早期实现只把"有摘录的医案"塞进查表字典，导致"这条
    医案确实没有摘录、正确显示『原文缺失』"跟"cases.json 里压根没有这个
    id"（真正的编造）落进同一个 if 分支——按这条判据算出的 1060 条引用里
    44 条全部属于前者，一条真正编造的都没有。这条测试钉住这两种情况现在
    分得开：字典必须覆盖全部医案 id（值可以是 None），漏了没摘录的那些
    传进来，就是在重演这个坑。"""
    real = "这条医案确实存在。"
    prompt = (_prompt_block("real-0001-p0-0", real)
             + "\n" + _prompt_block("hallucinated-9999-p0-0", "编出来的一条"))
    out = audit_case_excerpt_grounding(prompt, {"real-0001-p0-0": real})
    assert out["n_refs"] == 2
    reasons = {u["case_id"]: u["reason"] for u in out["ungrounded"]}
    assert reasons["hallucinated-9999-p0-0"] == "cases.json 里没有这条医案 id"
    assert "real-0001-p0-0" not in reasons


def test_zero_references_is_the_honest_answer_for_derived_mode():
    """`S3_MODE=derived` 的 prompt 里没有「【参考医案】」这种块——0 处引用
    是诚实答案，不是"这条检查没跑"（模块文档第 3 条）。"""
    out = audit_case_excerpt_grounding("这是一段完全不含医案引用的 prompt 文本。", {})
    assert out == {"n_refs": 0, "n_grounded": 0, "ungrounded": []}


def test_multiple_references_in_the_same_prompt_are_all_scanned():
    real_a, real_b = "医案甲的原文。", "医案乙的原文。"
    prompt = _prompt_block("a-0001-p0-0", real_a) + "\n" + _prompt_block("b-0001-p0-0", real_b)
    out = audit_case_excerpt_grounding(prompt, {"a-0001-p0-0": real_a, "b-0001-p0-0": real_b})
    assert out == {"n_refs": 2, "n_grounded": 2, "ungrounded": []}


# ---------- R57 闸门：三分返回值，不是二分 ----------

def test_gate_check_distinguishes_not_run_from_failed(tmp_path):
    """"还没跑"和"跑了但没过"是两种不同的理由，读到的人要能分清楚，
    不能都变成一个裸的 False（跟 CLAUDE.md 的三分返回值纪律同一条理）。"""
    missing = tmp_path / "does_not_exist.json"
    ok, reason = check_r57_gate_passed(missing)
    assert ok is False
    assert "还没跑" in reason


def test_gate_check_passes_when_the_report_says_so(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"content_metrics_valid": True, "all_gates_passed": True}),
                 encoding="utf-8")
    ok, reason = check_r57_gate_passed(p)
    assert ok is True


def test_gate_check_fails_loudly_when_gates_failed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"content_metrics_valid": True, "all_gates_passed": False}),
                 encoding="utf-8")
    ok, reason = check_r57_gate_passed(p)
    assert ok is False
    assert "没过" in reason


def test_gate_check_rejects_a_fake_backend_report(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"content_metrics_valid": False, "all_gates_passed": True}),
                 encoding="utf-8")
    ok, reason = check_r57_gate_passed(p)
    assert ok is False, "假后端的报告不该被当成真机闸门通过的证据"
