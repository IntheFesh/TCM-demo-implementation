"""R19：三份文档的数字全部可核或标 ⏳。

R19 结束时是**四份**，走同一个核对器：`eval/RESULTS.md`、`README.md`、
`docs/R11-R19_report.md`、`DEMO.md`。

DEMO.md 是这一轮加进去的，起因是这里的 `test_demo_md_does_not_copy_any_metric_value`
——它拿注册表里的**真值**去 DEMO.md 里搜，搜出 4 个：ε 全局均值 0.2611、
SDT 关安全闸前后的 23.173 / 27.729、旧检索层的 0.366。而 DEMO.md 当时写着
"这份 DEMO 不复制数字"：**声称和实际对不上**。修法是给那 4 个数补凭据记号、
把声称改成"讲解里必须出现的数每个都带凭据"，而不是把数删掉——那几个数是讲解的一部分。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.collect_results import (
    DEFAULT_CHECK_PATHS,
    EVIDENCE,
    _is_metric_table_header,
    check,
    evidence_value,
    metric_rows,
)

ROOT = Path(__file__).resolve().parent.parent


def test_all_four_docs_are_in_the_checker():
    names = {p.name for p in DEFAULT_CHECK_PATHS}
    assert names == {"RESULTS.md", "README.md", "R11-R19_report.md", "DEMO.md"}


@pytest.mark.parametrize("path", DEFAULT_CHECK_PATHS, ids=lambda p: p.name)
def test_each_checked_doc_passes(path):
    """`--check` 的退出码是这一轮五个数里的第四个。"""
    result = check(path.read_text(encoding="utf-8"))
    assert result["mismatches"] == [], result["mismatches"]
    assert result["unresolved"] == [], result["unresolved"]
    assert result["missing_in_line"] == [], result["missing_in_line"]
    assert result["rows_unmarked"] == [], result["rows_unmarked"]


def test_metric_table_header_needs_a_separator_row_under_it():
    """R19 实测撞到的：正文单元格里提到「凭据」的行被当成了表头，
    于是它**后面**那张表整张被当成指标表，5 行报成漏标凭据。

    表头的结构特征是下一行是分隔行（`|---|---|`），正文行没有这个特征。
    """
    lines = [
        "| # | 指标 | 凭据 |",
        "|---|---|---|",
        "| 1 | 某个数 | ✅ `a.json:k=1` |",
        "",
        "| R17 | 两个凭据盲区纳入注册表 |",   # 正文行，不是表头
        "| 6 | 录制 + 回放 | 23/23 |",        # 不该被当成指标行
    ]
    assert _is_metric_table_header(lines, 0) is True
    assert _is_metric_table_header(lines, 4) is False
    rows = metric_rows("\n".join(lines))
    assert len(rows) == 1 and rows[0].startswith("| 1 |")


def test_the_report_pins_this_rounds_five_numbers():
    """九轮表里 R19 那一行的数要能被核。历史几轮的数是 git 可核的、不是文件可核的，
    所以只给 R19 那一行上记号——给历史行编造凭据比不给更糟。"""
    text = (ROOT / "docs" / "R11-R19_report.md").read_text(encoding="utf-8")
    for key in ("bench.pytest_passed", "bench.pytest_skipped", "bench.pytest_failed",
                "bench.pytest_wall_s", "bench.playwright_states_passed"):
        assert f"bench/sandbox.json:{key}=" in text, key


def test_the_report_says_which_numbers_deliberately_have_no_evidence():
    """ruff / min_length 计数 / --check 退出码是命令的当场输出，不落任何文件。
    **说出来**比留个空白好——留空白的话下一个人会以为是漏标了。"""
    text = (ROOT / "docs" / "R11-R19_report.md").read_text(encoding="utf-8")
    assert "没有凭据记号也不该有" in text
    assert "不落任何文件" in text


def test_every_metric_value_quoted_in_demo_md_carries_an_evidence_mark():
    """DEMO.md 里出现的每个指标值都要在**同一行**带凭据记号。

    拿注册表里**当前真实的**值去 DEMO.md 里搜——这是发现"声称不复制数字、
    实际引了 4 个"的那条判据。现在它的作用反过来了：引可以，但必须带记号，
    否则那个数会随文件漂而文档不知道。
    """
    demo_path = ROOT / "DEMO.md"
    demo = demo_path.read_text(encoding="utf-8")
    lines = demo.splitlines()
    unmarked = []
    for key in EVIDENCE:
        value, path = evidence_value(key)
        if value is None or not path.exists():
            continue
        text = str(value)
        # 一位数和两位数太容易撞（端口、条数、章节号），只查三位以上或带小数点的
        if len(text.replace(".", "").replace("-", "")) < 3:
            continue
        for ln in lines:
            if text in ln and f"{key}=" not in ln:
                # 同一行里可能是另一个键的值（0.2611 既是 epsilon_online.mean
                # 也是 stratification.global_mean），只要这一行有**某个**记号就算带了
                if ":" not in ln or "=" not in ln:
                    unmarked.append(f"{key}={text} @ {ln.strip()[:60]}")
    assert unmarked == [], f"DEMO.md 里这些指标值没带凭据记号：{unmarked}"


def test_demo_md_points_at_the_single_source_and_the_checker():
    demo = (ROOT / "DEMO.md").read_text(encoding="utf-8")
    # 原来写的是"这份 DEMO 不复制数字"，而它实际引了 4 个——**有意的表述变更**：
    # 改成"不另维护一份 + 引的数都带凭据"，那才是它真正做到的事。
    assert "这份 DEMO **不另维护一份**" in demo
    assert "每一个都带凭据记号" in demo
    assert "scripts.collect_results --check" in demo


def test_the_report_has_the_onsite_checklist_with_commands_and_criteria():
    """「无法完成项」只允许"需要 GPU / 需要 AutoDL 上的文件 / 需要用户提供 X"三类，
    每条附上机命令。这条钉住那张表的形状。"""
    text = (ROOT / "docs" / "R11-R19_report.md").read_text(encoding="utf-8")
    sect = text[text.index("## 四、上机 ⏳ 清单"):]
    sect = sect[:sect.index("\n## ")]
    rows = [ln for ln in sect.splitlines() if ln.startswith("| ") and "---" not in ln]
    # 表头 + 至少 11 项
    assert len(rows) >= 12, f"清单只有 {len(rows) - 1} 项"
    for ln in rows[1:]:
        assert "`" in ln, f"这一项没给命令：{ln[:60]}"
    for must in ("verify_replay", "bench_startup", "train_lora", "verify_role_fill",
                 "run_pharmacology_extraction", "build_syndrome_textbook"):
        assert must in sect, f"清单里少了 {must}"


def test_the_report_records_the_nine_round_test_counts_monotonically():
    """测试条数只增不减是硬约束。九轮表里的数要能体现这一点——
    这条同时是"表被手改坏了"的闸门。"""
    import re

    text = (ROOT / "docs" / "R11-R19_report.md").read_text(encoding="utf-8")
    sect = text[text.index("## 一、九轮五个数"):]
    sect = sect[:sect.index("**R19 那一行")]
    counts = [int(m) for m in re.findall(r"\|\s*\*?\*?(\d{4})\s*/\s*\d+\s*/\s*0", sect)]
    assert len(counts) == 11, f"表里只解析出 {len(counts)} 行：{counts}"
    assert counts == sorted(counts), f"条数不是单调不减：{counts}"
    assert counts[0] == 2279, "第一行是 R11 的 2279，它是这一串的起点"
    # 最后一行不写死：它必须等于 bench 文件里**这一轮实测**的条数。
    # 写死的话每轮改两处（文件和这里），而这两处一旦不同步，
    # 文档里的数就变成了一个没人核的数——这个项目漂过三次的正是这种。
    latest, path = evidence_value("bench.pytest_passed")
    if path.exists() and latest is not None:
        assert counts[-1] == latest, (
            f"九轮表最后一行是 {counts[-1]}，而 {path.name} 里是 {latest}")
