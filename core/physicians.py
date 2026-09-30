"""医家注册表：id、显示名、出处、年代、学派、前端配色。全项目的医家元数据只在这里
定义一处——core/chain.py、学派分组（图谱四层收缩权重的学派层 λ2，见
core/graph/weights.py）、前端配色都从这里取，不要在别处再手写
"ye_tianshi": "叶天士" 这种映射，两处会分叉。`color`/`color_bg` 由 `/health`
下发、前端注入成 CSS 变量，**CSS 里不写死身份色**：写死的常量也算一处实现，
注册表加一位医家时 CSS 那份副本不会跟着长出来，新医家在界面上就没有颜色。

注册表共五位医家：

- **三位组成三列集注**（`enabled=True`）：叶天士、吴鞠通（温病）与张锡纯
  （衷中参西）。三家、两个学派，λ2（学派层）与「跨学派分歧大于师承内」
  才有真正跨学派的对照。衷中参西学派下只有张锡纯一位，λ2 对他不构成独立
  信号（见 offline/graph_stats.py 的提示行）。三家医案均出自公有领域古籍。
- **两位参考医家**（`enabled=False`）：李可、王云启。语料版权受限、不随仓库
  分发，使用者需自行获取后放进 `data/local_corpora/`。他们进检索、训练导出、
  图谱医案层与「参考医家」引用区，不占集注的列。

`enabled`（谁算三列集注的一员）与 `in_synthesis`（谁参与结构化模式的综合分析）
是两个字段，因为它们回答两个不同的问题，理由见 `physicians_for_synthesis`。
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
        "enabled": True,
        "in_synthesis": True,
        "source": "data/ye_tianshi/*.json（《临证指南医案》，公有领域）",
    },
    "wu_jutong": {
        "name": "吴鞠通",
        "book": "吴鞠通医案",
        "years": "1758-1836",
        "school": "温病",
        "color": "#9C6B16",  # 黄芩 —— 苦辛通降
        "color_bg": "#F4EDDF",
        "enabled": True,
        "in_synthesis": True,
        "source": "data/wu_jutong/*.json（《吴鞠通医案》，公有领域）",
    },
    "zhang_xichun": {
        "name": "张锡纯",
        "book": "医学衷中参西录",
        "years": "1860-1933",
        "school": "衷中参西",
        "color": "#8A4736",  # 赭石 —— 他最标志的药就是生赭石
        "color_bg": "#F2E7E3",
        "enabled": True,
        "in_synthesis": True,
        "source": "books/584-医学衷中参西录.txt（公有领域）",
    },
    # ---- 参考医家：注册但**不参与集注**（enabled=False）----
    #
    # 三列集注的三位是温病 × 2 + 衷中参西 × 1，这个组合是 λ2（学派层）能不能
    # 成立的对照设计。李可、王云启都是现当代肿瘤方向，塞进三列会同时坏掉两件事：
    # 版面（四列/五列并排读不了）和对照（学派维度被稀释成"每人一个学派"）。
    #
    # 但他们的医案有独立的参考价值，语料**要进检索、进训练、进图谱医案层、
    # 进「参考医家」引用区**。所以是 enabled=False 而不是不注册：注册了才有
    # 唯一的 id、才有统一的 name→id 解析、才能被 search_cases 按 physician 查到。
    #
    # **years / school 是 None，不是猜的。** 两份语料的前言/书名页里都没有生卒年
    # 和学派归属（王云启那份的「代序」说的是"省级名中医""湖湘中医文化"、
    # 学术思想列了七八条，那是文字描述不是一个可用于配对的离散学派标签）。
    # 查不到就留空，不编——`pairwise_divergence` 见到任一方没有 school 会把
    # 这一对判成 "unknown"，那是对的，比给一个猜的学派然后让它去参与
    # "跨学派分歧大于师承内"的统计好得多。
    "li_ke": {
        "name": "李可",
        "book": "李可医案（肿瘤案汇编）",
        "years": None,
        "school": None,
        # 石绿。不在集注三家的身份色里——这两位不占列，只在「参考医家」
        # 引用区出现，需要一个能跟三家区分开的色。
        "color": "#4A6B4E",
        "color_bg": "#E8EEE8",
        "enabled": False,
        "in_synthesis": True,
        "source": "data/local_corpora/李可医案.txt（版权受限，不随仓库分发，需自行获取）",
    },
    "wang_yunqi": {
        "name": "王云启",
        "book": "王云启治癌验案录",
        "years": None,
        "school": None,
        "color": "#5B5470",  # 藤紫
        "color_bg": "#EAE8EF",
        "enabled": False,
        "in_synthesis": True,
        "source": "data/local_corpora/王云启医案.txt（版权受限，不随仓库分发，需自行获取）",
    },
}


#: 结构化模式下那份结论的展示元数据。
#: 显示名是「本次辨证」，理由见 `core/chain.py::SYNTHESIS_PHYSICIAN_NAME`
#: 那段注释（不给结论加投票的外观）。名字只有那一处定义，这里的 `name` 从它取。
#:
#: **刻意不放进 `PHYSICIANS`。** 放进去的话 `resolve_physician_id("synthesis")`
#: 会把它解析成一个合法医家，于是检索层会去找「synthesis 的医案库」（恒空）、
#: 分歧度会把它当成又一位医家参与两两配对。它不是一位医家，是一份结论的署名。
#:
#: 但它需要姓名和配色——`api/main.py::_serialize_result` 按 id 查配色，查不到会退到
#: 灰色兜底，而前端把灰色当「未知医家」显示。所以元数据在这里定义**一处**，
#: 不在 api 层和前端各写一份（docs/ARCHITECTURE.md §4：这里的"同一概念"是
#: "这份结论长什么样"）。
SYNTHESIS_DISPLAY: dict = {
    # 从 core/chain.py 取，避免两处各写一份中文名：改名时其中一处漏改，
    # 界面上两个地方的叫法就会不一样。
    # 延迟 import 放在下面 `synthesis_display()` 里——core.chain 反过来 import
    # 这个模块，顶层直接 import 会成环。
    "name": None,
    "book": "叶天士《临证指南医案》/ 吴鞠通《吴鞠通医案》/ 张锡纯《医学衷中参西录》"
            "/ 李可医案 / 王云启治癌验案录",
    "years": None,      # 五家跨两百余年，给一个区间等于给一个假精确
    "school": None,     # 融合的产物没有单一学派，填一个会让 λ2 统计把它算进去
    "color": "#3B4A6B",   # 靛青。跟五位医家的身份色都不同——它不是其中任何一位
    "color_bg": "#E7EAF1",
}


def synthesis_display() -> dict:
    """带上显示名的那一份。**名字的唯一定义在 `core.chain`**，这里现取。"""
    from core.chain import SYNTHESIS_PHYSICIAN_NAME

    return {**SYNTHESIS_DISPLAY, "name": SYNTHESIS_PHYSICIAN_NAME}


def physicians_enabled(registry: dict[str, dict] | None = None) -> dict[str, dict]:
    """参与三列集注的医家。**遍历 PHYSICIANS 的地方一律走这个入口**——
    chain / 前端三列 / 评测 / 训练各自写一遍 `if info["enabled"]` 的话，
    漏掉一处的后果是某条路径悄悄多算了两位医家：分歧度会把李可和三家一起算
    n 方交并比（那个数直接失去意义），ε 的对照基准也跟着变。

    docs/ARCHITECTURE.md §4「同一概念只有一处实现」：这里的"同一概念"是
    「谁算集注的一员」。

    `registry` 参数：调用方传自己模块里那个 `PHYSICIANS` 名字。看起来多余
    （不传也读同一个 dict），但它让**测试的打桩方式有效**——大量测试用
    `monkeypatch.setattr(某模块, "PHYSICIANS", {...})` 换一个两位医家的小
    注册表。不接这个参数的话，这些桩全部失效：函数读的是 `core.physicians`
    自己的那份，桩打在别的模块上。

    **筛选逻辑仍然只有这一处**，调用方传的只是数据。"""
    return {pid: info for pid, info in (registry if registry is not None else PHYSICIANS).items()
            if info.get("enabled", True)}


def physicians_all(registry: dict[str, dict] | None = None) -> dict[str, dict]:
    """全部注册医家，含 enabled=False 的。检索语料、训练导出、图谱医案层、
    「参考医家」引用区走这个——**它们要的是"这个 id 合法吗、他的语料在哪"**，
    跟"他算不算集注的一员"是两个问题。

    两个入口都存在的意义就在这儿：调用方必须显式选一个，而选的时候就得想清楚
    自己问的是哪个问题。`PHYSICIANS` 本身仍然公开（注册表是数据），但**新增
    的遍历一律走这两个函数之一，不要在别处自己 filter**。"""
    return dict(registry if registry is not None else PHYSICIANS)


def physicians_for_synthesis(registry: dict[str, dict] | None = None) -> dict[str, dict]:
    """参与**综合分析**（`S3_MODE=structured`）的医家。五位全在。

    ## 为什么是单独一个字段，而不是让五位都 `enabled`

    `enabled` 回答的是「谁算三列集注的一员」（见 `physicians_enabled` 的文档）。
    结构化模式没有三列——它只出**一份**融合结论，所以"谁参与"在这两种模式下
    **不是同一个问题**。一个字段答两个问题，正是 docs/ARCHITECTURE.md §4
    要防的形状（这里是注册表字段的语义，不是匹配逻辑）。

    **让五位都 `enabled` 的代价可以直接算出来**：李可与王云启的 `school`
    都是 None（两份语料的前言里查不到，不编），五位两两配对共 10 对，
    其中 **7 对（70%）** 的学派判定会变成 "unknown"——而 λ2（学派层权重）与
    「跨学派分歧大于师承内」这条对照都依赖学派标签。
    结构化模式不需要付这个代价：它要的是"五家的思路都进这次综合"，
    靠 `in_synthesis` 就够了，不必动 `enabled`。

    默认值是 **True**：新注册的医家自动参与综合分析，
    要排除得显式写 `in_synthesis: False`。跟 `enabled` 的默认 True 一致。
    """
    return {pid: info for pid, info in (registry if registry is not None else PHYSICIANS).items()
            if info.get("in_synthesis", True)}


def physicians_for_mode(mode: str, registry: dict[str, dict] | None = None) -> dict[str, dict]:
    """按 `S3_MODE` 选名单。**这一跳只有一处实现**——chain / usage / api / 前端
    各自写一遍 `if mode == "structured"` 的话，漏一处的后果跟
    `physicians_enabled` 文档里说的完全一样：某条路径悄悄多算或少算了两位医家。

    `mode` 由调用方传进来、不在这里读环境变量：同一个请求的几处判断必须用同一个
    值（跟 `run_physician` 的 `retriever_mode`、`bypass_safety` 同一条纪律——
    进程级环境变量会让两个并发请求互相污染）。

    `derived` 恒返回**空字典**，不是 `physicians_enabled` 也不是
    `physicians_for_synthesis` 的某个子集：演绎推导模式不检索任何医案，也就
    没有"哪位医家参与了这次结论"这件事——0 是这个问题在这种模式下唯一诚实的
    答案，不是"数据缺失暂记 0"。`calls_per_consult`/`scripts/preflight_runtime.py`
    读到的这个 0 就是对的，不是需要另外特殊处理的边界情况。
    """
    from core.llm import S3_MODES

    if mode not in S3_MODES:
        raise ValueError(f"S3 模式 {mode!r} 不认识，只能是：{' / '.join(S3_MODES)}")
    if mode == "derived":
        return {}
    return (physicians_for_synthesis(registry) if mode == "structured"
            else physicians_enabled(registry))


def resolve_physician_id(value: str | None) -> str | None:
    """把 id 或中文名解析成 id。已经是 id 就原样返回，是中文名就转成 id，
    都不是（含 None/空串）返回 None。

    存在的理由：模型在 ReAct 里看到的是 prompt 给的中文名（$name），它填
    physician 参数时自然填中文名，而 cases.json / data/case_triples.jsonl
    里存的是 id——不经解析直接比较，医案层工具会恒返回空（docs/ARCHITECTURE.md §5）。
    **全项目唯一的 name→id 入口**：任何接收外部输入（模型输出、HTTP 路径
    参数、CLI）的边界都过这里，不要在过滤处直接比较（docs/ARCHITECTURE.md §5
    「标识符在边界统一解析」）。注册表没有别名字段，需要别名时扩这一处，
    别在调用方各自兜。"""
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
    图谱四层收缩权重的学派层（core/graph/weights.py）按这个分组统计。"""
    out: dict[str, list[str]] = {}
    for pid, info in PHYSICIANS.items():
        out.setdefault(info["school"], []).append(pid)
    return out
