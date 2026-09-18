"""R46 §7.5：诊中知识检索面板——独立于问诊流程的四类速查。

医师在诊中要查的东西跟问诊链路上的检索**不是一回事**：问诊链路查的是"这条
主诉像哪几条医案"，而诊中速查是"黄连什么性味""柴胡疏肝散组成是什么""这个
证型教材推荐什么方""叶天士治这个证爱用什么"。后者是**查字典**，要的是快，
不是准召回。

所以这一层：
  - 不走向量检索、不调模型（那两样都到不了 200 ms）；
  - 直接查已经装载在内存里的本体与规律层；
  - 四类各自独立，**互不融合**——融合会让"我明明在查药"变成"返回了三条医案"。

响应预算 200 ms（§7.5 第 13 条）。本体是惰性加载的，**第一次查会把它装进来**
——所以预算算的是"装载之后"的每次查询，`tests/test_knowledge_panel.py` 里
先预热再计时，这一点写在那条测试里。
"""
from __future__ import annotations

from typing import Literal

#: 四类。**Literal 不是自由字符串**：前端按类别切页签，多一个拼错的类别
#: 会长出一个空页签。
KnowledgeKind = Literal["herb", "formula", "guideline", "pattern"]

KIND_LABEL: dict[str, str] = {
    "herb": "本草",
    "formula": "方剂",
    "guideline": "教材推荐方案",
    "pattern": "名老中医用药规律",
}

#: 一次返回多少条。**不是分页**：速查要的是"最像的那几条"，
#: 翻到第 3 页的人其实是该换个词重查。
DEFAULT_LIMIT = 8


def _match(text: str, q: str) -> bool:
    return bool(q) and q in (text or "")


def search_herbs(q: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    from core.ontology import get_ontology

    onto = get_ontology()
    out: list[dict] = []
    for name, herb in onto.herbs.items():
        if not (_match(name, q) or any(_match(a, q) for a in herb.aliases)
                or any(_match(e, q) for e in herb.effects)):
            continue
        refs = herb.refs.get("性味") or herb.refs.get("功效") or ()
        span = next((r.span for r in refs if r.span), "")
        book = next((r.book for r in refs if r.span), "")
        out.append({
            "kind": "herb", "title": name,
            "summary": "；".join(x for x in (
                herb.nature and f"性味{herb.nature}",
                herb.meridians and "归" + "、".join(sorted(herb.meridians)) + "经",
                herb.effects and "功效" + "、".join(herb.effects[:4]),
            ) if x),
            "source": book, "span": span,
        })
        if len(out) >= limit:
            break
    return out


def search_formulas(q: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    from core.ontology import get_ontology

    onto = get_ontology()
    out: list[dict] = []
    for name, f in onto.formulas.items():
        if not (_match(name, q) or any(_match(i, q) for i in f.indications)
                or any(_match(x, q) for x in f.functions)):
            continue
        refs = f.refs.get("功用") or f.refs.get("主治") or ()
        span = next((r.span for r in refs if r.span), "")
        book = next((r.book for r in refs if r.span), "")
        out.append({
            "kind": "formula", "title": name,
            "summary": "；".join(x for x in (
                f.functions and "功用" + "、".join(f.functions),
                f.indications and "主治" + "、".join(f.indications),
                f.composition and "组成" + "、".join(n for n, _d in f.composition[:8]),
            ) if x),
            "source": book, "span": span,
        })
        if len(out) >= limit:
            break
    return out


def search_guidelines(q: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    from core.guideline_compare import BASIS_LABEL, load_guidelines

    out: list[dict] = []
    for e in load_guidelines():
        if not (_match(e.syndrome, q) or _match(e.recommended_formula, q)
                or _match(e.recommended_principle, q)):
            continue
        out.append({
            "kind": "guideline", "title": f"{e.syndrome} → {e.recommended_formula}",
            "summary": f"{BASIS_LABEL}：治法{e.recommended_principle or '（未记）'}",
            "source": e.source, "span": e.span,
        })
        if len(out) >= limit:
            break
    return out


def search_patterns(q: str, limit: int = DEFAULT_LIMIT, physician: str = "") -> list[dict]:
    """名老中医用药规律。**physician 过滤走 `resolve_physician_id`**——
    模型和人都可能填中文名，而底层数据存的是 id（SOURCES.md 第 31 条那个坑）。
    """
    from core.ontology import get_ontology
    from core.physicians import resolve_physician_id

    pid = resolve_physician_id(physician) if physician else ""
    out: list[dict] = []
    # `patterns_for("")` = 不按证型过滤（空串是任何 group 的子串，见那个方法的
    # 注释）。**不给 Ontology 另加一个"全部规律"的取法**——那会是同一件事的
    # 第二处实现，而这里要的只是"先全拿出来再按词筛"。
    for p in get_ontology().patterns_for("", pid or None):
        hay = f"{p.get('physician', '')}{p.get('group_value', '')}{p.get('value', '')}"
        if not _match(hay, q):
            continue
        out.append({
            "kind": "pattern",
            "title": f"{p.get('physician', '')}·{p.get('value', '')}",
            "summary": (f"{p.get('kind', '')}；支持 {p.get('support', 0)} 张方"
                        f"{'；' + str(p.get('group_value')) if p.get('group_value') else ''}"),
            "source": "医案统计", "span": "、".join((p.get("case_ids") or [])[:5]),
        })
        if len(out) >= limit:
            break
    return out


_SEARCHERS = {
    "herb": search_herbs,
    "formula": search_formulas,
    "guideline": search_guidelines,
    "pattern": search_patterns,
}


def search(q: str, kind: str = "", limit: int = DEFAULT_LIMIT,
           physician: str = "") -> dict:
    """速查。`kind` 为空时四类各查一遍。

    返回里**分类别摆**，不混成一个列表：混起来之后"我在查药"就变成了
    "系统返回了一堆东西"，而医师得自己在里面找哪几条是药。
    """
    q = (q or "").strip()
    if not q:
        return {"query": "", "groups": [], "note": "输入要查的药名、方名、证型或医家名"}
    kinds = [kind] if kind in _SEARCHERS else list(_SEARCHERS)
    groups = []
    for k in kinds:
        fn = _SEARCHERS[k]
        rows = fn(q, limit, physician) if k == "pattern" else fn(q, limit)
        groups.append({"kind": k, "label": KIND_LABEL[k], "items": rows,
                       "n": len(rows)})
    total = sum(g["n"] for g in groups)
    return {
        "query": q,
        "groups": groups,
        # 三分法：查到了 / 查了但没有 / 这一类数据不在。前两者的区别写在 note 里。
        "note": "" if total else f"四类知识里都没有匹配「{q}」的条目（已查本草、方剂、教材推荐方案、用药规律）",
    }
