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


def slice_excerpt(raw: str, visits: list, idx: int) -> str | None:
    """从整段 raw 里切出第 idx 诊对应的片段。

    纯字符串定位，不调 LLM。按 visit_marker 在原文里的位置切：
      第 0 诊 -> 段首到第一个 marker
      第 i 诊 -> 本诊 marker 到下一诊 marker（或段尾）
    marker 为 None 或定位失败时返回 None——宁可留空，不要给错的片段。
    """
    markers = [(i, v.visit_marker) for i, v in enumerate(visits) if v.visit_marker]
    positions = []
    search_from = 0
    for i, mk in markers:
        pos = raw.find(mk, search_from)
        if pos >= 0:
            positions.append((i, pos))
            search_from = pos + len(mk)

    if not positions:
        # 整段只有一诊时，整段就是它的片段
        return raw if len(visits) == 1 else None

    pos_map = dict(positions)
    ordered = [p for _, p in positions]

    if idx == 0 and idx not in pos_map:
        end = ordered[0] if ordered else len(raw)
        return raw[:end].strip() or None

    if idx not in pos_map:
        return None

    start = pos_map[idx]
    later = [p for p in ordered if p > start]
    end = later[0] if later else len(raw)
    return raw[start:end].strip() or None


def expand_segment(segment: dict, result: SegmentPatients) -> list[CaseRecord]:
    """把一个粗段的 SegmentPatients 展开成多条 CaseRecord。一个粗段可能有 0/1/多个病人，
    每个病人自己的 case_group_id 用 {physician}-{seg_id}-p{病人序号} 区分，
    诊次内的链式关系（prev_case_id）按病人各自独立维护。"""
    physician = segment["physician"]
    raw = segment["text"]
    records: list[CaseRecord] = []
    for p_idx, sequence in enumerate(result.patients):
        # seg_id 本身已含 physician 前缀（如 ye_tianshi-0031），不要再拼一次，
        # 否则 case_id 变成 ye_tianshi-ye_tianshi-0031-p6-0
        group_id = f"{segment['seg_id']}-p{p_idx}"
        prev_id: str | None = None
        # 兜底：LLM 偶尔会把多个病人判成"1个病人N诊"且 visit_index 全为 0，
        # 导致同组内 case_id 重复。case_id 是检索引用/图谱边/证据链的主键，
        # 唯一性必须由代码保证，不能依赖 LLM 每次都判对。
        _seen_idx = [v.visit_index for v in sequence.visits]
        _idx_dup = len(_seen_idx) != len(set(_seen_idx))
        for _v_pos, visit in enumerate(sequence.visits):
            _suffix = _v_pos if _idx_dup else visit.visit_index
            case_id = f"{group_id}-{_suffix}"
            record = CaseRecord(
                case_id=case_id,
                physician=physician,
                raw=raw,
                raw_excerpt=slice_excerpt(raw, sequence.visits, _v_pos),
                case_group_id=group_id,
                prev_case_id=prev_id,
                **visit.model_dump(),
            )
            records.append(record)
            prev_id = case_id
    return records


def check_visit_index_dup(segment: dict, result: SegmentPatients) -> list[dict]:
    """检出 visit_index 重复（已在 expand_segment 用序号兜底）的病人组。
    不静默修掉——这是数据质量信号，统计需要知道有多少条走了兜底。"""
    out = []
    for p_idx, sequence in enumerate(result.patients):
        idxs = [v.visit_index for v in sequence.visits]
        if len(idxs) != len(set(idxs)):
            out.append({
                "seg_id": segment["seg_id"],
                "check": "visit_index_dup",
                "patient": p_idx,
                "n_visits": len(idxs),
                "note": "visit_index 重复，case_id 已用枚举序号兜底",
            })
    return out


def cross_validate(segment: dict, result: SegmentPatients) -> list[dict]:
    """两个交叉校验并列，都只记录不丢弃：
    1. 病人数：LLM 切出的病人数 vs 段内 head_hints 数
    2. 诊次总量：LLM 切出的诊次总数 vs 段内 (head_hints + follow_hints)
       —— 粘连段场景下没法把某个 follow_hint 精确归属到具体哪个病人，只能在
       整段层面做总量校验，比 R1 单病人版本粗，但仍然是一个真实的一致性信号。"""
    warnings = []
    llm_patients = len(result.patients)
    # 张锡纯的粗段带 structural_patient_count（原文自带的「属性：」身份行数），
    # 这比 head_hints 可靠得多——那套正则对他的案首体例根本不触发，用它校验
    # 会把 43 段里十几段记成不一致。有结构性判据就用它，且不做 visit_total 校验
    # （follow_hints 对这本书同样无效）。
    structural = segment.get("structural_patient_count")
    if structural is not None:
        if llm_patients != structural:
            warnings.append({
                "seg_id": segment["seg_id"],
                "check": "patient_count",
                "llm_count": llm_patients,
                "regex_count": structural,
            })
        return warnings
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
    warnings = cross_validate(segment, result) + check_visit_index_dup(segment, result)
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
            # check_visit_index_dup 的告警没有 llm_count/regex_count 两个键——原来这里
            # 直接下标，第一条 visit_index_dup 告警就 KeyError，整批结果丢失、
            # cases.json 写不出来。
            if "llm_count" in w:
                print(f"  [WARN] {w['check']} 不一致：LLM={w['llm_count']}  正则估计={w['regex_count']}")
            else:
                print(f"  [WARN] {w['check']}：{w.get('note', '')}")

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
