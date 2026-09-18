"""R57：四组消融的判据。**零 LLM、零网络**——跑在 eval/ablation/{spec,r57}.py 里。

只测"错了也不会报错"的那几件事：分组定义是不是任务书原文那三个布尔、
旋钮切换有没有串味、C/D 配对一致率的分母对不对、`--backend fake` 能不能
跑通 `S3_MODE=derived`（这条是真的在沙盒里踩过的坑，见
scripts/bench_consult.py 的两处修复）。真机 20 条主诉 × 4 组的内容指标
不在这里测——那是 eval/，不是 tests/。
"""
from __future__ import annotations

import json
import os

import pytest

from eval.ablation.r57 import (
    KNOBS,
    _fmt_rate,
    aggregate,
    apply_group,
    build_report,
    metrics_from_result,
    pair_consistency,
    select_pi_wei_men_complaints,
)
from eval.ablation.spec import (
    GATE_C_RULE_REFS_COMPLETENESS_MIN,
    GATE_CD_CONSISTENCY_MIN,
    GATE_MIN_SAMPLE_SIZE,
    GROUPS,
    group_by_key,
)


def test_the_four_groups_match_the_task_spec_exactly():
    """任务书原文（A=无医理层+医案进推导相，B=无医理层+无医案，
    C=有医理层+无医案，D=有医理层+第三相佐证）——这份定义只在
    `eval/ablation/spec.py` 里写一次，这条测试钉住它没有被后续改动悄悄漂移。"""
    assert [g.key for g in GROUPS] == ["A", "B", "C", "D"]
    by_key = {g.key: g for g in GROUPS}
    assert (by_key["A"].theory_layer, by_key["A"].cases_in_derivation,
           by_key["A"].corroboration) == (False, True, False)
    assert (by_key["B"].theory_layer, by_key["B"].cases_in_derivation,
           by_key["B"].corroboration) == (False, False, False)
    assert (by_key["C"].theory_layer, by_key["C"].cases_in_derivation,
           by_key["C"].corroboration) == (True, False, False)
    assert (by_key["D"].theory_layer, by_key["D"].cases_in_derivation,
           by_key["D"].corroboration) == (True, False, True)


def test_the_env_translation_is_the_only_place_that_does_it():
    """`.env` 是三个布尔翻译成实际旋钮的**唯一一处**——这条测的是翻译规则本身
    （cases_in_derivation → S3_MODE，其余两个直接映射），不是分组定义。"""
    by_key = {g.key: g for g in GROUPS}
    assert by_key["A"].env == {"S3_MODE": "structured", "THEORY_LAYER": "off",
                               "CORROBORATION": "off"}
    assert by_key["C"].env == {"S3_MODE": "derived", "THEORY_LAYER": "on",
                               "CORROBORATION": "off"}
    assert by_key["D"].env == {"S3_MODE": "derived", "THEORY_LAYER": "on",
                               "CORROBORATION": "on"}


def test_applying_a_group_clears_the_other_knobs_and_restores(monkeypatch):
    monkeypatch.setenv("THEORY_LAYER", "off")
    monkeypatch.setenv("CORROBORATION", "on")
    before = {k: os.environ.get(k) for k in KNOBS}
    with apply_group(group_by_key("C")):
        assert os.environ["S3_MODE"] == "derived"
        assert os.environ["THEORY_LAYER"] == "on"
        assert os.environ["CORROBORATION"] == "off", "C 组不该带着上一次的 on 跑"
    assert {k: os.environ.get(k) for k in KNOBS} == before


# ---------- 指标原料 ----------

def _fake_derived_result(*, syndrome="脾气虚证", method="健脾益气", formula="四君子汤",
                         first_pass=True, completeness=1.0, n_hallucinated=0) -> dict:
    return {
        "results": [{
            "s3": object(),
            "s3_structured": {"syndrome": {"name": syndrome}, "method": {"principle": method},
                              "formula": {"candidate": {"name": formula}}},
            "herbs_grounded_ratio": 0.8,
            "verifier_metrics": {"verifier_first_pass": first_pass},
            "derivation_completeness_ratio": completeness,
            "hallucinated": ["x"] * n_hallucinated,
        }],
        "manifest": {"llm_calls": 3, "s3_mode": "derived"},
    }


def test_metrics_from_result_reads_derived_specific_fields():
    m = metrics_from_result(_fake_derived_result())
    assert m["has_output"] is True
    assert m["verifier_first_pass"] is True
    assert m["rule_refs_completeness"] == 1.0
    assert m["syndrome"] == "脾气虚证" and m["method"] == "健脾益气" and m["formula"] == "四君子汤"


def test_metrics_from_result_treats_structured_mode_rule_refs_as_absent_not_zero():
    """A 组（structured）没有 `derivation_completeness_ratio` 这个键——
    `metrics_from_result` 必须原样传 None，不能把"没有这个概念"悄悄算成 0。"""
    result = _fake_derived_result()
    del result["results"][0]["derivation_completeness_ratio"]
    m = metrics_from_result(result)
    assert m["rule_refs_completeness"] is None


def test_metrics_from_result_no_output_when_s3_is_none():
    m = metrics_from_result({"results": [{"s3": None}]})
    assert m == {"has_output": False}


# ---------- R60 §2.5：被 SymbolicVeto 拦下的那一行不许把违规细节也扔了 ----------

def _fake_symbolic_veto_result(rule="herb_source_fabricated") -> dict:
    """跟 `core/chain.py` 里 `except SymbolicVeto` 分支实际拼出来的形状一致：
    `results` 恒为空列表，违规细节挂在顶层 `verification_veto`。"""
    return {
        "results": [], "rejected": True,
        "reject_reason": f"这张方在符号验证中有不可下发的问题（{rule}），"
                         "系统已按本体原文重开 1 轮仍未消除，因此不给出方药。",
        "verification_veto": [
            {"rule": rule, "herbs": ["党参"], "reason": "模型引用的原文对不上",
             "counterexample": "模型写的是「回阳救逆」；本体里党参的功效原文是「补中益气」"},
        ],
    }


def test_metrics_from_result_keeps_verification_veto_when_rejected():
    """这是本条修复的核心：`has_output=False` 分支原来直接扔掉整个 `result`，
    现在必须把 `verification_veto` 带出来，不然排障只能重跑真机。"""
    m = metrics_from_result(_fake_symbolic_veto_result())
    assert m["has_output"] is False
    assert m["verification_veto"] == [
        {"rule": "herb_source_fabricated", "herbs": ["党参"], "reason": "模型引用的原文对不上",
         "counterexample": "模型写的是「回阳救逆」；本体里党参的功效原文是「补中益气」"},
    ]


def test_metrics_from_result_verification_veto_carries_the_actual_rule_name():
    """换一条规则名（`herb_source_paraphrased`）也要原样带出来——不是只认
    `herb_source_fabricated` 这一个硬编码的规则名。"""
    m = metrics_from_result(_fake_symbolic_veto_result(rule="incompatible_pair"))
    assert m["verification_veto"][0]["rule"] == "incompatible_pair"


def test_metrics_from_result_does_not_invent_a_verification_veto_key_when_absent():
    """正常产出的问诊（没被拦）不该多出一个空的 `verification_veto` 键——
    没有就是没有，不是 `None` 占位，免得下游把"没被拦"跟"被拦了但值是 None"
    弄混。"""
    m = metrics_from_result(_fake_derived_result())
    assert "verification_veto" not in m
    m2 = metrics_from_result({"results": [{"s3": None}]})
    assert "verification_veto" not in m2


def test_aggregate_marks_rule_refs_not_applicable_for_structured_group():
    """跟 R38 的「不适用不是 0」同一条诚实约束：A 组走 structured，
    rule_refs 完整率这一格恒是「不适用」，不管跑了多少条主诉——**不看这一组
    实际有没有产出 rule_refs**，只看它是不是 structured（`group.env["S3_MODE"]`），
    因为"不适用"这件事由模式本身决定，不该被数据凑巧对不对得上影响。"""
    result = _fake_derived_result()
    del result["results"][0]["derivation_completeness_ratio"]
    rows = [{"ok": True, "llm_calls": 3, "elapsed_s": 1.0,
            "metrics": metrics_from_result(result)}]
    agg = aggregate(rows, group_by_key("A"), content_valid=True)
    assert agg["rule_refs_applicable"] is False
    assert agg["rule_refs_completeness_rate"] is None


def test_aggregate_computes_rule_refs_completeness_for_derived_groups():
    rows = [{"ok": True, "llm_calls": 3, "elapsed_s": 1.0,
            "metrics": metrics_from_result(_fake_derived_result(completeness=0.9))}]
    agg = aggregate(rows, group_by_key("C"), content_valid=True)
    assert agg["rule_refs_applicable"] is True
    assert agg["rule_refs_completeness_rate"]["value"] == 0.9


def test_fake_backend_content_metrics_are_none_regardless_of_data():
    rows = [{"ok": True, "llm_calls": 3, "elapsed_s": 1.0,
            "metrics": metrics_from_result(_fake_derived_result())}]
    agg = aggregate(rows, group_by_key("C"), content_valid=False)
    assert agg["verifier_first_pass_rate"] is None
    assert agg["rule_refs_completeness_rate"] is None
    assert agg["content_note"]


# ---------- C vs D 一致率 ----------

def test_pair_consistency_matches_on_record_id_not_position():
    """配对必须按 `record_id`，不能假设两组列表顺序一致——`run_group` 是
    顺序跑的，理论上顺序相同，但"理论上相同"不该是配对判据依赖的东西。"""
    c_rows = [{"record_id": "p2", "ok": True, "metrics": metrics_from_result(
                  _fake_derived_result(syndrome="X"))},
             {"record_id": "p1", "ok": True, "metrics": metrics_from_result(
                  _fake_derived_result(syndrome="Y"))}]
    d_rows = [{"record_id": "p1", "ok": True, "metrics": metrics_from_result(
                  _fake_derived_result(syndrome="Y"))},
             {"record_id": "p2", "ok": True, "metrics": metrics_from_result(
                  _fake_derived_result(syndrome="X"))}]
    out = pair_consistency(c_rows, d_rows, content_valid=True)
    assert out["value"] == 1.0
    assert out["denominator"] == 2


def test_pair_consistency_flags_a_real_mismatch():
    c_rows = [{"record_id": "p1", "ok": True,
              "metrics": metrics_from_result(_fake_derived_result(syndrome="脾气虚证"))}]
    d_rows = [{"record_id": "p1", "ok": True,
              "metrics": metrics_from_result(_fake_derived_result(syndrome="脾阳虚证"))}]
    out = pair_consistency(c_rows, d_rows, content_valid=True)
    assert out["value"] == 0.0
    assert out["mismatches"][0]["record_id"] == "p1"


def test_pair_consistency_skips_failed_runs_on_either_side():
    c_rows = [{"record_id": "p1", "ok": False, "metrics": {"has_output": False}}]
    d_rows = [{"record_id": "p1", "ok": True,
              "metrics": metrics_from_result(_fake_derived_result())}]
    out = pair_consistency(c_rows, d_rows, content_valid=True)
    assert out["denominator"] == 0


def test_pair_consistency_is_meaningless_under_the_fake_backend():
    out = pair_consistency([], [], content_valid=False)
    assert out["value"] is None
    assert "没有意义" in out["note"]


def test_the_two_gate_thresholds_are_the_ones_the_user_set():
    assert GATE_C_RULE_REFS_COMPLETENESS_MIN == 0.9
    assert GATE_CD_CONSISTENCY_MIN == 0.9


# ---------- R59：三处判定 bug ----------
# 用户真机跑 --limit 2 探针实测出来的三个假阳性：⏳ 被当成通过、样本量 1 也判
# 通过、B 组 rule_refs 满分具有误导性。这里用 build_report 端到端复现每一条
# （不是单独 mock 内部函数），因为 bug 本身就是"几个函数各自正确、拼起来才
# 出问题"那一类——跟 CLAUDE.md 第 31 条「第三次这类最难查」同一条判据。

def _rows(n: int, *, first_pass=True, completeness=1.0,
         syndrome="脾气虚证") -> list[dict]:
    # record_id **不带组前缀**：真实跑法是同一批主诉在四组里各跑一次，
    # `pair_consistency` 靠 record_id 把 C/D 两组的同一条主诉配对——带上组
    # 前缀（"C0"/"D0"）会让配对集合永远是空的，consistency 恒测不出来。
    return [{"record_id": f"q{i}", "complaint": "x", "ok": True, "error": None,
            "elapsed_s": 1.0, "llm_calls": 3,
            "metrics": metrics_from_result(_fake_derived_result(
                syndrome=syndrome, first_pass=first_pass, completeness=completeness))}
           for i in range(n)]


def _report(rows_by_group: dict, *, content_valid=True) -> dict:
    n = max(len(v) for v in rows_by_group.values())
    complaints = [{"record_id": f"q{i}", "syndrome": None, "complaint": "x"} for i in range(n)]
    return build_report(rows_by_group, backend_info={"id": "fake", "model": "m",
                                                      "comparability_warning": None},
                        complaints=complaints, content_valid=content_valid)


def test_a_pending_gate_does_not_get_silently_counted_as_passed():
    """**真机实测过的假阳性**：C 组一次过率、rule_refs 完整率两条门跑够了样本
    且都过，第三条（C-D 一致率，因为没跑 D 组）量不到——旧逻辑先把 None
    过滤掉再对剩下的做 all()，两条 True 就被判"全过"。正确答案是「判不了」，
    不是「过了」：⏳ 不是 ✅。"""
    n = GATE_MIN_SAMPLE_SIZE
    rows = {"A": _rows(n, first_pass=False), "C": _rows(n, first_pass=True,
                                                             completeness=0.95)}
    report = _report(rows)
    passed = [g["passed"] for g in report["gates"]]
    assert passed[0] is True   # C 一次过率 1.0 ≥ A 的 0.0
    assert passed[1] is True   # C rule_refs 完整率 0.95 ≥ 0.9
    assert passed[2] is None   # C-D 一致率：D 没跑，量不到
    assert report["all_gates_passed"] is None, (
        "两条过、一条没测出来 ≠ 全过——这正是真机报告里出现过的错误总判定")


def test_any_failing_gate_makes_the_overall_verdict_fail_even_with_a_pending_gate():
    n = GATE_MIN_SAMPLE_SIZE
    rows = {"A": _rows(n, first_pass=True),
           "C": _rows(n, first_pass=True, completeness=0.5)}  # 完整率不够
    report = _report(rows)
    assert report["gates"][1]["passed"] is False
    assert report["gates"][2]["passed"] is None  # D 没跑
    assert report["all_gates_passed"] is False, "有一条没过，不能因为另一条没测出来就打问号"


def test_all_three_gates_passing_with_enough_samples_is_the_only_true_case():
    n = GATE_MIN_SAMPLE_SIZE
    rows = {"A": _rows(n, first_pass=False),
           "C": _rows(n, first_pass=True, completeness=0.95),
           "D": _rows(n, first_pass=True, completeness=0.95)}
    report = _report(rows)
    assert all(g["passed"] is True for g in report["gates"])
    assert report["all_gates_passed"] is True


def test_a_sample_of_one_does_not_pass_the_gate_even_at_a_perfect_ratio():
    """**真机实测过的假阳性 #2**：`--limit 2` 探针里 C-D 一致率是「1（1/1）」——
    分母 1，理论上"100% 一致"，但样本量太小，这个 1.0 不代表任何东西。
    低于 `GATE_MIN_SAMPLE_SIZE` 时必须判「样本不足」，不能因为比率算出来
    正好 ≥ 阈值就放行。"""
    rows = {"A": _rows(1, first_pass=True), "C": _rows(1, first_pass=True,
                                                            completeness=1.0),
           "D": _rows(1, first_pass=True, completeness=1.0)}
    report = _report(rows)
    for gate in report["gates"]:
        assert gate["passed"] is None, gate
        assert "样本不足" in gate["detail"], gate
    assert report["all_gates_passed"] is None


def test_b_group_rule_refs_is_not_applicable_when_theory_layer_is_off():
    """**真机实测过的假阳性 #3**：B 组（THEORY_LAYER=off）rule_refs 完整率
    量出 1.0，C 组量出 0.375——不是"C 比 B 差"，是 B 那个 1.0 本身没有意义
    （B 组根本没有规则可引）。R59 把 B 组也标成「不适用」，理由跟 A 组
    （structured，没有这个键）不同，note 要能看出是哪一种不适用。"""
    b_group = group_by_key("B")
    assert b_group.env["THEORY_LAYER"] == "off"
    rows = _rows(GATE_MIN_SAMPLE_SIZE, first_pass=True, completeness=1.0)
    agg = aggregate(rows, b_group, content_valid=True)
    assert agg["rule_refs_applicable"] is False
    assert agg["rule_refs_completeness_rate"] is None
    assert "THEORY_LAYER" in agg["rule_refs_note"]

    a_group = group_by_key("A")
    a_agg = aggregate(_rows(GATE_MIN_SAMPLE_SIZE), a_group, content_valid=True)
    assert a_agg["rule_refs_applicable"] is False
    assert a_agg["rule_refs_note"] != agg["rule_refs_note"], (
        "A 组「不适用」和 B 组「不适用」是两个不同的原因，note 不能一样")


def test_c_and_d_still_report_rule_refs_as_applicable():
    for key in ("C", "D"):
        g = group_by_key(key)
        assert g.env["THEORY_LAYER"] == "on"
        agg = aggregate(_rows(GATE_MIN_SAMPLE_SIZE, completeness=0.9), g, content_valid=True)
        assert agg["rule_refs_applicable"] is True
        assert agg["rule_refs_note"] is None


def test_fmt_rate_shows_the_sample_size_next_to_the_ratio():
    assert _fmt_rate({"value": 0.8, "n": 4, "denominator": 5}) == "0.8（4/5）"
    assert _fmt_rate({"value": 0.95, "n": 20}) == "0.95（n=20）"
    assert _fmt_rate(None) == "⏳"
    assert _fmt_rate({"value": None}) == "⏳"


def test_fmt_rate_flags_a_small_sample_even_at_a_clean_ratio():
    small = _fmt_rate({"value": 1.0, "n": 1, "denominator": 1})
    assert "样本不足" in small
    big = _fmt_rate({"value": 1.0, "n": GATE_MIN_SAMPLE_SIZE, "denominator": GATE_MIN_SAMPLE_SIZE})
    assert "样本不足" not in big


# ---------- 主诉筛选（脾胃门 20 条） ----------

@pytest.fixture
def sdt_dir(tmp_path):
    """一份 4 条记录的合成 Train 数据：2 条脾胃门（1 条单证型、1 条双证型）、
    2 条非脾胃门——够验证筛选与排序逻辑，不需要真实 SDT 数据集。"""
    rows = [
        {"Medical Record ID": "病例9", "Clinical Data": "非脾胃门",
        "TCM Syndrome": "热伤阳络;血热妄行"},
        {"Medical Record ID": "病例3", "Clinical Data": "脾胃门单证",
        "TCM Syndrome": "脾胃虚弱"},
        {"Medical Record ID": "病例1", "Clinical Data": "脾胃门双证",
        "TCM Syndrome": "肝胃不和;痰热互结"},
        {"Medical Record ID": "病例5", "Clinical Data": "非脾胃门二",
        "TCM Syndrome": "心肾两亏"},
    ]
    d = tmp_path / "data"
    d.mkdir()
    (d / "Train_TCM_Data_v1.json").write_text(json.dumps(rows, ensure_ascii=False),
                                               encoding="utf-8")
    return tmp_path


def test_selection_filters_by_the_first_syndrome_term_not_substring_search(sdt_dir):
    """判据是"证型第一个词含脾/胃"，不是在 Clinical Data 原文里搜关键词——
    "非脾胃门"这个字符串本身含"脾胃"两个字，substring 搜索会把它也算进去，
    这条测试专门钉住这个反例。"""
    picked = select_pi_wei_men_complaints(sdt_dir, n=2)
    ids = {p["record_id"] for p in picked}
    assert ids == {"病例3", "病例1"}


def test_selection_prefers_fewer_syndrome_terms_then_lower_record_id(sdt_dir):
    picked = select_pi_wei_men_complaints(sdt_dir, n=2)
    assert [p["record_id"] for p in picked] == ["病例3", "病例1"]


def test_selection_is_deterministic_across_repeated_calls(sdt_dir):
    a = select_pi_wei_men_complaints(sdt_dir, n=2)
    b = select_pi_wei_men_complaints(sdt_dir, n=2)
    assert a == b


def test_selection_raises_when_not_enough_pi_wei_men_records(sdt_dir):
    with pytest.raises(ValueError, match="不够"):
        select_pi_wei_men_complaints(sdt_dir, n=20)


# ---------- fake 后端在 S3_MODE=derived 下真的能跑通 ----------
# 这两条是回归测试：R57 第一次真的用 --backend fake 跑 derived 模式时，
# scripts/bench_consult.py 里两处沉睡的假设被踩醒——不属于 R57 自己的逻辑，
# 但修复的动机是这一轮，放在这个文件里比另起一个文件更容易看出因果。

def test_fake_backend_produces_a_schema_valid_s3derived_payload():
    from scripts.bench_consult import minimal_payload
    from core.schemas import S3Derived

    payload = minimal_payload(S3Derived)
    S3Derived.model_validate(payload)  # 不抛就是过


def test_invalid_reason_does_not_misjudge_a_healthy_derived_result():
    from scripts.bench_consult import invalid_reason

    result = {"manifest": {"s3_mode": "derived"}, "results": [{"s3": object()}]}
    assert invalid_reason(result) is None
