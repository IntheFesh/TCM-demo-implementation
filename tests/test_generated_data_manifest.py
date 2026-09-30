"""落盘的生成物与当前代码一致。

`data/standard/syndromes.jsonl` 是由教材 markdown 生成的。解析器或 OCR 修正表改了而没有
重新生成，落盘那份就与复现命令跑出来的不一样，而教材解析的单元测试查不出这一点
（它们查的是"这条规则生效了吗"）。`syndromes_manifest.json` 记录生成时的指纹。

判据分两层，因为重新抽取需要教材 markdown，而它不随仓库分发：

  这个文件（pytest，离线）          落盘 jsonl 被手改过吗、修正表改了没重新生成吗、
                                  manifest 自己记的计数对不对、解析器代码指纹对不对
  scripts/verify_generated_data.py 有教材 markdown 时重抽一遍，逐字节比较
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from core.schemas import SyndromeDefinition
from offline.build_syndrome_textbook import (
    DEFAULT_OUT_PATH,
    MANIFEST_PATH,
    OCR_FIXES_PATH,
    PARSER_PATH,
    read_manifest,
)

ROOT = Path(__file__).resolve().parent.parent
VERIFIER = ROOT / "scripts" / "verify_generated_data.py"
PIPELINE = ROOT / "scripts" / "run_pipeline.sh"


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def manifest():
    m = read_manifest()
    assert m is not None, f"{MANIFEST_PATH} 不在——生成 jsonl 的那一步应该顺手写它"
    return m


def _committed_entries() -> list[SyndromeDefinition]:
    out = []
    for line in DEFAULT_OUT_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(SyndromeDefinition.model_validate_json(line))
    return out


# ---------- 三个 sha256 ----------

def test_the_manifest_is_in_version_control():
    """`data/standard/*.jsonl` 有 .gitignore 例外，但这是 `.json`——
    确认它没被 `*.jsonl` 之外的什么规则吞掉（docs/ARCHITECTURE.md §6）。"""
    import subprocess
    r = subprocess.run(["git", "check-ignore", "-q", str(MANIFEST_PATH.relative_to(ROOT))],
                       cwd=ROOT, capture_output=True)
    assert r.returncode != 0, "manifest 被 gitignore 了，等于没有"


def test_the_committed_jsonl_has_not_been_hand_edited(manifest):
    """**手改落盘的 jsonl 是不行的**：改了它，图谱和 manifest 都不知道。
    要改就改上游（修正表 / 解析器）再重新生成。"""
    assert manifest["syndromes_sha256"] == _sha256(DEFAULT_OUT_PATH), (
        "落盘的 syndromes.jsonl 跟 manifest 记的指纹对不上。要么它被手改过，"
        "要么重新生成之后 manifest 没跟着写。"
    )


def test_the_ocr_fix_table_did_not_change_without_regenerating(manifest):
    """**这条是"修正表改了、生成物没重跑"的判据。** 比如表里加了「便唐→便溏」，
    而落盘的 jsonl 里「纳呆便唐」还在——表改了、生成物没重跑，没有任何东西报错。"""
    assert manifest["ocr_fixes_sha256"] == _sha256(OCR_FIXES_PATH), (
        "ocr_fixes.tsv 改过但证候表没重新生成。跑："
        "python -m offline.build_syndrome_textbook --md-path <教材> "
        "--out data/standard/syndromes.jsonl --append"
    )


def test_the_parser_fingerprint_matches_the_current_code(manifest):
    """解析器指纹忽略注释与文档字符串（`offline/code_fingerprint.py`），所以只改注释
    不需要重新生成；改了代码就要重新生成（需要教材 markdown）并更新 manifest。"""
    from offline.code_fingerprint import code_fingerprint

    assert manifest["parser_sha256"] == code_fingerprint(PARSER_PATH)


def test_the_fingerprint_ignores_comments_and_docstrings_but_not_code(tmp_path):
    from offline.code_fingerprint import code_fingerprint

    base = tmp_path / "a.py"
    base.write_text('def f(x):\n    """说明。"""\n    return x + 1  # 加一\n', encoding="utf-8")
    reworded = tmp_path / "b.py"
    reworded.write_text('def f(x):\n    """换一种说法。\n\n    多一段。\n    """\n'
                        '    # 新注释\n    return x + 1\n', encoding="utf-8")
    changed = tmp_path / "c.py"
    changed.write_text('def f(x):\n    """说明。"""\n    return x + 2  # 加一\n', encoding="utf-8")
    assert code_fingerprint(base) == code_fingerprint(reworded)
    assert code_fingerprint(base) != code_fingerprint(changed)


# ---------- manifest 自己记的计数 ----------

def test_the_recorded_counts_match_the_committed_file(manifest):
    entries = _committed_entries()
    textbook = [e for e in entries if e.source == "textbook"]
    assert manifest["n_lines"] == len(entries)
    assert manifest["n_textbook"] == len(textbook)
    groups = Counter((e.name, e.disease) for e in textbook)
    assert manifest["n_duplicate_name_disease_groups"] == sum(1 for c in groups.values() if c > 1)


def test_the_manifest_records_which_textbook_it_is(manifest):
    """manifest 要能说清这一份是哪一本教材的产物。"""
    assert manifest["layout"] == "neike"
    assert manifest["book"] == "中医内科学"
    assert manifest["generated_at"].endswith("Z")


def test_the_manifest_records_the_heuristic_heading_count(manifest):
    """靠启发式判据认下来的证型标题条数也记进 manifest：它一变就说明原文排版
    与生成时的那份不一样了。"""
    assert manifest["headings_bare_numbered"] == 38
    assert manifest["n_suspicious"] == 27


# ---------- 那个脚本本身 ----------

def test_the_verifier_exists_and_documents_its_exit_codes():
    src = VERIFIER.read_text(encoding="utf-8")
    for code, meaning in ((0, "一致"), (1, "不一致"), (2, "没有 manifest"), (3, "没核")):
        assert f"  {code}  " in src, f"退出码 {code}（{meaning}）没写进文档字符串"
    # 拿不到教材时必须是 3（"没核"），不是 0（"核过了没问题"）
    assert "return 3" in src


def test_the_verifier_is_wired_into_the_pipeline():
    """流水线步骤 1（零调用验证）会运行这个核对脚本。"""
    src = PIPELINE.read_text(encoding="utf-8")
    assert "scripts.verify_generated_data" in src


def test_the_verifier_runs_clean_on_the_committed_tree():
    """仓库当前状态下，落盘的证候表就是当前代码的产物。有教材 markdown（books/）时
    逐字节重抽；没有时退出码 3（"没核"），这条判据按 3 也算通过，但三个指纹必须一致
    （否则退出码是 1）。"""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "scripts.verify_generated_data"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode in (0, 3), f"退出码 {r.returncode}\n{r.stdout}\n{r.stderr}"
    if r.returncode == 3:
        assert "没核" in r.stdout
    else:
        assert "就是当前这套代码" in r.stdout


def test_the_verifier_catches_a_stale_committed_file(tmp_path):
    """把 manifest 记的指纹改掉，脚本必须报出来。

    改的是 tmp_path 里的副本，不是版本控制里那份：原地改再在 `finally` 里恢复的话，
    一旦运行被打断，文件就留在坏状态里。脚本因此有 `--manifest` / `--jsonl` 两个开关。
    """
    import subprocess
    import sys

    bad = tmp_path / "syndromes_manifest.json"
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    data["syndromes_sha256"] = "0" * 64
    bad.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    r = subprocess.run([sys.executable, "-m", "scripts.verify_generated_data",
                        "--manifest", str(bad)],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 1, r.stdout
    assert "落盘 jsonl" in r.stdout
    # 版本控制里的文件没有被改动
    assert json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["syndromes_sha256"] != "0" * 64


def test_a_missing_manifest_is_its_own_exit_code(tmp_path):
    """"没有这份记录" 跟 "有记录但对不上" 要分开——前者去生成，后者去查哪里变了。

    同上：指向一个 tmp_path 里**不存在**的路径，不去 unlink 版本控制里那份。
    """
    import subprocess
    import sys

    r = subprocess.run([sys.executable, "-m", "scripts.verify_generated_data",
                        "--manifest", str(tmp_path / "没有这个文件.json")],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 2, r.stdout
    assert "没有" in r.stdout
    assert MANIFEST_PATH.exists(), "真文件必须还在"


def test_no_test_in_this_file_writes_to_the_version_controlled_data_dir():
    """这个文件里不许出现对 `MANIFEST_PATH` 的写操作。

    判据是源码里没有这种调用，而不是"跑一遍看文件变没变"：后者只在运行正好被打断
    的那次才看得出来。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    # 先切掉这条测试自己的函数体，否则它的禁用词表就是第一个命中项。
    src = src[:src.index("def " + "test_no_test_in_this_file_writes")]
    for attr in ("MANIFEST_PATH", "DEFAULT_OUT_PATH"):
        for verb in ("write_text", "write_bytes", "unlink"):
            forbidden = f"{attr}.{verb}"
            assert forbidden not in src, (
                f"{forbidden} 会原地改版本控制里的生成物；用 --manifest / --jsonl "
                "指向 tmp_path 里的副本"
            )
