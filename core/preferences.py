"""R62 §6.6 + §4.3：每个使用者的界面设置与用药习惯。

## 为什么不长在 core/history.py 里

那个文件存的是**一条条记录**（问过哪些诊、收藏过哪些方），一条写进去就不再
改；这里存的是**每个键当前是什么值**（默认剂型是饮片还是颗粒），写第二次是
覆盖第一次。两种形态的读法不同——前者要"全部列出来"，后者要"取最新的那一份"
——合进一个文件之后，`list_*` 会把历次设置的中间态也列出来。

CLAUDE.md 第 31 条的例外条款：两处回答的**不是同一个问题**。这里写清楚区别。

存储仍然照 `core/history.py` 的形态：一份 JSONL 追加写，读最后一条
（跟 `save_emr`/`get_emr` 完全一样）。追加而不是覆写，是为了"这位医师什么
时候改了忌用药清单"这件事仍然查得出来——设置被改过而没人记得改过，是最难
查的那一类问题。

## 文件落在 data/ 根下，不是 data/standard/

这是**运行时产生的用户数据**，不该进版本控制。`.gitignore` 对 `*.jsonl`
整体忽略、只对 `data/standard/*.jsonl` 开了例外（CLAUDE.md 那个已经踩过两次
的坑），所以落在 `data/` 根下正好是对的——不是漏配了例外。

## 拼错的键当场抛，不静默存进去

跟 `core/product_mode.py::require_internal` 同一条纪律。一个静默接受
`defualt_doses` 的设置接口，会让"我明明设了 14 剂"这件事永远查不出原因。
"""
from __future__ import annotations

from pathlib import Path

from core.history import DATA_DIR, _append, _read
from core.product_mode import PRODUCT_ROLES

PREFERENCES_PATH = DATA_DIR / "preferences.jsonl"

#: 剂型三选。**一处定义**——设置面板、处方表的剂型下拉、导出时的用法文案
#: 三处都问这里。§6.3：剂型影响用法与输出格式。
DOSAGE_FORMS: tuple[str, ...] = ("饮片", "颗粒", "膏方")

#: 剂数四选（§4.3 那张表原文）。不是任意正整数：这是个下拉框，
#: 允许任意值就得再加一道"输入校验"，而那道校验会跟这里的取值范围分叉。
DOSES_CHOICES: tuple[int, ...] = (3, 5, 7, 14)

#: 常用煎服法三选（§4.3「常用三种」）。剂型换了默认煎服法也该换，
#: 所以按剂型给默认——`default_usage_for(form)`。
USAGE_CHOICES: dict[str, tuple[str, ...]] = {
    "饮片": ("水煎服，每日1剂，分2次温服",
             "水煎服，每日1剂，分3次温服",
             "水煎服，两日1剂，分4次温服"),
    "颗粒": ("开水冲服，每日1剂，分2次温服",
             "开水冲服，每日1剂，分3次温服",
             "开水冲服，两日1剂，分4次温服"),
    "膏方": ("每次1匙，开水化服，每日2次",
             "每次1匙，开水化服，每日1次，晨起空腹",
             "每次半匙，开水化服，每日2次"),
}

#: 常用药味数区间（§6.6）。区间而不是一个数：医师的习惯是"十二三味"，
#: 不是"恰好 13 味"。
HERB_COUNT_BANDS: tuple[str, ...] = ("8-12", "12-16", "16+")

#: 字号两档（§4.3）。
FONT_SIZES: tuple[str, ...] = ("standard", "large")


def default_usage_for(form: str) -> str:
    """这个剂型的默认煎服法。剂型认不出时退到饮片那一档——
    界面上剂型是个只有三项的下拉，认不出只可能来自老客户端或手改的存档。"""
    return USAGE_CHOICES.get(form, USAGE_CHOICES["饮片"])[0]


#: 全部可设置的键与默认值。**清单在这里，不在前端、也不在各个读取点**。
#: 前端写死一份默认值的话，这里改了默认剂数，界面上不会跟着变。
DEFAULTS: dict = {
    "role": "doctor",
    "font_size": "standard",
    "dosage_form": "饮片",
    "doses_count": 7,
    "usage": USAGE_CHOICES["饮片"][0],
    "herb_count_band": "12-16",
    "avoid_herbs": [],
    "signature": "",
}


class UnknownPreference(KeyError):
    """设置了一个不存在的键。"""

    def __init__(self, key: str) -> None:
        super().__init__(
            f"没有 {key!r} 这一项设置；可设置的是：{'、'.join(sorted(DEFAULTS))}")


class BadPreferenceValue(ValueError):
    """键对了但值不在允许范围内。"""


def _check(key: str, value):
    """逐键校验。**返回归一之后的值**，不是只判对错——`doses_count` 前端传
    过来是字符串 "7"，存成字符串的话下次读出来跟默认值 7 不是同一个类型，
    而界面上那个下拉是按值相等选中的。"""
    if key not in DEFAULTS:
        raise UnknownPreference(key)
    if key == "role":
        if value not in PRODUCT_ROLES:
            raise BadPreferenceValue(
                f"角色只能是 {'、'.join(PRODUCT_ROLES)}，收到 {value!r}")
        return value
    if key == "font_size":
        if value not in FONT_SIZES:
            raise BadPreferenceValue(f"字号只能是 {'、'.join(FONT_SIZES)}，收到 {value!r}")
        return value
    if key == "dosage_form":
        if value not in DOSAGE_FORMS:
            raise BadPreferenceValue(f"剂型只能是 {'、'.join(DOSAGE_FORMS)}，收到 {value!r}")
        return value
    if key == "doses_count":
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise BadPreferenceValue(f"剂数要是数字，收到 {value!r}") from None
        if n not in DOSES_CHOICES:
            raise BadPreferenceValue(
                f"剂数只能是 {'、'.join(str(d) for d in DOSES_CHOICES)}，收到 {value!r}")
        return n
    if key == "herb_count_band":
        if value not in HERB_COUNT_BANDS:
            raise BadPreferenceValue(
                f"常用药味数只能是 {'、'.join(HERB_COUNT_BANDS)}，收到 {value!r}")
        return value
    if key == "avoid_herbs":
        if isinstance(value, str):
            # 界面上这是一个顿号分隔的输入框。**在这里切，不在前端切**：
            # 切法散成两份之后，前端用顿号、这里用逗号，会出现"存进去了但
            # 匹配不上"——而那个 bug 只在药名里恰好带分隔符时才显形。
            value = [v.strip() for v in value.replace("，", "、").replace(",", "、").split("、")]
        if not isinstance(value, list):
            raise BadPreferenceValue(f"忌用药要是列表或顿号分隔的字符串，收到 {type(value).__name__}")
        return [str(v).strip() for v in value if str(v).strip()]
    if key == "signature":
        return str(value or "").strip()
    if key == "usage":
        return str(value or "").strip() or DEFAULTS["usage"]
    raise UnknownPreference(key)          # 不会到：上面已经穷举了 DEFAULTS


def get_preferences(user: str = "", *, path: Path | None = None) -> dict:
    """这位使用者当前的全部设置。没设置过就是一整份默认值。

    `user` 空串是合法的——单机使用时没有登录态，全部设置存在同一个"匿名"
    桶里。这不是权限模型，是"这台机器上这个人"。
    """
    rows = [r for r in _read(path or PREFERENCES_PATH) if r.get("user", "") == (user or "")]
    out = dict(DEFAULTS)
    out["avoid_herbs"] = list(DEFAULTS["avoid_herbs"])
    for r in rows:                         # 后写的覆盖先写的
        for k, v in (r.get("prefs") or {}).items():
            if k in DEFAULTS:
                out[k] = v
    return out


def set_preferences(user: str = "", *, path: Path | None = None, **changes) -> dict:
    """改几项设置，返回改完之后的完整一份。

    **全部校验通过才写**：一次请求里有一项值非法就整批不写，而不是写进去
    一半——界面上那个面板是一次提交多项的，写一半会让"我改了三项，只生效了
    两项"这种事发生得悄无声息。
    """
    checked = {k: _check(k, v) for k, v in changes.items()}
    # 换了剂型、又没同时指定煎服法时，煎服法跟着换成新剂型的默认。
    # **只在当前值还是某个剂型的默认值时才跟着换**——医师手写过一句自己的
    # 煎服法，换剂型不该把它冲掉。判据是"当前这句在不在 USAGE_CHOICES 里"，
    # 不是记一个"用户改过没有"的标记：那个标记会跟实际值分叉。
    if "dosage_form" in checked and "usage" not in checked:
        cur = get_preferences(user, path=path).get("usage", "")
        if any(cur in group for group in USAGE_CHOICES.values()):
            checked["usage"] = default_usage_for(checked["dosage_form"])
    if checked:
        _append(path or PREFERENCES_PATH, {"user": user or "", "prefs": checked})
    return get_preferences(user, path=path)


def preferences_prompt_text(prefs: dict | None) -> str:
    """把用药习惯翻成给模型看的约束文本（§6.6）。

    **只翻真正影响用药的那两项**（药味数区间、忌用药）。字号、署名、默认剂数
    这些是界面与文书的事，摆进 prompt 只会稀释真正的约束——模型看到的每一句
    都在跟别的句子抢注意力。
    """
    p = prefs or {}
    lines: list[str] = []
    band = p.get("herb_count_band")
    if band:
        lines.append(f"- 这位医师习惯开 {band} 味的方，给建议时按这个规模来")
    avoid = [a for a in (p.get("avoid_herbs") or []) if a]
    if avoid:
        lines.append(f"- 忌用药：{'、'.join(avoid)}。这几味**不要出现在任何建议里**")
    form = p.get("dosage_form")
    if form and form != DEFAULTS["dosage_form"]:
        lines.append(f"- 默认剂型是{form}，用法按这个剂型写")
    return "\n".join(lines)
