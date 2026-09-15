"""R19：ε 按思考设置分文件 + 拒绝跨设置覆盖。

零 LLM 调用：测的是路径解析和那道覆盖闸门，不是 ε 本身。
"""
from __future__ import annotations

import json

from offline.estimate_epsilon import (
    DEFAULT_OUT_PATH,
    OUT_PATH_BY_S3_THINKING,
    refuse_cross_thinking_overwrite,
    resolve_out_path,
)


def test_default_setting_keeps_the_existing_file_name():
    """改名会让 eval/RESULTS.md 里一批 `epsilon.json:…` 的凭据记号全部失效。"""
    assert OUT_PATH_BY_S3_THINKING["enabled"] == DEFAULT_OUT_PATH
    assert DEFAULT_OUT_PATH.name == "epsilon.json"


def test_disabled_setting_gets_its_own_file():
    """两套数不可比，共用一个文件名就等于后跑的那套悄悄覆盖前一套。"""
    assert OUT_PATH_BY_S3_THINKING["disabled"].name == "epsilon_s3_disabled.json"
    assert OUT_PATH_BY_S3_THINKING["disabled"] != DEFAULT_OUT_PATH


def test_explicit_out_wins_over_the_setting(tmp_path):
    p = tmp_path / "custom.json"
    assert resolve_out_path(p, "disabled") == p


def test_unknown_setting_falls_back_to_the_default_path():
    """拼一个 epsilon_s3_<乱码>.json 出来的话，没人知道那个文件是什么。"""
    assert resolve_out_path(None, "whatever") == DEFAULT_OUT_PATH


def test_overwrite_is_refused_across_thinking_settings(tmp_path):
    """这道闸门防的是：关掉思考重跑、覆盖同一个文件名，而 RESULTS.md 里的凭据
    记号文件名和键名都没变，`--check` 照样绿——数已经换了一套设置。"""
    p = tmp_path / "epsilon.json"
    p.write_text(json.dumps({"s3_thinking": "enabled"}), encoding="utf-8")
    msg = refuse_cross_thinking_overwrite(p, "disabled", force=False)
    assert msg and "拒绝覆盖" in msg
    assert "--force" in msg and "不可比" in msg


def test_overwrite_is_allowed_within_the_same_setting(tmp_path):
    """同一套设置重跑一次是正常动作，不该被拦。"""
    p = tmp_path / "epsilon.json"
    p.write_text(json.dumps({"s3_thinking": "enabled"}), encoding="utf-8")
    assert refuse_cross_thinking_overwrite(p, "enabled", force=False) is None


def test_force_overrides_the_guard(tmp_path):
    p = tmp_path / "epsilon.json"
    p.write_text(json.dumps({"s3_thinking": "enabled"}), encoding="utf-8")
    assert refuse_cross_thinking_overwrite(p, "disabled", force=True) is None


def test_a_pre_r19_file_without_the_field_is_not_blocked(tmp_path, capsys):
    """R19 之前跑的文件没有这个字段。拿缺失字段当"设置不同"拦下来只会让人
    以为闸门坏了——但要说一句。"""
    p = tmp_path / "epsilon.json"
    p.write_text(json.dumps({"epsilon_online": {"mean": 0.26}}), encoding="utf-8")
    assert refuse_cross_thinking_overwrite(p, "disabled", force=False) is None
    assert "R19 之前跑的" in capsys.readouterr().err


def test_a_missing_or_unreadable_file_is_not_blocked(tmp_path):
    assert refuse_cross_thinking_overwrite(tmp_path / "nope.json", "disabled", False) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert refuse_cross_thinking_overwrite(bad, "disabled", False) is None


def test_the_thinking_setting_is_recorded_in_the_output():
    """「这个 ε 是哪套设置跑的」跟 ε 本身一样需要凭据。"""
    src = (DEFAULT_OUT_PATH.parent.parent / "offline" / "estimate_epsilon.py").read_text(
        encoding="utf-8")
    assert '"s3_thinking": s3_thinking(),' in src
    assert '"thinking_by_step": thinking_by_step(),' in src


def test_both_settings_have_evidence_keys():
    """两套数是两个凭据键，不是同一个键的两次取值——同一个键会让 RESULTS.md
    那一格说不清它报的是哪套设置。"""
    from scripts.collect_results import EVIDENCE
    assert "epsilon_online.s3_thinking" in EVIDENCE
    for suffix in ("mean", "p95", "llm_calls", "s3_thinking"):
        assert f"epsilon_s3_disabled.{suffix}" in EVIDENCE
    assert EVIDENCE["epsilon_s3_disabled.mean"][0] == "epsilon_s3_disabled.json"


def test_onsite_segment_4_runs_both_settings():
    """并列对照要真的跑两次。只在文档里写"也可以关掉思考跑一次"等于没跑。"""
    src = (DEFAULT_OUT_PATH.parent.parent / "scripts" / "run_onsite.sh").read_text(
        encoding="utf-8")
    body = src[src.index("seg_4() {"):]
    body = body[:body.index("\n}\n")]
    assert body.count("offline.estimate_epsilon") == 2
    assert "S3_THINKING=disabled" in body
    # disabled 那一套挂了不该让段 4 整段算失败——默认那一套已经落盘了
    assert "段 4 不因此算失败" in body
