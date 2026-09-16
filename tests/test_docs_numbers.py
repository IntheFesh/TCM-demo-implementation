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
    round_report_paths,
)

ROOT = Path(__file__).resolve().parent.parent


def test_the_four_fixed_docs_and_every_round_report_are_in_the_checker():
    """**有意的判据变更（R21）**：原来是"恰好这四份"。R21 起每轮一份
    `docs/reports/R<N>_report.md`，报告里带 `bench/sandbox.json:…` 凭据记号，
    所以它们也必须进核对器——而且是 glob 进来的：靠人记着往元组里加一个名字，
    忘了的那一轮读起来跟被核过的一样。"""
    names = {p.name for p in DEFAULT_CHECK_PATHS}
    assert {"RESULTS.md", "README.md", "R11-R19_report.md", "DEMO.md"} <= names
    round_names = {p.name for p in round_report_paths()}
    assert round_names, "docs/reports/ 下一份轮次报告都没有"
    assert round_names <= names, f"这些轮次报告没进核对器：{round_names - names}"


def _current_round_report():
    """最新那一轮的报告。R11–R19 那份是**合起来的历史总账**，从 R21 起它不再
    承担"当前轮"这个角色——当前轮的五个数挂在 `docs/reports/R<N>_report.md`。"""
    paths = round_report_paths()
    assert paths, "docs/reports/ 下没有任何一轮的报告"
    return paths[-1]


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
    """**当前轮**报告里的五个数要能被核。历史几轮的数是 git 可核的、不是文件可核的，
    所以只给当前轮上记号——给历史行编造凭据比不给更糟。

    **有意的判据变更（R21）**：原来查的是 `docs/R11-R19_report.md`。R21 重跑
    `bench_sandbox --all` 把 `eval/bench/sandbox.json` 整份覆盖成了 R21 的数，
    于是挂在它上面的 R19 凭据当场报了 6 处不一致——**一个会被下一轮覆盖的文件，
    不能给一个历史轮次的数当凭据**。历史值改指 git 的那个 commit，
    「当前轮」这个角色转给 `docs/reports/R<N>_report.md`。"""
    text = _current_round_report().read_text(encoding="utf-8")
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
    # **有意的判据变更（R21）**：原来要求最后一行 == bench 文件里的条数。
    # 那时这份文件就是"当前轮报告"，现在它是一张**封版的历史表**（最后一行是
    # R19 的 2693，凭据在 `git show a22398a:eval/bench/sandbox.json`）。
    # 还能钉住的是那条硬约束本身——条数只增不减，所以历史表的末行不许超过当前实测。
    latest, path = evidence_value("bench.pytest_passed")
    if path.exists() and latest is not None:
        assert counts[-1] <= latest, (
            f"九轮表最后一行是 {counts[-1]}，而 {path.name} 里是 {latest}"
            "——条数只增不减，历史表的末行不该比当前实测还大")


def test_the_current_round_report_states_the_measured_test_count():
    """当前轮报告里写的条数必须等于 bench 文件里**这一轮实测**的条数。
    这一条是上面那条移交过来的责任：写死在文档里的条数一旦跟文件不同步，
    它就变成一个没人核的数——这个项目漂过三次的正是这种。"""
    latest, bench_path = evidence_value("bench.pytest_passed")
    if not bench_path.exists() or latest is None:
        pytest.skip("eval/bench/sandbox.json 还没有 pytest_passed")
    report = _current_round_report()
    text = report.read_text(encoding="utf-8")
    token = f"bench/sandbox.json:bench.pytest_passed={latest}"
    assert token in text, f"{report.name} 里没有 `{token}`"
