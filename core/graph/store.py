"""知识图谱存储层：`GraphStore` 接口 + `NetworkXStore` 实现。跟 core/llm.py 的
LLMBackend 是同一个模式——调用方只依赖接口，实现可以替换。

判据：换存储后端时，新增一个实现 `GraphStore` 的类即可，调用方不用改。
图谱规模是几千节点，内存中的 networkx 图足够，不引入独立的图数据库服务。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from core.graph.schema import EDGE_TYPES, NODE_TYPES


class GraphStore(Protocol):
    def add_node(self, node_id: str, **attrs) -> None: ...
    def add_edge(self, src: str, dst: str, *, edge_key: str | None = None, **attrs) -> None: ...
    def get_node(self, node_id: str) -> dict | None: ...
    def neighbors(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]: ...
    def in_neighbors(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]: ...
    def find_nodes(self, node_type: str, **filters) -> list[str]: ...
    def save(self, path: Path) -> None: ...
    def load(self, path: Path) -> None: ...


class NetworkXStore:
    """主实现。惰性创建底层 MultiDiGraph——networkx 本身不重，但"在 import 时就
    实例化图对象"仍然违反项目"惰性初始化"的约定（docs/ARCHITECTURE.md §2），
    做法同 core/llm.py。

    用 MultiDiGraph 是因为同一对 (src, dst) 节点之间可能同时存在多种关系
    （比如一个 case 节点既 evidences 一个 syndrome，理论上也可能有别的关系）。
    add_edge 默认用 edge_type 当 multi-edge 的 key：同一个 (src, dst, edge_type)
    三元组重复调用 add_edge 是"更新这条边的属性"而不是"新增一条重复边"，
    不需要先查后改。需要在同一对节点间保留多条同类型边时（例如 indicates 边
    按证候各留一条），传 edge_key 显式区分。
    """

    def __init__(self) -> None:
        self._g = None

    @property
    def g(self):
        if self._g is None:
            import networkx as nx

            self._g = nx.MultiDiGraph()
        return self._g

    def add_node(self, node_id: str, **attrs) -> None:
        # 节点/边类型的词表在 core/graph/schema.py，这里是唯一的落地检查点：
        # 建图脚本手误写成 "Symptom" 会在建图时炸，而不是等到工具层查不到节点。
        node_type = attrs.get("node_type")
        if node_type is not None and node_type not in NODE_TYPES:
            raise ValueError(f"未知的 node_type {node_type!r}，合法值：{sorted(NODE_TYPES)}")
        self.g.add_node(node_id, **attrs)

    def add_edge(self, src: str, dst: str, *, edge_key: str | None = None, **attrs) -> None:
        """edge_key 显式指定 multi-edge 的 key，不传则用 edge_type（默认语义见类文档）。

        什么时候必须传：同一对 (src, dst) 之间存在多条同类型、但来源不同的边。
        indicates 就是这种——「纳呆 提示 胃」这件事 SP-02/SP-03/SP-05 三条证候
        各说了一次，是三条独立的出处，不是同一条边被写了三遍。不区分 key 的话
        后写的会静默盖掉先写的，via_syndrome 和 is_cardinal 一起丢，主症/次症
        标注也可能被改写。丢的还恰好是跨证候共现的症状——那正是辨别证候时
        最有信息量的一批。
        """
        edge_type = attrs.get("edge_type")
        if edge_type is not None and edge_type not in EDGE_TYPES:
            raise ValueError(f"未知的 edge_type {edge_type!r}，合法值：{sorted(EDGE_TYPES)}")
        self.g.add_edge(src, dst, key=edge_key or edge_type, **attrs)

    def get_node(self, node_id: str) -> dict | None:
        if node_id not in self.g:
            return None
        return dict(self.g.nodes[node_id])

    def neighbors(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]:
        if node_id not in self.g:
            return []
        out = []
        for _, dst, data in self.g.out_edges(node_id, data=True):
            if edge_type is not None and data.get("edge_type") != edge_type:
                continue
            out.append((dst, dict(data)))
        return out

    def in_neighbors(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]:
        """反向邻居。图里的边都是单向的（symptom->element->syndrome），只有出边
        的话"这个证候由哪些证素构成""这个证素被哪些症状提示"这两类问题就查不了——
        core/tools.py 的 query_graph 工具要回答的正是它们。放进 GraphStore 协议
        而不是让调用方直接摸 NetworkXStore._g：绕过协议就等于把存储实现焊死在
        业务代码里。"""
        if node_id not in self.g:
            return []
        out = []
        for src, _, data in self.g.in_edges(node_id, data=True):
            if edge_type is not None and data.get("edge_type") != edge_type:
                continue
            out.append((src, dict(data)))
        return out

    def find_nodes(self, node_type: str, **filters) -> list[str]:
        result = []
        for node_id, data in self.g.nodes(data=True):
            if data.get("node_type") != node_type:
                continue
            if all(data.get(k) == v for k, v in filters.items()):
                result.append(node_id)
        return result

    # node_link_data 默认用 "source"/"target" 当边端点的结构字段名——跟我们自己
    # 每条边都有的 source 属性（provenance：gb_standard/case/textbook 等）撞名。
    # 不改用别的字段名的话，save() 时我们自己的 source 属性会被端点 id 静默覆盖，
    # load() 回来后这条边的 provenance 直接丢失（边上完全没有 source 键）——
    # 而 provenance 正是这个项目防幻觉设计要追的东西，丢了不能算无关小事。
    # 用 _node_src/_node_dst 当结构字段名，把 "source" 让给我们自己的属性。
    _LINK_SOURCE_KEY = "_node_src"
    _LINK_TARGET_KEY = "_node_dst"

    def save(self, path: Path) -> None:
        import networkx as nx

        data = nx.node_link_data(
            self.g, edges="edges", source=self._LINK_SOURCE_KEY, target=self._LINK_TARGET_KEY
        )
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, path: Path) -> None:
        import networkx as nx

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self._g = nx.node_link_graph(
            data,
            edges="edges",
            multigraph=True,
            directed=True,
            source=self._LINK_SOURCE_KEY,
            target=self._LINK_TARGET_KEY,
        )
