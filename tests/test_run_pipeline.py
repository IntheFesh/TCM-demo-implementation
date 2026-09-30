"""scripts/run_pipeline.sh 的离线测试。

bash 脚本也要有测试：流水线里写错一个模块路径，要到那一步真跑时才发现，而那时前面
几步的钱已经花了。所以这里解析脚本本身——步骤表连不连续、人工确认在不在该在的位置、
里面 `python -m` 的每个模块是不是真的存在——并把几段关键逻辑抠出来真跑一遍。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_pipeline.sh"


def _step_rows() -> list[tuple[str, str, str, str, str]]:
    """步骤表原文里这几条测试关心的那几格：`(步骤号, 名称, 预估调用数, 人工确认, 说明)`。

    解析走 `scripts.pipeline_plan.parse_steps`，不在这里另写正则：各写一份的话，
    表里加一格就要同时改好几处，漏改的那处不会报错、只会少断言一件事。
    步骤顺序和检索模式的判据在 tests/test_pipeline_order_and_mode.py。
    """
    from scripts.pipeline_plan import parse_steps

    rows = parse_steps(SCRIPT.read_text(encoding="utf-8"))
    return [(r["num"], r["name"], r["calls"], r["gate"], r["note"]) for r in rows]


def _steps() -> list[tuple[str, str, int, str, str]]:
    """同上，但「预估调用数」解析成整数——走 `scripts/pipeline_plan.resolve_calls`，
    跟脚本里 `resolve_calls()` 调的是同一个实现，不在测试里另算一遍。"""
    from scripts.pipeline_plan import resolve_calls

    return [(n, name, resolve_calls(calls), gate, note)
            for n, name, calls, gate, note in _step_rows()]


def _body(start: str, end: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    return src[src.index(start):src.index(end)]


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_step_numbers_are_contiguous_and_unique():
    nums = [int(r[0]) for r in _steps()]
    assert nums == list(range(len(nums))), nums


def test_steps_are_ordered_by_dependency_first_then_cost():
    """执行顺序按「依赖优先，其次成本」排：

    - 前两步零调用：免费能发现的问题先发现；
    - 药理层抽取在录制和评测之前：core/tools.py 读它落盘的数据，否则录下来的是
      「数据文件不存在」的工具输出；
    - 性能基准在药理层抽取之后：开 ReAct 那次基准要读它的产出；
    - full_context 检验在所有花钱的步骤之前：它决定后面按哪套默认配置、哪套单价跑；
    - 评测仍是不被依赖的步骤里最贵的一步；
    - 可选的蒸馏最后执行，中途停下来不会漏掉任何必做项。
    """
    rows = _steps()
    assert rows[0][2] == 0 and rows[1][2] == 0, "前两步必须是零调用"
    names = [r[1] for r in rows]
    i_pharm = next(i for i, n in enumerate(names) if "药理层" in n)
    i_record = next(i for i, n in enumerate(names) if "录制" in n)
    i_eval = next(i for i, n in enumerate(names) if n == "评测")
    i_bench = next(i for i, n in enumerate(names) if "性能基准" in n)
    i_fc = next(i for i, n in enumerate(names) if "full_context" in n)
    assert i_pharm < i_record < i_eval, "药理层抽取必须在录制和评测之前（core/tools.py 读它的产出）"
    assert i_pharm < i_bench, "性能基准必须在药理层抽取之后（开 ReAct 那次要读它的产出）"
    assert i_fc < i_bench and i_fc < i_eval and i_fc < i_pharm
    calls = [r[2] for r in rows]
    assert calls[i_eval] > calls[i_record]
    assert calls[i_eval] > calls[i_bench]
    assert calls[i_eval] > calls[i_fc]
    assert "蒸馏" in rows[-1][1], f"最后一个执行的步骤是 {rows[-1][1]}，不是蒸馏"


def test_exactly_three_human_gates():
    """人工确认存在的理由是：闸门没过就往下跑，后面的调用全部白花。步骤 11 的确认
    针对的是花费：蒸馏真跑之前要有人对估出来的条数和花费点头。

    判据逐个列出是哪几步，而不是放宽成"至少有两处"：后者发现不了某一步去掉了确认。
    """
    gated = [r[0] for r in _steps() if r[3] == "YES"]
    assert gated == ["4", "6", "11"], gated


def test_every_declared_gate_actually_asks():
    """步骤表里标 YES 的每一步，脚本里都真有一次 `gate <步骤号>` 调用。
    只在表里标、脚本里不问的话，那个确认点只存在于清单上。"""
    text = SCRIPT.read_text(encoding="utf-8")
    for n in [r[0] for r in _steps() if r[3] == "YES"]:
        assert re.search(rf"\bgate {n} ", text), f"步骤 {n} 标了人工确认，但脚本里没有 gate {n}"


def test_every_python_module_the_pipeline_invokes_actually_exists():
    """流水线写错一个模块路径，要到跑到那一步才发现。"""
    text = SCRIPT.read_text(encoding="utf-8")
    modules = set(re.findall(r"python3? -m ([\w.]+)", text))
    assert modules, "脚本里一个 python -m 都没有？"
    missing = []
    for mod in sorted(modules):
        if mod == "pytest":
            continue
        rel = Path(mod.replace(".", "/"))
        if not ((ROOT / rel).with_suffix(".py").exists() or (ROOT / rel / "__init__.py").exists()):
            missing.append(mod)
    assert not missing, f"流水线里这些模块不存在：{missing}"


def test_every_shell_script_the_pipeline_invokes_exists():
    text = SCRIPT.read_text(encoding="utf-8")
    for rel in set(re.findall(r"bash (scripts/[\w./-]+\.sh)", text)):
        assert (ROOT / rel).exists(), rel


def test_every_doc_the_pipeline_points_to_exists():
    """脚本里指给人看的文档必须真的在。"""
    text = SCRIPT.read_text(encoding="utf-8")
    docs = set(re.findall(r"docs/[\w./-]+\.md", text))
    assert "docs/DEPLOYMENT.md" in docs, "故障排查要指向 docs/DEPLOYMENT.md"
    missing = [d for d in sorted(docs) if not (ROOT / d).exists()]
    assert not missing, f"脚本里指向的这些文档不存在：{missing}"


def test_each_step_prints_a_timestamp_and_an_exit_code():
    """开始和结束都打时间戳、结束时打退出码，是可续跑的前提：断了之后要能一眼看出断在哪。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count("date -Is") >= 2          # 开始 + 结束
    assert "退出码 $rc" in text


def test_a_failing_step_does_not_stop_the_later_ones():
    """步骤之间只有先后，没有连锁中止：药理层抽取挂了，录制照样该跑。
    所以不用 `set -e`，run_step 的最后一条语句是无条件 `return 0`。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -e\n" not in text and "set -euo" not in text
    body = text[text.index("run_step() {"):]
    body = body[:body.index("\n}\n")]
    last = [ln.strip() for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")][-1]
    assert last.startswith("return 0"), f"run_step 末尾不是无条件 return 0，是：{last}"


def test_dry_run_prints_the_plan_and_runs_nothing():
    """开跑前先看每一步的预估调用数和成本，决定跑到哪一步。"""
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stderr
    assert "预估调用" in out.stdout
    assert "什么都没跑" in out.stdout
    for _n, name, *_ in _steps():
        assert name in out.stdout


def test_dry_run_total_matches_the_step_table():
    """总计那行按模式分开算，但总调用数仍然是清单上每一步之和。"""
    out = subprocess.run(["bash", str(SCRIPT), "--dry-run"],
                         capture_output=True, text=True, cwd=ROOT)
    total = sum(r[2] for r in _steps())
    assert f"合计 {total} 次" in out.stdout


def test_help_prints_the_header_usage():
    out = subprocess.run(["bash", str(SCRIPT), "--help"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0
    for flag in ("--dry-run", "--resume", "--status", "--from", "--only", "--yes"):
        assert flag in out.stdout, flag
    assert "PIPELINE_STATE" in out.stdout


def test_unknown_argument_fails_loudly():
    out = subprocess.run(["bash", str(SCRIPT), "--nope"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 2 and "未知参数" in out.stderr


def test_epsilon_step_estimate_is_read_from_the_file_not_written_down():
    """步骤 5 的预估调用数不写死，必须是 `auto:eval/epsilon.json`。

    两条断言分别钉住：
    1. 那一格是 `auto:eval/epsilon.json`——步骤表里没有这个数字的副本，ε 重跑之后
       不会有一个过期的数留在表里；
    2. 解析出来的值等于文件里三段 llm_calls 之和——现读的确实是这份文件、这几段，
       不是碰巧给了个数（只有第 1 条的话，读法写错也发现不了）。
    """
    d = json.loads((ROOT / "eval" / "epsilon.json").read_text(encoding="utf-8"))
    measured = sum((d.get(k) or {}).get("llm_calls") or 0
                   for k in ("epsilon_online", "epsilon_s2", "epsilon_extract"))
    raw = [r for r in _step_rows() if r[0] == "5"][0]
    assert raw[2] == "auto:eval/epsilon.json", f"步骤 5 的预估被写死成了 {raw[2]}"
    step = [r for r in _steps() if r[0] == "5"][0]
    assert step[2] == measured, f"现读得到 {step[2]}，epsilon.json 三段之和 {measured}"


def test_no_step_note_duplicates_a_number_that_lives_in_a_file():
    """说明文字里也不抄那个现读的数——抄了就会跟文件一起过期。"""
    from scripts.pipeline_plan import resolve_calls

    for n, name, calls, _gate, note in _step_rows():
        if not calls.startswith("auto:"):
            continue
        assert str(resolve_calls(calls)) not in note, f"步骤 {n}（{name}）的说明里抄了这个数"


def test_recording_estimate_matches_the_record_plan():
    """步骤 7 的预估要跟 record_fixtures 的录制清单对得上。"""
    import scripts.record_fixtures as rf

    planned = sum(s.estimated_calls for s in rf.build_plan())
    step = [r for r in _steps() if r[0] == "7"][0]
    assert step[2] == planned, f"步骤表写 {step[2]}，录制清单是 {planned}"


def test_step_zero_checks_the_model_is_still_served():
    """步骤 0 要在产生任何花费之前挡掉服务端不认识的模型名：这种请求可能得到
    HTTP 200 + 空响应体而不是 404，表现为每次调用空串 → 校验失败 → 重试耗尽 → LLMError，
    错误信息里看不出根因。

    这条只解析脚本文本（真查清单要网络，`tests/` 不联网）：步骤 0 里要有查 /models
    的那段，并且能区分"查不到"（跳过，不算失败）和"清单里没有它"（`SystemExit(1)`）。
    """
    step0 = _body("step_0() {", "step_1() {")
    assert "/models" in step0, "步骤 0 没有查模型清单"
    assert "LLM_MODEL" in step0 and "SystemExit(1)" in step0
    assert "跳过这一项" in step0, "查不到清单必须跳过，而不是拦住后面所有步骤"
    # 查清单不是 LLM 调用，步骤 0 的预估仍然是 0
    step0_row = [r for r in _steps() if r[0] == "0"][0]
    assert step0_row[2] == 0


@pytest.mark.parametrize("flag", ["--from", "--only"])
def test_from_and_only_require_a_step_number(flag):
    out = subprocess.run(["bash", str(SCRIPT), flag],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode != 0


def test_only_flag_runs_a_single_step():
    """--only 3 只跑步骤 3；--from N 跳过 N 之前的步骤。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'if [ -n "$ONLY" ]; then' in text
    assert '[ "$n" = "$ONLY" ] || continue' in text
    assert '[ "$n" -lt "$FROM" ]' in text


def test_every_step_function_is_dispatched():
    """run_step 的 case 分支要覆盖步骤表里的每一步：漏掉一支，那一步会什么都不跑、
    报退出码 0，是一个"跑完了"的假绿。"""
    body = _body("run_step()", 'if [ "$STATUS" = "1" ]')
    for n, *_ in _steps():
        assert f"step_{n} " in body, f"run_step 里没有调 step_{n}"


# ---------- 人工确认读的是终端，不是步骤表 ----------


def _stubbed_pipeline() -> str:
    """把真脚本的每个步骤函数换成只打一行的桩，其余逻辑（参数、状态文件、确认点、
    主循环）原样保留。桩插在所有函数定义之后、主流程之前，覆盖掉同名定义。"""
    src = SCRIPT.read_text(encoding="utf-8")
    stubs = "\n".join(f'step_{n}() {{ echo "RAN step_{n}"; }}' for n, *_ in _step_rows())
    stubs += '\nstep_6b() { echo "RAN step_6b"; }\n'
    stubs += "warn_if_peak() { :; }\n"
    anchor = 'if [ "$STATUS" = "1" ]; then'
    assert anchor in src
    return src.replace(anchor, stubs + anchor, 1)


def test_a_human_gate_reads_the_answer_not_the_next_row_of_the_step_table(tmp_path):
    """主循环从步骤表逐行读；确认点的 `read` 如果也读标准输入，读到的是步骤表的
    下一行（不是 y），于是这一步被判成人工中止，下一步也被整行吃掉、根本不跑。
    脚本因此从文件描述符 3 读步骤表，标准输入留给人。"""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "LANG": "C.UTF-8", "PIPELINE_STATE": str(tmp_path / "state.tsv")}
    out = subprocess.run(["bash", "-c", _stubbed_pipeline(), str(SCRIPT), "--from", "3"],
                         input="y\ny\ny\n", capture_output=True, text=True, cwd=ROOT,
                         env=env, timeout=180)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    for n in ("3", "4", "5", "6", "6b", "7", "8", "9", "10", "11"):
        assert f"RAN step_{n}\n" in out.stdout, f"step_{n} 没跑：\n{out.stdout[-2000:]}"
    assert "人工中止" not in out.stdout
    state = (tmp_path / "state.tsv").read_text(encoding="utf-8")
    assert re.search(r"^4\t0\t", state, re.M), state


def test_a_declined_gate_is_recorded_as_a_manual_abort(tmp_path):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path),
           "LANG": "C.UTF-8", "PIPELINE_STATE": str(tmp_path / "state.tsv")}
    out = subprocess.run(["bash", "-c", _stubbed_pipeline(), str(SCRIPT), "--only", "4"],
                         input="n\n", capture_output=True, text=True, cwd=ROOT,
                         env=env, timeout=180)
    assert out.returncode == 1, out.stdout[-2000:]
    assert "[步骤 4] 人工中止" in out.stdout
    assert re.search(r"^4\t10\t", (tmp_path / "state.tsv").read_text(encoding="utf-8"), re.M)


# ---------- 步骤 3：full_context 检验 ----------


def test_the_full_context_step_runs_its_four_checks():
    """前缀规模、缓存命中率、full_context 下的 E3/E4、字体子集化，一步里跑完并各带判据。"""
    body = _body("step_3() {", "step_4() {")
    assert "core.context_prefix --report" in body
    assert "bench_consult --backend real --repeat 2" in body
    assert "RETRIEVER_MODE=full_context python -m eval.run_eval --e3 --e4" in body
    assert "subset_fonts" in body


def test_the_full_context_step_says_its_numbers_are_not_comparable():
    """检索模式、采样次数、推理档位都跟 top3 系不同：这一步的 E3/E4 单列一行，
    不覆盖 top3 系那张表。"""
    body = _body("step_3() {", "step_4() {")
    assert "不可比" in body
    assert "并列报，不相减" in body


def _gate_snippet() -> str:
    """把步骤 3 里那段 heredoc 的 python 抠出来单独跑。

    抠出来跑而不是"读一遍源码断言有这几个字"：一段没被执行过的兜底代码，
    跟没有兜底是一回事。
    """
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("python - <<'GATE_PY'")
    body = src[start:src.index("GATE_PY", start + 20)]
    return body.split("\n", 1)[1]


def test_the_gate_snippet_does_not_hardcode_the_threshold():
    """闸门阈值只有一处定义（eval/run_eval.py::GATE_OUTPUT_CHANGE_RATE）。
    抄一个 0.4 在这里，将来闸门调了，脚本还按旧值放行。"""
    snippet = _gate_snippet()
    assert "GATE_OUTPUT_CHANGE_RATE" in snippet
    assert "0.4" not in snippet


def _run_gate(tmp_path, e3_rate, e4_rate):
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "report_e3.json").write_text(
        json.dumps({"e3": {"change_rate": e3_rate}}), encoding="utf-8")
    (tmp_path / "eval" / "report_e4.json").write_text(
        json.dumps({"e4": {"change_rate": e4_rate}}), encoding="utf-8")
    return subprocess.run([sys.executable, "-c", _gate_snippet()], cwd=tmp_path,
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": str(ROOT)})


def test_the_gate_snippet_passes_when_both_rates_clear_the_bar(tmp_path):
    out = _run_gate(tmp_path, 0.51, 0.62)
    assert out.returncode == 0, out.stderr
    assert "可以继续当默认" in out.stdout


def test_a_failed_gate_exits_nonzero_and_names_the_way_back(tmp_path):
    """闸门没过就停，并给出退路：full_context 当默认检索模式是拿这个闸门担保的。"""
    out = _run_gate(tmp_path, 0.31, 0.55)
    assert out.returncode == 1
    assert "export RETRIEVER_MODE=hybrid" in out.stderr
    assert "0.310" in out.stderr, "要说清是哪个数没过"


def test_a_null_change_rate_is_treated_as_not_passing(tmp_path):
    """change_rate 为 null 表示闸门无法判定（两侧检索全为空）。
    无法判定不等于通过——按通过处理会让一次什么都没测到的运行变成绿灯。"""
    out = _run_gate(tmp_path, None, 0.55)
    assert out.returncode == 1
    assert "无法判定" in out.stderr


# ---------- 步骤 11：蒸馏（可选） ----------


def test_the_distillation_step_trains_on_an_explicit_base_model():
    """基座由 DISTILL_BASE_MODEL 指定（Hugging Face 仓库 id），经 `--base-model` 传给
    scripts.train_lora；步骤里不写死任何基座。"""
    body = _body("step_11() {", "run_step()")
    assert 'python -m scripts.train_lora --data distill_v4 --base-model "$DISTILL_BASE_MODEL"' in body
    assert "--base " not in body


def test_the_distillation_step_is_skipped_without_a_base_model():
    """没设 DISTILL_BASE_MODEL 时整步跳过，并说清要设什么；跳过要发生在任何花钱的动作之前。"""
    body = _body("step_11() {", "run_step()")
    skip = body.index('if [ -z "${DISTILL_BASE_MODEL:-}" ]')
    assert skip < body.index("offline.distill_from_v4")
    assert "跳过" in body[skip:skip + 400] and "DISTILL_BASE_MODEL" in body[skip:skip + 400]


def test_the_distillation_step_asks_before_spending():
    """`--yes-spend` 放行超过确认线的花费；放行之前必须先过人工确认。"""
    body = _body("step_11() {", "run_step()")
    assert body.index("gate 11 ") < body.index("--yes-spend")
