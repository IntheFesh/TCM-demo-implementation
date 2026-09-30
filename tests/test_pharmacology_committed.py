"""药理层两个文件（本草、方剂三元组）在版本控制里：`data/standard/` 下随仓库提交。

这个文件分两半：
  - 落盘目录、读路径解析、调用方统一走 `core.data_paths`——只依赖代码，总是运行；
  - 行数与 schema 校验——只在文件存在时运行，缺文件时 skip，不假装通过。
"""
from __future__ import annotations

import json
import subprocess

import pytest

from core import tools
from core.data_paths import (
    CANONICAL_DIR,
    pharmacology_filename,
    pharmacology_read_path,
    pharmacology_write_path,
)
from core.schemas import FormularyRecord, MateriaMedicaRecord

ROOT = CANONICAL_DIR.parent.parent
KINDS = ("materia_medica", "formulary")
# 两份 jsonl 的行数。这两个数与 eval/RESULTS.md 里 `pharmacology.n_materia_medica` /
# `pharmacology.n_formulary` 两个凭据键核对的是同一对数（凭据键直接数文件行数）。
# 合并来的开源数据集的行带 `dataset` 字段，按它可以把两拨数据分开数，
# 见 `tests/test_merge_open_sources.py`。
EXPECTED_ROWS = {"materia_medica": 10156, "formulary": 5067}


# ---------- 落盘目录：必须是进版本控制的那个 ----------

@pytest.mark.parametrize("kind", KINDS)
def test_write_path_is_under_data_standard(kind):
    """`.gitignore` 对 `*.jsonl` 整体忽略、只给 `data/standard/*.jsonl` 开了例外
    （docs/ARCHITECTURE.md §6）。

    写到 `data/` 的别处会被静默吞掉，所以落盘路径必须在 `data/standard/` 下。
    """
    p = pharmacology_write_path(kind)
    assert p.parent == CANONICAL_DIR
    assert p.name == pharmacology_filename(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_gitignore_actually_lets_the_write_path_through(kind):
    """不是"看 .gitignore 文本觉得应该行"，是真的问 git。"""
    p = pharmacology_write_path(kind)
    r = subprocess.run(["git", "check-ignore", "-q", str(p)], cwd=ROOT,
                       capture_output=True)
    # git check-ignore 退出码 1 = 没被忽略
    assert r.returncode == 1, f"{p} 会被 .gitignore 吞掉"


def test_the_data_root_would_be_ignored():
    """对照：data/ 根目录下的同名文件会被 .gitignore 忽略。没有这条对照，上面那条
    证明不了什么——两条合起来说明只有 data/standard/ 这个位置能进版本控制。"""
    r = subprocess.run(
        ["git", "check-ignore", "-q", str(ROOT / "data" / "materia_medica.jsonl")],
        cwd=ROOT, capture_output=True)
    assert r.returncode == 0, "data/ 根目录下的 .jsonl 应当被忽略"


# ---------- 读路径 ----------

@pytest.mark.parametrize("kind", KINDS)
def test_read_path_is_none_when_the_file_is_missing(kind, tmp_path, monkeypatch):
    """返回 None，不是返回一个不存在的路径——后者会让每个调用方各自实现
    一遍存在性判断。"""
    import core.data_paths as dp
    monkeypatch.setattr(dp, "CANONICAL_DIR", tmp_path / "a")
    assert dp.pharmacology_read_path(kind) is None
    # 要在报错里说出路径的场合仍拿得到"应该在哪儿"
    assert dp.pharmacology_read_path_or_canonical(kind).name == pharmacology_filename(kind)


# ---------- 三个调用方走的是同一处 ----------

def test_all_three_call_sites_resolve_through_data_paths():
    """「这个文件在哪儿」只能有一处答案（docs/ARCHITECTURE.md §4）。

    判据不是"读代码觉得对"，是真去数源码里还有没有第二处写死的路径字面量。
    """
    import re
    hits = []
    for rel in ("core/tools.py", "offline/export_sft.py",
                "offline/extract_reference_triples.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        # 只看真实代码行，注释和文档字符串里提到文件名是在解释，不是实现
        for lineno, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r'"(?:materia_medica|formulary)\.jsonl"', code):
                hits.append(f"{rel}:{lineno}")
    assert hits == [], f"还有写死的路径字面量：{hits}"


def test_tools_honours_an_overridden_path(tmp_path, monkeypatch):
    """测试和部署会 monkeypatch MATERIA_MEDICA_PATH。直接调 read_path 会让这个
    覆盖静默失效——读了另一份文件、不报错，最难查的一类。"""
    p = tmp_path / "materia_medica.jsonl"
    row = {"s": "黄芪", "p": "性味", "o": "甘，微温",
           "source_span": "黄芪，味甘微温", "source": "classic", "book": "神农本草经"}
    p.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(tools, "MATERIA_MEDICA_PATH", p)
    tools.reset_tool_caches()
    try:
        assert tools._materia_medica_path() == p
        assert tools._load_materia_medica() == [row]
    finally:
        tools.reset_tool_caches()


def test_tools_resolves_late_created_files(tmp_path, monkeypatch):
    """文件在 import 之后才生成（服务先起来，药理层抽取之后才落盘）。
    只用 import 时算好的常量会永远读不到新文件。"""
    import core.data_paths as dp
    canon = tmp_path / "standard"
    canon.mkdir()
    monkeypatch.setattr(dp, "CANONICAL_DIR", canon)
    # 模块级常量指向一个不存在的文件（= import 时文件不存在）
    monkeypatch.setattr(tools, "MATERIA_MEDICA_PATH", canon / "materia_medica.jsonl")
    tools.reset_tool_caches()
    assert tools._materia_medica_path() is None      # 此刻确实没有
    (canon / "materia_medica.jsonl").write_text("{}\n", encoding="utf-8")
    assert tools._materia_medica_path() == canon / "materia_medica.jsonl"
    tools.reset_tool_caches()


# ---------- 文件真的在时才校验的那几条 ----------

def _rows(kind):
    p = pharmacology_read_path(kind)
    if p is None:
        pytest.skip(
            f"{pharmacology_filename(kind)} 不存在。抽取命令：python -m offline.extract_{kind} "
            f"--input books/… "
            f"--source … --book …，产物落 {pharmacology_write_path(kind)}"
        )
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.mark.parametrize("kind,model", [("materia_medica", MateriaMedicaRecord),
                                        ("formulary", FormularyRecord)])
def test_every_committed_row_validates_against_its_schema(kind, model):
    """进版本控制的每一行都要过 schema——source_span 非空这条防幻觉约束
    不能只在抽取时校验一次，落盘之后也要能再验。"""
    for i, row in enumerate(_rows(kind), 1):
        model(**row)  # 任何一行不合法就在这里抛，行号在报错里


@pytest.mark.parametrize("kind", KINDS)
def test_committed_row_counts_match_the_recorded_numbers(kind):
    """规模数有对照基准（docs/ARCHITECTURE.md §7）：
    这两个数写在 eval/RESULTS.md 里、带凭据记号，这条测试是它们的另一端。"""
    assert len(_rows(kind)) == EXPECTED_ROWS[kind]
