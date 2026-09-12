"""core/retrieval_hybrid.py 的离线测试。

跟 tests/test_retrieval.py 一样的约束：不下载真实 embedding 模型。BM25/jieba
是纯本地计算，可以真跑；稠密那一路用 FakeModel 固定住返回向量——只是跳过
`SentenceTransformer` 的模型加载，不是跳过"稠密排序逻辑该怎么走"这件事本身。
"""
import json

import numpy as np
import pytest

from core import retrieval_hybrid as rh
from core.retrieval_hybrid import ALLOWED_MODES, HybridRetriever, _rrf_fuse
from core.schemas import CaseRecord


def _case(case_id, physician, symptoms):
    return CaseRecord(
        case_id=case_id, case_group_id=case_id, physician=physician,
        raw="原文", symptoms=symptoms,
    )


def _write_cases(tmp_path, cases):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps([c.model_dump() for c in cases]), encoding="utf-8"
    )
    return path


class FakeModel:
    """给 _dense_ranking/search 里 self._model.encode(query) 用的假模型，
    只需要认识测试里会问到的那几个 query 字符串。"""

    def __init__(self, query_vectors: dict[str, list[float]]):
        self._vectors = query_vectors

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True):
        return np.array([self._vectors[t] for t in texts])


def _install_fake_dense(retriever, embeddings, query_vectors):
    """绕过真实模型加载：_ensure_encoded 只在 self._model is None 时才会去下载，
    这里提前把 _model/_embeddings 灌好，_ensure_encoded 直接短路。"""
    retriever._model = FakeModel(query_vectors)
    retriever._embeddings = np.array(embeddings, dtype=float)


# ---------- _rrf_fuse：纯函数，跟检索器完全无关 ----------


def test_rrf_fuse_empty_rankings_returns_empty():
    assert _rrf_fuse([]) == []
    assert _rrf_fuse([[], []]) == []


def test_rrf_fuse_single_ranking_preserves_order():
    fused = _rrf_fuse([[5, 2, 8]])
    assert [i for i, _ in fused] == [5, 2, 8]


def test_rrf_fuse_matches_formula_by_hand():
    # ranking1: 0 排名1, 1 排名2, 2 排名3
    # ranking2: 2 排名1, 0 排名2, 1 排名3
    fused = _rrf_fuse([[0, 1, 2], [2, 0, 1]], rrf_k=60)
    scores = dict(fused)
    assert scores[0] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[1] == pytest.approx(1 / 62 + 1 / 63)
    assert scores[2] == pytest.approx(1 / 63 + 1 / 61)
    # 融合排名：两路都靠前的 0 第一，其次是在一路里拿到第一的 2，最后是 1
    assert [i for i, _ in fused] == [0, 2, 1]


def test_rrf_fuse_item_present_in_only_one_ranking_still_included():
    fused = _rrf_fuse([[0, 1], [9]])
    idxs = [i for i, _ in fused]
    assert set(idxs) == {0, 1, 9}


def test_rrf_fuse_item_in_both_rankings_outranks_top_of_single_ranking():
    """同时出现在两路里、即便名次不是各自第一，也该压过只在一路里排第一的项——
    这正是混合检索比单路检索多出来的信号。"""
    fused = _rrf_fuse([[0, 1], [1, 2]])
    idxs = [i for i, _ in fused]
    assert idxs[0] == 1  # 1 在两路都出现（第2名、第1名）


# ---------- jieba 自定义词典 ----------


def test_ensure_jieba_missing_dict_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(rh, "JIEBA_DICT_PATH", tmp_path / "不存在.txt")
    cases_path = _write_cases(tmp_path, [_case("a", "ye_tianshi", ["纳差"])])
    retriever = HybridRetriever(cases_path=cases_path)
    retriever._tokenize("随便一句话")  # 不该抛异常
    assert retriever._jieba_ready is True


def test_tokenize_keeps_custom_dict_word_whole(tmp_path, monkeypatch):
    dict_path = tmp_path / "jieba_dict.txt"
    dict_path.write_text("癥瘕 1000\n", encoding="utf-8")
    monkeypatch.setattr(rh, "JIEBA_DICT_PATH", dict_path)
    cases_path = _write_cases(tmp_path, [_case("a", "ye_tianshi", ["纳差"])])
    retriever = HybridRetriever(cases_path=cases_path)

    tokens = retriever._tokenize("患者素有癥瘕之疾")
    assert "癥瘕" in tokens


def test_ensure_jieba_is_lazy_and_idempotent(tmp_path, monkeypatch):
    dict_path = tmp_path / "jieba_dict.txt"
    dict_path.write_text("癥瘕 1000\n", encoding="utf-8")
    monkeypatch.setattr(rh, "JIEBA_DICT_PATH", dict_path)
    cases_path = _write_cases(tmp_path, [_case("a", "ye_tianshi", ["纳差"])])
    retriever = HybridRetriever(cases_path=cases_path)

    assert retriever._jieba_ready is False
    retriever._ensure_jieba()
    assert retriever._jieba_ready is True
    retriever._ensure_jieba()  # 第二次不该重新加载/报错
    assert retriever._jieba_ready is True


# ---------- BM25 一路：纯本地计算，不用碰 embedding 模型 ----------


def _filler_cases(physician="ye_tianshi"):
    """BM25 的经典 idf 公式在语料只有 2 篇时会退化：一个词只要不是两篇都出现，
    idf 算出来正好是 0（log((N-n+0.5)/(n+0.5)) 在 N=2, n=1 时等于 log(1)），
    "关键词命中"这个信号会被直接抹平，跟检索器本身对不对无关，是语料太小的
    数值现象。测试语料垫够 3 篇以上、且舌脉各不相同以绕开这个退化。"""
    return [
        _case_with_tongue_pulse("unrelated1", physician, ["口苦口黏", "身重困倦"], "黄腻", "滑"),
        _case_with_tongue_pulse("unrelated2", physician, ["腹痛喜按", "四肢不温"], "淡白", "沉迟"),
    ]


def _case_with_tongue_pulse(case_id, physician, symptoms, tongue, pulse):
    return CaseRecord(
        case_id=case_id, case_group_id=case_id, physician=physician,
        raw="原文", symptoms=symptoms, tongue=tongue, pulse=pulse,
    )


def test_bm25_ranking_prefers_exact_keyword_overlap(tmp_path):
    cases = [
        _case_with_tongue_pulse("match", "ye_tianshi", ["噎膈反胃", "食入即吐"], "红", "弦"),
        *_filler_cases(),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    idxs = [0, 1, 2]
    ranking = retriever._bm25_ranking("噎膈反胃，食入即吐", idxs)
    assert ranking[0][0] == 0  # 关键词重合的案子排第一，(下标, 分数) 元组


def test_bm25_ranking_only_covers_given_physician_idxs(tmp_path):
    cases = [
        _case("ye1", "ye_tianshi", ["噎膈反胃"]),
        _case("wu1", "wu_jutong", ["噎膈反胃"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    ranking = retriever._bm25_ranking("噎膈反胃", idxs=[0])
    assert [i for i, _ in ranking] == [0]


def test_bm25_ranking_has_no_min_score_parameter():
    """BM25 分数不在 [0,1] 的可比尺度上，不接受 min_score——这是 K3a 的明确设计，
    见 core/retrieval_hybrid.py 模块文档字符串。"""
    import inspect

    sig = inspect.signature(HybridRetriever._bm25_ranking)
    assert "min_score" not in sig.parameters


# ---------- P0-6：BM25 语料复用 DenseRetriever 过滤/编码过的 _case_texts ----------


def test_bm25_ranking_matches_on_raw_excerpt_content(tmp_path):
    """BM25 语料现在是 self._case_texts（含 raw_excerpt），不是重新调用
    _case_to_text——这里验证的是"raw_excerpt 里的关键词真的能被 BM25 检索到"，
    不是内部实现走了哪条路径。"""
    cases = [
        CaseRecord(case_id="thin", case_group_id="thin", physician="ye_tianshi",
                   raw="原文", symptoms=[], raw_excerpt="六两，加葶苈子二帖。"),
        *_filler_cases(),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    ranking = retriever._bm25_ranking("葶苈子", idxs=[0, 1, 2])
    assert ranking[0][0] == 0  # 只有 raw_excerpt 里含"葶苈子"的那条排第一


def test_no_content_case_is_excluded_from_bm25_corpus_and_indices_stay_aligned(tmp_path):
    """没有 symptoms 也没有 raw_excerpt 的医案在 DenseRetriever.__init__ 就被
    跳过（P0-6），不会进 self._cases——这里验证跳过之后 BM25 语料的下标仍然
    跟 self._cases 对齐，不会因为跳过了中间一条而错位。"""
    cases = [
        _case("keep1", "ye_tianshi", ["纳差乏力"]),
        CaseRecord(case_id="skip", case_group_id="skip", physician="ye_tianshi",
                   raw="原文", symptoms=[], raw_excerpt=None),
        _case("keep2", "ye_tianshi", ["噎膈反胃"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    assert [c.case_id for c in retriever._cases] == ["keep1", "keep2"]
    assert retriever.skipped_no_content_ids == ["skip"]
    ranking = retriever._bm25_ranking("噎膈反胃", idxs=[0, 1])
    assert dict(ranking).keys() == {0, 1}  # 下标只到 1（两条医案），没有越界


# ---------- search()：三种 mode 的调度 ----------


def _build_two_case_retriever(tmp_path, embeddings, query_vectors, with_filler=False):
    """idx0 稠密分高但 BM25 关键词不沾边；idx1 稠密分低但 BM25 关键词精确命中。
    这样才能同时验证稠密和关键词两路各自在起作用。

    with_filler=True 会再垫两篇不相关案例——BM25 经典 idf 公式在语料只有 2 篇
    时会退化到 0（见 test_bm25_ranking_prefers_exact_keyword_overlap 的注释），
    凡是要靠 BM25 排出相对名次的测试都需要这三篇以上的语料垫底；纯 dense 模式
    的测试不碰 BM25，不需要垫。"""
    cases = [
        _case("dense_favored", "ye_tianshi", ["纳差乏力"]),
        _case("bm25_favored", "ye_tianshi", ["噎膈反胃"]),
    ]
    if with_filler:
        cases += _filler_cases()
        embeddings = list(embeddings) + [[-1.0, -1.0]] * len(_filler_cases())
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    _install_fake_dense(retriever, embeddings, query_vectors)
    return retriever


def test_search_dense_mode_uses_only_dense_ranking(tmp_path):
    query = "噎膈反胃，食入即吐"
    retriever = _build_two_case_retriever(
        tmp_path,
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        query_vectors={query: [1.0, 0.0]},  # 只跟 idx0 对齐
    )
    hits = retriever.search(query, "ye_tianshi", k=2, mode="dense")
    assert [c.case_id for c, _ in hits] == ["dense_favored", "bm25_favored"]


def test_search_bm25_mode_uses_only_keyword_ranking(tmp_path):
    query = "噎膈反胃，食入即吐"
    retriever = _build_two_case_retriever(
        tmp_path,
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        query_vectors={query: [1.0, 0.0]},  # 稠密上更像 idx0，但 bm25 模式不看这个
        with_filler=True,
    )
    hits = retriever.search(query, "ye_tianshi", k=2, mode="bm25")
    assert hits[0][0].case_id == "bm25_favored"


def test_search_bm25_mode_never_touches_dense_model(tmp_path):
    """bm25 模式的排序和展示分都不该依赖稠密模型——K3a 的设计目标之一就是
    bm25 这一路能在没装/没下载 embedding 模型的环境里独立跑（这台沙箱连
    huggingface hub 都是网络隔离的，`_ensure_encoded` 会直接 403，见
    core/retrieval_hybrid.py 里 search() 的调度注释）。不装 FakeModel，
    _model 全程留 None，search 也不该报错。"""
    cases = [
        _case_with_tongue_pulse("match", "ye_tianshi", ["噎膈反胃", "食入即吐"], "红", "弦"),
        *_filler_cases(),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    hits = retriever.search("噎膈反胃，食入即吐", "ye_tianshi", k=1, mode="bm25")
    assert hits[0][0].case_id == "match"
    assert retriever._model is None  # 全程没碰过稠密模型


def test_search_hybrid_mode_display_score_is_dense_similarity(tmp_path):
    """hybrid 模式展示的分要跟 dense 模式同一把尺子（真实余弦相似度），不是
    RRF 融合分——RRF 分数是 1/(k+rank) 量级的小数，不是给人看的"相似度"。"""
    query = "噎膈反胃，食入即吐"
    cases = [
        _case("dense_top", "ye_tianshi", ["纳差乏力"]),
        _case("keyword_exact", "ye_tianshi", ["噎膈反胃", "食入即吐"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    _install_fake_dense(
        retriever, embeddings=[[1.0, 0.0], [0.0, 1.0]], query_vectors={query: [1.0, 0.0]}
    )

    hits = retriever.search(query, "ye_tianshi", k=2, mode="hybrid")
    scores = {c.case_id: score for c, score in hits}
    assert scores["dense_top"] == pytest.approx(1.0)  # 真实余弦相似度，不是融合分
    assert scores["keyword_exact"] == pytest.approx(0.0)  # 只在 bm25 那一路进榜，没有稠密分可展示


def test_search_hybrid_mode_promotes_case_relevant_in_either_signal(tmp_path):
    """hybrid 至少要能在某条主诉上把 dense-only 排不到前面的案子拉进 top-1——
    这是 K3a 的闸门标准。"""
    query = "噎膈反胃，食入即吐"
    cases = [
        _case("dense_top", "ye_tianshi", ["纳差乏力"]),
        _case("keyword_exact", "ye_tianshi", ["噎膈反胃", "食入即吐"]),
        _case("neither", "ye_tianshi", ["口苦口黏"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    # 稠密分：idx0 明显最像 query，idx1/idx2 都不像
    _install_fake_dense(
        retriever,
        embeddings=[[1.0, 0.0], [0.1, 0.1], [0.0, 1.0]],
        query_vectors={query: [1.0, 0.0]},
    )

    dense_hits = retriever.search(query, "ye_tianshi", k=1, mode="dense")
    hybrid_hits = retriever.search(query, "ye_tianshi", k=1, mode="hybrid")

    assert dense_hits[0][0].case_id == "dense_top"
    assert hybrid_hits[0][0].case_id != dense_hits[0][0].case_id
    assert hybrid_hits[0][0].case_id == "keyword_exact"


# ---------- P0-9：hybrid 融合阶段不能被 min_score 污染 ----------


def test_hybrid_dense_ranking_probe_ignores_the_requested_min_score(tmp_path, monkeypatch):
    """融合阶段必须用未过滤的 dense 排名——旧实现把真实 min_score 传进
    _dense_ranking，dense 路只剩少数条目，它们在融合里必然占据高排名
    （哪怕本身相似度只是刚过线），bm25 路排名靠后但没被 dense 卡掉的好结果
    打不过"dense+bm25 双路都有"的条目。这里用 spy 直接断言融合阶段调用
    _dense_ranking 时传的 min_score 是 0.0，不是请求方给的那个值。"""
    query = "q"
    cases = [_case("a", "ye_tianshi", ["纳差"]), _case("b", "ye_tianshi", ["乏力"])]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    _install_fake_dense(retriever, embeddings=[[1.0, 0.0], [0.0, 1.0]],
                        query_vectors={query: [1.0, 0.0]})

    seen_min_scores = []
    original = retriever._dense_ranking

    def spy(query, idxs, min_score):
        seen_min_scores.append(min_score)
        return original(query, idxs, min_score)

    monkeypatch.setattr(retriever, "_dense_ranking", spy)
    retriever.search(query, "ye_tianshi", k=2, mode="hybrid", min_score=0.9)
    assert seen_min_scores == [0.0], (
        f"融合阶段调用 _dense_ranking 时必须传 min_score=0.0（不过滤），"
        f"实际传的是 {seen_min_scores}——请求方给的 0.9 不该在这一步生效"
    )


def test_hybrid_mode_min_score_no_longer_gates_final_output(tmp_path):
    """**契约变更，P0-13**：这条测试原名
    test_hybrid_mode_actually_filters_final_output_by_min_score，原断言是
    "BM25 关键词命中但 dense 分很低的条目不该出现在 hybrid 结果里"——P0-13
    的诊断证明这条断言本身就是根因：融合之后再套一层单路 dense 阈值，等于
    让 dense 对 BM25 的发现拥有否决权（AutoDL 实测「情志不畅」那条主诉，
    全库唯一精确命中的医案就是这样被滤掉的，见
    test_fusion_admits_bm25_only_match_that_fails_the_dense_threshold）。
    P0-13 改动 1 之后 min_score 不再gate hybrid 的最终输出——这里反过来
    断言：BM25 关键词精确命中、dense 分很低的条目现在**会**出现在 hybrid
    结果里（min_score 只还在 dense 模式下起过滤作用，见下面
    test_search_min_score_only_filters_dense_path，那条测试的语义没变，
    dense 分支本身没被这次改动碰过）。"""
    query = "噎膈反胃"
    cases = [
        _case("keyword_match_low_dense", "ye_tianshi", ["噎膈反胃"]),
        _case("dense_favored", "ye_tianshi", ["纳差乏力"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    # idx0（关键词命中）稠密相似度很低；idx1 稠密相似度很高
    _install_fake_dense(
        retriever, embeddings=[[0.05, 0.0], [1.0, 0.0]],
        query_vectors={query: [1.0, 0.0]},
    )

    bm25_hits = retriever.search(query, "ye_tianshi", k=2, mode="bm25")
    assert bm25_hits[0][0].case_id == "keyword_match_low_dense"  # 纯 bm25 会选它

    hybrid_hits = retriever.search(query, "ye_tianshi", k=2, mode="hybrid", min_score=0.5)
    hybrid_ids = [c.case_id for c, _ in hybrid_hits]
    assert "keyword_match_low_dense" in hybrid_ids, (
        "dense 分只有 0.05、min_score=0.5——但它是 BM25 精确命中的条目，"
        "P0-13 之后不该再被单路 dense 阈值滤掉"
    )


def test_hybrid_mode_min_score_zero_keeps_old_behavior_unchanged(tmp_path):
    """min_score=0.0（默认）时融合前后都不过滤，跟改造前的行为一致——
    P0-9 只改了"非零 min_score 时过滤时机"，不该影响默认路径。"""
    query = "噎膈反胃，食入即吐"
    retriever = _build_two_case_retriever(
        tmp_path,
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        query_vectors={query: [1.0, 0.0]},
    )
    hits = retriever.search(query, "ye_tianshi", k=2, mode="hybrid")
    assert {c.case_id for c, _ in hits} == {"dense_favored", "bm25_favored"}


# ---------- P0-13：融合准入不能再套单路（dense）阈值 ----------
#
# 这是 P0-9 的返工。P0-9 把 min_score 过滤从"融合前"挪到了"融合后"，
# 但过滤条件本身没变——融合完成后仍然要求每条结果的 dense 相似度
# ≥ min_score。这在 k 很小（比如 k=3）时跟"融合前过滤"的效果一样：
# 能进最终结果的还是只有 dense 认可的那些，BM25 单独找到、dense 分不够
# 的条目一样进不去。真实案例：AutoDL 实测「情志不畅」这条主诉，全库
# 唯一精确命中"情志诱因"的医案（ye_tianshi-0030-p0-0）dense 排名在
# 50 名之外（dense 分远低于 adaptive_min_score 算出的 0.69-0.78），
# bm25 排第 2——无论 RRF 把它排多靠前，这个阈值都会把它滤掉。


def test_fusion_admits_bm25_only_match_that_fails_the_dense_threshold(tmp_path):
    """精确复现实测的失败场景：三条候选 A（dense 0.80，bm25 零命中）、
    B（dense 0.79，bm25 部分命中"情志"）、C（dense **0.50**，bm25 **最强**，
    唯一精确命中全部关键词的一条）。min_score=0.75 时 C 的 dense 分远低于
    阈值。另垫 2 条完全不相关的 filler——BM25 语料只有 3 篇时，"情志"这个
    词恰好出现在 2/3 篇里，idf 会退化成负数（跟 test_bm25_ranking_
    prefers_exact_keyword_overlap 那条注释描述的是同一类退化，只是触发
    条件不同：这里不是"语料只有 2 篇"，是"某词恰好出现在语料的一半"），
    垫够 5 篇才能让 idf 恢复成正常的正数、B 的部分命中才能正确排在 A（零
    命中）和 C（全部命中）之间——这个中间排名本身不是测试要断言的东西，
    只是为了让"C 是 bm25 里最强的那条"这个前提在正常的 idf 环境下成立，
    不依赖一次刚好触发退化的边界语料。

    这条测试必须先跑一次确认在修复前是红的（C 不在结果里），修复后
    （去掉融合后的单路阈值过滤）才应该转绿——不是一上来就是绿的断言，
    否则测不出真问题（P0-13 的自查清单第 10 条）。"""
    query = "情志不适即发"
    cases = [
        _case("A_dense_favored", "ye_tianshi", ["纳差", "乏力", "头晕"]),
        _case("B_dense_favored", "ye_tianshi", ["情志不畅", "胃痛"]),
        _case("C_bm25_exact_match", "ye_tianshi", ["情志不适即发"]),
        *_filler_cases(),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    # dense 分直接等于查询向量跟医案向量的点积（FakeModel 不做真实归一化，
    # 见 _install_fake_dense）：A=0.80，B=0.79，C=0.50，filler 远低于阈值。
    _install_fake_dense(
        retriever,
        embeddings=[[0.80, 0.0], [0.79, 0.0], [0.50, 0.0], [-1.0, 0.0], [-1.0, 0.0]],
        query_vectors={query: [1.0, 0.0]},
    )
    # bm25 排名实测（jieba 分词 + BM25Okapi）：C（精确命中"情志""不适""即发"）
    # 远高于 B（只命中"情志"）远高于 A/filler（零命中）。
    bm25_ranking = retriever._bm25_ranking(query, idxs=[0, 1, 2, 3, 4])
    assert [i for i, _ in bm25_ranking[:3]] == [2, 1, 0], (
        "这条断言只是确认测试数据本身符合设计意图（C 排 1，B 排 2，A 排 3），"
        "不是在测产品代码"
    )

    hits = retriever.search(query, "ye_tianshi", k=3, mode="hybrid", min_score=0.75)
    hit_ids = {c.case_id for c, _ in hits}
    assert "C_bm25_exact_match" in hit_ids, (
        "C 是全库唯一精确命中关键词的医案（bm25 排第 1），dense 分 0.50 远低于 "
        "min_score=0.75——如果这条断言失败，说明融合准入仍然在用单路 dense "
        "阈值卡它，P0-13 改动 1 没有生效"
    )


def test_search_min_score_only_filters_dense_path(tmp_path):
    query = "噎膈反胃"
    cases = [
        _case("low_dense_high_bm25", "ye_tianshi", ["噎膈反胃"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    _install_fake_dense(
        retriever, embeddings=[[0.1, 0.0]], query_vectors={query: [1.0, 0.0]}
    )
    # 稠密余弦相似度 = 0.1，明显低于 min_score=0.7

    dense_hits = retriever.search(query, "ye_tianshi", k=3, mode="dense", min_score=0.7)
    bm25_hits = retriever.search(query, "ye_tianshi", k=3, mode="bm25", min_score=0.7)

    assert dense_hits == []  # dense 模式：低于阈值的被过滤掉
    assert len(bm25_hits) == 1  # bm25 模式：min_score 不作用于这一路


def test_search_defaults_to_env_var_when_mode_not_passed(tmp_path, monkeypatch):
    query = "噎膈反胃"
    retriever = _build_two_case_retriever(
        tmp_path,
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        query_vectors={query: [1.0, 0.0]},
    )
    monkeypatch.setenv("RETRIEVER_MODE", "dense")
    hits = retriever.search(query, "ye_tianshi", k=1)
    assert hits[0][0].case_id == "dense_favored"


def test_search_unknown_mode_raises_value_error(tmp_path):
    cases_path = _write_cases(tmp_path, [_case("a", "ye_tianshi", ["纳差"])])
    retriever = HybridRetriever(cases_path=cases_path)
    with pytest.raises(ValueError, match="vector_db"):
        retriever.search("q", "ye_tianshi", mode="vector_db")


def test_search_graph_mode_without_query_elements_raises_not_silently_degrades(tmp_path):
    """K3b 的明确设计：mode='graph' 不传 query_elements 必须报错，不能悄悄
    退回别的模式——调用方会以为自己拿到的是证素路的结果。"""
    cases_path = _write_cases(tmp_path, [_case("a", "ye_tianshi", ["纳差"])])
    retriever = HybridRetriever(cases_path=cases_path)
    with pytest.raises(ValueError, match="query_elements"):
        retriever.search("q", "ye_tianshi", mode="graph")
    with pytest.raises(ValueError, match="query_elements"):
        retriever.search("q", "ye_tianshi", mode="graph", query_elements=[])


class FakeElementRetriever:
    """跳过真实 data/element_index.json——直接注入 ranking() 的返回值。"""

    def __init__(self, ranking_by_case_ids: dict):
        self._ranking_by_case_ids = ranking_by_case_ids

    def ranking(self, query_elements, case_ids):
        return self._ranking_by_case_ids.get(tuple(sorted(case_ids)), [])


def test_search_graph_mode_ranks_by_element_overlap(tmp_path):
    cases = [
        _case("no_overlap", "ye_tianshi", ["口苦"]),
        _case("full_overlap", "ye_tianshi", ["胃脘胀满"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    retriever._element_retriever = FakeElementRetriever({
        ("full_overlap", "no_overlap"): [("full_overlap", 0.8)],
    })

    hits = retriever.search("q", "ye_tianshi", k=2, mode="graph", query_elements=["脾", "气滞"])
    assert [c.case_id for c, _ in hits] == ["full_overlap"]
    assert hits[0][1] == pytest.approx(0.8)  # 展示分就是真实 Jaccard 相似度
    assert retriever._model is None  # graph 模式不该碰稠密模型


def test_search_hybrid_mode_without_query_elements_stays_two_way(tmp_path):
    """向后兼容：不传 query_elements 时 hybrid 模式的行为不变（K3a 那一版的
    两路融合），不强制调用方在算出证素之前就提供它。"""
    query = "噎膈反胃，食入即吐"
    retriever = _build_two_case_retriever(
        tmp_path,
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        query_vectors={query: [1.0, 0.0]},
        with_filler=True,
    )
    retriever.search(query, "ye_tianshi", k=1, mode="hybrid")
    assert retriever._element_retriever is None  # 没传 query_elements，不该去碰它


def test_search_hybrid_mode_with_query_elements_fuses_three_ways(tmp_path):
    """传了 query_elements 就该三路融合：一个案子只有 graph 一路支持，
    dense/bm25 都排不到它，也该能借着 graph 信号进 top-1。"""
    query = "无关查询文本"
    cases = [
        _case("dense_favored", "ye_tianshi", ["其他症状"]),
        _case("graph_only_favored", "ye_tianshi", ["图谱信号案例"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    _install_fake_dense(
        retriever, embeddings=[[1.0, 0.0], [0.0, 0.0]], query_vectors={query: [1.0, 0.0]}
    )
    retriever._element_retriever = FakeElementRetriever({
        ("dense_favored", "graph_only_favored"): [("graph_only_favored", 0.9)],
    })

    hits = retriever.search(query, "ye_tianshi", k=1, mode="hybrid", query_elements=["脾"])
    assert hits[0][0].case_id == "graph_only_favored"


def test_search_empty_physician_returns_empty_without_touching_model(tmp_path):
    cases_path = _write_cases(tmp_path, [_case("a", "ye_tianshi", ["纳差"])])
    retriever = HybridRetriever(cases_path=cases_path)
    assert retriever.search("q", "wu_jutong", mode="hybrid") == []
    assert retriever._model is None  # 没有案子可比，不该去碰模型


def test_allowed_modes_includes_graph():
    assert ALLOWED_MODES == {"dense", "bm25", "graph", "hybrid"}
