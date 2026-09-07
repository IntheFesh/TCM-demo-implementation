"""core/react.py 的离线测试。用脚本化的假 LLM 驱动循环，不需要网络。

重点在五种 terminated_by 各自都能到达，以及"模型犯错"的三条路径
（工具名写错、参数不合法、原地重复）都不会让循环崩掉——ReAct 里一个异常
等于整条问诊挂掉。
"""
import json

import pytest

from core import chain, react
from core.react import (
    MAX_OBSERVATION_CHARS,
    MAX_STEPS,
    format_history,
    format_tools,
    format_trace_for_s3,
    react_enabled,
    run_react,
)
from core.llm import LLMError
from core.schemas import ReActStep, ReActStepRecord, ReActTrace


class ScriptedLLM:
    """按脚本逐步返回 ReActStep。脚本用完还被调用就是测试写错了，直接报错。"""

    def __init__(self, steps: list[ReActStep | Exception]):
        self.script = list(steps)
        self.calls = 0

    def model_name(self):
        return "fake-model"

    def backend_id(self):
        return "fake"

    def comparability_warning(self):
        return None

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        assert schema is ReActStep, f"这个假后端只驱动 ReAct 循环，收到 {schema}"
        self.calls += 1
        assert self.script, "脚本已用完但循环还在要下一步"
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def scripted(monkeypatch):
    def _install(steps):
        llm = ScriptedLLM(steps)
        monkeypatch.setattr(react, "get_llm", lambda: llm)
        return llm
    return _install


def _run():
    return run_react(name="叶天士", symptoms="纳差；脘痞", elements_summary="脾（location）")


# ---------- 五种 terminated_by ----------

def test_finish_on_first_step(scripted):
    llm = scripted([ReActStep(thought="证素已经清楚", action="finish")])
    trace = _run()
    assert trace.terminated_by == "finish"
    assert trace.llm_calls == 1 == llm.calls
    assert len(trace.steps) == 1


def test_tool_call_then_finish(scripted):
    scripted([
        ReActStep(thought="先看纳差指向哪些证素", action="query_graph",
                  action_input={"node": "纳呆"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert trace.terminated_by == "finish"
    assert [s.action for s in trace.steps] == ["query_graph", "finish"]
    assert '"found": true' in trace.steps[0].observation.lower()


def test_max_steps_when_model_never_finishes(scripted):
    """撞 max_steps 和模型主动收尾要分开记——前者说明 prompt 没让它知道
    什么时候算够了，混成一个"结束了"就看不出来。"""
    scripted([
        ReActStep(thought=f"再查一个 {i}", action="query_graph",
                  action_input={"node": f"节点{i}"})
        for i in range(MAX_STEPS)
    ])
    trace = _run()
    assert trace.terminated_by == "max_steps"
    assert len(trace.steps) == MAX_STEPS
    assert trace.llm_calls == MAX_STEPS


def test_ask_user_terminates_with_pending_question(scripted):
    scripted([ReActStep(thought="分不开湿热和虚寒", action="ask_user",
                        action_input={"question": "有没有口苦？", "reason": "分不开"})])
    trace = _run()
    assert trace.terminated_by == "ask_user"
    assert trace.pending_question == "有没有口苦？"


def test_llm_error_is_recorded_not_raised(scripted):
    """一次 LLM 失败不该让整条问诊挂掉。"""
    scripted([LLMError("后端挂了")])
    trace = _run()
    assert trace.terminated_by == "error"
    assert "后端挂了" in trace.steps[0].note


def test_two_consecutive_duplicates_stop_the_loop(scripted):
    scripted([
        ReActStep(thought="查一下", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="再查一遍", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="还查这个", action="query_graph", action_input={"node": "纳呆"}),
    ])
    trace = _run()
    assert trace.terminated_by == "no_progress"
    assert len(trace.steps) == 3


# ---------- 模型犯错的三条路径 ----------

def test_unknown_tool_name_is_fed_back_not_fatal(scripted):
    """工具名写错用 observation 回灌只花 1 次调用；如果让 pydantic 校验失败，
    generate() 会重试三次，一个笔误烧掉 3 次。"""
    scripted([
        ReActStep(thought="查图谱", action="query_knowledge_graph",
                  action_input={"node": "纳呆"}),
        ReActStep(thought="改用对的名字", action="query_graph",
                  action_input={"node": "纳呆"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert trace.terminated_by == "finish"
    assert trace.steps[0].note == "工具名不存在"
    assert "query_graph" in trace.steps[0].observation
    assert "finish" in trace.steps[0].observation


def test_bad_arguments_recorded_as_error_observation(scripted):
    scripted([
        ReActStep(thought="漏了必填参数", action="lookup_standard", action_input={}),
        ReActStep(thought="补上", action="finish"),
    ])
    trace = _run()
    assert trace.steps[0].note == "参数不合法"
    assert "error" in trace.steps[0].observation


def test_duplicate_call_is_not_re_executed(scripted, monkeypatch):
    """重复调用不真的跑工具：结果不会变，跑一遍只是浪费。"""
    calls = []
    real = react.run_tool

    def counting(name, args):
        calls.append(name)
        return real(name, args)

    monkeypatch.setattr(react, "run_tool", counting)
    scripted([
        ReActStep(thought="查", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="再查", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="换一个", action="query_graph", action_input={"node": "便溏"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert calls == ["query_graph", "query_graph"], "第 2 步是重复调用，不该真的执行"
    assert trace.steps[1].note == "重复调用"
    assert "第 1 步完全相同" in trace.steps[1].observation


def test_different_arguments_are_not_duplicates(scripted):
    scripted([
        ReActStep(thought="a", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="b", action="query_graph", action_input={"node": "便溏"}),
        ReActStep(thought="c", action="finish"),
    ])
    trace = _run()
    assert trace.terminated_by == "finish"
    assert all(s.note is None for s in trace.steps)


def test_observation_is_truncated(scripted):
    scripted([
        ReActStep(thought="拉一大坨", action="query_graph",
                  action_input={"node": "element::脾", "limit": 200}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    obs = trace.steps[0].observation
    assert len(obs) <= MAX_OBSERVATION_CHARS + 40
    assert "已截断" in obs


# ---------- prompt 拼装 ----------

def test_format_tools_covers_every_registered_tool():
    text = format_tools()
    from core.tools import TOOLS
    for name in TOOLS:
        assert name in text
    assert "必填" in text


def test_format_history_empty_and_nonempty():
    assert "第 1 步" in format_history([])
    rec = ReActStepRecord(step=1, thought="t", action="query_graph",
                          action_input={"node": "纳呆"}, observation="{}", note="重复调用")
    text = format_history([rec])
    assert "第1步" in text and "query_graph" in text and "重复调用" in text


def test_format_trace_for_s3_skips_finish_and_keeps_citation_rule():
    trace = ReActTrace(
        steps=[
            ReActStepRecord(step=1, thought="t", action="query_graph",
                            action_input={"node": "纳呆"}, observation="OBS"),
            ReActStepRecord(step=2, thought="t", action="finish", observation="（结束）"),
        ],
        terminated_by="finish",
    )
    text = format_trace_for_s3(trace)
    assert "OBS" in text
    assert "finish" not in text
    assert "cited_case_ids" in text, "附加证据里必须重申引用白名单，否则等于给了绕过的口子"


def test_format_trace_for_s3_empty_trace_is_empty_string():
    assert format_trace_for_s3(ReActTrace(steps=[], terminated_by="max_steps")) == ""


def test_react_enabled_defaults_off(monkeypatch):
    monkeypatch.delenv("USE_REACT", raising=False)
    assert react_enabled() is False
    monkeypatch.setenv("USE_REACT", "1")
    assert react_enabled() is True
    monkeypatch.setenv("USE_REACT", "0")
    assert react_enabled() is False


# ---------- 审查修复 ----------

def test_invalid_ask_user_arguments_are_fed_back_not_terminal(scripted):
    """ask_user 漏了 reason 时不能以 terminated_by="ask_user" 收尾——那样
    pending_question 是 None、却没有任何问题可问。跟别的工具一样回灌纠正。"""
    scripted([
        ReActStep(thought="想问", action="ask_user", action_input={"question": "有没有口苦？"}),
        ReActStep(thought="补上 reason", action="ask_user",
                  action_input={"question": "有没有口苦？", "reason": "分不开"}),
    ])
    trace = _run()
    assert trace.terminated_by == "ask_user"
    assert trace.pending_question == "有没有口苦？"
    assert trace.steps[0].note == "参数不合法"


def test_search_cases_results_are_collected_into_the_whitelist(scripted, monkeypatch):
    """工具描述向模型承诺"可以引用这里返回的 id"，这些 id 必须进幻觉判定的白名单。"""
    monkeypatch.setattr(react, "run_tool", lambda name, args: {
        "available": True, "cases": [{"case_id": "ye_tianshi-0042-p1-0"}, {"case_id": "ye_tianshi-0043-p1-0"}],
    } if name == "search_cases" else {"found": False})
    scripted([
        ReActStep(thought="找先例", action="search_cases",
                  action_input={"query": "纳差", "physician": "ye_tianshi"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert trace.retrieved_case_ids == ["ye_tianshi-0042-p1-0", "ye_tianshi-0043-p1-0"]


def test_pending_answer_is_shown_to_s3():
    trace = ReActTrace(steps=[ReActStepRecord(step=1, thought="t", action="ask_user",
                                              action_input={"question": "有没有口苦？", "reason": "r"},
                                              observation="{}")],
                       terminated_by="ask_user", pending_question="有没有口苦？", pending_answer="没有")
    text = format_trace_for_s3(trace)
    assert "有没有口苦？" in text and "患者答：没有" in text
