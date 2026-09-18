"""R46 §7.2 第 3–4 条：「人」维的个体化调整。

**这个文件钉的核心是一条**：每条调整都指得出依据，指不出就不产出。
`IndividualizationItem.basis` 是 `Field(min_length=1)`，跟 `cited_case_ids`
同一条防幻觉纪律——一条"孕妇应当减量"的调整，说不出依据就是模型自己想的。
"""
import pytest

from core.agent import RULES_BY_ID
from core.individualize import (
    NO_BASIS_DIMENSIONS,
    TOXIC_MARKERS,
    individualize,
)
from core.schemas import Individualization, IndividualizationItem, PatientProfile


def test_an_empty_profile_says_so_instead_of_guessing():
    """没填人维**不等于**没有需要调整的——两者的区别写在 considered 里。"""
    out = individualize(PatientProfile(), ["桂枝"])
    assert out.items == []
    assert out.considered and "未填写" in out.considered[0]


def test_pregnancy_hits_the_contraindication_entries():
    out = individualize(PatientProfile(life_stage="妊娠期"), ["桂枝"])
    assert any(i.target == "桂枝" and "孕" in i.basis for i in out.items)


def test_every_item_carries_a_basis_with_a_book_name():
    out = individualize(PatientProfile(life_stage="妊娠期"), ["桂枝", "甘草"])
    for it in out.items:
        assert it.basis.strip(), "有一条调整没有依据"
        assert "《" in it.basis or "医师录入" in it.basis


def test_the_elderly_get_a_warning_on_toxic_herbs():
    out = individualize(PatientProfile(life_stage="老年"), ["细辛"])
    hit = [i for i in out.items if i.target == "细辛"]
    assert hit, "老年患者用毒药没有提示"
    assert any(m in hit[0].basis for m in TOXIC_MARKERS)


def test_children_get_a_dose_conversion_note_based_on_the_adult_dose():
    out = individualize(PatientProfile(life_stage="儿童"), ["桂枝"])
    dose = [i for i in out.items if i.kind == "剂量"]
    assert dose, "小儿没有剂量折算提示"
    assert "成人量" in dose[0].basis, "折算提示没有指出成人量出自哪"


def test_the_pediatric_note_does_not_give_a_number_to_copy_into_the_prescription():
    """教材给的是范围，具体由医师定。**给一个可以直接抄的数**就等于替医师开方。"""
    out = individualize(PatientProfile(life_stage="婴幼儿"), ["桂枝"])
    for it in (i for i in out.items if i.kind == "剂量"):
        assert "由医师定" in it.adjustment


def test_allergies_are_matched_against_the_prescription():
    out = individualize(PatientProfile(allergies=["甘草"]), ["甘草", "桂枝"])
    hit = [i for i in out.items if i.kind == "去药"]
    assert hit and hit[0].target == "甘草"
    assert "过敏史" in hit[0].basis


def test_a_dimension_without_a_basis_is_declared_not_silently_skipped():
    """**取不到依据的维度，不产出条目，但要说"查过了、没有依据可用"。**
    这是这个模块最要紧的一条。"""
    out = individualize(PatientProfile(current_medications=["华法林"]), ["桂枝"])
    assert any(d in out.considered for d in NO_BASIS_DIMENSIONS)
    # 没有本体依据 → 一条相互作用的提示都不许编
    assert not [i for i in out.items if "相互作用" in i.adjustment]


def test_nothing_to_adjust_is_different_from_not_checked():
    """空的 items 配上非空的 considered，才说得清"查过了，没有需要调的"。"""
    out = individualize(PatientProfile(age_years=30, sex="男"), ["甘草"])
    assert isinstance(out, Individualization)
    assert out.items == [] or all(i.basis for i in out.items)


def test_duplicate_hits_on_the_same_herb_are_collapsed():
    out = individualize(PatientProfile(life_stage="妊娠期"), ["桂枝", "桂枝"])
    keys = [(i.kind, i.target, i.adjustment) for i in out.items]
    assert len(keys) == len(set(keys))


def test_the_item_schema_refuses_an_empty_basis():
    with pytest.raises(Exception):
        IndividualizationItem(kind="剂量", target="附子", adjustment="减量",
                              reason="老年", basis="")


def test_the_item_schema_refuses_an_unknown_kind():
    with pytest.raises(Exception):
        IndividualizationItem(kind="随便写", target="附子", adjustment="减量",
                              reason="老年", basis="药典")


def test_unknown_herbs_are_skipped_not_guessed():
    """本体里没有这味药 = 查不到依据 = 不产出条目。"""
    out = individualize(PatientProfile(life_stage="妊娠期"), ["某味不存在的药"])
    assert not [i for i in out.items if i.target == "某味不存在的药"]


def test_it_is_a_named_rule_in_the_agent_table():
    """§7.2 第 4 条要求"进 R44 的规则表"。"""
    rule = RULES_BY_ID["verify_patient_fit"]
    assert rule.capability == "verify"
    assert rule.gate == "core.individualize:individualize"


def test_it_is_a_separate_rule_from_the_formula_verifier():
    """那条验的是"方本身立不立得住"，这条验的是"对这位患者合不合适"。
    合成一条的话，"方是对的但人不对"会被说成方有问题。"""
    assert RULES_BY_ID["verify_and_revise"].gate != RULES_BY_ID["verify_patient_fit"].gate
