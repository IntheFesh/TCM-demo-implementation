"""把本项目的推理链适配到 TCMEval-SDT 的四项任务上。

**为什么不是直接把 consult() 的输出转个格式就完事：**SDT 的任务形状跟这个 demo
不是一回事。demo 是「主诉 → 两位医家各自的证型/治法/方药 + 分歧」，SDT 是
「医案 → 原样摘录临床信息 / 病机多选 / 证型多选 / 写一段辨证分析」。两位医家
的分歧对照在 SDT 上没有对应物（它只要一个答案），而 SDT 的 Task1 要的是**原文
片段**，跟我们 S1 做的术语归一化正好相反。所以适配器复用的是链的推理部分
（S1/S2 的证素分析），另配三个 SDT 形状的输出头。

**两个 Solver 必须都在。** 项目规则「任何数字都必须带对照」：只报
「我们的结构化推理链拿了 X 分」没有意义，必须同时有 BaselineSolver（同一个
模型、同样的三个输出头、但不喂证素分析）的分做对照，差值才是这条链的贡献。

**安全否决不许为了跑分关掉。** SDT 里有 6%（Validation）/ 16%（Test）的记录
会命中危重症状拦截，它们会产出空答案、得 0 分。那是这套系统真实的行为——
关掉它跑出来的分不是这套系统的分。`ignore_safety_veto` 这个开关存在只是为了
让人能量化"安全层花了多少分"，用它跑出来的数必须单独标注，不能混进主结果。
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from core.llm import get_llm, load_prompt, render
from core.safety import check_safety, safety_bypassed
from eval.sdt.data import SdtRecord, to_line


# ---------- 三个输出头的 LLM schema ----------

class ExtractedInfo(BaseModel):
    # min_length=1：空摘录必然 0 分，白花一次调用。让 schema 挡下来走重试，
    # 比收一个空列表再自己判空强。
    items: list[str] = Field(min_length=1)


class SelectedOptions(BaseModel):
    # 这两个都可以为空：模型确实拿不准时不选是合法策略（选错扣分、漏选只是
    # 不得分），强制它选反而会压低分数。这跟 ExtractedInfo 的情况不同。
    pathogenesis: list[str] = Field(default_factory=list)
    syndrome: list[str] = Field(default_factory=list)


class CaseSummary(BaseModel):
    summary: str = Field(min_length=1)


# ---------- 一条记录的作答 ----------

@dataclass
class SdtAnswer:
    record_id: str
    clinical_information: list[str] = field(default_factory=list)
    pathogenesis_answers: list[str] = field(default_factory=list)
    syndrome_answers: list[str] = field(default_factory=list)
    summary: str = ""
    # 非 None 表示这条被安全否决拦下，四个字段都空、这条记录得 0 分
    safety_rejected: str | None = None
    llm_calls: int = 0

    def to_line(self) -> str:
        return to_line(self.record_id, self.clinical_information,
                       self.pathogenesis_answers, self.syndrome_answers, self.summary)


def _format_options(options: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in sorted(options.items()))


def filter_valid_options(chosen: list[str], options: dict[str, str]) -> list[str]:
    """只保留确实存在的选项字母，并去重、按字母排序。

    丢掉不存在的字母不是在改答案：官方的 score_proportional 里，一个不在
    正确集合里的字母只会增加 wrong_count、拉低分母，一个根本不存在的选项
    永远不可能是对的。丢掉它是格式修正，不是挑答案。**有效字母一个都不丢。**
    """

    valid: set[str] = set()
    text_to_key = {v: k for k, v in options.items()}
    for raw in chosen:
        if not raw:
            continue
        # 模型常把几个字母塞进一个元素（"A;B"、"A、B"、"AB"）或者回选项文本而不是字母。
        # 原来只认"整个元素恰好等于一个字母"，这些情况会被静默丢成空、该题记 0 分。
        for piece in re.split(r"[;；,，、/\s]+", raw.strip()):
            if not piece:
                continue
            key = piece.split(":")[0].split("：")[0].strip().upper()
            if key in options:
                valid.add(key)
            elif piece in text_to_key:
                valid.add(text_to_key[piece])
            elif re.fullmatch(r"[A-Za-z]{2,}", piece):
                valid.update(ch for ch in piece.upper() if ch in options)
    return sorted(valid)


# ---------- 两个 Solver ----------

class BaselineSolver:
    """对照组：同一个模型、同样的三个输出头，但不注入任何证素分析。

    它跑出来的分是「这个模型裸做 SDT 能拿多少」，ChainSolver 减去它才是
    结构化推理链本身的贡献。没有这一组，"我们拿了 X 分"这句话没有基准。
    """

    name = "baseline"

    def reasoning_block(self, record: SdtRecord) -> str:
        return ""

    def solve(self, record: SdtRecord, ignore_safety_veto: bool | None = None) -> SdtAnswer:
        """ignore_safety_veto=None 时读环境变量 EVAL_MODE（默认关），显式传布尔值
        优先——判定实现只有 core.safety.safety_bypassed 一处，SDT 这条链路和
        core.chain.consult 那条共用它，不各写一套。

        默认值从 False 改成 None 是刻意的：留 False 的话环境变量永远被覆盖成
        "不旁路"，EVAL_MODE 对 SDT 就是死的。行为上没有回归——不传参时未设
        EVAL_MODE 仍然是不旁路，跟改之前一致。"""
        answer = SdtAnswer(record_id=record.record_id)

        # check_safety 照跑，只是命中后要不要中止由 safety_bypassed 决定
        reject = None if safety_bypassed(ignore_safety_veto) else check_safety([record.clinical_data])
        if reject is not None:
            answer.safety_rejected = reject
            return answer

        block = self.reasoning_block(record)
        calls = 0

        extract = get_llm().generate(
            system=render(load_prompt("sdt_extract")["system"],
                          clinical_data=record.clinical_data),
            user="", schema=ExtractedInfo)
        calls += 1
        answer.clinical_information = extract.items

        selected = get_llm().generate(
            system=render(load_prompt("sdt_select")["system"],
                          clinical_data=record.clinical_data,
                          reasoning_block=block,
                          pathogenesis_options=_format_options(record.pathogenesis_options),
                          syndrome_options=_format_options(record.syndrome_options)),
            user="", schema=SelectedOptions)
        calls += 1
        answer.pathogenesis_answers = filter_valid_options(
            selected.pathogenesis, record.pathogenesis_options)
        answer.syndrome_answers = filter_valid_options(
            selected.syndrome, record.syndrome_options)

        summary = get_llm().generate(
            system=render(load_prompt("sdt_summary")["system"],
                          clinical_data=record.clinical_data, reasoning_block=block),
            user="", schema=CaseSummary)
        calls += 1
        answer.summary = summary.summary

        answer.llm_calls = calls + self.extra_calls
        return answer

    extra_calls = 0


class ChainSolver(BaselineSolver):
    """实验组：先跑本项目的 S1（症状标准化）+ S2（证素推断），把证素分析
    注入 Task2/3/4 的提示词，其余完全一致。

    只改这一个变量，跟 BaselineSolver 的差值才归因得清楚。**Task1 不注入**：
    它要的是原文片段，喂归一化后的术语只会把它带偏——这也是我们的链跟 SDT
    任务形状不吻合的那一处。
    """

    name = "chain"
    extra_calls = 2  # S1 + S2

    def __init__(self):
        self._cache: dict[str, str] = {}

    def reasoning_block(self, record: SdtRecord) -> str:
        if record.record_id in self._cache:
            return self._cache[record.record_id]
        from core.chain import _format_elements_summary, infer_elements, normalize

        s1 = normalize(record.clinical_data)
        s2 = infer_elements(s1)
        block = (
            "以下是对这份医案先做的结构化分析，供参考（可能不完整，与原文冲突时以原文为准）：\n"
            f"  标准化症状：{'；'.join(s1.symptoms) or '（无）'}\n"
            f"  舌：{s1.tongue or '未记'}　脉：{s1.pulse or '未记'}\n"
            f"  证素：{_format_elements_summary(s2)}\n"
        )
        self._cache[record.record_id] = block
        return block


SOLVERS = {"baseline": BaselineSolver, "chain": ChainSolver}
