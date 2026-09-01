"""遍历 data/{physician}/*.json（粗段，由 offline.split_cases 生成），用 LLM 判断每段
里有几个病人、每个病人几诊，展开成 CaseRecord，写出 cases.json。

用法：
    python -m offline.extract_cases            # 全量
    python -m offline.extract_cases --limit 3   # 先跑 3 个粗段试水
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.llm import get_llm, load_prompt, render
from core.schemas import CaseRecord, SegmentPatients

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = Path(__file__).resolve().parent.parent / "cases.json"
WARNINGS_PATH = Path(__file__).resolve().parent.parent / "extract_warnings.json"


def iter_segment_files(limit: int | None = None):
    """遍历 data/{physician}/*.json（粗段），按 physician 目录名、文件名排序，保证可复现。"""
    count = 0
    for physician_dir in sorted(p for p in DATA_ROOT.iterdir() if p.is_dir()):
        for seg_path in sorted(physician_dir.glob("*.json")):
            if limit is not None and count >= limit:
                return
            yield seg_path
            count += 1


def _format_hints(hints: list[dict], label: str) -> str:
    if not hints:
        return f"（{label}：无）"
    items = [f"{h['matched']!r}" for h in hints]
    return f"（{label}，仅供参考、不代表实际边界：{', '.join(items)}）"


def extract_segment(segment: dict) -> SegmentPatients:
    prompt = load_prompt("s0_extract_case")
    hints_text = (
        _format_hints(segment["head_hints"], "疑似病人标识")
        + "\n"
        + _format_hints(segment["follow_hints"], "疑似复诊标记")
    )
    system = render(prompt["system"], raw_text=segment["text"], follow_hints=hints_text)
    return get_llm().generate(system=system, user="", schema=SegmentPatients)


def expand_segment(segment: dict, result: SegmentPatients) -> list[CaseRecord]:
    """把一个粗段的 SegmentPatients 展开成多条 CaseRecord。一个粗段可能有 0/1/多个病人，
    每个病人自己的 case_group_id 用 {physician}-{seg_id}-p{病人序号} 区分，
    诊次内的链式关系（prev_case_id）按病人各自独立维护。"""
    physician = segment["physician"]
    raw = segment["text"]
    records: list[CaseRecord] = []
    for p_idx, sequence in enumerate(result.patients):
        group_id = f"{physician}-{segment['seg_id']}-p{p_idx}"
        prev_id: str | None = None
        for visit in sequence.visits:
            case_id = f"{group_id}-{visit.visit_index}"
            record = CaseRecord(
                case_id=case_id,
                physician=physician,
                raw=raw,
                case_group_id=group_id,
                prev_case_id=prev_id,
                **visit.model_dump(),
            )
            records.append(record)
            prev_id = case_id
    return records


def cross_validate(segment: dict, result: SegmentPatients) -> list[dict]:
    """两个交叉校验并列，都只记录不丢弃：
    1. 病人数：LLM 切出的病人数 vs 段内 head_hints 数
    2. 诊次总量：LLM 切出的诊次总数 vs 段内 (head_hints + follow_hints)
       —— 粘连段场景下没法把某个 follow_hint 精确归属到具体哪个病人，只能在
       整段层面做总量校验，比 R1 单病人版本粗，但仍然是一个真实的一致性信号。"""
    warnings = []
    llm_patients = len(result.patients)
    regex_patients = len(segment["head_hints"])
    if abs(llm_patients - regex_patients) >= 2:
        warnings.append({
            "seg_id": segment["seg_id"],
            "check": "patient_count",
            "llm_count": llm_patients,
            "regex_count": regex_patients,
        })

    llm_visits = sum(len(p.visits) for p in result.patients)
    regex_visits = len(segment["head_hints"]) + len(segment["follow_hints"])
    if abs(llm_visits - regex_visits) >= 2:
        warnings.append({
            "seg_id": segment["seg_id"],
            "check": "visit_total",
            "llm_count": llm_visits,
            "regex_count": regex_visits,
        })
    return warnings


def extract_one(seg_path: Path) -> tuple[list[CaseRecord], list[dict]]:
    segment = json.loads(seg_path.read_text(encoding="utf-8"))
    result = extract_segment(segment)
    records = expand_segment(segment, result)
    warnings = cross_validate(segment, result)
    return records, warnings


def main(argv: list[str] | None = None) -> None:
    # argv 显式可传是为了让测试能直接调用 main()，而不必依赖/污染 sys.argv
    # （pytest 运行时 sys.argv 是 pytest 自己的参数，argparse 会读错）。
    parser = argparse.ArgumentParser(description="离线抽取粗段为结构化 JSON（病人/诊次由 LLM 判断）")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个粗段，方便试水")
    args = parser.parse_args(argv)

    all_records: list[CaseRecord] = []
    warnings: list[dict] = []
    n_success = 0
    n_fail = 0
    n_segments_with_zero_patients = 0

    for seg_path in iter_segment_files(args.limit):
        try:
            records, seg_warnings = extract_one(seg_path)
        except Exception as e:  # noqa: BLE001 - 单个粗段失败不应中断整体
            n_fail += 1
            print(f"[FAIL] {seg_path.name}: {e}")
            continue

        all_records.extend(records)
        n_success += 1
        n_patients = len({r.case_group_id for r in records})
        if n_patients == 0:
            n_segments_with_zero_patients += 1
        print(f"[OK] {seg_path.stem}  病人数={n_patients}  诊次数={len(records)}")

        for w in seg_warnings:
            warnings.append(w)
            print(f"  [WARN] {w['check']} 不一致：LLM={w['llm_count']}  正则估计={w['regex_count']}")

    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump([r.model_dump() for r in all_records], f, ensure_ascii=False, indent=2)
    with WARNINGS_PATH.open("w", encoding="utf-8") as f:
        json.dump(warnings, f, ensure_ascii=False, indent=2)

    groups: dict[str, list[CaseRecord]] = {}
    for r in all_records:
        groups.setdefault(r.case_group_id, []).append(r)
    visit_counts = [len(v) for v in groups.values()]
    dist = Counter(n if n <= 3 else "≥4" for n in visit_counts)

    print("---")
    print(f"成功处理粗段数：{n_success}  失败数：{n_fail}  零病人粗段数：{n_segments_with_zero_patients}")
    print(f"总病人数：{len(groups)}  总诊次数：{len(all_records)}")
    print(
        "诊次分布：1诊={} 2诊={} 3诊={} ≥4诊={}".format(
            dist.get(1, 0), dist.get(2, 0), dist.get(3, 0), dist.get("≥4", 0)
        )
    )
    print(f"最长序列长度：{max(visit_counts) if visit_counts else 0}")
    print(f"交叉校验不一致数：{len(warnings)}（按 check 类型：{Counter(w['check'] for w in warnings)}）")
    print(f"已写出 {OUT_PATH}")
    print(f"已写出 {WARNINGS_PATH}")


if __name__ == "__main__":
    main()
