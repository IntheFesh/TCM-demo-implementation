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
    ("黑色大便", "黑便"), ("血便", "血便"), ("腹痛剧烈", "剧烈腹痛"),
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
