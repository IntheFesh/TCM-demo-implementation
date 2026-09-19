"""R63 §1：经典方候选——按**当前辨证目标**筛方剂本体，并把组成连剂量一起给出。

## 为什么要有这一层

R62 的组方实验室里「从经典方开始」拿的是 `/api/knowledge/search?kind=formula`
的搜索结果，搜索词只是证型名或治法**当字面关键词**扔进去。实测的后果是：
脾胃门的证型选好之后弹出来的是麻黄汤、大青龙汤、桂枝汤这一串解表剂
——按方名相似度排出来的，跟当前这个证一点关系没有。按钮因此成了摆设。

这里回答的问题换了：**"这个证 / 这个治法 / 这个病位，教材里的经典方是哪几张"**。
判据分三层，每一层都说得出理由，说不出理由的方不进候选。

## 一个都匹配不上时不给"随便几张"

匹配不到就返回空列表加一句话，让界面提示按方名搜索。给一串不相关的方比
什么都不给更糟：它看起来像系统给的建议，而那是没有依据的建议。
（CLAUDE.md：工具返回空必须能区分"参数错了""数据不在""确实没匹配"三种情况
——这里三种分别是 `error` / `available=False` / `items=[] 且 note 说明已查 N 首`。）

## 判据全部复用既有实现

- 主治含证型 → `Ontology.formulas_for_syndrome`（证名去尾「证」那一步在那边）
- 功用对治法 → `core.guideline_compare.principle_matches`（治法↔功效同义表，
  跟 R34 的 `effect_matches_method` 同一处实现）
- 组成与剂量 → `Ontology.formula().composition`（`parse_composition` 的产物）
- 剂量原文 → 克数 → `core.ontology.parse_dose_g`

本模块**不持有任何一张自己的方剂表或同义词表**。
"""
from __future__ import annotations

from core.guideline_compare import principle_matches
from core.ontology import parse_dose_g

#: 候选上限。§1.3 定的 12——再多一屏放不下，而 chip 一多"按理由挑"就退化成乱点。
MAX_CANDIDATES = 12

#: 匹配层级。数字小的优先，同一张方只保留最强的那条理由。
TIER_INDICATION = 1     # 主治含当前证型
TIER_FUNCTION = 2       # 功用对得上当前治法
TIER_LOCUS = 3          # 主治含当前病位
TIER_NAME = 0           # 按方名搜（用户明确点名，压过一切推断）


#: 组成里出现过、但**不是药名**的段。全部出自对 235 首方 372 个短条目的实测，
#: 清一色是炮制、修治与遗留碎片。表里刻意**不收**任何可能是药名异写的写法
#: （「银花」「芥穗」「苇根」「干葛」这类归 `HERB_ALIASES` 管，不在这里丢）。
_NOT_A_HERB: frozenset[str] = frozenset({
    "各", "如无", "干用", "黄色", "白者", "二分或", "一半筛", "两仁者",
    "去皮", "去白", "去壳", "去皮尖", "去皮脐", "去脐用", "去瓣", "去面",
    "麸炒", "微炒", "炒珠", "炒黄", "晒干", "蒸熟", "炙干", "煅粉",
    "敲破", "砸碎", "细锉", "捣细", "海捣细", "石捶碎", "研如泥", "醋研",
    "绵裹", "水渍", "汁炒", "汁淹", "汁炙熟", "别作脂",
    "刷去土", "香去土", "泥固济", "同焰硝", "火红",
})


def _is_not_a_herb(name: str, ontology) -> bool:
    """这一段是不是修治说明或剂量碎片。**正面认定，认不出就当药名放过。**"""
    from core.ontology import _DOSE_ONLY_RE
    n = (name or "").strip()
    if not n:
        return True
    if _DOSE_ONLY_RE.match(n):
        return True
    if n in _NOT_A_HERB:
        return True
    # 单字且本草层里没有这味药：「擘」「碎」「炙」「心」「土」「尖」这一类。
    # 单字真药（若有）几乎都在本草层里，所以这一条不会误伤。
    return len(n) == 1 and ontology.herb(n) is None


def _composition_items(formula, ontology) -> list[dict]:
    """`Formula.composition` → 处方表能直接吃的行。

    **剂量照带，并标明它是原方剂量**（§1.3 第 1、2 条）。R62 的实现刻意把剂量
    抹掉了，顾虑是"教材剂量是原方的，照抄会让人以为那是这一次的判断"——顾虑
    对，结论错：导入一张空剂量的方等于只省了打药名。正确做法是带上并标出
    `dose_is_original`，界面写着「原方」，医师改一下标记就消失。

    **古制不换算**（钱/两/枚/粒）：`dose` 留 None，原文进 `dose_text`。
    换算需要朝代与度量衡的判断，给一个错的克数比留空危险得多
    ——这跟 `parse_composition` 留原文是同一条理由。实测本项目的方剂本体来自
    古籍原文，594 条剂量**一条都不是克**，所以这条不是边角情况而是常态。

    组成里混进来的碎片不进处方表。**判据是"能正面认定它不是药名"**，
    不是"本草层查不到就丢"——后者试过，代价不可接受：本草层只有 1232 味，
    查不到的 2 字条目里既有「各」「去皮」也有「银花」「芥穗」（本草层没收的
    药名异写），一起丢掉等于从处方里静默抹掉真药，比多显示一行「各」危险。

    所以只丢两类，两类都是正面认定的：
      1. 整段就是一个剂量（复用 `_DOSE_ONLY_RE`）——「一升」「半斤」「二合」；
      2. 整段是炮制/修治说明（`_NOT_A_HERB` 这张表，或单字且本草层无此药）
         ——「去皮」「麸炒」「绵裹」「擘」「碎」。

    `_NOT_A_HERB` 是新表，但它回答的是一个**此前没人回答过的问题**：
    "组成里这一段是药名还是修治说明"。`HERB_ALIASES` 回答"这个写法归到哪味药"，
    本草层回答"本项目收没收这味药"——都不是这个问题
    （CLAUDE.md 第 31 条的例外要写清区别）。表里每一项都出自对 235 首方实测
    的 372 个短条目，不是凭印象列的。
    """
    out: list[dict] = []
    for name, dose_text in (formula.composition or ()):
        if _is_not_a_herb(name, ontology):
            continue
        g = parse_dose_g(dose_text)
        out.append({
            "name": name,
            "dose": g,
            "dose_unit": "g",
            "dose_text": dose_text or "",
            # 只在真的带了剂量时才标「原方」：没剂量的那一味标了等于说谎。
            "dose_is_original": bool(dose_text),
        })
    return out


def _first_span(formula) -> tuple[str, str]:
    for pred in ("主治", "功用", "组成"):
        for r in formula.refs.get(pred, ()):
            if r.span:
                return r.book or "", r.span
    return "", ""


def _entry(formula, tier: int, reason: str, ontology) -> dict:
    book, span = _first_span(formula)
    return {
        "name": formula.name,
        "tier": tier,
        "reason": reason,
        "composition": _composition_items(formula, ontology),
        "functions": list(formula.functions),
        "indications": list(formula.indications),
        "book": book,
        "span": span,
    }


def _quote(text: str, limit: int = 14) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"


def candidates(*, syndrome: str = "", method: str = "", locus: str = "",
               query: str = "", limit: int = MAX_CANDIDATES,
               ontology=None) -> dict:
    """当前辨证目标下的经典方候选。零 LLM，全部现查。

    `query` 非空时按**方名**搜，并且压过证型/治法/病位的推断——用户点名要找
    哪张方，就不该再被"这张方跟你的证不搭"挡回去。
    """
    if ontology is None:
        from core.ontology import get_ontology
        ontology = get_ontology()
    if not getattr(ontology, "available", False):
        return {"available": False, "items": [], "n": 0, "n_scanned": 0,
                "note": "方剂数据尚未加载，无法检索经典方。"}

    all_formulas = list(ontology.formulas.values())
    picked: dict[str, dict] = {}

    def offer(f, tier: int, reason: str) -> None:
        old = picked.get(f.name)
        if old is None or tier < old["tier"]:
            picked[f.name] = _entry(f, tier, reason, ontology)

    q = (query or "").strip()
    if q:
        for f in all_formulas:
            if q in f.name:
                offer(f, TIER_NAME, f"方名含「{q}」")

    syn = (syndrome or "").strip()
    if syn:
        for f in ontology.formulas_for_syndrome(syn):
            hit = next((i for i in f.indications if syn.removesuffix("证") in i), "")
            offer(f, TIER_INDICATION, f"主治「{_quote(hit)}」")

    mth = (method or "").strip()
    if mth:
        for f in all_formulas:
            hit = next((fn for fn in f.functions if principle_matches(mth, fn)), "")
            if hit:
                offer(f, TIER_FUNCTION, f"功用「{_quote(hit)}」对应治法「{_quote(mth)}」")

    loc = (locus or "").strip()
    if loc:
        for part in _locus_terms(loc):
            for f in all_formulas:
                hit = next((i for i in f.indications if part in i), "")
                if hit:
                    offer(f, TIER_LOCUS, f"主治「{_quote(hit)}」涉及{part}")

    items = sorted(picked.values(), key=lambda e: (e["tier"], -len(e["composition"]), e["name"]))
    items = items[: max(1, int(limit or MAX_CANDIDATES))]
    n_scanned = len(all_formulas)
    if not items:
        note = (f"已查 {n_scanned} 首方，未匹配到与当前"
                f"{'证型' if syn else ('治法' if mth else '目标')}相关的经典方。"
                "可用下面的搜索框按方名查找。")
    else:
        note = ""
    return {"available": True, "items": items, "n": len(items),
            "n_scanned": n_scanned, "note": note}


def _locus_terms(locus: str) -> list[str]:
    """病位文本 → 可比的脏腑词。

    `node_explain` 给的病位是「脾、胃」或「脾胃」这类写法，逐字拿去比会把
    「脾胃」整体当一个词而主治里写的是「脾虚」。切成单脏腑再比。
    判据表复用本体的 `MERIDIANS`——那张表就是这个项目的脏腑词表，
    不另起一份（CLAUDE.md 第 31 条）。
    """
    from core.ontology import MERIDIANS
    return [m for m in MERIDIANS if m in (locus or "")]
