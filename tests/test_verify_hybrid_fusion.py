"""scripts/verify_hybrid_fusion.py 的离线测试：假检索器验证脚本本身的
逻辑（命中判定、退出码、参数解析），不需要真实语料——那部分（P0-13
验证 C）留给 AutoDL 上跑这个脚本本身。
"""
from types import SimpleNamespace

from core.retrieval import Retriever
from scripts import verify_hybrid_fusion as vhf


def _case(case_id):
    return SimpleNamespace(case_id=case_id)


class _FixedRankingRetriever(Retriever):
    """按 mode 返回不同的排名列表——不管 query/physician/k/min_score
    是什么，只用来测 main() 怎么消费 search() 的返回值。"""

    def __init__(self, rankings_by_mode: dict[str, list[str]]):
        self._rankings_by_mode = rankings_by_mode

    def search(self, query, physician, k=3, min_score=0.0, mode=None, **kwargs):
        ids = self._rankings_by_mode.get(mode, [])
        return [(_case(cid), 1.0 - i * 0.01) for i, cid in enumerate(ids[:k])]


def _install(monkeypatch, retriever):
    """main() 内部用 `from core.retrieval import get_retriever` 局部导入，
    补丁要打在 core.retrieval 模块本身上，不是 scripts.verify_hybrid_fusion
    ——后者在 main() 执行前根本没有这个名字。"""
    import core.retrieval as retrieval_mod

    monkeypatch.setattr(retrieval_mod, "get_retriever", lambda: retriever)


def test_main_returns_zero_when_expected_case_id_is_in_hybrid_top_n(monkeypatch, capsys):
    retriever = _FixedRankingRetriever({
        "dense": ["a", "b", "c"],
        "bm25": ["x", "target", "y"],
        "hybrid": ["a", "target", "c"],
    })
    _install(monkeypatch, retriever)

    code = vhf.main(["--physician", "ye_tianshi", "--expect-case-id", "target"])
    out = capsys.readouterr().out

    assert code == 0
    assert "★命中" in out
    assert "target ★" in out


def test_main_returns_one_when_expected_case_id_is_not_in_hybrid_top_n(monkeypatch, capsys):
    retriever = _FixedRankingRetriever({
        "dense": ["a", "b", "c"],
        "bm25": ["target", "x", "y"],
        "hybrid": ["a", "b", "c"],  # target 不在 hybrid top-3 里
    })
    _install(monkeypatch, retriever)

    code = vhf.main(["--physician", "ye_tianshi", "--expect-case-id", "target"])
    err = capsys.readouterr().err

    assert code == 1
    # 最终结论打 stderr——退出码之外还要有一行不依赖 stdout 的失败原因。
    assert "✗ 未命中，本次修复未生效" in err


def test_main_reports_rank_beyond_top_n_when_not_hit(monkeypatch, capsys):
    """没进 top-3 但在更宽的诊断名单里能找到时，要报出真实排名——
    跟 P0-13 报告里"这条不在 top-50 里"是同一种诊断方式。"""
    hybrid_ranking = ["a", "b", "c"] + [f"filler{i}" for i in range(10)] + ["target"]
    retriever = _FixedRankingRetriever({
        "dense": ["a", "b", "c"],
        "bm25": ["x", "y", "z"],
        "hybrid": hybrid_ranking,
    })
    _install(monkeypatch, retriever)

    code = vhf.main(["--physician", "ye_tianshi", "--expect-case-id", "target"])
    out = capsys.readouterr().out

    assert code == 1
    assert f"排第 {len(hybrid_ranking)} 名" in out


def test_main_reports_not_in_wide_list_when_absent_everywhere(monkeypatch, capsys):
    retriever = _FixedRankingRetriever({
        "dense": ["a", "b", "c"],
        "bm25": ["x", "y", "z"],
        "hybrid": ["a", "b", "c"],
    })
    _install(monkeypatch, retriever)

    code = vhf.main(["--physician", "ye_tianshi", "--expect-case-id", "nowhere"])
    out = capsys.readouterr().out

    assert code == 1
    assert "不在前" in out


def test_main_uses_default_query_physician_and_expect_case_id_when_no_args(monkeypatch, capsys):
    """默认参数就是 P0-13 报告里那条真实案例——裸跑这个脚本（不带任何参数）
    要能直接验证那条案例，用户说明里明确要求"一条命令跑完"。"""
    seen_queries = []

    class _RecordingRetriever(Retriever):
        def search(self, query, physician, k=3, min_score=0.0, mode=None, **kwargs):
            seen_queries.append((query, physician, mode))
            return []

    import core.retrieval as retrieval_mod
    monkeypatch.setattr(retrieval_mod, "get_retriever", lambda: _RecordingRetriever())

    vhf.main([])

    assert all(q == vhf.DEFAULT_QUERY for q, _, _ in seen_queries)
    assert all(p == vhf.DEFAULT_PHYSICIAN for _, p, _ in seen_queries)
    assert {m for _, _, m in seen_queries} == {"dense", "bm25", "hybrid"}


def test_main_returns_one_when_retriever_unavailable(monkeypatch, capsys):
    import core.retrieval as retrieval_mod

    def _raise():
        raise FileNotFoundError("未找到 cases.json")

    monkeypatch.setattr(retrieval_mod, "get_retriever", _raise)

    code = vhf.main([])
    err = capsys.readouterr().err

    assert code == 1
    assert "cases.json" in err


def test_main_accepts_custom_top_n(monkeypatch, capsys):
    retriever = _FixedRankingRetriever({
        "dense": ["a", "b", "c", "d", "e"],
        "bm25": ["a", "b", "c", "d", "e"],
        "hybrid": ["a", "b", "c", "d", "target"],
    })
    _install(monkeypatch, retriever)

    # top-3 时看不到 target（排第 5），top-5 时能看到
    code_top3 = vhf.main(["--expect-case-id", "target", "--top-n", "3"])
    code_top5 = vhf.main(["--expect-case-id", "target", "--top-n", "5"])

    assert code_top3 == 1
    assert code_top5 == 0
