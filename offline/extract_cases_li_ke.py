"""李可医案抽取器：切案规则是本地的，抽字段仍然走 `offline/extract_cases.py`
那一套 LLM schema（`SegmentPatients` → `CaseRecord`）。

    python -m offline.extract_cases_li_ke --dry-run        # 只切案、不调 LLM
    python -m offline.extract_cases_li_ke --limit 5        # 切完前 5 案再抽

## 为什么单独写一个而不是扩 extract_cases.py

`extract_cases.py` 读的是 `data/{physician}/*.json` 那种**已经切好的粗段**
（`offline/split_cases.py` 按《临证指南医案》的门类体例切出来的）。李可这份是
一整个 `.txt`，体例完全不同：编号标题（`1.1 脑瘤头痛`）分案、目录页混在正文
最前面、三四五章是论述不是医案。

**只有切案规则不同，抽取那一步一个字都不改**——同一套 prompt、同一个
`SegmentPatients` schema、同一个 `expand_segment`。切案规则单独一个函数
（`split_cases`），纯本地、零 LLM，所以能用合成文本测透。

## 三件在切案边界就算好的事

**一、`scope` 枚举**（`oncology` / `spleen_stomach` / `other`）。判定复用
`offline/assess_case_scope.py` 的 `ONCOLOGY_HINTS` 词表和 `core/elements.py`
的脾胃词，不另写一份——CLAUDE.md 第 31 条。

旧的布尔 `out_of_scope` 保留一轮（`scope != "spleen_stomach"` 时为真），
下游还在读它；**新代码一律读 `scope`**：布尔只能回答"在不在范围内"，
而真正要区分的是"是肿瘤案"和"是别的科"——前者是这批语料的主体（李可这份
整本都是肿瘤案），后者才是零星的。

**二、`incompatible_pairs`：算，但不排除。** R8 实测这份语料 556 段里有
75 段海藻甘草同用——那是李可的用药特征，不是数据错误。把它们排除掉等于
把这位医家最有辨识度的部分删掉。所以这里只标出**具体是哪几对**
（`has_incompatible_pair` 那个布尔回答不了这个问题），排不排除交给
`export_sft --exclude-incompatible`（默认不排）。

**三、`visit_index`：李可这份体例里一案一诊。** 复诊信息写在正文叙述里
（"服药 75 日赴京复查"），不是独立的"二诊""三诊"段落——规则切不出来，
所以一律 0，**不猜**。真有多诊的案子由 LLM 抽取那一步的 `SegmentPatients`
去判（它看的是全文），跟《临证指南医案》那一路完全一致。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core.elements import LOCATIONS
from core.safety_output import check_incompatible
from offline.assess_case_scope import ONCOLOGY_HINTS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "local_corpora" / "李可医案.txt"
DEFAULT_OUT = ROOT / "data" / "cases_li_ke.json"
PHYSICIAN_ID = "li_ke"

# 编号标题：`1.1 脑瘤头痛` / `2.63 皮癌` / `3.2.1 痰毒热化型，攻癌夺命汤`。
# 三级编号（3.2.1）是论述小节，不是医案——但**不靠编号层数判**，靠下面
# `looks_like_a_case` 的内容判据：层数是体例的偶然，内容才是本质。
_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)+)\s+(\S.*?)\s*$")

# 剂量：`生黄芪 120g` / `麻黄 (另) 15g` / `蜂蜜 150ml`。医案必有方，论述多半没有。
_DOSE_RE = re.compile(r"\d+\s*(?:g|ml|钱|两|枚|条|只)\b")

# 病人标记：`张某，女，25 岁` / `两渡煤矿绞车工蔡某，49 岁`。
# **不够用**：实测 `1.8 食管癌` 写的是"李可老母，年六旬"——既没有"某"也没有
# "岁"。所以下面的 `looks_like_a_case` 把它跟正文长度做成"或"，不是硬要求。
_PATIENT_RE = re.compile(r"(?:某|氏)\s*[，,、]|[\d]+\s*岁|老母|患者|年[六七八九]?旬")

# 医案正文的长度下限。实测这份语料里论述小节是 0/96/97/115 字，而最短的医案
# 正文（`2.35 胃癌全切术`）是 53 字——那一条是被下面的标题规则切坏的，修好之后
# 最短的真医案在 300 字以上。200 取在两者之间，**不是拍的**：调到 120 会把
# `3.5 伏邪入里当外透`（115 字）收进来，调到 400 会漏掉真医案。
_MIN_CASE_CHARS = 200

# 标题长度上限。真标题是病名（`脑瘤头痛`/`胃小弯癌`），最长的也就十来个字；
# 而正文里"2.20 头三七 200g，血琥珀、高丽参…"这种**药方续行**恰好也以
# `数字.数字 ` 开头，会被标题正则吃掉、把一个案子劈成两半。实测劈坏 3 处。
_MAX_TITLE_CHARS = 20

# 目录页：正文最前面那几段，一行里塞着十几个 `N.M 标题…页码`。判据是
# **一行里出现多个编号标题**——正文的标题一行只有一个。
_TOC_RE = re.compile(r"(?:\d+\.\d+\s+\S.*?){3,}")

# 脾胃门词表复用 core/elements.py 的病位词，不另写一份。
_SPLEEN_STOMACH = tuple(w for w in LOCATIONS if w in {"脾", "胃", "肠", "中焦"})


def _is_real_heading(title: str) -> bool:
    """`2.20 肺癌 2` 是标题，`2.20 头三七 200g，血琥珀…` 是药方续行。

    判据是标题**短且不含剂量**。用长度而不是"有没有药名"：药名表匹配会把
    `1.8 食管癌` 这种带药材同名字的病名误判掉，而长度这条对这份语料是干净的
    （真标题最长十来个字，续行都在 40 字以上）。
    """
    return len(title) <= _MAX_TITLE_CHARS and not _DOSE_RE.search(title)


def looks_like_a_case(body: str) -> bool:
    """这一节是医案还是论述。

    判据是**内容**不是编号层数：三级编号（`3.2.1 痰毒热化型，攻癌夺命汤`）
    在李可这份里是论述小节，但换一本书可能就是医案编号。要有方（剂量）
    **并且**有病人（某/岁）——只有方的是"常用方"罗列，只有病人的是医话。
    """
    if not _DOSE_RE.search(body):
        return False
    return bool(_PATIENT_RE.search(body)) or len(body) >= _MIN_CASE_CHARS


def classify_scope(text: str) -> str:
    """`oncology` / `spleen_stomach` / `other`。

    **肿瘤优先**：一个案子既提到胃又提到癌（"胃小弯癌"），它首先是肿瘤案——
    倒过来判会把整本肿瘤案里带"胃"字的那些统统标成脾胃门，而 demo 的定位
    是脾胃门，那批案子会以"在范围内"的身份混进主路径。
    """
    if any(w in text for w in ONCOLOGY_HINTS):
        return "oncology"
    if any(w in text for w in _SPLEEN_STOMACH):
        return "spleen_stomach"
    return "other"


def _strip_toc(lines: list[str]) -> list[str]:
    """把开头的目录页丢掉。只丢**开头**连续的那一段：正文里也可能有一行恰好
    像目录（比如列举"1.1、1.2 两案合参"），从中间丢会把正文挖掉一块。"""
    out = list(lines)
    while out and (not out[0].strip() or _TOC_RE.search(out[0])):
        out.pop(0)
    return out


def split_cases(text: str) -> list[dict]:
    """按编号标题切案。返回 `[{case_no, title, body, ...}]`，纯本地零 LLM。

    切法：遇到编号标题就开一节，节的内容是到下一个编号标题之前的全部正文；
    然后用 `looks_like_a_case` 把论述节滤掉。**滤掉的不是静默丢弃**——
    `main()` 会把两边的条数都打出来，"556 段切出 N 案、滤掉 M 节论述"
    这两个数一起看才知道规则合不合适。
    """
    lines = _strip_toc(text.splitlines())
    sections: list[dict] = []
    current: dict | None = None
    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m and _is_real_heading(m.group(2)) and not _TOC_RE.search(line):
            if current:
                sections.append(current)
            current = {"case_no": m.group(1), "title": m.group(2), "lines": []}
            continue
        if current is not None:
            current["lines"].append(line)
    if current:
        sections.append(current)

    cases = []
    dropped: list[dict] = []
    for sec in sections:
        body = "\n".join(sec["lines"]).strip()
        if not looks_like_a_case(body):
            # **不是静默丢弃**：记下来，main() 会逐条打出来。规则切错了只有
            # 把被丢掉的东西摊开才看得见——"切出 57 案"这个数单独看永远是对的。
            dropped.append({"case_no": sec["case_no"], "title": sec["title"],
                            "n_chars": len(body)})
            continue
        raw = f"{sec['case_no']} {sec['title']}\n{body}"
        pairs = check_incompatible(_herb_words(body))
        cases.append({
            "case_id": f"{PHYSICIAN_ID}-{sec['case_no'].replace('.', '-')}",
            "physician": PHYSICIAN_ID,
            "case_no": sec["case_no"],
            "title": sec["title"],
            "raw_excerpt": raw,
            # 一案一诊：复诊写在叙述里，规则切不出来，不猜（见模块文档字符串）。
            "visit_index": 0,
            "case_group_id": f"{PHYSICIAN_ID}-{sec['case_no'].replace('.', '-')}",
            "prev_case_id": None,
            "scope": classify_scope(raw),
            # 旧字段保留一轮：下游还在读它。新代码读 scope。
            "out_of_scope": classify_scope(raw) != "spleen_stomach",
            "incompatible_pairs": [f"{a}-{b}" for a, b in pairs],
            "has_incompatible_pair": bool(pairs),
        })
    split_cases.last_dropped = dropped  # type: ignore[attr-defined]
    return cases


def _herb_words(body: str) -> list[str]:
    """从正文里抠出可能的药名，喂给 `check_incompatible`。

    切法故意粗：按标点和剂量切，留下的中文片段当候选药名。
    `check_incompatible` 自己会在十八反/十九畏表里查，查不到的片段是噪声、
    不会误报——**宁可多喂几个词，不要漏掉一对反药**。
    """
    cleaned = _DOSE_RE.sub("，", body)
    parts = re.split(r"[，,、。；;：:()（）\s]+", cleaned)
    return [p for p in parts if p and re.fullmatch(r"[一-鿿]{2,4}", p)]


def summarize(cases: list[dict]) -> dict:
    """给 --dry-run 报的那几个数。每个都要能跟语料对上，不是"大概"。"""
    from collections import Counter
    return {
        "n_cases": len(cases),
        "dropped": list(getattr(split_cases, "last_dropped", [])),
        "by_scope": dict(Counter(c["scope"] for c in cases)),
        "n_incompatible": sum(1 for c in cases if c["has_incompatible_pair"]),
        "incompatible_pairs": dict(Counter(
            p for c in cases for p in c["incompatible_pairs"])),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 案")
    ap.add_argument("--dry-run", action="store_true",
                    help="只切案 + 报数，不调 LLM（零成本，用来核切案规则）")
    args = ap.parse_args(argv)

    if not args.input.exists():
        print(f"找不到语料：{args.input}\n"
              f"这份语料版权受限、不随仓库分发，见 data/SOURCES.md。")
        return 2

    text = args.input.read_text(encoding="utf-8")
    cases = split_cases(text)
    if args.limit > 0:
        cases = cases[:args.limit]
    stats = summarize(cases)
    print(f"切出 {stats['n_cases']} 案")
    print(f"  按范围：{stats['by_scope']}")
    print(f"  含反药配伍：{stats['n_incompatible']} 案 —— **算但不排除**，"
          f"那是李可的用药特征，不是数据错误")
    for pair, n in sorted(stats["incompatible_pairs"].items(), key=lambda kv: -kv[1]):
        print(f"    {pair}：{n} 案")
    if stats["dropped"]:
        print(f"\n判为论述、没当医案切的 {len(stats['dropped'])} 节"
              f"（**列出来给人看，不是静默丢弃**）：")
        for d in stats["dropped"]:
            print(f"    {d['case_no']} {d['title']}（正文 {d['n_chars']} 字）")

    if args.dry_run:
        print("\n--dry-run：没有调用 LLM，也没有写文件。")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {args.out}（切案结果；字段抽取见下一步）")
    print("下一步（要真实 LLM）：python -m offline.extract_cases --from-json", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
