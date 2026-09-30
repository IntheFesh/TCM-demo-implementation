"""证型标题的几种写法统一在 syndrome_heading 一处判定。

扫描件把同一件事写成了几种形状：

    `# 1.胃中寒冷`     带 `#` 的编号项
    `（3）肝火犯肺`     小项
    `# （4）肺阴亏虚`   带 `#` 的小项
    `1）痰热腑实`       丢了左括号的小项
    `7.痰火扰心`       丢了行首 `#` 的编号项（**靠启发式判据**）

最后一种认不出来，下一块临床表现就会沿用上一个证型的名字（"证型名被上一条
复用"）。而放宽 `#` 这个锚点要面对大量不带 `#` 的编号行，其中绝大多数是正文
段落，所以这一种形状另设启发式判据（长度 + 无句读）。

这个文件钉的是那条启发式判据。它是从原文量出来的，不是猜的，所以判据也按
量出来的形状写。
"""
from __future__ import annotations

import pytest

from offline.build_syndrome_textbook import (
    HEADING_BARE_NUMBERED,
    HEADING_HASH_NUMBERED,
    HEADING_PAREN,
    LAYOUTS,
    _HEADING_MAX_LEN,
    parse_textbook,
    syndrome_heading,
)


def _write(tmp_path, text: str):
    p = tmp_path / "t.md"
    p.write_text(text, encoding="utf-8")
    return p


# ---------- 每种写法各自认得出来 ----------

@pytest.mark.parametrize("line,name,kind", [
    ("# 1.胃中寒冷", "胃中寒冷", HEADING_HASH_NUMBERED),
    ("# 2、胃火上逆", "胃火上逆", HEADING_HASH_NUMBERED),
    ("（3）肝火犯肺", "肝火犯肺", HEADING_PAREN),
    ("# （4）肺阴亏虚", "肺阴亏虚", HEADING_PAREN),
    ("1）痰热腑实", "痰热腑实", HEADING_PAREN),
    ("7.痰火扰心", "痰火扰心", HEADING_BARE_NUMBERED),
])
def test_every_heading_form_is_recognised(line, name, kind):
    assert syndrome_heading(line) == (name, kind)


def test_the_paren_forms_need_no_heuristic():
    """`# （4）…` 和 `1）…` 这两种括号形状无歧义（正文段落不会以编号括号开头），
    **所以它们不需要启发式判据**——只有 bare_numbered 设门槛。
    """
    assert syndrome_heading("# （4）肺阴亏虚")[1] == HEADING_PAREN
    assert syndrome_heading("1）痰热腑实")[1] == HEADING_PAREN


# ---------- 启发式判据只管"不带 # 的编号项" ----------

# 原文里真实的正文段落开头（几百字一行）。判据必须把它们全挡掉。
_REAL_BODY_LINES = [
    "1.辨咳嗽由于邪阻于肺，肺失宣肃，肺气上逆而作。据其病程的久暂，可分为暴咳与久咳两类。",
    "2.辨喘以呼吸喘促，甚则张口抬肩为特征。主要病机为肺气升降出入失常。",
    "3.辨痰此痰指有形之痰液。由于肺气失于敷布，津液停聚而成。",
    "5.注意心系病的危重证候。心阳虚或阴伤及阳者，可导致心阳浮越。",
]


@pytest.mark.parametrize("line", _REAL_BODY_LINES)
def test_body_paragraphs_are_not_mistaken_for_headings(line):
    """**主力判据是句读**：正文段落必然含句读，而带 `#` 的真标题里含句读的
    只在前言，证型标题一个都没有。"""
    assert syndrome_heading(line) is None


def test_a_long_bare_numbered_line_without_punctuation_is_still_rejected():
    """长度是兜底判据：真标题绝大多数不超过 5 字，取 ≤_HEADING_MAX_LEN。"""
    assert syndrome_heading("9." + "肝" * (_HEADING_MAX_LEN + 1)) is None
    assert syndrome_heading("9." + "肝" * _HEADING_MAX_LEN) is not None


def test_the_thresholds_do_not_apply_to_the_unambiguous_forms():
    """带 `#`、带括号的三种形状**不设门槛**——给它们也加长度限制会把前言里
    那几条合法的长标题（「加强数字化建设，丰富拓展教材内容」16 字带逗号）误杀。"""
    long_with_punct = "加强数字化建设，丰富拓展教材内容"
    assert syndrome_heading(f"# 3.{long_with_punct}") == (long_with_punct, HEADING_HASH_NUMBERED)
    assert syndrome_heading(f"（3）{long_with_punct}") == (long_with_punct, HEADING_PAREN)
    # 同一段文字**不带 `#`** 时必须被挡掉
    assert syndrome_heading(f"3.{long_with_punct}") is None


# ---------- 不许把病名标题吃掉 ----------

@pytest.mark.parametrize("line", [
    "# 第五节 逆", "# 第一节 胃痛", "# 第三节   闭",
    "临床表现：胃痛隐隐，大便溏薄。", "证机概要：脾胃虚寒。", "常用药：白术、干姜。", "",
])
def test_non_heading_lines_stay_non_headings(line):
    """病名标题（`# 第N节 XXX`）和三个字段标签都不许被当成证型标题——
    `_DISEASE_RE` 在主循环里排在前面，但这个函数自己也不该认它们。"""
    assert syndrome_heading(line) is None


# ---------- 嵌进标题里的小项 ----------

@pytest.mark.parametrize("line,expected", [
    ("2.缓解期（1）肺虚", "肺虚"),
    ("2.虚瘩（1）脾胃虚弱", "脾胃虚弱"),
    ("4.狂证（1）痰火扰神", "痰火扰神"),
    ("2.变证（1）黄痘", "黄痘"),
])
def test_an_embedded_subitem_wins_over_the_stage_label(line, expected):
    """`2.缓解期（1）肺虚`：`缓解期` 是分期小标题、`（1）肺虚` 才是证型。
    取最后一个 `（N）` 之后的部分。这种嵌入形式只出现在"不带 `#` 的编号项"这一类里。"""
    assert syndrome_heading(line)[0] == expected


def test_a_heading_without_an_embedded_subitem_is_untouched():
    """对不含嵌入形式的标题，这一步是恒等变换——所以可以对所有形状统一施加，
    不必分三种情况写三遍。"""
    for line in ("# 1.胃中寒冷", "（3）肝火犯肺", "7.痰火扰心"):
        assert "（" not in syndrome_heading(line)[0]


# ---------- 接进 parse_textbook ----------

def test_a_bare_numbered_heading_gives_the_block_its_own_name(tmp_path):
    """第二个块的标题丢了行首 `#`，认不出来的话它会沿用第一个块的名字。"""
    text = (
        "# 第一节 心悸\n\n# 6.瘀阻心脉\n\n"
        "临床表现：心悸不安，胸闷不舒，舌质紫暗，脉涩。\n"
        "证机概要：血瘀气滞，心脉瘀阻。\n"
        "常用药：桃仁、红花。\n\n"
        "7.痰火扰心\n\n"
        "临床表现：心悸时发时止，胸闷烦躁，舌红，苔黄腻，脉弦滑。\n"
        "证机概要：痰浊停聚，郁久化火，痰火扰心。\n"
        "常用药：黄连、半夏。\n"
    )
    entries, stats = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert [e.name for e in entries] == ["瘀阻心脉证", "痰火扰心证"]
    assert stats["headings_bare_numbered"] == 1


def test_the_heuristic_count_is_reported(tmp_path):
    """靠启发式认下来的条数**要报出来**：判据是长度 + 无句读，条数一变就说明
    原文排版跟校准判据时用的那份不一样了，那时该重新逐行核，不是调阈值。"""
    text = (
        "# 第一节 胃痛\n\n# 1.肝气犯胃\n\n"
        "临床表现：胃脘胀痛，舌淡红，脉弦。\n证机概要：肝气犯胃。\n常用药：柴胡。\n"
    )
    _, stats = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert stats["headings_bare_numbered"] == 0
    assert "headings_bare_numbered" in stats


def test_a_non_syndrome_line_that_passes_the_heuristic_produces_no_entry(tmp_path):
    """原文里的 `3.传统验痰法诊断法`（肺痈的诊断小节）过了判据，但它**不是证型**。
    为什么无害：它后面没有紧跟「临床表现：」块——诊断小节后面是「验痰法：…」。
    所以它只是把 `current_syndrome_name` 设成一个不会被用到的值。

    **这条判据是"允许一个已知的假阳性"的依据**，不是"判据没有假阳性"。
    """
    text = (
        "# 第一节 肺痈\n\n# 1.初期\n\n"
        "临床表现：恶寒发热，咳嗽胸痛，舌苔薄黄，脉浮数。\n"
        "证机概要：风热外袭，内舍于肺。\n常用药：银花、连翘。\n\n"
        "3.传统验痰法诊断法\n\n"
        "验痰法：脓血浊痰吐入水中，沉者是脓，浮者是痰。\n\n"
        "# 2.成痈期\n\n"
        "临床表现：身热转甚，咳嗽气急，舌红苔黄腻，脉滑数。\n"
        "证机概要：热毒蕴肺，蒸液成痰。\n常用药：苇茎、桃仁。\n"
    )
    entries, _ = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert [e.name for e in entries] == ["初期证", "成痈期证"], \
        "那行诊断小节被当成证型名用掉了"


def test_parse_textbook_has_no_second_heading_matcher():
    """**全模块唯一的"这是不是证型标题"判定**（docs/ARCHITECTURE.md §4）：
    主循环只调用 syndrome_heading，不摊开几个各写一套判据的分支。"""
    src = open("offline/build_syndrome_textbook.py", encoding="utf-8").read()
    body = src[src.index("def parse_textbook("):src.index("\ndef load_committed_textbook_entries")]
    assert body.count("syndrome_heading(") == 1
    # 下面三个名字是"各写一套判据"的写法（单独的标题正则、小项正则、bare 分支），
    # 不许以定义或调用的形式出现。只查定义和调用，不查注释里的提及。
    for gone in ("_SYN_HEADING_RE", "_SYN_SUBITEM_RE", "bare_heading_name"):
        assert f"{gone} =" not in src, f"{gone} 的定义还在"
        assert f"{gone}.match" not in src, f"{gone} 还在被调用"
        assert f"{gone}(" not in body, f"{gone} 还在主循环里被调用"


def test_all_heading_forms_parse_in_one_document(tmp_path):
    """几种写法混在一份文件里各自成块——这是"合成一处判定没有互相吃掉"的判据。"""
    text = (
        "# 第一节 咳嗽\n\n"
        "# 1.风寒袭肺\n\n临床表现：咳声重浊，舌苔薄白，脉浮。\n"
        "证机概要：风寒袭肺，肺气失宣。\n常用药：麻黄。\n\n"
        "（2）痰热郁肺\n\n临床表现：咳嗽气粗，痰多黄稠，舌红苔黄，脉滑数。\n"
        "证机概要：痰热壅肺，肺失肃降。\n常用药：黄芩。\n\n"
        "# （3）肺阴亏虚\n\n临床表现：干咳，痰少质黏，舌红少苔，脉细数。\n"
        "证机概要：肺阴亏虚，虚热内灼。\n常用药：沙参。\n\n"
        "4）肝火犯肺\n\n临床表现：上气咳逆阵作，咽干口苦，舌红，苔薄黄。\n"
        "证机概要：肝郁化火，上逆侮肺。\n常用药：桑白皮。\n\n"
        "5.肺气亏虚\n\n临床表现：咳而气短，痰白清稀，舌淡，脉弱。\n"
        "证机概要：肺气亏虚，气不化津。\n常用药：党参。\n"
    )
    entries, stats = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert [e.name for e in entries] == [
        "风寒袭肺证", "痰热郁肺证", "肺阴亏虚证", "肝火犯肺证", "肺气亏虚证",
    ]
    assert stats["headings_bare_numbered"] == 1, "只有最后那个 `5.` 靠启发式"
