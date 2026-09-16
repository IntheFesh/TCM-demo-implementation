"""R21：前缀缓存的可观测性。

命中率不是估的——响应 `usage` 里有 `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens`（官方文档 https://api-docs.deepseek.com/guides/kv_cache/）。
这里测的是"取到了、累加对了、算出来的率对了、非 DeepSeek 后端如实为 None"。
"""
from __future__ import annotations

import pytest

from core.llm import (
    CACHE_HIT_FIELD,
    CACHE_MISS_FIELD,
    current_usage_stats,
    new_usage_stats,
    record_usage,
)


@pytest.fixture(autouse=True)
def _fresh_stats():
    new_usage_stats()
    yield


class _Usage:
    """SDK 那种属性对象，不是 dict——两种形状都要认。"""

    def __init__(self, hit, miss, completion=0, reasoning=None):
        setattr(self, CACHE_HIT_FIELD, hit)
        setattr(self, CACHE_MISS_FIELD, miss)
        self.completion_tokens = completion
        if reasoning is not None:
            self.completion_tokens_details = {"reasoning_tokens": reasoning}


def test_usage_fields_are_read_from_a_dict():
    record_usage({CACHE_HIT_FIELD: 900, CACHE_MISS_FIELD: 100, "completion_tokens": 30})
    stats = current_usage_stats()
    assert stats[CACHE_HIT_FIELD] == 900
    assert stats[CACHE_MISS_FIELD] == 100
    assert stats["completion_tokens"] == 30
    assert stats["n_reported"] == 1


def test_usage_fields_are_read_from_an_sdk_object():
    record_usage(_Usage(500, 500, completion=20, reasoning=1234))
    stats = current_usage_stats()
    assert stats[CACHE_HIT_FIELD] == 500
    assert stats["reasoning_tokens"] == 1234


def test_usage_accumulates_across_calls():
    """一次问诊有 5~11 次调用，manifest 要的是整次的命中率。只留最后一次的话，
    S1/S2 那几次短调用会把 S3 那次大命中盖掉。"""
    record_usage({CACHE_HIT_FIELD: 0, CACHE_MISS_FIELD: 1000})
    record_usage({CACHE_HIT_FIELD: 900, CACHE_MISS_FIELD: 100})
    stats = current_usage_stats()
    assert stats[CACHE_HIT_FIELD] == 900
    assert stats[CACHE_MISS_FIELD] == 1100
    assert stats["n_reported"] == 2


def test_a_backend_that_does_not_report_cache_fields_counts_as_not_reported():
    """非 DeepSeek 后端的 usage 里没有这两个键。不能因为它有 completion_tokens
    就把它算成"报了缓存数"。"""
    record_usage({"completion_tokens": 42, "prompt_tokens": 100})
    assert current_usage_stats() is None


def test_all_zero_is_not_the_same_as_not_reported():
    """全 0 会被读成"跑了但一次没命中"，而真相可能是"这个后端不报这个数"。"""
    assert current_usage_stats() is None  # 一次都没报
    record_usage({CACHE_HIT_FIELD: 0, CACHE_MISS_FIELD: 0})
    stats = current_usage_stats()
    assert stats is not None and stats["n_reported"] == 1


def test_no_stats_context_is_a_noop():
    """离线脚本直接调 run_physician 时没开统计——record_usage 不该抛。"""
    import core.llm as llm_mod

    llm_mod._usage_stats.set(None)
    record_usage({CACHE_HIT_FIELD: 1, CACHE_MISS_FIELD: 1})
    assert current_usage_stats() is None


# ---------- manifest ----------

def test_manifest_has_the_four_cache_keys_and_computes_the_ratio():
    from core.chain import _cache_usage_fields

    record_usage({CACHE_HIT_FIELD: 900, CACHE_MISS_FIELD: 100, "completion_tokens": 10})
    got = _cache_usage_fields()
    assert got["cache_hit_tokens"] == 900
    assert got["cache_miss_tokens"] == 100
    assert got["cache_hit_ratio"] == 0.9
    assert "reasoning_tokens" in got


def test_manifest_ratio_is_none_when_nothing_was_reported():
    """0.0 会被读成"跑了但一次没命中"，而实际是"这个后端不报这个数"。"""
    from core.chain import _cache_usage_fields

    got = _cache_usage_fields()
    assert got == {"cache_hit_tokens": None, "cache_miss_tokens": None,
                   "cache_hit_ratio": None, "reasoning_tokens": None}


def test_manifest_carries_retriever_mode_and_prefix_tokens(monkeypatch):
    from core import chain

    m = chain._build_manifest(123, 5, False, retriever_mode="hybrid")
    assert m["retriever_mode"] == "hybrid"
    # top3 系下前缀那几段根本不存在 → None 而不是 0
    assert m["prefix_tokens_by_section"] is None
    for k in ("cache_hit_tokens", "cache_miss_tokens", "cache_hit_ratio"):
        assert k in m


def test_manifest_warns_when_the_retriever_mode_is_not_the_default():
    from core import chain

    m = chain._build_manifest(1, 1, False, retriever_mode="bm25")
    assert "不可比" in (m["comparability_warning"] or "")
    assert "RETRIEVER_MODE=bm25" in (m["comparability_warning"] or "")


def test_prefix_tokens_report_does_not_take_down_the_consult(monkeypatch):
    """统计项取不到（没有 cases.json）时报一个 error 字段，不抛——
    manifest 不该因为一个统计项让整次问诊失败。"""
    from core import chain

    m = chain._build_manifest(1, 1, False, retriever_mode="full_context")
    got = m["prefix_tokens_by_section"]
    assert got is not None
    assert isinstance(got, dict)
    # 沙盒里没有 cases.json → 要么是 error，要么是各段读数；两种都不许是 None
    assert got


# ---------- 真实响应路径 ----------

def test_openai_compat_backend_records_usage_from_the_response(monkeypatch):
    """`record_usage` 就在 `_complete` 里调——一次 generate 可能重试多次，
    每次请求都有自己的 usage，漏掉重试那几次会让命中率偏高。"""
    from core.llm import OpenAICompatBackend

    class _Msg:
        content = '{"ok": 1}'

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]
        usage = _Usage(640, 64, completion=8)

    fake_client = type("C", (), {"chat": type("Ch", (), {"completions": type(
        "Co", (), {"create": staticmethod(lambda **kw: _Resp())})()})()})()
    backend = OpenAICompatBackend.__new__(OpenAICompatBackend)
    # `client` 是 property（惰性建 SDK 客户端），只能在类上打桩
    monkeypatch.setattr(OpenAICompatBackend, "client",
                        property(lambda self: fake_client), raising=False)
    monkeypatch.setattr(backend, "_request_model_name", lambda: "deepseek-v4-pro")
    # R22 起这个方法多收一个 reasoning_effort（max 档要更大的 max_tokens），
    # 替身用 *args 接住：这条测的是 usage 有没有被记下来，不是 max_tokens 算得对不对。
    monkeypatch.setattr(backend, "_default_max_tokens", lambda *args: 1024)
    out = backend._complete([{"role": "system", "content": "x"}], 0.0)
    assert out == '{"ok": 1}'
    stats = current_usage_stats()
    assert stats[CACHE_HIT_FIELD] == 640 and stats[CACHE_MISS_FIELD] == 64


def test_the_worker_thread_sees_the_callers_context():
    """R21 实测的坑：`_complete` 跑在 `_complete_within_deadline` 起的工作线程里，
    而 `threading.Thread` 起的线程拿到的是一份**空** Context——调用方设的
    ContextVar 在里面看不见，于是 manifest 里的 cache_hit_ratio 恒 None，
    而日志里一个错都没有。修法是 `copy_context().run()`。
    """
    from core.llm import LLMBackend

    class _B(LLMBackend):
        def model_name(self):
            return "fake"

        def backend_id(self):
            return "fake"

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kwargs):
            record_usage({CACHE_HIT_FIELD: 128, CACHE_MISS_FIELD: 64})
            return "{}"

    b = _B()
    b._complete_within_deadline([{"role": "system", "content": "x"}], 0.0, None, None, None, 30.0)
    stats = current_usage_stats()
    assert stats is not None, "工作线程里记的 usage 调用方读不到 = copy_context 丢了"
    assert stats[CACHE_HIT_FIELD] == 128


def test_bench_reports_the_hit_ratio_per_run_and_has_a_gate():
    """逐次列出来而不是只报均值：第一次必然接近 0（冷缓存），跟第二次平均
    一下就看不出"第二次到底命中了没有"。"""
    from scripts.bench_consult import CACHE_HIT_GATE, summarize

    runs = [
        {"ok": True, "elapsed_s": 1.0, "llm_calls": 5, "by_step": {}, "calls": [],
         "usage_available": False, "cache_hit_ratio": 0.01, "retriever_mode": "full_context"},
        {"ok": True, "elapsed_s": 1.0, "llm_calls": 5, "by_step": {}, "calls": [],
         "usage_available": False, "cache_hit_ratio": 0.97, "retriever_mode": "full_context"},
    ]
    s = summarize(runs)
    assert s["cache_hit_ratio_by_run"] == [0.01, 0.97]
    assert s["cache_hit_ratio_last"] == 0.97
    assert CACHE_HIT_GATE == 0.9
    assert s["cache_hit_ratio_last"] >= CACHE_HIT_GATE


def test_the_fake_backend_simulates_the_documented_64_token_blocks():
    """模拟的粒度必须跟官方一致（64 token 一块，不足一块不缓存）——
    模拟一个跟真机不同的粒度就失去了模拟的意义。"""
    from scripts.bench_consult import FAKE_CACHE_BLOCK_TOKENS, build_fake_backend

    assert FAKE_CACHE_BLOCK_TOKENS == 64
    b = build_fake_backend(0.0, simulate_cache=True)
    msgs = [{"role": "system", "content": "脘腹痞满纳谷不香" * 500}]
    b._complete(msgs, 0.0)
    first = current_usage_stats()
    assert first[CACHE_HIT_FIELD] == 0, "第一次是冷缓存"
    b._complete(msgs, 0.0)
    second = current_usage_stats()
    hit_delta = second[CACHE_HIT_FIELD] - first[CACHE_HIT_FIELD]
    assert hit_delta > 0 and hit_delta % FAKE_CACHE_BLOCK_TOKENS == 0
