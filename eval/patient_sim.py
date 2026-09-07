"""患者模拟器：给追问循环提供 ask_fn。两个实现，共用同一个可调用签名。

放 eval/ 不放 core/：LLM 版要真实调用，而 tests/ 必须保持"不需要网络、不需要
API key、秒级跑完"（CLAUDE.md 约定）。ScriptedPatient 虽然不调 LLM，也放在这里
——两种患者模拟器归一处，找的人不用猜在哪个目录。

签名跟 core.followup.AskFn 一致（问题字符串进，回答字符串或 None 出），所以
真人命令行、这两个模拟器、前端可以互换，换的是传给 consult 的那个函数，
不是追问循环本身。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from core.llm import get_llm, load_prompt, render


class PatientAnswer(BaseModel):
    # min_length=1：空回答会被追问循环当成 unknown，白烧一轮。宁可让 schema
    # 校验失败走重试，也不要收一个空字符串。
    answer: str = Field(min_length=1)


class ScriptedPatient:
    """按预设症状集合机械作答，不调 LLM。

    用于：离线跑通追问链路、以及在真实评测里当"完美患者"基线——它对每个问题
    都给出确定无误的是/否，所以它跑出来的追问效果是上界，真实患者只会更差。
    报告里引用追问收益时必须说明用的是哪个患者，两者的数不能混。
    """

    def __init__(self, present: list[str], absent: list[str] | None = None,
                 default: str = "没有"):
        self.present = list(present)
        self.absent = list(absent or [])
        self.default = default
        self.asked: list[str] = []

    def __call__(self, question: str) -> str:
        self.asked.append(question)
        for s in self.present:
            if s in question:
                return f"有，{s}"
        for s in self.absent:
            if s in question:
                return f"没有{s}"
        return self.default


class SimulatedPatient:
    """LLM 扮演的患者。profile 是只有它自己知道的病情，医生问什么答什么。

    每次追问 1 次调用——所以它只用于 eval/，不进 demo 的默认路径。
    """

    def __init__(self, profile: str):
        self.profile = profile
        self.history: list[tuple[str, str]] = []

    def _history_text(self) -> str:
        if not self.history:
            return "（还没问过）"
        return "\n".join(f"- 问：{q}　答：{a}" for q, a in self.history)

    def __call__(self, question: str) -> str:
        prompt = load_prompt("patient_sim")
        system = render(
            prompt["system"], profile=self.profile,
            history=self._history_text(), question=question,
        )
        out = get_llm().generate(system=system, user="", schema=PatientAnswer)
        self.history.append((question, out.answer))
        return out.answer
