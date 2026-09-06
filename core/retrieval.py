"""医案检索层：给定患者症状，在某位医家的医案库里检索最相关的参考医案。"""
from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from core.schemas import CaseRecord

CASES_PATH = Path(__file__).resolve().parent.parent / "cases.json"


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
        if self._model is not None:
            return
        with self._encode_lock:
            if self._model is not None:
                return
            self._load()

    def _load(self) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        texts = [_case_to_text(c) for c in self._cases]
        self._embeddings = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True
        )

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


def get_retriever() -> Retriever:
    """惰性单例。"""
    global _retriever_singleton
    if _retriever_singleton is None:
        _retriever_singleton = DenseRetriever()
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
