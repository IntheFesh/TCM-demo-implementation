"""R52 `prompts/v1/s3_derived.yaml`：模板本身的硬约束。

跟 `tests/test_s3_structured_prompt.py` 同一条理由（`tests/test_llm_backend.py`
那批 s3_syndrome 测试的延续）：prompt 是这条链上唯一"改了不会报错"的东西，
这份文件专测**这份 prompt 跟 s3_structured.yaml 的关键区别有没有被悄悄抄丢**
——没有 `$refs`、没有医案词汇、要求引 rule_id、允许 insufficient。
"""
from __future__ import annotations

import re
import subprocess

import pytest

from core.llm import PROMPTS_ROOT, load_prompt, render

PROMPT_NAME = "s3_derived"
PLACEHOLDERS = {"elements_summary", "symptoms", "theory_rules", "knowledge"}

# system 段里不该出现的词——除了 dose_evidence 那一处"没有 dose_evidence"的
# 显式说明（告诉模型这个字段不存在），其余任何出现都说明是从 s3_structured.yaml
# 抄漏的。
_FORBIDDEN_IN_SYSTEM = (
    "cited_case_ids", "physician_influences", "physician_source",
    "$refs", "$physicians", "$physician_ids", "参考医案",
)


@pytest.fixture(scope="module")
def prompt() -> dict:
    return load_prompt(PROMPT_NAME)


def test_the_file_exists_and_has_system_and_notes(prompt):
    assert set(prompt) == {"system", "notes"}
    assert prompt["system"].strip() and prompt["notes"].strip()


def test_the_placeholders_are_exactly_these_four(prompt):
    found = set(re.findall(r"(?<!\$)\$\{?([A-Za-z_]\w*)\}?", prompt["system"]))
    assert found == PLACEHOLDERS


def test_render_needs_all_four_and_rejects_a_missing_one(prompt):
    kwargs = {k: f"<{k}>" for k in PLACEHOLDERS}
    out = render(prompt["system"], **kwargs)
    for k in PLACEHOLDERS:
        assert f"<{k}>" in out
    with pytest.raises(KeyError):
        render(prompt["system"], **{k: v for k, v in kwargs.items() if k != "symptoms"})


def test_there_is_no_reference_case_block(prompt):
    """全文没有 `$refs` 占位符，也没有"参考医案"这个提法——这一相从设计上
    就没有医案可参考，不是"$refs 传了空字符串"那种退化。"""
    s = prompt["system"]
    for forbidden in _FORBIDDEN_IN_SYSTEM:
        assert forbidden not in s, f"{forbidden!r} 不该出现在 s3_derived.yaml 的 system 里"


def test_dose_evidence_is_explicitly_said_to_not_exist(prompt):
    """`dose_evidence` 这个词允许出现，但只能是"告诉模型这个字段不存在"的那句话，
    不能是"请填 dose_evidence"那种沿用 s3_structured.yaml 措辞的残留。"""
    s = prompt["system"]
    assert "没有 dose_evidence" in s
    assert "dose_evidence：" not in s, "不该再要求模型去填这个字段"


def test_the_five_step_chain_names_all_appear(prompt):
    s = prompt["system"]
    for step in ("病变脏腑", "证型", "治法", "方剂", "药物组成"):
        assert step in s


def test_rule_refs_and_insufficient_are_explained(prompt):
    s = prompt["system"]
    assert "rule_refs" in s and "insufficient" in s
    assert "missing_rule_kind" in s
    for kind in ("organ_relation", "pathomechanism", "treatment_principle", "compatibility"):
        assert kind in s


def test_the_model_is_told_not_to_reference_any_physician(prompt):
    s = prompt["system"]
    assert "没有被告知任何医家的名字" in s or "不该在推理里提到任何人的临床经验" in s


def test_composed_formulas_are_still_required_to_have_a_name(prompt):
    """自组方也要起名字——`FormulaCandidate.name` 没有放松成可选，prompt 要
    显式提醒，不能让模型以为 composed 就可以不填。"""
    s = prompt["system"]
    assert "自组方也要起名字" in s or "composed 也要" in s or "不要留空" in s


def test_the_example_skeleton_has_rule_refs_on_every_step(prompt):
    """占位符骨架示例里，五步链的每一步（含每味药）都要示范 rule_refs +
    insufficient 这对字段——只在正文说明里提过不算数，模型最先抄的是示例。"""
    s = prompt["system"]
    # 示例 JSON 块从 "organs" 开始
    example = s[s.index('"organs"'):]
    assert example.count('"rule_refs"') >= 5
    assert example.count('"insufficient"') >= 5


def test_the_notes_explain_why_three_prompts_coexist(prompt):
    n = prompt["notes"]
    assert "s3_structured.yaml" in n
    assert "s3_syndrome.yaml" in n or "legacy" in n
    assert "derived" in n


def test_neither_the_legacy_nor_the_structured_prompt_was_touched():
    """§0.6 同一条纪律：新增 derived 这一档，不改另外两份既有 prompt——
    R1~R32（legacy）、R33~R51（structured）的数字都在它们下面跑出来。"""
    r = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--",
         "prompts/v1/s3_syndrome.yaml", "prompts/v1/s3_structured.yaml"],
        cwd=PROMPTS_ROOT.parent, capture_output=True, text=True)
    assert r.stdout.strip() == "", "s3_syndrome.yaml / s3_structured.yaml 被改动了"


# ---------- R62 §3.2：五步链之外的四项 ----------


def test_the_prompt_asks_for_all_four_r62_sections(prompt):
    """四项在 schema 里是带默认值的（历史录制里没有这几个字段，必填会让
    每一份 fixture 当场解析失败）——所以"模型到底会不会填"完全取决于这份
    prompt 有没有要。这条测试是那个要求的唯一守门人。"""
    s = prompt["system"]
    for field in ("key_points", "differential", "modifications", "self_assessment"):
        assert field in s, f"prompt 里没有要求 {field}，schema 上它又是可选的，结果就是永远空着"


def test_the_prompt_states_the_minimum_counts_for_differential_and_modifications(prompt):
    """§3.2 原文：鉴别 ≥2 条。少于两条的"鉴别"说不出这个结论排除了什么，
    而"排除了什么"正是判断可信度的依据。"""
    s = prompt["system"]
    assert "至少 2 条" in s
    assert "至少 3 条" in s          # key_points


def test_the_prompt_forbids_listing_the_conclusion_itself_as_a_differential(prompt):
    """跟 `S3Derived._differential_excludes_other_syndromes` 是同一条约束的
    两个入口：prompt 先说清楚，schema 再兜底。只有 schema 拦、prompt 不说，
    模型会反复撞上这条校验，白白多跑 revise 轮次。"""
    assert "不能填本次的结论证型" in prompt["system"]


def test_the_modification_example_uses_the_same_item_shape_as_herb_items(prompt):
    """加减建议里的药用 `item`（跟 herb_items 同形状），不是另起一组
    herb/dose 字段——界面上这一条是可以一键写回处方表的。"""
    s = prompt["system"]
    example = s[s.index('"modifications"'):]
    assert '"item"' in example
    assert '"dose_unit"' in example


def test_self_assessment_has_no_score_field(prompt):
    """自评只呈现、不择优。示例里一旦出现一个可比大小的数，下游迟早拿它排序
    ——而"模型自评"排出来的先后没有任何依据（R44 消除投票痕迹那一轮的教训）。"""
    s = prompt["system"]
    example = s[s.index('"self_assessment"'):]
    example = example[:example.index("}") + 1]
    for banned in ("score", "confidence", "rating", "评分", "置信"):
        assert banned not in example
