"""医家注册表：id、显示名、出处、年代、学派、前端配色。全项目的医家元数据只在这里
定义一处——core/chain.py、K2 的学派分组、前端配色都从这里取，不要在别处再手写
"ye_tianshi": "叶天士" 这种映射，两处会分叉。

R2 计划加入张锡纯（衷中参西派）后，这里会是三位医家、两个学派——那是 K2 的 λ2
（学派层）从"必然与医家层共线的假信号"变成"真的有跨学派对照"的分界点。
张锡纯加入之前，不要把 λ2 的数字当成已经验证过的东西看待
（见 offline/graph_stats.py 里的警告行）。
"""
from __future__ import annotations

PHYSICIANS: dict[str, dict] = {
    "ye_tianshi": {
        "name": "叶天士",
        "book": "临证指南医案",
        "years": "1667-1746",
        "school": "温病",
        "color": "#1D9E75",
    },
    "wu_jutong": {
        "name": "吴鞠通",
        "book": "吴鞠通医案",
        "years": "1758-1836",
        "school": "温病",
        "color": "#D85A30",
    },
}


def schools() -> dict[str, list[str]]:
    """学派 -> 该学派下的医家 id 列表（保持 PHYSICIANS 里的插入顺序）。
    K2 的层级回退权重（学派层）按这个分组统计。"""
    out: dict[str, list[str]] = {}
    for pid, info in PHYSICIANS.items():
        out.setdefault(info["school"], []).append(pid)
    return out
