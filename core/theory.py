"""R51：医理规则层的查询接口。`data/standard/tcm_theory.jsonl` 的四类规则
（藏象关系、病机传变、治则推导、配伍理论）在这里加载成可查询结构。

**这是 R52 演绎推导的地基**：第一相 prompt 要把"能推出什么"摆在模型面前，
而不是让模型凭记忆回忆——这个模块回答的就是"能推出什么"。

惰性加载（CLAUDE.md：加载大文件的对象一律惰性初始化）。文件不在时返回空，
**不抛**：这一层数据缺失时，第一相会如实标 `insufficient`，不是让整个服务
起不来。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

TCM_THEORY_PATH = Path(__file__).resolve().parent.parent / "data" / "standard" / "tcm_theory.jsonl"

RuleKind = str  # "organ_relation" | "pathomechanism" | "treatment_principle" | "compatibility"


@dataclass(frozen=True)
class TheoryRule:
    """四类规则的公共外壳。具体字段按 `kind` 读 `payload`——**不用四个子类**：
    调用方（R52 的 prompt 渲染、`rule()` 查找）大多只关心 id/span/source，
    分子类会让"给我这条规则的文本"这个最常见操作要先判断类型。"""

    id: str
    kind: RuleKind
    source: str
    span: str
    confidence: str
    applies_to: str
    payload: dict = field(default_factory=dict)

    def __getattr__(self, name: str):
        # payload 里的字段直接当属性读（r.subject、r.principle 这样用）。
        # 只在 dataclass 自己的字段找不到时才落到这里（Python 的属性查找顺序）。
        try:
            return self.payload[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "source": self.source,
                "span": self.span, "confidence": self.confidence,
                "applies_to": self.applies_to, **self.payload}


_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "organ_relation": ("subject", "relation", "object", "mechanism",
                       "direction", "trigger_elements", "implied_elements"),
    "pathomechanism": ("from", "to", "condition", "mechanism", "markers"),
    "treatment_principle": ("when_nature", "when_location", "principle",
                            "method_keywords", "contraindicated_methods"),
    "compatibility": ("relation", "definition", "example_pairs"),
}


def _to_rule(row: dict) -> TheoryRule:
    kind = row["kind"]
    keys = _PAYLOAD_KEYS.get(kind, ())
    payload = {k: row[k] for k in keys if k in row}
    return TheoryRule(id=row["id"], kind=kind, source=row.get("source", ""),
                      span=row.get("span", ""), confidence=row.get("confidence", ""),
                      applies_to=row.get("applies_to", ""), payload=payload)


_cache: tuple[TheoryRule, ...] | None = None
_lock = threading.Lock()


def load_theory(path: Path | None = None) -> tuple[TheoryRule, ...]:
    global _cache
    if path is None and _cache is not None:
        return _cache
    p = path or TCM_THEORY_PATH
    rows: list[TheoryRule] = []
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(_to_rule(json.loads(line)))
    out = tuple(rows)
    if path is None:
        with _lock:
            _cache = out
    return out


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def rule(rule_id: str) -> TheoryRule | None:
    """按 id 查一条——R52 的 schema 校验 `rule_refs` 时用它确认引用真实存在。"""
    return next((r for r in load_theory() if r.id == rule_id), None)


def _by_kind(kind: str) -> list[TheoryRule]:
    return [r for r in load_theory() if r.kind == kind]


def organ_relations(element: str) -> list[TheoryRule]:
    """这个证素（脏腑或病性）能推出哪些藏象关系——`element` 命中
    `trigger_elements` 即算。"""
    if not element:
        return []
    return [r for r in _by_kind("organ_relation") if element in r.trigger_elements]


def transitions(from_elements: list[str], condition: str | None = None) -> list[TheoryRule]:
    """给定一组已确立的证素，哪些病机传变能被触发——`from` 是 `from_elements`
    的子集即算命中（`from_elements` 可以比规则要求的更多，规则本身可以只
    要 1 个或 2 个证素同时成立）。

    `condition` 目前只用于展示（每条规则自带触发条件的文字说明），不做
    额外过滤——"这个条件成不成立"要模型结合症状判断，不是这一层能算的。
    """
    if not from_elements:
        return []
    given = set(from_elements)
    out = [r for r in _by_kind("pathomechanism") if set(r.payload["from"]) <= given]
    return out


def principles_for(nature: list[str] | str, location: list[str] | str) -> list[TheoryRule]:
    """按证素的病位（location）与病性（nature）推治则。

    **两段匹配**：先找"nature 命中且 location 也命中"的具体化规则（更贴切），
    再找"nature 命中、location 留空（普遍适用）"的通用规则（虚则补之这类）。
    两段都收集、都返回——调用方（R52 prompt）看到的是"能推出哪几条"，
    不是"哪一条最好"，排序留给模型在 `rule_refs` 里自己引用哪几条。

    `nature`/`location` 接受单值或列表：证型的病性可能不止一个（气虚+湿）。
    """
    natures = {nature} if isinstance(nature, str) else set(nature or ())
    locations = {location} if isinstance(location, str) else set(location or ())
    out: list[TheoryRule] = []
    for r in _by_kind("treatment_principle"):
        wn = set(r.payload["when_nature"])
        wl = set(r.payload["when_location"])
        if wn and not (wn & natures):
            continue
        if wl and not (wl & locations):
            continue
        # `wn`/`wl` 都留空的规则只有「虚则补之」那 12 条元治则——它们要在
        # **确实给了某个病性**时才算命中，否则一次没给任何证素信息的查询会
        # 无条件命中一堆"放之四海而皆准"的规则。`wn` 空但 `wl` 非空的规则
        # （"脾宜升则健"这类脏腑生理默认方向）不受这条限制：病位定了、病性
        # 还没定（或 syndromes.jsonl 没标注）时，这十条正是为这种情况兜底的，
        # 要求它们也必须先有 nature 会让它们形同虚设。
        if not wn and not wl and not natures:
            continue
        out.append(r)
    return out


def compatibility(relation: str | None = None, herbs: list[str] | None = None) -> list[TheoryRule]:
    """按配伍关系类型（"相须"/"君药"…）或药对查配伍理论。

    `herbs` 给两味药时查它们是不是某条 `example_pairs` 里记录的药对——
    **只查有记录的药对**，查不到不代表这两味药不能配，只代表这一版规则表
    没收录这个例子（R52 的 prompt 会如实说"没有查到配伍先例"，不是说"不能配"）。
    """
    rows = _by_kind("compatibility")
    if relation:
        rows = [r for r in rows if r.payload["relation"] == relation]
    if herbs:
        want = set(h.strip() for h in herbs if h.strip())
        rows = [r for r in rows
                if any(set(pair) == want for pair in r.payload["example_pairs"])]
    return rows


def role_construction_rules() -> list[TheoryRule]:
    """君臣佐使各自的构成规则（供 R52/R53 的组方与验证复用，不用各自
    再从 `compatibility(relation=...)` 拼四次）。"""
    wanted = {"君药", "臣药", "佐药", "使药", "组方原则"}
    return [r for r in _by_kind("compatibility") if r.payload["relation"] in wanted]


def coverage_stats(syndromes: list[dict]) -> dict:
    """`principles_for` 对一批证候（`syndromes.jsonl` 的行）的覆盖率。
    R51 §1.4 的验收判据要报这个数——**报出数，不是只报"够了"**。"""
    total = 0
    matched = 0
    for row in syndromes:
        if row.get("is_category"):
            continue
        total += 1
        if principles_for(row.get("nature") or [], row.get("location") or []):
            matched += 1
    return {"total": total, "matched": matched,
            "ratio": round(matched / total, 4) if total else 0.0}
