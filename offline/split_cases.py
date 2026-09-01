"""从公版古籍切出"粗段"，交给 LLM（offline/extract_cases.py 的 s0 步骤）判断段内
到底有几个病人、每个病人几诊。源: xiaopangxia/TCM-Ancient-Books

R1 用案首正则（YE_START/WU_START）硬切案边界，R1 的 --stats-only 诊断发现这条路
走不通：吴鞠通全书密度只有叶天士的 1/5.24（708 字/段 vs 135 字/段），案首正则漏切
掉大量案首体例（纪年开头、官职头衔指代病人等），把多个病人焊成一段——而这类体例
"族婶母""一叟""通廷尉"层出不穷，手写穷举是无底洞。

这一版改变方向：正则不再决定案边界，只负责两件事——
  1. 把正文切成喂给模型的"粗段"（按空行 + 长度上限，宁可粘连不可切碎）
  2. 在粗段里找 head_hints（疑似病人标识）和 follow_hints（疑似复诊标记）作为
     提示 + 事后交叉校验用，不再是判定依据

这和 R1 对复诊标记的处理是同一个思路：正则从"裁判"降级成"提示"，真正的边界
判断交给 LLM。head_hints 的正则刻意写得宽松（宁可多报），因为它现在只是线索。
"""
import argparse
import re
import pathlib
import unicodedata
from collections import Counter

# ---------- head_hints：疑似病人标识（宽松，只做提示 + 交叉校验，不做判据） ----------
HEAD_HINT_PATTERNS = [
    ("姓名岁数", re.compile(r"[一-龥]{1,3}(氏)?\s+([一二三四五六七八九十百]+岁|[甲乙丙丁戊己庚辛壬癸][^\s]{0,6}年)")),
    ("单字姓氏", re.compile(r"(?:^|\n)[一-龥](（[^）]{1,8}）|\s)")),  # 叶天士式：单字姓+（年龄/氏）或空格
    ("纪年", re.compile(r"[甲乙丙丁戊己庚辛壬癸][子丑寅卯辰巳午未申酉戌亥]年")),
    ("称谓", re.compile(r"一(人|妇人|妇|叟|童子|少年|老者|媪)")),
    ("族称", re.compile(r"(族|堂)[一-龥]{1,3}|某氏")),
    # 常见清代官职/尊称，不追求穷举——这份列表本来就只是提示，漏掉的交给 LLM 判断
    ("头衔", re.compile(r"(太史|太守|通判|廷尉|观察|大令|明府|茂才|孝廉|封翁|方伯|太仆|中丞|少宰|光禄)")),
]

# ---------- follow_hints：疑似复诊标记（沿用 R1 的 FOLLOW / FOLLOW_KEYWORDS） ----------
FOLLOW = re.compile(r"^(又|[初廿卅一二三四五六七八九十]+[日月]|[正一二三四五六七八九十腊]+月)")
FOLLOW_KEYWORDS = re.compile(r"复诊|二诊|三诊|服\S{0,3}剂")

TAIL = re.compile(r"^(徐评|案中|<目录>|<篇名>)")

BOOKS = {
    "ye_tianshi": dict(
        src="367-临证指南医案.txt",
        gates=["胃脘痛", "脾胃", "木乘土", "噎膈反胃", "呕吐", "痞", "痰饮", "肿胀", "积聚"],
        concat_gates=False,  # 密度高（135 字/段），逐门类分别切，没必要跨门类拼接
    ),
    "wu_jutong": dict(
        src="361-吴鞠通医案.txt",
        gates=["胃痛", "脾胃", "呕吐", "反胃", "噎", "泄泻", "痞", "痰饮", "肿胀", "积聚", "滞下"],
        concat_gates=True,  # 密度低、案首体例杂：先按门类过滤太早，门类边界处的病人
                            # 会被 <篇名> 硬切断（"脾胃病人焊在温病病人后面"是边界内的事，
                            # 这里至少先把所有命中门类拼成一条连续正文再切，缓解门类间的截断）
    ),
}


def read(p):
    # 原文实际编码是 GB18030（GBK 的超集）。对目前这三本书两者解码结果逐字相同
    # （已核对），但 GB18030 能覆盖 GBK 之外的字，换成它没有下行风险。
    return pathlib.Path(p).read_bytes().decode("gb18030", errors="ignore")


def sections(text, gates):
    out = []
    for p in re.split(r"<篇名>", text):
        title = p.split("\n", 1)[0].strip()
        if title in gates:
            body = p.split("\n", 1)[1] if "\n" in p else ""
            out.append((title, body.replace("属性：", "")))
    return out


def clean(lines):
    t = "\n".join(lines)
    t = t.replace("\\x", "")  # 原书用 \x按∶\x 这类标记表示强调，是转录产物，不是正文
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def chunk_chapter(body, max_len=1500, soft_min=400):
    """把一段门类正文切成粗段：优先在空行处断开，撞长度上限则强制断开。
    宁可粘连（多个病人挤一段）也不可切碎（把一个病人切两半）——精确边界现在
    交给 LLM，这里只负责切成能喂模型的大小。

    经验证：这三本书里空行很稀疏（叶天士/吴鞠通的 gates 范围内都只占 3.5%~4.2%
    的行，且不是逐病人分隔），所以实际上长度上限才是主导机制，空行只是偶尔生效
    的"顺便断在这里更好"，不要预期它能承担主要的分段职责。

    返回 list[list[str]]（每个粗段的原始行列表，未 clean），方便复用 clean()。"""
    lines = body.split("\n")
    segments, cur, cur_len = [], [], 0
    for line in lines:
        s = line.strip()
        if TAIL.match(s):
            continue  # 编者按语/结构标记，整行丢弃，不进入任何段
        if not s:
            if cur and cur_len >= soft_min:
                segments.append(cur)
                cur, cur_len = [], 0
            continue
        cur.append(s)
        cur_len += len(s)
        if cur_len >= max_len:
            segments.append(cur)
            cur, cur_len = [], 0
    if cur:
        segments.append(cur)
    return segments


def find_head_hints(text):
    hits = []
    for kind, pat in HEAD_HINT_PATTERNS:
        for m in pat.finditer(text):
            hits.append({"pos": m.start(), "matched": m.group(0).strip(), "kind": kind})
    hits.sort(key=lambda h: h["pos"])
    return hits


def find_follow_hints(text):
    hits = []
    offset = 0
    for line in text.split("\n"):
        m = FOLLOW.match(line)
        if m:
            hits.append({"pos": offset, "matched": m.group(0)})
        offset += len(line) + 1  # +1 补回 split 时吃掉的换行符
    for m in FOLLOW_KEYWORDS.finditer(text):
        hits.append({"pos": m.start(), "matched": m.group(0)})
    hits.sort(key=lambda h: h["pos"])
    return hits


def collect_segments(pid, cfg, books_dir=".", max_len=1500):
    """纯正则扫描，返回这位医家的全部粗段：
    [{seg_id, physician, text, char_len, head_hints, follow_hints}, ...]。零 LLM 调用。"""
    text = read(pathlib.Path(books_dir) / cfg["src"])
    secs = sections(text, cfg["gates"])

    if cfg.get("concat_gates"):
        combined = "\n".join(body for _, body in secs)
        chunks = chunk_chapter(combined, max_len=max_len)
    else:
        chunks = []
        for _, body in secs:
            chunks.extend(chunk_chapter(body, max_len=max_len))

    segments = []
    for i, lines in enumerate(chunks):
        t = clean(lines)
        if not t:
            continue
        segments.append({
            "seg_id": f"{pid}-{i:04d}",
            "physician": pid,
            "text": t,
            "char_len": len(t),
            "head_hints": find_head_hints(t),
            "follow_hints": find_follow_hints(t),
        })
    return segments


def print_segment_stats(pid, segments):
    n = len(segments)
    hint_counts = [len(seg["head_hints"]) for seg in segments]
    follow_counts = [len(seg["follow_hints"]) for seg in segments]
    total_hints = sum(hint_counts)
    total_follow = sum(follow_counts)
    n_ge2 = sum(1 for c in hint_counts if c >= 2)
    max_c = max(hint_counts) if hint_counts else 0
    lens = sorted(seg["char_len"] for seg in segments)
    median_len = lens[len(lens) // 2] if lens else 0

    print(f"{pid}: 粗段总数 {n}  字数中位数 {median_len}")
    print(f"  head_hints 总数 {total_hints}（总数/段数 = {total_hints / n:.2f}）")
    print(f"  head_hints>=2 的段：{n_ge2} / {n} ({n_ge2 / n * 100:.1f}%)  最大值 {max_c}")
    print(f"  follow_hints 总数 {total_follow}（总数/段数 = {total_follow / n:.2f}）")
    print("  head_hints 数量分布：")
    dist = Counter(hint_counts)
    for v in sorted(dist):
        bar = "#" * min(dist[v], 60)
        print(f"    {v:>2} : {dist[v]:>4}  {bar}")
    return {"n_segments": n, "total_head_hints": total_hints, "n_ge2": n_ge2, "max_hints": max_c}


def write_segments(pid, segments, outdir="out"):
    import json

    d = pathlib.Path(outdir) / pid
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.json"):
        f.unlink()
    for seg in segments:
        (d / f"{seg['seg_id']}.json").write_text(
            json.dumps(seg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(f"  已写出 {len(segments)} 个粗段到 {d}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从古籍原文切出粗段（病人/诊次边界交给 LLM 判断）")
    parser.add_argument("--stats-only", action="store_true",
                         help="只跑正则统计，不写文件，不需要网络/LLM")
    parser.add_argument("--books-dir", default=".",
                         help="古籍原文 txt 所在目录（默认当前目录）")
    parser.add_argument("--max-len", type=int, default=1500, help="粗段字数上限")
    args = parser.parse_args()

    for pid, cfg in BOOKS.items():
        segments = collect_segments(pid, cfg, books_dir=args.books_dir, max_len=args.max_len)
        print_segment_stats(pid, segments)
        if not args.stats_only:
            write_segments(pid, segments)
