"""R18-C：王云启医案的切案规则（`offline/extract_cases_wang_yunqi.py`）。

## 为什么先探针再定规则

李可那份的规则（编号标题分案）照抄到这份上**一条都切不出来**：这份的各论是
「第 N 章 → 一、二、三（病种）→ 若干病案」三级结构，病案本身没有编号、
没有标题。

`--probe` 的实测输出（3197 行的 .txt）：

    第 N 章 33 处 ｜ 一、二、三 65 处 ｜ 病人行 77 处 ｜ 「病案分析」7 处

**「病案分析」只有 7 处**——拿它当分隔符会把 77 个病案切成 7 块。
病人行才是分案点。这个结论是数出来的，不是看出来的。
"""
from offline.extract_cases_wang_yunqi import (
    _PATIENT_LINE_RE, probe, split_cases, summarize,
)

CORPUS = """第三章 消化系统肿瘤

一、胃癌

胃癌是消化道最常见的恶性肿瘤之一。

余××,男，60岁。因反复胃脘胀痛半年就诊。辨证为脾胃虚寒，痰瘀互结。

处方：黄芪 30g, 白术 15g, 陈皮 10g, 半夏 9g。

二诊：胀痛减轻，纳食渐增。上方加砂仁 6g。

病案分析：胃癌属中医"胃脘痛""积聚"范畴。

尹某，男，43岁。确诊肝癌一年，胁下痞块。

处方：柴胡 10g, 鳖甲 30g, 莪术 15g。

二、肝癌

肝癌的中医治疗以扶正祛邪为主。

张××,女，37岁。乳腺癌术后，乏力纳差。

处方：党参 15g, 茯苓 15g。
"""


def test_the_probe_counts_the_structure_instead_of_guessing_it():
    info = probe(CORPUS)
    assert info["n_patient_lines"] == 3
    assert info["n_analysis_marks"] == 1
    assert info["n_chapters"] == 1 and info["n_sections"] == 2
    assert info["sample_patients"][0] == "余××,男，60岁"


def test_the_analysis_marker_would_have_been_the_wrong_delimiter():
    """这条把"为什么不用它"钉成一个数：病人行 3 个、「病案分析」1 个。
    用后者当分隔符会把 3 个病案切成 1 块。"""
    info = probe(CORPUS)
    assert info["n_analysis_marks"] < info["n_patient_lines"]


def test_the_patient_line_regex_takes_the_real_shapes():
    """姓名用 `×` 脱敏、逗号半角全角混用、年龄前后可能有空格——三样都要认。
    实测这份语料里三种写法都出现了。"""
    for raw in ("余××,男，60岁", "李×,男，57岁", "尹某，男，43岁", "谢××,女，49岁"):
        assert _PATIENT_LINE_RE.search(raw), raw
    # 不该命中的：综述里提到的年龄段
    assert not _PATIENT_LINE_RE.search("中晚期患者五年生存率为 34%～60%")


def test_cases_split_at_the_patient_lines():
    cases = split_cases(CORPUS)
    assert len(cases) == 3
    assert cases[0]["title"] == "余××,男，60岁"
    assert cases[0]["sex"] == "男" and cases[0]["age"] == 60
    assert "黄芪" in cases[0]["raw_excerpt"]
    assert "尹某" not in cases[0]["raw_excerpt"], "两案串了"


def test_a_new_section_ends_the_previous_case():
    """病种讲完了、后面是下一个病种的综述，不属于上一案。不截的话第 2 案会
    把「二、肝癌」整段综述吞进去——那段文字里没有病人，但有病名和治法，
    抽取那一步会把它当成这个病人的内容。"""
    cases = split_cases(CORPUS)
    assert "肝癌的中医治疗以扶正祛邪为主" not in cases[1]["raw_excerpt"]


def test_explicit_return_visits_are_counted_not_guessed():
    """跟李可那份不同，这份的复诊是**显式**的（「二诊」「三诊」）。数得出来就数，
    数不出来仍然是 1（只有初诊），不猜。"""
    cases = split_cases(CORPUS)
    assert cases[0]["n_visits_in_text"] == 2   # 初诊 + 二诊
    assert cases[2]["n_visits_in_text"] == 1


def test_every_case_carries_the_sequence_fields():
    for c in split_cases(CORPUS):
        assert c["case_group_id"] == c["case_id"]
        assert c["prev_case_id"] is None
        assert c["visit_index"] == 0


def test_scope_and_incompatible_reuse_the_li_ke_implementation():
    """`classify_scope` / `_herb_words` 从李可那个模块 import，不复制一份——
    "这批医案是不是肿瘤案"这个判断已经有实现了（CLAUDE.md 第 31 条）。"""
    import offline.extract_cases_wang_yunqi as wy
    import offline.extract_cases_li_ke as lk
    assert wy.classify_scope is lk.classify_scope
    assert wy._herb_words is lk._herb_words
    cases = split_cases(CORPUS)
    assert all(c["scope"] == "oncology" for c in cases)


def test_summary_counts_add_up():
    stats = summarize(split_cases(CORPUS))
    assert stats["n_cases"] == 3
    assert stats["n_with_analysis"] == 1
    assert stats["n_multi_visit"] == 1
    assert sum(stats["by_scope"].values()) == 3


def test_a_corpus_with_no_patient_lines_yields_nothing():
    assert split_cases("第一章 综述\n\n这一章全是综述，没有病案。") == []
