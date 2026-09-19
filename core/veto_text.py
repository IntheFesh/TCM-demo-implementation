"""R65：验证器规则 id → 给人看的一句话。**产品面文案的唯一出处。**

## 为什么要有这个模块

用户真机截图里，医师角色下整页显示：

    请尽快就医
    这张方在符号验证中有不可下发的问题（herb_source_fabricated），…
    本页不提供方药内容。

三处错：内部规则 id 印在了界面上（R62 §7.2 明令不许）；用了红旗症状专用的
措辞（「请尽快就医」是患者遇到危重征象才说的话，跟验证否决毫无关系）；
以及最根本的——医师被当成了需要保护的患者。

前两处的根因是**同一个**：`SymbolicVeto.reason` 那句话是给日志/研究模式写的，
却被响应直接转给了前端。**这个模块把"给人看的话"和"给排障看的细节"分开**，
产品面只拿前者。

## 一处实现

`api/main.py` 渲染响应时调这里，前端拿到的就已经是人话——**不是前端自己映射**。
理由跟 R63 的经典方候选一样：判据在服务端，前端另写一份就会漂，
而这一份漂掉的表现是"界面上又出现了规则 id"，正是这一轮要修的事。

`RULE_TEXT` 必须覆盖 `formula_verifier.ALL_RULES` 的每一条——少一条就会退回
显示 id，所以 `tests/test_veto_presentation.py` 拿两边的名单对比，缺一条红一条。
"""
from __future__ import annotations

from core.formula_verifier import ALL_RULES, VETO_RULES

#: 规则 id → 一句人话。`{herbs}` 会被换成涉及的药味。
#:
#: 写法上的三条规矩：
#:   1. **不出现规则 id、字段名、本体路径**；
#:   2. 说**现象**不说判据实现（"出处与本草原文对不上"，不是
#:      "source_span 在 ontology 里匹配失败"）；
#:   3. 不下判断说谁的错——"可能是生成有误"而不是"模型编造了出处"，
#:      医师看到的应该是一个待核项，不是一个结论。
RULE_TEXT: dict[str, str] = {
    # ---- veto 级（改了 MAX_REVISE_ROUNDS 轮仍在，方不下发）----
    "incompatible_pair": "方中{herbs}属十八反/十九畏，同方相见不可下发",
    "dose_exceeds": "{herbs}的剂量超出药典常用上限",
    "herb_source_fabricated": "方中{herbs}的出处与本草原文对不上，可能是生成有误",
    # ---- revise 级（回灌重开用，正常不会走到 veto 展示，但也要有人话）----
    "herb_not_in_ontology": "{herbs}在本系统的本草库里查不到，可能是药名写法问题",
    "herb_source_paraphrased": "{herbs}引用的原文是转述而非原句",
    "meridian_coverage": "{herbs}的归经与本证的病位对不上",
    "nature_conflict": "{herbs}的药性与本证的寒热方向相反",
    "effect_matches_method": "{herbs}的功效与本次治法对不上",
    "role_structure": "方中的君臣佐使结构不完整",
    "principle_matches_syndrome": "治法与所辨证型对不上",
    "method_not_contraindicated": "治法与本证的禁忌相冲",
    "pathomechanism_consistent": "病机链与所辨证型对不上",
    "role_structure_by_rule": "君臣佐使的分配与配伍规则对不上",
}

#: 医师/学生看到的那条黄条。**不是红色整页**——验证没过是一个待核项，
#: 不是急症。措辞里刻意有"以下内容供参考"：完整推导与方剂照常显示。
DOCTOR_BANNER = (
    "系统核查未通过：{summary}。以下内容供参考，导出已暂时禁用。"
    "请人工复核后再使用。"
)

#: 患者看到的那句。**绝不用「请尽快就医」**——那是红旗症状专用
#: （见 `RED_FLAG_ONLY_PHRASES`）。这里说的是"这次没能给出方"，
#: 跟"你的症状危险"是两件完全不同的事。
PATIENT_NOTICE = (
    "本次未能给出可供参考的方剂。系统在核对方药与本草原文时发现不一致，"
    "按规则不予呈现。建议携带此次记录咨询医师。"
)

#: **只许出现在红旗拦截的渲染路径里**的两句话。
#: `tests/test_veto_presentation.py` 扫源码：验证否决那条路径里出现即红。
#: 这一条是"两套呈现不许串味"的可执行判据——R65 之前它们串了。
RED_FLAG_ONLY_PHRASES: tuple[str, ...] = ("请尽快就医", "本页不提供方药内容")


def _herb_phrase(herbs) -> str:
    """涉及的药味 → 「柴胡」/「甘草」与「海藻」。

    一味、两味、多味分开写：拼成「柴胡、海藻」读起来像一味药的别名，
    而十八反那条恰恰要让人看出是**两味药相见**。
    """
    names = [str(h).strip() for h in (herbs or []) if str(h).strip()]
    if not names:
        return "某一味药"
    if len(names) == 1:
        return f"「{names[0]}」"
    if len(names) == 2:
        return f"「{names[0]}」与「{names[1]}」"
    return "、".join(f"「{n}」" for n in names)


def describe(rule: str, herbs=None) -> str:
    """一条违规 → 一句人话。**认不出的规则也不许泄露 id。**

    认不出时给一句笼统但诚实的话，而不是把 id 印出来：id 印出来是
    R62 §7.2 禁的事，而"某一味药未通过核查"虽然信息少，至少不是内部编码。
    真要查是哪条规则，研究模式的 `verification` 字段里有。
    """
    tpl = RULE_TEXT.get(rule)
    if not tpl:
        return f"{_herb_phrase(herbs)}未通过系统核查"
    return tpl.format(herbs=_herb_phrase(herbs))


def summarize(violations) -> str:
    """多条违规 → 黄条里那半句。去重并保序，最多列三条。"""
    seen: list[str] = []
    for v in violations or []:
        rule = v.get("rule") if isinstance(v, dict) else getattr(v, "rule", "")
        herbs = v.get("herbs") if isinstance(v, dict) else getattr(v, "herbs", ())
        text = describe(rule, herbs)
        if text not in seen:
            seen.append(text)
    if not seen:
        return "方药与本草原文有不一致之处"
    head = "；".join(seen[:3])
    return head + (f"（另有 {len(seen) - 3} 项）" if len(seen) > 3 else "")


def flagged_herbs(violations) -> list[str]:
    """哪几味药要在处方表里标红。去重保序。"""
    out: list[str] = []
    for v in violations or []:
        herbs = v.get("herbs") if isinstance(v, dict) else getattr(v, "herbs", ())
        for h in herbs or ():
            name = str(h).strip()
            if name and name not in out:
                out.append(name)
    return out


def herb_reasons(violations) -> dict[str, str]:
    """药名 → 标在那一行旁边的短说明。一味药命中多条时用第一条。"""
    out: dict[str, str] = {}
    for v in violations or []:
        rule = v.get("rule") if isinstance(v, dict) else getattr(v, "rule", "")
        herbs = v.get("herbs") if isinstance(v, dict) else getattr(v, "herbs", ())
        short = SHORT_REASON.get(rule, "未通过核查")
        for h in herbs or ():
            out.setdefault(str(h).strip(), short)
    return out


#: 标在处方表那一行旁边的极短说明（列宽有限，一句话放不下）。
SHORT_REASON: dict[str, str] = {
    "incompatible_pair": "配伍禁忌",
    "dose_exceeds": "超药典上限",
    "herb_source_fabricated": "出处对不上",
    "herb_not_in_ontology": "本草库查不到",
    "herb_source_paraphrased": "出处是转述",
    "meridian_coverage": "归经不合病位",
    "nature_conflict": "药性相反",
    "effect_matches_method": "功效不合治法",
}


def coverage_gap() -> tuple[str, ...]:
    """`ALL_RULES` 里还没有人话版本的规则。测试拿它当判据。"""
    return tuple(r for r in ALL_RULES if r not in RULE_TEXT)


def veto_rules() -> tuple[str, ...]:
    return VETO_RULES
