"""R12-D：思考模式按步控制。

DeepSeek 的推理模型默认开思考。两个后果都要按步处理：抽取类的步骤（S1/S2/追问/
ReAct）开着思考又慢又没有增益；而且**思考模式下 temperature 不生效**——那正是
v4-pro"同样输入不同输出"的根因，也就是 ε 和 fixture 可复现性一起失效的原因。
"""
import pytest
from pydantic import BaseModel, Field

from core import chain
from core.llm import (
    LLMBackend,
    OpenAICompatBackend,
    S3_REASONING_EFFORT_FULL_CONTEXT,
    S3_REASONING_EFFORT_TOP3,
    S3_THINKING_DEFAULT,
    STEP_THINKING,
    s3_reasoning_effort,
    s3_thinking,
    thinking_by_step,
    thinking_for,
)
from core.schemas import S3Syndrome
from tests.test_chain import FakeLLM, FakeRetriever, _fake_cases


class Out(BaseModel):
    ok: bool
    note: str = Field(min_length=1)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("S3_THINKING", raising=False)


# ---------- 这张表本身 ----------


def test_extraction_steps_have_thinking_disabled():
    """S1/S2/追问/残差/ReAct 全部关思考。这几步是结构化抽取或工具选择，
    思考不带来增益，却让每次调用从两三秒变成几十秒。"""
    for step in ("s1", "s2", "followup", "residual", "react"):
        assert thinking_for(step) == {"thinking": "disabled", "reasoning_effort": None}, step


def test_s3_keeps_thinking_on_by_default():
    """S3 是这条链上唯一真正需要推理的一步——默认开。

    **有意的契约变更（R22）**：原来断言 `reasoning_effort == "high"` 是写死的。
    R22 起 effort 的默认值**跟检索方式绑**：full_context（默认）下 `max`、
    top3 系下 `high`。理由见 `core/llm.py::s3_reasoning_effort` 的文档字符串——
    full_context 下输入已经是十几万 token 且靠缓存便宜 30 倍，这时限制推理深度
    是省小钱费大钱；top3 保持 high 是为了跟 R1~R21 的数字可比。
    """
    assert s3_thinking() == S3_THINKING_DEFAULT == "enabled"
    assert thinking_for("s3") == {"thinking": "enabled",
                                  "reasoning_effort": S3_REASONING_EFFORT_FULL_CONTEXT}


def test_s3_effort_default_follows_the_retriever_mode(monkeypatch):
    """两系各自的默认档。写成两条断言而不是一条参数化：这两个值的**理由不同**
    （一个是"输入已经很贵了"，一个是"要跟历史数字可比"），合成一条会把理由抹掉。"""
    monkeypatch.delenv("S3_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("RETRIEVER_MODE", raising=False)
    assert s3_reasoning_effort() == S3_REASONING_EFFORT_FULL_CONTEXT == "max"
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    assert s3_reasoning_effort() == S3_REASONING_EFFORT_TOP3 == "high"


def test_s3_effort_env_var_wins_over_the_mode_default(monkeypatch):
    monkeypatch.setenv("S3_REASONING_EFFORT", "low")
    assert s3_reasoning_effort() == "low"
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    assert s3_reasoning_effort() == "low", "显式指定不该被检索方式覆盖"


def test_an_unknown_effort_falls_back_loudly(monkeypatch, capsys):
    """拼错一档的表现是"这次悄悄用了别的设置"——跟没设一样看不出来，
    所以要吼一声（同 thinking_for 未知 step 那条）。"""
    monkeypatch.setenv("S3_REASONING_EFFORT", "ultra")
    assert s3_reasoning_effort() == S3_REASONING_EFFORT_FULL_CONTEXT
    assert "只认" in capsys.readouterr().err


def test_s3_thinking_env_var_turns_it_off(monkeypatch):
    monkeypatch.setenv("S3_THINKING", "disabled")
    assert thinking_for("s3") == {"thinking": "disabled", "reasoning_effort": None}
    assert thinking_by_step()["s3"] == "disabled"


def test_a_bogus_s3_thinking_value_falls_back_loudly(monkeypatch, capsys):
    monkeypatch.setenv("S3_THINKING", "maybe")
    assert s3_thinking() == S3_THINKING_DEFAULT
    assert "S3_THINKING" in capsys.readouterr().err


def test_an_unknown_step_name_is_not_silently_defaulted(capsys):
    """拼错一个步骤名的表现是"那一步悄悄用了别的设置"，跟没设一样看不出来。"""
    assert thinking_for("s9") == {"thinking": None, "reasoning_effort": None}
    err = capsys.readouterr().err
    assert "s9" in err and "没有这个步骤" in err


def test_thinking_by_step_covers_every_step_in_the_table():
    assert set(thinking_by_step()) == set(STEP_THINKING)


# ---------- 真的传到请求里了 ----------


def _fake_client(captured: dict):
    class FakeCompletions:
        def create(self, **kw):
            captured.update(kw)

            class R:
                choices = [type("C", (), {"message": type("M", (), {
                    "content": '{"ok":true,"note":"n"}'})()})]
            return R()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    return FakeClient()


def test_openai_backend_sends_thinking_and_effort():
    captured: dict = {}
    b = OpenAICompatBackend()
    b._client = _fake_client(captured)
    b._complete([{"role": "user", "content": "x"}], 0.0,
                thinking="enabled", reasoning_effort="high")
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}
    assert captured["reasoning_effort"] == "high"


def test_temperature_is_not_sent_when_thinking_is_on():
    """**思考模式下 temperature 不生效**（DeepSeek 文档）。既然不生效就不传：
    传了会让 manifest 里那个 temperature 看起来像是生效了的实验条件，而"同样输入
    不同输出"正是被这个误解坑过一次的地方。"""
    captured: dict = {}
    b = OpenAICompatBackend()
    b._client = _fake_client(captured)
    b._complete([{"role": "user", "content": "x"}], 0.0, thinking="enabled")
    assert "temperature" not in captured

    captured.clear()
    b._complete([{"role": "user", "content": "x"}], 0.0, thinking="disabled")
    assert captured["temperature"] == 0.0


def test_not_passing_thinking_sends_nothing_extra():
    """不指定就是不指定，走 API 默认——不许替调用方猜一个值填进去。"""
    captured: dict = {}
    b = OpenAICompatBackend()
    b._client = _fake_client(captured)
    b._complete([{"role": "user", "content": "x"}], 0.0)
    assert "extra_body" not in captured and "reasoning_effort" not in captured
    assert captured["temperature"] == 0.0


def test_other_backends_accept_and_ignore_the_parameters():
    """非 DeepSeek 后端（回放、claude_cli、进程内 vLLM）没有这个开关。
    **必须如实忽略而不是报错**——多一个不认识的关键字就是 TypeError，
    那会让整条链在切后端时直接崩。"""
    class Plain(LLMBackend):
        def model_name(self):
            return "fake"

        def backend_id(self):
            return "fake"

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kwargs):
            assert kwargs.get("thinking") == "disabled"
            return '{"ok": true, "note": "n"}'

    assert Plain().generate(system="s", user="u", schema=Out,
                            **thinking_for("s1")).ok is True


def test_replay_fixtures_are_not_affected_by_thinking(tmp_path):
    """回放的索引是 (schema 名, system 文本的 sha256)——思考参数不进 key。
    进了的话每改一次思考设置，全部 fixture 就得重录一遍。"""
    from core.llm_replay import fixture_key

    assert fixture_key("S1Normalize", "同一段 system") == fixture_key("S1Normalize", "同一段 system")


# ---------- manifest ----------


def _consult(monkeypatch):
    from core.physicians import PHYSICIANS as REG

    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=["ye_tianshi-001"], herbs=["党参"])
    monkeypatch.setattr(chain, "get_llm", lambda: FakeLLM({i["name"]: s3 for i in REG.values()}))
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    return chain.consult("纳差乏力")


def test_manifest_records_thinking_for_every_step(monkeypatch):
    """换了这张表 = 数字不可比，所以它跟 model 一样是 manifest 的一等字段。"""
    manifest = _consult(monkeypatch)["manifest"]
    assert manifest["thinking_by_step"] == thinking_by_step()
    assert set(manifest["thinking_by_step"]) == set(STEP_THINKING)


def test_manifest_temperature_effective_is_per_step_not_a_single_number(monkeypatch):
    """**不写成一个标量**：各步的思考设置不同，所以"这次跑的 temperature 是多少"
    本来就没有单一答案。写一个标量就得挑一步代表全体，那是在报告里埋一句不准确的话。"""
    manifest = _consult(monkeypatch)["manifest"]
    effective = manifest["temperature_effective"]
    assert effective["s1"] == 0.0 and effective["s2"] == 0.0
    assert effective["s3"] is None, "S3 开着思考，temperature 不生效，必须记 null"


def test_turning_off_s3_thinking_triggers_the_comparability_warning(monkeypatch):
    """关思考跑出来的数字跟默认配置下的不可比——跟换模型同级，必须自己说出来。"""
    monkeypatch.setenv("S3_THINKING", "disabled")
    manifest = _consult(monkeypatch)["manifest"]
    assert manifest["temperature_effective"]["s3"] == 0.0
    warning = manifest["comparability_warning"] or ""
    assert "S3_THINKING=disabled" in warning and "不可比" in warning


def test_default_thinking_does_not_add_a_warning(monkeypatch):
    """对照：默认配置下不该凭空多出一句警告——遍地是警告等于没有警告。"""
    manifest = _consult(monkeypatch)["manifest"]
    assert "S3_THINKING" not in (manifest["comparability_warning"] or "")
