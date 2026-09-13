"""本地模型后端（vLLM）的离线测试：**这台机器上没有装 vllm，也不该为了跑这些
测试去装**——测的是我们传给 vLLM 的参数对不对，不是 vLLM 本身对不对。

两种模式各自的测法：
  - `LLM_MODE=local`（server 模式）：vLLM 起的是 OpenAI 兼容接口，所以把
    `openai.OpenAI` 换成一个记录调用参数的假客户端，断言 extra_body 里
    guided_json / lora_request 确实传进去了。
  - `LLM_MODE=local_inproc`（进程内模式）：往 sys.modules 里塞一个假的 `vllm`
    模块（含 LLM/SamplingParams/GuidedDecodingParams/LoRARequest），断言
    SamplingParams 和 LoRARequest 的构造参数对。

最容易漏的一条单独测：**没装 vllm 时 `core.llm` 仍然能 import**。顶层
`import vllm` 会让沙盒/CI 里 1600 多个跟 vLLM 毫无关系的测试全崩。
"""
import sys
import types

import pytest
from pydantic import BaseModel, Field

import core.llm as core_llm
from core.llm import (
    LLMError,
    OpenAICompatBackend,
    VLLMBackend,
    VLLMInProcessBackend,
    get_backend,
)


class Tiny(BaseModel):
    ok: bool
    note: str = Field(min_length=1)


# ---------- 没装 vllm 时 core.llm 必须能 import ----------


def test_core_llm_has_no_module_level_vllm_import():
    """**最容易漏的一条**：`import vllm` 必须在函数体里，不能在模块顶层——
    顶层 import 会让沙盒/CI（都没装 vllm）连 `core.llm` 都 import 不了，
    1600 多个跟 vLLM 毫无关系的测试全崩。

    用 AST 静态检查而不是"把 vllm 拦掉再 reload 一遍 core.llm"：reload 会
    产出一套新的类对象（新的 LLMError/VLLMBackend），而其它测试模块在
    import 时已经绑定了旧的那套，`pytest.raises(LLMError)` 和 isinstance
    从此全部假阴性——这个坑在写这条测试时真的踩到了（5 条其它测试变红）。
    静态检查没有任何全局副作用，而且证明的是"源码里没有顶层 vllm import"
    这个更强的性质：在装了 vllm 的机器上同样有效，不依赖"当前机器碰巧没装"。
    """
    import ast
    from pathlib import Path

    source = Path(core_llm.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in tree.body:  # 只看模块顶层，函数/类体内部的 import 是允许的
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "vllm"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "vllm":
                offenders.append(node.module)
    assert offenders == [], f"core/llm.py 顶层出现了 vllm import：{offenders}"
    # 顺带确认这台机器上 import core.llm 真的没把 vllm 拉进来
    assert "vllm" not in sys.modules


def test_metadata_methods_work_without_vllm(monkeypatch):
    """没装 vllm 时，manifest 要问的三个方法（model_name/backend_id/
    comparability_warning）都不能触发 import——manifest 在没配置本地模型的
    机器上也会被问到。"""
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/Qwen2.5-1.5B-Instruct")
    for backend in (VLLMBackend(), VLLMInProcessBackend()):
        assert backend.model_name() == "/weights/Qwen2.5-1.5B-Instruct"
        assert backend.backend_id() in ("local", "local_inproc")
        assert backend.comparability_warning() is not None
    assert "vllm" not in sys.modules


# ---------- LLM_MODE 分派 ----------


def test_get_backend_local_inproc(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "local_inproc")
    assert isinstance(get_backend(), VLLMInProcessBackend)


def test_vllm_server_backend_is_an_openai_compat_subclass():
    """继承而不是复制：客户端构造、重试语义、max_tokens 默认值只有一份实现
    （CLAUDE.md「同一概念只能有一处实现」）。这条测试钉住继承关系本身——
    改成复制一份代码的话它会红。"""
    assert issubclass(VLLMBackend, OpenAICompatBackend)
    # _complete 必须是自己的（要加 extra_body），其余复用父类的
    assert VLLMBackend._complete is not OpenAICompatBackend._complete
    assert VLLMBackend.client is OpenAICompatBackend.client


# ---------- server 模式：假 OpenAI 客户端，看我们传了什么 ----------


class _FakeCompletions:
    def __init__(self, recorder: list[dict], content: str):
        self._recorder = recorder
        self._content = content

    def create(self, **kwargs):
        self._recorder.append(kwargs)
        message = types.SimpleNamespace(content=self._content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _FakeOpenAI:
    """记录 OpenAI(...) 的构造参数和 chat.completions.create(...) 的调用参数。"""

    last_init: dict = {}

    def __init__(self, **kwargs):
        type(self).last_init = kwargs
        self.calls: list[dict] = []
        self.chat = types.SimpleNamespace(
            completions=_FakeCompletions(self.calls, '{"ok": true, "note": "ok"}')
        )


@pytest.fixture
def fake_openai(monkeypatch):
    import openai

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    return _FakeOpenAI


def _clear_local_env(monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_MODEL_PATH",
                "LORA_DIR", "LLM_MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)


def test_server_mode_defaults_to_local_base_url_and_placeholder_key(fake_openai, monkeypatch):
    """vLLM server 默认在本机 8000 端口；api_key 是占位串——vLLM 不校验它，
    但 OpenAI SDK 不允许空值（会去找 OPENAI_API_KEY，找不到就抛）。"""
    _clear_local_env(monkeypatch)
    backend = VLLMBackend()
    backend.client  # 触发惰性构造
    assert fake_openai.last_init["base_url"] == "http://127.0.0.1:8000/v1"
    assert fake_openai.last_init["api_key"] == "EMPTY"
    assert fake_openai.last_init["max_retries"] == 0  # 重试只在 generate() 一处


def test_server_mode_respects_explicit_base_url_and_api_key(fake_openai, monkeypatch):
    """server 可以用 --api-key 开鉴权、也可以跑在别的地址上，显式设置要生效。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "http://10.0.0.7:9001/v1")
    monkeypatch.setenv("LLM_API_KEY", "真的钥匙")
    backend = VLLMBackend()
    backend.client
    assert fake_openai.last_init["base_url"] == "http://10.0.0.7:9001/v1"
    assert fake_openai.last_init["api_key"] == "真的钥匙"


def test_server_mode_manifest_reports_weight_path_but_request_uses_served_name(
    fake_openai, monkeypatch
):
    """两个值必须分开：manifest 记权重路径（"tcm-local"这种别名对复现没用），
    HTTP 请求填 served name（server 只认它）。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/Qwen2.5-1.5B-Instruct")
    monkeypatch.setenv("LLM_MODEL", "tcm-local")
    backend = VLLMBackend()
    assert backend.model_name() == "/weights/Qwen2.5-1.5B-Instruct"
    assert backend._request_model_name() == "tcm-local"

    backend._complete([{"role": "user", "content": "x"}], 0.0)
    assert backend.client.calls[0]["model"] == "tcm-local"


def test_server_mode_falls_back_to_weight_path_as_request_model(fake_openai, monkeypatch):
    """没传 --served-model-name 时 vLLM 用权重路径当模型名，请求就填路径。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/Qwen2.5-1.5B-Instruct")
    backend = VLLMBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0)
    assert backend.client.calls[0]["model"] == "/weights/Qwen2.5-1.5B-Instruct"


def test_server_mode_passes_guided_json_built_from_the_schema(fake_openai, monkeypatch):
    """guided_decoding 是本地模型相对云端 API 的真实优势：从解码层保证输出
    符合 schema，而不是靠"prompt 里塞 schema + 三次重试"。"""
    _clear_local_env(monkeypatch)
    backend = VLLMBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, schema=Tiny)
    extra_body = backend.client.calls[0]["extra_body"]
    assert extra_body["guided_json"] == Tiny.model_json_schema()


def test_server_mode_guided_json_key_is_overridable_for_vllm_version_drift(
    fake_openai, monkeypatch
):
    """vLLM 的这个参数名在版本间变过。撞上版本不认 guided_json 时，运维改一个
    环境变量就能绕过，不用改代码等下一轮——但默认值仍然是 guided_json。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setattr("core.llm.VLLM_GUIDED_JSON_KEY", "structured_json")
    backend = VLLMBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, schema=Tiny)
    extra_body = backend.client.calls[0]["extra_body"]
    assert extra_body["structured_json"] == Tiny.model_json_schema()
    assert "guided_json" not in extra_body


def test_server_mode_schema_and_physician_never_leak_into_the_sdk_call(fake_openai, monkeypatch):
    """schema/physician 是给后端自己看的，**不能原样转给 OpenAI SDK**——多一个
    它不认的关键字参数就是 TypeError。这也是这两个参数为什么是显式形参、
    不塞 **kwargs（父类会把 kwargs 整个转给 SDK）。"""
    _clear_local_env(monkeypatch)
    backend = VLLMBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, schema=Tiny, physician="ye_tianshi")
    sent = backend.client.calls[0]
    assert "schema" not in sent and "physician" not in sent


def test_api_backend_also_does_not_leak_schema_and_physician(fake_openai, monkeypatch):
    """同一条约束在云端后端上也要成立——它拿不到 guided_decoding/LoRA，如实
    忽略这两个参数，但绝不能把它们转给 SDK。"""
    _clear_local_env(monkeypatch)
    backend = OpenAICompatBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, schema=Tiny, physician="ye_tianshi")
    sent = backend.client.calls[0]
    assert "schema" not in sent and "physician" not in sent
    assert "extra_body" not in sent  # 云端不该凭空多出 extra_body


def test_generate_end_to_end_carries_guided_json_and_lora(fake_openai, monkeypatch, tmp_path):
    """从 generate() 一路到 SDK 调用：schema 和 physician 是 generate() 的参数，
    要真的变成 extra_body 里的 guided_json / lora_request，中间不能断。
    只测通路，不测 vLLM 本身。"""
    _clear_local_env(monkeypatch)
    (tmp_path / "ye_tianshi").mkdir()
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    backend = VLLMBackend()
    out = backend.generate(system="s", user="u", schema=Tiny, physician="ye_tianshi")
    assert out.ok is True
    extra_body = backend.client.calls[0]["extra_body"]
    assert extra_body["guided_json"] == Tiny.model_json_schema()
    assert extra_body["lora_request"] == {
        "lora_name": "ye_tianshi", "lora_path": str(tmp_path / "ye_tianshi"),
    }


# ---------- LoRA：三种情况分清楚，目录缺失必须报错 ----------


def test_no_lora_request_when_lora_dir_unset(fake_openai, monkeypatch):
    """阶段五的 LoRA 还没训，LORA_DIR 不设置就是走基座模型——这是正常状态，
    不是错误。"""
    _clear_local_env(monkeypatch)
    backend = VLLMBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, physician="ye_tianshi")
    assert "lora_request" not in backend.client.calls[0]["extra_body"]
    assert backend.lora_for("ye_tianshi") is None


def test_no_lora_request_for_physician_agnostic_calls(fake_openai, monkeypatch, tmp_path):
    """S1/S2 这类跟医家无关的步骤不带 physician，即使配了 LORA_DIR 也不挂
    adapter——不是错误，这些步骤本来就没有"哪位医家"可言。"""
    _clear_local_env(monkeypatch)
    (tmp_path / "ye_tianshi").mkdir()
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    backend = VLLMBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, schema=Tiny)  # 没有 physician
    assert "lora_request" not in backend.client.calls[0]["extra_body"]
    assert backend.lora_for(None) is None


def test_missing_lora_dir_for_physician_raises_instead_of_falling_back(monkeypatch, tmp_path):
    """**核心约束**：LORA_DIR 配了、但这位医家的 adapter 目录不存在时报错，
    不静默退化成基座模型——静默退化会让"这位医家用的是他自己的 LoRA"这句
    声称变成假的，而且是静默变假：报告照样写着 LoRA 跑的。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LORA_DIR", str(tmp_path))  # 目录在，但里面没有 zhang_xichun
    backend = VLLMBackend()
    with pytest.raises(LLMError) as ei:
        backend._complete([{"role": "user", "content": "x"}], 0.0, physician="zhang_xichun")
    msg = str(ei.value)
    assert "zhang_xichun" in msg
    assert str(tmp_path) in msg
    assert "不静默退化" in msg


def test_missing_lora_dir_raises_in_inproc_mode_too(monkeypatch, tmp_path):
    """同一条约束在进程内模式下也要成立——两种模式共用同一处 LoRA 路径解析，
    不是各写一份判断（那样迟早只修一边）。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    with pytest.raises(LLMError):
        VLLMInProcessBackend()._lora_request("zhang_xichun")


def test_lora_for_reports_the_adapter_actually_used(monkeypatch, tmp_path):
    """manifest 靠 lora_for() 如实记录，不是"配了 LORA_DIR 就假设每位医家都
    用上了自己的 adapter"。"""
    _clear_local_env(monkeypatch)
    (tmp_path / "wu_jutong").mkdir()
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    backend = VLLMBackend()
    assert backend.lora_for("wu_jutong") == "wu_jutong"
    assert backend.lora_for(None) is None


def test_comparability_warning_states_model_and_lora_state(monkeypatch, tmp_path):
    """manifest 里的这句话是报告读者判断"这个数能不能跟别的比"的唯一依据，
    要同时说清后端、权重、LoRA 状态。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/Qwen2.5-1.5B-Instruct")
    w_base = VLLMBackend().comparability_warning()
    assert "local" in w_base and "/weights/Qwen2.5-1.5B-Instruct" in w_base
    assert "LoRA: 未加载" in w_base
    assert "DeepSeek" in w_base

    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    w_lora = VLLMBackend().comparability_warning()
    assert str(tmp_path) in w_lora
    assert "未加载" not in w_lora


# ---------- 进程内模式：假 vllm 模块 ----------


class _FakeSamplingParams:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeGuidedDecodingParams:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeLoRARequest:
    def __init__(self, name, lora_id, path):
        self.name, self.lora_id, self.path = name, lora_id, path


class _FakeLLM:
    """记录 vllm.LLM(...) 的构造参数与 chat(...) 的调用参数。"""

    last_init: dict = {}

    def __init__(self, **kwargs):
        type(self).last_init = kwargs
        self.chat_calls: list[dict] = []
        self.text = '{"ok": true, "note": "来自进程内 vLLM"}'
        self.malformed = False

    def chat(self, messages, **kwargs):
        self.chat_calls.append({"messages": messages, **kwargs})
        if self.malformed:
            return []
        inner = types.SimpleNamespace(text=self.text)
        return [types.SimpleNamespace(outputs=[inner])]


@pytest.fixture
def fake_vllm(monkeypatch):
    """往 sys.modules 里塞一个假 vllm（含要用到的三个子模块），这样
    `import vllm` / `from vllm.sampling_params import ...` /
    `from vllm.lora.request import ...` 都能成功，而机器上并没有装 vllm。"""
    vllm_mod = types.ModuleType("vllm")
    vllm_mod.LLM = _FakeLLM
    vllm_mod.SamplingParams = _FakeSamplingParams

    sampling_mod = types.ModuleType("vllm.sampling_params")
    sampling_mod.GuidedDecodingParams = _FakeGuidedDecodingParams
    vllm_mod.sampling_params = sampling_mod

    lora_mod = types.ModuleType("vllm.lora")
    request_mod = types.ModuleType("vllm.lora.request")
    request_mod.LoRARequest = _FakeLoRARequest
    lora_mod.request = request_mod
    vllm_mod.lora = lora_mod

    monkeypatch.setitem(sys.modules, "vllm", vllm_mod)
    monkeypatch.setitem(sys.modules, "vllm.sampling_params", sampling_mod)
    monkeypatch.setitem(sys.modules, "vllm.lora", lora_mod)
    monkeypatch.setitem(sys.modules, "vllm.lora.request", request_mod)
    return vllm_mod


def test_inproc_requires_model_path(fake_vllm, monkeypatch):
    """进程内模式没有 server 可以问，权重路径必须显式给——报错要说清要设哪个
    环境变量，不是让人去猜。"""
    _clear_local_env(monkeypatch)
    with pytest.raises(LLMError) as ei:
        VLLMInProcessBackend()._complete([{"role": "user", "content": "x"}], 0.0)
    assert "LLM_MODEL_PATH" in str(ei.value)


def test_inproc_engine_is_lazy_and_configured_from_env(fake_vllm, monkeypatch):
    """加载权重是几十秒级的重 IO，不能在 __init__ 里做——manifest 问一句
    model_name() 都会触发。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/Qwen2.5-1.5B-Instruct")
    monkeypatch.setenv("VLLM_MAX_MODEL_LEN", "4096")
    monkeypatch.setenv("VLLM_GPU_MEMORY_UTILIZATION", "0.7")
    backend = VLLMInProcessBackend()
    assert backend._llm is None  # 只构造后端还没加载模型
    backend.model_name()
    assert backend._llm is None  # 问元数据也不该加载

    engine = backend._engine
    assert engine is not None
    assert _FakeLLM.last_init["model"] == "/weights/Qwen2.5-1.5B-Instruct"
    assert _FakeLLM.last_init["max_model_len"] == 4096
    assert _FakeLLM.last_init["gpu_memory_utilization"] == 0.7
    assert _FakeLLM.last_init["enable_lora"] is False  # 没配 LORA_DIR 就不开
    assert backend._engine is engine  # 第二次拿的是同一个，不重复加载


def test_inproc_enables_lora_only_when_lora_dir_set(fake_vllm, monkeypatch, tmp_path):
    """enable_lora 要在引擎创建时就定，之后改不了；没配 LORA_DIR 时不开，
    省掉 LoRA 的显存与调度开销。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    VLLMInProcessBackend()._engine
    assert _FakeLLM.last_init["enable_lora"] is True


def test_inproc_passes_guided_decoding_and_returns_text(fake_vllm, monkeypatch):
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    backend = VLLMInProcessBackend()
    out = backend._complete([{"role": "user", "content": "x"}], 0.3, max_tokens=512, schema=Tiny)
    assert out == '{"ok": true, "note": "来自进程内 vLLM"}'

    call = backend._engine.chat_calls[0]
    sp = call["sampling_params"]
    assert sp.kwargs["temperature"] == 0.3
    assert sp.kwargs["max_tokens"] == 512
    assert sp.kwargs["guided_decoding"].kwargs["json"] == Tiny.model_json_schema()
    assert call["lora_request"] is None


def test_inproc_no_guided_decoding_without_schema(fake_vllm, monkeypatch):
    """schema 是可选的（虽然 generate() 总会传）——没有 schema 时不该凭空
    造一个空的 GuidedDecodingParams，那会把解码约束成"任意 JSON"之外的东西。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    backend = VLLMInProcessBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0)
    assert backend._engine.chat_calls[0]["sampling_params"].kwargs["guided_decoding"] is None


def test_inproc_lora_request_id_is_stable_per_adapter(fake_vllm, monkeypatch, tmp_path):
    """同一个 adapter 每次给不同 id，vLLM 会当成不同 adapter 反复加载。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    for pid in ("ye_tianshi", "wu_jutong"):
        (tmp_path / pid).mkdir()
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    backend = VLLMInProcessBackend()

    first = backend._lora_request("ye_tianshi")
    again = backend._lora_request("ye_tianshi")
    other = backend._lora_request("wu_jutong")
    assert first.lora_id == again.lora_id
    assert other.lora_id != first.lora_id
    assert first.name == "ye_tianshi"
    assert first.path == str(tmp_path / "ye_tianshi")


def test_inproc_lora_request_reaches_chat(fake_vllm, monkeypatch, tmp_path):
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    (tmp_path / "zhang_xichun").mkdir()
    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    backend = VLLMInProcessBackend()
    backend._complete([{"role": "user", "content": "x"}], 0.0, physician="zhang_xichun")
    assert backend._engine.chat_calls[0]["lora_request"].name == "zhang_xichun"


def test_inproc_malformed_output_raises_instead_of_returning_empty(fake_vllm, monkeypatch):
    """返回结构不符合预期（vLLM 版本改了返回形状）时报错，不返回空串——空串
    会被 generate() 当成"模型输出不合 schema"重试三次，真实原因就被埋掉了。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    backend = VLLMInProcessBackend()
    backend._engine.malformed = True
    with pytest.raises(LLMError) as ei:
        backend._complete([{"role": "user", "content": "x"}], 0.0)
    assert "vllm" in str(ei.value).lower() or "vLLM" in str(ei.value)


def test_inproc_generate_end_to_end(fake_vllm, monkeypatch):
    """generate() 的校验/重试逻辑对进程内后端同样适用——假 vLLM 返回合法
    JSON，一次就该通过。"""
    _clear_local_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_PATH", "/weights/m")
    backend = VLLMInProcessBackend()
    out = backend.generate(system="s", user="u", schema=Tiny)
    assert out.ok is True and out.note == "来自进程内 vLLM"


# ---------- 基类默认实现：调用方不用判断后端类型 ----------


def test_non_local_backends_report_no_lora():
    """`lora_for`/`lora_dir` 做成基类方法，让 core/chain.py 那一行无条件可写
    ——调用方不该知道有几种后端、哪种支持 LoRA。云端后端恒为 None。

    推理链那一侧（physician 传到 generate、adapter 记进结果和 manifest）在
    tests/test_chain.py 里测：那边有钉住两位医家的 autouse fixture 和现成的
    假检索器，不在这里重造一套。"""
    backend = OpenAICompatBackend()
    assert backend.lora_for("ye_tianshi") is None
    assert backend.lora_dir() is None
