"""core/batch.py 的离线测试：失败分类 + 失败率警告，两件事全项目共用的
唯一实现。"""
from core.batch import (
    FAILURE_RATE_WARNING_THRESHOLD,
    classify_llm_failure,
    warn_if_failure_rate_high,
)
from core.llm import LLMError, LLMTruncatedError


def test_classify_llm_failure_unwraps_cause_when_present():
    """core.llm.generate() 用 `raise LLMError(...) from last_error` 保留了
    真实底层异常——分类要取 __cause__ 的类型名，不是 LLMError 这个包装类型
    名本身。"""
    try:
        try:
            raise TimeoutError("网络超时")
        except TimeoutError as cause:
            raise LLMError("重试耗尽") from cause
    except LLMError as e:
        assert classify_llm_failure(e) == "TimeoutError"


def test_classify_llm_failure_falls_back_to_error_type_without_cause():
    """没有 __cause__（异常跟 LLM 调用无关，在别处直接抛出）时退回异常
    自身的类型名，不猜、不报错。"""
    error = RuntimeError("跟 LLM 调用无关的错误")
    assert error.__cause__ is None
    assert classify_llm_failure(error) == "RuntimeError"


def test_classify_llm_failure_works_on_llm_truncated_error_too():
    """LLMTruncatedError 是 LLMError 的子类，同样的分类逻辑要适用——虽然
    实践中调用方一般会先单独 catch 它（截断不值得重跑），这里只确认分类
    函数本身不因为是子类就出错。"""
    try:
        try:
            raise ValueError("json_invalid")
        except ValueError as cause:
            raise LLMTruncatedError("疑似截断") from cause
    except LLMTruncatedError as e:
        assert classify_llm_failure(e) == "ValueError"


def test_warn_if_failure_rate_high_triggers_above_threshold(capsys):
    warn_if_failure_rate_high("测试标签", 3, 10)  # 30% > 20% 阈值
    out = capsys.readouterr().out
    assert "警告" in out
    assert "测试标签" in out
    assert "3/10" in out


def test_warn_if_failure_rate_high_silent_at_or_below_threshold(capsys):
    warn_if_failure_rate_high("测试标签", 2, 10)  # 恰好等于 20% 阈值，不触发
    assert capsys.readouterr().out == ""


def test_warn_if_failure_rate_high_no_samples_does_not_divide_by_zero(capsys):
    warn_if_failure_rate_high("测试标签", 0, 0)  # 不该抛 ZeroDivisionError
    assert capsys.readouterr().out == ""


def test_failure_rate_warning_threshold_is_a_shared_constant_not_a_magic_number():
    """防止将来有人在某一处改小/改大阈值又忘了同步另外几处——四处批处理
    入口共用同一个常量对象，不是各自拷贝一份同样的数字。"""
    assert FAILURE_RATE_WARNING_THRESHOLD == 0.20
