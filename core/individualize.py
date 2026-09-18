"""R46 §7.2：「人」这一维的个体化调整——同一个证，不同的人用药不一样。

对标黄煌的「方—病—人」模式。此前这条推理链上完全没有"人"：只有症状、证素、
证型、方；一张给 68 岁高血压老太太的方和给 8 岁孩子的方，系统给出来是同一张。

## 每一条都必须指得出依据，指不出就不产出

`IndividualizationItem.basis` 是 `Field(min_length=1)`——跟 `cited_case_ids`
同一条防幻觉纪律。所以这个模块的规则**全部从本体现查**：

| 维度 | 依据来自哪 | 实测条数 |
|---|---|---|
| 妊娠/哺乳 | 本草本体「禁忌」谓词里提到孕的条目 | 210 条（《中药学》） |
| 毒峻药（老年慎用） | 本草本体「性味」里标有毒/小毒/大毒 | 225 条（《中药学》） |
| 儿童剂量 | 本草本体「用量」+ `core.safety_output.DOSE_LIMITS` | 见那两处 |
| 肝肾功能不全 | 本体「禁忌」里提到肝/肾的条目 | 现查 |
| 过敏史 | 医师填的表单本身 | — |
| **在服西药相互作用** | **这个项目没有药物相互作用本体** | **0：不提示，如实记在 `considered` 里** |

最后一行是这个模块最要紧的一条：**取不到依据的维度，不产出条目，但要说
"查过了、没有依据可用"**。空的 `items` 配上非空的 `considered`，才说得清
"查过了没有需要调的"和"根本没查"的区别。

## 这一层不改方，只提示

它产出的是**给医师看的调整建议清单**，不直接改 `formula_candidates`——
处方是医师签的（见 docs/glossary.md「方剂」与「处方」的区别）。
"""
from __future__ import annotations

from core.herbs import strip_dose_and_parens
from core.schemas import Individualization, IndividualizationItem, PatientProfile

#: 毒性标记。本草「性味」原文里的写法就这三种。
TOXIC_MARKERS = ("大毒", "有毒", "小毒")

#: 需要按"峻药慎用"提示的生理阶段。
FRAIL_STAGES = ("老年", "婴幼儿", "儿童")

#: 儿童剂量折算的口径。**不是一个可以直接抄进处方的数**——教材给的是范围，
#: 具体由医师定。这里只把成人量摆出来并注明折算惯例。
PEDIATRIC_NOTE = {
    "婴幼儿": "婴幼儿一般按成人量的 1/4 左右折算",
    "儿童": "学龄期儿童一般按成人量的 1/2 左右折算",
}

#: 查过但拿不到依据的维度。**如实列出来**，不是默默跳过。
NO_BASIS_DIMENSIONS = (
    "在服西药的相互作用（本项目没有药物相互作用本体，取不到依据即不提示）",
)


def _first_span(herb, predicate: str) -> tuple[str, str]:
    """取这个谓词的第一条**非空**出处：(原文片段, 书名)。没有就 ("", "")。"""
    for ref in herb.refs.get(predicate, ()):
        if ref.span.strip():
            return ref.span.strip(), ref.book
    return "", ""


def individualize(
    profile: PatientProfile | None,
    herbs: list[str] | None,
    syndrome: str = "",
) -> Individualization:
    """按「人」维查一遍这张方。

    `herbs` 是药名列表（带不带剂量都行，内部走
    `core.herbs.strip_dose_and_parens` 归一——**不另写一套去剂量的逻辑**）。
    """
    considered: list[str] = []
    items: list[IndividualizationItem] = []
    profile = profile or PatientProfile()
    names = [strip_dose_and_parens(h) for h in (herbs or [])]
    names = [n for n in names if n]

    if profile.is_empty():
        return Individualization(
            items=[],
            considered=["未填写患者的年龄/性别/体质等信息，本次没有可依据的个体化维度"],
        )

    from core.ontology import get_ontology

    onto = get_ontology()

    # 1) 妊娠 / 哺乳
    if profile.life_stage in ("妊娠期", "哺乳期"):
        considered.append(f"{profile.life_stage}用药禁忌（本草「禁忌」条目）")
        for n in names:
            herb = onto.herb(n)
            if not herb:
                continue
            span, book = _first_span(herb, "禁忌")
            if span and "孕" in span:
                items.append(IndividualizationItem(
                    kind="慎用提示", target=n,
                    adjustment=f"{profile.life_stage}慎用或禁用，请医师复核是否保留",
                    reason=f"患者处于{profile.life_stage}",
                    basis=f"《{book}》：{span}" if book else span,
                ))

    # 2) 毒峻药：老年与小儿
    if profile.life_stage in FRAIL_STAGES:
        considered.append(f"{profile.life_stage}对毒峻药的耐受（本草「性味」毒性标记）")
        for n in names:
            herb = onto.herb(n)
            if not herb:
                continue
            span, book = _first_span(herb, "性味")
            if span and any(m in span for m in TOXIC_MARKERS):
                items.append(IndividualizationItem(
                    kind="慎用提示", target=n,
                    adjustment=f"{profile.life_stage}患者慎用，如需保留请下调剂量并缩短疗程",
                    reason=f"患者为{profile.life_stage}，对毒峻药耐受差",
                    basis=f"《{book}》：{span}" if book else span,
                ))

    # 3) 儿童剂量折算
    if profile.life_stage in PEDIATRIC_NOTE:
        considered.append("小儿剂量折算（本草「用量」条目）")
        for n in names:
            herb = onto.herb(n)
            if not herb:
                continue
            span, book = _first_span(herb, "用量")
            if not span:
                continue
            items.append(IndividualizationItem(
                kind="剂量", target=n,
                adjustment=PEDIATRIC_NOTE[profile.life_stage] + "，具体由医师定",
                reason=f"患者为{profile.life_stage}",
                basis=f"成人量出自《{book}》：{span}" if book else span,
            ))

    # 4) 肝肾功能不全
    for organ, flag in (("肝", profile.hepatic_impairment), ("肾", profile.renal_impairment)):
        if flag != "有":
            continue
        considered.append(f"{organ}功能不全用药（本草「禁忌」条目）")
        for n in names:
            herb = onto.herb(n)
            if not herb:
                continue
            span, book = _first_span(herb, "禁忌")
            if span and organ in span:
                items.append(IndividualizationItem(
                    kind="慎用提示", target=n,
                    adjustment=f"{organ}功能不全者慎用，请医师复核",
                    reason=f"患者{organ}功能不全",
                    basis=f"《{book}》：{span}" if book else span,
                ))

    # 5) 过敏史：依据就是医师填的那一条，不需要本体
    if profile.allergies:
        considered.append("过敏史比对（依据为医师录入的过敏史本身）")
        for n in names:
            for a in profile.allergies:
                if a and (a in n or n in a):
                    items.append(IndividualizationItem(
                        kind="去药", target=n,
                        adjustment="建议去掉这一味",
                        reason=f"与录入的过敏史「{a}」相符",
                        basis=f"医师录入的过敏史：{a}",
                    ))

    # 6) 取不到依据的维度：**说出来**
    if profile.current_medications:
        considered.extend(NO_BASIS_DIMENSIONS)

    if syndrome:
        considered.append(f"证型「{syndrome}」下的上述各维度")

    # 同一味药可能命中多条（既毒又孕妇慎用），按 (kind, target, adjustment) 去重
    seen: set[tuple[str, str, str]] = set()
    uniq: list[IndividualizationItem] = []
    for it in items:
        key = (it.kind, it.target, it.adjustment)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    return Individualization(items=uniq, considered=considered)
