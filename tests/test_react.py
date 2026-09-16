"""core/react.py 的离线测试。用脚本化的假 LLM 驱动循环，不需要网络。

重点在五种 terminated_by 各自都能到达，以及"模型犯错"的三条路径
（工具名写错、参数不合法、原地重复）都不会让循环崩掉——ReAct 里一个异常
等于整条问诊挂掉。
"""

import pytest

from core import react
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
    """R21 **有意的契约变更**：`USE_REACT=1` 不再无条件为 True。

    原断言是 `monkeypatch.setenv("USE_REACT","1"); assert react_enabled() is True`
    ——那一版还没有 full_context 这个模式。现在默认模式是 full_context，而 ReAct
    的两件工具就是在检索语料，语料已经全在上下文里时它是冗余的（§1.3）。
    所以这条拆成两半：top3 系里 USE_REACT=1 照旧为 True（下一条测试），
    full_context 下即使显式开也关（再下一条）。
    """
    monkeypatch.delenv("USE_REACT", raising=False)
    assert react_enabled() is False
    monkeypatch.setenv("USE_REACT", "0")
    assert react_enabled() is False


def test_react_enabled_still_honours_the_flag_in_the_top3_modes(monkeypatch):
    monkeypatch.setenv("USE_REACT", "1")
    for mode in ("hybrid", "dense", "bm25", "graph"):
        monkeypatch.setenv("RETRIEVER_MODE", mode)
        assert react_enabled() is True, mode


def test_react_is_off_in_full_context_even_when_asked_for(monkeypatch, capsys):
    """**说一句再关**：静默关掉会让"我开了 ReAct 但 trace 是空的"变成一个
    查不出原因的现象。"""
    import core.react as react_mod

    monkeypatch.setattr(react_mod, "_warned_react_off_in_full_context", False)
    monkeypatch.setenv("USE_REACT", "1")
    monkeypatch.setenv("RETRIEVER_MODE", "full_context")
    assert react_enabled() is False
    err = capsys.readouterr().err
    assert "ReAct 在这个模式下关闭" in err
    assert "top3" in err


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


# ---------- on_step：SSE 分步进度用的回调 ----------


def test_on_step_not_called_when_absent(scripted):
    """不传 on_step（CLI、eval/、离线批跑的现状）时循环不该因为多了这个参数
    而改变行为——这条只是确认默认值不引入任何副作用，行为已经被上面十几条
    老测试钉死了，这里不重复断言 trace 内容。"""
    scripted([ReActStep(thought="够了", action="finish")])
    _run()  # 不传 on_step，不抛异常即通过


def test_on_step_fires_once_per_recorded_step_including_continue_paths(scripted):
    """事件数必须恒等于 trace.steps 的条数——包括"工具名写错""参数不合法"
    这类中途 continue、不终止循环的步骤。只测终止路径会漏掉这两条，
    而这正是 SSE 场景下"进度条只走了一半"这类 bug 藏身的地方。"""
    events = []
    scripted([
        ReActStep(thought="查错了名字", action="query_knowledge_graph",  # 工具名不存在 -> continue
                  action_input={"node": "纳呆"}),
        ReActStep(thought="漏了必填参数", action="lookup_standard", action_input={}),  # 参数不合法 -> continue
        ReActStep(thought="够了，这句会被截断到八十字以内用于进度展示" * 3, action="finish"),
    ])
    trace = run_react(
        name="叶天士", symptoms="纳差", elements_summary="脾",
        on_step=lambda name, data: events.append((name, data)),
    )
    assert len(events) == len(trace.steps) == 3
    assert [e[0] for e in events] == ["react_step"] * 3
    assert [e[1]["step"] for e in events] == [1, 2, 3]
    assert events[0][1]["note"] == "工具名不存在"
    assert events[1][1]["note"] == "参数不合法"
    assert events[2][1]["action"] == "finish"
    # thought 截断到 80 字，不把完整推理过程都塞进每一条 SSE 消息
    assert len(events[2][1]["thought"]) <= 80
    assert events[0][1]["physician_name"] == "叶天士"


# ---------- P1 ReAct 修复：两条止损提示 ----------
#
# 三条真实 trace 里两条把 max_steps 花在国标层的死胡同上：trace C 在两个
# 候选证候编号之间来回查，trace B 连续换词查国标图谱查不到。这两条测试组
# 分别钉住纯函数判定（快、精确）和端到端行为（提示真的进了 observation）。


def test_looks_like_standard_code_recognizes_all_three_real_formats():
    """data/standard/syndromes.jsonl 里真实出现的三种编号格式都要认得出来，
    证候名（纯中文）不能被误判成编号。"""
    assert react._looks_like_standard_code("SP-10") is True
    assert react._looks_like_standard_code("TB-127") is True
    assert react._looks_like_standard_code("B04.06.02.03.01.03") is True
    assert react._looks_like_standard_code("脾胃虚寒证") is False
    assert react._looks_like_standard_code(None) is False
    assert react._looks_like_standard_code("") is False


def test_should_hint_code_disambiguation_requires_both_consecutive_and_both_codes():
    # 两次都是编号——trace C 的场景，触发
    assert react._should_hint_code_disambiguation(
        "lookup_standard", "TB-127", "lookup_standard", "SP-10") is True
    # 两次都是证候名——正常试错（trace B 换名字就查到了），不触发
    assert react._should_hint_code_disambiguation(
        "lookup_standard", "脾阳虚证", "lookup_standard", "脾胃虚寒证") is False
    # 一个编号一个名字——不是"同类目标"
    assert react._should_hint_code_disambiguation(
        "lookup_standard", "TB-127", "lookup_standard", "脾胃虚寒证") is False
    # 上一步不是 lookup_standard——不连续，不触发
    assert react._should_hint_code_disambiguation(
        "query_graph", "TB-127", "lookup_standard", "SP-10") is False


def test_should_hint_graph_miss_requires_both_consecutive_and_both_false():
    assert react._should_hint_graph_miss(
        "query_graph", {"found": False}, "query_graph", {"found": False}) is True
    assert react._should_hint_graph_miss(
        "query_graph", {"found": False}, "query_graph", {"found": True}) is False
    assert react._should_hint_graph_miss(
        "query_graph", {"found": True}, "query_graph", {"found": False}) is False
    # lookup_standard 的 found:false 是另一件事，不归这条提示管
    assert react._should_hint_graph_miss(
        "lookup_standard", {"found": False}, "query_graph", {"found": False}) is False
    assert react._should_hint_graph_miss(None, None, "query_graph", {"found": False}) is False


def test_hint_appears_when_two_consecutive_lookup_standard_query_codes(scripted, monkeypatch):
    monkeypatch.setattr(react, "run_tool", lambda name, args: {"found": False, "candidates": []})
    scripted([
        ReActStep(thought="查第一个候选编号", action="lookup_standard", action_input={"query": "TB-127"}),
        ReActStep(thought="查第二个候选编号", action="lookup_standard", action_input={"query": "SP-10"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert react.CODE_DISAMBIGUATION_HINT not in trace.steps[0].observation
    assert react.CODE_DISAMBIGUATION_HINT in trace.steps[1].observation


def test_hint_absent_when_lookup_standard_queries_are_different_kinds(scripted, monkeypatch):
    """连续两次都是 lookup_standard，但换的是证候名不是编号——trace B 那种
    正常试错，不该被打断。"""
    monkeypatch.setattr(react, "run_tool", lambda name, args: {"found": False, "candidates": []})
    scripted([
        ReActStep(thought="先按编号查", action="lookup_standard", action_input={"query": "TB-127"}),
        ReActStep(thought="换成名字查", action="lookup_standard", action_input={"query": "脾胃虚寒证"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert react.CODE_DISAMBIGUATION_HINT not in trace.steps[0].observation
    assert react.CODE_DISAMBIGUATION_HINT not in trace.steps[1].observation


def test_hint_appears_when_two_consecutive_query_graph_miss(scripted, monkeypatch):
    monkeypatch.setattr(react, "run_tool", lambda name, args: {"found": False, "near_matches": []})
    scripted([
        ReActStep(thought="查第一个词", action="query_graph", action_input={"node": "胃中隐痛"}),
        ReActStep(thought="换个词再查", action="query_graph", action_input={"node": "胃脘痛"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert react.GRAPH_MISS_HINT not in trace.steps[0].observation
    assert react.GRAPH_MISS_HINT in trace.steps[1].observation


def test_hint_absent_when_query_graph_miss_then_hit(scripted, monkeypatch):
    results = iter([
        {"found": False, "near_matches": []},
        {"found": True, "node": "symptom::口苦", "neighbors": []},
    ])
    monkeypatch.setattr(react, "run_tool", lambda name, args: next(results))
    scripted([
        ReActStep(thought="查第一个词", action="query_graph", action_input={"node": "胃中隐痛"}),
        ReActStep(thought="换个词再查", action="query_graph", action_input={"node": "口苦"}),
        ReActStep(thought="够了", action="finish"),
    ])
    trace = _run()
    assert react.GRAPH_MISS_HINT not in trace.steps[0].observation
    assert react.GRAPH_MISS_HINT not in trace.steps[1].observation


# ---------- P1-1.1b：prompt 同时给 id 和中文名 ----------
#
# 模型在 ReAct 里只能看到 prompt 给的东西。之前只给中文名（$name），它填
# physician 参数时自然填中文名，而医案库按 id 存——医案层工具 9 次调用全空
# （SOURCES.md 第 31 条）。这三条钉住 $physician_id 真的被填进 prompt。


class _CapturingLLM(ScriptedLLM):
    """在 ScriptedLLM 基础上把每次收到的 system 提示词存下来。"""

    def __init__(self, steps):
        super().__init__(steps)
        self.systems: list[str] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.systems.append(system)
        return super().generate(system, user, schema, temperature, **kwargs)


def test_prompt_carries_physician_id_resolved_from_chinese_name(monkeypatch):
    llm = _CapturingLLM([ReActStep(thought="够了", action="finish")])
    monkeypatch.setattr(react, "get_llm", lambda: llm)
    run_react(name="叶天士", symptoms="纳差", elements_summary="脾")
    system = llm.systems[0]
    assert "「叶天士」（id: ye_tianshi）" in system
    assert "physician 参数请填 id（ye_tianshi）" in system
    assert "$physician_id" not in system  # 占位符必须被真的替换掉


def test_prompt_uses_explicit_physician_id_over_name_lookup(monkeypatch):
    """core/chain.py 显式传 id——主路径不靠中文名反查。"""
    llm = _CapturingLLM([ReActStep(thought="够了", action="finish")])
    monkeypatch.setattr(react, "get_llm", lambda: llm)
    run_react(name="叶天士", symptoms="纳差", elements_summary="脾", physician_id="wu_jutong")
    assert "id: wu_jutong" in llm.systems[0]


def test_prompt_falls_back_to_raw_name_when_name_is_unregistered(monkeypatch):
    """反查不到就原样放 name，不编一个 id——工具层会回列出可用值的报错，
    模型据此能纠正（见 run_react 文档字符串）。"""
    llm = _CapturingLLM([ReActStep(thought="够了", action="finish")])
    monkeypatch.setattr(react, "get_llm", lambda: llm)
    run_react(name="华佗", symptoms="纳差", elements_summary="脾")
    assert "「华佗」（id: 华佗）" in llm.systems[0]


def test_on_step_fires_on_llm_error_and_no_progress_too(monkeypatch):
    """error 和 no_progress 这两种终止路径也不能漏——它们各自只有一条
    独立的 return 语句，跟其余五条路径不共用同一段收尾代码。"""
    from core import react as react_mod

    events = []
    monkeypatch.setattr(react_mod, "get_llm", lambda: ScriptedLLM([LLMError("挂了")]))
    trace = run_react(name="叶天士", symptoms="纳差", elements_summary="脾",
                      on_step=lambda name, data: events.append((name, data)))
    assert trace.terminated_by == "error"
    assert len(events) == 1

    events.clear()
    monkeypatch.setattr(react_mod, "get_llm", lambda: ScriptedLLM([
        ReActStep(thought="查", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="再查一遍", action="query_graph", action_input={"node": "纳呆"}),
        ReActStep(thought="还查", action="query_graph", action_input={"node": "纳呆"}),
    ]))
    trace = run_react(name="叶天士", symptoms="纳差", elements_summary="脾",
                      on_step=lambda name, data: events.append((name, data)))
    assert trace.terminated_by == "no_progress"
    assert len(events) == len(trace.steps) == 3
