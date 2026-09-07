"""K3b：从 cases.json 的 symptoms 字段建 data/element_index.json——「这条医案
连到哪些证素」的离线索引，给 core/retrieval_graph.py 的 ElementRetriever 用。

**不依赖 data/case_triples.jsonl（X3 产出）。** X3 的三元组是自由格式的自然
语言关系（谓词不限定枚举，见 prompts/v1/s5_extract_triples.yaml），要从里面
可靠地识别出"这条是不是一次症状表现"需要再猜一层，猜错了这份索引就带着
噪声。cases.json 的 symptoms 字段本身就是 S0 阶段已经抽干净的症状列表，
直接能用，没必要绕经 X3 再猜一遍。

症状->证素的匹配复用 core.tools._match_graph_symptoms（患者原话/医案症状
片段级双向包含匹配国标症状节点，再走 indicates 边到证素）——这是
core/tools.py 的 check_residual 已经在用的同一套匹配器，不新写一套字面匹配
（CLAUDE.md：这个项目已经在这堵墙上撞过三次）。

用法：
    python -m offline.build_element_index
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.graph.store import NetworkXStore
from core.schemas import CaseRecord
from core.tools import _match_graph_symptoms

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_GRAPH_PATH = ROOT / "data" / "graph.json"
DEFAULT_OUT_PATH = ROOT / "data" / "element_index.json"


def case_elements(store: NetworkXStore, symptoms: list[str]) -> list[str]:
    """一条医案的症状列表 -> 它连到的证素名集合（去掉 element:: 前缀）。
    跟 core.tools.check_residual 走的是同一条链路：症状 -> 匹配到的国标症状
    节点 -> indicates 边 -> 证素节点。"""
    elements: set[str] = set()
    for s in symptoms:
        matched = _match_graph_symptoms(store, s)
        for sym_id in matched:
            for dst, _data in store.neighbors(sym_id, edge_type="indicates"):
                elements.add(dst.removeprefix("element::"))
    return sorted(elements)


def build_index(cases: list[CaseRecord], store: NetworkXStore) -> dict:
    index = {}
    for c in cases:
        index[c.case_id] = {
            "physician": c.physician,
            "elements": case_elements(store, c.symptoms),
        }
    return index


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="K3b：从 cases.json 建证素索引")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--graph-path", type=Path, default=DEFAULT_GRAPH_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.cases_path}。先跑 offline/extract_cases.py 生成 cases.json。"
        )
    if not args.graph_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.graph_path}。先跑 offline/build_graph.py 建国标层图谱——"
            "证素索引靠图里的 symptom -indicates-> element 边做匹配。"
        )

    cases = [
        CaseRecord.model_validate(r)
        for r in json.loads(args.cases_path.read_text(encoding="utf-8"))
    ]
    store = NetworkXStore()
    store.load(args.graph_path)

    index = build_index(cases, store)

    n_with_elements = sum(1 for v in index.values() if v["elements"])
    print(f"读入 {len(cases)} 条医案，{n_with_elements} 条匹配到至少一个证素，"
          f"{len(cases) - n_with_elements} 条一个证素都没匹配到（症状表述在国标层图里找不到对应节点）")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
