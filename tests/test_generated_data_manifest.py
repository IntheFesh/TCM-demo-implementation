"""R31：落盘的生成物跟当前代码一致——这件事以前没有任何东西盯着。

R29 发现 `data/standard/syndromes.jsonl` 按仓库里的复现命令重跑一遍，跟落盘那份
**对不上**：3 个证型名、8 条症状列表不同，而且落盘那份里还留着「纳呆便唐」，
而 `ocr_fixes.tsv` 明明有「便唐→便溏」这一条。落盘的是旧解析器的产物，
README 给的是新解析器，中间**没有判据**，而且 31 条教材解析测试全绿
——它们查的是"这条规则生效了吗"，不是"落盘的是不是这套代码的产物"。

判据分两层，因为重新抽一遍要教材 markdown，**而它不在版本控制里**：

  这个文件（pytest，离线秒级）      查能离线查的三件：
                                  落盘 jsonl 被手改过吗、
                                  修正表改了没重新生成吗（**R29 那个缺陷正是它能抓的**）、
                                  manifest 自己记的计数对不对
  scripts/verify_generated_data.py 查离线查不了的那件：解析器改了没重新生成吗
  （在有教材的机器上跑）            ——它真的重抽一遍，逐字节比

分两层不是妥协，是因为这两件事**能不能离线查**本来就不同。把第二件也塞进 pytest
只有两个结局：要么 tests 需要网络（破掉「秒级、不需要网络」那条），
要么它在没有教材时静默跳过——而静默跳过的判据等于没有判据。
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
ONSITE = ROOT / "scripts" / "run_onsite.sh"


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
    确认它没被 `*.jsonl` 之外的什么规则吞掉。CLAUDE.md 那个坑已经踩过四次。"""
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
    """**这一条就是 R29 那个缺陷的判据。** 当时表里加了「便唐→便溏」，
    而落盘的 jsonl 里「纳呆便唐」还在——表改了、生成物没重跑，没有任何东西报错。"""
    assert manifest["ocr_fixes_sha256"] == _sha256(OCR_FIXES_PATH), (
        "ocr_fixes.tsv 改过但证候表没重新生成。跑："
        "python -m offline.build_syndrome_textbook --md-path <教材> "
        "--out data/standard/syndromes.jsonl --append"
    )


def test_the_parser_hash_is_recorded_even_though_pytest_cannot_check_it(manifest):
    """解析器的 sha256 **记下来但不在这里断言**：断言它等于现算值，就等于要求
    每次动 `build_syndrome_textbook.py`（哪怕只改一句注释）都必须重新生成一遍,
    而重新生成要教材 markdown——clone 这个仓库的人手里没有，会被一条自己修不了的
    红judgment卡住。所以这一条只查"这个字段在、格式对"，真比对交给
    `scripts/verify_generated_data.py`。
    """
    assert len(manifest["parser_sha256"]) == 64
    assert all(c in "0123456789abcdef" for c in manifest["parser_sha256"])
    assert PARSER_PATH.exists()


# ---------- manifest 自己记的计数 ----------

def test_the_recorded_counts_match_the_committed_file(manifest):
    entries = _committed_entries()
    textbook = [e for e in entries if e.source == "textbook"]
    assert manifest["n_lines"] == len(entries)
    assert manifest["n_textbook"] == len(textbook)
    groups = Counter((e.name, e.disease) for e in textbook)
    assert manifest["n_duplicate_name_disease_groups"] == sum(1 for c in groups.values() if c > 1)


def test_the_manifest_records_which_textbook_it_is(manifest):
    """五本专科教材扩表之后，manifest 要能说清这一份是哪一本的产物。"""
    assert manifest["layout"] == "neike"
    assert manifest["book"] == "中医内科学"
    assert manifest["generated_at"].endswith("Z")


def test_the_manifest_records_the_heuristic_heading_count(manifest):
    """靠启发式判据认下来的证型标题条数（R31 实测 38）也记进 manifest——
    它一变就说明原文排版跟当初量的那份不一样了。"""
    assert manifest["headings_bare_numbered"] == 38
    assert manifest["n_suspicious"] == 27


# ---------- 那个脚本本身 ----------

def test_the_verifier_exists_and_documents_its_exit_codes():
    src = VERIFIER.read_text(encoding="utf-8")
    for code, meaning in ((0, "一致"), (1, "不一致"), (2, "没有 manifest"), (3, "没核")):
        assert f"  {code}  " in src, f"退出码 {code}（{meaning}）没写进文档字符串"
    # 拿不到教材时必须是 3（"没核"），不是 0（"核过了没问题"）
    assert "return 3" in src


def test_the_verifier_is_wired_into_the_onsite_script():
    """判据放在脚本里、脚本没人跑，等于没有判据。上机剧本段 1 是零调用的验证段。"""
    src = ONSITE.read_text(encoding="utf-8")
    assert "scripts.verify_generated_data" in src


def test_the_verifier_runs_clean_on_the_committed_tree():
    """**这一条是这一轮的验收**：仓库当前状态下，落盘的证候表就是当前代码的产物。
    沙盒里有教材（/tmp/tcmds），所以这条能真跑；没有教材的机器上它退 3
    （"没核"），那时这条判据按 3 也算通过——**不把"没核"读成"核过了"**。
    """
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
    """把 manifest 记的指纹改掉一位，脚本必须报出来——**先红后绿里的红**。

    **改的是 tmp_path 里的副本，不是版本控制里那份。** R31 的第一版原地改真文件、
    在 `finally` 里恢复：只要那次跑被打断（Ctrl-C、超时被杀、pytest 自己崩），
    文件就留在坏状态里，而下一次跑报的是「落盘的 jsonl 被手改过」——
    指向一个完全错误的原因。R33 实测踩到过一次。
    脚本因此有了 `--manifest` / `--jsonl` 两个开关。
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
    # 真文件一个字节都没动过——这条断言就是这次修复本身的判据。
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
    """判据化上面那条教训：这个文件里不许出现对 `MANIFEST_PATH` 的写操作。

    判据是**源码里没有这种调用**，不是"跑一遍看文件变没变"——后者只在正好被打断
    的那次才看得出来，而那正是这个缺陷难查的原因。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    # **先切掉这条测试自己的函数体**，不然它的禁用词表就是第一个命中项
    # （R31 在 `test_parse_textbook_has_no_second_heading_matcher` 上踩过同一形状：
    # 一条"源码里不许出现 X"的测试，自己的源码里必然出现 X）。
    src = src[:src.index("def " + "test_no_test_in_this_file_writes")]
    for attr in ("MANIFEST_PATH", "DEFAULT_OUT_PATH"):
        for verb in ("write_text", "write_bytes", "unlink"):
            forbidden = f"{attr}.{verb}"
            assert forbidden not in src, (
                f"{forbidden} 会原地改版本控制里的生成物；用 --manifest / --jsonl "
                "指向 tmp_path 里的副本"
            )
