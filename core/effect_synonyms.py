"""治法 → 功效 的同义映射表加载与查询。R34 的 `effect_matches_method` 规则靠它判。

**这张表回答的问题**：「疏肝理气」这个**治法**，对应本草条目里哪些**功效**表述。
治法与功效在文献里是两套措辞，裸子串比会把大部分正确的药判成不匹配
——那条验证规则就变成恒假，等于没有验证。

**跟 `core/syndrome_norm.py::SYNONYMS` 不是同一件事**（CLAUDE.md 第 31 条的例外
必须写清区别）：那张表回答「这个词属于哪个证候门类」，本表回答「这个治法对应
哪些功效表述」。合并成一张，以后改一边会看不出是否连带影响另一边。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EFFECT_SYNONYMS_PATH = ROOT / "data" / "standard" / "effect_synonyms.tsv"

#: 来源只有这两种。**必须分开**：一份表里混着核过的和没核的，
#: 整份表的可信度只能按最低那条算。
SOURCES = ("textbook", "common")


@dataclass(frozen=True)
class EffectSynonym:
    method: str
    effects: tuple[str, ...]
    source: str


_table: tuple[EffectSynonym, ...] | None = None
_lock = threading.Lock()


def load_effect_synonyms(path: Path | None = None) -> tuple[EffectSynonym, ...]:
    """读 TSV。**文件缺失直接抛**——它在版本控制里，缺了说明工作树不完整，
    而静默返回空表会让 `effect_matches_method` 规则恒假且没人发现。"""
    p = path or EFFECT_SYNONYMS_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"未找到功效同义词表 {p}。它在版本控制里（data/standard/*.tsv），"
            "缺失说明工作树不完整——不是可以跳过的一步：缺了它 R34 的"
            "「功效对治法」规则会恒假。"
        )
    rows: list[EffectSynonym] = []
    seen: set[str] = set()
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if cols[0] == "治法":       # 表头
            continue
        if len(cols) < 3:
            raise ValueError(f"{p}:{lineno} 要有「治法\\t功效词\\t来源」三列，实际：{line!r}")
        method = cols[0].strip()
        effects = tuple(dict.fromkeys(e.strip() for e in cols[1].split(",") if e.strip()))
        source = cols[2].strip()
        if not method or not effects:
            raise ValueError(f"{p}:{lineno} 治法与功效词都不许空：{line!r}")
        if source not in SOURCES:
            raise ValueError(
                f"{p}:{lineno}（{method}）来源是 {source!r}，只能是：{' / '.join(SOURCES)}"
            )
        if method in seen:
            raise ValueError(f"{p}:{lineno} 治法「{method}」重复——重复项只会让人以为改对了")
        seen.add(method)
        rows.append(EffectSynonym(method=method, effects=effects, source=source))
    return tuple(rows)


def _cached() -> tuple[EffectSynonym, ...]:
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                _table = load_effect_synonyms()
    return _table


def reset_for_tests() -> None:
    global _table
    with _lock:
        _table = None


def expand_effect(term: str) -> tuple[str, ...]:
    """给一个治法（或功效词），返回它的全部同义功效词，**含它自己**。

    双向：`expand_effect("疏肝理气")` 和 `expand_effect("疏肝解郁")` 都能拿到
    同一族词。表里查不到就只返回它自己——**不返回空**：查不到的治法
    （模型写了个表外说法）应该退化成裸子串比，而不是让规则判它"无论如何都不匹配"。
    """
    t = (term or "").strip()
    if not t:
        return ()
    out: list[str] = [t]
    for row in _cached():
        if t == row.method or t in row.effects:
            for e in (row.method, *row.effects):
                if e not in out:
                    out.append(e)
    return tuple(out)


def methods_for_effect(effect: str) -> tuple[str, ...]:
    """反查：这个功效词对应哪些治法。"""
    e = (effect or "").strip()
    if not e:
        return ()
    return tuple(row.method for row in _cached() if e in row.effects or e == row.method)


def table_stats() -> dict:
    rows = _cached()
    return {
        "n_rows": len(rows),
        "n_textbook": sum(1 for r in rows if r.source == "textbook"),
        "n_common": sum(1 for r in rows if r.source == "common"),
        "n_effect_terms": len({e for r in rows for e in r.effects}),
    }
