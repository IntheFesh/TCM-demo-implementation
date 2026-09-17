"""核对落盘的 `data/standard/syndromes.jsonl` 跟**当前代码 + 当前修正表**跑出来的一致。

**为什么需要这个脚本**：R29 发现落盘的那份跟仓库里的复现命令跑出来的不一样
——3 个证型名、8 条症状列表不同，而且落盘那份里还留着「纳呆便唐」，
而 `ocr_fixes.tsv` 明明有「便唐→便溏」这一条。落盘的是旧解析器的产物，
README 的复现命令给的是新解析器，**两者之间没有任何判据**，
而且所有测试都绿（它们查的是"这条规则生效了吗"，不是"落盘的是不是这套代码的产物"）。

**为什么不能只靠 pytest**：重新抽一遍要教材 markdown，而它不在版本控制里
（`books/` 是 gitignore 的，十四五教材要另外 clone）。所以分两层：
  - pytest（`tests/test_generated_data_manifest.py`）查**能离线查的那部分**：
    jsonl 有没有被手改、修正表改了有没有重新生成、manifest 自己的计数对不对；
  - 这个脚本查那条离线查不了的：**解析器改了有没有重新生成**——它真的重跑一遍
    抽取，逐字节比 sha256。

退出码：
  0  一致
  1  不一致（jsonl 需要重新生成；输出里给命令）
  2  没有 manifest（R31 之前生成的那些）——去重新生成一次
  3  拿不到教材 markdown，**没核**（不是"核过了没问题"）

用法：
    python -m scripts.verify_generated_data \\
        --md-path /tmp/tcmds/十四五教材/中医内科学.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from offline.build_syndrome_textbook import (
    DEFAULT_OUT_PATH,
    LAYOUTS,
    MANIFEST_PATH,
    OCR_FIXES_PATH,
    PARSER_PATH,
    build_manifest,
    parse_textbook,
    read_manifest,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MD_CANDIDATES = (
    Path("/tmp/tcmds/十四五教材/中医内科学.md"),
    ROOT / "books" / "中医内科学.md",
)

REGENERATE_HINT = (
    "重新生成（会连 manifest 一起写）：\n"
    "  python - <<'PY'\n"
    "  import json, pathlib\n"
    "  p = pathlib.Path('data/standard/syndromes.jsonl')\n"
    "  keep = [l for l in p.read_text(encoding='utf-8').splitlines()\n"
    "          if l.strip() and json.loads(l).get('source') != 'textbook']\n"
    "  p.write_text(''.join(l + '\\n' for l in keep), encoding='utf-8')\n"
    "  PY\n"
    "  python -m offline.build_syndrome_textbook \\\n"
    "      --md-path <教材 markdown> --out data/standard/syndromes.jsonl --append\n"
    "  python -m offline.build_graph --all      # 图谱也跟着重建\n"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _find_md(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit if explicit.exists() else None
    for c in DEFAULT_MD_CANDIDATES:
        if c.exists():
            return c
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--md-path", type=Path, default=None,
                    help=f"教材 markdown。不传就找这几处：{', '.join(map(str, DEFAULT_MD_CANDIDATES))}")
    ap.add_argument("--layout", choices=sorted(LAYOUTS), default="neike")
    args = ap.parse_args(argv)

    manifest = read_manifest()
    if manifest is None:
        print(f"✗ 没有 {MANIFEST_PATH.name}——落盘的 jsonl 是 R31 之前生成的，"
              "没有任何东西记着它是哪一套代码的产物。")
        print(REGENERATE_HINT)
        return 2

    print(f"manifest 生成于 {manifest['generated_at']}，记录的是 {manifest['book']}"
          f"（--layout {manifest['layout']}）")

    # 先报三个 sha256 的现状，再决定退出码——**三条都要打出来**，
    # 只报"不一致"不说是哪一条不一致，等于让人从头查。
    rows = [
        ("落盘 jsonl", manifest["syndromes_sha256"], _sha256(DEFAULT_OUT_PATH)),
        ("OCR 修正表", manifest["ocr_fixes_sha256"], _sha256(OCR_FIXES_PATH)),
        ("解析器", manifest["parser_sha256"], _sha256(PARSER_PATH)),
    ]
    stale = []
    for label, recorded, actual in rows:
        ok = recorded == actual
        print(f"  {'✓' if ok else '✗'} {label}：记录 {recorded[:12]} / 现算 {actual[:12]}")
        if not ok:
            stale.append(label)

    md = _find_md(args.md_path)
    if md is None:
        print("\n⏳ 拿不到教材 markdown，**逐字节重抽这一步没核**"
              "（不是核过了没问题）。先 clone TCM_Datasets 再跑这个脚本：")
        print("  git clone --depth 1 https://github.com/PanckooAI/TCM_Datasets.git /tmp/tcmds")
        if stale:
            print(f"\n✗ 不过上面三个指纹里 {'、'.join(stale)} 已经对不上了，"
                  "落盘那份肯定要重新生成。")
            print(REGENERATE_HINT)
            return 1
        return 3

    entries, stats = parse_textbook(md, LAYOUTS[args.layout])
    fresh = build_manifest(DEFAULT_OUT_PATH, entries, stats)
    # 逐条比计数，再比整份 jsonl 的内容——计数先比是因为它能说出**差在哪**，
    # 而 sha256 只能说"不一样"。
    mismatches = [
        (k, manifest.get(k), fresh[k])
        for k in ("n_textbook", "n_duplicate_name_disease_groups", "n_suspicious",
                  "headings_bare_numbered")
        if manifest.get(k) != fresh[k]
    ]
    for k, was, now in mismatches:
        print(f"  ✗ {k}：manifest 记的是 {was}，现在重抽是 {now}")

    # 真正的判据：把重抽的结果按同样的顺序拼出来，跟落盘那份的教材部分逐字节比。
    committed = [ln for ln in DEFAULT_OUT_PATH.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    # 按解析后的 source 判，不按字面找子串——`model_dump_json()` 不带空格
    # （`"source":"textbook"`），照字面找会一条都不匹配，于是 337 行全被当成
    # 非教材条目留下来、再追加 320 行，比出来是 657 行"不一致"。第一版就是这么错的。
    non_textbook = [ln for ln in committed if json.loads(ln).get("source") != "textbook"]
    rebuilt = non_textbook + [e.model_dump_json() for e in entries]
    same = "\n".join(committed) == "\n".join(rebuilt)
    print(f"  {'✓' if same else '✗'} 逐字节重抽：{'一致' if same else '不一致'}"
          f"（落盘 {len(committed)} 行 / 重抽 {len(rebuilt)} 行）")

    if same and not mismatches and not stale:
        print("\n✓ 落盘的证候表就是当前这套代码 + 当前这张修正表的产物。")
        return 0
    print("\n✗ 落盘的那份不是当前代码的产物。")
    print(REGENERATE_HINT)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
