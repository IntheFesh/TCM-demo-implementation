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


def _segment_rows() -> list[tuple[str, str, str, str, str]]:
    """从脚本的 SEGMENTS 数组解析出段表**原文**（「预估调用数」那一格不解析，
    可能是 `auto:<文件>`）。**测试读的就是脚本里那一份**，不在测试里另抄一份
    ——抄一份就会漂。"""
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"SEGMENTS=\(\n(.*?)\n\)", text, re.S)
    assert block, "脚本里找不到 SEGMENTS 数组"
    rows = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        n, name, calls, gate, note = line.strip('"').split("|")
        rows.append((n, name, calls, gate, note))
    return rows


def _segments() -> list[tuple[str, str, int, str, str]]:
    """同上，但「预估调用数」解析成整数——解析走 `scripts/onsite_plan.resolve_calls`，
    跟剧本里 `resolve_calls()` 调的是同一个实现，不在测试里另算一遍。"""
    from scripts.onsite_plan import resolve_calls

    return [(n, name, resolve_calls(calls), gate, note)
            for n, name, calls, gate, note in _segment_rows()]


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_segment_numbers_are_contiguous_and_unique():
    nums = [int(r[0]) for r in _segments()]
    assert nums == list(range(len(nums))), nums


def test_segments_are_ordered_cheapest_first_and_dependencies_before_dependents():
    """段序按「依赖 + 成本」排：零调用的先跑（免费的问题先发现掉），最贵的放最后
    ——**但依赖优先于成本**。R8 之前这里断言"最后一段是最贵的"，那时段 5 写的是
    拍的 500；R8 按真实数据算出段 5 是 2167（六源预过滤后 2137 块 + 试抽），比段 7
    的 1200 贵，而段 5 不能挪到最后：`core/tools.py` 读 data/materia_medica.jsonl，
    段 6 的录制和段 7 的评测都要在药理层数据落盘**之后**跑，否则录下来的是
    「数据文件不存在」的工具输出。所以这条改成：前两段零调用；药理层抽取在录制和
    评测之前；最后一段是全套评测（没有任何段依赖它、且是不被依赖的段里最贵的）。
    这是一次有意的契约变更，不是把断言改绿。"""
    rows = _segments()
    assert rows[0][2] == 0 and rows[1][2] == 0, "前两段必须是零调用"
    names = [r[1] for r in rows]
    i_pharm = next(i for i, n in enumerate(names) if "药理层" in n)
    i_record = next(i for i, n in enumerate(names) if "录制" in n)
    i_eval = next(i for i, n in enumerate(names) if "评测" in n)
    assert i_pharm < i_record < i_eval, "药理层抽取必须在录制和评测之前（core/tools.py 读它的产出）"
    assert i_eval == len(rows) - 1, "全套评测必须是最后一段"
    calls = [r[2] for r in rows]
    # 不被依赖的段里（录制、评测），评测是最贵的那个
    assert calls[i_eval] > calls[i_record]


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


def test_epsilon_segment_estimate_is_read_from_the_file_not_written_down():
    """段 4 的预估调用数**不许写死**，必须是 `auto:eval/epsilon.json`。

    **这是一次有意的契约变更**（R10），不是把断言改绿：原来这条断言「剧本里写的数
    == epsilon.json 三段 llm_calls 之和」，两边都是数，文件一重跑（AutoDL 实测 212，
    仓库里这份是 215）就红，而红的时候要改的是剧本里那个写死的数——换成 212 之后
    下次重跑再红一次。所以判据从"两个数相等"改成"剧本那一格根本没有数"。

    两条断言分别钉住：
    1. 那一格是 `auto:eval/epsilon.json`——剧本里没有这个数字的副本；
    2. 解析出来的值等于文件里三段 llm_calls 之和——现读的确实是这份文件、这几段，
       不是碰巧给了个数（只有第 1 条的话，读法写错也发现不了）。
    """
    import json

    d = json.loads((ROOT / "eval" / "epsilon.json").read_text(encoding="utf-8"))
    measured = sum((d.get(k) or {}).get("llm_calls") or 0
                   for k in ("epsilon_online", "epsilon_s2", "epsilon_extract"))
    raw4 = [r for r in _segment_rows() if r[0] == "4"][0]
    assert raw4[2] == "auto:eval/epsilon.json", f"段 4 的预估又被写死成 {raw4[2]}"
    seg4 = [r for r in _segments() if r[0] == "4"][0]
    assert seg4[2] == measured, f"现读得到 {seg4[2]}，epsilon.json 三段之和 {measured}"


def test_no_segment_estimate_duplicates_a_number_that_lives_in_a_file():
    """说明文字里也不许再抄一遍那个现读的数——R10 之前段 4 的说明写「实测 215 次
    调用」，跟段表那一格同时过期。判据：`auto:` 的那些段，说明里不出现它的当前值。"""
    for n, name, calls, _gate, note in _segment_rows():
        if not calls.startswith("auto:"):
            continue
        from scripts.onsite_plan import resolve_calls

        assert str(resolve_calls(calls)) not in note, f"段 {n}（{name}）的说明里抄了这个数"


def test_record_segment_estimate_matches_the_record_plan():
    """段 6 的 278 次要跟 record_fixtures 的清单对得上（那份清单 R6-3 加过一条
    triage 场景，272 → 278）。"""
    import scripts.record_fixtures as rf

    planned = sum(s.estimated_calls for s in rf.build_plan())
    seg6 = [r for r in _segments() if r[0] == "6"][0]
    assert seg6[2] == planned, f"剧本写 {seg6[2]}，录制清单是 {planned}"


def test_segment_zero_checks_the_model_is_still_served():
    """**段 0 要在花第一分钱之前挡掉"模型名已下线"。** R10 的实测：deepseek-chat
    下线之后拿它发请求得到的是 HTTP 200 + 空响应体（不是 404），症状是每次调用空串
    → 校验失败 → 重试三次 → LLMError，一整段的钱白花，而错误信息里看不出根因。

    这条只解析脚本文本（真查清单要网络，`tests/` 不许联网）：段 0 里要有查 /models
    的那段、要能区分"查不到"（跳过，不算失败）和"清单里没有它"（`SystemExit(1)`）。
    三个分支的真实行为在沙盒里用一个本地假服务端跑过，见 SOURCES.md 第 51 条。
    """
    text = SCRIPT.read_text(encoding="utf-8")
    seg0 = text[text.index("seg_0() {"):text.index("seg_1() {")]
    assert "/models" in seg0, "段 0 没有查模型清单"
    assert "LLM_MODEL" in seg0 and "SystemExit(1)" in seg0
    assert "跳过这一项" in seg0, "查不到清单必须跳过而不是拦住后面所有段"
    # 零调用这件事要保住：查清单不是 LLM 调用，段 0 的预估仍然是 0
    seg0_row = [r for r in _segments() if r[0] == "0"][0]
    assert seg0_row[2] == 0


# ---------- 失败预案 ----------


def test_troubleshooting_first_entry_is_about_silence_and_says_it_is_no_longer_normal():
    """**关于"静默"的那一条必须在最前面**——两次代价都出在这上面：一次误判卡死
    杀了三个正常进程，一次反过来（DeepSeek 真挂死 46 分钟没人发现）。

    **R9 起断言反过来了**：原来这条钉的是"静默很久**是正常的**"，因为当时长任务
    真的不出声；现在所有长任务都有进度条和心跳（`core/progress.py`），"静默"就
    等于真卡住，文档第 0 条据此改写。这是有意的契约变更——判据仍然是"第一条讲
    静默"，只是结论从"正常"变成"不正常"，断言跟着反。（不改它也会碰巧过：新标题里
    "静默"和"正常"两个词都在——那种"碰巧还绿"正是必须显式处理这条的理由。）"""
    text = DOC.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.+)$", text, re.M)
    assert headings, "文档里没有二级标题"
    assert "静默" in headings[0], headings[0]
    assert "不再是正常" in headings[0], headings[0]
    section = text[text.index(headings[0]):text.index("## 1. ")]
    assert "心跳" in section, "第 0 条必须讲心跳——没有它就没有新判据"
    assert "连心跳都没有" in section


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
