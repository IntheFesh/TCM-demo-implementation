"""R35：从 `cases.json` 挖名医用药规律。**零 LLM 调用**，纯统计。

产出 `data/standard/prescribing_patterns.jsonl`，消费方是
`core/ontology.py::patterns_for()` → `core/context_prefix.py` 的知识块规律段
（那一段在超预算时**永不裁**——它是"融合五家"这件事在知识层的载体）。

## 为什么是统计而不是让模型总结

"叶天士在脾胃气虚证下常用党参、白术"这句话，让模型从医案里总结出来是**生成**，
会编；数出来是**计数**，不会。而且数出来的每一条都能回指到具体的 case_id，
模型总结的回指不了。这跟 `RationaleRecord`（规则抽取而非 LLM 抽取）是同一个选择。

## 四类规律

| kind | 挖的是什么 | 数据来源 |
|---|---|---|
| `herb` | 高频药 | `herbs` 列表计数 |
| `herb_pair` | 常用药对 | `herbs` 两两共现计数 |
| `dose` | 剂量习惯 | **从 `raw` 原文里抓**（`herbs` 没有结构化剂量） |
| `modification` | 加减习惯 | 复诊序列里前后两诊的药物差集 |

## 三个诚实约束

**一、每条都带 `support` 与 `case_ids`。** 两味药在 3 张方里一起出现过不构成
"某位医家习惯用这个药对"。缺了 `case_ids` 的规律无法回查，等于一句没有出处的话。

**二、按医家与按医家+证型两档都产出。** `cases.json` 里 1075 诊次只有 **116 条**
标了证型（10.8%）——只按证型分组的话九成医案进不了规律层；只按医家分组则丢掉了
"他在这个证下怎么用药"这个更有用的粒度。两档的 `support` 天差地别，
所以 `group_by` 字段必须在产物里，引用时要能看出这条是哪一档。

**三、十八反十九畏标出来、不删掉。** 古籍医案里真的有这种配伍（历史事实），
删掉等于篡改语料。判据复用 `core.safety_output.INCOMPATIBLE_PAIRS`，
说明文本复用 `INCOMPATIBLE_TRAINING_NOTE`，不另写一套。
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

from core.data_paths import CANONICAL_DIR
from core.herbs import normalize_herb
from core.physicians import PHYSICIANS, physicians_all
from core.safety_output import INCOMPATIBLE_PAIRS, normalize_for_incompat
from core.schemas import PrescribingPattern

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
#: 落 `data/standard/`。CLAUDE.md 那条坑踩过四次：`.gitignore` 对 `*.jsonl`
#: 整体忽略、只对 `data/standard/*.jsonl` 开了例外，落在别处会被静默吞掉。
DEFAULT_OUT_PATH = CANONICAL_DIR / "prescribing_patterns.jsonl"

#: 一条规律至少要几张方支持。
#:
#: **3 是取舍不是测量**，理由写在这里：2 张方的"共现"在 751 张方的语料里是噪声
#: （随机两味常用药共现 2 次的概率不低），而提到 5 会让证型档几乎产不出东西
#: （116 条带证型的医案分散在几十个证里）。`--min-support` 可调，
#: 产物里每条都带 `support`，引用时读者自己能判断够不够。
MIN_SUPPORT = 3
#: 剂量规律至少要几个抓到的剂量值才报中位数。3 个以下的"中位数"没有意义。
MIN_DOSE_SAMPLES = 3

#: 从原文里抓"药名 + 数字 + 单位"。古籍用钱/两/分，现代用 g/克。
#: **中文数字也要认**（「三钱」「一两」）——古籍医案里绝大多数是中文数字，
#: 只认阿拉伯数字等于把叶天士/吴鞠通那 854 张方的剂量全漏掉。
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "半": 0.5}
_UNIT_TO_G = {
    # 清代一钱 ≈ 3.73g、一两 ≈ 37.3g、一分 ≈ 0.373g。取整到 3.7 / 37 / 0.37：
    # **这是换算不是测量**，古籍度量衡本身按朝代与地域浮动，给到小数点后两位
    # 是假精确。换算写在这一处，产物里只存克数。
    "钱": 3.7, "两": 37.0, "分": 0.37, "g": 1.0, "克": 1.0,
}
#: 数量与单位分开写。**没有"药名"这一段**——让正则去猜药名是 R35 踩的坑：
#: 懒惰量词 `([一-龥]{1,6}?)` 只会吃到最短前缀，抓出来的是「少」「用」「得」
#: 而不是药名，751 张有单位字的方里只抓到 69 张（9%）。改成锚在已知药名上
#: 之后见 `extract_doses`。
_QTY = r"\d+(?:\.\d+)?|[一二三四五六七八九十]{1,3}|半"
_UNIT = r"钱|两|分|g|克"
#: 一节剂量：「三钱」「12g」，或者「钱半」这种单位在前的写法（= 一钱半）。
_DOSE_ONE_RE = re.compile(rf"(?:({_QTY})\s*({_UNIT}))|(?:({_UNIT})半)")
#: 药名与剂量之间允许夹什么。**这里刻意不放任意汉字**：放开了
#: 「生石膏 防己(三钱)」会把防己的三钱记到生石膏头上。只允许空白/括号/标点，
#: 加最多两个炮制字——「(生五钱)」「(炙一两)」「(炒一两)」这类写法在
#: 《吴鞠通医案》里很常见，不放炮制字会漏掉。
_GAP_RE = re.compile(r"[\s(（,，、]{0,4}(?:[生炙炒煨制焙酒盐醋蜜净飞研煅]{1,2})?[\s(（]{0,2}")
#: 单味药一次用量的上限（克）。超过的一律丢：那不是药量，多半抓到了
#: 「水八杯」这类煎法或药材总重。上限不能定太低——「灶中黄土(四两)」= 148g
#: 是真的用量；古籍方里单味药到不了 500g。
MAX_PLAUSIBLE_DOSE_G = 500.0


def _to_number(raw: str) -> float | None:
    """「3」/「三」/「十二」/「半」→ 数。认不出返回 None，**不猜**。"""
    if not raw:
        return None
    if raw.replace(".", "", 1).isdigit():
        return float(raw)
    if len(raw) == 1:
        return float(_CN_NUM[raw]) if raw in _CN_NUM else None
    # 「十二」= 12、「二十」= 20、「十」= 10
    if raw[0] == "十":
        rest = _CN_NUM.get(raw[1:], 0) if len(raw) > 1 else 0
        return float(10 + (rest or 0))
    if len(raw) == 2 and raw[1] == "十":
        return float(_CN_NUM.get(raw[0], 0) * 10)
    if len(raw) == 3 and raw[1] == "十":
        return float(_CN_NUM.get(raw[0], 0) * 10 + _CN_NUM.get(raw[2], 0))
    return None


def herb_spellings(case: dict) -> dict[str, str]:
    """{原文写法: 归一名}。归一名自己也当一种写法放进去——原文里两种都可能出现。

    剂量抽取要锚在**这张方真的开了的药名**上。反过来（先让正则猜药名再查表）
    抓不到，原因写在 `_QTY` 那组常量的注释里。
    """
    out: dict[str, str] = {}
    for raw in (case.get("herbs") or []):
        spelling = (raw or "").strip()
        if not spelling:
            continue
        name = normalize_herb(spelling)
        if not name:
            continue
        out.setdefault(spelling, name)
        out.setdefault(name, name)
    return out


def _parse_dose_at(text: str, pos: int) -> float | None:
    """从 `pos` 处解析剂量串，返回克数；认不出返回 None，**不猜**。

    「一两二钱」要累加成 44.4g 而不是 37g，所以最多续读一节；而且续读必须
    紧挨着（中间有空白或标点就停）——「二钱 三钱」是两味药各自的量，
    加到同一味头上就错了。
    """
    total = 0.0
    seen = 0
    while seen < 2:
        m = _DOSE_ONE_RE.match(text, pos)
        if not m:
            break
        num_raw, unit, unit_half = m.group(1), m.group(2), m.group(3)
        if unit_half:
            grams = 1.5 * _UNIT_TO_G[unit_half]   # 「钱半」= 一钱半
        else:
            n = _to_number(num_raw)
            if n is None:
                break
            grams = n * _UNIT_TO_G[unit]
        total += grams
        pos = m.end()
        seen += 1
    return round(total, 2) if seen else None


def extract_doses(raw: str, herbs: dict[str, str]) -> dict[str, list[float]]:
    """从一段原文里抓 {归一药名: [克数…]}。`herbs` 是 `herb_spellings()` 的产物。

    只认这张方真的开了的药：原文里还有病史叙述、「服三剂」「日三次」这类数字，
    不锚在药名上会把剂数当药量。

    同一个数字不重复计入同一味药：「姜半夏(五钱)」里「半夏」也能对上，
    归一后是同一味，只能算一次。
    """
    text = raw or ""
    out: dict[str, list[float]] = collections.defaultdict(list)
    counted: set[tuple[str, int]] = set()
    # 长写法优先，让「姜半夏」先落位，短写法再补。
    for spelling in sorted(herbs, key=lambda s: (-len(s), s)):
        name = herbs[spelling]
        cursor = 0
        while True:
            idx = text.find(spelling, cursor)
            if idx < 0:
                break
            cursor = idx + len(spelling)
            pos = _GAP_RE.match(text, cursor).end()
            grams = _parse_dose_at(text, pos)
            if grams is None:
                continue
            key = (name, pos)
            if key in counted:
                continue
            counted.add(key)
            if 0 < grams <= MAX_PLAUSIBLE_DOSE_G:
                out[name].append(grams)
    return dict(out)


def _has_incompatible(herbs: list[str]) -> bool:
    """这组药里有没有十八反十九畏的一对。**判据整个来自 safety_output**。"""
    present = {normalize_for_incompat(h) for h in herbs}
    present.discard("")
    return any(pair <= present for pair in INCOMPATIBLE_PAIRS)


def _pattern_id(kind: str, physician: str, group_value: str, herbs: list[str]) -> str:
    """确定性 id：同一份语料重跑必须得到同一个 id（产物要进版本控制，
    id 变了整份文件的 diff 就没法看）。所以是内容哈希，不是自增序号。"""
    key = "|".join([kind, physician, group_value, *sorted(herbs)])
    return f"{kind}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:10]}"


def _normalized_herbs(case: dict) -> list[str]:
    """一张方的药名，归一 + 去重保序。**归一走 `core.herbs.normalize_herb`**
    ——「炙黄芪」和「黄芪」算同一味，不然高频统计会把同一味药拆成好几条。"""
    out: list[str] = []
    for raw in (case.get("herbs") or []):
        name = normalize_herb(raw)
        if name and name not in out:
            out.append(name)
    return out


def _groups(cases: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    """分组：{(group_by, physician, group_value): [医案…]}。两档都产出，见模块文档。"""
    out: dict[tuple[str, str, str], list[dict]] = collections.defaultdict(list)
    for c in cases:
        pid = c.get("physician")
        if not pid:
            continue
        out[("physician", pid, "")].append(c)
        syn = (c.get("syndrome") or "").strip()
        if syn:
            out[("physician_syndrome", pid, syn)].append(c)
    return dict(out)


def mine_herb_patterns(group_key, cases, *, min_support: int) -> list[PrescribingPattern]:
    """高频药。"""
    group_by, pid, group_value = group_key
    name = physicians_all(PHYSICIANS).get(pid, {}).get("name") or pid
    counts: dict[str, list[str]] = collections.defaultdict(list)
    # 分母只数"这一组里真的抄了方的诊次"。用 len(cases) 当分母会把没抄方的
    # 诊次（1075 诊次里 324 条没有 herbs）算进去，百分比被压低约 1/3——
    # **一个分母说错的百分比比没有百分比更坏**，它看起来是可核的。
    n_with_herbs = sum(1 for c in cases if _normalized_herbs(c))
    for c in cases:
        for h in _normalized_herbs(c):
            counts[h].append(c["case_id"])
    out = []
    for herb, ids in sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(ids) < min_support:
            continue
        out.append(PrescribingPattern(
            pattern_id=_pattern_id("herb", pid, group_value, [herb]),
            kind="herb", physician=pid, physician_name=name,
            group_by=group_by, group_value=group_value,
            herbs=[herb], support=len(ids), case_ids=sorted(ids),
            note=f"在这一组有方的 {n_with_herbs} 诊次里出现 {len(ids)} 次"
                 f"（{len(ids) / max(1, n_with_herbs):.0%}；这一组共 {len(cases)} 诊次，"
                 f"其余没抄方）",
            has_incompatible_pair=False,   # 单味药谈不上配伍
        ))
    return out


def mine_pair_patterns(group_key, cases, *, min_support: int) -> list[PrescribingPattern]:
    """常用药对。两两共现，**不做三味以上的组合**——组合数爆炸而 support 必然更低，
    在 751 张方的语料上挖不出可信的三味组合（这是数据量的限制，不是没想到）。"""
    group_by, pid, group_value = group_key
    name = physicians_all(PHYSICIANS).get(pid, {}).get("name") or pid
    counts: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for c in cases:
        hs = sorted(_normalized_herbs(c))
        for i, a in enumerate(hs):
            for b in hs[i + 1:]:
                counts[(a, b)].append(c["case_id"])
    out = []
    for (a, b), ids in sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(ids) < min_support:
            continue
        out.append(PrescribingPattern(
            pattern_id=_pattern_id("herb_pair", pid, group_value, [a, b]),
            kind="herb_pair", physician=pid, physician_name=name,
            group_by=group_by, group_value=group_value,
            herbs=[a, b], support=len(ids), case_ids=sorted(ids),
            note=f"同方共现 {len(ids)} 次",
            has_incompatible_pair=_has_incompatible([a, b]),
        ))
    return out


def mine_dose_patterns(group_key, cases, *, min_support: int) -> list[PrescribingPattern]:
    """剂量习惯。**从 `raw` 原文抓**，`cases.json` 的 `herbs` 没有结构化剂量。

    报中位数而不是均值：古籍医案里偶有「生石膏二两」这种大剂量，
    均值会被单条拉走，中位数不会。同时报 min/max，让读者自己看离散程度。

    **已知局限（不修，如实记）**：`raw` 是粗段，一段里常含同一病人的多次复诊，
    还有丸散方——「姜半夏(十两)」「广皮(五两)」是制丸的一料总量而不是一次用量，
    抽出来就是 370g / 185g。这两个数是原文里真有的，删掉等于篡改语料；
    中位数不受它们影响，而 `dose_max_g` 会露出来，所以**min/max 必须一起报**，
    只报中位数就把这个局限藏起来了。
    """
    group_by, pid, group_value = group_key
    name = physicians_all(PHYSICIANS).get(pid, {}).get("name") or pid
    doses: dict[str, list[float]] = collections.defaultdict(list)
    ids: dict[str, list[str]] = collections.defaultdict(list)
    for c in cases:
        spellings = herb_spellings(c)
        if not spellings:
            continue
        got = extract_doses(c.get("raw") or "", spellings)
        for herb, vals in got.items():
            doses[herb].extend(vals)
            ids[herb].append(c["case_id"])
    out = []
    for herb, vals in sorted(doses.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        cids = sorted(set(ids[herb]))
        if len(vals) < MIN_DOSE_SAMPLES or len(cids) < min_support:
            continue
        out.append(PrescribingPattern(
            pattern_id=_pattern_id("dose", pid, group_value, [herb]),
            kind="dose", physician=pid, physician_name=name,
            group_by=group_by, group_value=group_value,
            herbs=[herb], support=len(cids), case_ids=cids,
            dose_median_g=round(statistics.median(vals), 2),
            dose_min_g=min(vals), dose_max_g=max(vals),
            note=f"从 {len(cids)} 张方的原文里抓到 {len(vals)} 个剂量值"
                 "（钱/两/分按清代度量衡折成克，见模块常量 _UNIT_TO_G）",
            has_incompatible_pair=False,
        ))
    return out


def mine_modification_patterns(cases: list[dict], *, min_support: int
                               ) -> list[PrescribingPattern]:
    """加减习惯：复诊序列里前后两诊的药物差集。

    **按医家分组，不按证型**：复诊序列本来就少（154 组），再按证型切就没有 support 了。
    同一味药"被加了 N 次"才算一条规律；只被加过 1 次的是这一例的个别处置。
    """
    by_group: dict[str, list[dict]] = collections.defaultdict(list)
    for c in cases:
        gid = c.get("case_group_id")
        if gid:
            by_group[gid].append(c)
    added: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    removed: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for visits in by_group.values():
        if len(visits) < 2:
            continue
        visits = sorted(visits, key=lambda c: c.get("visit_index") or 0)
        for prev, cur in zip(visits, visits[1:]):
            pid = cur.get("physician")
            if not pid:
                continue
            a, b = set(_normalized_herbs(prev)), set(_normalized_herbs(cur))
            if not a or not b:
                continue
            for herb in sorted(b - a):
                added[(pid, herb)].append(cur["case_id"])
            for herb in sorted(a - b):
                removed[(pid, herb)].append(cur["case_id"])
    out = []
    for label, table in (("复诊加", added), ("复诊去", removed)):
        for (pid, herb), ids in sorted(table.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(ids) < min_support:
                continue
            name = physicians_all(PHYSICIANS).get(pid, {}).get("name") or pid
            out.append(PrescribingPattern(
                pattern_id=_pattern_id("modification", pid, label, [herb]),
                kind="modification", physician=pid, physician_name=name,
                group_by="physician", group_value="",
                herbs=[herb], support=len(ids), case_ids=sorted(ids),
                note=f"{label}：{len(ids)} 次复诊里{label[-1]}了这味药",
                has_incompatible_pair=False,
            ))
    return out


def mine(cases: list[dict], *, min_support: int = MIN_SUPPORT,
         include_out_of_scope: bool = False) -> tuple[list[PrescribingPattern], dict]:
    """全部四类。返回 `(规律列表, 统计)`。**确定性**：同一份输入必得同一份输出，
    顺序也相同（每一步都排过序）——产物要进版本控制，顺序不定的话 diff 没法看。

    定位外医案（`out_of_scope`，李可肿瘤医案 57 例 + 王云启治癌验案 77 例）
    **默认不挖**，跟 `export_sft.filter_out_of_scope` 同一个取舍：本项目的证候表、
    检索语料、评测主诉全在脾胃门，肿瘤科的用药规律混进知识块会让模型照着
    开出评测覆盖不到的方。判断本身只有一个来源——`data/local_corpora/MANIFEST.json`
    的人工声明写进每条记录的 `out_of_scope` 标记，这里和 export_sft 都只读标记、
    不各自判一次（第 31 条）。排除了几条要报出来，不能静默少挖。
    """
    n_flagged = sum(1 for c in cases if c.get("out_of_scope"))
    if not include_out_of_scope:
        cases = [c for c in cases if not c.get("out_of_scope")]
    patterns: list[PrescribingPattern] = []
    groups = _groups(cases)
    for key in sorted(groups):
        rows = groups[key]
        patterns += mine_herb_patterns(key, rows, min_support=min_support)
        patterns += mine_pair_patterns(key, rows, min_support=min_support)
        patterns += mine_dose_patterns(key, rows, min_support=min_support)
    patterns += mine_modification_patterns(cases, min_support=min_support)
    # 同一个 pattern_id 只留第一条：两档分组可能算出同一条（医家档与证型档
    # 的 group_value 不同，id 里含它，所以实际不会撞；这一步是兜底）。
    seen: set[str] = set()
    deduped = []
    for p in patterns:
        if p.pattern_id in seen:
            continue
        seen.add(p.pattern_id)
        deduped.append(p)
    kinds = collections.Counter(p.kind for p in deduped)
    stats = {
        "n_cases": len(cases),
        "n_out_of_scope_flagged": n_flagged,
        "out_of_scope_included": bool(include_out_of_scope),
        "n_cases_with_herbs": sum(1 for c in cases if c.get("herbs")),
        "n_cases_with_syndrome": sum(1 for c in cases if (c.get("syndrome") or "").strip()),
        "n_patterns": len(deduped),
        "by_kind": dict(kinds),
        "by_group_by": dict(collections.Counter(p.group_by for p in deduped)),
        "n_with_incompatible_pair": sum(1 for p in deduped if p.has_incompatible_pair),
        "min_support": min_support,
        "max_support": max((p.support for p in deduped), default=0),
    }
    return deduped, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--min-support", type=int, default=MIN_SUPPORT,
                    help=f"一条规律至少几张方支持（默认 {MIN_SUPPORT}）")
    ap.add_argument("--dry-run", action="store_true", help="只打统计，不落盘")
    ap.add_argument("--include-out-of-scope", action="store_true",
                    help="把定位外（out_of_scope）的医案也挖进去。默认排除，"
                         "理由见 mine() 的文档")
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        print(f"✗ 没有 {args.cases_path}——这一步要 cases.json（医案抽取的产物）。"
              "先跑 `python -m offline.extract_cases`。", file=sys.stderr)
        return 2
    if args.min_support < 1:
        print(f"✗ --min-support={args.min_support} 必须 ≥ 1", file=sys.stderr)
        return 2

    cases = json.loads(args.cases_path.read_text(encoding="utf-8"))
    patterns, stats = mine(cases, min_support=args.min_support,
                           include_out_of_scope=args.include_out_of_scope)

    print(f"读入 {stats['n_cases']} 诊次（有药 {stats['n_cases_with_herbs']}，"
          f"有证型 {stats['n_cases_with_syndrome']}）")
    if stats["n_out_of_scope_flagged"]:
        verb = "**也挖进去了**" if stats["out_of_scope_included"] else "已排除"
        print(f"  定位外（out_of_scope）医案 {stats['n_out_of_scope_flagged']} 条：{verb}")
    print(f"挖出 {stats['n_patterns']} 条规律（min_support={stats['min_support']}）")
    print(f"  按类型：{stats['by_kind']}")
    print(f"  按分组：{stats['by_group_by']}")
    print(f"  support 上限：{stats['max_support']}")
    print(f"  涉及十八反十九畏的：{stats['n_with_incompatible_pair']} 条"
          "（**标出来不删掉**，古籍里真有这种配伍）")
    if stats["n_with_incompatible_pair"]:
        from core.safety_output import INCOMPATIBLE_TRAINING_NOTE

        print(f"  {INCOMPATIBLE_TRAINING_NOTE}")

    if args.dry_run:
        print("（--dry-run，没有落盘）")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "\n".join(p.model_dump_json() for p in patterns) + "\n", encoding="utf-8")
    print(f"已写出 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
