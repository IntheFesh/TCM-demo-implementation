"""R39：往申报书 docx 里**只增不改**地补内容，并给出「原文一个字没动」的机器校验。

    # ① 先给原件拍一份指纹（跑在**改之前**）
    python -m scripts.annotate_docx fingerprint 申报书.docx --out 申报书.fingerprint.json

    # ② 按补充清单往里加（新增段落/新增句子一律红色 E54C5E）
    python -m scripts.annotate_docx annotate 申报书.docx --additions additions.json \\
        --fingerprint 申报书.fingerprint.json --out 申报书.已补充.docx

    # ③ 校验：原有的每一个 run 都没被动过（**这一步是交付物的一部分**）
    python -m scripts.annotate_docx verify 申报书.已补充.docx \\
        --fingerprint 申报书.fingerprint.json

## 为什么要有这个脚本

用户手里那份申报书的**黑色正文一个字都不许变**。而"我小心地没动它"是一句
无法核实的话——`python-docx` 打开再保存本身就会重写整个包（XML 重排、
rsid 变化、部件顺序变化），肉眼和 `git diff` 都看不出正文有没有被改。

所以校验的口径定死在**文本层，不在文件层**，而且**按顺序比、不按下标比**：
  · 指纹 = 把补充色的 run 滤掉之后，剩下那串文字的 `sha256` **序列**；
  · 校验 = 新文件同样滤一遍，两条序列必须一字不差、一序不乱；
  · **新增的东西只能是新的 run / 新的段落**，且必须是红色——红色是"这是补的"
    的唯一标记，也是校验里区分新旧的依据。

  为什么不按 `(段落序号, run 序号)` 比：往中间插一段，后面所有段落的下标整体
  后移，按下标比会把每一段原文都报成"变了"。**一条会误报的校验，第二次就会被
  人加白名单绕过去**，那就白立了。

## 三条实现纪律

1. **不碰既有 run 的任何属性。** 改 `run.text` 会丢格式，改 `run.font` 会动样式，
   两者都会让"黑字没变"变成假话。新增内容一律 `paragraph.add_run()` 或
   `insert_paragraph_before()`，原有对象只读。
2. **不重排版。** 不动 `section` 的页边距/页眉页脚、不插分页符——那些会让
   后面所有页的排版跟着动，而"黑字没变"在读者眼里包括"它还在原来那一页"。
3. **加不进去就报错，不猜。** 锚点（要加在哪个段落后面）找不到时直接失败，
   并列出最像的三个段落。猜一个位置插进去 = 把补充内容放在错的章节下面，
   而那比没加更糟。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

#: 补充内容的颜色。总纲指定 `E54C5E`——**唯一一处定义**，脚本和报告都引它。
ADDITION_COLOR = "E54C5E"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_addition_color(run) -> bool:
    """这个 run 是不是补充色。取不到颜色（继承样式）时按"不是"处理——
    **宁可把补充误判成原文**：那样校验会更严，而不是更松。"""
    try:
        color = run.font.color
        if color is None or color.rgb is None:
            return False
        return str(color.rgb).upper() == ADDITION_COLOR.upper()
    except (AttributeError, ValueError):
        return False


def _black_sequences(doc) -> tuple[list[dict], list[dict]]:
    """文档里**非补充色**的 run 序列和段落序列，按文档顺序。

    **按顺序比，不按下标比**：往中间插一段会让后面所有段落的下标整体后移，
    而下标一移，按 `(段落号, run 号)` 比对的校验会把每一段原文都报成"变了"
    ——一条会误报的校验，第二次就会被人加白名单绕过去，那就白立了。
    真正要断言的是：**把补充内容滤掉之后，剩下的字跟原来一字不差、一序不乱。**
    """
    runs, paras = [], []
    for pi, para in enumerate(doc.paragraphs):
        kept = [r for r in para.runs if not _is_addition_color(r)]
        for r in kept:
            runs.append({"sha": _sha(r.text), "len": len(r.text),
                         "head": r.text[:12]})
        if not para.runs or kept:
            # 整段都是补充色的段落（本轮新加的那些）不参与比对；
            # 原件里本来就空的段落（排版用的空行）保留，删掉它也要能被发现。
            text = "".join(r.text for r in kept)
            paras.append({"sha": _sha(text), "len": len(text), "head": text[:16],
                          "src_index": pi})
    return runs, paras


def fingerprint(path: str | Path) -> dict:
    """给一份 docx 拍指纹。**只读**，不写回任何东西。"""
    import docx

    doc = docx.Document(str(path))
    runs, paras = _black_sequences(doc)
    n_additions = sum(1 for p in doc.paragraphs for r in p.runs
                      if _is_addition_color(r))
    return {
        "file": Path(path).name,
        "n_paragraphs": len(doc.paragraphs),
        "n_black_runs": len(runs),
        "n_additions": n_additions,
        "color": ADDITION_COLOR,
        "runs": runs,
        "paragraphs": paras,
    }


def _first_diff(old: list[dict], new: list[dict]) -> dict | None:
    """两条序列第一处对不上的地方。返回 None 表示完全一致。"""
    for i, (a, b) in enumerate(zip(old, new)):
        if a["sha"] != b["sha"]:
            return {"at": i, "was": a.get("head", ""), "now": b.get("head", ""),
                    "was_len": a["len"], "now_len": b["len"]}
    if len(old) != len(new):
        longer, which = (old, "少了") if len(old) > len(new) else (new, "多了")
        extra = longer[min(len(old), len(new))]
        return {"at": min(len(old), len(new)), "kind": which,
                "was": extra.get("head", ""), "was_len": extra["len"],
                "now": "", "now_len": 0}
    return None


def verify(path: str | Path, baseline: dict) -> dict:
    """滤掉补充色之后，剩下的字必须跟基线**一字不差、一序不乱**。

    返回 `{"ok": bool, "run_diff": ..., "paragraph_diff": ..., ...}`。
    **不抛异常**：调用方要拿到"哪一处对不上"，而不是一个 True/False。
    上一轮补的红字不算原文（它们在两边都被滤掉），允许再改。
    """
    now = fingerprint(path)
    run_diff = _first_diff(baseline["runs"], now["runs"])
    para_diff = _first_diff(baseline["paragraphs"], now["paragraphs"])
    return {
        "ok": run_diff is None and para_diff is None,
        "n_baseline_runs": len(baseline["runs"]),
        "n_now_runs": len(now["runs"]),
        "n_baseline_paragraphs": len(baseline["paragraphs"]),
        "n_now_paragraphs": len(now["paragraphs"]),
        # 两道各报各的：run 级抓"某一句被改了"，段落级抓"Word 把几个 run 合并了"
        # 这种下标会变但字没变的情况（那时 run 级可能误报、段落级仍然是绿的）。
        "run_diff": run_diff,
        "paragraph_diff": para_diff,
        "n_additions": now["n_additions"],
    }


def _find_anchor(doc, anchor: str) -> int:
    """锚点段落的序号。找不到就抛，并给出最像的三段——**不猜位置**。"""
    exact = [i for i, p in enumerate(doc.paragraphs) if p.text.strip() == anchor.strip()]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ValueError(f"锚点「{anchor}」在文档里出现了 {len(exact)} 次"
                         f"（段落 {exact}）——加个更长的锚点把它们区分开")
    contains = [i for i, p in enumerate(doc.paragraphs) if anchor.strip() in p.text]
    if len(contains) == 1:
        return contains[0]
    hint = "；".join(f"第{i}段「{doc.paragraphs[i].text[:20]}」" for i in contains[:3])
    raise ValueError(f"找不到锚点「{anchor}」。最接近的几段：{hint or '（一个都没有）'}")


def annotate(path: str | Path, additions: list[dict], out: str | Path) -> dict:
    """按清单往文档里加内容。每条 `{"anchor": ..., "text": ..., "style": 可选}`。

    `anchor` 为空串表示加在文末。**新增段落一律红色**，既有段落一个字不动。
    """
    import docx
    from docx.shared import RGBColor

    doc = docx.Document(str(path))
    added = []
    for item in additions:
        text = item.get("text", "")
        if not text.strip():
            raise ValueError(f"补充条目是空的：{item}")
        anchor = item.get("anchor", "")
        if anchor:
            idx = _find_anchor(doc, anchor)
            # **插在锚点之后**：`insert_paragraph_before` 是 python-docx 唯一
            # 提供的插入口，所以插到"锚点的下一段"之前；锚点是最后一段时退化成追加。
            if idx + 1 < len(doc.paragraphs):
                para = doc.paragraphs[idx + 1].insert_paragraph_before("")
            else:
                para = doc.add_paragraph("")
        else:
            para = doc.add_paragraph("")
        if item.get("style"):
            try:
                para.style = item["style"]
            except KeyError as e:
                raise ValueError(f"文档里没有这个样式：{item['style']}") from e
        run = para.add_run(text)
        run.font.color.rgb = RGBColor.from_string(ADDITION_COLOR)
        added.append({"anchor": anchor, "chars": len(text)})
    doc.save(str(out))
    return {"out": str(out), "n_added": len(added), "added": added,
            "color": ADDITION_COLOR}


def _cmd_fingerprint(args) -> int:
    fp = fingerprint(args.docx)
    Path(args.out).write_text(json.dumps(fp, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"{fp['file']}：{fp['n_paragraphs']} 段、{fp['n_black_runs']} 段黑字"
          f"（另有 {fp['n_additions']} 处补充色）→ {args.out}")
    return 0


def _cmd_annotate(args) -> int:
    additions = json.loads(Path(args.additions).read_text(encoding="utf-8"))
    if isinstance(additions, dict):
        additions = additions.get("additions", [])
    base = json.loads(Path(args.fingerprint).read_text(encoding="utf-8"))
    result = annotate(args.docx, additions, args.out)
    report = verify(args.out, base)
    print(f"补了 {result['n_added']} 处（{ADDITION_COLOR}）→ {result['out']}")
    _print_verify(report)
    return 0 if report["ok"] else 1


def _cmd_verify(args) -> int:
    base = json.loads(Path(args.fingerprint).read_text(encoding="utf-8"))
    report = verify(args.docx, base)
    _print_verify(report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return 0 if report["ok"] else 1


def _print_verify(report: dict) -> None:
    if report["ok"]:
        print(f"✓ 原文未被改动：滤掉补充色之后 {report['n_baseline_runs']} 段文字"
              f"逐条 hash 一致、顺序一致；本文件里的补充内容共 "
              f"{report['n_additions']} 处（{ADDITION_COLOR}）")
        return
    print("✗ 原文被改动了", file=sys.stderr)
    for label, diff in (("run", report["run_diff"]), ("段落", report["paragraph_diff"])):
        if not diff:
            continue
        kind = diff.get("kind")
        if kind:
            print(f"  {label} 序列{kind}一处（第 {diff['at']} 条）："
                  f"「{diff['was']}…」{diff['was_len']} 字", file=sys.stderr)
        else:
            print(f"  {label} 序列第 {diff['at']} 条变了："
                  f"「{diff['was']}…」{diff['was_len']} 字 → "
                  f"「{diff['now']}…」{diff['now_len']} 字", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fingerprint", help="给原件拍指纹（改之前跑）")
    f.add_argument("docx")
    f.add_argument("--out", required=True)
    f.set_defaults(func=_cmd_fingerprint)

    a = sub.add_parser("annotate", help="按清单加红色补充内容")
    a.add_argument("docx")
    a.add_argument("--additions", required=True)
    a.add_argument("--fingerprint", required=True)
    a.add_argument("--out", required=True)
    a.set_defaults(func=_cmd_annotate)

    v = sub.add_parser("verify", help="校验原文一个字没动")
    v.add_argument("docx")
    v.add_argument("--fingerprint", required=True)
    v.add_argument("--json-out", default=None)
    v.set_defaults(func=_cmd_verify)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
