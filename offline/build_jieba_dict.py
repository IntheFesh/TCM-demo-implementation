"""生成 `data/jieba_dict.txt`：中医术语自定义词典，喂给 K3a 的 BM25 检索分词。

不做这一步的话，jieba 默认词典会把「脘痞」切成「脘」「痞」两个字、「嗳气泛酸」
切成「嗳气」「泛酸」——常用症状/证素/证候门类词被切碎，BM25 那一路的关键词精确
匹配就等于白搭（`core/retrieval_hybrid.py` 的模块文档字符串里有这条实测：
`list(jieba.cut("脘痞不饥"))` 未加词典时是 `['脘','痞','不','饥']`）。

三处来源，一行一词（`词 词频`）：
  1. `core.elements.ELEMENTS`（证素表，病位 + 病性）
  2. `core.syndrome_norm.SYNONYMS` 的键（证候门类词及其变体写法）
  3. `cases.json` 里出现 >=3 次的症状表述（真实语料；`cases.json` 不存在时
     跳过这一项，不编造）

用法：
    python -m offline.build_jieba_dict
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from core.elements import ELEMENTS
from core.syndrome_norm import SYNONYMS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_OUT_PATH = ROOT / "data" / "jieba_dict.txt"
MIN_SYMPTOM_FREQ = 3
# jieba 自定义词典的词频：给一个较大的固定值，只是为了让分词器倾向"整词不拆"，
# 不是在拟合真实语言模型的词频分布，不追求精确。
WORD_FREQ = 1000


def collect_words(cases_path: Path = DEFAULT_CASES_PATH) -> tuple[list[str], bool]:
    """返回 (词表, cases.json 是否参与了)。"""
    words: dict[str, None] = {}
    for w in ELEMENTS:
        words.setdefault(w, None)
    for w in SYNONYMS:
        words.setdefault(w, None)

    cases_used = cases_path.exists()
    if cases_used:
        cases = json.loads(cases_path.read_text(encoding="utf-8"))
        counter: Counter = Counter()
        for c in cases:
            for s in (c.get("symptoms") or []):
                counter[s] += 1
        for w, n in counter.items():
            if n >= MIN_SYMPTOM_FREQ:
                words.setdefault(w, None)

    # jieba 词典里放一整句症状描述没有意义、只会污染分词（把它当成一个不可分割的
    # "词"，反而让其中真正该识别的子词也识别不出来）——只收 2-8 字的短词。
    return sorted(w for w in words if 2 <= len(w) <= 8), cases_used


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="生成 jieba 自定义词典（K3a 混合检索用）")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    args = ap.parse_args(argv)

    words, cases_used = collect_words(args.cases_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(f"{w} {WORD_FREQ}" for w in words) + "\n", encoding="utf-8")

    print(f"写出 {len(words)} 个词到 {args.out}")
    print(f"  来源：ELEMENTS + SYNONYMS 键（一定有）"
          f"{'，以及 cases.json 里出现 >=3 次的症状（已用真实数据）' if cases_used else ''}")
    if not cases_used:
        print(f"  【注意】{args.cases_path} 不存在，跳过第 3 个来源。"
              "先跑 offline/extract_cases.py 生成它，再重跑本脚本能收进更多真实症状词。")


if __name__ == "__main__":
    main()
