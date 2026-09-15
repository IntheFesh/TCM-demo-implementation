"""R1 分层 Jaccard（君臣 / 佐使）的离线测试。

这一轮要回答的问题是"herb_jaccard 那个 0.53 里，多少是核心判断不一致、多少
只是佐使加减不同"。分层指标的正确性全在三件事上，这个文件逐条钉住：

  1. 数学：三层的集合怎么切、Jaccard 怎么算
  2. role=None 的药去哪了（哪都不去，只计 n_unroled），n_unroled 报得对不对
  3. **herb_jaccard 一个字没变**——E3/E4/E9 的历史数字都基于它，加不加 role
     标注、加不加分层字段，它都必须是同一个数

外加两条接口层的：eval/epsilon.json 里 epsilon_core/epsilon_adjunct 的结构，
以及 core/chain.py 那三个 ε 加载器读它的方式。
"""
import json

import pytest

from core import chain
from core.herbs import ADJUNCT_ROLES, CORE_ROLES, role_partitioned_herb_sets
from core.schemas import CaseRecord, FormulaCandidate, HerbItem, S3Syndrome

from tests.test_chain import FakeLLM, FakeRetriever, _fake_cases


@pytest.fixture(autouse=True)
def _pin_two_physicians(monkeypatch):
    """跟 tests/test_chain.py 同名 fixture 同一个理由，且必须在这个文件里再写一遍
    ——autouse fixture 只作用于定义它的那个模块，import FakeLLM 不会把它带过来。
    不钉的话注册表里的第三位医家（张锡纯）会走 FakeLLM 的兜底响应（党参/白术、
    没有 role），分层全变 None、herb_jaccard 变 1.0，测试测的就不是它想测的东西了。
    需要三位医家的那条测试自己把这个覆盖回去。"""
    from core.physicians import physicians_enabled as _enabled

    REG = _enabled()

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})


def _items(*specs) -> list[HerbItem]:
    """(药名, role) 两元组批量造 herb_items；role 传 None 就是"没标注"。"""
    return [HerbItem(name=n, role=r) for n, r in specs]


def _s3(items: list[HerbItem], syndrome="肝胃不和证", pid="ye_tianshi") -> S3Syndrome:
    return S3Syndrome(
        syndrome=syndrome, reasoning="x", treatment_principle="疏肝和胃",
        formula_candidates=[FormulaCandidate(
            name="柴胡疏肝散加减", source="modified", base_formula="柴胡疏肝散",
            confidence="high", rationale="x", herb_items=items,
        )],
        cited_case_ids=[f"{pid}-001"],
    )


# ---------- 1. 分层的数学 ----------


def test_partition_splits_core_and_adjunct_by_role():
    """君臣进 core、佐使进 adjunct，一味药不会同时进两层。"""
    parts = role_partitioned_herb_sets(_items(
        ("半夏", "君"), ("延胡索", "臣"), ("神曲", "佐"), ("甘草", "使"),
    ))
    assert parts["core"] == {"半夏", "延胡索"}
    assert parts["adjunct"] == {"神曲", "甘草"}
    assert parts["core"] & parts["adjunct"] == set()
    assert parts["n_unroled"] == 0 and parts["n_items"] == 4


def test_partition_uses_the_same_herb_normalization_as_herb_jaccard():
    """分层的数要跟 herb_jaccard 放在一起读，两边"怎么把药名变成集合"必须是
    同一个定义（core/herbs.py::normalize_herb）——「云苓块」和「茯苓」是同一味。"""
    a = role_partitioned_herb_sets(_items(("云苓块", "君"), ("炙甘草", "臣")))
    b = role_partitioned_herb_sets(_items(("茯苓", "君"), ("甘草", "臣")))
    assert a["core"] == b["core"] == {"茯苓", "甘草"}


def test_partition_drops_western_drugs():
    """西药不进任何一层，跟 herb_jaccard 的做法一致：只有张锡纯用西药，算进去
    会把跨学派分歧系统性推高，而那个推高是假的。剔掉也不该算进 n_items
    （否则填充率的分母里混进了压根不参与分层的条目）。"""
    parts = role_partitioned_herb_sets(_items(
        ("黄芪", "君"), ("阿斯匹林", "佐"), ("山药", "臣"),
    ))
    assert parts["core"] == {"黄芪", "山药"}
    assert parts["adjunct"] == set()
    assert parts["n_items"] == 2


def test_role_constants_cover_exactly_the_four_schema_roles():
    """CORE_ROLES + ADJUNCT_ROLES 必须正好是 HerbItem.role 那个 Literal 的四个
    合法值——schema 以后加了第五种 role，这条会红，提醒分层跟着改，不会让新
    role 静默掉进"未标注"那一类。"""
    import typing

    literal = HerbItem.model_fields["role"].annotation
    allowed = set(typing.get_args(typing.get_args(literal)[0]))
    assert allowed == set(CORE_ROLES) | set(ADJUNCT_ROLES)


def test_layered_jaccard_math_on_three_hand_built_groups():
    """三组手算：君臣全同（0.0）、佐使全不同（1.0）、部分重叠（1-1/3）。"""
    same = [{"半夏", "茯苓"}, {"半夏", "茯苓"}, {"半夏", "茯苓"}]
    disjoint = [{"神曲"}, {"黄连"}, {"桑叶"}]
    partial = [{"半夏", "陈皮"}, {"半夏", "青皮"}]
    assert chain._layered_jaccard(same) == 0.0
    assert chain._layered_jaccard(disjoint) == 1.0
    assert chain._layered_jaccard(partial) == round(1 - 1 / 3, 3)


def test_layered_jaccard_is_nway_not_pairwise_mean():
    """跟 herb_jaccard 用同一个 n 方公式（三家都用的药才算共同），不是两两
    距离取平均——两者在三集合上给出不同的数，这条用能区分的输入钉住。"""
    sets = [{"a", "b"}, {"a", "c"}, {"a", "d"}]
    assert chain._layered_jaccard(sets) == 0.75              # 1 - 1/4，n 方
    assert chain._layered_jaccard(sets) != round(1 - 1 / 3, 3)  # 两两均值是 0.667


# ---------- 2. role=None 与空层 ----------


def test_unroled_herbs_go_into_neither_layer_but_are_counted():
    """没标 role 的药哪一层都不进，只计 n_unroled——硬塞进某一类会让指标的
    含义变得不可解释。"""
    parts = role_partitioned_herb_sets(_items(
        ("半夏", "君"), ("神曲", None), ("桑叶", None),
    ))
    assert parts["core"] == {"半夏"}
    assert parts["adjunct"] == set()
    assert parts["n_unroled"] == 2
    assert parts["n_items"] == 3


def test_empty_layer_returns_none_not_zero():
    """**0.0 的意思是"完全相同"，跟"没数据"是两回事。** 一层为空（那位医家
    这一层的药全没标 role）时必须返回 None：返回 0.0 会凭空报出"核心用药完全
    一致"，返回 1.0 会凭空报出"毫无重叠"，两个都是假的。"""
    assert chain._layered_jaccard([set(), set()]) is None
    assert chain._layered_jaccard([{"半夏"}, set()]) is None   # 一边有一边空也不算
    assert chain._layered_jaccard([{"半夏"}]) is None          # 少于两位医家


def test_divergence_reports_layers_and_unroled_counts(monkeypatch):
    """端到端：两位医家君臣一致、佐使全不同（实测形状），分层必须把这件事
    分开报，n_unroled 按医家给。"""
    ye = _s3(_items(("半夏", "君"), ("茯苓", "臣"), ("神曲", "佐")), pid="ye_tianshi")
    wu = _s3(_items(("半夏", "君"), ("茯苓", "臣"), ("黄连", "佐"), ("桑叶", None)),
             pid="wu_jutong")
    monkeypatch.setattr(chain, "get_llm", lambda: FakeLLM({"叶天士": ye, "吴鞠通": wu}))
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    div = chain.consult("胃脘胀痛")["divergence"]

    assert div["core_jaccard"] == 0.0        # 君臣骨架一致
    assert div["adjunct_jaccard"] == 1.0     # 佐使毫无重叠
    assert div["shared_core_herbs"] == ["半夏", "茯苓"]
    assert div["shared_adjunct_herbs"] == []
    assert div["n_unroled"] == {"ye_tianshi": 0, "wu_jutong": 1}
    assert "0 的意思是" in div["layer_note"]


def test_divergence_layers_are_none_when_roles_missing(monkeypatch):
    """一位医家完全没标 role：分层两个数都是 None（不是 0），n_unroled 说明
    为什么——前端照这个显示"不适用"，不显示一个假的 0。"""
    ye = _s3(_items(("半夏", "君"), ("神曲", "佐")), pid="ye_tianshi")
    wu = _s3(_items(("半夏", None), ("黄连", None)), pid="wu_jutong")
    monkeypatch.setattr(chain, "get_llm", lambda: FakeLLM({"叶天士": ye, "吴鞠通": wu}))
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))

    div = chain.consult("胃脘胀痛")["divergence"]

    assert div["core_jaccard"] is None and div["adjunct_jaccard"] is None
    assert div["shared_core_herbs"] == [] and div["shared_adjunct_herbs"] == []
    assert div["n_unroled"] == {"ye_tianshi": 0, "wu_jutong": 2}
    # 整方那个数照旧有值：没标 role 不影响它
    assert div["herb_jaccard"] is not None


def test_selected_herb_items_filters_the_legacy_placeholder():
    """旧式构造（只给 herbs，且一味药都没给）会合成一个占位条目——它不能被
    当成"一味没标 role 的药"，否则 n_unroled 凭空多一味、填充率凭空掉一截。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["ye_tianshi-001"])
    assert s3.herbs == [] and s3.formula is None
    assert s3.selected_herb_items == []
    assert role_partitioned_herb_sets(s3.selected_herb_items) == {
        "core": set(), "adjunct": set(), "n_unroled": 0, "n_items": 0,
    }


def test_selected_herb_items_follows_the_selected_index():
    """读的是 selected 那个候选方，不是第一个——分层 ε 跟 herbs 派生字段必须
    看同一张方。"""
    s3 = S3Syndrome(
        syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["c1"],
        formula_candidates=[
            FormulaCandidate(name="甲", source="classic", confidence="low",
                             rationale="x", herb_items=_items(("半夏", "君"))),
            FormulaCandidate(name="乙", source="classic", confidence="high",
                             rationale="x", herb_items=_items(("黄连", "臣"))),
        ],
        selected=1,
    )
    assert [i.name for i in s3.selected_herb_items] == ["黄连"]
    assert s3.herbs == ["黄连"]


# ---------- 3. herb_jaccard 没被动过 ----------


def _consult_div(monkeypatch, ye_items, wu_items):
    monkeypatch.setattr(chain, "get_llm", lambda: FakeLLM({
        "叶天士": _s3(ye_items, pid="ye_tianshi"),
        "吴鞠通": _s3(wu_items, pid="wu_jutong"),
    }))
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    return chain.consult("胃脘胀痛")["divergence"]


def test_herb_jaccard_is_unchanged_by_role_annotation(monkeypatch):
    """**这条是 R1 的护栏。** 同样的药、只是标不标 role，herb_jaccard 必须
    一模一样：E3/E4/E9 的历史数字全基于这个字段，它跟着 R1 变了就不可比了。
    分层是新增的两个数，不是把原来那个数改了。"""
    ye_names = [("半夏", None), ("茯苓", None), ("神曲", None)]
    wu_names = [("半夏", None), ("茯苓", None), ("黄连", None)]
    unroled = _consult_div(monkeypatch, _items(*ye_names), _items(*wu_names))

    ye_roled = [("半夏", "君"), ("茯苓", "臣"), ("神曲", "佐")]
    wu_roled = [("半夏", "君"), ("茯苓", "臣"), ("黄连", "佐")]
    roled = _consult_div(monkeypatch, _items(*ye_roled), _items(*wu_roled))

    assert unroled["herb_jaccard"] == roled["herb_jaccard"] == round(1 - 2 / 4, 3)
    assert unroled["shared_herbs"] == roled["shared_herbs"] == ["半夏", "茯苓"]
    # 分层才是随 role 变化的那部分
    assert unroled["core_jaccard"] is None and roled["core_jaccard"] == 0.0


def test_herb_jaccard_stays_the_nway_intersection_over_union(monkeypatch):
    """算法本身钉住：n 方交并比，不是两两平均、不是分层的任何一个数。
    三位医家、故意让 n 方值（0.75）跟两两均值（0.667）不同。"""
    from core.physicians import physicians_enabled as _enabled

    REAL = _enabled()

    monkeypatch.setattr(chain, "PHYSICIANS", REAL)
    herbs = {"ye_tianshi": [("半夏", "君"), ("茯苓", "佐")],
             "wu_jutong": [("半夏", "君"), ("黄连", "佐")],
             "zhang_xichun": [("半夏", "君"), ("桑叶", "佐")]}
    monkeypatch.setattr(chain, "get_llm", lambda: FakeLLM({
        info["name"]: _s3(_items(*herbs[pid]), pid=pid) for pid, info in REAL.items()
    }))
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever([
        CaseRecord(case_id=f"{pid}-001", case_group_id=f"{pid}-001", physician=pid,
                   raw="原文", symptoms=["纳差"], syndrome="脾胃气虚", herbs=["党参"])
        for pid in REAL
    ]))

    div = chain.consult("胃脘胀痛")["divergence"]
    assert div["herb_jaccard"] == 0.75                      # 1 - 1/4，n 方
    assert div["herb_jaccard"] != round(1 - 1 / 3, 3)       # 不是两两均值
    assert div["core_jaccard"] == 0.0                       # 君臣三家全同
    assert div["adjunct_jaccard"] == 1.0                    # 佐使三家全不同
    assert div["method"] == "nway_jaccard+pairwise"


# ---------- 4. epsilon.json 的新字段与加载器 ----------


def _write_epsilon(tmp_path, monkeypatch, payload: dict):
    path = tmp_path / "epsilon.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(chain, "EPSILON_PATH", path)


def test_load_epsilon_layer_means_reads_the_two_new_top_level_keys(tmp_path, monkeypatch):
    _write_epsilon(tmp_path, monkeypatch, {
        "epsilon_online": {"mean": 0.24},
        "epsilon_core": {"mean": 0.08, "role_fill_rate": 0.97},
        "epsilon_adjunct": {"mean": 0.41, "role_fill_rate": 0.97},
    })
    assert chain.load_epsilon_online() == 0.24
    assert chain.load_epsilon_layer_means() == {"core": 0.08, "adjunct": 0.41}


def test_load_epsilon_layer_means_is_none_on_a_pre_r1_file(tmp_path, monkeypatch):
    """R1 之前跑出来的 epsilon.json 没有这两个键——要返回 None（前端如实展示
    "未测"），不能抛异常也不能编一个数。"""
    _write_epsilon(tmp_path, monkeypatch, {"epsilon_online": {"mean": 0.24}})
    assert chain.load_epsilon_layer_means() == {"core": None, "adjunct": None}


def test_load_epsilon_layer_means_is_none_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(chain, "EPSILON_PATH", tmp_path / "nope.json")
    assert chain.load_epsilon_layer_means() == {"core": None, "adjunct": None}


def test_divergence_carries_all_three_epsilons(tmp_path, monkeypatch):
    """分层的数必须带**自己那一层**的对照基准（CLAUDE.md「任何数字都必须带
    对照」）：拿 ε_online 去卡君臣层会低估一致性、卡佐使层会高估发散。"""
    _write_epsilon(tmp_path, monkeypatch, {
        "epsilon_online": {"mean": 0.24},
        "epsilon_core": {"mean": 0.08},
        "epsilon_adjunct": {"mean": 0.41},
    })
    div = _consult_div(monkeypatch,
                       _items(("半夏", "君"), ("神曲", "佐")),
                       _items(("半夏", "君"), ("黄连", "佐")))
    assert div["epsilon_online"] == 0.24
    assert div["epsilon_core"] == 0.08
    assert div["epsilon_adjunct"] == 0.41


@pytest.mark.parametrize("layer", ["core", "adjunct"])
def test_epsilon_layer_result_has_the_same_shape_as_epsilon_online(layer):
    """epsilon_core / epsilon_adjunct 跟 epsilon_online 结构平行——读 JSON 的
    代码（和人）不用为分层学第二套字段名。另外分层多带三个字段：role 填充率
    和它的分子分母，因为这两个数可信到什么程度全看它。"""
    from offline import estimate_epsilon as ee

    ye = _s3(_items(("半夏", "君"), ("神曲", "佐")), pid="ye_tianshi")

    def consult_fn(complaint):
        return {"rejected": False, "insufficient": False, "manifest": {"llm_calls": 3},
                "results": [{"physician": "ye_tianshi", "s3": ye}]}

    online = ee.estimate_epsilon_online(["主诉甲"], n_repeats=2, consult_fn=consult_fn)
    layers = online.pop("layers")
    assert set(online) == set(layers[layer]) - {"role_fill_rate", "n_herb_items", "n_unroled"}
    assert layers[layer]["role_fill_rate"] == 1.0
    assert layers[layer]["n_unroled"] == 0
