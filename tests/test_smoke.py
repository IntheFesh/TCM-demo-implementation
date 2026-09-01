"""不需要网络的冒烟测试：schema 约束、模板渲染、markdown 围栏剥离。"""
import pytest
from pydantic import ValidationError

from core.llm import render, strip_code_fence
from core.schemas import CaseSequence, ElementHit, S3Syndrome


def test_element_hit_rejects_empty_supporting_symptoms():
    with pytest.raises(ValidationError):
        ElementHit(
            element="脾",
            kind="location",
            supporting_symptoms=[],
            confidence="high",
        )


def test_case_sequence_rejects_empty_visits():
    with pytest.raises(ValidationError):
        CaseSequence(visits=[])


def test_s3_syndrome_rejects_empty_cited_case_ids():
    with pytest.raises(ValidationError):
        S3Syndrome(
            syndrome="脾胃气虚",
            reasoning="纳差乏力，脉细弱",
            treatment_principle="健脾益气",
            cited_case_ids=[],
        )


def test_render_handles_braces_in_template():
    template = '示例输出：{"symptoms": ["$sym"], "count": 1}'
    result = render(template, sym="纳差")
    assert result == '示例输出：{"symptoms": ["纳差"], "count": 1}'


def test_render_missing_var_does_not_raise():
    result = render("你好 $name，年龄 $age", name="张三")
    assert "张三" in result
    assert "$age" in result  # safe_substitute：缺变量原样保留，不抛异常


def test_strip_code_fence_with_json_fence():
    text = '```json\n{"a": 1}\n```'
    assert strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_with_plain_fence():
    text = '```\n{"a": 1}\n```'
    assert strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_without_fence():
    text = '{"a": 1}'
    assert strip_code_fence(text) == '{"a": 1}'
