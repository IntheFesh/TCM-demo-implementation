"""K3b：证素路检索。dense（语义相似度）和 bm25（关键词重合，K3a）都是文本层面
的信号——两条医案文字表述完全不同（一个写"胃脘胀痛"，另一个写"脘腹痞满"），
即使证素完全一致，这两路都可能给不出高分。graph 这一路走的是结构化信号：
给定这次问诊 S2 已经推断出的证素（query_elements），按"这条医案连到多少个
同样的证素"给案例打分，跟两条医案怎么措辞无关。

data/element_index.json（offline/build_element_index.py 的产物）是「case_id
-> 它连到哪些证素」的离线索引，直接从 cases.json 的 symptoms 字段建，见那份
脚本的模块文档字符串（不依赖 X3 的自由格式三元组，是刻意的设计选择）。

打分复用 core.setstats.jaccard_distance——医案证素集合和 query_elements
的 Jaccard 相似度，跟 core/chain.py 的 herb_jaccard、core/setstats.py 的
pairwise_jaccard_stats 是同一个"两个集合有多像"的问题、同一个函数，
不是又写一套（三次同类实现里第二次真正复用了这条 CLAUDE.md 的约束）。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.setstats import jaccard_distance

ELEMENT_INDEX_PATH = Path(__file__).resolve().parent.parent / "data" / "element_index.json"


class ElementRetriever:
    """惰性单例的伴生对象，不在 HybridRetriever 里内联——按"将来换实现时，
    改的是一个类还是整个模块"这条判据，证素索引的打分方式要换（比如换成按
    证素权重加权而不是纯 Jaccard），改这一个类就够，不用碰 HybridRetriever
    的调度逻辑。"""

    def __init__(self, index_path: Path = ELEMENT_INDEX_PATH):
        if not index_path.exists():
            raise FileNotFoundError(
                f"未找到 {index_path}。请先运行 `python -m offline.build_element_index` "
                "生成证素索引，再使用 graph 检索模式。"
            )
        self._index: dict = json.loads(index_path.read_text(encoding="utf-8"))

    def ranking(
        self, query_elements: list[str], case_ids: list[str]
    ) -> list[tuple[str, float]]:
        """按 Jaccard 相似度降序返回 (case_id, 相似度)，相似度为 0（无共同证素，
        或该医案一个证素都没索引到）的不返回——没有共同证素不构成"这条医案
        跟这次证素推断有关系"的证据，塞进结果只会稀释真正相关的案子。"""
        query_set = set(query_elements)
        scored = []
        for cid in case_ids:
            entry = self._index.get(cid)
            if not entry:
                continue
            case_elements = set(entry.get("elements") or [])
            if not case_elements:
                continue
            similarity = 1.0 - jaccard_distance(query_set, case_elements)
            if similarity > 0:
                scored.append((cid, similarity))
        scored.sort(key=lambda x: -x[1])
        return scored
