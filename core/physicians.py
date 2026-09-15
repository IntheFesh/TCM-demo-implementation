"""医家注册表：id、显示名、出处、年代、学派、前端配色。全项目的医家元数据只在这里
定义一处（R13 起 `color`/`color_bg` 由 `/health` 下发、前端注入成 CSS 变量，
**CSS 里不许再写死身份色**——写死的常量也算一处实现，注册表加第四位医家时
CSS 那份副本不会跟着长出来，新医家在界面上就没有颜色）——core/chain.py、K2 的学派分组、前端配色都从这里取，不要在别处再手写
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
        "color": "#2C5F5A",  # 青黛 —— 温病轻清
        "color_bg": "#E6EFED",
    },
    "wu_jutong": {
        "name": "吴鞠通",
        "book": "吴鞠通医案",
        "years": "1758-1836",
        "school": "温病",
        "color": "#9C6B16",  # 黄芩 —— 苦辛通降
        "color_bg": "#F4EDDF",
    },
    "zhang_xichun": {
        "name": "张锡纯",
        "book": "医学衷中参西录",
        "years": "1860-1933",
        "school": "衷中参西",
        "color": "#8A4736",  # 赭石 —— 他最标志的药就是生赭石，三条医案里出现三次
        "color_bg": "#F2E7E3",
    },
}


def resolve_physician_id(value: str | None) -> str | None:
    """把 id 或中文名解析成 id。已经是 id 就原样返回，是中文名就转成 id，
    都不是（含 None/空串）返回 None。

    存在的理由：模型在 ReAct 里只能看到 prompt 给的中文名（$name），它填
    physician 参数时自然填中文名，而 cases.json / data/case_triples.jsonl
    里存的是 id——这个不匹配让医案层工具恒返回空（AutoDL 实测 9 次调用
    全空，见 SOURCES.md 第 31 条）。**全项目唯一的 name→id 入口**：任何接收
    外部输入（模型输出、HTTP 路径参数、CLI）的边界都过这里，不要在过滤处
    直接比较（CLAUDE.md「标识符只有一种规范形式，边界上统一解析」）。
    注册表目前没有别名字段，有了别名再扩这一处，别在调用方各自兜。"""
    if not value:
        return None
    v = value.strip()
    if v in PHYSICIANS:
        return v
    for pid, info in PHYSICIANS.items():
        if info["name"] == v:
            return pid
    return None


def physician_choices_text() -> str:
    """给模型/报错看的可用值清单：「ye_tianshi(叶天士) / wu_jutong(吴鞠通) /
    ...」。从注册表动态拼，加医家时这里不用改——工具 schema 描述和解析失败
    的报错都用它，两处措辞不会分叉。"""
    return " / ".join(f"{pid}({info['name']})" for pid, info in PHYSICIANS.items())


def schools() -> dict[str, list[str]]:
    """学派 -> 该学派下的医家 id 列表（保持 PHYSICIANS 里的插入顺序）。
    K2 的层级回退权重（学派层）按这个分组统计。"""
    out: dict[str, list[str]] = {}
    for pid, info in PHYSICIANS.items():
        out.setdefault(info["school"], []).append(pid)
    return out
