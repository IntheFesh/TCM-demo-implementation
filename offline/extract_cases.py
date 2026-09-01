"""遍历 data/{physician}/*.txt，用 LLM 把古籍医案原文切成诊次序列，展开成 CaseRecord，
写出 cases.json。

用法：
    python -m offline.extract_cases            # 全量
    python -m offline.extract_cases --limit 3   # 先跑 3 个试水
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.llm import get_llm, load_prompt, render
from core.schemas import CaseRecord, CaseSequence
from offline.split_cases import FOLLOW, FOLLOW_KEYWORDS

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = Path(__file__).resolve().parent.parent / "cases.json"
WARNINGS_PATH = Path(__file__).resolve().parent.parent / "extract_warnings.json"


def iter_case_files(limit: int | None = None):
    """遍历 data/{physician}/*.txt，按 physician 目录名、文件名排序，保证可复现。"""
    count = 0
    for physician_dir in sorted(p for p in DATA_ROOT.iterdir() if p.is_dir()):
        physician = physician_dir.name
        for txt_path in sorted(physician_dir.glob("*.txt")):
            if limit is not None and count >= limit:
                return
            yield physician, txt_path
            count += 1


def _follow_hints(raw: str) -> list[str]:
    """正则扫出的疑似复诊标记，注入 prompt 当提示用（不是切分依据本身）。"""
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    hints = [l[:12] for l in lines[1:] if FOLLOW.match(l)]  # 跳过案首行本身
    hints += FOLLOW_KEYWORDS.findall(raw)
    return hints


def _regex_visit_estimate(raw: str) -> int:
    """交叉校验用的诊次数估计：正则命中的复诊标记数 + 1（初诊）。"""
    lines = [l.strip() for l in raw.split("\n") if l.strip()]
    follow_marks = sum(1 for l in lines[1:] if FOLLOW.match(l))
    return follow_marks + 1


def extract_sequence(raw: str) -> CaseSequence:
    prompt = load_prompt("s0_extract_case")
    hints = _follow_hints(raw)
    hints_text = "、".join(hints) if hints else "（未扫到疑似标记，可能是单诊案）"
    system = render(prompt["system"], raw_text=raw, follow_hints=hints_text)
    return get_llm().generate(system=system, user="", schema=CaseSequence)


def expand_sequence(physician: str, stem: str, sequence: CaseSequence, raw: str) -> list[CaseRecord]:
    """把一个 CaseSequence 展开成多条 CaseRecord，串好 case_group_id / prev_case_id。

    raw 字段在每一诊上都存完整原文（而不是切出这一诊对应的片段）：按诊次
    机械切分原文风险很高（容易切错行），而完整原文本来就是可验证的真值，
    每条记录都能溯源到同一份原文不算信息损失。
    """
    group_id = f"{physician}-{stem}"
    records: list[CaseRecord] = []
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


def extract_one(physician: str, txt_path: Path) -> tuple[list[CaseRecord], dict | None]:
    """返回 (这个案展开出的全部 CaseRecord, 交叉校验不一致时的 warning dict 或 None)。"""
    raw = txt_path.read_text(encoding="utf-8").strip()
    sequence = extract_sequence(raw)
    records = expand_sequence(physician, txt_path.stem, sequence, raw)

    llm_count = len(sequence.visits)
    regex_count = _regex_visit_estimate(raw)
    warning = None
    if abs(llm_count - regex_count) >= 2:
        warning = {
            "case_group_id": f"{physician}-{txt_path.stem}",
            "llm_visit_count": llm_count,
            "regex_visit_estimate": regex_count,
        }
    return records, warning


def main(argv: list[str] | None = None) -> None:
    # argv 显式可传是为了让测试能直接调用 main()，而不必依赖/污染 sys.argv
    # （pytest 运行时 sys.argv 是 pytest 自己的参数，argparse 会读错）。
    parser = argparse.ArgumentParser(description="离线抽取医案诊次序列为结构化 JSON")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个文件，方便试水")
    args = parser.parse_args(argv)

    all_records: list[CaseRecord] = []
    warnings: list[dict] = []
    n_success = 0
    n_fail = 0

    for physician, txt_path in iter_case_files(args.limit):
        try:
            records, warning = extract_one(physician, txt_path)
        except Exception as e:  # noqa: BLE001 - 单个文件失败不应中断整体
            n_fail += 1
            print(f"[FAIL] {physician}/{txt_path.name}: {e}")
            continue

        all_records.extend(records)
        n_success += 1
        syndromes = [r.syndrome for r in records]
        print(f"[OK] {physician}-{txt_path.stem}  {len(records)} 诊  syndrome={syndromes}")

        if warning is not None:
            warnings.append(warning)
            print(
                f"  [WARN] 交叉校验不一致：LLM 切出 {warning['llm_visit_count']} 诊，"
                f"正则估计 {warning['regex_visit_estimate']} 诊——不自动丢弃，已记入 "
                f"{WARNINGS_PATH.name}，请人工核查"
            )

    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump([r.model_dump() for r in all_records], f, ensure_ascii=False, indent=2)
    with WARNINGS_PATH.open("w", encoding="utf-8") as f:
        json.dump(warnings, f, ensure_ascii=False, indent=2)

    groups: dict[str, list[CaseRecord]] = {}
    for r in all_records:
        groups.setdefault(r.case_group_id, []).append(r)
    visit_counts = [len(v) for v in groups.values()]
    dist = Counter(
        n if n <= 3 else "≥4"
        for n in visit_counts
    )

    print("---")
    print(f"成功数：{n_success}  失败数：{n_fail}")
    print(f"总案数（病人数）：{len(groups)}  总诊次数：{len(all_records)}")
    print(
        "诊次分布：1诊={} 2诊={} 3诊={} ≥4诊={}".format(
            dist.get(1, 0), dist.get(2, 0), dist.get(3, 0), dist.get("≥4", 0)
        )
    )
    print(f"最长序列长度：{max(visit_counts) if visit_counts else 0}")
    print(f"交叉校验不一致的案数：{len(warnings)}")
    print(f"已写出 {OUT_PATH}")
    print(f"已写出 {WARNINGS_PATH}")


if __name__ == "__main__":
    main()
