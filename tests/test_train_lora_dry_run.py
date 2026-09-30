"""多个基座 × 五位医家：`--dry-run` 在 CPU 上可以运行，不需要 peft 与 GPU。

测的是离线能测的两件事：计划算得对不对、缺依赖时报得对不对。真正的训练需要
`requirements-train.txt` 与 GPU，由 `--check-deps` 的退出码说明能否运行。
"""
from __future__ import annotations

import json


from core.physicians import PHYSICIANS, physicians_all
from scripts.train_lora import (
    build_plan,
    format_plan_text,
    main,
    report_deps,
    resolve_base,
)

BASES = [resolve_base("org/base-a"), resolve_base("/path/to/base-b")]
BASE_KEYS = [b["key"] for b in BASES]


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


def test_plan_covers_every_base_times_every_physician(tmp_path):
    """判据是"基座数 × 注册表里的医家数"，不写死 adapter 的个数。"""
    pids = sorted(physicians_all(PHYSICIANS))
    assert len(pids) == 5, f"注册表现在是 {len(pids)} 位：{pids}"
    plan = build_plan(_rows(), pids, BASE_KEYS, tmp_path)
    assert len(plan["jobs"]) == len(BASES) * len(pids)
    assert {j["physician_id"] for j in plan["jobs"]} == set(pids)
    assert {j["base"] for j in plan["jobs"]} == set(BASE_KEYS)


def test_each_adapter_gets_its_own_output_dir(tmp_path):
    """每个 adapter 一个目录。撞目录会让后训的覆盖先训的，基座之间的对照也就没了。"""
    pids = sorted(physicians_all(PHYSICIANS))
    plan = build_plan(_rows(), pids, BASE_KEYS, tmp_path)
    dirs = [j["out_dir"] for j in plan["jobs"]]
    assert len(set(dirs)) == len(dirs)


def test_plan_shouts_when_a_physician_has_no_training_samples(tmp_path):
    """某位医家在样本里没有条目时 train 是 0。计划里必须提示：否则 adapter 目录
    照样建出来，其中几个是空训练，看目录看不出区别。
    """
    rows = [r for r in _rows() if r["meta"]["physician_id"] != "li_ke"]
    pids = sorted(physicians_all(PHYSICIANS))
    plan = build_plan(rows, pids, [BASE_KEYS[0]], tmp_path)
    text = format_plan_text(plan, [BASES[0]])
    assert "train 为 0" in text
    like = next(j for j in plan["jobs"] if j["physician_id"] == "li_ke")
    assert like["train"] == 0


def test_plan_still_shouts_when_heldout_is_zero(tmp_path):
    """heldout 为 0 的警示与 train 为 0 的警示互不影响。"""
    rows = _rows()
    for r in rows:
        r["meta"]["split"] = "train"
    plan = build_plan(rows, ["ye_tianshi"], [BASE_KEYS[0]], tmp_path)
    text = format_plan_text(plan, [BASES[0]])
    assert "heldout 为 0" in text


def test_dry_run_on_cpu_needs_neither_peft_nor_a_gpu(tmp_path, capsys):
    """20 条样本、CPU、退出码 0。"""
    rc = main(["--dry-run", "--samples", str(_samples_file(tmp_path, _rows(20))),
               "--base-model", "org/base-a", "--base-model", "/path/to/base-b"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "样本总数 20" in out
    # 两个基座 × 注册表里的每位医家，每个 adapter 一行
    assert out.count("→ ") >= len(BASES) * len(physicians_all(PHYSICIANS))
    assert "没有加载模型，没有训练" in out


def test_dry_run_says_what_it_did_not_check(tmp_path, capsys):
    """--dry-run 过了不等于能训起来。**没查的事要说出来**，否则它会被当成
    "训练这一步已经验证过了"。"""
    main(["--dry-run", "--samples", str(_samples_file(tmp_path, _rows(20))),
          "--base-model", "org/base-a"])
    out = capsys.readouterr().out
    assert "没有检查的事" in out
    assert "显存" in out and "peft" in out


def test_dry_run_on_an_empty_sample_file_exits_nonzero(tmp_path, capsys):
    """空样本文件：退出码 1 + 一句说明，而不是崩溃。"""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(["--dry-run", "--samples", str(empty), "--base-model", "org/base-a"]) == 1
    assert "样本文件是空的" in capsys.readouterr().out


def test_check_deps_reports_each_dependency_and_exits_nonzero_when_missing(capsys):
    """逐个报而不是笼统说"环境不对"：torch/transformers 装了、peft 没装时，
    笼统报会让人重装一遍已经有的几个 G。"""
    ok = report_deps()
    out = capsys.readouterr().out
    for mod in ("torch", "transformers", "peft", "accelerate"):
        assert mod in out
    if not ok:
        assert "requirements-train.txt" in out


def test_check_deps_exit_code_matches_report_deps(capsys):
    """`--check-deps` 的退出码就是"能不能训练"的机器判据。"""
    expected = 0 if report_deps() else 1
    capsys.readouterr()
    assert main(["--check-deps"]) == expected
