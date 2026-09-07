"""药名归一。从 core/chain.py 拆出来的——core/safety_output.py 也要用它，
留在 chain 里会造成 chain ↔ safety_output 循环导入。

只做"把同一味药的不同写法归到可比对的形式"，不含任何业务判断。
chain.py 仍然 re-export normalize_herb / strip_dose，老调用方不用改。
"""
from __future__ import annotations

import re

_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
# 剂量写法实测有「一钱半」「钱半」「一钱五分」——原来的正则只认「数字+单位」，
# 这三种会把「半」「五分」残留在药名里，后面别名表、禁忌表、Jaccard 全部错配。
_DOSE_RE = re.compile(
    r"(?:[一二三四五六七八九十百半\d.]+(?:钱|两|分|克|g|枚|片|条|支|具|个|茶匙|杯)"
    r"(?:半)?(?:[一二三四五六七八九十\d]+(?:分|厘))?|钱半|两半)\s*$"
)

# 炮制前缀/后缀：同一味药在不同医家笔下写法不同（广皮=陈皮、炙草=炙甘草），
# 不归一的话药物集合比对会把同一味药算成两味，Jaccard 被系统性推高——
# 实测出现过两边实际用药大量重合、Jaccard 却算成 1.0 的情况。
_AFFIX_CHARS = "炒焦生制炙姜酒醋盐煨煅蜜清净广川云北南东西"
_HERB_SUFFIX = re.compile(r"(汁|炭|末|粉|片|块|皮尖)$")

HERB_ALIASES: dict[str, str] = {
    "广皮": "陈皮", "橘皮": "陈皮", "新会皮": "陈皮",
    "炙草": "甘草", "炙甘草": "甘草", "生甘草": "甘草", "粉甘草": "甘草",
    "云苓": "茯苓", "白苓": "茯苓", "茯苓块": "茯苓", "茯苓皮": "茯苓", "赤苓": "茯苓",
    "川连": "黄连", "真云连": "黄连", "山连": "黄连", "雅连": "黄连",
    "北沙参": "沙参", "南沙参": "沙参",
    "白扁豆": "扁豆", "生扁豆": "扁豆",
    "半夏曲": "半夏", "姜半夏": "半夏", "制半夏": "半夏", "法半夏": "半夏",
    "小枳实": "枳实", "淡吴萸": "吴茱萸", "吴萸": "吴茱萸",
    "老浓朴": "厚朴", "浓朴": "厚朴",
    "焦六曲": "神曲", "六曲": "神曲", "建曲": "神曲",
    "焦山楂": "山楂", "生山楂": "山楂",
    "潞党参": "党参", "台党参": "党参",
    "冬术": "白术", "於术": "白术",
    # 实测语料里出现、原来归不到一起的写法（「云苓块」与「茯苓块」曾被算成两味药）
    "云连": "黄连", "川黄连": "黄连",
    "云苓块": "茯苓", "云苓皮": "茯苓", "苓块": "茯苓",
    # 生地/熟地是两味药性相反的药，只把「生」这一系归到地黄，熟地黄保持原名
    "生地": "地黄", "生地黄": "地黄", "干地黄": "地黄", "大生地": "地黄", "细生地": "地黄",
}


def strip_dose_and_parens(herb: str) -> str:
    """只剥括号注释和剂量，**不动炮制前后缀**。

    单独暴露出来是因为后缀剥离对某些药名过头："天花粉"会被剥成"天花"、
    "黑顺片"会被剥成"黑顺"。配伍禁忌那边需要在剥后缀之前先查一次别名表
    （见 core/safety_output.normalize_for_incompat）。

    顺序重要：先剥括号（"旋覆花二钱（包煎）"的括号在剂量之后，
    不先剥掉的话 $ 锚点匹配不到剂量），再剥剂量。
    """
    s = _PAREN_RE.sub("", herb).strip()
    return _DOSE_RE.sub("", s).strip()


def _alias(s: str) -> str | None:
    """查别名表：原样查一次，剥掉后缀再查一次。"""
    return HERB_ALIASES.get(s) or HERB_ALIASES.get(_HERB_SUFFIX.sub("", s).strip())


def normalize_herb(herb: str) -> str:
    """把药名归一到可比对的形式：剥括号注释、剥剂量、查别名表、剥炮制前后缀。

    前缀是**逐字**剥、每剥一个字查一次别名表，不是一次性全剥完再查。原来一次性
    贪婪剥完（「炒广皮」→「皮」）只剩一个字就退回原串，别名表从头到尾没机会命中
    ——实测「炒广皮」「广皮炭」「云苓块」都因此归不到陈皮/茯苓，两位医家写法不同
    就被算成两味药，Jaccard 被系统性推高，正是这个模块要防的事。
    """
    s = strip_dose_and_parens(herb)
    if not s:
        return ""
    core = s
    while True:
        hit = _alias(core)
        if hit:
            return hit
        # 剥到只剩两个字就停：再剥就是「生姜」→「姜」这种剥过头
        if len(core) > 2 and core[0] in _AFFIX_CHARS:
            core = core[1:]
            continue
        break
    stripped = _HERB_SUFFIX.sub("", core).strip()
    return stripped if len(stripped) >= 2 else core


def strip_dose(herb: str) -> str:
    """保留旧名，内部走 normalize_herb。"""
    return normalize_herb(herb)
