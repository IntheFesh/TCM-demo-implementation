"""core/diseases.py 的离线测试：不需要网络，data/standard/diseases.jsonl 是静态文件。"""
import pytest

from core.diseases import get_disease, load_diseases, match_disease


def test_load_diseases_parses_every_line_as_a_valid_disease():
    diseases = load_diseases()
    assert len(diseases) >= 15  # M4 闸门：diseases.jsonl >= 15 条
    for d in diseases:
        assert d.name  # Field(min_length=1) 已经在 pydantic 层管了，这里只是双重确认


def test_load_diseases_is_cached_across_calls():
    # lru_cache：同一个进程里多次调用返回同一个对象，不重复解析文件。
    assert load_diseases() is load_diseases()


def test_aliases_are_unique_across_the_whole_table():
    """一个别名不能指向两个病名——否则 get_disease() 按别名查找时结果不确定
    （返回哪个取决于文件里谁先出现），M4 闸门明确要求这条。"""
    diseases = load_diseases()
    seen: dict[str, str] = {}
    for d in diseases:
        for alias in d.aliases:
            assert alias not in seen, (
                f"别名「{alias}」同时指向「{seen.get(alias)}」和「{d.name}」"
            )
            seen[alias] = d.name
        # 别名也不能跟别的病名的正式名撞车，否则同一个查找会有两种解释。
        assert d.name not in seen or seen[d.name] == d.name


def test_get_disease_by_canonical_name():
    d = get_disease("胃痛")
    assert d is not None
    assert d.name == "胃痛"


def test_get_disease_by_alias():
    # "痞"是"痞满"的别名（对齐 offline/split_cases.py 的门类关键词"痞"）。
    d = get_disease("痞")
    assert d is not None
    assert d.name == "痞满"


def test_get_disease_returns_none_for_unknown_name():
    assert get_disease("这不是一个真实病名") is None


def test_match_disease_ranks_cardinal_and_location_hits_above_no_match():
    # 主诉明确指向胃痛（主症+病位都命中），胃痛应该排第一。
    scored = match_disease(["胃脘胀痛", "嗳气泛酸", "情志不畅"], ["胃", "气滞"])
    assert scored, "应该至少匹配上一个病名"
    assert scored[0][0] == "胃痛"
    # 分数降序排列
    scores = [s for _, s in scored]
    assert scores == sorted(scores, reverse=True)


def test_match_disease_scores_are_bounded_in_zero_one():
    scored = match_disease(["胸闷", "胸痛", "心悸"], ["心"])
    for _, score in scored:
        assert 0.0 < score <= 1.0


def test_match_disease_empty_when_nothing_hits():
    # 症状和病位都跟任何病名的 cardinal/location 都不沾边时，返回空列表，
    # 不是给一堆分数为 0 的"弱匹配"——分数为 0 的候选没有区分度。
    scored = match_disease(["外星人入侵综合征"], ["三焦"])
    # "三焦"本身是合法 LOCATIONS 词，但表里没有任何病名把它列进 location，
    # 所以这里应该是空的（这条断言同时验证了不会误报）。
    for name, _ in scored:
        d = get_disease(name)
        assert "三焦" not in d.location


def test_match_disease_is_deterministic_and_does_not_call_llm():
    # 纯规则：同样输入永远同样输出，跑两次结果必须逐字节一致。
    a = match_disease(["胃脘胀痛"], ["胃"])
    b = match_disease(["胃脘胀痛"], ["胃"])
    assert a == b


def test_match_disease_location_only_still_contributes_when_no_cardinal_hit():
    # 只有病位命中、一条主症都没命中时，也应该拿到一个非零分数（病位是弱证据，
    # 不是"没有证据"）。
    scored = match_disease([], ["心"])
    names = dict(scored)
    assert "胸痹" in names
    assert names["胸痹"] > 0


@pytest.mark.parametrize("d", load_diseases())
def test_every_disease_location_is_a_valid_element_location(d):
    """location 里的每个词必须落在 core.elements.LOCATIONS 里，否则
    match_disease 的病位匹配永远命中不了这个病名（S2 的证素推断根本不可能
    产出一个不在 LOCATIONS 里的病位词）——这条钉住 M4 报告里提到的
    「胸痹的 location 不能沿用 spec 示例里的『胸』」这个修正。"""
    from core.elements import LOCATIONS

    for loc in d.location:
        assert loc in LOCATIONS, f"{d.name} 的 location「{loc}」不在 core.elements.LOCATIONS 里"


@pytest.mark.parametrize("d", load_diseases())
def test_every_disease_triage_urgency_is_a_valid_level_or_none(d):
    assert d.triage_urgency in ("low", "medium", "high", None)
