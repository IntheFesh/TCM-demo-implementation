"""R57 中途断了怎么续跑：合并分组分别跑出来的局部报告。

`eval.ablation.r57` 一次调用只把 `--groups` 指定的那几组写进一份完整报告
（`build_report()` 只看这次调用内存里的 `rows_by_group`，不读、不合并磁盘上
已有的报告文件）——100 次问诊（R61 加了 E 组之后，20 条 × 5 组）里断在第三
组，重跑整条命令等于前两组的钱和时间全部重花。真正的续跑方式是**按组分别
跑、分别存盘**：

    python -m eval.ablation.r57 --backend real --sdt-dir <TCMEval-SDT> \\
        --groups A --out eval/report_ablation_r57_A.json
    python -m eval.ablation.r57 --backend real --sdt-dir <TCMEval-SDT> \\
        --groups B --out eval/report_ablation_r57_B.json
    ...（C、D、E 同理，断在哪组就只重跑哪组——E 是 C 组的噪声地板复测，
    见 `eval/ablation/spec.py`）

跑完全部五组的局部文件之后用这个脚本合并：

    python -m eval.ablation.merge_r57 \\
        eval/report_ablation_r57_A.json eval/report_ablation_r57_B.json \\
        eval/report_ablation_r57_C.json eval/report_ablation_r57_D.json \\
        eval/report_ablation_r57_E.json \\
        --out eval/report_ablation_r57.json

合并不是拼 JSON 文本——是把每份局部报告里 `rows`（每条主诉的原始结果）
按组取出来，**重新跑一遍 `aggregate()`/`build_report()`**，这样硬指标是在
合并后的完整数据上算出来的，不是几份独立算好的半成品拼在一起（半成品拼接
会漏掉 `pair_consistency` 这类需要同时看两组原始行的计算）。

**几份局部文件必须来自同一份 `complaints`**（同一次 `--sdt-dir`/
`--queries-path` 选出的同 20 条主诉，`record_id` 要能一一对上）——不检查
这件事就合并，等于拿不同组的不同主诉结果硬凑成"一次消融实验"，C vs D/C vs E
的配对比较会静默地把不同主诉的结果错配在一起。这个脚本在合并前校验
`complaints` 逐条 `record_id`/`complaint` 是否一致，不一致就报错退出，
不猜、不将就。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval.ablation.r57 import build_report, to_markdown

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = ROOT / "eval" / "report_ablation_r57.json"


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"文件不在：{path}") from None
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} 不是合法 JSON：{e}") from None


def _check_same_complaints(reports: list[tuple[Path, dict]]) -> list[dict]:
    """几份局部报告必须出自同一批主诉，否则 C/D、C/E 配对比较会错配。"""
    base_path, base = reports[0]
    base_complaints = base.get("complaints") or []
    if not base_complaints:
        raise SystemExit(f"{base_path} 里没有 complaints 字段，不像是一份 r57 报告")
    for path, report in reports[1:]:
        these = report.get("complaints") or []
        if [(c.get("record_id"), c.get("complaint")) for c in these] != \
           [(c.get("record_id"), c.get("complaint")) for c in base_complaints]:
            raise SystemExit(
                f"{path} 的 complaints 跟 {base_path} 对不上——两份不是同一次"
                "跑出来的（record_id/主诉文本不一致），不能合并。检查是不是"
                "用了不同的 --sdt-dir 或数据集换过版本。")
    return base_complaints


def merge(paths: list[Path]) -> dict:
    if not paths:
        raise SystemExit("至少给一个 --in 文件")
    reports = [(p, _load(p)) for p in paths]
    complaints = _check_same_complaints(reports)

    rows_by_group: dict[str, list[dict]] = {}
    backend_info = None
    content_valid = None
    for path, report in reports:
        rows = report.get("rows") or {}
        for key, group_rows in rows.items():
            if key in rows_by_group:
                print(f"⚠ 组 {key} 在多份文件里都出现（{path} 是后一份，"
                     f"覆盖前一份）——如果这不是有意的重跑替换，请检查输入文件列表",
                     file=sys.stderr)
            rows_by_group[key] = group_rows
        this_backend = report.get("backend")
        if backend_info is None:
            backend_info = this_backend
        elif this_backend != backend_info:
            print(f"⚠ {path} 的 backend（{this_backend}）跟前面几份"
                 f"（{backend_info}）不一样——合并出的报告里 backend 字段"
                 "取第一份的，但这通常意味着几次跑用了不同后端，数字不可比",
                 file=sys.stderr)
        this_valid = report.get("content_metrics_valid")
        if content_valid is None:
            content_valid = this_valid
        elif this_valid != content_valid:
            raise SystemExit(
                f"{path} 的 content_metrics_valid={this_valid} 跟前面几份"
                f"（{content_valid}）不一样——不能把 fake 后端和 real 后端的"
                "结果混进同一份报告")

    missing = [g for g in "ABCDE" if g not in rows_by_group]
    if missing:
        print(f"⚠ 缺组：{'/'.join(missing)}——合并出的报告里这几组不存在，"
             "涉及这几组的闸门会判成「缺数据」而不是失败", file=sys.stderr)

    report = build_report(rows_by_group, backend_info=backend_info or {},
                          complaints=complaints, content_valid=bool(content_valid))
    report["merged_from"] = [str(p) for p, _ in reports]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path,
                    help="按组分别跑出来的局部报告 JSON（2~5 份，"
                         "--out 各不相同的那些文件）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--md", type=Path, default=None)
    args = ap.parse_args(argv)

    report = merge(args.inputs)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    md = args.md or args.out.with_suffix(".md")
    md.write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))
    print(f"→ {args.out}\n→ {md}")
    if report.get("content_metrics_valid") and report.get("all_gates_passed") is False:
        print("✗ 至少一条硬指标没过", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
