"""知识图谱存储层：接口 + 两个实现。跟 core/llm.py 的 LLMBackend/OpenAICompatBackend/
VLLMBackend 是同一个模式——接口写好、主实现落地、第二实现留占位。

判据：将来要换存储后端，改的是这个文件里的一个类，还是整个模块重写？必须是前者。
demo 规模约 2000 节点，NetworkXStore 足够；Neo4j 只增加运维负担、不增加能力，
所以只写占位，不接依赖。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class GraphStore(Protocol):
    def add_node(self, node_id: str, **attrs) -> None: ...
    def add_edge(self, src: str, dst: str, **attrs) -> None: ...
    def get_node(self, node_id: str) -> dict | None: ...
    def neighbors(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]: ...
    def find_nodes(self, node_type: str, **filters) -> list[str]: ...
    def save(self, path: Path) -> None: ...
    def load(self, path: Path) -> None: ...


class NetworkXStore:
    """主实现。惰性创建底层 MultiDiGraph——networkx 本身不重，但"在 import 时就
    实例化图对象"仍然违反项目"惰性初始化"的约定，做法照抄 core/llm.py 的模式。

    用 MultiDiGraph 是因为同一对 (src, dst) 节点之间可能同时存在多种关系
    （比如一个 case 节点既 evidences 一个 syndrome，理论上也可能有别的关系）。
    add_edge 用 edge_type 当 multi-edge 的 key：同一个 (src, dst, edge_type)
    三元组重复调用 add_edge 是"更新这条边的属性"而不是"新增一条重复边"——
    K2 给 indicates 边加 weight_by_physician 就是靠这个语义实现的，不是靠
    先查后改。
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
        self.g.add_node(node_id, **attrs)

    def add_edge(self, src: str, dst: str, **attrs) -> None:
        edge_type = attrs.get("edge_type")
        self.g.add_edge(src, dst, key=edge_type, **attrs)

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

    def find_nodes(self, node_type: str, **filters) -> list[str]:
        result = []
        for node_id, data in self.g.nodes(data=True):
            if data.get("node_type") != node_type:
                continue
            if all(data.get(k) == v for k, v in filters.items()):
                result.append(node_id)
        return result

    def save(self, path: Path) -> None:
        import networkx as nx

        data = nx.node_link_data(self.g, edges="edges")
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self, path: Path) -> None:
        import networkx as nx

        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self._g = nx.node_link_graph(data, edges="edges", multigraph=True, directed=True)


class Neo4jStore:
    """占位实现，未测试。正式阶段本地/云端部署 Neo4j 时启用，需核对当时的
    neo4j 驱动 API（官方 Python 驱动的事务写法、connect 参数这几年变过）。

    demo/完整版都不接这个类——2000 节点规模 NetworkXStore 完全够用，接 Neo4j
    只会多一个要运维的服务。写出来只是为了把 GraphStore 接口的"可替换性"
    坐实：真要换存储后端时，业务代码不用动，只需要把这个类的方法体填上。
    """

    def add_node(self, node_id: str, **attrs) -> None:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")

    def add_edge(self, src: str, dst: str, **attrs) -> None:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")

    def get_node(self, node_id: str) -> dict | None:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")

    def neighbors(self, node_id: str, edge_type: str | None = None) -> list[tuple[str, dict]]:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")

    def find_nodes(self, node_type: str, **filters) -> list[str]:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")

    def save(self, path: Path) -> None:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")

    def load(self, path: Path) -> None:
        raise NotImplementedError("Neo4jStore 未实现，正式阶段部署 Neo4j 时补全")
