"""产品模式：正式版（`PRODUCT_MODE=1`，默认）与内部研究版（`PRODUCT_MODE=0`）
的**唯一分派点**。

**能力不删，产品面不露。** 这个模块一行代码都不删：研究端点、对照模式、评测
脚本、分歧度读数全部留在仓库里（它们是申报材料与后续研究的资产）。它只回答
一个问题——这一次运行该不该把它们露出来。删代码不可逆，藏起来可逆。

**为什么是一个模块而不是各处 `os.environ.get("PRODUCT_MODE")`。**
要藏的东西有十六条，散着判断的话以后加第十七条会漏掉其中几处，而漏掉的那
几处恰恰是没人盯着的角落（顶栏折叠起来的那一块、某个角色下才出现的面板）。
这跟 CLAUDE.md「同一概念的匹配逻辑只能有一处实现」是同一条：判据是"这个
判断此前有没有人做过"。

**内部功能在产品模式下被调用即抛错，不静默返回空。** 静默是 demo 味的来源：
一个"点了没反应"的按钮比一个明确说"这里没有这个功能"的 404 更难查，也更像
半成品。所以 `require_internal()` 抛 `InternalOnly`，由 HTTP 层翻成 404
（**不是 403**——403 承认这个端点存在，404 连存在性都不暴露）。
"""
from __future__ import annotations

import os

PRODUCT_MODE_ENV = "PRODUCT_MODE"

#: 认得的真假词。跟 `core/llm.py` 的同名私有常量刻意各留一份：那边回答的是
#: "这个开关怎么解析"，两边合并成一处会让 core.llm 反过来依赖产品模式模块
#: ——而产品模式要读 `core.llm` 的后端名（见 api 层），成环。走 CLAUDE.md
#: 「同一概念只能有一处实现」的那条例外：两处回答的不是同一个问题。
_TRUE_WORDS = ("1", "true", "yes", "on")
_FALSE_WORDS = ("0", "false", "no", "off")

#: 内部功能清单：id → 中文说明。**清单在这里，不在各个调用点**——
#: `require_internal("byok")` 写错一个字母时要当场炸，不能悄悄放行。
INTERNAL_FEATURES: dict[str, str] = {
    "usage_dashboard": "用量看板与共享额度展示",
    "byok": "访问者自带 API key",
    "retriever_mode_picker": "检索模式选择",
    "role_researcher": "研究者角色（完整信息）",
    "legacy_compare": "对照模式（多家并列三列）",
    "divergence_readout": "分歧度与噪声地板读数",
    "run_manifest": "运行清单（模型、调用次数、耗时）",
    "token_panel": "上下文与缓存命中面板",
    "eval_endpoints": "评测端点",
    "debug_endpoints": "内部诊断端点",
}

#: 全部角色。产品面只剩前三个——「研究者（完整信息）」那一档在
#: `PRODUCT_MODE=0` 下照旧可用，能力没删。
PRODUCT_ROLES: tuple[str, ...] = ("doctor", "student", "patient")
INTERNAL_ROLES: tuple[str, ...] = ("researcher",)
ALL_ROLES: tuple[str, ...] = PRODUCT_ROLES + INTERNAL_ROLES

#: 角色的产品面名字。「医师」不是「医生」——见 docs/glossary.md，
#: 全站术语只认一种写法。
ROLE_LABEL: dict[str, str] = {
    "doctor": "医师",
    "student": "学生",
    "patient": "患者",
    "researcher": "研究者（完整信息）",
}


class InternalOnly(RuntimeError):
    """内部功能在产品模式下被调用。

    带 `feature`，HTTP 层据此翻成 404 并写日志——日志里要能看出**是哪一项**
    被从产品面调到了，否则查起来只知道"有人打了个不存在的地址"。
    """

    def __init__(self, feature: str) -> None:
        self.feature = feature
        what = INTERNAL_FEATURES.get(feature, feature)
        super().__init__(f"「{what}」是内部功能，产品模式（PRODUCT_MODE=1）下不提供")


def is_product_mode() -> bool:
    """默认 **True**：正式版是默认形态，内部研究版才要显式关掉。

    认不出的值按默认走并打一句 stderr，不抛异常——一个拼错的环境变量不该
    让整个服务起不来，但也不能让它悄悄改变产品形态（这正是 `EVAL_MODE`
    那条教训：静默生效的开关会在演示当天才被发现）。
    """
    raw = (os.environ.get(PRODUCT_MODE_ENV) or "").strip().lower()
    if raw == "":
        return True
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    import sys

    print(f"[product_mode] 认不出 {PRODUCT_MODE_ENV}={raw!r}，"
          f"按产品模式处理（可用值：{'/'.join(_TRUE_WORDS)} 或 "
          f"{'/'.join(_FALSE_WORDS)}）", file=sys.stderr)
    return True


def require_internal(feature: str) -> None:
    """内部功能的入口守卫。产品模式下抛 `InternalOnly`，内部模式下放行。

    `feature` 必须在 `INTERNAL_FEATURES` 里——写错名字当场 `KeyError`，
    不是悄悄放行。一个永远放行的守卫比没有守卫更糟：它看起来像守着。
    """
    if feature not in INTERNAL_FEATURES:
        raise KeyError(
            f"未登记的内部功能 {feature!r}；先加进 INTERNAL_FEATURES，"
            f"当前有：{'、'.join(INTERNAL_FEATURES)}")
    if is_product_mode():
        raise InternalOnly(feature)


def available_roles() -> tuple[str, ...]:
    """这一次运行里**界面上能选**的角色。"""
    return PRODUCT_ROLES if is_product_mode() else ALL_ROLES


def default_role() -> str:
    """请求没带 role 时用哪一个。

    产品模式下是「医师」——它是三甲里真正的使用者。内部模式保持
    `researcher`（此前所有研究侧测试与脚本依赖这个默认值，改它等于在一轮
    产品化里顺手改掉研究侧的默认行为，那是两件事）。
    """
    return "doctor" if is_product_mode() else "researcher"


def resolve_role(raw: str | None) -> str:
    """角色标识的**唯一解析入口**（CLAUDE.md「边界上统一解析」）。

    三种结果分得清清楚楚：
      - 空 → 这次运行的默认角色
      - 产品模式下的 `researcher` → `InternalOnly`（能力还在，产品面不给）
      - 认不出的字符串 → `ValueError`，把可用值列出来让调用方自我纠正
    """
    if raw is None or str(raw).strip() == "":
        return default_role()
    role = str(raw).strip().lower()
    if role not in ALL_ROLES:
        raise ValueError(f"认不出的角色 {raw!r}；可用：{'、'.join(available_roles())}")
    if role in INTERNAL_ROLES and is_product_mode():
        raise InternalOnly("role_researcher")
    return role


def product_flags() -> dict:
    """下发给前端的那一份。**前端不自己读环境变量**（它读不到），
    也不按 URL 猜——产品形态由服务端说了算，前端只是把它应用到 DOM 上。
    """
    return {
        "product_mode": is_product_mode(),
        "roles": [{"id": r, "label": ROLE_LABEL[r]} for r in available_roles()],
        "default_role": default_role(),
    }
