"""R4 药理层数据准备的离线测试：切块验证、DOSE_LIMITS 交叉校验的覆盖率一类、
十八反标记、docx 转换、范围评估。

**这一轮全部能在沙盒验完**（真机那一步只是下载六个源 + 真跑抽取）：这几个脚本
本身都是零 LLM 调用的，它们存在的意义就是在花几百次调用之前把数据问题挡住。
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from offline import assess_case_scope as scope
from offline import docx_to_text, tag_incompatible_cases
from offline import extract_materia_medica as emm
from scripts import verify_pharmacology_chunks as vpc

ROOT = Path(__file__).resolve().parent.parent


# ---------- R4-1：下载脚本（只能静态检查，沙盒没网络） ----------


def test_fetch_script_lists_all_six_sources_with_encoding_and_source_tag():
    """六个源、各自的编码和 source 标签都要在脚本里写死——编码靠猜会得到
    静默的乱码语料（GB18030 的中文字节序列有相当概率能被 UTF-8 解码成乱码
    而不抛异常）。

    **教材是 .md 不是 .txt**（路径 十四五教材/xxx.md），古籍编号是三位补零
    ——第一版六条 URL 全猜错，由项目方实测校正，见 data/SOURCES.md 第 42 条。
    """
    text = (ROOT / "scripts" / "fetch_pharmacology_sources.sh").read_text(encoding="utf-8")
    for name in ("中药学.md", "临床中药学.md", "中药炮制学.md", "方剂学.md",
                 "000-神农本草经.txt", "018-本草备要.txt"):
        assert name in text, f"{name} 不在下载脚本的源表里"
    # 四本教材 UTF-8、两本古籍 GB18030。切块模式**六个源全是 heading**——R8 之前
    # 古籍是 blank-line（这里原来断言 heading 4 / blank-line 2），R8 在真实数据上
    # 实测 blank-line 会把 `<篇名>丹沙` 跟正文切开、药名被丢，改成 heading 模式
    # 认 `<篇名>` 行（见 offline/extract_reference_triples.split_blocks）。这是一次
    # 有意的契约变更，不是把断言改绿：源表的切块列跟 EXPECTED_SOURCES 逐字段比对
    # 的那条测试（下面）没动，两张表必须一起改。
    assert text.count("|utf-8|modern|") == 4
    assert text.count("|gb18030|classic|") == 2
    assert text.count("|heading|") == 6
    assert text.count("|blank-line|") == 0
    # 教材路径在 十四五教材/ 下（URL 编码后的形式），不是 books/
    assert text.count("%E5%8D%81%E5%9B%9B%E4%BA%94%E6%95%99%E6%9D%90/") == 4
    assert "/books/" not in text


def test_fetch_script_has_real_expected_byte_counts_not_zero():
    """六个源的 expected_bytes 都已实测，不再是 0（0 = 跳过校验）。
    写死在测试里，是为了让"有人为了让脚本跑过去把它改回 0"这件事变红。"""
    text = (ROOT / "scripts" / "fetch_pharmacology_sources.sh").read_text(encoding="utf-8")
    for byte_count in ("1542065", "951968", "1436809", "1098496", "180115", "293521"):
        assert f"|{byte_count}|" in text, f"expected_bytes {byte_count} 不在源表里"


def test_fetch_script_verifies_bytes_before_transcoding():
    """**校验必须在转码之前。** expected_bytes 记的是上游原始文件的大小
    （古籍是 GB18030 原文），而 GB18030→UTF-8 中文会从 2 字节变 3 字节——
    拿转换后的大小去比那个数会每次都不符，这道闸门就变成了永远报错。"""
    text = (ROOT / "scripts" / "fetch_pharmacology_sources.sh").read_text(encoding="utf-8")
    verify_at = text.index('if ! verify_bytes "$tmp"')
    iconv_at = text.index('if ! iconv -f GB18030')
    assert verify_at < iconv_at, "字节校验跑在转码之后了"
    assert "上游原始文件" in text


def test_fetch_script_verifies_byte_count_and_refuses_to_continue():
    """字节数校验是这个脚本的核心：367 那本少了约 10% 时切粗段的统计数字碰巧
    没变，差点带着残缺原文抽了几百次。校验失败必须退出，不能"看统计像不像话"。"""
    text = (ROOT / "scripts" / "fetch_pharmacology_sources.sh").read_text(encoding="utf-8")
    assert "verify_bytes" in text and "wc -c" in text
    assert "exit 1" in text
    assert "10%" in text          # 把那次教训写在脚本里，不是只写在 SOURCES.md
    assert "iconv" in text        # 古籍要转码，缺 iconv 要提前退出


def test_fetch_script_handles_the_proxy_and_turns_it_back_off():
    """AutoDL 上 GitHub 要走学术加速代理、其余域名走代理反而更慢或不通。
    六个源全在 GitHub，所以开代理下载、下完关掉——留着开会影响后面装包。"""
    text = (ROOT / "scripts" / "fetch_pharmacology_sources.sh").read_text(encoding="utf-8")
    assert "/etc/network_turbo" in text
    assert "proxy_on" in text and "proxy_off" in text
    assert "trap proxy_off EXIT" in text      # 中途失败也要关


def test_fetch_script_dry_run_downloads_nothing(tmp_path):
    """--dry-run 要能在没有网络的机器上跑完（这台沙盒就是），并且不产生文件。"""
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "fetch_pharmacology_sources.sh"),
         "--dry-run", "--dest", str(tmp_path)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "不下载" in proc.stdout
    assert list(tmp_path.iterdir()) == []


def test_fetch_script_and_chunk_verifier_list_the_same_six_sources():
    """下载脚本（bash）和切块验证（Python）各有一张源表，加/删源时两处都要改。
    这条**逐字段**比对两张表（文件名、source、推荐切块模式），漏改一处就红——
    刻意不强行共用一份表（要引入一个中间文件，而这张表一年动不了一次），
    但不能让它们静默分叉。"""
    text = (ROOT / "scripts" / "fetch_pharmacology_sources.sh").read_text(encoding="utf-8")
    rows = {line.split("|")[0].strip('" ') : line.split("|")
            for line in text.splitlines() if line.strip().startswith('"') and "|http" in line}
    assert len(rows) == 6 and len(vpc.EXPECTED_SOURCES) == 6
    for name, (source, _kind, chunk_by, _purpose) in vpc.EXPECTED_SOURCES.items():
        assert name in rows, f"{name} 在切块验证的表里但不在下载脚本里"
        fields = rows[name]
        assert fields[3] == source, f"{name} 的 source 两边不一致"
        assert fields[5] == chunk_by, f"{name} 的推荐切块模式两边不一致"


# ---------- R4-2：切块验证 ----------


def _entry(i: int) -> str:
    return (f"药材{i}\n【性味】辛，温。\n【归经】归肺、脾经。\n"
            f"【功效】解表散寒。\n【用量】煎服，{3 + i % 6}～{9 + i % 6}g。")


def _normal_corpus(n: int = 60) -> str:
    return "\n\n".join(_entry(i) for i in range(n)) + "\n"


def test_chunk_stats_uses_the_engine_split_not_its_own():
    """切法必须复用引擎的 split_blocks——自己再写一个"差不多的"切法等于验了
    一个真实抽取不会走的路径。用源码检查钉住。"""
    src = (ROOT / "scripts" / "verify_pharmacology_chunks.py").read_text(encoding="utf-8")
    assert "from offline.extract_reference_triples import split_blocks" in src
    assert "def split_blocks" not in src


def test_chunk_stats_reports_median_max_min(tmp_path):
    stats = vpc.chunk_stats(_normal_corpus(60))
    assert stats["n_blocks"] == 60
    assert stats["min_chars"] <= stats["median_chars"] <= stats["max_chars"]
    assert stats["longest_index"] is not None and stats["shortest_index"] is not None


def test_chunk_stats_on_empty_text_does_not_crash():
    stats = vpc.chunk_stats("")
    assert stats["n_blocks"] == 0 and stats["median_chars"] == 0
    assert stats["longest_index"] is None
    assert vpc.check_anomalies(stats)     # 0 块当然过不了块数阈值


@pytest.mark.parametrize("threshold,value", [
    ("MIN_BLOCKS", 50), ("MEDIAN_MAX_CHARS", 3000), ("BLOCK_MAX_CHARS", 10000),
])
def test_the_three_thresholds_are_the_declared_values(threshold, value):
    """阈值写死在这里，改了要有人主动改测试——一个可以随手调大的阈值等于没有
    阈值（"块数不够就把 50 调成 5"是最容易发生的事）。"""
    assert getattr(vpc, threshold) == value


def test_each_threshold_has_a_stated_reason():
    """每个阈值都要带"为什么是这个数"。没有依据的阈值只会让人下次直接调大它。
    BLOCK_MAX_CHARS 那条还必须提到引擎的 MAX_TOKENS——那是它的硬依据
    （一块一万字的输入注定顶到 16384 被判截断，这次调用纯浪费）。"""
    assert "条目" in vpc.MIN_BLOCKS_REASON
    assert "整节" in vpc.MEDIAN_REASON
    assert "MAX_TOKENS=16384" in vpc.BLOCK_MAX_REASON
    from offline.extract_reference_triples import MAX_TOKENS

    assert str(MAX_TOKENS) in vpc.BLOCK_MAX_REASON   # 依据跟引擎实际值对得上


def test_anomaly_too_few_blocks(tmp_path):
    """古籍 txt 常常一条一行、没有空行——按空行切会切成一块（整本书）。"""
    problems = vpc.check_anomalies(vpc.chunk_stats("药0，味辛温。\n药1，味辛温。\n" * 200))
    assert any("块数" in p for p in problems)


def test_anomaly_median_too_long():
    text = "\n\n".join("正文" * 2000 for _ in range(60))
    problems = vpc.check_anomalies(vpc.chunk_stats(text))
    assert any("中位数" in p for p in problems)


def test_anomaly_single_block_too_long():
    text = "整章" * 6000 + "\n\n" + _normal_corpus(60)
    problems = vpc.check_anomalies(vpc.chunk_stats(text))
    assert any("最长块" in p for p in problems)


def test_normal_corpus_passes_all_three():
    assert vpc.check_anomalies(vpc.chunk_stats(_normal_corpus(60))) == []


def test_report_prints_the_raw_text_of_the_first_blocks(tmp_path, capsys):
    """**原文必须打出来。** 统计数字说不了"切出来的是不是一味药"，那只有人看
    原文才判得出来——这个脚本的主要产出其实就是这几段原文。"""
    path = tmp_path / "中药学.md"
    path.write_text(_normal_corpus(60), encoding="utf-8")
    problems = vpc.report_source(path, ("modern", "materia_medica", "blank-line", "性味归经"),
                                "blank-line", show=2, preview_chars=200)
    out = capsys.readouterr().out
    assert problems == []
    assert "前 2 块原文" in out
    assert "【性味】辛，温。" in out           # 真的把原文打出来了
    assert "人工确认切的是不是一味药" in out
    assert "第 0 块" in out and "第 1 块" in out


def test_report_also_prints_the_longest_block_when_it_is_too_long(tmp_path, capsys):
    """最长那块超阈值时要把它的开头打出来——判断"它是不是整章"要看内容。"""
    path = tmp_path / "方剂学.md"
    path.write_text("第一章 解表剂\n" + "方剂内容" * 5000, encoding="utf-8")
    vpc.report_source(path, ("modern", "formulary", "blank-line", "方剂"),
                      "blank-line", 1, 120)
    out = capsys.readouterr().out
    assert "最长那块的开头" in out and "第一章 解表剂" in out


def test_main_returns_2_when_nothing_is_downloaded(tmp_path, capsys):
    """没下载 ≠ 没通过。退出码 2 跟 1 分开，CI 里能区分"忘了下"和"切错了"。"""
    assert vpc.main(["--books-dir", str(tmp_path)]) == 2
    assert "还没下载" in capsys.readouterr().err


def test_main_returns_0_and_1_and_reports_skipped_sources(tmp_path, capsys):
    # 教材推荐 heading，所以这里显式传 blank-line（合成语料是空行分段的）
    (tmp_path / "中药学.md").write_text(_normal_corpus(60), encoding="utf-8")
    assert vpc.main(["--books-dir", str(tmp_path), "--show", "0",
                     "--chunk-by", "blank-line"]) == 0
    out = capsys.readouterr().out
    # 缺源不算失败，但必须如实报出来——不然"全过了"会被读成"六个源都在"
    assert "本次跳过（不算失败，但它们没有被验过）" in out
    assert "缺 5 个没验" in out
    assert "阈值只能排除明显切错" in out      # 不夸大这道检查的效力

    (tmp_path / "000-神农本草经.txt").write_text("药，味辛温。\n" * 200, encoding="utf-8")
    assert vpc.main(["--books-dir", str(tmp_path), "--show", "0",
                     "--chunk-by", "blank-line"]) == 1
    assert "不要带着切错的块去跑抽取" in capsys.readouterr().err


def test_main_single_file_mode_accepts_files_outside_the_six(tmp_path, capsys):
    """docx 转出来的医案 txt 也要能验切块粒度（转换脚本的收尾提示就是让人跑这个）。"""
    path = tmp_path / "李可医案.txt"
    path.write_text(_normal_corpus(60), encoding="utf-8")
    assert vpc.main(["--file", str(path), "--show", "0"]) == 0
    assert "不在六源清单里" in capsys.readouterr().out


# ---------- R4-3：DOSE_LIMITS 交叉校验 ----------


def _dose_row(herb: str, dose: str, source: str = "modern") -> dict:
    return {"s": herb, "p": "用量", "o": dose, "source": source, "book": "中药学"}


def test_crosscheck_reports_coverage_and_what_the_table_has_but_extraction_missed():
    """R4-3 要的第三类：**表里有但抽取没抽到**。少了它，62 味里只抽到 2 味也能
    报出"一致 2 条"这种看起来不错的数——而那恰恰说明语料或切块有问题。"""
    report = emm.crosscheck_dose_limits([_dose_row("制附子", "3～15g"), _dose_row("细辛", "1～3g")])
    assert report["coverage"]["n_covered"] == 2
    assert report["coverage"]["n_canonical_in_table"] > 2
    assert 0 < report["coverage"]["rate"] < 1
    missing = {e["herb"] for e in report["missing_from_extraction"]}
    assert "全蝎" in missing            # 表里有、这次没抽到
    assert "附子" not in missing        # 抽到了（制附子归一成附子）
    assert "覆盖率" in report["note"]
    for entry in report["missing_from_extraction"]:
        # 带上表里的原始写法，好判断是不是写法问题
        assert entry["names_in_table"]


def test_crosscheck_coverage_denominator_is_canonical_names_not_raw_entries():
    """分母用归一后的规范名数，不是 len(DOSE_LIMITS)——"制附子"/"黑顺片"…… 都
    归到"附子"，按原始条目数算会让覆盖率被系统性低估，而低估出来的数会被读成
    "语料不够"。"""
    from core.safety_output import DOSE_LIMITS

    report = emm.crosscheck_dose_limits([_dose_row("附子", "3～15g")])
    cov = report["coverage"]
    assert cov["n_entries_in_table"] == len(DOSE_LIMITS)
    assert cov["n_canonical_in_table"] < cov["n_entries_in_table"]


def test_crosscheck_does_not_modify_dose_limits():
    """**不自动改 DOSE_LIMITS。** 那张表的来源声称是"人工从药典查的"，
    按 LLM 抽取结果自动改它等于把安全上限的来源从药典换成了模型。"""
    from core.safety_output import DOSE_LIMITS

    before = dict(DOSE_LIMITS)
    report = emm.crosscheck_dose_limits([_dose_row("附子", "9～30g")])   # 远超 15g 上限
    assert report["inconsistent"], "这条该被判成不一致"
    assert DOSE_LIMITS == before, "crosscheck 改了 DOSE_LIMITS"
    assert "不自动改 DOSE_LIMITS" in report["note"]


def test_crosscheck_normalizes_herb_names_with_core_herbs():
    """药名归一复用 core/herbs.py，不另写一套。用源码检查 + 行为各钉一次。"""
    src = (ROOT / "offline" / "extract_materia_medica.py").read_text(encoding="utf-8")
    assert "from core.herbs import normalize_herb" in src
    report = emm.crosscheck_dose_limits([_dose_row("制附子", "3～15g")])
    assert [e["herb"] for e in report["consistent"]] == ["附子"]
    assert report["consistent"][0]["raw"] == "制附子"     # 原始写法也留着


def test_crosscheck_ignores_classic_records():
    """DOSE_LIMITS 的来源是现代药典/教材，跟古籍的钱两不在同一把尺子上。"""
    report = emm.crosscheck_dose_limits([_dose_row("附子", "9～30g", source="classic")])
    assert report["consistent"] == [] and report["inconsistent"] == []
    assert report["coverage"]["n_covered"] == 0


def test_after_write_prints_missing_and_says_not_to_change_the_table(capsys):
    class _Args:
        crosscheck = True

    emm._after_write([_dose_row("附子", "9～30g")], _Args())
    out = capsys.readouterr().out
    assert "不一致：附子" in out
    assert "表里有但没抽到" in out
    assert "不自动改表" in out
    # 不刷屏：最多列 MISSING_PREVIEW 味
    listed = out.split("表里有但没抽到（")[1].split("）")[0]
    assert f"列前 {emm.MISSING_PREVIEW}" in listed


# ---------- R4-4：十八反标记 ----------


def test_tag_uses_check_incompatible_and_not_its_own_matching():
    """药对匹配只能有一处实现（core.safety_output.check_incompatible）。
    两处实现会出现"安全层拦了、标记没标"或反过来，而这两个结论必须一致。

    用 AST 查**真实的 import**，不查源码文本：文档字符串里提到
    `normalize_for_incompat`（解释 check_incompatible 内部做了什么）是好注释，
    按文本查会把它误判成"又写了一套"。
    """
    import ast

    tree = ast.parse((ROOT / "offline" / "tag_incompatible_cases.py").read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "core.safety_output"
        for alias in node.names
    }
    assert imported == {"check_incompatible"}, (
        f"从 core.safety_output 里还 import 了别的：{imported - {'check_incompatible'}}"
        "——药对匹配和归一都该留在那一处实现里"
    )


def test_tag_marks_the_pair_and_the_flag():
    rows = [
        {"case_id": "a", "herbs": ["海藻", "甘草", "半夏"]},      # 十八反
        {"case_id": "b", "herbs": ["党参", "白术"]},
        {"case_id": "c"},                                        # 没有药物字段
    ]
    stats = tag_incompatible_cases.tag_rows(rows)
    assert rows[0]["has_incompatible_pair"] is True
    assert rows[0]["incompatible_pairs"] == [["海藻", "甘草"]]
    assert rows[1]["has_incompatible_pair"] is False
    assert "incompatible_pairs" not in rows[1]
    assert rows[2]["has_incompatible_pair"] is False
    assert stats["n_tagged"] == 1 and stats["n_without_herbs"] == 1
    # 键按码点排序（海 U+6D77 < 甘 U+7518）——排序是为了让同一对在不同医案里
    # 出现顺序不同时不被统计成两种，不是为了好看
    assert stats["pairs"] == {"海藻 反 甘草": 1}


def test_tag_recognizes_processed_name_variants():
    """"制附子"这类炮制写法要认——check_incompatible 内部走
    normalize_for_incompat，这条确认标记脚本没有绕过它。"""
    rows = [{"case_id": "a", "herbs": ["制附子", "瓜蒌"]}]
    tag_incompatible_cases.tag_rows(rows)
    assert rows[0]["has_incompatible_pair"] is True


def test_tag_clears_a_stale_pair_field():
    """之前跑过、这次不再命中（药物字段被修正过）要把旧字段清掉，
    不然会留下一个跟 has_incompatible_pair=False 矛盾的残留。"""
    rows = [{"case_id": "a", "herbs": ["党参"], "has_incompatible_pair": True,
             "incompatible_pairs": [["海藻", "甘草"]]}]
    tag_incompatible_cases.tag_rows(rows)
    assert rows[0]["has_incompatible_pair"] is False
    assert "incompatible_pairs" not in rows[0]


def test_tag_works_on_any_case_file_json_and_jsonl(tmp_path):
    """**对任意医案文件通用，不是给李可那一份写死的。** .json（数组）和
    .jsonl（一行一条）都要能处理。"""
    rows = [{"case_id": "a", "herbs": ["海藻", "甘草"]}, {"case_id": "b", "herbs": ["党参"]}]
    as_json = tmp_path / "cases.json"
    as_json.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    as_jsonl = tmp_path / "cases.jsonl"
    as_jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                        encoding="utf-8")
    for path in (as_json, as_jsonl):
        assert tag_incompatible_cases.main(["--input", str(path)]) == 0
        out = tag_incompatible_cases.load_rows(path)
        assert out[0]["has_incompatible_pair"] is True
        assert out[1]["has_incompatible_pair"] is False


def test_tag_rejects_an_unknown_extension_rather_than_guessing(tmp_path):
    """按扩展名判断，不按内容猜——猜错了报出来的错跟真正的问题无关。"""
    path = tmp_path / "cases.txt"
    path.write_text("[]", encoding="utf-8")
    assert tag_incompatible_cases.main(["--input", str(path)]) == 1


def test_tag_dry_run_writes_nothing(tmp_path, capsys):
    path = tmp_path / "cases.jsonl"
    original = json.dumps({"case_id": "a", "herbs": ["海藻", "甘草"]}, ensure_ascii=False) + "\n"
    path.write_text(original, encoding="utf-8")
    assert tag_incompatible_cases.main(["--input", str(path), "--dry-run"]) == 0
    assert path.read_text(encoding="utf-8") == original
    assert "不写文件" in capsys.readouterr().out


def test_tag_report_explains_the_tradeoff(capsys):
    rows = [{"case_id": "a", "herbs": ["海藻", "甘草"]}]
    stats = tag_incompatible_cases.tag_rows(rows)
    report = tag_incompatible_cases.format_report(stats, Path("x.jsonl"))
    assert "不会被删" in report        # 只在训练导出那一环排除，不删数据
    assert "自相矛盾" in report        # 说清为什么排除


# ---------- R4-4：训练导出默认排除 ----------


def _case(case_id: str, incompatible: bool):
    from core.schemas import CaseRecord

    return CaseRecord(case_id=case_id, case_group_id=case_id, physician="ye_tianshi",
                      raw="原文", symptoms=["纳差"], syndrome="脾胃气虚",
                      herbs=["海藻", "甘草"] if incompatible else ["党参"],
                      has_incompatible_pair=incompatible)


def test_export_excludes_incompatible_by_default(capsys):
    from offline.export_sft import filter_incompatible_pairs

    cases = [_case("a", True), _case("b", False)]
    kept = filter_incompatible_pairs(cases)
    assert [c.case_id for c in kept] == ["b"]
    assert "排除 1 条" in capsys.readouterr().err


def test_export_include_flag_keeps_them_but_warns_loudly(capsys):
    """带上时照样警告——**R18-G 改了这句警告的内容**（有意的契约变更）。

    原断言是 `"--include-incompatible" in err and "默认是排除的" in err`。
    两处都不再成立而且**不该**成立：R18-G 起带上它们是默认，那句话会把读日志
    的人误导成"我打开了一个非默认开关"。新的那句说的是同一个风险
    （不附提示 → 模型学成"这种配伍可以开" → 撞自己的安全层），
    外加指出排掉它们的正确开关是 --exclude-incompatible。
    """
    from offline.export_sft import filter_incompatible_pairs

    cases = [_case("a", True), _case("b", False)]
    kept = filter_incompatible_pairs(cases, include=True)
    assert [c.case_id for c in kept] == ["a", "b"]
    err = capsys.readouterr().err
    assert "--exclude-incompatible 可以排掉" in err
    assert "R18-G 起这是默认" in err
    assert "自己的安全层面前跑不通" in err


def test_export_cli_has_the_flag_and_defaults_to_including():
    """R18-G **有意的契约变更**：默认从"排除"改成"带上 + 链末附配伍提示"。

    原断言是 `"include=args.include_incompatible" in src`（默认排除、显式开关
    才带上）。改的理由：李可 57 例里有 21 例含反药配对，按原默认他贡献的样本
    数是 0，而 R18 的目的正是把他接进训练。这里改成钉新的那一端——
    `--exclude-incompatible` 存在、且接线是 `include=not args.exclude_incompatible`。
    旧开关 `--include-incompatible` 仍然接受（README 和 run_onsite.sh 里写了它），
    只是不再改变结果，另一条测试钉这件事。
    """
    src = (ROOT / "offline" / "export_sft.py").read_text(encoding="utf-8")
    assert '"--exclude-incompatible", action="store_true"' in src
    assert "include=not args.exclude_incompatible" in src
    # 旧开关留着，不能删（照文档敲命令的人会吃 unrecognized arguments）
    assert '"--include-incompatible", action="store_true"' in src


def test_export_cli_legacy_include_flag_is_accepted_and_says_it_is_a_noop(capsys, tmp_path):
    """留着旧开关但不再改变结果——那就必须**说出来**，否则传了它的人会以为
    自己打开了什么。"""
    import json
    from core.schemas import CaseRecord
    from offline import export_sft

    rows = [CaseRecord(case_id="a", case_group_id="a", physician="ye_tianshi", raw="x",
                       symptoms=["面肿"], syndrome="湿热", pathogenesis="邪干阳位",
                       treatment_principle="清肃上焦", herbs=["杏仁"]).model_dump()]
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    export_sft.main(["--format", "chain", "--cases-path", str(cases_path),
                     "--triples-path", str(tmp_path / "m.jsonl"),
                     "--out", str(tmp_path / "o.jsonl"), "--include-incompatible"])
    err = capsys.readouterr().err
    assert "--include-incompatible 从 R18-G 起是默认行为" in err
    assert "不再改变任何结果" in err


# ---------- R4-4：docx 转换 ----------


def _make_docx(path: Path, paragraphs: list[str], table_rows: list[list[str]] | None = None):
    # importorskip：python-docx 是可选依赖（只有处理 .docx 语料时才要）。
    # 裸 import 会让干净机器上一批跟它无关的断言一起红——这个形状已经撞过四次，
    # 判据写在 tests/test_conftest_embedding_marker.py 里。
    docx = pytest.importorskip("docx")

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table_rows:
        table = d.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for i, row in enumerate(table_rows):
            for j, cell in enumerate(row):
                table.cell(i, j).text = cell
    d.save(str(path))


def test_docx_to_text_keeps_paragraphs_separated_by_blank_lines(tmp_path):
    """下游按空行切块，一块应该 = docx 的一个段落。"""
    src = tmp_path / "李可医案.docx"
    _make_docx(src, ["第一案：患者男，60 岁。", "", "第二案：患者女，45 岁。"])
    assert docx_to_text.main(["--input", str(src)]) == 0
    text = (tmp_path / "李可医案.txt").read_text(encoding="utf-8")
    assert text == "第一案：患者男，60 岁。\n\n第二案：患者女，45 岁。\n"
    # docx 里本来的空段落不该变成额外的空行（那会把一块切成两块）
    from offline.extract_reference_triples import split_blocks

    assert len(split_blocks(text, "blank-line")) == 2


def test_docx_to_text_picks_up_tables(tmp_path):
    """医案 docx 偶尔用表格排"药物-剂量"，漏掉表格等于漏掉处方。"""
    src = tmp_path / "a.docx"
    _make_docx(src, ["某案"], table_rows=[["附子", "30g"], ["甘草", "10g"]])
    docx_to_text.main(["--input", str(src), "--out", str(tmp_path / "a.txt")])
    text = (tmp_path / "a.txt").read_text(encoding="utf-8")
    assert "附子\t30g" in text and "甘草\t10g" in text


def test_docx_to_text_skip_tables_flag(tmp_path):
    src = tmp_path / "b.docx"
    _make_docx(src, ["某案"], table_rows=[["附子", "30g"]])
    docx_to_text.main(["--input", str(src), "--out", str(tmp_path / "b.txt"), "--skip-tables"])
    assert "附子" not in (tmp_path / "b.txt").read_text(encoding="utf-8")


def test_docx_to_text_dry_run_writes_nothing_and_previews(tmp_path, capsys):
    src = tmp_path / "c.docx"
    _make_docx(src, ["第一案：患者男。"])
    assert docx_to_text.main(["--input", str(src), "--dry-run"]) == 0
    assert not (tmp_path / "c.txt").exists()
    out = capsys.readouterr().out
    assert "第一案：患者男。" in out and "不写文件" in out


def test_docx_to_text_reports_an_empty_document_instead_of_writing_nothing(tmp_path, capsys):
    """扫描版 docx 只有图片、没有正文——要说清楚，不要写出一个空 txt 让人
    以为转成功了。"""
    src = tmp_path / "d.docx"
    _make_docx(src, [])
    assert docx_to_text.main(["--input", str(src)]) == 1
    assert "扫描版 docx" in capsys.readouterr().err


def test_docx_to_text_tells_you_to_verify_chunking_next(tmp_path, capsys):
    """docx 的段落粒度因人而异（有人一整篇一个段落），转完必须验切块。"""
    src = tmp_path / "e.docx"
    _make_docx(src, ["某案"])
    docx_to_text.main(["--input", str(src)])
    assert "verify_pharmacology_chunks" in capsys.readouterr().out


def test_docx_import_name_trap_is_documented():
    """包名 python-docx、import 名 docx；`pip install docx` 会装到一个同名的
    废弃包上然后 import 成功但 API 完全不同。这个坑要写在两处（requirements
    和脚本里），不然下次还会踩。"""
    assert "python-docx" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
    for path in (ROOT / "requirements.txt", ROOT / "offline" / "docx_to_text.py"):
        assert "废弃包" in path.read_text(encoding="utf-8")


# ---------- R4-4：范围评估 ----------


def test_scope_assessment_reuses_lookup_standard():
    """"这个证型名在标准表里有没有"已经有实现了（core.tools.lookup_standard），
    不另写字面比对——第一次撞墙是覆盖检查用字面子串，第三次是同一个词两个工具
    给出相反答案。"""
    src = (ROOT / "offline" / "assess_case_scope.py").read_text(encoding="utf-8")
    assert "from core.tools import lookup_standard" in src
    # 标准表路径只有一处（core.tools.STANDARD_PATH）：这里不开第二个入口。
    # 检查的是 argparse 有没有这个参数，不是文档字符串里有没有提到它。
    assert '"--syndromes-path"' not in src


def test_scope_assessment_counts_covered_and_uncovered():
    rows = [
        {"case_id": "1", "syndrome": "肝胃不和证", "raw": "胃脘胀痛"},
        {"case_id": "2", "syndrome": "气滞血瘤证", "raw": "颈部肿块"},
        {"case_id": "3", "raw": "没有证型字段"},
    ]
    r = scope.assess(rows)
    assert r["n_rows"] == 3 and r["n_without_syndrome"] == 1
    assert "肝胃不和证" in r["covered"]
    assert "气滞血瘤证" in r["uncovered"]
    assert r["coverage_rate"] == 0.5
    assert r["n_standard_syndromes"] > 300


def test_scope_assessment_flags_oncology_without_filtering():
    """肿瘤词只作提示、不参与任何过滤——命中不代表这条是肿瘤科的。"""
    rows = [{"case_id": "1", "syndrome": "肝胃不和证", "raw": "胃癌术后，化疗后纳差"}]
    r = scope.assess(rows)
    assert r["oncology_hits"]
    assert r["n_covered_records"] == 1      # 照样算进覆盖，没有被过滤掉
    report = scope.format_report(r, show=5)
    assert "只作提示，不参与任何过滤" in report
    assert "这是产品决定，不是脚本能定的" in report


def test_scope_assessment_does_not_auto_decide(capsys):
    """覆盖率低有两种含义（另一个科 / 只是写法不同），**代码分不出是哪一种**，
    所以只报数不给 PASS/FAIL。给它设阈值自动判决等于假装这件事能自动化。"""
    rows = [{"case_id": "1", "syndrome": "某个表里没有的证", "raw": "x"}]
    report = scope.format_report(scope.assess(rows), show=5)
    assert "代码分不出是哪一种" in report
    src = (ROOT / "offline" / "assess_case_scope.py").read_text(encoding="utf-8")
    assert "覆盖率低不是失败" in src


def test_scope_assessment_cli_returns_zero_even_with_low_coverage(tmp_path, capsys):
    path = tmp_path / "wyq.jsonl"
    path.write_text(json.dumps(
        {"case_id": "1", "syndrome": "气滞血瘤证", "raw": "胃癌"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert scope.main(["--input", str(path)]) == 0
    assert "零 LLM 调用" in capsys.readouterr().out


def test_scope_assessment_shares_the_file_loader_with_the_tagger():
    """两个脚本读的是同一种医案文件，载入约定（.json 数组 / .jsonl 一行一条）
    只能有一处——各写一份的话一边支持 .json、另一边不支持这种事迟早出现。"""
    src = (ROOT / "offline" / "assess_case_scope.py").read_text(encoding="utf-8")
    assert "from offline.tag_incompatible_cases import load_rows" in src


def test_all_new_scripts_run_as_modules():
    """上机清单里全都是 `python -m ...` 形式，四个脚本都要能这么跑起来。"""
    for module in ("offline.tag_incompatible_cases", "offline.assess_case_scope",
                   "offline.docx_to_text", "scripts.verify_pharmacology_chunks"):
        proc = subprocess.run([sys.executable, "-m", module, "--help"],
                              capture_output=True, text=True, cwd=ROOT, timeout=90)
        assert proc.returncode == 0, f"{module}: {proc.stderr}"


# ---------- R4 收尾：markdown 的切块粒度（这一轮最重要的发现） ----------
#
# 教材是 .md（OCR 转出来的 markdown），一味药的条目是「# 药名」加下面若干段
# （性味/归经/功效/用法用量）。按空行切会把一味药切成五六块，而其中
# 「【用法用量】煎服，9～30g」那块里**根本没有药名**。
#
# 这不是"效果好不好"的问题：s6 prompt 要求 s 是"原文里这一条目的药材正名"，
# 而 s 只有 min_length=1 约束、**不过 source_span 逐字核验**（核验只管 o 的
# 出处）。所以一个没有药名的块会让模型给一个猜的 s，然后一路写进
# data/materia_medica.jsonl，没有任何一道闸门拦得住。切块粒度是防幻觉的前提。

MARKDOWN_TEXTBOOK = """# 第一章 解表药

本章讨论解表药的分类与应用，这段前言要够长才不会被 min_chars 丢掉。

# 麻黄

【性味】辛、微苦，温。

【归经】归肺、膀胱经。

【用法用量】煎服，2～10g。

# 桂枝

【性味】辛、甘，温。

【用法用量】煎服，3～10g。
"""


def test_heading_mode_keeps_one_herb_per_block():
    from offline.extract_reference_triples import split_blocks

    blocks = split_blocks(MARKDOWN_TEXTBOOK, "heading")
    assert len(blocks) == 3            # 章前言 + 麻黄 + 桂枝
    herb_block = next(b for b in blocks if b.startswith("# 麻黄"))
    # 一味药的全部字段在同一块里，药名也在
    assert "【性味】" in herb_block and "【用法用量】" in herb_block
    assert "桂枝" not in herb_block     # 没有粘上下一味药


def test_blank_line_mode_separates_the_dose_from_the_herb_name():
    """**这条是 heading 模式存在的理由。** 按空行切之后，「用法用量」那一块里
    没有任何药名——模型只能猜一个 s，而 s 不过 source_span 核验。"""
    from offline.extract_reference_triples import split_blocks

    blocks = split_blocks(MARKDOWN_TEXTBOOK, "blank-line")
    dose_blocks = [b for b in blocks if "用法用量" in b]
    assert dose_blocks, "合成语料里应该有用量块"
    for b in dose_blocks:
        assert "麻黄" not in b and "桂枝" not in b, "这一块里居然有药名，样例造错了"
    # 药名那一行（"# 麻黄"）短于 MIN_BLOCK_CHARS，直接被丢掉了——连"另一块里
    # 有药名"都谈不上
    from offline.extract_reference_triples import MIN_BLOCK_CHARS

    assert len("# 麻黄") < MIN_BLOCK_CHARS
    assert not any(b.strip() == "# 麻黄" for b in blocks)


def test_heading_mode_keeps_the_preamble_instead_of_dropping_it():
    """第一个标题之前的内容单独成一块，不丢掉——"丢了什么"该由 min_chars 和
    切块验证脚本判断，不该由切块函数偷偷决定。"""
    from offline.extract_reference_triples import split_blocks

    text = "没有标题的前言，长度足够不被丢掉。\n\n# 麻黄\n\n【性味】辛温。"
    blocks = split_blocks(text, "heading")
    assert len(blocks) == 2 and blocks[0].startswith("没有标题的前言")


def test_heading_mode_is_registered_in_chunk_modes_and_cli():
    """三种切法都要能从命令行传——EXPECTED_SOURCES 里记的推荐值必须是合法值。"""
    from offline.extract_reference_triples import CHUNK_MODES

    assert set(CHUNK_MODES) == {"blank-line", "line", "heading"}
    for _name, (_src, _kind, chunk_by, _purpose) in vpc.EXPECTED_SOURCES.items():
        assert chunk_by in CHUNK_MODES


def test_verifier_uses_the_per_source_recommended_mode_by_default(tmp_path, capsys):
    """不传 --chunk-by 时按每个源的推荐值跑（R8 起六个源都是 heading：教材认「#」、
    古籍认「<篇名>」）。传一个全局值只是为了对比，不该是默认——默认值搞错会让
    验证验的是另一个切法，而真实抽取用的是推荐那个。"""
    (tmp_path / "中药学.md").write_text(MARKDOWN_TEXTBOOK, encoding="utf-8")
    vpc.main(["--books-dir", str(tmp_path), "--show", "0"])
    out = capsys.readouterr().out
    assert "切块模式：heading" in out
    assert "按每个源的推荐值" in out


def test_verifier_warns_when_told_to_use_a_non_recommended_mode(tmp_path, capsys):
    """显式传了非推荐模式不拦，但要说清代价——不是"随便哪个都行"。"""
    (tmp_path / "中药学.md").write_text(MARKDOWN_TEXTBOOK, encoding="utf-8")
    vpc.main(["--books-dir", str(tmp_path), "--show", "0", "--chunk-by", "blank-line"])
    out = capsys.readouterr().out
    assert "用的不是推荐模式" in out
    assert "s 不过 source_span 核验" in out


def test_compare_modes_reports_all_three_side_by_side(tmp_path, capsys):
    """三种切法的块数/中位数并排——差一个数量级时选哪种就很明显了。"""
    (tmp_path / "中药学.md").write_text(MARKDOWN_TEXTBOOK, encoding="utf-8")
    vpc.main(["--books-dir", str(tmp_path), "--show", "0", "--compare-modes"])
    out = capsys.readouterr().out
    assert "三种切法对比" in out
    for mode in ("blank-line", "line", "heading"):
        assert mode in out
    assert "← 推荐" in out


def test_compare_modes_is_a_pure_function_without_the_block_text():
    """对比只看统计，不带原文——带上原文会让输出刷屏，而要看原文有 --show。"""
    result = vpc.compare_modes(MARKDOWN_TEXTBOOK)
    assert set(result) == {"blank-line", "line", "heading"}
    for stats in result.values():
        assert "blocks" not in stats
        assert stats["n_blocks"] > 0
    assert result["heading"]["n_blocks"] < result["blank-line"]["n_blocks"]
