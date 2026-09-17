"""R34 符号验证器：把 S3′ 开出的方逐条拿去本体里对，对不上的回灌给模型重开。

**这一层回答的问题跟既有两层都不同**，三层并列不重叠（CLAUDE.md 第 31 条）：

| 层 | 回答的问题 | 产出 | 动作 |
|---|---|---|---|
| `core/safety_output.py` | 这方能不能**发出去** | `FormulaSafety` | 拦截 |
| `core/formula_check.py` | 这方**拟得好不好** | `Advice` | 建议 |
| **本模块** | 这方的每一条主张在**本体里站不站得住** | `Violation` / `Unverifiable` | **回灌重开** |

前两层判的是方本身；这一层判的是**方与它声称的依据之间的关系**——模型说
「用柴胡因为它疏肝解郁、入肝经」，本体里柴胡的归经原文写的是什么？对不上就是
一条可以指着原文说出来的错，而"指着原文"正是 LLM-Modulo 那条实证
（幻觉 63% → 1.7%）的机制所在。

## 七条规则，两种级别

revise（可改，回灌重开）：
  `meridian_coverage`     方中药的归经覆盖不了辨出来的病变脏腑
  `nature_conflict`       证型寒热方向与主方药性相悖
  `effect_matches_method` 药的功效跟治法对不上
  `role_structure`        君臣佐使结构不成立

veto（不可下发，残余不发）：
  `incompatible_pair`     十八反十九畏
  `dose_exceeds`          超药典常用上限
  `herb_grounded`         **引用了本体里不存在的原文**（编造出处）

## `herb_grounded` 为什么判的是"编造出处"而不是"这味药不在本体里"

实测（R34）：`cases.json` 里归一后 578 种药名，本体查得到 **230 种（39.8%）**。
把"不在本体里"当 veto，几乎每张方都会被否掉——而那不是模型的错，是**药理层
覆盖不全**这个数据事实。两件事必须分开：

  - 模型给了一条 `OntologyRef`，而那个 `span` 在本体里查不到 → **编造出处，veto**
  - 这味药本体里根本没有 → **`Unverifiable`，不是违规**（见下）

## `Unverifiable`：查不到依据 ≠ 查到了且通过

本体的缺谓词是实测出来的：归经缺 **598/1232（49%）**、用量缺 **672（55%）**、
禁忌缺 770、炮制 790。一条规则要用归经而那味药没有归经，正确的结论是
**"判不了"**，不是"通过"。

所以 `VerificationResult.passed` 的定义是：无 veto、无 revise、**且 `unverifiable`
为空**。`unverifiable` 非空时 `passed=False` 且 `status="partially_verified"`，
前端与报告如实显示"这几味药的归经缺失，归经覆盖规则无法判定"。

**静默跳过是这一层最危险的失败模式**：它让"我们做了符号验证"这句话在覆盖率
只有一半的数据上依然成立，而那句话届时是假的。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from core.effect_synonyms import expand_effect
from core.elements import LOCATIONS
from core.formula_check import syndrome_channels
from core.ontology import Ontology, get_ontology
from core.safety_output import (
    check_dose_limits,
    check_incompatible,
    check_thermal_consistency,
    dose_limit_entry,
)
from core.schemas import HerbItem, OntologyRef, _S3StructuredBase

#: 规则名。**顺序即报告顺序**，veto 在前（先看能不能发，再看拟得对不对）。
VETO_RULES: tuple[str, ...] = ("incompatible_pair", "dose_exceeds", "herb_grounded")
REVISE_RULES: tuple[str, ...] = (
    "meridian_coverage", "nature_conflict", "effect_matches_method", "role_structure",
)
ALL_RULES: tuple[str, ...] = VETO_RULES + REVISE_RULES

Severity = Literal["veto", "revise"]

#: 闭环最多重开几轮。**不是无限循环**：`llm_calls` 要可预测（manifest 里那个数
#: 是额度结算与成本比较的依据），而且模型改三轮还改不好时，再改一轮的期望收益
#: 已经低于又花一次调用的代价。环境变量 `MAX_REVISE_ROUNDS` 可覆盖（做消融用）。
MAX_REVISE_ROUNDS = 3
MAX_REVISE_ROUNDS_ENV = "MAX_REVISE_ROUNDS"


def max_revise_rounds() -> int:
    """环境变量优先。非正整数直接抛——**它直接决定调用数**，把 3 写成 30 的表现
    只是"这次好慢"，跟 `s3_best_of_n` 同一条理由。"""
    raw = (os.environ.get(MAX_REVISE_ROUNDS_ENV) or "").strip()
    if not raw:
        return MAX_REVISE_ROUNDS
    try:
        n = int(raw)
    except ValueError as e:
        raise ValueError(f"{MAX_REVISE_ROUNDS_ENV}={raw!r} 不是整数") from e
    if n < 0:
        raise ValueError(f"{MAX_REVISE_ROUNDS_ENV}={n} 必须 ≥ 0（0 = 关掉闭环）")
    return n


@dataclass(frozen=True)
class Violation:
    """一条违规。`counterexample` **必须含本体原文**。

    为什么必须含原文：这条违规要被回灌给模型重开，而"你的归经不对"这种说法
    模型没法照着改；"本草里黄芪的归经原文是『归脾、肺经』，而你辨的病位是肝"
    才是可执行的反例。**空的 counterexample 等于没有反例**——有一条测试钉住
    每条规则产出的 Violation 都带非空 counterexample。
    """

    rule: str
    severity: Severity
    herbs: tuple[str, ...]
    reason: str
    counterexample: str
    refs: tuple[OntologyRef, ...] = ()

    def __post_init__(self) -> None:
        if self.rule not in ALL_RULES:
            raise ValueError(f"未知规则 {self.rule!r}，只能是：{', '.join(ALL_RULES)}")
        expect: Severity = "veto" if self.rule in VETO_RULES else "revise"
        if self.severity != expect:
            raise ValueError(
                f"规则 {self.rule} 的级别固定是 {expect}，不是 {self.severity}"
                "——级别是规则的属性，不许逐条传，否则同一条规则在两处会有两种后果"
            )
        if not self.counterexample.strip():
            raise ValueError(
                f"规则 {self.rule} 的 counterexample 是空的。"
                "一条要回灌给模型的违规必须能指着本体原文说出哪里不对，"
                "空反例等于没有反例（见 Violation 的文档字符串）。"
            )


@dataclass(frozen=True)
class Unverifiable:
    """一条**判不了**的规则。不是通过，也不是违规。

    `missing_predicate` 记的是"缺哪一项才判不了"——归经/用量/功效/性味，
    或者 `本体条目` 表示这味药在本体里根本没有。前端与报告要能把这句话原样
    显示给人看，所以 `reason` 是一句完整的话而不是一个代号。
    """

    rule: str
    herbs: tuple[str, ...]
    missing_predicate: str
    reason: str

    def __post_init__(self) -> None:
        if self.rule not in ALL_RULES:
            raise ValueError(f"未知规则 {self.rule!r}")
        if not self.missing_predicate.strip() or not self.reason.strip():
            raise ValueError("missing_predicate 与 reason 都不许为空——"
                            "「判不了」这件事本身要说得出是缺了什么")


@dataclass(frozen=True)
class VerificationResult:
    """一次验证的全部结论。

    `passed` 的定义（R34a）：**无 veto、无 revise、且 `unverifiable` 为空**。
    前两条是"没查出问题"，第三条是"该查的都真的查了"——少了第三条，
    "已验证通过"这句话在覆盖率只有一半的本体上依然成立，而那时它是假的。
    """

    violations: tuple[Violation, ...] = ()
    unverifiable: tuple[Unverifiable, ...] = ()
    #: 本体可用吗。False 时 `checked_rules` 为空、`unverifiable` 覆盖全部七条。
    ontology_available: bool = True
    #: 这一次真的跑过判定的规则（跑了但判不了的不算）。
    checked_rules: tuple[str, ...] = ()

    @property
    def vetoes(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == "veto")

    @property
    def revisables(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.severity == "revise")

    @property
    def passed(self) -> bool:
        return not self.violations and not self.unverifiable

    @property
    def status(self) -> str:
        """`verified` / `partially_verified` / `revise_needed` / `vetoed`。

        次序即严重性：有 veto 就是 vetoed（不下发），其次 revise_needed（重开），
        其次 partially_verified（查不全），全清才是 verified。
        """
        if self.vetoes:
            return "vetoed"
        if self.revisables:
            return "revise_needed"
        if self.unverifiable:
            return "partially_verified"
        return "verified"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "passed": self.passed,
            "ontology_available": self.ontology_available,
            "checked_rules": list(self.checked_rules),
            "n_veto": len(self.vetoes),
            "n_revise": len(self.revisables),
            "n_unverifiable": len(self.unverifiable),
            "violations": [
                {"rule": v.rule, "severity": v.severity, "herbs": list(v.herbs),
                 "reason": v.reason, "counterexample": v.counterexample}
                for v in self.violations
            ],
            "unverifiable": [
                {"rule": u.rule, "herbs": list(u.herbs),
                 "missing_predicate": u.missing_predicate, "reason": u.reason}
                for u in self.unverifiable
            ],
        }


# ---------- 七条规则 ----------
#
# 每条规则的签名都是 `(s3, ont) -> (violations, unverifiable, checked)`：
# `checked` 是这条规则**真的判了**的时候它自己的名字，判不了时为空。
# 三元组而不是只返回 violations，是因为"没违规"有两种：查过没问题、和查不了。


def _items(s3: _S3StructuredBase) -> list[HerbItem]:
    return list(s3.formula.candidate.herb_items)


def _herb_names(s3: _S3StructuredBase) -> list[str]:
    return [i.name for i in _items(s3)]


def _span_of(ont: Ontology, name: str, predicate: str) -> str | None:
    """本体里这味药这个谓词的原文片段。查不到返回 None。

    取第一条非空 span：同一谓词可能有多个来源，反例只需要一条能指着看的原文。
    """
    h = ont.herb(name)
    if h is None:
        return None
    for ref in h.refs.get(predicate, ()):
        if ref.span.strip():
            return ref.span.strip()
    return None


def check_incompatible_pair(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """十八反十九畏。**判据整个来自 `core.safety_output.check_incompatible`**
    ——那张 24 对的表是唯一实现，这里只把它的输出转成带本体原文的 Violation。

    这条规则**永远判得了**：配伍表不依赖本体，所以不会进 unverifiable。
    """
    names = _herb_names(s3)
    out: list[Violation] = []
    for a, b in check_incompatible(names):
        pair = ont.is_incompatible(a, b) or f"{a}-{b}"
        # 反例优先用本体里两味药的禁忌原文；本体没收就退到配伍表本身
        # （那张表也是"原文"——十八反歌诀，来源在 safety_output 里注明）。
        spans = [s for s in (_span_of(ont, a, "禁忌"), _span_of(ont, b, "禁忌")) if s]
        counter = ("；".join(spans) if spans
                   else f"十八反十九畏表收录了这一对：{pair}（见 core/safety_output.py 的来源注记）")
        out.append(Violation(
            rule="incompatible_pair", severity="veto", herbs=(a, b),
            reason=f"{a} 与 {b} 属配伍禁忌（{pair}），同方相见不可下发",
            counterexample=counter,
        ))
    return out, [], ["incompatible_pair"]


def check_dose_exceeds(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """超药典常用上限。**判据来自 `core.safety_output.check_dose_limits`**。

    剂量缺失（`dose is None`）的药进 **unverifiable** 而不是静默通过：
    「没写剂量」不是「剂量合规」。本体的用量谓词缺 672/1232（55%），
    所以这条规则在真实数据上会有相当一部分判不了——那正是要被报出来的事。
    """
    items = _items(s3)
    out: list[Violation] = []
    unver: list[Unverifiable] = []
    for v in check_dose_limits(items):
        entry = dose_limit_entry(v.herb)
        counter = (f"药典常用上限 {v.limit_g}g（{entry[1] if entry else v.reason}）；"
                   f"本方开的是 {v.dose}{v.unit}")
        span = _span_of(ont, v.herb, "用量")
        if span:
            counter += f"；本草用量原文：{span}"
        out.append(Violation(
            rule="dose_exceeds", severity="veto", herbs=(v.herb,),
            reason=f"{v.herb} 剂量 {v.dose}{v.unit} 超过常用上限 {v.limit_g}g",
            counterexample=counter,
        ))
    for it in items:
        if it.dose is None and dose_limit_entry(it.name) is not None:
            unver.append(Unverifiable(
                rule="dose_exceeds", herbs=(it.name,), missing_predicate="剂量",
                reason=f"{it.name} 在药典限量表里（有上限可比），但本方没写剂量，"
                       "超没超限判不了——「没写剂量」不是「剂量合规」",
            ))
    return out, unver, ["dose_exceeds"]


def check_herb_grounded(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """模型给的 `OntologyRef` 在本体里**找得到那段原文**吗。

    **判的是"编造出处"，不是"这味药不在本体里"**（见模块文档字符串）：
      - 引用的 span 在本体该 (药名, 谓词) 下找不到 → veto（编造）
      - 这味药本体里根本没有 → unverifiable（数据缺，不是模型的错）
      - 这味药在本体里但模型一条 ref 都没给 → unverifiable（没有可核的主张）

    span 的比对用**双向子串**而不是相等：模型照抄时可能只抄了其中一句
    （本体原文往往是一整段），要求逐字相等会把正确的引用判成编造。
    """
    out: list[Violation] = []
    unver: list[Unverifiable] = []
    for choice in s3.herb_choices:
        name = choice.item.name
        herb = ont.herb(name)
        if herb is None:
            unver.append(Unverifiable(
                rule="herb_grounded", herbs=(name,), missing_predicate="本体条目",
                reason=f"本体里没有「{name}」这味药（本草 {len(ont.herbs)} 味覆盖不到它），"
                       "它的功效依据无法核实——这是药理层覆盖不全，不是模型编造",
            ))
            continue
        if not choice.ontology_refs:
            unver.append(Unverifiable(
                rule="herb_grounded", herbs=(name,), missing_predicate="ontology_refs",
                reason=f"「{name}」在本体里有条目，但模型没有给出任何 ontology_refs，"
                       "没有可核的引用——不算编造，算没引",
            ))
            continue
        for ref in choice.ontology_refs:
            if ref.kind != "herb":
                continue
            span = ref.span.strip()
            真 = [r.span.strip() for r in herb.refs.get(ref.predicate, ()) if r.span.strip()]
            if not 真:
                unver.append(Unverifiable(
                    rule="herb_grounded", herbs=(name,), missing_predicate=ref.predicate,
                    reason=f"模型引了「{name}」的{ref.predicate}，"
                           f"而本体里这味药没有{ref.predicate}这一项，对不了",
                ))
                continue
            if not any(span in t or t in span for t in 真):
                out.append(Violation(
                    rule="herb_grounded", severity="veto", herbs=(name,),
                    reason=f"模型引用的「{name}·{ref.predicate}」原文在本体里找不到，"
                           "这是编造出处",
                    counterexample=f"模型写的是「{span}」；本体里「{name}」的"
                                   f"{ref.predicate}原文是「{真[0]}」",
                    refs=(ref,),
                ))
    return out, unver, ["herb_grounded"]


def check_meridian_coverage(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """方中药的归经，覆盖不覆盖第 1 步辨出来的病变脏腑。

    病位词表**复用 `core.elements.LOCATIONS`**（经 `formula_check.syndrome_channels`）
    ——证素抽取、证素索引、方剂建议、这一层，四处问的是同一个问题
    「这个词是哪个病位」，不另写一张脏腑表（第 31 条）。

    归经缺 **598/1232（49%）**，所以这条规则最常进 unverifiable：
    一味药没有归经，它到底入不入肝经**判不了**，不能当成"不入"去凑一条违规。
    """
    organs = [o.organ for o in s3.organs]
    targets = [x for x in organs if x in LOCATIONS] or syndrome_channels(s3.syndrome.name)
    if not targets:
        return [], [Unverifiable(
            rule="meridian_coverage", herbs=(), missing_predicate="病位",
            reason=f"第 1 步辨出的脏腑 {organs} 都不在病位词表里，"
                   f"证型「{s3.syndrome.name}」也没带病位字样，归经覆盖判不了",
        )], []

    covered: set[str] = set()
    unver: list[Unverifiable] = []
    n_with_meridian = 0
    for it in _items(s3):
        h = ont.herb(it.name)
        if h is None or not h.meridians:
            unver.append(Unverifiable(
                rule="meridian_coverage", herbs=(it.name,), missing_predicate="归经",
                reason=(f"本体里没有「{it.name}」" if h is None
                        else f"本体里「{it.name}」没有归经这一项")
                       + "，它入不入" + "/".join(targets) + "经判不了",
            ))
            continue
        n_with_meridian += 1
        covered |= h.meridians
    # **一味都判不了的时候不出违规**：那时"没覆盖"只是"不知道"，
    # 报一条违规会让模型去改一个本来可能是对的方。
    if n_with_meridian == 0:
        return [], unver, []
    missing = [t for t in targets if t not in covered]
    if not missing:
        return [], unver, ["meridian_coverage"]
    spans = []
    for it in _items(s3)[:3]:
        s = _span_of(ont, it.name, "归经")
        if s:
            spans.append(f"{it.name}：{s}")
    return [Violation(
        rule="meridian_coverage", severity="revise", herbs=tuple(_herb_names(s3)),
        reason=f"辨出的病变脏腑 {'、'.join(missing)} 没有一味药的归经覆盖到"
               f"（已核 {n_with_meridian} 味有归经记载的药）",
        counterexample="本草归经原文：" + "；".join(spans) if spans else
                       f"这 {n_with_meridian} 味药的归经合起来是 {'、'.join(sorted(covered))}，"
                       f"不含 {'、'.join(missing)}",
    )], unver, ["meridian_coverage"]


def check_nature_conflict(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """证型寒热方向与主方药性相悖。

    **判据来自 `core.safety_output.check_thermal_consistency`**（那条规则只看
    证型名的字面，粗糙处已写进它自己的文档）。这里加的是本体那一层的反例：
    把主方前几味药在本体里的性味原文附上，模型才知道该换哪一味。

    证型没有明确寒热方向、或寒热错杂时 `check_thermal_consistency` 返回 None
    ——那是**规则不适用**，不是通过，所以进 unverifiable。
    """
    warn = check_thermal_consistency(s3.syndrome.name, _herb_names(s3))
    if warn is None:
        return [], [Unverifiable(
            rule="nature_conflict", herbs=(), missing_predicate="证型寒热方向",
            reason=f"证型「{s3.syndrome.name}」没有明确的寒热方向（或寒热错杂），"
                   "这条规则不适用——不是通过，是判不了",
        )], []
    spans = []
    for it in _items(s3)[:6]:
        s = _span_of(ont, it.name, "性味")
        if s:
            spans.append(f"{it.name}：{s}")
    if not spans:
        return [], [Unverifiable(
            rule="nature_conflict", herbs=tuple(_herb_names(s3)[:6]),
            missing_predicate="性味",
            reason="主方前 6 味在本体里都没有性味记载，寒热方向对不对判不了"
                   f"（粗判提示：{warn}）",
        )], []
    return [Violation(
        rule="nature_conflict", severity="revise", herbs=tuple(_herb_names(s3)[:6]),
        reason=warn,
        counterexample="本草性味原文：" + "；".join(spans),
    )], [], ["nature_conflict"]


def check_effect_matches_method(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """每味药的功效，跟第 3 步的治法对不对得上。

    治法词 → 功效词的展开**走 `core.effect_synonyms.expand_effect`**：
    治法（「疏肝理气」）与功效（「疏肝解郁」）在文献里是两套措辞，裸子串比会把
    大部分正确的药判成不匹配，这条规则就变成恒假（见那个模块的文档）。

    功效缺 49/1232（4%，本体里最全的一项），所以这条规则大多判得了。
    **一味药对不上不算违规**：佐使药本来就可能针对兼夹症，所以判据是
    「没有任何一味药的功效对得上治法」——那时这张方跟它声称的治法无关。
    """
    keys = set(expand_effect(s3.method.principle))
    for t in s3.method.targets:
        keys |= set(expand_effect(t))
    matched: list[str] = []
    unver: list[Unverifiable] = []
    n_with_effects = 0
    for it in _items(s3):
        h = ont.herb(it.name)
        if h is None or not h.effects:
            unver.append(Unverifiable(
                rule="effect_matches_method", herbs=(it.name,), missing_predicate="功效",
                reason=(f"本体里没有「{it.name}」" if h is None
                        else f"本体里「{it.name}」没有功效这一项")
                       + f"，它的功效跟治法「{s3.method.principle}」对不对得上判不了",
            ))
            continue
        n_with_effects += 1
        if any(k in e for e in h.effects for k in keys):
            matched.append(it.name)
    if n_with_effects == 0:
        return [], unver, []
    if matched:
        return [], unver, ["effect_matches_method"]
    spans = []
    for it in _items(s3)[:4]:
        s = _span_of(ont, it.name, "功效")
        if s:
            spans.append(f"{it.name}：{s}")
    return [Violation(
        rule="effect_matches_method", severity="revise",
        herbs=tuple(_herb_names(s3)),
        reason=f"已核 {n_with_effects} 味有功效记载的药，没有一味的功效对得上治法"
               f"「{s3.method.principle}」（展开后的功效词：{'、'.join(sorted(keys)[:8])}…）",
        counterexample="本草功效原文：" + "；".join(spans) if spans else
                       f"这 {n_with_effects} 味药的功效都不含 {'、'.join(sorted(keys)[:5])}",
    )], unver, ["effect_matches_method"]


def check_role_structure(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """君臣佐使结构成不成立。

    **这条规则不依赖本体**（role 是模型自己标在 `HerbItem` 上的），所以它
    永远判得了——除了一种情况：一味药的 role 都没标，那时结构判不了。

    三条判据，都是方剂学的基本结构要求：
      1. 至少一味君药——没有君药的方说不清主攻什么
      2. 君药不超过 3 味——都是君等于没有君
      3. 佐使药数量不超过君臣之和——R1 量过：ε_online 里相当一部分是无依据的
         佐使加减（叶天士那三次君臣骨架全同，变的全是佐使）
    """
    items = _items(s3)
    roles = [i.role for i in items if i.role]
    if not roles:
        return [], [Unverifiable(
            rule="role_structure", herbs=tuple(_herb_names(s3)),
            missing_predicate="role",
            reason=f"这张方 {len(items)} 味药一个 role 都没标，君臣佐使结构判不了",
        )], []
    n = {r: roles.count(r) for r in ("君", "臣", "佐", "使")}
    problems = []
    if n["君"] == 0:
        problems.append("没有君药")
    if n["君"] > 3:
        problems.append(f"君药 {n['君']} 味（都是君等于没有君）")
    if n["佐"] + n["使"] > n["君"] + n["臣"]:
        problems.append(f"佐使共 {n['佐'] + n['使']} 味，多于君臣 {n['君'] + n['臣']} 味")
    if not problems:
        return [], [], ["role_structure"]
    detail = "、".join(f"{r} {n[r]} 味" for r in ("君", "臣", "佐", "使"))
    return [Violation(
        rule="role_structure", severity="revise", herbs=tuple(_herb_names(s3)),
        reason="；".join(problems),
        # 这条规则的反例是**这张方自己的结构**，不是本体原文——它判的不是
        # "跟本草对不对得上"，而是"这张方内部的结构成不成立"。
        counterexample=f"本方 {len(items)} 味药的角色分布：{detail}"
                       f"（未标 role 的 {len(items) - len(roles)} 味）",
    )], [], ["role_structure"]


#: 规则名 → 实现。`verify_formula` 按 `ALL_RULES` 的顺序跑，**不按字典顺序**。
RULE_FUNCS = {
    "incompatible_pair": check_incompatible_pair,
    "dose_exceeds": check_dose_exceeds,
    "herb_grounded": check_herb_grounded,
    "meridian_coverage": check_meridian_coverage,
    "nature_conflict": check_nature_conflict,
    "effect_matches_method": check_effect_matches_method,
    "role_structure": check_role_structure,
}


def verify_formula(s3: _S3StructuredBase, *, ontology: Ontology | None = None
                   ) -> VerificationResult:
    """跑七条规则。本体不可用时**七条全部进 unverifiable**，不是全部通过。

    这是这一层最要紧的一条语义：药理层数据不在的机器上，"符号验证通过"必须
    报成"一条都没验"（`status="partially_verified"`、`passed=False`），
    否则那句话在没有本体的环境里恒真，而它恒真时毫无意义。
    """
    ont = ontology if ontology is not None else get_ontology()
    if not ont.available:
        return VerificationResult(
            violations=(),
            unverifiable=tuple(
                Unverifiable(rule=r, herbs=(), missing_predicate="药理层数据",
                             reason="本体不可用（data/standard/materia_medica.jsonl 与 "
                                    "formulary.jsonl 不在），这条规则一次都没跑")
                for r in ALL_RULES),
            ontology_available=False, checked_rules=(),
        )
    violations: list[Violation] = []
    unver: list[Unverifiable] = []
    checked: list[str] = []
    for rule in ALL_RULES:
        v, u, c = RULE_FUNCS[rule](s3, ont)
        violations.extend(v)
        unver.extend(u)
        checked.extend(c)
    return VerificationResult(
        violations=tuple(violations), unverifiable=tuple(unver),
        ontology_available=True, checked_rules=tuple(checked),
    )


# ---------- 回灌：把违规写成模型能照着改的一段话 ----------

def format_violations_for_revise(result: VerificationResult) -> str:
    """回灌给模型的那段文本。**veto 与 revise 分开列**，并且每条都带反例原文。

    只列 `violations`，**不列 `unverifiable`**：那些是本体缺数据，模型改方也改不出
    数据来，写进去只会让它以为自己错了、去改一个本来可能对的地方。
    `unverifiable` 的去处是 manifest 与前端（如实显示"这几条判不了"）。
    """
    if not result.violations:
        return ""
    lines = ["\n\n【符号验证不通过】下面每一条都附了本体原文，请据此重开这张方。"]
    if result.vetoes:
        lines.append("\n必须解决（否则这张方不会下发）：")
        for i, v in enumerate(result.vetoes, 1):
            lines.append(f"{i}. [{v.rule}] {v.reason}\n   依据：{v.counterexample}")
    if result.revisables:
        lines.append("\n需要修正：")
        for i, v in enumerate(result.revisables, 1):
            lines.append(f"{i}. [{v.rule}] {v.reason}\n   依据：{v.counterexample}")
    lines.append("\n其余要求一条都不许省：五步链、逐字引用上一步的结论、"
                 "每味药都要有用药理由、引用的本体原文必须照抄。")
    return "\n".join(lines)


# ---------- 三指标 ----------

def herbs_grounded_ratio(s3: _S3StructuredBase) -> float:
    """带本体引用的药味占比。**分母是这张方的药味数，不是本体总药味数。**

    R34b：这两个集合完全不同，实测——本体 **1232** 味，而 `cases.json` 里归一后的
    药名 **578** 种，其中本体查得到 **230 种（39.8%）**。
    拿 1232 当分母算出来的数没有任何意义（它回答的是"本体里有多少味药被这张方
    用到了"，而那个比值恒接近 0）。

    这个函数是 `_S3StructuredBase.herbs_grounded_ratio()` 的**转发**，不是第二份
    实现——放在这里是因为三指标要在同一处定义（第 31 条）。
    """
    return s3.herbs_grounded_ratio()


def ontology_coverage_of_corpus(*, ontology: Ontology | None = None,
                                cases_path=None) -> dict:
    """本体覆盖了医案语料里多少种药名。**这是数据质量指标，不是模型指标。**

    跟 `herbs_grounded_ratio` 放在一起定义，正是为了让两者的分母不会被混用：
      - `herbs_grounded_ratio`：分母 = **这张方**的药味数（模型指标）
      - 本函数：分母 = **医案语料**里出现的药名种数（数据指标）

    同时报"按种数"和"按出现次数"两个比值：实测 230/578 = 39.8%（按种数）
    但 3380/4799 = 70.4%（按次数）——常用药覆盖得好，古籍特有写法的长尾覆盖差。
    只报前者会低估它对真实问诊的支撑，只报后者会掩盖长尾缺口。
    """
    import collections
    import json
    from pathlib import Path

    from core.herbs import normalize_herb

    ont = ontology if ontology is not None else get_ontology()
    p = Path(cases_path) if cases_path else (
        Path(__file__).resolve().parent.parent / "cases.json")
    if not p.exists():
        return {"available": False, "note": f"{p.name} 不在，覆盖率算不了"}
    counts: collections.Counter = collections.Counter()
    for case in json.loads(p.read_text(encoding="utf-8")):
        for raw in (case.get("herbs") or []):
            name = normalize_herb(raw)
            if name:
                counts[name] += 1
    if not counts:
        return {"available": False, "note": "语料里没有药名"}
    hit = [n for n in counts if ont.herb(n) is not None]
    n_occ_hit = sum(counts[n] for n in hit)
    n_occ = sum(counts.values())
    return {
        "available": True,
        "n_ontology_herbs": len(ont.herbs),
        "n_corpus_herb_names": len(counts),
        "n_covered_names": len(hit),
        "coverage_by_name": round(len(hit) / len(counts), 4),
        "n_corpus_occurrences": n_occ,
        "n_covered_occurrences": n_occ_hit,
        "coverage_by_occurrence": round(n_occ_hit / n_occ, 4),
    }


def verifier_metrics(rounds: list[VerificationResult], s3: _S3StructuredBase | None = None
                     ) -> dict:
    """R34 要报的三个指标 + 每一轮的状态。

    `verifier_first_pass_rate` 这里是 0/1（单次问诊要么第一轮就过要么没过）——
    **跨主诉的比率由调用方聚合**（eval/），不在这里攒状态：这个模块是纯函数，
    攒状态会让两个并发请求互相污染。
    """
    first = rounds[0] if rounds else None
    return {
        "n_rounds": len(rounds),
        # 第一轮就 passed = 没有 veto、没有 revise、也没有判不了的
        "verifier_first_pass": bool(first and first.passed),
        "first_pass_status": first.status if first else None,
        "final_status": rounds[-1].status if rounds else None,
        # 重开轮数 = 总轮数 - 1（第一轮不是"重开"）
        "revise_rounds": max(0, len(rounds) - 1),
        "herbs_grounded_ratio": (herbs_grounded_ratio(s3) if s3 is not None else None),
        "statuses": [r.status for r in rounds],
        "n_unverifiable_final": len(rounds[-1].unverifiable) if rounds else None,
    }
