"""R60 §2.1.2：`prompts/v1/s3_derived.yaml` 里"ontology_refs.span 要照抄"这个
要求，跟 `core/formula_verifier.py` 里真正核对 span 的判据（`check_herb_source_fabricated`/
`check_herb_source_paraphrased`），原来各说各的——提示词只说"照抄"，没说清楚
"抄不对会怎样"；验证器只管判，没说清楚"要求模型怎么填"。这份测试用源码级
grep 钉住两处必须互相指名，改一处忘了改另一处会直接红——不靠人工记住去同步。

R61：这条规则是全局的，三条 S3 路径共用同一个验证器，但 R60 只改了
`s3_derived.yaml` 一个文件——`s3_structured.yaml`（A 组）没跟上，A 组因此
在 R60 之后独自违规到 87% 的 veto 率。**参数化到两份提示词**，不是只测
`s3_derived.yaml` 一份，否则这次"只改一处"的漏改本身测不出来。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROMPT_PATHS = (
    ROOT / "prompts" / "v1" / "s3_derived.yaml",
    ROOT / "prompts" / "v1" / "s3_structured.yaml",
)
VERIFIER_PATH = ROOT / "core" / "formula_verifier.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("prompt_path", PROMPT_PATHS, ids=lambda p: p.name)
def test_prompt_names_the_two_verifier_functions_that_check_its_span_rule(prompt_path):
    """提示词里"span 照抄"那一节必须点名具体是哪两个函数在核对，不能只说
    "系统会核对"这种模糊的话——模糊到读不出该改哪个文件。"""
    text = _read(prompt_path)
    assert "check_herb_source_fabricated" in text
    assert "check_herb_source_paraphrased" in text
    assert "core/formula_verifier.py" in text


def test_verifier_points_back_to_both_prompt_files():
    """反过来，验证器的判据也要点名是在核对哪几份提示词的哪条约定，不能让人
    只看 `core/formula_verifier.py` 猜不出这条判据是回答哪份提示词里的哪句话
    ——三条 S3 路径共用这一份验证器，点名要覆盖到会走这条规则的每一份。"""
    text = _read(VERIFIER_PATH)
    assert "prompts/v1/s3_derived.yaml" in text
    assert "prompts/v1/s3_structured.yaml" in text


@pytest.mark.parametrize("prompt_path", PROMPT_PATHS, ids=lambda p: p.name)
def test_prompt_explains_the_consequence_of_not_copying_verbatim(prompt_path):
    """不只是"照抄"两个字，还要说清楚"抄不对会怎样"（revise 还有机会改，
    veto 直接不下发）——这是 R60 诊断出来的根因（提示词只说要求，没说后果，
    模型没有动机真的去抄），补上后果说明才算把约定写完整。"""
    text = _read(prompt_path)
    assert "veto" in text
    assert "revise" in text


@pytest.mark.parametrize("prompt_path", PROMPT_PATHS, ids=lambda p: p.name)
def test_knowledge_block_promises_a_copyable_span_section(prompt_path):
    """提示词让模型去"知识块里的可摘录原文"这个小节抄——这个小节必须真实
    存在于知识块生成代码里，不能是提示词单方面承诺了一个不存在的东西
    （见 `core/context_prefix.py::_focused_herb_block` 的"可摘录原文"）。"""
    prompt_text = _read(prompt_path)
    context_prefix_text = _read(ROOT / "core" / "context_prefix.py")
    assert "可摘录原文" in prompt_text
    assert "可摘录原文" in context_prefix_text
