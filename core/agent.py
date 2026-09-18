"""R44：把散在推理链里的「什么时候停 / 问 / 取证 / 验」收成**一张规则表**。

## 为什么要这一层

`consult()` 里对这四件事的判断原来是九处散落的 `if`：

    if reject_reason is not None and not bypass:            → 返回一份拦截结果
    if followup.stopped_by == "safety" and not bypass:      → 返回一份拦截结果
    if reject is not None and not bypass:                   → 返回一份拦截结果
    （ReAct 追问的回答那一处又是一份）
    if not s2.elements and not residual…:                   → 返回一份信息不足结果

这有三个具体后果，**都不是风格问题**：

1. **那份"被拦截的返回值"有四份拷贝，而且已经开始漂**——其中一份带
   `"coverage": None`、另一份没有，键的顺序也各不相同。前端按同一份契约读，
   缺一个键就是 KeyError。
2. **"为什么停在这里"只存在于代码里**，返回值里只有一句 `reject_reason`。
   产品面上没法回答"系统在这一步做了什么决定、凭什么"，而三甲的主治医师问的
   正是这个。
3. **加一条新规则要改五个地方**，漏掉一处的表现是"某条路径上那个新判断没生效"
   ——正是 CLAUDE.md 第 31 条说的那种撞墙。

## 四能力

这一层**不实现任何判断**，只决定"此刻该由谁上场"。判断仍然在原来的模块里：

| 能力 | 做什么 | 实现在哪（**唯一实现**） |
|---|---|---|
| `stop` | 停下来，不产出方药 | `core.safety.check_safety` / `core.formula_verifier` / 证素为空 |
| `ask` | 信息不够就问，不硬答 | `core.followup.run_followup` |
| `gather` | 缺依据就去查 | `core.react.run_react` + `core.tools` |
| `verify` | 开完方自己验一遍，不合格就重开 | `core.formula_verifier.verify_formula` |

**"四能力"不是四个新模块**，是给已经存在的四件事起了一个统一的名字，
好让"什么时候用哪一个"能被写成一张可读、可测、可改的表。

## 规则表的三条纪律

**一、顺序即优先级，安全永远在最前。** CLAUDE.md 那条"安全否决在 S2 之前"
在这里的具体形式是：`stop` 类规则排在所有 `ask`/`gather`/`verify` 之前，
而且有一条测试比这张表的顺序。

**二、每条规则都要能说出"为什么"。** `why` 是给人看的一句话，随决策一起进
`agent_trace`，前端直接显示。没有 `why` 的规则等于一个不能解释的行为。

**三、这一层不碰数据，只回答"该做什么"。** `decide()` 是纯函数：喂一份状态，
返回一个决策。所以它能被逐条断言，不用起模型、不用起服务。

## 规则表**不是控制流**（这一点容易误解）

`stop` 这一族是真的由规则表拦下来的：`consult()` 问 `decide()`，命中就返回。

`ask` / `gather` / `verify` 这三族**不是**——它们仍然由 `consult()` 按流程直接调，
规则表只在它们发生之后记一笔。这是有意的：那三件事的触发条件天然在各自模块
内部（`run_followup` 自己算信息增益、`run_react` 自己决定要不要再走一步、
验证器自己按十一条规则判），把触发权挪到这张表，就会变成"表里放一个空壳条件、
真条件还在模块里"——**那正是这一层要消灭的第二处实现**。

所以规则表在这三条上的作用是**命名与解释**（这件事叫什么、为什么做），
不是控制流。写在这里是为了让下一个想"把它们也改成 decide() 驱动"的人先看到
这段话。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal

#: 四能力。**顺序就是它们在一次问诊里出现的先后**（停 > 问 > 取证 > 验），
#: 不是字母序。
Capability = Literal["stop", "ask", "gather", "verify"]

CAPABILITIES: tuple[Capability, ...] = ("stop", "ask", "gather", "verify")

#: 能力的中文名。**后端下发，前端不写死**（同 `CHAIN_LAYERS` 的层名那条）。
CAPABILITY_LABEL: dict[str, str] = {
    "stop": "中止",
    "ask": "追问",
    "gather": "取证",
    "verify": "自验",
}

#: 中止的三类去向。**三类的产品含义完全不同，不许合并成一个"失败"**：
#:   safety   —— 危重，要的是让人立刻去急诊，不是换一张方；
#:   evidence —— 依据不足，要的是补信息；
#:   veto     —— 方子自己没过验证，要的是重开或人工介入。
StopKind = Literal["safety", "evidence", "veto"]

STOP_KIND_LABEL: dict[str, str] = {
    "safety": "危重拦截",
    "evidence": "依据不足",
    "veto": "验证否决",
}


@dataclass(frozen=True)
class AgentRule:
    """一条规则。**`gate` 写的是判断的实现在哪，不是判断本身**。

    这个字段不是注释：有一条测试拿它去核实"这条规则说的那个模块真的存在、
    而且这一层没有自己再实现一遍"。
    """

    id: str
    capability: Capability
    #: 给人看的一句话，随决策一起下发。
    why: str
    #: 判断的唯一实现在哪（`模块:符号`）。
    gate: str
    #: 只有 `capability == "stop"` 时有意义。
    stop_kind: StopKind | None = None
    #: 这条规则能不能被 `EVAL_MODE` 放行。**安全类永远不能**——评测要量化
    #: "安全否决花了多少分"，靠的是 `safety_flag` 如实记录，不是让它不触发。
    #: 见 `core.safety.safety_bypassed` 的文档。
    bypassable: bool = False


#: **顺序即优先级。** 前面的先判，命中就停。
#:
#: 安全三条排在最前不是排版：CLAUDE.md 的「安全否决在 S2 之前」与「追问的回答
#: 必须先过 check_safety」两条铁律，在这张表里就是这三条的位置。
AGENT_RULES: tuple[AgentRule, ...] = (
    AgentRule(
        id="danger_in_complaint",
        capability="stop", stop_kind="safety", bypassable=True,
        why="主诉或症状里出现危重表述，先去急诊——这一步在证素推断之前，不产出任何方药。",
        gate="core.safety:check_safety",
    ),
    AgentRule(
        id="danger_in_followup_answer",
        capability="stop", stop_kind="safety", bypassable=True,
        why="追问问出了危重症状，按同一道否决处理——追问是安全层的后门，这里堵上。",
        gate="core.safety:check_safety",
    ),
    AgentRule(
        id="danger_in_asserted",
        capability="stop", stop_kind="safety", bypassable=True,
        why="追问确认下来的症状里仍有危重表述（双保险，防 followup 的判据将来被改松）。",
        gate="core.safety:check_safety",
    ),
    AgentRule(
        id="danger_in_react_answer",
        capability="stop", stop_kind="safety", bypassable=True,
        why="取证过程中向患者追问，回答里出现危重表述——回答进证素推断之前先过安全层。",
        gate="core.safety:check_safety",
    ),
    AgentRule(
        id="no_elements",
        capability="stop", stop_kind="evidence",
        why="证素层为空，结构化推理没有落点。继续开方等于绕开证素看主诉猜证型，"
            "那样的方药没有可追溯的依据——宁可如实说信息不足。",
        gate="core.chain:infer_elements",
    ),
    AgentRule(
        id="symbolic_veto",
        capability="stop", stop_kind="veto",
        why="符号验证器给出 veto 级违规（配伍禁忌/超量），重开若干轮之后仍未通过。",
        gate="core.formula_verifier:verify_formula",
    ),
    AgentRule(
        id="ask_for_missing_symptoms",
        capability="ask",
        why="现有症状还不足以把证素分辨开，问几个信息量最大的问题——"
            "每轮 0 次模型调用（规则解析 + 图上贝叶斯更新）。",
        gate="core.followup:run_followup",
    ),
    AgentRule(
        id="gather_evidence",
        capability="gather",
        why="开方之前先去医案与本体里取证，让每一条结论都能指回它依据的原文。",
        gate="core.react:run_react",
    ),
    AgentRule(
        id="verify_and_revise",
        capability="verify",
        why="开完方自己按十一条规则验一遍（本体七条 + R53 医理一致性四条），"
           "有 veto/revise 级违规就把违规回灌给模型重开。",
        gate="core.formula_verifier:verify_formula",
    ),
    # R46 §7.2 第 4 条：「人」这一维。**跟上面那条 verify 是两件事**——
    # 那条验的是"这张方本身立不立得住"（配伍、剂量、归经），这条验的是
    # "这张方对**这位患者**合不合适"（妊娠、小儿、老年、肝肾功能、过敏史）。
    # 合成一条的话，"方是对的但人不对"会被说成方有问题。
    AgentRule(
        id="verify_patient_fit",
        capability="verify",
        why="按患者的年龄/生理阶段/肝肾功能/过敏史再核一遍这张方，"
            "每条提示都指得出本草原文；取不到依据的维度不提示、但要说已经查过。",
        gate="core.individualize:individualize",
    ),
)

RULES_BY_ID: dict[str, AgentRule] = {r.id: r for r in AGENT_RULES}


@dataclass(frozen=True)
class AgentDecision:
    """一次决策。**`detail` 是那条规则的具体证据**（命中的危重词、违规条目…），
    `why` 是规则本身的理由——两者分开：理由是固定的，证据是这一次的。"""

    rule_id: str
    capability: Capability
    why: str
    stop_kind: StopKind | None = None
    detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "rule": self.rule_id,
            "capability": self.capability,
            "capability_label": CAPABILITY_LABEL[self.capability],
            "why": self.why,
            "stop_kind": self.stop_kind,
            "stop_kind_label": STOP_KIND_LABEL.get(self.stop_kind or "", None),
            "detail": self.detail,
        }


@dataclass
class AgentTrace:
    """这一次问诊里代理做过的全部决策，按发生顺序。

    **不是日志**：它进响应体，产品面直接显示"我在这一步做了什么、凭什么"。
    这也是「消除投票痕迹」的另一半——读者看到的是**一位医师的决策过程**，
    不是"几个人投了票"。
    """

    decisions: list[AgentDecision] = field(default_factory=list)
    #: R55：每记一条决策就同步往 SSE 发一个 `agent_step` 事件，(事件名, 数据) -> None。
    #: 不参与 `__eq__`/`__repr__`——它是一次性的回调对象，不是这份 trace 的数据。
    #: 不传（CLI、eval/、批跑现状）就完全不影响原来的行为，跟 `consult()` 的
    #: `on_step` 同一条纪律。**改造前 `agent_trace` 只在整次问诊结束时随最终
    #: 结果一起下发**——四能力（停/问/取证/验）做过什么，前端只能等到最后
    #: 才知道；这里把同一份数据在发生的当下也发一遍，不是新造一层机制。
    on_step: Callable[[str, dict], None] | None = field(default=None, repr=False, compare=False)

    def append(self, d: AgentDecision) -> AgentDecision:
        """加一条已经构造好的决策（`decide()` 的返回值）并广播。

        `record()` 和直接 `trace.decisions.append(...)` 曾经是两条并行的写入
        路径——前者走 `record()`、后者在 `core/chain.py` 里散落四处直接操作
        `.decisions` 这个列表，`on_step` 广播只接在其中一条上就会有一半的决策
        不广播。CLAUDE.md「同一概念的匹配逻辑只能有一处实现」：写入 + 广播现在
        只有这一处实现，`record()` 和调用方都改成走它。
        """
        self.decisions.append(d)
        if self.on_step is not None:
            self.on_step("agent_step", d.to_dict())
        return d

    def record(self, rule_id: str, detail: str | None = None) -> AgentDecision:
        rule = RULES_BY_ID.get(rule_id)
        if rule is None:
            raise KeyError(f"没有这条规则：{rule_id}（规则表在 core/agent.py）")
        d = AgentDecision(rule_id=rule.id, capability=rule.capability, why=rule.why,
                          stop_kind=rule.stop_kind, detail=detail)
        return self.append(d)

    def stopped(self) -> AgentDecision | None:
        """有没有停过。**取第一条**：一次问诊最多停一次，后面的都不会发生。"""
        for d in self.decisions:
            if d.capability == "stop":
                return d
        return None

    def to_list(self) -> list[dict]:
        return [d.to_dict() for d in self.decisions]


def decide(rule_id: str, hit: object, *, bypass: bool = False) -> AgentDecision | None:
    """一条规则的判定结果 → 决策（或 None）。

    `hit` 是那条规则的**实现**返回的东西（`check_safety` 的命中词、
    `verify_formula` 的违规列表…）。**这一层不判断，只翻译**——判断在
    `gate` 指的那个模块里，这里再判一遍就是第二处实现。

    `bypass` 只对 `bypassable=True` 的规则有效（`EVAL_MODE`）。安全类规则
    标了 `bypassable=True` 是因为评测要让被拦的主诉也走完一遍拿到分数，
    而"本来会被拦"这件事照样如实记在 `safety_flag` 里——**放行的是流程，
    不是记录**。
    """
    rule = RULES_BY_ID.get(rule_id)
    if rule is None:
        raise KeyError(f"没有这条规则：{rule_id}（规则表在 core/agent.py）")
    if not hit:
        return None
    if bypass and rule.bypassable:
        return None
    return AgentDecision(rule_id=rule.id, capability=rule.capability, why=rule.why,
                         stop_kind=rule.stop_kind,
                         detail=str(hit) if not isinstance(hit, bool) else None)


def rules_for(capability: Capability) -> tuple[AgentRule, ...]:
    return tuple(r for r in AGENT_RULES if r.capability == capability)


def stop_rules_come_first() -> bool:
    """**安全永远在最前**这条纪律的可执行形式。有一条测试直接断言它。"""
    order = [CAPABILITIES.index(r.capability) for r in AGENT_RULES]
    return order == sorted(order)


# ---------- R44：消除投票痕迹 ----------
#
# 这个系统的内部机制里确实有"几位医家各出一份结论再合起来"这一步（legacy 三列
# 模式），而**那是研究能力，不是产品形态**。总纲 §12 的原话是「能力不删，
# 产品面不露」。
#
# 具体到措辞：产品面（患者/医生/学生）上不许出现把结论说成"投票/表决/几家综合"
# 的词——读者要看到的是**一位医师的推理过程**，名老中医经验是它引用的**依据**，
# 不是投票人。研究面（researcher 角色）照旧给全部三列与分歧读数。
#
# 这张表是那条约束的唯一定义，测试从它取词，不手抄。
VOTING_WORDS: tuple[str, ...] = (
    "投票", "表决", "少数服从多数", "多数票", "得票", "票数", "各占一票",
)

#: 这些词本身不是"投票"，但把它们摆在**结论**上就是在说"这是几个人拼出来的"。
#: 允许出现在研究面与说明文字里，不允许出现在产品面的结论措辞里。
ENSEMBLE_WORDS: tuple[str, ...] = ("五家综合", "三家综合", "综合三位", "综合五位")


def has_voting_language(text: str) -> str | None:
    """文本里有没有投票措辞。命中返回那个词，没有返回 None。

    **一处实现**：前端的判据、测试的判据、产品面的过滤都走这一个函数，
    各写一套正则必然漂。
    """
    s = text or ""
    for w in (*VOTING_WORDS, *ENSEMBLE_WORDS):
        if w in s:
            return w
    return None
