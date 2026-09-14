"""scripts/run_onsite.sh 与 docs/onsite_troubleshooting.md 的离线测试。

bash 脚本也要有测试：上机剧本写错一个模块路径，代价是在真机上跑到那一段才发现，
而那时候前面几段的钱已经花了。所以这里**解析脚本本身**——段表连不连续、人工卡点
在不在该在的位置、里面 `python -m` 的每个模块是不是真的存在。
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_onsite.sh"
DOC = ROOT / "docs" / "onsite_troubleshooting.md"


def _segments() -> list[tuple[str, str, int, str, str]]:
    """从脚本的 SEGMENTS 数组解析出段表。**测试读的就是脚本里那一份**，
    不在测试里另抄一份——抄一份就会漂。"""
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"SEGMENTS=\(\n(.*?)\n\)", text, re.S)
    assert block, "脚本里找不到 SEGMENTS 数组"
    rows = []
    for line in block.group(1).splitlines():
        line = line.strip().strip('"')
        if not line:
            continue
        n, name, calls, gate, note = line.split("|")
        rows.append((n, name, int(calls), gate, note))
    return rows


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_segment_numbers_are_contiguous_and_unique():
    nums = [int(r[0]) for r in _segments()]
    assert nums == list(range(len(nums))), nums


def test_segments_are_ordered_cheapest_first_and_most_expensive_last():
    """段序按「依赖 + 成本」排：零调用的先跑（免费的问题先发现掉），
    最贵的放最后。这条钉住那个顺序不会被随手改乱。"""
    rows = _segments()
    assert rows[0][2] == 0 and rows[1][2] == 0, "前两段必须是零调用"
    calls = [r[2] for r in rows]
    assert calls[-1] == max(calls), "最贵的一段必须在最后"


def test_exactly_two_human_gates_and_they_are_segments_3_and_5():
    """两处人工卡点存在的理由是：闸门没过就往下跑，后面几百次调用全部白花。"""
    gated = [r[0] for r in _segments() if r[3] == "YES"]
    assert gated == ["3", "5"], gated


def test_every_python_module_the_runbook_invokes_actually_exists():
    """上机剧本写错一个模块路径，要到真机上跑到那一段才发现。"""
    text = SCRIPT.read_text(encoding="utf-8")
    modules = set(re.findall(r"python -m ([\w.]+)", text))
    assert modules, "脚本里一个 python -m 都没有？"
    missing = []
    for mod in sorted(modules):
        if mod == "pytest":
            continue
        rel = Path(mod.replace(".", "/"))
        if not ((ROOT / rel).with_suffix(".py").exists() or (ROOT / rel / "__init__.py").exists()):
            missing.append(mod)
    assert not missing, f"剧本里这些模块不存在：{missing}"


def test_every_shell_script_the_runbook_invokes_exists():
    text = SCRIPT.read_text(encoding="utf-8")
    for rel in set(re.findall(r"bash (scripts/[\w./-]+\.sh)", text)):
        assert (ROOT / rel).exists(), rel


def test_each_segment_prints_a_timestamp_and_an_exit_code():
    """「段末打时间戳和退出码」是可续跑的前提：断了之后要能一眼看出断在哪。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count("date -Is") >= 2          # 段首 + 段末
    assert "退出码 $rc" in text


def test_a_failing_segment_does_not_stop_the_later_ones():
    """段与段之间只有先后没有依赖崩塌——段 5 挂了，段 6 的录制照样该跑。
    所以 run_segment 末尾无条件 `return 0`，而不是 `set -e` 掉整个脚本。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -e\n" not in text and "set -euo" not in text
    assert re.search(r"RESULTS\+=\(.*\)\n\s*return 0", text), "run_segment 末尾要无条件 return 0"


def test_dry_run_prints_the_plan_and_runs_nothing():
    """开跑前先看每段的预估调用数和成本，决定跑到哪一段。"""
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "预估调用" in out.stdout
    assert "什么都没跑" in out.stdout
    for n, name, *_ in _segments():
        assert name in out.stdout


def test_dry_run_total_matches_the_segment_table():
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    total = sum(r[2] for r in _segments())
    assert f"预估 {total} 次调用" in out.stdout


def test_unknown_argument_fails_loudly():
    out = subprocess.run(["bash", str(SCRIPT), "--nope"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 2 and "未知参数" in out.stderr


def test_epsilon_segment_estimate_comes_from_the_measured_run():
    """段 4 的 215 次不是拍脑袋：`eval/epsilon.json` 里三段 llm_calls 之和。
    哪天那份文件重跑了、数变了，这条会红——那时候要改的是剧本里的估算。"""
    import json

    d = json.loads((ROOT / "eval" / "epsilon.json").read_text(encoding="utf-8"))
    measured = sum((d.get(k) or {}).get("llm_calls") or 0
                   for k in ("epsilon_online", "epsilon_s2", "epsilon_extract"))
    seg4 = [r for r in _segments() if r[0] == "4"][0]
    assert seg4[2] == measured, f"剧本写 {seg4[2]}，epsilon.json 实测 {measured}"


def test_record_segment_estimate_matches_the_record_plan():
    """段 6 的 278 次要跟 record_fixtures 的清单对得上（那份清单 R6-3 加过一条
    triage 场景，272 → 278）。"""
    import scripts.record_fixtures as rf

    planned = sum(s.estimated_calls for s in rf.build_plan())
    seg6 = [r for r in _segments() if r[0] == "6"][0]
    assert seg6[2] == planned, f"剧本写 {seg6[2]}，录制清单是 {planned}"


# ---------- 失败预案 ----------


def test_troubleshooting_first_entry_is_the_silence_is_normal_one():
    """**「静默很久是正常的」必须在最前面。** 上一轮因为误判卡死，杀了三个正常
    运行的进程，浪费一小时和几百次调用——这条排第二都嫌晚。"""
    text = DOC.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings, "文档里没有二级标题"
    assert "静默" in headings[0] and "正常" in headings[0], headings[0]


def test_troubleshooting_says_not_to_ps_or_kill():
    text = DOC.read_text(encoding="utf-8")
    assert "不要用 `ps` / `kill`" in text or "不要 ps/kill" in text


def test_troubleshooting_covers_every_case_the_runbook_can_hit():
    """剧本里出现的这几件事，预案里都要有对应条目。"""
    text = DOC.read_text(encoding="utf-8")
    for keyword in ("RETRIEVER_MODE", "LLMTruncatedError", "VLLM_GUIDED_JSON_KEY",
                    "graph_stats", "collect_results --check", "OOM", "replay"):
        assert keyword in text, keyword


def test_troubleshooting_thresholds_match_the_code():
    """预案里写的两个阈值必须跟代码里的一致，不能各写各的。"""
    from core.batch import FAILURE_RATE_WARNING_THRESHOLD
    from core.llm import TRUNCATION_MIN_LENGTH

    text = DOC.read_text(encoding="utf-8")
    assert f"{TRUNCATION_MIN_LENGTH} 字符" in text
    assert f"{FAILURE_RATE_WARNING_THRESHOLD:.0%}" in text


@pytest.mark.parametrize("flag", ["--from", "--only"])
def test_resume_flags_require_a_segment_number(flag):
    out = subprocess.run(["bash", str(SCRIPT), flag],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode != 0


def test_only_flag_runs_a_single_segment(tmp_path):
    """--only 3 只跑段 3。这里用 --dry-run 之外的路径验不现实（段 3 要真调用），
    所以验的是参数解析：--only 之后脚本不再按 --from 的规则跳过。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'if [ -n "$ONLY" ]; then' in text
    assert '[ "$n" = "$ONLY" ] || continue' in text
    assert '[ "$n" -lt "$FROM" ]' in text


def test_python_in_tests_can_import_the_record_plan():
    """上一条测试 import 了 scripts.record_fixtures，这里确认它在这台环境上
    真的 import 得进来（它模块顶层不加载任何模型/大文件）。"""
    assert sys.modules.get("scripts.record_fixtures") or True
    import scripts.record_fixtures  # noqa: F401
