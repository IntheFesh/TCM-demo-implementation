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
import re
from dataclasses import dataclass
from pathlib import Path

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


def load_ocr_fixes(path: Path | None = None) -> list[tuple[str, str]]:
    """读 data/standard/ocr_fixes.tsv。返回 [(错, 对)]，**按左列长度降序**。

    降序是必须的：表里同时有「大便唐薄→大便溏薄」和「便唐→便溏」，先替换短的
    会把长的那条永远匹配不到。
    恒等项（错 == 对）和空行直接报错而不是忽略——一条恒等项在表里只可能是
    手误，静默忽略会让人以为它生效了。
    """
    p = path or OCR_FIXES_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"未找到 OCR 修正表 {p}。它在版本控制里（data/standard/*.tsv），"
            "缺失说明工作树不完整，不是可以跳过的一步。"
        )
    pairs: list[tuple[str, str]] = []
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
        if not wrong or not right:
            raise ValueError(f"{p}:{lineno} 两列都不许空：{line!r}")
        if wrong == right:
            raise ValueError(f"{p}:{lineno} 恒等项（{wrong}）在表里只可能是手误")
        pairs.append((wrong, right))
    pairs.sort(key=lambda kv: -len(kv[0]))
    return pairs


def apply_ocr_fixes(text: str, fixes: list[tuple[str, str]] | None = None) -> str:
    """整词替换。**不逐字替换**——只写「唐→溏」会把「唐代」一起改掉。"""
    for wrong, right in (fixes if fixes is not None else load_ocr_fixes()):
        text = text.replace(wrong, right)
    return text


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
    ocr_fixes: list[tuple[str, str]] | None = None,
) -> tuple[list[SyndromeDefinition], dict]:
    """返回 (抽出的证候定义, 统计)。统计里如实报跳过的块和跳过原因——
    "临床表现：" 在原文里出现的次数是抽取质量的上界，跳过多少、为什么跳，
    不能不报。"""
    lay = layout or LAYOUTS["neike"]
    clinical_re = _label_re(lay.clinical_labels)
    pathogenesis_re = _label_re(lay.pathogenesis_labels)
    end_re = _label_re(lay.end_labels)
    # OCR 修正在**切分之前**做：错字落在标签上（「证候分忻：」）会让整块解析不到，
    # 落在症状里会让症状节点 id 跟图谱对不上。切完再修就晚了。
    raw_text = apply_ocr_fixes(md_path.read_text(encoding="utf-8"), ocr_fixes)
    lines = raw_text.splitlines()
    stats = {
        "layout": lay.key, "book": lay.book,
        "clinical_blocks_seen": 0, "extracted": 0,
        "skipped_no_disease": 0, "skipped_no_syndrome_name": 0,
        "skipped_no_pathogenesis": 0, "skipped_no_elements_matched": 0,
    }
    current_disease: str | None = None
    current_syndrome_name: str | None = None
    entries: list[SyndromeDefinition] = []
    seq = 0

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()

        m = _DISEASE_RE.match(line)
        if m:
            current_disease = _clean_disease_name(m.group(1))
            i += 1
            continue

        m = _SYN_HEADING_RE.match(line)
        if m:
            current_syndrome_name = m.group(1).strip()
            i += 1
            continue

        m = _SYN_SUBITEM_RE.match(line)
        if m:
            current_syndrome_name = m.group(1).strip()
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
            symptoms, tongue_pulse = _extract_symptoms_and_tongue(clinical_text)
            if not symptoms:
                stats["skipped_no_elements_matched"] += 1
                continue

            name = current_syndrome_name
            if not name.endswith("证"):
                name += "证"
            seq += 1
            entries.append(SyndromeDefinition(
                code=f"{lay.code_prefix}-{seq:03d}",
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

    return entries, stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="从十四五教材抽取证候定义（规则脚本，零 LLM 调用）")
    ap.add_argument("--md-path", type=Path, required=True)
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
        "--append", action="store_true",
        help="追加到 --out 已有内容后面，不传就是覆盖写（先读一遍旧内容确认不是误覆盖）",
    )
    args = ap.parse_args(argv)

    if not args.md_path.exists():
        raise FileNotFoundError(f"未找到 {args.md_path}。先 clone TCM_Datasets 仓库。")

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
