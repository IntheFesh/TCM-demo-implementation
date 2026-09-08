"""G3：追问闭环。用 G1 的信息增益选下一个问题，收回答案，更新后验，再选下一个。

三条硬约束（顺序也是硬的）：

1. **回答先过 check_safety，再做别的任何事。** 追问是安全否决层的后门——S2 之前
   拦的是初始主诉，如果追问问出「有黑便」而回答直接进证素推断，那道拦截就被绕过去了
   （CLAUDE.md 改造期约定）。所以安全检查在解析答案之前，命中即整轮终止、不产出方药。

2. **否定回答必须进后验，不只是从候选池里去掉。** 见 core/tools.py
   syndrome_posterior 的文档字符串。

3. **每轮 0 次 LLM 调用。** 答案解析用规则（我们问的是一个具体症状的封闭问题，
   答案本质上就是是/否/不确定），后验更新是纯图计算。整个追问循环只在最后
   ——且仅当问出了新症状时——重跑一次 S2 把新症状并进证素。按 G2 实测的成本
   （ReAct 每位医家固定 5 次调用），追问再按轮收费的话这个 demo 就没法用了。
"""
from __future__ import annotations

import os
from typing import Callable

from core.safety import check_safety, danger_confirmed_by_answer, mentions_danger, veto_message
from core.schemas import FollowupResult, HistoryItem
from core.tools import question_candidates

MAX_ASK_ROUNDS = 3

# 低于这个信息增益就认为"再问一句也问不出什么了"，收敛退出。0.05 bit 大约相当于
# 把一个 17 选 1 的问题削掉 3% 的不确定性——继续问的收益已经低于多问一句的代价。
# 这是策略阈值不是物理常数，改它只影响"什么时候停"，不影响问题的排序。
MIN_USEFUL_IG = 0.05

# 答案解析的三张表。顺序有意义：先查不确定，再查否定，最后才查肯定——
# 「没有」里含「有」，反过来查会把所有否定读成肯定。
_UNCERTAIN = ("不知道", "不清楚", "说不清", "不确定", "不好说", "记不清", "时有时无")
_NEGATION = ("没有", "没", "不", "无", "未", "否", "从来")
_AFFIRM = ("有", "是", "对", "会", "经常", "一直", "偶尔", "确实", "嗯")
# 带限定语的肯定：「有一点，不多」「有，但不严重」——句首是肯定、后半句的「不」
# 是程度限定，不是否认。不先认出来的话它们会被归成 no，危重症状写进 denied、
# S3 收到「患者明确否认：便血」照常开方（实测能端到端复现）。
_AFFIRM_LEAD = ("有", "是的", "对", "嗯", "确实", "偶尔", "经常", "一直", "会")

# 提问方：给一个问题，返回患者的回答；返回 None 表示对方不打算回答（关掉了对话框、
# 命令行 Ctrl-C 等）。做成注入的函数是为了让真人、患者模拟器、前端三种来源共用
# 同一套循环——换来源改的是传进来的这个函数，不是循环本身。
AskFn = Callable[[str], str | None]


def fast_mode_enabled() -> bool:
    """演示现场网络慢或临时预算紧张时的兜底开关。默认关。

    **全项目唯一的 FAST_MODE 判定实现**，三处代码路径都调它、不各写一套：
      1. 本模块的追问循环：max_rounds 降到 0（一个问题都不问）
      2. core/react.py 的 run_react：步数上限降到 FAST_MODE_MAX_STEPS
      3. core/chain.py 的 run_residual：残差辨证整体关闭
    三处必须同时生效——只关一半的开关是陷阱：用户以为省了预算，实际还在花。

    判定实现留在这个模块是历史原因（FAST_MODE 最早只管追问）。没有挪到中立
    模块：本项目三个开关的约定就是"住在它主要治理的模块里"
    （USE_REACT 在 core/react.py，EVAL_MODE 在 core/safety.py），为一个函数
    新建一个 config 模块反而会让这三个开关的摆放变得不一致。
    """
    return os.environ.get("FAST_MODE", "0").lower() in ("1", "true", "yes")


def parse_answer(answer: str) -> str:
    """把患者的自由文本回答归成 yes / no / unknown。

    只做规则不调 LLM：我们问的是「有没有 X？」这种封闭问题，答案本质上就是三选一，
    为此每轮烧一次调用不划算（G2 实测每次调用约 4–8s）。代价是遇到「一半有一半没有」
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

    history: list[HistoryItem] = []
    asserted: list[str] = []
    denied: list[str] = []
    asked: list[str] = []

    for _ in range(max_rounds):
        candidates = question_candidates(
            elements, k=1, known_symptoms=symptoms, asked=asked,
            physician=physician, asserted_symptoms=asserted, denied_symptoms=denied,
        )
        if not candidates:
            return _result(history, asserted, denied, "no_candidate")
        top = candidates[0]
        ig = top.get("information_gain")
        if ig is not None and ig < MIN_USEFUL_IG:
            return _result(history, asserted, denied, "converged")

        answer = ask_fn(top["question"])
        if answer is None:
            return _result(history, asserted, denied, "no_answer")

        verdict = parse_answer(answer)
        # 安全检查在写入后验之前，两条路都要堵：回答原文里带危重词
        # （「有，这两天还解了黑便」），以及**问的本身就是危重症状、患者只答一个「有」**
        # （「有没有便血？」→「有」）。第二条此前是漏的：question_candidates 算好的
        # safety_relevant 标记全仓库没有任何消费方，实测答「有」就把「便血」写进了
        # asserted、S2/S3 照常开方——正是 CLAUDE.md 那条约定要堵的后门。
        reject = check_safety([answer])
        # 问的本身是危重症状时，只有明确否认才放行（yes 拦，unknown 也拦）。判据在
        # core.safety.danger_confirmed_by_answer 一处实现，ReAct 的 ask_user 路径
        # （core/chain.py）调的是同一个函数——之前两边各写一套、看的文本还不一样。
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


def _result(history, asserted, denied, stopped_by, reject_reason=None) -> FollowupResult:
    return FollowupResult(
        history=history, asserted=asserted, denied=denied,
        rounds=len(history), stopped_by=stopped_by, reject_reason=reject_reason,
    )


def format_followup_for_s3(followup: FollowupResult) -> str:
    """把追问结果压成一段附加信息，追加到 S3 提示词后面。

    **否认的那部分尤其不能省。** 肯定的症状会被并进症状表传下去，否认的不会——
    如果 S3 看不到「患者明确说没有口苦」，它照样可能按湿热去开方。追加而不是改
    s3_syndrome.yaml：没走追问时提示词要跟改造前逐字节一致（同 G2 的理由）。
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
