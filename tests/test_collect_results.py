"""scripts/collect_results.py 的测试。两类：

1. 合成 eval/ 目录，验证核对器本身的行为（凭据核对、ε 分层的两个方向、各种"取不到"）。
2. 对着仓库里的 eval/ 文件运行，钉住 eval/RESULTS.md 与 README.md 引用的每个数。
"""
import json
from pathlib import Path

import pytest

from scripts import collect_results as cr

# ---------- 合成 eval/ 目录 ----------


def _epsilon(per_query_means, global_mean):
    """per_query_means: 每条主诉一个值列表，核对器取均值作为这条主诉的地板。"""
    return {
        "epsilon_online": {
            "mean": global_mean, "p50": 0.0, "p95": 0.9, "n": 27,
            "n_queries": len(per_query_means), "n_queries_used": len(per_query_means),
            "n_repeats": 3, "llm_calls": 100,
            "by_physician": {"ye_tianshi": {"mean": 0.2, "n": 9}},
            "per_query": [
                {"query": f"主诉{i}", "skipped": False,
                 "by_physician": {"ye_tianshi": {"values": vals}}}
                for i, vals in enumerate(per_query_means)
            ],
        },
        "model": "deepseek-chat", "backend": "api", "generated_at": "2026-09-11T06:23:11Z",
        "comparability_warning": None,
    }


def _report(label, change_rate, backend=None):
    r = {
        "generated_at": "2026-09-12T00:00:00+00:00",
        "ablations": [{"label": label, "change_rate": change_rate, "n_usable": 27}],
        "hallucination": {"with_reference_cases": {"n": 27, "n_hallucinated": 0, "rate": 0.0},
                          "without_reference_cases": {"n": 0, "n_hallucinated": 0}},
    }
    if backend is not None:
        r["backend"] = backend
    return r


def _ledger():
    def row(solver, score, partial=False, ignore=False):
        return {"event": "run", "split": "Test", "solver": solver, "score": score,
                "partial": partial, "ignore_safety_veto": ignore, "n_records": 50,
                "model": "deepseek-chat", "backend": "api", "timestamp": "2026-09-12"}
    return [row("chain", 21.702), row("baseline", 22.068), row("chain", 22.833),
            row("chain", 27.729, partial=True, ignore=True)]


def _make_eval_dir(tmp_path, *, epsilon=None, e3=None, e8=None, ledger=True):
    d = tmp_path / "eval"
    (d / "sdt").mkdir(parents=True)
    (d / "epsilon.json").write_text(json.dumps(
        epsilon if epsilon is not None else _epsilon([[0.0], [0.6]], 0.3),
        ensure_ascii=False), encoding="utf-8")
    (d / "report_e3.json").write_text(json.dumps(
        e3 if e3 is not None else _report("swapped", 0.3348), ensure_ascii=False),
        encoding="utf-8")
    (d / "report_e4.json").write_text(json.dumps(_report("none", 0.3514), ensure_ascii=False),
                                      encoding="utf-8")
    (d / "report_e9.json").write_text(json.dumps(_report("react_on", 0.2503), ensure_ascii=False),
                                      encoding="utf-8")
    (d / "report_e8.json").write_text(json.dumps(
        e8 if e8 is not None else {
            "generated_at": "2026-09-12T00:00:00+00:00",
            "retriever_mode_effect": {"output_difference_rate": 0.366,
                                      "graph_mode_caveat": "覆盖率 444/941（47%）"}},
        ensure_ascii=False), encoding="utf-8")
    if ledger:
        # ledger=True 用默认台账；也可以传一份自定义台账（两段式台账的测试要加 event=scored 行）。
        rows = _ledger() if ledger is True else ledger
        (d / "sdt" / "test_run_log.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return d


# ---------- evidence_value：三种"取不到"分开 ----------


def test_evidence_value_reads_each_registered_key(tmp_path):
    d = _make_eval_dir(tmp_path)
    assert cr.evidence_value("e3.change_rate", d)[0] == 0.3348
    assert cr.evidence_value("e4.change_rate", d)[0] == 0.3514
    assert cr.evidence_value("e9.change_rate", d)[0] == 0.2503
    assert cr.evidence_value("e8.output_difference_rate", d)[0] == 0.366
    assert cr.evidence_value("hallucination.n", d)[0] == 27
    assert cr.evidence_value("sdt.baseline", d)[0] == 22.068
    assert cr.evidence_value("sdt.ignore_safety_veto", d)[0] == 27.729


def test_sdt_first_and_last_follow_ledger_order_not_timestamps(tmp_path):
    """台账是追加写的，顺序就是时间顺序。同一天多次运行的时间戳可能相同，
    按时间戳排序不稳定，所以 first/last 按列表顺序取。"""
    d = _make_eval_dir(tmp_path)
    assert cr.evidence_value("sdt.chain_first", d)[0] == 21.702
    assert cr.evidence_value("sdt.chain_last", d)[0] == 22.833


def test_sdt_score_comes_from_the_scored_event_when_the_run_row_has_none(tmp_path):
    """两段式台账：一次运行留下的 event=run 行 `score` 为 None（运行结束时官方计分
    还没跑），分数在之后的 event=scored 行上。取值器必须跨事件链接，否则每运行一次
    SDT，chain_last 就取不到值。"""
    ledger = _ledger() + [
        {"event": "run", "split": "Test", "solver": "chain", "score": None,
         "submission": "out/sdt_chain_v3.txt", "partial": False,
         "ignore_safety_veto": False, "n_records": 50, "model": "deepseek-chat"},
        {"event": "scored", "split": "Test", "submission": "out/sdt_chain_v3.txt",
         "score": 23.173103937264152, "score_kind": "official_automated_score"},
    ]
    d = _make_eval_dir(tmp_path, ledger=ledger)
    assert cr.evidence_value("sdt.chain_last", d)[0] == 23.173103937264152
    # 前面几条不受影响：内联分数优先，不会被之后的 scored 事件覆盖
    assert cr.evidence_value("sdt.chain_first", d)[0] == 21.702
    assert cr.evidence_value("sdt.baseline", d)[0] == 22.068


def test_sdt_score_links_by_submission_not_by_adjacency(tmp_path):
    """链接靠 submission 字段，不靠"紧挨着的上一条"：两条之间可以插进别的事件。"""
    ledger = _ledger() + [
        {"event": "run", "split": "Test", "solver": "chain", "score": None,
         "submission": "out/A.txt", "partial": False, "ignore_safety_veto": False},
        {"event": "scored", "split": "Test", "submission": "out/B.txt", "score": 99.9},
        {"event": "scored", "split": "Test", "submission": "out/A.txt", "score": 23.173},
    ]
    d = _make_eval_dir(tmp_path, ledger=ledger)
    assert cr.evidence_value("sdt.chain_last", d)[0] == 23.173


def test_sdt_score_is_none_when_the_run_was_never_scored(tmp_path):
    """运行过但没有计分：报"取不到"，不退回一个更早的分数。"""
    ledger = _ledger() + [
        {"event": "run", "split": "Test", "solver": "chain", "score": None,
         "submission": "out/never_scored.txt", "partial": False,
         "ignore_safety_veto": False},
    ]
    d = _make_eval_dir(tmp_path, ledger=ledger)
    assert cr.evidence_value("sdt.chain_last", d)[0] is None


def test_evidence_value_rejects_a_key_not_in_the_registry(tmp_path):
    with pytest.raises(KeyError, match="不在注册表里"):
        cr.evidence_value("e5.change_rate", _make_eval_dir(tmp_path))


def test_evidence_value_returns_none_when_the_file_is_missing(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    value, path = cr.evidence_value("e3.change_rate", d)
    assert value is None and not path.exists()


def test_evidence_value_returns_none_when_the_key_is_absent_from_the_report(tmp_path):
    """文件在、但报告里没有这一项，与"文件不存在"分开：前者要重新运行评测，
    后者要提交文件。"""
    d = _make_eval_dir(tmp_path, e3={"generated_at": "x", "ablations": []})
    value, path = cr.evidence_value("e3.change_rate", d)
    assert value is None and path.exists()


# ---------- collect：后端字段的两种形状 ----------


def test_collect_reads_a_plain_string_backend(tmp_path):
    rows = {r["key"]: r for r in cr.collect(_make_eval_dir(tmp_path))}
    assert rows["epsilon_online.mean"]["backend"] == "api"
    assert rows["epsilon_online.mean"]["model"] == "deepseek-chat"


def test_collect_reads_the_backend_tags_block(tmp_path):
    """run_eval 的报告里 backend 是 backend_tags() 产出的块，不是字符串。"""
    d = _make_eval_dir(tmp_path, e3=_report("swapped", 0.4,
                                            backend={"models": ["tcm-local"],
                                                     "backends": ["local"]}))
    rows = {r["key"]: r for r in cr.collect(d)}
    assert rows["e3.change_rate"]["backend"] == ["local"]
    assert rows["e3.change_rate"]["model"] == "tcm-local"


def test_collect_leaves_backend_none_when_the_report_has_no_tag(tmp_path):
    """报告里没有 backend 就报 None，不替它猜一个默认值。"""
    rows = {r["key"]: r for r in cr.collect(_make_eval_dir(tmp_path))}
    assert rows["e3.change_rate"]["backend"] is None


def test_collect_marks_a_missing_file_instead_of_dropping_the_row(tmp_path):
    d = _make_eval_dir(tmp_path, ledger=False)
    rows = {r["key"]: r for r in cr.collect(d)}
    assert rows["sdt.baseline"]["exists"] is False
    assert rows["sdt.baseline"]["value"] is None
    assert "sdt.baseline" in rows        # 缺文件的键也在表里占一行


def test_extra_notes_carries_the_caveat_verbatim(tmp_path):
    notes = cr.extra_notes(_make_eval_dir(tmp_path))
    assert notes["e8.graph_mode_caveat"] == "覆盖率 444/941（47%）"


# ---------- ε 分层：两个方向的错分开报 ----------


def test_epsilon_by_query_averages_all_pairs_of_a_query(tmp_path):
    d = _make_eval_dir(tmp_path, epsilon=_epsilon([[0.0, 0.4], [0.6]], 0.3))
    rows = cr.epsilon_by_query(d)
    assert rows[0]["mean"] == 0.2 and rows[0]["min"] == 0.0 and rows[0]["max"] == 0.4
    assert rows[0]["n_values"] == 2


def test_epsilon_stratification_counts_both_error_directions(tmp_path):
    """用全局均值一刀切会同时犯两个方向的错：地板低于全局均值的会漏判，高于的会误判。
    两个方向分开计数，不合成一个"错判率"。"""
    d = _make_eval_dir(tmp_path, epsilon=_epsilon([[0.0], [0.1], [0.5], [0.6]], 0.3))
    s = cr.epsilon_stratification(d)
    assert s["n_floor_below_global"] == 2      # 0.0 / 0.1
    assert s["n_floor_above_global"] == 2      # 0.5 / 0.6
    assert s["n_floor_zero"] == 1
    assert s["per_query_mean_min"] == 0.0 and s["per_query_mean_max"] == 0.6
    assert s["max_over_global"] == 2.0         # 0.6 / 0.3


def test_epsilon_stratification_excludes_skipped_queries(tmp_path):
    eps = _epsilon([[0.0], [0.6]], 0.3)
    eps["epsilon_online"]["per_query"].append(
        {"query": "被安全否决的一条", "skipped": True, "by_physician": {}})
    d = _make_eval_dir(tmp_path, epsilon=eps)
    s = cr.epsilon_stratification(d)
    assert s["n_queries_used"] == 2 and s["n_skipped"] == 1


def test_epsilon_helpers_are_empty_without_the_file(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert cr.epsilon_by_query(d) == [] and cr.epsilon_stratification(d) == {}


# ---------- --check：凭据核对 ----------


def _md(row: str) -> str:
    """表头必须带「凭据」列：`metric_rows()` 靠它识别指标表。"""
    return "| # | 指标 | 后端 | 当前值 | 凭据 |\n|---|---|---|---|---|\n" + row + "\n"


def test_parse_evidence_tokens_reads_file_key_value():
    tokens = cr.parse_evidence_tokens("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |")
    assert len(tokens) == 1
    assert tokens[0]["file"] == "report_e3.json"
    assert tokens[0]["key"] == "e3.change_rate"
    assert tokens[0]["stated"] == "0.335"


def test_check_passes_when_the_stated_value_rounds_to_the_file_value(tmp_path):
    """文档按三位小数写 0.335、文件里是 0.3348，算一致。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | deepseek | 0.335 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert result["ok"] and result["checked"] == 1


def test_check_catches_a_number_that_drifted_from_the_file(tmp_path):
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | deepseek | 0.34 | `report_e3.json:e3.change_rate=0.34` |"), d)
    assert not result["ok"]
    assert result["mismatches"][0]["actual"] == 0.3348
    assert result["mismatches"][0]["stated"] == "0.34"


def test_check_catches_an_unregistered_key(tmp_path):
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | x | 0.1 | `report_e3.json:e3.made_up=0.1` |"), d)
    assert not result["ok"] and "不在注册表里" in result["unresolved"][0]["reason"]


def test_check_catches_a_token_pointing_at_the_wrong_file(tmp_path):
    """每个键注册在哪个文件是固定的。把 e3 的键挂到 report_e4.json 上要报错。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | x | 0.335 | `report_e4.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and "注册的是" in result["unresolved"][0]["reason"]


def test_check_catches_a_missing_file(tmp_path):
    d = _make_eval_dir(tmp_path, ledger=False)
    result = cr.check(_md("| 7 | SDT | x | 22.068 | `sdt/test_run_log.jsonl:sdt.baseline=22.068` |"), d)
    assert not result["ok"] and "文件不存在" in result["unresolved"][0]["reason"]


def test_check_catches_a_key_absent_from_the_report(tmp_path):
    d = _make_eval_dir(tmp_path, e3={"generated_at": "x", "ablations": []})
    result = cr.check(_md("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and "取不到键" in result["unresolved"][0]["reason"]


def test_check_catches_evidence_that_disagrees_with_its_own_row(tmp_path):
    """凭据写 0.335、正文写 0.451：凭据支持的不是这一行展示的数。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | x | 0.451 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and result["missing_in_line"][0]["stated"] == "0.335"


def test_check_fails_on_a_row_whose_evidence_cell_is_blank(tmp_path):
    """凭据表里的编号行没有记号 = 漏标，算失败。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 9 | E2 | deepseek | 0.420 vs 0.569 | |"), d)
    assert not result["ok"]
    assert len(result["rows_unmarked"]) == 1
    assert "没有凭据记号" in cr.format_check(result)


def test_check_only_treats_numbered_rows_in_an_evidence_table_as_metric_rows(tmp_path):
    """文档里还有别的说明性表格；只有带「凭据」列的表里的编号行才要求带记号。"""
    d = _make_eval_dir(tmp_path)
    text = ("| 说明 | 文件里的值 |\n|---|---|\n"
            "| 1 | 0.3348 |\n"                     # 编号行，但不在带「凭据」的表里
            "\n"
            "| # | 指标 | 后端 | 当前值 | 凭据 |\n|---|---|---|---|---|\n"
            "| 3 | E3 | deepseek | 0.451 | 见上 |\n")
    result = cr.check(text, d)
    assert len(result["rows_unmarked"]) == 1
    assert result["rows_unmarked"][0].startswith("| 3 |")


def test_check_accepts_dash_suffixed_row_numbering(tmp_path):
    """并列行的编号可以写成 `3-local`，同样算指标行。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3-local | E3 | tcm-local | 0.451 | |"), d)
    assert len(result["rows_unmarked"]) == 1


def test_metric_rows_only_looks_inside_a_table_with_an_evidence_column():
    """判据是结构性的：表头里有「凭据」列的表才是指标表。"""
    text = ("| 块 | 一句话 | 详细 |\n|---|---|---|\n| 1 | 系统构成 | 下一节 |\n"
            "\n"
            "| # | 指标 | 凭据 |\n|---|---|---|\n| 3 | E3 | x |\n")
    rows = cr.metric_rows(text)
    assert len(rows) == 1 and rows[0].startswith("| 3 |")


# ---------- md / json 同步检查与重渲染 ----------


def _md_for(json_path, ts):
    json_path.with_suffix(".md").write_text(
        f"# 评测汇总（{ts}）\n\n共 10 条查询。\n", encoding="utf-8")


def test_md_json_sync_flags_a_stale_md(tmp_path):
    """凭据记号只核 json 里的数，md 是否与 json 同一次渲染要单独检查。"""
    d = _make_eval_dir(tmp_path)
    # _report() 里的 generated_at 是 2026-09-12，md 写成 09-01
    _md_for(d / "report_e3.json", "2026-09-01T00:00:00+00:00")
    drift = cr.check_md_json_sync(d)
    assert len(drift) == 1
    assert drift[0]["json"] == "report_e3.json"
    assert drift[0]["md_ts"] == "2026-09-01T00:00:00+00:00"


def test_md_json_sync_is_quiet_when_they_match(tmp_path):
    d = _make_eval_dir(tmp_path)
    _md_for(d / "report_e3.json", "2026-09-12T00:00:00+00:00")
    (d / "report_e3.json").write_text(json.dumps(
        dict(_report("swapped", 0.3348), generated_at="2026-09-12T00:00:00+00:00"),
        ensure_ascii=False), encoding="utf-8")
    assert cr.check_md_json_sync(d) == []


def test_md_json_sync_skips_a_json_with_no_md(tmp_path):
    """只有 json 没有 md 不算不一致：那是还没有渲染过。"""
    assert cr.check_md_json_sync(_make_eval_dir(tmp_path)) == []


def test_check_fails_on_md_drift_even_when_every_token_matches(tmp_path):
    d = _make_eval_dir(tmp_path)
    _md_for(d / "report_e3.json", "2026-09-01T00:00:00+00:00")
    result = cr.check(_md("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and result["mismatches"] == []
    assert len(result["md_json_drift"]) == 1
    assert "--rerender" in cr.format_check(result)


def test_rerender_rewrites_the_stale_md_from_its_own_json(tmp_path):
    """md 是 json 的确定性渲染产物，重渲染零 LLM 调用、不产生新数字。"""
    d = _make_eval_dir(tmp_path)
    e8 = d / "report_e8.json"
    report = json.loads(e8.read_text(encoding="utf-8"))
    # 补齐 render_markdown 需要的键（合成 report 只有核对凭据用的几段）
    report.update({
        "n_queries": 10,
        "divergence_vs_epsilon": {"available": True, "note": "n"},
        "divergence_per_query": [],
        "school_pairs": {"note": "s"},
        "hallucination": {"note": "h"},
        "safety_veto": {"note": "v"},
        "retrieval_mode_comparisons": [], "ablations": [],
        "react_process": None,
        "retriever_mode_effect": {"output_difference_rate": 0.366, "note": "r"},
    })
    e8.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    _md_for(e8, "2026-09-01T00:00:00+00:00")
    done = cr.rerender_drifted_md(d)
    assert [x["rendered"] for x in done] == [True]
    assert cr.check_md_json_sync(d) == []
    assert report["generated_at"] in (d / "report_e8.md").read_text(encoding="utf-8")


def test_rerender_reports_a_missing_key_instead_of_crashing(tmp_path):
    """json 缺 render_markdown 需要的键时，报出缺哪个键。"""
    d = _make_eval_dir(tmp_path)
    _md_for(d / "report_e3.json", "2026-09-01T00:00:00+00:00")
    done = cr.rerender_drifted_md(d)
    assert done and done[0]["rendered"] is False and "缺键" in done[0]["reason"]


# ---------- 各类凭据键 ----------


def test_paired_divergence_keys_count_verdicts_not_the_global_cut(tmp_path):
    """`divergence_per_query` 是逐条配对 ε 的判决，`divergence_vs_epsilon` 用全局 ε 计算。
    同一份文件里两者都有，凭据键只取前者。"""
    d = _make_eval_dir(tmp_path)
    e3 = d / "report_e3.json"
    report = json.loads(e3.read_text(encoding="utf-8"))
    report["divergence_per_query"] = (
        [{"verdict": "real_divergence"}] * 9 + [{"verdict": "unusable"}])
    e3.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert cr.evidence_value("e3.paired_real_divergence", d)[0] == 9
    assert cr.evidence_value("e3.paired_unusable", d)[0] == 1
    assert cr.evidence_value("e3.paired_within_noise", d)[0] == 0


def test_school_keys_read_from_each_report_separately(tmp_path):
    """两份报告的 school_pairs 各自读取，文档并列报出两者。"""
    d = _make_eval_dir(tmp_path)
    for name, lineage, cross in (("report_e8.json", 0.568, 0.557),
                                 ("report_e9.json", 0.453, 0.584)):
        path = d / name
        report = json.loads(path.read_text(encoding="utf-8"))
        report["school_pairs"] = {"lineage_mean": lineage, "cross_school_mean": cross,
                                  "n_cross_school_gt_lineage": 4}
        path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    assert cr.evidence_value("e8.school_lineage_mean", d)[0] == 0.568
    assert cr.evidence_value("e9.school_lineage_mean", d)[0] == 0.453


def test_knowledge_base_keys_count_lines_of_the_data_files(tmp_path):
    """知识库规模的键直接数 data/standard/ 下的 jsonl 行数；路径写成相对 eval/ 的形式。"""
    d = _make_eval_dir(tmp_path)
    std = tmp_path / "data" / "standard"
    std.mkdir(parents=True)
    (std / "materia_medica.jsonl").write_text('{"a": 1}\n{"a": 2}\n\n', encoding="utf-8")
    (std / "formulary.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    assert cr.evidence_value("pharmacology.n_materia_medica", d)[0] == 2
    assert cr.evidence_value("pharmacology.n_formulary", d)[0] == 1


def test_epsilon_stratification_keys_are_registered_and_readable():
    for key in ("epsilon.stratification.global_mean",
                "epsilon.stratification.n_queries_used",
                "epsilon.stratification.per_query_mean_min",
                "epsilon.stratification.per_query_mean_max",
                "epsilon.stratification.n_floor_below_global",
                "epsilon.stratification.n_floor_above_global",
                "epsilon.stratification.max_over_global"):
        assert key in cr.EVIDENCE, f"{key} 不在注册表里"
        value, path = cr.evidence_value(key)
        assert path.exists() and value is not None, f"{key} 取不到：{value}"


def test_the_stratification_getter_and_the_printer_agree():
    """注册表的取值函数与 `epsilon_stratification()` 共用 `_stratification_from`，结果一致。"""
    strat = cr.epsilon_stratification()
    assert cr.evidence_value("epsilon.stratification.global_mean")[0] == strat["global_mean"]
    assert cr.evidence_value("epsilon.stratification.n_floor_above_global")[0] \
        == strat["n_floor_above_global"]
    assert cr.evidence_value("epsilon.stratification.max_over_global")[0] \
        == strat["max_over_global"]


# ---------- 仓库里的真实文件 ----------


def test_default_check_paths_are_results_md_and_readme():
    names = {p.name for p in cr.DEFAULT_CHECK_PATHS}
    assert names == {"RESULTS.md", "README.md"}
    assert all(p.exists() for p in cr.DEFAULT_CHECK_PATHS)


def test_repo_results_md_evidence_all_checks_out():
    """改了 RESULTS.md 里带记号的数，或改了它引用的报告文件，这一条就失败。"""
    text = cr.DEFAULT_RESULTS_MD.read_text(encoding="utf-8")
    result = cr.check(text)
    assert result["ok"], cr.format_check(result)
    assert result["checked"] >= 40


def test_repo_readme_evidence_all_checks_out():
    """README 里的评测数字与 RESULTS.md 走同一套凭据记号、同一个核对器。"""
    readme = Path(cr.ROOT) / "README.md"
    result = cr.check(readme.read_text(encoding="utf-8"))
    assert result["ok"], cr.format_check(result)
    assert result["checked"] >= 5


def test_every_report_md_is_in_sync_with_its_json():
    assert cr.check_md_json_sync() == []


def test_repo_epsilon_stratification_has_both_error_directions():
    """RESULTS.md「ε 按主诉分层」一节的结论需要两个方向都非零。"""
    s = cr.epsilon_stratification()
    assert s["n_floor_below_global"] > 0 and s["n_floor_above_global"] > 0
    assert s["n_floor_below_global"] + s["n_floor_above_global"] <= s["n_queries_used"]


def test_the_two_e2_reports_point_in_opposite_directions():
    """RESULTS.md 的读数说明写的是：两份报告的 E2 判据一份成立、一份不成立，
    所以 E2 只报出、不设闸门。两份报告任何一份变了，这一条会提醒更新那段说明。"""
    e9_holds = (cr.evidence_value("e9.school_cross_mean")[0]
                > cr.evidence_value("e9.school_lineage_mean")[0])
    e8_holds = (cr.evidence_value("e8.school_cross_mean")[0]
                > cr.evidence_value("e8.school_lineage_mean")[0])
    assert e9_holds and not e8_holds


def test_results_md_uses_the_stratification_keys():
    results = cr.DEFAULT_RESULTS_MD.read_text(encoding="utf-8")
    assert "epsilon.stratification.global_mean=" in results
    assert "epsilon.stratification.max_over_global=" in results


# ---------- CLI ----------


def test_main_prints_the_collected_table(tmp_path, capsys):
    cr.main(["--eval-dir", str(_make_eval_dir(tmp_path))])
    out = capsys.readouterr().out
    assert "各报告文件里的实际数值" in out
    assert "e3.change_rate" in out and "0.3348" in out
    assert "ε 分层" in out


def test_main_json_output_is_machine_readable(tmp_path, capsys):
    cr.main(["--eval-dir", str(_make_eval_dir(tmp_path)), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert {"collected", "notes", "epsilon_stratification", "epsilon_by_query"} <= set(data)


def test_main_check_exits_zero_when_consistent(tmp_path, capsys):
    d = _make_eval_dir(tmp_path)
    md = tmp_path / "R.md"
    md.write_text(_md("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |"),
                  encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cr.main(["--eval-dir", str(d), "--check", str(md)])
    assert e.value.code == 0
    assert "全部一致" in capsys.readouterr().out


def test_main_check_exits_nonzero_on_drift(tmp_path, capsys):
    d = _make_eval_dir(tmp_path)
    md = tmp_path / "R.md"
    md.write_text(_md("| 3 | E3 | x | 0.34 | `report_e3.json:e3.change_rate=0.34` |"),
                  encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        cr.main(["--eval-dir", str(d), "--check", str(md)])
    assert e.value.code == 1
    assert "文件里实际是 0.3348" in capsys.readouterr().out


def test_main_rerender_says_nothing_to_do_when_in_sync(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cr.main(["--eval-dir", str(_make_eval_dir(tmp_path)), "--rerender"])
    assert e.value.code == 0
    assert "没有需要重渲染的 md" in capsys.readouterr().out
