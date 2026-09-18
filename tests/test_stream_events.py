"""core/chain.py::consult() 的 on_step 分步进度回调离线测试（SSE 分步进度用）。

这条回调是 core/chain.py + core/react.py 两处协同产出的一条时间线，两处各自的
离线测试（tests/test_react.py 的 on_step 用例、这里的 consult 级用例）合起来
才覆盖完整——CLAUDE.md 第三次撞墙那类坑：光测 run_react() 单独发的事件测不出
consult() 会不会漏转发、会不会漏了某个自然边界不上报。

不测 SSE 传输本身（那是 api/main.py 的事，走真实 uvicorn + curl -N 验证），
这里只测"回调按什么顺序、带什么数据被调用"这个跟传输方式无关的契约。
"""
import pytest

from core import chain, react
from core.schemas import S1Normalize, S3Syndrome
from tests.test_chain import FakeLLM, FakeRetriever, ReActFakeLLM, _fake_cases


@pytest.fixture(autouse=True)
def _pin_two_physicians(monkeypatch):
    """跟 tests/test_chain.py 的同名 fixture钉住同一件事，但这里必须单独声明一份：
    autouse fixture 的作用域是它所在的模块，从 test_chain 导入 FakeLLM 等类不会
    把那边的 autouse 一并带过来。这个文件里的用例把"两位医家各一段"写死在事件
    序列的断言里（physician_start/physician_done 的名字列表、事件条数），注册表
    增长到三位、四位不该让这些断言无缘无故变红——那不是这些用例要测的东西。"""
    from core.physicians import PHYSICIANS as REG

    two = {k: REG[k] for k in ("ye_tianshi", "wu_jutong")}
    monkeypatch.setattr(chain, "PHYSICIANS", two)


def _setup_two_physicians(monkeypatch):
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"], herbs=["党参", "白术"])
    s3_wu = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                       cited_case_ids=["wu_jutong-001"], herbs=["茯苓", "陈皮"])
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    return fake_llm


def test_on_step_absent_does_not_change_anything(monkeypatch):
    """不传 on_step 是这个函数迄今为止的全部调用方（CLI、eval/、老测试）的现状，
    这条只确认默认值不引入副作用——具体行为已经被 test_chain.py 里几十条老测试
    钉死，这里不重复断言 outcome 的内容。"""
    _setup_two_physicians(monkeypatch)
    chain.consult("纳差乏力")  # 不传 on_step，不抛异常即通过


def _prefix_and_per_physician(events):
    """把事件流拆成「全局前缀」和「每位医家各自的子序列」。

    **R12 起必须这么断言，这是有意的契约变更**：三位医家改成并发之后，
    `physician_start` / `react_step` / `s3_start` / `physician_done` 在时间线上会
    交错（叶天士的 done 完全可能排在吴鞠通的 start 后面），一条写死的全局顺序在
    并发下**必然**不稳定——写这条辅助函数之前实测过：同一条测试连跑几次，有时
    碰巧跟串行顺序一样、有时不一样，这种"有时绿"比一直红更糟。

    它原来钉的其实是两件事，两件都没变，所以判据拆成两条：
    ① 全局前缀（S1 → S2 → 追问）仍然有序、仍然排在所有医家事件之前；
    ② 每位医家自己的子序列仍然是 start →（react_step…）→ s3_start → done。
    （docs/DESIGN.md §4.7 的订正写的就是这条：前端必须按 `physician` 字段路由，
    不许假设顺序。）
    """
    names = [e[0] for e in events]
    # R36：`s3_delta` / `s3_done` 也是医家事件（带 physician 字段、前端按它路由）。
    # 加进这个集合不是为了让断言过——下面那条 `assert name in physician_events`
    # 正是"新事件没人接"的守卫，漏掉的话新事件会被当成"混进来的东西"。
    physician_events = {"physician_start", "react_step", "s3_start",
                        "s3_delta", "s3_done", "physician_done"}
    first = next(i for i, n in enumerate(names) if n in physician_events)
    last = max(i for i, n in enumerate(names) if n in physician_events)
    # R55：`agent_step` 是 `consult()` 收尾时（取证/自验/个体化这几笔小结，
    # core/chain.py 里 `trace.record("gather_evidence"/"verify_and_revise"/
    # "verify_patient_fit", ...)`）广播的**全局**事件，不属于任何一位医家——
    # 它只会在全部医家都跑完之后成段出现，不会夹在医家事件中间。单独校验这一
    # 段，不然会被下面那条"医家事件之间混进了 xxx"的守卫误判成一种没人认的
    # 医家事件（这份测试本身就是当年在真机上发现"新事件没人接"这类 bug 之后
    # 加的守卫，agent_step 是这一轮新增的合法信号，不是需要拦下的噪音）。
    tail = names[last + 1:]
    assert all(n == "agent_step" for n in tail), (
        f"医家事件结束之后只该跟着 agent_step 收尾事件，混进了 {tail}"
    )
    per_physician: dict[str, list[str]] = {}
    for name, data in events[first:last + 1]:
        assert name in physician_events, f"医家事件之间混进了 {name}"
        pid = data.get("physician")
        assert pid, f"{name} 事件没带 physician 字段——并发之后前端没法路由"
        per_physician.setdefault(pid, []).append(name)
    return names[:first], per_physician


def test_on_step_emits_expected_sequence_without_react(monkeypatch):
    """不开 ReAct、没有提问渠道（ask_fn=None）时的最简路径：s1 -> s2 -> 追问结束
    （no_answer，0 轮）-> 两位医家各 physician_start -> s3_start -> physician_done。
    残差本例不触发（FakeLLM 的 S2 只留 1 条未解释症状，低于 RESIDUAL_MIN_COUNT=2），
    所以序列里不该出现 residual_done——这条顺带钉住"没触发就不该乱发事件"。"""
    _setup_two_physicians(monkeypatch)
    events = []
    outcome = chain.consult("纳差乏力", on_step=lambda name, data: events.append((name, data)))

    prefix, per_physician = _prefix_and_per_physician(events)
    assert prefix == ["s1_done", "s2_done", "followup_done"]
    assert set(per_physician) == {"ye_tianshi", "wu_jutong"}
    for pid, seq in per_physician.items():
        # R36 起 S3 结束也发一个事件（`s3_done`，带流式计数与"为什么没流式"）。
        # **它排在 physician_done 之前**：s3_done 说的是"模型的文本出完了"，
        # physician_done 说的是"这一位的结论定了"（中间还有安全层/验证器）。
        assert seq == ["physician_start", "s3_start", "s3_done",
                       "physician_done"], (pid, seq)

    s1_data = events[0][1]
    assert s1_data["symptoms"] == outcome["s1"].symptoms

    followup_data = events[2][1]
    assert followup_data["stopped_by"] == "no_answer"  # 没传 ask_fn
    assert followup_data["rounds"] == 0

    # 并发之后谁先发 start 是不确定的，能钉的是"两位都发了、各发一次"。
    # **结果的顺序仍是注册表顺序**（下面 outcome["results"] 那条）——"结果有序"
    # 是契约（前端三列按它排），"事件有序"并发之后不再成立，两件事。
    phys_starts = [e[1]["physician"] for e in events if e[0] == "physician_start"]
    phys_dones = [e[1]["physician"] for e in events if e[0] == "physician_done"]
    assert sorted(phys_starts) == ["wu_jutong", "ye_tianshi"]
    assert sorted(phys_dones) == ["wu_jutong", "ye_tianshi"]
    assert [r["physician"] for r in outcome["results"]] == ["ye_tianshi", "wu_jutong"]
    # physician_done 带的证型/用药要跟 outcome["results"] 里真实的一致，
    # 不能是"发了个事件"但内容对不上
    ye_done = next(e[1] for e in events if e[0] == "physician_done" and e[1]["physician"] == "ye_tianshi")
    assert ye_done["syndrome"] == "脾胃气虚"
    assert ye_done["herbs"] == ["党参", "白术"]


def test_on_step_react_step_events_are_interleaved_between_start_and_s3(monkeypatch):
    """开了 ReAct 时，react_step 事件必须夹在 physician_start 和 s3_start 之间
    ——这是 run_physician 内部"先取证、再开方"这个顺序在事件时间线上的镜像，
    顺序错了会让前端进度条在"取证中"和"开方中"之间跳来跳去。"""
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"], herbs=["党参"])
    s3_wu = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                       cited_case_ids=["wu_jutong-001"], herbs=["茯苓"])
    fake_llm = ReActFakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.setattr(react, "get_llm", lambda: fake_llm)

    events = []
    chain.consult("纳差乏力", use_react=True,
                  on_step=lambda name, data: events.append((name, data)))

    prefix, per_physician = _prefix_and_per_physician(events)
    assert prefix == ["s1_done", "s2_done", "followup_done"]
    # ReActFakeLLM 每位医家跑 2 步（查一次 + finish）。**这一条是这个测试的核心**：
    # 并发只让医家之间交错，医家**内部**"先取证、再开方"的顺序一点没变。
    for pid, seq in per_physician.items():
        assert seq == ["physician_start", "react_step", "react_step",
                       "s3_start", "s3_done", "physician_done"], (pid, seq)
    steps: dict[str, list[int]] = {}
    names_seen: dict[str, str] = {}
    for name, data in events:
        if name == "react_step":
            steps.setdefault(data["physician"], []).append(data["step"])
            names_seen[data["physician"]] = data["physician_name"]
    assert steps == {"ye_tianshi": [1, 2], "wu_jutong": [1, 2]}
    assert names_seen == {"ye_tianshi": "叶天士", "wu_jutong": "吴鞠通"}


def test_on_step_reports_residual_when_triggered(monkeypatch):
    """残差辨证真的触发时要能在事件流里看到——照抄 test_chain.py 里触发残差的
    构造方式（3 条未解释症状，占比超阈值），不是凭空编一个新场景。"""
    from core.schemas import ElementHit, S2Elements

    class ResidualFakeLLM(FakeLLM):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._s2_calls = 0

        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is S2Elements:
                self._s2_calls += 1
                if self._s2_calls == 1:
                    return S2Elements(elements=[
                        ElementHit(element="脾", kind="location",
                                  supporting_symptoms=["纳差"], confidence="high"),
                    ])
                # 残差轮：把「乏力」「腹胀」都解释掉
                return S2Elements(elements=[
                    ElementHit(element="气虚", kind="nature",
                              supporting_symptoms=["乏力", "腹胀"], confidence="medium"),
                ])
            return super().generate(system, user, schema, temperature, **kwargs)

    fake_llm = ResidualFakeLLM(
        {"叶天士": S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                             cited_case_ids=["ye_tianshi-001"], herbs=["党参"]),
         "吴鞠通": S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                             cited_case_ids=["wu_jutong-001"], herbs=["茯苓"])},
        s1=S1Normalize(symptoms=["纳差", "乏力", "腹胀"], tongue=None, pulse=None, unmapped=[]),
    )
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    events = []
    chain.consult("纳差乏力腹胀", on_step=lambda name, data: events.append((name, data)))
    names = [e[0] for e in events]
    assert "residual_done" in names
    residual_data = next(e[1] for e in events if e[0] == "residual_done")
    assert set(residual_data["newly_explained"]) == {"乏力", "腹胀"}
    # residual_done 必须在 s2_done 之后、physician_start 之前——它是"两位医家
    # 共用的一步"，不该跟任何一位医家的进度绑在一起
    assert names.index("residual_done") < names.index("physician_start")
    assert names.index("s2_done") < names.index("residual_done")


def test_on_step_emits_full_sequence_when_one_physician_has_empty_retrieval(monkeypatch):
    """某位医家检索为空（真实场景：min_score 卡掉全部结果，叶天士出现过这种情况）时，
    S3 改走 S3SyndromeUnreferenced，但那位医家的 SSE 事件序列不该因此缺一段——
    physician_start / s3_start / physician_done 三个都要在。

    这条不是新场景，是给"检索为空"这条路径补的事件完整性回归：这里注册张锡纯前
    就曾经因为假 LLM 只认 S3Syndrome（恒等比较，见 tests/test_chain.py 的
    issubclass(schema, _S3Base) 那处修复）而崩溃过——不是这些用例本来就测过的东西
    没测出来，是这条路径以前根本没有真正走通过。不新增假医家、不碰
    _fake_cases()：用已有的两位医家，只让其中一位的检索结果为空，跟真实会发生的
    场景（阈值卡掉全部结果）保持同一种成因。
    """
    s3_ye = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                       cited_case_ids=["ye_tianshi-001"], herbs=["党参", "白术"])
    s3_wu = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                       cited_case_ids=["wu_jutong-001"], herbs=["茯苓", "陈皮"])
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    # 只留叶天士的医案，吴鞠通检索为空
    only_ye_cases = [c for c in _fake_cases() if c.physician == "ye_tianshi"]
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(only_ye_cases))

    events = []
    outcome = chain.consult("纳差乏力", on_step=lambda name, data: events.append((name, data)))

    prefix, per_physician = _prefix_and_per_physician(events)
    assert prefix == ["s1_done", "s2_done", "followup_done"]
    assert set(per_physician) == {"ye_tianshi", "wu_jutong"}
    for pid, seq in per_physician.items():
        # R36 起 S3 结束也发一个事件（`s3_done`，带流式计数与"为什么没流式"）。
        # **它排在 physician_done 之前**：s3_done 说的是"模型的文本出完了"，
        # physician_done 说的是"这一位的结论定了"（中间还有安全层/验证器）。
        assert seq == ["physician_start", "s3_start", "s3_done",
                       "physician_done"], (pid, seq)
    # 并发之后谁先发 start 是不确定的，能钉的是"两位都发了、各发一次"。
    # **结果的顺序仍是注册表顺序**（下面 outcome["results"] 那条）——"结果有序"
    # 是契约（前端三列按它排），"事件有序"并发之后不再成立，两件事。
    phys_starts = [e[1]["physician"] for e in events if e[0] == "physician_start"]
    phys_dones = [e[1]["physician"] for e in events if e[0] == "physician_done"]
    assert sorted(phys_starts) == ["wu_jutong", "ye_tianshi"]
    assert sorted(phys_dones) == ["wu_jutong", "ye_tianshi"]
    assert [r["physician"] for r in outcome["results"]] == ["ye_tianshi", "wu_jutong"]

    # 确认吴鞠通那一支确实走的是检索为空这条路径，不是碰巧凑对了序列——
    # 序列完整不能靠巧合证明，要靠"真的触发了这条路径"来证明
    ye_result = next(r for r in outcome["results"] if r["physician"] == "ye_tianshi")
    wu_result = next(r for r in outcome["results"] if r["physician"] == "wu_jutong")
    assert ye_result["no_reference_cases"] is False
    assert wu_result["no_reference_cases"] is True
    assert wu_result["s3"].cited_case_ids == []
