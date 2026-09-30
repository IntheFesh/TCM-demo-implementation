"""部署前自检（`scripts/preflight_deploy.py`）。"""
from __future__ import annotations

import re

from scripts import preflight_deploy as pd


def _one(fn, *args) -> pd.Check:
    rep = pd.Report()
    fn(rep, *args)
    assert len(rep.checks) >= 1
    return rep.checks[0]


# ---------- 退出码 ----------


def test_exit_code_distinguishes_blocking_from_warning():
    rep = pd.Report()
    rep.add(pd.Check("a", "a", True, ok=True))
    assert rep.exit_code() == 0
    rep.add(pd.Check("b", "b", False, ok=False))
    assert rep.exit_code() == 2
    rep.add(pd.Check("c", "c", True, ok=False))
    assert rep.exit_code() == 1


def test_a_skipped_check_does_not_count_as_failed():
    rep = pd.Report()
    rep.add(pd.Check("a", "a", True, ok=False, skipped="不适用"))
    assert rep.exit_code() == 0


# ---------- 回放 fixture ----------


def test_replay_fixtures_are_looked_up_where_the_replay_backend_reads_them(monkeypatch, tmp_path):
    """目录与 `ReplayBackend` 用的是同一个：`core.llm_replay.fixtures_dir()`。"""
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("REPLAY_FIXTURES_DIR", str(tmp_path))
    (tmp_path / "S2Output_0123456789ab.json").write_text("{}", encoding="utf-8")
    c = _one(pd.check_replay_fixtures)
    assert c.ok and c.measured == "1 个 fixture"


def test_meta_files_alone_do_not_count_as_fixtures(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("REPLAY_FIXTURES_DIR", str(tmp_path))
    (tmp_path / "_baseline.json").write_text("{}", encoding="utf-8")
    c = _one(pd.check_replay_fixtures)
    assert not c.ok and "record_fixtures" in c.fix


def test_replay_fixtures_are_skipped_outside_replay_mode(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "api")
    c = _one(pd.check_replay_fixtures)
    assert c.skipped


# ---------- 模型后端 ----------


def test_the_fix_for_a_missing_key_only_names_modes_that_exist(monkeypatch):
    """修复建议里提到的 LLM_MODE 取值必须是 `get_backend()` 认识的。"""
    monkeypatch.setenv("LLM_MODE", "api")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    c = _one(pd.check_llm_config, True)
    assert not c.ok
    modes = set(re.findall(r"LLM_MODE=(\w+)", c.fix))
    assert modes and modes <= {"api", "local", "local_inproc", "replay"}, modes


# ---------- 并发上限 ----------


def test_concurrency_within_range_passes(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_CONSULTS", "4")
    assert _one(pd.check_concurrency_config).ok


def test_concurrency_out_of_range_warns_without_blocking(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_CONSULTS", "32")
    c = _one(pd.check_concurrency_config)
    assert not c.ok and not c.blocking
    assert "loadtest" in c.fix
