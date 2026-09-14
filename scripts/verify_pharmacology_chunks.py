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

# 六个源：文件名 -> (source 标签, kind, 推荐切块模式, 用途)。跟
# scripts/fetch_pharmacology_sources.sh 的 SOURCES 表一一对应；那边负责下载，
# 这边负责验切块。**两处都列一遍是刻意的**：下载脚本是 bash、这里是 Python，
# 强行共用一份表要引入一个中间文件，而这张表一年动不了一次，代价不划算。
# 加/删源时两处都要改——tests/test_pharmacology_prep.py 有一条测试逐字段比对
# 两张表（文件名、source、切块模式），漏改一处会红。
#
# **教材是 .md（markdown）不是 .txt**，而且推荐切块模式是 heading 不是
# blank-line：一味药的条目是「# 药名」加下面若干段（性味/归经/功效/用法用量），
# 按空行切会把一味药切成五六块、其中"用量"那块里根本没有药名。见
# offline/extract_reference_triples.split_blocks 的文档字符串（那里写了为什么
# 这不是"效果好不好"而是防幻觉的前提）。
EXPECTED_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "中药学.md": ("modern", "materia_medica", "heading", "性味归经功效用量"),
    "临床中药学.md": ("modern", "materia_medica", "heading", "临床用量、配伍"),
    "中药炮制学.md": ("modern", "materia_medica", "heading", "炮制方法与目的"),
    "方剂学.md": ("modern", "formulary", "heading", "方剂组成、君臣佐使、加减法"),
    "000-神农本草经.txt": ("classic", "materia_medica", "blank-line", "古籍本草"),
    "018-本草备要.txt": ("classic", "materia_medica", "blank-line", "古籍本草"),
}

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

# BLOCK_MAX_CHARS = 10000：单块上万字一定有问题。这个数还有一个硬依据——
# 引擎的 MAX_TOKENS=16384，而中文约 1 token/字，一块一万字的输入加上
# schema hint 和输出，会直接顶到上限被判截断（LLMTruncatedError），
# 那一块的调用是纯浪费。所以这不只是"不像一个条目"，是"这一块注定抽不出来"。
BLOCK_MAX_CHARS = 10000

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


def report_source(path: Path, label: tuple[str, str, str, str], chunk_by: str,
                  show: int, preview_chars: int, compare: bool = False) -> list[str]:
    """打印一个源的切块统计 + 前 N 块原文，返回问题清单。

    **原文必须打出来。** 统计数字能说"切成了 800 块、中位数 400 字"，说不了
    "切出来的是不是一味药"——那只有人看原文才判得出来。这个脚本的主要产出
    其实就是这几段原文，阈值检查只是顺手能自动化的那部分。
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
          f"（= 真实抽取的调用数）")
    print(f"每块字数：中位数 {stats['median_chars']}　均值 {stats['mean_chars']}　"
          f"最短 {stats['min_chars']}（第 {stats['shortest_index']} 块）　"
          f"最长 {stats['max_chars']}（第 {stats['longest_index']} 块）")
    problems = check_anomalies(stats)
    for p in problems:
        print(f"  ✗ {p}")
    if not problems:
        print("  ✓ 三个阈值检查都过")
    print()
    n = min(show, stats["n_blocks"])
    if n:
        print(f"前 {n} 块原文（**人工确认切的是不是一味药 / 一张方**）：")
        for i, block in enumerate(stats["blocks"][:n]):
            print(f"  --- 第 {i} 块（{len(block)} 字）---")
            for line in _preview(block, preview_chars).splitlines():
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
    args = ap.parse_args(argv)

    print("**零 LLM 调用**：只切块 + 统计 + 打印原文。一块 = 真实抽取的一次调用，"
          "所以这一步过不了就不要去跑抽取。")
    print("切块模式：" + (f"命令行指定 {args.chunk_by}（对所有源生效）"
                         if args.chunk_by else "按每个源的推荐值（教材 heading、古籍 blank-line）")
          + "——真实抽取要传跟这里一致的 --chunk-by")
    print()

    if args.file is not None:
        # 不在六源清单里的文件（docx 转出来的医案 txt 之类）默认按 blank-line，
        # 并标明它不在清单里——不猜它该用哪种切法。
        targets = [(args.file, EXPECTED_SOURCES.get(
            args.file.name, ("unknown", "unknown", "blank-line", "（不在六源清单里）")))]
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
    for path, label in present:
        problems_by_source[path.name] = report_source(
            path, label, args.chunk_by, args.show, args.preview_chars,
            compare=args.compare_modes)

    print("=" * 70)
    failed = {k: v for k, v in problems_by_source.items() if v}
    print(f"验了 {len(present)} 个源，{len(present) - len(failed)} 个过、{len(failed)} 个没过"
          f"（六源清单共 {len(EXPECTED_SOURCES)} 个，缺 {len(missing)} 个没验）")
    if not failed:
        print("★ 阈值检查全过。**但阈值只能排除明显切错**——请人工看一遍上面打印的"
              "前几块原文，确认切出来的是一味药 / 一张方，再去跑真实抽取。")
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
