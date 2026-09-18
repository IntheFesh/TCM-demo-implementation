"""R46 §7.3：循证对照层。

**两条贯穿全文件的纪律：**
1. 这一层只呈现差异，**不参与选择**——没有分数、没有排序、不回答"哪个更好"。
2. 底本是**教材推荐方案**，不是《中医药循证临床实践指南》。那份指南的全文不在
   这个项目里，照抄一份"指南说什么"等于编造出处。界面文案如实称
   「与教材推荐方案的对照」。
"""
import ast
import json
from pathlib import Path

import pytest

from core.guideline_compare import (
    BASIS_ID,
    BASIS_LABEL,
    GUIDELINES_PATH,
    compare,
    coverage_stats,
    entries_for,
    load_guidelines,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------- 底本 ----------

def test_the_data_file_lives_in_the_canonical_directory():
    """CLAUDE.md：新增的 `.jsonl` 一律放 `data/standard/`——`.gitignore` 对
    `*.jsonl` 是整体忽略、只对那个目录开了例外。这个坑已经踩过两次。"""
    assert GUIDELINES_PATH.parent == ROOT / "data" / "standard"


def test_the_file_is_actually_tracked_by_git():
    """上一条只查路径对不对，这一条查它**真的进了版本控制**——
    路径对但被忽略掉，症状是"我这儿有、你那儿没有"。"""
    import subprocess

    out = subprocess.run(["git", "check-ignore", str(GUIDELINES_PATH)],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode != 0, f"{GUIDELINES_PATH} 被 .gitignore 吞了"


def test_every_row_carries_its_source_text():
    """**取不到出处原文的条目不收。** 一条没有出处的"推荐方案"在这个项目里
    等于没有——它会被当成可核对的依据显示出来。"""
    rows = load_guidelines()
    assert rows, "底本是空的"
    for r in rows:
        assert r.span.strip(), f"{r.syndrome}/{r.recommended_formula} 没有原文片段"
        assert r.source.strip(), f"{r.syndrome}/{r.recommended_formula} 没有书名"


def test_the_evidence_level_says_it_is_a_textbook():
    for r in load_guidelines():
        assert r.evidence_level == BASIS_ID


def test_the_basis_label_does_not_claim_to_be_a_guideline():
    """界面上这一层叫「教材推荐方案」。称它"指南"就是给一份拿不到的文件背书。"""
    assert "指南" not in BASIS_LABEL
    assert BASIS_LABEL == "教材推荐方案"


def test_coverage_is_reported_not_hidden():
    """产品面显示「未覆盖」时，读的人得知道这一层整体覆盖到什么程度，
    否则会以为是自己这一次特殊。"""
    s = coverage_stats()
    assert s["rows"] > 0 and s["syndromes"] > 0
    assert s["books"] and s["basis_label"] == BASIS_LABEL


def test_a_missing_file_gives_an_empty_basis_not_a_crash(tmp_path):
    """这一层是增量信息，缺了它辨证照跑。"""
    assert load_guidelines(tmp_path / "nope.jsonl") == ()


# ---------- 三类结果 ----------

def _any_covered_syndrome() -> str:
    return load_guidelines()[0].syndrome


def test_not_covered_says_so_and_does_not_invent():
    out = compare("一个底本里没有的证型", "疏肝", "柴胡疏肝散", ["柴胡"])
    assert out["covered"] is False
    assert out["not_covered"] and "没有" in out["not_covered"]
    assert out["aligned"] == [] and out["deviations"] == []


def test_not_covered_does_not_imply_the_reasoning_is_wrong():
    """**措辞要紧**：未覆盖不代表推导有误，也不代表它被教材支持。"""
    out = compare("一个底本里没有的证型", "疏肝", "柴胡疏肝散", [])
    assert "不代表推导有误" in out["not_covered"]


def test_a_matching_formula_lands_in_aligned_with_its_source():
    e = load_guidelines()[0]
    out = compare(e.syndrome, e.recommended_principle, e.recommended_formula, [])
    assert out["covered"] is True
    hit = [a for a in out["aligned"] if a["what"] == "主方"]
    assert hit and hit[0]["source"] and hit[0]["span"]


def test_a_different_formula_lands_in_deviations_with_both_sides():
    e = load_guidelines()[0]
    out = compare(e.syndrome, e.recommended_principle, "一个不在教材里的方", [])
    d = [x for x in out["deviations"] if x["what"] == "主方"]
    assert d, "主方不同没有进 deviations"
    assert d[0]["ours"] == "一个不在教材里的方"
    assert d[0]["recommended"], "没有说教材推荐的是什么"
    assert d[0]["source"], "差异条目没有出处"


def test_the_principle_comparison_goes_through_the_synonym_table():
    """治法一致与否走 `core.effect_synonyms`，不裸比字符串——
    覆盖检查那次撞墙（「水肿」vs「肿胀」）就是裸比出来的。"""
    src = (ROOT / "core" / "guideline_compare.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    body = ast.get_source_segment(src, tree) or src
    assert "expand_effect" in body
    assert "from core.effect_synonyms import" in src


def test_modifications_are_only_counted_when_the_formula_matches():
    """方都不一样，比药味差几味没有意义。"""
    e = load_guidelines()[0]
    out = compare(e.syndrome, "", "一个不在教材里的方", ["柴胡", "黄芩"])
    assert not [d for d in out["deviations"] if d["what"] == "加减"]


def test_the_summary_is_one_line_for_the_product_surface():
    e = load_guidelines()[0]
    out = compare(e.syndrome, e.recommended_principle, e.recommended_formula, [])
    assert "\n" not in out["summary"]
    assert out["summary"].startswith(f"与{BASIS_LABEL}的对照：")


def test_an_empty_syndrome_is_treated_as_not_covered():
    out = compare("", "", "", [])
    assert out["covered"] is False


# ---------- 不用于择优 ----------

def test_the_module_exposes_no_score_or_ranking():
    """这一层有分数就会变成另一种投票（R44 刚把投票痕迹从产品面消除）。"""
    import core.guideline_compare as mod

    for name in dir(mod):
        low = name.lower()
        assert not any(w in low for w in ("score", "rank", "best", "prefer", "choose")), \
            f"这一层出现了择优的迹象：{name}"


def test_the_comparison_result_has_no_recommendation_field():
    e = load_guidelines()[0]
    out = compare(e.syndrome, e.recommended_principle, "别的方", [])
    assert "recommended_choice" not in out and "winner" not in out
    # `recommended` 在 deviations 里指的是"教材推荐的方案"，不是"建议采用"
    for d in out["deviations"]:
        assert set(d) <= {"what", "recommended", "ours", "note", "source", "span"}


def test_the_module_says_out_loud_that_it_is_not_for_choosing():
    src = (ROOT / "core" / "guideline_compare.py").read_text(encoding="utf-8")
    assert "不是用来择优的" in src or "不参与选择" in src


def test_the_generated_rows_match_what_the_builder_produces():
    """生成物与生成它的代码必须一致——不一致说明有人手改了 jsonl，
    而手改的那一行下次重新生成时会消失。"""
    from offline.build_guidelines import build_rows

    on_disk = [json.loads(line) for line in
               GUIDELINES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    fresh = build_rows()
    assert len(on_disk) == len(fresh), "磁盘上的行数跟现在重新生成的不一致"
    assert on_disk[0] == fresh[0]


def test_entries_for_strips_the_trailing_zheng_character():
    e = load_guidelines()[0]
    stem = e.syndrome.removesuffix("证")
    assert entries_for(stem), "去掉尾「证」之后匹配不上了"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_entries_for_handles_empty_input(bad):
    assert entries_for(bad) == []
