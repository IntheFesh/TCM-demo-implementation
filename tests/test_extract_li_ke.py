"""R18-B：李可医案的切案规则（`offline/extract_cases_li_ke.py`）。

切案是**纯本地零 LLM** 的一步，所以能在合成文本上测透。抽字段那一步仍然走
`extract_cases.py` 的同一套 schema，不在这里重测。

判据都在合成文本上，不依赖那份版权受限的语料——它不随仓库分发，
测试不能要求它存在（`tests/` 必须"不需要网络、秒级跑完"）。
"""
import pytest

from offline.extract_cases_li_ke import (
    _is_real_heading, classify_scope, looks_like_a_case, split_cases, summarize,
)

CASE = """1.1 脑瘤头痛

张某，女，25 岁。 脑瘤术后复发，头痛如破，呕涎沫而肢厥。予改良乌头汤加味。

生黄芪 120g, 当归、附子、川乌各 30g, 麻黄 15g, 细辛 20g, 炙甘草 60g。

服药 75 日赴京复查，病灶消失。

1.2 鼻硬结症

蔡某，49 岁。 鼻尖部长一小红疹，后渐长至黄豆大即化脓。证属肝气郁积化火。

柴胡 10g, 赤芍、当归各 30g, 海藻 30g, 甘草 10g, 夏枯草 120g。

上方连服 15 剂，角状物脱落而愈。

3.1 治肿瘤思想

治癌要过四道关，整体失调四大证。这是论述，不是医案。
"""


def test_numbered_headings_split_the_text_into_cases():
    cases = split_cases(CASE)
    assert [c["case_no"] for c in cases] == ["1.1", "1.2"]
    assert cases[0]["title"] == "脑瘤头痛"
    assert "张某" in cases[0]["raw_excerpt"]
    assert "蔡某" not in cases[0]["raw_excerpt"], "两案串了"


def test_a_section_with_no_prescription_is_not_a_case():
    """论述小节（`3.1 治肿瘤思想`）没有方、没有病人。**判据是内容不是编号层数**
    ——三级编号在这份语料里是论述，换一本书可能就是医案编号。"""
    assert not looks_like_a_case("治癌要过四道关，整体失调四大证。这是论述。")
    assert looks_like_a_case("张某，女，25 岁。生黄芪 120g, 当归 30g。")


def test_a_long_case_without_a_patient_marker_still_counts():
    """实测 `1.8 食管癌` 写的是"李可老母，年六旬"——既没有"某"也没有"岁"。
    硬要求病人标记会把真医案丢掉，所以病人标记跟正文长度是**或**的关系。"""
    body = "李可老母，年六旬。" + "食管梗阻已久，水饮不能下咽。" * 16 + "生黄芪 120g。"
    assert len(body) >= 200
    assert looks_like_a_case(body)


def test_a_prescription_continuation_line_is_not_a_heading():
    """正文里"2.20 头三七 200g，血琥珀、高丽参各 100g…"这种**药方续行**恰好
    也以 `数字.数字 ` 开头，会被标题正则吃掉、把一个案子劈成两半。实测劈坏 3 处。
    判据：标题短且不含剂量。"""
    assert _is_real_heading("肺癌 2")
    assert not _is_real_heading("头三七 200g，血琥珀、高丽参、胎盘、鹿茸各 100g。")
    assert not _is_real_heading("这是一个非常非常非常非常长的标题超过二十个字了吧")


def test_the_table_of_contents_at_the_top_is_dropped():
    """目录页跟正文标题长得一样，只是一行里塞了十几个。只丢**开头**连续的
    那一段——正文里也可能有一行恰好像目录，从中间丢会把正文挖掉一块。"""
    toc = "目录：1.1 脑瘤头痛...1 1.2 鼻硬结症..3 1.3 皮肌炎...5\n\n" + CASE
    assert [c["case_no"] for c in split_cases(toc)] == ["1.1", "1.2"]


def test_oncology_wins_over_spleen_stomach():
    """一个案子既提到胃又提到癌（"胃小弯癌"），它首先是肿瘤案——倒过来判会把
    整本肿瘤案里带"胃"字的统统标成脾胃门，而 demo 的定位是脾胃门，
    那批案子会以"在范围内"的身份混进主路径。"""
    assert classify_scope("胃小弯癌，胃脘胀痛") == "oncology"
    assert classify_scope("胃脘胀痛，脾虚湿困") == "spleen_stomach"
    assert classify_scope("头痛如破，肢厥") == "other"


def test_incompatible_pairs_are_named_not_just_flagged():
    """`has_incompatible_pair` 那个布尔回答不了"是哪几对"。而**排不排除是
    下游的事**——那 75 段海藻甘草同用是李可的用药特征，不是数据错误。"""
    cases = split_cases(CASE)
    by_no = {c["case_no"]: c for c in cases}
    assert by_no["1.2"]["has_incompatible_pair"] is True
    assert by_no["1.2"]["incompatible_pairs"] == ["海藻-甘草"]
    assert by_no["1.1"]["incompatible_pairs"] == []


def test_every_case_keeps_the_sequence_fields_even_at_one_visit():
    """`case_group_id` / `visit_index` / `prev_case_id` 是 `feat/divergence-v1`
    定的数据形态（CLAUDE.md「数据形态变了」）。一案一诊也要带齐——下游
    （检索、SFT 导出、图构造）读的是这三个字段，缺了就要各自兜一次 None。"""
    for c in split_cases(CASE):
        assert c["visit_index"] == 0
        assert c["case_group_id"] == c["case_id"]
        assert c["prev_case_id"] is None


def test_dropped_sections_are_reported_not_silently_discarded():
    """"切出 N 案"这个数单独看永远是对的。规则切错了只有把被丢掉的东西
    摊开才看得见。"""
    split_cases(CASE)
    stats = summarize(split_cases(CASE))
    assert stats["dropped"], "论述小节被静默丢弃了"
    assert stats["dropped"][0]["case_no"] == "3.1"
    assert stats["dropped"][0]["n_chars"] > 0


def test_summary_counts_add_up():
    cases = split_cases(CASE)
    stats = summarize(cases)
    assert stats["n_cases"] == 2
    assert sum(stats["by_scope"].values()) == 2
    assert stats["n_incompatible"] == 1


@pytest.mark.parametrize("text", ["", "没有任何编号标题的一段话。", "1.1 只有标题没有正文"])
def test_degenerate_inputs_return_nothing_instead_of_raising(text):
    assert split_cases(text) == []
