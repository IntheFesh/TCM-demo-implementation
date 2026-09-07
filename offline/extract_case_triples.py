"""X3：从 cases.json 里每一诊的原文抽取三元组，写入 data/case_triples.jsonl，
一行一条 `{case_id, physician, s, p, o, source_span}`——这个格式不是这里定的，
是 core/tools.py 的 `query_case_graph()` 已经在读的既有契约（那份代码和它的
测试 tests/test_tools.py 是先写好的），这里只是把契约要求的文件真的产出来。

**这是一次真实的 LLM 抽取，不是确定性转换。** CaseRecord 的 syndrome/
treatment_principle/formula/herbs 这几个字段虽然 S0 阶段已经抽过一次，但那是
"这一诊的证候是什么"这种粗粒度归纳，不是"哪句原文具体证明了哪条关系"这种
细粒度、带精确出处的标注——后者是 query_case_graph 的 source_span 承诺要给
的东西（"这条结论出自医案哪一句"），S0 的字段做不到这个粒度，必须重新过一遍
原文。prompts/v1/s5_extract_triples.yaml 就是这次抽取用的 prompt。

**source_span 的防幻觉核验在这里做，不在 pydantic schema 里。** schema
（core.schemas.CaseTripleItem）只能校验"这个字段非空"，校验不了"这段文字
是不是真的在原文里"——那需要拿模型返回的 source_span 去原文里做逐字查找。
查不到就整条三元组丢弃，不写进输出文件；丢了多少条要在统计里如实报出来，
不能静默吞掉（吞了的话，"三元组文件的 source_span 都可核验"这句话就是假的）。

用法：
    python -m offline.extract_case_triples
    python -m offline.extract_case_triples --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.llm import get_llm, load_prompt, render
from core.schemas import CaseRecord, CaseTripleExtraction, CaseTripleRecord

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_OUT_PATH = ROOT / "data" / "case_triples.jsonl"


def _source_text(case: CaseRecord) -> str | None:
    """喂给模型、也是 source_span 核验基准的原文。优先诊次原文片段（跟"这一诊
    具体证明了什么"颗粒度一致）；缺失时退到整段原文——仍然是这条医案的真实
    文字，只是核验时可核对的范围更大。raw 也拿不到（不该发生，raw 是必填
    字段）才返回 None，调用方据此跳过，不编造。"""
    if case.raw_excerpt:
        return case.raw_excerpt
    return case.raw or None


def extract_case(case: CaseRecord) -> tuple[list[CaseTripleRecord], int]:
    """对一条医案调一次 S5，返回 (核验通过的三元组记录, 核验没过被丢弃的条数)。"""
    text = _source_text(case)
    if text is None:
        return [], 0

    prompt = load_prompt("s5_extract_triples")
    system = render(prompt["system"], raw_text=text)
    extraction = get_llm().generate(
        system=system, user="", schema=CaseTripleExtraction
    )

    records = []
    n_rejected = 0
    for item in extraction.triples:
        if item.source_span not in text:
            n_rejected += 1
            continue
        records.append(CaseTripleRecord(
            case_id=case.case_id, physician=case.physician,
            s=item.s, p=item.p, o=item.o, source_span=item.source_span,
        ))
    return records, n_rejected


def extract_all(cases: list[CaseRecord]) -> tuple[list[CaseTripleRecord], dict]:
    all_records: list[CaseTripleRecord] = []
    stats = {
        "cases": len(cases), "cases_no_text": 0, "cases_no_triples": 0,
        "triples_extracted": 0, "triples_rejected_span_not_found": 0,
        "llm_calls": 0,
    }
    for case in cases:
        text = _source_text(case)
        if text is None:
            stats["cases_no_text"] += 1
            continue
        records, n_rejected = extract_case(case)
        stats["llm_calls"] += 1
        stats["triples_rejected_span_not_found"] += n_rejected
        if not records:
            stats["cases_no_triples"] += 1
        stats["triples_extracted"] += len(records)
        all_records.extend(records)
    return all_records, stats


def _estimate_call_count(cases_path: Path, limit: int | None) -> int:
    if not cases_path.exists():
        return 0
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    if limit is not None:
        raw = raw[:limit]
    return sum(1 for c in raw if c.get("raw_excerpt") or c.get("raw"))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="X3：从 cases.json 抽医案三元组（真实 LLM 调用）")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条医案，调试/控成本用")
    ap.add_argument("--dry-run", action="store_true", help="只打印预估调用数，不真的调模型")
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.cases_path}。先跑 offline/extract_cases.py 生成 cases.json。"
        )

    if args.dry_run:
        n_calls = _estimate_call_count(args.cases_path, args.limit)
        print(f"--dry-run：预估调用数 = {n_calls}（每条有原文的医案 1 次 S5 调用），不真的调模型")
        return

    raw = json.loads(args.cases_path.read_text(encoding="utf-8"))
    if args.limit is not None:
        raw = raw[: args.limit]
    cases = [CaseRecord.model_validate(r) for r in raw]

    records, stats = extract_all(cases)

    print(f"读入 {stats['cases']} 条医案（{stats['cases_no_text']} 条无原文可用，已跳过）")
    print(f"S5 调用 {stats['llm_calls']} 次，抽出 {stats['triples_extracted']} 条三元组通过核验，"
          f"{stats['triples_rejected_span_not_found']} 条因 source_span 在原文里找不到被丢弃")
    print(f"{stats['cases_no_triples']} 条医案调了模型但一条三元组都没抽出/全部核验未过")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
