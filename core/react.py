"""G2：把 G1 的工具层接成一个 ReAct 循环，放在 S3 之前给它补证据。

跟 S3 的关系（**这个设计选择是有意的，别改成让模型直接输出最终答案**）：
循环只负责"查证据"，最终的证型/治法/方药仍然由原来的 S3 生成，schema 还是
`S3Syndrome`，`cited_case_ids` 的 `min_length=1` 和检索到的 refs 白名单
一个都没动。这样两件事成立：

  1. 防幻觉约束不因为加了 ReAct 而被绕开。让模型在 finish 那一步直接吐
     S3Syndrome，等于把"引用必须来自检索结果"的校验搬进一个更长、更容易
     跑偏的上下文里。
  2. use_react=True/False 输出的是同一个 schema，可以直接 A/B——开关一开
     结论就没法比了的话，这个开关也就没用了。

循环的账要记全：`terminated_by` 分五种（finish / ask_user / max_steps /
no_progress / error）。撞 max_steps 和模型主动收尾是完全不同的结论——前者说明
prompt 没让它知道什么时候算够了——混成一个"结束了"就看不出来。
"""
from __future__ import annotations

import json
import os

from core.followup import fast_mode_enabled
from core.llm import LLMError, get_llm, load_prompt, render
from core.schemas import ReActStep, ReActStepRecord, ReActTrace
from core.tools import TOOLS, run_tool, tools_manifest

MAX_STEPS = 5
# FAST_MODE 下的步数上限。2 步是有意的下限而不是 1：ReAct 至少要能"查一次 +
# 收尾"，压到 1 步就只剩一次工具调用、连收尾的机会都没有，轨迹会必然以
# max_steps 结束，看起来像模型不会收尾，实际是被上限卡死的——那正是
# SOURCES.md 第 11/12 条要求把这两种情况分开看的原因。
FAST_MODE_MAX_STEPS = 2
# observation 塞回 prompt 时的截断长度。不截断的话 search_cases 一次返回三条
# 完整医案，几步之后 history 会把 prompt 撑到几千字，后面的步反而看不清重点。
MAX_OBSERVATION_CHARS = 1200
FINISH_ACTION = "finish"
ASK_ACTION = "ask_user"


def react_enabled() -> bool:
    """默认关。ReAct 每位医家多花 1-5 次调用，成本翻倍，不该是默认路径；
    真正要它的是"给我看推理过程"这类演示场景，显式开。"""
    return os.environ.get("USE_REACT", "0").lower() in ("1", "true", "yes")


def format_tools() -> str:
    """工具清单从 tools_manifest() 渲染，不在 yaml 里手抄——手抄的那份会跟代码
    分叉，而模型看到的是手抄的那份。"""
    lines = []
    for spec in tools_manifest():
        props = spec["parameters"].get("properties", {})
        required = set(spec["parameters"].get("required", []))
        params = "，".join(
            f"{k}{'（必填）' if k in required else ''}" for k in props
        )
        lines.append(f"- {spec['name']}（参数：{params or '无'}）\n    {spec['description']}")
    return "\n".join(lines)


def format_history(records: list[ReActStepRecord]) -> str:
    if not records:
        return "（还没有查过任何东西，这是第 1 步）"
    lines = []
    for r in records:
        args = json.dumps(r.action_input, ensure_ascii=False)
        lines.append(f"第{r.step}步 调用 {r.action}({args})\n    结果：{r.observation}")
        if r.note:
            lines.append(f"    注意：{r.note}")
    return "\n".join(lines)


def _truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"…（结果过长，已截断，共 {len(text)} 字）"


def _observation_text(result: dict) -> str:
    return _truncate(json.dumps(result, ensure_ascii=False))


def _call_key(action: str, action_input: dict) -> str:
    return f"{action}::{json.dumps(action_input, ensure_ascii=False, sort_keys=True)}"


def run_react(
    name: str,
    symptoms: str,
    elements_summary: str,
    max_steps: int | None = None,
) -> ReActTrace:
    """跑一轮 ReAct，返回完整轨迹。不抛异常：LLM 调用失败也记进轨迹返回，
    让上层决定要不要继续——一次工具层的意外不该让整条问诊挂掉。

    max_steps=None 时按 FAST_MODE 决定（开着降到 FAST_MODE_MAX_STEPS，否则
    MAX_STEPS），显式传数字优先。形状跟 use_react=None / eval_mode=None 一致。
    判断放在这里而不是 chain.py 的调用点：只在调用方生效的开关是半吊子，
    换一个调用方进来就漏了。"""
    if max_steps is None:
        max_steps = FAST_MODE_MAX_STEPS if fast_mode_enabled() else MAX_STEPS
    prompt = load_prompt("s3_react")
    records: list[ReActStepRecord] = []
    seen: dict[str, int] = {}
    consecutive_dupes = 0
    llm_calls = 0
    retrieved: list[str] = []

    for step in range(1, max_steps + 1):
        system = render(
            prompt["system"],
            name=name,
            symptoms=symptoms,
            elements_summary=elements_summary,
            tools=format_tools(),
            history=format_history(records),
            remaining=str(max_steps - step + 1),
        )
        try:
            out: ReActStep = get_llm().generate(system=system, user="", schema=ReActStep)
            llm_calls += 1
        except LLMError as e:
            records.append(ReActStepRecord(
                step=step, thought="（本步 LLM 调用失败）", action="(none)",
                observation="", note=f"LLM 调用失败：{e}",
            ))
            llm_calls += 1
            return ReActTrace(steps=records, retrieved_case_ids=retrieved, terminated_by="error", llm_calls=llm_calls)

        action = out.action.strip()

        if action == FINISH_ACTION:
            records.append(ReActStepRecord(
                step=step, thought=out.thought, action=FINISH_ACTION,
                observation="（模型判断证据已足够，结束取证）",
            ))
            return ReActTrace(steps=records, retrieved_case_ids=retrieved, terminated_by="finish", llm_calls=llm_calls)

        if action == ASK_ACTION:
            result = run_tool(ASK_ACTION, out.action_input)
            if "error" in result:
                # 参数不合法（漏了 reason 之类）跟别的工具一样回灌纠正，不能以
                # ask_user 收尾——那样 terminated_by="ask_user" 却没有问题可问。
                records.append(ReActStepRecord(
                    step=step, thought=out.thought, action=ASK_ACTION,
                    action_input=out.action_input, observation=_observation_text(result),
                    note="参数不合法",
                ))
                continue
            records.append(ReActStepRecord(
                step=step, thought=out.thought, action=ASK_ACTION,
                action_input=out.action_input, observation=_observation_text(result),
            ))
            return ReActTrace(steps=records, retrieved_case_ids=retrieved, terminated_by="ask_user",
                pending_question=result.get("question"), llm_calls=llm_calls,
            )

        if action not in TOOLS:
            # 工具名写错不是致命错误：把可用清单当 observation 回灌，1 次调用就能纠正。
            records.append(ReActStepRecord(
                step=step, thought=out.thought, action=action,
                action_input=out.action_input,
                observation=_observation_text({
                    "error": f"没有名为 {action} 的工具",
                    "available_tools": sorted(TOOLS.keys()) + [FINISH_ACTION],
                }),
                note="工具名不存在",
            ))
            continue

        key = _call_key(action, out.action_input)
        if key in seen:
            # 重复调用不再真的执行工具：结果不会变，执行一遍只是浪费。
            consecutive_dupes += 1
            records.append(ReActStepRecord(
                step=step, thought=out.thought, action=action,
                action_input=out.action_input,
                observation=f"（与第 {seen[key]} 步完全相同的调用，结果不会变，未重复执行）",
                note="重复调用",
            ))
            if consecutive_dupes >= 2:
                # 连着两次原地打转就停：再问下去只会烧调用次数。
                return ReActTrace(steps=records, retrieved_case_ids=retrieved, terminated_by="no_progress", llm_calls=llm_calls
                )
            continue
        consecutive_dupes = 0
        seen[key] = step

        result = run_tool(action, out.action_input)
        if action == "search_cases":
            retrieved.extend(c.get("case_id") for c in result.get("cases", []) if c.get("case_id"))
        records.append(ReActStepRecord(
            step=step, thought=out.thought, action=action,
            action_input=out.action_input, observation=_observation_text(result),
            note="参数不合法" if "error" in result else None,
        ))

    return ReActTrace(steps=records, retrieved_case_ids=retrieved, terminated_by="max_steps", llm_calls=llm_calls)


def format_trace_for_s3(trace: ReActTrace) -> str:
    """把轨迹压成一段附加证据，追加到原来的 S3 prompt 后面。

    追加而不是改 s3_syndrome.yaml 里加一个 $evidence 占位符：不开 ReAct 时
    prompt 要跟改造前逐字节一致，否则 use_react 的 A/B 就混进了 prompt 变化
    这个额外变量。
    """
    if not trace.steps:
        return ""
    lines = ["\n\n【取证过程】以下是在给出结论前实际查到的证据，请结合它们推理："]
    for r in trace.steps:
        if r.action == FINISH_ACTION:
            continue
        args = json.dumps(r.action_input, ensure_ascii=False)
        lines.append(f"- {r.action}({args}) → {r.observation}")
    if trace.pending_question and trace.pending_answer:
        lines.append(f"- 向患者追问「{trace.pending_question}」→ 患者答：{trace.pending_answer}")
    lines.append(
        "注意：cited_case_ids 只能引用上面「参考医案」里给出的 id，"
        "以及取证过程中 search_cases 实际返回过的 id；其他地方出现的 id 不算。"
    )
    return "\n".join(lines)
