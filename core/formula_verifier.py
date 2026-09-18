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

## 十一条规则，两种级别，两个数据源（R53 起）

前七条（R34）都从 `core/ontology.py`（本草/方剂本体）取证；R53 加的四条从
`core/theory.py`（R51 的医理规则层）取证——**两个数据源各自独立可用/不可用**，
`ont.available` 是 0/1、`load_theory()` 是另一个 0/1，一个数据源缺了不该把
另一个数据源能判的规则也一起打成 unverifiable（`ONTOLOGY_RULES`/`THEORY_RULES`
两张表分别管各自的开关，见 `verify_formula`）。

revise（可改，回灌重开）：
  `meridian_coverage`          方中药的归经覆盖不了辨出来的病变脏腑（本体）
  `nature_conflict`            证型寒热方向与主方药性相悖（本体）
  `effect_matches_method`      药的功效跟治法对不上（本体）
  `role_structure`             君臣佐使结构不成立（本体，看 role 计数）
  `principle_matches_syndrome` 治法跟辨出的脏腑对应的治则规则没有交集（医理）
  `method_not_contraindicated` 治法用了对应治则规则明确列为禁忌的方法（医理）
  `pathomechanism_consistent`  多个病变脏腑之间查不到医理规则能联系起来（医理）
  `role_structure_by_rule`     君药没有针对主病机（医理，看 for_element 对不对得上）

veto（不可下发，残余不发）：
  `incompatible_pair`     十八反十九畏（本体）
  `dose_exceeds`          超药典常用上限（本体）
  `herb_grounded`         **引用了本体里不存在的原文**（编造出处，本体）

## 为什么 R53 那四条都是 revise，不是 veto

veto 级现有三条判的是**具体、可核实的危险或欺骗**（配伍相反、超量、编造出处）
——查不到反驳空间。R53 那四条判的是"这条推理链跟医理规则库对不对得上"，而
医理规则库（171 条，`curated`/`classic` 两档置信度）不是穷尽的：查不到匹配
规则更可能是规则库还没收录这种组合，不是这条链错了。定成 veto 会让"规则库
不全"直接变成"这张方不能发"，那是拿数据覆盖率的锅让患者背——所以是"拟得
不够好，回灌重开"，不是"不可下发"。

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
import threading
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
from core.theory import (
    load_theory,
    organ_relations as theory_organ_relations,
    principles_for as theory_principles_for,
    role_construction_rules as theory_role_construction_rules,
    transitions as theory_transitions,
)

#: 规则名。**顺序即报告顺序**，veto 在前（先看能不能发，再看拟得对不对）。
VETO_RULES: tuple[str, ...] = ("incompatible_pair", "dose_exceeds", "herb_grounded")
REVISE_RULES: tuple[str, ...] = (
    "meridian_coverage", "nature_conflict", "effect_matches_method", "role_structure",
    # R53：医理一致性四条，接在本体那四条 revise 规则后面。
    "principle_matches_syndrome", "method_not_contraindicated",
    "pathomechanism_consistent", "role_structure_by_rule",
)
ALL_RULES: tuple[str, ...] = VETO_RULES + REVISE_RULES

#: R34 起的七条，数据源是 `core/ontology.py`（本草/方剂本体）。
ONTOLOGY_RULES: tuple[str, ...] = (
    "incompatible_pair", "dose_exceeds", "herb_grounded",
    "meridian_coverage", "nature_conflict", "effect_matches_method", "role_structure",
)
#: R53 新增的四条，数据源是 `core/theory.py`（R51 医理规则层）。**两张表互斥、
#: 并集等于 `ALL_RULES`**——`verify_formula` 按各自的数据源独立判断可用性，
#: 一张表缺数据不该连累另一张表能判的规则（见模块文档字符串）。
THEORY_RULES: tuple[str, ...] = (
    "principle_matches_syndrome", "method_not_contraindicated",
    "pathomechanism_consistent", "role_structure_by_rule",
)

#: 规则名 / 结论名 → 中文名。**展示层只认中文名，id 只在数据层出现**
#: （CLAUDE.md「标识符只有一种规范形式」的显示层版本）。
#:
#: 为什么这张表在后端而不在前端：规则清单本身在这里（`ALL_RULES`），
#: 前端另建一张表就意味着以后加规则要改两处，而漏改那一处的表现是界面上
#: 冒出一个英文 id——R37 的截图上「meridian_coverage缺归经」就是这么印出来的。
#: 序列化时随每条违规一起下发（`to_dict` 的 `rule_label`），前端只负责显示。
RULE_LABELS: dict[str, str] = {
    "incompatible_pair": "配伍禁忌",
    "dose_exceeds": "超量",
    "herb_grounded": "药味有本体出处",
    "meridian_coverage": "归经覆盖病位",
    "nature_conflict": "寒热方向",
    "effect_matches_method": "功效对得上治法",
    "role_structure": "君臣佐使结构",
    "principle_matches_syndrome": "治法对得上治则",
    "method_not_contraindicated": "治法未犯治则禁忌",
    "pathomechanism_consistent": "病位间医理关联",
    "role_structure_by_rule": "君药针对主病机",
}

#: 四种结论的中文名。次序即严重性，见 `VerificationResult.status`。
STATUS_LABELS: dict[str, str] = {
    "vetoed": "拦截",
    "revise_needed": "需重开",
    "partially_verified": "部分验证",
    "verified": "已验证",
}


def rule_label(rule: str) -> str:
    """规则 id → 中文名。**查不到就回落到 id 本身**：显示一个陌生的英文名
    比显示空白好（至少能搜到它是什么），但那说明这张表漏了一条，
    `test_every_rule_has_a_chinese_label` 会先一步红。"""
    return RULE_LABELS.get(rule, rule)


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)

Severity = Literal["veto", "revise"]

#: 闭环最多重开几轮。**不是无限循环**：`llm_calls` 要可预测（manifest 里那个数
#: 是额度结算与成本比较的依据）。**R53 产品默认从 3 降到 1**：规则从七条扩到
#: 十一条之后一次重开要同时改对更多条判据，多留的第二三轮改的是"同一次没改对
#: 的地方再试一次"，每一轮都是一次完整的 S3 重开调用——这个默认值是按调用成本
#: 定的权衡，不是靠真机实测的收益曲线定的（真机数字要等 R57/R58），沙盒里
#: 拿不出那条曲线就不装作有。环境变量 `MAX_REVISE_ROUNDS` 可覆盖
#: （R38/R57 的消融要拿更大的值当对照组）。
MAX_REVISE_ROUNDS = 1
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
    #: 本体可用吗。False 时 `ONTOLOGY_RULES` 那七条全部落进 `unverifiable`。
    ontology_available: bool = True
    #: 医理规则层（R51/R53）可用吗。False 时 `THEORY_RULES` 那四条全部落进
    #: `unverifiable`——跟 `ontology_available` 是两个独立的开关（两个数据源，
    #: 一个缺了不该连累另一个能判的规则，见模块文档字符串）。
    theory_available: bool = True
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
            "status_label": status_label(self.status),
            "passed": self.passed,
            "ontology_available": self.ontology_available,
            "theory_available": self.theory_available,
            "checked_rules": list(self.checked_rules),
            # R56 §6 第 13 条：产品面要把这份结论摆成逐条核对清单（"归经覆盖
            # 病位 ✓"这种），每条要显示中文名——`rule_label` 这唯一一张表就在
            # 这个模块里，不在前端另建一份（CLAUDE.md「同一概念只能有一处
            # 实现」）。`violations`/`unverifiable` 已经各带一份 rule_label，
            # 这里补的是**通过**（在 checked_rules 里、没有违规）的那些规则的
            # 中文名——它们不落在前两个列表里，没地方带这份映射。
            "checked_rule_labels": {r: rule_label(r) for r in self.checked_rules},
            "n_veto": len(self.vetoes),
            "n_revise": len(self.revisables),
            "n_unverifiable": len(self.unverifiable),
            "violations": [
                {"rule": v.rule, "rule_label": rule_label(v.rule),
                 "severity": v.severity, "herbs": list(v.herbs),
                 "reason": v.reason, "counterexample": v.counterexample}
                for v in self.violations
            ],
            "unverifiable": [
                {"rule": u.rule, "rule_label": rule_label(u.rule),
                 "herbs": list(u.herbs),
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


# ---------- R53：医理一致性四条（数据源是 core/theory.py，不是本体） ----------
#
# 这四条**只读 `s3.organs`/`s3.syndrome`/`s3.method`/`s3.herb_choices`
# 这几个通用字段**，不碰 `rule_refs`/`cited_case_ids` 这类只在某一种 schema
# 上才有的字段——`S3Structured` 与 `S3Derived` 字段名相同（R52 的设计），
# 这四条规则因此对两种 schema 都直接生效，不用为哪种模式各写一份。


def _theory_unavailable(rule: str) -> Unverifiable:
    return Unverifiable(
        rule=rule, herbs=(), missing_predicate="医理规则层数据",
        reason="data/standard/tcm_theory.jsonl 不在（或未生成），这条规则一次都没跑",
    )


def check_principle_matches_syndrome(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """治法（第 3 步）跟辨出的脏腑（第 1 步）对应的治则推导规则有没有交集。

    按 `s3.organs` 的脏腑名查 `core.theory.principles_for`（不给病性，只按
    病位查——两种 schema 都保证有 organs，不保证有干净的病性标注）。查到的
    每条规则带 `method_keywords`（如「疏肝」「理气」），`method.principle`
    只要覆盖到其中一条关键词就算对得上——治法允许比规则更具体，但不能
    完全脱节。

    查不到任何对应规则时**判不了，不是通过**：医理规则层目前 171 条，
    覆盖不到的脏腑组合不代表这条治法就有问题。
    """
    organ_names = [o.organ for o in s3.organs]
    if not load_theory():
        return [], [_theory_unavailable("principle_matches_syndrome")], []
    candidates = theory_principles_for([], organ_names)
    if not candidates:
        return [], [Unverifiable(
            rule="principle_matches_syndrome", herbs=(), missing_predicate="治则规则",
            reason=f"脏腑 {organ_names} 在医理规则层查不到对应的治则推导规则，"
                   "这条无法判定",
        )], []
    principle_text = s3.method.principle
    matched = [r for r in candidates
              if any(kw in principle_text for kw in r.payload["method_keywords"])]
    if matched:
        return [], [], ["principle_matches_syndrome"]
    kw_all = sorted({kw for r in candidates for kw in r.payload["method_keywords"]})
    return [Violation(
        rule="principle_matches_syndrome", severity="revise",
        herbs=tuple(_herb_names(s3)),
        reason=f"治法「{principle_text}」跟脏腑 {organ_names} 对应的治则推导规则"
               f"（关键词：{'、'.join(kw_all) or '（该规则未标关键词）'}）没有任何交集",
        counterexample=f"[{candidates[0].id}] {candidates[0].span}",
    )], [], ["principle_matches_syndrome"]


def check_method_not_contraindicated(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """治法（含 targets）有没有用到脏腑对应治则规则里明确列为禁忌的方法。

    `core/theory.py` 的 `TREATMENT_PRINCIPLES` 里相当一部分规则同时带
    `method_keywords`（该怎么治）与 `contraindicated_methods`（不该怎么治）
    ——这条规则直接查后者，`principle_matches_syndrome` 查前者，两条互补
    但不是同一件事：治法可以既不在推荐关键词里、也没踩中禁忌（判两条都不违规，
    只是这一步"依据不算强"，那是 `S3Derived.insufficient` 该报的事，不是这里）。
    """
    organ_names = [o.organ for o in s3.organs]
    if not load_theory():
        return [], [_theory_unavailable("method_not_contraindicated")], []
    candidates = theory_principles_for([], organ_names)
    if not candidates:
        return [], [Unverifiable(
            rule="method_not_contraindicated", herbs=(), missing_predicate="治则规则",
            reason=f"脏腑 {organ_names} 在医理规则层查不到对应的治则推导规则，"
                   "这条无法判定",
        )], []
    principle_text = s3.method.principle
    targets_text = "；".join(s3.method.targets)
    hit: tuple = ()
    for r in candidates:
        for bad in r.payload["contraindicated_methods"]:
            if bad and (bad in principle_text or bad in targets_text):
                hit = (r, bad)
                break
        if hit:
            break
    if not hit:
        return [], [], ["method_not_contraindicated"]
    rule, bad = hit
    return [Violation(
        rule="method_not_contraindicated", severity="revise",
        herbs=tuple(_herb_names(s3)),
        reason=f"治法「{principle_text}」（targets：{targets_text}）用到了「{bad}」，"
               f"而脏腑 {organ_names} 对应的治则规则明确把它列为禁忌方法",
        counterexample=f"[{rule.id}] {rule.span}",
    )], [], ["method_not_contraindicated"]


def check_pathomechanism_consistent(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """辨出的多个病变脏腑之间，医理规则层查不查得到关联。

    只有一个脏腑时没什么可核对的关系，直接算过。两个及以上时，要求
    脏腑两两之间至少有一条真实的藏象关系（`organ_relations`，如"肝木克脾土"）
    或病机传变（`transitions`）能把它们联系起来——不是随便把几个脏腑摆在一起
    就算"辨证"，脏腑之间总该有个说得清的理由。
    """
    organ_names = [o.organ for o in s3.organs]
    if not load_theory():
        return [], [_theory_unavailable("pathomechanism_consistent")], []
    if len(organ_names) < 2:
        return [], [], ["pathomechanism_consistent"]
    for a in organ_names:
        for rule in theory_organ_relations(a):
            if rule.payload["object"] in organ_names:
                return [], [], ["pathomechanism_consistent"]
    if theory_transitions(organ_names):
        return [], [], ["pathomechanism_consistent"]
    return [Violation(
        rule="pathomechanism_consistent", severity="revise",
        herbs=tuple(_herb_names(s3)),
        reason=f"病变脏腑 {organ_names} 两两之间，医理规则层查不到任何藏象关系或"
               "病机传变能把它们联系起来",
        counterexample=f"已查 {len(organ_names)} 个脏腑两两间的藏象关系与病机传变，均无匹配"
                       "——如果这几个脏腑确实相关，应当在 organs 的 pathogenesis 里"
                       "写清楚是通过哪条医理关联起来的",
    )], [], ["pathomechanism_consistent"]


def check_role_structure_by_rule(s3, ont) -> tuple[list[Violation], list[Unverifiable], list[str]]:
    """君药有没有针对主病机。

    跟既有 `role_structure` 不是一回事：那条数的是君臣佐使的**数量**关系，
    这条查的是君药的**去向**对不对——配伍理论里"君药"的定义是"针对主病或
    主证起主要治疗作用的药物"（`core.theory.role_construction_rules` 里那条
    `relation="君药"` 的规则），所以君药的 `for_element` 该落在辨出来的脏腑
    （`s3.organs`）上，不能只针对治法 `targets` 里派生出来的某条兼夹症状
    ——那样这味药更像佐使，不该标君。
    """
    if not load_theory():
        return [], [_theory_unavailable("role_structure_by_rule")], []
    chief_rule = next(
        (r for r in theory_role_construction_rules() if r.payload["relation"] == "君药"), None)
    if chief_rule is None:
        return [], [Unverifiable(
            rule="role_structure_by_rule", herbs=(), missing_predicate="君药定义规则",
            reason="配伍理论里没有「君药」这条定义规则，这条无法判定",
        )], []
    primary = {o.organ for o in s3.organs}
    chiefs = [c for c in s3.herb_choices if c.item.role == "君"]
    if not chiefs:
        # 一味君药都没标：`role_structure` 已经把"role 全空"这件事报过了
        # （unverifiable 或"没有君药"违规），这里不重复报，算过就是。
        return [], [], ["role_structure_by_rule"]
    stray = sorted({c.item.name for c in chiefs if c.for_element not in primary})
    if not stray:
        return [], [], ["role_structure_by_rule"]
    return [Violation(
        rule="role_structure_by_rule", severity="revise", herbs=tuple(stray),
        reason=f"君药 {stray} 的 for_element 不在辨出的病变脏腑 {sorted(primary)} 里，"
               "只针对了治法 targets 里的某条派生目标——君药理应针对主病机",
        counterexample=f"[{chief_rule.id}] {chief_rule.span}",
    )], [], ["role_structure_by_rule"]


class BatchedOntology:
    """把一张方里的药名**一次全解析完**，七条规则共用这一份。

    R40 实测的动机：七条规则各自逐味 `ont.herb()`，而 `herb()` 每次都要跑一遍
    `normalize_herb`（去炮制前缀、去剂量、查别名）。一张 12 味的方 = 7×12 = 84 次
    归一 + 84 次查表，其中 72 次是重复劳动。改成一次 `herbs_batch()` 之后是
    12 次。`_span_of()` 也走 `herb()`，所以它一起受益。

    **委托而不是继承**：`Ontology` 的其余方法（`is_incompatible`、
    `formulas_for_syndrome`、`herbs`、`available`…）原样透出去，这个类只拦
    `herb()` 一个方法。继承会把"本体是什么"和"这一次验证怎么查得快"两件事
    绑在一个类型上，而换本体实现时前者要能替换、后者不该跟着改。

    缓存范围是**一次 `verify_formula` 调用**——不是进程级缓存：本体可以被
    `reset_ontology_for_tests()` 换掉，跨调用缓存会让换本体之后的验证读到旧值。
    """

    def __init__(self, ont: Ontology, names: list[str] | tuple[str, ...]) -> None:
        self._ont = ont
        self._resolved = ont.herbs_batch(names)
        #: 批量表命中/未命中的次数。**报出来**：如果 misses 远大于 hits，说明
        #: 预解析的名字集合取错了（规则在查方子以外的药名），批量化就没生效。
        self.hits = 0
        self.misses = 0

    def herb(self, name: str):
        if name in self._resolved:
            self.hits += 1
            return self._resolved[name]
        self.misses += 1
        return self._ont.herb(name)

    def __getattr__(self, attr):
        return getattr(self._ont, attr)


def _all_names(s3: _S3StructuredBase) -> list[str]:
    """预解析要覆盖的全部药名：方中药 + `herb_choices` 里的药。

    **两处都要**：`check_herb_grounded` 遍历的是 `herb_choices`，它跟
    `formula.candidate.herb_items` 通常一致但 schema 上是两个字段，
    只取前者会让 grounded 那一条全部落到 `misses` 上。
    """
    names = [i.name for i in _items(s3)]
    names += [c.item.name for c in getattr(s3, "herb_choices", ())]
    return names


#: 规则名 → 实现。`verify_formula` 按 `ALL_RULES` 的顺序跑，**不按字典顺序**。
RULE_FUNCS = {
    "incompatible_pair": check_incompatible_pair,
    "dose_exceeds": check_dose_exceeds,
    "herb_grounded": check_herb_grounded,
    "meridian_coverage": check_meridian_coverage,
    "nature_conflict": check_nature_conflict,
    "effect_matches_method": check_effect_matches_method,
    "role_structure": check_role_structure,
    "principle_matches_syndrome": check_principle_matches_syndrome,
    "method_not_contraindicated": check_method_not_contraindicated,
    "pathomechanism_consistent": check_pathomechanism_consistent,
    "role_structure_by_rule": check_role_structure_by_rule,
}


def verify_formula(s3: _S3StructuredBase, *, ontology: Ontology | None = None
                   ) -> VerificationResult:
    """跑十一条规则（R34 七条 + R53 四条）。**两个数据源分别判断可用性**：
    本体不可用时 `ONTOLOGY_RULES` 那七条全部进 unverifiable，医理规则层
    不可用时 `THEORY_RULES` 那四条全部进 unverifiable——两件事独立发生，
    一个数据源缺了不该连累另一个数据源能判的规则（模块文档字符串那条）。

    这是这一层最要紧的一条语义：某个数据源不在的机器上，那个数据源能管的
    规则必须报成"一条都没验"（`status="partially_verified"`、`passed=False`），
    否则那句话在没有那份数据的环境里恒真，而它恒真时毫无意义。
    """
    ont = ontology if ontology is not None else get_ontology()
    violations: list[Violation] = []
    unver: list[Unverifiable] = []
    checked: list[str] = []

    if not ont.available:
        unver.extend(
            Unverifiable(rule=r, herbs=(), missing_predicate="药理层数据",
                         reason="本体不可用（data/standard/materia_medica.jsonl 与 "
                                "formulary.jsonl 不在），这条规则一次都没跑")
            for r in ONTOLOGY_RULES)
    else:
        # R40：**7N → 1**。七条规则原先各自逐味查本体，这里一次解析完再共用。
        # 包一层而不是改规则的签名：规则的入参形状是这一层的公开契约
        # （`(s3, ont) -> (violations, unverifiable, checked)`，医院要增补规则就照它写），
        # 为了查得快去改那个契约，等于让每一条将来新增的规则都背上批量表这个细节。
        batched = BatchedOntology(ont, _all_names(s3))
        for rule in ONTOLOGY_RULES:
            v, u, c = RULE_FUNCS[rule](s3, batched)
            violations.extend(v)
            unver.extend(u)
            checked.extend(c)

    theory_available = bool(load_theory())
    if not theory_available:
        unver.extend(_theory_unavailable(r) for r in THEORY_RULES)
    else:
        for rule in THEORY_RULES:
            v, u, c = RULE_FUNCS[rule](s3, ont)  # 这四条不用 ont，签名对齐只为一致
            violations.extend(v)
            unver.extend(u)
            checked.extend(c)

    return VerificationResult(
        violations=tuple(violations), unverifiable=tuple(unver),
        ontology_available=ont.available, theory_available=theory_available,
        checked_rules=tuple(checked),
    )


# ---------- R40：投机执行（流式期间先跑"只看药名"的那两条规则） ----------

#: 只需要**药名（+剂量）**就能判的规则。这两条不依赖证型/治法/君臣佐使，
#: 所以 S3 还在流式输出、药名刚出来时就能先跑。
#:
#: 为什么只有这两条：`herb_grounded` 要 `ontology_refs`（模型写在后面），
#: `meridian_coverage` 要 `organs`，`nature_conflict` 要证型名，
#: `effect_matches_method` 要治法，`role_structure` 要 role——都在药名之后才有。
#: **表里多放一条就是在不完整的输入上下结论**，那比晚一点知道糟得多。
INCREMENTAL_RULES: tuple[str, ...] = ("incompatible_pair", "dose_exceeds")


def verify_incremental(items: list[HerbItem] | tuple[HerbItem, ...], *,
                       ontology: Ontology | None = None) -> VerificationResult:
    """只用药名+剂量能判的那两条规则，**流式期间就能跑**。

    ## 这一项省的不是吞吐，是"知道得早"

    R40 实测：完整七条规则在一张 12 味的方上是 **0.137 ms**（批量查表之后），
    所以"提前把一部分活干掉"在耗时上省不出任何东西——这一点必须先说清楚，
    否则这个函数看起来像一个没有收益的优化。

    它真正的价值是**临床反馈的时机**：配伍禁忌（十八反十九畏）和超药典上限
    是 veto 级的，一旦命中这张方根本不会下发。等整段 S3 输出完（真实后端上
    几十秒到几分钟）再告诉医生"这张方作废了"，那几十秒是白等的。药名一出来
    就能判，就能当场发一个警示事件。

    ## 结果不复用进最终验证

    最终的 `verify_formula` 照样把七条全跑一遍，**不跳过这两条**。理由：
    流式期间拿到的药名是**可能不完整的**（解析中途的 JSON），在不完整输入上
    得出的"通过"不能算通过。投机执行的定义就是"结果可能作废"，把它当成
    已经验过的部分会让"符号验证通过"这句话失去意义。
    """
    ont = ontology if ontology is not None else get_ontology()
    if not ont.available:
        return VerificationResult(
            violations=(),
            unverifiable=tuple(
                Unverifiable(rule=r, herbs=(), missing_predicate="药理层数据",
                             reason="本体不可用，这条规则一次都没跑")
                for r in INCREMENTAL_RULES),
            ontology_available=False, checked_rules=())
    shim = _ItemsOnlyS3(tuple(items))
    batched = BatchedOntology(ont, [i.name for i in items])
    violations: list[Violation] = []
    unver: list[Unverifiable] = []
    checked: list[str] = []
    for rule in INCREMENTAL_RULES:
        v, u, c = RULE_FUNCS[rule](shim, batched)
        violations.extend(v)
        unver.extend(u)
        checked.extend(c)
    return VerificationResult(violations=tuple(violations), unverifiable=tuple(unver),
                              ontology_available=True, checked_rules=tuple(checked))


class _ItemsOnlyS3:
    """喂给那两条规则的最小壳：它们只读 `formula.candidate.herb_items`。

    为什么不构造一个真的 `S3Structured`：那个 schema 的每个字段都有
    `Field(min_length=1)` 防幻觉约束（CLAUDE.md 那条铁律），流式中途根本
    填不出合法值。**正确做法是新建一个不含那些字段的形状，不是放松原来的约束**
    ——铁律原文就是这么写的。这个壳不是 pydantic 模型、不参与任何对外契约，
    只在本模块内部活一瞬间。
    """

    def __init__(self, items: tuple[HerbItem, ...]) -> None:
        self.formula = type("_F", (), {"candidate": type("_C", (), {"herb_items": items})()})()


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


#: `_corpus_herb_counts` 的缓存：{(路径, mtime_ns, size): Counter}。
#: 进程级、只增不清（一次运行里语料最多换一两次），带锁是因为 api/main.py
#: 是多线程并发问诊。
_corpus_counts_cache: dict = {}
_corpus_counts_lock = threading.Lock()


def reset_corpus_counts_cache() -> None:
    """清缓存。测试用——**不是**给业务代码用的：业务侧靠 (mtime, size) 自然失效。"""
    with _corpus_counts_lock:
        _corpus_counts_cache.clear()


def _corpus_herb_counts(path, *, cache: bool = True):
    """医案语料里每种归一药名出现多少次。缓存纪律见
    `ontology_coverage_of_corpus` 的文档字符串。"""
    import collections
    import json

    key = None
    if cache:
        try:
            st = path.stat()
            key = (str(path), st.st_mtime_ns, st.st_size)
        except OSError:
            key = None            # stat 不了就不缓存，别为了缓存去猜一个键
        if key is not None:
            hit = _corpus_counts_cache.get(key)
            if hit is not None:
                return hit
    from core.herbs import normalize_herb

    counts: collections.Counter = collections.Counter()
    for case in json.loads(path.read_text(encoding="utf-8")):
        for raw in (case.get("herbs") or []):
            name = normalize_herb(raw)
            if name:
                counts[name] += 1
    # 空表不缓存（文件可能在进程起来之后才生成）
    if key is not None and counts:
        with _corpus_counts_lock:
            _corpus_counts_cache[key] = counts
    return counts


def ontology_coverage_of_corpus(*, ontology: Ontology | None = None,
                                cases_path=None) -> dict:
    """本体覆盖了医案语料里多少种药名。**这是数据质量指标，不是模型指标。**

    跟 `herbs_grounded_ratio` 放在一起定义，正是为了让两者的分母不会被混用：
      - `herbs_grounded_ratio`：分母 = **这张方**的药味数（模型指标）
      - 本函数：分母 = **医案语料**里出现的药名种数（数据指标）

    同时报"按种数"和"按出现次数"两个比值：实测 230/578 = 39.8%（按种数）
    但 3380/4799 = 70.4%（按次数）——常用药覆盖得好，古籍特有写法的长尾覆盖差。
    只报前者会低估它对真实问诊的支撑，只报后者会掩盖长尾缺口。

    ## 药名计数按文件签名缓存（R36 补）

    这个函数每次 consult 都会被 manifest 调一次，而它要读 1MB 的 `cases.json`
    再把 4799 次药名逐个归一——实测 **52ms/次**，纯属白花（语料不变时结果恒定）。

    缓存的三条纪律跟 `context_prefix.build_entry_index` 那处一致：
      1. **只缓存"从默认路径读全量语料"这一路**：调用方自己给了 `cases_path`
         的那一路不碰缓存（测试常拿 tmp_path 造小语料，缓存会让下一个调用
         读到别人的表）；
      2. **空结果不缓存**：文件可能在进程起来之后才生成；
      3. 键带上文件的 `(mtime_ns, size)`——重抽了语料就自然失效，那正是它该
         失效的时机（同 `_prefix_tokens_or_none` 按 sha 记忆化的做法，只是这里
         不必再读一遍文件算 sha）。
    本体不进键：`ont.herb()` 的查表在缓存之外，换本体照样重算命中集合。
    """
    from pathlib import Path

    ont = ontology if ontology is not None else get_ontology()
    p = Path(cases_path) if cases_path else (
        Path(__file__).resolve().parent.parent / "cases.json")
    if not p.exists():
        return {"available": False, "note": f"{p.name} 不在，覆盖率算不了"}
    counts = _corpus_herb_counts(p, cache=cases_path is None)
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
