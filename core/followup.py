"""追问闭环：用信息增益选下一个问题，收回答案，更新后验，再选下一个。

三条硬约束（顺序也是硬的）：

1. **回答先过 check_safety，再做别的任何事。** 追问是安全否决层的后门——S2 之前
   拦的是初始主诉，如果追问问出「有黑便」而回答直接进证素推断，那道拦截就被绕过去了
   （docs/ARCHITECTURE.md §3）。所以安全检查在解析答案之前，命中即整轮终止、不产出方药。

2. **否定回答必须进后验，不只是从候选池里去掉。** 见 core/tools.py
   syndrome_posterior 的文档字符串。

3. **每轮 0 次 LLM 调用。** 答案解析用规则（追问问的是一个具体症状的封闭问题，
   答案本质上就是是/否/不确定），后验更新是纯图计算。整个追问循环只在最后
   ——且仅当问出了新症状时——重跑一次 S2 把新症状并进证素。每轮再调一次 LLM 的话，
   成本会随追问轮数线性叠加（开了 ReAct，每位医家就已经多出数次调用）。
"""
from __future__ import annotations

import os
from typing import Callable

from core.diseases import match_disease
from core.safety import check_safety, danger_confirmed_by_answer, mentions_danger, veto_message
from core.schemas import FollowupResult, HistoryItem
from core.tools import question_candidates

MAX_ASK_ROUNDS = 3

#: 追问轮数上限的产品默认**按角色分**，不是一个全局常量。
#: 医师已经完成了望闻问切四诊，系统再追问三轮是把医师当患者审——所以医师
#: 角色限 0 轮（拒绝追问，直接按现有信息辨证或如实说证素不足）；患者角色限 1
#: 轮（多轮像被审问）。`student`（教学，追问过程本身是教学内容的一部分）与
#: `researcher`（内部角色，要看完整行为）不在这张表里，落到下面
#: `max_ask_rounds_for_role()` 的默认分支，沿用 `MAX_ASK_ROUNDS`——「可开」
#: 不是"给它另一个数"，是"不限制它"。
ROLE_MAX_ASK_ROUNDS: dict[str, int] = {
    "doctor": 0,
    "patient": 1,
}


def max_ask_rounds_for_role(role: str | None) -> int:
    """role → 追问轮数上限的**唯一映射**。这是全项目唯一一处按角色
    决定追问轮数的地方，不要在调用方各写一份 if role == "doctor" ...。

    不认识的 role（`None`、`"student"`、`"researcher"`、或任何将来加的新角色）
    一律落回 `MAX_ASK_ROUNDS`——没写进 `ROLE_MAX_ASK_ROUNDS` 就是"这个角色
    不受限制"，不是"忘了配置"。
    """
    return ROLE_MAX_ASK_ROUNDS.get(role or "", MAX_ASK_ROUNDS)

# 低于这个信息增益（单位 bit）就认为"再问一句也问不出什么了"，收敛退出——继续问的
# 收益已经低于多问一句的代价。这是策略阈值不是物理常数，改它只影响"什么时候停"，
# 不影响问题的排序。
MIN_USEFUL_IG = 0.05

# 答案解析的三张表。顺序有意义：先查不确定，再查否定，最后才查肯定——
# 「没有」里含「有」，反过来查会把所有否定读成肯定。
_UNCERTAIN = ("不知道", "不清楚", "说不清", "不确定", "不好说", "记不清", "时有时无")
_NEGATION = ("没有", "没", "不", "无", "未", "否", "从来")
_AFFIRM = ("有", "是", "对", "会", "经常", "一直", "偶尔", "确实", "嗯")
# 带限定语的肯定：「有一点，不多」「有，但不严重」——句首是肯定、后半句的「不」
# 是程度限定，不是否认。不先认出来的话它们会被归成 no，危重症状写进 denied、
# S3 收到「患者明确否认：便血」照常开方。
_AFFIRM_LEAD = ("有", "是的", "对", "嗯", "确实", "偶尔", "经常", "一直", "会")

# 提问方：给一个问题，返回患者的回答；返回 None 表示对方不打算回答（关掉了对话框、
# 命令行 Ctrl-C 等）。做成注入的函数是为了让真人、患者模拟器、前端三种来源共用
# 同一套循环——换来源改的是传进来的这个函数，不是循环本身。
AskFn = Callable[[str], str | None]


def fast_mode_enabled() -> bool:
    """网络慢或临时预算紧张时的兜底开关。默认关。

    **全项目唯一的 FAST_MODE 判定实现**，四处代码路径都调它、不各写一套：
      1. 本模块的追问循环：max_rounds 降到 0（一个问题都不问）
      2. core/react.py 的 run_react：步数上限降到 FAST_MODE_MAX_STEPS
      3. core/chain.py 的 run_residual：残差辨证整体关闭
      4. core/llm.py 的 s3_best_of_n：采样次数降到 1（best-of-N 是这条链上最大的
         成本倍数，不降它 FAST_MODE 就名不副实）
    四处必须同时生效——只关一半的开关是陷阱：用户以为省了预算，实际还在花。

    判定实现放在本模块，不另建 config 模块：本项目开关的约定是"住在它主要治理的
    模块里"（USE_REACT 在 core/react.py，EVAL_MODE 在 core/safety.py），为一个函数
    新建一个 config 模块反而会让这几个开关的摆放变得不一致。
    """
    return os.environ.get("FAST_MODE", "0").lower() in ("1", "true", "yes")


def parse_answer(answer: str) -> str:
    """把患者的自由文本回答归成 yes / no / unknown。

    只做规则不调 LLM：追问问的是「有没有 X？」这种封闭问题，答案本质上就是三选一，
    为此每轮烧一次 LLM 调用不划算。代价是遇到「一半有一半没有」
    这类回答会归到 unknown——归错成 yes/no 会把一条假证据写进后验，宁可当没问到。
    """
    a = (answer or "").strip().lstrip("，,。.！!　 ")
    if not a:
        return "unknown"
    if any(m in a for m in _UNCERTAIN):
        return "unknown"
    # 句首是肯定词的一律判 yes，不看后面的「不」——「有一点，不多」是肯定
    if any(a.startswith(m) for m in _AFFIRM_LEAD):
        return "yes"
    if any(m in a for m in _NEGATION):
        return "no"
    if any(m in a for m in _AFFIRM):
        return "yes"
    return "unknown"


def run_followup(
    symptoms: list[str],
    elements: list[str],
    ask_fn: AskFn | None,
    max_rounds: int = MAX_ASK_ROUNDS,
    physician: str | None = None,
) -> FollowupResult:
    """跑追问循环。ask_fn 为 None（没有提问渠道）时直接返回空结果，不是报错。"""
    if fast_mode_enabled():
        return FollowupResult(stopped_by="fast_mode")
    if ask_fn is None:
        return FollowupResult(stopped_by="no_answer")

    # 用 match_disease（现成的规则打分）先估一个最可能的病名，把候选池收窄到同一病名下
    # （见 core.tools._scope_by_disease）。不收窄的话，后验在全量候选（不分病种）上
    # 归一化，单条追问答案的影响小到接近浮点噪声；而且跟主诉无关的病种（主诉两胁胀满，
    # 候选里却有"中风脱证"）会让"不省人事""突然昏厥"这类安全关键词也有非零的区分度，
    # 被"安全相关症状保证进候选"的机制排到最前面，追问预算就花在与主诉无关的危重症状
    # 问题上。诊断相关性的判断放在候选池这一层，不是在选出来之后再过滤。
    #
    # 取分数最高的一个；一条都没匹配上（罕见，比如主诉极简短）时 disease_hint
    # 是 None，question_candidates / syndrome_posterior 都把它当作"不收窄"，
    # 用全量候选，不是报错。
    try:
        disease_matches = match_disease(symptoms, elements)
    except FileNotFoundError:
        disease_matches = []
    disease_hint = disease_matches[0][0] if disease_matches else None

    history: list[HistoryItem] = []
    asserted: list[str] = []
    denied: list[str] = []
    asked: list[str] = []

    for _ in range(max_rounds):
        candidates = question_candidates(
            elements, k=1, known_symptoms=symptoms, asked=asked,
            physician=physician, asserted_symptoms=asserted, denied_symptoms=denied,
            disease_hint=disease_hint,
        )
        if not candidates:
            return _result(history, asserted, denied, "no_candidate")
        top = candidates[0]
        ig = top.get("information_gain")
        # 安全相关的候选不受这道收敛门槛约束：core/tools.py::question_candidates
        # 保证它们排到最前面时可以带着很低甚至趋零的信息增益（它们回答的是"要不要
        # 转诊"，不是"能不能帮图区分当前候选证候"，两者本就可能不相关）。这里如果
        # 照常按 ig < MIN_USEFUL_IG 判"已收敛"，会把一个被刻意提到最前面、专门
        # 要问的危重症状问题在问都没问的情况下直接判定为"不用问了"，
        # 生产链路固定 k=1，top 就是唯一会被消费的候选——判错这一条，安全相关症状
        # 保证进候选的机制就形同虚设。
        if not top.get("safety_relevant") and ig is not None and ig < MIN_USEFUL_IG:
            return _result(history, asserted, denied, "converged")

        answer = ask_fn(top["question"])
        if answer is None:
            return _result(history, asserted, denied, "no_answer")

        verdict = parse_answer(answer)
        # 安全检查在写入后验之前，两条路都要堵：回答原文里带危重词
        # （「有，这两天还解了黑便」），以及**问的本身就是危重症状、患者只答一个「有」**
        # （「有没有便血？」→「有」）。后一种要把问题本身和候选症状名一起送去判（见下面
        # 的 danger_confirmed_by_answer）——不然答「有」就把「便血」写进 asserted，
        # S2/S3 照常开方，追问这个入口就绕过了安全否决（docs/ARCHITECTURE.md §3）。
        reject = check_safety([answer])
        # 问的本身是危重症状时，只有明确否认才放行（yes 拦，unknown 也拦）。判据在
        # core.safety.danger_confirmed_by_answer 一处实现，ReAct 的 ask_user 路径
        # （core/chain.py）调的是同一个函数，不各写一套。
        if reject is None:
            reject = danger_confirmed_by_answer(top["question"], verdict, symptom=top.get("symptom"))
        # 十问歌后备问的是话题（symptom 为 None），答案里若提到危重内容，check_safety
        # 已经在上面拦了；这里再用 mentions_danger 兜一层"提到但被当成否定句式"的情况，
        # 例如「解的是黑的」这种没有明确否定词、check_safety 也认得，但换成
        # 「不太成形，颜色发黑」时前置否定规则可能误判。
        if reject is None and verdict != "no" and mentions_danger(answer):
            reject = check_safety([f"患者自述：{answer}"]) or veto_message(mentions_danger(answer))
        if reject is not None:
            history.append(HistoryItem(
                question=top["question"], answer=answer,
                symptom=top.get("symptom"), topic=top.get("topic"),
                safety_hit=reject,
            ))
            return _result(history, asserted, denied, "safety", reject_reason=reject)

        item = HistoryItem(
            question=top["question"], answer=answer,
            symptom=top.get("symptom"), topic=top.get("topic"),
        )
        # 十问歌后备问的是话题不是具体症状，答案归不到某条国标症状上——
        # 这时只记录，不往后验里塞任何东西。硬塞会把"问了个宽泛问题"
        # 当成"确认了某条症状"，那是凭空造证据。
        if top.get("symptom"):
            if verdict == "yes":
                item.asserted = [top["symptom"]]
                asserted.append(top["symptom"])
            elif verdict == "no":
                item.denied = [top["symptom"]]
                denied.append(top["symptom"])
            asked.append(top["symptom"])
        elif top.get("topic"):
            asked.append(top["topic"])
        history.append(item)

    return _result(history, asserted, denied, "max_rounds")


#: 六种停因的中文名。**跟 `FollowupResult.stopped_by` 的 Literal 一一对应**，
#: 由 `_serialize_followup` 随结果下发给前端——展示层只认中文名，id 只在数据层
#: （跟 `core/formula_verifier.RULE_LABELS` 同一条理由：枚举在哪，它的中文名就在哪，
#: 前端另建一张表意味着以后加一种停因要改两处，而漏改的表现是界面上冒出英文 id）。
STOP_LABELS: dict[str, str] = {
    "max_rounds": "问满轮次",
    "converged": "再问也问不出新信息",
    "no_candidate": "没有可问的问题",
    "safety": "回答触发安全否决",
    "fast_mode": "被 FAST_MODE 跳过",
    "no_answer": "提问方没给回答",
}


def stop_label(stopped_by: str) -> str:
    """停因 id → 中文名。查不到回落到 id 本身（少一条比显示空白好找）。"""
    return STOP_LABELS.get(stopped_by, stopped_by)


def _result(history, asserted, denied, stopped_by, reject_reason=None) -> FollowupResult:
    return FollowupResult(
        history=history, asserted=asserted, denied=denied,
        rounds=len(history), stopped_by=stopped_by, reject_reason=reject_reason,
    )


def format_followup_for_s3(followup: FollowupResult) -> str:
    """把追问结果压成一段附加信息，追加到 S3 提示词后面。

    **否认的那部分尤其不能省。** 肯定的症状会被并进症状表传下去，否认的不会——
    如果 S3 看不到「患者明确说没有口苦」，它照样可能按湿热去开方。追加而不是改
    s3_syndrome.yaml：没走追问时这里返回空串，S3 的提示词就是原模板本身。
    """
    if not followup.history:
        return ""
    lines = ["\n\n【追问结果】以下是本次问诊中向患者追问得到的答复："]
    for item in followup.history:
        lines.append(f"- 问：{item.question}　答：{item.answer}")
    if followup.asserted:
        lines.append(f"患者确认存在：{'、'.join(followup.asserted)}")
    if followup.denied:
        lines.append(
            f"患者明确否认：{'、'.join(followup.denied)}"
            "——这几条是阴性证据，辨证时不要当成未知，更不要按存在处理。"
        )
    return "\n".join(lines)
