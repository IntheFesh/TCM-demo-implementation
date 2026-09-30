"""core/schemas.py 的离线测试：HerbItem / FormulaCandidate / _S3Base。

其余 schema（S1Normalize、S2Elements、ReAct*、CaseRecord 家族……）没有专门的测试
文件，它们的约束是在使用它们的模块（core/chain.py、core/tools.py……）里间接测的。
_S3Base 有两条 model_validator，逻辑复杂到值得单独测——尤其是"扁平字段怎么合成出
formula_candidates"这条扁平构造路径：如果只在 test_chain.py 里跟着别的测试顺带
覆盖，合成逻辑本身出了偏差，不容易第一时间定位到是 schema 层的问题还是
chain.py 用错了。
"""
import pytest
from pydantic import ValidationError

from core.llm import LLMBackend as _LLMBackend
from core.schemas import FormulaCandidate, HerbItem, S3Syndrome, S3SyndromeUnreferenced


# ---------- HerbItem ----------


def test_herb_item_only_name_required_rest_default_to_none():
    h = HerbItem(name="党参")
    assert h.name == "党参"
    assert h.dose is None
    assert h.dose_unit == "g"
    assert h.processing is None
    assert h.decoction is None
    assert h.role is None
    assert h.function_in_formula is None
    assert h.dose_evidence == []


def test_herb_item_rejects_empty_name():
    """跟 core/schemas.py 其余 min_length=1 约束同一条纪律：名字是唯一保证
    非幻觉的锚点，不能允许用空字符串凑数。"""
    with pytest.raises(ValidationError):
        HerbItem(name="")


def test_herb_item_carries_full_structured_fields():
    h = HerbItem(
        name="附子", dose=15.0, dose_unit="g", processing="制",
        decoction="先煎", role="君", function_in_formula="回阳救逆",
        dose_evidence=["ye_tianshi-001"],
    )
    assert (h.dose, h.decoction, h.role) == (15.0, "先煎", "君")
    assert h.dose_evidence == ["ye_tianshi-001"]


# ---------- FormulaCandidate.base_formula 的 model_validator ----------


def _cand(**kw):
    base = dict(
        name="四君子汤", source="classic", confidence="high",
        rationale="脾胃气虚，健脾益气", herb_items=[{"name": "党参"}],
    )
    base.update(kw)
    return FormulaCandidate(**base)


def test_modified_requires_base_formula():
    with pytest.raises(ValidationError, match="base_formula"):
        _cand(source="modified", base_formula=None)


def test_modified_with_base_formula_is_accepted():
    c = _cand(source="modified", base_formula="四君子汤")
    assert c.source == "modified" and c.base_formula == "四君子汤"


@pytest.mark.parametrize("source", ["classic", "composed"])
def test_classic_and_composed_reject_base_formula(source):
    with pytest.raises(ValidationError, match="base_formula"):
        _cand(source=source, base_formula="四君子汤")


@pytest.mark.parametrize("source", ["classic", "composed"])
def test_classic_and_composed_accept_no_base_formula(source):
    c = _cand(source=source, base_formula=None)
    assert c.base_formula is None


def test_formula_candidate_requires_at_least_one_herb():
    with pytest.raises(ValidationError):
        _cand(herb_items=[])


# ---------- _S3Base：显式给 formula_candidates ----------


def _with_candidates(selected=0, cited_case_ids=("a",)):
    return S3Syndrome(
        syndrome="脾胃气虚", reasoning="纳差乏力", treatment_principle="健脾益气",
        cited_case_ids=list(cited_case_ids),
        formula_candidates=[
            {"name": "四君子汤", "source": "classic", "confidence": "high",
             "rationale": "r1", "herb_items": [{"name": "党参", "role": "君"}, {"name": "白术"}]},
            {"name": "香砂六君子汤", "source": "modified", "base_formula": "四君子汤",
             "confidence": "medium", "rationale": "r2",
             "herb_items": [{"name": "党参"}, {"name": "木香"}, {"name": "西药阿斯匹林"}]},
        ],
        selected=selected,
    )


def test_herbs_and_formula_are_derived_from_the_selected_candidate():
    s3 = _with_candidates(selected=0)
    assert s3.formula == "四君子汤"
    assert s3.herbs == ["党参", "白术"]
    assert s3.western_drugs == []


def test_selecting_a_different_candidate_changes_the_derived_flat_fields():
    """selected 不是恒为 0：换一个候选方，herbs/formula 跟着换，不是只认第一个。"""
    s3 = _with_candidates(selected=1)
    assert s3.formula == "香砂六君子汤"
    assert s3.herbs == ["党参", "木香"]
    assert s3.western_drugs == ["西药阿斯匹林"], "混进 herb_items 的西药也要在派生时被挑出来"


def test_explicit_flat_fields_are_overwritten_not_trusted():
    """扁平字段由 formula_candidates[selected] 派生，不信任调用方传入的值：就算调用方
    手写了自相矛盾的 herbs/formula，构造完成后也必须是派生出来的那一份，不是调用方
    传入的那一份。"""
    s3 = S3Syndrome(
        syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"],
        formula="调用方瞎写的方名", herbs=["调用方瞎写的药"], western_drugs=["也是瞎写的"],
        formula_candidates=[{
            "name": "真方名", "source": "composed", "confidence": "low",
            "rationale": "r", "herb_items": [{"name": "真药名"}],
        }],
    )
    assert s3.formula == "真方名"
    assert s3.herbs == ["真药名"]
    assert s3.western_drugs == []


@pytest.mark.parametrize("selected", [-1, 2, 99])
def test_selected_out_of_bounds_is_rejected(selected):
    with pytest.raises(ValidationError, match="越界"):
        _with_candidates(selected=selected)


def test_formula_candidates_min_and_max_length():
    with pytest.raises(ValidationError):
        S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                   cited_case_ids=["a"], formula_candidates=[])
    with pytest.raises(ValidationError):
        one = {"name": "x", "source": "composed", "confidence": "low",
               "rationale": "r", "herb_items": [{"name": "药"}]}
        S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                   cited_case_ids=["a"], formula_candidates=[one, one, one, one])


def test_disease_field_defaults_to_none_and_is_a_plain_passthrough():
    assert _with_candidates().disease is None
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], disease="胃痛",
                    formula_candidates=[{"name": "x", "source": "composed",
                                        "confidence": "low", "rationale": "r",
                                        "herb_items": [{"name": "药"}]}])
    assert s3.disease == "胃痛"


# ---------- _S3Base：只给扁平字段（扁平构造合成）----------


def test_flat_construction_synthesizes_exactly_one_composed_candidate():
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], herbs=["党参", "白术"], formula="四君子汤")
    assert len(s3.formula_candidates) == 1
    cand = s3.formula_candidates[0]
    assert cand.name == "四君子汤" and cand.source == "composed"
    assert [i.name for i in cand.herb_items] == ["党参", "白术"]
    assert s3.selected == 0


def test_flat_construction_round_trips_herbs_and_formula_unchanged():
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], herbs=["党参", "白术"], formula="四君子汤")
    assert s3.herbs == ["党参", "白术"]
    assert s3.formula == "四君子汤"


def test_flat_construction_splits_western_drugs_mixed_into_herbs():
    """混进 herbs 的西药被分流到 western_drugs——这件事由合成路径 + 派生路径的
    组合负责，结果是 herbs 里只剩中药、西药单列。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], herbs=["党参", "西药阿斯匹林", "白术"])
    assert s3.herbs == ["党参", "白术"]
    assert s3.western_drugs == ["西药阿斯匹林"]


def test_flat_construction_with_nothing_given_keeps_empty_defaults():
    """完全没给 herbs/formula/western_drugs/formula_candidates 时，.herbs 必须
    是 []、.formula 必须是 None——不能因为 FormulaCandidate.herb_items 要求
    至少一味药，就悄悄塞一味假药进 .herbs：那会把 herb_jaccard 从 None 变成 0.0。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"])
    assert s3.herbs == []
    assert s3.formula is None
    assert s3.western_drugs == []
    # formula_candidates 本身是一个合法的、非空的候选方列表——
    # 占位符只在派生结果里被过滤掉，底层数据结构保持完整
    assert len(s3.formula_candidates) == 1
    assert len(s3.formula_candidates[0].herb_items) == 1


def test_flat_placeholder_never_leaks_into_derived_herbs_or_formula():
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"])
    placeholder_texts = {s3.formula_candidates[0].name, s3.formula_candidates[0].herb_items[0].name}
    assert not (placeholder_texts & set(s3.herbs))
    assert s3.formula not in placeholder_texts or s3.formula is None


def test_cited_case_ids_empty_list_is_rejected():
    """防幻觉约束：`cited_case_ids` 的 min_length=1 不受 formula_candidates 影响，
    空列表被拒绝。"""
    with pytest.raises(ValidationError):
        S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=[])


# ---------- S3Syndrome 与 S3SyndromeUnreferenced 保持同步 ----------


def test_s3_schemas_share_every_field_except_cited_case_ids():
    """两个 schema 的字段集合必须只在 cited_case_ids 上有差异——这条测试不依赖
    "它们都继承自 _S3Base"这个实现细节，就算继承被拆开、手写成两份，
    这条测试也能立刻抓到字段漂移。"""
    referenced_fields = set(S3Syndrome.model_fields)
    unreferenced_fields = set(S3SyndromeUnreferenced.model_fields)
    assert referenced_fields - unreferenced_fields == {"cited_case_ids"}
    assert unreferenced_fields - referenced_fields == set()


def test_s3_syndrome_unreferenced_cited_case_ids_is_a_property_not_a_field():
    u = S3SyndromeUnreferenced(syndrome="x", reasoning="x", treatment_principle="x")
    assert u.cited_case_ids == []
    assert "cited_case_ids" not in u.model_dump()


def test_s3_syndrome_unreferenced_gets_the_same_derivation():
    u = S3SyndromeUnreferenced(syndrome="x", reasoning="x", treatment_principle="x",
                               herbs=["党参", "西药阿斯匹林"], formula="四君子汤")
    assert u.herbs == ["党参"]
    assert u.western_drugs == ["西药阿斯匹林"]
    assert u.formula == "四君子汤"


def test_s3_syndrome_unreferenced_selected_out_of_bounds_is_rejected():
    with pytest.raises(ValidationError, match="越界"):
        S3SyndromeUnreferenced(
            syndrome="x", reasoning="x", treatment_principle="x", selected=5,
            formula_candidates=[{"name": "x", "source": "composed", "confidence": "low",
                                 "rationale": "r", "herb_items": [{"name": "药"}]}],
        )


# ---------- 真实生产路径：LLMBackend.generate() 解析 JSON ----------


class _FixedJsonBackend(_LLMBackend):
    """只返回预设 JSON 字符串的假后端，走的是 LLMBackend.generate() 真实的
    `schema.model_validate_json(strip_code_fence(raw))` 那一步（core/llm.py），
    不是直接在 Python 里构造 pydantic 对象——真实模型吐 JSON 时走的就是这条路径。"""

    def __init__(self, raw_json: str):
        self._raw = raw_json

    def model_name(self):
        return "fixed"

    def backend_id(self):
        return "fixed"

    def _complete(self, messages, temperature, **kw):
        return self._raw


def test_generate_accepts_flat_json_without_formula_candidates():
    """JSON 里没有 formula_candidates 这个键、只有扁平的 formula / herbs 时，
    generate() 走合成路径，派生出的 formula / herbs / western_drugs 与扁平输入一致。"""
    raw = (
        '{"syndrome": "脾胃气虚", "reasoning": "纳差乏力", '
        '"treatment_principle": "健脾益气", "formula": "四君子汤", '
        '"herbs": ["党参", "西药阿斯匹林", "白术"], "western_drugs": [], '
        '"cited_case_ids": ["ye_tianshi-001"], "note": null}'
    )
    s3 = _FixedJsonBackend(raw).generate(system="s", user="u", schema=S3Syndrome)
    assert s3.formula == "四君子汤"
    assert s3.herbs == ["党参", "白术"]
    assert s3.western_drugs == ["西药阿斯匹林"]
    assert s3.cited_case_ids == ["ye_tianshi-001"]
    assert len(s3.formula_candidates) == 1  # 扁平 JSON 走合成路径，不是 LLM 给的


def test_generate_accepts_json_with_formula_candidates():
    """JSON 里带 formula_candidates 时，generate() 这条真实调用路径能正确解析，
    disease / herbs 等派生字段从选中的候选方取值。"""
    raw = (
        '{"disease": "胃痛", "syndrome": "脾胃气虚", "reasoning": "r", '
        '"treatment_principle": "健脾益气", "selected": 0, '
        '"formula_candidates": [{"name": "四君子汤", "source": "classic", '
        '"confidence": "high", "rationale": "r", '
        '"herb_items": [{"name": "党参", "role": "君"}, {"name": "白术", "role": "臣"}]}], '
        '"cited_case_ids": ["ye_tianshi-001"]}'
    )
    s3 = _FixedJsonBackend(raw).generate(system="s", user="u", schema=S3Syndrome)
    assert s3.disease == "胃痛"
    assert s3.herbs == ["党参", "白术"]
    assert s3.formula_candidates[0].herb_items[0].role == "君"
