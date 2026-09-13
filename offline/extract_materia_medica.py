"""总纲 2.2（M12）：本草三元组入口。抽取逻辑全在 offline/extract_reference_triples.py
（跟 extract_formulary.py 共用一份引擎），这里只做两件事：把 kind 固定成
materia_medica；抽完之后把「用量」跟 core/safety_output.py 的 DOSE_LIMITS
（62 味）自动交叉校验——不一致的列出来人工核，这是一次免费的质检。

用法：
    python -m offline.extract_materia_medica --input books/xxx.txt --source classic --book 神农本草经
    python -m offline.extract_materia_medica --input books/中药学.txt --source modern --book 中药学 --crosscheck
"""
from __future__ import annotations

import re

from core.herbs import normalize_herb
from core.safety_output import DOSE_LIMITS
from offline import extract_reference_triples as engine

# 「用量」的 o 是原文原样（"9～30g""3-9克""一钱"）。只解析克为单位的现代写法，
# 取区间上限；古籍的钱/两不换算——两个量纲不是同一把尺子，硬换算会把
# "一钱≈3g 还是 3.7g"这种没有定论的事悄悄埋进对照里。解析不了的条目单独
# 报在 unparsed 里，不静默丢。
_GRAM_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[~～\-—至到]\s*(\d+(?:\.\d+)?)\s*(?:g|克)"
)
_GRAM_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|克)")


def parse_max_grams(dose_text: str) -> float | None:
    """从「用量」原文里取克为单位的上限：区间取右端，单值取本身；不是克就 None。"""
    m = _GRAM_RANGE_RE.search(dose_text)
    if m:
        return float(m.group(2))
    m = _GRAM_SINGLE_RE.search(dose_text)
    if m:
        return float(m.group(1))
    return None


def crosscheck_dose_limits(rows: list[dict]) -> dict:
    """把抽出的「用量」跟 DOSE_LIMITS 逐味比对。药名两边都过 core.herbs.
    normalize_herb（同一个归一器，不另写一套）。三种结果分开列：
      consistent   抽出的上限 <= DOSE_LIMITS 的上限（教材/药典通常给常用量，
                   不超过安全上限是预期）
      inconsistent 抽出的上限 > DOSE_LIMITS 的上限——要么 DOSE_LIMITS 定低了，
                   要么抽错了，两种都要人看
      not_in_dose_limits  抽出了用量但 DOSE_LIMITS 里没有这味药（62 味之外）
      unparsed     用量不是克为单位（古籍钱两）或格式解析不了，不比对
    只比 source=modern 的记录：DOSE_LIMITS 的来源是现代药典/教材，跟古籍的
    钱两不在同一把尺子上。"""
    limit_by_canon = {normalize_herb(name): limit for name, (limit, _note) in DOSE_LIMITS.items()}
    out = {"consistent": [], "inconsistent": [], "not_in_dose_limits": [], "unparsed": []}
    for row in rows:
        if row.get("p") != "用量" or row.get("source") != "modern":
            continue
        herb = normalize_herb(row.get("s") or "")
        extracted = parse_max_grams(row.get("o") or "")
        entry = {"herb": herb, "raw": row.get("s"), "dose_text": row.get("o"), "book": row.get("book")}
        if extracted is None:
            out["unparsed"].append(entry)
            continue
        entry["extracted_max_g"] = extracted
        if herb not in limit_by_canon:
            out["not_in_dose_limits"].append(entry)
            continue
        entry["dose_limit_g"] = limit_by_canon[herb]
        (out["consistent"] if extracted <= limit_by_canon[herb] else out["inconsistent"]).append(entry)
    out["note"] = (
        f"DOSE_LIMITS 交叉校验（只比 modern 记录）：一致 {len(out['consistent'])}，"
        f"不一致 {len(out['inconsistent'])}（需人工核），"
        f"DOSE_LIMITS 里没有 {len(out['not_in_dose_limits'])}，解析不了 {len(out['unparsed'])}。"
    )
    return out


def _after_write(rows: list[dict], args) -> None:
    if not getattr(args, "crosscheck", False):
        return
    report = crosscheck_dose_limits(rows)
    print(report["note"])
    for entry in report["inconsistent"]:
        print(f"  不一致：{entry['herb']}（{entry['book']}）抽出上限 {entry['extracted_max_g']}g"
              f" > DOSE_LIMITS {entry['dose_limit_g']}g，原文「{entry['dose_text']}」")


def main(argv: list[str] | None = None) -> None:
    parser = engine.build_parser("materia_medica")
    parser.add_argument("--crosscheck", action="store_true",
                        help="抽完之后把「用量」跟 core/safety_output.py 的 DOSE_LIMITS 交叉校验")
    # 复用引擎的 run()：它自己会再 parse 一次同一份 argv——这里先 parse 只是
    # 为了让 --crosscheck 出现在 --help 里，真正的执行流程仍然只有引擎那一处。
    args, _ = parser.parse_known_args(argv)
    engine_argv = [a for a in (argv if argv is not None else []) if a != "--crosscheck"]
    if argv is None:
        import sys

        engine_argv = [a for a in sys.argv[1:] if a != "--crosscheck"]

    def after_write(rows: list[dict], engine_args) -> None:
        engine_args.crosscheck = args.crosscheck
        _after_write(rows, engine_args)

    engine.run(engine_argv, kind_name="materia_medica", after_write=after_write)


if __name__ == "__main__":
    main()
