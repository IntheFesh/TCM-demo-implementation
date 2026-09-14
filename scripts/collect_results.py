"""把 eval/ 下各 report 文件里的数**机读**出来，并核对 eval/RESULTS.md 没有手抄错。

存在的理由：RESULTS.md 是给人读的汇总，数字是手写进去的。手写的数会漂
——改一处忘一处、或者把 0.3348 记成 0.34，而没有任何东西会报错。这个脚本做两件事：

    python -m scripts.collect_results            # 打出文件里**实际**是什么数
    python -m scripts.collect_results --check    # 核对 RESULTS.md 引用的数跟文件一致

**核对的机制**：RESULTS.md 每行的「凭据」列写 `文件名:键=值` 这样的记号
（例如 `report_e3.json:e3.change_rate=0.335`）。核对时逐个记号去文件里取真值，
比不上就退出码 1 并同时打出两个数。**没有记号的行会被单独列出来**——那不是错误，
是"这个数在本仓库里没有文件凭据"这个事实本身，它必须是看得见的一行，而不是
默认通过。

**能查到什么、查不到什么**（说清楚比让人以为它保证了一切更有用）：
  - 能查：凭据记号里的数跟文件不一致；文件里那个键不存在；文件缺失；
    凭据写的数在同一行的正文里找不到（凭据和正文各说一套）。
  - 查不到：一个数**该**引哪个文件（凭据记号是人写的，写错了文件名会被当成
    另一个键查不到而报错，但把 E3 的凭据挂到 E4 的行上、两边都存在时查不出来）。
    也查不到文件本身是不是最新一轮跑出来的——时间戳打出来给人看，脚本不判断。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "eval"
DEFAULT_RESULTS_MD = EVAL_DIR / "RESULTS.md"

EPSILON_JSON = "epsilon.json"
E3_JSON = "report_e3.json"
E4_JSON = "report_e4.json"
E8_JSON = "report_e8.json"
E9_JSON = "report_e9.json"
SDT_LEDGER = "sdt/test_run_log.jsonl"


def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _ablation(report: dict, label: str) -> dict | None:
    for a in report.get("ablations") or []:
        if a.get("label") == label:
            return a
    return None


def _sdt_runs(rows: list[dict], solver: str, ignore_safety_veto: bool,
              partial: bool) -> list[dict]:
    """台账是**追加写**的，所以列表顺序就是时间顺序——`chain_first` / `chain_last`
    的"第一次/最后一次"靠的是这个，不是靠时间戳排序（同一天的几次跑时间戳一样，
    排序不稳定）。"""
    return [r for r in rows
            if r.get("event") == "run" and r.get("solver") == solver
            and bool(r.get("ignore_safety_veto")) is ignore_safety_veto
            and bool(r.get("partial")) is partial]


# 凭据记号能引用的键：键 → (文件相对 eval/ 的路径, 取值函数)。
# **这是唯一的注册表**——RESULTS.md 里写的 `文件名:键` 必须在这里，写错的键会
# 被当成"查不到"报错，而不是静默跳过。
EVIDENCE: dict[str, tuple[str, object]] = {
    "epsilon_online.mean": (EPSILON_JSON, lambda d: d["epsilon_online"]["mean"]),
    "epsilon_online.p50": (EPSILON_JSON, lambda d: d["epsilon_online"]["p50"]),
    "epsilon_online.p95": (EPSILON_JSON, lambda d: d["epsilon_online"]["p95"]),
    "epsilon_online.n_queries_used": (EPSILON_JSON,
                                      lambda d: d["epsilon_online"]["n_queries_used"]),
    "e3.change_rate": (E3_JSON, lambda d: _ablation(d, "swapped")["change_rate"]),
    "e4.change_rate": (E4_JSON, lambda d: _ablation(d, "none")["change_rate"]),
    "e9.change_rate": (E9_JSON, lambda d: _ablation(d, "react_on")["change_rate"]),
    "e8.output_difference_rate": (E8_JSON,
                                  lambda d: d["retriever_mode_effect"]["output_difference_rate"]),
    "hallucination.n": (E3_JSON, lambda d: d["hallucination"]["with_reference_cases"]["n"]),
    "hallucination.n_hallucinated": (
        E3_JSON, lambda d: d["hallucination"]["with_reference_cases"]["n_hallucinated"]),
    "sdt.chain_first": (SDT_LEDGER,
                        lambda rows: _sdt_runs(rows, "chain", False, False)[0]["score"]),
    "sdt.chain_last": (SDT_LEDGER,
                       lambda rows: _sdt_runs(rows, "chain", False, False)[-1]["score"]),
    "sdt.baseline": (SDT_LEDGER,
                     lambda rows: _sdt_runs(rows, "baseline", False, False)[0]["score"]),
    "sdt.ignore_safety_veto": (SDT_LEDGER,
                               lambda rows: _sdt_runs(rows, "chain", True, True)[0]["score"]),
}


def evidence_value(key: str, eval_dir: Path = EVAL_DIR):
    """凭据键 → (值, 文件路径)。值为 None 表示文件在、但那个键取不到
    （老版本的 report 没有这一项）——跟"文件不存在"分开报，两者要修的东西不同。"""
    if key not in EVIDENCE:
        raise KeyError(f"凭据键 {key!r} 不在注册表里；可用：{sorted(EVIDENCE)}")
    rel, getter = EVIDENCE[key]
    path = eval_dir / rel
    data = _load_jsonl(path) if rel.endswith(".jsonl") else _load_json(path)
    if data is None:
        return None, path
    try:
        return getter(data), path
    except (KeyError, IndexError, TypeError):
        return None, path


def collect(eval_dir: Path = EVAL_DIR) -> list[dict]:
    """每个能从文件里读出来的量一行：键、值、文件、文件的 generated_at、后端。

    后端也从文件里读，不写死 deepseek——`epsilon.json` 有 model/backend；
    report_e*.json 的 backend 是 R5 才加的（`backend_tags()`），老文件没有这一项，
    那就如实报 None 而不是替它猜一个（R5-4：猜一个默认值等于把别人跑的数标成我们的）。
    """
    rows: list[dict] = []
    for key in EVIDENCE:
        value, path = evidence_value(key, eval_dir)
        rel = EVIDENCE[key][0]
        meta = _load_jsonl(path) if rel.endswith(".jsonl") else _load_json(path)
        generated_at = backend = model = None
        if isinstance(meta, dict):
            generated_at = meta.get("generated_at")
            raw_backend = meta.get("backend")
            # epsilon.json 里 backend 是个字符串；R5 起 report_e*.json 里它是
            # backend_tags() 产出的块（`{"models": [...], "backends": [...]}`）。
            # 两种形状都认，老文件根本没有这一项就是 None——不替它猜一个。
            if isinstance(raw_backend, dict):
                backend = raw_backend.get("backends")
                model = (raw_backend.get("models") or [None])[0]
            else:
                backend = raw_backend
                model = meta.get("model")
        elif isinstance(meta, list) and meta:
            generated_at = meta[-1].get("timestamp")
            backend = meta[-1].get("backend")
            model = meta[-1].get("model")
        rows.append({
            "key": key, "value": value, "file": rel,
            "exists": path.exists(), "generated_at": generated_at,
            "model": model, "backend": backend,
        })
    return rows


def extra_notes(eval_dir: Path = EVAL_DIR) -> dict:
    """不是数字但必须跟着数字走的 caveat，原样从文件里带出来——手抄 caveat
    跟手抄数字一样会漂。"""
    out: dict = {}
    e8 = _load_json(eval_dir / E8_JSON)
    if e8 and (e8.get("retriever_mode_effect") or {}).get("graph_mode_caveat"):
        out["e8.graph_mode_caveat"] = e8["retriever_mode_effect"]["graph_mode_caveat"]
    eps = _load_json(eval_dir / EPSILON_JSON)
    if eps:
        out["epsilon.comparability_warning"] = eps.get("comparability_warning")
        out["epsilon.by_physician"] = (eps.get("epsilon_online") or {}).get("by_physician")
    return out


def epsilon_by_query(eval_dir: Path = EVAL_DIR) -> list[dict]:
    """ε 逐条主诉的地板，从 epsilon.json 的 per_query 机读。ε 按证型分层那一节
    的数字全部来自这里——那一节是整份报告里最容易被写成"大概从 0 到 0.5"的地方。

    一条主诉的地板 = 该条下所有(医家, 重复对)的 Jaccard 距离的均值，跟
    `epsilon_online.mean` 同一个口径（那个数是全部 81 个值的均值），所以逐条的数
    跟全局的数可以直接比较、能说"这条的地板是全局均值的几倍"。
    """
    eps = _load_json(eval_dir / EPSILON_JSON)
    if not eps:
        return []
    rows = []
    for q in (eps.get("epsilon_online") or {}).get("per_query") or []:
        values = [v for bp in (q.get("by_physician") or {}).values()
                  for v in (bp.get("values") or [])]
        rows.append({
            "query": q.get("query", ""),
            "skipped": bool(q.get("skipped")),
            "n_values": len(values),
            "mean": round(sum(values) / len(values), 4) if values else None,
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
        })
    return rows


def epsilon_stratification(eval_dir: Path = EVAL_DIR) -> dict:
    """一刀切用全局均值当阈值，会在多少条主诉上判错、往哪个方向判错。

    **这不是一个修辞，是一个可以算出来的数**：地板低于全局均值的主诉上，
    真实分歧在"它的地板 ~ 全局均值"之间的那些会被**漏判**（当成噪声）；
    地板高于全局均值的主诉上，噪声在"全局均值 ~ 它的地板"之间的会被**误判**
    （当成真实分歧）。两个方向的条数分开报，不合成一个"错判率"。
    """
    eps = _load_json(eval_dir / EPSILON_JSON)
    if not eps:
        return {}
    global_mean = (eps.get("epsilon_online") or {}).get("mean")
    rows = [r for r in epsilon_by_query(eval_dir) if not r["skipped"] and r["mean"] is not None]
    below = [r for r in rows if r["mean"] < global_mean]
    above = [r for r in rows if r["mean"] > global_mean]
    return {
        "global_mean": global_mean,
        "n_queries_used": len(rows),
        "n_skipped": sum(1 for r in epsilon_by_query(eval_dir) if r["skipped"]),
        "per_query_mean_min": min(r["mean"] for r in rows) if rows else None,
        "per_query_mean_max": max(r["mean"] for r in rows) if rows else None,
        "n_floor_below_global": len(below),
        "n_floor_above_global": len(above),
        "n_floor_zero": sum(1 for r in rows if r["mean"] == 0.0),
        "max_over_global": (round(max(r["mean"] for r in rows) / global_mean, 2)
                            if rows and global_mean else None),
    }


# ---------- --check：核对 RESULTS.md ----------

# 凭据记号：`文件名:键=值`。反引号可有可无（markdown 里通常包着）。
_TOKEN_RE = re.compile(r"([A-Za-z0-9_./-]+\.jsonl?):([A-Za-z0-9_.]+)=(-?\d+(?:\.\d+)?)")


def parse_evidence_tokens(text: str) -> list[dict]:
    """从 RESULTS.md 全文里抓凭据记号。按行抓，记住行号和整行内容——
    报错时要能说"哪一行"，还要能检查同一行的正文里有没有这个数。"""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _TOKEN_RE.finditer(line):
            out.append({"lineno": lineno, "line": line, "file": m.group(1),
                        "key": m.group(2), "stated": m.group(3),
                        "token": m.group(0)})
    return out


def _matches(stated: str, actual) -> bool:
    """按凭据里写的小数位数四舍五入后比较：文档里写 0.335、文件里是 0.3348，
    算一致（报告按三位小数写是有意的）。整数按整数比。"""
    if actual is None:
        return False
    if "." in stated:
        return round(float(actual), len(stated.split(".")[1])) == float(stated)
    return float(actual) == float(stated)


def check(text: str, eval_dir: Path = EVAL_DIR) -> dict:
    """返回 {ok, mismatches, unresolved, missing_in_line, checked, rows_without_evidence}。
    `rows_without_evidence` 不进 ok 的判定——没有文件凭据的行是一个要被看见的事实，
    不是一个错误（当前值那几行就是这种情况：修复后那一轮的 report 还没提交）。"""
    tokens = parse_evidence_tokens(text)
    mismatches, unresolved, missing_in_line = [], [], []
    for t in tokens:
        try:
            actual, path = evidence_value(t["key"], eval_dir)
        except KeyError as e:
            unresolved.append({**t, "reason": str(e)})
            continue
        if not path.exists():
            unresolved.append({**t, "reason": f"文件不存在：{path}"})
            continue
        if actual is None:
            unresolved.append({**t, "reason": f"{path.name} 里取不到键 {t['key']}"})
            continue
        if t["file"] != EVIDENCE[t["key"]][0]:
            unresolved.append({
                **t, "reason": f"凭据写的文件是 {t['file']}，但 {t['key']} 注册的是 "
                               f"{EVIDENCE[t['key']][0]}"})
            continue
        if not _matches(t["stated"], actual):
            mismatches.append({**t, "actual": actual})
            continue
        # 凭据说的数，同一行的正文里也得出现——否则凭据和正文各说一套
        body = t["line"].replace(t["token"], "")
        if t["stated"] not in body:
            missing_in_line.append({**t, "actual": actual})
    # 只认**编号行**（`| 3 | …` / `| 3-local | …`）为指标行。用 `[\w-]+` 会把
    # 表头（`| 凭据键 | …`）和别的说明性表格也算进来，"没有凭据的行"那张清单就被
    # 噪声淹掉，人就不看它了——一个没人看的清单等于没有。
    table_rows = [line for line in text.splitlines()
                  if re.match(r"^\| \d+(?:-\w+)? \| ", line)]
    rows_without_evidence = [line for line in table_rows if not _TOKEN_RE.search(line)]
    return {
        "ok": not (mismatches or unresolved or missing_in_line),
        "checked": len(tokens),
        "mismatches": mismatches, "unresolved": unresolved,
        "missing_in_line": missing_in_line,
        "rows_without_evidence": rows_without_evidence,
    }


def format_collected(rows: list[dict], notes: dict, strat: dict) -> str:
    lines = ["# eval/ 各 report 文件里**实际**是什么数（机读，不是手抄）", ""]
    lines.append("| 凭据键 | 值 | 文件 | 文件生成于 | model | backend |")
    lines.append("|---|---|---|---|---|---|")
    for r in rows:
        value = "（文件不存在）" if not r["exists"] else (
            "（键取不到）" if r["value"] is None else r["value"])
        lines.append(f"| `{r['key']}` | {value} | `{r['file']}` | "
                     f"{r['generated_at'] or '—'} | {r['model'] or '—'} | {r['backend'] or '—'} |")
    if strat:
        lines += ["", "## ε 分层（逐条主诉的地板 vs 全局均值）", "",
                  f"- 全局均值 {strat['global_mean']}，可用主诉 {strat['n_queries_used']} 条"
                  f"（另 {strat['n_skipped']} 条被安全否决跳过）",
                  f"- 逐条地板 {strat['per_query_mean_min']} ~ {strat['per_query_mean_max']}"
                  f"，最高的一条是全局均值的 {strat['max_over_global']} 倍",
                  f"- 地板**低于**全局均值的 {strat['n_floor_below_global']} 条"
                  f"（一刀切会在这些条上漏判真实分歧），"
                  f"**高于**的 {strat['n_floor_above_global']} 条"
                  f"（一刀切会在这些条上把噪声当分歧）",
                  f"- 地板恰为 0 的 {strat['n_floor_zero']} 条"]
    if notes:
        lines += ["", "## 跟着数字走的 caveat（原样从文件里带出来）", ""]
        for k, v in notes.items():
            lines.append(f"- `{k}`：{v}")
    return "\n".join(lines)


def format_check(result: dict) -> str:
    lines = [f"核对了 {result['checked']} 个凭据记号。"]
    for m in result["mismatches"]:
        lines.append(f"✗ 第 {m['lineno']} 行 `{m['token']}`：文件里实际是 {m['actual']}，"
                     f"文档写的是 {m['stated']}")
    for u in result["unresolved"]:
        lines.append(f"✗ 第 {u['lineno']} 行 `{u['token']}`：{u['reason']}")
    for m in result["missing_in_line"]:
        lines.append(f"✗ 第 {m['lineno']} 行：凭据写 {m['stated']}，但这一行的正文里"
                     f"找不到这个数（凭据和正文各说一套）")
    if result["ok"]:
        lines.append("✓ 全部一致。")
    n = len(result["rows_without_evidence"])
    lines.append("")
    lines.append(f"另有 {n} 行表格没有文件凭据——**这不是错误**，是「这个数在本仓库里"
                 "没有文件可核」这个事实。要让它有凭据，就把那一轮的 report 文件提交进来：")
    for line in result["rows_without_evidence"]:
        lines.append(f"  · {line[:110]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="机读 eval/ 各 report 文件的数，并核对 RESULTS.md 没抄错")
    ap.add_argument("--eval-dir", type=Path, default=EVAL_DIR)
    ap.add_argument("--check", nargs="?", type=Path, const=DEFAULT_RESULTS_MD, default=None,
                    help="核对这份 markdown 里的凭据记号（默认 eval/RESULTS.md）；不一致退出码 1")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)

    rows = collect(args.eval_dir)
    notes = extra_notes(args.eval_dir)
    strat = epsilon_stratification(args.eval_dir)

    if args.check is not None:
        result = check(args.check.read_text(encoding="utf-8"), args.eval_dir)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_check(result))
        raise SystemExit(0 if result["ok"] else 1)

    if args.json:
        print(json.dumps({"collected": rows, "notes": notes,
                          "epsilon_stratification": strat,
                          "epsilon_by_query": epsilon_by_query(args.eval_dir)},
                         ensure_ascii=False, indent=2))
        return
    print(format_collected(rows, notes, strat))


if __name__ == "__main__":
    main()
