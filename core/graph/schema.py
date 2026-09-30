"""知识图谱的节点/边类型词表。core/graph/store.py 的 add_node/add_edge 对着它
校验 node_type / edge_type——这是它唯一的消费方，也是"symptom"和"Symptom"这种
手误唯一能被拦住的地方（建图脚本、weights.py、tools.py 里写的都是字面量）。
不做更多：不是类型系统。

offline/build_graph.py 从证候定义写入 symptom / element / syndrome 三类节点和
indicates / composes / is_a 三类边；cases.json 存在时再挂上 case 节点与
evidences 边。therapy / formula / herb / physician 节点和 treated_by /
realized_by / contains / practiced_by 边是词表里预留的类型，它们的数据来源是
治法国标与方剂，不是证候定义。
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

# 边的 source（出处）属性刻意**不**在这里列词表：它取自证候定义的来源档次
# （textbook / official_consensus / secondary_verified / journal / group_standard /
# manual 等），医案层的边是 case——它是数据标注，不是封闭枚举，在这里另列一份
# 只会跟数据对不上。
