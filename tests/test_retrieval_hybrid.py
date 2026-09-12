"""core/retrieval_hybrid.py 的离线测试。

跟 tests/test_retrieval.py 一样的约束：不下载真实 embedding 模型。BM25/jieba
是纯本地计算，可以真跑；稠密那一路用 FakeModel 固定住返回向量——只是跳过
`SentenceTransformer` 的模型加载，不是跳过"稠密排序逻辑该怎么走"这件事本身。
"""
import json

import numpy as np
import pytest

from core import retrieval_hybrid as rh
from core.retrieval_hybrid import ALLOWED_MODES, HybridRetriever, _apply_bm25_floor, _rrf_fuse
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


def test_graph_ranking_has_no_min_score_parameter():
    """跟上面 bm25 那条镜像的测试（P0-13 独立审计发现的覆盖缺口）：
    _graph_ranking 同样不接受 min_score，之前只在注释/文档字符串里提过，
    没有独立断言过。K3b 的 _graph_ranking 文档字符串已经说明了理由——
    证素集合通常只有两三个元素，Jaccard 相似度哪怕只共享一个证素也能到
    0.3-0.5，套用为稠密分校准的阈值会把这条信号基本过滤没。"""
    import inspect

    sig = inspect.signature(HybridRetriever._graph_ranking)
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


# ---------- P0-13 续：RRF 把强 bm25 信号压到 top-N 之外，bm25 保底修复 ----------
#
# 上面 test_fusion_admits_bm25_only_match_that_fails_the_dense_threshold 修的是
# "进不进候选池"；这里修的是"进了候选池、RRF 排完名之后排第几"——AutoDL 真实
# 语料复现：目标医案 dense 排名 100 开外、bm25 排第 2，进了候选池但被 RRF 压到
# 第 31 名。见 core/retrieval_hybrid.py 里 BM25_FLOOR_N 的注释，三个修法的
# 算法比较用的就是这条测试的数字。


def test_apply_bm25_floor_promotes_bm25_top_n_ahead_of_everything_else():
    fused = [(10, 0.9), (11, 0.8), (12, 0.02), (13, 0.01)]  # RRF 融合序，12/13 排最后
    bm25_ranking = [(12, 99.0), (13, 90.0), (10, 1.0), (11, 0.5)]  # bm25 里 12/13 才是前两名
    promoted = _apply_bm25_floor(fused, bm25_ranking, floor_n=2)
    assert [i for i, _ in promoted[:2]] == [12, 13]  # 保底的两条被提到最前面
    assert [i for i, _ in promoted[2:]] == [10, 11]  # 组内顺序仍按 fused 分，不是打乱成 bm25 分排序


def test_apply_bm25_floor_zero_is_no_op():
    fused = [(1, 0.9), (2, 0.1)]
    bm25_ranking = [(2, 5.0), (1, 1.0)]
    assert _apply_bm25_floor(fused, bm25_ranking, floor_n=0) == fused


def test_apply_bm25_floor_does_not_duplicate_items_already_high_in_fused():
    """保底集合跟 fused 前几名重叠时不该出现重复条目。"""
    fused = [(1, 0.9), (2, 0.5), (3, 0.1)]
    bm25_ranking = [(1, 9.0), (2, 1.0), (3, 0.5)]  # 1 本来就是 bm25 第一，也是 fused 第一
    promoted = _apply_bm25_floor(fused, bm25_ranking, floor_n=1)
    assert [i for i, _ in promoted] == [1, 2, 3]  # 没有重复，顺序不变


def _filler_pool(n, physician="ye_tianshi", prefix="filler"):
    return [_case(f"{prefix}{i}", physician, ["占位"]) for i in range(n)]


def test_bm25_floor_rescues_target_that_rrf_ranks_below_top_n(tmp_path, monkeypatch):
    """精确复现 AutoDL 实测的失败场景：目标医案 dense 排名 100（全库倒数）、
    bm25 排第 2（全库唯一精确命中），对手 A/B/C 分别是 dense 第 1/2/3 名、
    bm25 第 50/60/80 名——跟 core/retrieval_hybrid.py 里 BM25_FLOOR_N 那段
    注释算的是同一组数字。用 monkeypatch 直接注入这组排名（不依赖真实
    embedding/BM25 恰好算出这几个名次，那样没法稳定复现），确认 bm25 保底
    修复后目标能进 hybrid 的 top-3。

    这条测试在 BM25_FLOOR_N 引入前必须是红的——见下面
    test_bm25_floor_disabled_reproduces_the_original_failure，把 floor_n
    强制夹成 0 后同样的场景确实未命中，证明这条测试真的钉住了这次修复，
    不是数据凑巧一开始就是绿的（P0-13 自查清单第 10 条同款要求）。"""
    target_idx, a_idx, b_idx, c_idx = 0, 1, 2, 3
    cases = [
        _case("target", "ye_tianshi", ["占位"]),
        _case("A", "ye_tianshi", ["占位"]),
        _case("B", "ye_tianshi", ["占位"]),
        _case("C", "ye_tianshi", ["占位"]),
        *_filler_pool(96),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    filler_idxs = list(range(4, 100))  # 96 个 filler，idx 4~99

    # dense 排名：A/B/C 第 1/2/3，96 个 filler 占第 4~99 名，目标垫底第 100。
    dense_ranking = [(a_idx, 0.99), (b_idx, 0.98), (c_idx, 0.97)]
    dense_ranking += [(i, 0.5) for i in filler_idxs]
    dense_ranking.append((target_idx, 0.01))
    assert len(dense_ranking) == 100

    # bm25 排名：目标第 2 名，A/B/C 第 50/60/80 名，其余名次用 filler 填满，
    # 保证"第 X 名"是真实名次，不是凑出来的近似值。filler 消费顺序跟上面
    # dense 那段反过来（reversed）——避免同一个 filler 在两路都排前面，
    # 那样会让它跟 A/B/C 抢 fused 前几名，稀释了这条测试要验证的东西。
    bm25_fillers = iter(reversed(filler_idxs))

    def take(n):
        return [next(bm25_fillers) for _ in range(n)]

    bm25_ranking = [(i, 10.0) for i in take(1)]        # 第 1 名：filler
    bm25_ranking.append((target_idx, 9.9))              # 第 2 名：目标
    bm25_ranking += [(i, 5.0) for i in take(47)]         # 第 3~49 名
    bm25_ranking.append((a_idx, 2.0))                    # 第 50 名：A
    bm25_ranking += [(i, 1.9) for i in take(9)]           # 第 51~59 名
    bm25_ranking.append((b_idx, 1.5))                     # 第 60 名：B
    bm25_ranking += [(i, 1.4) for i in take(19)]           # 第 61~79 名
    bm25_ranking.append((c_idx, 1.0))                      # 第 80 名：C
    bm25_ranking += [(i, 0.5) for i in take(20)]            # 第 81~100 名
    assert len(bm25_ranking) == 100
    assert next(bm25_fillers, None) is None  # 恰好用完 96 个 filler，没多也没少

    monkeypatch.setattr(retriever, "_dense_ranking",
                         lambda query, idxs, min_score: dense_ranking)
    monkeypatch.setattr(retriever, "_bm25_ranking",
                         lambda query, idxs: bm25_ranking)

    hits = retriever.search("q", "ye_tianshi", k=3, mode="hybrid")
    hit_ids = {c.case_id for c, _ in hits}
    assert "target" in hit_ids, (
        "目标 dense#100/bm25#2，RRF_K=60 下融合分排在 A/B/C 之后（算法见 "
        "core/retrieval_hybrid.py 的 BM25_FLOOR_N 注释）——bm25 保底（N=2）"
        "应该把它强制留在 top-3 里，不管 RRF 怎么排"
    )


def test_bm25_floor_disabled_reproduces_the_original_failure(tmp_path, monkeypatch):
    """跟上面那条同一组数据，把 floor_n 强制夹成 0——证明上面那条测试是真的
    在测 bm25 保底这个机制，不是巧合地一开始就是绿的。"""
    target_idx, a_idx, b_idx, c_idx = 0, 1, 2, 3
    cases = [
        _case("target", "ye_tianshi", ["占位"]),
        _case("A", "ye_tianshi", ["占位"]),
        _case("B", "ye_tianshi", ["占位"]),
        _case("C", "ye_tianshi", ["占位"]),
        *_filler_pool(96),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)

    filler_idxs = list(range(4, 100))
    dense_ranking = [(a_idx, 0.99), (b_idx, 0.98), (c_idx, 0.97)]
    dense_ranking += [(i, 0.5) for i in filler_idxs]
    dense_ranking.append((target_idx, 0.01))

    bm25_fillers = iter(reversed(filler_idxs))

    def take(n):
        return [next(bm25_fillers) for _ in range(n)]

    bm25_ranking = [(i, 10.0) for i in take(1)]
    bm25_ranking.append((target_idx, 9.9))
    bm25_ranking += [(i, 5.0) for i in take(47)]
    bm25_ranking.append((a_idx, 2.0))
    bm25_ranking += [(i, 1.9) for i in take(9)]
    bm25_ranking.append((b_idx, 1.5))
    bm25_ranking += [(i, 1.4) for i in take(19)]
    bm25_ranking.append((c_idx, 1.0))
    bm25_ranking += [(i, 0.5) for i in take(20)]

    monkeypatch.setattr(retriever, "_dense_ranking",
                         lambda query, idxs, min_score: dense_ranking)
    monkeypatch.setattr(retriever, "_bm25_ranking",
                         lambda query, idxs: bm25_ranking)
    monkeypatch.setattr(rh, "BM25_FLOOR_N", 0)

    hits = retriever.search("q", "ye_tianshi", k=3, mode="hybrid")
    hit_ids = {c.case_id for c, _ in hits}
    assert "target" not in hit_ids, (
        "没有保底时，目标应该还是原来那个失败场景——RRF 分排在 A/B/C 之后，"
        "进不了 top-3。如果这条测试也是绿的，说明上面那条测试的绿不是保底"
        "机制带来的，是数据本身凑巧"
    )


def test_hybrid_floor_leaves_room_for_pure_rrf_when_k_is_small(tmp_path, monkeypatch):
    """floor_n 会被夹到 min(BM25_FLOOR_N, k-1)——k 很小时不能让保底吃掉全部
    名额，否则那次调用 hybrid 的结果集合跟纯 bm25 的 top-N 完全一样，E8
    消融就失去意义了（见 core/retrieval_hybrid.py 里 BM25_FLOOR_N 注释）。"""
    cases = [
        _case("X_bm25_top", "ye_tianshi", ["占位"]),
        _case("Y_bm25_second", "ye_tianshi", ["占位"]),
        _case("Z_dense_top_bm25_worst", "ye_tianshi", ["占位"]),
    ]
    cases_path = _write_cases(tmp_path, cases)
    retriever = HybridRetriever(cases_path=cases_path)
    # dense：Z 第一，X 第二，Y 第三。bm25：X 第一，Y 第二，Z 垫底。
    # RRF_K=60 手算：X=1/62+1/61=0.032522，Z=1/61+1/63=0.032266，
    # Y=1/63+1/62=0.032002——纯 RRF 排名是 X > Z > Y，Z 本来就该赢 Y。
    monkeypatch.setattr(retriever, "_dense_ranking",
                         lambda query, idxs, min_score: [(2, 0.99), (0, 0.5), (1, 0.4)])
    monkeypatch.setattr(retriever, "_bm25_ranking",
                         lambda query, idxs: [(0, 9.0), (1, 8.0), (2, 0.1)])

    hits = retriever.search("q", "ye_tianshi", k=2, mode="hybrid")
    hit_ids = {c.case_id for c, _ in hits}

    assert hit_ids == {"X_bm25_top", "Z_dense_top_bm25_worst"}, (
        "k=2 时保底应该被夹到 floor_n=min(2, k-1)=1——只强制留 bm25 第一名 "
        "1 个名额，剩下 1 个名额留给纯 RRF 排名，让 Z（RRF 分比 Y 高但 "
        "bm25 排最后）顶掉 Y（bm25 第二但 RRF 分更低）。如果保底不夹这个 "
        "上限，k=2 时 hybrid 会退化成跟 bm25 的 top-2 完全一样（X, Y），"
        "Z 这条纯 RRF 的贡献就被抹掉了"
    )
