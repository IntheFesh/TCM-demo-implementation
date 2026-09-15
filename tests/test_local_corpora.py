"""R8-2：本地语料规范化（scripts/normalize_local_corpora.py）、粗段目录只认医家 id、
训练导出的 out_of_scope 过滤。零 LLM 调用。

用 python-docx 现造两份小 docx、一份带"版权页"头的 txt、一份 584 重复文件，
在 tmp 目录里跑整个脚本——原文件名故意用真实上传的那几个（带空格和没闭合的括号）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import normalize_local_corpora as nlc

ORIGINAL_NAMES = {
    "王云启医案.docx": "2_王云启(1).docx",
    "李可医案.docx": "李可医案.docx",
    "脾胃论.txt": "脾胃论 (中医经典文库) (金·李东垣 [金·李东垣], 古聖先賢) (z-library.sk,.txt",
}


def _make_docx(path: Path, paragraphs: list[str]) -> None:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(str(path))


def _seed(data_dir: Path, books_dir: Path | None, with_584: bool = True) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _make_docx(data_dir / ORIGINAL_NAMES["王云启医案.docx"],
               ["代序", "肝癌患者，胃脘胀痛，纳差。", "食道癌术后化疗，呕吐。"])
    _make_docx(data_dir / ORIGINAL_NAMES["李可医案.docx"],
               ["脑瘤头痛案", "处方：海藻 30g 甘草 10g 附子 15g 半夏 12g", "宫颈癌案，泄泻。"])
    (data_dir / ORIGINAL_NAMES["脾胃论.txt"]).write_text(
        "版权页\n\n如果你想获得更多免费电子书请加小编 QQ，这一段是推广广告不是正文。\n\n"
        "太阴阳明论云：太阴阳明为表里，脾胃脉也。\n\n柴胡一两五钱 甘草炙 黄芪 升麻八钱 人参\n",
        encoding="utf-8")
    if with_584:
        (data_dir / nlc.DUPLICATE_OF_BOOKS).write_bytes("<篇名>医学衷中参西录\n".encode("gb18030"))
    if books_dir is not None:
        books_dir.mkdir(parents=True, exist_ok=True)


def _run(tmp_path: Path, **kw) -> int:
    return nlc.normalize(tmp_path / "data", tmp_path / "books", **kw)


def test_normalize_moves_files_to_safe_names_and_writes_a_complete_manifest(tmp_path, capsys):
    _seed(tmp_path / "data", tmp_path / "books")
    assert _run(tmp_path) == 0
    local = tmp_path / "data" / "local_corpora"
    for safe, original in ORIGINAL_NAMES.items():
        assert (local / safe).exists(), safe
        assert not (tmp_path / "data" / original).exists(), "要搬走，不是复制"
    # docx 顺手转成 txt（同一套 docx_to_text 转换）
    assert "胃脘胀痛" in (local / "王云启医案.txt").read_text(encoding="utf-8")
    manifest = json.loads((local / nlc.MANIFEST_NAME).read_text(encoding="utf-8"))
    entries = {e["file"]: e for e in manifest["entries"]}
    assert set(entries) == {"local_corpora/王云启医案.docx", "local_corpora/李可医案.docx", "local_corpora/脾胃论.txt"}
    e = entries["local_corpora/王云启医案.docx"]
    assert e["original_name"] == "2_王云启(1).docx"
    assert e["bytes"] == (local / "王云启医案.docx").stat().st_size
    assert len(e["sha256"]) == 64 and e["encoding"] == "docx" and e["kind"] == "case_docx"
    assert e["out_of_scope"] is True and e["copyright_status"] == "copyrighted"
    assert e["derived_text"] == "local_corpora/王云启医案.txt"
    assert "origin" in e and "scope_reason" in e
    assert entries["local_corpora/脾胃论.txt"]["out_of_scope"] is False
    assert entries["local_corpora/脾胃论.txt"]["original_name"] == ORIGINAL_NAMES["脾胃论.txt"]
    out = capsys.readouterr().out
    assert "搬走，不复制" in out and "已写出" in out


def test_normalize_is_idempotent_second_run_changes_nothing(tmp_path, capsys):
    _seed(tmp_path / "data", tmp_path / "books")
    assert _run(tmp_path) == 0
    local = tmp_path / "data" / "local_corpora"
    manifest = local / nlc.MANIFEST_NAME
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in local.iterdir()}
    capsys.readouterr()
    assert _run(tmp_path) == 0
    after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in local.iterdir()}
    assert before == after, "第二遍不该改任何文件（内容和 mtime 都不变）"
    out = capsys.readouterr().out
    assert "MANIFEST.json 内容没变" in out and "已在位" in out
    assert manifest.exists()


def test_reupload_identical_is_deleted_and_different_is_a_conflict(tmp_path, capsys):
    _seed(tmp_path / "data", tmp_path / "books")
    assert _run(tmp_path) == 0
    # 又上传了一次、内容一样 → 删掉根目录那份。
    # **必须 copy 字节，不能重新 _make_docx 一份**：python-docx 会把生成时刻写进
    # docProps/core.xml 的 created/modified，而 zip 条目本身的时间戳粒度是 2 秒。
    # 重新生成的那份只有在跟第一次落在同一个 2 秒窗口里才逐字节相同——快机器上
    # 恰好总是相同，慢机器或负载高时就跨窗口，脚本据此如实报"冲突"退出 1，
    # 测试红在 `assert _run(tmp_path) == 0`。这是测试的写法问题，不是脚本的 bug。
    # 下面 test_584_duplicate_rules 里"逐字节相同"那一步用的就是 copy 字节，
    # 这里跟它对齐。
    _make_docx(tmp_path / "data" / "__probe.docx", ["x"])  # 保证 python-docx 可用
    (tmp_path / "data" / "__probe.docx").unlink()
    (tmp_path / "data" / ORIGINAL_NAMES["李可医案.docx"]).write_bytes(
        (tmp_path / "data" / "local_corpora" / "李可医案.docx").read_bytes())
    assert _run(tmp_path) == 0
    assert not (tmp_path / "data" / ORIGINAL_NAMES["李可医案.docx"]).exists()
    assert "逐字节相同，删掉根目录这份重复" in capsys.readouterr().out
    # 内容不同 → 两份都不动，退出码 1
    _make_docx(tmp_path / "data" / ORIGINAL_NAMES["李可医案.docx"], ["另一份不一样的李可医案"])
    assert _run(tmp_path) == 1
    assert (tmp_path / "data" / ORIGINAL_NAMES["李可医案.docx"]).exists()
    assert "冲突" in capsys.readouterr().out
    assert "另一份不一样" not in (tmp_path / "data" / "local_corpora" / "李可医案.txt").read_text(encoding="utf-8")


def test_584_duplicate_rules(tmp_path, capsys):
    data, books = tmp_path / "data", tmp_path / "books"
    # books/ 里没有 → 搬过去
    _seed(data, None)
    assert _run(tmp_path) == 0
    assert (books / nlc.DUPLICATE_OF_BOOKS).exists() and not (data / nlc.DUPLICATE_OF_BOOKS).exists()
    assert "搬过去" in capsys.readouterr().out
    # books/ 有、逐字节相同 → 删 data/ 那份
    (data / nlc.DUPLICATE_OF_BOOKS).write_bytes((books / nlc.DUPLICATE_OF_BOOKS).read_bytes())
    assert _run(tmp_path) == 0
    assert not (data / nlc.DUPLICATE_OF_BOOKS).exists()
    assert "逐字节相同，删掉 data/ 这份" in capsys.readouterr().out
    # 两边不同 → 冲突，两份都不动
    (data / nlc.DUPLICATE_OF_BOOKS).write_bytes(b"different")
    assert _run(tmp_path) == 1
    assert (data / nlc.DUPLICATE_OF_BOOKS).read_bytes() == b"different"
    assert (books / nlc.DUPLICATE_OF_BOOKS).read_bytes() != b"different"


def test_dry_run_moves_and_writes_nothing(tmp_path, capsys):
    _seed(tmp_path / "data", tmp_path / "books")
    listing = sorted(os.listdir(tmp_path / "data"))
    assert _run(tmp_path, dry_run=True) == 0
    assert sorted(os.listdir(tmp_path / "data")) == listing
    assert not (tmp_path / "data" / "local_corpora").exists()
    out = capsys.readouterr().out
    assert "--dry-run：什么都没改" in out and "将写出" in out
    # 干跑时的统计行也用规范名，不用还没搬的原名
    assert "local_corpora/王云启医案.docx：" in out


def test_scope_stats_reuse_the_existing_matchers_and_carry_denominators():
    paras = ["肝癌患者，胃脘胀痛，纳差。", "处方：海藻 30g 甘草 10g 附片 15g 半夏 12g", "今日天气晴。"]
    st = nlc.scope_stats(paras)
    assert st["n_paragraphs"] == 3
    assert st["spleen_stomach_paragraphs"] == 1 and st["spleen_stomach_rate"] == round(1 / 3, 4)
    assert st["oncology_paragraphs"] == 1
    assert st["incompatible_pair_paragraphs"] == 1
    # 按类目名归并：附片→乌头，跟 check_incompatible 同一套归一
    assert st["incompatible_pairs"] == {"海藻 反 甘草": 1, "乌头 反 半夏": 1}
    assert nlc.scope_stats([])["spleen_stomach_rate"] is None


def test_manifest_json_would_be_picked_up_as_a_segment_without_the_physician_filter(tmp_path, monkeypatch):
    """data/local_corpora/MANIFEST.json 是 .json——extract_cases 原来"是目录就遍历"，
    会把它当粗段读。现在只认医家 id 命名的目录。"""
    from offline import extract_cases

    (tmp_path / "ye_tianshi").mkdir()
    (tmp_path / "ye_tianshi" / "ye_tianshi-0000.json").write_text("{}", encoding="utf-8")
    (tmp_path / "local_corpora").mkdir()
    (tmp_path / "local_corpora" / "MANIFEST.json").write_text("{}", encoding="utf-8")
    (tmp_path / "standard").mkdir()
    (tmp_path / "standard" / "x.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(extract_cases, "DATA_ROOT", tmp_path)
    assert [p.name for p in extract_cases.iter_segment_files()] == ["ye_tianshi-0000.json"]


# ---------- 训练导出：out_of_scope 默认排除 ----------


def _case(case_id: str, out_of_scope: bool):
    from core.schemas import CaseRecord

    return CaseRecord(case_id=case_id, case_group_id=case_id, physician="ye_tianshi",
                      raw="原文", symptoms=["纳差"], syndrome="脾胃气虚", herbs=["党参"],
                      out_of_scope=out_of_scope)


def test_case_record_out_of_scope_defaults_to_false():
    from core.schemas import CaseRecord

    assert CaseRecord.model_fields["out_of_scope"].default is False


def test_export_excludes_out_of_scope_by_default(capsys):
    from offline.export_sft import filter_out_of_scope

    kept = filter_out_of_scope([_case("a", True), _case("b", False)])
    assert [c.case_id for c in kept] == ["b"]
    assert "排除 1 条定位外" in capsys.readouterr().err


def test_export_include_out_of_scope_keeps_them_but_warns(capsys):
    from offline.export_sft import filter_out_of_scope

    kept = filter_out_of_scope([_case("a", True), _case("b", False)], include=True)
    assert [c.case_id for c in kept] == ["a", "b"]
    err = capsys.readouterr().err
    assert "--include-out-of-scope" in err and "默认是排除的" in err


def test_export_cli_has_the_out_of_scope_flag_and_wires_it():
    src = (Path(__file__).resolve().parent.parent / "offline" / "export_sft.py").read_text(encoding="utf-8")
    assert '"--include-out-of-scope", action="store_true"' in src
    assert "include=args.include_out_of_scope" in src


@pytest.mark.parametrize("safe_name", list(ORIGINAL_NAMES))
def test_safe_names_have_no_shell_hostile_characters(safe_name):
    assert not any(ch in safe_name for ch in " ()[],")


# ---------- 声明表：两个独立判断（R8 收尾） ----------


def test_two_independent_judgements_are_not_merged():
    """`out_of_scope`（训练集要不要它）和 `pharmacology_source`（药理层抽取能不能拿
    它当输入）回答的不是同一个问题。《脾胃论》是判据：**在定位内**，但同样不能进
    药理层抽取（按空行切方名和组成不在一块）。合并成一个字段就看不出这个区别了。"""
    from offline import local_corpora as lc

    peiwei = lc.spec_for_target("脾胃论.txt")
    assert peiwei is not None
    assert peiwei.out_of_scope is False          # 在定位内
    assert peiwei.pharmacology_source is False   # 但不是本草/方剂参考文献
    assert "方名" in peiwei.pharmacology_reason and "source_span" in peiwei.pharmacology_reason
    # 每份都要写明两个理由，不能只标一个布尔值
    for spec in lc.LOCAL_CORPORA:
        assert spec.scope_reason and spec.pharmacology_reason


def test_spec_for_path_also_recognises_the_derived_txt_and_ignores_the_directory():
    """真正会被拿去喂抽取的是 docx 派生出来的那份 .txt；从别处拷一份改成这个名字
    同样该被认出来（只看文件名，不看目录）。"""
    from offline import local_corpora as lc

    assert lc.spec_for_path("data/local_corpora/李可医案.txt").target == "李可医案.docx"
    assert lc.spec_for_path("/tmp/somewhere/李可医案.txt") is not None
    assert lc.spec_for_path(Path("books/中药学.md")) is None


def test_non_reference_reason_only_speaks_for_files_in_the_table():
    from offline import local_corpora as lc

    assert lc.non_reference_reason("books/中药学.md") is None
    assert lc.non_reference_reason("books/000-神农本草经.txt") is None
    for spec in lc.non_pharmacology_corpora():
        reason = lc.non_reference_reason(f"data/local_corpora/{spec.target}")
        assert reason is not None and spec.target in reason
        assert ("定位外" in reason) == spec.out_of_scope


def test_manifest_records_the_pharmacology_source_declaration_too():
    """MANIFEST 是给人看的那份账：两个判断都要落在里面，不然"为什么它没进抽取"
    只能去读代码。"""
    from scripts import normalize_local_corpora as nlc

    manifest = json.loads((Path(__file__).resolve().parent.parent / "data" / "local_corpora"
                           / nlc.MANIFEST_NAME).read_text(encoding="utf-8"))
    for entry in manifest["entries"]:
        assert entry["pharmacology_source"] is False
        assert entry["pharmacology_reason"]
        assert "out_of_scope" in entry
