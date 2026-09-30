"""药理层两个数据文件的路径解析。**只此一处**。

`materia_medica.jsonl` / `formulary.jsonl` 是带 `source_span` 的三元组，由真实 LLM
抽取产生：耗时长、要花 token，重跑一次得到的也不是同一份文件。这种数据不进版本
控制，就等于"这份数据只存在于一台机器上"，所以它们进版本控制。

`.gitignore` 对 `*.jsonl` 整体忽略、只给 `data/standard/*.jsonl` 开了例外
（docs/ARCHITECTURE.md §6），所以这两个文件的落盘位置是 `data/standard/`。

读写两侧都走同一个函数（docs/ARCHITECTURE.md §4）："这个文件在哪儿"这个问题只能
有一处答案。写死几份常量就是几处实现。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parent.parent

PharmacologyKind = Literal["materia_medica", "formulary"]

# 落盘目录：进版本控制的那个。
CANONICAL_DIR = ROOT / "data" / "standard"


def pharmacology_filename(kind: PharmacologyKind) -> str:
    return f"{kind}.jsonl"


def pharmacology_write_path(kind: PharmacologyKind) -> Path:
    """抽取结果往哪儿写。永远是 data/standard/——写到 data/ 其他位置会被 .gitignore 忽略。"""
    return CANONICAL_DIR / pharmacology_filename(kind)


def pharmacology_read_path(kind: PharmacologyKind) -> Path | None:
    """读哪一份。文件不存在时返回 None（不是抛异常，也不是返回一个不存在的路径）。

    返回 None 而不是"返回路径让调用方自己 .exists()"：后者会让每个调用方各自
    实现一遍存在性判断。
    """
    p = pharmacology_write_path(kind)
    return p if p.exists() else None


def pharmacology_read_path_or_canonical(kind: PharmacologyKind) -> Path:
    """给"要在报错信息里说出路径"的场合用：没有文件时给出应在的路径，
    这样提示里说的是"应该在哪儿"而不是一个空值。"""
    return pharmacology_read_path(kind) or pharmacology_write_path(kind)
