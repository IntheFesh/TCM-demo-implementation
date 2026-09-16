"""R23：方剂层的「建议」——比安全层更软的一层，同时给 R22 的 best-of-N 一把排序尺。

## 为什么要有第二层，而不是把规则加进 core/safety_output.py

安全层回答的是「**这方能不能发出去**」：十八反十九畏、超药典剂量，命中就
`blocking`，`run_physician` 会把问题回灌给模型重开一次。
这一层回答的是「**多张候选方里哪张拟得更好**」：缺引经药、性味功效重复、
寒热方向，这些都不该拦截（寒热错杂本来就寒热并用，"重复用药"有时是有意的
相须相使），但它们能把三张候选方排出先后。

CLAUDE.md「同一概念只能有一处实现」的例外条款说得很清楚：两处回答的**不是
同一个问题**时才允许分开，而且要在代码里写清区别。这里就是那种情况——
把两层合成一张表，以后调打分权重（排序需求）会连带改动拦截判据（病人安全），
而那两件事应该能各自单独改。

所以这一层**不重新实现任何判据**：五条规则里有三条直接调安全层的函数
（`check_incompatible` / `check_dose_limits` / `check_thermal_consistency`），
剩下两条（归经、性味功效）读的是药理层三元组，走 `build_entry_index` 那一处。

## 缺数据时的三分法

归经和性味功效两条规则要 `data/standard/materia_medica.jsonl`，而那份文件是
AutoDL 上跑真实 LLM 抽取才有的。沙盒里没有它 —— 这时**不是静默少两条建议**，
而是在 `FormulaCheck.skipped` 里如实列出「哪条规则没跑、为什么、怎么才能跑」。
三种情况必须分得开（SOURCES.md 第 31 条的老教训）：
  1. 数据文件不存在 → `skipped` 里带 `available: false` 和产物路径
  2. 数据在、这味药不在表里 → `skipped` 里带「已查 N 味」，不是"没问题"
  3. 数据在、查过了、确实没问题 → advice 里没有这一类，也没有 skipped
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.context_prefix import build_entry_index
from core.data_paths import pharmacology_read_path, pharmacology_write_path
from core.elements import LOCATIONS
from core.herbs import normalize_herb
from core.safety_output import (
    SHIBAFAN,
    check_dose_limits,
    check_incompatible,
    check_thermal_consistency,
)
from core.schemas import Advice, HerbItem

# 药理层里这三个谓词是这一层要读的。谓词名不在这里另起一套——跟
# core/context_prefix.py 的 MATERIA_QUICK_PREDICATES 用的是同一批字面值，
# 它们都来自抽取脚本写进 .jsonl 的 p 字段。
CHANNEL_PREDICATE = "归经"
DUPLICATE_PREDICATES = ("性味", "功效")

# 两味药的性味功效术语集合重合到这个比例就算「重复」。分母取**较小**的那个集合：
# 一味只标了 2 个功效的药和一味标了 8 个的药，如果那 2 个全被覆盖，
# 前者在这张方里就是多余的——按并集算（Jaccard）会因为分母被 8 撑大而漏掉它。
DUPLICATE_OVERLAP_GATE = 0.6

# 权重：**一处定义**，score_formula 和前端的排序都问这里。
# 比例只保证一件事——配伍禁忌永远比重复用药严重。绝对值没有临床含义。
ADVICE_WEIGHTS: dict[str, float] = {
    "incompatible": 1.0,
    "over_dose": 0.5,
    "thermal_mismatch": 0.3,
    "missing_channel_guide": 0.15,
    "duplicate_effect": 0.1,
}

SEVERITY_BY_KIND: dict[str, str] = {
    "incompatible": "blocking",
    "over_dose": "blocking",
    "thermal_mismatch": "warning",
    "missing_channel_guide": "suggestion",
    "duplicate_effect": "suggestion",
}

MATERIA_KIND = "materia_medica"
_TERM_SPLIT_RE = re.compile(r"[、，,；;。\s]+")
# 「归脾、胃经」这类写法里的框架字，切完要去掉，否则每味药都带一个「归」「经」
# 术语，任意两味药的重合度都被抬高。
_CHANNEL_FRAME = ("归", "经", "入")


def _terms(values: list[str]) -> set[str]:
    """把药理层里那种「疏肝解郁、健脾和中」的值切成术语集合。

    这个切分器**跟 core/syndrome_norm.py 回答的不是同一个问题**：那边回答
    「这个词属于哪个证候门类」（水肿≡肿胀），这边只是把一串顿号分隔的药性术语
    拆开，不做任何同义归一——药性术语的同义表这个项目还没有，编一张出来会让
    「重复用药」这条建议建立在一个没人核过的表上。所以这里只切不归一，
    并如实承认：写法不同的同义功效（"健脾"vs"补脾"）这条规则目前抓不到。
    """
    out: set[str] = set()
    for value in values:
        for tok in _TERM_SPLIT_RE.split(value or ""):
            tok = tok.strip()
            if len(tok) >= 2:
                out.add(tok)
    return out


def _channel_terms(values: list[str]) -> set[str]:
    """归经的值切成脏腑集合：「归脾、胃经」→ {脾, 胃}。"""
    out: set[str] = set()
    for tok in _terms(values) | {t for v in values for t in _TERM_SPLIT_RE.split(v or "")}:
        cleaned = tok
        for frame in _CHANNEL_FRAME:
            cleaned = cleaned.replace(frame, "")
        cleaned = cleaned.strip()
        if cleaned:
            out.add(cleaned)
    return out


def syndrome_channels(syndrome: str) -> list[str]:
    """证型名里出现的病位。**复用 core.elements.LOCATIONS**，不另写一张脏腑表——
    证素抽取、证素索引、这一层三处问的是同一个问题「这个词是哪个病位」。

    顺序按 LOCATIONS 里的顺序，不按在证型名里出现的先后：advice 的内容要
    对同一个证型永远一样（`score_formula` 的结果才是确定的）。
    """
    return [loc for loc in LOCATIONS if loc in (syndrome or "")]


def materia_index(materia: dict | None = None) -> dict | None:
    """{药名: {谓词: [值…]}}，数据文件不存在时返回 None（不是空 dict）。

    None 和 `{}` 必须分开：前者是"这张表还没建出来"，后者是"表建好了但是空的"。
    调用方据此给出两种完全不同的 skipped 理由。
    """
    if materia is not None:
        return materia
    if pharmacology_read_path(MATERIA_KIND) is None:  # type: ignore[arg-type]
        return None
    return build_entry_index(MATERIA_KIND)


def herb_props(index: dict, name: str) -> dict[str, list[str]] | None:
    """按原名查，查不到再按 normalize_herb 查一次——跟 `check_dose_limits`
    对 DOSE_LIMITS 的查法完全一致（先原名后归一），不是这里另发明的顺序。"""
    return index.get(name) or index.get(normalize_herb(name))


@dataclass(frozen=True)
class FormulaCheck:
    """`check_formula` 的结果。advice 之外还要带 skipped——

    "这条规则没给出建议"和"这条规则根本没跑"在界面上长得一模一样，
    而它们的含义相反。skipped 让后者看得见。
    """

    advice: tuple[Advice, ...] = ()
    skipped: tuple[dict, ...] = ()
    materia_available: bool = False
    n_materia_checked: int = 0

    @property
    def score(self) -> float:
        return score_formula(self.advice)

    def by_kind(self, kind: str) -> tuple[Advice, ...]:
        return tuple(a for a in self.advice if a.kind == kind)


def score_formula(advice) -> float:
    """1.0 − Σ权重，下限 0.0。

    **这是一把粗排序尺，不是疗效评分。** 它的全部用途是：R22 的 best-of-N 在
    **同一次问诊**采样出的 N 张方之间挑一张。跨问诊比这个分没有意义——
    不同证型能触发的规则条数本来就不同（没有明确寒热方向的证型永远触发不了
    `thermal_mismatch`），分高只说明"这张方踩到的规则少"。

    权重只在 `ADVICE_WEIGHTS` 一处定义。同一类多条就多扣一次（两对十八反比
    一对严重），扣到 0 就不再往下扣——负分在排序里没有额外信息，
    而它会让"0.0 分"和"−1.5 分"看起来像两种不同的结论。
    """
    total = sum(ADVICE_WEIGHTS.get(a.kind, 0.0) for a in advice)
    return max(0.0, round(1.0 - total, 6))


def _incompatible_source(pair: tuple[str, str]) -> str:
    """这一对出自十八反还是十九畏。安全层把两张表分开存着，这里照它的分法说，
    不自己判断"这看起来像反还是像畏"。"""
    return "十八反" if frozenset(pair) in SHIBAFAN else "十九畏"


def check_incompatible_advice(herbs: list[str]) -> list[Advice]:
    return [
        Advice(kind="incompatible", herbs=[a, b],
               reason=f"{a} 与 {b} 属配伍禁忌，同方相见须改方",
               source_span=_incompatible_source((a, b)),
               severity=SEVERITY_BY_KIND["incompatible"])
        for a, b in check_incompatible(herbs)
    ]


def check_dose_advice(items: list[HerbItem]) -> list[Advice]:
    return [
        Advice(kind="over_dose", herbs=[v.herb],
               reason=f"{v.herb} {v.dose}{v.unit} 超过常用上限 {v.limit_g}g",
               source_span=v.reason,
               severity=SEVERITY_BY_KIND["over_dose"])
        for v in check_dose_limits(items)
    ]


def check_thermal_advice(syndrome: str, items: list[HerbItem]) -> list[Advice]:
    """寒热方向。判据整段交给 `check_thermal_consistency`——它已经处理了
    "证型里同时有寒和热就不判"、"过半按实际主方味数算"这些边界。"""
    warning = check_thermal_consistency(syndrome, [i.name for i in items])
    if warning is None:
        return []
    return [Advice(kind="thermal_mismatch", herbs=[],
                   reason=warning, source_span=None,
                   severity=SEVERITY_BY_KIND["thermal_mismatch"])]


def check_channel_advice(syndrome: str, items: list[HerbItem], index: dict) -> list[Advice]:
    """证型指向的病位上，一味归该经的药都没有 → 缺引经药。

    没有病位的证型（比如「气滞证」）不判：这条规则的前提是"知道要往哪引"。
    **空方也不判**：0 味药不是"缺引经药"，是根本还没有方——可编辑处方表从空表
    开始，医生删到 0 味时前端仍会调一次校验，那时报一条"方中没有一味药归脾经"
    是字面为真但毫无用处的噪音（跟 PrescriptionValidateRequest 对 0 味药
    "返回全空的 FormulaSafety 才是诚实结果"是同一条判断）。
    """
    channels = syndrome_channels(syndrome)
    if not channels or not items:
        return []
    covered: set[str] = set()
    for item in items:
        props = herb_props(index, item.name)
        if not props:
            continue
        herb_channels = _channel_terms(props.get(CHANNEL_PREDICATE, []))
        covered |= {c for c in channels if c in herb_channels}
    missing = [c for c in channels if c not in covered]
    if not missing:
        return []
    return [Advice(
        kind="missing_channel_guide", herbs=[],
        reason=f"证型指向{'、'.join(missing)}，但方中没有一味药归{'、'.join(missing)}经",
        source_span=None, severity=SEVERITY_BY_KIND["missing_channel_guide"])]


def check_duplicate_advice(items: list[HerbItem], index: dict) -> list[Advice]:
    """两味药的性味功效术语重合 ≥ `DUPLICATE_OVERLAP_GATE` → 重复用药。

    只两两比，不做聚类：三味药互相重复会出三条建议，这是有意的——
    合并成一条"这三味重复"就必须替人决定留哪一味，那是医家的判断。
    """
    props_by_herb: list[tuple[str, set[str]]] = []
    for item in items:
        props = herb_props(index, item.name)
        if not props:
            continue
        terms: set[str] = set()
        for pred in DUPLICATE_PREDICATES:
            terms |= _terms(props.get(pred, []))
        if terms:
            props_by_herb.append((item.name, terms))

    out: list[Advice] = []
    for i in range(len(props_by_herb)):
        for j in range(i + 1, len(props_by_herb)):
            a, terms_a = props_by_herb[i]
            b, terms_b = props_by_herb[j]
            shared = terms_a & terms_b
            ratio = len(shared) / min(len(terms_a), len(terms_b))
            if ratio >= DUPLICATE_OVERLAP_GATE:
                out.append(Advice(
                    kind="duplicate_effect", herbs=[a, b],
                    reason=(f"{a} 与 {b} 性味功效重合 {ratio:.0%}"
                            f"（{'、'.join(sorted(shared))}），考虑去其一"),
                    source_span=None,
                    severity=SEVERITY_BY_KIND["duplicate_effect"]))
    return out


def _sort_key(advice: Advice) -> tuple:
    """排序：先按权重从重到轻，同权重按 kind、再按涉及的药名。
    确定性是硬要求——同一张方两次调用必须给出**同一个顺序**，
    否则前端的"第一条建议"会来回跳，而 R22 的打分也就不可复现。"""
    return (-ADVICE_WEIGHTS.get(advice.kind, 0.0), advice.kind, tuple(advice.herbs))


@dataclass(frozen=True)
class _SkipReason:
    rule: str
    reason: str
    available: bool = True
    path: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"rule": self.rule, "reason": self.reason, "available": self.available}
        if self.path:
            out["path"] = self.path
        out.update(self.extra)
        return out


_MATERIA_RULES = ("missing_channel_guide", "duplicate_effect")


def check_formula(syndrome: str, herb_items: list[HerbItem], *,
                  materia: dict | None = None) -> FormulaCheck:
    """五条规则跑一遍。三条不依赖任何数据文件，两条要药理层本草表。

    `syndrome` 允许空串：`/api/prescription/export` 那条路径就没有证型可传
    （见 api/main.py 的注释）。空证型下寒热和归经两条自动不判——这是规则
    不适用，不是"通过了"，所以它们也进 skipped。
    """
    herbs = [i.name for i in herb_items]
    advice: list[Advice] = []
    advice += check_incompatible_advice(herbs)
    advice += check_dose_advice(herb_items)
    advice += check_thermal_advice(syndrome, herb_items)

    skipped: list[_SkipReason] = []
    if not (syndrome or "").strip():
        skipped.append(_SkipReason(
            "thermal_mismatch", "没有证型可判寒热方向（这条规则不适用，不是通过）"))

    index = materia_index(materia)
    if index is None:
        path = pharmacology_write_path(MATERIA_KIND)  # type: ignore[arg-type]
        for rule in _MATERIA_RULES:
            skipped.append(_SkipReason(
                rule, "药理层本草表还没建出来（AutoDL 上跑 "
                      "`python -m scripts.run_pharmacology_extraction` 才有）",
                available=False, path=str(path)))
        n_checked = 0
    else:
        n_checked = sum(1 for i in herb_items if herb_props(index, i.name))
        channel_advice = check_channel_advice(syndrome, herb_items, index)
        advice += channel_advice
        advice += check_duplicate_advice(herb_items, index)
        if not syndrome_channels(syndrome):
            skipped.append(_SkipReason(
                "missing_channel_guide",
                "证型里没有病位，不知道要往哪引经（这条规则不适用，不是通过）"))
        elif not herb_items:
            skipped.append(_SkipReason(
                "missing_channel_guide",
                "方里还没有药，无从判引经（这条规则不适用，不是通过）"))
        if n_checked < len(herb_items):
            skipped.append(_SkipReason(
                "duplicate_effect",
                f"本草表里查到 {n_checked}/{len(herb_items)} 味药，"
                "查不到的那几味没参与重复判定",
                extra={"n_checked": n_checked, "n_herbs": len(herb_items)}))

    return FormulaCheck(
        advice=tuple(sorted(advice, key=_sort_key)),
        skipped=tuple(s.as_dict() for s in skipped),
        materia_available=index is not None,
        n_materia_checked=n_checked,
    )


def advice_dicts(check: FormulaCheck) -> list[dict]:
    """给 API/前端用的序列化形状。**一处实现**：/api/prescription/validate 和
    每位医家的 results[i].advice 用的是同一个函数，不是两处各 model_dump 一遍。"""
    return [a.model_dump() for a in check.advice]
