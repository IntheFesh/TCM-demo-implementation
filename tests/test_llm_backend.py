"""core/llm.py 后端层的离线测试：LLM_MODE 分派、共享重试、ClaudeCLIBackend
的参数拼装与错误路径。全部不真调 CLI、不联网——subprocess 用 monkeypatch 挡掉。
"""
import json
import subprocess

import pytest
from pydantic import BaseModel, Field

from core.llm import (
    ClaudeCLIBackend,
    LLMBackend,
    LLMError,
    LLMTruncatedError,
    OpenAICompatBackend,
    VLLMBackend,
    _looks_like_truncated_json,
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
        self.kwargs_seen: list[dict] = []

    def model_name(self) -> str:
        return "scripted"

    def backend_id(self) -> str:
        return "scripted"

    def _complete(self, messages, temperature, **kwargs) -> str:
        # 深拷一份：generate 会往同一个 list 里 append，不拷的话历史会被后续修改覆盖
        self.calls.append([dict(m) for m in messages])
        self.kwargs_seen.append(dict(kwargs))
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


def test_generate_pins_field_names_in_schema_hint():
    """字段名约束加在 schema hint 里（一处覆盖四个 prompt），不加在各个 yaml 里
    ——yaml 里的手写示例会跟 schemas.py 漂移，schema hint 是自动导出的不会。"""
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate("业务提示词", "usr", Tiny)
    system_msg = b.calls[0][0]["content"]
    assert "字段名必须与上述 schema 完全一致" in system_msg
    # 括号里的例子来自实测失败模式，是文档也是约束，别删
    assert "element" in system_msg and "name" in system_msg


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


# ---------- 审查修复 ----------

def test_transport_errors_retry_without_feeding_back_stale_output():
    """超时/非零退出这类传输错误没有"上一次输出"可回灌——回灌上一轮的陈旧 raw
    或空串只会让模型收到文不对题的纠错指令。"""
    from core.llm import LLMBackend
    from pydantic import BaseModel

    class Out(BaseModel):
        a: int

    class Flaky(LLMBackend):
        def __init__(self):
            self.seen = []

        def model_name(self): return "m"
        def backend_id(self): return "t"

        def _complete(self, messages, temperature, **kw):
            self.seen.append(len(messages))
            if len(self.seen) == 1:
                raise TimeoutError("超时")
            return '{"a": 1}'

    b = Flaky()
    assert b.generate(system="s", user="u", schema=Out).a == 1
    assert b.seen == [2, 2], "传输错误后 messages 不该多出回灌的两条"


def test_validation_errors_still_feed_back():
    from core.llm import LLMBackend
    from pydantic import BaseModel

    class Out(BaseModel):
        a: int

    class Wrong(LLMBackend):
        def __init__(self):
            self.seen = []

        def model_name(self): return "m"
        def backend_id(self): return "t"

        def _complete(self, messages, temperature, **kw):
            self.seen.append(len(messages))
            return '{"a": "x"}' if len(self.seen) == 1 else '{"a": 1}'

    b = Wrong()
    assert b.generate(system="s", user="u", schema=Out).a == 1
    assert b.seen == [2, 4]


@pytest.mark.parametrize("text,expected", [
    ('```JSON\n{"a": 1}\n```', '{"a": 1}'),
    ('好的，结果如下：\n```json\n{"a": 1}\n```\n以上。', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('{"a": 1}', '{"a": 1}'),
])
def test_strip_code_fence_variants(text, expected):
    from core.llm import strip_code_fence

    assert strip_code_fence(text) == expected


def test_render_rejects_missing_placeholders_for_every_prompt():
    """每个 yaml 的占位符集合与调用方传的 kwargs 必须逐一吻合。这里钉住占位符
    集合本身：yaml 新加一个 $var 而调用方没跟上，这条会先红。"""
    import re
    from core.llm import PROMPTS_ROOT, load_prompt

    expected = {
        "s0_extract_case": {"raw_text", "follow_hints"},
        "s1_normalize": {"complaint"},
        "s2_elements": {"elements", "symptoms", "tongue", "pulse"},
        "s3_syndrome": {"name", "elements_summary", "symptoms", "refs"},
        "s3_react": {"name", "symptoms", "elements_summary", "tools", "history", "remaining"},
        "patient_sim": {"profile", "history", "question"},
        "sdt_extract": {"clinical_data"},
        "sdt_select": {"reasoning_block", "clinical_data", "pathogenesis_options", "syndrome_options"},
        "sdt_summary": {"reasoning_block", "clinical_data"},
        "s5_extract_triples": {"raw_text"},
    }
    for path in (PROMPTS_ROOT / "v1").glob("*.yaml"):
        found = set(re.findall(r"(?<!\$)\$\{?([A-Za-z_]\w*)\}?", load_prompt(path.stem)["system"]))
        assert found == expected[path.stem], f"{path.name} 的占位符变了：{found}"


# ---------- M3：s3_syndrome.yaml 的嵌入示例与硬约束 ----------


def _s3_prompt_embedded_example() -> dict:
    """s3_syndrome.yaml 末尾嵌了一段完整的输出示例——schema hint 对嵌套结构
    表达力有限，实测（M1）证实了这一点，示例能显著降低格式错误率。这段示例
    是手写的 JSON，藏在一大段 prose 里，改 prompt 时最容易被顺手改坏（多一个
    逗号、少一个引号）而不会有任何报错提示——除非有测试盯着它。"""
    import json

    from core.llm import load_prompt

    system = load_prompt("s3_syndrome")["system"]
    lines = system.split("\n")
    opening_braces = [i for i, line in enumerate(lines) if line.strip() == "{"]
    assert opening_braces, "s3_syndrome.yaml 里没找到嵌入的 JSON 示例（顶格的 { 都没有）"
    example_text = "\n".join(lines[opening_braces[0]:])
    return json.loads(example_text)


def test_s3_prompt_embedded_example_is_valid_json():
    _s3_prompt_embedded_example()  # 解析失败会直接抛 JSONDecodeError


def test_s3_prompt_embedded_example_validates_against_the_real_schema():
    """不仅要是合法 JSON，还要真的能喂进 core.schemas.S3Syndrome——包括 M1/M2
    加的那些 model_validator（selected 越界检查、base_formula 双向约束）。
    示例本身违反自己教模型遵守的约束，比没有示例更糟。"""
    from core.schemas import S3Syndrome

    obj = _s3_prompt_embedded_example()
    s3 = S3Syndrome.model_validate(obj)
    assert s3.formula == "柴胡疏肝散"  # 对应 selected=0 那个 classic 候选方


def test_s3_prompt_embedded_example_demonstrates_all_three_sources_and_varied_confidence():
    """示例存在的意义是"教会模型怎么填三种来源、置信度不能都填 high"——如果
    示例自己三个都写 classic 或者三个都 high，等于示范了一个错误答案。"""
    obj = _s3_prompt_embedded_example()
    cands = obj["formula_candidates"]
    assert 2 <= len(cands) <= 3
    assert {c["source"] for c in cands} == {"classic", "modified", "composed"}
    assert len({c["confidence"] for c in cands}) > 1, "示例不该三个候选方置信度都一样"
    assert any(c["source"] == "classic" for c in cands)


def test_s3_prompt_embedded_example_includes_reasoning_plain_without_jargon():
    """M6：reasoning_plain 是可选 schema 字段（不逼旧构造点都补），但 prompt
    层面是硬要求——示例本身要示范"这是什么样子"，而且不能自己就带着专业
    术语（反面示范比没有示范更糟）。"""
    obj = _s3_prompt_embedded_example()
    assert obj.get("reasoning_plain"), "示例里 reasoning_plain 不能是空的"
    jargon = ["肝木乘土", "中焦气机", "阴虚阳亢", "横逆犯胃", "疏泄"]
    for term in jargon:
        assert term not in obj["reasoning_plain"], (
            f"reasoning_plain 示例文本里出现了专业术语「{term}」，"
            "这是给患者看的通俗版，不该带这类词"
        )


@pytest.mark.parametrize("phrase", [
    "至少要有一个",  # 至少一个 classic 候选方的硬约束
    "不要三个候选方都填high",
    "剂量不确定时填null，不要猜一个数",
    "这个字段关系到用药安全",  # decoction 字段的安全性说明
    "不能出现",  # reasoning_plain 禁止专业术语那句的开头
])
def test_s3_prompt_contains_the_hard_constraints(phrase):
    """这几句不是随手写的修饰语，是 M3 要解决的具体问题（模型倾向三个都填
    high、role 大量为 null）对应的明文约束——被误删或改写成模糊表述时，
    这条测试要能先红，而不是等真实调用跑出退步的格式遵从度才发现。

    yaml 里的 prose 为了可读性手动折行，逐字匹配会被行内换行拆散（"不要\n
    三个候选方都填 high" 这种），所以先把连续空白（含换行）压成单个空格再比对，
    这样只要语义短语完整、不管折在哪一行都能测到——真正要盯住的是"这句话
    还在不在"，不是"它有没有被折成两行"。
    """
    import re

    from core.llm import load_prompt

    normalized = re.sub(r"\s+", "", load_prompt("s3_syndrome")["system"])
    assert re.sub(r"\s+", "", phrase) in normalized


# ---------- 传输错误重试之间的退避 ----------


class _FlakyThenOk(LLMBackend):
    """前 n_fail 次 _complete 抛传输错误，之后返回合法 JSON。"""

    RETRY_BACKOFF_SECONDS = (1.0, 2.0)  # conftest 把基类清零了，这里显式设回真实值

    def __init__(self, n_fail: int):
        self.n_fail = n_fail
        self.calls = 0
        self.sleeps: list[float] = []
        self._sleep = self.sleeps.append  # 不真睡，只记录

    def model_name(self):
        return "m"

    def backend_id(self):
        return "t"

    def _complete(self, messages, temperature, **kw):
        self.calls += 1
        if self.calls <= self.n_fail:
            raise TimeoutError("超时")
        return '{"ok": true, "note": "x"}'


def test_transport_retry_backs_off_1s_then_2s():
    b = _FlakyThenOk(n_fail=2)
    assert b.generate(system="s", user="u", schema=Tiny).ok is True
    assert b.sleeps == [1.0, 2.0]


def test_no_backoff_after_the_last_attempt():
    """三次全挂：只退避两次（两次尝试之间），最后一次失败后直接抛，不再白等。"""
    b = _FlakyThenOk(n_fail=3)
    with pytest.raises(LLMError):
        b.generate(system="s", user="u", schema=Tiny)
    assert b.sleeps == [1.0, 2.0]


def test_validation_errors_retry_immediately_without_backoff():
    """校验错误是模型格式没对，回灌错误信息立刻重问才有意义，等一秒不会答得更对。"""
    b = ScriptedBackend(['{"ok": true}', '{"ok": true, "note": "x"}'])  # 第一次少 note
    sleeps: list[float] = []
    b._sleep = sleeps.append
    b.RETRY_BACKOFF_SECONDS = (1.0, 2.0)
    assert b.generate(system="s", user="u", schema=Tiny).note == "x"
    assert sleeps == []


# ---------- max_tokens：显式参数，不塞进 **kwargs ----------


def test_generate_passes_max_tokens_to_complete():
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate(system="s", user="u", schema=Tiny, max_tokens=16384)
    assert b.kwargs_seen[0]["max_tokens"] == 16384


def test_generate_defaults_max_tokens_to_none():
    """不传就是 None，各后端自己决定默认值（OpenAICompatBackend 落到环境变量，
    ClaudeCLIBackend 忽略）——generate() 本身不该替后端猜一个数字。"""
    b = ScriptedBackend(['{"ok":true,"note":"n"}'])
    b.generate(system="s", user="u", schema=Tiny)
    assert b.kwargs_seen[0]["max_tokens"] is None


def test_openai_backend_max_tokens_overrides_env_var(monkeypatch):
    """显式传的 max_tokens 要真的传到 SDK 调用里，不是只存在签名上没用上。"""
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    monkeypatch.setenv("LLM_MAX_TOKENS", "8192")
    b = OpenAICompatBackend()
    b._client = FakeClient()
    b._complete([{"role": "user", "content": "x"}], 0.0, max_tokens=16384)
    assert captured["max_tokens"] == 16384


def test_openai_backend_max_tokens_falls_back_to_env_var_when_not_passed(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    monkeypatch.setenv("LLM_MAX_TOKENS", "12000")
    b = OpenAICompatBackend()
    b._client = FakeClient()
    b._complete([{"role": "user", "content": "x"}], 0.0)
    assert captured["max_tokens"] == 12000


def test_claude_cli_complete_accepts_and_ignores_max_tokens(monkeypatch):
    """CLI 没有对应开关（build_command 里确认过没有 token 上限参数），传了也不该
    报错——截断风险交给 generate() 里的 EOF 检测兜底，不是这里的事。"""
    payload = json.dumps({"is_error": False, "result": '{"ok":true,"note":"从 CLI 来"}'})
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(0, stdout=payload))
    out = ClaudeCLIBackend()._complete(
        [{"role": "user", "content": "x"}], 0.0, max_tokens=16384
    )
    assert out == '{"ok":true,"note":"从 CLI 来"}'


# ---------- 输出被截断（撞 max_tokens）：直接失败，不当格式错误重试 ----------


def _truncated_json_for(schema) -> str:
    """构造一个在字符串字段中途被切断的 JSON，模拟真实撞 max_tokens 的输出。"""
    return '{"ok": true, "note": "这段话说到一半被砍掉了，后面还有内容但是没'


def test_looks_like_truncated_json_detects_eof_at_end_of_text():
    from pydantic import ValidationError

    text = _truncated_json_for(Tiny)
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text) is True


def test_looks_like_truncated_json_does_not_flag_mid_text_syntax_errors():
    """缺逗号这类语法错误报在文本中间，不是"读到末尾断了"，不该被当成截断——
    这类错误重试有意义（模型只是格式没对），误判成截断会让本该能修好的输出
    白白被跳过。"""
    from pydantic import ValidationError

    text = '{"ok": true "note": "缺个逗号"}'
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text) is False


def test_looks_like_truncated_json_handles_multiline_output():
    """模型有时会把 JSON 打印成多行（缩进/换行），"末尾"要按最后一行算，
    不能直接拿 len(text) 跟 pydantic 报的 column 比——column 是行内位置，
    多行时那样比较会永远比不上，把真截断当成不是截断。"""
    from pydantic import ValidationError

    text = '{\n  "ok": true,\n  "note": "这一行被砍断了没有闭合引号'
    try:
        Tiny.model_validate_json(text)
        raise AssertionError("这段构造的输入应该解析失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, text) is True


def test_looks_like_truncated_json_ignores_non_json_invalid_errors():
    """字段类型错（不是 JSON 语法错）走的是另一条 pydantic 错误类型，
    不该被这个只管"JSON 本身解析失败"的判断函数误伤。"""
    from pydantic import ValidationError

    try:
        Tiny.model_validate_json('{"ok": "不是布尔值", "note": "x"}')
        raise AssertionError("这段构造的输入应该校验失败，测试前提不成立")
    except ValidationError as e:
        assert _looks_like_truncated_json(e, '{"ok": "不是布尔值", "note": "x"}') is False


def test_generate_raises_truncated_error_without_retrying():
    """截断了就直接失败，不重试三次——同样的输入会在同一处再次被截断，
    重试是白烧调用。这条用真实会撞到的场景构造：只给一次截断响应，
    如果代码还在重试就会 IndexError（脚本只有一条）而不是我们要的
    LLMTruncatedError，能确认"真的只调用了一次"。"""
    b = ScriptedBackend([_truncated_json_for(Tiny)])
    with pytest.raises(LLMTruncatedError) as ei:
        b.generate(system="s", user="u", schema=Tiny)
    assert len(b.calls) == 1  # 没有重试
    msg = str(ei.value)
    assert "截断" in msg
    assert "backend=scripted" in msg


def test_generate_truncated_error_is_also_an_llm_error():
    """子类关系：广义捕获 LLMError 的既有调用方不用为了这条改代码。"""
    assert issubclass(LLMTruncatedError, LLMError)
