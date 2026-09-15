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
import re
from typing import Callable

from core.followup import fast_mode_enabled
from core.llm import LLMError, get_llm, load_prompt, render, thinking_for
from core.physicians import resolve_physician_id
from core.schemas import ReActStep, ReActStepRecord, ReActTrace
from core.tools import TOOLS, run_tool, tools_manifest

# 分步进度回调：(事件名, 数据字典) -> None。定义在这里（而不是 core/chain.py）
# 是因为依赖方向是单向的——chain.py import react.py，反过来会成环。跟 AskFn
# 定义在 core/followup.py、chain.py 再导入是同一个先例：谁最先需要这个类型，
# 类型就定义在谁那，上游模块导入下游的，不新建一个中立类型模块。
StepFn = Callable[[str, dict], None]

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


# ---------------------------------------------------------------------------
# P1 ReAct 修复：两条止损提示。实测 60 次工具调用里国标层（lookup_standard+
# query_graph）占 73%、医案层（search_cases+query_case_graph）只占 12%，
# 三条真实 trace 里有两条把 max_steps 花在国标层的死胡同上——一条在两个
# 候选证候编号之间来回查（trace C），一条连续换词查国标图谱查不到（trace B）。
# 这两条是"给模型的信息，不是强制"：观察窗口够宽（连续两次才触发），命中后
# 也只是在 observation 里加一句建议，模型仍然可以继续按自己的判断查下去——
# 剥夺它在特殊病例上的判断空间，比"没提示"更危险。
#
# 只覆盖了三条真实 trace 里出现过的场景：trace B 那种"连续两次换词查国标
# 图谱查不到"，trace C 那种"连续两次在两个证候编号之间来回查"。故意不覆盖
# "连续两次查近义证候名"（比如 trace B 里 lookup_standard 从「脾阳虚证」
# 换成「脾胃虚寒证」、第二次就查到了那种）——那是正常的试错，换个说法就能
# 查到，提示反而会打断一次本来会成功的尝试。
# ---------------------------------------------------------------------------

_STANDARD_CODE_RE = re.compile(r"^[A-Za-z]{1,4}[-.\d]+$")

CODE_DISAMBIGUATION_HINT = (
    "【提示】继续区分标准证候编号对最终开什么方帮助有限——标准证候名和这位"
    "医家医案里的说法是两套术语。建议转查 search_cases 或 query_case_graph，"
    "看这位医家遇到类似症状实际用了什么方。"
)

GRAPH_MISS_HINT = (
    "【提示】患者原话往往不在国标的 1282 个症状节点里（这是清代医案与"
    "现代国标术语体系差异的已知结果，见 SOURCES.md）。继续换词查大概率"
    "还是查不到，建议改用 search_cases 检索这位医家的医案原文。"
)


def _looks_like_standard_code(query: str | None) -> bool:
    """query 是不是证候编号（如 SP-10、TB-127、B04.06.02.03.01.03），不是
    证候名。三种真实编号格式都是纯 ASCII（字母打头，后面跟数字/点/横杠），
    证候名全是中文——两者字符集不重叠，不需要对照 data/standard 实际枚举
    一遍就能分辨。"""
    return bool(_STANDARD_CODE_RE.match((query or "").strip()))


def _should_hint_code_disambiguation(
    prev_action: str | None, prev_query: str | None,
    action: str, query: str | None,
) -> bool:
    """连续两次 lookup_standard 都在查证候编号——大概率是在纠结"到底是哪个
    编号"（trace C：「痰饮」查出 SP-10/TB-127 两个候选后，接连两步分别查
    这两个编号，这个区分对最终开什么方没有影响）。只覆盖"两次都是编号"，
    不覆盖"两次都是证候名"——见模块顶部那段注释，后一种是正常试错。"""
    if prev_action != "lookup_standard" or action != "lookup_standard":
        return False
    return _looks_like_standard_code(prev_query) and _looks_like_standard_code(query)


def _should_hint_graph_miss(
    prev_action: str | None, prev_result: dict | None,
    action: str, result: dict,
) -> bool:
    """连续两次 query_graph 都 found:false（trace B：「胃中隐痛」「胃脘痛」
    连续两次查不到）。只看 query_graph 自己的 found 字段——lookup_standard
    的 found:false 是另一个工具的另一件事，止损提示是 CODE_DISAMBIGUATION_
    HINT，不在这里管。"""
    if prev_action != "query_graph" or action != "query_graph":
        return False
    return (prev_result or {}).get("found") is False and result.get("found") is False


def run_react(
    name: str,
    symptoms: str,
    elements_summary: str,
    max_steps: int | None = None,
    on_step: StepFn | None = None,
    physician_id: str | None = None,
) -> ReActTrace:
    """跑一轮 ReAct，返回完整轨迹。不抛异常：LLM 调用失败也记进轨迹返回，
    让上层决定要不要继续——一次工具层的意外不该让整条问诊挂掉。

    physician_id 是 prompt 里 $physician_id 的来源：模型调 search_cases /
    query_case_graph 时 physician 参数要填的就是它。生产路径（core/chain.py）
    显式传 id；不传时用 resolve_physician_id(name) 从中文名反查——这是兜底
    不是主路径，让模型一开始就拿到 id 比事后解析可靠。连中文名都反查不到
    （未注册的名字，只有测试会这么传）就原样放 name：工具层那边会返回列出
    可用值的报错，模型能据此纠正，比在这里编一个 id 诚实。

    max_steps=None 时按 FAST_MODE 决定（开着降到 FAST_MODE_MAX_STEPS，否则
    MAX_STEPS），显式传数字优先。形状跟 use_react=None / eval_mode=None 一致。
    判断放在这里而不是 chain.py 的调用点：只在调用方生效的开关是半吊子，
    换一个调用方进来就漏了。

    on_step 不传时（CLI、离线批跑、eval/ 全都不传）整个循环跟改造前逐字节一致
    ——没有 SSE 场景时不该为进度上报多花一次判断之外的开销。传了就在每一步
    落地（含"重复调用""工具名不存在"这类中途 continue 的步骤，不止 finish/
    ask_user/max_steps 这几种终止路径）之后调一次，事件数恒等于 trace.steps
    的条数——这是它的不变量，别在某个 continue 分支漏调。"""
    if max_steps is None:
        max_steps = FAST_MODE_MAX_STEPS if fast_mode_enabled() else MAX_STEPS
    if physician_id is None:
        physician_id = resolve_physician_id(name) or name
    prompt = load_prompt("s3_react")
    records: list[ReActStepRecord] = []
    seen: dict[str, int] = {}
    consecutive_dupes = 0
    llm_calls = 0
    retrieved: list[str] = []
    # 连续两次止损提示要看的是"上一次真正执行的工具调用"，不是"上一步"——
    # 工具名写错、重复调用这两类 continue 分支没有真的查任何东西，不该打断
    # 或冒充一次连续性。只在下面真正执行工具的分支末尾更新这三个变量。
    prev_action: str | None = None
    prev_result: dict | None = None
    prev_query: str | None = None

    def emit_step() -> None:
        if on_step is None:
            return
        r = records[-1]
        on_step("react_step", {
            # physician（id）跟 physician_name（中文名）都带上：并发之后前端按 id 路由
            # 到对应的列（docs/DESIGN.md §4.7 订正），中文名只用来显示。只给中文名的话
            # 路由就得在前端做一次名字→id 的反查——那正是第 31 条禁止的第二处实现。
            "physician": physician_id, "physician_name": name,
            "step": r.step, "action": r.action,
            "thought": (r.thought or "")[:80], "note": r.note,
        })

    for step in range(1, max_steps + 1):
        system = render(
            prompt["system"],
            name=name,
            physician_id=physician_id,
            symptoms=symptoms,
            elements_summary=elements_summary,
            tools=format_tools(),
            history=format_history(records),
            remaining=str(max_steps - step + 1),
        )
        try:
            # physician_id 传下去是给本地后端选 LoRA adapter 用的（阶段五每位
            # 医家一个）：ReAct 的每一步也是这位医家在推理，不是通用步骤。
            # 云端后端如实忽略这个参数，见 core/llm.py::LLMBackend._complete。
            # 关思考：ReAct 的每一步是"选哪个工具、填什么参数"这种结构化决策，
            # 步数上限本来就把探索空间压得很小，思考带来的增益远不抵它的耗时。
            out: ReActStep = get_llm().generate(
                system=system, user="", schema=ReActStep, physician=physician_id,
                **thinking_for("react"),
            )
            llm_calls += 1
        except LLMError as e:
            records.append(ReActStepRecord(
                step=step, thought="（本步 LLM 调用失败）", action="(none)",
                observation="", note=f"LLM 调用失败：{e}",
            ))
            emit_step()
            llm_calls += 1
            return ReActTrace(steps=records, retrieved_case_ids=retrieved, terminated_by="error", llm_calls=llm_calls)

        action = out.action.strip()

        if action == FINISH_ACTION:
            records.append(ReActStepRecord(
                step=step, thought=out.thought, action=FINISH_ACTION,
                observation="（模型判断证据已足够，结束取证）",
            ))
            emit_step()
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
                emit_step()
                continue
            records.append(ReActStepRecord(
                step=step, thought=out.thought, action=ASK_ACTION,
                action_input=out.action_input, observation=_observation_text(result),
            ))
            emit_step()
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
            emit_step()
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
            emit_step()
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

        observation = _observation_text(result)
        query = out.action_input.get("query")
        if _should_hint_code_disambiguation(prev_action, prev_query, action, query):
            observation = f"{observation}\n{CODE_DISAMBIGUATION_HINT}"
        elif _should_hint_graph_miss(prev_action, prev_result, action, result):
            observation = f"{observation}\n{GRAPH_MISS_HINT}"

        records.append(ReActStepRecord(
            step=step, thought=out.thought, action=action,
            action_input=out.action_input, observation=observation,
            note="参数不合法" if "error" in result else None,
        ))
        emit_step()
        prev_action, prev_result, prev_query = action, result, query

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
