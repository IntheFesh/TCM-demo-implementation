"""R8-2：把用户上传到 data/ 根目录的本地语料**规范化**进 data/local_corpora/。零 LLM 调用。

    python -m scripts.normalize_local_corpora            # 搬文件 + 转 txt + 写 MANIFEST.json
    python -m scripts.normalize_local_corpora --dry-run  # 只报要做什么

退出码：0 = 完成（**包括"没什么可做"**——这个脚本是幂等的，跑第二遍就是没事做）；
1 = 冲突（同一份语料两处内容不同），要人决定。

## 为什么要有这一步

上传上来的四个文件名是 `2_王云启(1).docx`、`脾胃论 (中医经典文库) (金·李东垣
[金·李东垣], 古聖先賢) (z-library.sk,.txt`（空格、方括号、逗号、**没闭合的括号**）——
写进任何 shell 命令都要转义，剧本里一个引号没配对整段就废。规范名（`王云启医案.docx` /
`李可医案.docx` / `脾胃论.txt`）是给剧本用的；原名、字节数、sha256、编码、来源说明
记进 `data/local_corpora/MANIFEST.json`——文件挪走之后"这是哪来的"不能只靠 git log。

## 三条规则

1. **幂等。** 目标已经在、内容一样 → 什么都不做；原文件还在 data/ 根目录（比如又
   上传了一次）且内容一样 → 删掉那份重复的；内容不一样 → 不动，打印两边的 sha256，
   退出码 1——**代码不替人决定留哪份**。
2. **`584-医学衷中参西录.txt` 是 books/ 里那本的重复**（R8 实测逐字节相同，sha256
   748281fc…）。books/ 是 split_cases 读它的地方、也是 README 3.1 说好的位置，而且
   books/ 刻意不进版本控制（古籍从公开仓库可下载）；data/ 里再放一份 1.1MB 就是把
   "不入版本控制"这条规则绕过去了。所以：books/ 有且相同 → 删 data/ 那份；
   books/ 没有 → 把 data/ 那份**搬过去**（不是复制）；两边不同 → 规则 1。
3. **docx 顺手转成 txt**（`offline.docx_to_text` 同一套转换，不另写），放在同目录
   `<规范名>.txt`，**不进版本控制**（.gitignore 里显式列了：它是派生物、可重生成，
   而且两本都是现代出版物）。剧本段 1 拿这份 txt 跑切块验证。

## 范围与十八反：只报数，判断写在表里

王云启（治癌验案录）和李可（这份 docx 全是肿瘤医案）不在本项目的脾胃门定位里。
R8 三个选项里选的是 ②：**接进来、标 `out_of_scope: true`、训练导出默认排除**
（`offline/export_sft.py --include-out-of-scope` 才带上）。选 ② 的依据是数出来的，
不是拍的：每份语料的段落里含脾胃门门类词（`core.syndrome_norm.SYNONYMS`，全项目
唯一一张门类词表）的比例、含肿瘤词（`offline.assess_case_scope.ONCOLOGY_HINTS`）的
比例，都写进 MANIFEST 的 `scope_stats`。十八反十九畏同理：每段里出现的已知药名
交给 `core.safety_output.check_incompatible`（全项目唯一实现）判，命中的段数写进
`scope_stats.incompatible_pair_paragraphs`——李可那份实测 75 段海藻甘草同用。

`out_of_scope` 和 `copyright_status` 是这张表上的**声明**，不是从数字自动推的：
数字给人看，决定由人写进 LOCAL_CORPORA（现在住在 `offline/local_corpora.py`）。
那张表上还有一个**独立**的字段 `pharmacology_source`：这份语料是不是本草/方剂参考
文献。它跟 `out_of_scope` 回答的不是同一个问题（《脾胃论》在定位内，但按空行切
方名和组成不在一块，同样不能进药理层抽取），理由写在那张表的模块文档里。
抽取引擎据此在开跑前拦住"拿医案去抽本草三元组"。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.safety_output import (  # noqa: E402
    INCOMPAT_ALIASES,
    INCOMPATIBLE_PAIRS,
    check_incompatible,
    normalize_for_incompat,
)
from core.syndrome_norm import normalize as spleen_stomach_terms  # noqa: E402
from offline.assess_case_scope import ONCOLOGY_HINTS  # noqa: E402
from offline.docx_to_text import docx_paragraphs, to_text  # noqa: E402
from offline.extract_reference_triples import split_blocks  # noqa: E402
# 声明表住在 offline/（抽取引擎也要读它来拦住"拿医案抽本草"，而 offline/ 不能
# import scripts/）。这个脚本是它唯一的写入方。
from offline.local_corpora import (  # noqa: E402
    DUPLICATE_OF_BOOKS,
    LOCAL_CORPORA,
    LOCAL_DIR_NAME,
    MANIFEST_NAME,
    CorpusSpec,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_BOOKS_DIR = ROOT / "books"

# 十八反十九畏表里出现过的所有写法（类目名 + 别名）。段落里出现哪些，就把哪些
# 交给 check_incompatible 判——"含不含反药对"的判断只有那一处实现，这里只负责
# "段落里提到了哪些已知药名"。
_KNOWN_INCOMPAT_NAMES: tuple[str, ...] = tuple(sorted(
    set(INCOMPAT_ALIASES) | {member for pair in INCOMPATIBLE_PAIRS for member in pair},
    key=len, reverse=True))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_original(data_dir: Path, spec: CorpusSpec) -> Path | None:
    hits = sorted(p for p in data_dir.iterdir()
                  if p.is_file() and p.name.startswith(spec.original_prefix)
                  and p.name.endswith(spec.suffix))
    return hits[0] if hits else None


def scope_stats(paragraphs: list[str]) -> dict:
    """三个比例，每个都带分母。判据全部复用既有实现，见模块文档。"""
    n = len(paragraphs)
    ss = sum(1 for p in paragraphs if spleen_stomach_terms(p))
    onc = sum(1 for p in paragraphs if any(h in p for h in ONCOLOGY_HINTS))
    pair_hits: dict[str, int] = {}
    n_pair_paragraphs = 0
    for p in paragraphs:
        mentioned = [name for name in _KNOWN_INCOMPAT_NAMES if name in p]
        found = check_incompatible(mentioned)
        if found:
            n_pair_paragraphs += 1
            for a, b in found:
                # check_incompatible 返回的是原始写法（"附片"「附子」「制川乌」），
                # 统计按类目名归并（都是"乌头"），否则同一对会被数成三四种
                key = " 反 ".join(sorted((normalize_for_incompat(a), normalize_for_incompat(b))))
                pair_hits[key] = pair_hits.get(key, 0) + 1
    return {
        "n_paragraphs": n,
        "spleen_stomach_paragraphs": ss,
        "spleen_stomach_rate": round(ss / n, 4) if n else None,
        "oncology_paragraphs": onc,
        "oncology_rate": round(onc / n, 4) if n else None,
        "incompatible_pair_paragraphs": n_pair_paragraphs,
        "incompatible_pairs": dict(sorted(pair_hits.items(), key=lambda kv: -kv[1])),
        "note": "比例的分母是非空段落数（docx）或空行分段的块数（txt）；"
                "脾胃门词表 = core.syndrome_norm.SYNONYMS，肿瘤词表 = "
                "offline.assess_case_scope.ONCOLOGY_HINTS，反药判定 = core.safety_output.check_incompatible",
    }


def paragraphs_of(path: Path, spec: CorpusSpec) -> list[str]:
    if spec.kind == "case_docx":
        return docx_paragraphs(path)
    return split_blocks(path.read_text(encoding=spec.encoding), "blank-line")


def _same_bytes(a: Path, b: Path) -> bool:
    return a.stat().st_size == b.stat().st_size and sha256_of(a) == sha256_of(b)


def place_file(src: Path | None, dst: Path, dry_run: bool, log: list[str]) -> bool:
    """规则 1。返回 False = 冲突（两边内容不同）。"""
    if src is None:
        if dst.exists():
            log.append(f"  = {dst.name} 已在位，原文件不在 data/ 根目录（没事做）")
            return True
        log.append(f"  ? {dst.name}：data/ 根目录和 {dst.parent.name}/ 都没有这份语料，跳过")
        return True
    if not dst.exists():
        log.append(f"  → {src.name}  ⇒  {dst.parent.name}/{dst.name}（搬走，不复制）")
        if not dry_run:
            src.rename(dst)
        return True
    if _same_bytes(src, dst):
        log.append(f"  × {src.name}：跟 {dst.parent.name}/{dst.name} 逐字节相同，删掉根目录这份重复")
        if not dry_run:
            src.unlink()
        return True
    log.append(f"  ✗ 冲突：{src.name} 与 {dst.parent.name}/{dst.name} 内容不同"
               f"（sha256 {sha256_of(src)[:12]} vs {sha256_of(dst)[:12]}），两份都没动，要人决定留哪份")
    return False


def dedupe_against_books(data_dir: Path, books_dir: Path, dry_run: bool, log: list[str]) -> bool:
    """规则 2。返回 False = 冲突。"""
    src = data_dir / DUPLICATE_OF_BOOKS
    dst = books_dir / DUPLICATE_OF_BOOKS
    if not src.exists():
        log.append(f"  = {DUPLICATE_OF_BOOKS} 不在 data/ 根目录（没事做）")
        return True
    if not dst.exists():
        log.append(f"  → {DUPLICATE_OF_BOOKS}  ⇒  {books_dir}/（books/ 里没有，搬过去；books/ 不进版本控制）")
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        return True
    if _same_bytes(src, dst):
        log.append(f"  × {DUPLICATE_OF_BOOKS}：跟 {books_dir}/ 里那本逐字节相同，删掉 data/ 这份")
        if not dry_run:
            src.unlink()
        return True
    log.append(f"  ✗ 冲突：data/{DUPLICATE_OF_BOOKS} 与 {books_dir}/{DUPLICATE_OF_BOOKS} 内容不同"
               f"（sha256 {sha256_of(src)[:12]} vs {sha256_of(dst)[:12]}），两份都没动，要人决定留哪份")
    return False


def derived_text_path(target: Path) -> Path:
    return target.with_suffix(".txt")


def write_if_changed(path: Path, content: str, dry_run: bool) -> bool:
    """幂等落盘：内容没变就不碰文件（mtime 也不动）。返回是否写了。"""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return True


def build_entry(spec: CorpusSpec, target: Path, original_name: str | None,
                derived: Path | None) -> dict:
    entry = asdict(spec)
    # 三个只在认原文件时有用的字段不进表：file 已经是规范名
    for key in ("original_prefix", "suffix", "target"):
        entry.pop(key)
    entry.update({
        # 记规范名，不记 target.name：--dry-run 时文件还没搬，target 指向的还是原文件
        "file": f"{LOCAL_DIR_NAME}/{spec.target}",
        "original_name": original_name,
        "bytes": target.stat().st_size,
        "sha256": sha256_of(target),
        "derived_text": f"{LOCAL_DIR_NAME}/{derived.name}" if derived is not None else None,
        "scope_stats": scope_stats(paragraphs_of(target, spec)),
    })
    return entry


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(data_dir: Path, books_dir: Path, dry_run: bool = False) -> int:
    local_dir = data_dir / LOCAL_DIR_NAME
    manifest_path = local_dir / MANIFEST_NAME
    old_entries = {e["file"]: e for e in load_manifest(manifest_path).get("entries", [])}
    log: list[str] = []
    ok = True
    if not dry_run:
        local_dir.mkdir(parents=True, exist_ok=True)

    ok &= dedupe_against_books(data_dir, books_dir, dry_run, log)

    entries: list[dict] = []
    for spec in LOCAL_CORPORA:
        src = find_original(data_dir, spec)
        dst = local_dir / spec.target
        ok &= place_file(src, dst, dry_run, log)
        if dry_run and src is not None and not dst.exists():
            # 干跑时文件还没搬，统计只能按原位置算
            dst = src
        if not dst.exists():
            continue
        derived = None
        if spec.kind == "case_docx":
            derived = derived_text_path(local_dir / spec.target)
            text = to_text(docx_paragraphs(dst))
            if write_if_changed(derived, text, dry_run):
                log.append(f"  ✎ {derived.name}：从 docx 重新生成（{len(text)} 字，不进版本控制）")
            else:
                log.append(f"  = {derived.name} 内容没变")
        original_name = src.name if src is not None else (
            old_entries.get(f"{LOCAL_DIR_NAME}/{spec.target}", {}).get("original_name"))
        entries.append(build_entry(spec, dst, original_name, derived))

    manifest = {
        "generated_by": "scripts/normalize_local_corpora.py（幂等；重跑不改内容就不写文件）",
        "note": "out_of_scope / copyright_status 是 LOCAL_CORPORA 里的人工声明，scope_stats 是数出来给人看的依据；"
                "两者的关系见 scripts/normalize_local_corpora.py 模块文档",
        "entries": sorted(entries, key=lambda e: e["file"]),
    }
    content = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if write_if_changed(manifest_path, content, dry_run):
        log.append(f"  ✎ {MANIFEST_NAME}：{'将' if dry_run else '已'}写出（{len(entries)} 条）")
    else:
        log.append(f"  = {MANIFEST_NAME} 内容没变")

    print("**零 LLM 调用**：搬文件 / 转 txt / 写 MANIFEST。" + ("（--dry-run：什么都没改）" if dry_run else ""))
    print("\n".join(log))
    for e in entries:
        st = e["scope_stats"]
        print(f"  {e['file']}：{st['n_paragraphs']} 段，脾胃门词 {st['spleen_stomach_paragraphs']} 段"
              f"（{st['spleen_stomach_rate']:.1%}），肿瘤词 {st['oncology_paragraphs']} 段"
              f"（{st['oncology_rate']:.1%}），反药同段 {st['incompatible_pair_paragraphs']} 段"
              f"　→ out_of_scope={e['out_of_scope']}，copyright={e['copyright_status']}")
    if not ok:
        print("有冲突（见上面 ✗ 那几行）：两份内容不同的文件都没动，人决定留哪份后再跑。", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 data/ 根目录的本地语料规范化进 data/local_corpora/（幂等）")
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--books-dir", type=Path, default=DEFAULT_BOOKS_DIR)
    ap.add_argument("--dry-run", action="store_true", help="只报要做什么，不搬、不写")
    args = ap.parse_args(argv)
    return normalize(args.data_dir, args.books_dir, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
