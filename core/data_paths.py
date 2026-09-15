"""药理层两个数据文件的路径解析。**只此一处**。

R18-F 把 `data/materia_medica.jsonl` / `data/formulary.jsonl` 从"生成物、不进
版本控制"改成"进版本控制"——它们是 10248 + 3737 条带 `source_span` 的三元组，
在 AutoDL 上跑一次要几个小时和一笔 token 钱，重跑一次得到的不是同一份文件
（真实 LLM 抽取）。这种东西不进版本控制，等于"这份数据只存在于一台机器上"。

`.gitignore` 对 `*.jsonl` 整体忽略、只给 `data/standard/*.jsonl` 开了例外
（CLAUDE.md「已知的坑」里那条已经踩过两次），所以进版本控制就必须挪到
`data/standard/` 下。

**旧路径要继续能读**：AutoDL 上那台机器已经有 `data/materia_medica.jsonl`，
硬切路径会让它"文件在、却读不到"——而 `core/tools.py` 读不到时返回的是
`available: false`，模型看到的是"药理层没数据"，跟真的没跑过抽取分不出来。
所以解析顺序是：先看 `data/standard/`，没有再看 `data/`，两处都没有才是真没有。

三个调用方（core/tools.py 读、offline/export_sft.py 读、
offline/extract_reference_triples.py 写）走同一个函数——CLAUDE.md 第 31 条：
"这个文件在哪儿"这个问题只能有一处答案。写死三份常量就是三处实现。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent

PharmacologyKind = Literal["materia_medica", "formulary"]

# 落盘首选目录：进版本控制的那个。
CANONICAL_DIR = ROOT / "data" / "standard"
# 旧目录：R18-F 之前的落盘位置，仍然可读，不再往这里写。
LEGACY_DIR = ROOT / "data"


def pharmacology_filename(kind: PharmacologyKind) -> str:
    return f"{kind}.jsonl"


def pharmacology_write_path(kind: PharmacologyKind) -> Path:
    """新抽取往哪儿写。永远是 data/standard/——写进旧目录会被 .gitignore 吞掉。"""
    return CANONICAL_DIR / pharmacology_filename(kind)


def pharmacology_read_path(kind: PharmacologyKind) -> Path | None:
    """读哪一份。两处都没有返回 None（不是抛异常，也不是返回一个不存在的路径）。

    返回 None 而不是"返回首选路径让调用方自己 .exists()"：后者会让每个调用方
    各自实现一遍回退顺序，回退顺序就有了三份。
    """
    for d in (CANONICAL_DIR, LEGACY_DIR):
        p = d / pharmacology_filename(kind)
        if p.exists():
            return p
    return None


def pharmacology_read_path_or_canonical(kind: PharmacologyKind) -> Path:
    """给"要在报错信息里说出路径"的场合用：没有文件时给出首选路径，
    这样提示里说的是"应该在哪儿"而不是一个空值。"""
    return pharmacology_read_path(kind) or pharmacology_write_path(kind)
