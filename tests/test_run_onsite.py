"""scripts/run_onsite.sh 与 docs/onsite_troubleshooting.md 的离线测试。

bash 脚本也要有测试：上机剧本写错一个模块路径，代价是在真机上跑到那一段才发现，
而那时候前面几段的钱已经花了。所以这里**解析脚本本身**——段表连不连续、人工卡点
在不在该在的位置、里面 `python -m` 的每个模块是不是真的存在。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_onsite.sh"
DOC = ROOT / "docs" / "onsite_troubleshooting.md"


def _segment_rows() -> list[tuple[str, str, str, str, str]]:
    """段表**原文**里这几条测试关心的那几格：`(段号, 名称, 预估调用数, 人工卡点, 说明)`。

    **R28 起解析走 `scripts.onsite_plan.parse_segments`**，不在这里写正则：
    段表这一轮从五格变成七格（多了执行序和检索模式），而原来每个测试文件各写一个
    正则去抠它——加一格就要同时改好几处，漏改的那处不会报错、只会少断言一件事。
    这里只是把七格里这几条判据用得到的那几格拆出来，执行序和模式有它们自己的
    判据文件（tests/test_onsite_order_and_mode.py）。
    """
    from scripts.onsite_plan import parse_segments

    rows = parse_segments(SCRIPT.read_text(encoding="utf-8"))
    return [(r["num"], r["name"], r["calls"], r["gate"], r["note"]) for r in rows]


def _segment_rows_in_execution_order() -> list[tuple[str, str, str, str, str]]:
    """同上，但按**执行序**排。段号顺序和执行顺序 R28 之后是两回事。"""
    from scripts.onsite_plan import segments_in_execution_order

    rows = segments_in_execution_order(SCRIPT.read_text(encoding="utf-8"))
    return [(r["num"], r["name"], r["calls"], r["gate"], r["note"]) for r in rows]


def _segments() -> list[tuple[str, str, int, str, str]]:
    """同上，但「预估调用数」解析成整数——解析走 `scripts/onsite_plan.resolve_calls`，
    跟剧本里 `resolve_calls()` 调的是同一个实现，不在测试里另算一遍。"""
    from scripts.onsite_plan import resolve_calls

    return [(n, name, resolve_calls(calls), gate, note)
            for n, name, calls, gate, note in _segment_rows()]


def _segments_in_execution_order() -> list[tuple[str, str, int, str, str]]:
    from scripts.onsite_plan import resolve_calls

    return [(n, name, resolve_calls(calls), gate, note)
            for n, name, calls, gate, note in _segment_rows_in_execution_order()]


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_segment_numbers_are_contiguous_and_unique():
    nums = [int(r[0]) for r in _segments()]
    assert nums == list(range(len(nums))), nums


def test_segments_are_ordered_cheapest_first_and_dependencies_before_dependents():
    """段序按「依赖 + 成本」排：零调用的先跑（免费的问题先发现掉），最贵的排在后面
    ——**但依赖优先于成本**。R8 之前这里断言"最后一段是最贵的"，那时段 5 写的是
    拍的 500；R8 按真实数据算出段 5 是 2167（六源预过滤后 2137 块 + 试抽），比段 7
    的 1200 贵，而段 5 不能挪到最后：`core/tools.py` 读 data/materia_medica.jsonl，
    段 6 的录制和段 7 的评测都要在药理层数据落盘**之后**跑，否则录下来的是
    「数据文件不存在」的工具输出。所以这条改成：前两段零调用；药理层抽取在录制和
    评测之前；最后一段是全套评测（没有任何段依赖它、且是不被依赖的段里最贵的）。
    这是一次有意的契约变更，不是把断言改绿。"""
    # **R28 起按执行序判**，不按段号顺序：段号是身份，执行序才是"先跑谁"。
    rows = _segments_in_execution_order()
    assert rows[0][2] == 0 and rows[1][2] == 0, "前两段必须是零调用"
    names = [r[1] for r in rows]
    i_pharm = next(i for i, n in enumerate(names) if "药理层" in n)
    i_record = next(i for i, n in enumerate(names) if "录制" in n)
    i_eval = next(i for i, n in enumerate(names) if "评测" in n)
    assert i_pharm < i_record < i_eval, "药理层抽取必须在录制和评测之前（core/tools.py 读它的产出）"
    calls = [r[2] for r in rows]
    # 不被依赖的段里（录制、评测），评测是最贵的那个
    assert calls[i_eval] > calls[i_record]
    # **R11 起最后一段不再是全套评测，是性能基准**——这是有意的契约变更，不是把断言改绿。
    # 原断言是 `i_eval == len(rows) - 1`（"全套评测必须是最后一段"），它当初钉的其实是
    # "最贵的那一段放最后，前面的段挂了也不至于先把钱花光"。段 8 性能基准只有 38 次调用，
    # 不违反那个本意；而它必须排在段 5 之后有硬依据：core/tools.py 读段 5 落盘的
    # data/materia_medica.jsonl，开 ReAct 的那一次基准在段 5 之前量到的是"工具返回
    # 数据文件不存在"的耗时，跟真实形态不是同一个系统。所以判据改成两条：
    # 评测仍是最贵的一段，且性能基准在它之后。
    i_bench = next(i for i, n in enumerate(names) if "性能基准" in n)
    assert calls[i_eval] > calls[i_bench], "不被依赖的三段里，评测仍该是最贵的那个"
    assert i_pharm < i_bench, "性能基准必须在药理层抽取之后（开 ReAct 那次要读它的产出）"
    # **R25 起最后一段是 R21~R24 的上机项**（又一次有意的契约变更，理由跟 R11 那次
    # 同一形状）：原断言是「性能基准必须是最后一段」，它钉的本意是"最贵的放后面、
    # 依赖别人的放后面"。段 9 只有 33 次调用，不违反前半句；而它必须在最后有硬依据
    # ——它要的东西前面几段都得先有（前缀规模要 cases.json、命中率要真实 API、
    # full_context 下的 E3/E4 要评测框架跑通、字体子集化要联网取原始字体）。
    i_r21 = next(i for i, n in enumerate(names) if "R21~R24" in n)
    assert calls[i_eval] > calls[i_r21], "评测仍是最贵的一段（按调用数）"
    # **R28 把这一条翻了过来**：R25 那版写的是「性能基准在段 9 之前」，理由是
    # "段 9 要的东西前面几段都得先有"。R28 发现那个理由只对段 9 的**四个子步骤中的
    # 两个**成立（前缀规模要 cases.json、字体要联网），而另外两个（缓存命中率、
    # full_context 的 E3/E4）只要 cases.json + 真实 key——它们决定的是
    # **后面每一段按哪套单价花钱**，晚跑一小时的代价是前面几千次调用按错的默认
    # 配置花掉。所以段 9 提到了所有花钱的段之前，性能基准反而在它后面。
    # **R26 起最后一段是蒸馏**（第三次同一形状的契约变更）：原断言是「R21~R24
    # 那一段必须是最后一段」。段 10 排在它后面有两条硬依据：它要 cases.json、
    # 要真实 API（跟段 9 同样的前提），而且**整段可以不做**——可选的段排在必做的
    # 段后面，中途停下来不会漏掉任何必做项。
    # **R38 起按执行序断言，不按表里的行号**：段表按段号排（段号是身份，
    # 动它等于让状态文件作废），而"谁最后跑"写在执行序那一格里。R38 的段 11
    # 段号最大但必做，所以它排在蒸馏之前——可选的段仍然是最后一个执行的。
    in_order = _segments_in_execution_order()
    assert "蒸馏" in in_order[-1][1], f"最后一个执行的段是 {in_order[-1][1]}，不是蒸馏"
    # **R28：段 9 从最后挪到了最前面（执行序 3）**，理由不是成本是依赖——
    # 它验的是「full_context 还能不能当默认」，闸门不过后面每一段的成本口径都变。
    # 所以这条断言翻了个方向：以前是 i_r21 < i_last（它在蒸馏之前），
    # 现在是它在**所有花钱的段**之前。
    assert i_r21 < i_bench and i_r21 < i_eval and i_r21 < i_pharm


def test_exactly_two_human_gates_and_they_are_segments_3_and_5():
    """人工卡点存在的理由是：闸门没过就往下跑，后面几百次调用全部白花。

    **有意的契约变更（R26）**：卡点从 3/5 两处变成 3/5/10 三处。段 10 的卡点
    不是因为"它可能失败"，而是因为它**花的钱超过 ¥30 那条线**（§0.5 第 2 条
    第三款），而超过那条线的动作必须有人点头。判据保持"逐个列出是哪几段"
    而不是放宽成"至少有两处"——后者再也发现不了"某段偷偷去掉了卡点"。
    """
    gated = [r[0] for r in _segments() if r[3] == "YES"]
    assert gated == ["3", "5", "10"], gated


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
    # 判据改成「run_segment 的**最后一条语句**是无条件 return 0」。
    # 原来写的是 `RESULTS+=(...)` 紧接着 `return 0` 的正则——R19 在两者之间插了
    # 一句 record_segment（每段的退出码要落盘，见 --resume），正则就不匹配了。
    # 这不是放松：盯着"函数末尾是不是无条件 return 0"比盯着"它前一行是什么"
    # 更贴近这条约束本身。
    body = text[text.index("run_segment() {"):]
    body = body[:body.index("\n}\n")]
    last = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")][-1]
    assert last.startswith("return 0"), f"run_segment 末尾不是无条件 return 0，是：{last}"


def test_dry_run_prints_the_plan_and_runs_nothing():
    """开跑前先看每段的预估调用数和成本，决定跑到哪一段。"""
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "预估调用" in out.stdout
    assert "执行序" in out.stdout, "R28 起清单要同时打段号和执行序"
    assert "什么都没跑" in out.stdout
    for n, name, *_ in _segments():
        assert name in out.stdout


def test_dry_run_total_matches_the_segment_table():
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    total = sum(r[2] for r in _segments())
    # R28 起总计那行按模式分开算，措辞跟着变：`合计 N 次 ≈ ¥X`。
    # **仍然是"清单上每段之和"这件事**，只是不再把两套单价加成一个数。
    assert f"合计 {total} 次" in out.stdout


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
    # R28：跳过判据从"段号小于 FROM"变成"**执行序**小于 FROM 的执行序"——
    # 段号和执行序解耦之后，按段号跳会把已经跑过的段 9 再跑一遍。
    assert '[ "$order" -lt "$FROM_ORDER" ]' in text


def test_python_in_tests_can_import_the_record_plan():
    """上一条测试 import 了 scripts.record_fixtures，这里确认它在这台环境上
    真的 import 得进来（它模块顶层不加载任何模型/大文件）。"""
    assert sys.modules.get("scripts.record_fixtures") or True
    import scripts.record_fixtures  # noqa: F401


# ---------- R24 补丁 0.4：E3/E4 闸门没过要有退路 ----------


def _gate_snippet() -> str:
    """把段 9 里那段 heredoc 的 python 抠出来，单独跑。

    抠出来跑而不是"读一遍源码断言有这几个字"：一段没被执行过的兜底代码
    跟没有兜底是一回事——R9 那轮的教训（静默不再正常）就是这个形状。
    """
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("python - <<'GATE_PY'"):src.index("GATE_PY", src.index("python - <<'GATE_PY'") + 20)]
    return body.split("\n", 1)[1]


def test_the_gate_snippet_does_not_hardcode_the_threshold():
    """闸门阈值只有一处定义（eval/run_eval.py::GATE_OUTPUT_CHANGE_RATE）。
    剧本里抄一个 0.4 的后果：将来闸门调了，剧本还按旧值放行。"""
    snippet = _gate_snippet()
    assert "GATE_OUTPUT_CHANGE_RATE" in snippet
    assert "0.4" not in snippet


def test_the_gate_snippet_passes_when_both_rates_clear_the_bar(tmp_path):
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "report_e3.json").write_text(
        json.dumps({"e3": {"change_rate": 0.51}}), encoding="utf-8")
    (tmp_path / "eval" / "report_e4.json").write_text(
        json.dumps({"e4": {"change_rate": 0.62}}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", _gate_snippet()], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    assert "可以继续当默认" in out.stdout


def test_a_failed_gate_exits_nonzero_and_names_the_way_back(tmp_path):
    """**闸门没过就停，并且给退路。** 把默认检索模式换成 full_context 是拿这个
    闸门担保的；闸门不过还继续演示，等于带着一个没过闸门的默认配置上台。"""
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "report_e3.json").write_text(
        json.dumps({"e3": {"change_rate": 0.31}}), encoding="utf-8")
    (tmp_path / "eval" / "report_e4.json").write_text(
        json.dumps({"e4": {"change_rate": 0.55}}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", _gate_snippet()], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 1
    assert "export RETRIEVER_MODE=hybrid" in out.stderr
    assert "0.310" in out.stderr, "要说清是哪个数没过"


def test_a_null_change_rate_is_treated_as_not_passing(tmp_path):
    """change_rate 为 null 表示"闸门无法判定"（两侧检索全为空）。
    **无法判定不等于通过**——按通过处理会让一次什么都没测到的跑变成绿灯。"""
    import subprocess

    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "report_e3.json").write_text(
        json.dumps({"e3": {"change_rate": None}}), encoding="utf-8")
    (tmp_path / "eval" / "report_e4.json").write_text(
        json.dumps({"e4": {"change_rate": 0.55}}), encoding="utf-8")
    out = subprocess.run([sys.executable, "-c", _gate_snippet()], cwd=tmp_path,
                         capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 1
    assert "无法判定" in out.stderr
