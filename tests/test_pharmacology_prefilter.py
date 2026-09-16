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


def test_unknown_source_skips_only_the_structure_free_classes():
    """docx 转出来的医案 txt 没有结构判据（不猜它该长什么样），只跳过跟源类型无关的
    那几类（过短/表格/索引/超长）。R8 收尾加了「索引」类，所以这条的名字从"三类"改成
    不带数字的说法——断言本身没放松。"""
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
    # R8 收尾在「表格」后面插了「索引」类（纯文本的药名-页码索引，表格占比拦不住它），
    # 所以这一行从四个数变成五个数。**这是有意的契约变更**：五个数仍然加起来等于跳过
    # 总数（上一行断言），顺序仍然是 SKIP_REASONS 的顺序，没有放松任何判据。
    assert ps.format_prefilter_summary(4, counts) == (
        "4 块 → 保留 1 块（跳过：过短 1 / 表格 1 / 索引 0 / 超长 0 / 无结构标记 1）")


# ---------- 索引类（R8 收尾） ----------


PAGE_INDEX_BLOCK = "# 附药：海金沙藤 196  \n\n石韦 196  \n冬葵子 196  \n灯心草 197  \n草 198\n"
FUYAO_ENTRY = ("# 附药：葛花  \n\n本品为豆科植物野葛的未开放花蕾。性味甘，平；归脾、胃经。"
               "功能解酒毒，醒脾和胃。主要用于饮酒过度，头痛头昏、烦渴、呕吐等症。常用量 3～15g。  \n")


def test_index_line_ratio_separates_the_page_index_from_a_real_fuyao_entry():
    """真实数据上这两群分得很开：目录页的附药索引 1.0，15 个真附药条目全是 0.0。"""
    assert ps.index_line_ratio(PAGE_INDEX_BLOCK) == 1.0
    assert ps.index_line_ratio(FUYAO_ENTRY) == 0.0
    # 不足 INDEX_MIN_LINES 行正文不判这一类——一行的巧合不构成"这是个索引"
    assert ps.index_line_ratio("# 附药：某药 196  \n\n石韦 196\n") == 0.0


def test_page_index_block_is_skipped_and_the_real_entry_is_kept():
    """`_FUYAO_TITLE_RE` 只看标题行，所以目录页的附药索引也顶着「# 附药：」——
    正文是「药名 + 页码」列表，靠索引行占比拦下来。"""
    assert ps.is_modern_entry(PAGE_INDEX_BLOCK)          # 标题形状确实像附药条目
    assert ps.classify_block(PAGE_INDEX_BLOCK, "modern") == ps.SKIP_INDEX
    assert ps.classify_block(FUYAO_ENTRY, "modern") is None


def test_index_is_judged_before_长度_and_structure():
    """判序：索引在「超长」和「结构标记」之前——一个既超长又是索引的块该记成索引
    （它是什么，比它多长更有信息量），而且古籍源的索引页也要拦下来。"""
    long_index = PAGE_INDEX_BLOCK + "".join(f"药名{i} {i}\n" for i in range(4000))
    assert len(long_index) > ps.BLOCK_MAX_CHARS
    assert ps.classify_block(long_index, "modern") == ps.SKIP_INDEX
    assert ps.classify_block(PAGE_INDEX_BLOCK, "classic") == ps.SKIP_INDEX
    assert ps.classify_block(PAGE_INDEX_BLOCK, "unknown") == ps.SKIP_INDEX


def test_skip_reasons_order_is_the_documented_judgement_order():
    assert ps.SKIP_REASONS == ("过短", "表格", "索引", "超长", "无结构标记")


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


@pytest.mark.parametrize("heading", ["# 《金匮要略》  ", "# Xiongdanfen（《新修本草》)  ", "# Chuanbeimu（《神农本草经》）  "])
def test_heading_mode_treats_source_attribution_lines_as_continuation(heading):
    """R8 审查（review:chunker）在真实数据上抓出来的：方剂学 35 张方、中药学 2 味药的
    标题下面那行「出处书名 / 拼音」被 OCR 升成了 `#`，真标题（`# 大黄附子汤`，6 字）
    单独成块短于 MIN_BLOCK_CHARS 被丢，正文挂在 `# 《金匮要略》` 下面——块里没有方名。"""
    text = "# 大黄附子汤  \n\n" + heading + "\n\n【组成】大黄三两 附子三枚 细辛二两  \n\n【功用】温里散寒，通便止痛。  \n"
    blocks = ert.split_blocks(text, "heading")
    assert len(blocks) == 1 and blocks[0].startswith("# 大黄附子汤") and "【组成】" in blocks[0], blocks


def test_heading_mode_keeps_real_titles_that_merely_contain_a_book_name():
    text = MODERN_ENTRY + "\n# 一、现存最早的本草专著 一《神农本草经》  \n\n【作者】不详。  \n\n【成书年代】东汉末年。  \n"
    blocks = ert.split_blocks(text, "heading")
    assert len(blocks) == 2 and blocks[1].startswith("# 一、现存最早的本草专著")


def test_modern_predicate_accepts_fuyao_sub_entries_written_in_prose():
    """R8 审查（review:prefilter）：中药学 15 个「# 附药：葛花」子条目字段是散文、没有
    `【】`，原判据把 22 味药全丢了。「附药：」是标题行的结构标记，跟 `【字段】` 同一性质。"""
    fuyao = ("# 附药：葛花  \n\n本品为豆科植物野葛的未开放花蕾。性味甘，平；归脾、胃经。"
             "功能解酒毒，醒脾和胃。常用量 3～15g。  \n")
    assert ps.is_modern_entry(fuyao)
    assert ps.classify_block(fuyao, "modern") is None


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


def test_engine_dry_run_prints_every_skip_class(tmp_path, capsys):
    """R8 收尾加了「索引」类，所以这一行从四个数变成五个数（有意的契约变更，
    见 test_prefilter_keeps_block_indexes_and_counts_add_up 里的说明）。"""
    from offline import extract_materia_medica as emm

    emm.main(["--input", str(_textbook(tmp_path)), "--source", "modern", "--book", "中药学",
              "--chunk-by", "heading", "--dry-run", "--out", str(tmp_path / "o.jsonl")])
    out = capsys.readouterr().out
    assert "预过滤：5 块 → 保留 2 块（跳过：过短 1 / 表格 1 / 索引 0 / 超长 0 / 无结构标记 1）" in out
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

    # R28：段表从五格变成七格（多了执行序和检索模式）。**解析走
    # scripts.onsite_plan.parse_segments**，不在这里再抠一次正则——
    # 各写一份的后果这一轮已经吃到了：格子一变，四个测试文件同时炸。
    from scripts.onsite_plan import parse_segments

    seg5 = next(r for r in parse_segments(text) if r["num"] == "5")
    calls, note = seg5["calls"], seg5["note"]
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


# ---------- 非参考文献输入的闸门（R8 收尾） ----------


def _case_txt(tmp_path: Path) -> Path:
    """规范名就是闸门的判据（只看文件名，不看目录）——从别处拷一份改成这个名字
    同样该被拦住。"""
    p = tmp_path / "李可医案.txt"
    p.write_text("目录：1.1 脑瘤头痛...1 1.2 鼻硬结症..\n\n" + CLASSIC_ENTRY, encoding="utf-8")
    return p


def test_engine_refuses_a_non_reference_local_corpus_before_spending_a_call(tmp_path, monkeypatch):
    """**在花第一次调用之前**拦住"拿医案去抽本草三元组"：药理层抽的是性味/归经/
    功效/用量，医案里没有这些字段。"""
    from offline import extract_materia_medica as emm

    def boom():  # 真调了模型就会炸——证明拦在调用之前
        raise AssertionError("不该走到调模型这一步")

    monkeypatch.setattr(ert, "get_llm", boom)
    with pytest.raises(SystemExit, match="拒绝抽取"):
        emm.main(["--input", str(_case_txt(tmp_path)), "--source", "classic",
                  "--book", "李可医案", "--out", str(tmp_path / "o.jsonl")])
    assert not (tmp_path / "o.jsonl").exists()


@pytest.mark.parametrize("flag", ["--include-out-of-scope", "--allow-non-reference-input"])
def test_engine_lets_it_through_with_either_spelling_of_the_override(tmp_path, capsys, flag):
    from offline import extract_materia_medica as emm

    emm.main(["--input", str(_case_txt(tmp_path)), "--source", "classic", "--book", "李可医案",
              "--dry-run", flag, "--out", str(tmp_path / "o.jsonl")])
    assert "预估调用数" in capsys.readouterr().out


def test_engine_does_not_second_guess_files_outside_the_declaration_table(tmp_path, capsys):
    """不在声明表里的文件一律放行——用户自己下的本草/方书不该被这张表挡住。"""
    from offline import extract_materia_medica as emm

    emm.main(["--input", str(_textbook(tmp_path)), "--source", "modern", "--book", "中药学",
              "--chunk-by", "heading", "--dry-run", "--out", str(tmp_path / "o.jsonl")])
    assert "预估调用数" in capsys.readouterr().out


def test_batch_dry_run_says_out_loud_that_local_corpora_are_not_included(tmp_path, capsys):
    """默认行为要说出来：不打这一行，"六个源"会被读成"所有语料"。"""
    _textbook(tmp_path)
    assert rpe.run_all(tmp_path, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "本地语料：3 份**都不在药理层抽取范围内**（其中 out_of_scope 2 份）" in out
    for name in ("王云启医案.docx", "李可医案.docx", "脾胃论.txt"):
        assert name in out


def test_verifier_file_mode_says_the_block_count_is_not_a_call_estimate(tmp_path, capsys):
    """段 1 对本地语料跑这个脚本只为看段落粒度。原来它照样打「真实抽取预估调用数
    937」——那句会被读成"该花 937 次调用"。"""
    p = tmp_path / "王云启医案.txt"
    # 够 MIN_BLOCKS=50 块，好让退出码是 0——这条测的是措辞，不是阈值
    p.write_text("\n\n".join(
        [f"序言 {i}：认识云启主任已是多年，常闻其老师、同事、同道均翘首称颂。" for i in range(30)]
        + [f"第 {i} 案：肝癌患者，胃脘胀痛，纳差，予肝复方加减，七剂。" for i in range(30)]),
        encoding="utf-8")
    assert vpc.main(["--file", str(p), "--show", "0"]) == 0
    out = capsys.readouterr().out
    assert "不是**抽取预估调用数" in out
    assert "不进抽取**，不是调用数" in out
    assert "真实抽取预估调用数" not in out


def test_onsite_segment_5_number_tracks_the_measured_kept_blocks():
    import re

    text = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    # R28：段表从五格变成七格（多了执行序和检索模式）。**解析走
    # scripts.onsite_plan.parse_segments**，不在这里再抠一次正则——
    # 各写一份的后果这一轮已经吃到了：格子一变，四个测试文件同时炸。
    from scripts.onsite_plan import parse_segments

    seg5 = next(r for r in parse_segments(text) if r["num"] == "5")
    calls, note = seg5["calls"], seg5["note"]
    kept = int(re.search(r"预过滤后 (\d+) 块", note).group(1))
    assert int(calls) == kept + 6 * 5


def test_onsite_segment_1_warns_that_sdt_error_analysis_needs_segment_7_first():
    """段 1 的这一项依赖段 7 的产物，第一次跑必然跳过——不写明白，每次跑段 1 都会
    有人以为哪里没配好。"""
    text = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    seg1 = text[text.index("seg_1() {"): text.index("seg_2() {")]
    assert "第一次跑必然跳过" in seg1 and "段 7 的产物" in seg1
    assert "只看段落粒度" in seg1
