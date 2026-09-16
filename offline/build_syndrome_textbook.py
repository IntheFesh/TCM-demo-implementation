"""从十四五规划教材《中医内科学》原文抽取证候定义，追加进
data/standard/syndromes.jsonl。**纯规则脚本，零 LLM 调用**——教材原文每个
证型都按「临床表现：/证机概要：/治法：/代表方：/常用药：」这个固定四元组
（实际用到的是前两项）排版，规则就能可靠切分，不需要模型。

**用途**：K2 的病位/病性证素权重目前只有 17 条手工条目撑着，样本量太小；
G1 的信息增益、追问链路也只在小候选池上验证过。教材扩表把候选池从 17 条
撑到几百条，是在真实规模下暴露"小样本下够用、大样本下会出问题"这类假设
（见追问链路那轮修复的报告）。

**抽取规则**（跟真实原文的排版结构绑定，改了排版这份脚本就要跟着改）：
  - 病名：最近一个 `# 第N节 XXX` 标题
  - 证型名：最近一个 `# N.XXX` 或 `（N）XXX` 标题；append "证" 如果原文没带
  - definition：直接用证机概要原文（"证机概要"本来就是这个证候机理的摘要）
  - location/nature：证机概要文本里匹配 core.elements 的 LOCATIONS/NATURES
    固定词表（子串匹配，跟 K1 建图谱骨架用的同一套词表）
  - tongue_pulse：临床表现文本里最后一个"舌"字往后的部分
  - cardinal_symptoms：临床表现文本里舌脉之前的部分，按"，"/"；"切分
  - secondary_symptoms：**留空**。教材原文是一个扁平症状列表，没有标注
    哪些是主症哪些是次症——规则脚本没有语义理解能力区分不出来，宁可留空
    也不编一个猜的划分（次症留空只影响 K2 权重估计的"次症"这一支，不影响
    location/cardinal_symptoms 这两个"定义性"字段，见 offline/build_graph.py
    的 check_corpus_coverage 对这两档字段的区分）

**不去重**：同一个证候名（比如"脾胃虚寒证"）在不同病名下各自是一条独立
记录，不合并——合并会丢掉"这条是哪个病下辨出来的"这个信息，而这正是
disease_hint 收窄候选池要用的锚点。也不跟原有 17 条做名字去重：378 这个
总数（361+17）本来就没减掉重名的。

用法：
    git clone --depth 1 https://github.com/PanckooAI/TCM_Datasets.git /tmp/tcmds
    python -m offline.build_syndrome_textbook \\
        --md-path /tmp/tcmds/十四五教材/中医内科学.md \\
        --out data/standard/syndromes.jsonl --append
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from core.elements import LOCATIONS, NATURES
from core.schemas import SyndromeDefinition

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_PATH = ROOT / "data" / "standard" / "syndromes.jsonl"

_DISEASE_RE = re.compile(r"^#\s*第[一二三四五六七八九十百]*节\s*(.+?)\s*$")
_SYN_HEADING_RE = re.compile(r"^#\s*\d+[\.、]\s*(.+?)\s*$")
_SYN_SUBITEM_RE = re.compile(r"^（\d+）\s*(.+?)\s*$")
# 三个字段标签的正则现在按教材（TextbookLayout）现编，不再是模块级常量——
# 五本专科教材的标签跟内科不一样，写死在模块级就只能服务一本。

_ELEMENT_VOCAB = LOCATIONS + NATURES  # 匹配顺序：先长的病位/病性词，见下面的排序


# ---------- R18-E：五本专科教材的排版差异 ----------

OCR_FIXES_PATH = ROOT / "data" / "standard" / "ocr_fixes.tsv"

# 一条修正规则作用在哪一列。**R29 新增的第四列。**
#   name / disease  证型名、病名（R29 之前这两列一条规则都没指向过）
#   symptom         cardinal_symptoms + tongue_pulse（表原本瞄的就是这两项）
#   all             不限列——在切分之前对整份原文生效，这是旧行的默认值
# 为什么需要这一列：「疽→疸」是单字规则。病名列是 51 个值的闭集合，里面没有一个
# 合法含「疽」；而正文里「痈疽》」的疽是对的（实测 1 处）。同一条规则在一列安全、
# 在另一列会把对的原文改错——所以规则必须能说清自己管哪一列。
OCR_SCOPES = ("name", "disease", "symptom", "all")


class OcrFix(NamedTuple):
    """修正表的一行。字段顺序跟文件里的列顺序一致。

    用命名元组而不是 `(错, 对)` 二元组：从两列扩到四列之后，位置解包
    （`for w, r in fixes`）会静默错位成「把说明当右列」，而命名元组当场报错。
    """

    wrong: str
    right: str
    why: str
    scope: str


def load_ocr_fixes(path: Path | None = None) -> list[OcrFix]:
    """读 data/standard/ocr_fixes.tsv。返回 [OcrFix]，**按左列长度降序**。

    降序是必须的：表里同时有「大便唐薄→大便溏薄」和「便唐→便溏」，先替换短的
    会把长的那条永远匹配不到。
    恒等项（错 == 对）和空行直接报错而不是忽略——一条恒等项在表里只可能是
    手误，静默忽略会让人以为它生效了。

    三条加载期校验，都是"这张表本身写错了"而不是"原文有错字"：
      - scope 不在 OCR_SCOPES 里 → 报错并列出四个合法值。静默当成 `all`
        会让一条本该限定在病名列的单字规则全局生效，把对的原文改坏。
      - 某条的右列里含着另一条（或自己）的左列 → 报错。`str.replace` 之下
        这种表是不幂等的：「面唇发→面唇发绀」跑两遍会变成「面唇发绀绀」。
      - 说明列为空 → 报错。这张表的每一条都要能回答"为什么这条安全"。
    """
    p = path or OCR_FIXES_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"未找到 OCR 修正表 {p}。它在版本控制里（data/standard/*.tsv），"
            "缺失说明工作树不完整，不是可以跳过的一步。"
        )
    fixes: list[OcrFix] = []
    for lineno, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cols = line.split("\t")
        if cols[0] == "错":  # 表头
            continue
        if len(cols) < 2:
            raise ValueError(f"{p}:{lineno} 至少要有「错\\t对」两列，实际：{line!r}")
        wrong, right = cols[0].strip(), cols[1].strip()
        why = cols[2].strip() if len(cols) > 2 else ""
        # 旧行只有三列（错/对/说明），默认 all——这是为了**行为逐字节不变**：
        # 默认成别的值会让已经落盘的教材条目重跑出不一样的结果。
        scope = cols[3].strip() if len(cols) > 3 and cols[3].strip() else "all"
        if not wrong or not right:
            raise ValueError(f"{p}:{lineno} 两列都不许空：{line!r}")
        if wrong == right:
            raise ValueError(f"{p}:{lineno} 恒等项（{wrong}）在表里只可能是手误")
        if not why:
            raise ValueError(
                f"{p}:{lineno}（{wrong}→{right}）第三列要写清为什么会错 / 为什么这条安全"
            )
        if scope not in OCR_SCOPES:
            raise ValueError(
                f"{p}:{lineno}（{wrong}→{right}）scope 是 {scope!r}，"
                f"合法值只有：{' / '.join(OCR_SCOPES)}"
            )
        fixes.append(OcrFix(wrong=wrong, right=right, why=why, scope=scope))
    fixes.sort(key=lambda f: -len(f.wrong))
    _reject_non_idempotent(fixes, p)
    return fixes


def _reject_non_idempotent(fixes: list[OcrFix], path: Path) -> None:
    """右列里含着某条左列 → 多次替换会累加。**在加载时拒绝，不在使用时补救。**

    只在同一作用域（或跟 `all`）之间比：symptom 的规则跟 name 的规则不会落在
    同一段文本上，它们之间的包含关系不构成问题。
    """
    for f in fixes:
        for g in fixes:
            if "all" not in (f.scope, g.scope) and f.scope != g.scope:
                continue
            if g.wrong in f.right:
                raise ValueError(
                    f"{path}：「{f.wrong}→{f.right}」的右列里含着「{g.wrong}」，"
                    f"这张表跑两遍结果会变（不幂等）。把右列带上下文改写成"
                    "不含任何左列的形式，或者把这两条合成一条。"
                )


def apply_ocr_fixes(
    text: str,
    fixes: list[OcrFix] | None = None,
    *,
    scope: str = "all",
) -> str:
    """整词替换。**不逐字替换**——只写「唐→溏」会把「唐代」一起改掉。

    `scope` 是**正在修的那一列**，不是"筛哪些规则"。一条规则参与进来的判据是
    「这条规则管不管这一列」：`fix.scope == "all"` 或 `fix.scope == scope`。
    所以默认 `scope="all"` 就是旧行为——只施加不限列的那些规则。
    """
    if scope not in OCR_SCOPES:
        raise ValueError(f"scope 只能是 {' / '.join(OCR_SCOPES)}，收到 {scope!r}")
    for f in (fixes if fixes is not None else load_ocr_fixes()):
        if f.scope == "all" or f.scope == scope:
            text = text.replace(f.wrong, f.right)
    return text


# ---------- R29：可疑条目（报出来，不自动改） ----------

# 去掉「证」只剩一个字、但教材里确实这么叫的证型名。
# 中风分闭证/脱证，厥证分实证/虚证——它们不是掉字。
# **这不是第二张匹配表**：它回答的是"这个短名字是不是教材里的正式叫法"，
# 跟 ocr_fixes.tsv 回答的"这个写法是不是 OCR 错字"不是同一个问题
# （CLAUDE.md 第 31 条的例外要写清两个问题的区别，这就是那一句）。
SHORT_NAME_WHITELIST = frozenset({"闭证", "脱证", "实证", "虚证"})

# 四条可疑判据的理由文本。**做成常量**是因为报告和测试都要引它，
# 写死在两处以后改一边就对不上了。
SUSPICIOUS_ONE_CHAR_DISEASE = "病名疑似掉字：只剩一个字"
SUSPICIOUS_NAME_NOT_ZHENG = "证型名不以「证」结尾且不在白名单里"
SUSPICIOUS_SHORT_NAME = "证型名疑似掉字：去掉「证」只剩一个字"
SUSPICIOUS_REUSED_NAME = "证型名疑似被上一条复用：同名同病出现多次"


@dataclass(frozen=True)
class SuspiciousEntry:
    """一条"看起来不对但不能自动改"的记录。

    **为什么只报不改**：`disease=逆` 少的是「呃」、`name=阻心脉证` 少的是「瘀」，
    而教材 markdown 原文本身就没有那个字（`# 第五节 逆` 在原文第 5968 行、
    `# 6.阻心脉` 在第 2981 行）。补字要靠语义猜，猜错了就是把一个错的名字
    换成另一个错的名字，而且以后没人知道它被动过——**改错比不改坏**。
    """

    code: str
    name: str
    disease: str | None
    reason: str
    # 教材原文行号。从已落盘的 jsonl 复查时取不到（教材 markdown 不在版本控制里），
    # 那时是 None，报告里要照实说"行号取不到"而不是编一个。
    lineno: int | None = None


def find_suspicious_entries(
    entries: list[SyndromeDefinition],
    linenos: dict[str, dict[str, int]] | None = None,
) -> list[SuspiciousEntry]:
    """扫一遍抽出来的条目，报出四类可疑形状。**不改任何字段。**

    判据是"这个数被报出来"，不是"它归零"——归零要么是真的修好了解析器，
    要么是有人把判据放宽了，而后者从这个函数的返回值上看不出来。

    `linenos`：code → {"name": 证型名标题行号, "disease": 病名标题行号}。
    没有就都报 None。
    """
    groups = Counter((e.name, e.disease) for e in entries)
    out: list[SuspiciousEntry] = []
    for e in entries:
        where = (linenos or {}).get(e.code, {})
        reasons: list[tuple[str, str]] = []   # (reason, 取哪个行号)
        if e.disease is not None and len(e.disease.strip()) == 1:
            reasons.append((SUSPICIOUS_ONE_CHAR_DISEASE, "disease"))
        # 这一条**当前 0 条命中**：parse_textbook 会给没带「证」的名字补上。
        # 留着它是为了将来有人去掉那个补字动作时不至于没人看着——
        # 一条永远不触发的判据不算覆盖，报告里照实写 0。
        if not e.name.endswith("证") and e.name not in SHORT_NAME_WHITELIST:
            reasons.append((SUSPICIOUS_NAME_NOT_ZHENG, "name"))
        if len(e.name.removesuffix("证")) <= 1 and e.name not in SHORT_NAME_WHITELIST:
            reasons.append((SUSPICIOUS_SHORT_NAME, "name"))
        if groups[(e.name, e.disease)] > 1:
            reasons.append((SUSPICIOUS_REUSED_NAME, "name"))
        for reason, which in reasons:
            out.append(SuspiciousEntry(
                code=e.code, name=e.name, disease=e.disease,
                reason=reason, lineno=where.get(which),
            ))
    return out


@dataclass(frozen=True)
class TextbookLayout:
    """一本教材的排版。

    五本专科教材的字段标签跟《中医内科学》不一样：内科是「临床表现：/证机概要：」，
    外科是「证候：/治法：」，妇科是「主要证候：/证候分析：」……**只有标签不同**，
    切症状与舌脉、匹配证素、清病名这三件事完全一样，所以做成数据而不是五份
    复制粘贴的解析函数（CLAUDE.md「同一概念只能有一处实现」）。

    每个字段都是**标签元组**而不是单个标签：同一本教材里「证候：」「主要证候：」
    两种写法都出现过，写死一个会让另一种全部落空。
    """

    key: str
    book: str
    code_prefix: str
    clinical_labels: tuple[str, ...]
    pathogenesis_labels: tuple[str, ...]
    # 一个证型块的结束标志。内科是「常用药：」，外科/儿科是「方药：」，
    # 推拿没有方药、以「手法：」收尾——缺了它块会一直吞到下一个「临床表现：」。
    end_labels: tuple[str, ...]


LAYOUTS: dict[str, TextbookLayout] = {
    # 内科是既有的那一本，标签和 code 前缀保持原样——378 那一批条目的 code
    # 是 TB-xxx，改前缀会让已经落盘的 syndromes.jsonl 跟重跑结果对不上。
    "neike": TextbookLayout(
        key="neike", book="中医内科学", code_prefix="TB",
        clinical_labels=("临床表现",),
        pathogenesis_labels=("证机概要",),
        end_labels=("常用药", "代表方"),
    ),
    "waike": TextbookLayout(
        key="waike", book="中医外科学", code_prefix="WK",
        clinical_labels=("证候", "临床表现"),
        pathogenesis_labels=("辨证分析", "证候分析", "证机概要"),
        end_labels=("方药", "常用药", "外治法"),
    ),
    "fuke": TextbookLayout(
        key="fuke", book="中医妇科学", code_prefix="FK",
        clinical_labels=("主要证候", "证候"),
        pathogenesis_labels=("证候分析", "证机概要"),
        end_labels=("方药举例", "方药", "常用药"),
    ),
    "erke": TextbookLayout(
        key="erke", book="中医儿科学", code_prefix="EK",
        clinical_labels=("证候", "临床表现"),
        pathogenesis_labels=("辨证", "证候分析", "证机概要"),
        end_labels=("方药", "常用药"),
    ),
    "yanke": TextbookLayout(
        key="yanke", book="中医眼科学", code_prefix="YK",
        clinical_labels=("症状", "自觉症状", "临床表现"),
        pathogenesis_labels=("证候分析", "证机概要"),
        end_labels=("方药", "常用药"),
    ),
    "tuina": TextbookLayout(
        key="tuina", book="推拿学", code_prefix="TN",
        clinical_labels=("临床表现", "症状"),
        pathogenesis_labels=("证候分析", "辨证"),
        # 推拿教材没有方药，块以「治则/手法/操作」收尾。漏了这一条，
        # 一个块会一直吞到下一个「临床表现：」，症状里混进上一节的手法描述。
        end_labels=("手法", "操作", "治则"),
    ),
}


def _label_re(labels: tuple[str, ...]) -> re.Pattern[str]:
    """把标签元组编成「^(?:标签A|标签B)[：:]\\s*(.*)$」。

    标签按长度降序排：「证候」和「主要证候」同时在表里时，`^(?:证候|主要证候)`
    对「主要证候：」这一行**匹配不上**（锚在行首），但对「证候分析：」会误命中
    ——所以还要求标签后面紧跟冒号，`证候分析` 因此不会被 `证候` 吃掉。
    """
    alts = "|".join(re.escape(x) for x in sorted(labels, key=len, reverse=True))
    return re.compile(r"^(?:%s)[：:]\s*(.*)$" % alts)


def detect_layout(text: str) -> dict[str, dict[str, int]]:
    """每种排版的两个关键标签在这份原文里各出现多少次。

    给「解析出 0 条」这个结果分三种情况用（CLAUDE.md 那条：工具返回空必须能
    区分"参数错了/文件不存在/确实没匹配"）：
      - 所有排版的计数都是 0 → 这份文件的标签跟六种都不一样，要先看原文
      - 选中的排版计数是 0、别的排版不是 0 → --layout 传错了
      - 选中的排版计数不是 0 但抽出 0 条 → 真的是块内结构不符合预期
    """
    out: dict[str, dict[str, int]] = {}
    for key, lay in LAYOUTS.items():
        cre, pre = _label_re(lay.clinical_labels), _label_re(lay.pathogenesis_labels)
        c = sum(1 for ln in text.splitlines() if cre.match(ln.strip()))
        p = sum(1 for ln in text.splitlines() if pre.match(ln.strip()))
        out[key] = {"clinical": c, "pathogenesis": p}
    return out



def _clean_disease_name(raw: str) -> str:
    """去掉可能粘连的页码（"喘证 51" 这类，候选目录/前置章节标题里出现过），
    以及 OCR 扫描留下的内部空格（"痰  饮" 应该是"痰饮"、"积 聚" 应该是
    "积聚"）——中文病名内部不该有空格，这些空格是扫描件排版的产物，不清理
    的话跟 data/standard/diseases.jsonl 里干净的病名（"痰饮"没有空格）字面
    对不上，match_disease() 算出来的病名传给 disease_hint 会因为多了个空格
    而精确匹配失败，disease_hint 的收窄效果直接失效。"""
    no_page_number = re.sub(r"\s*\d+\s*$", "", raw).strip()
    return re.sub(r"\s+", "", no_page_number)


def _extract_symptoms_and_tongue(clinical_text: str) -> tuple[list[str], str | None]:
    """临床表现原文 -> (症状列表, 舌脉)。最后一个"舌"字之前的内容是症状，
    从那里往后是舌脉——教材原文的临床表现固定以"……舌×××，脉×××"收尾。"""
    idx = clinical_text.rfind("舌")
    if idx == -1:
        symptoms_text, tongue_pulse = clinical_text, None
    else:
        symptoms_text, tongue_pulse = clinical_text[:idx], clinical_text[idx:].rstrip("。 ")
    parts = re.split(r"[，,；;、]", symptoms_text)
    symptoms = [p.strip() for p in parts if p.strip()]
    return symptoms, tongue_pulse


def _match_elements(pathogenesis_text: str) -> tuple[list[str], list[str]]:
    """证机概要原文里子串匹配 LOCATIONS/NATURES，按词表里的顺序去重收集——
    跟 K1 建图谱骨架用的是同一份词表（core.elements），不是另起一套。"""
    location = [loc for loc in LOCATIONS if loc in pathogenesis_text]
    nature = [nat for nat in NATURES if nat in pathogenesis_text]
    return location, nature


def parse_textbook(
    md_path: Path,
    layout: TextbookLayout | None = None,
    *,
    ocr_fixes: list[OcrFix] | None = None,
) -> tuple[list[SyndromeDefinition], dict]:
    """返回 (抽出的证候定义, 统计)。统计里如实报跳过的块和跳过原因——
    "临床表现：" 在原文里出现的次数是抽取质量的上界，跳过多少、为什么跳，
    不能不报。"""
    lay = layout or LAYOUTS["neike"]
    clinical_re = _label_re(lay.clinical_labels)
    pathogenesis_re = _label_re(lay.pathogenesis_labels)
    end_re = _label_re(lay.end_labels)
    fixes = ocr_fixes if ocr_fixes is not None else load_ocr_fixes()
    # 不限列（scope=all）的 OCR 修正在**切分之前**做：错字落在标签上
    # （「证候分忻：」）会让整块解析不到，落在症状里会让症状节点 id 跟图谱对不上。
    # 切完再修就晚了。**R29 之后这一遍只施加 all 那些规则**——限定到某一列的
    # 规则（「疽→疸」只管病名）在下面各列抽出来的那一刻单独施加，
    # 顺序仍然是"修正在切分之前"：病名/证型名不再切分，症状那一列是在
    # `_extract_symptoms_and_tongue` 之前修的。
    raw_text = apply_ocr_fixes(md_path.read_text(encoding="utf-8"), fixes)
    lines = raw_text.splitlines()
    stats: dict = {
        "layout": lay.key, "book": lay.book,
        "clinical_blocks_seen": 0, "extracted": 0,
        "skipped_no_disease": 0, "skipped_no_syndrome_name": 0,
        "skipped_no_pathogenesis": 0, "skipped_no_elements_matched": 0,
    }
    current_disease: str | None = None
    current_syndrome_name: str | None = None
    disease_lineno: int | None = None
    name_lineno: int | None = None
    linenos: dict[str, dict[str, int]] = {}
    entries: list[SyndromeDefinition] = []
    seq = 0

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()

        m = _DISEASE_RE.match(line)
        if m:
            # 病名列单独过一遍 scope=disease 的规则（「疽→疸」：病名列是闭集合，
            # 里面没有一个合法含「疽」，而正文里「痈疽》」是对的）
            current_disease = apply_ocr_fixes(
                _clean_disease_name(m.group(1)), fixes, scope="disease")
            disease_lineno = i + 1
            i += 1
            continue

        m = _SYN_HEADING_RE.match(line)
        if m:
            current_syndrome_name = apply_ocr_fixes(m.group(1).strip(), fixes, scope="name")
            name_lineno = i + 1
            i += 1
            continue

        m = _SYN_SUBITEM_RE.match(line)
        if m:
            current_syndrome_name = apply_ocr_fixes(m.group(1).strip(), fixes, scope="name")
            name_lineno = i + 1
            i += 1
            continue

        m = clinical_re.match(line)
        if m:
            stats["clinical_blocks_seen"] += 1
            clinical_text = m.group(1)
            pathogenesis_text: str | None = None
            j = i + 1
            while j < n:
                l2 = lines[j].strip()
                pm = pathogenesis_re.match(l2)
                if pm:
                    pathogenesis_text = pm.group(1)
                if end_re.match(l2):
                    j += 1
                    break
                # 下一个临床表现块开始了还没见到常用药，说明这块解析失败
                # （原文排版跟预期不一致），就地结束，不越界吞下一块的内容
                if clinical_re.match(l2) and j != i:
                    break
                j += 1
            i = j

            if current_disease is None:
                stats["skipped_no_disease"] += 1
                continue
            if not current_syndrome_name:
                stats["skipped_no_syndrome_name"] += 1
                continue
            if not pathogenesis_text:
                stats["skipped_no_pathogenesis"] += 1
                continue
            location, nature = _match_elements(pathogenesis_text)
            if not location and not nature:
                stats["skipped_no_elements_matched"] += 1
                continue
            # 症状/舌脉那一列：scope=symptom 的规则在**切分之前**施加
            symptoms, tongue_pulse = _extract_symptoms_and_tongue(
                apply_ocr_fixes(clinical_text, fixes, scope="symptom"))
            if not symptoms:
                stats["skipped_no_elements_matched"] += 1
                continue

            name = current_syndrome_name
            if not name.endswith("证"):
                name += "证"
            seq += 1
            code = f"{lay.code_prefix}-{seq:03d}"
            linenos[code] = {k: v for k, v in
                             (("disease", disease_lineno), ("name", name_lineno))
                             if v is not None}
            entries.append(SyndromeDefinition(
                code=code,
                name=name,
                is_category=False,
                parent=None,
                definition=pathogenesis_text,
                location=location,
                nature=nature,
                cardinal_symptoms=symptoms,
                secondary_symptoms=[],
                tongue_pulse=tongue_pulse,
                disease=current_disease,
                source="textbook",
            ))
            stats["extracted"] += 1
            continue

        i += 1

    stats["linenos"] = linenos
    stats["suspicious"] = find_suspicious_entries(entries, linenos)
    stats["n_suspicious"] = len(stats["suspicious"])
    return entries, stats


def load_committed_textbook_entries(path: Path | None = None) -> list[SyndromeDefinition]:
    """从已落盘的 syndromes.jsonl 里读回教材条目（`source == "textbook"`）。

    为什么需要这条路：教材 markdown 不在版本控制里（`books/` 是 gitignore 的，
    十四五教材要另外 clone），所以 clone 这个仓库的人**手上只有 jsonl**。
    可疑条目清单要能在那种情况下也复查得出来，只是拿不到原文行号。
    """
    p = path or DEFAULT_OUT_PATH
    if not p.exists():
        raise FileNotFoundError(f"未找到 {p}。它在版本控制里，缺失说明工作树不完整。")
    out: list[SyndromeDefinition] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("source") == "textbook":
            out.append(SyndromeDefinition(**data))
    return out


def print_suspicious(suspicious: list[SuspiciousEntry], *, with_linenos: bool) -> None:
    """打印可疑条目清单。**按理由分组**，每组先报条数再逐条列。

    条数在前是因为这份清单的用途是"这一轮比上一轮多了还是少了"——
    逐条往下翻到底才知道有多少条，那个数就没人看了。
    """
    print(f"\n可疑条目 {len(suspicious)} 条"
          + ("" if with_linenos else "（**原文行号取不到**：教材 markdown 不在版本控制里，"
                                     "这份清单是从 data/standard/syndromes.jsonl 复查的）"))
    if not suspicious:
        print("  —— 一条都没有。这不一定是好消息：先确认判据还在（"
              "find_suspicious_entries 的四条），再确认解析器真的修好了。")
        return
    by_reason: dict[str, list[SuspiciousEntry]] = {}
    for s in suspicious:
        by_reason.setdefault(s.reason, []).append(s)
    for reason, group in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"\n  【{reason}】{len(group)} 条")
        for s in group:
            where = f"  原文第 {s.lineno} 行" if s.lineno is not None else ""
            print(f"    {s.code}  {s.name}（{s.disease or '—'}）{where}")
    print("\n  这些**不自动改**：掉的那个字在教材原文里就没有（`# 第五节 逆` 在原文"
          "第 5968 行、`# 6.阻心脉` 在第 2981 行），补字要靠语义猜——"
          "改错比不改坏。判据是这个数被报出来，不是它归零。")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="从十四五教材抽取证候定义（规则脚本，零 LLM 调用）")
    ap.add_argument(
        "--md-path", type=Path, default=None,
        help="教材 markdown。抽取和 --detect 必须传；--report 不传就从 --out 复查"
             "（那时报不出原文行号）",
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument(
        "--layout", choices=sorted(LAYOUTS), default="neike",
        help="教材排版。五本专科教材的字段标签跟《中医内科学》不一样，传错会抽出 0 条",
    )
    ap.add_argument(
        "--detect", action="store_true",
        help="只报六种排版的关键标签在这份原文里各出现多少次，不抽不写。"
             "抽出 0 条时先跑这个——它区分「标签都不匹配」和「--layout 传错了」",
    )
    ap.add_argument(
        "--report", action="store_true",
        help="只打印可疑条目清单（病名/证型名看起来掉了字、或名字被上一条复用的），"
             "**一个字节都不写**。不传 --md-path 就从 --out 已落盘的条目复查",
    )
    ap.add_argument(
        "--append", action="store_true",
        help="追加到 --out 已有内容后面，不传就是覆盖写（先读一遍旧内容确认不是误覆盖）",
    )
    args = ap.parse_args(argv)

    if args.md_path is None and not args.report:
        ap.error("抽取需要 --md-path（只看可疑条目清单的话传 --report）")
    if args.md_path is not None and not args.md_path.exists():
        raise FileNotFoundError(f"未找到 {args.md_path}。先 clone TCM_Datasets 仓库。")

    if args.report:
        # **只报不写。** 这条路径一个字节都不落盘——它是给"上机之前先看一眼
        # 还有多少条名字不对"用的，不是抽取流程的一环。
        if args.md_path is None:
            entries = load_committed_textbook_entries(args.out)
            print(f"从 {args.out} 复查 {len(entries)} 条教材条目")
            print_suspicious(find_suspicious_entries(entries), with_linenos=False)
        else:
            entries, stats = parse_textbook(args.md_path, LAYOUTS[args.layout])
            print(f"教材：{stats['book']}（--layout {stats['layout']}），"
                  f"抽出 {stats['extracted']} 条")
            print_suspicious(stats["suspicious"], with_linenos=True)
        return

    if args.detect:
        counts = detect_layout(args.md_path.read_text(encoding="utf-8"))
        for key, c in sorted(counts.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"  {key:8s} {LAYOUTS[key].book:8s} "
                  f"临床表现类标签 {c['clinical']:4d}  病机类标签 {c['pathogenesis']:4d}")
        if not any(sum(c.values()) for c in counts.values()):
            print("六种排版的标签一个都没出现——先打开原文看它的字段怎么写的，"
                  "不要改 --layout 反复试。")
        return

    layout = LAYOUTS[args.layout]
    entries, stats = parse_textbook(args.md_path, layout)

    print(f"教材：{stats['book']}（--layout {stats['layout']}）")
    print(f"原文里「{layout.clinical_labels[0]}：」等临床表现类标签出现 "
          f"{stats['clinical_blocks_seen']} 次")
    print(f"抽出 {stats['extracted']} 条证候定义")
    print(f"跳过：无病名上下文 {stats['skipped_no_disease']}，无证型名 {stats['skipped_no_syndrome_name']}，"
          f"无证机概要 {stats['skipped_no_pathogenesis']}，"
          f"匹配不到症状或病位/病性证素 {stats['skipped_no_elements_matched']}")
    diseases = sorted({e.disease for e in entries if e.disease})
    print(f"覆盖 {len(diseases)} 个病名")
    if not entries:
        # 抽出 0 条不是"跑完了"，要说清下一步——这一条是 CLAUDE.md
        # 「工具返回空必须能区分三种情况」在这个脚本上的落点。
        print("抽出 0 条。跑 --detect 看是标签全不匹配（要读原文）"
              "还是 --layout 传错了（别的排版计数不是 0）。")

    existing_lines: list[str] = []
    if args.append and args.out.exists():
        existing_lines = args.out.read_text(encoding="utf-8").splitlines()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for line in existing_lines:
            if line.strip():
                f.write(line.strip() + "\n")
        for e in entries:
            f.write(e.model_dump_json() + "\n")

    total = len(existing_lines) + len(entries)
    print(f"已写出 {args.out}（{'追加，' if args.append else ''}共 {total} 条）")


if __name__ == "__main__":
    main()
