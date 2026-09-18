"""把本项目接到 MTCMB 的 TCM-PR（方剂推荐）上。两个 solver，一个对照。

**结构照搬 `eval/sdt/adapter.py`**：同一个模型、同一份提示词、同一个输出形状，
**唯一的差是 `$extra` 里塞不塞本项目 S1+S2 的证素分析**。只改一个变量，
差值才归因得清楚——SDT 那一轮的经验，这里不另发明一套。

安全否决照跑：命中危重信号的记录返回空方、按"没作答"记（`score.py` 把它们
单列，不混进均值）。**不许为了跑分关掉**——关掉跑出来的分不是这套系统的分。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from core.llm import get_llm, load_prompt, render
from core.safety import check_safety, safety_bypassed
from eval.mtcmb.data import PrescriptionRecord


class Prescription(BaseModel):
    """模型开的方。`herbs` 允许为空——**拿不准时不凑数是合法策略**
    （多开一味错药和少开一味对药代价相当），所以这里不写 min_length=1。
    这跟 `core/schemas.py` 里那些防幻觉约束不是一回事：那边约束的是
    "声称有依据就必须指得出依据"，这里约束的是"开几味药"。"""

    herbs: list[str] = Field(default_factory=list)
    reasoning: str = ""


@dataclass
class PrescriptionAnswer:
    record_id: str
    herbs: list[str] = field(default_factory=list)
    reasoning: str = ""
    safety_rejected: str | None = None
    llm_calls: int = 0
    error: str | None = None
    #: 失败的**类型**（TimeoutError / ValidationError…），跟 error 分开：
    #: 报告里要能看出"这批失败是同一种原因"还是"五花八门"。
    error_kind: str | None = None


class BaselineSolver:
    """对照组：裸模型开方，不注入证素分析。"""

    name = "baseline"
    extra_calls = 0

    def extra_block(self, record: PrescriptionRecord) -> str:
        return ""

    def solve(self, record: PrescriptionRecord,
              ignore_safety_veto: bool | None = None) -> PrescriptionAnswer:
        answer = PrescriptionAnswer(record_id=record.record_id)
        reject = (None if safety_bypassed(ignore_safety_veto)
                  else check_safety([record.question]))
        if reject is not None:
            answer.safety_rejected = reject
            return answer
        try:
            out = get_llm().generate(
                system=render(load_prompt("mtcmb_prescribe")["system"],
                              extra=self.extra_block(record)),
                user=record.question, schema=Prescription)
        except Exception as e:  # noqa: BLE001 - 一条跑挂不该把整批丢掉（同 R9 的失败容忍）
            # **分类在这里做**：`classify_llm_failure` 要的是异常对象
            # （它看 `__cause__`，那才是真正的底层原因）。传到上层再分类时
            # 手里只剩一个字符串，四种失败会长成同一个样。
            from core.batch import classify_llm_failure

            answer.error = f"{classify_llm_failure(e)}: {e}"
            answer.error_kind = classify_llm_failure(e)
            return answer
        answer.herbs = [h.strip() for h in out.herbs if h and h.strip()]
        answer.reasoning = out.reasoning
        answer.llm_calls = 1 + self.extra_calls
        return answer


class ChainSolver(BaselineSolver):
    """实验组：先跑 S1+S2，把证素分析塞进同一份提示词的 `$extra`。"""

    name = "chain"
    extra_calls = 2  # S1 + S2

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def extra_block(self, record: PrescriptionRecord) -> str:
        if record.record_id in self._cache:
            return self._cache[record.record_id]
        from core.chain import _format_elements_summary, infer_elements, normalize

        s1 = normalize(record.question)
        s2 = infer_elements(s1)
        block = (
            "以下是对这段描述先做的结构化分析，供参考"
            "（可能不完整，与原文冲突时以原文为准）：\n"
            f"  标准化症状：{'；'.join(s1.symptoms) or '（无）'}\n"
            f"  舌：{s1.tongue or '未记'}　脉：{s1.pulse or '未记'}\n"
            f"  证素：{_format_elements_summary(s2)}\n"
        )
        self._cache[record.record_id] = block
        return block


SOLVERS = {"baseline": BaselineSolver, "chain": ChainSolver}
