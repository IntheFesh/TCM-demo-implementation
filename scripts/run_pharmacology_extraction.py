"""段 5 的批量入口：按 offline/pharmacology_sources.EXPECTED_SOURCES 逐个源调
extract_materia_medica / extract_formulary（R8-3）。

R8 之前剧本段 5 写的是 `python -m offline.extract_materia_medica --limit 5`——
引擎的 --input/--source/--book 三个参数都是必填，这条命令在 argparse 那一步就退出
（退出码 2），段 5 在真机上一步都跑不了；就算补上参数，六个源要六条命令、各自的
--book/--source/--chunk-by 都得手抄，方剂学那本还得换入口脚本。这里从那张表取
参数，人只给 --limit-blocks / --dry-run / --crosscheck。

    python -m scripts.run_pharmacology_extraction --dry-run           # 预估调用数（预过滤后），零调用
    python -m scripts.run_pharmacology_extraction --limit-blocks 5    # 每个源抽 5 块看质量（段 5 卡点）
    python -m scripts.run_pharmacology_extraction --crosscheck        # 全量 + DOSE_LIMITS 交叉校验
    python -m scripts.run_pharmacology_extraction --only-source 方剂学.md

退出码：0 = 在场的源都跑完；1 = 有源抛了异常（**其它源照跑**，最后汇总）；
2 = 一个源都没找到（那是"还没下载"，先跑 scripts/fetch_pharmacology_sources.sh）。

## 一个源挂了不影响别的源

跟 run_onsite.sh「一段失败不影响后面的段」同一条理由：六个源是六批独立的调用，
方剂学那本抽到一半 API 抖了，不该让神农本草经那批也不跑。每个源的异常记下来，
最后一起报，退出码 1。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.progress import Progress  # noqa: E402
from offline import extract_formulary, extract_materia_medica  # noqa: E402
from offline.local_corpora import non_pharmacology_corpora  # noqa: E402
from offline.pharmacology_sources import EXPECTED_SOURCES, book_title  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOOKS_DIR = ROOT / "books"

# kind -> 入口脚本的 main。两个入口都是引擎 run() 的薄包装，这里只负责选对。
ENTRY_MAINS = {
    "materia_medica": extract_materia_medica.main,
    "formulary": extract_formulary.main,
}


def build_argv(name: str, path: Path, limit_blocks: int | None, dry_run: bool,
               no_prefilter: bool, crosscheck: bool) -> list[str]:
    """一个源的引擎参数，全部从 EXPECTED_SOURCES 取，不手抄。
    --crosscheck 只有本草入口认（方剂没有用量可对），所以按 kind 决定加不加。"""
    source, kind, chunk_by, _purpose = EXPECTED_SOURCES[name]
    argv = ["--input", str(path), "--source", source, "--book", book_title(name),
            "--chunk-by", chunk_by]
    if limit_blocks is not None:
        argv += ["--limit-blocks", str(limit_blocks)]
    if dry_run:
        argv.append("--dry-run")
    if no_prefilter:
        argv.append("--no-prefilter")
    if crosscheck and kind == "materia_medica":
        argv.append("--crosscheck")
    return argv


def print_local_corpora_accounting() -> None:
    """**默认行为要说出来**：本地语料（data/local_corpora/）一份都不在这次抽取里。
    不打这一行，"六个源"看起来就像是"所有语料"，而用户刚在段 1 看到那几份语料被
    切成了上千块。每份的理由从 offline/local_corpora.py 的声明表取，不在这里重写。"""
    specs = non_pharmacology_corpora()
    if not specs:
        return
    n_oos = sum(1 for s in specs if s.out_of_scope)
    print(f"本地语料：{len(specs)} 份**都不在药理层抽取范围内**"
          f"（其中 out_of_scope {n_oos} 份），不计入下面的预估调用数：")
    for spec in specs:
        print(f"  - {spec.target}：{spec.pharmacology_reason}")
    print("  （引擎也会拦：直接 --input 指到它们会被拒绝，除非加 --include-out-of-scope）")
    print()


def run_all(books_dir: Path, limit_blocks: int | None = None, dry_run: bool = False,
            no_prefilter: bool = False, crosscheck: bool = False,
            only_source: str | None = None) -> int:
    names = [n for n in EXPECTED_SOURCES if only_source is None or n == only_source]
    if only_source is not None and not names:
        print(f"--only-source {only_source!r} 不在六源清单里，可选：{'、'.join(EXPECTED_SOURCES)}",
              file=sys.stderr)
        return 2
    present = [n for n in names if (books_dir / n).exists()]
    missing = [n for n in names if n not in present]
    if not present:
        print(f"一个源都没找到（找的是 {books_dir}）。先跑 bash scripts/fetch_pharmacology_sources.sh"
              "——这不是「抽取失败」，是「还没下载」，退出码 2。", file=sys.stderr)
        return 2
    if missing:
        print(f"以下源不在 {books_dir}，本次跳过（不算失败，但它们没有被抽）：{'、'.join(missing)}")
    if dry_run:
        print()
        print_local_corpora_accounting()

    failures: dict[str, str] = {}
    # 源级进度（六个源）；**源内每一块的进度由引擎自己的进度条打**
    # （offline/extract_reference_triples.py 里那一个），两层各管一层，
    # 不在这里另算一遍块数。--dry-run 时不打（它本来就是几行就完）。
    bar = None if dry_run else Progress(total=len(present), label="药理层抽取（按源）", unit="源")
    for name in present:
        _source, kind, _chunk_by, purpose = EXPECTED_SOURCES[name]
        argv = build_argv(name, books_dir / name, limit_blocks, dry_run, no_prefilter, crosscheck)
        print()
        print(f"=== {name}　[{kind}]　{purpose}　→ {' '.join(argv[2:])} ===")
        try:
            ENTRY_MAINS[kind](argv)
            if bar is not None:
                bar.advance(note=name)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - 一个源挂了不影响别的源，见模块文档
            failures[name] = f"{type(e).__name__}: {e}"
            print(f"  ✗ {name} 没跑完：{failures[name]}", file=sys.stderr)
            if bar is not None:
                bar.note(f"{name} 没跑完：{type(e).__name__}")
    if bar is not None:
        bar.close(f"{len(present) - len(failures)}/{len(present)} 个源跑完")

    print()
    print("=" * 70)
    print(f"跑了 {len(present)} 个源，{len(present) - len(failures)} 个跑完、{len(failures)} 个没跑完"
          f"（六源清单共 {len(EXPECTED_SOURCES)} 个，缺 {len(missing)} 个没抽）")
    for name, why in failures.items():
        print(f"  ✗ {name}：{why}", file=sys.stderr)
    if failures:
        print("处置：看上面各源自己打出来的「这些块值得重跑：--only-blocks …」，或者 "
              "--only-source <文件名> 单独重跑那一个源。", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="段 5：按六源清单批量跑药理层抽取（真实 LLM 调用）")
    ap.add_argument("--books-dir", type=Path, default=DEFAULT_BOOKS_DIR)
    ap.add_argument("--limit-blocks", type=int, default=None,
                    help="每个源只抽（预过滤后的）前 N 块——段 5 卡点先看 5 块的质量再全量")
    ap.add_argument("--dry-run", action="store_true", help="只打印每个源的块数和预估调用数，零调用")
    ap.add_argument("--no-prefilter", action="store_true", help="关掉块级预过滤（透传给引擎）")
    ap.add_argument("--crosscheck", action="store_true",
                    help="本草那几本抽完后跟 DOSE_LIMITS 交叉校验（透传给 extract_materia_medica）")
    ap.add_argument("--only-source", default=None, help="只跑清单里的这一个文件名")
    args = ap.parse_args(argv)
    return run_all(args.books_dir, limit_blocks=args.limit_blocks, dry_run=args.dry_run,
                   no_prefilter=args.no_prefilter, crosscheck=args.crosscheck,
                   only_source=args.only_source)


if __name__ == "__main__":
    sys.exit(main())
