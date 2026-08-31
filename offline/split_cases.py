"""从公版古籍切出单个医案(初诊段)。源: xiaopangxia/TCM-Ancient-Books"""
import re, pathlib, unicodedata

# 案起始: ①姓+（年龄/氏）  ②姓+空格  ③某(氏/年龄)
YE_START = re.compile(r"^[\u4e00-\u9fa5](（[^）]{1,8}）|\s)")
WU_START = re.compile(r"^[\u4e00-\u9fa5]{1,3}(氏)?\s+([一二三四五六七八九十百]+岁|[甲乙丙丁戊己庚辛壬癸][^\s]*年)")
# 复诊标记
FOLLOW = re.compile(r"^(又|[初廿卅一二三四五六七八九十]+[日月]|[正一二三四五六七八九十腊]+月)")

BOOKS = {
    "ye_tianshi": dict(src="367-临证指南医案.txt",
                       gates=["胃脘痛", "脾胃", "木乘土", "噎膈反胃", "呕吐", "痞", "痰饮", "肿胀", "积聚"],
                       start=YE_START),
    "wu_jutong":  dict(src="361-吴鞠通医案.txt",
                       gates=["胃痛", "脾胃", "呕吐", "反胃", "噎", "泄泻", "痞", "痰饮", "肿胀", "积聚", "滞下"],
                       start=WU_START),
}


def read(p):
    return pathlib.Path(p).read_bytes().decode("gbk", errors="ignore")


def sections(text, gates):
    out = []
    for p in re.split(r"<篇名>", text):
        title = p.split("\n", 1)[0].strip()
        if title in gates:
            body = p.split("\n", 1)[1] if "\n" in p else ""
            out.append((title, body.replace("属性：", "")))
    return out


def split_cases(body, start_re):
    cases, cur = [], []
    for line in body.split("\n"):
        s = line.strip()
        if not s:
            continue
        if start_re.match(s):
            if cur:
                cases.append(cur)
            cur = [s]
        elif cur:
            cur.append(s)
    if cur:
        cases.append(cur)
    return cases


TAIL = re.compile(r"^(徐评|案中|<目录>|<篇名>)")

def first_visit(lines):
    """只保留初诊段: 截断到第一个复诊标记或篇末按语之前"""
    out = []
    for i, l in enumerate(lines):
        if TAIL.match(l):
            break
        if i > 0 and FOLLOW.match(l):
            break
        if re.search(r"[（(][\u4e00-\u9fa5]{2,4}[）)]\s*$", l) and len(l) > 30 and i > 2:
            break                      # 编者署名结尾的按语
        out.append(l)
    return out


def clean(lines):
    t = "\n".join(lines)
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def ok(t):
    n = len(t)
    if not (100 <= n <= 420):
        return -1
    if t.count("。") + t.count(",") < 2:
        return -1
    if re.match(r"^(又|按)", t):          # 漏切的复诊/按语
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


def run(pid, cfg, want=15, outdir="out"):
    picked = []
    for title, body in sections(read(cfg["src"]), cfg["gates"]):
        for lines in split_cases(body, cfg["start"]):
            t = clean(first_visit(lines))
            s = ok(t)
            if s > 0:
                picked.append((s, title, t))
    picked.sort(key=lambda x: -x[0])
    per, chosen, seen = {}, [], set()
    cap = max(2, want // len(cfg["gates"]) + 2)
    for s, title, t in picked:
        if per.get(title, 0) >= cap or t[:30] in seen:
            continue
        per[title] = per.get(title, 0) + 1
        seen.add(t[:30])
        chosen.append((title, t))
        if len(chosen) >= want:
            break
    d = pathlib.Path(outdir) / pid
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.txt"):
        f.unlink()
    for i, (title, t) in enumerate(chosen, 1):
        (d / f"{i:03d}.txt").write_text(t + "\n", encoding="utf-8")
    print(f"{pid}: 候选 {len(picked)} → 采用 {len(chosen)}  门类 {per}")
    return chosen


if __name__ == "__main__":
    for pid, cfg in BOOKS.items():
        run(pid, cfg)
