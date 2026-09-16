"""R28-A/B：上机剧本的**执行顺序**与**每段检索模式**。

这一轮修的是一条会花掉十倍钱的链子：

  1. R21 把 `effective_mode()` 的默认值换成 `full_context`；
  2. 剧本里除了段 9 没有任何一段钉住模式，于是段 4/6/7/8 全部继承新默认；
  3. 两套单价差 29 倍（top3 ¥0.0055/次 vs full_context ¥0.16/次），
     段 7 的 1200 次于是从清单上标的 ¥6.6 变成约 ¥192；
  4. 而段 9 的闸门一旦不过，脚本提示退回 hybrid——段 6 在 full_context 下录的
     278 条 fixture 当场全部失效（键含 system 全文）。

所以三件事必须一起改：**执行顺序**（段 9 提到段 3 之前，先知道默认成不成立）、
**每段钉死模式**（不许继承）、**成本按模式算**。段号不动——`--only` / `--from` /
`--resume` 和状态文件都按段号寻址，动段号会牵动那一整套语义。
"""
from __future__ import annotations

import os
import re
import subprocess

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_onsite.sh"


def _segments():
    from scripts.onsite_plan import parse_segments

    return parse_segments(SCRIPT.read_text(encoding="utf-8"))


def _run(args, state: Path | None = None, extra_env: dict | None = None):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
           "PYTHONPATH": str(ROOT)}
    if state is not None:
        env["ONSITE_STATE"] = str(state)
    env.update(extra_env or {})
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=180)


# ---------- A：执行顺序与段号解耦 ----------


def test_every_segment_declares_both_a_number_and_an_execution_order():
    rows = _segments()
    assert rows, "段表解析不出东西"
    for row in rows:
        assert row["num"] is not None and row["order"] is not None, row


def test_segment_numbers_are_unchanged_zero_to_ten():
    """**段号不动**：`--only` / `--from` / `--resume` 和状态文件都按段号寻址。"""
    assert [r["num"] for r in _segments()] == [str(i) for i in range(11)]


def test_the_execution_order_is_a_permutation_not_a_free_for_all():
    """order 必须是 0..N-1 的一个排列——重复或跳号意味着有段永远不跑、
    或者两段的先后取决于数组顺序（那就等于没有 order）。"""
    orders = sorted(int(r["order"]) for r in _segments())
    assert orders == list(range(len(orders)))


def test_segment_nine_runs_before_segment_three():
    """**这一轮的核心**：段 9 决定「默认配置成不成立」，而它只要 cases.json +
    真实 key。放在最后跑意味着前面几千次调用是按一个还没验证的默认配置花的。"""
    order = {r["num"]: int(r["order"]) for r in _segments()}
    assert order["9"] < order["3"]
    for n in ("3", "4", "5", "6", "7", "8", "10"):
        assert order["9"] < order[n], f"段 9 必须排在段 {n} 之前"


def test_the_zero_call_segments_still_run_first():
    """免费的问题先发现掉——这条没变。"""
    rows = sorted(_segments(), key=lambda r: int(r["order"]))
    assert [r["num"] for r in rows[:3]] == ["0", "1", "2"]
    assert rows[0]["calls"] == "0" and rows[1]["calls"] == "0"


def test_the_runner_loops_in_execution_order_not_array_order():
    """光有 order 字段不够，跑的时候得真按它排。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "segments_in_execution_order" in src


def test_dry_run_prints_both_the_number_and_the_order():
    out = _run(["--dry-run"])
    assert out.returncode == 0, out.stderr
    assert "执行序" in out.stdout and "段号" in out.stdout


def test_dry_run_marks_the_gate_on_segment_nine():
    """段 9 那行要写明：闸门不过的话，后面各段的成本口径全都变。"""
    out = _run(["--dry-run"])
    line = next(ln for ln in out.stdout.splitlines() if ln.lstrip().startswith("9 ")
                or re.match(r"^\s*9\s", ln))
    assert "闸门" in line
    assert "hybrid" in line


# ---------- B：每段钉死模式 ----------


def test_every_segment_declares_a_known_mode():
    from scripts.onsite_plan import SEGMENT_MODES

    for row in _segments():
        assert row["mode"] in SEGMENT_MODES, row


def test_the_segments_with_top3_history_are_pinned_to_top3():
    """段 3/4/6/7/8 的历史数据、E3/E4 旧基线、fixture 全是 top3 系的，
    换模式就不可比——**不可比不是"数字变了"，是那几行历史值作废**。"""
    mode = {r["num"]: r["mode"] for r in _segments()}
    for n in ("3", "4", "6", "7", "8"):
        assert mode[n] == "top3", f"段 {n} 应该钉成 top3，现在是 {mode[n]}"


def test_segment_nine_is_the_only_full_context_segment():
    mode = {r["num"]: r["mode"] for r in _segments()}
    assert mode["9"] == "full_context"
    assert [n for n, m in mode.items() if m == "full_context"] == ["9"]


def test_the_offline_segments_are_marked_not_applicable():
    """段 5（药理层抽取）和段 10（蒸馏）不走检索层。标 n/a 而不是随便挑一个模式：
    挑一个的话，将来有人读这张表会以为那个模式对结果有影响。"""
    mode = {r["num"]: r["mode"] for r in _segments()}
    assert mode["5"] == "n/a" and mode["10"] == "n/a"


def test_top3_maps_to_hybrid_in_exactly_one_place():
    """`top3` 是一系四种模式的统称，真要 export 的是其中一个具体的模式。
    这个映射只能有一处——bash 里再抄一份，改默认值时就会有一处忘了改。"""
    from scripts.onsite_plan import retriever_mode_for

    assert retriever_mode_for("top3") == "hybrid"
    assert retriever_mode_for("full_context") == "full_context"
    assert retriever_mode_for("n/a") is None
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("apply_retriever_mode()"):src.index("print_plan()")]
    assert "onsite_plan" in body, "bash 要问 onsite_plan，不要自己映射"


def test_applying_a_mode_really_exports_it():
    """把 `apply_retriever_mode` 抠出来单独跑：**一段没被执行过的兜底代码
    跟没有兜底是一回事**（R9「静默不再正常」是同一个形状）。"""
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
    # n/a：**清掉而不是留着上一段的值**——"不许靠环境继承"这句话的字面意思。
    assert "na=<unset>" in out.stdout


def test_the_two_unit_prices_are_defined_in_one_place():
    """两套单价差 29 倍。bash 里抄一份的后果很具体：改了价的那一处对，
    另一处继续按旧价估，而估算不会报错。"""
    from scripts.onsite_plan import unit_price_cny

    assert unit_price_cny("top3") == 0.0055
    assert unit_price_cny("full_context") > unit_price_cny("top3") * 20
    # **n/a 按 top3 的均价算，不是 0。** 写这条判据时先写成 0，`--dry-run` 一跑
    # 就看出来了：段 5（药理层抽取 2181 次）的模式是 n/a，按 0 算清单会说
    # "全剧本最贵的那一段不要钱"。"不走检索层"和"不花钱"是两件事。
    assert unit_price_cny("n/a") == unit_price_cny("top3")
    # 只看**代码行**：注释里写「full_context ¥0.16/次」是在解释这一轮修的是什么，
    # 那不是一份会被用到的副本。判据要卡的是"有没有第二个地方在拿这个数算钱"。
    code = "\n".join(ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "0.16" not in code, "full_context 的单价不许作为值出现在 bash 里"
    assert "0.0055" not in code, "top3 的单价同理——bash 只负责问 onsite_plan"


def test_the_full_context_price_comes_from_the_shared_price_table():
    """单价不是拍的：命中输入 18 万 token + 一次输出，按 core/usage.py 的价目表算。"""
    from core.usage import cost_cny
    from scripts.onsite_plan import (
        FULL_CONTEXT_PREFIX_TOKENS, OUT_TOKENS_PER_CALL, unit_price_cny,
    )

    expected = cost_cny(hit_tokens=FULL_CONTEXT_PREFIX_TOKENS,
                        out_tokens=OUT_TOKENS_PER_CALL, peak=True)
    assert abs(unit_price_cny("full_context") - expected) < 1e-9


def test_changing_a_segment_to_full_context_jumps_its_cost():
    """把段 7（1200 次）的模式换成 full_context，估算必须跟着跳一个量级。
    **断言的是"两套单价确实被用上了"，不是某个具体的数**——单价一改，
    写死数的断言就会红，而红的原因跟这条判据要守的东西无关。"""
    from scripts.onsite_plan import segment_cost_cny, unit_price_cny

    calls = 1200
    cheap = segment_cost_cny("top3", calls)
    dear = segment_cost_cny("full_context", calls)
    assert dear / cheap == pytest.approx(
        unit_price_cny("full_context") / unit_price_cny("top3"))
    assert dear > cheap * 20


def test_the_plan_totals_are_split_by_mode():
    """一张把两套单价加在一起的总表是**误导**：它给出的是一个谁都对不上的数。"""
    out = _run(["--dry-run"])
    assert out.returncode == 0, out.stderr
    assert "top3" in out.stdout and "full_context" in out.stdout
    assert "模式" in out.stdout
    assert re.search(r"top3.*¥", out.stdout)


# ---------- A3：闸门的决定写进状态文件 ----------


def test_the_gate_decision_is_written_into_the_state_file(tmp_path):
    """闸门不过 → 退回 hybrid 这个**决定**要落盘。不落盘的话，
    下一次 `--resume` 起来的段又按 full_context 跑，而人早就决定退回去了。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "record_mode_decision" in src
    # `mode_decision()` 是 `record_mode_decision()` 的子串——从 record 之后再找，
    # 否则切片长度为 0（写这条判据时就这么错过一次，bash 报的是 `record_: command not found`）。
    begin = src.index("MODE_DECISION_KEY=")     # 常量跟函数一起抠出来，否则键是空串
    fn = src[begin:src.index("\nmode_decision()", begin)]
    state = tmp_path / "state.tsv"
    probe = (f'cd {ROOT}\nONSITE_STATE={state}\n{fn}\n'
             'record_mode_decision hybrid\n')
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "hybrid" in state.read_text(encoding="utf-8")
    assert "retriever_mode_decided" in state.read_text(encoding="utf-8")


def test_an_old_state_file_without_a_decision_line_still_resumes(tmp_path):
    """**状态文件格式不变**：旧文件（只有 `段号\\t退出码\\t时间` 三列）要照常能读。
    这条是段号不动那个决定的兑现——改格式等于让所有在跑的机器上的状态文件作废。"""
    state = tmp_path / "state.tsv"
    state.write_text("0\t0\t2026-09-15T01:00:00+00:00\n"
                     "1\t1\t2026-09-15T01:05:00+00:00\n", encoding="utf-8")
    out = _run(["--status"], state=state)
    assert out.returncode == 0, out.stderr
    assert "0（成功）" in out.stdout and "1（失败）" in out.stdout
    assert "--resume 会从段 1 开始" in out.stdout


def test_the_decided_mode_is_announced_when_segments_start(tmp_path):
    """决定落盘之后，后面每段起来要打印当前口径——人才知道现在花的是哪套钱。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "mode_decision" in src
    body = src[src.index("run_segment()"):src.index("if [ \"$STATUS\" = \"1\" ]")]
    assert "mode_decision" in body or "announce_mode" in body


# ---------- C 的剧本侧：段 6 要在段 9 之后 ----------


def test_segment_six_says_its_output_is_bound_to_the_mode():
    rows = {r["num"]: r for r in _segments()}
    assert "模式" in rows["6"]["note"] or "检索模式" in rows["6"]["note"]
    assert "段 9" in rows["6"]["note"]


def test_dry_run_warns_that_recording_depends_on_the_gate(tmp_path):
    """段 9 还没通过时，段 6 那行要有黄字警告——录了也可能白录。"""
    out = _run(["--dry-run"], state=tmp_path / "nope.tsv")
    assert "白录" in out.stdout or "先跑段 9" in out.stdout


# ---------- 顺带修掉的那个 bug ----------


def test_segment_ten_is_actually_dispatched():
    """`run_segment` 的 case 分支原来只到 9，段 10 会**什么都不跑然后报退出码 0**
    ——一个"跑完了"的假绿。读代码时发现的，不是测试发现的，所以补一条判据。"""
    src = SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("run_segment()"):src.index("if [ \"$STATUS\" = \"1\" ]")]
    for n in range(11):
        assert f"seg_{n}" in body, f"run_segment 里没有调 seg_{n}"
