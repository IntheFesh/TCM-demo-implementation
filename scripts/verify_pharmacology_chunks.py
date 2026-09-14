"""R4-2：药理层六个源的**切块验证**。零 LLM 调用。

抽取引擎（`offline/extract_reference_triples.py`）按空行分块，一块**应该**是
一味药 / 一张方的条目。但六个源的排版格式各不相同（古籍是竖排转录、教材有
章节标题和表格），切出来可能是整章、可能是半句。**这一步零成本，但决定了真实
抽取会不会白花几百次调用**——一块 = 一次调用，切错了不是"结果差一点"，是
整批数据没有意义。

    python -m scripts.verify_pharmacology_chunks                      # 默认 books/
    python -m scripts.verify_pharmacology_chunks --books-dir /path
    python -m scripts.verify_pharmacology_chunks --file books/中药学.txt --show 5

退出码：0 = 所有在场的源都过基本检查；1 = 有源没过；2 = 一个源都没找到
（那不是"没过"，是"还没下载"——先跑 scripts/fetch_pharmacology_sources.sh）。

**切块复用引擎的 `split_blocks`，不另写一套**（CLAUDE.md「同一概念只能有一处
实现」）：这里验的必须是真实抽取会用的那个切法，自己再写一个"差不多的"切法
等于验了一个不存在的东西。
"""
from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOOKS_DIR = ROOT / "books"

# 六个源的表住在 offline/pharmacology_sources.py（R8 挪过去的：抽取引擎的预过滤
# 和批量抽取入口也要读它，而 offline/ 不能反过来 import scripts/）。这里再导出
# 同名，调用方和测试不用改。表的说明（跟下载脚本各列一份是刻意的、教材为什么
# 是 heading、古籍为什么 R8 起也是 heading）都在那个模块里。BLOCK_MAX_CHARS
# 也从那里来：预过滤的「超长」一类和这里的第三个阈值必须是同一个数。
from offline.pharmacology_sources import (  # noqa: E402
    BLOCK_MAX_CHARS,
    ENTRY_PREDICATES,
    EXPECTED_SOURCES,
    format_prefilter_summary,
)
from offline.extract_reference_triples import plan_blocks  # noqa: E402
from offline.local_corpora import non_reference_reason  # noqa: E402

UNKNOWN_SOURCE = "unknown"
FILTERED_PREVIEW = 5

# ---- 三个异常阈值，以及每个数的依据 ----
#
# MIN_BLOCKS = 50：一本本草/教材至少讲几百味药或上百张方。切出来不到 50 块
# 只有两种可能：这本书没有空行分段（整章成一块），或者下载的是残缺文件。
# 50 这个数取得比"几百"宽松很多，是为了让它只在**明显切错**时报警，
# 不在"这本书确实薄"时误报。
MIN_BLOCKS = 50

# MEDIAN_MAX_CHARS = 3000：一味药的条目（性味/归经/功效/主治/用量/禁忌）
# 实测量级几百字；一张方的条目（组成/用法/功用/主治/方解/加减）上千字。
# 中位数超过 3000 说明**典型的一块已经不是一个条目**，大概率整节成一块。
# 用中位数而不是均值：一两个超长块（前言、目录）会把均值拉飞，中位数才反映
# "典型的一块长什么样"。
MEDIAN_MAX_CHARS = 3000

# BLOCK_MAX_CHARS（单块上限 10000 字）的依据写在 offline/pharmacology_sources.py：
# 引擎 MAX_TOKENS=16384、中文约 1 token/字，一万字的块注定被判截断。

MIN_BLOCKS_REASON = f"一本本草/教材至少上百个条目，不到 {MIN_BLOCKS} 块 = 没按条目切开或文件残缺"
MEDIAN_REASON = f"典型一块超过 {MEDIAN_MAX_CHARS} 字 = 一块已经不是一个条目（大概率整节成一块）"
BLOCK_MAX_REASON = (f"单块超过 {BLOCK_MAX_CHARS} 字，按中文约 1 token/字算会顶到引擎的 "
                    f"MAX_TOKENS=16384 被判截断，这一次调用注定浪费")

DEFAULT_SHOW = 3


def chunk_stats(text: str, chunk_by: str = "blank-line") -> dict:
    """切块统计。纯函数，好单测。切法复用引擎的 split_blocks。"""
    from offline.extract_reference_triples import split_blocks

    blocks = split_blocks(text, chunk_by)
    lengths = [len(b) for b in blocks]
    return {
        "n_blocks": len(blocks),
        "n_chars": len(text),
        "median_chars": statistics.median(lengths) if lengths else 0,
        "mean_chars": round(statistics.mean(lengths), 1) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
        "min_chars": min(lengths) if lengths else 0,
        "longest_index": lengths.index(max(lengths)) if lengths else None,
        "shortest_index": lengths.index(min(lengths)) if lengths else None,
        "blocks": blocks,
    }


def check_anomalies(stats: dict) -> list[str]:
    """三个阈值的检查，返回问题清单（空 = 通过）。每条都带上"为什么是这个数"
    ——一个没有依据的阈值只会让人下次直接把它调大。"""
    problems = []
    if stats["n_blocks"] < MIN_BLOCKS:
        problems.append(f"块数 {stats['n_blocks']} < {MIN_BLOCKS}（切得太粗）：{MIN_BLOCKS_REASON}")
    if stats["median_chars"] > MEDIAN_MAX_CHARS:
        problems.append(f"字数中位数 {stats['median_chars']} > {MEDIAN_MAX_CHARS}：{MEDIAN_REASON}")
    if stats["max_chars"] > BLOCK_MAX_CHARS:
        problems.append(f"最长块 {stats['max_chars']} 字 > {BLOCK_MAX_CHARS}：{BLOCK_MAX_REASON}")
    return problems


def _preview(block: str, limit: int) -> str:
    body = block[:limit]
    return body + ("…" if len(block) > limit else "")


def compare_modes(text: str) -> dict[str, dict]:
    """三种切法各切一遍，只报统计不报原文。用来回答"这个源该用哪种切法"——
    同一份原文在三种切法下的块数/中位数差一个数量级时，选哪种就很明显了。"""
    return {mode: {k: v for k, v in chunk_stats(text, mode).items() if k != "blocks"}
            for mode in ("blank-line", "line", "heading")}


def prefilter_counts(text: str, chunk_by: str, source: str) -> tuple[int, int, dict[str, int]]:
    """(切块总数, 预过滤后保留数, 各类跳过计数)。保留数 = 真实抽取的调用数。
    走引擎的 plan_blocks，不另算——三处各算一遍迟早对不上。"""
    all_blocks, kept, _skipped, counts = plan_blocks(text, chunk_by, source, prefilter=True)
    return len(all_blocks), len(kept), counts


def report_source(path: Path, label: tuple[str, str, str, str], chunk_by: str,
                  show: int, preview_chars: int, compare: bool = False,
                  prefilter: bool = True, for_extraction: bool = True) -> list[str]:
    """打印一个源的切块统计 + 预过滤结果 + 前 N 块原文，返回问题清单。

    **原文必须打出来。** 统计数字能说"切成了 800 块、中位数 400 字"，说不了
    "切出来的是不是一味药"——那只有人看原文才判得出来。这个脚本的主要产出
    其实就是这几段原文，阈值检查只是顺手能自动化的那部分。

    R8 起前 N 块打的是**预过滤后保留的**前 N 块（真实抽取会喂给模型的那些），
    另外把**被预过滤跳过的前 FILTERED_PREVIEW 块**连原因一起打出来——预过滤
    是按结构判的，判错了只有人看原文才看得出来，这两组原文缺一组都不完整。
    三个阈值检查对**预过滤后保留的块**做——那才是真实抽取会喂给模型的；中药学的
    药名索引表一块 18686 字，但它被预过滤按「表格」跳掉了，不会有那一次调用，
    再拿它判"没过"就是在拦一个不存在的浪费。切块结果里最长的那块仍然会打出开头
    （信息，不是判据）。`--no-prefilter` 时退回 R4-2 的行为：阈值对切块结果做，
    前 N 块 = 切块结果的前 N 块。
    """
    source, kind, recommended, purpose = label
    text = path.read_text(encoding="utf-8")
    effective = chunk_by or recommended
    stats = chunk_stats(text, effective)
    print(f"=== {path.name}　[{source} / {kind}]　{purpose} ===")
    print(f"切块模式：{effective}"
          + ("" if chunk_by is None else f"（命令行指定；这个源推荐 {recommended}）"))
    if chunk_by is not None and chunk_by != recommended:
        # 不拦，但要说：推荐值是按这个源的真实排版定的（见 EXPECTED_SOURCES 注释）
        print(f"  ⚠ 用的不是推荐模式（推荐 {recommended}）。"
              "教材用 blank-line 会把一味药切成五六块、其中「用量」那块没有药名——"
              "那种块抽出来的 s 是模型猜的，而 s 不过 source_span 核验。")
    print(f"全文 {stats['n_chars']} 字　切成 {stats['n_blocks']} 块"
          + ("（预过滤前）" if prefilter else "（= 真实抽取的调用数）"))
    print(f"每块字数：中位数 {stats['median_chars']}　均值 {stats['mean_chars']}　"
          f"最短 {stats['min_chars']}（第 {stats['shortest_index']} 块）　"
          f"最长 {stats['max_chars']}（第 {stats['longest_index']} 块）")
    if prefilter:
        _all, kept, skipped, counts = plan_blocks(text, effective, source, prefilter=True)
        rule = (f"source={source} 的结构判据" if source in ENTRY_PREDICATES
                else f"source={source} 没有结构判据，只跳过过短/表格/超长三类")
        tail = ("　← 保留数 = 真实抽取的调用数" if for_extraction
                else "　← **这份语料不进药理层抽取**，这里只看切块粒度，保留数不是调用数")
        print(f"预过滤（{rule}）：{format_prefilter_summary(len(_all), counts)}{tail}")
        kept_lengths = [len(b) for _i, b in kept]
        judged = {
            "n_blocks": len(kept),
            "median_chars": statistics.median(kept_lengths) if kept_lengths else 0,
            "max_chars": max(kept_lengths) if kept_lengths else 0,
        }
        if kept:
            longest_i = max(kept, key=lambda ib: len(ib[1]))[0]
            print(f"保留块字数：中位数 {judged['median_chars']}　"
                  f"最短 {min(kept_lengths)}　最长 {judged['max_chars']}（第 {longest_i} 块）")
        judged_on = f"对预过滤后保留的 {len(kept)} 块"
        shown = kept
    else:
        judged, judged_on = stats, "对切块结果"
        shown, skipped = list(enumerate(stats["blocks"])), []
    problems = check_anomalies(judged)
    for p in problems:
        print(f"  ✗ {p}")
    if not problems:
        print(f"  ✓ 三个阈值检查都过（{judged_on}）")
    print()
    n = min(show, len(shown))
    if n:
        print(f"前 {n} 块原文（**人工确认切的是不是一味药 / 一张方**"
              + ("；块号是切块结果里的位置" if prefilter else "") + "）：")
        for i, block in shown[:n]:
            print(f"  --- 第 {i} 块（{len(block)} 字）---")
            for line in _preview(block, preview_chars).splitlines():
                print(f"    {line}")
        print()
    if skipped:
        m = min(FILTERED_PREVIEW, len(skipped))
        print(f"被预过滤跳过的前 {m} 块（**人工确认跳掉的确实不是条目**）：")
        for i, reason, block in skipped[:m]:
            print(f"  --- 第 {i} 块（{len(block)} 字）[{reason}]---")
            for line in _preview(block, min(preview_chars, 160)).splitlines():
                print(f"    {line}")
        print()
    if compare:
        print("三种切法对比（同一份原文，只看统计）：")
        for mode, st in compare_modes(text).items():
            mark = "　← 推荐" if mode == recommended else ""
            print(f"  {mode:<11} {st['n_blocks']:>6} 块　中位数 {st['median_chars']:>7}"
                  f"　最长 {st['max_chars']:>7}{mark}")
        print()
    if stats["blocks"] and stats["max_chars"] > BLOCK_MAX_CHARS:
        longest = stats["blocks"][stats["longest_index"]]
        print(f"最长那块的开头（第 {stats['longest_index']} 块，{len(longest)} 字，"
              "看看它是不是整章）：")
        for line in _preview(longest, preview_chars).splitlines():
            print(f"    {line}")
        print()
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="R4-2：药理层数据源的切块验证（零 LLM 调用）")
    ap.add_argument("--books-dir", type=Path, default=DEFAULT_BOOKS_DIR)
    ap.add_argument("--file", type=Path, default=None,
                    help="只验这一个文件（不在六源清单里的也能验，标签按 unknown 处理）")
    ap.add_argument("--chunk-by", default=None,
                    choices=("blank-line", "line", "heading"),
                    help="不传 = 用每个源在 EXPECTED_SOURCES 里的推荐模式（教材 heading、"
                         "古籍 blank-line）。传了就对所有源用同一个值，并在不等于推荐值时"
                         "警告——真实抽取要传跟这里一致的 --chunk-by，否则验的不是同一个切法")
    ap.add_argument("--compare-modes", action="store_true",
                    help="每个源额外报三种切法的块数/中位数对比，用来确认推荐模式选对了")
    ap.add_argument("--show", type=int, default=DEFAULT_SHOW, help="每个源打印前几块原文")
    ap.add_argument("--preview-chars", type=int, default=400, help="每块原文打印前多少字")
    ap.add_argument("--source", choices=("classic", "modern", UNKNOWN_SOURCE), default=None,
                    help="只对 --file 生效：这个文件按哪种源类型做预过滤的结构判据"
                         "（classic=古籍 <篇名>/剂量词，modern=教材 【字段】）。不传 = 六源清单里"
                         "有就用清单的，没有就 unknown（不做结构判据，只跳过过短/表格/超长）")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="不做块级预过滤，前 N 块打的是切块结果的前 N 块（R4-2 的行为）。"
                         "只在核对预过滤本身时用——真实抽取默认是开着预过滤的")
    args = ap.parse_args(argv)

    print("**零 LLM 调用**：只切块 + 预过滤 + 统计 + 打印原文。预过滤后保留的一块 = "
          "真实抽取的一次调用，所以这一步过不了就不要去跑抽取。")
    print("切块模式：" + (f"命令行指定 {args.chunk_by}（对所有源生效）"
                         if args.chunk_by else "按每个源的推荐值（六个源都是 heading：教材认「#」、古籍认「<篇名>」）")
          + "——真实抽取要传跟这里一致的 --chunk-by")
    print()

    # 本地语料（医案 docx 转出来的 txt、脾胃论）拿这个脚本只为看**段落粒度**，
    # 它们不进药理层抽取（判断在 offline/local_corpora.py 那一处）。不说这句，
    # 下面那句「真实抽取预估调用数 937」会被读成"该花 937 次调用"。
    not_for_extraction = None if args.file is None else non_reference_reason(args.file)
    if not_for_extraction is not None:
        print(f"⚠ {not_for_extraction}")
        print("  所以下面的块数**不是**抽取预估调用数，只用来看这份语料的段落粒度"
              "（docx 的段落粒度因人而异，见 offline/docx_to_text.py）。")
        print()

    if args.file is not None:
        # 不在六源清单里的文件（docx 转出来的医案 txt 之类）默认按 blank-line，
        # 并标明它不在清单里——不猜它该用哪种切法；源类型（决定预过滤的结构判据）
        # 可以用 --source 指定，不指定就 unknown = 不做结构判据。
        label = EXPECTED_SOURCES.get(
            args.file.name, (UNKNOWN_SOURCE, UNKNOWN_SOURCE, "blank-line", "（不在六源清单里）"))
        if args.source is not None:
            label = (args.source, *label[1:])
        targets = [(args.file, label)]
    else:
        targets = [(args.books_dir / name, label)
                   for name, label in EXPECTED_SOURCES.items()]

    present = [(p, label) for p, label in targets if p.exists()]
    missing = [p for p, _ in targets if not p.exists()]
    if not present:
        print(f"一个源都没找到（找的是 {args.books_dir}）。先跑："
              "\n  bash scripts/fetch_pharmacology_sources.sh"
              "\n这不是「没过」，是「还没下载」，所以退出码是 2。", file=sys.stderr)
        return 2
    if missing:
        # 缺源不算失败：允许一本一本地下、一本一本地验。但要如实报出来，
        # 不然"六个源都过了"这句话会被读成"六个源都在"。
        print(f"以下源不在 {args.books_dir}，本次跳过（不算失败，但它们没有被验过）：")
        for p in missing:
            print(f"  - {p.name}")
        print()

    problems_by_source: dict[str, list[str]] = {}
    total_blocks = total_kept = 0
    total_counts: dict[str, int] = {}
    for path, label in present:
        problems_by_source[path.name] = report_source(
            path, label, args.chunk_by, args.show, args.preview_chars,
            compare=args.compare_modes, prefilter=not args.no_prefilter,
            for_extraction=not_for_extraction is None)
        if not args.no_prefilter:
            n_all, n_kept, counts = prefilter_counts(
                path.read_text(encoding="utf-8"), args.chunk_by or label[2], label[0])
            total_blocks += n_all
            total_kept += n_kept
            for reason, n in counts.items():
                total_counts[reason] = total_counts.get(reason, 0) + n

    print("=" * 70)
    failed = {k: v for k, v in problems_by_source.items() if v}
    print(f"验了 {len(present)} 个源，{len(present) - len(failed)} 个过、{len(failed)} 个没过"
          f"（六源清单共 {len(EXPECTED_SOURCES)} 个，缺 {len(missing)} 个没验）")
    if not args.no_prefilter:
        # 这一行就是剧本段 5 的预估调用数的来源（run_onsite.sh 的 SEGMENTS 表）
        tail = (f"　→ 真实抽取预估调用数 {total_kept}" if not_for_extraction is None
                else f"　→ 粒度参考 {total_kept} 块（**不进抽取**，不是调用数）")
        print(f"合计（{len(present)} 个源）：切 {format_prefilter_summary(total_blocks, total_counts)}{tail}")
    if not failed:
        if not_for_extraction is None:
            print("★ 阈值检查全过。**但阈值只能排除明显切错**——请人工看一遍上面打印的"
                  "前几块原文，确认切出来的是一味药 / 一张方，再去跑真实抽取。")
        else:
            print("★ 阈值检查全过。这份语料**不进药理层抽取**（理由见上面那行 ⚠），"
                  "这里看的是段落粒度：一块应该是一条医案 / 一段正文，不是上万字的一整篇。")
        return 0
    print("✗ 以下源没过阈值检查：", file=sys.stderr)
    for name, problems in failed.items():
        for p in problems:
            print(f"  {name}：{p}", file=sys.stderr)
    print("处置：换 --chunk-by（教材用 heading、有的古籍 txt 一条一行用 line）、"
          "或先手工把条目切细、"
          "或确认下载是否残缺（字节数校验见 fetch_pharmacology_sources.sh）。"
          "**不要带着切错的块去跑抽取**——一块一次调用，几百次调用会全部作废。",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
