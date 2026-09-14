"""把 .docx 医案转成抽取链路能吃的 UTF-8 txt。

用户手上那两份现代医案（王云启治癌验案录、李可医案）是 docx，而全项目的抽取
入口（`offline/split_cases.py`、`offline/extract_reference_triples.py`）只读 txt。
这个脚本只做格式转换，**不做任何内容判断**——切分、抽取、打标都在各自的环节。

    python -m offline.docx_to_text --input 李可医案.docx --out books/李可医案.txt
    python -m offline.docx_to_text --input x.docx --out x.txt --dry-run

退出码：0 成功；1 转换失败；2 环境不具备（没装 python-docx）。

## 段落之间留空行，因为下游按空行切块

`extract_reference_triples.split_blocks` 默认以空行为界，一块应该是一个条目。
docx 的一个"段落"通常就是一段话（一条医案的一段），所以这里的规则是：
**每个非空段落一行，段落之间空一行**。这样下游 blank-line 切块得到的一块 =
docx 的一个段落。docx 里本来就有的空段落（排版用的）不重复输出空行——那会
把一块切成两块。

**转完必须跑一次切块验证**（`scripts/verify_pharmacology_chunks.py --file`）：
docx 的段落粒度因人而异，有人一整篇医案一个段落、有人一句话一个段落，前者
切出来是一个上万字的块（注定顶到 max_tokens），后者切出来是几百个半句。
这件事看统计数字比看原文快，所以它是独立的一步、不塞进这个脚本。

## 表格

docx 里的表格不在 `document.paragraphs` 里。医案 docx 偶尔用表格排"药物-剂量"，
漏掉表格等于漏掉处方，所以表格按行输出、单元格用制表符分隔（`--skip-tables`
可关）。**不猜表头、不重排列**：这里是格式转换，理解表格语义是抽取环节的事。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def docx_paragraphs(path: Path, include_tables: bool = True) -> list[str]:
    """按文档顺序取出非空段落文本（可选含表格行）。

    **惰性 import python-docx**：全项目只有这一个脚本需要它，模块顶层 import
    会让没装它的机器连 `python -m offline.xxx` 别的脚本都跑不了
    （CLAUDE.md「加载大文件的对象一律惰性初始化」的同一条理由）。
    """
    try:
        import docx  # python-docx
    except ImportError as e:  # pragma: no cover - 装了就走不到
        raise SystemExit(
            "没装 python-docx（`pip install python-docx`，已在 requirements.txt 里）。"
            "注意包名是 python-docx、import 名是 docx，装成 `pip install docx` "
            "会装到一个同名的废弃包上。"
        ) from e

    document = docx.Document(str(path))
    out = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    if include_tables:
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    out.append("\t".join(cells))
    return out


def to_text(paragraphs: list[str]) -> str:
    """段落之间空一行（下游按空行切块，一块 = 一个段落）。"""
    return "\n\n".join(paragraphs) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="docx 医案 → UTF-8 txt（只做格式转换）")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None,
                    help="默认跟输入同名同目录、扩展名换成 .txt")
    ap.add_argument("--skip-tables", action="store_true",
                    help="不抽 docx 表格。默认抽——医案 docx 偶尔用表格排药物剂量，"
                         "漏掉表格等于漏掉处方")
    ap.add_argument("--dry-run", action="store_true", help="只报段落数和前几段，不写文件")
    ap.add_argument("--preview", type=int, default=3, help="--dry-run 打印前几段")
    args = ap.parse_args(argv)

    if not args.input.exists():
        print(f"未找到 {args.input}", file=sys.stderr)
        return 1
    paragraphs = docx_paragraphs(args.input, include_tables=not args.skip_tables)
    if not paragraphs:
        print(f"{args.input} 里一个非空段落都没有——确认这是不是一个有正文的 docx"
              "（扫描版 docx 只有图片，正文要先 OCR）。", file=sys.stderr)
        return 1

    lengths = [len(p) for p in paragraphs]
    print(f"{args.input.name}：{len(paragraphs)} 个非空段落，"
          f"共 {sum(lengths)} 字，最长段落 {max(lengths)} 字")
    for i, p in enumerate(paragraphs[: args.preview]):
        print(f"  --- 第 {i} 段（{len(p)} 字）---")
        print(f"    {p[:200]}{'…' if len(p) > 200 else ''}")

    if args.dry_run:
        print("--dry-run：不写文件。")
        return 0

    out = args.out or args.input.with_suffix(".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_text(paragraphs), encoding="utf-8")
    print(f"已写出 {out}（UTF-8，段落之间空一行）")
    print("下一步（零成本，必做）：确认段落粒度适合按空行切块——")
    print(f"  python -m scripts.verify_pharmacology_chunks --file {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
