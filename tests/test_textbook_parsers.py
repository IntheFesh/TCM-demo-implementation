"""R18-E：五本专科教材的排版解析 + OCR 修正表。

**这些测试用的是合成 fixture，不是真实教材原文。** 五本专科教材的 markdown
不在仓库里（`books/` 只有神农本草经/本草备要/两本医案/衷中参西录和四本中药方剂
教材），沙盒里也 clone 不到 TCM_Datasets（网络策略拦掉了）。所以：
  - 排版差异、OCR 修正、标签探测这三件事**在这里测得是真的**——它们只依赖
    解析代码本身；
  - 「337 条扩到 ≈1444 条」这个规模数**没有在这里测**，也不该假装测了。
    上机命令见 R18 报告的「无法完成项」一节。
"""
from __future__ import annotations

import pytest

from offline.build_syndrome_textbook import (
    LAYOUTS,
    OCR_FIXES_PATH,
    TextbookLayout,
    _label_re,
    apply_ocr_fixes,
    detect_layout,
    load_ocr_fixes,
    parse_textbook,
)

# 六本教材的块结构一样（病名标题 → 证型标题 → 两个字段 → 收尾标签），
# 只有标签用词不同。fixture 按这个形状生成，不是六份手抄。
_BLOCK = """# 第一节 {disease}

# 1.{syndrome}

{clinical}：胃脘胀痛，食后加重，嗳气泛酸，舌淡红，苔薄白，脉弦。
{pathogenesis}：肝气犯胃，胃失和降，气机阻滞。
{end}：柴胡、白芍、枳壳、甘草。
"""


def _fixture(layout: TextbookLayout, disease: str = "胃痛", syndrome: str = "肝气犯胃") -> str:
    return _BLOCK.format(
        disease=disease, syndrome=syndrome,
        clinical=layout.clinical_labels[0],
        pathogenesis=layout.pathogenesis_labels[0],
        end=layout.end_labels[0],
    )


def _write(tmp_path, text: str):
    p = tmp_path / "t.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---------- 六种排版都能解析 ----------

@pytest.mark.parametrize("key", sorted(LAYOUTS))
def test_every_layout_parses_its_own_field_labels(tmp_path, key):
    """每本教材用自己的标签都能抽出条目。这是「五个解析器一个都不能少」的判据。"""
    lay = LAYOUTS[key]
    entries, stats = parse_textbook(_write(tmp_path, _fixture(lay)), lay, ocr_fixes=[])
    assert stats["clinical_blocks_seen"] == 1, f"{key} 没认出临床表现类标签"
    assert len(entries) == 1, f"{key} 抽出 {len(entries)} 条，期望 1 条"
    e = entries[0]
    assert e.disease == "胃痛"
    assert e.name == "肝气犯胃证"          # 原文没带「证」字，补上
    assert e.definition.startswith("肝气犯胃")
    assert e.source == "textbook"


@pytest.mark.parametrize("key", sorted(LAYOUTS))
def test_code_prefix_is_per_textbook(tmp_path, key):
    """六本教材的 code 前缀必须互不相同，否则两本教材的条目 code 会撞车。

    内科仍是 TB：已经落盘的 378 条用的就是 TB-xxx，改前缀会让重跑结果跟
    版本控制里的 syndromes.jsonl 对不上。
    """
    lay = LAYOUTS[key]
    entries, _ = parse_textbook(_write(tmp_path, _fixture(lay)), lay, ocr_fixes=[])
    assert entries[0].code.startswith(lay.code_prefix + "-")
    assert LAYOUTS["neike"].code_prefix == "TB"


def test_code_prefixes_are_unique():
    prefixes = [lay.code_prefix for lay in LAYOUTS.values()]
    assert len(set(prefixes)) == len(prefixes), f"前缀撞车：{prefixes}"


def test_wrong_layout_yields_nothing_rather_than_garbage(tmp_path):
    """用妇科排版去解析推拿原文：抽出 0 条，而不是抽出错的条目。

    推拿教材没有方药、以「手法：」收尾；妇科的 end_labels 里没有「手法」，
    块会一直吞下去——所以这一条同时钉住「传错 --layout 的后果是空而不是脏」。
    """
    text = _fixture(LAYOUTS["tuina"])
    entries, stats = parse_textbook(_write(tmp_path, text), LAYOUTS["fuke"], ocr_fixes=[])
    assert entries == []
    assert stats["clinical_blocks_seen"] == 0


# ---------- 标签元组 ----------

def test_label_re_does_not_let_a_short_label_eat_a_longer_one():
    """「证候」和「证候分析」都在表里时，`^证候[：:]` 不能吃掉「证候分析：」。

    判据是标签后面**紧跟冒号**——不要求这一点的话外科的临床表现标签
    「证候」会把病机标签「证候分析」一起匹配掉，两个字段读到同一行。
    """
    cre = _label_re(("证候", "临床表现"))
    assert cre.match("证候：胃脘胀痛")
    assert cre.match("证候分析：肝气犯胃") is None


def test_layout_accepts_several_labels_for_the_same_field(tmp_path):
    """同一本教材里两种写法都出现过，写死一个会让另一种全部落空。"""
    lay = LAYOUTS["waike"]
    assert "证候" in lay.clinical_labels and "临床表现" in lay.clinical_labels
    alt = _BLOCK.format(disease="乳痈", syndrome="气滞热壅",
                        clinical="临床表现", pathogenesis="辨证分析", end="方药")
    entries, _ = parse_textbook(_write(tmp_path, alt), lay, ocr_fixes=[])
    assert len(entries) == 1 and entries[0].disease == "乳痈"


def test_tuina_layout_ends_a_block_on_shoufa(tmp_path):
    """推拿教材没有方药。漏了「手法：」这个收尾标签，一个块会一直吞到下一个
    「临床表现：」，症状里混进上一节的手法描述。"""
    lay = LAYOUTS["tuina"]
    assert "手法" in lay.end_labels
    text = (
        "# 第一节 落枕\n\n# 1.风寒外袭\n\n"
        "临床表现：颈项僵痛，转侧不利，舌淡，苔薄白，脉浮紧。\n"
        "证候分析：风寒袭表，经络气滞。\n"
        "手法：一指禅推法、拿法、颈项部拔伸法。\n\n"
        "# 2.气滞血瘀\n\n"
        "临床表现：颈项刺痛，痛处固定，舌暗，脉弦。\n"
        "证候分析：气滞血瘀，经络不通。\n"
        "手法：滚法、弹拨法。\n"
    )
    entries, stats = parse_textbook(_write(tmp_path, text), lay, ocr_fixes=[])
    assert stats["clinical_blocks_seen"] == 2
    assert len(entries) == 2
    # 第一条的症状里不能出现第二条的手法词
    assert not any("滚法" in s for s in entries[0].cardinal_symptoms)


# ---------- OCR 修正表 ----------

def test_ocr_fixes_file_is_in_version_control():
    assert OCR_FIXES_PATH.exists(), "ocr_fixes.tsv 要在 data/standard/ 下进版本控制"


def test_ocr_fixes_load_sorted_longest_first():
    """表里同时有「大便唐薄」和「便唐」，先替换短的会让长的永远匹配不到。"""
    pairs = load_ocr_fixes()
    lengths = [len(w) for w, _ in pairs]
    assert lengths == sorted(lengths, reverse=True)
    assert ("大便唐薄", "大便溏薄") in pairs
    assert ("便唐", "便溏") in pairs


def test_ocr_fixes_cover_the_four_known_error_shapes():
    """四类实测错法：溏→唐、蒌→萎、白→自、掉字（香薷饮）。"""
    got = dict(load_ocr_fixes())
    assert got["便唐"] == "便溏"
    assert got["瓜萎"] == "瓜蒌"
    assert got["自术"] == "白术"
    assert got["新加香饮"] == "新加香薷饮"


def test_apply_ocr_fixes_is_whole_word_not_per_character():
    """只写「唐→溏」会把「唐代」一起改掉，所以左列一律带上下文。"""
    assert apply_ocr_fixes("大便唐薄，纳差") == "大便溏薄，纳差"
    assert "唐" in apply_ocr_fixes("唐代医家孙思邈")   # 「唐代」不该被动


def test_apply_ocr_fixes_prefers_the_longer_rule():
    assert apply_ocr_fixes("大便唐薄") == "大便溏薄"   # 不是「大便溏薄」被切成「大溏薄」


def test_load_ocr_fixes_rejects_identity_rows(tmp_path):
    """恒等项在表里只可能是手误，静默忽略会让人以为它生效了。"""
    p = tmp_path / "f.tsv"
    p.write_text("错\t对\t说明\n脉弦\t脉弦\t手误\n", encoding="utf-8")
    with pytest.raises(ValueError, match="恒等项"):
        load_ocr_fixes(p)


def test_load_ocr_fixes_rejects_single_column_rows(tmp_path):
    p = tmp_path / "f.tsv"
    p.write_text("错\t对\t说明\n只有一列\n", encoding="utf-8")
    with pytest.raises(ValueError, match="两列"):
        load_ocr_fixes(p)


def test_missing_ocr_fixes_file_raises_rather_than_silently_skipping(tmp_path):
    """修正表在版本控制里，缺失说明工作树不完整，不是可以跳过的一步。"""
    with pytest.raises(FileNotFoundError):
        load_ocr_fixes(tmp_path / "nope.tsv")


def test_ocr_fixes_run_before_splitting(tmp_path):
    """错字落在症状里会让症状节点 id 跟图谱对不上——切完再修就晚了。"""
    text = (
        "# 第一节 胃痛\n\n# 1.脾胃虚寒\n\n"
        "临床表现：胃痛隐隐，大便唐薄，舌淡，苔薄自，脉细。\n"
        "证机概要：脾胃虚寒，中阳不振。\n"
        "常用药：自术、干姜。\n"
    )
    entries, _ = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert "大便溏薄" in entries[0].cardinal_symptoms
    assert entries[0].tongue_pulse is not None and "苔薄白" in entries[0].tongue_pulse


# ---------- 标签探测 ----------

def test_detect_layout_points_at_the_right_textbook(tmp_path):
    """抽出 0 条时先跑 --detect：它区分「标签全不匹配」和「--layout 传错了」。"""
    counts = detect_layout(_fixture(LAYOUTS["fuke"]))
    # 妇科的两个标签都命中
    assert counts["fuke"] == {"clinical": 1, "pathogenesis": 1}
    # 内科的标签一个都不命中（妇科用「主要证候/证候分析」）
    assert counts["neike"] == {"clinical": 0, "pathogenesis": 0}


def test_detect_layout_all_zero_means_unknown_layout():
    counts = detect_layout("# 第一节 胃痛\n这一段什么标签都没有。\n")
    assert all(sum(c.values()) == 0 for c in counts.values())


# ---------- 既有内科行为不变 ----------

def test_neike_default_layout_is_backward_compatible(tmp_path):
    """`parse_textbook(path)` 不传 layout 时仍然是内科那一套——378 条就是这么抽的。"""
    text = _fixture(LAYOUTS["neike"])
    entries, stats = parse_textbook(_write(tmp_path, text))
    assert stats["layout"] == "neike"
    assert entries[0].code.startswith("TB-")


def test_secondary_symptoms_stay_empty_for_every_layout(tmp_path):
    """教材原文是扁平症状表，没标主次。规则脚本区分不出来，宁可留空也不编。"""
    for lay in LAYOUTS.values():
        entries, _ = parse_textbook(_write(tmp_path, _fixture(lay)), lay, ocr_fixes=[])
        assert entries[0].secondary_symptoms == []


def test_elements_come_from_core_elements_not_a_new_word_list(tmp_path):
    """证素匹配走 core.elements 的 LOCATIONS/NATURES（CLAUDE.md 第 31 条）。"""
    from core.elements import LOCATIONS, NATURES
    entries, _ = parse_textbook(_write(tmp_path, _fixture(LAYOUTS["neike"])),
                                LAYOUTS["neike"], ocr_fixes=[])
    e = entries[0]
    assert set(e.location) <= set(LOCATIONS)
    assert set(e.nature) <= set(NATURES)
    assert "胃" in e.location and "肝" in e.location   # 证机概要写的是「肝气犯胃」
