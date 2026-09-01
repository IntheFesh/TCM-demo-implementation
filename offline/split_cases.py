"""从公版古籍切出医案（保留完整诊次序列）。源: xiaopangxia/TCM-Ancient-Books

R1 改造：不再只保留初诊段——复诊序列（同一病人 初诊→二诊→三诊 的证型演变）是
本项目的核心研究对象，旧版 first_visit() 会把它整段丢掉。现在改成保留整案，
诊次的拆分交给 offline/extract_cases.py 里的 LLM 步骤去做（因为三本书的复诊
标记形态完全不同，正则切不干净，只能当提示用）。

--stats-only 是纯正则、零 LLM 调用的离线前置闸门：跑一遍就知道"这本书带复诊
标记的案够不够多"，不需要等到接了真实模型才发现数据不够。
"""
import argparse
import re
import pathlib
import unicodedata
from collections import Counter

# 案起始: ①姓+（年龄/氏）  ②姓+空格  ③某(氏/年龄)
YE_START = re.compile(r"^[一-龥](（[^）]{1,8}）|\s)")
WU_START = re.compile(r"^[一-龥]{1,3}(氏)?\s+([一二三四五六七八九十百]+岁|[甲乙丙丁戊己庚辛壬癸][^\s]*年)")
# 复诊标记（行首）：叶天士用「又」，吴鞠通用干支/序数日期
FOLLOW = re.compile(r"^(又|[初廿卅一二三四五六七八九十]+[日月]|[正一二三四五六七八九十腊]+月)")
# 复诊关键词（不要求行首，叙事体的书复诊常年嵌在句子中间）
FOLLOW_KEYWORDS = re.compile(r"复诊|二诊|三诊|服\S{0,3}剂")

BOOKS = {
    "ye_tianshi": dict(src="367-临证指南医案.txt",
                       gates=["胃脘痛", "脾胃", "木乘土", "噎膈反胃", "呕吐", "痞", "痰饮", "肿胀", "积聚"],
                       start=YE_START),
    "wu_jutong":  dict(src="361-吴鞠通医案.txt",
                       gates=["胃痛", "脾胃", "呕吐", "反胃", "噎", "泄泻", "痞", "痰饮", "肿胀", "积聚", "滞下"],
                       start=WU_START),
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


def split_cases(body, start_re):
    """按 start_re 找案边界。关键点：一行如果同时像案首（匹配 start_re）又像复诊
    标记（匹配 FOLLOW，比如"又 服前方"——单字"又"+空格，正好落进 YE_START 的
    "单字+空格"模式），要按复诊处理、并入当前案，而不是被误判成新案的开头。
    不这样做的后果是：叶天士的"又"字复诊行会把一个多诊病人切成好几个假案，
    其中除了第一段（真正的初诊），其余每段都只有一两行，长度不到 100 字，
    会在 ok() 的长度下限那关被直接拒掉——复诊内容看似"跑没了"，其实是在这里
    就被错误地拆散了，keep_full_case() 根本救不回来。"""
    cases, cur = [], []
    for line in body.split("\n"):
        s = line.strip()
        if not s:
            continue
        if start_re.match(s) and not FOLLOW.match(s):
            if cur:
                cases.append(cur)
            cur = [s]
        elif cur:
            cur.append(s)
    if cur:
        cases.append(cur)
    return cases


TAIL = re.compile(r"^(徐评|案中|<目录>|<篇名>)")


def keep_full_case(lines):
    """保留整案，只截断篇末按语——不再截断复诊段。复诊序列是本项目的核心研究对象，
    旧版 first_visit() 在这里把它们整段丢了，是 R1 要修的 bug。"""
    out = []
    for i, l in enumerate(lines):
        if TAIL.match(l):
            break
        if re.search(r"[（(][一-龥]{2,4}[）)]\s*$", l) and len(l) > 30 and i > 2:
            break                      # 编者署名结尾的按语
        out.append(l)
    return out


def follow_hint(lines):
    """粗算一个案里的复诊迹象强度：命中 FOLLOW 正则的行数 + 命中复诊关键词的次数。
    只是排序用的启发式分数（决定谁被优先选中），不参与 ok() 的合法性判定。
    一行同时命中两者会被计两次，这里不追求精确计数，只追求"分越高复诊迹象越强"。"""
    line_hits = sum(1 for l in lines if FOLLOW.match(l))
    text = "\n".join(lines)
    keyword_hits = len(FOLLOW_KEYWORDS.findall(text))
    return line_hits + keyword_hits


def clean(lines):
    t = "\n".join(lines)
    t = t.replace("\\x", "")  # 原书用 \x按∶\x 这类标记表示强调，是转录产物，不是正文
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def ok(t):
    n = len(t)
    if not (100 <= n <= 1200):            # 上限从 420 放宽到 1200：多诊案更长
        return -1
    if t.count("。") + t.count(",") < 2:
        return -1
    if re.match(r"^按", t):               # 只拒绝「按」开头（按语被误当成案首）
        return -1
    head = t.split("\n")[0]
    if len(head) < 12:                    # 首行过短多半是切碎了
        return -1
    # 需要有药物行: 空格分隔的多个短词
    lines = t.split("\n")
    di = next((i for i, l in enumerate(lines[1:], 1)
               if len(l.split()) >= 4 and len(l) < 100), None)
    if di is None:
        return -1
    if len("".join(lines[:di])) > 300:   # 药物行之前全是议论
        return -1
    return 200 + min(n, 400)


def collect(pid, cfg, books_dir="."):
    """纯正则扫描，返回全部满足 ok() 的候选案：(follow_hint, 质量分, 门类, 正文)。
    零 LLM 调用。"""
    src_path = pathlib.Path(books_dir) / cfg["src"]
    candidates = []
    for title, body in sections(read(src_path), cfg["gates"]):
        for lines in split_cases(body, cfg["start"]):
            full_lines = keep_full_case(lines)
            t = clean(full_lines)
            s = ok(t)
            if s > 0:
                candidates.append((follow_hint(full_lines), s, title, t))
    return candidates


def select(candidates, want, n_gates):
    """优先无上限地选出全部 follow_hint>0 的候选，不够 want 时才用 follow_hint=0
    的候选按门类上限补足。

    门类上限（cap）原本是为了在"质量分数"这一个维度下保证门类覆盖面，不让某个
    大门类把配额占满。但现在的首要目标是 follow_hint>0 的案，这类案本来就稀缺
    （比如吴鞠通"痰饮"门类集中了大部分复诊案），如果对它们也套用 cap，会把明明
    存在的复诊案挡在外面、换成没有复诊信号的案去填门类配额——这正是这一版最先
    踩到的坑，不能不修就往下走。"""
    hinted = sorted((c for c in candidates if c[0] > 0), key=lambda c: (-c[0], -c[1]))
    unhinted = sorted((c for c in candidates if c[0] == 0), key=lambda c: -c[1])

    per, chosen, seen = {}, [], set()

    def take(c):
        fh, s, title, t = c
        if t[:30] in seen:
            return
        seen.add(t[:30])
        per[title] = per.get(title, 0) + 1
        chosen.append((fh, title, t))

    for c in hinted:
        if len(chosen) >= want:
            break
        take(c)

    cap = max(2, want // n_gates + 2)
    for c in unhinted:
        if len(chosen) >= want:
            break
        if per.get(c[2], 0) >= cap:
            continue
        take(c)

    return chosen, per


def print_stats(pid, candidates, chosen, per):
    n_hinted = sum(1 for fh, _, _ in chosen if fh > 0)
    print(f"{pid}: 候选 {len(candidates)} 案 → 采用 {len(chosen)} 案  门类分布 {per}")
    print(f"  采用案里 follow_hint>0 的数量：{n_hinted} / {len(chosen)}")
    hist = Counter(fh for fh, _, _ in chosen)
    if hist:
        max_fh = max(hist)
        print("  follow_hint 分布直方图（采用案）：")
        for v in range(0, max_fh + 1):
            n = hist.get(v, 0)
            if n:
                print(f"    {v:>2} : {n:>3}  {'#' * n}")
    return n_hinted


def run(pid, cfg, want=30, outdir="out", books_dir="."):
    candidates = collect(pid, cfg, books_dir=books_dir)
    chosen, per = select(candidates, want, len(cfg["gates"]))
    print_stats(pid, candidates, chosen, per)

    d = pathlib.Path(outdir) / pid
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.txt"):
        f.unlink()
    for i, (fh, title, t) in enumerate(chosen, 1):
        (d / f"{i:03d}.txt").write_text(t + "\n", encoding="utf-8")
    print(f"  已写出 {len(chosen)} 个文件到 {d}/")
    return chosen


def stats_only(pid, cfg, want=30, books_dir="."):
    """纯正则、零 LLM 调用、不写任何文件的离线闸门检查。"""
    candidates = collect(pid, cfg, books_dir=books_dir)
    chosen, per = select(candidates, want, len(cfg["gates"]))
    return print_stats(pid, candidates, chosen, per)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从古籍原文切出医案（保留完整诊次序列）")
    parser.add_argument("--stats-only", action="store_true",
                         help="只跑正则统计，不写文件，不需要网络/LLM")
    parser.add_argument("--books-dir", default=".",
                         help="古籍原文 txt 所在目录（默认当前目录）")
    parser.add_argument("--want", type=int, default=30, help="每位医家目标采用案数")
    args = parser.parse_args()

    if args.stats_only:
        results = {}
        for pid, cfg in BOOKS.items():
            results[pid] = stats_only(pid, cfg, want=args.want, books_dir=args.books_dir)
        print("\n=== G1 闸门判据：叶天士、吴鞠通各自 follow_hint>0 的采用案 ≥25 ===")
        for pid, n in results.items():
            verdict = "PASS" if n >= 25 else "FAIL"
            print(f"  {pid}: {n}  [{verdict}]")
    else:
        for pid, cfg in BOOKS.items():
            run(pid, cfg, want=args.want, books_dir=args.books_dir)
