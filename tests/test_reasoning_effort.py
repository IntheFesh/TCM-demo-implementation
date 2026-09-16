"""R22：`reasoning_effort` 那条链——从配置到 max_tokens 档位到 manifest。

配置本身怎么取默认值在 `tests/test_thinking_control.py`（那里是 thinking/effort
同族旋钮的家）。这个文件测的是**effort 传下去之后的后果**：
  · `effort=max` 那一档的 max_tokens 必须更高（32768 装不下 max 档的推理过程）；
  · 云端后端真的把它发出去了；
  · 本地后端也按同一档选上限（否则同一段代码在两个后端上会在不同长度处被截断）；
  · manifest 如实记它，而且**关了思考时是 None**——记一个不生效的实验条件比不记更糟。
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from core.llm import (
    DEFAULT_MAX_TOKENS,
    MAX_EFFORT_MAX_TOKENS,
    REASONING_EFFORTS,
    THINKING_MAX_TOKENS,
    LLMBackend,
    OpenAICompatBackend,
)


class Tiny(BaseModel):
    ok: bool


class _Bare(LLMBackend):
    def model_name(self):
        return "deepseek-v4-pro"

    def backend_id(self):
        return "fake"

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kw):
        return '{"ok": true}'


def test_max_effort_gets_its_own_higher_ceiling():
    """三档，从低到高：关思考 → 开思考 → effort=max。

    `effort=max` 单独一档的理由：max_tokens 盖住的是 reasoning + 可见输出两部分，
    而 max 档的推理本身就能吃掉几万 token。沿用 32768 的后果是**推理没写完就撞
    上限**，表现为一个在末尾处解析失败的 JSON（白烧一次调用才看出来）。
    """
    backend = _Bare()
    assert backend._default_max_tokens("disabled") == DEFAULT_MAX_TOKENS
    assert backend._default_max_tokens("enabled") == THINKING_MAX_TOKENS
    assert backend._default_max_tokens("enabled", "max") == MAX_EFFORT_MAX_TOKENS
    assert DEFAULT_MAX_TOKENS < THINKING_MAX_TOKENS < MAX_EFFORT_MAX_TOKENS


def test_the_lower_efforts_stay_on_the_thinking_tier():
    """low/medium/high 不各给一档：这三档的推理量级没有差到需要不同上限，
    分档只在"会不会撞上限"这一件事上有意义，不是为了好看而分。"""
    backend = _Bare()
    for effort in ("low", "medium", "high"):
        assert backend._default_max_tokens("enabled", effort) == THINKING_MAX_TOKENS


def test_effort_without_thinking_does_not_raise_the_ceiling():
    """关了思考时 effort 不生效（`thinking_for` 那时也不会传它），
    所以即使传进来也不该抬高上限——否则一个不生效的参数会悄悄改掉预算。"""
    assert _Bare()._default_max_tokens("disabled", "max") == DEFAULT_MAX_TOKENS


def test_llm_max_tokens_env_still_wins_over_every_tier(monkeypatch):
    """`LLM_MAX_TOKENS` 是人为覆盖，压过所有档位——R22 加档不改这条既有契约。"""
    monkeypatch.setenv("LLM_MAX_TOKENS", "777")
    assert _Bare()._default_max_tokens("enabled", "max") == 777


def test_the_cloud_backend_sends_effort_and_uses_its_ceiling(monkeypatch):
    """effort 要真的发出去（`reasoning_effort` 是顶层参数，不是 extra_body），
    并且这次调用的 max_tokens 走 max 档。"""
    captured: dict = {}

    class _Resp:
        class _Choice:
            class _Msg:
                content = '{"ok": true}'
            message = _Msg()
        choices = [_Choice()]
        usage = None

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    backend = OpenAICompatBackend()
    monkeypatch.setattr(OpenAICompatBackend, "client",
                        property(lambda self: _Client()), raising=False)
    monkeypatch.setattr(backend, "_request_model_name", lambda: "deepseek-v4-pro")
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)

    backend._complete([{"role": "system", "content": "x"}], 0.0,
                      thinking="enabled", reasoning_effort="max")
    assert captured["reasoning_effort"] == "max"
    assert captured["max_tokens"] == MAX_EFFORT_MAX_TOKENS
    # 思考模式下 temperature 不生效，所以不传（R12 既有契约，这里顺带钉住没被改坏）
    assert "temperature" not in captured


def test_the_local_backend_picks_the_same_tier():
    """本地后端没有 effort 这个开关，但**上限档位要跟着走**：不跟的话，
    同一段代码在云端和本地会在不同长度处被截断，而那种差异极难归因。
    判据直接问 `_sampling_params` 的签名——vllm 装不上的机器也能跑这条。"""
    import inspect

    from core.llm import VLLMInProcessBackend

    sig = inspect.signature(VLLMInProcessBackend._sampling_params)
    assert "reasoning_effort" in sig.parameters


def test_the_truncation_error_names_the_effort_it_used():
    """撞上限的报错里要能看出走的是哪一档——三档之间差 8 倍，
    只说"未设置（走后端默认值）"定位不到。"""
    from core.llm import LLMTruncatedError

    class Truncating(_Bare):
        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kw):
            return '{"ok":true,"note":"' + "医" * 400

    with pytest.raises(LLMTruncatedError) as e:
        Truncating().generate(system="s", user="u", schema=Tiny,
                              thinking="enabled", reasoning_effort="max")
    msg = str(e.value)
    assert "reasoning_effort=max" in msg
    assert str(MAX_EFFORT_MAX_TOKENS) in msg


def test_every_effort_is_one_of_the_four_official_tiers():
    """四档是官方的取值集合。写成常量供别处引用，别处就不会各写一份字面量。"""
    assert REASONING_EFFORTS == ("low", "medium", "high", "max")
