"""R64：把现成的开源结构化数据合并进本体。**只合并，不抽取。**

## 三条铁律（每一条都在代码里有对应的守卫）

1. **只填空槽，绝不覆盖**——`_fillable()` 里 `herb.has(p)` 为真就跳过。
   不比较哪个"更好"：比较就要有判据，而我们没有能判"哪本书更对"的依据。
2. **每条都带 `source` 与 `source_span`**，span 为空的不收。本项目的核心主张是
   "每一步注明出处"，来路不明的数据进来就毁了这个主张；`--stats` 里
   "出处 span 为空 0 条"是这条铁律的探针，合并后必须仍是 0。
3. **不新建药条**——匹配不上现有 1232 味的一律丢。扩药材数会动图谱节点数与
   一堆已量过的凭据（覆盖率、ε、分歧度），那是另一轮的事。

## 不碰安全判据

`core/safety_output.py` 的 `INCOMPATIBLE_PAIRS`（24 对）与 `DOSE_LIMITS`（62 味）
是**安全闸门**，本轮合并进来的"用量""禁忌"只进释义，不进那两张表。
`tests/test_merge_open_sources.py` 逐一比对合并前后 `dose_limit_entry()`
对 62 味的返回值。

## 各源的授权

| 源 | 授权 | 本轮用了什么 |
|---|---|---|
| nihaixia-app | **Apache 2.0** | 465 味本草的性味/归经/功效/用量/禁忌、327 首方剂 |
| zhongyao-xuexi-baodian | **仓库无 LICENSE**（内容出自公版古籍） | 别名表、305 味的四项、神农本草经气味主治 |
| CMLM-ZhongJing | MIT（**但数据集不在仓库里**，见 R64 变更说明） | — |

授权状态如实记在 `data/SOURCES.md`；无 LICENSE 那一个商用前需另行确认。

用法（一次一个源，好逐个 commit）：

    python -m offline.merge_open_sources --source nihaixia-herbs --repo <path> --write
    python -m offline.merge_open_sources --source baodian-alias   --repo <path> --write

不加 `--write` 只报数字（dry run），这是默认。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from core.herbs import HERB_ALIASES, normalize_herb
from core.ontology import MERIDIANS, get_ontology, reset_ontology_for_tests

ROOT = Path(__file__).resolve().parent.parent
MATERIA_PATH = ROOT / "data" / "standard" / "materia_medica.jsonl"
FORMULARY_PATH = ROOT / "data" / "standard" / "formulary.jsonl"
MERGED_ALIAS_PATH = ROOT / "data" / "standard" / "herb_aliases_merged.tsv"

#: 药典分列的品种，**永远不许归成一条**（R59 已有测试钉住这五对）。
#: 合并别名时任何一端落在这里面就丢掉这条候选——南北五味子、川怀牛膝
#: 性效不同，归一了就等于开错药。
SPLIT_SPECIES: tuple[tuple[str, ...], ...] = (
    ("五味子", "南五味子", "北五味子"),
    ("牛膝", "川牛膝", "怀牛膝"),
    ("地黄", "生地黄", "熟地黄", "干地黄"),
    ("何首乌", "生何首乌", "制首乌", "制何首乌"),
    ("木香", "川木香", "广木香"),
)
_SPLIT_FLAT = {n for group in SPLIT_SPECIES for n in group}


def _is_truncation_of(alias: str, canon: str) -> bool:
    """别名是不是正名**掉了头或尾几个字**的写法（「洋参」对「西洋参」）。

    这一类必须丢。它看起来像个无害的简称，实际上正是这个项目明确判过不许
    归并的那个形状——产地字与生/制字**是药名的一部分**，剥掉就把两味药并成
    一味（`tests/test_herbs.py::test_distinct_herbs_never_collapse_into_one_name`
    钉着六对）。第一版没有这条守卫，合并进来的「洋参→西洋参」当场把那条测试
    弄红了：批量合并来的表**不许覆盖一条既有测试已经判过的事**。

    反方向是正常的、要保留的：「紫丹参→丹参」里正名是别名的后缀，
    那是加修饰字而不是掉字。
    """
    return alias != canon and (canon.endswith(alias) or canon.startswith(alias))


def _span_ok(text: str) -> bool:
    """span 非空才收（铁律二）。纯空白、纯标点都算空。"""
    return bool(re.sub(r"[\s。，、；：,.;:]+", "", text or ""))


def _row(subject: str, predicate: str, value: str, *, book: str, dataset: str,
         span: str, kind: str) -> dict:
    """一行三元组。字段名与 `MateriaMedicaRecord` / `FormularyRecord` 对齐。

    **`source` 不是"从哪拿的"**，是 `Literal["classic", "modern"]`——古籍口径
    还是现代教材口径。第一版把仓库路径写进 `source`，schema 当场拒掉，
    那正是它该做的事。数据集出处走 `dataset`（R64 新增字段），
    授权要按数据集追责，所以它必须独立成字段而不是拼在 `book` 里。
    """
    assert kind in ("classic", "modern"), kind
    return {"s": subject, "p": predicate, "o": value.strip(),
            "book": book, "source": kind, "dataset": dataset,
            "source_span": span.strip()}


def _fillable(ont, name: str, predicate: str) -> bool:
    """这一味的这个谓词是不是空槽（铁律一）。"""
    h = ont.herb(name)
    return h is not None and not h.has(predicate)


# ---------- 源一：nihaixia-app（Apache 2.0） ----------

def _nihaixia_herbs(repo: Path, ont) -> list[dict]:
    """465 味本草 → 空槽。字段是结构化的，不用解析自由文本。

    `source_span` 用 `original`（《神农本草经》原文），它是这条记录真正的出处；
    `original` 为空时退回被合并的那个字段自身的原文——**不编一个 span 出来**。
    """
    data = json.loads((repo / "assets" / "data" / "herbs.json").read_text(encoding="utf-8"))
    book = "神农本草经（倪海厦人纪讲义校勘版）"
    ds = "nihaixia-app/assets/data/herbs.json (Apache-2.0)"
    out: list[dict] = []
    for r in data["herbs"]:
        name = normalize_herb(r.get("name") or "")
        if not name or ont.herb(name) is None:
            continue
        original = (r.get("original") or "").strip()
        # 性味：flavor 与 nature 合起来才是一句「甘，微寒」
        pairs = [
            ("性味", " ".join(x for x in (r.get("flavor") or "", r.get("nature") or "") if x).strip()),
            ("归经", "、".join(m for m in (r.get("meridians") or []) if m)),
            ("功效", (r.get("action") or "").strip()),
            ("用量", (r.get("dosage") or "").strip()),
            ("禁忌", (r.get("contraindication") or "").strip()),
        ]
        for pred, val in pairs:
            if not val or not _fillable(ont, name, pred):
                continue
            span = original or val
            if not _span_ok(span):
                continue
            out.append(_row(name, pred, val, book=book, dataset=ds, span=span,
                            kind="classic"))
    return out


def _nihaixia_formulas(repo: Path, ont) -> list[dict]:
    """327 首方剂 → 方剂本体里没有的那些。**方剂是按方名新增的**
    （§4.1 第 5 步明确要 235 → 更多），跟"不新建药条"那条不冲突：
    药条一多会动图谱节点数与一堆量过的凭据，方剂不在那些凭据里。
    """
    data = json.loads((repo / "assets" / "data" / "formulas.json").read_text(encoding="utf-8"))
    book = "倪海厦人纪系列（伤寒论/金匮要略）"
    ds = "nihaixia-app/assets/data/formulas.json (Apache-2.0)"
    have = {n.strip() for n in ont.formulas}
    out: list[dict] = []
    for f in data["formulas"]:
        name = (f.get("name") or "").strip()
        if not name or name in have:
            continue
        # 方名里带数字/「第」/括号的整条不收。`tests/test_ontology_coverage.py::
        # test_no_formula_name_carries_a_chapter_prefix` 钉着"方名不含章节标记"
        # ——它防的是抽取时把章节号粘进方名。这个数据集里「乳癌经验方第一方」
        # 是真名不是artifact，但那条守卫分辨不出来，而**放宽一条守卫去换两首方
        # 不值得**：守卫松了以后真的粘进章节号也就没人发现了。
        if _CHAPTER_MARK_RE.search(name):
            continue
        comps = [c for c in (f.get("components") or []) if (c.get("name") or "").strip()]
        if not comps:
            continue
        span = (f.get("indication") or f.get("explanation") or "").strip()
        if not _span_ok(span):
            continue
        for c in comps:
            dose = (c.get("dosage") or "").strip()
            out.append(_row(name, "组成", f"{c['name'].strip()}{(' ' + dose) if dose else ''}",
                            book=book, dataset=ds, span=span, kind="classic"))
        for pred, val in (("主治", f.get("indication") or ""),
                          ("功用", f.get("explanation") or "")):
            if (val or "").strip():
                out.append(_row(name, pred, val, book=book, dataset=ds, span=span,
                                kind="classic"))
    return out


# ---------- 源二：zhongyao-xuexi-baodian（仓库无 LICENSE） ----------

def _load_js_const(path: Path, name: str):
    """从纯数据 JS 里取一个 const。**括号配平后交给 json.loads**，不用 eval：
    这些文件是别人仓库里的内容，用 eval 等于执行外部代码。
    只对 JSON 兼容的那几个常量有效（ALIAS / JBI_EXTRA / BENCAO）。
    """
    txt = path.read_text(encoding="utf-8")
    m = re.search(rf"const\s+{re.escape(name)}\s*=\s*", txt)
    if not m:
        raise ValueError(f"{path.name} 里没有 const {name}")
    start = i = m.end()
    open_ch = txt[i]
    close_ch = {"{": "}", "[": "]"}[open_ch]
    depth, in_str, esc = 0, False, False
    while i < len(txt):
        c = txt[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return json.loads(txt[start:i + 1])
        i += 1
    raise ValueError(f"{path.name} 的 const {name} 括号不配平")


def _baodian_alias(repo: Path, ont) -> list[tuple[str, str, str]]:
    """别名表 → (别名, 正名, 出处)。补归一缺口，不进三元组。

    四道过滤，每一道都有它防的事：
      1. 正名不在本体里 → 丢（不新建药条）
      2. 别名已经能归一到同一个正名 → 丢（重复，表只会变大不会更准）
      3. 别名**本身是本体里另一味药** → 丢（会把两味药并成一味）
      4. 任一端在 `SPLIT_SPECIES` 里 → 丢（药典分列的品种不许归并）
    """
    alias = _load_js_const(repo / "aliases.js", "ALIAS")
    src = "zhongyao-xuexi-baodian/aliases.js"
    # **生成时把上一次的合并表清空**，否则这个脚本不可重跑：`normalize_herb`
    # 现在会读那张表，于是上一轮写进去的 740 条全部落进下面
    # 「已经能归一了」那道过滤，第二次跑输出 1 条、把表覆盖成 1 行。
    # 实测踩过：第二次跑完 `wc -l` 从 745 变成 6。
    # 判据只看**人工审过的 `HERB_ALIASES`**，这样每次跑都是同一个结果。
    from core import herbs as _herbs
    _herbs._merged = {}
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for canon, names in alias.items():
        c = normalize_herb(canon) or canon
        if ont.herb(c) is None or c in _SPLIT_FLAT:
            continue
        for a in names:
            a = (a or "").strip()
            if not a or a in seen or a in HERB_ALIASES:
                continue
            if a in _SPLIT_FLAT:
                continue
            if normalize_herb(a) == c:
                continue
            if ont.herb(normalize_herb(a) or a) is not None:
                continue
            if _is_truncation_of(a, c):
                continue
            seen.add(a)
            out.append((a, c, src))
    return out


#: `HERBS` 的一行：`['麻黄','mh','辛、微苦，温','肺、膀胱经','发汗解表…','2～10g',…]`
#: 一行一味，所以按行取前六个单引号字段就够，不用 JS 解释器。
_HERBS_ROW_RE = re.compile(r"^\s*\['([^']*)','([^']*)','([^']*)','([^']*)','([^']*)','([^']*)'")


def _baodian_herbs(repo: Path, ont) -> list[dict]:
    """tcm-data.js 的 HERBS（305 味，中药学口径的性味/归经/功效/用量）→ 空槽。"""
    book = "中药学（zhongyao-xuexi-baodian 整理）"
    ds = "zhongyao-xuexi-baodian/tcm-data.js#HERBS（仓库无 LICENSE）"
    out: list[dict] = []
    for line in (repo / "tcm-data.js").read_text(encoding="utf-8").splitlines():
        m = _HERBS_ROW_RE.match(line)
        if not m:
            continue
        raw, _py, xw, gj, gx, yl = m.groups()
        name = normalize_herb(raw) or raw
        if ont.herb(name) is None:
            continue
        span = line.strip().rstrip(",")
        for pred, val in (("性味", xw), ("归经", gj), ("功效", gx), ("用量", yl)):
            if val.strip() and _fillable(ont, name, pred) and _span_ok(span):
                out.append(_row(name, pred, val, book=book, dataset=ds, span=span,
                                kind="modern"))
    return out


def _baodian_bencao(repo: Path, ont) -> list[dict]:
    """bencao.js 的 BENCAO（《本草纲目》气味/主治）→ 性味与功效的空槽。

    **本轮没用上。** 这个文件的键没加引号（`{gm:{...}}`），`_load_js_const`
    的"括号配平 + json.loads"读不了它，而读它就得上 JS 解释器——对别人仓库里
    的文件跑 eval 不做。实测它只能填 3 个槽位（性味 1、功效 2），
    为 3 个槽位引入一个 eval 不值得。留着函数与这段说明，下一轮要是换了
    正经的 JS 解析器再接上。
    """
    data = _load_js_const(repo / "bencao.js", "BENCAO")
    ds = "zhongyao-xuexi-baodian/bencao.js（仓库无 LICENSE）"
    out: list[dict] = []
    for raw, d in data.items():
        name = normalize_herb(raw) or raw
        if ont.herb(name) is None:
            continue
        gm = d.get("gm") or {}
        for pred, key in (("性味", "w"), ("功效", "z")):
            val = (gm.get(key) or "").strip()
            if val and _fillable(ont, name, pred) and _span_ok(val):
                out.append(_row(name, pred, val, book="本草纲目", dataset=ds, span=val,
                                kind="classic"))
    return out


#: 方名里的章节标记。跟 `test_no_formula_name_carries_a_chapter_prefix`
#: 的正则一致——两边不一致的话合并进来的东西会当场把那条测试弄红。
_CHAPTER_MARK_RE = re.compile(r"[0-9第章节（）()]")

_GUIJING_RE = re.compile(r"(入|归)([^，。；\n]{1,20}?)经")


def _baodian_jianbie(repo: Path, ont) -> list[dict]:
    """jianbie_extra.js 的 JBI_EXTRA（639 条，底层数据 Apache-2.0）→ 空槽。

    §3 第 7 条：`s` 字段形如「苦辛，微寒，归肝胆经」——**归经要拆出来**。
    拆法是找「归…经」/「入…经」，并且拆出来的脏腑必须在本体的 `MERIDIANS`
    词表里认得出至少一个；认不出的整条跳过（"拆不干净的不硬塞"）。
    """
    data = _load_js_const(repo / "jianbie_extra.js", "JBI_EXTRA")
    ds = ("zhongyao-xuexi-baodian/jianbie_extra.js"
          "（底层：中药世家 MedicineRecommendation, Apache-2.0）")
    out: list[dict] = []
    for raw, d in data.items():
        name = normalize_herb(raw) or raw
        if ont.herb(name) is None:
            continue
        s_field = (d.get("s") or "").strip()
        for pred, val in (("性味", s_field), ("功效", (d.get("f") or "").strip()),
                          ("用量", (d.get("y") or "").strip())):
            if val and _fillable(ont, name, pred) and _span_ok(val):
                out.append(_row(name, pred, val, book="中华本草（数据集整理）",
                                dataset=ds, span=val, kind="modern"))
        if _fillable(ont, name, "归经") and s_field:
            m = _GUIJING_RE.search(s_field)
            if m:
                organs = [x for x in MERIDIANS if x in m.group(2)]
                if organs:
                    out.append(_row(name, "归经", "、".join(organs),
                                    book="中华本草（数据集整理）", dataset=ds,
                                    span=s_field, kind="modern"))
    return out


SOURCES = {
    "nihaixia-herbs": _nihaixia_herbs,
    "nihaixia-formulas": _nihaixia_formulas,
    "baodian-herbs": _baodian_herbs,
    # "baodian-bencao": 见该函数的说明——文件读不了，且只值 3 个槽位
    "baodian-jianbie": _baodian_jianbie,
}


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_alias_tsv(pairs: list[tuple[str, str, str]]) -> None:
    lines = ["# R64：从开源数据合并来的药名别名。**手工审过的那批在 core/herbs.py 的",
             "# HERB_ALIASES 里**，这个文件是批量合并来的，冲突时以代码里那份为准。",
             "# 药典分列的品种（南北五味子、川怀牛膝、生熟地黄、生制首乌、川广木香）",
             "# 在生成时就被排除，不在这里出现。",
             "别名\t正名\t出处"]
    lines += [f"{a}\t{c}\t{s}" for a, c, s in sorted(pairs)]
    MERGED_ALIAS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    choices=[*SOURCES, "baodian-alias"])
    ap.add_argument("--repo", required=True, help="clone 下来的仓库目录")
    ap.add_argument("--write", action="store_true", help="真的写文件（默认只报数）")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"仓库目录不存在：{repo}", file=sys.stderr)
        return 2
    reset_ontology_for_tests()
    ont = get_ontology()
    if not ont.available:
        print("本体层数据文件不在，无法判断哪些是空槽——拒绝合并。", file=sys.stderr)
        return 2

    if args.source == "baodian-alias":
        pairs = _baodian_alias(repo, ont)
        print(f"可新增别名 {len(pairs)} 条")
        for a, c, _s in pairs[:10]:
            print(f"  {a} → {c}")
        if args.write:
            _write_alias_tsv(pairs)
            print(f"写到 {MERGED_ALIAS_PATH.relative_to(ROOT)}")
        return 0

    rows = SOURCES[args.source](repo, ont)
    import collections
    by_pred = collections.Counter(r["p"] for r in rows)
    subjects = {r["s"] for r in rows}
    print(f"{args.source}：{len(rows)} 条，涉及 {len(subjects)} 个主语")
    for p, n in by_pred.most_common():
        print(f"  {p}\t{n}")
    assert all(_span_ok(r["source_span"]) for r in rows), "有 span 为空的行漏进来了"
    assert all(r["dataset"] for r in rows), "有 dataset 为空的行漏进来了"
    # **落盘前逐行过 schema**。第一版没这一步，`source` 写成仓库路径的 380 行
    # 全都写进了文件，是 `tests/test_pharmacology_committed.py` 事后抓出来的
    # ——那已经是"坏数据进了版本控制"之后。写之前挡住比写之后发现便宜得多。
    from core.schemas import FormularyRecord, MateriaMedicaRecord
    model = FormularyRecord if "formulas" in args.source else MateriaMedicaRecord
    for r in rows:
        model.model_validate(r)
    if args.write:
        path = FORMULARY_PATH if "formulas" in args.source else MATERIA_PATH
        _append_jsonl(path, rows)
        print(f"追加到 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
