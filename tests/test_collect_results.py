"""scripts/collect_results.py 的测试。两类：

1. 合成 eval/ 目录，验脚本本身的行为（凭据核对、ε 分层的两个方向、各种"取不到"）。
2. **对着仓库里真实的 eval/ 文件跑**，把 eval/RESULTS.md 引用的每个数钉住——
   这一类是这个脚本存在的理由：手抄的数会漂，而漂的时候没有任何东西会报错。
"""
import json

import pytest

from scripts import collect_results as cr

# ---------- 合成 eval/ 目录 ----------


def _epsilon(per_query_means, global_mean):
    """per_query_means: 每条主诉一个（值列表），脚本取均值当这条的地板。"""
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
        # ledger 可以传一份自定义台账（True = 用默认的 _ledger()）。两段式台账
        # 那几条测试要往里塞 event=scored 行，光有一个开关不够。
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
    """台账是追加写的，顺序就是时间顺序。同一天几次跑的时间戳一样，
    按时间戳排序不稳定——所以 first/last 认列表顺序。"""
    d = _make_eval_dir(tmp_path)
    assert cr.evidence_value("sdt.chain_first", d)[0] == 21.702
    assert cr.evidence_value("sdt.chain_last", d)[0] == 22.833


def test_sdt_score_comes_from_the_scored_event_when_the_run_row_has_none(tmp_path):
    """**两段式台账**：真跑一次留下的 event=run 行，`score` 按设计就是 None
    （log_run 的注释：分数在这一刻还不知道，由 scored 事件补），分数在后来的
    event=scored 行上。

    这条钉的是取值器必须跨事件链接。修复前它取的是 run 行的 score，于是
    **每真跑一次 SDT，chain_last 就变成 None 一次**——护栏在每次正常使用
    之后自己失效，而回填进来的那四条因为分数内联在 run 行里，一直是绿的、
    把这个缺陷盖住了。
    """
    ledger = _ledger() + [
        {"event": "run", "split": "Test", "solver": "chain", "score": None,
         "submission": "out/sdt_chain_v3.txt", "partial": False,
         "ignore_safety_veto": False, "n_records": 50, "model": "deepseek-chat"},
        {"event": "scored", "split": "Test", "submission": "out/sdt_chain_v3.txt",
         "score": 23.173103937264152, "score_kind": "official_automated_score"},
    ]
    d = _make_eval_dir(tmp_path, ledger=ledger)
    assert cr.evidence_value("sdt.chain_last", d)[0] == 23.173103937264152
    # 前面几条不受影响：内联分数优先，不会被后来的 scored 事件串味
    assert cr.evidence_value("sdt.chain_first", d)[0] == 21.702
    assert cr.evidence_value("sdt.baseline", d)[0] == 22.068


def test_sdt_score_links_by_submission_not_by_adjacency(tmp_path):
    """链接靠 submission 字段，不靠"紧挨着的上一条"。台账是追加写的，两条
    之间可以插进别的事件；按相邻猜会在那时候悄悄取错一份提交的分数。"""
    ledger = _ledger() + [
        {"event": "run", "split": "Test", "solver": "chain", "score": None,
         "submission": "out/A.txt", "partial": False, "ignore_safety_veto": False},
        {"event": "scored", "split": "Test", "submission": "out/B.txt", "score": 99.9},
        {"event": "scored", "split": "Test", "submission": "out/A.txt", "score": 23.173},
    ]
    d = _make_eval_dir(tmp_path, ledger=ledger)
    assert cr.evidence_value("sdt.chain_last", d)[0] == 23.173


def test_sdt_score_is_none_when_the_run_was_never_scored(tmp_path):
    """跑了但从没算过分——如实报"取不到"，不退回一个旧分数冒充。"""
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


def test_evidence_value_returns_none_when_the_key_is_absent_from_an_old_report(tmp_path):
    """文件在、但那一项不存在（老版本的 report 没有这个字段）——跟"文件不存在"
    分开：前者要重跑，后者要提交文件，要修的东西不一样。"""
    d = _make_eval_dir(tmp_path, e3={"generated_at": "x", "ablations": []})
    value, path = cr.evidence_value("e3.change_rate", d)
    assert value is None and path.exists()


# ---------- collect：后端两种形状都认，不替老文件猜一个 ----------


def test_collect_reads_a_plain_string_backend(tmp_path):
    rows = {r["key"]: r for r in cr.collect(_make_eval_dir(tmp_path))}
    assert rows["epsilon_online.mean"]["backend"] == "api"
    assert rows["epsilon_online.mean"]["model"] == "deepseek-chat"


def test_collect_reads_the_r5_backend_block(tmp_path):
    """R5 起 report 里 backend 是 backend_tags() 产出的块，不是字符串。"""
    d = _make_eval_dir(tmp_path, e3=_report("swapped", 0.4,
                                            backend={"models": ["tcm-local"],
                                                     "backends": ["local"]}))
    rows = {r["key"]: r for r in cr.collect(d)}
    assert rows["e3.change_rate"]["backend"] == ["local"]
    assert rows["e3.change_rate"]["model"] == "tcm-local"


def test_collect_leaves_backend_none_for_a_report_that_predates_the_tag(tmp_path):
    """老 report 没有 backend 这一项就报 None——不替它猜一个 deepseek
    （R5-4：猜一个默认值等于把别人跑的数标成我们的）。"""
    rows = {r["key"]: r for r in cr.collect(_make_eval_dir(tmp_path))}
    assert rows["e3.change_rate"]["backend"] is None


def test_collect_marks_a_missing_file_instead_of_dropping_the_row(tmp_path):
    d = _make_eval_dir(tmp_path, ledger=False)
    rows = {r["key"]: r for r in cr.collect(d)}
    assert rows["sdt.baseline"]["exists"] is False
    assert rows["sdt.baseline"]["value"] is None
    assert "sdt.baseline" in rows        # 缺文件的键也要在表里占一行，不能消失


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
    """一刀切会同时犯两个方向的错，条数是算出来的：地板低于全局均值的会漏判，
    高于的会误判。**不合成一个"错判率"**——漏判是少说一句话，误判是把系统抖动
    包装成学术发现，代价不一样。"""
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
        {"query": "被安全否决那条", "skipped": True, "by_physician": {}})
    d = _make_eval_dir(tmp_path, epsilon=eps)
    s = cr.epsilon_stratification(d)
    assert s["n_queries_used"] == 2 and s["n_skipped"] == 1


def test_epsilon_helpers_are_empty_without_the_file(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert cr.epsilon_by_query(d) == [] and cr.epsilon_stratification(d) == {}


# ---------- --check：凭据核对 ----------


def _md(row: str) -> str:
    """表头必须带「凭据」列——`metric_rows()` 就是靠它认出"这张表是指标表"的
    （见 test_metric_rows_only_looks_inside_a_table_with_an_evidence_column）。"""
    return "| # | 指标 | 后端 | 当前值 | 凭据 |\n|---|---|---|---|---|\n" + row + "\n"


def test_parse_evidence_tokens_reads_file_key_value():
    tokens = cr.parse_evidence_tokens("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |")
    assert len(tokens) == 1
    assert tokens[0]["file"] == "report_e3.json"
    assert tokens[0]["key"] == "e3.change_rate"
    assert tokens[0]["stated"] == "0.335"


def test_check_passes_when_the_stated_value_rounds_to_the_file_value(tmp_path):
    """文档按三位小数写 0.335、文件里是 0.3348，算一致——报告写三位是有意的。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | deepseek | 0.335 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert result["ok"] and result["checked"] == 1


def test_check_catches_a_number_that_drifted_from_the_file(tmp_path):
    """这就是这个脚本存在的理由：手抄的 0.34 不会有任何东西报错。"""
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
    """键注册在哪个文件是定死的。凭据把 e3 挂到 report_e4.json 上要报错——
    否则"这个数是从哪读的"就成了一句没人核的话。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | x | 0.335 | `report_e4.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and "注册的是" in result["unresolved"][0]["reason"]


def test_check_catches_a_missing_file(tmp_path):
    d = _make_eval_dir(tmp_path, ledger=False)
    result = cr.check(_md("| 7 | SDT | x | 22.068 | `sdt/test_run_log.jsonl:sdt.baseline=22.068` |"), d)
    assert not result["ok"] and "文件不存在" in result["unresolved"][0]["reason"]


def test_check_catches_a_key_absent_from_an_old_report(tmp_path):
    d = _make_eval_dir(tmp_path, e3={"generated_at": "x", "ablations": []})
    result = cr.check(_md("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and "取不到键" in result["unresolved"][0]["reason"]


def test_check_catches_evidence_that_disagrees_with_its_own_row(tmp_path):
    """凭据说 0.335、正文写 0.451，两边各说一套——这种行读起来像有凭据，
    实际上凭据支持的不是它展示的那个数。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3 | E3 | x | 0.451 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and result["missing_in_line"][0]["stated"] == "0.335"


def test_check_lists_pending_rows_without_failing(tmp_path):
    """契约变更：`rows_without_evidence` 拆成两个键。**没有凭据分两种**——⏳ 标了
    「还没跑过」的是刻意如此（真机数据不存在），不算失败；凭据列既没有记号也没有
    ⏳ 标记的是**漏标**，算失败。合成一个键的时候，一个新加的行凭据列留空，读起来
    跟有凭据的行一样，而没有任何东西会提醒。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 9 | E2 | deepseek | 0.420 vs 0.569 | ⏳ 还没跑过 |"), d)
    assert result["ok"]
    assert len(result["rows_pending_measurement"]) == 1
    assert result["rows_unmarked"] == []


def test_check_fails_on_a_row_whose_evidence_cell_is_blank(tmp_path):
    """凭据列留空 = 漏标，算失败。这是上一条测试里"合成一个键"会漏掉的那种情况。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 9 | E2 | deepseek | 0.420 vs 0.569 | |"), d)
    assert not result["ok"]
    assert len(result["rows_unmarked"]) == 1
    assert "漏标" in cr.format_check(result)


def test_check_only_treats_numbered_rows_as_metric_rows(tmp_path):
    """文件里还有别的说明性表格（凭据核对表、ε 逐条地板表）。把它们也算进
    "没有凭据的行"，那张清单就被噪声淹掉，人就不看它了。"""
    d = _make_eval_dir(tmp_path)
    text = ("| 说明 | 文件里的值 |\n|---|---|\n"
            "| 1 | 0.3348 |\n"                     # 编号行，但不在带「凭据」的表里
            "\n"
            "| # | 指标 | 后端 | 当前值 | 凭据 |\n|---|---|---|---|---|\n"
            "| 3 | E3 | deepseek | 0.451 | ⏳ 还没跑过 |\n")
    result = cr.check(text, d)
    assert len(result["rows_pending_measurement"]) == 1
    assert result["rows_pending_measurement"][0].startswith("| 3 |")


def test_check_accepts_the_dash_suffixed_local_row_numbering(tmp_path):
    """并列加行的编号是 `3-local`（R5-4），也算指标行。"""
    d = _make_eval_dir(tmp_path)
    result = cr.check(_md("| 3-local | E3 | tcm-local | 待跑 | ⏳ 还没跑过 |"), d)
    assert len(result["rows_pending_measurement"]) == 1


# ---------- 对着仓库真实文件：钉住 RESULTS.md 不漂 ----------


def test_repo_results_md_evidence_all_checks_out():
    """**这一条是把"不要手抄"变成 pytest 能拦的事。** 谁改了 RESULTS.md 里带凭据
    记号的数，或者改了那几个 report 文件，这条就红。"""
    text = (cr.DEFAULT_RESULTS_MD).read_text(encoding="utf-8")
    result = cr.check(text)
    assert result["ok"], cr.format_check(result)
    assert result["checked"] >= 13      # 少了说明凭据记号被删掉了，来看一眼


def test_the_current_values_come_from_eval_and_the_baselines_from_archive():
    """**这条测试的上一版做了它该做的事然后红了。** 它原来断言
    `eval/report_e3.json` 里是 0.3348（修复前），并在文档字符串里写明"有人提交了
    修复后那一轮的 report 这条就会红，那时候要做的是把 RESULTS.md 的凭据列改过来"。
    修复后那一轮提交进来（f1d5520）之后它确实红了，于是凭据列改了、修复前那一轮
    归档了。现在钉住的是新的分工：`eval/` 里是当前值，`eval/archive/2026-09-12/`
    里是修复前的对照，两端都能核。"""
    assert cr.evidence_value("e3.change_rate")[0] == 0.4508
    assert cr.evidence_value("e4.change_rate")[0] == 0.4969
    assert cr.evidence_value("e8.output_difference_rate")[0] == 0.4369
    assert cr.evidence_value("e9.change_rate")[0] == 0.4628
    assert cr.evidence_value("archive.e3.change_rate")[0] == 0.3348
    assert cr.evidence_value("archive.e4.change_rate")[0] == 0.3514
    assert cr.evidence_value("archive.e8.output_difference_rate")[0] == 0.366
    assert cr.evidence_value("archive.e9.change_rate")[0] == 0.2503


def test_the_gate_actually_passes_on_the_current_values_and_failed_before():
    """闸门 ≥ 0.4 这件事本身要可核：修复前四项全部不过，修复后 E3/E4/E9 全过。
    「修复前 → 修复后」这个叙事的两端都在文件里，不靠一句话。"""
    for key in ("e3.change_rate", "e4.change_rate", "e9.change_rate"):
        assert cr.evidence_value(key)[0] >= 0.4
        assert cr.evidence_value(f"archive.{key}")[0] < 0.4


def test_e2_school_pairs_flip_between_the_two_committed_runs():
    """**同一份代码、同一批主诉，跑两次，E2 的判据一次成立一次不成立。**
    这是「只报出、不设闸门」这个决定最硬的实测依据。哪天这条测试红了（两轮方向
    一致了），要改的是 RESULTS.md 第 9 行的叙述，不是把这条测试删掉。"""
    e9_holds = (cr.evidence_value("e9.school_cross_mean")[0]
                > cr.evidence_value("e9.school_lineage_mean")[0])
    e8_holds = (cr.evidence_value("e8.school_cross_mean")[0]
                > cr.evidence_value("e8.school_lineage_mean")[0])
    assert e9_holds and not e8_holds
    assert cr.evidence_value("e9.school_n_cross_gt_lineage")[0] == 5
    assert cr.evidence_value("e8.school_n_cross_gt_lineage")[0] == 4


def test_every_report_md_is_in_sync_with_its_json():
    """一份旧的人读报告躺在一份新的数据旁边，而人只会读 md。这条测试是那次
    e8.md/e9.md 没跟着同步（只同步了 e3/e4）之后加的。"""
    assert cr.check_md_json_sync() == []


def test_repo_epsilon_stratification_has_both_error_directions():
    """ε 分层那一节的两个条数从这里来。两边都非零才说明"一刀切会同时犯两个方向的
    错"这句话有实测支撑；哪边是 0，那一节就得重写。"""
    s = cr.epsilon_stratification()
    assert s["n_floor_below_global"] > 0 and s["n_floor_above_global"] > 0
    assert s["n_floor_below_global"] + s["n_floor_above_global"] == s["n_queries_used"]


# ---------- CLI ----------


def test_main_prints_the_machine_read_table(tmp_path, capsys):
    cr.main(["--eval-dir", str(_make_eval_dir(tmp_path))])
    out = capsys.readouterr().out
    assert "机读，不是手抄" in out
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


# ---------- md / json 同步检查与重渲染 ----------


def _md_for(json_path, ts):
    json_path.with_suffix(".md").write_text(
        f"# V1 评测汇总（{ts}）\n\n共 10 条查询。\n", encoding="utf-8")


def test_md_json_sync_flags_a_stale_md(tmp_path):
    """凭据记号只管 json 里的数，管不到 md——所以要单独查。"""
    d = _make_eval_dir(tmp_path)
    # _report() 里的 generated_at 是 2026-09-12，md 故意写成 09-01
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
    """只有 json 没有 md 不算漂——那是"还没渲染过"，不是"渲染过但过期了"。"""
    assert cr.check_md_json_sync(_make_eval_dir(tmp_path)) == []


def test_check_fails_on_md_drift_even_when_every_token_matches(tmp_path):
    d = _make_eval_dir(tmp_path)
    _md_for(d / "report_e3.json", "2026-09-01T00:00:00+00:00")
    result = cr.check(_md("| 3 | E3 | x | 0.335 | `report_e3.json:e3.change_rate=0.335` |"), d)
    assert not result["ok"] and result["mismatches"] == []
    assert len(result["md_json_drift"]) == 1
    assert "只读 md" in cr.format_check(result)


def test_rerender_rewrites_the_stale_md_from_its_own_json(tmp_path):
    """md 是 json 的确定性渲染产物，重渲染不会造出任何新数字（零 LLM 调用）。"""
    d = _make_eval_dir(tmp_path)
    e8 = d / "report_e8.json"
    report = json.loads(e8.read_text(encoding="utf-8"))
    # render_markdown 要的键补齐（这几份合成 report 只有测凭据用的那几段）
    report.update({
        "n_queries": 10,
        "divergence_vs_epsilon": {"available": True, "note": "n"},
        "divergence_per_query": [],
        "school_pairs": {"note": "s"},
        "hallucination": {"note": "h"},
        "safety_veto": {"note": "v"},
        "retrieval_mode_comparisons": [], "ablations": [],
        "react_process": None,
        # retriever_mode_effect 非 None 时 render_markdown 要读它的 note
        "retriever_mode_effect": {"output_difference_rate": 0.366, "note": "r"},
    })
    e8.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    _md_for(e8, "2026-09-01T00:00:00+00:00")
    done = cr.rerender_drifted_md(d)
    assert [x["rendered"] for x in done] == [True]
    assert cr.check_md_json_sync(d) == []
    assert report["generated_at"] in (d / "report_e8.md").read_text(encoding="utf-8")


def test_rerender_reports_a_missing_key_instead_of_crashing(tmp_path):
    """老 json 缺 render_markdown 要的键时，报出缺哪个键，不抛一个看不懂的栈。"""
    d = _make_eval_dir(tmp_path)
    _md_for(d / "report_e3.json", "2026-09-01T00:00:00+00:00")
    done = cr.rerender_drifted_md(d)
    assert done and done[0]["rendered"] is False and "缺键" in done[0]["reason"]


def test_main_rerender_says_nothing_to_do_when_in_sync(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        cr.main(["--eval-dir", str(_make_eval_dir(tmp_path)), "--rerender"])
    assert e.value.code == 0
    assert "没有需要重渲染的 md" in capsys.readouterr().out


# ---------- 新增凭据键 ----------


def test_paired_divergence_keys_count_verdicts_not_the_global_cut(tmp_path):
    """`divergence_per_query` 是**逐条配对** ε 的判决，`divergence_vs_epsilon` 是拿
    全局 ε 一刀切算的。同一份文件里两个都有，引用时必须说清是哪一个。"""
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
    """两份 report 的 school_pairs 各读各的——只挑一份写进文档就是在挑对自己
    有利的那一次。"""
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


def test_archive_keys_point_at_the_archive_directory(tmp_path):
    """归档目录一轮一个日期，只放不改——上表「修复前」那一列全靠"这个目录里的
    四份文件是同一轮跑出来的"这个前提。"""
    d = _make_eval_dir(tmp_path)
    arch = d / cr.ARCHIVE_2026_09_12
    arch.mkdir(parents=True)
    (arch / "report_e3.json").write_text(
        json.dumps(_report("swapped", 0.3348), ensure_ascii=False), encoding="utf-8")
    value, path = cr.evidence_value("archive.e3.change_rate", d)
    assert value == 0.3348
    assert cr.ARCHIVE_2026_09_12 in str(path)


def test_archive_key_missing_file_is_reported_not_silently_none(tmp_path):
    value, path = cr.evidence_value("archive.e3.change_rate", _make_eval_dir(tmp_path))
    assert value is None and not path.exists()


def test_repo_readme_evidence_all_checks_out():
    """README 里的评测数字跟 RESULTS.md 走**同一套凭据记号、同一个核对器**——
    README 手抄一份数字出来漂了，跟 RESULTS.md 漂了是同一个问题，不该有两套机制。"""
    from pathlib import Path as _Path

    readme = _Path(__file__).resolve().parent.parent / "README.md"
    result = cr.check(readme.read_text(encoding="utf-8"))
    assert result["ok"], cr.format_check(result)
    assert result["checked"] >= 9      # 少了说明 README 里的凭据记号被删掉了


def test_default_check_paths_cover_the_four_fixed_docs_plus_every_round_report():
    """R19 从两份扩到四份，**R21 再改成"四份固定 + 每轮一份报告"**（两次都是
    有意的契约变更）。R19 那次加进来的是 `docs/R11-R19_report.md` 和 `DEMO.md`。
    R21 这次加的是 `docs/reports/R<N>_report.md`——**glob 进来，不写死名字**：
    靠人记着往元组里加一个名字，忘了的那一轮，它里面的凭据记号谁都不核，
    而它读起来跟被核过的一样。

    所以这条不再断言"恰好 N 份"（那会变成每轮都要改一次的数），
    改成断言四份固定的都在、且 `docs/reports/` 下的每一份都在。"""
    names = {p.name for p in cr.DEFAULT_CHECK_PATHS}
    assert {"RESULTS.md", "README.md", "R11-R19_report.md", "DEMO.md"} <= names
    round_names = {p.name for p in cr.round_report_paths()}
    assert round_names, "docs/reports/ 下一份轮次报告都没有"
    assert round_names <= names
    # 排序是按**轮次号**的：字符串排序下 R9 会排在 R21 后面
    nums = [int(p.name[1:].split("_")[0]) for p in cr.round_report_paths()]
    assert nums == sorted(nums)


def test_metric_rows_only_looks_inside_a_table_with_an_evidence_column():
    """判据是结构性的：表头里有「凭据」这一列 → 这张表是指标表。别的文档里
    「1/2/3」开头的普通表格（README 的已知局限清单就是）不该被当成漏标凭据。"""
    text = ("| 块 | 一句话 | 详细 |\n|---|---|---|\n| 1 | 系统构成 | 下一节 |\n"
            "\n"
            "| # | 指标 | 凭据 |\n|---|---|---|\n| 3 | E3 | ⏳ 还没跑过 |\n")
    rows = cr.metric_rows(text)
    assert len(rows) == 1 and rows[0].startswith("| 3 |")


# ============================================================================
# R17：两个凭据盲区纳入注册表（SOURCES.md 第 45 条九记的那两个）
# ============================================================================


def test_graph_scale_numbers_are_now_checkable():
    """**盲区一：图谱规模。** README 和 RESULTS 里写着节点数/边数/证候数/症状数，
    而这几个数一直没有凭据记号——它们不在 eval/ 下的任何 report 里。于是
    "重建图谱之后忘了回写"没有任何机制拦得住，而**它已经发生过**（839 → 941）。"""
    for key in ("graph.n_nodes", "graph.n_edges", "graph.n_syndromes",
                "graph.n_symptoms", "graph.n_elements"):
        assert key in cr.EVIDENCE, f"{key} 没进注册表"
        value, path = cr.evidence_value(key)
        assert path.exists(), f"{key} 指向的文件不在：{path}"
        assert isinstance(value, int) and value > 0, f"{key} 取不到数：{value}"


def test_graph_counts_match_a_fresh_read_of_the_file():
    """注册表里的 getter 跟直接数文件必须一致——否则凭据机制本身在骗人。"""
    import json
    from pathlib import Path
    raw = json.loads((Path(cr.ROOT) / "data" / "graph.json").read_text(encoding="utf-8"))
    assert cr.evidence_value("graph.n_nodes")[0] == len(raw["nodes"])
    assert cr.evidence_value("graph.n_edges")[0] == len(raw["edges"])
    assert cr.evidence_value("graph.n_syndromes")[0] == sum(
        1 for n in raw["nodes"] if n.get("node_type") == "syndrome")


def test_epsilon_stratification_numbers_are_now_checkable():
    """**盲区二：ε 分层。** 这一节的每个数都是从 epsilon.json 的 per_query
    现算的，之前只在打印时算一次、没进注册表，所以 RESULTS.md 里那一节的数字
    是手抄的——而这个项目手抄数字漂过三次。"""
    for key in ("epsilon.stratification.global_mean",
                "epsilon.stratification.n_queries_used",
                "epsilon.stratification.per_query_mean_min",
                "epsilon.stratification.per_query_mean_max",
                "epsilon.stratification.n_floor_below_global",
                "epsilon.stratification.n_floor_above_global",
                "epsilon.stratification.max_over_global"):
        assert key in cr.EVIDENCE, f"{key} 没进注册表"
        value, path = cr.evidence_value(key)
        assert path.exists() and value is not None, f"{key} 取不到：{value}"


def test_the_stratification_getter_and_the_printer_agree():
    """注册表的 getter 和 `epsilon_stratification()` 必须给出同一套数——
    两条路算同一件事，算法收口在 `_stratification_from` 那一处。"""
    strat = cr.epsilon_stratification()
    assert cr.evidence_value("epsilon.stratification.global_mean")[0] == strat["global_mean"]
    assert cr.evidence_value("epsilon.stratification.n_floor_above_global")[0] \
        == strat["n_floor_above_global"]
    assert cr.evidence_value("epsilon.stratification.max_over_global")[0] \
        == strat["max_over_global"]


def test_below_plus_above_never_exceeds_the_usable_count():
    """分层那两个方向的条数是**算出来的不是估的**（RESULTS.md 那一节的原话）。
    这条是它们的自洽性：低于 + 高于 ≤ 可用条数（相等的那条两边都不算）。"""
    strat = cr.epsilon_stratification()
    assert strat["n_floor_below_global"] + strat["n_floor_above_global"] \
        <= strat["n_queries_used"]


def test_the_docs_actually_use_the_new_keys():
    """注册表里有、文档里没人用，等于这一轮什么都没做——盲区还是盲区。"""
    from pathlib import Path
    root = Path(cr.ROOT)
    readme = (root / "README.md").read_text(encoding="utf-8")
    results = (root / "eval" / "RESULTS.md").read_text(encoding="utf-8")
    assert "graph.n_nodes=" in readme
    assert "graph.n_symptoms=" in readme
    assert "epsilon.stratification.global_mean=" in results
    assert "epsilon.stratification.max_over_global=" in results


def test_check_still_passes_with_the_new_marks():
    """新加的记号本身也要能通过核对——加一批核不过的记号比不加更糟。"""
    from pathlib import Path
    root = Path(cr.ROOT)
    for doc in (root / "eval" / "RESULTS.md", root / "README.md"):
        result = cr.check(doc.read_text(encoding="utf-8"))
        assert not result["mismatches"], result["mismatches"]
        assert not result["unresolved"], result["unresolved"]
        assert not result["missing_in_line"], result["missing_in_line"]


# ---------- R23：每轮一份不可变快照 ----------


def test_round_snapshots_are_sorted_by_round_number_not_by_string():
    """字符串排序下 R9 排在 R21 后面。轮次号是数字，就按数字排。"""
    names = cr._round_snapshot_names()
    nums = [int(n[1:]) for n in names]
    assert nums == sorted(nums)


def test_every_round_snapshot_registers_its_own_evidence_keys():
    """`bench/rounds/R23.json` 一落盘，`round.R23.pytest_passed` 就该可用——
    **glob 注册，不是一轮一轮往 EVIDENCE 里加九行**。忘了加的那一轮，
    报告里的凭据记号会被当成查不到的键；而更糟的做法是有人为了让 --check 过
    就把记号删掉，那一轮的数于是变成没人核的数。"""
    names = cr._round_snapshot_names()
    assert names, "eval/bench/rounds/ 下一份快照都没有"
    for name in names:
        for metric in cr.ROUND_METRIC_KEYS:
            key = f"round.{name}.{metric}"
            assert key in cr.EVIDENCE, key
            value, path = cr.evidence_value(key)
            assert path.exists() and value is not None, key


def test_the_round_snapshot_is_not_the_same_file_as_the_current_measurement():
    """这两份文件的分工是整个机制的要点：sandbox.json 会被下一轮覆盖
    （RESULTS.md 的性能表引它），rounds/R<N>.json 此后不再动（每轮报告引它）。
    判据写成"路径不同"而不是比内容——同一轮里两份内容本来就一样。"""
    _, current = cr.evidence_value("bench.pytest_passed")
    latest = cr._round_snapshot_names()[-1]
    _, snapshot = cr.evidence_value(f"round.{latest}.pytest_passed")
    assert current != snapshot
    assert snapshot.parent.name == "rounds"


def test_bench_sandbox_rejects_a_malformed_round_name():
    """轮次名的形状被凭据注册表的 glob 依赖（`round.R23.*`）。
    写成 `r23` 或 `R23-fix` 的后果是那份快照谁都不核，而它看起来跟被核过的一样
    ——所以在入口就拒绝，不是"能跑就行"。"""
    import scripts.bench_sandbox as bs

    for bad in ("r23", "R23-fix", "23", "round23"):
        with pytest.raises(SystemExit):
            bs.main(["--show", "--round", bad])


def test_patch_reports_are_checked_too(tmp_path):
    """补丁报告（`R24_patch_report.md` 这种）也要进核对器。

    一份不进核对器的报告可以一直挂着一个早就对不上的数，而它读起来跟被核过的
    一样——这套凭据机制存在的全部理由就是这个。同号时不带后缀的排在前面。
    """
    from scripts.collect_results import round_report_paths

    for name in ("R24_report.md", "R24_patch_report.md", "R9_report.md", "notes.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    names = [p.name for p in round_report_paths(tmp_path)]
    assert names == ["R9_report.md", "R24_report.md", "R24_patch_report.md"]
