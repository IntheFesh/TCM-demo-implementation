"""R18-D：从《脾胃论》抽「病机 → 治法」立论三元组。

跟药理层（S6/S7，offline/extract_reference_triples.py）**刻意不是同一条路**：

  - 那边是真实 LLM 抽取，产物落 `data/` 下、不进版本控制；
  - 这边是**确定性规则抽取**，产物落 `data/standard/rationale_pwl.jsonl`、
    要进版本控制。进版本控制的生成物必须任何人在任何机器上重跑都字字相同，
    LLM 做不到这件事。

规则抽取在这本书上够用，是因为《脾胃论》的处置句本身就规整：
「如脉缓，病怠惰嗜卧，四肢不收，或大便泄泻，此湿胜，从平胃散。」
——条件在前、断语在中、方在后，这不是我们迁就规则去挑句子，是李东垣的行文如此。
抽不出来的句子（长篇引《内经》的论述段）就是抽不出来，**不编**。

`source_span` 是逐字截的原句，落盘前 `verify_spans()` 核验它真的出现在原文里。
规则抽取不会编造 span，但会因为切句边界算错而截出一段原文里不存在的文字
（比如把两句之间的标点吃掉），这道核验拦的正是那个。

证素判断复用 `core.elements` 的 LOCATIONS/NATURES（CLAUDE.md 第 31 条：
「这个词是不是证素」这个问题在本项目里已经有一处实现，不另写字面表）。
治法词表是本模块新建的——它回答的是**另一个问题**：「这个字是不是李东垣
升降浮沉补泻那套治法用语」。合进 ELEMENTS 会让「湿」既是证素又是治法，
合进 SYNONYMS 会让证候归一化把「补」映射到某个门类去。

用法：
    python -m offline.extract_rationale_pwl            # 写 data/standard/rationale_pwl.jsonl
    python -m offline.extract_rationale_pwl --probe    # 只打印语料结构，不写文件
    python -m offline.extract_rationale_pwl --dry-run  # 抽但不落盘，打印前若干条
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from core.elements import LOCATIONS, NATURES
from core.schemas import RationaleRecord
from offline.pharmacology_sources import has_classic_dose

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT / "data" / "local_corpora" / "脾胃论.txt"
DEFAULT_OUTPUT = ROOT / "data" / "standard" / "rationale_pwl.jsonl"
BOOK = "脾胃论"

# 篇名判据：整行**只有汉字**（允许 OCR 拆出来的行内空格），长度 3~24。
# 判据刻意不写成「以论/验/法/方收尾」的白名单：这本书的小节名收尾字散得很开
# （「脏气法时升降浮沉补泻之图」「摄养」「远欲」「省言箴」「论饮酒过伤」），
# 白名单漏掉的那些小节会整段并到上一篇名下，而"整行没有标点"这条判据对它们
# 一视同仁——正文段落必定带「。」或「，」，短到不带标点的行只能是标题。
_MAX_CHAPTER_CHARS = 24
_CHAPTER_RE = re.compile(r"^[一-鿿]{3,%d}$" % _MAX_CHAPTER_CHARS)
# 卷次行（「脾胃论卷上」）不是篇名。它也是一整行纯汉字，不排掉的话会变成一篇，
# 把它后面到第一个真篇名之间的行吃进去。
_VOLUME_RE = re.compile(r"^脾胃论卷[上中下]$")

# 目录整块要先切掉，不能靠「同一个篇名在正文里还会出现第二次、只认最后一次」
# ——带行内空格的那四个篇名在正文里的写法跟目录里不一样，"最后一次"就落在
# 目录行上，于是整块目录（连同后面几十行推销电子书的广告）被当成那一篇的正文。
# 改成：从「Table of Contents」那一行起，连续的"像篇名的行"整段丢掉，
# 遇到第一行不像篇名的（广告行带全角逗号）为止。
_TOC_MARK = "Table of Contents"
# 句读：目录行一律没有，正文段和广告行一定有。
_PUNCT_MARK = "。，；：！？、"

# 校注编号「〔1〕」和行内夹注要从 source_span 里剔掉吗？不剔。span 的用处是
# 「让人能回原文核对这句话」，剔了就对不上原文了。但切句时要认得它不是句末。
_ANNOT_RE = re.compile(r"〔\d+\s*〕")
# 校注块以一长串连字符起头，整段都是整理者的话、不是李东垣的原文，不抽。
_ANNOT_BLOCK_RE = re.compile(r"^-{5,}$")

_SENT_END = "。；！？"

# 方名：以下面这些字收尾，且前面 2~7 个汉字。「四物汤中摘一味」这种要能只截到
# 「四物汤」，所以方名后面不能贴汉字之外的东西——用 re 的最短匹配加收尾锚定。
_FORMULA_RE = re.compile(r"([一-鿿]{1,7}(?:汤|散|丸|饮|膏|丹|煎))")
# 「从平胃散」「用清暑益气汤」：处置动词 + 方名。
# 第二个形式「四君子汤中去茯苓」「五苓散中加一二味」是方名 + 「中」——原文里
# 「在某方的基础上加减」就是这么写的，没有处置动词。只认动词形式会漏掉这一整类
# （实测漏 10 条），而它们恰恰是本书最有临床意义的加减条文。
# 不认光秃秃的方名（「五苓散治渴而小便不利」是在讲方的功用，不是给这一证用方）。
_USE_FORMULA_RE = re.compile(
    r"(?:(?:从|用|服|投)" + _FORMULA_RE.pattern + r"|" + _FORMULA_RE.pattern + r"中)"
)

# 加药/去药：句末的「加X」「去X」。X 是药名串（顿号分隔），不含标点。
_ADD_RE = re.compile(r"加([一-鿿]{2,4}(?:、[一-鿿]{2,4}){0,5})$")
_DROP_RE = re.compile(r"去([一-鿿]{2,4}(?:、[一-鿿]{2,4}){0,5})$")
# 「加」后面跟的不一定是药名，实测撞出三种：
#   「摘一二味加正药中」→ 正药中（是"加到哪里"，不是加什么）
#   「于甘草五分中加一分可也」→ 一分可也（是剂量）
#   「乃散而不收，可加芍药收之」→ 芍药收之（药名后面挂了个动词）
# 仓库里没有药名词表（HERB_ALIASES 只收别名、jieba_dict 只收症状证素），
# 所以判据只能是形状：不以虚词收尾、不以数词开头。
# **宁可漏掉几条也不要把"正药中"当成一味药**——这一层要进版本控制，
# 错条目会一直留在那儿。
_FUNCTION_TAIL = "之也矣焉耳中可者上下内外"
_NUMERAL_HEAD = "一二三四五六七八九十半数两"


def looks_like_herb_list(o: str) -> bool:
    if not o or o[-1] in _FUNCTION_TAIL or o[0] in _NUMERAL_HEAD:
        return False
    return all(2 <= len(tok) <= 4 for tok in o.split("、") if tok)

# 治法用语。这张表回答「这个字是不是李东垣那套治法动作」，跟 ELEMENTS 回答的
# 「这个词是不是证素」不是同一个问题，所以不并表（CLAUDE.md 第 31 条的例外条款：
# 走例外必须在代码里写清楚两个问题的区别）。
# 「升降浮沉补泻」六字出自本书「脏气法时升降浮沉补泻之图」，是李东垣立论的骨架；
# 「汗吐下和温清消」是八法里本书实际用到的那几个。
TREATMENTS: tuple[str, ...] = (
    "升阳", "升", "降", "浮", "沉", "补", "泻",
    "汗", "吐", "下", "和", "温", "清", "消", "滋", "收",
)
# 触发字**刻意不含「则」**：「脾病则下流乘肾」里的「下」是"下流"的下，不是治法的
# 下法。第一版含「则」，这一句就抽出了 (脾病)-[治法]->(下) 这条错的。
_TREAT_RE = re.compile(r"(?:当|宜|须)((?:%s))(?:之|阳|气)?" % "|".join(TREATMENTS))

# 禁忌：本书有「用药宜禁论」整整一篇，禁忌是它的主要内容，不是边角。
_FORBID_RE = re.compile(r"(勿|不可|不得|不宜|忌|禁)([一-鿿]{1,6})")

# 病机句：「A则B」，其中 B 要带证素词才算病机结论（「胃病」「脾虚」算，
# 「万化安」不算）。这道证素过滤是复用 core.elements，不是本模块自己的表。
_MECHANISM_RE = re.compile(r"^(.{2,30}?)则(.{2,30})$")
_ELEMENT_WORDS = tuple(LOCATIONS) + tuple(NATURES)
# 「病」「虚」「伤」这几个字单独出现也是病机结论的标志（「脾胃乃伤」里的「伤」）。
_MECHANISM_TAIL_WORDS = ("病", "虚", "实", "伤", "衰", "乱", "痛", "泄", "厥")

# 句子短于这个长度抽不出有意义的条件-处置对（「亦加之」「渴亦加之」）。
_MIN_SENT_CHARS = 6
# 长于这个长度的多半是引《内经》的整段论述，条件部分已经不可辨，抽出来的 s
# 会是一大段文字——那不是三元组，是把原文搬了一遍。
_MAX_SENT_CHARS = 120


def _clean_lines(text: str) -> list[str]:
    """去空行、去校注块。返回的每一项是原文里的一行（已 strip）。"""
    out: list[str] = []
    in_annot = False
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if _ANNOT_BLOCK_RE.match(ln):
            # 校注块一直延续到下一个篇名——整理者的话不抽。
            in_annot = True
            continue
        if in_annot:
            if _chapter_title(ln) is not None:
                in_annot = False
            else:
                continue
        out.append(ln)
    return out


def _chapter_body_lines(text: str) -> list[str]:
    return drop_front_matter(_clean_lines(text))


def _chapter_title(line: str) -> str | None:
    """行内空白抹掉再比：原书篇名有四个是跨行排的，转成 txt 后留下行内空格。

    两类"看起来像篇名"的行要排掉，都是实测撞出来的：
      - 药量行（「黄丹二钱定粉舶上硫黄陀僧已上各三钱轻粉少许」）整行无标点、
        长度也够短。剂量判据复用 pharmacology_sources.has_classic_dose，
        不另写正则（CLAUDE.md 第 31 条）。
      - 君臣佐使标注行（「白术君人参臣甘草佐芍药佐黄连使黄芪臣桑白皮佐」）
        同样无标点。判据是**行尾是角色字**且角色字出现三次以上：只数出现次数
        会把「君臣佐使法」这个真篇名一起误杀（它四个角色字全有），
        而标注行是「药名+角色字」交替、必定以角色字收尾。
    排掉之后方名（「补中益气汤」「枳术丸」）仍算篇名，这是**故意的**：
    《脾胃论》本身就是论与方交替编排，方名是真实的结构层级，
    三元组挂在「补中益气汤」名下比挂在上一篇论名下更接近原书。
    """
    squeezed = re.sub(r"\s+", "", line)
    if _VOLUME_RE.match(squeezed):
        return None
    if not _CHAPTER_RE.match(squeezed):
        return None
    if has_classic_dose(squeezed):
        return None
    if squeezed[-1] in "君臣佐使" and sum(squeezed.count(c) for c in "君臣佐使") >= 3:
        return None
    return squeezed


def drop_front_matter(lines: list[str]) -> list[str]:
    """切掉版权页 + 目录块 + 目录后面那几十行推销电子书的广告。

    锚点是「Table of Contents」那一行：它之前全是版权页和序，之后一直到
    **第一行带句读的行**为止都是目录本体。

    判据是"带句读"而不是"像篇名"：目录里混着几行 `_chapter_title` 认不出来的
    （「摄 养」只两个字、「《内经》仲景所说脾胃」带书名号），逐行要求像篇名的话
    目录会在这些行上提前结束，后面剩下的目录行全部变成假篇名——实测就是这样
    漏了 3 个（用药宜禁论/脾胃将理法/省言箴 各多出一份空篇）。
    目录行一律没有句读，正文段和广告行一定有。

    广告块本身不用单独认——它落在第一个正文篇名之前，`_chapter_spans` 不会
    把无主的行归给任何篇。
    """
    start = next((i for i, ln in enumerate(lines) if _TOC_MARK in ln), None)
    if start is None:
        # 没有目录标记的语料（测试用的小样本）整份都当正文，不静默返回空。
        return lines
    i = start + 1
    while i < len(lines) and not any(ch in lines[i] for ch in _PUNCT_MARK):
        i += 1
    return lines[i:]


def _chapter_spans(lines: list[str]) -> list[tuple[str, list[str]]]:
    """切成 [(篇名, 该篇正文行)]。"""
    starts: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        name = _chapter_title(ln)
        if name is not None:
            starts.append((i, name))
    spans: list[tuple[str, list[str]]] = []
    for k, (i, name) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        body = lines[i + 1 : end]
        if body:
            spans.append((name, body))
    return spans


def split_sentences(body: list[str]) -> list[str]:
    """按句末标点切句。分号也算句末：本书的条件-处置句大量用分号并列
    （「腹中痛者，加甘草、白芍药；腹痛兼发热，加黄芩」），不切分号的话
    一句里会有两条处置，抽出来的 o 是两条混在一起的。"""
    out: list[str] = []
    for ln in body:
        buf = ""
        for ch in ln:
            buf += ch
            if ch in _SENT_END:
                s = buf.strip(_SENT_END + "，、 ").strip()
                if s:
                    out.append(s)
                buf = ""
        s = buf.strip(_SENT_END + "，、 ").strip()
        if s:
            out.append(s)
    return out


def _condition_of(sent: str, tail_start: int) -> str:
    """取处置部分之前那一截作为 s（条件）。

    以最后一个逗号为界：「如肺气短促，或不足者，加人参、白芍药」的条件是
    「或不足者」还是「如肺气短促，或不足者」？取后者——只取最后一小段会丢掉
    「肺气短促」这个真正的证候，而条件在这本书里是逐层加细的。
    """
    head = sent[:tail_start].strip("，、；：")
    head = re.sub(r"^(?:如|若|假令|凡|其|且)", "", head).strip("，、 ")
    return head


def _is_mechanism_conclusion(o: str) -> bool:
    if any(w in o for w in _ELEMENT_WORDS) and any(
        w in o for w in _MECHANISM_TAIL_WORDS
    ):
        return True
    # 「脾胃乃伤」：证素 + 「乃」+ 结论字。
    return bool(re.search(r"(?:乃|即|自)[一-鿿]{1,3}$", o)) and any(
        w in o for w in _ELEMENT_WORDS
    )


def _shortest_conclusion(tail: str) -> str | None:
    """「则」后面那一截逐段收短，取第一个成立的病机结论。

    「形体劳役则脾病，脾病则怠惰嗜卧，四肢不收，大便泄泻」整截拿来当 o，
    得到的是半句原文而不是一个结论。按「，」逐段加长、取第一个过
    `_is_mechanism_conclusion` 的（这里是「脾病」），三元组才是三元组。
    """
    parts = tail.split("，")
    for k in range(1, len(parts) + 1):
        cand = "，".join(parts[:k])
        if _is_mechanism_conclusion(cand):
            return cand
    return None


def extract_from_sentence(sent: str, chapter: str) -> list[RationaleRecord]:
    """一句抽 0~N 条。同一句可以既有方又有加药（「四君子汤中去茯苓，加黄芪」），
    所以返回列表而不是 0/1。"""
    if not (_MIN_SENT_CHARS <= len(sent) <= _MAX_SENT_CHARS):
        return []
    out: list[RationaleRecord] = []

    def add(p: str, s: str, o: str) -> None:
        s, o = s.strip("，、；： "), o.strip("，、；： ")
        if not s or not o:
            return
        out.append(
            RationaleRecord(
                s=s, p=p, o=o, source_span=sent, chapter=chapter, book=BOOK
            )
        )

    m = _USE_FORMULA_RE.search(sent)
    if m:
        # 两个分支各有一个捕获组，命中哪个就用哪个。
        add("用方", _condition_of(sent, m.start()), m.group(1) or m.group(2))

    m = _ADD_RE.search(sent)
    if m and looks_like_herb_list(m.group(1)):
        add("加药", _condition_of(sent, m.start()), m.group(1))

    m = _DROP_RE.search(sent)
    if m and looks_like_herb_list(m.group(1)):
        add("去药", _condition_of(sent, m.start()), m.group(1))

    m = _FORBID_RE.search(sent)
    if m:
        add("禁忌", _condition_of(sent, m.start()), m.group(1) + m.group(2))

    m = _TREAT_RE.search(sent)
    if m:
        add("治法", _condition_of(sent, m.start()), m.group(1))

    m = _MECHANISM_RE.match(sent)
    if m:
        concl = _shortest_conclusion(m.group(2))
        if concl:
            add("病机", m.group(1), concl)

    return out


def verify_spans(records: list[RationaleRecord], text: str) -> tuple[list[RationaleRecord], int]:
    """逐字核验 source_span 出现在原文里。核验不过的整条丢弃并计数——
    跟 X3/S6 同一道闸门，理由见模块文档字符串。"""
    kept, dropped = [], 0
    for r in records:
        if r.source_span in text:
            kept.append(r)
        else:
            dropped += 1
    return kept, dropped


def probe(text: str) -> dict:
    """先探再切（R18-C 立的规矩）：不写文件，只报语料结构，用来核对切分是否合理。"""
    lines = _chapter_body_lines(text)
    spans = _chapter_spans(lines)
    sents = [s for _, body in spans for s in split_sentences(body)]
    return {
        "n_lines": len(lines),
        "n_chapters": len(spans),
        "n_sentences": len(sents),
        "chapters": [name for name, _ in spans],
        "n_sentences_in_range": sum(
            1 for s in sents if _MIN_SENT_CHARS <= len(s) <= _MAX_SENT_CHARS
        ),
    }


def extract(text: str) -> tuple[list[RationaleRecord], dict]:
    lines = _chapter_body_lines(text)
    spans = _chapter_spans(lines)
    raw: list[RationaleRecord] = []
    for name, body in spans:
        for sent in split_sentences(body):
            raw.extend(extract_from_sentence(sent, name))
    kept, dropped = verify_spans(raw, text)
    # 同一句同一谓词可能被两条规则都命中（「不渴而小便自利」既像禁忌又像病机），
    # 按 (s,p,o) 去重——重复条目对下游只是噪声。
    seen: set[tuple[str, str, str]] = set()
    uniq: list[RationaleRecord] = []
    for r in kept:
        key = (r.s, r.p, r.o)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq, summarize(uniq, n_raw=len(raw), n_span_dropped=dropped)


def summarize(records: list[RationaleRecord], *, n_raw: int, n_span_dropped: int) -> dict:
    by_p: dict[str, int] = {}
    by_chapter: dict[str, int] = {}
    for r in records:
        by_p[r.p] = by_p.get(r.p, 0) + 1
        by_chapter[r.chapter] = by_chapter.get(r.chapter, 0) + 1
    return {
        "n_triples": len(records),
        "n_raw": n_raw,
        "n_span_dropped": n_span_dropped,
        "n_dedup_dropped": n_raw - n_span_dropped - len(records),
        "by_predicate": dict(sorted(by_p.items(), key=lambda kv: -kv[1])),
        "n_chapters_with_triples": len(by_chapter),
        "by_chapter_top5": dict(sorted(by_chapter.items(), key=lambda kv: -kv[1])[:5]),
    }


def write_jsonl(records: list[RationaleRecord], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="《脾胃论》立论三元组抽取（确定性）")
    ap.add_argument("--input", default=str(DEFAULT_INPUT))
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT))
    ap.add_argument("--probe", action="store_true", help="只报语料结构，不抽不写")
    ap.add_argument("--dry-run", action="store_true", help="抽但不落盘")
    ap.add_argument("--show", type=int, default=10, help="打印前 N 条")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        print(f"[错误] 找不到语料：{src}")
        return 1
    text = src.read_text(encoding="utf-8")

    if args.probe:
        print(json.dumps(probe(text), ensure_ascii=False, indent=2))
        return 0

    records, stats = extract(text)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    for r in records[: args.show]:
        print(f"  ({r.s}) -[{r.p}]-> ({r.o})   〔{r.chapter}〕")
    if args.dry_run:
        print("[dry-run] 未落盘")
        return 0
    write_jsonl(records, Path(args.output))
    print(f"[写入] {args.output}（{len(records)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
