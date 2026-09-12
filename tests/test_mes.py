"""eval/mes/export.py + collect.py 的离线测试：不调用真实 consult()。

医家数不写死：大多数用例把 PHYSICIANS 钉在两位（ye_tianshi/wu_jutong），
跟这个项目里 _pin_two_physicians 是同一类理由——两位医家时的行为更简单、
测试意图更清楚；额外几条用例专门验证三位（及以上）医家时的泛化，不能
只测两位就假设"以后加了第三位也一定对"。
"""
import json

import pytest

from eval.mes import collect as mc
from eval.mes import export as me


class _FakeS3:
    def __init__(self, syndrome, reasoning="因为...", treatment_principle="法",
                 formula="方", herbs=None):
        self.syndrome = syndrome
        self.reasoning = reasoning
        self.treatment_principle = treatment_principle
        self.formula = formula
        self.herbs = herbs or ["药1"]


def _result(rejected=False, insufficient=False, physicians=None):
    return {
        "rejected": rejected, "insufficient": insufficient,
        "results": physicians or [],
    }


def _pr(physician, syndrome):
    return {"physician": physician, "s3": _FakeS3(syndrome)}


def _pin_physicians(monkeypatch, module, ids):
    monkeypatch.setattr(module, "PHYSICIANS", {pid: {} for pid in ids})


# ---------- build_blind_items（两位医家，默认场景）----------


def test_build_blind_items_skips_rejected_and_insufficient(monkeypatch):
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    queries = ["q1", "q2", "q3"]
    results = [
        _result(rejected=True),
        _result(insufficient=True),
        _result(physicians=[_pr("ye_tianshi", "甲"), _pr("wu_jutong", "乙")]),
    ]
    items, key, skipped = me.build_blind_items(queries, results, seed=1)
    assert len(items) == 1
    assert items[0]["query"] == "q3"
    assert skipped == {"rejected": 1, "insufficient": 1, "wrong_physician_count": 0}


def test_build_blind_items_skips_when_physician_count_does_not_match_registry(monkeypatch):
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    queries = ["q1"]
    results = [_result(physicians=[_pr("ye_tianshi", "甲")])]
    items, key, skipped = me.build_blind_items(queries, results, seed=1)
    assert items == []
    assert skipped["wrong_physician_count"] == 1


def test_build_blind_items_hides_physician_identity_and_case_ids(monkeypatch):
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    queries = ["q1"]
    results = [_result(physicians=[_pr("ye_tianshi", "甲证"), _pr("wu_jutong", "乙证")])]
    items, key, _ = me.build_blind_items(queries, results, seed=1)
    item = items[0]
    assert "physician" not in item["A"] and "physician" not in item["B"]
    assert "cited_case_ids" not in item["A"]
    assert item["winner"] is None
    # 答案表记录真实身份，且 A/B 分别对应两位不同医家
    ans = key[item["item_id"]]
    assert {ans["A"], ans["B"]} == {"ye_tianshi", "wu_jutong"}


def test_build_blind_items_length_mismatch_raises(monkeypatch):
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    with pytest.raises(ValueError, match="不一致"):
        me.build_blind_items(["q1", "q2"], [_result()], seed=1)


def test_build_blind_items_reproducible_with_same_seed(monkeypatch):
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    queries = ["q1", "q2", "q3", "q4"]
    results = [
        _result(physicians=[_pr("ye_tianshi", f"证{i}"), _pr("wu_jutong", f"证{i}b")])
        for i in range(4)
    ]
    items1, key1, _ = me.build_blind_items(queries, results, seed=42)
    items2, key2, _ = me.build_blind_items(queries, results, seed=42)
    assert key1 == key2


def test_build_blind_items_actually_shuffles_across_items(monkeypatch):
    """种子固定但每条 item 各自掷一次硬币——不能所有条目的 A 恒等于同一位医家，
    否则"匿名"名存实亡（评分人看几条就能反推 A 恒为叶天士）。"""
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    queries = [f"q{i}" for i in range(20)]
    results = [
        _result(physicians=[_pr("ye_tianshi", f"证{i}"), _pr("wu_jutong", f"证{i}b")])
        for i in range(20)
    ]
    items, key, _ = me.build_blind_items(queries, results, seed=7)
    a_physicians = {key[it["item_id"]]["A"] for it in items}
    assert a_physicians == {"ye_tianshi", "wu_jutong"}  # 两种都出现过


# ---------- build_blind_items（三位及以上医家，泛化）----------


def test_build_blind_items_supports_three_physicians(monkeypatch):
    """张锡纯注册进 PHYSICIANS 之后（bef7415），三位医家的盲评应该并排出
    A/B/C 三列，不是继续按 2 的硬编码跳过。"""
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong", "zhang_xichun"])
    queries = ["q1"]
    results = [_result(physicians=[
        _pr("ye_tianshi", "甲"), _pr("wu_jutong", "乙"), _pr("zhang_xichun", "丙"),
    ])]
    items, key, skipped = me.build_blind_items(queries, results, seed=1)
    assert len(items) == 1
    item = items[0]
    assert set(key[item["item_id"]]) == {"A", "B", "C"}
    assert set(key[item["item_id"]].values()) == {"ye_tianshi", "wu_jutong", "zhang_xichun"}
    assert {"A", "B", "C"} <= set(item)
    assert skipped == {"rejected": 0, "insufficient": 0, "wrong_physician_count": 0}


def test_build_blind_items_three_physicians_shuffles_all_three_slots(monkeypatch):
    """不能只是"C 恒等于第三位医家"——三位都要在三个位置上出现过，否则
    "匿名"对第三列形同虚设。"""
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong", "zhang_xichun"])
    queries = [f"q{i}" for i in range(30)]
    results = [
        _result(physicians=[
            _pr("ye_tianshi", f"证{i}"), _pr("wu_jutong", f"证{i}b"), _pr("zhang_xichun", f"证{i}c"),
        ])
        for i in range(30)
    ]
    items, key, _ = me.build_blind_items(queries, results, seed=3)
    c_physicians = {key[it["item_id"]]["C"] for it in items}
    assert c_physicians == {"ye_tianshi", "wu_jutong", "zhang_xichun"}


def test_build_blind_items_skips_when_only_two_of_three_physicians_present(monkeypatch):
    """注册表是 3 位，但这次 consult 只拿到 2 位的结果（比如某位医家那步
    出了别的问题）——按"医家数不对"跳过，不硬凑一场只有两列的对照进三列表。"""
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong", "zhang_xichun"])
    queries = ["q1"]
    results = [_result(physicians=[_pr("ye_tianshi", "甲"), _pr("wu_jutong", "乙")])]
    items, key, skipped = me.build_blind_items(queries, results, seed=1)
    assert items == []
    assert skipped["wrong_physician_count"] == 1


# ---------- collect_ratings ----------


def _item(item_id, winner):
    return {"item_id": item_id, "query": "q", "A": {}, "B": {}, "winner": winner}


def test_collect_ratings_counts_wins_by_real_physician(monkeypatch):
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items = [_item("i0", "A"), _item("i1", "B"), _item("i2", "A")]
    answer_key = {
        "i0": {"A": "ye_tianshi", "B": "wu_jutong"},
        "i1": {"A": "wu_jutong", "B": "ye_tianshi"},  # B=ye_tianshi 赢
        "i2": {"A": "wu_jutong", "B": "ye_tianshi"},  # A=wu_jutong 赢
    }
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    # i0: winner=A -> ye_tianshi 赢；i1: winner=B -> ye_tianshi 赢；i2: winner=A -> wu_jutong 赢
    assert r["wins"] == {"ye_tianshi": 2, "wu_jutong": 1}
    assert r["n_rated"] == 3


def test_collect_ratings_ties_counted_separately_not_as_wins(monkeypatch):
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items = [_item("i0", "tie"), _item("i1", "A")]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong"}, "i1": {"A": "ye_tianshi", "B": "wu_jutong"}}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert r["n_tie"] == 1
    assert r["wins"]["ye_tianshi"] == 1


def test_collect_ratings_unrated_items_skipped_not_counted(monkeypatch):
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items = [_item("i0", None), _item("i1", "A")]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong"}, "i1": {"A": "ye_tianshi", "B": "wu_jutong"}}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert r["n_unrated"] == 1
    assert r["n_rated"] == 1


def test_collect_ratings_missing_answer_key_skipped_not_crashed(monkeypatch):
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items = [_item("unknown-id", "A")]
    r = mc.collect_ratings(items, {}, "ye_tianshi", "wu_jutong")
    assert r["n_missing_answer_key"] == 1
    assert r["wins"] == {"ye_tianshi": 0, "wu_jutong": 0}


def test_collect_ratings_includes_mcnemar(monkeypatch):
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items = [_item(f"i{i}", "A") for i in range(3)] + [_item(f"j{i}", "B") for i in range(1)]
    answer_key = {it["item_id"]: {"A": "ye_tianshi", "B": "wu_jutong"} for it in items}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert "p_value" in r["mcnemar"]


def test_collect_ratings_third_physician_win_counted_as_other_not_dropped(monkeypatch):
    """三位医家的题里，winner="C" 且答案表里 C 是第三位医家：不在
    physician_a/physician_b 里的胜利要算进 other_wins，不能悄悄丢掉，也
    不能崩。"""
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong", "zhang_xichun"])
    items = [{"item_id": "i0", "query": "q", "winner": "C"}]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong", "C": "zhang_xichun"}}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert r["n_rated"] == 1
    assert r["other_wins"] == 1
    assert r["wins"] == {"ye_tianshi": 0, "wu_jutong": 0}


def test_collect_ratings_letter_beyond_registry_size_treated_as_unrated(monkeypatch):
    """只有两位医家时，winner="C" 结构上就不合法（不在 A/B/tie 里），按未评分
    处理，不当成一次答案表缺失。"""
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items = [{"item_id": "i0", "query": "q", "winner": "C"}]
    r = mc.collect_ratings(items, {}, "ye_tianshi", "wu_jutong")
    assert r["n_rated"] == 0
    assert r["n_unrated"] == 1
    assert r["n_missing_answer_key"] == 0


def test_collect_ratings_valid_letter_but_missing_from_this_items_key(monkeypatch):
    """医家数是 3（winner="C" 结构上合法），但这条题的答案表本身只有 A/B
    两把钥匙（数据不一致，比如这条题其实是两位医家跑出来的）——按查不到
    答案表处理，不当 0 次胜利悄悄吞掉。"""
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong", "zhang_xichun"])
    items = [{"item_id": "i0", "query": "q", "winner": "C"}]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong"}}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert r["n_missing_answer_key"] == 1
    assert r["other_wins"] == 0


# ---------- CLI ----------


def test_export_main_dry_run_does_not_call_consult(tmp_path, monkeypatch, capsys):
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("--dry-run 不该真的调用")

    monkeypatch.setattr("core.chain.consult", boom)
    me.main(["--queries-path", str(queries_path), "--dry-run"])
    assert "预估调用数" in capsys.readouterr().out


def test_export_main_writes_items_and_answer_key(tmp_path, monkeypatch):
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong"])
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n", encoding="utf-8")
    out_items = tmp_path / "items.json"
    out_key = tmp_path / "key.json"

    fake_result = _result(physicians=[_pr("ye_tianshi", "甲"), _pr("wu_jutong", "乙")])
    monkeypatch.setattr("core.chain.consult", lambda q: fake_result)

    me.main([
        "--queries-path", str(queries_path), "--out-items", str(out_items),
        "--out-answer-key", str(out_key),
    ])
    items = json.loads(out_items.read_text(encoding="utf-8"))
    key = json.loads(out_key.read_text(encoding="utf-8"))
    assert len(items) == 1
    assert items[0]["item_id"] in key


def test_export_main_prints_skip_reasons_separately(tmp_path, monkeypatch, capsys):
    """跳过原因要分开报，不能合并成一句看不出是哪个条件在起作用——之前
    "医家数不为 2" 跳过全部 10 条，合并报的话第一眼看不出是这个条件在挡。"""
    _pin_physicians(monkeypatch, me, ["ye_tianshi", "wu_jutong", "zhang_xichun"])
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n", encoding="utf-8")

    # 只拿到两位医家的结果，但注册表是三位——应该被计入 wrong_physician_count
    fake_result = _result(physicians=[_pr("ye_tianshi", "甲"), _pr("wu_jutong", "乙")])
    monkeypatch.setattr("core.chain.consult", lambda q: fake_result)

    me.main(["--queries-path", str(queries_path),
             "--out-items", str(tmp_path / "items.json"),
             "--out-answer-key", str(tmp_path / "key.json")])
    out = capsys.readouterr().out
    assert "因安全拦截" in out and "因信息不足" in out and "因医家数不等于" in out


def test_collect_main_raises_clear_error_when_items_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="export"):
        mc.main(["--items-path", str(tmp_path / "nope.json"),
                 "--answer-key-path", str(tmp_path / "nope2.json")])


def test_collect_main_end_to_end(tmp_path, monkeypatch):
    _pin_physicians(monkeypatch, mc, ["ye_tianshi", "wu_jutong"])
    items_path = tmp_path / "items.json"
    key_path = tmp_path / "key.json"
    out_path = tmp_path / "out.json"
    items = [_item("i0", "A")]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong"}}
    items_path.write_text(json.dumps(items), encoding="utf-8")
    key_path.write_text(json.dumps(answer_key), encoding="utf-8")

    mc.main([
        "--items-path", str(items_path), "--answer-key-path", str(key_path),
        "--out", str(out_path),
    ])
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result["wins"]["ye_tianshi"] == 1
