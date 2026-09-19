"""R65 一：本体外的药名到底是什么——扩药材数之前先把它分类。

## 为什么需要这个脚本

R64 合并时立了一条"不新建药条"，收益因此受限：四个开源源里**852 个药名在
我们 1232 味之外**，它们带着性味/归经/功效/用量，全部被丢掉了。

下一步是"扩别名"还是"扩药材数"，取决于这 852 个是什么：
  - 如果多是**异写/古名** → 扩 `HERB_ALIASES` 就够，不用动药材数；
  - 如果多是**本体真的没有的药** → 只能扩药材数。

## 这个脚本的主要结论是一个"不能这么做"

**按字符串相似度自动分类是不可靠的，这一点本身就是结论。** 两次尝试都产出了
危险的假阳性：
  - 编辑距离 1：`三七叶 ~ 三七`、`丝瓜子 ~ 丝瓜` ——同株不同药用部位，
    性味功效不同，归成一条等于开错药（跟南北五味子同一类错误）；
  - 加了部位后缀守卫之后仍有：`人尿 ~ 人参`、`冰糖 ~ 冰片`、`升药 ~ 升麻`
    ——中药名差一个字通常是**另一味药**，不是另一种写法。

所以脚本**只做分桶和举例，不产出可直接合并的别名表**。哪些是真异写必须人工
过（R59 那一轮就是这么做的），这个脚本的作用是把 852 个缩小到值得人工看的那些。

跑：`python -m scripts.diagnose_expansion_candidates --repos <baodian> <nihaixia>`
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core.herbs import normalize_herb
from core.ontology import get_ontology

#: 药用部位与加工品后缀。词根是另一味药、后缀全在这张表里的，**是另一味药**。
#: 这张表回答的是"这个名字是不是某味药的某个部位"，跟 `HERB_ALIASES`
#: （"这个写法归到哪味药"）不是同一个问题，所以另立一张。
PART_SUFFIX = (
    "叶", "花", "根", "茎", "子", "仁", "皮", "藤", "络", "壳", "油", "汁",
    "霜", "炭", "须", "芽", "蒂", "核", "衣", "梗", "刺", "枝", "头", "尾",
    "肉", "膏", "粉", "灰", "水", "苗", "蕊", "角",
)

N_EXAMPLES = 10


def _load_js_const(path: Path, name: str):
    txt = path.read_text(encoding="utf-8")
    m = re.search(rf"const\s+{re.escape(name)}\s*=\s*", txt)
    if not m:
        return None
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
    return None


_HERBS_ROW_RE = re.compile(r"^\s*\['([^']*)'")


def collect_source_names(repos: list[Path]) -> set[str]:
    """四个源里出现过的药名。读不到的源跳过并不报错——这个脚本是探针，
    少一个源只是少一部分候选，不该因此整个跑不了。"""
    names: set[str] = set()
    for repo in repos:
        herbs_json = repo / "assets" / "data" / "herbs.json"
        if herbs_json.exists():
            data = json.loads(herbs_json.read_text(encoding="utf-8"))
            names |= {(r.get("name") or "").strip() for r in data.get("herbs", [])}
        tcm = repo / "tcm-data.js"
        if tcm.exists():
            for line in tcm.read_text(encoding="utf-8").splitlines():
                m = _HERBS_ROW_RE.match(line)
                if m:
                    names.add(m.group(1).strip())
        for fname, const in (("jianbie_extra.js", "JBI_EXTRA"), ("kuozhan.js", "KZ")):
            p = repo / fname
            if not p.exists():
                continue
            try:
                d = _load_js_const(p, const)
            except json.JSONDecodeError:
                # `kuozhan.js` 的键没加引号，严格 JSON 读不了，而对别人仓库里的
                # 文件跑 eval 不做。这个脚本只需要**键名**（药名），
                # 所以退回逐行扫顶层键——拿不到值不影响分类。
                d = None
            if isinstance(d, dict):
                names |= {k.strip() for k in d}
            else:
                names |= _scan_top_level_keys(p)
    return {n for n in names if n}


#: 顶层键（缩进两格以内、后面跟一个 `{`）。只用来取药名，不取值。
_TOP_KEY_RE = re.compile(r'^\s{0,2}"?([^":,{}\s]{1,20})"?\s*:\s*\{')


def _scan_top_level_keys(path: Path) -> set[str]:
    return {m.group(1) for line in path.read_text(encoding="utf-8").splitlines()
            if (m := _TOP_KEY_RE.match(line))}


def classify(names: set[str], ont) -> dict:
    canon = sorted(ont.herbs)
    outside = sorted(n for n in names if (normalize_herb(n) or n) not in ont.herbs)
    buckets: dict[str, list] = {"fragment": [], "part_of": [], "near_miss": [], "new": []}
    for n in outside:
        if re.search(r"[，、,]", n):
            buckets["fragment"].append(n)
            continue
        root = next((c for c in canon
                     if len(c) >= 2 and n.startswith(c) and n[len(c):]
                     and all(ch in PART_SUFFIX for ch in n[len(c):])), None)
        if root:
            buckets["part_of"].append((n, root))
            continue
        near = [c for c in canon
                if len(c) == len(n) and sum(1 for x, y in zip(n, c) if x != y) == 1]
        if near:
            buckets["near_miss"].append((n, near[:2]))
            continue
        buckets["new"].append(n)
    return {"n_source_names": len(names), "n_outside": len(outside), "buckets": buckets}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", nargs="+", required=True)
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    ont = get_ontology()
    if not ont.available:
        print("本体层数据文件不在，无法判断哪些药在本体外。")
        return 2
    names = collect_source_names([Path(r).expanduser().resolve() for r in args.repos])
    if not names:
        print("四个源里一个药名都没读到——检查 --repos 路径。")
        return 2
    res = classify(names, ont)
    b = res["buckets"]
    n_out = res["n_outside"]
    print(f"本体 {len(ont.herbs)} 味；源里 {res['n_source_names']} 个药名，"
          f"**其中 {n_out} 个在本体之外**\n")
    rows = [
        ("(c) 多药串解析碎片（两边都不该收）", len(b["fragment"])),
        ("(b1) 同株不同药用部位（是另一味药）", len(b["part_of"])),
        ("(?) 与某个正名差一个字（**不可自动归并**）", len(b["near_miss"])),
        ("(b2) 本体里完全没有的药", len(b["new"])),
    ]
    for label, n in rows:
        print(f"  {label:<44} {n:4d} 味 {n / n_out * 100:4.1f}%")

    print(f"\n差一个字那一桶的前 {N_EXAMPLES} 个——**看一眼就知道为什么不能自动合并**：")
    for n, near in b["near_miss"][:N_EXAMPLES]:
        print(f"    {n} ~ {near}")
    print(f"\n部位药前 {N_EXAMPLES} 个（归并会把不同药效并成一条）：")
    for n, root in b["part_of"][:N_EXAMPLES]:
        print(f"    {n} ← 词根 {root}")
    print(f"\n本体里完全没有的前 {N_EXAMPLES} 个（要收只能扩药材数）：")
    print("    " + "、".join(b["new"][:N_EXAMPLES]))
    print(f"\n多药串（全部 {len(b['fragment'])} 个）：" + "、".join(b["fragment"]))

    auto_safe = 0
    print(f"\n**可以自动合并成别名的：{auto_safe} 个。** 差一个字那一桶里既有真异写"
          "（卤咸/卤碱→卤盐）也有完全不同的药（人尿/人参），判据不在字符串里，"
          "\n必须人工过——这个脚本的作用是把要人工看的从 852 缩到"
          f" {len(b['near_miss'])} 个。")
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"n_source_names": res["n_source_names"], "n_outside": n_out,
             "counts": {k: len(v) for k, v in b.items()},
             "near_miss_examples": [[n, list(c)] for n, c in b["near_miss"][:50]],
             "new_examples": b["new"][:50],
             "part_of_examples": [[n, c] for n, c in b["part_of"][:50]],
             "fragments": b["fragment"]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n写到 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
