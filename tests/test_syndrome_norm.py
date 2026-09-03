"""core/syndrome_norm.py 的离线测试：SYNONYMS 归一化。不需要网络。"""
from core.syndrome_norm import canonical, normalize


def test_canonical_maps_variant_to_canonical_name():
    assert canonical("水肿") == "肿胀"
    assert canonical("胃脘痛") == "胃痛"


def test_canonical_returns_input_unchanged_when_not_in_table():
    assert canonical("不存在的词") == "不存在的词"


def test_normalize_finds_canonical_concept_via_variant_substring():
    hits = normalize("患者水肿明显，按之凹陷。")
    assert "肿胀" in hits


def test_normalize_finds_both_directions_of_mu_cheng_tu():
    assert "木乘土" in normalize("古称木旺乘土证")
    assert "木乘土" in normalize("古称土虚木乘证")


def test_normalize_returns_empty_set_when_nothing_matches():
    assert normalize("一段完全无关的文本") == set()


def test_normalize_can_return_multiple_concepts():
    hits = normalize("大便溏稀，胃脘隐痛，兼见水肿。")
    assert {"泄泻", "胃痛", "肿胀"} <= hits
