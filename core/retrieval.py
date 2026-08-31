"""医案检索层：给定患者症状，在某位医家的医案库里检索最相关的参考医案。"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path

from core.schemas import CaseRecord

CASES_PATH = Path(__file__).resolve().parent.parent / "cases.json"


class Retriever(ABC):
    @abstractmethod
    def search(
        self, query: str, physician: str, k: int = 3
    ) -> list[tuple[CaseRecord, float]]:
        """返回 [(医案, 相似度), ...]，按相似度降序，只在给定医家的医案里排序。"""
        raise NotImplementedError


def _case_to_text(case: CaseRecord) -> str:
    """把结构化医案编码成一段紧凑文本用于向量化。"""
    symptoms = "；".join(case.symptoms) if case.symptoms else "（无记录症状）"
    tongue = case.tongue or "未记"
    pulse = case.pulse or "未记"
    return f"{symptoms}。舌{tongue}，脉{pulse}"


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

    def _ensure_encoded(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
        texts = [_case_to_text(c) for c in self._cases]
        self._embeddings = self._model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True
        )

    def search(
        self, query: str, physician: str, k: int = 3
    ) -> list[tuple[CaseRecord, float]]:
        self._ensure_encoded()

        idxs = [i for i, c in enumerate(self._cases) if c.physician == physician]
        if not idxs:
            return []

        query_vec = self._model.encode(
            [query], normalize_embeddings=True, convert_to_numpy=True
        )[0]

        scored = [
            (i, float(self._embeddings[i] @ query_vec)) for i in idxs
        ]
        scored.sort(key=lambda x: -x[1])
        top = scored[:k]
        return [(self._cases[i], score) for i, score in top]


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
