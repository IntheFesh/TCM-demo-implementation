"""R38：MTCMB TCM-PR 适配的判据。**零 LLM、零网络**——数据不在这台机器上。

这里测的是"数据没来之前就能定死"的那几件事：切药名、归一后再比、
分母为 0 时报 None 不报 0、探针能不能把"字段猜错了"这件事说清楚、
两份产出口径不一致时不给差值。
"""
from __future__ import annotations

import json

import pytest

from eval.mtcmb.data import (
    FIELD_CANDIDATES,
    MAX_PLAUSIBLE_HERBS,
    load_records,
    probe,
    split_herbs,
)
from eval.mtcmb.run import compare
from eval.mtcmb.score import normalize_set, score_one, score_records


@pytest.mark.parametrize("text,expect", [
    ("柴胡、白芍、枳壳、甘草", ["柴胡", "白芍", "枳壳", "甘草"]),
    ("柴胡,白芍;枳壳 甘草", ["柴胡", "白芍", "枳壳", "甘草"]),
    ("柴胡 + 白芍", ["柴胡", "白芍"]),
    ("", []),
    ("、、、", []),
])
def test_split_herbs_handles_the_separators_that_actually_show_up(text, expect):
    assert split_herbs(text) == expect


def test_normalization_goes_through_the_one_implementation():
    """「炒白术」和「白术」是同一味药。按字面比会判成两味——这个项目在
    同一堵墙上撞过三次，所以归一只能走 `core.herbs.normalize_herb`。"""
    from core.herbs import normalize_herb

    assert normalize_set(["炒白术", "白术"]) == {normalize_herb("白术")}
    assert len(normalize_set(["炒白术", "白术"])) == 1
    assert "" not in normalize_set(["", "  "])


def test_score_one_counts_hits_after_normalizing():
    row = score_one(["炒白术", "茯苓", "陈皮"], ["白术", "茯苓", "半夏"])
    assert row["n_hit"] == 2
    assert row["precision"] == pytest.approx(2 / 3, abs=1e-4)
    assert row["recall"] == pytest.approx(2 / 3, abs=1e-4)
    assert row["exact"] is False


def test_an_empty_prediction_is_not_zero_precision():
    """**没作答 ≠ 答错。** 分母为 0 时报 None——把它算成 0 分，等于把
    "系统拒绝作答"和"系统答错了"算成同一件事。"""
    row = score_one([], ["白术"])
    assert row["precision"] is None
    assert row["f1"] is None
    assert row["empty_pred"] is True


def test_empty_answers_are_reported_separately_not_averaged_in():
    pairs = [("a", ["白术"], ["白术"]),
             ("b", [], ["茯苓"]),            # 安全否决 / 模型没作答
             ("c", ["陈皮"], [])]            # 参考方缺失（测试集不公开答案）
    out = score_records(pairs)
    assert out["n_records"] == 3
    assert out["n_scored"] == 1
    assert out["n_empty_pred"] == 1
    assert out["n_empty_gold"] == 1
    assert out["macro"]["f1"] == 1.0
    assert out["exact_match"] == {"n": 1, "denominator": 1, "value": 1.0}


def test_macro_and_micro_are_both_reported():
    """两个口径都要在，**报告里写清用的是哪一个**：micro 会让开了 20 味药
    的那条记录说了算，macro 每条一票。"""
    pairs = [("a", ["白术"], ["白术"]),
             ("b", ["茯苓", "陈皮", "半夏", "甘草"], ["茯苓"])]
    out = score_records(pairs)
    assert out["macro"]["precision"] == pytest.approx((1.0 + 0.25) / 2, abs=1e-4)
    assert out["micro"]["precision"] == pytest.approx(2 / 5, abs=1e-4)
    assert out["macro"]["denominator"] and out["micro"]["denominator"]


def test_the_score_says_it_is_not_the_official_number():
    """这个分不是官方分。**标记跟着数走**，不然两种来源的分迟早并排进一张表。"""
    out = score_records([("a", ["白术"], ["白术"])])
    assert out["scorer"] == "eval.mtcmb.score"


def _write(tmp_path, rows, name="pr.json"):
    (tmp_path / name).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_load_records_reads_the_declared_fields(tmp_path):
    d = _write(tmp_path, [{"id": "1", "question": "胃脘胀痛", "answer": "柴胡、白芍"}])
    [rec] = load_records(d)
    assert rec.record_id == "1" and rec.question == "胃脘胀痛"
    assert rec.gold_herbs == ("柴胡", "白芍")


def test_load_records_refuses_to_guess_when_the_question_field_is_missing(tmp_path):
    """**不返回一份半空的列表**：半空的列表会一路跑到打分那一步才露馅，
    而那时已经花了一整批调用的钱。报错里要带上"这条记录实际有哪些键"。"""
    d = _write(tmp_path, [{"id": "1", "描述": "胃脘胀痛", "answer": "柴胡"}])
    with pytest.raises(KeyError) as e:
        load_records(d)
    assert "描述" in str(e.value), "报错没说清这条记录实际有哪些键"


def test_probe_says_loudly_when_no_gold_prescription_was_read(tmp_path):
    """字段猜错的后果不是报错，是一份"所有人都得 0 分"的漂亮报告。
    探针必须把这句话说出来。"""
    d = _write(tmp_path, [{"id": "1", "question": "胃脘胀痛", "处方": "柴胡、白芍"}])
    report = probe(d)
    assert report["n_rows"] == 1 and report["n_with_answer"] == 0
    assert any("0 分" in p for p in report["problems"])
    assert "处方" in report["keys"]


def test_probe_flags_an_answer_field_that_is_clearly_prose(tmp_path):
    """参考方超过上限 = 多半把整段话当成了药名（分隔符不对或字段指错）。"""
    d = _write(tmp_path, [{"id": "1", "question": "q",
                           "answer": "、".join(f"药{i}" for i in range(MAX_PLAUSIBLE_HERBS + 5))}])
    report = probe(d)
    assert any(str(MAX_PLAUSIBLE_HERBS) in p for p in report["problems"])


def test_probe_flags_a_split_with_no_answers_at_all(tmp_path):
    """测试集常常不公开答案。**那份数据上不许算分**——探针要先说出来。"""
    d = _write(tmp_path, [{"id": "1", "question": "q", "answer": "柴胡"},
                          {"id": "2", "question": "q2"}])
    report = probe(d)
    assert any("没有参考方" in p for p in report["problems"])


def test_field_candidates_are_ordered_and_documented():
    """字段名没在这台机器上核过，所以"多认几个同义名"是有意的——
    但它必须是一张**看得见的表**，不是散落在代码里的 `row.get("x") or row.get("y")`。"""
    assert set(FIELD_CANDIDATES) == {"record_id", "question", "answer"}
    for names in FIELD_CANDIDATES.values():
        assert names and all(isinstance(n, str) for n in names)


def test_jsonl_and_bom_are_both_handled(tmp_path):
    """SDT 那边踩过 BOM（金标准第一条恒 0 分）。这里先剥掉。"""
    (tmp_path / "pr.jsonl").write_text(
        "﻿" + json.dumps({"id": "1", "question": "q", "answer": "柴胡"},
                              ensure_ascii=False) + "\n", encoding="utf-8")
    [rec] = load_records(tmp_path)
    assert rec.record_id == "1" and rec.gold_herbs == ("柴胡",)


def _report(solver, *, n=2, f1=0.5, safety=0, bypass=False):
    return {"solver": solver, "n_records": n, "llm_calls": n,
            "n_safety_rejected": safety, "ignore_safety_veto": bypass,
            "score": {"scorer": "eval.mtcmb.score",
                      "macro": {"f1": f1, "precision": f1, "recall": f1},
                      "micro": {"f1": f1}, "exact_match": {"value": f1}}}


def test_compare_gives_a_delta_only_when_both_sides_are_comparable():
    out = compare(_report("baseline", f1=0.4), _report("chain", f1=0.6))
    assert "| macro F1 | 0.4 | 0.6 | 0.2 |" in out
    # 一边旁路了安全层 = 两个数不可比，差值留空并说明
    mixed = compare(_report("baseline", f1=0.4),
                    _report("chain", f1=0.6, bypass=True))
    assert "| macro F1 | 0.4 | 0.6 | — |" in mixed
    assert "旁路了安全层" in mixed


def test_compare_always_says_the_score_is_not_official():
    out = compare(_report("baseline"), _report("chain"))
    assert "不是官方分" in out
