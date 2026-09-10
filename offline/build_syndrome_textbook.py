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
from pathlib import Path

from core.elements import LOCATIONS, NATURES
from core.schemas import SyndromeDefinition

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_PATH = ROOT / "data" / "standard" / "syndromes.jsonl"

_DISEASE_RE = re.compile(r"^#\s*第[一二三四五六七八九十百]*节\s*(.+?)\s*$")
_SYN_HEADING_RE = re.compile(r"^#\s*\d+[\.、]\s*(.+?)\s*$")
_SYN_SUBITEM_RE = re.compile(r"^（\d+）\s*(.+?)\s*$")
_CLINICAL_RE = re.compile(r"^临床表现[：:]\s*(.*)$")
_PATHOGENESIS_RE = re.compile(r"^证机概要[：:]\s*(.*)$")
_HERBS_RE = re.compile(r"^常用药[：:]")

_ELEMENT_VOCAB = LOCATIONS + NATURES  # 匹配顺序：先长的病位/病性词，见下面的排序


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


def parse_textbook(md_path: Path) -> tuple[list[SyndromeDefinition], dict]:
    """返回 (抽出的证候定义, 统计)。统计里如实报跳过的块和跳过原因——
    "临床表现：" 在原文里出现的次数是抽取质量的上界，跳过多少、为什么跳，
    不能不报。"""
    lines = md_path.read_text(encoding="utf-8").splitlines()
    stats = {
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

        m = _CLINICAL_RE.match(line)
        if m:
            stats["clinical_blocks_seen"] += 1
            clinical_text = m.group(1)
            pathogenesis_text: str | None = None
            j = i + 1
            while j < n:
                l2 = lines[j].strip()
                pm = _PATHOGENESIS_RE.match(l2)
                if pm:
                    pathogenesis_text = pm.group(1)
                if _HERBS_RE.match(l2):
                    j += 1
                    break
                # 下一个临床表现块开始了还没见到常用药，说明这块解析失败
                # （原文排版跟预期不一致），就地结束，不越界吞下一块的内容
                if _CLINICAL_RE.match(l2) and j != i:
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
                code=f"TB-{seq:03d}",
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
        "--append", action="store_true",
        help="追加到 --out 已有内容后面，不传就是覆盖写（先读一遍旧内容确认不是误覆盖）",
    )
    args = ap.parse_args(argv)

    if not args.md_path.exists():
        raise FileNotFoundError(f"未找到 {args.md_path}。先 clone TCM_Datasets 仓库。")

    entries, stats = parse_textbook(args.md_path)

    print(f"原文里「临床表现：」出现 {stats['clinical_blocks_seen']} 次")
    print(f"抽出 {stats['extracted']} 条证候定义")
    print(f"跳过：无病名上下文 {stats['skipped_no_disease']}，无证型名 {stats['skipped_no_syndrome_name']}，"
          f"无证机概要 {stats['skipped_no_pathogenesis']}，"
          f"匹配不到症状或病位/病性证素 {stats['skipped_no_elements_matched']}")
    diseases = sorted({e.disease for e in entries if e.disease})
    print(f"覆盖 {len(diseases)} 个病名")

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
