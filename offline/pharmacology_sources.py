"""药理层六个源的**唯一一张表** + 按源类型的**块级预过滤**（R8-1）。

这张表原来住在 scripts/verify_pharmacology_chunks.py 里。R8 把它挪到 offline/
是因为现在有三个消费者：切块验证（scripts/）、抽取引擎（offline/
extract_reference_triples.py 的预过滤）、批量抽取入口（scripts/
run_pharmacology_extraction.py）。引擎不能反过来 import scripts/，所以表要放在
最底层。切块验证脚本仍然 `from offline.pharmacology_sources import
EXPECTED_SOURCES` 再导出同名，调用方不用改。

## 为什么要预过滤（R8 AutoDL 实测）

六个源切完是 4818 块 = 4818 次调用 ≈ ¥26.5，而剧本段 5 的预算写的是 500——
差 9.6 倍。差出来的那些块不是条目：教材的封面/CIP/公众号/目录/药名索引表
（一块 14496 字、235 个 `<tr>`）、章节导语、复习思考题；古籍的 `<目录>` 行、
序言。喂给模型只会诱导它从几个字里编三元组，**每一块都是白花的钱**。

## 判据只有结构，没有关键词黑名单

「公众号」「主编」「版权页」这种黑名单是打地鼠：下一本书换个词就漏。这里的
判据全部是**排版结构**，而且**按源类型分**——教材和古籍的条目长得根本不一样：

  modern（十四五教材 .md）  一个条目 = 「# 药名」+ 若干 `【字段】`。四本书的
        字段名各不相同（中药学是【功效】【用法用量】，临床中药学是【处方用名】
        【基本功效】，炮制学是【炮制方法】【质量要求】，方剂学是【组成】【主治】），
        所以判据不是"含某个字段"，是"**含任何一个 `【…】` 字段标签**"——章节
        导语、目录、索引表、复习题里一个都没有（实测）。
  classic（中医古籍 txt）    一个条目 = `<篇名>药名` + `内容：…`/`属性：…` 正文
        （xiaopangxia/TCM-Ancient-Books 全库的转录体例），或者一段带**剂量词**
        （钱/两/分/枚/铢）的方药——后者给《脾胃论》这类不带 `<篇名>` 标记、
        条目是方剂的本子用。序言、`<目录>` 行、版权页三样都没有。

判据挂在这张表的 `source` 列上（`ENTRY_PREDICATES` 按 source 取），**不另存
一份"这个文件是教材还是古籍"的判断**——抽取引擎的 `--source` 参数、切块验证
的标签、这里的谓词，读的是同一列。

## 五类跳过，按这个顺序判

  过短        短于 MIN_ENTRY_CHARS（见那个常数的依据）
  表格        HTML 表格占比超过 TABLE_RATIO_MAX（排成 HTML 表的药名-页码索引）
  索引        正文主要是「药名 + 页码」行（排成纯文本的同一种东西，见
              INDEX_LINE_RATIO_MAX）
  超长        长于 BLOCK_MAX_CHARS（跟切块验证的阈值是同一个常数，理由在那里）
  无结构标记  该源类型的结构判据不命中

顺序有讲究：先判便宜的、跟源类型无关的四类，最后才判结构——这样 `--dry-run`
打出来的五个数加起来等于跳过总数，人一眼能看出"被丢的主要是什么"。
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

# 文件名 -> (source 标签, kind, 推荐切块模式, 用途)。跟
# scripts/fetch_pharmacology_sources.sh 的 SOURCES 表一一对应；那边负责下载，
# 这边负责验切块和抽取。**两处都列一遍是刻意的**：下载脚本是 bash、这里是
# Python，强行共用一份表要引入一个中间文件，而这张表一年动不了一次，代价
# 不划算。加/删源时两处都要改——tests/test_pharmacology_prep.py 有一条测试
# 逐字段比对两张表（文件名、source、切块模式），漏改一处会红。
#
# 切块模式**六个源现在全是 heading**：教材是 markdown 的「# 药名」；古籍是
# 转录体例的 `<篇名>药名`——R8 实测 blank-line 会把 `<篇名>丹沙`（7 字）跟
# 下一段的 `内容：味甘微寒…` 切成两块，前者短于 MIN_BLOCK_CHARS 直接被丢，
# 于是 379 味药里 350 味的药名根本进不了模型（见 split_blocks 的文档字符串）。
EXPECTED_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "中药学.md": ("modern", "materia_medica", "heading", "性味归经功效用量"),
    "临床中药学.md": ("modern", "materia_medica", "heading", "临床用量、配伍"),
    "中药炮制学.md": ("modern", "materia_medica", "heading", "炮制方法与目的"),
    "方剂学.md": ("modern", "formulary", "heading", "方剂组成、君臣佐使、加减法"),
    "000-神农本草经.txt": ("classic", "materia_medica", "heading", "古籍本草"),
    "018-本草备要.txt": ("classic", "materia_medica", "heading", "古籍本草"),
}

SOURCE_TYPES = ("classic", "modern")


def book_title(filename: str) -> str:
    """文件名 -> 写进记录 book 字段的书名：去掉扩展名和古籍库的三位数编号前缀
    （"000-神农本草经.txt" -> "神农本草经"，"中药学.md" -> "中药学"）。"""
    return re.sub(r"^\d{3}-", "", Path(filename).stem)


# ---- 阈值，以及每个数的依据 ----

# BLOCK_MAX_CHARS = 10000：单块上万字一定有问题。硬依据——引擎的 MAX_TOKENS=16384，
# 中文约 1 token/字，一块一万字的输入加上 schema hint 和输出会直接顶到上限被判
# 截断（LLMTruncatedError），那一块的调用是纯浪费。所以这不只是"不像一个条目"，
# 是"这一块注定抽不出来"。切块验证的第三个阈值用的也是这个常数。
BLOCK_MAX_CHARS = 10000

# MIN_ENTRY_CHARS = 30：一个条目至少要有"药名 + 一个字段标签 + 一个值"。
# 实测六个源里结构判据命中的最短块：教材 21～62 字（临床中药学那个 21 字的是
# OCR 把一味药切残了），古籍 `<篇名>X` + `内容：味X，X。主X。` 的最短也在 30 字
# 上下。定 30 是取在"真条目的下沿"，不是取在"垃圾的上沿"——比它短的块里能有的
# 只是标题行、页码、「第三节」。引擎原来的 MIN_BLOCK_CHARS=8 是切块函数的下限
# （防止空块），不是"像不像条目"的判据，两个数回答的不是同一个问题。
MIN_ENTRY_CHARS = 30

# TABLE_RATIO_MAX = 0.8：块里 <table>…</table> 占的字符比例。实测教材的
# 药名-页码索引表、教材目录、各专业课时表这类块占比 0.84～0.99；而**条目里
# 自带**的小表格（临床中药学「用药甄别」的对比表、方剂学附方表）占比 0.2～0.72。
# 两群之间在 0.72～0.84 有一个空档，0.8 取在空档里。
TABLE_RATIO_MAX = 0.8

# INDEX_LINE_RATIO_MAX = 0.5：块里「药名 + 页码」形状的正文行占比。目录页的附药
# 索引（`# 附药：海金沙藤 196` + `石韦 196 / 冬葵子 196 / 灯心草 197`）跟第 803 块
# 那个 HTML 索引表是同一种东西，只是没排成表格，所以表格占比那条拦不住它。
# 实测这两群分得很开：那一块是 1.0，而中药学 15 个**真**附药条目全是 0.0
# （它们的正文是散文「性味甘，平；归脾、胃经…常用量 3～15g」，行尾没有页码）。
# 0.5 取在中间，两边都有 0.5 的余量。不足 2 行正文的块不判这一类——一行的巧合
# 不构成"这是个索引"。
INDEX_LINE_RATIO_MAX = 0.5
INDEX_MIN_LINES = 2

SKIP_TOO_SHORT = "过短"
SKIP_TABLE = "表格"
SKIP_INDEX = "索引"
SKIP_TOO_LONG = "超长"
SKIP_NO_STRUCTURE = "无结构标记"
SKIP_REASONS = (SKIP_TOO_SHORT, SKIP_TABLE, SKIP_INDEX, SKIP_TOO_LONG, SKIP_NO_STRUCTURE)

# 教材条目的字段标签：`【功效】`「【用法用量】」……四本书各有各的字段名，
# 判据是"有任何一个"，见模块文档。**必须在行首**：条目的字段标签总是一段的
# 开头（"【处方用名】王不留行…"），而总论正文里也会提到字段名（炮制学：
# "2020 年版《中国药典》…其中【炮制】项下有…"），那是句中引用，不是条目——
# 不限行首会把这类总论段放进来（实测炮制学 +15 块、中药学 +1 块）。上限 12 字
# 是为了不把「【」开头的整句话（OCR 偶尔把书名号识别成方头括号）当成标签。
_FIELD_LABEL_RE = re.compile(r"^\s*【[^】]{1,12}】", re.M)
# 教材的**附药子条目**：「# 附药：葛花」「# 附药：绿豆衣、赤小豆、黑豆」。这是十四五
# 教材的排版约定（正条目后面附几味近缘药），字段刻意写成散文（"性味甘，平；归脾、
# 胃经。功能解酒毒…常用量 3～15g"），没有 `【】`。R8 审查（review:prefilter）在真实
# 数据上抓出来：中药学 15 个附药块、22 味药全被判成「无结构标记」丢掉——预过滤最贵
# 的那种错（真条目一丢就永远抽不到）。「附药：」是标题行的结构标记，跟 `【字段】`
# 同一性质，不是内容关键词。
_FUYAO_TITLE_RE = re.compile(r"^#{1,6}\s+附药[：:]", re.M)
# 古籍转录体例的条目标题行；正文标记是 `内容：`（神农本草经、本草备要）或
# `属性：`（医学衷中参西录）。
_PIAN_LINE_RE = re.compile(r"^<篇名>", re.M)
_CLASSIC_BODY_RE = re.compile(r"内容：|属性：")
# 古籍方药的剂量词：「黄 一钱」「生山药（一两）」。给不带 <篇名> 标记的本子
# （《脾胃论》）用；`两` 在数词里也出现（"二两"），所以数词集合里含它。
# **不复用 core/herbs._DOSE_RE**（CLAUDE.md「同一概念只有一处实现」的例外，两处回答
# 的不是同一个问题）：那条正则锚在词尾（`\s*$`），回答的是"这个药名 token 末尾挂
# 着什么剂量、要剥掉多少"，单位表因此含 克/g/片/杯 这些现代和量具单位；这里回答
# 的是"这一块原文里有没有古籍方药的剂量写法"，在整块里任意位置搜、只认古籍单位。
# 硬把前者去掉锚点拿来用，克/g 会把教材的「9～30g」也判成古籍方药。
_CLASSIC_DOSE_RE = re.compile(r"[一二三四五六七八九十半两]+(钱|两|分|枚|铢|升|斤)")
_TABLE_RE = re.compile(r"<table.*?</table>", re.S)
# 「药名 + 页码」行：一个不含空白的词（药名，至多 14 字）后面跟一个 1~4 位数字，
# 整行到此为止。判的是**结构**（行尾页码密度），不是"目录""索引"这类词。
_INDEX_LINE_RE = re.compile(r"^\S{1,14}\s+\d{1,4}\s*$")


def is_modern_entry(block: str) -> bool:
    return _FIELD_LABEL_RE.search(block) is not None or _FUYAO_TITLE_RE.search(block) is not None


def has_classic_dose(text: str) -> bool:
    """这段文字里有没有古籍方药的剂量写法（「二钱」「一两五钱」）。

    R18-D 的《脾胃论》篇名识别也要问这个问题——「黄丹二钱定粉舶上硫黄陀僧已上各
    三钱轻粉少许」整行没有标点、长度也够短，不认剂量就会被当成篇名。抽成公开函数
    而不是在那边另写一条正则（CLAUDE.md 第 31 条），_CLASSIC_DOSE_RE 本来就是
    为《脾胃论》这类不带 <篇名> 标记的本子写的。
    """
    return _CLASSIC_DOSE_RE.search(text) is not None


def is_classic_entry(block: str) -> bool:
    if _PIAN_LINE_RE.search(block) and _CLASSIC_BODY_RE.search(block):
        return True
    return has_classic_dose(block)


# 按 EXPECTED_SOURCES 的 source 列取谓词。没有对应谓词的 source（切块验证的
# `--file` 模式给不在六源清单里的文件标 "unknown"）不做结构判据，只跳过
# 过短/表格/索引/超长这四类跟源类型无关的——不猜它该长什么样。
ENTRY_PREDICATES: dict[str, Callable[[str], bool]] = {
    "modern": is_modern_entry,
    "classic": is_classic_entry,
}


def index_line_ratio(block: str) -> float:
    """正文行（不含首行标题）里「药名 + 页码」形状的占比。不足 INDEX_MIN_LINES 行
    正文一律 0.0——一行的巧合不构成"这是个索引"。"""
    lines = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
    if len(lines) < INDEX_MIN_LINES:
        return 0.0
    return sum(bool(_INDEX_LINE_RE.match(ln)) for ln in lines) / len(lines)


def table_ratio(block: str) -> float:
    if not block:
        return 0.0
    inside = sum(len(m.group(0)) for m in _TABLE_RE.finditer(block))
    return inside / len(block)


def classify_block(block: str, source: str | None) -> str | None:
    """返回跳过原因（SKIP_REASONS 之一），None = 保留。判序见模块文档。"""
    if len(block) < MIN_ENTRY_CHARS:
        return SKIP_TOO_SHORT
    if "<table" in block and table_ratio(block) > TABLE_RATIO_MAX:
        return SKIP_TABLE
    if index_line_ratio(block) >= INDEX_LINE_RATIO_MAX:
        return SKIP_INDEX
    if len(block) > BLOCK_MAX_CHARS:
        return SKIP_TOO_LONG
    predicate = ENTRY_PREDICATES.get(source or "")
    if predicate is not None and not predicate(block):
        return SKIP_NO_STRUCTURE
    return None


def prefilter_blocks(
    blocks: list[tuple[int, str]], source: str | None,
) -> tuple[list[tuple[int, str]], list[tuple[int, str, str]], dict[str, int]]:
    """把 (块号, 原文) 列表分成保留 / 跳过两份。跳过的带原因，块号沿用切块时
    的编号——`--only-blocks` 重跑和输出行里的 `_block` 字段用的都是它，
    预过滤不能把编号弄乱。"""
    kept: list[tuple[int, str]] = []
    skipped: list[tuple[int, str, str]] = []
    counts = {reason: 0 for reason in SKIP_REASONS}
    for index, block in blocks:
        reason = classify_block(block, source)
        if reason is None:
            kept.append((index, block))
        else:
            skipped.append((index, reason, block))
            counts[reason] += 1
    return kept, skipped, counts


def format_prefilter_summary(n_total: int, counts: dict[str, int]) -> str:
    """「740 块 → 保留 457 块（跳过：过短 9 / 表格 8 / 索引 25 / 超长 0 / 无结构标记 241）」。
    五个数加起来 = 跳过总数，人一眼能看出被丢的主要是什么。"""
    n_skipped = sum(counts.get(r, 0) for r in SKIP_REASONS)
    detail = " / ".join(f"{r} {counts.get(r, 0)}" for r in SKIP_REASONS)
    return f"{n_total} 块 → 保留 {n_total - n_skipped} 块（跳过：{detail}）"
