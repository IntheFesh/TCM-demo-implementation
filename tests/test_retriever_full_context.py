"""R21：`full_context` 检索模式。

它"不检索"，但仍然是一个 mode——这样 `full_context` 跟 `top3` 系是同一条代码
路径上的两个取值，两组数才可比（绕过检索层的话对照里就混进了"换了代码路径"）。
"""
from __future__ import annotations

import json

from core.chain import _search_cases
from core.retrieval import FULL_CONTEXT_SCORE, FullContextRetriever, full_context_hits
from core.retrieval_hybrid import (
    ALLOWED_MODES,
    DEFAULT_MODE,
    RETRIEVER_MODE_ENV,
    TOP3_MODES,
    HybridRetriever,
    effective_mode,
)
from core.schemas import CaseRecord, S2Elements


def _case(cid, pid, **kw):
    base = dict(case_id=cid, case_group_id=cid, physician=pid, raw="原文" * 20,
                raw_excerpt="某患者，脘腹痞满，纳谷不香。", symptoms=["纳差"],
                herbs=["白术"], formula="四君子汤")
    base.update(kw)
    return CaseRecord(**base)


def _write_cases(tmp_path, cases):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([c.model_dump() for c in cases], ensure_ascii=False), encoding="utf-8")
    return p


# ---------- 注册与默认值 ----------

def test_full_context_is_registered_and_is_the_default():
    assert "full_context" in ALLOWED_MODES
    assert DEFAULT_MODE == "full_context"
    assert "full_context" not in TOP3_MODES


def test_the_old_four_modes_are_kept_as_the_comparison_arm():
    """旧四种一个没删：RESULTS.md 里那几行历史数据属于这一系。"""
    assert TOP3_MODES == {"dense", "bm25", "graph", "hybrid"}
    assert TOP3_MODES < ALLOWED_MODES


def test_effective_mode_is_the_single_place_the_env_var_is_read(monkeypatch):
    monkeypatch.delenv(RETRIEVER_MODE_ENV, raising=False)
    assert effective_mode() == DEFAULT_MODE
    assert effective_mode("hybrid") == "hybrid"
    monkeypatch.setenv(RETRIEVER_MODE_ENV, "bm25")
    assert effective_mode() == "bm25"
    # 显式传参压过环境变量
    assert effective_mode("dense") == "dense"


def test_only_retrieval_hybrid_reads_the_env_var_name():
    """`core/chain.py` 那条源码级测试钉住 chain 不许碰它；这一条钉住
    "名字本身只有一处"——别处要在消息里提它就 import 这个常量。"""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for rel in ("core/chain.py", "core/react.py", "api/main.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if '"RETRIEVER_MODE"' in code or "'RETRIEVER_MODE'" in code:
                hits.append(f"{rel}:{lineno}")
    assert hits == [], f"这些地方又写了一遍环境变量名：{hits}"


# ---------- 行为 ----------

def test_full_context_returns_every_case_of_that_physician(tmp_path):
    cases = [_case(f"ye-{i}", "ye_tianshi") for i in range(5)] + [_case("wu-0", "wu_jutong")]
    r = FullContextRetriever(cases_path=_write_cases(tmp_path, cases))
    hits = r.search("随便什么主诉", "ye_tianshi")
    assert [c.case_id for c, _ in hits] == [f"ye-{i}" for i in range(5)]
    assert all(score == FULL_CONTEXT_SCORE for _, score in hits)


def test_the_fixed_score_is_one_not_zero_and_not_none():
    """0.0 会被读成"完全不相关"，而语义恰恰相反；None 会在前端变成 null。"""
    assert FULL_CONTEXT_SCORE == 1.0
    assert FullContextRetriever.FIXED_SCORE == FULL_CONTEXT_SCORE


def test_k_and_min_score_are_ignored_but_said_out_loud(tmp_path, capsys):
    """静默忽略会让调用方以为自己限了条数。"""
    cases = [_case(f"ye-{i}", "ye_tianshi") for i in range(5)]
    r = FullContextRetriever(cases_path=_write_cases(tmp_path, cases))
    hits = r.search("q", "ye_tianshi", k=2, min_score=0.9)
    assert len(hits) == 5
    assert "忽略 k=2" in capsys.readouterr().err


def test_hybrid_retriever_delegates_to_the_same_function(tmp_path, monkeypatch):
    """两处各写一遍排序的话，两条路给出的医案顺序可能不同，
    而顺序不同 = 缓存前缀 byte 不同 = 缓存永远不命中。"""
    cases = [_case(f"ye-{i}", "ye_tianshi") for i in range(4)]
    path = _write_cases(tmp_path, cases)
    via_hybrid = HybridRetriever(cases_path=path).search("q", "ye_tianshi", mode="full_context")
    via_full = FullContextRetriever(cases_path=path).search("q", "ye_tianshi")
    assert [c.case_id for c, _ in via_hybrid] == [c.case_id for c, _ in via_full]
    direct = full_context_hits(cases, "ye_tianshi")
    assert [c.case_id for c, _ in direct] == [c.case_id for c, _ in via_full]


def test_full_context_never_loads_the_embedding_model(tmp_path):
    """这个模式不算相似度，碰模型就是白花几百 MB 和几十秒。"""
    cases = [_case(f"ye-{i}", "ye_tianshi") for i in range(3)]
    r = HybridRetriever(cases_path=_write_cases(tmp_path, cases))
    r.search("q", "ye_tianshi", mode="full_context")
    assert r._model is None


# ---------- refs_mode 的三种取值 ----------

class _Recorder:
    """记 `_format_case_block` 被调了几次、每次是谁——用来验"复用了那个函数"。"""

    def __init__(self):
        self.calls = []

    def __call__(self, case):
        from core.chain import _format_case_block

        self.calls.append(case.case_id)
        return _format_case_block(case)


def test_assemble_maps_own_swapped_none_onto_the_case_block():
    """own/swapped/none 不在 context_prefix 里再实现一遍——选谁由
    `_search_cases` 一处决定（它已经按 refs_mode 换过 physician 了）。"""
    from core.context_prefix import REFS_POINTER, REFS_POINTER_EMPTY, assemble

    mine = [_case("ye-0", "ye_tianshi")]
    theirs = [_case("wu-0", "wu_jutong")]

    own = assemble("ye_tianshi", case_block=mine, symptoms="纳差", materia={}, formulary={})
    assert "【参考医案】ye-0" in own and REFS_POINTER in own

    swapped = assemble("ye_tianshi", case_block=theirs, symptoms="纳差", materia={}, formulary={})
    # 指令段仍然是叶天士（这正是 E3 的条件：换语料不换口吻）
    assert "叶天士" in swapped and "【参考医案】wu-0" in swapped

    none = assemble("ye_tianshi", case_block=[], symptoms="纳差", materia={}, formulary={})
    assert REFS_POINTER_EMPTY in none and "【参考医案】" not in none


def test_assemble_uses_format_case_block_for_every_case():
    from core.context_prefix import assemble

    rec = _Recorder()
    cases = [_case(f"ye-{i}", "ye_tianshi") for i in range(3)]
    assemble("ye_tianshi", case_block=cases, symptoms="纳差", materia={}, formulary={},
             format_case=rec)
    assert rec.calls == ["ye-0", "ye-1", "ye-2"]


def test_search_cases_passes_the_mode_through(tmp_path, monkeypatch):
    """`_search_cases` 是全项目唯一一处把 retriever_mode 翻成 search() 参数的地方。"""
    from core import chain

    cases = [_case(f"ye-{i}", "ye_tianshi") for i in range(4)]
    r = HybridRetriever(cases_path=_write_cases(tmp_path, cases))
    monkeypatch.setattr(chain, "get_retriever", lambda: r)
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    hits, low = _search_cases("纳差", "ye_tianshi", s2, "full_context")
    assert len(hits) == 4
    assert low is False


def test_full_context_is_what_run_physician_uses_by_default(monkeypatch):
    """默认路径就是 full_context——这是这一轮的核心契约变更。"""
    monkeypatch.delenv(RETRIEVER_MODE_ENV, raising=False)
    assert effective_mode(None) == "full_context"
