"""R12-A：三位医家并发跑 S3 的离线测试。

并发改的是"什么时候跑"，不许改"跑出什么"。这个文件逐条钉住四条约束
（`core/chain.py::_run_physicians_into` 的文档字符串列的那四条），以及一条最容易
在并发下被悄悄破坏的语义：安全否决必须取消其余医家、一条方药都不返回。
"""
import threading
import time

import pytest

from core import chain
from core.schemas import S3Syndrome
from tests.test_chain import FakeLLM, FakeRetriever, _fake_cases


@pytest.fixture(autouse=True)
def _three_physicians(monkeypatch):
    """用真实注册表的全部医家跑，不裁剪——并发的收益和风险都随医家数变化。"""
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", dict(REG))


def _s3(name: str) -> S3Syndrome:
    return S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                      cited_case_ids=["ye_tianshi-001"], herbs=[name])


def _setup(monkeypatch, llm=None):
    from core.physicians import PHYSICIANS as REG

    fake = llm or FakeLLM({info["name"]: _s3(pid) for pid, info in REG.items()})
    monkeypatch.setattr(chain, "get_llm", lambda: fake)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    return fake


class SlowLLM(FakeLLM):
    """S3 睡一会儿的假后端，用来把"并发了没有"变成一个可测的时间差。"""

    def __init__(self, *args, delay: float = 0.3, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay
        self.concurrent_peak = 0
        self._in_flight = 0
        self._lock = threading.Lock()

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        from core.schemas import _S3Base

        if issubclass(schema, _S3Base):
            with self._lock:
                self._in_flight += 1
                self.concurrent_peak = max(self.concurrent_peak, self._in_flight)
            try:
                time.sleep(self.delay)
            finally:
                with self._lock:
                    self._in_flight -= 1
        return super().generate(system, user, schema, temperature, **kwargs)


def test_results_stay_in_registry_order(monkeypatch):
    """**顺序是契约**：前端三列按它排、分歧度两两配对按它取。并发下完成顺序是乱的，
    所以收集结果时按 `PHYSICIANS` 的顺序取 future，不用 as_completed 的顺序。"""
    _setup(monkeypatch)
    outcome = chain.consult("纳差乏力")
    assert [r["physician"] for r in outcome["results"]] == list(chain.PHYSICIANS)


def test_the_three_physicians_really_run_at_the_same_time(monkeypatch):
    """并发的机器可验证据有两条，缺一不可：
    ① 同一时刻在飞的 S3 调用数达到过医家数（不是一个接一个）；
    ② 总耗时接近**一次** delay 而不是三次。
    只看 ② 的话，"某位医家被跳过了"也会让总耗时变短。
    """
    llm = SlowLLM({info["name"]: _s3(pid) for pid, info in chain.PHYSICIANS.items()},
                  delay=0.3)
    _setup(monkeypatch, llm)
    t0 = time.perf_counter()
    outcome = chain.consult("纳差乏力")
    elapsed = time.perf_counter() - t0
    n = len(chain.PHYSICIANS)
    assert llm.concurrent_peak == n, f"同时在飞的只有 {llm.concurrent_peak} 个"
    assert elapsed < 0.3 * n * 0.7, f"总耗时 {elapsed:.2f}s 看起来还是串行"
    assert len(outcome["results"]) == n, "并发不能少跑任何一位医家"


def test_max_workers_follows_the_registry_size(monkeypatch):
    """注册表加到四位医家时并发度要跟着变——写死 3 的话第四位会排队等前三位。"""
    captured = {}
    real_pool = chain.ThreadPoolExecutor

    def spy(*args, **kwargs):
        captured["max_workers"] = kwargs.get("max_workers")
        return real_pool(*args, **kwargs)

    monkeypatch.setattr(chain, "ThreadPoolExecutor", spy)
    from core.physicians import PHYSICIANS as REG

    four = dict(REG)
    four["li_ke"] = {"name": "李可", "book": "李可医案", "years": "1930-2013",
                     "school": "扶阳", "color": "#8A4736"}
    monkeypatch.setattr(chain, "PHYSICIANS", four)
    _setup(monkeypatch, FakeLLM({info["name"]: _s3(pid) for pid, info in four.items()}))
    chain.consult("纳差乏力")
    assert captured["max_workers"] == 4


def test_llm_calls_are_summed_across_workers(monkeypatch):
    """每位医家的调用数在自己的线程里数，主线程求和。少算一次，manifest 里的
    成本和"这次跑了几次"就都是错的。"""
    _setup(monkeypatch)
    outcome = chain.consult("纳差乏力")
    # S1 + S2 + 每位医家一次 S3
    assert outcome["manifest"]["llm_calls"] == 2 + len(chain.PHYSICIANS)


def test_a_safety_veto_in_one_worker_cancels_the_rest_and_returns_no_formula(monkeypatch):
    """**被拦截的请求不产出任何方药**——这是改并发之前就有的语义，不许变松。
    ReAct 追问问出危重症状时，已经跑完的医家结果也不返回。"""
    from core.chain import SafetyVeto

    started = threading.Event()

    class VetoLLM(FakeLLM):
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            from core.schemas import _S3Base

            if issubclass(schema, _S3Base) and chain.PHYSICIANS["wu_jutong"]["name"] in system:
                started.set()
                raise SafetyVeto("追问问出便血", llm_calls=1)
            if issubclass(schema, _S3Base):
                time.sleep(0.2)  # 别人还在跑的时候否决才谈得上"取消其余线程"
            return super().generate(system, user, schema, temperature, **kwargs)

    _setup(monkeypatch, VetoLLM({info["name"]: _s3(pid)
                                 for pid, info in chain.PHYSICIANS.items()}))
    outcome = chain.consult("纳差乏力")
    assert started.is_set()
    assert outcome["rejected"] is True
    assert outcome["results"] == [], "被否决之后一条方药都不许返回"
    assert "便血" in outcome["reject_reason"]


def test_a_real_failure_in_one_worker_is_not_swallowed(monkeypatch):
    """一位医家真的失败（LLMError 之类）时，不能因为别人成功了就被吞掉——
    串行时它会直接冒出去，并发之后也必须。"""
    from core.llm import LLMError

    class BrokenLLM(FakeLLM):
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            from core.schemas import _S3Base

            if issubclass(schema, _S3Base) and chain.PHYSICIANS["ye_tianshi"]["name"] in system:
                raise LLMError("这位医家的 S3 三次都失败了")
            return super().generate(system, user, schema, temperature, **kwargs)

    _setup(monkeypatch, BrokenLLM({info["name"]: _s3(pid)
                                   for pid, info in chain.PHYSICIANS.items()}))
    with pytest.raises(LLMError, match="三次都失败"):
        chain.consult("纳差乏力")


def test_ask_fn_is_serialized_across_physicians(monkeypatch):
    """追问渠道背后是一个人，同一时刻只可能回答一个问题。`_ConsultStream.ask` 的
    `_pending` 也是"每个问题一条队列"的形状——两位医家同时提问，后一位会覆盖前一位
    的队列，前一位于是永远等不到答案。所以并发之后必须串行化。"""
    overlap = []
    in_ask = {"n": 0}
    lock = threading.Lock()

    def ask(question: str) -> str:
        with lock:
            in_ask["n"] += 1
            overlap.append(in_ask["n"])
        time.sleep(0.05)
        with lock:
            in_ask["n"] -= 1
        return "没有"

    serialized = chain._serialize_ask(ask)
    threads = [threading.Thread(target=serialized, args=(f"问题{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert overlap == [1, 1, 1, 1], f"有两个问题同时在等回答：{overlap}"


def test_serialize_ask_passes_none_through():
    """不传提问渠道时不许凭空造一个——没有 ask_fn 的语义是"不追问"，
    包一层 lock 之后变成"有渠道但答不上来"是另一回事。"""
    assert chain._serialize_ask(None) is None


def test_context_vars_reach_the_worker_threads(monkeypatch):
    """`use_llm()` 的逐请求后端覆盖走的是 ContextVar（BYOK、超额降级到回放都靠它），
    而 ContextVar **不会**自动跟着 ThreadPoolExecutor 的线程走。不显式
    `copy_context().run` 的话，worker 里 `get_llm()` 拿到的是进程单例——访问者的
    key 没被用上、额度照扣，而且**一声不响**。"""
    import core.llm as llm_mod
    from core.physicians import PHYSICIANS as REG

    override = FakeLLM({info["name"]: _s3("来自覆盖后端") for info in REG.values()})
    singleton = FakeLLM({info["name"]: _s3("来自进程单例") for info in REG.values()})
    monkeypatch.setattr(llm_mod, "_llm_singleton", singleton)
    monkeypatch.setattr(chain, "get_llm", llm_mod.get_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    with llm_mod.use_llm(override):
        outcome = chain.consult("纳差乏力")
    herbs = {tuple(r["s3"].herbs) for r in outcome["results"]}
    assert herbs == {("来自覆盖后端",)}, f"worker 里拿到的不是覆盖的后端：{herbs}"


def test_every_physician_event_carries_the_physician_id(monkeypatch):
    """并发之后事件会交错，前端只能按 `physician` 字段路由到对应的列。
    任何一个医家事件漏了这个字段，那一列就永远不会更新。"""
    _setup(monkeypatch)
    events = []
    chain.consult("纳差乏力", use_react=False,
                  on_step=lambda name, data: events.append((name, data)))
    physician_events = [e for e in events
                        if e[0] in ("physician_start", "physician_done", "s3_start")]
    assert physician_events
    for name, data in physician_events:
        assert data.get("physician") in chain.PHYSICIANS, (name, data)


def test_s1_still_runs_exactly_once_for_all_physicians(monkeypatch):
    """CLAUDE.md 明令：S1 全局只跑一次，两位医家共用结果。并发很容易把它变成
    "每个 worker 各跑一次"——那样三位医家的症状列表会不同，后面构图时症状节点
    id 对不上，边会指向不存在的节点。"""
    fake = _setup(monkeypatch)
    chain.consult("纳差乏力")
    assert fake.calls.count("S1Normalize") == 1
    assert fake.calls.count("S2Elements") == 1
