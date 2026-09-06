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

import re
import time

from core.elements import ELEMENTS, LOCATIONS, NATURES
from core.llm import get_llm, load_prompt, render
from core.physicians import PHYSICIANS
from core.retrieval import get_retriever
from core.safety import check_safety
from core.schemas import CaseRecord, S1Normalize, S2Elements, S3Syndrome

# 检索相似度下限。实测正常匹配在 0.85-0.90，低于 0.70 基本是"库里没有相关案子"，
# 此时给空列表比塞三条不相关的更诚实——S3 prompt 里已有"参考医案差异较大时
# 如实说明"的处理分支。
MIN_RETRIEVAL_SCORE = 0.70

# 残差辨证触发阈值：未解释症状 >=2 条 且 占比 >=30% 时，用这些症状再跑一轮，
# 看能不能构成兼夹证。S2 共享之后未解释症状是全局唯一一份，所以残差也只跑一次，
# 结果两位医家共用——这比原方案（每位医家各跑一轮）省一半调用，也更一致。
RESIDUAL_MIN_COUNT = 2
RESIDUAL_THRESHOLD = 0.30
RESIDUAL_MAX_ROUNDS = 1


_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_DOSE_RE = re.compile(r"[一二三四五六七八九十百半\d.]+(?:钱|两|分|克|g|枚|片|条|支|具|个|茶匙|杯)\s*$")

# 炮制前缀/后缀：同一味药在不同医家笔下写法不同（广皮=陈皮、炙草=炙甘草），
# 不归一的话药物集合比对会把同一味药算成两味，Jaccard 被系统性推高——
# 实测出现过两边实际用药大量重合、Jaccard 却算成 1.0 的情况。
_HERB_AFFIX = re.compile(r"^(炒|焦|生|制|炙|姜|酒|醋|盐|煨|煅|蜜|清|净|广|川|云|北|南|东|西)+")
_HERB_SUFFIX = re.compile(r"(汁|炭|末|粉|片|块|皮尖)$")

HERB_ALIASES: dict[str, str] = {
    "广皮": "陈皮", "橘皮": "陈皮", "新会皮": "陈皮",
    "炙草": "甘草", "炙甘草": "甘草", "生甘草": "甘草", "粉甘草": "甘草",
    "云苓": "茯苓", "白苓": "茯苓", "茯苓块": "茯苓", "茯苓皮": "茯苓", "赤苓": "茯苓",
    "川连": "黄连", "真云连": "黄连", "山连": "黄连", "雅连": "黄连",
    "北沙参": "沙参", "南沙参": "沙参",
    "白扁豆": "扁豆", "生扁豆": "扁豆",
    "半夏曲": "半夏", "姜半夏": "半夏", "制半夏": "半夏", "法半夏": "半夏",
    "小枳实": "枳实", "淡吴萸": "吴茱萸", "吴萸": "吴茱萸",
    "老浓朴": "厚朴", "浓朴": "厚朴",
    "焦六曲": "神曲", "六曲": "神曲", "建曲": "神曲",
    "焦山楂": "山楂", "生山楂": "山楂",
    "潞党参": "党参", "台党参": "党参",
    "冬术": "白术", "於术": "白术",
}


def normalize_herb(herb: str) -> str:
    """把药名归一到可比对的形式：剥括号注释、剥剂量、查别名表、剥炮制前后缀。

    顺序重要：先剥括号（"旋覆花二钱（包煎）"的括号在剂量之后，
    不先剥掉的话 $ 锚点匹配不到剂量），再剥剂量，最后才做别名归一。
    """
    s = _PAREN_RE.sub("", herb).strip()
    s = _DOSE_RE.sub("", s).strip()
    if not s:
        return ""
    if s in HERB_ALIASES:
        return HERB_ALIASES[s]
    stripped = _HERB_SUFFIX.sub("", _HERB_AFFIX.sub("", s)).strip()
    if stripped in HERB_ALIASES:
        return HERB_ALIASES[stripped]
    # 剥完只剩一个字多半剥过头了（"生姜"->"姜"），保留原形
    return stripped if len(stripped) >= 2 else s


def strip_dose(herb: str) -> str:
    """保留旧名，内部走 normalize_herb。"""
    return normalize_herb(herb)


def _format_case_line(case: CaseRecord) -> str:
    """把一个参考医案压缩成一行，喂给 S3 prompt。"""
    symptoms = "；".join(case.symptoms) if case.symptoms else "无"
    herbs = "、".join(case.herbs) if case.herbs else "无"
    vi = case.visit_index or 0
    visit_desc = "初诊" if vi == 0 else f"第{vi + 1}诊"
    fields = [
        f"id={case.case_id}",
        f"诊次={visit_desc}",
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


def infer_elements(s1: S1Normalize) -> S2Elements:
    """S2 证素推断。全局只跑一次，所有医家共用——理由同 S1：

    s2_elements.yaml 的占位符里没有 $name，模型根本不知道自己在为哪位医家推断，
    temperature=0 下对每位医家各跑一次只会得到几乎相同的结果，白花调用。
    设计上医家条件化发生在 S3（通过检索到的该医家医案），S2 是客观的证素抽取。
    图上证素层本来也是所有医家共享同一批节点（api/main.py 的 elem:: 去重）。
    """
    symptoms_text = "；".join(s1.symptoms)
    s2_prompt = load_prompt("s2_elements")
    s2_system = render(
        s2_prompt["system"],
        elements=(
            f"病位证素（kind 填 location）：{'、'.join(LOCATIONS)}\n"
            f"  病性证素（kind 填 nature）：{'、'.join(NATURES)}"
        ),
        symptoms=symptoms_text,
        tongue=s1.tongue or "未记",
        pulse=s1.pulse or "未记",
    )
    return get_llm().generate(system=s2_system, user="", schema=S2Elements)


def run_physician(
    s1: S1Normalize, s2: S2Elements, physician: str, physician_name: str
) -> dict:
    symptoms_text = "；".join(s1.symptoms)

    # 检索该医家 top-3 医案
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"
    hits = get_retriever().search(query, physician, k=3, min_score=MIN_RETRIEVAL_SCORE)
    # refs 要给前端证据链侧栏用：只给 (id, score) 的话，用户看到
    # ye_tianshi-0031-p6-0 完全不知道那是什么医案，"可追溯"这个卖点就断在这里。
    refs = [
        {
            "case_id": case.case_id,
            "score": round(score, 3),
            "visit_index": case.visit_index or 0,
            "visit_label": "初诊" if not case.visit_index else f"第{case.visit_index + 1}诊",
            "symptoms": case.symptoms or [],
            "tongue": case.tongue,
            "pulse": case.pulse,
            "syndrome": case.syndrome,
            "treatment_principle": case.treatment_principle,
            "formula": case.formula,
            "herbs": case.herbs or [],
            # 该诊次对应的原文片段（不是整段粗段）
            "excerpt": case.raw_excerpt,
        }
        for case, score in hits
    ]
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

    ref_ids = {r["case_id"] for r in refs}
    hallucinated = [cid for cid in s3.cited_case_ids if cid not in ref_ids]

    return {
        "physician": physician,
        "physician_name": physician_name,
        "s2": s2,
        "s3": s3,
        "refs": refs,
        "hallucinated": hallucinated,
    }


def _build_manifest(elapsed_ms: int, llm_calls: int) -> dict:
    """跑这一次用的是什么模型、什么 prompt 版本、几次调用。
    竞赛材料里写"我们的结果"时，这几行元数据就是全部的可信度来源。"""
    import hashlib
    import os
    from pathlib import Path as _P

    cases_sha = None
    cp = _P(__file__).resolve().parent.parent / "cases.json"
    if cp.exists():
        cases_sha = hashlib.sha256(cp.read_bytes()).hexdigest()[:12]
    return {
        "model": os.getenv("LLM_MODEL", "unknown"),
        "prompt_version": "v1",
        "cases_sha256": cases_sha,
        "elapsed_ms": elapsed_ms,
        "llm_calls": llm_calls,
    }


def run_residual(s1: S1Normalize, s2: S2Elements) -> dict | None:
    """残差辨证：拿 S2 明确列出的未解释症状再跑一轮证素推断。

    这是"系统知道自己哪里没说清楚"的落点——不做的话 unexplained_symptoms
    只是个统计数字，界面上看不出系统承认了什么。
    """
    # 不能只信 unexplained_symptoms 字段——模型经常漏填它，
    # 实测有症状明明没被任何证素引用、该字段却是空的。
    # 取并集：字段声明的 + 实际没被任何 supporting_symptoms 提到的。
    declared = set(s2.unexplained_symptoms or [])
    referenced = {
        sym for hit in s2.elements for sym in hit.supporting_symptoms
    }
    actual = {s for s in s1.symptoms if s not in referenced}
    unexplained = sorted(declared | actual, key=lambda x: s1.symptoms.index(x) if x in s1.symptoms else 999)
    total = len(s1.symptoms) or 1
    if len(unexplained) < RESIDUAL_MIN_COUNT:
        return None
    if len(unexplained) / total < RESIDUAL_THRESHOLD:
        return None

    residual_s1 = S1Normalize(
        symptoms=unexplained, tongue=s1.tongue, pulse=s1.pulse, unmapped=[]
    )
    s2r = infer_elements(residual_s1)
    newly = [
        sym
        for hit in s2r.elements
        for sym in hit.supporting_symptoms
        if sym in unexplained
    ]
    return {
        "triggered": True,
        "input_symptoms": unexplained,
        "s2": s2r,
        "newly_explained": sorted(set(newly)),
        "still_unexplained": [s for s in unexplained if s not in set(newly)],
        "coverage_before": round((total - len(unexplained)) / total, 3),
        "coverage_after": round((total - len(unexplained) + len(set(newly))) / total, 3),
    }


def consult(complaint: str) -> dict:
    _t0 = time.time()
    s1 = normalize(complaint)

    # 安全否决必须在这里、S2 开始之前——命中就直接返回，S2/S3 一次都不调用，
    # 不产出任何方药。不要把这道检查挪到 run_physician 内部或结果的 note 字段。
    # 三处都要查：S1 可能把"最近吐了两次血"这类病史陈述归进 unmapped
    # （s1_normalize.yaml 明确要求含糊的病史表述放 unmapped），只查 symptoms 会漏。
    reject_reason = check_safety([complaint] + s1.symptoms + s1.unmapped)
    if reject_reason is not None:
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": True,
            "reject_reason": reject_reason,
            "manifest": _build_manifest(int((time.time() - _t0) * 1000), 1),
        }

    s2 = infer_elements(s1)
    residual = run_residual(s1, s2)

    # 证素层为空 = 结构化推理没有落点。此时若继续跑 S3，模型会绕开证素
    # 直接"看主诉猜证型"（实测「胸闷气短」这类信息量过低的主诉，S2 返回空证素，
    # S3 仍给出完整证型和方药，推理过程里自己写着"证素分析未给出明确结论，
    # 然从症状推之"）。那样 S1->S2->S3 的分步设计就退化成了单步问答，
    # 而且输出的方药没有任何可追溯的依据。宁可如实说信息不足。
    explained = {sym for hit in s2.elements for sym in hit.supporting_symptoms}
    residual_explained = set((residual or {}).get("newly_explained") or [])
    coverage = len(explained | residual_explained) / (len(s1.symptoms) or 1)

    if not s2.elements and not (residual and residual["s2"].elements):
        return {
            "s1": s1,
            "results": [],
            "divergence": None,
            "rejected": False,
            "reject_reason": None,
            "s2": s2,
            "residual": residual,
            "insufficient": True,
            "insufficient_reason": (
                "现有症状不足以推断证素，无法进行有依据的辨证。"
                "请补充更多信息：起病与加重缓解的诱因、疼痛或不适的性质与部位、"
                "饮食与二便情况、寒热喜恶、舌象与脉象。"
            ),
            "coverage": round(coverage, 3),
            "manifest": _build_manifest(
                int((time.time() - _t0) * 1000), 2 + (1 if residual else 0)
            ),
        }
    results = [
        run_physician(s1, s2, physician, info["name"])
        for physician, info in PHYSICIANS.items()
    ]

    syndromes = {r["physician"]: r["s3"].syndrome for r in results}
    values = list(syndromes.values())
    same = len(set(values)) <= 1

    # 字符串比对会把"脾胃气虚，运化失健"和"脾虚湿困，中焦不运"判为分歧，
    # 哪怕两者治法、方剂一字不差（实测 10 条主诉分歧率 9/9，指标无区分度）。
    # 改用药物集合的 Jaccard 距离作为主指标：用药是医家风格最实在的落点，
    # 而证型命名的差异很大程度上只是措辞。
    herb_sets = [
        {h for h in (normalize_herb(x) for x in (r["s3"].herbs or [])) if h}
        for r in results
    ]
    if len(herb_sets) >= 2 and any(herb_sets):
        inter = set.intersection(*herb_sets)
        union = set.union(*herb_sets)
        herb_jaccard = 1.0 - (len(inter) / len(union)) if union else 0.0
        shared_herbs = sorted(inter)
    else:
        herb_jaccard = None
        shared_herbs = []

    tp_same = len(set(r["s3"].treatment_principle for r in results)) <= 1

    divergence = {
        "same": same,
        "method": "exact_string_match",
        # 0=用药完全一致，1=毫无重叠
        "herb_jaccard": round(herb_jaccard, 3) if herb_jaccard is not None else None,
        "shared_herbs": shared_herbs,
        "treatment_principle_same": tp_same,
    }

    return {
        "s1": s1,
        "results": results,
        "divergence": divergence,
        "rejected": False,
        "reject_reason": None,
        "s2": s2,
        "residual": residual,
        "insufficient": False,
        "insufficient_reason": None,
        "coverage": round(coverage, 3),
        # S1 一次 + S2 一次 + 每位医家 S3 一次
        "manifest": _build_manifest(
            int((time.time() - _t0) * 1000), 2 + len(results) + (1 if residual else 0)
        ),
    }


if __name__ == "__main__":
    from pathlib import Path

    queries_path = Path(__file__).resolve().parent.parent / "tests" / "queries.txt"
    queries = [
        q.strip() for q in queries_path.read_text(encoding="utf-8").splitlines() if q.strip()
    ]

    n_divergent = 0
    n_hallucinated = 0
    n_rejected = 0
    durations = []

    for i, complaint in enumerate(queries, 1):
        t0 = time.time()
        outcome = consult(complaint)
        elapsed = time.time() - t0
        durations.append(elapsed)

        print(f"\n[{i}] 主诉：{complaint}")

        if outcome["rejected"]:
            n_rejected += 1
            print(f"  [安全拦截] {outcome['reject_reason']}")
            print(f"  耗时：{elapsed:.1f}s")
            continue

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
        hj = div.get("herb_jaccard")
        tp = "治法一致" if div.get("treatment_principle_same") else "治法不同"
        shared = div.get("shared_herbs") or []
        print(
            f"  分歧：证型{'不同' if div['same'] is False else '相同'}｜{tp}｜"
            f"药物Jaccard={hj if hj is not None else 'NA'}"
            f"｜共用药={('、'.join(shared[:6]) or '无')}"
        )
        if not div["same"]:
            n_divergent += 1
        print(f"  耗时：{elapsed:.1f}s")

    print("\n=== 统计 ===")
    print(f"安全拦截例数：{n_rejected}/{len(queries)}")
    print(f"分歧例数：{n_divergent}/{len(queries) - n_rejected}（分母排除被拦截的例数）")
    print(f"幻觉例数：{n_hallucinated}/{len(queries) - n_rejected}（分母排除被拦截的例数）")
    if durations:
        print(f"平均耗时：{sum(durations) / len(durations):.1f}s")
