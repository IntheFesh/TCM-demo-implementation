"""结构化推理链（SRC）：症状标准化 -> 证素推断 -> 检索 -> 证候辨析。全项目核心。

数据流（严格按此，不要改）：

    S1 症状标准化：全局只跑一次，两位医家共用结果
      |
      对每位医家（顺序执行，不用 asyncio）：
        S2 证素推断（注入 $elements）
        检索该医家 top-3 医案
        S3 证候+治法+方（注入 $name 和参考医案）

S1 必须只跑一次：如果对每位医家各跑一次，两次输出的症状列表会不同，
后面构图时症状节点 id 对不上，边会指向不存在的节点。
"""
from __future__ import annotations

import time

from core.elements import ELEMENTS
from core.llm import get_llm, load_prompt, render
from core.physicians import PHYSICIANS
from core.retrieval import get_retriever
from core.schemas import CaseRecord, S1Normalize, S2Elements, S3Syndrome


def _format_case_line(case: CaseRecord) -> str:
    """把一个参考医案压缩成一行，喂给 S3 prompt。"""
    symptoms = "；".join(case.symptoms) if case.symptoms else "无"
    herbs = "、".join(case.herbs) if case.herbs else "无"
    fields = [
        f"id={case.case_id}",
        f"症状={symptoms}",
        f"舌={case.tongue or '未记'}",
        f"脉={case.pulse or '未记'}",
        f"证={case.syndrome or '未记'}",
        f"病机={case.pathogenesis or '未记'}",
        f"治法={case.treatment_principle or '未记'}",
        f"方={case.formula or '未记'}",
        f"药={herbs}",
    ]
    return "；".join(fields)


def _format_elements_summary(s2: S2Elements) -> str:
    """把 S2 的证素命中列表压缩成一段文本，喂给 S3 prompt。"""
    if not s2.elements:
        return "（未推断出明确证素）"
    lines = []
    for hit in s2.elements:
        supporting = "、".join(hit.supporting_symptoms)
        lines.append(
            f"{hit.element}（{hit.kind}，置信度{hit.confidence}，依据：{supporting}）"
        )
    return "；".join(lines)


def normalize(complaint: str) -> S1Normalize:
    prompt = load_prompt("s1_normalize")
    system = render(prompt["system"], complaint=complaint)
    return get_llm().generate(system=system, user="", schema=S1Normalize)


def run_physician(s1: S1Normalize, physician: str, physician_name: str) -> dict:
    symptoms_text = "；".join(s1.symptoms)

    # S2 证素推断
    s2_prompt = load_prompt("s2_elements")
    s2_system = render(
        s2_prompt["system"],
        elements="、".join(ELEMENTS),
        symptoms=symptoms_text,
        tongue=s1.tongue or "未记",
        pulse=s1.pulse or "未记",
    )
    s2: S2Elements = get_llm().generate(system=s2_system, user="", schema=S2Elements)

    # 检索该医家 top-3 医案
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"
    hits = get_retriever().search(query, physician, k=3)
    refs = [(case.case_id, round(score, 3)) for case, score in hits]
    refs_text = "\n".join(_format_case_line(case) for case, _ in hits) or "（无可用参考医案）"

    # S3 证候+治法+方
    s3_prompt = load_prompt("s3_syndrome")
    s3_system = render(
        s3_prompt["system"],
        name=physician_name,
        elements_summary=_format_elements_summary(s2),
        symptoms=symptoms_text,
        refs=refs_text,
    )
    s3: S3Syndrome = get_llm().generate(system=s3_system, user="", schema=S3Syndrome)

    ref_ids = {case_id for case_id, _ in refs}
    hallucinated = [cid for cid in s3.cited_case_ids if cid not in ref_ids]

    return {
        "physician": physician,
        "physician_name": physician_name,
        "s2": s2,
        "s3": s3,
        "refs": refs,
        "hallucinated": hallucinated,
    }


def consult(complaint: str) -> dict:
    s1 = normalize(complaint)

    results = [
        run_physician(s1, physician, info["name"]) for physician, info in PHYSICIANS.items()
    ]

    syndromes = {r["physician"]: r["s3"].syndrome for r in results}
    values = list(syndromes.values())
    same = len(set(values)) <= 1
    divergence = {
        "same": same,
        # 目前是按证型名称精确比对的粗判，正式版会换成 JS 散度等语义层面的分歧度量。
        "method": "exact_string_match",
    }

    return {"s1": s1, "results": results, "divergence": divergence}


if __name__ == "__main__":
    from pathlib import Path

    queries_path = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
    queries = [
        q.strip() for q in queries_path.read_text(encoding="utf-8").splitlines() if q.strip()
    ]

    n_divergent = 0
    n_hallucinated = 0
    durations = []

    for i, complaint in enumerate(queries, 1):
        t0 = time.time()
        outcome = consult(complaint)
        elapsed = time.time() - t0
        durations.append(elapsed)

        print(f"\n[{i}] 主诉：{complaint}")
        for r in outcome["results"]:
            s3 = r["s3"]
            print(
                f"  {r['physician_name']}：证型={s3.syndrome}  "
                f"治法={s3.treatment_principle}  方={s3.formula}  药={'、'.join(s3.herbs)}"
            )
            if r["hallucinated"]:
                n_hallucinated += 1
                print(f"    [幻觉] 引用了检索结果之外的医案 id：{r['hallucinated']}")

        div = outcome["divergence"]
        print(f"  分歧：{div['same'] is False}（{div['method']}）")
        if not div["same"]:
            n_divergent += 1
        print(f"  耗时：{elapsed:.1f}s")

    print("\n=== 统计 ===")
    print(f"分歧例数：{n_divergent}/{len(queries)}")
    print(f"幻觉例数：{n_hallucinated}/{len(queries)}")
    if durations:
        print(f"平均耗时：{sum(durations) / len(durations):.1f}s")
