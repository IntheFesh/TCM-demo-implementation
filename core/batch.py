"""批量调 LLM 时"失败怎么分类"、"失败率高了要不要吼一声"——这两件事全项目
只在这里实现一处。

`offline/extract_case_triples.py`（X3）最早定下规矩：失败原因取
`type(err.__cause__).__name__`，不解析错误信息文本（文本格式一变解析就错）。
后来 `eval/run_eval.py`、`offline/estimate_epsilon.py`、`eval/sdt/run.py`
四处批处理入口都要按同一个口径分类失败——这本来是同一个概念，一开始各写
各的（run_eval.py 第一版直接拼 `f"{type(e).__name__}: {e}"`，没有解出
`__cause__`），按 CLAUDE.md「同一概念的匹配逻辑只能有一处实现」收进一处。

**注意范围**：这里只抽两件真正逐字相同的事——分类、警告。每个收集器"怎么
记录失败样本、失败样本占不占哪个分母"这件事本身不抽，因为四处的数据形状
完全不同（pairs 列表 / (query, physician) 记录 / 集合列表 / 答题记录），
硬凑一个通用签名反而会比各自的 10 行 try/except 更难读——这是判断过的
"不抽"，不是漏抽。
"""
from __future__ import annotations

# 少量失败（API 抖动/限流）可以接受，大量失败说明这批结果不可信——0.20 是
# 这两者之间一个保守的分界，不是精确值。四处批处理入口共用同一个数字，
# 不是因为这个数字本身有多精确，是因为"多高算高"应该是一个跨脚本一致的
# 判断，不该 run_eval 用 20%、estimate_epsilon 用 30%，报告之间没法比。
FAILURE_RATE_WARNING_THRESHOLD = 0.20


def classify_llm_failure(error: BaseException) -> str:
    """失败原因分类：真实异常类型名，不是"LLMError"这个包装类型名本身。

    `core.llm.LLMBackend.generate()` 在重试耗尽后 `raise LLMError(...) from
    last_error`——`__cause__` 上挂着真正的底层异常（TimeoutError、
    RateLimitError、ValidationError……）。直接 `type(e).__name__` 只会告诉
    你"是个 LLMError"，四处失败不管什么原因都长一个样，分不出"这批失败是
    同一种原因"还是"五花八门"。没有 `__cause__`（异常跟 LLM 调用无关、在
    别处抛出）时退回 `type(error).__name__` 本身，不猜。
    """
    cause = error.__cause__
    return type(cause).__name__ if cause is not None else type(error).__name__


def warn_if_failure_rate_high(label: str, n_failed: int, n_total: int) -> None:
    """失败率超过 FAILURE_RATE_WARNING_THRESHOLD 时在 stdout 打醒目警告。

    只负责"要不要额外吼一声"——报告本身在各自的 n_failed/note 字段里已经
    如实记了数，这个函数不重复那份记录。n_total<=0 时不判定（没有样本谈不上
    失败率），避免除零。
    """
    if n_total <= 0:
        return
    rate = n_failed / n_total
    if rate > FAILURE_RATE_WARNING_THRESHOLD:
        print(
            f"⚠️  警告：{label} 有 {n_failed}/{n_total}（{rate:.1%}）条样本因调用失败被跳过，"
            f"超过 {FAILURE_RATE_WARNING_THRESHOLD:.0%} 的警戒线——"
            "这次结果的可信度存疑，建议检查 LLM 后端是否稳定后重跑，不要直接采信。"
        )
