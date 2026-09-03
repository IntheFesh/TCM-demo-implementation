"""知识图谱的节点/边类型常量。只集中定义类型名和它们的含义说明，不做校验框架——
这些字符串会被 build_graph.py、weights.py、tools.py 反复用到，集中定义是为了不让
"symptom" 和 "Symptom" 这种手误各处散落，不是为了搞一套类型系统。

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

# 每条边的 source 属性只能是这三种之一——用来区分"这条边是标准骨架"还是
# "数据权重/证据"，是后面 K2 层级回退收缩的判据来源，不能省。
EDGE_SOURCES = ("standard", "case", "textbook")
