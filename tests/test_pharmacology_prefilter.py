"""R8-1/R8-3：块级预过滤、heading 切块的两处修正、批量抽取入口、切块验证的预过滤输出。
零 LLM 调用。

判据全部按真实数据的排版形状写（R8 在六个源上实测），合成语料照着那些形状造：
教材 = 「# 药名」+ 行首 `【字段】`；古籍 = `<目录>` / `<篇名>药名` / `内容：…`。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from offline import extract_reference_triples as ert
from offline import pharmacology_sources as ps
from scripts import run_pharmacology_extraction as rpe
from scripts import verify_pharmacology_chunks as vpc

ROOT = Path(__file__).resolve().parent.parent

MODERN_ENTRY = ("# 麻黄  \n\nMahuang（《神农本草经》）  \n\n【药性】辛、微苦，温。归肺、膀胱经。  \n\n"
                "【功效】发汗解表，宣肺平喘，利水消肿。  \n\n【用法用量】煎服，2～10g。  \n")
MODERN_CONTINUATION = "# 【现代研究】  \n\n本品含麻黄碱、伪麻黄碱等生物碱。  \n"
RUNNING_HEAD = "# 106 中药炮制学  \n\n（承上页）炒至规定程度。  \n"
CLASSIC_ENTRY = "<目录>卷一\\上经\n\n<篇名>丹沙\n\n内容：味甘，微寒。主身体五脏百病，养精神。生山谷。\n"
CLASSIC_BOOK_HEADER = "<篇名>神农本草经\n书名：神农本草经\n作者：孙星衍  \n朝代：清  \n"
INDEX_TABLE = ("# 十五画  \n\n<html><body><table>" + "".join(
    f"<tr><td>药名{i}</td><td>{i}</td></tr>" for i in range(60)) + "</table></body></html>\n")


# ---------- 谓词与分类 ----------


def test_modern_predicate_requires_a_field_label_at_line_start():
    assert ps.is_modern_entry(MODERN_ENTRY)
    # 总论正文里提到字段名（句中）不算：炮制学"其中【炮制】项下有…"实测会被误收
    assert not ps.is_modern_entry("# 总论  \n\n2020 年版《中国药典》共收载饮片 819 种，其中【炮制】项下有处理的达 92 种。")
    assert not ps.is_modern_entry("# 第一节 发散风寒药  \n\n凡以发散风寒为主要作用的药物，称发散风寒药。")


def test_classic_predicate_accepts_pian_plus_body_or_dose_words():
    assert ps.is_classic_entry(CLASSIC_ENTRY)
    assert ps.is_classic_entry("<篇名>1．资生汤\n\n属性：治劳瘵羸弱已甚。\n生山药（一两） 玄参（五钱）")
    # 书头：只有 <篇名>书名 + 书名/作者/朝代，没有正文标记
    assert not ps.is_classic_entry(CLASSIC_BOOK_HEADER)
    # 《脾胃论》这类不带 <篇名> 的本子：方药段靠剂量词
    assert ps.is_classic_entry("柴胡一两五钱 甘草炙 黄芪 苍术泔浸 羌活已上各一两 升麻八钱 人参")
    assert not ps.is_classic_entry("太阴阳明论云：太阴阳明为表里，脾胃脉也，生病而异者何也？")


def test_entry_predicates_are_keyed_by_the_source_column_of_expected_sources():
    """判据挂在 EXPECTED_SOURCES 的 source 列上：表里出现的每个 source 值都有谓词，
    没有第二份"这是教材还是古籍"的判断。"""
    sources_in_table = {label[0] for label in ps.EXPECTED_SOURCES.values()}
    assert sources_in_table == set(ps.ENTRY_PREDICATES) == set(ps.SOURCE_TYPES)


@pytest.mark.parametrize("filename,expected", [
    ("000-神农本草经.txt", "神农本草经"), ("018-本草备要.txt", "本草备要"),
    ("中药学.md", "中药学"), ("方剂学.md", "方剂学"),
])
def test_book_title_strips_number_prefix_and_suffix(filename, expected):
    assert ps.book_title(filename) == expected


def test_classify_order_is_short_then_table_then_long_then_structure():
    assert ps.classify_block("# 中药学  \n\n（第五版）", "modern") == ps.SKIP_TOO_SHORT
    assert ps.classify_block(INDEX_TABLE, "modern") == ps.SKIP_TABLE
    # 条目里自带的小表格（占比低）不算表格块
    entry_with_table = MODERN_ENTRY + "<table><tr><td>麻黄</td><td>桂枝</td></tr></table>\n"
    assert ps.table_ratio(entry_with_table) < ps.TABLE_RATIO_MAX
    assert ps.classify_block(entry_with_table, "modern") is None
    assert ps.classify_block(MODERN_ENTRY + "正文" * 6000, "modern") == ps.SKIP_TOO_LONG
    assert ps.classify_block("# 第一节 发散风寒药  \n\n凡以发散风寒为主要作用的药物，称发散风寒药，本节共十味。",
                             "modern") == ps.SKIP_NO_STRUCTURE
    assert ps.classify_block(MODERN_ENTRY, "modern") is None
    assert ps.classify_block(CLASSIC_ENTRY, "classic") is None
    assert ps.classify_block(MODERN_ENTRY, "classic") == ps.SKIP_NO_STRUCTURE


def test_unknown_source_skips_only_the_three_structure_free_classes():
    """docx 转出来的医案 txt 没有结构判据（不猜它该长什么样），只跳过短/表格/超长。"""
    prose = "患者男，60 岁，胃脘胀痛三月，纳差，舌淡苔白，脉弦细。予香砂六君子汤加减，七剂。"
    assert ps.classify_block(prose, "unknown") is None
    assert ps.classify_block(prose, None) is None
    assert ps.classify_block("某案", "unknown") == ps.SKIP_TOO_SHORT


def test_thresholds_are_the_declared_values_and_shared_with_the_verifier():
    assert ps.MIN_ENTRY_CHARS == 30
    assert ps.TABLE_RATIO_MAX == 0.8
    assert ps.BLOCK_MAX_CHARS == 10000
    # 切块验证的第三个阈值和预过滤的「超长」必须是同一个数——两处各写一个迟早分叉
    assert vpc.BLOCK_MAX_CHARS is ps.BLOCK_MAX_CHARS
    assert vpc.EXPECTED_SOURCES is ps.EXPECTED_SOURCES


def test_prefilter_keeps_block_indexes_and_counts_add_up():
    blocks = list(enumerate(["# 中药学  \n\n（第五版）", MODERN_ENTRY, INDEX_TABLE,
                             "# 第一节 发散风寒药  \n\n凡以发散风寒为主要作用的药物，称发散风寒药，本节共十味。"]))
    kept, skipped, counts = ps.prefilter_blocks(blocks, "modern")
    assert [i for i, _ in kept] == [1]                       # 块号沿用切块时的编号
    assert [(i, r) for i, r, _ in skipped] == [(0, "过短"), (2, "表格"), (3, "无结构标记")]
    assert sum(counts.values()) == len(skipped)
    assert ps.format_prefilter_summary(4, counts) == "4 块 → 保留 1 块（跳过：过短 1 / 表格 1 / 超长 0 / 无结构标记 1）"


# ---------- heading 切块的两处修正 ----------


def test_heading_mode_does_not_start_a_block_at_a_field_label_heading():
    """OCR 把 `【临床应用】` 升成了 `#`：临床中药学每味药的用法用量都在那一块，
    按它开新块那一块里没有药名。"""
    blocks = ert.split_blocks(MODERN_ENTRY + "\n" + MODERN_CONTINUATION + "\n# 桂枝  \n\n【药性】辛、甘，温。\n",
                              "heading")
    assert len(blocks) == 2
    assert "【现代研究】" in blocks[0] and blocks[0].startswith("# 麻黄")
    assert blocks[1].startswith("# 桂枝")


@pytest.mark.parametrize("heading", ["# 106 中药炮制学  ", "# 38 方剂学  ", "# 75  ", "# 16目录  ", "# 8临床中药学  "])
def test_heading_mode_treats_page_running_heads_as_continuation(heading):
    text = MODERN_ENTRY + "\n" + heading + "\n\n【方解】方中麻黄为君。  \n\n# 桂枝  \n\n【药性】辛、甘，温。\n"
    blocks = ert.split_blocks(text, "heading")
    assert len(blocks) == 2 and "【方解】" in blocks[0], blocks


@pytest.mark.parametrize("heading", ["# 痛泻要方 67  ", "# 2.制马钱子  ", "# 1．资生汤  ", "# 第二节 辛凉解表剂  "])
def test_heading_mode_still_starts_blocks_at_real_titles_with_numbers(heading):
    """页码在后的真标题、编号标题（数字后面紧跟标点）照常开新块——只有"页码在前、
    后面至多一个书名"才是页眉。"""
    text = MODERN_ENTRY + "\n" + heading + "\n\n【组成】白术 白芍 陈皮 防风。  \n"
    blocks = ert.split_blocks(text, "heading")
    assert len(blocks) == 2 and blocks[1].startswith(heading.strip()), blocks


def test_heading_mode_splits_guji_txt_at_pian_and_attaches_the_toc_line():
    """古籍转录体例：`<篇名>` 是标题行；它前面单独成块的 `<目录>` 行并进来
    （"卷一\\上经"是品级，留着有用）。R8 之前古籍用 blank-line：`<篇名>丹沙`
    7 字短于 MIN_BLOCK_CHARS 被丢，正文块里没有药名。"""
    text = CLASSIC_ENTRY + "\n<目录>卷一\\上经\n\n<篇名>云母\n\n内容：味甘平。主身皮死肌，中风寒热。\n"
    blocks = ert.split_blocks(text, "heading")
    assert len(blocks) == 2
    assert blocks[0].startswith("<目录>卷一\\上经") and "<篇名>丹沙" in blocks[0] and "内容：味甘" in blocks[0]
    assert blocks[1].startswith("<目录>") and "<篇名>云母" in blocks[1]
    # 对照：blank-line 切法确实把药名跟正文切开了（这是改推荐模式的原因）
    old = ert.split_blocks(text, "blank-line")
    assert not any("<篇名>丹沙" in b for b in old)
    assert any(b.startswith("内容：味甘，微寒") for b in old)


def test_expected_sources_all_recommend_heading_now():
    assert {label[2] for label in ps.EXPECTED_SOURCES.values()} == {"heading"}


# ---------- 引擎：--dry-run / --no-prefilter / --limit-blocks / --only-blocks ----------


def _textbook(tmp_path: Path) -> Path:
    p = tmp_path / "中药学.md"
    p.write_text("# 中药学  \n\n（第五版）\n\n" + MODERN_ENTRY + "\n" + INDEX_TABLE + "\n"
                 "# 第一节 发散风寒药  \n\n凡以发散风寒为主要作用的药物，称发散风寒药，本节共十味。\n\n"
                 "# 桂枝  \n\n【药性】辛、甘，温。归心、肺、膀胱经。  \n\n【用法用量】煎服，3～10g。  \n",
                 encoding="utf-8")
    return p


def test_plan_blocks_returns_all_kept_skipped_and_counts(tmp_path):
    text = _textbook(tmp_path).read_text(encoding="utf-8")
    all_blocks, kept, skipped, counts = ert.plan_blocks(text, "heading", "modern")
    assert len(all_blocks) == 5 and [i for i, _ in kept] == [1, 4]
    assert {r for _i, r, _b in skipped} == {"过短", "表格", "无结构标记"}
    assert ert.plan_blocks(text, "heading", "modern", prefilter=False)[1] == all_blocks


def test_engine_dry_run_prints_the_four_skip_classes(tmp_path, capsys):
    from offline import extract_materia_medica as emm

    emm.main(["--input", str(_textbook(tmp_path)), "--source", "modern", "--book", "中药学",
              "--chunk-by", "heading", "--dry-run", "--out", str(tmp_path / "o.jsonl")])
    out = capsys.readouterr().out
    assert "预过滤：5 块 → 保留 2 块（跳过：过短 1 / 表格 1 / 超长 0 / 无结构标记 1）" in out
    assert "预估调用数 2" in out


def test_engine_no_prefilter_and_limit_blocks_alias(tmp_path, capsys):
    from offline import extract_materia_medica as emm

    base = ["--input", str(_textbook(tmp_path)), "--source", "modern", "--book", "中药学",
            "--chunk-by", "heading", "--dry-run", "--out", str(tmp_path / "o.jsonl")]
    emm.main(base + ["--no-prefilter"])
    assert "预过滤已关（--no-prefilter）" in capsys.readouterr().out
    emm.main(base + ["--limit-blocks", "1"])
    out = capsys.readouterr().out
    assert "本次将处理 1 块 = 预估调用数 1" in out


def test_engine_refuses_only_blocks_that_the_prefilter_skipped(tmp_path):
    from offline import extract_materia_medica as emm

    with pytest.raises(SystemExit, match="被预过滤跳过"):
        emm.main(["--input", str(_textbook(tmp_path)), "--source", "modern", "--book", "中药学",
                  "--chunk-by", "heading", "--only-blocks", "2", "--out", str(tmp_path / "o.jsonl")])


# ---------- 切块验证：预过滤输出 ----------


def test_verifier_prints_kept_first_blocks_and_the_filtered_first_five(tmp_path, capsys):
    path = _textbook(tmp_path)
    problems = vpc.report_source(path, ps.EXPECTED_SOURCES["中药学.md"], None, show=3, preview_chars=80)
    out = capsys.readouterr().out
    assert "预过滤（source=modern 的结构判据）：5 块 → 保留 2 块" in out
    assert "前 2 块原文" in out and "第 1 块" in out and "# 麻黄" in out
    assert "被预过滤跳过的前 3 块" in out
    assert "[过短]" in out and "[表格]" in out and "[无结构标记]" in out
    # 阈值对保留块判：2 块 < MIN_BLOCKS 是这份合成语料太小，不是切错
    assert any("块数 2 <" in p for p in problems)


def test_verifier_no_prefilter_restores_the_r4_behaviour(tmp_path, capsys):
    path = _textbook(tmp_path)
    vpc.report_source(path, ps.EXPECTED_SOURCES["中药学.md"], None, show=1, preview_chars=80, prefilter=False)
    out = capsys.readouterr().out
    assert "预过滤" not in out and "第 0 块" in out and "被预过滤跳过" not in out


def test_verifier_main_prints_the_total_estimated_calls(tmp_path, capsys):
    _textbook(tmp_path)
    vpc.main(["--books-dir", str(tmp_path), "--show", "0"])
    out = capsys.readouterr().out
    assert "合计（1 个源）：切 5 块 → 保留 2 块" in out and "真实抽取预估调用数 2" in out


def test_verifier_file_mode_takes_source_for_the_structure_rule(tmp_path, capsys):
    p = tmp_path / "脾胃论.txt"
    # 第一块 9 字：长过切块函数的 MIN_BLOCK_CHARS=8（不然根本不成块）、短于预过滤的
    # MIN_ENTRY_CHARS=30——两个下限回答的不是同一个问题，见 pharmacology_sources
    p.write_text("图书在版编目数据\n\n如果你想获得更多免费电子书请加小编 QQ，这一段是推广广告不是正文。\n\n"
                 "柴胡一两五钱 甘草炙 黄芪 苍术泔浸去黑皮 羌活已上各一两 升麻八钱 人参 黄芩已上各七钱\n",
                 encoding="utf-8")
    assert vpc.main(["--file", str(p), "--source", "classic", "--show", "1"]) in (0, 1)
    out = capsys.readouterr().out
    assert "source=classic 的结构判据" in out
    assert "[过短]" in out and "[无结构标记]" in out
    assert "柴胡一两五钱" in out


# ---------- 批量入口 ----------


def test_build_argv_takes_everything_from_the_source_table():
    argv = rpe.build_argv("000-神农本草经.txt", Path("books/000-神农本草经.txt"), 5, True, False, True)
    assert argv == ["--input", "books/000-神农本草经.txt", "--source", "classic", "--book", "神农本草经",
                    "--chunk-by", "heading", "--limit-blocks", "5", "--dry-run", "--crosscheck"]
    # 方剂没有用量可对，--crosscheck 不传给它
    assert "--crosscheck" not in rpe.build_argv("方剂学.md", Path("x"), None, False, False, True)
    assert "--no-prefilter" in rpe.build_argv("方剂学.md", Path("x"), None, False, True, False)


def test_run_all_dry_run_over_present_sources_and_missing_reported(tmp_path, capsys):
    _textbook(tmp_path)
    (tmp_path / "000-神农本草经.txt").write_text(CLASSIC_BOOK_HEADER + "\n" + CLASSIC_ENTRY, encoding="utf-8")
    assert rpe.run_all(tmp_path, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "=== 中药学.md" in out and "=== 000-神农本草经.txt" in out
    assert "预估调用数 2" in out and "预估调用数 1" in out
    assert "本次跳过（不算失败，但它们没有被抽）" in out and "方剂学.md" in out


def test_run_all_returns_2_when_nothing_downloaded_or_unknown_source(tmp_path, capsys):
    assert rpe.run_all(tmp_path) == 2
    assert "还没下载" in capsys.readouterr().err
    assert rpe.run_all(tmp_path, only_source="不存在.md") == 2


def test_run_all_one_failing_source_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    _textbook(tmp_path)
    (tmp_path / "方剂学.md").write_text(MODERN_ENTRY, encoding="utf-8")
    ran = []

    def boom(argv):
        raise RuntimeError("API 抖了")

    def fine(argv):
        ran.append(argv[1])

    monkeypatch.setitem(rpe.ENTRY_MAINS, "materia_medica", boom)
    monkeypatch.setitem(rpe.ENTRY_MAINS, "formulary", fine)
    assert rpe.run_all(tmp_path) == 1
    assert ran and ran[0].endswith("方剂学.md")
    err = capsys.readouterr().err
    assert "中药学.md 没跑完：RuntimeError: API 抖了" in err


# ---------- 剧本：段 5 的数字有来源 ----------


def test_onsite_segment_5_uses_the_batch_entry_and_states_where_its_number_comes_from():
    text = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    assert "python -m scripts.run_pharmacology_extraction --limit-blocks 5" in text
    assert "python -m scripts.run_pharmacology_extraction --crosscheck" in text
    assert "offline.extract_materia_medica --limit 5\n" not in text
    import re

    row = next(line for line in text.splitlines() if line.strip().startswith('"5|'))
    _n, _name, calls, _gate, note = row.strip().strip('"').split("|")
    kept = int(re.search(r"预过滤后 (\d+) 块", note).group(1))
    assert int(calls) == kept + 6 * 5, "段 5 = 六源预过滤后的块数 + 每源 5 块试抽"
    assert "verify_pharmacology_chunks" in note


def test_onsite_segment_1_normalizes_local_corpora_before_verifying_them():
    text = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    seg1 = text[text.index("seg_1() {"): text.index("seg_2() {")]
    assert "python -m scripts.normalize_local_corpora" in seg1
    assert "--file data/local_corpora/脾胃论.txt --source classic" in seg1
    assert seg1.index("normalize_local_corpora") < seg1.index("verify_pharmacology_chunks")


def test_manifest_in_repo_matches_the_declared_corpora():
    """提交进仓库的 MANIFEST 必须是脚本生成的那份（三条、字段齐、声明跟表一致）。"""
    from scripts import normalize_local_corpora as nlc

    manifest = json.loads((ROOT / "data" / "local_corpora" / "MANIFEST.json").read_text(encoding="utf-8"))
    entries = {e["file"]: e for e in manifest["entries"]}
    assert set(entries) == {f"local_corpora/{s.target}" for s in nlc.LOCAL_CORPORA}
    for spec in nlc.LOCAL_CORPORA:
        e = entries[f"local_corpora/{spec.target}"]
        assert e["out_of_scope"] == spec.out_of_scope and e["copyright_status"] == spec.copyright_status
        for key in ("original_name", "bytes", "sha256", "encoding", "origin", "scope_stats"):
            assert key in e, key
        assert (ROOT / "data" / e["file"]).exists()
