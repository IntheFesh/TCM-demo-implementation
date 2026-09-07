"""core/chain.py::consult() 的 on_step 分步进度回调离线测试（SSE 分步进度用）。

这条回调是 core/chain.py + core/react.py 两处协同产出的一条时间线，两处各自的
离线测试（tests/test_react.py 的 on_step 用例、这里的 consult 级用例）合起来
才覆盖完整——CLAUDE.md 第三次撞墙那类坑：光测 run_react() 单独发的事件测不出
consult() 会不会漏转发、会不会漏了某个自然边界不上报。

不测 SSE 传输本身（那是 api/main.py 的事，走真实 uvicorn + curl -N 验证），
这里只测"回调按什么顺序、带什么数据被调用"这个跟传输方式无关的契约。
"""
from core import chain, react
from core.schemas import S1Normalize, S3Syndrome
from tests.test_chain import FakeLLM, FakeRetriever, ReActFakeLLM, _fake_cases


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


def test_on_step_emits_expected_sequence_without_react(monkeypatch):
    """不开 ReAct、没有提问渠道（ask_fn=None）时的最简路径：s1 -> s2 -> 追问结束
    （no_answer，0 轮）-> 两位医家各 physician_start -> s3_start -> physician_done。
    残差本例不触发（FakeLLM 的 S2 只留 1 条未解释症状，低于 RESIDUAL_MIN_COUNT=2），
    所以序列里不该出现 residual_done——这条顺带钉住"没触发就不该乱发事件"。"""
    _setup_two_physicians(monkeypatch)
    events = []
    outcome = chain.consult("纳差乏力", on_step=lambda name, data: events.append((name, data)))

    names = [e[0] for e in events]
    assert names == [
        "s1_done", "s2_done", "followup_done",
        "physician_start", "s3_start", "physician_done",
        "physician_start", "s3_start", "physician_done",
    ]

    s1_data = events[0][1]
    assert s1_data["symptoms"] == outcome["s1"].symptoms

    followup_data = events[2][1]
    assert followup_data["stopped_by"] == "no_answer"  # 没传 ask_fn
    assert followup_data["rounds"] == 0

    phys_starts = [e[1]["physician"] for e in events if e[0] == "physician_start"]
    phys_dones = [e[1]["physician"] for e in events if e[0] == "physician_done"]
    assert phys_starts == ["ye_tianshi", "wu_jutong"]
    assert phys_dones == ["ye_tianshi", "wu_jutong"]
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

    names = [e[0] for e in events]
    # ReActFakeLLM 每位医家跑 2 步（查一次 + finish），两位医家各自应该是
    # physician_start, react_step, react_step, s3_start, physician_done
    assert names == [
        "s1_done", "s2_done", "followup_done",
        "physician_start", "react_step", "react_step", "s3_start", "physician_done",
        "physician_start", "react_step", "react_step", "s3_start", "physician_done",
    ]
    react_events = [e[1] for e in events if e[0] == "react_step"]
    assert [r["physician_name"] for r in react_events] == ["叶天士", "叶天士", "吴鞠通", "吴鞠通"]
    assert [r["step"] for r in react_events] == [1, 2, 1, 2]


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
