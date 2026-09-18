"""R57 断点续跑：`eval.ablation.merge_r57` 把按组分别存盘的局部报告合成一份
完整报告。零 LLM、零网络——用 `eval.ablation.r57.build_report` 现成的聚合
逻辑构造局部文件，不重新实现一份假聚合。
"""
from __future__ import annotations

import json

import pytest

from eval.ablation.merge_r57 import merge
from eval.ablation.r57 import build_report, metrics_from_result

COMPLAINTS = [{"record_id": "p1", "syndrome": "脾气虚证", "complaint": "纳差乏力"},
             {"record_id": "p2", "syndrome": "脾阳虚证", "complaint": "腹泻畏寒"}]


def _fake_result(syndrome="脾气虚证"):
    return {
        "results": [{
            "s3": object(),
            "s3_structured": {"syndrome": {"name": syndrome}, "method": {"principle": "健脾"},
                              "formula": {"candidate": {"name": "四君子汤"}}},
            "herbs_grounded_ratio": 0.8,
            "verifier_metrics": {"verifier_first_pass": True},
            "derivation_completeness_ratio": 1.0,
            "hallucinated": [],
        }],
        "manifest": {"llm_calls": 3, "s3_mode": "derived"},
    }


def _rows(group_key: str, syndrome="脾气虚证"):
    return [{"record_id": c["record_id"], "complaint": c["complaint"], "ok": True,
            "error": None, "elapsed_s": 1.0, "llm_calls": 3,
            "metrics": metrics_from_result(_fake_result(syndrome))}
           for c in COMPLAINTS]


def _write_group_report(tmp_path, group_key: str, *, syndrome="脾气虚证",
                        content_valid=True, backend_id="api"):
    report = build_report(
        {group_key: _rows(group_key, syndrome)},
        backend_info={"id": backend_id, "model": "deepseek-v4-pro", "comparability_warning": None},
        complaints=COMPLAINTS, content_valid=content_valid)
    path = tmp_path / f"report_{group_key}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def test_merging_four_single_group_files_recovers_all_four_groups(tmp_path):
    paths = [_write_group_report(tmp_path, g) for g in "ABCD"]
    merged = merge(paths)
    assert set(merged["rows"]) == {"A", "B", "C", "D"}
    assert set(merged["groups"]) == {"A", "B", "C", "D"}


def test_merged_report_computes_c_vs_d_consistency_from_raw_rows(tmp_path):
    """局部文件里没有 pair_consistency（那需要同时看到 C 和 D 的原始行）——
    合并之后重新跑 build_report 才会算出来，这是「不是拼 JSON 文本」的证据。"""
    paths = [_write_group_report(tmp_path, "C", syndrome="脾气虚证"),
            _write_group_report(tmp_path, "D", syndrome="脾气虚证")]
    for p in paths:
        assert json.loads(p.read_text(encoding="utf-8"))["c_vs_d_consistency"] is None
    merged = merge(paths)
    assert merged["c_vs_d_consistency"]["value"] == 1.0


def test_merged_report_flags_a_real_c_vs_d_mismatch_across_files(tmp_path):
    paths = [_write_group_report(tmp_path, "C", syndrome="脾气虚证"),
            _write_group_report(tmp_path, "D", syndrome="脾阳虚证")]
    merged = merge(paths)
    assert merged["c_vs_d_consistency"]["value"] == 0.0


def test_mismatched_complaints_refuses_to_merge(tmp_path):
    """一份报告的主诉集合跟另一份对不上——最典型的场景是 --sdt-dir 中途换了
    数据集版本，或者手滑把另一轮跑的文件传了进来。合并这种输入会让 C/D 配对
    比较悄悄错配，必须直接拒绝。"""
    good = _write_group_report(tmp_path, "A")
    other_complaints = [{"record_id": "q1", "syndrome": None, "complaint": "别的主诉"}]
    bad_report = build_report(
        {"B": [{"record_id": "q1", "complaint": "别的主诉", "ok": True, "error": None,
               "elapsed_s": 1.0, "llm_calls": 3, "metrics": metrics_from_result(_fake_result())}]},
        backend_info={"id": "api", "model": "deepseek-v4-pro", "comparability_warning": None},
        complaints=other_complaints, content_valid=True)
    bad = tmp_path / "report_B_mismatched.json"
    bad.write_text(json.dumps(bad_report, ensure_ascii=False, default=str), encoding="utf-8")
    with pytest.raises(SystemExit):
        merge([good, bad])


def test_mixed_fake_and_real_backend_refuses_to_merge(tmp_path):
    """一份是 --backend fake 冒烟、一份是 --backend real 真机——两者的
    content_metrics_valid 不一样，混进一份报告会让内容指标看起来像是真机
    数字，实际掺了假后端的固定假文本。"""
    real = _write_group_report(tmp_path, "A", content_valid=True)
    fake = _write_group_report(tmp_path, "B", content_valid=False)
    with pytest.raises(SystemExit):
        merge([real, fake])


def test_missing_groups_does_not_crash_but_the_gate_reads_as_missing_data(tmp_path):
    """只合并出 A/B 两组——C/D 缺失时不该崩，闸门该诚实地读成「缺数据」不是
    「跑了但没过」（CLAUDE.md 的三分返回值：没跑 / 跑了没过 / 跑了过了）。"""
    paths = [_write_group_report(tmp_path, "A"), _write_group_report(tmp_path, "B")]
    merged = merge(paths)
    assert set(merged["rows"]) == {"A", "B"}
    for gate in merged["gates"]:
        assert gate["passed"] is None


def test_missing_file_raises_a_clear_error(tmp_path):
    with pytest.raises(SystemExit):
        merge([tmp_path / "does_not_exist.json"])
