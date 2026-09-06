"""core/llm.py 后端层的离线测试：LLM_MODE 分派、共享重试、ClaudeCLIBackend
的参数拼装与错误路径。全部不真调 CLI、不联网——subprocess 用 monkeypatch 挡掉。
"""
import json
import subprocess

import pytest
from pydantic import BaseModel, Field

from core import llm as llm_mod
from core.llm import (
    ClaudeCLIBackend,
    LLMBackend,
    LLMError,
    OpenAICompatBackend,
    VLLMBackend,
    get_backend,
    get_llm,
    reset_llm_singleton,
)


class Tiny(BaseModel):
    ok: bool
    note: str = Field(min_length=1)


class ScriptedBackend(LLMBackend):
    """按预设脚本依次返回原始文本的假后端，用来测基类的重试语义
    （不测某个具体厂商的实现）。"""

    def __init__(self, raws: list[str]):
        self.raws = raws
        self.calls: list[list[dict]] = []

    def model_name(self) -> str:
        return "scripted"

    def backend_id(self) -> str:
        return "scripted"

    def _complete(self, messages, temperature, **kwargs) -> str:
        # 深拷一份：generate 会往同一个 list 里 append，不拷的话历史会被后续修改覆盖
        self.calls.append([dict(m) for m in messages])
        return self.raws[len(self.calls) - 1]


# ---------- LLM_MODE 分派 ----------

def test_get_backend_defaults_to_openai_compat(monkeypatch):
    monkeypatch.delenv("LLM_MODE", raising=False)
    assert isinstance(get_backend(), OpenAICompatBackend)


def test_get_backend_claude_cli(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "claude_cli")
    assert isinstance(get_backend(), ClaudeCLIBackend)


def test_get_backend_local(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "local")
    assert isinstance(get_backend(), VLLMBackend)


def test_get_backend_unknown_mode_falls_back_to_api(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "什么鬼模式")
    assert isinstance(get_backend(), OpenAICompatBackend)


def test_reset_singleton_lets_mode_switch_take_effect(monkeypatch):
    """LLM_MODE 是进程级变量，不清单例的话切换不生效——这正是切后端时最容易
    踩的坑，所以要有测试守着。"""
    monkeypatch.setenv("LLM_MODE", "api")
    reset_llm_singleton()
    assert isinstance(get_llm(), OpenAICompatBackend)

    monkeypatch.setenv("LLM_MODE", "claude_cli")
    assert isinstance(get_llm(), OpenAICompatBackend)  # 单例还在，切换不生效

    reset_llm_singleton()
    assert isinstance(get_llm(), ClaudeCLIBackend)
    reset_llm_singleton()


# ---------- manifest 用的三个元数据方法 ----------

def test_openai_backend_reports_real_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    b = OpenAICompatBackend()
    assert b.model_name() == "deepseek-chat"
    assert b.backend_id() == "api"
    # 默认后端不带警告，否则每份正常报告都会挂一条噪音
    assert b.comparability_warning() is None


def test_claude_cli_backend_reports_claude_not_deepseek(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MODEL", raising=False)
    b = ClaudeCLIBackend()
    assert b.model_name() == "claude-sonnet-5"
    assert b.backend_id() == "claude_cli"
    assert "deepseek" not in b.model_name().lower()


def test_claude_cli_model_overridable(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "claude-opus-5")
    assert ClaudeCLIBackend().model_name() == "claude-opus-5"


def test_claude_cli_warning_names_backend_and_says_not_comparable(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MODEL", raising=False)
    w = ClaudeCLIBackend().comparability_warning()
    assert w is not None
    assert "claude_cli" in w
    assert "不可与" in w and "DeepSeek" in w


def test_vllm_backend_metadata(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_PATH", raising=False)
    b = VLLMBackend()
    assert b.backend_id() == "local"
    assert b.model_name() == "vllm-unconfigured"
    assert b.comparability_warning() is not None


def test_vllm_complete_still_not_implemented():
    with pytest.raises(NotImplementedError):
        VLLMBackend()._complete([{"role": "user", "content": "x"}], 0.0)


# ---------- 超时配置 ----------

def test_claude_cli_default_timeout(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_TIMEOUT", raising=False)
    assert ClaudeCLIBackend().timeout == 180


def test_claude_cli_timeout_env_override(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_TIMEOUT", "45")
    assert ClaudeCLIBackend().timeout == 45


def test_claude_cli_timeout_actually_passed_to_subprocess(monkeypatch):
    """光测属性不够——要确认这个值真的传进了 subprocess.run，
    否则改了配置也不生效。"""
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"is_error": False, "result": '{"ok":true,"note":"x"}'}), stderr=""
        )

    monkeypatch.setenv("CLAUDE_CLI_TIMEOUT", "77")
    monkeypatch.setattr(subprocess, "run", fake_run)
    ClaudeCLIBackend()._complete([{"role": "user", "content": "hi"}], 0.0)
    assert seen["timeout"] == 77


# ---------- CLI 参数拼装 ----------

def test_build_command_is_pure_completion(monkeypatch):
    monkeypatch.delenv("CLAUDE_CLI_MODEL", raising=False)
    cmd = ClaudeCLIBackend().build_command()
    assert cmd[:2] == ["claude", "-p"]
    assert "--output-format" in cmd and "json" in cmd
    # 纯补全的三个关键开关，少一个就会退化成"跑一个完整 agent"，慢 7 倍
    assert "--strict-mcp-config" in cmd
    assert "--no-session-persistence" in cmd
    assert "--system-prompt" in cmd
    # 工具必须全禁：留着 Read/Glob 模型可能真去读文件，那就不是纯补全了
    assert "--disallowedTools" in cmd
    for tool in ("Bash", "Read", "Write", "WebFetch", "Task"):
        assert tool in cmd


def test_build_command_uses_configured_model(monkeypatch):
    monkeypatch.setenv("CLAUDE_CLI_MODEL", "claude-haiku-4-5-20251001")
    cmd = ClaudeCLIBackend().build_command()
    assert "claude-haiku-4-5-20251001" in cmd


# ---------- 多轮消息压平（重试回灌靠它） ----------

def test_flatten_keeps_retry_feedback():
    """把 assistant/user 的重试对压掉，错误回灌就没了，重试等于白重试。"""
    msgs = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "第一次请求"},
        {"role": "assistant", "content": '{"bad":1}'},
        {"role": "user", "content": "上一次输出未通过校验，错误信息：xxx"},
    ]
    flat = ClaudeCLIBackend.flatten_messages(msgs)
    assert "系统提示" in flat
    assert '{"bad":1}' in flat
    assert "上一次输出未通过校验" in flat
    assert "[你上一次的输出]" in flat


def test_flatten_skips_empty_content():
    flat = ClaudeCLIBackend.flatten_messages(
        [{"role": "system", "content": "S"}, {"role": "user", "content": ""}]
    )
    assert flat == "S"


# ---------- 共享重试语义（基类，不是某个后端各写一套） ----------

def test_generate_succeeds_first_try():
    b = ScriptedBackend(['{"ok":true,"note":"good"}'])
    out = b.generate("sys", "usr", Tiny)
    assert out.ok is True
    assert len(b.calls) == 1


def test_generate_strips_markdown_fence():
    b = ScriptedBackend(['```json\n{"ok":true,"note":"fenced"}\n```'])
    assert b.generate("sys", "usr", Tiny).note == "fenced"


def test_generate_retries_and_feeds_error_back():
    """第一次字段错，第二次修对——这正是换模型时实测踩到的情形
    （模型把 element 写成 name），必须靠回灌纠正。"""
    b = ScriptedBackend(['{"ok":true}', '{"ok":true,"note":"fixed"}'])
    out = b.generate("sys", "usr", Tiny)
    assert out.note == "fixed"
    assert len(b.calls) == 2
    # 第二次的消息里必须带着上次的原始输出和校验错误
    second = b.calls[1]
    assert any(m["role"] == "assistant" and m["content"] == '{"ok":true}' for m in second)
    assert any("上一次输出未通过校验" in m["content"] for m in second if m["role"] == "user")


def test_generate_gives_up_after_three_attempts():
    b = ScriptedBackend(['{"bad":1}', '{"bad":2}', '{"bad":3}'])
    with pytest.raises(LLMError) as ei:
        b.generate("sys", "usr", Tiny)
    msg = str(ei.value)
    assert len(b.calls) == 3
    # 报错要能定位问题：后端、模型、schema、最后原始返回都在
    assert "backend=scripted" in msg
    assert "model=scripted" in msg
    assert "schema=Tiny" in msg
    assert "bad" in msg


def test_generate_injects_schema_into_system():
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate("我的业务提示词", "usr", Tiny)
    system_msg = b.calls[0][0]["content"]
    assert "我的业务提示词" in system_msg
    assert "JSON Schema" in system_msg


# ---------- CLI 错误路径（规则 6/7：不吞异常，完整带出上下文） ----------

def _fake_run_factory(returncode=0, stdout="", stderr=""):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
    return fake_run


def test_cli_nonzero_exit_raises_with_full_streams(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_run_factory(1, stdout="部分输出", stderr="真实报错信息")
    )
    with pytest.raises(LLMError) as ei:
        ClaudeCLIBackend()._complete([{"role": "user", "content": "x"}], 0.0)
    msg = str(ei.value)
    assert "退出码 1" in msg
    assert "部分输出" in msg
    assert "真实报错信息" in msg


def test_cli_unparseable_output_raises_with_raw(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(0, stdout="这不是 JSON"))
    with pytest.raises(LLMError) as ei:
        ClaudeCLIBackend()._complete([{"role": "user", "content": "x"}], 0.0)
    assert "无法解析" in str(ei.value)
    assert "这不是 JSON" in str(ei.value)


def test_cli_is_error_true_raises(monkeypatch):
    payload = json.dumps(
        {"is_error": True, "subtype": "error_max_turns", "api_error_status": None, "result": ""}
    )
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(0, stdout=payload))
    with pytest.raises(LLMError) as ei:
        ClaudeCLIBackend()._complete([{"role": "user", "content": "x"}], 0.0)
    assert "is_error=true" in str(ei.value)
    assert "error_max_turns" in str(ei.value)


def test_cli_missing_result_field_raises(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_run_factory(0, stdout=json.dumps({"is_error": False}))
    )
    with pytest.raises(LLMError) as ei:
        ClaudeCLIBackend()._complete([{"role": "user", "content": "x"}], 0.0)
    assert "没有可用的 result" in str(ei.value)


def test_cli_happy_path_returns_result_text(monkeypatch):
    payload = json.dumps({"is_error": False, "result": '{"ok":true,"note":"从 CLI 来"}'})
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(0, stdout=payload))
    out = ClaudeCLIBackend().generate("sys", "usr", Tiny)
    assert out.note == "从 CLI 来"
