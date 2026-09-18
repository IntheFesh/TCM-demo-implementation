"""R32 本体层：把药理层的三元组读成**可查询的结构化本体**，不是文本拼接。

**为什么要有这一层。** 在此之前药理层只有两个消费者：`core/tools.py::
query_materia_medica`（ReAct 工具，一次查一味药）和 `core/context_prefix.py`
的速查表（把 9776 + 3184 条全量拼成文本塞进稳定前缀）。两者都够不到
"归经覆盖不覆盖这个病位""这味药的功效跟治法对不对得上"这类**结构化判断**
——而那正是 R34 符号验证器要做的事，也是"让 AI 明白药理"跟"检索到一段原文"
的分界线。

**数据不在就如实说，不编。** 药理层的两个 jsonl 由 `offline/extract_reference_triples.py`
在有真实 LLM 的机器上抽取产出（上机剧本段 5，2181 块）。文件不在时
`get_ontology().available` 是 False，所有查询返回空/None——**跟"查了但没这味药"
是两个不同的信号**，调用方要能分开（CLAUDE.md：工具返回空必须能区分三种情况）。

**复用而不是另写**（CLAUDE.md 第 31 条，这个项目已经在同一堵墙上撞过三次）：
  - 三元组的加载与 `{主语: {谓词: [值]}}` 索引 → `core.context_prefix.build_entry_index`
  - 药名归一（"炙黄芪三钱" → "黄芪"）→ `core.herbs.normalize_herb`
  - 十八反十九畏 → `core.safety_output.INCOMPATIBLE_PAIRS`
  - 药典剂量上限 → `core.safety_output.DOSE_LIMITS`
本模块**不持有任何一张自己的药物知识表**，只做"把文本值解析成可比对的结构"。
"""
from __future__ import annotations

import argparse
import re
import threading
from dataclasses import dataclass, field
from typing import Literal

from core.context_prefix import build_entry_index
from core.herbs import normalize_herb
from core.safety_output import (
    INCOMPATIBLE_PAIRS,
    dose_limit_entry,
    normalize_for_incompat,
)

# ---------- 值域词表：只做"文本 → 可比对的枚举"，不做医学判断 ----------
#
# 这几张表回答的是"这段原文里提到了哪几个性/味/经"，**不回答**"这味药该不该用"。
# 后者是 R34 验证器的事，判据在那边。两件事分开是因为它们的错法不同：
# 这里错了是漏解析（可以靠覆盖率发现），那边错了是判错证（要靠反例发现）。

#: 药性。**长词在前**：「大寒」必须先于「寒」匹配，否则「大寒」会被切成「寒」，
#: 而 R34 的 `nature_conflict` 规则要靠"大寒/大热"这个强度区分才拦得住峻药。
NATURES: tuple[str, ...] = (
    "大寒", "大热", "微寒", "微温", "微热", "寒", "热", "温", "凉", "平",
)
#: 药味。七味 + 「淡」「涩」，跟教材一致。
FLAVORS: tuple[str, ...] = ("辛", "甘", "酸", "苦", "咸", "淡", "涩")
#: 归经。**长词在前**：「小肠」「大肠」必须先于「肠」「心」匹配。
MERIDIANS: tuple[str, ...] = (
    "小肠", "大肠", "膀胱", "心包", "三焦", "肺", "脾", "胃", "肝", "胆",
    "心", "肾",
)

#: 剂量原文里取上限用的数字模式。「3~9g」「3-10 克」「一般 3～9g」都能命中。
_DOSE_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[~～\-—－至]\s*(\d+(?:\.\d+)?)\s*(?:g|克)")
_DOSE_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:g|克)")
#: 功效/主治/组成里的并列分隔符。
_SPLIT_RE = re.compile(r"[，,、；;。\s]+")
#: 组成条目里的剂量部分：「柴胡 12g」「白芍9克」「炙甘草六钱」
_COMPOSITION_ITEM_RE = re.compile(r"^(.*?)\s*([\d一二三四五六七八九十]+(?:\.\d+)?\s*(?:g|克|钱|两|分|枚|片|条|只)?)?$")
#: 整段只有剂量、没有药名。方剂书里「柴胡 12g、黄芩 9g」用空格分药名与剂量，
#: 而 `_SPLIT_RE` 把空白也当分隔符，剂量会被切成独立一段跟药名走散——
#: 不认出这种段就会把剂量**静默丢掉**，而 R34 的 dose_exceeds 读的正是这个数。
_DOSE_ONLY_RE = re.compile(r"^[\d一二三四五六七八九十百]+(?:\.\d+)?\s*(?:g|克|钱|两|分|枚|片|条|只|ml|毫升)?$")

OntologyKind = Literal["materia_medica", "formulary"]


@dataclass(frozen=True)
class SourceRef:
    """一条谓词的出处。`span` 是抽取时留下的原文片段。

    **span 为空即视为该谓词缺失**——`MateriaMedicaRecord.source_span` 是
    `Field(min_length=1)`，空 span 只可能来自绕过 schema 写进去的行，
    那种行不该被当成有出处的证据（这是防幻觉设计的一部分）。
    """

    book: str
    source: str          # classic | modern
    span: str


@dataclass(frozen=True)
class Herb:
    """一味药的结构化本体条目。

    每个字段都可能缺（原文没写就是没写），缺就是 None / 空元组，
    **不填默认值**——`nature=None` 和 `nature="平"` 是两件事，后者是一个判断。
    """

    name: str
    aliases: tuple[str, ...] = ()
    nature: str | None = None
    flavor: tuple[str, ...] = ()
    meridians: frozenset[str] = frozenset()
    effects: tuple[str, ...] = ()
    dose_max_g: float | None = None
    contraindications: tuple[str, ...] = ()
    preparation: tuple[str, ...] = ()
    refs: dict[str, tuple[SourceRef, ...]] = field(default_factory=dict)

    def has(self, predicate: str) -> bool:
        """这个谓词有没有**带非空出处**的证据。"""
        return any(r.span.strip() for r in self.refs.get(predicate, ()))


@dataclass(frozen=True)
class Formula:
    name: str
    composition: tuple[tuple[str, str], ...] = ()   # (药名, 剂量原文)
    roles: dict[str, tuple[str, ...]] = field(default_factory=dict)
    indications: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    modifications: tuple[str, ...] = ()
    refs: dict[str, tuple[SourceRef, ...]] = field(default_factory=dict)

    def herb_names(self) -> tuple[str, ...]:
        return tuple(n for n, _dose in self.composition)


# ---------- 解析：文本值 → 结构 ----------

def parse_nature(values: list[str]) -> str | None:
    """性味原文 → 药性。**长词优先**，取第一个命中的。

    「苦、辛，微寒」→ 微寒；「大寒」不会被切成「寒」。
    一味药在不同来源里性可能写得不同（「微寒」vs「寒」），这里按**出现顺序**
    取第一个——不做"哪个来源更权威"的判断，那需要另一套依据。
    """
    for v in values:
        for n in NATURES:
            if n in v:
                return n
    return None


def parse_flavors(values: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for v in values:
        for f in FLAVORS:
            if f in v and f not in out:
                out.append(f)
    return tuple(out)


def parse_meridians(values: list[str]) -> frozenset[str]:
    """归经原文 → 经络集合。**长词优先**：「归大肠经」不能同时命中「大肠」和「肠」。

    做法是逐条把已命中的长词从文本里挖掉再匹配短词，而不是直接子串命中
    ——「归小肠经」会被「心」漏命中吗？不会，但「归心包经」会被「心」命中，
    所以「心包」必须先挖掉。
    """
    out: set[str] = set()
    for v in values:
        rest = v
        for m in MERIDIANS:          # 已按长度降序排好
            if m in rest:
                out.add(m)
                rest = rest.replace(m, "")
    return frozenset(out)


def parse_effects(values: list[str]) -> tuple[str, ...]:
    """功效原文 → 功效词元组。按并列分隔符切，去重保序，丢掉单字碎片。

    单字丢掉的理由跟 `core/tools.py::_GENERIC_FRAGMENTS` 同源：单字（「利」「和」）
    对任何治法都能匹配上，留着它 R34 的 `effect_matches_method` 就恒通过。
    """
    out: list[str] = []
    for v in values:
        for part in _SPLIT_RE.split(v):
            part = part.strip()
            if len(part) >= 2 and part not in out:
                out.append(part)
    return tuple(out)


def parse_dose_max_g(values: list[str]) -> float | None:
    """用量原文 → 克数上限。区间取上界，单值取该值，取所有条目里的**最大值**。

    取最大而不是最小：这个数在 R34 里是"开的量有没有超过本草说的范围"的参照，
    取最小会把正常剂量判成超量。真正的安全闸门是 `DOSE_LIMITS`（药典上限，
    veto 级），这里的数只是本体记录。
    """
    best: float | None = None
    for v in values:
        vals: list[float] = []
        for m in _DOSE_RANGE_RE.finditer(v):
            vals.append(float(m.group(2)))
        if not vals:
            vals = [float(m.group(1)) for m in _DOSE_SINGLE_RE.finditer(v)]
        for x in vals:
            if best is None or x > best:
                best = x
    return best


def parse_composition(values: list[str]) -> tuple[tuple[str, str], ...]:
    """组成原文 → ((药名, 剂量原文), …)。药名过 `normalize_herb`，剂量原样留。

    剂量留原文不解析成数字：方剂书里「六钱」「一两」「三枚」都有，换算成克需要
    朝代与度量衡的判断，那是另一件事，本轮不做——**留原文比给一个错的数字好**。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for v in values:
        for part in _SPLIT_RE.split(v):
            part = part.strip()
            if not part:
                continue
            # 落单的剂量补回上一味药，不丢掉（见 _DOSE_ONLY_RE 的注释）。
            # 只在上一味还没有剂量时补：「柴胡12g 9g」这种重复写法保留先出现的那个。
            if _DOSE_ONLY_RE.match(part):
                if out and not out[-1][1]:
                    out[-1] = (out[-1][0], part)
                continue
            m = _COMPOSITION_ITEM_RE.match(part)
            raw_name = (m.group(1) if m else part).strip()
            dose = (m.group(2) or "").strip() if m else ""
            name = normalize_herb(raw_name)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append((name, dose))
    return tuple(out)


def _refs_for(rows: list[dict], subject: str, predicate: str) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef(book=r.get("book") or "", source=r.get("source") or "",
                  span=r.get("source_span") or "")
        for r in rows
        if r.get("s") == subject and r.get("p") == predicate
    )


# ---------- 本体对象 ----------

class Ontology:
    """一份加载好的本体。**只读**，构造之后不再变。

    `available=False` 表示药理层数据文件不在（沙盒/新 clone 的常态）。
    那时所有查询返回空/None，**而不是抛异常**：调用方（S3 提示词组装、验证器）
    要能在没有药理层时降级运行，同时在 manifest 里如实记录"这次没有本体"。
    """

    def __init__(self, materia_rows: list[dict] | None = None,
                 formulary_rows: list[dict] | None = None,
                 patterns: list[dict] | None = None) -> None:
        self._materia_rows = materia_rows if materia_rows is not None else _rows("materia_medica")
        self._formulary_rows = formulary_rows if formulary_rows is not None else _rows("formulary")
        self._patterns = patterns if patterns is not None else _load_patterns()
        self.herbs: dict[str, Herb] = _build_herbs(self._materia_rows)
        self.formulas: dict[str, Formula] = _build_formulas(self._formulary_rows)
        # 别名 → 正名。`normalize_herb` 已经把大部分写法归一了，这张表补的是
        # "本体里存的就是别名"的情况（原文写「云苓」而不是「茯苓」）。
        self._by_alias: dict[str, str] = {}
        for h in self.herbs.values():
            for a in (h.name, *h.aliases):
                self._by_alias.setdefault(a, h.name)

    # -- 可用性 --

    @property
    def available(self) -> bool:
        """本草或方剂**任一**有内容即算可用。两者都空才是"药理层没跑过"。"""
        return bool(self.herbs or self.formulas)

    def stats(self) -> dict:
        missing = {p: 0 for p in ("性味", "归经", "功效", "用量", "禁忌", "炮制")}
        empty_span = 0
        for h in self.herbs.values():
            for p in missing:
                if not h.has(p):
                    missing[p] += 1
            for refs in h.refs.values():
                empty_span += sum(1 for r in refs if not r.span.strip())
        for f in self.formulas.values():
            for refs in f.refs.values():
                empty_span += sum(1 for r in refs if not r.span.strip())
        return {
            "available": self.available,
            "n_herbs": len(self.herbs),
            "n_formulas": len(self.formulas),
            "n_patterns": len(self._patterns),
            "missing_predicate_counts": missing,
            "empty_span_refs": empty_span,
        }

    def source_books(self) -> dict[str, dict[str, int]]:
        """三元组按书分布：`{"materia_medica": {书名: 条数}, "formulary": {...}}`。

        R42 加的，为了「循证对照」那一节能说清**对照基准是哪几部书、各多少条**。
        CLAUDE.md 那条铁律（任何数字都必须带对照）在这里的具体形式是：
        「这味药有出处」是句空话，「这味药的性味出自《本草备要》，而本项目的
        本草层一共只有五部书 9776 条」才是一个可核的说法。

        从已加载的原始行现算，**不缓存**：本体是只读的（构造后不再变），
        而这个接口只在点开一个节点时调一次，几毫秒。
        """
        out: dict[str, dict[str, int]] = {"materia_medica": {}, "formulary": {}}
        for key, rows in (("materia_medica", self._materia_rows),
                          ("formulary", self._formulary_rows)):
            bucket = out[key]
            for r in rows:
                book = (r.get("book") or "").strip() or "未标注"
                bucket[book] = bucket.get(book, 0) + 1
        return out

    # -- 九个查询接口 --

    def herb(self, name: str) -> Herb | None:
        """药名先走归一（"炙黄芪三钱" → "黄芪"），再查别名表。查不到返 None。"""
        if not name:
            return None
        n = normalize_herb(name)
        if not n:
            return None
        if n in self.herbs:
            return self.herbs[n]
        canon = self._by_alias.get(n)
        return self.herbs.get(canon) if canon else None

    def herbs_batch(self, names: list[str] | tuple[str, ...]) -> dict[str, Herb | None]:
        """一次解析一批药名。**"这些药在本体里是什么"这个问题的唯一批量入口。**

        R40：符号验证器本体那几条规则各自逐味 `ont.herb()`，一张 12 味的方要查 7×12 = 84
        次（每次都重跑一遍 `normalize_herb`）。这个方法让调用方只问一次，
        重复的名字只归一一次。

        查不到的名字**保留键、值为 None**，不从结果里省掉——省掉的话调用方
        分不清"没查"和"查了没有"，而那正是这一层最要紧的区分
        （`Unverifiable` 与 `Violation` 的分界）。
        """
        return {n: self.herb(n) for n in dict.fromkeys(names)}

    def herbs_by_meridian(self, meridian: str) -> list[Herb]:
        return [h for h in self.herbs.values() if meridian in h.meridians]

    def herbs_by_effect(self, keyword: str) -> list[Herb]:
        """功效含该关键词的药。关键词先过**功效同义词表**展开
        （「疏肝理气」→ 疏肝/理气/行气/解郁），再做子串匹配。

        走同义词表而不是裸子串：治法词（「疏肝理气」）和功效词（「疏肝解郁」）
        在教材里不是同一套措辞，裸子串会把大部分正确的药判成不匹配
        ——这正是 R34 `effect_matches_method` 规则会不会变成恒假的关键。
        """
        from core.effect_synonyms import expand_effect

        keys = expand_effect(keyword)
        if not keys:
            return []
        out: list[Herb] = []
        for h in self.herbs.values():
            if any(k in e for e in h.effects for k in keys):
                out.append(h)
        return out

    def herbs_by_nature(self, nature: str) -> list[Herb]:
        return [h for h in self.herbs.values() if h.nature == nature]

    def formula(self, name: str) -> Formula | None:
        if not name:
            return None
        return self.formulas.get(name.strip())

    def formulas_for_syndrome(self, syndrome: str) -> list[Formula]:
        """主治里提到这个证的方。证名去掉尾「证」再比——教材主治写的是
        「肝郁气滞」而条目名是「肝郁气滞证」，带「证」字比会一条都匹配不上。
        """
        if not syndrome:
            return []
        key = syndrome.strip().removesuffix("证")
        if not key:
            return []
        return [f for f in self.formulas.values()
                if any(key in ind for ind in f.indications)]

    def is_incompatible(self, a: str, b: str) -> str | None:
        """两味药是不是十八反/十九畏的一对。是就返回「甲-乙」，否则 None。

        **判据整个来自 `core.safety_output.INCOMPATIBLE_PAIRS`**（24 对）——
        改那张表这里跟着变，本模块不持有第二份配伍表。
        """
        na, nb = normalize_for_incompat(a), normalize_for_incompat(b)
        if not na or not nb or na == nb:
            return None
        for pair in INCOMPATIBLE_PAIRS:
            if {na, nb} == set(pair):
                x, y = sorted(pair)
                return f"{x}-{y}"
        return None

    def dose_limit(self, name: str) -> float | None:
        """药典剂量上限（克）。**查法整个走 `core.safety_output.dose_limit_entry`**，
        本模块不自己排查表顺序——排错一次就会把「巴豆霜 0.3g」换成「巴豆 0.0g」。"""
        hit = dose_limit_entry(name)
        return float(hit[0]) if hit else None

    def dose_limit_reason(self, name: str) -> str | None:
        hit = dose_limit_entry(name)
        return hit[1] if hit else None

    def patterns_for(self, syndrome: str, physician: str | None = None) -> list[dict]:
        """R35 挖出来的名医用药规律。R35 之前这份文件不存在，返回空列表。

        证名匹配跟 `formulas_for_syndrome` 同一条规矩（去掉尾「证」）。

        **医家档（`group_value=""`）恒命中任何证型**：空串是 `group in key`
        的子串，这是有意的——1075 诊次里只有 116 条标了证型，只放证型档
        等于九成语料进不了知识块。代价是返回量大（医家档单个医家可上千条），
        所以返回前必须排序（`sort_patterns`），让调用方"取前 N 条"是有意义的
        取法而不是碰运气。**截断在调用方做并记数**，不在这里悄悄少给。
        """
        if not self._patterns:
            return []
        key = (syndrome or "").strip().removesuffix("证")
        out = []
        for p in self._patterns:
            if physician and p.get("physician") != physician:
                continue
            group = str(p.get("group_value") or "")
            if key and key not in group and group not in key:
                continue
            out.append(p)
        return sort_patterns(out)


# ---------- 构造 ----------

def _rows(kind: OntologyKind) -> list[dict]:
    """原始三元组行。**复用 `context_prefix._load_triples` 的读法**：
    它已经处理了"文件不在返回空""坏行跳过"这两件事，另写一份就有两种读法。
    """
    from core.context_prefix import _load_triples

    return _load_triples(kind)


def _build_herbs(rows: list[dict]) -> dict[str, Herb]:
    index = build_entry_index("materia_medica", rows)
    out: dict[str, Herb] = {}
    for raw_name, preds in index.items():
        name = normalize_herb(raw_name) or raw_name
        aliases = tuple({raw_name} - {name})
        refs = {p: _refs_for(rows, raw_name, p) for p in preds}
        existing = out.get(name)
        herb = Herb(
            name=name,
            aliases=tuple(sorted(set(aliases) | set(existing.aliases if existing else ()))),
            nature=parse_nature(preds.get("性味", [])) or (existing.nature if existing else None),
            flavor=parse_flavors(preds.get("性味", [])) or (existing.flavor if existing else ()),
            meridians=parse_meridians(preds.get("归经", []))
            | (existing.meridians if existing else frozenset()),
            effects=tuple(dict.fromkeys(
                (existing.effects if existing else ()) + parse_effects(preds.get("功效", [])))),
            dose_max_g=parse_dose_max_g(preds.get("用量", []))
            or (existing.dose_max_g if existing else None),
            contraindications=tuple(dict.fromkeys(
                (existing.contraindications if existing else ())
                + parse_effects(preds.get("禁忌", [])))),
            preparation=tuple(dict.fromkeys(
                (existing.preparation if existing else ())
                + parse_effects(preds.get("炮制", [])))),
            refs={**(existing.refs if existing else {}), **refs},
        )
        out[name] = herb
    return out


def _build_formulas(rows: list[dict]) -> dict[str, Formula]:
    index = build_entry_index("formulary", rows)
    out: dict[str, Formula] = {}
    for name, preds in index.items():
        roles = {
            role: tuple(n for n, _ in parse_composition(preds.get(f"{role}药", [])))
            for role in ("君", "臣", "佐", "使")
            if preds.get(f"{role}药")
        }
        out[name] = Formula(
            name=name,
            composition=parse_composition(preds.get("组成", [])),
            roles=roles,
            indications=parse_effects(preds.get("主治", [])),
            functions=parse_effects(preds.get("功用", [])),
            modifications=tuple(preds.get("加减", [])),
            refs={p: _refs_for(rows, name, p) for p in preds},
        )
    return out


def sort_patterns(patterns: list[dict]) -> list[dict]:
    """规律的排序规则。**只有这一处实现**：`patterns_for` 与知识块的
    合并重排都走它，两处各写一套排序会让"取前 N 条"取到不同的 N 条。

    序：证型档在前（它比医家档更贴合本次辨证）→ support 从高到低
    → `pattern_id`（确定性兜底，同 support 的顺序不能随字典序抖动）。
    """
    return sorted(
        patterns,
        key=lambda p: (
            0 if p.get("group_by") == "physician_syndrome" else 1,
            -int(p.get("support") or 0),
            str(p.get("pattern_id") or ""),
        ),
    )


def _load_patterns() -> list[dict]:
    """R35 的 `data/standard/prescribing_patterns.jsonl`。不在就是空列表。"""
    import json

    from core.data_paths import CANONICAL_DIR

    p = CANONICAL_DIR / "prescribing_patterns.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


# ---------- 惰性单例 ----------
#
# CLAUDE.md：加载大文件的对象一律惰性初始化，禁止在模块顶层实例化。
# 双重检查锁跟 `core/tools.py::get_graph_store` 同一个形状。

_ontology: Ontology | None = None
_load_lock = threading.Lock()


def get_ontology() -> Ontology:
    global _ontology
    if _ontology is None:
        with _load_lock:
            if _ontology is None:
                _ontology = Ontology()
    return _ontology


def reset_ontology_for_tests() -> None:
    """只给测试用：换了数据文件之后清掉单例。生产代码不该调它。"""
    global _ontology
    with _load_lock:
        _ontology = None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="本体层自检（零 LLM 调用）")
    ap.add_argument("--stats", action="store_true", help="打印加载统计")
    # R34c：**先看清楚再决定改不改。** 3184 条方剂三元组归并出 235 首方，
    # 平均 13.5 条/首，而《方剂学》教材的方数远多于此——这两个数放在一起说明
    # 归并那一步有问题，但"问题在哪"要看方名的实际形态才知道（归一把不同方并到
    # 一起了？还是抽取时方名带了章节前缀？）。这两个开关只打印，不动任何数据。
    ap.add_argument("--dump-formulas", action="store_true",
                    help="逐行打印方名与它的谓词条数（看方名形态用，不改数据）")
    ap.add_argument("--dump-herbs", action="store_true",
                    help="逐行打印药名与它的谓词条数")
    args = ap.parse_args(argv)
    ont = get_ontology()
    s = ont.stats()
    if not s["available"]:
        print("药理层数据不在（data/standard/materia_medica.jsonl 与 formulary.jsonl）。")
        print("这不是代码缺陷：这两份数据由 offline/extract_reference_triples.py")
        print("在有真实 LLM 的机器上抽取产出（上机剧本段 5）。本体层此刻 available=False，")
        print("所有查询返回空/None，下游按「没有本体」降级并在 manifest 里如实记录。")
        return 2
    print(f"本草 {s['n_herbs']} 味 / 方剂 {s['n_formulas']} 首 / 用药规律 {s['n_patterns']} 条")
    print("缺谓词条数：" + "，".join(f"{p} {n}" for p, n in s["missing_predicate_counts"].items()))
    print(f"出处 span 为空的引用：{s['empty_span_refs']} 条")
    if args.dump_formulas:
        print("\n--- 方名（名字 | 谓词数 | 组成药味数 | 主治条数）---")
        for name, f in sorted(ont.formulas.items()):
            print(f"{name}\t{len(f.refs)}\t{len(f.composition)}\t{len(f.indications)}")
    if args.dump_herbs:
        print("\n--- 药名（名字 | 谓词数 | 性 | 归经数 | 功效数）---")
        for name, h in sorted(ont.herbs.items()):
            print(f"{name}\t{len(h.refs)}\t{h.nature or '-'}"
                  f"\t{len(h.meridians)}\t{len(h.effects)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
