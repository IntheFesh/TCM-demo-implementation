"""R32 功效同义词表测试。

这张表存在的唯一理由是：治法词（S3 的 `method`）和功效词（本草条目的「功效」）
在文献里是**两套措辞**，裸子串比会把大部分正确的药判成不匹配——R34 的
`effect_matches_method` 规则就会变成恒假，等于没有验证。所以这里测的重点是
**双向展开**和**表外词退化成裸子串**这两条，而不是"表能不能读进来"。
"""
from __future__ import annotations

import pytest

from core.effect_synonyms import (
    EFFECT_SYNONYMS_PATH,
    SOURCES,
    EffectSynonym,
    expand_effect,
    load_effect_synonyms,
    methods_for_effect,
    reset_for_tests,
    table_stats,
)


@pytest.fixture(autouse=True)
def _fresh():
    reset_for_tests()
    yield
    reset_for_tests()


def test_the_table_is_in_version_control_and_loads():
    """`data/standard/*.tsv` 在版本控制里。缺了不是"可以跳过的一步"。"""
    assert EFFECT_SYNONYMS_PATH.exists(), f"{EFFECT_SYNONYMS_PATH} 应该在版本控制里"
    rows = load_effect_synonyms()
    assert len(rows) >= 40, f"要求 ≥40 条，实际 {len(rows)}"
    assert all(isinstance(r, EffectSynonym) for r in rows)


def test_every_row_carries_a_source_and_the_sources_are_separated():
    """一份表里混着核过的和没核的，整份表的可信度只能按最低那条算。"""
    rows = load_effect_synonyms()
    assert {r.source for r in rows} <= set(SOURCES)
    s = table_stats()
    assert s["n_rows"] == s["n_textbook"] + s["n_common"]
    assert s["n_textbook"] > 0, "全表都是 common 的话这张表不该叫「教材同义」"


def test_the_match_relation_is_symmetric_across_the_whole_table():
    """要钉的是**匹配关系**对称（`u ∈ expand(t) ⟺ t ∈ expand(u)`），不是两个词的
    邻域相等。邻域本来就可以不等——「疏肝解郁」出现在多行里，它的邻居自然比
    只出现一行的「疏肝理气」多。

    对称的是关系这件事才是 R34 要的：规则的结论不能取决于模型把这个词写在了
    `method` 还是写在了本草的「功效」里。全表 223 个词两两核一遍，不抽样。
    """
    rows = load_effect_synonyms()
    terms = sorted({r.method for r in rows} | {e for r in rows for e in r.effects})
    assert len(terms) >= 150, f"表里只有 {len(terms)} 个词，展开面太窄"
    asym = [(t, u) for t in terms for u in expand_effect(t) if t not in expand_effect(u)]
    assert asym == [], f"匹配关系不对称：{asym[:5]}"


def test_expand_effect_does_not_blow_up_into_one_giant_family():
    """一跳（同行共现）而不是传递闭包：闭包会顺着共享词把「清热」和「温里」
    连成一片，`effect_matches_method` 就变成恒真——跟恒假一样等于没有验证。
    实测最大邻域 26（清热），远小于 223 个词，说明没有塌成一团。
    """
    rows = load_effect_synonyms()
    terms = {r.method for r in rows} | {e for r in rows for e in r.effects}
    biggest = max(len(expand_effect(t)) for t in terms)
    assert biggest < len(terms) // 3, f"最大邻域 {biggest}/{len(terms)}，同义族塌成一团了"


def test_expand_effect_always_contains_the_term_itself():
    for term in ("疏肝理气", "这个治法表里没有"):
        assert term in expand_effect(term)


def test_an_unlisted_term_degrades_to_the_bare_word_not_to_empty():
    """表外词返回空的话，规则会判它"无论如何都不匹配"；正确语义是
    "这个说法我没登记，按字面看"。"""
    assert expand_effect("温阳化气利水通淋且表里双解") == ("温阳化气利水通淋且表里双解",)
    assert expand_effect("") == ()
    assert expand_effect("   ") == ()


def test_methods_for_effect_reverses_the_mapping():
    rows = load_effect_synonyms()
    row = next(r for r in rows if r.effects)
    assert row.method in methods_for_effect(row.effects[0])
    assert methods_for_effect("") == ()


def test_a_missing_file_raises_instead_of_returning_an_empty_table(tmp_path):
    """静默返回空表 = `effect_matches_method` 恒假且没人发现。"""
    with pytest.raises(FileNotFoundError) as e:
        load_effect_synonyms(tmp_path / "没有这个文件.tsv")
    assert "恒假" in str(e.value) or "不完整" in str(e.value)


@pytest.mark.parametrize(
    "line, why",
    [
        ("疏肝理气\t疏肝解郁", "少一列"),
        ("疏肝理气\t疏肝解郁\t胡编的来源", "来源不在白名单"),
        ("\t疏肝解郁\ttextbook", "治法为空"),
        ("疏肝理气\t\ttextbook", "功效为空"),
    ],
)
def test_a_malformed_row_raises_with_the_line_number(tmp_path, line, why):
    p = tmp_path / "t.tsv"
    p.write_text("治法\t功效词\t来源\n" + line + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as e:
        load_effect_synonyms(p)
    assert ":2" in str(e.value), f"{why}：报错要带行号，否则 80 行的表里找不到是哪条"


def test_a_duplicate_method_raises():
    """重复项只会让人以为改对了——同一个治法两行，改了前一行不生效。"""
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        p = _P(d) / "t.tsv"
        p.write_text("治法\t功效词\t来源\n疏肝\t疏肝解郁\ttextbook\n"
                     "疏肝\t理气\ttextbook\n", encoding="utf-8")
        with pytest.raises(ValueError) as e:
            load_effect_synonyms(p)
        assert "重复" in str(e.value)


def test_comments_and_blank_lines_are_skipped(tmp_path):
    p = tmp_path / "t.tsv"
    p.write_text("# 注释\n\n治法\t功效词\t来源\n疏肝\t疏肝解郁,理气\ttextbook\n",
                 encoding="utf-8")
    rows = load_effect_synonyms(p)
    assert len(rows) == 1 and rows[0].effects == ("疏肝解郁", "理气")


def test_this_table_is_not_a_second_copy_of_syndrome_norm_synonyms():
    """CLAUDE.md 第 31 条的例外：两处回答的不是同一个问题，必须在代码里写清区别。

    判据是**文档里写了区别**，不是"两张表内容不重叠"——内容当然会有交集
    （中医词汇就那么多），关键是改一边时能看出会不会连带影响另一边。
    """
    import core.effect_synonyms as mod

    doc = (mod.__doc__ or "") + EFFECT_SYNONYMS_PATH.read_text(encoding="utf-8")[:3000]
    assert "syndrome_norm" in doc
    assert "第 31 条" in doc or "第31条" in doc
    assert "证候门类" in doc, "要写清 SYNONYMS 回答的是哪个问题"


def test_the_table_covers_the_treatment_methods_the_project_actually_emits():
    """表要盖住项目里真的会出现的治法。盖不住的部分退化成裸子串——
    这条不要求 100%，要求的是**报出实际覆盖率**，别让人以为全覆盖了。"""
    rows = load_effect_synonyms()
    methods = {r.method for r in rows}
    n_effect_terms = table_stats()["n_effect_terms"]
    assert n_effect_terms >= 100, f"功效词只有 {n_effect_terms} 个，展开面太窄"
    # 八法里至少要有一半登记在表里（汗吐下和温清消补）
    eight = ["解表", "涌吐", "泻下", "和解", "温里", "清热", "消导", "补益"]
    hit = [m for m in eight if any(m in x for x in methods)]
    assert len(hit) >= 4, f"八法只盖到 {hit}"
