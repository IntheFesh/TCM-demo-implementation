"""遍历 data/{physician}/*.txt，用 LLM 把古籍医案原文抽取成结构化 CaseRecord，写出 cases.json。

用法：
    python -m offline.extract_cases            # 全量
    python -m offline.extract_cases --limit 3   # 先跑 3 个试水
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.llm import get_llm, load_prompt, render
from core.schemas import CaseRecord, CaseStructured

DATA_ROOT = Path(__file__).resolve().parent.parent / "data"
OUT_PATH = Path(__file__).resolve().parent.parent / "cases.json"


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


def extract_one(physician: str, txt_path: Path) -> CaseRecord:
    raw = txt_path.read_text(encoding="utf-8").strip()
    prompt = load_prompt("s0_extract_case")
    system = render(prompt["system"], raw_text=raw)
    structured: CaseStructured = get_llm().generate(
        system=system, user="", schema=CaseStructured
    )
    case_id = f"{physician}-{txt_path.stem}"
    return CaseRecord(case_id=case_id, physician=physician, raw=raw, **structured.model_dump())


def main() -> None:
    parser = argparse.ArgumentParser(description="离线抽取医案为结构化 JSON")
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 个文件，方便试水")
    args = parser.parse_args()

    records: list[CaseRecord] = []
    n_success = 0
    n_fail = 0
    n_null_syndrome = 0

    for physician, txt_path in iter_case_files(args.limit):
        try:
            record = extract_one(physician, txt_path)
        except Exception as e:  # noqa: BLE001 - 单个文件失败不应中断整体
            n_fail += 1
            print(f"[FAIL] {physician}/{txt_path.name}: {e}")
            continue

        records.append(record)
        n_success += 1
        if record.syndrome is None:
            n_null_syndrome += 1
        print(f"[OK] {record.case_id}  syndrome={record.syndrome!r}")

    with OUT_PATH.open("w", encoding="utf-8") as f:
        json.dump([r.model_dump() for r in records], f, ensure_ascii=False, indent=2)

    print("---")
    print(f"成功数：{n_success}  失败数：{n_fail}  syndrome 为 null 的数量：{n_null_syndrome}")
    print(f"已写出 {OUT_PATH}")


if __name__ == "__main__":
    main()
