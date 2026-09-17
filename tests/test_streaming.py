"""R36：流式输出（后端 → 链路 → SSE → 前端）。

**这个文件要钉住的不是"流式能跑"，是"没流式的时候能说出为什么"。** R32 的教训
（演示跑的配置从来没被测过）在流式这件事上会以另一种形状重演：界面上转着圈，
而没人能区分「模型在想」「后端不支持流式」「best-of-N 这一路不流式」「卡住了」。
所以每一条"没有增量"的路径都必须带一句可读的原因，而且那句原因有测试。
"""
from __future__ import annotations

import json
import queue

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import chain
from core.llm import (
    LLMBackend,
    OpenAICompatBackend,
    _delta_texts,
    new_usage_stats,
    current_usage_stats,
)
from core.schemas import S1Normalize, S2Elements, S3Syndrome, ElementHit

from tests.test_api import _fake_outcome
from tests.test_chain import FakeRetriever, _case


# ---------- 夹具：能流式的假后端 ----------

class _Chunk:
    """模仿 SDK 的一个流式 chunk。`usage` 只在最后一个 chunk 上。"""

    class _Delta:
        def __init__(self, content=None, reasoning_content=None):
            self.content = content
            self.reasoning_content = reasoning_content

    class _Choice:
        def __init__(self, delta):
            self.delta = delta

    def __init__(self, content=None, reasoning=None, usage=None, no_choices=False):
        self.choices = [] if no_choices else [self._Choice(self._Delta(content, reasoning))]
        self.usage = usage


class StreamingFake(LLMBackend):
    """把预设文本切片吐出来的后端。`SUPPORTS_STREAMING=True` 才会收到 on_delta。"""

    SUPPORTS_STREAMING = True
    CHUNK = 30

    def __init__(self, payloads: dict[str, str], chunk: int | None = None):
        self.payloads = payloads
        self.chunk = chunk or self.CHUNK
        self.saw_on_delta: list[bool] = []

    def model_name(self):
        return "streaming-fake"

    def backend_id(self):
        return "streaming-fake"

    def comparability_warning(self):
        return "测试用假后端"

    def lora_for(self, physician=None):
        return None

    def lora_dir(self):
        return None

    def replay_info(self):
        return None

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, on_delta=None, **kwargs):
        self.saw_on_delta.append(on_delta is not None)
        text = self.payloads[schema.__name__]
        if on_delta is not None:
            for i in range(0, len(text), self.chunk):
                on_delta(text[i:i + self.chunk], "content")
        return text


def _s1_json():
    return S1Normalize(symptoms=["纳差", "乏力"], tongue="淡红", pulse="细弱",
                       unmapped=[]).model_dump_json()


def _s2_json():
    return S2Elements(elements=[ElementHit(
        element="脾", kind="location", supporting_symptoms=["纳差"],
        confidence="high")], unexplained_symptoms=[]).model_dump_json()


def _s3_json():
    return S3Syndrome(syndrome="脾胃气虚", reasoning="纳差乏力，脾失健运。" * 6,
                      treatment_principle="健脾益气",
                      formula="四君子汤", herbs=["党参", "白术", "茯苓", "甘草"],
                      cited_case_ids=["ye_tianshi-001"]).model_dump_json()


@pytest.fixture
def streaming_llm():
    return StreamingFake({"S1Normalize": _s1_json(), "S2Elements": _s2_json(),
                          "S3Syndrome": _s3_json(), "S3SyndromeUnreferenced": _s3_json()})


# ---------- 一、chunk 解析：思考与正文分开 ----------

def test_content_and_reasoning_are_reported_as_different_kinds():
    assert _delta_texts(_Chunk(content="方")) == [("方", "content")]
    assert _delta_texts(_Chunk(reasoning="先辨病位")) == [("先辨病位", "reasoning")]


def test_a_chunk_with_both_reports_both():
    got = _delta_texts(_Chunk(content="党参", reasoning="补气"))
    assert got == [("党参", "content"), ("补气", "reasoning")]


def test_the_usage_only_chunk_has_no_text():
    """流式的最后一个 chunk 只带 usage、`choices` 是空的——那是正常的，不是错误。"""
    assert _delta_texts(_Chunk(no_choices=True, usage={"x": 1})) == []


def test_empty_pieces_are_not_reported():
    assert _delta_texts(_Chunk(content="")) == []
    assert _delta_texts(_Chunk(content=None)) == []


# ---------- 二、后端：请求参数与 usage ----------

def test_streaming_request_turns_on_include_usage(monkeypatch):
    """**没有 include_usage 的话流式响应里一个 usage 都没有**，manifest 的
    cache_hit_ratio 静默变 None——R21 那个坑换到流式这条路上的同一个形状。"""
    captured: dict = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return [_Chunk(content='{"a":1}'),
                    _Chunk(no_choices=True,
                           usage={"prompt_cache_hit_tokens": 64,
                                  "prompt_cache_miss_tokens": 8,
                                  "completion_tokens": 3})]

    backend = OpenAICompatBackend()
    monkeypatch.setattr(type(backend), "client",
                        property(lambda self: type("C", (), {
                            "chat": type("Ch", (), {"completions": FakeCompletions()})()})()))
    new_usage_stats()
    got = []
    out = backend._complete([{"role": "system", "content": "s"}], 0.0,
                            on_delta=lambda t, k: got.append((t, k)))
    assert captured["stream"] is True
    assert captured["stream_options"] == {"include_usage": True}
    assert out == '{"a":1}'
    assert got == [('{"a":1}', "content")]
    # usage 真的记下来了（不是 0，也不是 None）
    assert current_usage_stats()["prompt_cache_hit_tokens"] == 64


def test_the_returned_text_excludes_reasoning(monkeypatch):
    """返回值要喂给 `model_validate_json`，思考过程拼进去就没有一次能过校验。"""
    class FakeCompletions:
        def create(self, **kwargs):
            return [_Chunk(reasoning="想一想"), _Chunk(content='{"a":'),
                    _Chunk(content="1}")]

    backend = OpenAICompatBackend()
    monkeypatch.setattr(type(backend), "client",
                        property(lambda self: type("C", (), {
                            "chat": type("Ch", (), {"completions": FakeCompletions()})()})()))
    new_usage_stats()
    seen = []
    out = backend._complete([{"role": "system", "content": "s"}], 0.0,
                            on_delta=lambda t, k: seen.append(k))
    assert out == '{"a":1}'
    assert seen == ["reasoning", "content", "content"]


def test_a_non_streaming_backend_never_receives_on_delta():
    """`SUPPORTS_STREAMING=False` 的后端连这个关键字都不该收到——收到了要么
    TypeError，要么被 `**kwargs` 静默吞掉（后者更糟：前端永远等不到增量而
    没人知道为什么）。"""
    class NotStreaming(StreamingFake):
        SUPPORTS_STREAMING = False

    backend = NotStreaming({"S1Normalize": _s1_json()})
    backend.generate(system="s", user="", schema=S1Normalize,
                     on_delta=lambda t, k: pytest.fail("不该被调用"))
    assert backend.saw_on_delta == [False]


def test_a_streaming_backend_does_receive_it(streaming_llm):
    got: list[tuple[str, str]] = []
    streaming_llm.generate(system="s", user="", schema=S1Normalize,
                           on_delta=lambda t, k: got.append((t, k)))
    assert streaming_llm.saw_on_delta == [True]
    assert "".join(t for t, _ in got) == _s1_json()


# ---------- 三、streaming_note：三种"没流式"要分得开 ----------

def test_streaming_note_distinguishes_three_cases():
    from core.llm_replay import ReplayBackend

    class NotStreaming(StreamingFake):
        SUPPORTS_STREAMING = False

    assert OpenAICompatBackend().streaming_note() is None
    assert "不支持流式" in NotStreaming({}).streaming_note()
    assert "模拟" in ReplayBackend().streaming_note()


# ---------- 四、S3DeltaEmitter：合并、计数、收尾 ----------

def _collect_emitter():
    events: list[tuple[str, dict]] = []
    em = chain.S3DeltaEmitter(lambda n, d: events.append((n, d)), "synthesis", "五家综合")
    return em, events


def test_small_pieces_are_coalesced_into_fewer_events():
    """一个 token 一帧的话一次 S3 几千帧 SSE。判据：攒够 80 字才发。"""
    em, events = _collect_emitter()
    em._last_flush["content"] = 1e18   # 掐掉时间那一路，只看字数判据
    for _ in range(10):
        em("十个字十个字", "content")
    assert len(events) < 10, "没有合并"
    em.flush()
    assert "".join(d["text"] for _, d in events) == "十个字十个字" * 10


def test_flush_emits_the_tail():
    em, events = _collect_emitter()
    em._last_flush["content"] = 1e18
    em("零星几个字", "content")
    assert events == []
    em.flush()
    assert len(events) == 1 and events[0][1]["text"] == "零星几个字"


def test_reasoning_and_content_never_mix():
    em, events = _collect_emitter()
    em._last_flush["content"] = em._last_flush["reasoning"] = 1e18
    em("正文", "content")
    em("思考", "reasoning")
    em.flush()
    by_kind = {d["kind"]: d["text"] for _, d in events}
    assert by_kind == {"content": "正文", "reasoning": "思考"}


def test_counters_and_first_delta_are_recorded():
    em, _ = _collect_emitter()
    assert em.summary()["first_delta_s"] is None
    em("abc", "content")
    em("de", "reasoning")
    em.flush()
    s = em.summary()
    assert s["chars_content"] == 3 and s["chars_reasoning"] == 2
    assert s["first_delta_s"] is not None and s["events"] >= 1


def test_an_unknown_kind_is_counted_not_dropped():
    """认不出的种类当正文处理并计数——丢掉的表现是"前端少了一段"，
    而那时没人知道少了什么。"""
    em, events = _collect_emitter()
    em("怪东西", "sideband")
    em.flush()
    assert em.summary()["chars_content"] == 3
    assert events and events[0][1]["kind"] == "content"


def test_no_events_when_there_is_no_callback():
    em = chain.S3DeltaEmitter(None, "synthesis", "五家综合")
    em("x", "content")
    em.flush()
    # 计数照样是真的：CLI / eval 那条路上 s3_done 里的数不能因为没人听就变空
    assert em.summary()["chars_content"] == 1


# ---------- 五、链路契约：s3_start → n×s3_delta → s3_done ----------

def _run_chain(monkeypatch, llm, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    case = _case(case_id="ye_tianshi-001", syndrome="脾胃气虚", herbs=["党参", "白术"])
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever([case]))
    events: list[tuple[str, dict]] = []
    out = chain.consult("纳差乏力", on_step=lambda n, d: events.append((n, d)))
    return out, events


def test_the_event_contract_is_start_then_deltas_then_done(monkeypatch, streaming_llm):
    """**契约是"每位医家自己的子序列"**，不是全局序列：legacy 下几位医家并发跑，
    甲的 s3_done 完全可能排在乙的 s3_start 后面（同 tests/test_stream_events.py
    那条"事件会交错、结果不会"）。"""
    out, events = _run_chain(monkeypatch, streaming_llm)
    pids = {d.get("physician") for n, d in events if n == "s3_start"}
    assert pids, "一个 s3_start 都没有"
    for pid in pids:
        names = [n for n, d in events
                 if n in ("s3_start", "s3_delta", "s3_done") and d.get("physician") == pid]
        assert names[0] == "s3_start" and names[-1] == "s3_done"
        assert names.count("s3_delta") >= 1
        assert set(names[1:-1]) == {"s3_delta"}, f"{pid}：start 与 done 之间只该有增量"
    assert out["results"], "链路本身要跑通"


def test_the_deltas_reassemble_into_exactly_the_model_output(monkeypatch, streaming_llm):
    """合并不许丢字。**这条是合并逻辑唯一真正要紧的性质**——少一段的表现是
    前端拼出来的 JSON 解析失败，而那时人会去查前端。"""
    _out, events = _run_chain(monkeypatch, streaming_llm)
    pids = {d.get("physician") for n, d in events if n == "s3_delta"}
    assert pids
    for pid in pids:
        text = "".join(d["text"] for n, d in events
                       if n == "s3_delta" and d["kind"] == "content" and d["physician"] == pid)
        assert text == _s3_json(), pid


def test_s3_done_carries_the_counts(monkeypatch, streaming_llm):
    _out, events = _run_chain(monkeypatch, streaming_llm)
    for done in [d for n, d in events if n == "s3_done"]:
        assert done["events"] >= 1
        assert done["chars_content"] == len(_s3_json())
        assert done["first_delta_s"] is not None
        assert done["streaming_note"] is None, "真流式时不该带「为什么没流式」的说明"


def test_the_manifest_reports_streaming_honestly(monkeypatch, streaming_llm):
    out, _events = _run_chain(monkeypatch, streaming_llm)
    st = out["manifest"]["streaming"]
    assert st["streamed"] is True
    assert st["n_streamed"] == st["n_total"] >= 1
    assert st["chars_content"] > 0
    assert st["first_delta_s_max"] is not None
    assert st["notes"] == []


def test_a_non_streaming_backend_is_reported_as_such(monkeypatch):
    class NotStreaming(StreamingFake):
        SUPPORTS_STREAMING = False

    llm = NotStreaming({"S1Normalize": _s1_json(), "S2Elements": _s2_json(),
                        "S3Syndrome": _s3_json(), "S3SyndromeUnreferenced": _s3_json()})
    out, events = _run_chain(monkeypatch, llm)
    assert not [n for n, _ in events if n == "s3_delta"]
    st = out["manifest"]["streaming"]
    assert st["streamed"] is False and st["events"] == 0
    assert any("不支持流式" in n for n in st["notes"])


def test_best_of_n_above_one_says_why_it_did_not_stream(monkeypatch, streaming_llm):
    """N>1 时几路同时在飞，增量混在一条流里没法用。**这条路不流式，但要说出原因**。"""
    out, events = _run_chain(monkeypatch, streaming_llm, S3_BEST_OF_N="2")
    assert not [n for n, _ in events if n == "s3_delta"]
    notes = out["manifest"]["streaming"]["notes"]
    assert any("best-of-N" in n for n in notes), notes


# ---------- 六、SSE：心跳 ----------

def test_heartbeat_interval_is_below_the_tightest_proxy_default():
    """nginx 的 proxy_read_timeout 默认 60 秒，多数反代/CDN 在 30~120 秒之间掐
    空闲连接。心跳必须明显小于最紧的那个默认值，否则等于没有。"""
    assert 5.0 <= api_main._HEARTBEAT_SECONDS <= 15.0


def test_a_slow_worker_gets_heartbeats(monkeypatch):
    """S3 那一步要等几十秒、期间一个字节都不发——被掐掉的表现是流突然结束、
    没有 error 也没有 done，前端只能转圈到底。"""
    import time as _time

    monkeypatch.setattr(api_main, "_HEARTBEAT_SECONDS", 0.05)

    def slow_consult(complaint, ask_fn=None, on_step=None, **kw):
        _time.sleep(0.35)
        on_step("s1_done", {"symptoms": ["纳差"]})
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", slow_consult)
    client = TestClient(api_main.app)
    out_q: queue.Queue = queue.Queue()
    with client.stream("POST", "/api/consult/stream", json={"complaint": "纳差"}) as resp:
        for line in resp.iter_lines():
            out_q.put(line)
    lines = list(out_q.queue)
    beats = [x for x in lines if x == "event: heartbeat"]
    assert beats, "慢 worker 期间一个心跳都没发"
    # 心跳带着已等待秒数：界面要能说"已等待 42 秒"，不是只闷着转圈
    idx = lines.index("event: heartbeat")
    payload = json.loads(lines[idx + 1][len("data: "):])
    assert payload["elapsed_s"] >= 0


def test_heartbeats_stop_once_events_flow(monkeypatch):
    """心跳只在**真的没别的东西可发**的时候发，不是定时汇报。"""
    monkeypatch.setattr(api_main, "_HEARTBEAT_SECONDS", 10.0)

    def fast_consult(complaint, ask_fn=None, on_step=None, **kw):
        on_step("s1_done", {"symptoms": ["纳差"]})
        return _fake_outcome()

    monkeypatch.setattr(api_main, "consult", fast_consult)
    client = TestClient(api_main.app)
    with client.stream("POST", "/api/consult/stream", json={"complaint": "纳差"}) as resp:
        names = [x for x in resp.iter_lines() if x.startswith("event: ")]
    assert "event: heartbeat" not in names
