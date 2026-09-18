"""R46 §7.3：循证对照层——把本次推导的结论与**教材推荐方案**逐项比对。

## 这一层不是用来择优的

深圳那家三甲的实际用法是：系统给出方案，并**详细标注与推荐方案的异同**。
医师需要知道"系统的推导跟教科书差在哪、为什么"。

**所以这一层只呈现差异，不参与选择。** 它没有分数、没有排序、不回答"哪个更好"
——那会把它变成另一种投票（R44 刚把投票痕迹从产品面上消除）。差异本身就是
给医师看的信息：一致说明有教材支持，不一致说明这一次的推导有它自己的理由，
而那个理由在推导链上写着。

## 底本是教材推荐方案，不是「指南」

任务书原文要的是《中医药循证临床实践指南》。**那份指南的全文不在这个项目里**，
也没有可公开获取的机读版本——照抄一份"指南说什么"等于编造出处。

所以底本是 `data/standard/guidelines.jsonl`，由
`offline/build_guidelines.py` 从《方剂学》本体生成，每条带书名与原文片段。
**界面文案如实称「与教材推荐方案的对照」，不称"指南"。**
将来拿到指南全文时，那个文件多一批 `source` 不同的行即可，本模块一行不用改。

## 比法各走各的既有实现

- 治法：`core.effect_synonyms.expand_effect`（治法↔功效同义表）
  ——不在这里另写一套字面比对；
- 药味：`core.herbs.strip_dose_and_parens` 去掉剂量与括注之后比集合
  ——加减处数就是对称差的大小。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from core.effect_synonyms import expand_effect
from core.herbs import strip_dose_and_parens

GUIDELINES_PATH = Path(__file__).resolve().parent.parent / "data" / "standard" / "guidelines.jsonl"

#: 产品面上这一层叫什么。**一处定义**——前端、节点释义、病历文书三处都从
#: 这里取，散成三份的话改一处漏两处，而漏掉的那两处会继续称它"指南"。
BASIS_LABEL = "教材推荐方案"
BASIS_ID = "textbook"


@dataclass(frozen=True)
class GuidelineEntry:
    syndrome: str
    syndrome_code: str
    disease: str
    recommended_principle: str
    recommended_formula: str
    evidence_level: str
    source: str
    span: str


_cache: tuple[GuidelineEntry, ...] | None = None
_lock = threading.Lock()


def load_guidelines(path: Path | None = None) -> tuple[GuidelineEntry, ...]:
    """惰性加载（CLAUDE.md：加载大文件的对象一律惰性初始化）。
    文件不在时返回空元组——**不抛**：这一层是增量信息，缺了它辨证照跑，
    只是产品面上少一行对照。"""
    global _cache
    if path is None and _cache is not None:
        return _cache
    p = path or GUIDELINES_PATH
    rows: list[GuidelineEntry] = []
    if p.exists():
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                rows.append(GuidelineEntry(
                    syndrome=d.get("syndrome", ""),
                    syndrome_code=d.get("syndrome_code", ""),
                    disease=d.get("disease", ""),
                    recommended_principle=d.get("recommended_principle", ""),
                    recommended_formula=d.get("recommended_formula", ""),
                    evidence_level=d.get("evidence_level", ""),
                    source=d.get("source", ""),
                    span=d.get("span", ""),
                ))
    out = tuple(rows)
    if path is None:
        with _lock:
            _cache = out
    return out


def reset_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def entries_for(syndrome: str) -> list[GuidelineEntry]:
    """这个证型下的全部推荐方案。

    去尾「证」再比——跟 `Ontology.formulas_for_syndrome` 与
    `node_explain.syndrome_row` 同一条规矩（生成这份文件的时候用的就是它）。
    """
    name = (syndrome or "").strip()
    if not name:
        return []
    stem = name.removesuffix("证")
    return [e for e in load_guidelines()
            if e.syndrome == name or e.syndrome.removesuffix("证") == stem]


def _principle_matches(ours: str, theirs: str) -> bool:
    """治法算不算一致。走同义表，不裸比字符串。

    覆盖检查那次撞墙（「水肿」vs「肿胀」判成未覆盖）就是裸比出来的；
    这里比的是治法，同一件事换个说法更常见（「疏肝理气」/「疏肝解郁」）。
    """
    if not ours or not theirs:
        return False
    a = set(expand_effect(ours))
    b = set(expand_effect(theirs))
    if a & b:
        return True
    # 教材的功用常常是几项合写（「发汗解表，宣肺平喘」）：逐项再比一次
    for part in _split_terms(theirs):
        if set(expand_effect(part)) & a:
            return True
    for part in _split_terms(ours):
        if set(expand_effect(part)) & b:
            return True
    return False


def _split_terms(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text or "":
        if ch in "，,、；;。 ":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(buf.strip())
    return out


def _herb_set(names: list[str] | tuple[str, ...]) -> set[str]:
    return {strip_dose_and_parens(n) for n in (names or []) if strip_dose_and_parens(n)}


def compare(
    syndrome: str,
    principle: str,
    formula_name: str,
    herbs: list[str] | None = None,
) -> dict:
    """本次结论 vs 教材推荐方案。

    三类结果分得清清楚楚（跟工具层返回空的三分法同一条纪律）：
      - `covered=False` → 这个证型**底本里没有**，如实说，不编；
      - `aligned`       → 一致的部分，每条带出处；
      - `deviations`    → 不一致的部分，每条含「推荐什么、本次是什么」。

    **没有分数，没有排序，没有"建议采用哪一个"。**
    """
    entries = entries_for(syndrome)
    base = {
        "basis": BASIS_ID,
        "basis_label": BASIS_LABEL,
        "syndrome": syndrome or "",
        "covered": bool(entries),
        "entries": [e.__dict__ for e in entries],
        "aligned": [],
        "deviations": [],
        "not_covered": None,
        "summary": "",
    }
    if not entries:
        base["not_covered"] = (
            f"{BASIS_LABEL}里没有「{syndrome or '（未给出证型）'}」这一条"
            f"——本次推导没有可比对的教材条目，不代表推导有误，也不代表它被教材支持。")
        base["summary"] = f"与{BASIS_LABEL}的对照：未覆盖本证型"
        return base

    ours_herbs = _herb_set(herbs or [])
    aligned: list[dict] = []
    deviations: list[dict] = []

    # 主方
    names = [e.recommended_formula for e in entries]
    if formula_name and formula_name in names:
        hit = next(e for e in entries if e.recommended_formula == formula_name)
        aligned.append({"what": "主方", "detail": f"与{BASIS_LABEL}一致：{formula_name}",
                        "source": hit.source, "span": hit.span})
    elif formula_name:
        deviations.append({
            "what": "主方",
            "recommended": "／".join(names),
            "ours": formula_name,
            "note": f"本次选的方不在{BASIS_LABEL}给这个证型列出的方里",
            "source": entries[0].source, "span": entries[0].span,
        })

    # 治法
    matched = next((e for e in entries
                    if _principle_matches(principle, e.recommended_principle)), None)
    if matched:
        aligned.append({"what": "治法",
                        "detail": f"与{BASIS_LABEL}一致：{matched.recommended_principle}",
                        "source": matched.source, "span": matched.span})
    elif principle:
        deviations.append({
            "what": "治法",
            "recommended": "／".join(e.recommended_principle for e in entries if e.recommended_principle),
            "ours": principle,
            "note": "治法与教材记载的功用不属同一族（已过治法↔功效同义表）",
            "source": entries[0].source, "span": entries[0].span,
        })

    # 加减：只有主方一致时才有意义——方都不一样，比药味差几味没有意义
    n_mod = 0
    if ours_herbs and formula_name and formula_name in names:
        from core.ontology import get_ontology

        f = get_ontology().formula(formula_name)
        theirs = _herb_set(list(f.herb_names())) if f else set()
        if theirs:
            added = sorted(ours_herbs - theirs)
            removed = sorted(theirs - ours_herbs)
            n_mod = len(added) + len(removed)
            if n_mod:
                deviations.append({
                    "what": "加减",
                    "recommended": "、".join(sorted(theirs)),
                    "ours": "、".join(sorted(ours_herbs)),
                    "note": (f"较教材原方加 {len(added)} 味"
                             f"（{'、'.join(added) or '无'}）、"
                             f"减 {len(removed)} 味（{'、'.join(removed) or '无'}）"),
                    "source": entries[0].source, "span": entries[0].span,
                })
            else:
                aligned.append({"what": "加减", "detail": "药味与教材原方相同",
                                "source": entries[0].source, "span": entries[0].span})

    base["aligned"] = aligned
    base["deviations"] = deviations
    parts = []
    if any(a["what"] == "主方" for a in aligned):
        parts.append("主方一致")
    elif any(d["what"] == "主方" for d in deviations):
        parts.append("主方不同")
    if any(a["what"] == "治法" for a in aligned):
        parts.append("治法一致")
    elif any(d["what"] == "治法" for d in deviations):
        parts.append("治法不同")
    if n_mod:
        parts.append(f"加减 {n_mod} 处")
    base["summary"] = f"与{BASIS_LABEL}的对照：" + ("，".join(parts) if parts else "无可比对项")
    return base


def coverage_stats() -> dict:
    """底本覆盖了多少证型。**产品面上要带这个数**——「未覆盖」出现时，
    读的人得知道这一层整体覆盖到什么程度，否则会以为是自己这一次特殊。"""
    rows = load_guidelines()
    return {
        "rows": len(rows),
        "syndromes": len({r.syndrome for r in rows}),
        "formulas": len({r.recommended_formula for r in rows}),
        "books": sorted({r.source for r in rows if r.source}),
        "basis_label": BASIS_LABEL,
    }
