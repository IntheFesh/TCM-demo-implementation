"""FAST_MODE 三处联动的离线测试。

改造前它只管追问一处。V3 要求它同时覆盖 ReAct 步数上限和残差辨证——三处必须
同时生效，只关一半的开关是陷阱：用户以为省了预算，实际还在花。

每处一条测试 + 一条集成测试（对比 llm_calls 的具体数字），另加一条钉住
"三处读的是同一个判定函数"的结构测试。
"""
import pytest

from core import chain, react
from core.followup import fast_mode_enabled
from core.schemas import ReActStep, S3Syndrome
from tests.test_chain import FakeRetriever, ReActFakeLLM, _fake_cases


@pytest.fixture(autouse=True)
def _pin_two_physicians(monkeypatch):
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})


def _s3(cid):
    return S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                      cited_case_ids=[cid], herbs=["党参", "白术"])


# ---------- 三处各一条 ----------


def test_fast_mode_caps_react_steps(monkeypatch):
    """第 1 处：ReAct 步数上限降到 FAST_MODE_MAX_STEPS。"""
    class NeverFinishLLM:
        """永远不 finish，让循环一直跑到撞上限——这样测出来的就是上限本身。"""

        def __init__(self):
            self.calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            self.calls += 1
            return ReActStep(thought="再查一个", action="query_graph",
                             action_input={"node": f"纳呆{self.calls}"})

    fake = NeverFinishLLM()
    monkeypatch.setattr(react, "get_llm", lambda: fake)

    monkeypatch.delenv("FAST_MODE", raising=False)
    normal = react.run_react(name="叶天士", symptoms="纳差", elements_summary="脾")
    assert len(normal.steps) == react.MAX_STEPS == 5

    fake.calls = 0
    monkeypatch.setenv("FAST_MODE", "1")
    fast = react.run_react(name="叶天士", symptoms="纳差", elements_summary="脾")
    assert len(fast.steps) == react.FAST_MODE_MAX_STEPS == 2
    assert fast.terminated_by == "max_steps"


def test_explicit_max_steps_beats_fast_mode(monkeypatch):
    """显式传的数字优先于环境变量——跟 use_react / eval_mode 一致。"""
    class NeverFinishLLM:
        def __init__(self):
            self.n = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            # 每步换一个 node：连续重复同一个调用会触发 no_progress 提前终止，
            # 那样测出来的就不是步数上限了
            self.n += 1
            return ReActStep(thought="t", action="query_graph",
                             action_input={"node": f"纳呆{self.n}"})

    # 复用同一个实例：写成 lambda: NeverFinishLLM() 的话每步都新建对象、self.n
    # 一直归零，node 名不变会触发 no_progress 提前终止，测出来就不是步数上限了
    fake = NeverFinishLLM()
    monkeypatch.setattr(react, "get_llm", lambda: fake)
    monkeypatch.setenv("FAST_MODE", "1")
    trace = react.run_react(name="叶天士", symptoms="纳差", elements_summary="脾", max_steps=4)
    assert len(trace.steps) == 4


def test_fast_mode_skips_followup(monkeypatch):
    """第 2 处：追问轮数降到 0（改造前就有，这里一并钉住，防止回归）。"""
    from core.followup import run_followup

    asked = []
    monkeypatch.setenv("FAST_MODE", "1")
    result = run_followup(["纳差"], ["脾"], lambda q: asked.append(q) or "有")
    assert result.rounds == 0
    assert result.stopped_by == "fast_mode"
    assert asked == [], "开了 FAST_MODE 一个问题都不该问"


def test_fast_mode_disables_residual(monkeypatch):
    """第 3 处：残差辨证整体关闭，哪怕未解释症状占比远超阈值。"""
    from core.schemas import ElementHit, S1Normalize, S2Elements

    # 4 条症状只有 1 条被解释 -> 未解释 3 条、占比 75%，远超 RESIDUAL_THRESHOLD
    s1 = S1Normalize(symptoms=["纳差", "乏力", "腹胀", "便溏"])
    s2 = S2Elements(elements=[ElementHit(element="脾", kind="location",
                                         supporting_symptoms=["纳差"], confidence="high")])
    calls = []
    monkeypatch.setattr(chain, "infer_elements", lambda x: calls.append(1) or S2Elements())

    monkeypatch.delenv("FAST_MODE", raising=False)
    assert chain.run_residual(s1, s2) is not None, "正常模式下这组输入应该触发残差"
    assert len(calls) == 1, "残差要真的多花一次 S2"

    calls.clear()
    monkeypatch.setenv("FAST_MODE", "1")
    assert chain.run_residual(s1, s2) is None
    assert calls == [], "关掉之后一次调用都不该发生"


# ---------- 集成：调用数的具体对比 ----------


def _consult_calls(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)

    class NeverFinishLLM(ReActFakeLLM):
        """ReAct 每步都不 finish，逼它跑满步数——这样两种模式的差值才是上限差值，
        不受"模型偶然早收尾"影响。每步换一个 node：连续重复同一个调用会触发
        no_progress 提前终止（实测过：不换的话正常模式只跑到 12 次而不是 16）。"""

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is ReActStep:
                self.calls.append("ReActStep")
                return ReActStep(thought="再查", action="query_graph",
                                 action_input={"node": f"纳呆{len(self.calls)}"})
            return super().generate(system, user, schema, temperature, **kwargs)

    fake = NeverFinishLLM({"叶天士": _s3("ye_tianshi-001"), "吴鞠通": _s3("wu_jutong-001")})
    monkeypatch.setattr(chain, "get_llm", lambda: fake)
    monkeypatch.setattr(react, "get_llm", lambda: fake)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    outcome = chain.consult("纳差乏力", use_react=True, ask_fn=lambda q: "有")
    return outcome, fake


def test_fast_mode_integration_cuts_llm_calls(monkeypatch):
    """一次完整 consult（开 ReAct + 有提问渠道）两种模式的调用数对比。

    实测（manifest.llm_calls 与真实 generate 次数完全一致，没有漏算）：
      正常模式 16 次 = S1 1 + S2 1 + 追问后重跑 S2 1 + 残差 S2 1
                       + 两位医家各 (ReAct 5 步 + S3 1) = 4 + 12
      FAST_MODE 8 次 = S1 1 + S2 1 + 两位医家各 (ReAct 2 步 + S3 1) = 2 + 6
                       （追问 0 轮所以不重跑 S2，残差整体关闭）
    降幅一半。这两个数字任何一个变了都要先想清楚为什么，不要直接改期望值。
    """
    normal, fake_n = _consult_calls(monkeypatch, FAST_MODE=None)
    normal_calls = normal["manifest"]["llm_calls"]

    fast, fake_f = _consult_calls(monkeypatch, FAST_MODE="1")
    fast_calls = fast["manifest"]["llm_calls"]

    assert normal_calls == 16, f"正常模式实际 {normal_calls} 次：{fake_n.calls}"
    assert fast_calls == 8, f"FAST_MODE 实际 {fast_calls} 次：{fake_f.calls}"
    assert fast_calls < normal_calls
    # 三处降级都体现在调用数里
    assert fake_f.calls.count("ReActStep") == 4, "两位医家各 2 步"
    assert fake_f.calls.count("S2Elements") == 1, "追问不重跑 S2、残差不跑"
    assert fast["followup"].stopped_by == "fast_mode"
    assert fast["residual"] is None
    # 关键：省了调用不等于不出结果，两位医家照样出方
    assert len(fast["results"]) == 2


# ---------- 结构：三处读的是同一个判定 ----------


def test_all_three_paths_read_the_same_judgment_function():
    """闸门：三处代码路径都调 core.followup.fast_mode_enabled，不各写一套
    os.environ.get("FAST_MODE") 的判断。"""
    import inspect

    for mod in (chain, react):
        src = inspect.getsource(mod)
        assert "fast_mode_enabled" in src, f"{mod.__name__} 没调用统一判定函数"
        assert 'environ.get("FAST_MODE"' not in src, f"{mod.__name__} 自己读了环境变量"
        assert "environ['FAST_MODE'" not in src


def test_fast_mode_enabled_accepts_common_truthy_forms(monkeypatch):
    for truthy in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("FAST_MODE", truthy)
        assert fast_mode_enabled() is True, truthy
    for falsy in ("0", "no", "", "off"):
        monkeypatch.setenv("FAST_MODE", falsy)
        assert fast_mode_enabled() is False, falsy
    monkeypatch.delenv("FAST_MODE", raising=False)
    assert fast_mode_enabled() is False


def test_prompt_remaining_counter_follows_the_fast_mode_cap(monkeypatch):
    """prompt 里的「还剩 N 步」必须跟着降级后的上限走，不能还报 5。

    SOURCES.md 第 11/12 条实测过：这个计数器会实质影响模型什么时候收尾。
    上限降到 2 却告诉模型"还剩 5 步"，它会按 5 步规划、必然被截断，轨迹看起来
    像"模型不会收尾"，实际是被上限卡死的——那正是那两条要求把这两种情况分开
    看的原因。
    """
    seen = []

    class RecordingLLM:
        def __init__(self):
            self.n = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            seen.append(system)
            self.n += 1
            return ReActStep(thought="t", action="query_graph",
                             action_input={"node": f"纳呆{self.n}"})

    fake = RecordingLLM()
    monkeypatch.setattr(react, "get_llm", lambda: fake)
    monkeypatch.setenv("FAST_MODE", "1")
    react.run_react(name="叶天士", symptoms="纳差", elements_summary="脾")

    assert len(seen) == react.FAST_MODE_MAX_STEPS == 2
    assert "还剩 2 步" in seen[0], seen[0][-200:]
    assert "还剩 1 步" in seen[1], seen[1][-200:]
