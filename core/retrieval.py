"""医案检索层：给定患者症状，在某位医家的医案库里检索最相关的参考医案。"""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from core.schemas import CaseRecord

CASES_PATH = Path(__file__).resolve().parent.parent / "cases.json"

# 检索相似度下限。实测正常匹配在 0.85-0.90，低于 0.70 基本是"库里没有相关案子"，
# 此时给空列表比塞三条不相关的更诚实。定义在这里而不是 chain.py：ReAct 的
# search_cases 工具也要用同一个阈值，而 tools 不能反向 import chain（循环）。
MIN_RETRIEVAL_SCORE = 0.70


class Retriever(ABC):
    @abstractmethod
    def search(
        self, query: str, physician: str, k: int = 3, min_score: float = 0.0
    ) -> list[tuple[CaseRecord, float]]:
        """返回 [(医案, 相似度), ...]，按相似度降序，只在给定医家的医案里排序。
        min_score 以下的结果不返回——宁可给空列表让 S3 知道"没有相关医案"，
        也不要塞三条不相关的案子进 prompt 逼模型模仿。"""
        raise NotImplementedError


def _case_to_text(case: CaseRecord) -> str:
    """把结构化医案编码成一段紧凑文本用于向量化。

    复诊段要跟初诊区分开：复诊原文常只写"服药后如何"，症状极简
    （"肿胀未除""汗至眉上"），如果和初诊平等编码，检索时会大量命中
    这些碎片——实测吴鞠通的 top-3 曾全是第 6/11 诊，一条初诊都没有。
    把治疗反应拼进文本，让复诊段的向量落在"疗效描述"而不是"主诉"附近。
    """
    symptoms = "；".join(case.symptoms) if case.symptoms else "（无记录症状）"
    tongue = case.tongue or "未记"
    pulse = case.pulse or "未记"
    base = f"{symptoms}。舌{tongue}，脉{pulse}"
    if case.visit_index and case.visit_index > 0:
        resp = case.response_to_prior or "（未记疗效）"
        return f"复诊第{case.visit_index + 1}诊。前次治疗后：{resp}。现症：{base}"
    return base


class DenseRetriever(Retriever):
    """用 sentence-transformers 的 bge-small-zh-v1.5 做稠密检索。惰性加载模型，
    禁止在模块顶层实例化（加载模型是重操作，不该在 import 时就发生）。"""

    def __init__(self, cases_path: Path = CASES_PATH):
        if not cases_path.exists():
            raise FileNotFoundError(
                f"未找到 {cases_path}。请先运行 `python -m offline.extract_cases` "
                "生成 cases.json，再使用检索功能。"
            )
        with cases_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        self._cases: list[CaseRecord] = [CaseRecord.model_validate(r) for r in raw]

        self._model = None  # 惰性加载，避免 import 阶段就下载/加载模型
        self._embeddings = None  # 惰性编码，随 _model 一起初始化

    _encode_lock = threading.Lock()

    def _ensure_encoded(self) -> None:
        # 冷启动时两个并发请求会各加载一份模型（几百 MB × 2）。
        # 双重检查：锁外先判一次避免每次请求都抢锁，锁内再判一次防竞态。
        #
        # 锁外快路径看的必须是 _embeddings 而不是 _model：_load() 里加载模型
        # 只要一两秒，随后给 839 条医案编码要十几秒——这段时间里 _model 已经
        # 非 None 而 _embeddings 还是 None。审计里实测过这个交错：线程 A 持锁
        # 在编码，线程 B 锁外看到 _model 就位直接放行，走到
        # `self._embeddings[i] @ query_vec` 时拿到的是 None，TypeError。
        # 所以 _load() 最后才发布 _embeddings，这里只认它。
        if self._embeddings is not None:
            return
        with self._encode_lock:
            if self._embeddings is not None:
                return
            self._load()

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer

        # 先在局部变量里把两样东西都建好，再按 _model → _embeddings 的顺序发布。
        # _ensure_encoded 的锁外快路径只认 _embeddings，它最后一个写入，
        # 别的线程看到它非 None 时 _model 一定已经就位。
        model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        texts = [_case_to_text(c) for c in self._cases]
        embeddings = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        self._model = model
        self._embeddings = embeddings

    # 初诊在排序时的加成。复诊段症状简短、内容是疗效描述，
    # 作为"该医家如何辨证"的参考价值低于初诊，但不完全排除——
    # 长序列里的中段复诊有时正好记录了证型转变。
    INITIAL_VISIT_BOOST = 1.08

    def search(
        self, query: str, physician: str, k: int = 3, min_score: float = 0.0
    ) -> list[tuple[CaseRecord, float]]:
        self._ensure_encoded()

        idxs = [i for i, c in enumerate(self._cases) if c.physician == physician]
        if not idxs:
            return []

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
            # 排序用加权分，返回给上层的仍是真实相似度，不要把加成混进展示值
            scored.append((i, rank_score, raw_score))

        scored.sort(key=lambda x: -x[1])
        return [(self._cases[i], raw) for i, _rank, raw in scored[:k]]


_retriever_singleton: Retriever | None = None
# 建单例要读整份 cases.json 并逐条 model_validate，几百毫秒；没有这把锁时两个
# 冷启动并发请求会各建一份 HybridRetriever，输的那份被在途请求引用着、之后又
# 各自加载一份几百 MB 的模型——DenseRetriever 那把 _encode_lock 是类属性，只能
# 让两次加载排队，挡不住加载两次。
_retriever_lock = threading.Lock()


def get_retriever() -> Retriever:
    """惰性单例。返回 HybridRetriever（DenseRetriever 的超集，见
    core/retrieval_hybrid.py）——这样 RETRIEVER_MODE 环境变量能在每次
    search() 调用时动态生效，不需要按 mode 分别建单例（V1 的 E8 消融
    只改环境变量重跑，不重启进程）。放在这里而不是模块顶层 import，
    是为了避免 core.retrieval 反向依赖 core.retrieval_hybrid 造成循环
    import（retrieval_hybrid 依赖 retrieval，不能反过来在模块顶层互相依赖）。
    """
    global _retriever_singleton
    if _retriever_singleton is None:
        with _retriever_lock:
            if _retriever_singleton is None:
                from core.retrieval_hybrid import HybridRetriever

                _retriever_singleton = HybridRetriever()
    return _retriever_singleton


if __name__ == "__main__":
    queries_path = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
    first_query = queries_path.read_text(encoding="utf-8").splitlines()[0].strip()
    print(f"查询：{first_query}\n")

    retriever = get_retriever()
    for physician in ["ye_tianshi", "wu_jutong"]:
        print(f"=== {physician} top-3 ===")
        for case, score in retriever.search(first_query, physician, k=3):
            symptoms_summary = "；".join(case.symptoms[:4])
            print(f"  {case.case_id}  相似度={score:.3f}  症状摘要：{symptoms_summary}")
        print()
