"""R59：`scripts/check_herb_coverage.py` 把用户真机那段一次性覆盖率探针固化
成可重复跑的脚本。这里只测纯逻辑（`count_herb_names`、阈值判定），不依赖真实
`cases.json`——沙盒里没有这个文件，跟 CLAUDE.md「tests/ 不需要真实数据、秒级
跑完」的约束一致；真实覆盖率数字由用户在真机跑 `python -m scripts.check_herb_coverage`
核验（见 R59 报告）。
"""
from __future__ import annotations

import json

from scripts.check_herb_coverage import count_herb_names, main


def test_count_herb_names_counts_occurrences_across_records():
    records = [
        {"herbs": ["白术", "茯苓"]},
        {"herbs": ["白术", " 甘草 "]},
        {"herbs": []},
        {"herbs": None},
    ]
    counts = count_herb_names(records)
    assert counts["白术"] == 2
    assert counts["茯苓"] == 1
    assert counts["甘草"] == 1  # 前后空白要 strip 掉，否则会被当成另一种写法


def test_count_herb_names_skips_blank_entries():
    records = [{"herbs": ["", "  ", "白术"]}]
    counts = count_herb_names(records)
    assert list(counts) == ["白术"]


def test_main_exits_zero_when_unresolved_ratio_is_within_threshold(tmp_path, capsys):
    cases_path = tmp_path / "cases.json"
    # 三味都是本体真实收录的正名，归一后应该全部可查到——不可解析比例 0%
    cases_path.write_text(json.dumps([
        {"herbs": ["白术"]}, {"herbs": ["茯苓"]}, {"herbs": ["甘草"]},
    ], ensure_ascii=False), encoding="utf-8")
    rc = main(["--cases-path", str(cases_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "✅ 达标" in out


def test_main_exits_nonzero_when_unresolved_ratio_exceeds_threshold(tmp_path, capsys):
    cases_path = tmp_path / "cases.json"
    # 全是本体和别名表都不认识的写法——不可解析比例 100%，必须判未达标
    cases_path.write_text(json.dumps([
        {"herbs": ["这不是任何真实药名甲", "这不是任何真实药名乙"]},
    ], ensure_ascii=False), encoding="utf-8")
    rc = main(["--cases-path", str(cases_path)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "❌ 未达标" in out


def test_main_reports_a_missing_cases_file_honestly(tmp_path, capsys):
    missing = tmp_path / "does_not_exist.json"
    rc = main(["--cases-path", str(missing)])
    assert rc == 2
    assert "不在" in capsys.readouterr().out


def test_main_respects_a_custom_threshold(tmp_path, capsys):
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([
        {"herbs": ["白术", "这不是任何真实药名甲"]},
    ], ensure_ascii=False), encoding="utf-8")
    # 1/2 查不到 = 50%——阈值放宽到 60% 应该过，收紧到 10% 应该不过
    assert main(["--cases-path", str(cases_path), "--max-unresolved", "0.6"]) == 0
    assert main(["--cases-path", str(cases_path), "--max-unresolved", "0.1"]) == 1
