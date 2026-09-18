"""R37：图上任一节点的「释义」——**零 LLM 调用**，四节，取不到就说取不到。

## 为什么是四节，而且固定

问诊图上点开一个节点，人想知道的是四件事，顺序也是固定的：

| # | 节 | 回答 | 数据来源 |
|---|---|---|---|
| 1 | 是什么 | 这个东西本身的定义 | 证候表 / 本草本体 / 方剂本体 / 证素表 |
| 2 | 出处原文 | 上一节那句话出自哪 | 本体三元组的 `source_span` + `book` |
| 3 | 名医怎么用 | 五家在医案里怎么用它 | R35 的 `prescribing_patterns.jsonl` |
| 4 | 注意 | 剂量上限 / 十八反 / 要单煎先煎 | `core.safety_output`（同一张表） |

**固定顺序不是排版偏好**：它是"先说是什么、再说凭什么、再说别人怎么用、
最后说风险"这条链。顺序一乱，读者会把"名医这么用过"当成"所以可以这么用"。

## 三条纪律

**一、零 LLM。** 释义全部来自本地数据。让模型现编一段解释是这个项目从头到尾
在防的那件事——一句没有出处的解释在这种场合的代价最大。

**二、取不到就 `available=False`，前端整块隐藏。** 不编一句"暂无更多信息"：
那句话占着位置、看起来像是查过了。四节里某一节空着就只是不返回那一节
（`sections` 里没有它），不是返回一个空壳。

**三、判据全部复用**。药名归一走 `core.herbs.normalize_herb`，剂量上限走
`core.safety_output.dose_limit_entry`，十八反走 `INCOMPATIBLE_PAIRS`，
证候定义走 `core.tools._load_standard()` 读的那一份表——一个都不另写
（CLAUDE.md 第 31 条）。
"""
from __future__ import annotations

from typing import Literal

NodeKind = Literal["symptom", "element", "syndrome", "formula", "herb", "case", "unknown"]

#: 节点 id 前缀 → 种类。问诊图（`api.main.to_graph`）与图谱浏览器
#: （`api.main._persistent_graph_to_cytoscape`）用的是同一套前缀，所以这张表
#: 两边共用，不各写一份。
_PREFIX_KIND: dict[str, NodeKind] = {
    "sym": "symptom",
    "elem": "element",
    "syn": "syndrome",
    "formula": "formula",
    "herb": "herb",
    "case": "case",
}

#: 四节的顺序与标题。**顺序是链条，不是排版偏好**（见模块文档）。
SECTION_ORDER = ("是什么", "出处原文", "名医怎么用", "注意")


def parse_node_id(node_id: str) -> tuple[NodeKind, str]:
    """`herb::synthesis::四君子汤::党参` → `("herb", "党参")`。

    **取最后一段作为名字**：问诊图的 id 里带医家与方名（那是为了同一味药在
    不同方里不被去重合并，见 `to_graph` 的注释），而释义要的是那味药本身。
    认不出前缀时返回 `("unknown", 原串)`——不猜，让上层如实报 available=False。
    """
    raw = (node_id or "").strip()
    if not raw:
        return "unknown", ""
    parts = raw.split("::")
    if len(parts) < 2:
        return "unknown", raw
    kind = _PREFIX_KIND.get(parts[0])
    if kind is None:
        return "unknown", raw
    return kind, parts[-1].strip()


def syndrome_row(name: str) -> dict | None:
    """证候表里那一条。**先精确匹配名字，再去掉尾「证」匹配一次**——
    跟 `Ontology.formulas_for_syndrome` 同一条规矩（那里也是这么去尾字的）。"""
    from core.tools import _load_standard

    rows = _load_standard() or []
    if not rows:
        return None
    exact = [r for r in rows if (r.get("name") or "") == name]
    if exact:
        return exact[0]
    stem = name.removesuffix("证")
    loose = [r for r in rows if (r.get("name") or "").removesuffix("证") == stem]
    return loose[0] if loose else None


def _section(heading: str, lines: list[str], source: str | None = None) -> dict | None:
    """一节。**空行全部滤掉；滤完没内容就返回 None**（那一节不出现）。"""
    kept = [ln for ln in (lines or []) if ln and ln.strip()]
    if not kept:
        return None
    out = {"heading": heading, "lines": kept}
    if source:
        out["source"] = source
    return out


def _herb_sections(name: str, *, ontology=None, patterns_limit: int = 3) -> list[dict]:
    from core.herbs import normalize_herb
    from core.ontology import get_ontology
    from core.safety_output import INCOMPATIBLE_PAIRS, dose_limit_entry, normalize_for_incompat

    ont = ontology if ontology is not None else get_ontology()
    norm = normalize_herb(name) or name
    herb = ont.herb(norm)
    out: list[dict | None] = []

    if herb is not None:
        what = []
        if herb.nature or herb.flavor:
            what.append(f"性味：{herb.nature or '-'}｜{'、'.join(herb.flavor) or '-'}")
        if herb.meridians:
            what.append(f"归经：{'、'.join(sorted(herb.meridians))}")
        if herb.effects:
            what.append(f"功效：{'、'.join(herb.effects)}")
        out.append(_section("是什么", what, source="本草本体"))
        spans = []
        for pred in ("性味", "归经", "功效", "用量"):
            for ref in (herb.refs.get(pred) or [])[:1]:
                if ref.span.strip():
                    spans.append(f"{pred}（{ref.book or '本草'}）：{ref.span.strip()}")
        out.append(_section("出处原文", spans, source="药理层三元组"))
    else:
        # 本体里没有这味药**不是"没有这味药"**，是本体覆盖不全（实测按种数 39.8%）。
        # 这句话必须说出来，否则读者会以为系统认为这味药不存在。
        out.append(_section(
            "是什么",
            [f"「{norm}」不在本项目的本草本体里（本体覆盖医案语料里的药名约四成，"
             "见 eval/RESULTS.md 的「本体对医案语料的覆盖」一行）。"
             "这不代表这味药不存在，只代表这里查不到它的性味归经。"],
            source="本体覆盖缺口"))

    # 三：名医怎么用（R35 的规律层）
    used = []
    pats = [p for p in _all_patterns(ont) if norm in (p.get("herbs") or [])]
    pats.sort(key=lambda p: -int(p.get("support") or 0))
    for p in pats[:patterns_limit]:
        who = p.get("physician_name") or p.get("physician")
        kind = p.get("kind")
        if kind == "dose" and p.get("dose_median_g") is not None:
            used.append(f"{who}：剂量中位数 {p['dose_median_g']}g"
                        f"（区间 {p.get('dose_min_g')}–{p.get('dose_max_g')}g，"
                        f"{p.get('support')} 张方）")
        elif kind == "herb_pair":
            other = [h for h in (p.get("herbs") or []) if h != norm]
            used.append(f"{who}：常与{'、'.join(other)}同用（{p.get('support')} 张方）")
        else:
            used.append(f"{who}：{p.get('note') or kind}（{p.get('support')} 张方）")
    out.append(_section("名医怎么用", used, source="本项目医案库统计（非教材）"))

    # 四：注意
    notes = []
    hit = dose_limit_entry(norm)
    if hit is not None:
        limit, reason = hit
        notes.append(f"常用量上限 {limit}g（{reason}）" if limit
                     else f"剂量另有规定：{reason}")
    key = normalize_for_incompat(norm)
    partners = sorted({h for pair in INCOMPATIBLE_PAIRS if key in pair
                       for h in pair if h != key})
    if partners:
        notes.append(f"十八反十九畏：不与{'、'.join(partners)}同用")
    if herb is not None and herb.preparation:
        notes.append(f"炮制：{'、'.join(herb.preparation)}")
    if not notes:
        notes.append("本项目的安全表里没有这味药的剂量上限或配伍禁忌条目——"
                     "**这是「查不到」，不是「没有风险」**。")
    out.append(_section("注意", notes, source="core/safety_output.py 同一张表"))
    return [s for s in out if s]


def _all_patterns(ont) -> list[dict]:
    """本体里那份规律表。走 `patterns_for("")`——空证名在那个接口里恒命中
    （见它的文档），所以这是"全部规律"的正规取法，不去碰它的私有字段。"""
    return ont.patterns_for("", physician=None)


def _syndrome_sections(name: str, *, ontology=None) -> list[dict]:
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    row = syndrome_row(name)
    out: list[dict | None] = []
    if row is not None:
        what = [row.get("definition") or ""]
        if row.get("location") or row.get("nature"):
            what.append(f"病位：{'、'.join(row.get('location') or []) or '-'}"
                        f"；病性：{'、'.join(row.get('nature') or []) or '-'}")
        if row.get("cardinal_symptoms"):
            what.append(f"主症：{'、'.join(row['cardinal_symptoms'])}")
        if row.get("secondary_symptoms"):
            what.append(f"次症：{'、'.join(row['secondary_symptoms'])}")
        if row.get("tongue_pulse"):
            what.append(f"舌脉：{row['tongue_pulse']}")
        out.append(_section("是什么", what, source=f"证候表 {row.get('code') or ''}".strip()))
        # 证候表的 `source` 是**来源标签**（official_consensus / textbook…），
        # 不是可以当引文读的原话——把它当"出处原文"显示出来，读者会以为那就是
        # 教材的原文。所以这一节报的是"这一条来自哪、编码是什么"，措辞照实。
        src = row.get("source") or "未标注"
        icd = row.get("icd11_code")
        out.append(_section("出处原文", [
            f"证候编码 {row.get('code') or '-'}｜来源标签：{src}"
            + (f"｜ICD-11：{icd}" if icd else ""),
            "（这一条是人工整理的证候参考表，不是逐字引文；教材原文见 books/ 下的源书）",
        ], source="data/standard/syndromes.jsonl"))
    else:
        out.append(_section("是什么", [
            f"「{name}」不在 data/standard/syndromes.jsonl 里。证候表现在收 "
            f"{len(_load_standard_rows())} 条，教材扩充还在进行（总纲阶段三）。"],
            source="证候表覆盖缺口"))
    # 三：这个证下五家怎么用药（证型档规律）
    used = []
    for p in ont.patterns_for(name, physician=None):
        if p.get("group_by") != "physician_syndrome":
            continue
        who = p.get("physician_name") or p.get("physician")
        used.append(f"{who}：{'、'.join(p.get('herbs') or [])}"
                    f"（{p.get('kind')}，{p.get('support')} 张方）")
        if len(used) >= 4:
            break
    out.append(_section("名医怎么用", used, source="本项目医案库统计（非教材）"))
    # 四：这个证对应的方（本体）
    formulas = [f.name for f in ont.formulas_for_syndrome(name)][:5]
    out.append(_section("注意", [
        f"方剂本体里主治含这个证的方：{'、'.join(formulas)}" if formulas else "",
        "证候表的定义是**教材口径**，医案里的用法可能更宽——两者不一致时以"
        "「出处原文」那一节为准。",
    ], source="方剂本体 + 口径说明"))
    return [s for s in out if s]


def _load_standard_rows() -> list[dict]:
    from core.tools import _load_standard

    return _load_standard() or []


def _formula_sections(name: str, *, ontology=None) -> list[dict]:
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    f = ont.formula(name)
    out: list[dict | None] = []
    if f is not None:
        what = []
        if f.composition:
            what.append("组成：" + "；".join(
                f"{n} {d}".strip() for n, d in f.composition))
        if f.functions:
            what.append(f"功用：{'、'.join(f.functions)}")
        if f.indications:
            what.append(f"主治：{'、'.join(f.indications)}")
        monarch = f.roles.get("君药") or ()
        if monarch:
            what.append(f"君药：{'、'.join(monarch)}")
        out.append(_section("是什么", what, source="方剂本体"))
        spans = []
        for pred in ("组成", "功用", "主治", "君药", "加减"):
            for ref in (f.refs.get(pred) or [])[:1]:
                if ref.span.strip():
                    spans.append(f"{pred}（{ref.book or '方剂'}）：{ref.span.strip()}")
        out.append(_section("出处原文", spans, source="药理层三元组"))
        out.append(_section("注意", [
            f"加减：{'；'.join(f.modifications)}" if f.modifications else "",
            "方剂本体现收 235 首（`python -m core.ontology --stats` 可核）——"
            "查不到一个方名**不等于这个方不存在**。",
        ], source="方剂本体 + 覆盖说明"))
    else:
        out.append(_section("是什么", [
            f"「{name}」不在方剂本体里（现收 235 首）。这不代表这个方不存在，"
            "只代表这里查不到它的组成与主治。"], source="本体覆盖缺口"))
    return [s for s in out if s]


def _element_sections(name: str) -> list[dict]:
    from core.elements import LOCATIONS, NATURES

    kind = "病位证素" if name in LOCATIONS else ("病性证素" if name in NATURES else None)
    rows = [r for r in _load_standard_rows()
            if name in (r.get("location") or []) or name in (r.get("nature") or [])]
    out: list[dict | None] = [_section("是什么", [
        f"{name}：{kind}" if kind else
        f"「{name}」不在证素词表里（病位 {len(LOCATIONS)} 个、病性 {len(NATURES)} 个）",
        f"证候表里有 {len(rows)} 条证候用到它" if rows else "",
    ], source="core/elements.py 词表")]
    out.append(_section("出处原文", [
        "；".join(f"{r.get('code')} {r.get('name')}" for r in rows[:6]),
    ], source="证候表"))
    return [s for s in out if s]


def _symptom_sections(name: str) -> list[dict]:
    rows = [r for r in _load_standard_rows()
            if name in (r.get("cardinal_symptoms") or [])
            or name in (r.get("secondary_symptoms") or [])]
    cardinal = [r for r in rows if name in (r.get("cardinal_symptoms") or [])]
    out: list[dict | None] = [_section("是什么", [
        f"症状条目「{name}」",
        f"证候表里 {len(rows)} 条证候提到它，其中 {len(cardinal)} 条把它列为主症"
        if rows else "证候表里没有证候提到它（可能是主诉里的自由表述，"
                     "S1 保留原样、没有并入标准条目）",
    ], source="证候表")]
    out.append(_section("名医怎么用", [
        "；".join(f"{r.get('name')}（{'主症' if name in (r.get('cardinal_symptoms') or []) else '次症'}）"
                  for r in rows[:6]),
    ], source="证候表"))
    return [s for s in out if s]


def explain_node(node_id: str, *, name: str | None = None, ontology=None) -> dict:
    """一个节点的四节释义。**零 LLM**。

    返回 `{"available": bool, "node": …, "kind": …, "title": …, "sections": [...]}`。
    `available=False` 时 `sections` 为空且带 `note` 说明为什么——前端整块隐藏，
    不显示一个"暂无信息"的空壳（见模块文档第二条纪律）。

    `name` 是**显示名覆盖**，不是可选的装饰：问诊图的证型节点 id 是
    `syn::{physician}`（那个 id 是证据链侧栏反查的键，改不得——见
    `api.main.to_graph` 的注释），名字只在 `label` 里。所以调用方（前端）
    把它手上的 label 一起传过来，这里优先用它。
    **种类仍然从 id 的前缀判**：名字可以覆盖，"这是什么东西"不行——
    让调用方同时决定这两件事的话，一个写错的前缀会静默走到另一条查询分支上。
    """
    kind, id_name = parse_node_id(node_id)
    # 覆盖名去掉「病名 · 证型」这种拼接（问诊图 layer 2 的 label 是拼出来的）：
    # 取最后一段——证候表里存的是证型名，不含病名前缀。
    override = (name or "").strip()
    if override:
        override = override.split("·")[-1].strip()
    name = override or id_name
    if not name or kind in ("unknown", "case"):
        note = ("认不出这个节点 id" if kind == "unknown"
                else "医案节点的释义就是医案本文，走证据链侧栏，不在这里重复一份")
        return {"available": False, "node": node_id, "kind": kind, "title": name,
                "sections": [], "note": note}
    builders = {
        "herb": lambda: _herb_sections(name, ontology=ontology),
        "formula": lambda: _formula_sections(name, ontology=ontology),
        "syndrome": lambda: _syndrome_sections(name, ontology=ontology),
        "element": lambda: _element_sections(name),
        "symptom": lambda: _symptom_sections(name),
    }
    sections = builders[kind]()
    # 固定顺序（见 SECTION_ORDER）：各 builder 自己的顺序已经是它，这里再排一次
    # 是为了"新增一节的人不必记得放对位置"。
    order = {h: i for i, h in enumerate(SECTION_ORDER)}
    sections.sort(key=lambda s: order.get(s["heading"], len(order)))
    return {
        "available": bool(sections),
        "node": node_id,
        "kind": kind,
        "title": name,
        "sections": sections,
        "note": None if sections else "本地数据里查不到这个节点的任何一节释义",
    }
