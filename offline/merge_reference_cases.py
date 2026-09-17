"""把参考医家（李可、王云启）的切案结果合并进 cases.json。

**为什么需要这一步**：`offline/extract_cases.py` 是三家医家专用的——它扫
`data/<医家id>/*.json` 的粗段目录、写死输出到 `cases.json`。而
`extract_cases_li_ke.py` / `extract_cases_wang_yunqi.py` 产出的是
`data/cases_<id>.json` 这种**单文件**，路径和形状都对不上；那两个脚本末尾打印的
「下一步：extract_cases --from-json …」是一条跑不通的命令（`--from-json` 这个参数
不存在），照着跑只会得到 argparse 报错，就算能跑也会把三家的 941 诊次覆盖掉。

**为什么不走 LLM 抽字段**：切案结果已经带 `raw_excerpt`（原文片段）。
`core/retrieval.py::load_cases` 的判据是「`symptoms` 或 `raw_excerpt` 至少有一个」，
所以带原文就已经能进检索、进图谱医案层、被「参考医家」栏引用。结构化字段
（symptoms/syndrome/herbs）是锦上添花，先接进来再说——134 案 × 一次 LLM 调用
可以后补，而合并这一步零调用、可逆。

**三条硬规则**：
  1. **追加不覆盖**：三家的 941 诊次一条都不能少。按 `case_id` 去重，已存在的跳过。
  2. **写盘前必备份**：`cases.json.bak.<时间戳>`。药理层那次被清空就是因为没有备份。
  3. **幂等**：重跑不会重复追加，也不会改动已有记录。

用法：
    python -m offline.merge_reference_cases            # 合并
    python -m offline.merge_reference_cases --dry-run  # 只报数，不写
    python -m offline.merge_reference_cases --remove   # 撤销（把参考医家的记录移除）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "cases.json"


def reference_physician_ids() -> list[str]:
    """参考医家 = 注册表里 enabled=False 的那些。不写死 id 列表：加第六位医家时
    只改注册表一处（CLAUDE.md 第 31 条）。"""
    from core.physicians import PHYSICIANS, physicians_enabled
    enabled = set(physicians_enabled(PHYSICIANS))
    return [pid for pid in PHYSICIANS if pid not in enabled]


def _as_bool(v) -> bool:
    """切案脚本经 JSON 往返后，布尔可能是字符串 'True'/'False'。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _as_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _none_if_null(v):
    """'None' 这个字符串是 JSON 往返的产物，不是真的有个叫 None 的 case_id。"""
    if v is None:
        return None
    if isinstance(v, str) and v.strip() in ("", "None", "null"):
        return None
    return v


def to_case_record(raw: dict) -> dict:
    """切案结果 → cases.json 的记录形状（core.schemas.CaseRecord）。

    只做类型归一，**不编造任何内容**：没有的字段就是 None / 空列表，
    等后续真正跑 LLM 抽字段时再填。`raw` 是必填项，用原文片段充当——
    切案脚本没有保留整个粗段，而 raw_excerpt 就是这一案的原文。
    """
    excerpt = raw.get("raw_excerpt") or ""
    return {
        "case_id": raw["case_id"],
        "physician": raw["physician"],
        "raw": raw.get("raw") or excerpt,
        "raw_excerpt": excerpt,
        "case_group_id": raw.get("case_group_id") or raw["case_id"],
        "prev_case_id": _none_if_null(raw.get("prev_case_id")),
        "visit_index": _as_int(raw.get("visit_index"), 0),
        # 下面这些等 LLM 抽字段那一步再填，现在如实留空
        "symptoms": [],
        "tongue": None,
        "pulse": None,
        "syndrome": None,
        "pathogenesis": None,
        "treatment_principle": None,
        "formula": None,
        "herbs": [],
        "western_drugs": [],
        "visit_marker": raw.get("title"),
        "visit_date": None,
        "response_to_prior": None,
        "copyright_status": "copyrighted",
        "scope": raw.get("scope"),
        "out_of_scope": _as_bool(raw.get("out_of_scope")),
        "has_incompatible_pair": _as_bool(raw.get("has_incompatible_pair")),
    }


def load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("cases", [])


def backup(path: Path) -> Path:
    dst = path.with_suffix(path.suffix + f".bak.{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, dst)
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把参考医家的切案结果合并进 cases.json")
    ap.add_argument("--dry-run", action="store_true", help="只报数，不写文件")
    ap.add_argument("--remove", action="store_true", help="撤销：移除参考医家的记录")
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    args = ap.parse_args(argv)

    ref_ids = reference_physician_ids()
    if not ref_ids:
        print("注册表里没有 enabled=False 的医家，没事做。")
        return 0
    print(f"参考医家（注册表 enabled=False）：{ref_ids}")

    if not args.cases.exists():
        print(f"✗ 找不到 {args.cases}，先跑 offline/extract_cases.py 生成三家的 cases.json",
              file=sys.stderr)
        return 1

    cases = load_json(args.cases)
    before = len(cases)
    by_phys_before: dict = {}
    for c in cases:
        by_phys_before[c.get("physician")] = by_phys_before.get(c.get("physician"), 0) + 1
    print(f"当前 cases.json：{before} 条诊次　{by_phys_before}")

    if args.remove:
        kept = [c for c in cases if c.get("physician") not in ref_ids]
        print(f"移除后：{len(kept)} 条（去掉 {before - len(kept)} 条）")
        if args.dry_run:
            print("--dry-run：没有写文件。")
            return 0
        print(f"已备份到 {backup(args.cases).name}")
        args.cases.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出 {args.cases}")
        return 0

    existing_ids = {c.get("case_id") for c in cases}
    added: list[dict] = []
    for pid in ref_ids:
        src = ROOT / "data" / f"cases_{pid}.json"
        rows = load_json(src)
        if not rows:
            print(f"  = {pid}：{src.name} 不存在或为空，跳过"
                  f"（先跑 offline/extract_cases_{pid}.py）")
            continue
        n_dup = n_new = n_noraw = 0
        for r in rows:
            if r.get("case_id") in existing_ids:
                n_dup += 1
                continue
            rec = to_case_record(r)
            # 判据跟 core/retrieval.py::load_cases 一致：symptoms 或 raw_excerpt
            # 至少有一个，否则编码不出文本、检索层会跳过它。
            if not rec["raw_excerpt"] and not rec["symptoms"]:
                n_noraw += 1
                continue
            added.append(rec)
            existing_ids.add(rec["case_id"])
            n_new += 1
        n_incompat = sum(1 for r in rows if _as_bool(r.get("has_incompatible_pair")))
        print(f"  + {pid}：{len(rows)} 案 → 新增 {n_new}，已存在 {n_dup}，"
              f"无原文跳过 {n_noraw}；含反药配伍 {n_incompat} 案")

    if not added:
        print("没有新增（已经合并过了，或者切案结果为空）。")
        return 0

    print(f"\n合计新增 {len(added)} 条，合并后 {before + len(added)} 条")
    if args.dry_run:
        print("--dry-run：没有写文件。")
        return 0

    print(f"已备份到 {backup(args.cases).name}")
    args.cases.write_text(
        json.dumps(cases + added, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写出 {args.cases}")
    print("\n下一步（都零 LLM 调用）：")
    print("  rm -rf data/cache                          # 语料变了，向量缓存要失效")
    print("  python -m offline.build_graph --all        # 医案层重建，参考医家进图")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
