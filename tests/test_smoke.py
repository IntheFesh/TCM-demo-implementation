"""不需要网络的冒烟测试：schema 约束、模板渲染、markdown 围栏剥离。"""
import pytest
from pydantic import ValidationError

from core.llm import render, strip_code_fence
from core.schemas import CaseSequence, ElementHit, S3Syndrome, SegmentPatients


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


def test_segment_patients_allows_empty_patients():
    # 故意和上面那条相反：一个粗段可能整段都是编者按语，没有病人是合法输出，
    # 不能把它也当成"防幻觉约束"加 min_length=1，否则会逼模型编一个病人出来。
    result = SegmentPatients(patients=[])
    assert result.patients == []


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


def test_render_missing_var_raises():
    """缺变量必须报错。原来是 safe_substitute 静默保留 "$age"：审查时数过，9 个 yaml
    的占位符与 9 处 render() 的 kwargs 逐一吻合，没有调用方在用"可选段落"这个口子，
    留着它只会让将来 yaml 新加的 $var 原样出现在 prompt 里而全部测试照样通过。"""
    import pytest

    with pytest.raises(KeyError, match="age"):
        render("你好 $name，年龄 $age", name="张三")
    assert render("你好 $name", name="张三") == "你好 张三"


def test_strip_code_fence_with_json_fence():
    text = '```json\n{"a": 1}\n```'
    assert strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_with_plain_fence():
    text = '```\n{"a": 1}\n```'
    assert strip_code_fence(text) == '{"a": 1}'


def test_strip_code_fence_without_fence():
    text = '{"a": 1}'
    assert strip_code_fence(text) == '{"a": 1}'
