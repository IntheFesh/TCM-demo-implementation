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

# 「表里有但没抽到」那一类最多列几味。全没抽到时刷一屏没有用——
# 总数在 note 里，要全量明细就读 crosscheck_dose_limits 的返回值。
MISSING_PREVIEW = 15


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
    normalize_herb（同一个归一器，不另写一套）。结果分五类：
      consistent   抽出的上限 <= DOSE_LIMITS 的上限（教材/药典通常给常用量，
                   不超过安全上限是预期）
      inconsistent 抽出的上限 > DOSE_LIMITS 的上限——要么 DOSE_LIMITS 定低了，
                   要么抽错了，两种都要人看
      not_in_dose_limits  抽出了用量但 DOSE_LIMITS 里没有这味药（62 味之外）
      missing_from_extraction  **反方向**：DOSE_LIMITS 里有这味药，但整批抽取
                   一条用量都没抽到它。这一类是**覆盖率**，跟上面三类问的不是
                   同一个问题：上面三类回答"抽出来的对不对"，这一类回答"该抽的
                   抽到了没有"。少了它，62 味里只抽到 3 味也能报出"一致 3 条"
                   这种看起来不错的数——而那恰恰说明这批语料或切块有问题。
                   它同时是 DOSE_LIMITS 这张人工表的反向质检：一味药在四本
                   教材里都找不到用量，值得看看是不是表里的名字写法有问题。
      unparsed     用量不是克为单位（古籍钱两）或格式解析不了，不比对
    只比 source=modern 的记录：DOSE_LIMITS 的来源是现代药典/教材，跟古籍的
    钱两不在同一把尺子上。

    **一条都不改 DOSE_LIMITS。** 那张表的来源声称是"人工从药典查的"，
    自动按抽取结果改它会让这个声称失效——抽取本身是 LLM 产出，拿它去改一张
    被安全层当上限用的表，等于把安全上限的来源从药典换成了模型。
    不一致的和未覆盖的一律列出来人工核。"""
    limit_by_canon = {normalize_herb(name): limit for name, (limit, _note) in DOSE_LIMITS.items()}
    # 归一之后 62 个条目会合并成更少的规范名（"制附子"/"黑顺片"…… 都归到
    # "附子"），覆盖率的分母要用**归一后**的规范名数，不是 len(DOSE_LIMITS)
    # ——按 62 算会让覆盖率被系统性低估，而低估出来的数会被读成"语料不够"。
    out = {"consistent": [], "inconsistent": [], "not_in_dose_limits": [],
           "missing_from_extraction": [], "unparsed": []}
    seen_canon: set[str] = set()
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
        seen_canon.add(herb)
        (out["consistent"] if extracted <= limit_by_canon[herb] else out["inconsistent"]).append(entry)

    # 反方向：表里有、抽取没抽到。按规范名报，并带上表里对应的原始写法，
    # 好判断是不是写法问题（"生首乌"那类只走别名表的名字最容易漏）。
    raw_by_canon: dict[str, list[str]] = {}
    for name in DOSE_LIMITS:
        raw_by_canon.setdefault(normalize_herb(name), []).append(name)
    out["missing_from_extraction"] = [
        {"herb": canon, "dose_limit_g": limit_by_canon[canon],
         "names_in_table": sorted(raw_by_canon.get(canon, []))}
        for canon in sorted(set(limit_by_canon) - seen_canon)
    ]
    out["coverage"] = {
        "n_canonical_in_table": len(limit_by_canon),
        "n_covered": len(seen_canon),
        # 分母是归一后的规范名数，见上面那段注释
        "rate": round(len(seen_canon) / len(limit_by_canon), 4) if limit_by_canon else None,
        "n_entries_in_table": len(DOSE_LIMITS),
    }
    out["note"] = (
        f"DOSE_LIMITS 交叉校验（只比 modern 记录）：一致 {len(out['consistent'])}，"
        f"不一致 {len(out['inconsistent'])}（需人工核），"
        f"DOSE_LIMITS 里没有 {len(out['not_in_dose_limits'])}，解析不了 {len(out['unparsed'])}；"
        f"覆盖率 {out['coverage']['n_covered']}/{out['coverage']['n_canonical_in_table']} 味"
        f"（归一后的规范名，表里共 {out['coverage']['n_entries_in_table']} 条），"
        f"表里有但一条用量都没抽到的 {len(out['missing_from_extraction'])} 味。"
        "**不一致和未覆盖都只列出来人工核，不自动改 DOSE_LIMITS**"
        "——那张表的来源是人工查药典，自动改会让这个来源声称失效。"
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
    missing = report["missing_from_extraction"]
    if missing:
        # 覆盖率低不是"抽取失败"，但它是"这批数据能不能用"的前提，要打出来。
        # 只列前 MISSING_PREVIEW 味：全没抽到时刷一屏没有用，总数在 note 里。
        preview = missing[:MISSING_PREVIEW]
        names = "、".join(
            f"{e['herb']}（表里写法 {'/'.join(e['names_in_table'])}）" for e in preview)
        print(f"  表里有但没抽到（{len(missing)} 味，列前 {len(preview)}）：{names}")
        print("  → 这些要人看：可能是语料里确实没讲，也可能是 DOSE_LIMITS 里的"
              "名字写法跟教材不一致（抽取按归一后的名字比对）。**不自动改表。**")


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
