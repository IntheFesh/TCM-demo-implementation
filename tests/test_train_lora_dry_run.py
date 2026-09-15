"""R18-H：两个基座 × 五位医家，`--dry-run` 在 CPU 上跑得通。

**真训练在这个沙盒里跑不了**：`peft` 和 `accelerate` 没装（torch/transformers 装了）。
所以这里测的是两件能测的事——计划算得对不对、缺依赖时报得对不对；
真训练那一步由 `--check-deps` 的退出码兜着，报告的「无法完成项」里有上机命令。
"""
from __future__ import annotations

import json

import pytest

from core.physicians import PHYSICIANS, physicians_all
from scripts.train_lora import (
    BASES,
    build_plan,
    format_plan_text,
    main,
    report_deps,
)


def _rows(n: int = 20) -> list[dict]:
    pids = sorted(physicians_all(PHYSICIANS))
    out = []
    for i in range(n):
        pid = pids[i % len(pids)]
        out.append({
            "input": f"胃脘胀痛（第 {i} 条）",
            "chain": [
                {"step": "症状→病机", "output": "肝气犯胃", "rationale": "原文片段",
                 "source": f"case:{pid}-{i:03d}", "rationale_source": f"case:{pid}-{i:03d}"},
                {"step": "病机→证型", "output": "肝气犯胃证", "rationale": None,
                 "source": f"case:{pid}-{i:03d}", "rationale_source": None},
            ],
            "meta": {"source_kind": "case", "physician_id": pid,
                     "case_id": f"{pid}-{i:03d}", "case_group_id": f"{pid}-g{i // 4}",
                     "copyright_status": "public_domain",
                     "split": "heldout" if i % 5 == 0 else "train",
                     "split_source": "case_group_id"},
        })
    return out


def _samples_file(tmp_path, rows):
    p = tmp_path / "sft_chain.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")
    return p


def test_plan_covers_two_bases_times_five_physicians(tmp_path):
    """R18-A 把注册表从两位扩到五位，训练计划要跟着变成 2×5=10 个 adapter。
    数字不写死成 10：判据是"基座数 × 注册表里的医家数"，加第六位医家时这条
    自己就跟着变。"""
    pids = sorted(physicians_all(PHYSICIANS))
    assert len(pids) == 5, f"注册表现在是 {len(pids)} 位：{pids}"
    plan = build_plan(_rows(), pids, sorted(BASES), tmp_path)
    assert len(plan["jobs"]) == len(BASES) * len(pids)
    assert {j["physician_id"] for j in plan["jobs"]} == set(pids)
    assert {j["base"] for j in plan["jobs"]} == set(BASES)


def test_each_adapter_gets_its_own_output_dir(tmp_path):
    """十个 adapter 十个目录。撞目录会让后训的覆盖先训的，而两个基座的对照
    正是这一轮要的东西。"""
    pids = sorted(physicians_all(PHYSICIANS))
    plan = build_plan(_rows(), pids, sorted(BASES), tmp_path)
    dirs = [j["out_dir"] for j in plan["jobs"]]
    assert len(set(dirs)) == len(dirs)


def test_plan_shouts_when_a_physician_has_no_training_samples(tmp_path):
    """注册表扩到五位之后，李可/王云启在 cases.json 里还没条目时 train 是 0。

    **必须在计划里喊出来**：不喊的话五个 adapter 目录照样建出来，
    其中两个是空训练，看目录看不出区别。
    """
    rows = [r for r in _rows() if r["meta"]["physician_id"] != "li_ke"]
    pids = sorted(physicians_all(PHYSICIANS))
    plan = build_plan(rows, pids, ["qwen2.5-1.5b"], tmp_path)
    text = format_plan_text(plan, [dict(BASES["qwen2.5-1.5b"], key="qwen2.5-1.5b")])
    assert "train 为 0" in text
    like = next(j for j in plan["jobs"] if j["physician_id"] == "li_ke")
    assert like["train"] == 0


def test_plan_still_shouts_when_heldout_is_zero(tmp_path):
    """对照：heldout 为 0 这条原有的警示没有被新的那条挤掉。"""
    rows = _rows()
    for r in rows:
        r["meta"]["split"] = "train"
    plan = build_plan(rows, ["ye_tianshi"], ["qwen2.5-1.5b"], tmp_path)
    text = format_plan_text(plan, [dict(BASES["qwen2.5-1.5b"], key="qwen2.5-1.5b")])
    assert "heldout 为 0" in text


def test_dry_run_on_cpu_needs_neither_peft_nor_a_gpu(tmp_path, capsys):
    """这是这一轮唯一能在沙盒里真跑的一步：20 条样本、CPU、退出码 0。"""
    rc = main(["--dry-run", "--samples", str(_samples_file(tmp_path, _rows(20)))])
    assert rc == 0
    out = capsys.readouterr().out
    assert "样本总数 20" in out
    # 十个 adapter 都在计划里
    assert out.count("→ ") >= len(BASES) * len(physicians_all(PHYSICIANS))
    assert "没有加载模型，没有训练" in out


def test_dry_run_says_what_it_did_not_check(tmp_path, capsys):
    """--dry-run 过了不等于能训起来。**没查的事要说出来**，否则它会被当成
    "训练这一步已经验证过了"。"""
    main(["--dry-run", "--samples", str(_samples_file(tmp_path, _rows(20)))])
    out = capsys.readouterr().out
    assert "没有检查的事" in out
    assert "显存" in out and "peft" in out


def test_dry_run_on_an_empty_sample_file_exits_nonzero(tmp_path, capsys):
    """空样本文件下原来会 IndexError（samples[0]）。空不是崩溃，是退出码 1 + 一句话。"""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["--dry-run", "--samples", str(empty)]) == 1
    assert "样本文件是空的" in capsys.readouterr().out


def test_check_deps_reports_each_dependency_and_exits_nonzero_when_missing(capsys):
    """逐个报而不是笼统说"环境不对"：这台机器上 torch/transformers 装了、
    peft 没装，笼统报会让人重装一遍已经有的几个 G。"""
    ok = report_deps()
    out = capsys.readouterr().out
    for mod in ("torch", "transformers", "peft", "accelerate"):
        assert mod in out
    if not ok:
        assert "requirements-train.txt" in out


def test_check_deps_exit_code_matches_report_deps(capsys):
    """`--check-deps` 的退出码就是这一项"能不能跑"的机器判据——
    报告里写一句"环境不支持"不算完成。"""
    expected = 0 if report_deps() else 1
    capsys.readouterr()
    assert main(["--check-deps"]) == expected


@pytest.mark.parametrize("base_key", sorted(BASES))
def test_both_bases_are_declared_with_a_reason_for_the_comparison(base_key):
    """两个基座是一组对照，不是两个候选：note 里要写清对照问的是什么。"""
    info = BASES[base_key]
    assert info["model"] and info["note"]
    assert isinstance(info["verified"], bool)
