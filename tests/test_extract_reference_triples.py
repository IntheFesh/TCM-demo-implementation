"""阶段二（药理层）离线测试：offline/extract_reference_triples.py（共用引擎）、
offline/extract_materia_medica.py（DOSE_LIMITS 交叉校验）、
offline/extract_formulary.py（薄入口）。假 LLM 后端，不需要网络。

跟 tests/test_extract_case_triples.py 同一套关注点：source_span 逐字核验、
截断/失败分开处理、增量落盘按块号合并、古籍/现代来源标签原样落盘。
"""
import json

import pytest
from pydantic import ValidationError

from core.llm import LLMError, LLMTruncatedError
from core.schemas import (
    FormularyExtraction,
    FormularyItem,
    MateriaMedicaExtraction,
    MateriaMedicaItem,
    MateriaMedicaRecord,
)
from offline import extract_formulary as ef
from offline import extract_materia_medica as emm
from offline import extract_reference_triples as ert

# 合成语料要**同时**过 modern 和 classic 两种预过滤判据（R8-1，见
# offline/pharmacology_sources.py）：行首的 `【字段】` 标签是教材条目的结构标记，
# 「三钱」这种剂量词是古籍方药的——同一份语料在下面的 modern（中药学）和
# classic（本经）两组端到端测试里都要用，缺一种标记那一组就会被预过滤全跳过。
BOOK_TEXT = """黄芪
【性味】甘，微温。归脾、肺经。补气升阳，固表止汗。9～30g。古方每用三钱。表实邪盛者不宜。

炙甘草
【性味】甘，平。归心、肺、脾、胃经。补脾益气。3～10g。古方每用一钱。反海藻、大戟。

第三节
"""


class FakeLLM:
    def __init__(self, result, schema):
        self.result = result
        self.schema = schema
        self.calls = 0
        self.systems: list[str] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        self.systems.append(system)
        assert schema is self.schema
        return self.result


class TruncatingFakeLLM:
    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        raise LLMTruncatedError("疑似截断（测试用）")


class FailingFakeLLM:
    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        try:
            raise TimeoutError("网络超时（测试用）")
        except TimeoutError as e:
            raise LLMError("重试 3 次仍失败（测试用）") from e


def _install(monkeypatch, llm):
    monkeypatch.setattr(ert, "get_llm", lambda: llm)


# ---------- split_blocks：纯函数 ----------


def test_split_blocks_blank_line_drops_short_headings():
    blocks = ert.split_blocks(BOOK_TEXT)
    assert len(blocks) == 2
    assert blocks[0].startswith("黄芪") and blocks[1].startswith("炙甘草")
    assert all("第三节" not in b for b in blocks)  # 短于 MIN_BLOCK_CHARS，不喂模型


def test_split_blocks_line_mode_and_bad_mode():
    assert ert.split_blocks("一行足够长的条目内容\n短\n另一行足够长的条目内容", chunk_by="line") == [
        "一行足够长的条目内容", "另一行足够长的条目内容",
    ]
    with pytest.raises(ValueError):
        ert.split_blocks("x", chunk_by="paragraph")


# ---------- schema：谓词 Literal 真的生效 ----------


def test_predicate_literals_reject_out_of_vocabulary():
    with pytest.raises(ValidationError):
        MateriaMedicaItem(s="黄芪", p="别名", o="北芪", source_span="x")
    with pytest.raises(ValidationError):
        FormularyItem(s="麻黄汤", p="方歌", o="x", source_span="x")
    with pytest.raises(ValidationError):
        MateriaMedicaRecord(s="黄芪", p="性味", o="甘", source_span="甘", source="ancient", book="b")


# ---------- extract_block：source_span 核验 + 来源标签 ----------


def test_extract_block_keeps_only_verifiable_spans_and_stamps_source_and_book(monkeypatch):
    block = ert.split_blocks(BOOK_TEXT)[0]
    _install(monkeypatch, FakeLLM(MateriaMedicaExtraction(triples=[
        MateriaMedicaItem(s="黄芪", p="性味", o="甘，微温", source_span="甘，微温。"),
        MateriaMedicaItem(s="黄芪", p="用量", o="9～30g", source_span="9～30g。"),
        MateriaMedicaItem(s="黄芪", p="功效", o="补气升阳", source_span="这句原文里没有"),
    ]), MateriaMedicaExtraction))
    records, rejected, failure = ert.extract_block(ert.KINDS["materia_medica"], block, "modern", "中药学")
    assert failure is None
    assert rejected == {"span_not_found": 1}
    assert [(r.p, r.source, r.book) for r in records] == [("性味", "modern", "中药学"), ("用量", "modern", "中药学")]


def test_extract_block_rejects_unknown_source_before_calling_the_model(monkeypatch):
    _install(monkeypatch, FakeLLM(MateriaMedicaExtraction(), MateriaMedicaExtraction))
    with pytest.raises(ValueError, match="source"):
        ert.extract_block(ert.KINDS["materia_medica"], "黄芪，甘，微温。", "ancient", "b")


def test_extract_block_truncation_and_failure_are_distinguished(monkeypatch):
    kind = ert.KINDS["formulary"]
    _install(monkeypatch, TruncatingFakeLLM())
    assert ert.extract_block(kind, "麻黄汤：麻黄三两。", "classic", "伤寒论")[2] == "truncated"
    _install(monkeypatch, FailingFakeLLM())
    assert ert.extract_block(kind, "麻黄汤：麻黄三两。", "classic", "伤寒论")[2] == "TimeoutError"


# ---------- extract_all：统计与回调 ----------


def test_extract_all_stats_and_callbacks(monkeypatch):
    kind = ert.KINDS["formulary"]
    _install(monkeypatch, FakeLLM(FormularyExtraction(triples=[
        FormularyItem(s="麻黄汤", p="君药", o="麻黄", source_span="麻黄为君"),
    ]), FormularyExtraction))
    blocks = [(0, "麻黄汤，麻黄为君，桂枝为臣。"), (3, "这一块原文里没有那句话。")]
    done, progress = [], []
    records, stats = ert.extract_all(
        kind, blocks, "classic", "伤寒论",
        on_progress=lambda i, n, snap: progress.append((i, n, snap["triples_extracted"])),
        on_block_done=lambda idx, recs, failure: done.append((idx, len(recs), failure)),
    )
    assert [r.s for r in records] == ["麻黄汤"]
    assert done == [(0, 1, None), (3, 0, None)]
    assert progress == [(2, 2, 1)]  # 不满 PROGRESS_EVERY 的尾巴也要报一次
    assert stats["blocks"] == 2 and stats["llm_calls"] == 2
    assert stats["triples_extracted"] == 1 and stats["triples_rejected_span_not_found"] == 1
    assert stats["blocks_no_triples"] == 1


# ---------- 落盘：按 (book, source, 块号) 合并 ----------


def test_load_and_write_rows_round_trip_and_merge(tmp_path):
    out = tmp_path / "mm.jsonl"
    grouped = {
        ("中药学", "modern", 0): [{"s": "黄芪", "p": "性味", "o": "甘", "source_span": "甘",
                                  "source": "modern", "book": "中药学", "_block": 0}],
        ("神农本草经", "classic", 0): [{"s": "黄芪", "p": "性味", "o": "味甘微温", "source_span": "味甘微温",
                                     "source": "classic", "book": "神农本草经", "_block": 0}],
    }
    ert.write_rows(out, grouped)
    assert ert.load_existing_rows(out) == grouped  # 两本书同块号不互相覆盖
    assert ert.load_existing_rows(tmp_path / "nope.jsonl") == {}


# ---------- run()：dry-run 与端到端 ----------


def _book(tmp_path):
    p = tmp_path / "book.txt"
    p.write_text(BOOK_TEXT, encoding="utf-8")
    return p


def test_run_dry_run_reports_block_count_without_calling_model(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FailingFakeLLM())  # 真调了就会报失败
    ef.main(["--input", str(_book(tmp_path)), "--source", "modern", "--book", "方剂学", "--dry-run",
             "--out", str(tmp_path / "f.jsonl")])
    out = capsys.readouterr().out
    assert "切成 2 块" in out and "预估调用数 2" in out
    assert not (tmp_path / "f.jsonl").exists()


def test_run_materia_medica_end_to_end_writes_rows_with_source_and_crosscheck(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FakeLLM(MateriaMedicaExtraction(triples=[
        MateriaMedicaItem(s="黄芪", p="用量", o="9～30g", source_span="9～30g。"),
    ]), MateriaMedicaExtraction))
    out = tmp_path / "mm.jsonl"
    emm.main(["--input", str(_book(tmp_path)), "--source", "modern", "--book", "中药学",
              "--out", str(out), "--crosscheck"])
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # 两块都返回同一条（假模型），但第二块（炙甘草）原文里没有"9～30g。"→ 被核验丢弃
    assert len(rows) == 1
    assert rows[0]["source"] == "modern" and rows[0]["book"] == "中药学" and rows[0]["_block"] == 0
    printed = capsys.readouterr().out
    assert "DOSE_LIMITS 交叉校验" in printed


def test_run_only_blocks_out_of_range_is_rejected(tmp_path, monkeypatch):
    _install(monkeypatch, FakeLLM(FormularyExtraction(), FormularyExtraction))
    with pytest.raises(SystemExit, match="超出范围"):
        ef.main(["--input", str(_book(tmp_path)), "--source", "classic", "--book", "b",
                 "--only-blocks", "7", "--out", str(tmp_path / "f.jsonl")])


def test_run_missing_input_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="README"):
        ef.main(["--input", str(tmp_path / "nope.txt"), "--source", "classic", "--book", "b"])


# ---------- DOSE_LIMITS 交叉校验（总纲 2.2） ----------


@pytest.mark.parametrize("text,expected", [
    ("9～30g", 30.0), ("3-9克", 9.0), ("15g", 15.0), ("一钱至三钱", None), ("", None),
])
def test_parse_max_grams(text, expected):
    assert emm.parse_max_grams(text) == expected


def test_crosscheck_dose_limits_buckets_consistent_inconsistent_missing_unparsed():
    rows = [
        {"s": "制附子", "p": "用量", "o": "3～15g", "source": "modern", "book": "中药学"},   # ≤ 15 一致
        {"s": "附子", "p": "用量", "o": "9～30g", "source": "modern", "book": "临床中药学"},  # > 15 不一致
        {"s": "不存在的药", "p": "用量", "o": "3～9g", "source": "modern", "book": "b"},
        {"s": "附子", "p": "用量", "o": "一钱", "source": "modern", "book": "b"},           # 钱两不比
        {"s": "附子", "p": "用量", "o": "9～30g", "source": "classic", "book": "本经"},      # 古籍不比
        {"s": "附子", "p": "性味", "o": "辛热", "source": "modern", "book": "b"},           # 不是用量
    ]
    report = emm.crosscheck_dose_limits(rows)
    assert [e["herb"] for e in report["consistent"]] == ["附子"]      # 制附子归一成附子
    assert [e["herb"] for e in report["inconsistent"]] == ["附子"]
    assert report["inconsistent"][0]["extracted_max_g"] == 30.0
    assert report["inconsistent"][0]["dose_limit_g"] == 15.0
    assert [e["raw"] for e in report["not_in_dose_limits"]] == ["不存在的药"]
    assert [e["dose_text"] for e in report["unparsed"]] == ["一钱"]
    assert "不一致 1" in report["note"] and "一致 1" in report["note"]


def test_formulary_entry_is_thin_and_uses_engine(tmp_path, monkeypatch, capsys):
    _install(monkeypatch, FakeLLM(FormularyExtraction(triples=[
        FormularyItem(s="黄芪", p="组成", o="黄芪", source_span="黄芪"),
    ]), FormularyExtraction))
    out = tmp_path / "f.jsonl"
    ef.main(["--input", str(_book(tmp_path)), "--source", "classic", "--book", "本经",
             "--out", str(out), "--limit", "1"])
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows == [{"s": "黄芪", "p": "组成", "o": "黄芪", "source_span": "黄芪",
                     "source": "classic", "book": "本经", "_block": 0}]
    assert "formulary：本经（classic）" in capsys.readouterr().out
