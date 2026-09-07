"""eval/mes/export.py + collect.py 的离线测试：不调用真实 consult()。"""
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


# ---------- build_blind_items ----------


def test_build_blind_items_skips_rejected_and_insufficient():
    queries = ["q1", "q2", "q3"]
    results = [
        _result(rejected=True),
        _result(insufficient=True),
        _result(physicians=[_pr("ye_tianshi", "甲"), _pr("wu_jutong", "乙")]),
    ]
    items, key = me.build_blind_items(queries, results, seed=1)
    assert len(items) == 1
    assert items[0]["query"] == "q3"


def test_build_blind_items_skips_when_not_exactly_two_physicians():
    queries = ["q1"]
    results = [_result(physicians=[_pr("ye_tianshi", "甲")])]
    items, key = me.build_blind_items(queries, results, seed=1)
    assert items == []


def test_build_blind_items_hides_physician_identity_and_case_ids():
    queries = ["q1"]
    results = [_result(physicians=[_pr("ye_tianshi", "甲证"), _pr("wu_jutong", "乙证")])]
    items, key = me.build_blind_items(queries, results, seed=1)
    item = items[0]
    assert "physician" not in item["A"] and "physician" not in item["B"]
    assert "cited_case_ids" not in item["A"]
    assert item["winner"] is None
    # 答案表记录真实身份，且 A/B 分别对应两位不同医家
    ans = key[item["item_id"]]
    assert {ans["A"], ans["B"]} == {"ye_tianshi", "wu_jutong"}


def test_build_blind_items_length_mismatch_raises():
    with pytest.raises(ValueError, match="不一致"):
        me.build_blind_items(["q1", "q2"], [_result()], seed=1)


def test_build_blind_items_reproducible_with_same_seed():
    queries = ["q1", "q2", "q3", "q4"]
    results = [
        _result(physicians=[_pr("ye_tianshi", f"证{i}"), _pr("wu_jutong", f"证{i}b")])
        for i in range(4)
    ]
    items1, key1 = me.build_blind_items(queries, results, seed=42)
    items2, key2 = me.build_blind_items(queries, results, seed=42)
    assert key1 == key2


def test_build_blind_items_actually_shuffles_across_items():
    """种子固定但每条 item 各自掷一次硬币——不能所有条目的 A 恒等于同一位医家，
    否则"匿名"名存实亡（评分人看几条就能反推 A 恒为叶天士）。"""
    queries = [f"q{i}" for i in range(20)]
    results = [
        _result(physicians=[_pr("ye_tianshi", f"证{i}"), _pr("wu_jutong", f"证{i}b")])
        for i in range(20)
    ]
    items, key = me.build_blind_items(queries, results, seed=7)
    a_physicians = {key[it["item_id"]]["A"] for it in items}
    assert a_physicians == {"ye_tianshi", "wu_jutong"}  # 两种都出现过


# ---------- collect_ratings ----------


def _item(item_id, winner):
    return {"item_id": item_id, "query": "q", "A": {}, "B": {}, "winner": winner}


def test_collect_ratings_counts_wins_by_real_physician():
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


def test_collect_ratings_ties_counted_separately_not_as_wins():
    items = [_item("i0", "tie"), _item("i1", "A")]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong"}, "i1": {"A": "ye_tianshi", "B": "wu_jutong"}}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert r["n_tie"] == 1
    assert r["wins"]["ye_tianshi"] == 1


def test_collect_ratings_unrated_items_skipped_not_counted():
    items = [_item("i0", None), _item("i1", "A")]
    answer_key = {"i0": {"A": "ye_tianshi", "B": "wu_jutong"}, "i1": {"A": "ye_tianshi", "B": "wu_jutong"}}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert r["n_unrated"] == 1
    assert r["n_rated"] == 1


def test_collect_ratings_missing_answer_key_skipped_not_crashed():
    items = [_item("unknown-id", "A")]
    r = mc.collect_ratings(items, {}, "ye_tianshi", "wu_jutong")
    assert r["n_missing_answer_key"] == 1
    assert r["wins"] == {"ye_tianshi": 0, "wu_jutong": 0}


def test_collect_ratings_includes_mcnemar():
    items = [_item(f"i{i}", "A") for i in range(3)] + [_item(f"j{i}", "B") for i in range(1)]
    answer_key = {it["item_id"]: {"A": "ye_tianshi", "B": "wu_jutong"} for it in items}
    r = mc.collect_ratings(items, answer_key, "ye_tianshi", "wu_jutong")
    assert "p_value" in r["mcnemar"]


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


def test_collect_main_raises_clear_error_when_items_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="export"):
        mc.main(["--items-path", str(tmp_path / "nope.json"),
                 "--answer-key-path", str(tmp_path / "nope2.json")])


def test_collect_main_end_to_end(tmp_path):
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
