"""R56 §6 第 13 条：⑨「校验与出处」从"违规 veto N / revise N"/"判不了 N（…）"
两行统计改成逐条核对清单——每条规则一行，✓ 通过 / ✗ 不通过（附原因） /
— 判不了（附缺什么），用的是真实的 `core/formula_verifier.py` 逐条校验数据
（`checked_rules` / `violations` / `unverifiable`），不是伪造的。

`verificationChecklistHtml` 是纯函数（输入 `verify_formula(...).to_dict()`
的形状，输出一段 HTML），跟真实后端序列化的字段名对齐——见
`test_uses_the_real_checked_rule_labels_field_from_the_backend`。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js

APP = load_app_js()


def _checklist(v: dict) -> str:
    js = f"process.stdout.write(verificationChecklistHtml({json.dumps(v, ensure_ascii=False)}));"
    proc = subprocess.run(["node", js_tmp(DOM_STUB + APP + "\n" + js)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def test_a_passed_rule_shows_a_checkmark():
    v = {"checked_rules": ["meridian_coverage"],
         "checked_rule_labels": {"meridian_coverage": "归经覆盖病位"},
         "violations": [], "unverifiable": []}
    out = _checklist(v)
    assert "归经覆盖病位" in out
    assert "verify-pass" in out and "✓" in out


def test_a_violated_rule_shows_a_cross_and_the_reason():
    v = {"checked_rules": [], "checked_rule_labels": {},
         "violations": [{"rule": "incompatible_pair", "rule_label": "配伍禁忌",
                         "severity": "veto", "herbs": ["甘草", "甘遂"],
                         "reason": "甘草与甘遂属十八反", "counterexample": "x"}],
         "unverifiable": []}
    out = _checklist(v)
    assert "配伍禁忌" in out and "✗" in out and "verify-fail" in out
    assert "甘草与甘遂属十八反" in out


def test_an_unverifiable_rule_shows_a_dash_and_what_is_missing():
    v = {"checked_rules": [], "checked_rule_labels": {},
         "violations": [],
         "unverifiable": [{"rule": "meridian_coverage", "rule_label": "归经覆盖病位",
                           "herbs": ["麻黄"], "missing_predicate": "归经",
                           "reason": "本体里「麻黄」没有归经这一项"}]}
    out = _checklist(v)
    assert "归经覆盖病位" in out and "—" in out and "verify-unverifiable" in out
    assert "本体里「麻黄」没有归经这一项" in out


def test_a_rule_that_is_both_checked_and_violated_shows_as_failed_not_passed():
    """`check_meridian_coverage` 那类规则：有违规时 `checked_rules` 依然带它
    自己的名字（"判过"跟"判的结果是不是违规"是两回事）。这条测试钉住优先级——
    出了问题比"判过"更要紧，不能因为它也在 checked_rules 里就显示成 ✓。"""
    v = {"checked_rules": ["meridian_coverage"],
         "checked_rule_labels": {"meridian_coverage": "归经覆盖病位"},
         "violations": [{"rule": "meridian_coverage", "rule_label": "归经覆盖病位",
                         "severity": "revise", "herbs": ["麻黄"],
                         "reason": "辨出的病变脏腑没有一味药的归经覆盖到",
                         "counterexample": "x"}],
         "unverifiable": []}
    out = _checklist(v)
    assert out.count("归经覆盖病位") == 1, "同一条规则不该在清单里重复出现"
    assert "verify-fail" in out and "verify-pass" not in out


def test_empty_verification_renders_nothing():
    out = _checklist({"checked_rules": [], "checked_rule_labels": {},
                      "violations": [], "unverifiable": []})
    assert out == ""


def test_labels_are_escaped():
    v = {"checked_rules": ["x"], "checked_rule_labels": {"x": "<script>alert(1)</script>"},
         "violations": [], "unverifiable": []}
    out = _checklist(v)
    assert "<script>" not in out


def test_a_real_backend_verification_result_renders_without_crashing():
    """跟后端 `VerificationResult.to_dict()` 的真实输出对齐，不是手搭的假
    JSON——伪造的字段名在这里测不出来。`tests/test_formula_verifier.py::
    test_to_dict_includes_checked_rule_labels_for_the_product_checklist`
    钉住字段本身存在；这里钉住前端拿到这份真实输出能正常渲染。"""
    from core.formula_verifier import ALL_RULES, VerificationResult

    real = VerificationResult(checked_rules=ALL_RULES).to_dict()
    out = _checklist(real)
    assert out.count("verify-pass") == len(ALL_RULES)
    assert "verify-fail" not in out and "verify-unverifiable" not in out

