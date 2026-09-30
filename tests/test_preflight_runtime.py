"""运行时自检（`scripts/preflight_runtime.py`）。

测的是：
  · 三档结果（ok / warn / fail）各自意味着什么、怎么影响退出码；
  · 每条 fail 都带一句怎么修；
  · 那几个"不报错但会改变默认行为"的环境变量确实被检查。
"""
from __future__ import annotations

from pathlib import Path

from scripts import preflight_runtime as preflight

ROOT = Path(__file__).resolve().parent.parent


# ---------- 三档与退出码 ----------


def test_the_three_statuses_map_to_exit_codes():
    """fail → 1；warn 默认不影响退出码，`--strict` 下影响。
    warn 不拦是有意的：**有时就是要用真实 LLM 调用**，而那时 LLM_MODE 不是 replay。"""
    ok = preflight.Report([preflight.Check("a", "ok", "")])
    warn = preflight.Report([preflight.Check("a", "warn", "")])
    fail = preflight.Report([preflight.Check("a", "fail", "")])
    assert ok.exit_code() == 0 and ok.exit_code(strict=True) == 0
    assert warn.exit_code() == 0 and warn.exit_code(strict=True) == 1
    assert fail.exit_code() == 1 and fail.exit_code(strict=True) == 1


def test_every_failing_check_carries_a_fix():
    """一条只说"有问题"而不说怎么修的检查，用处有限。"""
    report = preflight.run_all()
    for c in report.checks:
        if c.status == "fail":
            assert c.fix, f"{c.name} 是 fail 但没说怎么修"


def test_there_is_no_skipped_status():
    """**没有"跳过"这一档**：查不了的东西要么归 warn 并说明"这台机器查不了"，
    要么就别列进来。一条静默跳过的检查比没有这条检查更糟。"""
    report = preflight.run_all()
    assert {c.status for c in report.checks} <= {"ok", "warn", "fail"}


# ---------- 环境残留：不报错但会改变默认行为的那一类 ----------


def test_the_leftover_env_vars_are_the_ones_that_change_behaviour_silently():
    """七个变量，每个都配一句"留着会怎样"。只说"请清掉"的话，人会以为是洁癖。"""
    assert set(preflight.LEFTOVER_ENV) == {
        "RETRIEVER_MODE", "USE_REACT", "EVAL_MODE",
        "S3_BEST_OF_N", "S3_REASONING_EFFORT", "S3_THINKING", "LORA_DIR"}
    for var, why in preflight.LEFTOVER_ENV.items():
        assert len(why) > 8, f"{var} 的理由太短，读起来像洁癖"
    # EVAL_MODE 那一条要说出最严重的后果
    assert "安全否决不再中止" in preflight.LEFTOVER_ENV["EVAL_MODE"]


def test_a_leftover_retriever_mode_is_a_failure_not_a_warning():
    """残留不会报错，只会让系统运行的不是默认模式，而页面上没有任何提示。"""
    checks = preflight.check_leftover_env({"RETRIEVER_MODE": "bm25"})
    assert len(checks) == 1
    assert checks[0].status == "fail"
    assert "bm25" in checks[0].detail
    assert checks[0].fix == "unset RETRIEVER_MODE"


def test_a_clean_env_says_so_instead_of_staying_silent():
    checks = preflight.check_leftover_env({})
    assert len(checks) == 1 and checks[0].status == "ok"


def test_the_expected_env_is_a_suggestion_not_a_gate():
    """LLM_MODE/FAST_MODE 只提醒——有时就是要用真实 LLM 调用。"""
    checks = preflight.check_expected_env({"LLM_MODE": "api"})
    by_name = {c.name: c for c in checks}
    assert by_name["展示设置 LLM_MODE"].status == "warn"
    assert by_name["展示设置 LLM_MODE"].fix == "export LLM_MODE=replay"
    good = preflight.check_expected_env({"LLM_MODE": "replay", "FAST_MODE": "1"})
    assert all(c.status == "ok" for c in good)


# ---------- 逐项检查 ----------


def test_the_quota_check_uses_the_computed_calls_per_consult(monkeypatch):
    """额度折算要问 `calls_per_consult()`，不写死每次问诊的调用数。"""
    from core.usage import calls_per_consult

    monkeypatch.delenv("QUOTA_PER_IP_DAILY_CALLS", raising=False)
    assert str(calls_per_consult()) in preflight.check_quota().detail
    # 坏值从折算系数推出来，不写死：够不够一次问诊取决于 calls_per_consult()。
    monkeypatch.setenv("QUOTA_PER_IP_DAILY_CALLS", str(max(0, calls_per_consult() - 1)))
    bad = preflight.check_quota()
    assert bad.status == "fail" and "0 次问诊" in bad.detail
    monkeypatch.setenv("QUOTA_PER_IP_DAILY_CALLS", str(calls_per_consult() * 5))
    assert preflight.check_quota().status == "ok"


def test_the_prefix_cache_check_admits_it_cannot_see_the_remote(monkeypatch):
    """远端缓存的死活这台机器查不到（那是 DeepSeek 的内部状态）。
    **如实归 warn 并给预热命令，不假装查过。**"""
    monkeypatch.delenv("RETRIEVER_MODE", raising=False)
    c = preflight.check_prefix_cache()
    assert c.status == "warn"
    assert "查不到" in c.detail and "预热" in c.detail


def test_the_prefix_cache_check_says_when_the_mode_makes_it_moot(monkeypatch):
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    c = preflight.check_prefix_cache()
    assert "没有前缀缓存这回事" in c.detail


def test_the_font_check_distinguishes_three_states(tmp_path):
    """三种状态分开：没生成 / 生成了但没接进 CSS / 接好了。
    中间那种最容易漏——文件在，但页面还在从 CDN 取。"""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.css").write_text("@font-face { src: url(https://cdn…) }",
                                              encoding="utf-8")
    assert preflight.check_fonts(tmp_path).status == "warn"

    fonts = tmp_path / "web" / "vendor" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "noto-serif-sc-400-subset.woff2").write_bytes(b"x")
    mid = preflight.check_fonts(tmp_path)
    assert mid.status == "warn" and "还指着 CDN" in mid.detail

    (tmp_path / "web" / "app.css").write_text('src: url("vendor/fonts/a.woff2")',
                                              encoding="utf-8")
    assert preflight.check_fonts(tmp_path).status == "ok"


def test_the_missing_corpus_is_a_failure_with_the_command_to_fix_it(tmp_path):
    c = preflight.check_cases(tmp_path)
    assert c.status == "fail"
    assert "RetrievalUnavailable" in c.detail
    assert "extract_cases" in c.fix


def test_the_physician_check_reports_enabled_versus_registered():
    """三个数：本次模式参与几位 / 三列集注启用几位 / 注册表共几位。

    产品默认是 structured（五位全部参与综合分析，走 `in_synthesis` 而不是
    `enabled`），只报"启用 3 位"会与界面上的「五家综合」对不上。
    """
    from core.physicians import physicians_for_mode

    c = preflight.check_physicians()
    assert c.status == "ok"
    assert "启用" in c.detail and "注册表共" in c.detail
    assert "本次模式" in c.detail, "没报当前模式下到底几位参与"
    # 数字从注册表现算，不写死——注册表加一位这条测试不该跟着改
    from core.llm import s3_mode

    assert f"参与 {len(physicians_for_mode(s3_mode()))} 位" in c.detail


def test_the_credential_check_runs_the_same_checker_as_the_gate():
    """凭据核对这一项问的是 `collect_results.check`，不是另写一遍——
    否则运行时自检和 `--check` 会给出两个答案。"""
    import inspect

    src = inspect.getsource(preflight.check_credentials)
    assert "from scripts.collect_results import" in src
    assert "DEFAULT_CHECK_PATHS" in src


def test_the_json_output_is_machine_readable(capsys):
    code = preflight.main(["--json"])
    out = capsys.readouterr().out
    import json

    data = json.loads(out)
    assert {"checks", "n_fail", "n_warn"} <= set(data)
    assert data["checks"] and {"name", "status", "detail"} <= set(data["checks"][0])
    assert code in (0, 1)
