"""R18-F：药理层两个文件进版本控制。

**这两个文件不在沙盒里**（10248 + 3737 条，在 AutoDL 上跑真实 LLM 抽取才有）。
所以这个文件分两半：
  - 路径解析、回退顺序、覆盖优先、落盘目录——**在这里真测了**，它们只依赖代码；
  - 「10248 / 3737 条」这两个规模数——只能在文件存在时校验，缺文件时 skip，
    不假装通过。上机命令见 R18 报告的「无法完成项」一节。
"""
from __future__ import annotations

import json
import subprocess

import pytest

from core import tools
from core.data_paths import (
    CANONICAL_DIR,
    LEGACY_DIR,
    pharmacology_filename,
    pharmacology_read_path,
    pharmacology_write_path,
)
from core.schemas import FormularyRecord, MateriaMedicaRecord

ROOT = CANONICAL_DIR.parent.parent
KINDS = ("materia_medica", "formulary")
# 实测规模（R10 那一轮在 AutoDL 上跑出来的，eval/RESULTS.md 里有凭据记号）。
# 落盘实测（R34，两份 jsonl 进版本控制之后第一次可核）。
#
# **这两个数换过一次，换的理由要留着**：R18-F 到 R33 期间这里写的是
# 10248 / 3737 —— 那是 AutoDL 上一次抽取的**手抄**数字（eval/RESULTS.md 自己标着
# ⏳「两个 jsonl 还没提交进来」）。R34 数据进版本控制后实测是 9776 / 3184，
# 比手抄的少 472 / 553 条：落盘这一份是**另一次抽取**的产物（`c9ac580` 那轮把
# 离线抽取改成关思考模式重跑过），不是同一批。
#
# 现在这两个数是**可核的**（`pharmacology.n_materia_medica` /
# `pharmacology.n_formulary` 两个凭据键直接数文件行数），所以它们从"手抄"
# 变成了"落盘即判据"——这正是 RESULTS.md 那一节说的"文件一进来凭据键就自动生效"。
EXPECTED_ROWS = {"materia_medica": 9776, "formulary": 3184}


# ---------- 落盘目录：必须是进版本控制的那个 ----------

@pytest.mark.parametrize("kind", KINDS)
def test_write_path_is_under_data_standard(kind):
    """`.gitignore` 对 `*.jsonl` 整体忽略、只给 `data/standard/*.jsonl` 开了例外。

    写进 `data/` 会被静默吞掉——CLAUDE.md「已知的坑」里这条已经踩过两次
    （syndromes.jsonl 差点被吞、M4 的 diseases.jsonl 真的落在例外之外）。
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


def test_legacy_path_would_be_ignored():
    """对照：旧位置确实是被忽略的。没有这条对照，上面那条证明不了什么。"""
    r = subprocess.run(
        ["git", "check-ignore", "-q", str(LEGACY_DIR / "materia_medica.jsonl")],
        cwd=ROOT, capture_output=True)
    assert r.returncode == 0, "旧位置本来就该被忽略，否则 R18-F 这次搬迁没有意义"


# ---------- 读路径的回退顺序 ----------

@pytest.mark.parametrize("kind", KINDS)
def test_read_path_prefers_canonical_over_legacy(kind, tmp_path, monkeypatch):
    """两处都有文件时读 data/standard/ 那一份。"""
    import core.data_paths as dp
    canon, legacy = tmp_path / "standard", tmp_path / "legacy"
    canon.mkdir()
    legacy.mkdir()
    (canon / pharmacology_filename(kind)).write_text("{}\n", encoding="utf-8")
    (legacy / pharmacology_filename(kind)).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(dp, "CANONICAL_DIR", canon)
    monkeypatch.setattr(dp, "LEGACY_DIR", legacy)
    assert dp.pharmacology_read_path(kind) == canon / pharmacology_filename(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_read_path_falls_back_to_legacy(kind, tmp_path, monkeypatch):
    """AutoDL 上那台机器已经有 data/materia_medica.jsonl。硬切路径会让它
    "文件在、却读不到"——而读不到时返回的是 available: false，跟"真的没跑过
    抽取"分不出来。"""
    import core.data_paths as dp
    canon, legacy = tmp_path / "standard", tmp_path / "legacy"
    canon.mkdir()
    legacy.mkdir()
    (legacy / pharmacology_filename(kind)).write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(dp, "CANONICAL_DIR", canon)
    monkeypatch.setattr(dp, "LEGACY_DIR", legacy)
    assert dp.pharmacology_read_path(kind) == legacy / pharmacology_filename(kind)


@pytest.mark.parametrize("kind", KINDS)
def test_read_path_is_none_when_neither_exists(kind, tmp_path, monkeypatch):
    """返回 None，不是返回一个不存在的路径——后者会让每个调用方各自实现
    一遍回退顺序，回退顺序就有了三份。"""
    import core.data_paths as dp
    monkeypatch.setattr(dp, "CANONICAL_DIR", tmp_path / "a")
    monkeypatch.setattr(dp, "LEGACY_DIR", tmp_path / "b")
    assert dp.pharmacology_read_path(kind) is None
    # 要在报错里说出路径的场合仍拿得到"应该在哪儿"
    assert dp.pharmacology_read_path_or_canonical(kind).name == pharmacology_filename(kind)


# ---------- 三个调用方走的是同一处 ----------

def test_all_three_call_sites_resolve_through_data_paths():
    """CLAUDE.md 第 31 条：「这个文件在哪儿」只能有一处答案。

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
    """文件在 import 之后才生成（run_onsite.sh 段 5 落盘、服务先起来）。
    只用 import 时算好的常量会永远读不到新文件。"""
    import core.data_paths as dp
    canon = tmp_path / "standard"
    canon.mkdir()
    monkeypatch.setattr(dp, "CANONICAL_DIR", canon)
    monkeypatch.setattr(dp, "LEGACY_DIR", tmp_path / "nope")
    # 模块级常量指向一个不存在的文件（= import 时两处都还没有）
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
            f"{pharmacology_filename(kind)} 不在沙盒里（需要 AutoDL 上跑过抽取）。"
            f"上机命令：python -m offline.extract_{kind} --input books/… "
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
    """规模数有对照基准（CLAUDE.md「任何数字都必须带对照」）：
    这两个数写在 eval/RESULTS.md 里、带凭据记号，这条测试是它们的另一端。"""
    assert len(_rows(kind)) == EXPECTED_ROWS[kind]
