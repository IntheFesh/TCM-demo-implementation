"""文档里的评测数字全部可核：`DEFAULT_CHECK_PATHS` 里的每份文档都要通过凭据核对。"""
from __future__ import annotations

import pytest

from scripts.collect_results import (
    DEFAULT_CHECK_PATHS,
    _is_metric_table_header,
    check,
    metric_rows,
)


@pytest.mark.parametrize("path", DEFAULT_CHECK_PATHS, ids=lambda p: p.name)
def test_each_checked_doc_passes(path):
    result = check(path.read_text(encoding="utf-8"))
    assert result["mismatches"] == [], result["mismatches"]
    assert result["unresolved"] == [], result["unresolved"]
    assert result["missing_in_line"] == [], result["missing_in_line"]
    assert result["rows_unmarked"] == [], result["rows_unmarked"]
    assert result["md_json_drift"] == [], result["md_json_drift"]


def test_metric_table_header_needs_a_separator_row_under_it():
    """正文单元格里提到「凭据」的行不是表头：表头的下一行必须是分隔行（`|---|---|`）。
    否则它后面的普通表格会被整张当成指标表，每一行都报漏标。"""
    lines = [
        "| # | 指标 | 凭据 |",
        "|---|---|---|",
        "| 1 | 某个数 | `a.json:k=1` |",
        "",
        "| 说明 | 凭据记号的写法见上 |",    # 正文行，不是表头
        "| 6 | 录制 + 回放 | 23/23 |",        # 不是指标行
    ]
    assert _is_metric_table_header(lines, 0) is True
    assert _is_metric_table_header(lines, 4) is False
    rows = metric_rows("\n".join(lines))
    assert len(rows) == 1 and rows[0].startswith("| 1 |")
