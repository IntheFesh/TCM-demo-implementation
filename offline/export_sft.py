"""从 cases.json 派生 SFT 训练样本（alpaca 格式），写出 sft.jsonl。

现在数据量不够训练，这一步只是把管道建好。

用法：
    python -m offline.export_sft
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from core.schemas import CaseRecord

CASES_PATH = Path(__file__).resolve().parent.parent / "cases.json"
OUT_PATH = Path(__file__).resolve().parent.parent / "sft.jsonl"


def load_cases() -> list[CaseRecord]:
    with CASES_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return [CaseRecord.model_validate(r) for r in raw]


def filter_public_domain(cases: list[CaseRecord]) -> list[CaseRecord]:
    """版权合规的代码级强制点：现代出版书籍将来接入时若标了 copyrighted，
    这里必须把它挡在训练集之外。断言而不是静默过滤，是为了让违规数据
    在开发期就能被立刻发现，而不是悄悄漏进 sft.jsonl。"""
    kept = [c for c in cases if c.copyright_status == "public_domain"]
    assert all(c.copyright_status == "public_domain" for c in kept)
    return kept


def _sample(task: str, instruction: str, input_text: str, output: str, case: CaseRecord) -> dict:
    return {
        "instruction": instruction,
        "input": input_text,
        "output": output,
        "meta": {
            "physician_id": case.physician,
            "case_id": case.case_id,
            "task": task,
            "copyright_status": case.copyright_status,
        },
    }


def to_samples(case: CaseRecord) -> list[dict]:
    samples: list[dict] = []

    if case.syndrome:
        tongue = case.tongue or "未记"
        pulse = case.pulse or "未记"
        symptoms = "；".join(case.symptoms) if case.symptoms else "（无记录症状）"
        output = case.syndrome
        if case.pathogenesis:
            output = f"{case.syndrome}。病机：{case.pathogenesis}"
        samples.append(
            _sample(
                task="T1_辨证",
                instruction="根据以下症状、舌象、脉象，给出中医证型与病机。",
                input_text=f"症状：{symptoms}\n舌象：{tongue}\n脉象：{pulse}",
                output=output,
                case=case,
            )
        )

    if case.treatment_principle and case.syndrome:
        samples.append(
            _sample(
                task="T2_立法",
                instruction="根据以下中医证型，给出相应的治法。",
                input_text=f"证型：{case.syndrome}",
                output=case.treatment_principle,
                case=case,
            )
        )

    if case.herbs and case.syndrome and case.treatment_principle:
        formula_line = f"方名：{case.formula}\n" if case.formula else ""
        samples.append(
            _sample(
                task="T3_处方",
                instruction="根据以下中医证型与治法，给出方名（如有）与药物组成。",
                input_text=f"证型：{case.syndrome}\n治法：{case.treatment_principle}",
                output=f"{formula_line}药物：{'、'.join(case.herbs)}",
                case=case,
            )
        )

    # T7 的输入必须是**这一诊**对应的原文。raw 是整个粗段（同段多个病人、多诊共享、内容
    # 完全相同），拿它当输入而输出只有一个病人一诊的字段，会导出 N 条"同一输入、互相
    # 矛盾的输出"的训练样本，而且输出刻意省略输入里明明存在的信息——跟指令里
    # "原文中没有出现的信息填 null、禁止推断"自相矛盾。有 raw_excerpt 用 raw_excerpt；
    # 没有的话只在能确定整段就是这一诊（段内第 0 个病人的初诊）时才退回 raw。
    t7_input = case.raw_excerpt
    # 没有 -p 后缀的 case_group_id 是旧格式/单病人记录，整段就是这一个病人
    first_patient = case.case_group_id.endswith("-p0") or "-p" not in case.case_group_id
    if not t7_input and case.raw and (case.visit_index or 0) == 0 and first_patient:
        t7_input = case.raw
    if t7_input:
        structured = {
            "symptoms": case.symptoms,
            "tongue": case.tongue,
            "pulse": case.pulse,
            "syndrome": case.syndrome,
            "pathogenesis": case.pathogenesis,
            "treatment_principle": case.treatment_principle,
            "formula": case.formula,
            "herbs": case.herbs,
        }
        samples.append(
            _sample(
                task="T7_抽取",
                instruction="把下面这条古籍医案原文抽取成结构化字段（JSON），"
                "原文中没有出现的信息填 null 或空列表，禁止推断。",
                input_text=t7_input,
                output=json.dumps(structured, ensure_ascii=False),
                case=case,
            )
        )

    return samples


def main() -> None:
    cases = load_cases()
    cases = filter_public_domain(cases)

    samples: list[dict] = []
    for case in cases:
        samples.extend(to_samples(case))

    with OUT_PATH.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    task_dist = Counter(s["meta"]["task"] for s in samples)
    physician_dist = Counter(s["meta"]["physician_id"] for s in samples)

    print(f"总样本数：{len(samples)}")
    print(f"按 task 分布：{dict(task_dist)}")
    print(f"按 physician 分布：{dict(physician_dist)}")
    print(f"已写出 {OUT_PATH}")


if __name__ == "__main__":
    main()
