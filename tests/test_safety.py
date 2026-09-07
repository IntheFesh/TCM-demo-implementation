import pytest
"""core/safety.py 的离线测试：危重症状拦截，纯关键词匹配，不需要网络。"""
from core.safety import check_safety


def test_check_safety_returns_none_when_no_danger_keyword():
    assert check_safety(["纳差", "乏力", "腹胀"]) is None


def test_check_safety_detects_melena_variant_from_query_10():
    """tests/queries.txt 第 10 条设计给安全层的测试主诉：
    "近日解黑色柏油样便"——S1 标准化后至少应保留"黑便"或"柏油样便"这类症状词。"""
    reason = check_safety(["胃脘疼痛", "解黑色柏油样便", "头晕心慌"])
    assert reason is not None
    assert "柏油样便" in reason


def test_check_safety_detects_hematemesis():
    reason = check_safety(["呕血", "面色苍白"])
    assert reason is not None
    assert "呕血" in reason


def test_check_safety_detects_coma_and_shock():
    assert check_safety(["昏迷"]) is not None
    assert check_safety(["休克"]) is not None


def test_check_safety_matches_as_substring_not_exact_equality():
    # S1 标准化后的症状词通常带修饰语，不会是关键词本身的精确字符串
    reason = check_safety(["解黑色柏油样便三日"])
    assert reason is not None


def test_check_safety_dedupes_matched_keywords_in_reason():
    reason = check_safety(["呕血不止", "呕血量大"])
    assert reason.count("呕血") == 1


def test_check_safety_reason_is_human_readable_not_empty():
    reason = check_safety(["便血"])
    assert isinstance(reason, str)
    assert len(reason) > 0


# ---------- 审查修复：误报边界与否定 ----------

@pytest.mark.parametrize("text", [
    "咳嗽乏力气血不足",   # 「咳…气血」不是咳血
    "小便黄血压偏高",     # 「小便…血压」不是便血
    "顺便说一下血压高",   # 「顺便」不是大便
    "血虚萎黄",
    "无黑便", "否认呕血", "没有便血", "大便不黑",   # 阴性陈述
])
def test_no_false_alarm_on_common_benign_phrasings(text):
    assert check_safety([text]) is None


@pytest.mark.parametrize("text,label", [
    ("黑色大便", "黑便"), ("血便", "便血"), ("腹痛剧烈", "剧烈腹痛"),
    ("肚子疼得受不了", "剧烈腹痛"), ("腹部剧烈疼痛", "剧烈腹痛"),
    ("吐了不少血", "呕血"),   # 「不少」是数量词，不是否定——不能因为堵误报把它漏掉
    ("大便发黑", "黑便"), ("拉了黑便", "黑便"), ("呕出血块", "呕血"),
])
def test_colloquial_danger_phrasings_are_caught(text, label):
    reason = check_safety([text])
    assert reason is not None and label in reason


def test_synonym_labels_are_reported_once():
    """「吐血」命中关键词、又命中呕血的正则，拒绝文案里只能出现一个写法。"""
    reason = check_safety(["吐血不止"])
    assert reason.count("血") == 1


# ---------- 第二轮复核：否定判断按「宾语前缀」而不是「整个间隔」 ----------

@pytest.mark.parametrize("text", [
    # 上一版把 不/未/无/没 整个排除出黑便正则的间隔，把这些教科书式的柏油便描述漏掉了
    "大便不成形发黑", "大便不成形色黑", "大便不干发黑", "大便没成形发黑",
    "大便不畅色黑", "大便不少发黑", "大便不多但发黑",
    # 非重叠扫描 + 命中即 break：被否定的最左匹配会把后面真危重的表述一起吞掉
    "无便血但大便黑",
])
def test_negation_inside_the_gap_does_not_hide_real_melena(text):
    reason = check_safety([text])
    assert reason is not None and "黑便" in reason


@pytest.mark.parametrize("text", [
    "大便不带血", "大便无血", "大便没有血", "排便不带血", "拉出来没有血",
    "腹痛不剧烈", "肚子痛得不厉害", "肚子疼得不严重",
    "意识不模糊", "意识没有模糊", "神志不模糊",
    "没吐过血", "没黑便", "从未便血", "没有吐过血",
])
def test_negated_objects_are_not_alarms(text):
    """追问的阴性回答：parse_answer 判 no，check_safety 也必须放行，
    否则同一句话两处给出相反答案。"""
    assert check_safety([text]) is None


@pytest.mark.parametrize("text", [
    "呕吐血压偏高",          # 关键词裸子串扫描也要守正则那套语境边界
    "拉肚子舌苔黑", "便溏面黑", "排尿黑", "大便正常，面色黑",
    "舌苔黑腻，大便调", "面色黧黑，二便如常",
    "头痛得不行",            # 没有腹/肚/胃/脘 限定，不是本 demo 的危重信号
])
def test_context_bound_false_alarms(text):
    assert check_safety([text]) is None


@pytest.mark.parametrize("text,label", [
    ("剧烈胃痛", "剧烈腹痛"), ("胃痛剧烈", "剧烈腹痛"), ("胃脘剧痛", "剧烈腹痛"),
    ("胃痛难忍", "剧烈腹痛"), ("肚子痛得不行", "剧烈腹痛"), ("胃痛得要命", "剧烈腹痛"),
    ("昏过去", "昏迷"), ("晕倒", "昏迷"), ("晕厥", "昏迷"), ("不省人事", "昏迷"),
    ("咖啡渣样呕吐物", "咖啡渣"), ("大便暗红", "便血"), ("肛门出血", "肛门出血"),
    ("大便，黑色", "黑便"), ("大便 发黑", "黑便"), ("大便稀，颜色黑", "黑便"),
])
def test_coverage_matches_what_the_module_docstring_claims(text, label):
    """模块头注释宣称覆盖消化道出血/意识改变/休克/持续剧痛，实际要对得上。
    这是脾胃门 demo——患者最常写的是「胃痛」不是「腹痛」。"""
    reason = check_safety([text])
    assert reason is not None and label in reason


def test_mentions_danger_ignores_negation_on_purpose():
    """mentions_danger 回答的是「这句话提没提到危重信号」，check_safety 回答的是
    「这句话是不是在陈述危重症状」——两个不同的问题，所以对疑问句/否定句结果相反。
    追问层需要前者：「有没有便血？」这个**问题**是危重相关的。"""
    from core.safety import mentions_danger

    assert mentions_danger("有没有便血？") == "便血"
    assert check_safety(["有没有便血？"]) is None
