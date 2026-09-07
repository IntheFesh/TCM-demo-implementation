"""X3：从 cases.json 拼出医案三元组，写入 data/case_triples.jsonl，给
offline/build_graph.py 的第二层图谱构建用（治法/方剂/药物/医家节点，
treated_by/realized_by/contains/practiced_by 边——schema.py 里早就声明了
这些类型，K1 阶段一直没人填，因为数据来源是医案，不是证候标准）。

**这是确定性转换，不是新的 LLM 抽取。** CaseRecord 的 syndrome/
treatment_principle/formula/herbs 四个字段在 S0（offline/extract_cases.py）
阶段已经从原文里抽取过一次了。再起一次 LLM 调用去重新抽取同样的信息，只会
多引入一次幻觉面和调用成本，不会产生新信息——把已经结构化的字段串成
(证候 -治法-> 治法 -方剂-> 方剂 -含药-> 药物) 这条链，加上 (医案 -医家-> 医家)，
纯本地计算就能做完，不需要模型。

三元组的 source_span 全部取该诊次的 raw_excerpt（S0 阶段已经切好的"这一诊
对应原文哪几行"）。同一诊次产出的所有三元组共享同一个 source_span，颗粒度
是"诊次"而不是"字句"——从原文里精确定位"哪句话具体在证明哪条三元组"需要
额外一次 LLM 定位调用，demo 阶段不做（CLAUDE.md「不要过度设计」）。

**证候节点用医案自己的原文措辞，不尝试对齐国标证候。** 节点 id 是
`syndrome::case::{原文文本}`，跟 offline/build_graph.py 里国标证候的
`syndrome::{code}` 是两个完全不同的命名空间，不会碰撞，也不假装两者已经对齐。
把案例证候文本匹配到国标证候节点是 offline/build_graph.py 的 attach_cases()
已经在做的事（比 syn_name_to_id 字面相等），这里再实现一遍就是同一个匹配问题
写第二套逻辑——CLAUDE.md 明令禁止。X3 只管拼医案自己内部这条链，不管这条链
要不要、怎么接到国标图谱上，那是以后的工作。

用法：
    python -m offline.extract_case_triples
    python -m offline.extract_case_triples --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.herbs import normalize_herb
from core.schemas import CaseRecord, CaseTriple

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_OUT_PATH = ROOT / "data" / "case_triples.jsonl"


def _source_span(case: CaseRecord) -> str | None:
    """三元组的出处引用。优先诊次原文片段；缺失时退到整段原文——仍然是真实
    引用，只是颗粒度更粗，不能因为拿不到精确片段就放弃 source_span（等价于
    放松了防幻觉约束）。raw 也拿不到（不该发生，CaseRecord.raw 是必填字段）
    才返回 None，调用方据此跳过这条记录，不编造。"""
    if case.raw_excerpt:
        return case.raw_excerpt
    return case.raw or None


def case_to_triples(case: CaseRecord) -> list[CaseTriple]:
    """把一条 CaseRecord 已经结构化的字段拼成三元组列表。字段链条缺哪一环，
    从哪一环断开——不编造缺失的中间节点。"""
    span = _source_span(case)
    if span is None:
        return []

    case_node = f"case::{case.case_id}"
    physician_node = f"physician::{case.physician}"

    triples = [
        CaseTriple(
            case_id=case.case_id, physician=case.physician,
            subject=case_node, subject_type="case",
            predicate="practiced_by",
            object=physician_node, object_type="physician",
            source_span=span,
        )
    ]

    syndrome_text = (case.syndrome or "").strip()
    if not syndrome_text:
        return triples

    syndrome_node = f"syndrome::case::{syndrome_text}"
    triples.append(CaseTriple(
        case_id=case.case_id, physician=case.physician,
        subject=case_node, subject_type="case",
        predicate="evidences",
        object=syndrome_node, object_type="syndrome",
        source_span=span,
    ))

    therapy_text = (case.treatment_principle or "").strip()
    if not therapy_text:
        return triples

    therapy_node = f"therapy::{therapy_text}"
    triples.append(CaseTriple(
        case_id=case.case_id, physician=case.physician,
        subject=syndrome_node, subject_type="syndrome",
        predicate="treated_by",
        object=therapy_node, object_type="therapy",
        source_span=span,
    ))

    formula_text = (case.formula or "").strip()
    if not formula_text:
        return triples

    formula_node = f"formula::{formula_text}"
    triples.append(CaseTriple(
        case_id=case.case_id, physician=case.physician,
        subject=therapy_node, subject_type="therapy",
        predicate="realized_by",
        object=formula_node, object_type="formula",
        source_span=span,
    ))

    for raw_herb in case.herbs:
        herb_name = normalize_herb(raw_herb)
        if not herb_name:
            continue
        triples.append(CaseTriple(
            case_id=case.case_id, physician=case.physician,
            subject=formula_node, subject_type="formula",
            predicate="contains",
            object=f"herb::{herb_name}", object_type="herb",
            source_span=span,
        ))

    return triples


def extract_all(cases: list[CaseRecord]) -> list[CaseTriple]:
    triples: list[CaseTriple] = []
    for case in cases:
        triples.extend(case_to_triples(case))
    return triples


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="X3：从 cases.json 拼医案三元组")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条医案，调试用")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.cases_path}。先跑 offline/extract_cases.py 生成 cases.json。"
        )

    raw = json.loads(args.cases_path.read_text(encoding="utf-8"))
    if args.limit is not None:
        raw = raw[: args.limit]
    cases = [CaseRecord.model_validate(r) for r in raw]

    triples = extract_all(cases)

    by_predicate: dict[str, int] = {}
    for t in triples:
        by_predicate[t.predicate] = by_predicate.get(t.predicate, 0) + 1
    n_with_syndrome = sum(1 for t in triples if t.predicate == "evidences")
    n_full_chain = sum(1 for t in triples if t.predicate == "contains")

    print(f"读入 {len(cases)} 条医案，拼出 {len(triples)} 条三元组")
    print(f"按 predicate 分布：{by_predicate}")
    print(f"有证候文本的医案：{n_with_syndrome}/{len(cases)}"
          f"（证候->治法->方剂->药物走到 contains 这一环的三元组数：{n_full_chain}，"
          "链条缺哪一环就从哪一环断开，不是每条 evidences 都能走到底）")

    if args.dry_run:
        print("--dry-run：不写文件")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for t in triples:
            f.write(t.model_dump_json() + "\n")
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
