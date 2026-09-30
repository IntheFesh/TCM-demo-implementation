"""混合检索：在 DenseRetriever 的稠密向量检索之外叠加 BM25 关键词检索和证素路
（graph）检索，用 Reciprocal Rank Fusion（RRF）融合排名。同一个入口还提供
默认的 `full_context` 模式：不检索，把该医家的全部医案交给缓存前缀。

为什么是 RRF 而不是加权求和：稠密分是归一化余弦相似度，落在 [0,1]；BM25 分
是无界的、还随语料规模变化（idf 项）。两者要加权求和，必须先把 BM25 分数
归一化到可比尺度——而"怎么归一化"本身就是一个没有验证过的超参数，等于凭空
引入一个新的、没有基准的自由度。RRF 只用排名（第几名），不看分数绝对值，
不需要这一步，天然规避了这个问题。RRF_K=60 是原论文（Cormack et al. 2009）
的经验值，这里没有针对本项目重新调过——不过度设计，用文献默认值，不为一个
未经验证的收益去引入调参。

jieba 分词必须加载自定义词典（`offline/build_jieba_dict.py` 的产物），否则
中医术语会被切碎，BM25 的关键词匹配等于失效。例如词典只来自 ELEMENTS +
SYNONYMS 两个来源（没有 cases.json 时就是这样）：
    >>> list(jieba.cut("癥瘕"))
    ['癥', '瘕']          # 未加词典：拆成两个字
    >>> jieba.load_userdict("data/jieba_dict.txt")
    >>> list(jieba.cut("癥瘕"))
    ['癥瘕']              # 加词典后：识别成一个词
拆开后 BM25 用词袋模型算分时，"癥瘕"作为一个整体术语的匹配信号就丢了；
加载词典后才能被当成一个词正确命中。有 cases.json 时（`offline/extract_cases.py`
生成）词典还会并入真实医案里的高频症状表述，覆盖面更大，机制是一样的——
不需要 cases.json 也能验证这条设计成立。

**graph 一路走的是结构化信号，不是文本信号。** 给定这次问诊 S2 已经推断出的
证素（`query_elements`），按"这条医案连到多少个同样的证素"打分——两条医案
文字表述完全不同，只要底层证素一致，这条路能把它们连起来，dense/bm25 都
做不到。具体打分逻辑在 core/retrieval_graph.py（ElementRetriever），这里只管
调度。`mode="graph"` 要求调用方显式传 `query_elements`，不传就报错，不会静默
退化成别的模式——"我请求了 graph 检索，结果却是别的检索"这种静默降级比报错
更危险，调用方会以为拿到的是证素路的结果。`mode="hybrid"` 则相反：传了
`query_elements` 就三路融合，不传就两路融合（dense+bm25）——不强制调用方在
证素算出来之前就提供它，多一路信号是增益，不提供不算错。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from core.parallel import run_routes
from core.retrieval import CASES_PATH, DenseRetriever, full_context_hits
from core.retrieval_graph import ElementRetriever
from core.schemas import CaseRecord

JIEBA_DICT_PATH = Path(__file__).resolve().parent.parent / "data" / "jieba_dict.txt"
# 见 _ensure_jieba：守的是 jieba 的全局词典，不是某个实例
_JIEBA_GLOBAL_LOCK = threading.Lock()

# RRF 的经验常数，见模块文档字符串。
RRF_K = 60

# 检索模式分两个系：默认的 `full_context`（不检索，把该医家的全部医案交给缓存
# 前缀），以及每次只取少量参考医案的 top3 系（dense / bm25 / graph / hybrid）。
# 默认是 full_context 的理由：top-3 检索等于把模型服务的前缀缓存这个最大的杠杆
# 扔掉了（docs/DESIGN_NOTES.md §4，core/context_prefix.py 的模块文档字符串）。
ALLOWED_MODES = {"dense", "bm25", "graph", "hybrid", "full_context"}
#: top3 系：每次只取 k（默认 3）条参考医案的四种模式，E8（检索模式对比）就在这
#: 四种之间比较输出差异；full_context 看的是全量而不是前几条，不在其中。
TOP3_MODES = frozenset({"dense", "bm25", "graph", "hybrid"})
DEFAULT_MODE = "full_context"
#: 读这个模式的环境变量名。**只有本模块读它**（core/chain.py 那条源码级测试
#: 钉住了 chain 不许碰它）；别处要在消息里提它的名字就 import 这个常量，
#: 不要再写一遍字面量。
RETRIEVER_MODE_ENV = "RETRIEVER_MODE"


def effective_mode(mode: str | None = None) -> str:
    """这次实际用哪个模式。**只此一处解析默认值**——`core/chain.py` 要知道
    "这次是不是 full_context"来决定 prompt 怎么拼，它不该自己再读一遍
    RETRIEVER_MODE 环境变量（两处各读一遍，改了默认值就会有一处忘了改）。
    """
    return mode or os.environ.get(RETRIEVER_MODE_ENV, DEFAULT_MODE)


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


# ---------------------------------------------------------------------------
# bm25 保底名额：bm25 排名前 BM25_FLOOR_N 的条目一定进入 hybrid 的最终结果。
#
# 为什么需要它：中医术语的精确匹配是 dense 的短板（docs/DESIGN.md §4.2），
# 一条医案可能只有 bm25 认得出来——例如主诉里的「情志不畅」，全库唯一精确命中
# "情志诱因"的医案在 bm25 里名次靠前、在 dense 里名次靠后。融合之后不设单路阈值
# （见 search() 的 hybrid 分支）解决的是"这条医案进不进候选池"；进了候选池之后
# RRF 仍可能把它排到 top-k 之外，这是另一个问题，由保底名额解决。
#
# 三个办法用同一组名次比较（tests/test_retrieval_hybrid.py 的
# test_bm25_floor_rescues_target_that_rrf_ranks_below_top_n 构造的就是这组数：
# 目标 dense#100/bm25#2，对手 A dense#1/bm25#50，B dense#2/bm25#60，
# C dense#3/bm25#80）：
#
# 办法 A（调小 RRF_K）：解不了，跟 K 取多少无关。目标跟对手 A 的融合分差
#     f(K) = [1/(K+100) - 1/(K+50)] + [1/(K+2) - 1/(K+1)]
#          = -50/[(K+100)(K+50)] - 1/[(K+2)(K+1)]
# 两项对任意 K>0 都恒为负——dense 排名 1 对 100 名的位置优势，比 bm25
# 排名 2 对 50 名的优势大得多，这是排名差距本身的问题，不是 K 没调对。
#
# 办法 B（加权 RRF，w_dense/(K+r_dense) + w_bm25/(K+r_bm25)）：数学上可行——
# K=60、w_dense=1.0 时，w_bm25≈1.44 是压过对手 A 的门槛，w_bm25=2.0 时
# 目标反超全部三个对手（0.0385 对 0.0346/0.0328/0.0302）。但这个权重要靠
# "dense 对中医术语的区分度有多低"这类测量来校准，没有依据地定一个权重等于
# 引入一个新的自由度，而且它会改变所有查询的排序，所以不选。
#
# 办法 C（bm25 top-N 强制保底，采用）：不用猜数值，N 直接从排名推出来——
# 这组名次里 bm25 第 1 名是另一条医案，目标排第 2：N=1 时保底集合只有第 1 名，
# 目标进不去；N=2 时保底集合含目标，能进最终结果。它只影响被挤掉的那几条，
# 其余查询的排序不变。scripts/verify_hybrid_fusion.py 打印目标医案在各路检索中
# 的名次和融合分分解，可以在真实语料上核对某条医案是被谁挤下去的。
#
# 为什么不会让 hybrid 退化成"就是 bm25"（E8 里 hybrid 与 bm25 要有区别）：
# 保底只保证 bm25 排名前 floor_n 的条目一定在最终结果里，floor_n 会被夹到
# min(N, k-1)——生产环境 k=3、N=2 时留了 1 个名额纯给 RRF 融合排名决定，
# dense/graph 信号仍然能在那个名额上顶掉 bm25 保底之外的条目；就算 k 小到只剩
# N 个名额，保底集合内部的相对顺序、以及展示给用户的分数，仍然是 RRF 融合分/
# 真实 dense 相似度，不是裸的 bm25 分——跟纯 bm25 模式返回的是同一组 case_id
# 但排序依据和展示语义都不同。见 tests/test_retrieval_hybrid.py::
# test_hybrid_floor_leaves_room_for_pure_rrf_when_k_is_small。
BM25_FLOOR_N = 2


def _apply_bm25_floor(
    fused: list[tuple[int, float]],
    bm25_ranking: list[tuple[int, float]],
    floor_n: int,
) -> list[tuple[int, float]]:
    """把 bm25 排名前 floor_n 的条目提到 fused 列表最前面，组内仍按 fused
    分降序（不按 bm25 分重排）——保证它们截到 k 之后不会被 RRF 挤掉，
    同时"最靠前的是最可信的"这条语义不因为保底而变。纯函数，不依赖检索器
    状态，跟 _rrf_fuse 同样的理由方便单测。"""
    if floor_n <= 0:
        return fused
    guaranteed = {i for i, _ in bm25_ranking[:floor_n]}
    head = [pair for pair in fused if pair[0] in guaranteed]
    tail = [pair for pair in fused if pair[0] not in guaranteed]
    return head + tail


class HybridRetriever(DenseRetriever):
    """继承 DenseRetriever 复用稠密检索那一路（_ensure_encoded/_embeddings），
    新增 BM25 一路和 RRF 融合。mode 由调用方传入，缺省读 RETRIEVER_MODE 环境
    变量（再缺省用 DEFAULT_MODE）——这样 E8 消融只需要改环境变量重跑，不需要
    换检索器实例。"""

    def __init__(self, cases_path: Path = CASES_PATH):
        super().__init__(cases_path)
        self._bm25 = None  # 惰性构建，避免 import/构造阶段做重操作
        self._bm25_lock = threading.Lock()
        self._jieba_ready = False
        self._jieba_lock = threading.Lock()
        self._element_retriever: ElementRetriever | None = None
        self._element_retriever_lock = threading.Lock()
        # case_id -> 下标，供 graph 一路把 ElementRetriever 返回的 case_id 换回
        # 跟 dense/bm25 同一套下标体系去融合。这只是把医案列表遍历一遍，不是
        # "加载模型/大文件"，不需要惰性。
        self._case_id_to_idx = {c.case_id: i for i, c in enumerate(self._cases)}

    def _ensure_jieba(self) -> None:
        if self._jieba_ready:
            return
        with self._jieba_lock:
            if self._jieba_ready:
                return
            import jieba

            # jieba 的词典 trie 是进程级全局的，load_userdict 改的是它，不是
            # 这个实例的东西。_jieba_ready/_jieba_lock 按实例记是为了让测试能
            # 换词典路径重建实例，但真正写全局态的那一步要用模块级的锁排队——
            # 否则两个实例（测试里常见；服务里靠 get_retriever 的锁保证只有一个）
            # 同时 load_userdict 会一起改同一棵 trie。
            with _JIEBA_GLOBAL_LOCK:
                if JIEBA_DICT_PATH.exists():
                    # **自己开文件、自己关。** jieba 的 load_userdict 收到路径字符串时
                    # 会自己 open 但不 close，留一个悬空的文件描述符（Python 的
                    # ResourceWarning 默认被忽略，所以这个泄漏不容易被看见）。它也
                    # 接受 file-like，那就用 with 把生命周期拿回来。
                    with JIEBA_DICT_PATH.open("rb") as fh:
                        jieba.load_userdict(fh)
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
            # 复用 DenseRetriever.__init__ 已经算好、过滤过的 self._case_texts，
            # 不重新调用 _case_to_text——两处各算一遍不仅重复，一旦逻辑漂移还会
            # 让 BM25 语料和 dense 那一路对同一条医案编码出不同文本，语义上应该
            # 是同一件事却分叉成两份实现。
            corpus = [self._tokenize(t) for t in self._case_texts]
            self._bm25 = BM25Okapi(corpus)

    def _dense_ranking(
        self, query: str, idxs: list[int], min_score: float
    ) -> list[tuple[int, float]]:
        """按稠密相似度降序返回 (下标, 真实余弦相似度) 全排名（不截断到 k）。
        min_score 只作用于这一路——BM25 的分数不在同一尺度上，套用同一个阈值
        没有意义。返回的分是未经初诊加成/无方剂惩罚的真实相似度，那些调整
        只用于排序（DenseRetriever._rank_score，两处共用同一份公式）。"""
        self._ensure_encoded()
        query_vec = self._model.encode(
            [query], normalize_embeddings=True, convert_to_numpy=True
        )[0]
        scored = []
        for i in idxs:
            raw_score = float(self._embeddings[i] @ query_vec)
            if raw_score < min_score:
                continue
            rank_score = self._rank_score(self._cases[i], raw_score)
            scored.append((i, rank_score, raw_score))
        scored.sort(key=lambda x: -x[1])
        return [(i, raw) for i, _rank, raw in scored]

    def _bm25_ranking(self, query: str, idxs: list[int]) -> list[tuple[int, float]]:
        """按 BM25 分降序返回 (下标, BM25 分) 全排名。不做 min_score 过滤：
        BM25 分数在不同查询之间不可比，也不在 min_score 所针对的稠密余弦
        相似度那个刻度上（docs/DESIGN_NOTES.md §4）。"""
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
        min_score 过滤——0.70 是按稠密相似度的分布校准的阈值（见
        core/retrieval.py 的 MIN_RETRIEVAL_SCORE 注释），而证素集合的元素个数
        有限，Jaccard 的取值分布跟稠密余弦相似度不是一回事：有实际意义的重叠
        也常常落在 0.70 以下，套用为稠密分校准的阈值会把 graph 这条信号基本上
        过滤没。"""
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
        mode = effective_mode(mode)
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

        # 展示分按模式取：dense/hybrid 展示真实余弦相似度（hybrid 本来就要算稠密
        # 相似度去融合），graph 展示真实 Jaccard 相似度，两者都是 [0,1] 有界，
        # 前端/prompt 里"相似度"这个词才有意义；bm25 模式展示 BM25 原始分——
        # 不强行套一个没参与排序的稠密分，否则 bm25-only 就必须为了"好看的展示
        # 数字"去多算一次稠密编码，白白引入这条路径本不需要的模型依赖
        # （bm25 模式应该能在没有 embedding 模型的环境里独立跑，见
        # tests/test_retrieval_hybrid.py）。
        if mode == "full_context":
            # 不排序不筛选，直接交出全部——走模块级 full_context_hits（一处实现），
            # 不在这里另写一遍排序：顺序不同 = 缓存前缀 byte 不同 = 永不命中。
            return full_context_hits(self._cases, physician)
        if mode == "dense":
            scored = self._dense_ranking(query, idxs, min_score)
        elif mode == "bm25":
            scored = self._bm25_ranking(query, idxs)
        elif mode == "graph":
            scored = self._graph_ranking(query_elements, idxs)
        else:  # hybrid：query_elements 有就三路融合，没有就两路（dense + bm25）
            # 融合之后不对结果做任何单路阈值过滤：min_score 在 hybrid 模式下
            # 不影响准入（只有 dense 模式用它过滤，见 adaptive_min_score 的适用
            # 范围说明）。RRF 本身就是质量筛选机制——它的前提是"多路都认可的
            # 条目排名靠前"；混合检索存在的全部理由就是"对中医术语的精确匹配
            # 能力是 dense 缺的"（模块文档字符串）。如果融合之后还要求每条结果的
            # dense 相似度 ≥ min_score，dense 就对 BM25 的发现拥有了否决权：
            # BM25 单独找到、dense 分不够的条目，无论 RRF 把它排多靠前都会被
            # 滤掉，混合检索等于取消。所以 dense 那一路在融合阶段也不过滤
            # （下面 _dense_ranking 的 min_score=0.0）。
            #
            # 展示分仍然是 dense 相似度——dense 分低的条目照常返回、展示它真实的
            # 低分，不把它藏起来。前端和 E3 报告能看到"这条是 BM25 找到的、dense
            # 分很低"，这比让它悄悄消失或悄悄显示成误导性的 0.0 更诚实。
            #
            # 三路**并行**算：三路之间没有依赖（各自从 idxs 独立打分），串行跑
            # 没有任何收益。稠密路是 numpy 的矩阵乘——numpy 在算的时候放开 GIL，
            # 所以 BM25（纯 Python）能真的跟它重叠；证素路是集合运算，很短。
            #
            # **`rankings` 的顺序必须固定**：它的顺序进 RRF，换了顺序融合结果
            # 就变，那不是"更快"而是"不一样"。所以按固定的键取回，不按
            # `as_completed` 的到达顺序。
            routes = [("dense", lambda: self._dense_ranking(query, idxs, min_score=0.0)),
                      ("bm25", lambda: self._bm25_ranking(query, idxs))]
            if query_elements:
                routes.append(("graph", lambda: self._graph_ranking(query_elements, idxs)))
            done = run_routes(routes, thread_name_prefix="retrieve")
            dense_ranking = done["dense"]
            bm25_ranking = done["bm25"]
            dense_scores = dict(dense_ranking)
            rankings = [[i for i, _ in dense_ranking], [i for i, _ in bm25_ranking]]
            if query_elements:
                rankings.append([i for i, _ in done["graph"]])
            fused = _rrf_fuse(rankings)
            # bm25 保底：见 BM25_FLOOR_N 上面那段注释。floor_n 夹到
            # max(0, k-1)——k 很小时也要留至少 1 个名额给纯 RRF 排名，不然
            # 保底会把 hybrid 的返回集合挤成跟 bm25 的 top-N 完全一样。
            floor_n = min(BM25_FLOOR_N, max(0, k - 1))
            fused = _apply_bm25_floor(fused, bm25_ranking, floor_n)
            # dense_ranking 覆盖了 idxs 里的全部条目（min_score=0.0，不过滤），
            # .get(i, 0.0) 这个兜底理论上不会触发——保留它只是防御性写法
            # （万一某条医案不在 idxs 里却混进了 fused，那是别的 bug，不该
            # 在这里静默吞掉，但也不该在这里崩）。
            scored = [(i, dense_scores.get(i, 0.0)) for i, _ in fused]

        return [(self._cases[i], score) for i, score in scored[:k]]
