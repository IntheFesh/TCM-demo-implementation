"""core/schemas.py 的离线测试：M1 新增的 HerbItem / FormulaCandidate / _S3Base。

其余 schema（S1Normalize、S2Elements、ReAct*、CaseRecord 家族……）没有专门的测试
文件，是因为它们的约束一直是在使用它们的模块（core/chain.py、core/tools.py……）
里间接测的。这次不一样：_S3Base 新加了两条 model_validator，逻辑复杂到值得单独
测——尤其是"旧式扁平字段怎么合成出 formula_candidates"这条向后兼容路径，
如果只在 test_chain.py 里跟着别的测试顺带覆盖，合成逻辑本身出了偏差不容易
第一时间定位到是 schema 层的问题还是 chain.py 用错了。
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
    非幻觉的锚点，不能允许空字符串占位。"""
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


# ---------- _S3Base：新式构造（显式给 formula_candidates）----------


def _new_style(selected=0, cited_case_ids=("a",)):
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
    s3 = _new_style(selected=0)
    assert s3.formula == "四君子汤"
    assert s3.herbs == ["党参", "白术"]
    assert s3.western_drugs == []


def test_selecting_a_different_candidate_changes_the_derived_flat_fields():
    """selected 不是恒为 0：换一个候选方，herbs/formula 跟着换，不是只认第一个。"""
    s3 = _new_style(selected=1)
    assert s3.formula == "香砂六君子汤"
    assert s3.herbs == ["党参", "木香"]
    assert s3.western_drugs == ["西药阿斯匹林"], "混进 herb_items 的西药也要在派生时被挑出来"


def test_explicit_flat_fields_are_overwritten_not_trusted():
    """"不要靠调用方记得同步"的字面意思：就算调用方手写了自相矛盾的 herbs/formula，
    构造完成后也必须是从 formula_candidates[selected] 派生的那一份，不是调用方
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
        _new_style(selected=selected)


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
    assert _new_style().disease is None
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], disease="胃痛",
                    formula_candidates=[{"name": "x", "source": "composed",
                                        "confidence": "low", "rationale": "r",
                                        "herb_items": [{"name": "药"}]}])
    assert s3.disease == "胃痛"


# ---------- _S3Base：旧式构造（向后兼容合成）----------


def test_legacy_construction_synthesizes_exactly_one_composed_candidate():
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], herbs=["党参", "白术"], formula="四君子汤")
    assert len(s3.formula_candidates) == 1
    cand = s3.formula_candidates[0]
    assert cand.name == "四君子汤" and cand.source == "composed"
    assert [i.name for i in cand.herb_items] == ["党参", "白术"]
    assert s3.selected == 0


def test_legacy_construction_round_trips_herbs_and_formula_unchanged():
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], herbs=["党参", "白术"], formula="四君子汤")
    assert s3.herbs == ["党参", "白术"]
    assert s3.formula == "四君子汤"


def test_legacy_construction_still_splits_western_drugs_mixed_into_herbs():
    """M1 之前 core/chain.py 的 _split_western_into_s3 干的事——现在挪进了
    合成路径 + 派生路径的组合，行为必须逐字节保持。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                    cited_case_ids=["a"], herbs=["党参", "西药阿斯匹林", "白术"])
    assert s3.herbs == ["党参", "白术"]
    assert s3.western_drugs == ["西药阿斯匹林"]


def test_legacy_construction_with_nothing_given_keeps_old_empty_defaults():
    """完全没给 herbs/formula/western_drugs/formula_candidates 时，.herbs 必须
    还是 []、.formula 必须还是 None——不能因为 FormulaCandidate.herb_items 要求
    至少一味药，就悄悄塞一味假药进 .herbs，把 herb_jaccard 从 None 变成 0.0
    （这是一个没人要求过的行为变化，M1 迁移设计专门避开了它）。"""
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"])
    assert s3.herbs == []
    assert s3.formula is None
    assert s3.western_drugs == []
    # 但 formula_candidates 本身仍然是一个合法的、非空的候选方列表——
    # 占位符只在派生结果里被过滤掉，不代表底层数据结构被破坏
    assert len(s3.formula_candidates) == 1
    assert len(s3.formula_candidates[0].herb_items) == 1


def test_legacy_placeholder_never_leaks_into_derived_herbs_or_formula():
    s3 = S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"])
    placeholder_texts = {s3.formula_candidates[0].name, s3.formula_candidates[0].herb_items[0].name}
    assert not (placeholder_texts & set(s3.herbs))
    assert s3.formula not in placeholder_texts or s3.formula is None


def test_cited_case_ids_empty_list_still_rejected_with_new_schema():
    """防幻觉约束一个字没动：这条断言在改造前就有，这里重申一遍，钉住
    formula_candidates 的新增没有影响 cited_case_ids 的 min_length=1。"""
    with pytest.raises(ValidationError):
        S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=[])


# ---------- S3Syndrome 与 S3SyndromeUnreferenced 保持同步 ----------


def test_s3_schemas_share_every_field_except_cited_case_ids():
    """两个 schema 的字段集合必须只在 cited_case_ids 上有差异——这条测试不依赖
    "它们都继承自 _S3Base"这个实现细节，就算将来有人把继承拆开手写两份，
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


# ---------- 真实生产路径：LLMBackend.generate() 喂旧形状 JSON ----------


class _FixedJsonBackend(_LLMBackend):
    """只返回预设 JSON 字符串的假后端，走的是 LLMBackend.generate() 真实的
    `schema.model_validate_json(strip_code_fence(raw))` 那一步（core/llm.py），
    不是直接在 Python 里构造 pydantic 对象——这是 M3 改 prompt 之前，真实模型
    仍按旧 schema 吐 JSON 时会真正发生的路径。"""

    def __init__(self, raw_json: str):
        self._raw = raw_json

    def model_name(self):
        return "fixed"

    def backend_id(self):
        return "fixed"

    def _complete(self, messages, temperature, **kw):
        return self._raw


def test_generate_accepts_old_shape_json_without_formula_candidates():
    """M3 之前 prompt 没变，模型吐的还是旧 schema 的 JSON（没有 formula_candidates
    这个键）。这条测试保证 M1 单独合并到主干时，consult() 端到端行为不受影响——
    不用等到 M3 才能验证这一点。"""
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
    assert len(s3.formula_candidates) == 1  # 旧形状 JSON 走合成路径，不是 LLM 给的


def test_generate_accepts_new_shape_json_with_formula_candidates():
    """M3 之后模型会真的吐 formula_candidates；这里先确认 generate() 这条真实
    调用路径能正确解析新形状，不用等 M3 的 prompt 改完才第一次跑到这条代码。"""
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
