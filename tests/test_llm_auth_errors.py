"""401/402/422 不重试——文档说这三个是确定性的，重试只是把一次失败变成三次。"""
from __future__ import annotations

import pytest

from core.llm import LLMAuthError, OpenAICompatBackend
from core.schemas import S1Normalize


class _Boom(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


@pytest.mark.parametrize("status", [400, 401, 402, 422])
def test_deterministic_failures_are_not_retried(monkeypatch, status):
    calls = []
    b = OpenAICompatBackend()
    monkeypatch.setattr(b, "_complete_within_deadline",
                        lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(_Boom(status)))[0])
    monkeypatch.setattr(b, "_sleep", lambda s: None)
    with pytest.raises(LLMAuthError) as ei:
        b.generate(system="x", user="", schema=S1Normalize)
    assert len(calls) == 1, f"HTTP {status} 被重试了 {len(calls)} 次"
    assert ei.value.status_code == status


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_failures_are_still_retried(monkeypatch, status):
    calls = []
    b = OpenAICompatBackend()
    monkeypatch.setattr(b, "_complete_within_deadline",
                        lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(_Boom(status)))[0])
    monkeypatch.setattr(b, "_sleep", lambda s: None)
    with pytest.raises(Exception):
        b.generate(system="x", user="", schema=S1Normalize)
    assert len(calls) == b.MAX_ATTEMPTS


def test_the_message_tells_the_visitor_which_problem_it_is(monkeypatch):
    b = OpenAICompatBackend()
    monkeypatch.setattr(b, "_complete_within_deadline",
                        lambda *a, **k: (_ for _ in ()).throw(_Boom(402)))
    monkeypatch.setattr(b, "_sleep", lambda s: None)
    with pytest.raises(LLMAuthError) as ei:
        b.generate(system="x", user="", schema=S1Normalize)
    assert "余额不足" in str(ei.value)
