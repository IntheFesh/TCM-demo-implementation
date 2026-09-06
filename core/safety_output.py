"""输出侧安全校验（X2）：S3 开出方药之后查确定性规则。

跟 core/safety.py 的分工：
  core/safety.py        输入侧，S2 之前，拦危重症状，**拦下就不出方**
  core/safety_output.py 输出侧，S3 之后，查配伍禁忌与寒热方向，**只打回重生成一次**

为什么输出侧不能也"拦下不出"：输入侧拦的是"这个病人不该由 demo 处理"，
输出侧查的是"这一版方子开得有问题"——后者的正确处置是让模型重开，不是
拒绝服务。重生成只做一次、不循环：循环会让 llm_calls 变成不可预测的数，
manifest 里的调用数就失去意义了。

两条规则的硬度不同，处置也不同：
  十八反十九畏  是确定性的成文禁忌，命中就打回重生成
  寒热一致性    是粗规则（寒热错杂本来就寒热并用），只出警告不打回
"""
from __future__ import annotations

from core.herbs import normalize_herb, strip_dose_and_parens

# ---------- 归一：把同一味药的各种写法归到禁忌表用的名字 ----------

# chain.normalize_herb 已经处理了炮制前后缀和常见别名（炙甘草→甘草、
# 姜半夏→半夏、北沙参→沙参）。这里再加一层"禁忌类目"归并：十八反讲的是
# 药材类目，川乌/草乌/附子在配伍禁忌上按同一味处理。
#
# 一条刻意的例外：党参不归人参。党参是桔梗科，人参是五加科，
# "藜芦反人参"不涵盖党参——归错会把正常方子误报成禁忌。
# 红参归人参：红参就是人参蒸制品，同一物种，禁忌适用。安全检查宁可多报不可漏报。
INCOMPAT_ALIASES: dict[str, str] = {
    # 乌头类（十八反的"乌头"、十九畏的"川乌草乌"）
    "川乌": "乌头", "草乌": "乌头", "生川乌": "乌头", "制川乌": "乌头",
    "生草乌": "乌头", "制草乌": "乌头",
    "附子": "乌头", "制附子": "乌头", "黑顺片": "乌头", "白附片": "乌头",
    "淡附片": "乌头",
    # 贝母类
    "浙贝母": "贝母", "川贝母": "贝母", "浙贝": "贝母", "川贝": "贝母",
    "土贝母": "贝母", "平贝母": "贝母",
    # 瓜蒌类（含瓜蒌根天花粉）
    "栝楼": "瓜蒌", "全瓜蒌": "瓜蒌", "瓜蒌皮": "瓜蒌", "瓜蒌仁": "瓜蒌",
    "瓜蒌子": "瓜蒌", "天花粉": "瓜蒌",
    # 芍药类
    "白芍": "芍药", "赤芍": "芍药", "杭白芍": "芍药",
    # 人参类（红参同物种，党参/太子参/西洋参不归）
    "红参": "人参", "生晒参": "人参", "白参": "人参", "野山参": "人参",
    "高丽参": "人参",
    # 芒硝类（十九畏里"朴硝""牙硝"都是芒硝的异名）
    "朴硝": "芒硝", "牙硝": "芒硝", "玄明粉": "芒硝", "元明粉": "芒硝",
    "皮硝": "芒硝",
    # 肉桂类（十九畏"官桂畏石脂"）
    "官桂": "肉桂", "桂心": "肉桂",
    # 赤石脂
    "石脂": "赤石脂",
    # 牵牛类
    "牵牛子": "牵牛", "二丑": "牵牛", "黑丑": "牵牛", "白丑": "牵牛",
    # 巴豆
    "巴豆霜": "巴豆",
    # 丁香
    "公丁香": "丁香", "母丁香": "丁香",
    # 犀角（现已禁用，规则保留：古方里会出现）
    "广角": "犀角", "犀牛角": "犀角",
    # 海藻/甘遂/大戟/芫花的常见炮制写法
    "醋甘遂": "甘遂", "醋大戟": "大戟", "京大戟": "大戟", "醋芫花": "芫花",
    # 五灵脂
    "醋五灵脂": "五灵脂",
}


def normalize_for_incompat(herb: str) -> str:
    """归到禁忌表用的类目名。

    先剥剂量/括号后查一次别名表，**再**走完整归一——顺序不能反：
    通用归一会剥炮制后缀，把"天花粉"剥成"天花"、"黑顺片"剥成"黑顺"，
    剥完就查不到别名表了（实测这两个都漏检过）。
    """
    s = strip_dose_and_parens(herb)
    if s in INCOMPAT_ALIASES:
        return INCOMPAT_ALIASES[s]
    base = normalize_herb(herb)
    return INCOMPAT_ALIASES.get(base, base)


# ---------- 十八反十九畏 ----------

def _pairs(one: str, many: list[str]) -> list[frozenset[str]]:
    return [frozenset({one, other}) for other in many]


# 十八反：甘草组 4 + 乌头组 5 + 藜芦组 6 = 15 对
SHIBAFAN: list[frozenset[str]] = (
    _pairs("甘草", ["甘遂", "大戟", "海藻", "芫花"])
    + _pairs("乌头", ["贝母", "瓜蒌", "半夏", "白蔹", "白及"])
    + _pairs("藜芦", ["人参", "沙参", "丹参", "玄参", "细辛", "芍药"])
)

# 十九畏：9 对
SHIJIUWEI: list[frozenset[str]] = [
    frozenset({"硫黄", "芒硝"}),      # 硫黄畏朴硝（朴硝已归一到芒硝）
    frozenset({"水银", "砒霜"}),
    frozenset({"狼毒", "密陀僧"}),
    frozenset({"巴豆", "牵牛"}),
    frozenset({"丁香", "郁金"}),
    frozenset({"乌头", "犀角"}),      # 川乌草乌畏犀角
    frozenset({"芒硝", "三棱"}),      # 牙硝畏三棱
    frozenset({"肉桂", "赤石脂"}),    # 官桂畏石脂
    frozenset({"人参", "五灵脂"}),
]

INCOMPATIBLE_PAIRS: list[frozenset[str]] = SHIBAFAN + SHIJIUWEI


def check_incompatible(herbs: list[str]) -> list[tuple[str, str]]:
    """查十八反十九畏。返回冲突对（用**原始写法**，不是归一后的名字——
    评审要看的是模型实际开了什么，"制附子"比"乌头"更有信息量）。空列表 = 通过。"""
    # 归一名 → (第一次出现的原始写法, 在方中的位置)。同一味药重复出现只报一次。
    canon_to_raw: dict[str, tuple[str, int]] = {}
    for i, h in enumerate(herbs):
        c = normalize_for_incompat(h)
        if c and c not in canon_to_raw:
            canon_to_raw[c] = (h, i)

    found: list[tuple[str, str]] = []
    present = set(canon_to_raw)
    for pair in INCOMPATIBLE_PAIRS:
        if pair <= present:
            # 按在方中出现的先后排，不按拼音/码点——展示成"甘草 反 海藻"时
            # 跟着处方的书写顺序读起来才对得上。
            a, b = sorted(pair, key=lambda c: canon_to_raw[c][1])
            found.append((canon_to_raw[a][0], canon_to_raw[b][0]))
    return found


# ---------- 寒热一致性 ----------

# 各 30 味常用药。这两张表只用于粗判方向，不是完整药性表——
# 没收进来的药在这条规则下按"中性"处理，不会造成误报。
COLD_HERBS: frozenset[str] = frozenset({
    "黄连", "黄芩", "黄柏", "栀子", "石膏", "知母", "大黄", "芒硝",
    "龙胆草", "苦参", "金银花", "连翘", "蒲公英", "板蓝根", "生地黄",
    "玄参", "牡丹皮", "水牛角", "青蒿", "地骨皮", "白薇", "夏枯草",
    "决明子", "竹叶", "芦根", "鱼腥草", "败酱草", "白头翁", "秦皮", "牛蒡子",
})

HOT_HERBS: frozenset[str] = frozenset({
    "附子", "干姜", "肉桂", "吴茱萸", "细辛", "花椒", "高良姜", "丁香",
    "小茴香", "荜茇", "胡椒", "生姜", "桂枝", "麻黄", "紫苏", "荆芥",
    "防风", "白芷", "羌活", "独活", "苍术", "厚朴", "砂仁", "豆蔻",
    "草果", "益智仁", "补骨脂", "淫羊藿", "巴戟天", "仙茅",
})

MAIN_FORMULA_SIZE = 6      # 只看主方前 6 味，佐使药不算
THERMAL_MAJORITY = 3       # 前 6 味里超过 3 味 = 过半

_COLD_SYNDROME_KEYS = ("寒", "阳虚")
_HOT_SYNDROME_KEYS = ("热", "阴虚", "火")


def check_thermal_consistency(syndrome: str, herbs: list[str]) -> str | None:
    """证型寒热方向与主方药性是否相悖。返回警告文本或 None。

    **只警告不打回**：这条规则粗，寒热错杂证本来就寒热并用，硬打回会把
    正确的方子改坏。证型里同时出现寒与热的字样直接跳过。

    注意寒热的判断落在证型名的字面上（"脾胃虚寒证"含"寒"），不做语义理解——
    这是已知的粗糙处，写进 README 的局限里，不要在这里偷偷加推断。
    """
    has_cold = any(k in syndrome for k in _COLD_SYNDROME_KEYS)
    has_hot = any(k in syndrome for k in _HOT_SYNDROME_KEYS)

    if has_cold and has_hot:
        return None  # 寒热错杂，规则不适用
    if not has_cold and not has_hot:
        return None  # 证型没有明确寒热方向，不判

    main = [normalize_herb(h) for h in herbs[:MAIN_FORMULA_SIZE]]
    cold_hits = [h for h in main if h in COLD_HERBS]
    hot_hits = [h for h in main if h in HOT_HERBS]

    if has_cold and len(cold_hits) > THERMAL_MAJORITY:
        return (
            f"证型「{syndrome}」属寒/阳虚，但主方前 {len(main)} 味中有 "
            f"{len(cold_hits)} 味寒凉药（{'、'.join(cold_hits)}），寒热方向可能相悖"
        )
    if has_hot and len(hot_hits) > THERMAL_MAJORITY:
        return (
            f"证型「{syndrome}」属热/阴虚，但主方前 {len(main)} 味中有 "
            f"{len(hot_hits)} 味温热药（{'、'.join(hot_hits)}），寒热方向可能相悖"
        )
    return None


def format_conflicts(conflicts: list[tuple[str, str]]) -> str:
    """把冲突对拼成给 S3 重生成用的提示文本。"""
    return "、".join(f"{a} 与 {b}" for a, b in conflicts)
