"""R25：演示自检（`scripts/demo_preflight.py`）与材料索引（`docs/MATERIALS.md`）。

演示前五分钟没人会认真读 `env | grep` 的输出——这就是把 DEMO.md 那份清单
做成一个**带退出码**的检查器的全部理由。这里测的是：
  · 三档结果（ok / warn / fail）各自意味着什么、怎么影响退出码；
  · 每条 fail 都带一句怎么修（一条只说"有问题"的检查等于没检查）；
  · 那几个"不报错但让结果跟你讲的话对不上"的环境变量真的被查了；
  · 材料索引里的每个数都带凭据（它进了同一个核对器）。
"""
from __future__ import annotations

from pathlib import Path

from scripts import demo_preflight as dp

ROOT = Path(__file__).resolve().parent.parent


# ---------- 三档与退出码 ----------


def test_the_three_statuses_map_to_exit_codes():
    """fail → 1；warn 默认不影响退出码，`--strict` 下影响。
    warn 不拦是有意的：**有时就是要现场跑真实调用**，而那时 LLM_MODE 不是 replay。"""
    ok = dp.Report([dp.Check("a", "ok", "")])
    warn = dp.Report([dp.Check("a", "warn", "")])
    fail = dp.Report([dp.Check("a", "fail", "")])
    assert ok.exit_code() == 0 and ok.exit_code(strict=True) == 0
    assert warn.exit_code() == 0 and warn.exit_code(strict=True) == 1
    assert fail.exit_code() == 1 and fail.exit_code(strict=True) == 1


def test_every_failing_check_carries_a_fix():
    """一条只说"有问题"的检查，在演示前五分钟等于没有这条检查。"""
    report = dp.run_all()
    for c in report.checks:
        if c.status == "fail":
            assert c.fix, f"{c.name} 是 fail 但没说怎么修"


def test_there_is_no_skipped_status():
    """**没有"跳过"这一档**：查不了的东西要么归 warn 并说明"这台机器查不了"，
    要么就别列进来。一条静默跳过的检查比没有这条检查更糟。"""
    report = dp.run_all()
    assert {c.status for c in report.checks} <= {"ok", "warn", "fail"}


# ---------- 环境残留：这个项目真实踩过的那一类 ----------


def test_the_leftover_env_vars_are_the_ones_that_change_behaviour_silently():
    """七个变量，每个都配一句"留着会怎样"。只说"请清掉"的话，人会以为是洁癖。"""
    assert set(dp.LEFTOVER_ENV) == {
        "RETRIEVER_MODE", "USE_REACT", "EVAL_MODE",
        "S3_BEST_OF_N", "S3_REASONING_EFFORT", "S3_THINKING", "LORA_DIR"}
    for var, why in dp.LEFTOVER_ENV.items():
        assert len(why) > 8, f"{var} 的理由太短，读起来像洁癖"
    # EVAL_MODE 那一条要说出最严重的后果
    assert "安全否决不再中止" in dp.LEFTOVER_ENV["EVAL_MODE"]


def test_a_leftover_retriever_mode_is_a_failure_not_a_warning():
    """**这是这个项目真实发生过的事故形态**：残留不会报错，
    只会让演示跑的不是默认模式，而页面上没有任何提示。"""
    checks = dp.check_leftover_env({"RETRIEVER_MODE": "bm25"})
    assert len(checks) == 1
    assert checks[0].status == "fail"
    assert "bm25" in checks[0].detail
    assert checks[0].fix == "unset RETRIEVER_MODE"


def test_a_clean_env_says_so_instead_of_staying_silent():
    checks = dp.check_leftover_env({})
    assert len(checks) == 1 and checks[0].status == "ok"


def test_the_expected_env_is_a_suggestion_not_a_gate():
    """LLM_MODE/FAST_MODE 只提醒——有时就是要现场跑真实调用。"""
    checks = dp.check_expected_env({"LLM_MODE": "api"})
    by_name = {c.name: c for c in checks}
    assert by_name["演示设置 LLM_MODE"].status == "warn"
    assert by_name["演示设置 LLM_MODE"].fix == "export LLM_MODE=replay"
    good = dp.check_expected_env({"LLM_MODE": "replay", "FAST_MODE": "1"})
    assert all(c.status == "ok" for c in good)


# ---------- 逐项检查 ----------


def test_the_quota_check_uses_the_computed_calls_per_consult(monkeypatch):
    """额度折算要问 `calls_per_consult()`，不是写死的 5。R22 之后一次问诊
    是 11 次调用，写死旧值会让"今日约剩 N 次"虚报一倍。"""
    from core.usage import calls_per_consult

    monkeypatch.delenv("QUOTA_PER_IP_DAILY_CALLS", raising=False)
    assert str(calls_per_consult()) in dp.check_quota().detail
    monkeypatch.setenv("QUOTA_PER_IP_DAILY_CALLS", "5")
    bad = dp.check_quota()
    assert bad.status == "fail" and "0 次问诊" in bad.detail
    monkeypatch.setenv("QUOTA_PER_IP_DAILY_CALLS", str(calls_per_consult() * 5))
    assert dp.check_quota().status == "ok"


def test_the_prefix_cache_check_admits_it_cannot_see_the_remote(monkeypatch):
    """远端缓存的死活这台机器查不到（那是 DeepSeek 的内部状态）。
    **如实归 warn 并给预热命令，不假装查过。**"""
    monkeypatch.delenv("RETRIEVER_MODE", raising=False)
    c = dp.check_prefix_cache()
    assert c.status == "warn"
    assert "查不到" in c.detail and "预热" in c.detail


def test_the_prefix_cache_check_says_when_the_mode_makes_it_moot(monkeypatch):
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    c = dp.check_prefix_cache()
    assert "没有前缀缓存这回事" in c.detail


def test_the_font_check_distinguishes_three_states(tmp_path):
    """三种状态分开：没生成 / 生成了但没接进 CSS / 接好了。
    中间那种最容易漏——文件在，但页面还在从 CDN 取。"""
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "app.css").write_text("@font-face { src: url(https://cdn…) }",
                                              encoding="utf-8")
    assert dp.check_fonts(tmp_path).status == "warn"

    fonts = tmp_path / "web" / "vendor" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "noto-serif-sc-400-subset.woff2").write_bytes(b"x")
    mid = dp.check_fonts(tmp_path)
    assert mid.status == "warn" and "还指着 CDN" in mid.detail

    (tmp_path / "web" / "app.css").write_text('src: url("vendor/fonts/a.woff2")',
                                              encoding="utf-8")
    assert dp.check_fonts(tmp_path).status == "ok"


def test_the_missing_corpus_is_a_failure_with_the_command_to_fix_it(tmp_path):
    c = dp.check_cases(tmp_path)
    assert c.status == "fail"
    assert "RetrievalUnavailable" in c.detail
    assert "extract_cases" in c.fix


def test_the_physician_check_reports_enabled_versus_registered():
    """启用几位 / 注册表共几位——两个数分开报。R18 之后注册表有五位、
    启用三位，只报一个数说不清楚。"""
    c = dp.check_physicians()
    assert c.status == "ok"
    assert "启用" in c.detail and "注册表共" in c.detail


def test_the_credential_check_runs_the_same_checker_as_the_gate():
    """凭据核对这一项问的是 `collect_results.check`，不是另写一遍——
    否则演示自检和 `--check` 会给出两个答案。"""
    import inspect

    src = inspect.getsource(dp.check_credentials)
    assert "from scripts.collect_results import" in src
    assert "DEFAULT_CHECK_PATHS" in src


def test_the_json_output_is_machine_readable(capsys):
    code = dp.main(["--json"])
    out = capsys.readouterr().out
    import json

    data = json.loads(out)
    assert {"checks", "n_fail", "n_warn"} <= set(data)
    assert data["checks"] and {"name", "status", "detail"} <= set(data["checks"][0])
    assert code in (0, 1)


# ---------- 材料索引 ----------


def test_the_materials_doc_is_in_the_credential_checker():
    """竞赛材料里的每个数都要可核——那份文件的全部作用就是"把这些话讲给别人听"，
    而一句没有凭据的话在那种场合代价最大。"""
    from scripts.collect_results import DEFAULT_CHECK_PATHS, check

    names = {p.name for p in DEFAULT_CHECK_PATHS}
    assert "MATERIALS.md" in names
    path = ROOT / "docs" / "MATERIALS.md"
    result = check(path.read_text(encoding="utf-8"))
    assert result["mismatches"] == [], result["mismatches"]
    assert result["missing_in_line"] == [], result["missing_in_line"]


def test_the_materials_doc_says_what_cannot_be_claimed():
    """**这一节比"能讲什么"更重要**：讲的时候主动说，比被问出来强。"""
    text = (ROOT / "docs" / "MATERIALS.md").read_text(encoding="utf-8")
    section = text[text.index("## 四、**不能讲**"):]
    for must in ("full_context", "best-of-N", "LoRA", "药理层", "断网", "E2", "deepseek-chat"):
        assert must in section, f"「不能讲」那一节漏了 {must}"
    assert "两轮结论相反" in section


def test_the_materials_doc_points_at_the_preflight():
    text = (ROOT / "docs" / "MATERIALS.md").read_text(encoding="utf-8")
    assert "scripts.demo_preflight" in text
    assert "--strict" in text


# ---------- 上机剧本的段 9 ----------


def test_the_runbook_has_a_segment_for_the_r21_to_r24_items():
    """R21~R24 攒下来的上机项要有地方跑，否则它们只存在于四份报告的
    ⏳ 清单里——而那四份清单没有退出码。"""
    src = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    assert "seg_9()" in src
    assert "context_prefix --report" in src
    assert "bench_consult --backend real --repeat 2" in src
    assert "RETRIEVER_MODE=full_context python -m eval.run_eval --e3 --e4" in src
    assert "subset_fonts" in src


def test_the_new_segment_says_its_numbers_are_not_comparable():
    """R21 换了检索默认、R22 换了采样和推理档——**三个旋钮都换了实验条件**。
    段 9 跑出来的 E3/E4 是新起的一行，不是覆盖 top3 系那张表。"""
    src = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    body = src[src.index("seg_9()"):src.index("run_segment()")]
    assert "不可比" in body
    assert "并列报，不相减" in body
