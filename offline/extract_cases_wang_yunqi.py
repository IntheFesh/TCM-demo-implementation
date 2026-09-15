"""王云启医案抽取器。跟 `extract_cases_li_ke.py` 同一个形状——**切案规则本地、
抽字段仍走 `extract_cases.py` 那一套 LLM schema**——只是体例完全不同。

    python -m offline.extract_cases_wang_yunqi --probe       # 体例探针：先看结构
    python -m offline.extract_cases_wang_yunqi --dry-run     # 切案 + 报数，零 LLM

## 先探针，再定规则

李可那份是编号标题分案（`1.1 脑瘤头痛`），照抄过来一条都切不出：这份的
各论是「第 N 章 → 一、二、三（病种）→ 若干病案」三级结构，**病案本身没有
编号、没有标题**。

`--probe` 把结构里能数的东西都数一遍（章/节/病人行/处方行/「病案分析」段），
**先看数再定规则**。实测（1.x 版本的 .txt，3197 行）：

    第 N 章 33 处 ｜ 一、二、三 24 处 ｜ 病人行 78 处 ｜ 「病案分析」7 处

「病案分析」只有 7 处——拿它当分隔符会把 78 个病案切成 7 块。**病人行才是
这份语料真正的分案点**：`余××,男，60岁` / `尹某，男，43岁`。姓名用 `×` 脱敏、
逗号是半角、年龄前后可能有空格，三样都要在正则里认。

## 一案可能多诊

跟李可那份不同，这份的复诊是**显式**的（「二诊」「三诊」）。所以
`visit_index` 从正文里数得出来，不留 0：一个病人行之后出现几次「N 诊」，
就是几诊。数不出来的（只有一段叙述）仍然是 0，不猜。
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from core.safety_output import check_incompatible
from offline.extract_cases_li_ke import _DOSE_RE, _herb_words, classify_scope

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "local_corpora" / "王云启医案.txt"
DEFAULT_OUT = ROOT / "data" / "cases_wang_yunqi.json"
PHYSICIAN_ID = "wang_yunqi"

# 病人行：`余××,男，60岁` / `尹某，男，43岁` / `张××,女，37岁`。
# 姓名用 × 脱敏、逗号半角全角混用、年龄前后可能有空格——三样都认。
_PATIENT_LINE_RE = re.compile(
    r"([一-鿿]{1,4}(?:×{1,3}|某{1,2}))\s*[,，]\s*([男女])\s*[,，]\s*(\d{1,3})\s*岁")

# 章/节：切案时用来兜住"一个病种讲完了、还没出现下一个病人行"的情况。
_CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十]+章")
_SECTION_RE = re.compile(r"^[一二三四五六七八九十]+\s*、")

# 显式复诊标记。`二诊：` / `三诊，` / `二诊时`。
_VISIT_RE = re.compile(r"[二三四五六七八九十]\s*诊")

_ANALYSIS_MARK = "病案分析"


def probe(text: str, n_show: int = 20) -> dict:
    """体例探针：把结构里能数的东西数一遍。**零 LLM、不改任何文件。**

    存在的理由：新语料的切案规则只能从体例反推，而"体例长什么样"是一个
    可以数出来的事实，不是看几眼就能下的判断。李可那份的规则照抄到这份上
    一条都切不出来——如果不先数，这件事要到跑完 LLM 拿到 0 条结果才发现。
    """
    lines = text.splitlines()
    patients = [m for line in lines for m in [_PATIENT_LINE_RE.search(line)] if m]
    return {
        "n_lines": len(lines),
        "n_chapters": sum(1 for x in lines if _CHAPTER_RE.match(x.strip())),
        "n_sections": sum(1 for x in lines if _SECTION_RE.match(x.strip())),
        "n_patient_lines": len(patients),
        "n_analysis_marks": text.count(_ANALYSIS_MARK),
        "n_dose_lines": sum(1 for x in lines if _DOSE_RE.search(x)),
        "sample_patients": [m.group(0) for m in patients[:n_show]],
    }


def split_cases(text: str) -> list[dict]:
    """按病人行切案。一案 = 从病人行所在段落起，到下一个病人行（或下一章）为止。

    段落而不是行：这份 .txt 是 docx 转出来的，一个自然段占一行，病人行往往
    跟主诉写在同一行（`余××,男，60岁 因反复咳嗽…`）。从**行首**切会把主诉
    留在上一案里。
    """
    paras = [p for p in text.split("\n")]
    starts: list[int] = []
    for i, para in enumerate(paras):
        if _PATIENT_LINE_RE.search(para):
            starts.append(i)
    cases: list[dict] = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(paras)
        # 下一章/下一节在中间出现时提前收尾——病种讲完了，后面是下一个病种的
        # 综述，不属于这一案。
        for j in range(start + 1, end):
            s = paras[j].strip()
            if _CHAPTER_RE.match(s) or _SECTION_RE.match(s):
                end = j
                break
        body = "\n".join(paras[start:end]).strip()
        m = _PATIENT_LINE_RE.search(body)
        pairs = check_incompatible(_herb_words(body))
        case_id = f"{PHYSICIAN_ID}-{k + 1:04d}"
        cases.append({
            "case_id": case_id,
            "physician": PHYSICIAN_ID,
            "title": (m.group(0) if m else ""),
            "sex": m.group(2) if m else None,
            "age": int(m.group(3)) if m else None,
            "raw_excerpt": body,
            # 显式复诊：数正文里出现几次「N 诊」。数不出来就是 0，不猜。
            "visit_index": 0,
            "n_visits_in_text": len(set(_VISIT_RE.findall(body))) + 1,
            "case_group_id": case_id,
            "prev_case_id": None,
            "scope": classify_scope(body),
            "out_of_scope": classify_scope(body) != "spleen_stomach",
            "incompatible_pairs": [f"{a}-{b}" for a, b in pairs],
            "has_incompatible_pair": bool(pairs),
            "has_analysis": _ANALYSIS_MARK in body,
        })
    return cases


def summarize(cases: list[dict]) -> dict:
    return {
        "n_cases": len(cases),
        "by_scope": dict(Counter(c["scope"] for c in cases)),
        "n_with_analysis": sum(1 for c in cases if c["has_analysis"]),
        "n_multi_visit": sum(1 for c in cases if c["n_visits_in_text"] > 1),
        "n_incompatible": sum(1 for c in cases if c["has_incompatible_pair"]),
        "incompatible_pairs": dict(Counter(
            p for c in cases for p in c["incompatible_pairs"])),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--probe", action="store_true", help="只报体例结构，不切案")
    ap.add_argument("--dry-run", action="store_true", help="切案 + 报数，不写文件、零 LLM")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    if not args.input.exists():
        print(f"找不到语料：{args.input}\n这份语料版权受限、不随仓库分发，见 data/SOURCES.md。")
        return 2
    text = args.input.read_text(encoding="utf-8")

    if args.probe:
        info = probe(text)
        print(f"行数 {info['n_lines']}｜第 N 章 {info['n_chapters']}"
              f"｜一、二、三 {info['n_sections']}｜病人行 {info['n_patient_lines']}"
              f"｜「{_ANALYSIS_MARK}」{info['n_analysis_marks']}"
              f"｜含剂量的行 {info['n_dose_lines']}")
        print("\n前 20 个病人行（切案点就是它们）：")
        for s in info["sample_patients"]:
            print("   ", s)
        print(f"\n结论：「{_ANALYSIS_MARK}」只有 {info['n_analysis_marks']} 处，"
              f"拿它当分隔符会把 {info['n_patient_lines']} 个病案切成 "
              f"{info['n_analysis_marks']} 块——**病人行才是分案点**。")
        return 0

    cases = split_cases(text)
    if args.limit > 0:
        cases = cases[:args.limit]
    stats = summarize(cases)
    print(f"切出 {stats['n_cases']} 案")
    print(f"  按范围：{stats['by_scope']}")
    print(f"  带「{_ANALYSIS_MARK}」段：{stats['n_with_analysis']} 案")
    print(f"  正文里能数出复诊的：{stats['n_multi_visit']} 案")
    print(f"  含反药配伍：{stats['n_incompatible']} 案（**算但不排除**）")
    for pair, n in sorted(stats["incompatible_pairs"].items(), key=lambda kv: -kv[1]):
        print(f"    {pair}：{n} 案")

    if args.dry_run:
        print("\n--dry-run：没有调用 LLM，也没有写文件。")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
