"""`scripts/run_pipeline.sh` 的步骤顺序与每一步的检索模式。

两套检索模式的单价相差一个数量级以上。所以：
  - 步骤 3（full_context 检验）排在所有花钱的评测步骤之前，先确定默认检索模式是否成立；
  - 每一步在步骤表里声明自己的检索模式，运行时 export，不继承上一步或默认值；
  - 成本按每一步的模式分开估算。
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_pipeline.sh"


def _steps():
    from scripts.pipeline_plan import parse_steps

    return parse_steps(SCRIPT.read_text(encoding="utf-8"))


def _run(args, state: Path | None = None, extra_env: dict | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
           "PYTHONPATH": str(ROOT)}
    if state is not None:
        env["PIPELINE_STATE"] = str(state)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


# ---------- 步骤顺序 ----------


def test_steps_are_numbered_contiguously_in_table_order():
    """表的顺序就是执行顺序；步骤号从 0 连续编号，--only / --from / --resume 按它寻址。"""
    nums = [r["num"] for r in _steps()]
    assert nums == [str(i) for i in range(len(nums))]
    assert len(nums) == 12


def test_the_zero_call_steps_run_first():
    rows = _steps()
    assert rows[0]["calls"] == "0" and rows[1]["calls"] == "0"
    assert int(rows[2]["calls"]) <= 2


def test_the_full_context_check_runs_before_every_costly_top3_step():
    """步骤 3 只需要 cases.json 和真实 key，就能判断默认配置是否成立。排在后面的话，
    前面的调用都是按一个未经验证的默认配置花的。"""
    rows = _steps()
    fc = [int(r["num"]) for r in rows if r["mode"] == "full_context"]
    assert fc == [3]
    for r in rows:
        if r["mode"] == "top3":
            assert int(r["num"]) > 3, f"步骤 {r['num']}（{r['name']}）排在 full_context 检验之前"


def test_dry_run_marks_the_gate_on_the_full_context_step():
    out = _run(["--dry-run"])
    assert out.returncode == 0, out.stderr
    line = next(ln for ln in out.stdout.splitlines() if re.match(r"^\s*3\s", ln))
    assert "闸门" in line and "hybrid" in line


# ---------- 每一步固定检索模式 ----------


def test_every_step_declares_a_known_mode():
    from scripts.pipeline_plan import STEP_MODES

    for row in _steps():
        assert row["mode"] in STEP_MODES, row


def test_the_steps_compared_against_top3_results_are_pinned_to_top3():
    """role 填充率、ε、录制、评测、性能基准、消融的对照值都在 top3 系下产生，换模式就不可比。"""
    mode = {r["name"]: r["mode"] for r in _steps()}
    for name in ("role 填充率闸门", "噪声地板 ε", "录制回放", "评测", "性能基准", "消融"):
        assert mode[name] == "top3", f"{name} 应该固定为 top3，现在是 {mode[name]}"


def test_the_offline_steps_are_marked_not_applicable():
    """药理层抽取和蒸馏不走检索层，标 n/a（运行时清掉 RETRIEVER_MODE）。"""
    mode = {r["name"]: r["mode"] for r in _steps()}
    assert mode["药理层抽取"] == "n/a" and mode["蒸馏（可选）"] == "n/a"


def test_top3_maps_to_hybrid_in_exactly_one_place():
    """`top3` 是四种模式的统称，export 的是其中一个具体模式；映射只在 pipeline_plan 一处。"""
    from scripts.pipeline_plan import retriever_mode_for

    assert retriever_mode_for("top3") == "hybrid"
    assert retriever_mode_for("full_context") == "full_context"
    assert retriever_mode_for("n/a") is None
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("apply_retriever_mode()"):src.index("print_plan()")]
    assert "pipeline_plan" in body, "bash 要问 pipeline_plan，不自己映射"


def test_applying_a_mode_really_exports_it():
    src = SCRIPT.read_text(encoding="utf-8")
    fn = src[src.index("apply_retriever_mode()"):src.index("print_plan()")]
    probe = (f"cd {ROOT}\n{fn}\n"
             'export RETRIEVER_MODE=leftover\n'
             'apply_retriever_mode top3 >/dev/null\n'
             'echo "top3=${RETRIEVER_MODE:-<unset>}"\n'
             'apply_retriever_mode full_context >/dev/null\n'
             'echo "fc=${RETRIEVER_MODE:-<unset>}"\n'
             'apply_retriever_mode n/a >/dev/null\n'
             'echo "na=${RETRIEVER_MODE:-<unset>}"\n')
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True,
                         timeout=120, cwd=ROOT,
                         env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr
    assert "top3=hybrid" in out.stdout
    assert "fc=full_context" in out.stdout
    # n/a 清掉上一步留下的值，而不是保留
    assert "na=<unset>" in out.stdout


# ---------- 成本按模式估算 ----------


def test_the_two_unit_prices_are_defined_in_one_place():
    from scripts.pipeline_plan import unit_price_cny

    assert unit_price_cny("top3") == 0.0055
    assert unit_price_cny("full_context") > unit_price_cny("top3") * 20
    # n/a 按 top3 的均价算，不是 0：不走检索层的步骤照样调用模型
    assert unit_price_cny("n/a") == unit_price_cny("top3")
    code = "\n".join(ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "0.0055" not in code, "单价不作为值出现在 bash 里，bash 只问 pipeline_plan"


def test_the_full_context_price_comes_from_the_shared_price_table():
    """full_context 单价 = 命中缓存的知识前缀 + 一次输出，按 core/usage.py 的价目表计算。"""
    from core.usage import cost_cny
    from scripts.pipeline_plan import (
        FULL_CONTEXT_PREFIX_TOKENS, OUT_TOKENS_PER_CALL, unit_price_cny,
    )

    expected = cost_cny(hit_tokens=FULL_CONTEXT_PREFIX_TOKENS,
                        out_tokens=OUT_TOKENS_PER_CALL, peak=True)
    assert abs(unit_price_cny("full_context") - expected) < 1e-9


def test_changing_a_step_to_full_context_scales_its_cost_by_the_price_ratio():
    from scripts.pipeline_plan import step_cost_cny, unit_price_cny

    calls = 1200
    cheap = step_cost_cny("top3", calls)
    dear = step_cost_cny("full_context", calls)
    assert dear / cheap == pytest.approx(
        unit_price_cny("full_context") / unit_price_cny("top3"))


def test_the_plan_totals_are_split_by_mode():
    out = _run(["--dry-run"])
    assert out.returncode == 0, out.stderr
    assert "top3" in out.stdout and "full_context" in out.stdout
    assert re.search(r"top3.*¥", out.stdout)


# ---------- 闸门结论写进状态文件 ----------


def test_the_gate_decision_is_written_into_the_state_file(tmp_path):
    """闸门不通过 → 退回 hybrid 的结论要落盘，否则下一次 --resume 又按 full_context 运行。"""
    src = SCRIPT.read_text(encoding="utf-8")
    begin = src.index("MODE_DECISION_KEY=")
    fn = src[begin:src.index("\nmode_decision()", begin)]
    state = tmp_path / "state.tsv"
    probe = (f'cd {ROOT}\nPIPELINE_STATE={state}\n{fn}\n'
             'record_mode_decision hybrid\n')
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    text = state.read_text(encoding="utf-8")
    assert "hybrid" in text and "retriever_mode_decided" in text


def test_a_state_file_without_a_decision_line_still_resumes(tmp_path):
    """只有 `步骤号\\t退出码\\t时间` 三列的状态文件照常能读。"""
    state = tmp_path / "state.tsv"
    state.write_text("0\t0\t2026-09-15T01:00:00+00:00\n"
                     "1\t1\t2026-09-15T01:05:00+00:00\n", encoding="utf-8")
    out = _run(["--status"], state=state)
    assert out.returncode == 0, out.stderr
    assert "0（成功）" in out.stdout and "1（失败）" in out.stdout
    assert "--resume 会从步骤 1 开始" in out.stdout


def test_the_decided_mode_is_announced_when_a_step_starts():
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("run_step()"):src.index('if [ "$STATUS" = "1" ]')]
    assert "mode_decision" in body


def test_the_recording_step_says_its_output_is_bound_to_the_mode():
    rows = {r["name"]: r for r in _steps()}
    note = rows["录制回放"]["note"]
    assert "检索模式" in note and "步骤 3" in note


def test_dry_run_warns_that_recording_depends_on_the_gate(tmp_path):
    out = _run(["--dry-run"], state=tmp_path / "nope.tsv")
    assert "先跑步骤 3" in out.stdout
