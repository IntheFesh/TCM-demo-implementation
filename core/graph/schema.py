"""知识图谱的节点/边类型词表。core/graph/store.py 的 add_node/add_edge 对着它
校验 node_type / edge_type——这是它唯一的消费方，也是"symptom"和"Symptom"这种
手误唯一能被拦住的地方（建图脚本、weights.py、tools.py 里写的都是字面量，
之前这张表谁也不读，纯装饰）。不做更多：不是类型系统。

K1 目前只填充 symptom / element / syndrome 三类节点和 indicates / composes / is_a
三类边（见 offline/build_graph.py）。therapy / formula / herb / case / physician
节点和 treated_by / realized_by / contains / evidences / practiced_by 边留给
K2 及以后——它们的数据来源是医案和治法国标，不是 K1 用的证候定义。
"""

NODE_TYPES = {
    "symptom": "症状",
    "element": "证素",
    "syndrome": "证候",
    "therapy": "治法",
    "formula": "方剂",
    "herb": "药物",
    "case": "医案",
    "physician": "医家",
}

EDGE_TYPES = {
    "indicates": "symptom -> element  症状提示证素",
    "composes": "element -> syndrome  证素构成证候",
    "is_a": "syndrome -> syndrome  证候的类目层级",
    "treated_by": "syndrome -> therapy  证候对应治法",
    "realized_by": "therapy -> formula  治法对应方剂",
    "contains": "formula -> herb  方含药",
    "evidences": "case -> syndrome  医案作为证据",
    "practiced_by": "case -> physician  医案属于医家",
}

# 边的 source（出处）属性刻意**不**在这里列词表：data/graph.json 里实际出现的是
# official_consensus / secondary_verified / journal / group_standard / manual，
# 医案层挂上去之后还有 case——它是数据标注的自由文本，不是封闭枚举。之前这里
# 写着 ("standard", "case", "textbook")，跟数据对不上，谁也没读它。
