"""scripts/verify_local_backend.py 的离线测试。

脚本本身是给装了 vllm 的机器跑的，但**它的闸门判断逻辑必须在这里测**——
不然"闸门写错了导致永远绿"这种问题只能在 AutoDL 上撞到，而那正是这个脚本
要替我们发现问题的场合。用假后端把 generate() 换掉，直接检验三种结局：
一次过（0）、重试过（1，guided_json 没生效）、调用失败（1）。
"""
import pytest

from core.llm import LLMError
from scripts import verify_local_backend as vlb


class _FakeBackend:
    """按脚本实际会用到的接口做的最小假后端：generate 可以设置成"第 N 次
    才成功"，用来模拟 guided_json 没生效（靠 generate() 的重试兜底）。"""

    def __init__(self, succeed_on_attempt: int = 1, raise_llm_error: bool = False):
        self.succeed_on_attempt = succeed_on_attempt
        self.raise_llm_error = raise_llm_error
        self.attempts = 0
        self.physicians_seen: list[str | None] = []

    def backend_id(self):
        return "local"

    def model_name(self):
        return "/weights/fake"

    def comparability_warning(self):
        return "后端：local（假的），不可比。"

    def _complete(self, *a, **kw):  # 脚本会包一层计数器在这个方法上
        self.attempts += 1
        return '{"syndrome": "肝胃不和证", "confidence": 70}'

    def generate(self, system, user, schema, physician=None, **kwargs):
        self.physicians_seen.append(physician)
        if self.raise_llm_error:
            raise LLMError("模拟：重试耗尽")
        # 模拟 generate() 内部的重试：调 _complete 直到第 succeed_on_attempt 次
        for _ in range(self.succeed_on_attempt):
            self._complete()
        return schema(syndrome="肝胃不和证", confidence=70)


def _use_fake_backend(monkeypatch, backend):
    monkeypatch.setenv("LLM_MODE", "local")
    monkeypatch.delenv("LORA_DIR", raising=False)
    monkeypatch.setattr(vlb, "get_backend", lambda: backend)


def test_show_prompt_budget_reports_a_power_of_two_above_the_worst_case(capsys):
    """max-model-len 的取值必须从真实 prompt 现算，不能是脚本里写死的一个数
    ——prompt 改了这个数就该跟着变。这条同时守着"算出来的建议值 ≥ 最坏路径"。"""
    assert vlb.show_prompt_budget() == 0
    out = capsys.readouterr().out
    assert "最坏路径" in out and "--max-model-len 建议取" in out

    worst = int(out.split("最坏路径（ReAct trace + S3 + 2 次重试）≈")[1].split("tokens")[0])
    suggested = int(out.split("--max-model-len 建议取")[1].split("（")[0])
    assert suggested >= worst
    assert suggested & (suggested - 1) == 0  # 2 的幂


def test_start_vllm_script_max_model_len_matches_the_computed_budget(capsys):
    """脚本里的 --max-model-len 跟现算出来的建议值必须一致——两个数字各自
    维护迟早脱节（上一轮 test_api_stream 的 60 秒就是这么跟服务器自己的 120
    秒脱节的）。"""
    from pathlib import Path

    vlb.show_prompt_budget()
    suggested = int(capsys.readouterr().out.split("--max-model-len 建议取")[1].split("（")[0])
    script = Path(vlb.__file__).resolve().parent / "start_vllm.sh"
    text = script.read_text(encoding="utf-8")
    assert f"--max-model-len {suggested}" in text, (
        f"start_vllm.sh 里的 --max-model-len 跟现算的建议值 {suggested} 不一致"
    )


def test_main_returns_2_when_not_in_local_mode(monkeypatch, capsys):
    """退出码 2 = 配置不全、连试都试不了，跟"试了但没过"（1）分开——CI 里
    能区分"忘了设环境变量"和"真的接不上"。"""
    monkeypatch.setenv("LLM_MODE", "api")
    assert vlb.main([]) == 2


def test_main_passes_when_one_call_is_enough(monkeypatch, capsys):
    backend = _FakeBackend(succeed_on_attempt=1)
    _use_fake_backend(monkeypatch, backend)
    assert vlb.main([]) == 0
    out = capsys.readouterr().out
    assert "闸门 1" in out and "闸门 2" in out
    assert "全部闸门通过" in out


def test_main_fails_when_generate_needed_retries(monkeypatch, capsys):
    """**核心闸门**：guided_json 生效时输出必然合法、不该触发重试。重试还能
    成功也算不过——那等于白花两次调用，而且本地模型相对云端 API 的这个优势
    根本没用上，是配置问题（大概率 vLLM 版本不认这个键），必须暴露。"""
    backend = _FakeBackend(succeed_on_attempt=2)
    _use_fake_backend(monkeypatch, backend)
    assert vlb.main([]) == 1
    err = capsys.readouterr().err
    assert "闸门 2 未过" in err
    assert "VLLM_GUIDED_JSON_KEY" in err  # 报错要说清怎么改


def test_main_fails_when_the_call_itself_fails(monkeypatch, capsys):
    backend = _FakeBackend(raise_llm_error=True)
    _use_fake_backend(monkeypatch, backend)
    assert vlb.main([]) == 1
    assert "闸门 1" in capsys.readouterr().err


def test_main_passes_physician_through_for_a_lora_probe(monkeypatch, capsys):
    """--physician 是用来试"带 LoRA 的那条路"的，必须真的传下去，不然这个
    参数只是装饰。"""
    backend = _FakeBackend()
    _use_fake_backend(monkeypatch, backend)
    assert vlb.main(["--physician", "zhang_xichun"]) == 0
    assert backend.physicians_seen == ["zhang_xichun"]


def test_lora_check_reports_every_missing_adapter(monkeypatch, tmp_path, capsys):
    """LORA_DIR 设了就逐个已注册医家查目录，缺哪个报哪个——复用
    core/llm.py 的 _resolve_lora_path，不另写一套判断。"""
    from core.physicians import PHYSICIANS

    monkeypatch.setenv("LORA_DIR", str(tmp_path))
    (tmp_path / next(iter(PHYSICIANS))).mkdir()  # 只放第一位医家的 adapter
    problems = vlb._check_lora()
    assert len(problems) == len(PHYSICIANS) - 1
    assert all("不静默退化" in p for p in problems)


def test_lora_check_is_quiet_when_lora_dir_unset(monkeypatch, capsys):
    monkeypatch.delenv("LORA_DIR", raising=False)
    assert vlb._check_lora() == []
    assert "跑基座模型" in capsys.readouterr().out


def test_probe_schema_has_constrained_fields():
    """探针 schema 的字段必须带约束：全字段可选的 schema 连"模型有没有照
    schema 走"都测不出来（随便一个 {} 都合法），闸门就成了摆设。"""
    required = vlb._Probe.model_json_schema().get("required", [])
    assert set(required) == {"syndrome", "confidence"}
    with pytest.raises(Exception):
        vlb._Probe.model_validate({})  # 空对象必须不合法
