"""K3a/K3b：混合检索。在 DenseRetriever 的稠密向量检索之外叠加 BM25 关键词
检索（K3a）和证素路检索（K3b），用 Reciprocal Rank Fusion（RRF）融合排名。

为什么是 RRF 而不是加权求和：稠密分是归一化余弦相似度，落在 [0,1]；BM25 分
是无界的、还随语料规模变化（idf 项）。两者要加权求和，必须先把 BM25 分数
归一化到可比尺度——而"怎么归一化"本身就是一个没有验证过的超参数，等于凭空
引入一个新的、没有基准的自由度。RRF 只用排名（第几名），不看分数绝对值，
不需要这一步，天然规避了这个问题。RRF_K=60 是原论文（Cormack et al. 2009）
的经验值，这里没有针对本项目重新调过——同样是"不过度设计"：demo 阶段用文献
默认值，不为一个未经验证的收益去引入调参。

jieba 分词必须加载自定义词典（`offline/build_jieba_dict.py` 的产物），否则
中医术语会被切碎，BM25 的关键词匹配等于失效。实测（这台沙箱没有 cases.json，
词典只来自 ELEMENTS + SYNONYMS 两个源，下面这条是这两个源里真实验证过的）：
    >>> list(jieba.cut("癥瘕"))
    ['癥', '瘕']          # 未加词典：拆成两个字
    >>> jieba.load_userdict("data/jieba_dict.txt")
    >>> list(jieba.cut("癥瘕"))
    ['癥瘕']              # 加词典后：识别成一个词
拆开后 BM25 用词袋模型算分时，"癥瘕"作为一个整体术语的匹配信号就丢了；
加载词典后才能被当成一个词正确命中。cases.json 生成后（跑
`offline/extract_cases.py`）词典会并入真实医案里的高频症状表述，覆盖面
更大，但机制是一样的——不需要 cases.json 才能验证这条设计成立。

**graph 一路（K3b）走的是结构化信号，不是文本信号。** 给定这次问诊 S2 已经
推断出的证素（`query_elements`），按"这条医案连到多少个同样的证素"打分——
两条医案文字表述完全不同，只要底层证素一致，这条路能把它们连起来，dense/
bm25 都做不到。具体打分逻辑在 core/retrieval_graph.py（ElementRetriever），
这里只管调度。`mode="graph"` 要求调用方显式传 `query_elements`，不传就报错，
不会静默退化成别的模式——"我请求了 graph 检索，结果却是别的检索"这种静默
降级比报错更危险，调用方会以为拿到的是证素路的结果。`mode="hybrid"` 则相反：
传了 `query_elements` 就三路融合，不传就退回 K3a 的两路融合（dense+bm25）
——不强制调用方在还没算出证素之前就提供它，两路融合本来就是 hybrid 一直
以来的行为，多一路信号是增益，不提供不算错。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from core.retrieval import DenseRetriever, CASES_PATH, _case_to_text
from core.retrieval_graph import ElementRetriever
from core.schemas import CaseRecord

JIEBA_DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "jieba_dict.txt"

# RRF 的经验常数，见模块文档字符串。
RRF_K = 60

ALLOWED_MODES = {"dense", "bm25", "graph", "hybrid"}


def _rrf_fuse(
    rankings: list[list[int]], rrf_k: int = RRF_K
) -> list[tuple[int, float]]:
    """输入若干路排名（每路是按相关性降序的文档下标列表），输出融合后的
    (下标, 融合分) 列表，按融合分降序。纯函数，不依赖检索器状态，方便单测。"""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking, start=1):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (rrf_k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


class HybridRetriever(DenseRetriever):
    """继承 DenseRetriever 复用稠密检索那一路（_ensure_encoded/_embeddings），
    新增 BM25 一路和 RRF 融合。mode 由调用方传入，缺省读 RETRIEVER_MODE 环境
    变量——这样 V1 的 E8 消融只需要改环境变量重跑，不需要换检索器实例。"""

    def __init__(self, cases_path: Path = CASES_PATH):
        super().__init__(cases_path)
        self._bm25 = None  # 惰性构建，避免 import/构造阶段做重操作
        self._bm25_lock = threading.Lock()
        self._jieba_ready = False
        self._jieba_lock = threading.Lock()
        self._element_retriever: ElementRetriever | None = None
        self._element_retriever_lock = threading.Lock()
        # case_id -> 下标，供 graph 一路把 ElementRetriever 返回的 case_id 换回
        # 跟 dense/bm25 同一套下标体系去融合。案子数量是几百的量级，不是
        # "加载模型/大文件"，不需要惰性。
        self._case_id_to_idx = {c.case_id: i for i, c in enumerate(self._cases)}

    def _ensure_jieba(self) -> None:
        if self._jieba_ready:
            return
        with self._jieba_lock:
            if self._jieba_ready:
                return
            import jieba

            if JIEBA_DICT_PATH.exists():
                jieba.load_userdict(str(JIEBA_DICT_PATH))
            self._jieba_ready = True

    def _tokenize(self, text: str) -> list[str]:
        import jieba

        self._ensure_jieba()
        return [w for w in jieba.lcut(text) if w.strip()]

    def _ensure_bm25(self) -> None:
        if self._bm25 is not None:
            return
        with self._bm25_lock:
            if self._bm25 is not None:
                return
            from rank_bm25 import BM25Okapi

            self._ensure_jieba()
            corpus = [self._tokenize(_case_to_text(c)) for c in self._cases]
            self._bm25 = BM25Okapi(corpus)

    def _dense_ranking(
        self, query: str, idxs: list[int], min_score: float
    ) -> list[tuple[int, float]]:
        """按稠密相似度降序返回 (下标, 真实余弦相似度) 全排名（不截断到 k）。
        min_score 只作用于这一路——BM25 的分数不在同一尺度上，套用同一个阈值
        没有意义。返回的分是未经初诊加成的真实相似度，加成只用于排序。"""
        self._ensure_encoded()
        query_vec = self._model.encode(
            [query], normalize_embeddings=True, convert_to_numpy=True
        )[0]
        scored = []
        for i in idxs:
            raw_score = float(self._embeddings[i] @ query_vec)
            if raw_score < min_score:
                continue
            vi = self._cases[i].visit_index or 0
            rank_score = raw_score * (self.INITIAL_VISIT_BOOST if vi == 0 else 1.0)
            scored.append((i, rank_score, raw_score))
        scored.sort(key=lambda x: -x[1])
        return [(i, raw) for i, _rank, raw in scored]

    def _bm25_ranking(self, query: str, idxs: list[int]) -> list[tuple[int, float]]:
        """按 BM25 分降序返回 (下标, BM25 分) 全排名。不做 min_score 过滤，
        见类文档字符串。"""
        self._ensure_bm25()
        tokens = self._tokenize(query)
        all_scores = self._bm25.get_scores(tokens)
        scored = [(i, float(all_scores[i])) for i in idxs]
        scored.sort(key=lambda x: -x[1])
        return scored

    def _ensure_element_retriever(self) -> ElementRetriever:
        if self._element_retriever is not None:
            return self._element_retriever
        with self._element_retriever_lock:
            if self._element_retriever is None:
                self._element_retriever = ElementRetriever()
            return self._element_retriever

    def _graph_ranking(
        self, query_elements: list[str], idxs: list[int]
    ) -> list[tuple[int, float]]:
        """按证素 Jaccard 相似度降序返回 (下标, 相似度)。相似度就是真实
        Jaccard 值（[0,1] 有界），跟 dense 的余弦相似度同一个刻度，可以直接
        当展示分用，不像 BM25 分数那样需要区分"排序用"和"展示用"。不做
        min_score 过滤——0.70 是拿稠密相似度的实测分布校准出来的阈值
        （见 core/retrieval.py 的 MIN_RETRIEVAL_SCORE 注释），证素集合通常
        只有两三个元素，Jaccard 在这种小集合上哪怕只共享一个证素也能到
        0.3-0.5，套用为稠密分校准的阈值会把 graph 这条信号基本上过滤没。"""
        retriever = self._ensure_element_retriever()
        case_ids = [self._cases[i].case_id for i in idxs]
        by_case_id = retriever.ranking(query_elements, case_ids)
        return [(self._case_id_to_idx[cid], score) for cid, score in by_case_id]

    def search(
        self,
        query: str,
        physician: str,
        k: int = 3,
        min_score: float = 0.0,
        mode: str | None = None,
        query_elements: list[str] | None = None,
    ) -> list[tuple[CaseRecord, float]]:
        mode = mode or os.environ.get("RETRIEVER_MODE", "hybrid")
        if mode not in ALLOWED_MODES:
            raise ValueError(
                f"未知的 RETRIEVER_MODE={mode!r}，目前支持 {sorted(ALLOWED_MODES)}"
            )
        if mode == "graph" and not query_elements:
            # 非静默降级：请求的是证素路检索，没给证素就该报错，不能悄悄退回
            # 别的模式——调用方会以为自己拿到的是证素路的结果。
            raise ValueError(
                "mode='graph' 需要传非空的 query_elements（S2 推断出的证素列表）"
            )

        idxs = [i for i, c in enumerate(self._cases) if c.physician == physician]
        if not idxs:
            return []

        # 展示分跟排序用的分是同一路的：dense/graph/hybrid（hybrid 本来就要
        # 算稠密相似度去融合）展示真实余弦相似度或真实 Jaccard 相似度，两者都是
        # [0,1] 有界、前端/prompt 里"相似度"这个词才有意义；bm25 模式展示 BM25
        # 原始分——不强行套一个没参与排序的稠密分，否则 bm25-only 就必须为了
        # "好看的展示数字"去多算一次稠密编码，白白引入了这条路径本不需要的模型
        # 依赖（K3a 的设计目标之一就是 bm25 模式应该能在没有 embedding 模型的
        # 环境里独立跑，见 tests/test_retrieval_hybrid.py）。
        if mode == "dense":
            scored = self._dense_ranking(query, idxs, min_score)
        elif mode == "bm25":
            scored = self._bm25_ranking(query, idxs)
        elif mode == "graph":
            scored = self._graph_ranking(query_elements, idxs)
        else:  # hybrid：query_elements 有就三路融合，没有就退回两路（向后兼容）
            dense_ranking = self._dense_ranking(query, idxs, min_score)
            bm25_ranking = self._bm25_ranking(query, idxs)
            dense_scores = dict(dense_ranking)
            rankings = [[i for i, _ in dense_ranking], [i for i, _ in bm25_ranking]]
            if query_elements:
                rankings.append([i for i, _ in self._graph_ranking(query_elements, idxs)])
            fused = _rrf_fuse(rankings)
            # 展示分优先用稠密相似度（min_score 过滤剩下的那些才有）；一个案子
            # 只在 BM25/graph 那一路进了排名、稠密分被 min_score 过滤掉了，就没有
            # 真实稠密相似度可展示，回退到 0.0——这种案子本来就是"关键词/证素
            # 命中但语义上不够像"，展示分低是符合直觉的，不是 bug。
            scored = [(i, dense_scores.get(i, 0.0)) for i, _ in fused]

        return [(self._cases[i], score) for i, score in scored[:k]]
