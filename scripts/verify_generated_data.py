"""核对落盘的 `data/standard/syndromes.jsonl` 是**当前代码 + 当前修正表**的产物。

落盘的证候表是生成物。解析器或修正表改了而没有重新生成，落盘那份就与仓库里的复现
命令跑出来的不一样，而单元测试查的是"这条规则生效了吗"，查不出"落盘的是不是这套代码的
产物"。`data/standard/syndromes_manifest.json` 记录生成时的三个指纹，这个脚本逐一比对。

分两层，因为重新抽取需要教材 markdown，而它不随仓库分发（来源见 data/SOURCES.md）：
  - pytest（`tests/test_generated_data_manifest.py`）查离线能查的部分：
    jsonl 有没有被手改、修正表改了有没有重新生成、manifest 自己的计数对不对；
  - 这个脚本另外比对解析器代码的指纹（忽略注释与文档字符串，见
    `offline/code_fingerprint.py`），有教材 markdown 时再重跑一遍抽取，逐字节比较。

退出码：
  0  一致
  1  不一致（jsonl 需要重新生成；输出里给命令）
  2  没有 manifest——重新生成一次
  3  拿不到教材 markdown，逐字节重抽没核（不是"核过了没问题"）

用法：
    python -m scripts.verify_generated_data \\
        --md-path /path/to/TCM_Datasets/十四五教材/中医内科学.md
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from offline.code_fingerprint import code_fingerprint
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
    # 两个路径可覆盖：测试核对 tmp_path 里的副本，不原地改版本控制里的文件
    # （原地改再恢复的做法，一旦运行被打断，文件就留在坏状态里）。
    ap.add_argument("--manifest", type=Path, default=None,
                    help="要核的 manifest（默认 data/standard/syndromes_manifest.json）")
    ap.add_argument("--jsonl", type=Path, default=None,
                    help="要核的 syndromes.jsonl（默认 data/standard/syndromes.jsonl）")
    args = ap.parse_args(argv)

    manifest_path = args.manifest or MANIFEST_PATH
    jsonl_path = args.jsonl or DEFAULT_OUT_PATH

    manifest = read_manifest(manifest_path)
    if manifest is None:
        print(f"✗ 没有 {manifest_path.name}——没有任何记录说明落盘的 jsonl 是哪一套代码的产物。")
        print(REGENERATE_HINT)
        return 2

    print(f"manifest 生成于 {manifest['generated_at']}，记录的是 {manifest['book']}"
          f"（--layout {manifest['layout']}）")

    # 先报三个指纹的现状，再决定退出码：三条都打出来，说清是哪一条不一致。
    rows = [
        ("落盘 jsonl", manifest["syndromes_sha256"], _sha256(jsonl_path)),
        ("OCR 修正表", manifest["ocr_fixes_sha256"], _sha256(OCR_FIXES_PATH)),
        ("解析器", manifest["parser_sha256"], code_fingerprint(PARSER_PATH)),
    ]
    stale = []
    for label, recorded, actual in rows:
        ok = recorded == actual
        print(f"  {'✓' if ok else '✗'} {label}：记录 {recorded[:12]} / 现算 {actual[:12]}")
        if not ok:
            stale.append(label)

    md = _find_md(args.md_path)
    if md is None:
        print("\n注意：拿不到教材 markdown，**逐字节重抽这一步没核**"
              "（不是核过了没问题）。获取 TCM_Datasets 之后用 --md-path 指向教材再跑：")
        print("  git clone --depth 1 https://github.com/PanckooAI/TCM_Datasets.git /path/to/TCM_Datasets")
        print("  python -m scripts.verify_generated_data "
              "--md-path /path/to/TCM_Datasets/十四五教材/中医内科学.md")
        if stale:
            print(f"\n✗ 不过上面三个指纹里 {'、'.join(stale)} 已经对不上了，"
                  "落盘那份肯定要重新生成。")
            print(REGENERATE_HINT)
            return 1
        return 3

    entries, stats = parse_textbook(md, LAYOUTS[args.layout])
    fresh = build_manifest(jsonl_path, entries, stats)
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
    committed = [ln for ln in jsonl_path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    # 按解析后的 source 判，不按字面找子串：`model_dump_json()` 不带空格
    # （`"source":"textbook"`），照字面找会一条都不匹配。
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
