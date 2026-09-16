"""R21：既有 E3/E4 消融脚本在 `full_context` 下能跑（fake 后端，零网络）。

E3 = own vs swapped（换掉参考医案，结论变不变）；E4 = own vs none（有没有参考
医案，结论变不变）。闸门阈值不变 ≥ 0.4。**全量语料只会让"看到谁的医案"这个
信号更强，过不了闸门才是真发现**（§1.1）。
"""
from __future__ import annotations

from eval.run_eval import ablation_output_effect, collect_refs_mode_pairs


def _fake_consult(seen: list[dict]):
    """记下每次调用的 refs_mode / retriever_mode，并按 refs_mode 给不同的方。

    own → 四味；swapped → 换两味；none → 只剩两味。这样 E3/E4 的改变率非 0，
    能验"配对逻辑接上了"，而不是验模型。
    """
    herbs_by_mode = {
        "own": ["人参", "白术", "茯苓", "甘草"],
        "swapped": ["黄芪", "升麻", "茯苓", "甘草"],
        "none": ["茯苓", "甘草"],
    }

    def consult(query, *, refs_mode="own", retriever_mode=None, **kw):
        seen.append({"query": query, "refs_mode": refs_mode, "retriever_mode": retriever_mode})
        return {
            "rejected": False,
            "insufficient": False,
            "results": [
                {"physician": "ye_tianshi", "physician_name": "叶天士",
                 "refs": [], "s3": type("S3", (), {"herbs": herbs_by_mode[refs_mode]})()},
            ],
            "manifest": {"llm_calls": 5, "retriever_mode": retriever_mode},
        }

    return consult


def test_e3_and_e4_run_under_full_context():
    seen: list[dict] = []
    pairs = collect_refs_mode_pairs(["纳差乏力", "胃脘胀痛"], ["swapped", "none"],
                                    consult_fn=_fake_consult(seen),
                                    retriever_mode="full_context")
    assert set(pairs) == {"swapped", "none"}
    assert all(len(v) == 2 for v in pairs.values())
    # own 每条主诉只跑一次（E3+E4 一起跑时不重复算两遍）
    assert sum(1 for c in seen if c["refs_mode"] == "own") == 2


def test_own_and_ablated_use_the_same_retriever_mode():
    """一半 full_context 一半 top3 的配对比出来的不是"换掉参考医案的效果"，
    是两个变量混在一起。"""
    seen: list[dict] = []
    collect_refs_mode_pairs(["纳差乏力"], ["swapped", "none"],
                            consult_fn=_fake_consult(seen), retriever_mode="full_context")
    assert {c["retriever_mode"] for c in seen} == {"full_context"}


def test_not_passing_a_mode_leaves_the_default_in_place():
    """不传就一个额外关键字都不加——保持默认路径跟改造前逐字节一致
    （跟 `_search_cases` 的第 1 条同一个理由）。"""
    seen: list[dict] = []
    collect_refs_mode_pairs(["纳差乏力"], ["none"], consult_fn=_fake_consult(seen))
    assert {c["retriever_mode"] for c in seen} == {None}


def test_the_gate_threshold_is_unchanged_under_full_context():
    """闸门阈值不变 ≥ 0.4：换了检索模式不是放松判据的理由。"""
    seen: list[dict] = []
    pairs = collect_refs_mode_pairs(["纳差乏力", "胃脘胀痛", "口苦咽干"], ["swapped"],
                                    consult_fn=_fake_consult(seen),
                                    retriever_mode="full_context")
    effect = ablation_output_effect(pairs["swapped"], None, "swapped")
    assert effect["n_total"] == 3
    assert effect["change_rate"] is not None
    # own 四味 vs swapped 换两味 → Jaccard 距离 0.5 > 0.4
    assert effect["change_rate"] >= 0.4


def test_e8_stays_on_the_top3_modes_only():
    """E8 比的是 top3 系内部四种模式的差异。把 full_context 塞进来会让这个
    指标变味，RESULTS.md 里 E8 那一行就跟历史值不可比了。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent / "eval" / "run_eval.py").read_text(
        encoding="utf-8")
    assert "e8_modes = sorted(TOP3_MODES)" in src
    assert "collect_retriever_mode_samples(queries, e8_modes)" in src
