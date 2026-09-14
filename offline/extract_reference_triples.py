"""阶段二（药理层）：从本草 / 中药学 / 方剂学原文里抽三元组的**共用引擎**。
两个入口脚本（offline/extract_materia_medica.py、offline/extract_formulary.py）
只是把 kind 固定住再调这里——同一套"切块 → 调模型 → 逐字核验 source_span →
落盘"的逻辑只实现一次（CLAUDE.md「同一概念只能有一处实现」），两个 kind
只差 prompt、schema、输出文件。

跟 offline/extract_case_triples.py（X3）是同一个形状，理由也一样：
  - **真实 LLM 抽取，不是确定性转换。** 输入是整本书的原文，一块（默认按
    空行分块，一块通常是一味药 / 一张方的条目）调一次模型。
  - **source_span 的防幻觉核验在这里做，不在 pydantic schema 里。** schema
    只能校验非空，校验不了"这段文字是不是真的在这一块原文里"；核验不过的
    三元组整条丢弃、计数、如实报出，不静默吞。
  - **古籍与现代分开抽、分开存。** 每条带 source（classic | modern）和 book，
    由调用方 --source/--book 打标签，引擎不猜原文是哪个年代——古籍说"细辛，
    味辛温"，药典说"辛、温，归心肺肾经，1~3g"，术语和精度不同，混在一起会
    重演 λ1 那个"证型 0/116 对不上"的教训（SOURCES.md 第 7 节）。
  - **批处理要扛得住单条失败。** 截断（LLMTruncatedError）跳过计数不重跑；
    其余 LLMError 记下块号、支持 --only-blocks 重跑那几块；每 PROGRESS_EVERY
    块落盘一次。

产出文件放 data/ 下（data/materia_medica.jsonl、data/formulary.jsonl），跟
data/case_triples.jsonl 一样是"在有真实 LLM 的机器上生成的产物"，不进版本
控制（.gitignore 只给 data/standard/*.jsonl 开了例外，那是人工整理的静态表，
见 CLAUDE.md「已知的坑」）。

用法：
    python -m offline.extract_materia_medica --input books/xxx.txt --source classic --book 神农本草经
    python -m offline.extract_formulary --input books/方剂学.txt --source modern --book 方剂学 --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from core.llm import LLMError, LLMTruncatedError, get_llm, load_prompt, render
from core.schemas import (
    FormularyExtraction,
    FormularyRecord,
    MateriaMedicaExtraction,
    MateriaMedicaRecord,
)
from offline.pharmacology_sources import format_prefilter_summary, prefilter_blocks

ROOT = Path(__file__).resolve().parent.parent
# 每处理完这么多块报一次进度、落盘一次——跟 X3 同一个理由（中途崩了不该把
# 已经跑完的结果一起丢掉），也是同一个折中值。
PROGRESS_EVERY = 50
# 跟 S5 同一个上限、同一个理由：一味药/一张方的条目抽出十几条三元组时，
# source_span 一多就容易顶到默认 8192。
MAX_TOKENS = 16384
# 短于这个长度的块（章节标题、页码、"第三节"）不喂给模型：喂了只会诱导它
# 从几个字里编出三元组。
MIN_BLOCK_CHARS = 8

SOURCES = ("classic", "modern")
CHUNK_MODES = ("blank-line", "line", "heading")
# markdown 的 ATX 标题（# ～ ######）。十四五教材那批源是 .md（OCR 转出来的
# markdown），排版是「# 第N节 病名 / # N.证型名」这种层级标题 + 逐行标注的
# 字段，见 offline/build_syndrome_textbook.py 对同一批文件的解析规则。
_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
# 中医古籍 txt 库（xiaopangxia/TCM-Ancient-Books）的转录体例：`<篇名>药名` 是
# 条目标题，`<目录>卷一\上经` 是它上面的目录路径。两种行在 heading 模式下都
# 算标题行——跟 markdown 的「#」是同一个概念（一个标题到下一个标题之间是一块）。
# offline/split_cases.py 也按 `<篇名>` 切，但它回答的是另一个问题：「哪些篇名
# 属于脾胃门」（按 gate 过滤标题、剥掉 `属性：`），不是「怎么把整本书切成块」。
_GUJI_TITLE_RE = re.compile(r"^<(篇名|目录)>")
_GUJI_TOC_RE = re.compile(r"^<目录>")
_GUJI_PIAN_RE = re.compile(r"^<篇名>")
# **不该开新块的"标题"**（OCR 把两种东西也升成了 `#`，R8 实测）：
#   1. 字段标签本身：`# 【临床应用】`、`# 【炮制方法】`、`# 【现代研究】`。
#      临床中药学 333 味药里每一味的「用法用量」都在 `# 【临床应用】` 那一块——
#      按它开新块，那一块里没有药名，s 只能靠模型猜（353 处）；炮制学 437 处。
#   2. 页眉页脚：`# 106 中药炮制学`、`# 38 方剂学`、`# 75`、`# 16目录`——页码
#      开头、后面至多跟一个书名（OCR 有时把中间的空格吞掉）。方剂学 235 张方里
#      有一张（麻黄杏仁甘草石膏汤）被一个页眉从「证治机理」和「方解」之间劈成
#      两块。只认**页码在前**的形状：「痛泻要方 67」这种页码在后的是真标题带了
#      页码，仍然开新块；「1.分类」「2.制藤黄」这种编号标题（数字后面紧跟标点）
#      也不算——数字后面直接跟汉字才是吞了空格的页眉。四本教材逐一核过：这条
#      正则命中的 54 个标题全是页眉/目录页眉/编写说明页眉，没有一个条目。
_CONTINUATION_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:【|\d{1,4}(?:\s+\S{1,12}|[^\s\d.．、,，)）]\S{0,11})?\s*$)")
#   3. 出处行：`# 《金匮要略》`、`# Xiongdanfen（《新修本草》)`——条目标题下面那一行
#      "拼音 / 出处书名"被 OCR 也升成了 `#`。这一条是 R8 审查（review:chunker）在
#      真实数据上抓出来的：方剂学 35 张方、中药学 2 味药的正文都挂在这种出处行下面，
#      而真正的标题行（`# 大黄附子汤`，6 字）单独成块、短于 MIN_BLOCK_CHARS 被丢——
#      正是"块里没有药名、s 只能猜"的那个洞。只认"整行只有一个《书名》、前面至多
#      一段拉丁字母拼音"的形状：「一、现存最早的本草专著 一《神农本草经》」这种带
#      中文正文的真标题不算。
_SOURCE_LINE_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:[A-Za-z][A-Za-z ]*\s*)?[（(]?《[^》]+》[)）]?\s*$")


def _is_title_line(line: str) -> bool:
    if _GUJI_TITLE_RE.match(line):
        return True
    if not _HEADING_RE.match(line):
        return False
    return not (_CONTINUATION_HEADING_RE.match(line) or _SOURCE_LINE_HEADING_RE.match(line))


def _only_toc_lines(lines: list[str]) -> bool:
    return all(not line.strip() or _GUJI_TOC_RE.match(line) for line in lines)

OnProgress = Callable[[int, int, dict], None]
OnBlockDone = Callable[[int, list[BaseModel], "str | None"], None]


@dataclass(frozen=True)
class ReferenceKind:
    """一种参考文献三元组的全部差异：prompt、抽取 schema、落盘 schema、默认输出。"""

    name: str
    prompt: str
    extraction: type[BaseModel]
    record: type[BaseModel]
    default_out: Path


KINDS: dict[str, ReferenceKind] = {
    "materia_medica": ReferenceKind(
        name="materia_medica",
        prompt="s6_extract_materia_medica",
        extraction=MateriaMedicaExtraction,
        record=MateriaMedicaRecord,
        default_out=ROOT / "data" / "materia_medica.jsonl",
    ),
    "formulary": ReferenceKind(
        name="formulary",
        prompt="s7_extract_formulary",
        extraction=FormularyExtraction,
        record=FormularyRecord,
        default_out=ROOT / "data" / "formulary.jsonl",
    ),
}


def split_blocks(text: str, chunk_by: str = "blank-line", min_chars: int = MIN_BLOCK_CHARS) -> list[str]:
    """把整本原文切成一次调用一块。短于 min_chars 的块丢掉。纯函数，好单测。

    三种切法，对应三种真实排版：

      blank-line  空行为界。古籍本草的条目通常一味药一段。
      line        一行一块。有的古籍 txt 每条一条一行、通篇没有空行。
      heading     **markdown 标题为界**（一个标题到下一个标题之间算一块）。
                  十四五教材那批源是 .md，一味药的条目由「# 药名」加下面若干
                  个段落（性味/归经/功效/应用/用法用量）组成——**按空行切会把
                  一味药切成五六块，而其中「9～30g，水煎服」这种块里根本没有
                  药名**。那种块喂给模型，它要么抽不出、要么给一个猜的 `s`：
                  而 `s` 只有 `min_length=1` 约束、**不过 source_span 逐字
                  核验**（核验只管 o 的出处），所以一个猜错的药名会一路写进
                  data/materia_medica.jsonl 而没有任何一道闸门拦得住。
                  切块粒度在这里不是"效果好不好"的问题，是防幻觉的前提。

    heading 模式把标题行本身留在块里（药名就在那一行），第一个标题之前的内容
    （前言、目录）单独成一块——不丢掉它，是因为"丢了什么"应该由 min_chars 和
    切块验证脚本来判断，不该由切块函数偷偷决定。

    **R8 起 heading 模式也认古籍转录体例的标题行**（`<篇名>`/`<目录>`，见
    _GUJI_TITLE_RE）。R8 在真实数据上实测：两本古籍用 blank-line，`<篇名>丹沙`
    跟下一段 `内容：味甘微寒…` 之间隔着空行，被切成两块——前者 7 字、短于
    MIN_BLOCK_CHARS 直接被丢，神农本草经 379 味药里 350 味的药名进不了模型，
    模型看到的是一段没有主语的「味甘微寒。主…」。这跟上面教材按空行切的问题
    是同一个（药名跟字段不在一块），修法也是同一个（按标题切）。`<目录>` 行
    单独成块时会被下一个 `<篇名>` 并进来（"卷一\上经"「草部」是条目的品级/
    部类，留在块里有用），其它情况下各自成块。
    """
    if chunk_by not in CHUNK_MODES:
        raise ValueError(f"chunk_by 必须是 {CHUNK_MODES} 之一，收到 {chunk_by!r}")
    if chunk_by == "heading":
        raw_blocks, current = [], []
        for line in text.splitlines():
            if _is_title_line(line) and current and not (
                _GUJI_PIAN_RE.match(line) and _only_toc_lines(current)
            ):
                raw_blocks.append("\n".join(current))
                current = []
            current.append(line)
        if current:
            raw_blocks.append("\n".join(current))
    elif chunk_by == "line":
        raw_blocks = text.splitlines()
    else:
        raw_blocks, current = [], []
        for line in text.splitlines():
            if line.strip():
                current.append(line)
            elif current:
                raw_blocks.append("\n".join(current))
                current = []
        if current:
            raw_blocks.append("\n".join(current))
    return [b.strip() for b in raw_blocks if len(b.strip()) >= min_chars]


def plan_blocks(
    text: str, chunk_by: str, source: str | None, prefilter: bool = True,
) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str, str]], dict[str, int]]:
    """切块 + 预过滤，返回 (全部块, 保留块, 跳过块(带原因), 各类跳过计数)。
    run()、切块验证脚本、批量入口算"预估调用数"走的都是这一个函数——
    调用数 = 保留块数，三处各算一遍迟早对不上。块号一律是切块结果里的位置：
    预过滤只挑块、不重新编号，所以 --only-blocks 和输出行里的 _block 在开/关
    预过滤时指的是同一块。"""
    all_blocks = list(enumerate(split_blocks(text, chunk_by)))
    if not prefilter:
        return all_blocks, list(all_blocks), [], {}
    kept, skipped, counts = prefilter_blocks(all_blocks, source)
    return all_blocks, kept, skipped, counts


def extract_block(
    kind: ReferenceKind, block: str, source: str, book: str
) -> tuple[list[BaseModel], dict[str, int], str | None]:
    """对一块原文调一次模型，返回 (核验通过的记录, 丢弃计数, 失败原因)。
    失败原因三态跟 X3 一致：None 正常；"truncated" 截断不值得重跑；其余是
    底层异常类型名，值得事后 --only-blocks 重跑。"""
    if source not in SOURCES:
        raise ValueError(f"source 必须是 {SOURCES} 之一，收到 {source!r}")
    prompt = load_prompt(kind.prompt)
    system = render(prompt["system"], raw_text=block)
    try:
        extraction = get_llm().generate(
            system=system, user="", schema=kind.extraction, max_tokens=MAX_TOKENS,
        )
    except LLMTruncatedError:
        return [], {"span_not_found": 0}, "truncated"
    except LLMError as e:
        cause = e.__cause__
        return [], {"span_not_found": 0}, type(cause).__name__ if cause is not None else type(e).__name__

    records: list[BaseModel] = []
    rejected = {"span_not_found": 0}
    for item in extraction.triples:
        if item.source_span not in block:
            rejected["span_not_found"] += 1
            continue
        records.append(kind.record(
            s=item.s, p=item.p, o=item.o, source_span=item.source_span,
            source=source, book=book,
        ))
    return records, rejected, None


def extract_all(
    kind: ReferenceKind,
    blocks: list[tuple[int, str]],
    source: str,
    book: str,
    on_progress: OnProgress | None = None,
    on_block_done: OnBlockDone | None = None,
) -> tuple[list[BaseModel], dict]:
    """blocks 是 (块号, 原文) 列表——块号是它在整本书切块结果里的位置，
    --only-blocks 重跑时用它定位。回调语义跟 X3 的 extract_all 一致：
    on_block_done 每块必调（成功/截断/失败都调），on_progress 每
    PROGRESS_EVERY 块 + 最后一块各调一次，传的是快照不是活引用。"""
    all_records: list[BaseModel] = []
    total = len(blocks)
    stats = {
        "blocks": total, "blocks_no_triples": 0, "blocks_truncated": 0, "blocks_failed": 0,
        "failed_block_indexes": [], "failure_reasons": {},
        "triples_extracted": 0, "triples_rejected_span_not_found": 0, "llm_calls": 0,
    }
    for i, (block_index, block) in enumerate(blocks, start=1):
        records, rejected, failure = extract_block(kind, block, source, book)
        stats["llm_calls"] += 1
        if failure is not None:
            stats["failure_reasons"][failure] = stats["failure_reasons"].get(failure, 0) + 1
            if failure == "truncated":
                stats["blocks_truncated"] += 1
            else:
                stats["blocks_failed"] += 1
                stats["failed_block_indexes"].append(block_index)
        else:
            stats["triples_rejected_span_not_found"] += rejected["span_not_found"]
            if not records:
                stats["blocks_no_triples"] += 1
            stats["triples_extracted"] += len(records)
            all_records.extend(records)
        if on_block_done is not None:
            on_block_done(block_index, records, failure)
        if on_progress is not None and (i % PROGRESS_EVERY == 0 or i == total):
            snapshot = dict(stats)
            snapshot["failed_block_indexes"] = list(stats["failed_block_indexes"])
            snapshot["failure_reasons"] = dict(stats["failure_reasons"])
            on_progress(i, total, snapshot)
    return all_records, stats


def load_existing_rows(out_path: Path) -> dict[tuple[str, str, int], list[dict]]:
    """按 (book, source, 块号) 分组读已有输出，作为增量落盘 / --only-blocks
    重跑 / --append 换一本书的合并基准。文件不存在就是空字典，不是错误。
    每行带 _block 字段记块号——没有它，重跑某一块时不知道该替换哪些旧行。"""
    if not out_path.exists():
        return {}
    grouped: dict[tuple[str, str, int], list[dict]] = {}
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        key = (row.get("book", ""), row.get("source", ""), int(row.get("_block", -1)))
        grouped.setdefault(key, []).append(row)
    return grouped


def write_rows(out_path: Path, grouped: dict[tuple[str, str, int], list[dict]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for key in sorted(grouped):
            for row in grouped[key]:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_parser(kind_name: str | None) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="阶段二：从参考文献原文抽三元组（真实 LLM 调用）")
    if kind_name is None:
        ap.add_argument("--kind", choices=sorted(KINDS), required=True)
    ap.add_argument("--input", type=Path, required=True, help="整本原文 txt（UTF-8）")
    ap.add_argument("--source", choices=SOURCES, required=True,
                    help="classic=古籍本草/方书，modern=现代教材/药典；古籍与现代必须分开抽、分开存")
    ap.add_argument("--book", required=True, help="书名，写进每条记录的 book 字段")
    ap.add_argument("--out", type=Path, default=None, help="默认按 kind 取 data/materia_medica.jsonl 或 data/formulary.jsonl")
    ap.add_argument("--chunk-by", choices=CHUNK_MODES, default="blank-line")
    # --limit-blocks 是 --limit 的别名（R8）：剧本和文档里两个名字都出现过，
    # 跟 extract_case_triples 的 --limit 同形。一个参数两个拼法，不是两个参数。
    ap.add_argument("--limit", "--limit-blocks", dest="limit", type=int, default=None,
                    help="只处理（预过滤后的）前 N 块，调试/控成本用")
    ap.add_argument("--only-blocks", default="", help="只重跑这些块号（逗号分隔），结果按块号合并进 --out")
    ap.add_argument("--no-prefilter", action="store_true",
                    help="关掉块级预过滤（R8-1：默认按源类型跳过过短/表格/超长/无结构标记的块，"
                         "见 offline/pharmacology_sources.py）。只在核对预过滤本身时用")
    ap.add_argument("--dry-run", action="store_true", help="只打印块数（=预估调用数），不真的调模型")
    return ap


def run(argv: list[str] | None, kind_name: str | None, after_write=None) -> None:
    """两个入口脚本共用的 main。after_write(records_as_rows, args) 是入口脚本
    挂自己收尾动作的钩子（extract_materia_medica.py 挂 DOSE_LIMITS 交叉校验）。"""
    args = build_parser(kind_name).parse_args(argv)
    kind = KINDS[kind_name or args.kind]
    out_path = args.out or kind.default_out

    if not args.input.exists():
        raise FileNotFoundError(f"未找到 {args.input}——整本原文 txt 要自己准备，见 README 3.1 的下载说明。")
    text = args.input.read_text(encoding="utf-8")
    all_blocks, blocks, skipped, counts = plan_blocks(
        text, args.chunk_by, args.source, prefilter=not args.no_prefilter)
    prefilter_line = ("预过滤已关（--no-prefilter）" if args.no_prefilter
                      else "预过滤：" + format_prefilter_summary(len(all_blocks), counts))
    if args.only_blocks:
        wanted = {int(x) for x in args.only_blocks.split(",") if x.strip()}
        missing = wanted - {i for i, _ in all_blocks}
        if missing:
            raise SystemExit(f"--only-blocks 里这些块号超出范围（共 {len(all_blocks)} 块）：{sorted(missing)}")
        filtered = {i: reason for i, reason, _ in skipped if i in wanted}
        if filtered:
            # 点名要跑的块被预过滤挡住了，要说清楚是哪一类原因、怎么绕——不能静默
            # 少跑几块让人以为跑过了
            raise SystemExit(
                f"--only-blocks 里这些块被预过滤跳过了：{filtered}。"
                "确认要跑它们就加 --no-prefilter。")
        blocks = [(i, b) for i, b in blocks if i in wanted]
    if args.limit is not None:
        blocks = blocks[: args.limit]

    if args.dry_run:
        print(f"--dry-run：{args.input} 切成 {len(all_blocks)} 块（chunk_by={args.chunk_by}）；"
              f"{prefilter_line}；本次将处理 {len(blocks)} 块 = 预估调用数 {len(blocks)}，不真的调模型")
        return
    print(prefilter_line)

    by_block = load_existing_rows(out_path)

    def _on_block_done(block_index: int, records: list[BaseModel], failure: str | None) -> None:
        if failure is None:
            rows = [r.model_dump() for r in records]
            for row in rows:
                row["_block"] = block_index
            by_block[(args.book, args.source, block_index)] = rows

    def _on_progress(done: int, total: int, snapshot: dict) -> None:
        print(f"进度 {done}/{total}：抽出 {snapshot['triples_extracted']} 条，"
              f"{snapshot['blocks_truncated']} 块疑似截断，{snapshot['blocks_failed']} 块调用失败")
        write_rows(out_path, by_block)

    records, stats = extract_all(
        kind, blocks, args.source, args.book,
        on_progress=_on_progress, on_block_done=_on_block_done,
    )

    print(f"{kind.name}：{args.book}（{args.source}）切成 {len(all_blocks)} 块，本次处理 {stats['blocks']} 块，"
          f"调用 {stats['llm_calls']} 次，抽出 {stats['triples_extracted']} 条通过核验，"
          f"{stats['triples_rejected_span_not_found']} 条因 source_span 在原文里找不到被丢弃，"
          f"{stats['blocks_no_triples']} 块一条都没抽出")
    if stats["blocks_truncated"]:
        print(f"警告：{stats['blocks_truncated']} 块输出疑似截断（撞 max_tokens={MAX_TOKENS}），已跳过——"
              "重跑没有意义，块太大就换 --chunk-by 或先手工把条目切细。")
    if stats["blocks_failed"]:
        ids = ",".join(str(i) for i in stats["failed_block_indexes"])
        print(f"警告：{stats['blocks_failed']} 块调用失败（非截断），失败原因分布：{stats['failure_reasons']}")
        print(f"这些块值得重跑：--only-blocks {ids} --input {args.input} --source {args.source} "
              f"--book {args.book} --out {out_path}")

    write_rows(out_path, by_block)
    n_rows = sum(len(v) for v in by_block.values())
    print(f"已写出 {out_path}（{n_rows} 条，含本次未重新处理、从旧文件原样保留的部分）")
    if after_write is not None:
        after_write([row for rows in by_block.values() for row in rows], args)


def main(argv: list[str] | None = None) -> None:
    run(argv, kind_name=None)


if __name__ == "__main__":
    main()
