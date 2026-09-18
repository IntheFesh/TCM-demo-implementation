"""R59：`HERB_ALIASES` 只有 70 条，用户真机实测 `cases.json` 里的 698 种药名
写法，归一后（`core/herbs.normalize_herb` + 现有别名表）仍有 367 种（52.6%）
查不到本体正名——归一"一个都没减少"。这个脚本**只生成候选，不自动写入**
`core/herbs.HERB_ALIASES`：收错一条比不收更糟（会把两味药归成一味），
每条候选都要人工确认。

    python -m scripts.build_herb_aliases                    # 默认读 cases.json
    python -m scripts.build_herb_aliases --cases-path x.json
    python -m scripts.build_herb_aliases --names-path names.txt   # 备选：一行一个写法，不依赖 cases.json

**零 LLM 调用**——纯规则匹配，见模块内 `generate_candidates()`。

## 四类确定性规则（唯一实现，命中即停，不重复算）

1. **产地前缀**：剥 `云北南杭化州广川怀西建辽` 中的一个字符（云茯苓→茯苓）。
   `PRODUCER_PREFIX_CHARS` **只在这个脚本里用于生成候选**，不进
   `core/herbs.py::_AFFIX_CHARS`——那条注释写得很清楚：产地字是药名的一
   部分，逐字剥产地会把不同的药错误归成同一个名字，这条纪律不许违反。
2. **部位/修饰后缀**：剥 `叶皮柄梢尖心肉子仁`（不含"块"/"皮尖"——那两个
   已经在 `core/herbs.py::_HERB_SUFFIX` 里，这个脚本复用那个正则，不新建
   一份重叠的后缀表，CLAUDE.md「同一概念只有一处实现」）。
3. **修饰前缀**：剥 `全净大小老嫩`（全当归→当归）。
4. **简称补全（子串）**：写法是本体正名的子串、或本体正名是写法的子串，
   且唯一命中——丹皮→牡丹皮（前者）、乌附子→附子（后者，如果唯一）。

**叠加**：前缀规则可以连续剥多层（净杭萸肉→剥净→剥杭→萸肉），每一层剥完
都会连同后缀剥离、子串匹配一起再试一次——不是只在剥到底之后才查。

## 置信三档

- **high**：单条规则、精确命中本体正名。
- **medium**：多条规则叠加命中、或子串匹配有不止一个候选。
- **low**：子串匹配、唯一命中。

只生成候选，**不判断哪些是"真本体未收"**——脚本给不出候选，不代表这味药
真的不在本体里，也可能是规则覆盖不到的写法（"于术"→"白术"这类纯异名，
两个字都不一样，任何字符级规则都剥不出来，需要人工确认）。人工审查阶段
把明确"真的没有这味药"的写法记进 `data/standard/herb_not_in_ontology.tsv`，
其余留给下一步人工判断。
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.herbs import _HERB_SUFFIX as _EXISTING_SUFFIX_RE
from core.ontology import get_ontology

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_CANDIDATES_OUT = ROOT / "data" / "standard" / "herb_alias_candidates.tsv"
DEFAULT_NOT_IN_ONTOLOGY_OUT = ROOT / "data" / "standard" / "herb_not_in_ontology.tsv"

#: 仅用于生成候选，**不进 `core/herbs.py::_AFFIX_CHARS`**（见模块文档字符串）。
PRODUCER_PREFIX_CHARS = "云北南杭化州广川怀西建辽"
MODIFIER_PREFIX_CHARS = "全净大小老嫩"
#: 不含"块"/"皮尖"——那两个已在 `core.herbs._HERB_SUFFIX` 里，见下面
#: `_strip_suffix_variants` 对既有正则的复用。
PART_SUFFIX_CHARS = "叶皮柄梢尖心肉子仁"


def extract_herb_names_from_cases(records: list[dict]) -> Counter:
    """`cases.json` 是 `CaseRecord` 的扁平列表（`offline/extract_cases.py`
    写出来的形状），每条诊次的 `herbs` 字段是这一诊用的药名写法。"""
    counts: Counter = Counter()
    for r in records:
        for h in r.get("herbs") or []:
            h = (h or "").strip()
            if h:
                counts[h] += 1
    return counts


def _iter_prefix_strips(name: str):
    """从原串开始，逐层剥产地/修饰前缀，每剥一层都 `yield` 一次——跟
    `core.herbs.normalize_herb` 剥 `_AFFIX_CHARS` 同一个迭代写法（逐字剥、
    每剥一次都是一个候选点，不是一次性剥到底再查）。"""
    core = name
    rules: list[str] = []
    yield core, list(rules)
    while len(core) > 2:
        if core[0] in PRODUCER_PREFIX_CHARS:
            core = core[1:]
            rules.append("产地前缀")
        elif core[0] in MODIFIER_PREFIX_CHARS:
            core = core[1:]
            rules.append("修饰前缀")
        else:
            break
        yield core, list(rules)


def _strip_suffix_variants(name: str) -> list[tuple[str, str]]:
    """在某一层前缀剥离结果上，再试两种后缀剥法：这个脚本新增的部位/修饰
    后缀表，以及复用 `core.herbs._HERB_SUFFIX`（含"块"/"皮尖"/汁炭末粉片）
    ——不新建一套跟它重叠的后缀表。"""
    out = []
    if len(name) > 2 and name[-1] in PART_SUFFIX_CHARS:
        out.append((name[:-1], "部位后缀"))
    reused = _EXISTING_SUFFIX_RE.sub("", name)
    if reused != name and len(reused) >= 2:
        out.append((reused, "既有炮制/部位后缀（core.herbs._HERB_SUFFIX）"))
    return out


def _all_variants(name: str) -> list[tuple[str, list[str]]]:
    """一个写法能通过「剥前缀（0~N 层）+ 剥后缀（0~1 层）」到达的全部变体，
    每个变体带上到达它用过的规则列表（顺序即施加顺序）。**包含原串本身**
    （0 层剥离），排在第一个。"""
    variants: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for prefix_stripped, prefix_rules in _iter_prefix_strips(name):
        if prefix_stripped not in seen:
            seen.add(prefix_stripped)
            variants.append((prefix_stripped, prefix_rules))
        for suffix_stripped, suffix_rule in _strip_suffix_variants(prefix_stripped):
            if suffix_stripped not in seen:
                seen.add(suffix_stripped)
                variants.append((suffix_stripped, prefix_rules + [suffix_rule]))
    return variants


def _drops_a_distinguishing_marker(variant: str, candidate: str, canonical_names: set[str]) -> bool:
    """子串匹配可能把 `variant` 开头的"生"/"熟"炮制标记悄悄吞掉（比如
    "熟怀地黄"反向命中"地黄"——中间的"怀"被子串匹配跳过，"熟"这个字也
    跟着丢了）。**这个标记本身不是产地/修饰字符，`_iter_prefix_strips`
    从不剥它**（跟 `core/herbs.py::_AFFIX_CHARS` 故意不收"生"是同一条纪律：
    生地黄≠熟地黄）——但子串匹配是整串扫，不认前缀语义，会绕过这层保护。

    真正危险的只有一种情况：本体对同一味药**确实**生/熟分列两条正名
    （比如 地黄/熟地黄）。这时丢标记就可能把写法错配到另一个药典品种上。
    如果本体压根没有这条"标记+candidate"的正名（比如"半夏"没有对应的
    "熟半夏"独立条目），丢标记不会造成任何混淆，不用拦。"""
    if candidate.startswith(("生", "熟")):
        return False
    for marker in ("生", "熟"):
        if marker in variant and marker not in candidate and (marker + candidate) in canonical_names:
            return True
    return False


def generate_candidates(name: str, canonical_names: set[str]) -> list[dict]:
    """对一个查不到本体的写法，生成候选正名列表（可能为空）。每条候选是
    `{"candidate", "rule", "confidence"}`。**只生成，不自动判定哪条对**——
    见模块文档字符串的置信三档。"""
    variants = _all_variants(name)

    exact: dict[str, list[str]] = {}
    for variant, rules in variants:
        if variant == name:
            continue
        if variant in canonical_names:
            exact.setdefault(variant, rules)
    if exact:
        out = []
        for cand, rules in exact.items():
            confidence = "high" if len(rules) <= 1 else "medium"
            out.append({"candidate": cand, "rule": "+".join(rules), "confidence": confidence})
        return out

    # 没有精确命中：对原串和每一层剥离结果都试一次子串匹配（正向：写法是
    # 正名的子串；反向：正名是写法的子串），任何一层给出唯一候选就采用
    # 剥离最浅的那一层——变形越少，越不容易引入额外的歧义。
    for variant, rules in variants:
        forward = {c for c in canonical_names if variant in c and c != variant and len(variant) >= 2}
        backward = {c for c in canonical_names if c in variant and c != variant and len(c) >= 2}
        matches = sorted(forward | backward)
        matches = [m for m in matches if not _drops_a_distinguishing_marker(variant, m, canonical_names)]
        if len(matches) == 1:
            rule = "简称补全（子串）" if not rules else "+".join(rules) + "+简称补全（子串）"
            return [{"candidate": matches[0], "rule": rule, "confidence": "low"}]
        if len(matches) > 1:
            rule = "简称补全（子串，多候选）" if not rules else "+".join(rules) + "+简称补全（子串，多候选）"
            return [{"candidate": m, "rule": rule, "confidence": "medium"} for m in matches[:5]]
    return []


def build(counts: Counter, *, ontology=None) -> tuple[list[dict], list[dict]]:
    """返回 `(candidates_rows, not_in_ontology_rows)`。`counts` 是写法→出现
    次数（`extract_herb_names_from_cases` 的输出或等价物）。"""
    ont = ontology if ontology is not None else get_ontology()
    canonical_names = set(ont.herbs)

    candidate_rows: list[dict] = []
    no_candidate_rows: list[dict] = []
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if ont.herb(name) is not None:
            continue  # normalize_herb + 现有别名表已经解决，不算"查不到"
        cands = generate_candidates(name, canonical_names)
        if cands:
            for c in cands:
                candidate_rows.append({"写法": name, "候选正名": c["candidate"],
                                       "命中规则": c["rule"], "出现次数": n,
                                       "置信": c["confidence"]})
        else:
            no_candidate_rows.append({"写法": name, "出现次数": n})
    return candidate_rows, no_candidate_rows


def write_tsv(rows: list[dict], path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("\t".join(columns) + "\n")
        for r in rows:
            f.write("\t".join(str(r[c]) for c in columns) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--names-path", type=Path, default=None,
                    help="备选：一份纯文本写法列表（一行一个），跳过读 cases.json")
    ap.add_argument("--candidates-out", type=Path, default=DEFAULT_CANDIDATES_OUT)
    ap.add_argument("--not-in-ontology-out", type=Path, default=DEFAULT_NOT_IN_ONTOLOGY_OUT)
    args = ap.parse_args(argv)

    if args.names_path is not None:
        if not args.names_path.exists():
            print(f"写法列表文件不在：{args.names_path}")
            return 2
        lines = [ln.strip() for ln in args.names_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        counts = Counter(lines)
        n_records = None
    else:
        if not args.cases_path.exists():
            print(f"{args.cases_path} 不在——这个脚本要读真实医案数据才有意义，"
                 "按 README.md「快速开始」第 3 步生成，或用 --names-path 传一份"
                 "自备的写法列表跳过这一步。")
            return 2
        records = json.loads(args.cases_path.read_text(encoding="utf-8"))
        counts = extract_herb_names_from_cases(records)
        n_records = len(records)

    ont = get_ontology()
    if not ont.available:
        print("本体不可用（data/standard/materia_medica.jsonl 不在）——这个脚本"
             "拿本体正名当比对基准，没有本体就生成不出候选。")
        return 2

    total = len(counts)
    resolved = sum(1 for name in counts if ont.herb(name) is not None)
    candidate_rows, no_candidate_rows = build(counts, ontology=ont)

    write_tsv(candidate_rows, args.candidates_out,
             ["写法", "候选正名", "命中规则", "出现次数", "置信"])
    write_tsv(no_candidate_rows, args.not_in_ontology_out, ["写法", "出现次数"])

    by_confidence = Counter(r["置信"] for r in candidate_rows)
    n_names_with_candidate = len({r["写法"] for r in candidate_rows})
    unresolved = total - resolved

    print(f"{'从 ' + str(n_records) + ' 条诊次里' if n_records is not None else ''}"
         f"数出 {total} 种药名写法，{resolved} 种（{resolved / total:.1%}）本体已能"
         f"直接查到（normalize_herb + 现有 HERB_ALIASES），{unresolved} 种"
         f"（{unresolved / total:.1%}）查不到。")
    print(f"查不到的 {unresolved} 种里，{n_names_with_candidate} 种生成出了候选"
         f"（high={by_confidence.get('high', 0)}、medium={by_confidence.get('medium', 0)}、"
         f"low={by_confidence.get('low', 0)} 条候选，同一写法可能有多条候选）；"
         f"{len(no_candidate_rows)} 种一条候选都生成不出，进了"
         f"{args.not_in_ontology_out}。")
    print(f"→ {args.candidates_out}")
    print(f"→ {args.not_in_ontology_out}")
    print("\n下一步：人工审查 candidates 文件，high 档批量确认，medium/low 逐条"
         "判断，判不准宁可不收——回 core/herbs.py::HERB_ALIASES 手工合入，"
         "这个脚本不自动写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
