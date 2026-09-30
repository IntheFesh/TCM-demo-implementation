"""把 eval/ 下各报告文件里的数**机读**出来，并核对文档里引用的数没有抄错。

    python -m scripts.collect_results              # 打印各报告文件里实际是什么数
    python -m scripts.collect_results --check      # 核对 eval/RESULTS.md 与 README.md；不一致退出码 1
    python -m scripts.collect_results --rerender   # 用 .json 重新渲染与之不一致的 .md 报告（零 LLM 调用）

**核对机制**：文档里的每个评测数字旁边写一个凭据记号 `文件名:键=值`（文件路径相对
eval/，例如 `report_e3.json:e3.change_rate=0.451`）。核对时逐个记号去文件里取真值，
按记号写的小数位数比较，并要求这个数同时出现在同一行的正文里——凭据和正文不许各说一套。
「凭据」表里的编号行必须带记号，缺了算漏标。

能查到的：记号里的数与文件不一致、文件里没有这个键、文件不存在、正文与记号不一致、
报告的 .md 与 .json 不是同一次渲染。查不到的：一个数该引哪个文件（记号是人写的），
以及报告文件本身是不是最新一次运行的产物——生成时间会打印出来，由人判断。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chain import (  # noqa: E402
    epsilon_floor_of_query_record, epsilon_values_in_query_record,
)

ROOT = Path(__file__).resolve().parent.parent
EVAL_DIR = ROOT / "eval"
DEFAULT_RESULTS_MD = EVAL_DIR / "RESULTS.md"
#: 需要核对的文档。README 里的评测数字与 RESULTS.md 走同一套记号、同一个核对器。
DEFAULT_CHECK_PATHS = (DEFAULT_RESULTS_MD, ROOT / "README.md")

EPSILON_JSON = "epsilon.json"
E3_JSON = "report_e3.json"
E4_JSON = "report_e4.json"
E8_JSON = "report_e8.json"
E9_JSON = "report_e9.json"
SDT_LEDGER = "sdt/test_run_log.jsonl"
#: 关闭 S3 思考时的噪声地板（`S3_THINKING=disabled python -m offline.estimate_epsilon`）。
#: 与默认设置的 ε 不可比，所以是独立的文件与独立的凭据键。
EPSILON_S3_DISABLED_JSON = "epsilon_s3_disabled.json"
#: 知识库规模的来源是数据文件本身，不在 eval/ 下；路径写成相对 eval/ 的形式，
#: 让记号里的文件名可以直接打开。
MATERIA_MEDICA_JSONL = "../data/standard/materia_medica.jsonl"
FORMULARY_JSONL = "../data/standard/formulary.jsonl"
PRESCRIBING_PATTERNS_JSONL = "../data/standard/prescribing_patterns.jsonl"


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


def _paired_verdicts(report: dict, verdict: str) -> int:
    """`divergence_per_query` 里某个判决的条数。这是**逐条配对** ε 的判决（每条主诉与
    它自己的噪声地板比较），与 `divergence_vs_epsilon` 里按全局 ε 算的数不是一回事。"""
    return sum(1 for x in (report.get("divergence_per_query") or [])
               if x.get("verdict") == verdict)


def _school(report: dict, field: str):
    return (report.get("school_pairs") or {})[field]


def _sdt_score(rows: list[dict], run: dict):
    """一次运行的分数，**必须跨两种事件取**。

    台账是两段式的（见 eval/sdt/runlog.py）：`log_run` 在运行结束时写一条 event=run，
    官方计分要到之后才执行，所以这一行的 `score` 按设计为 None；分数由之后的
    `log_scored` 以一条 event=scored 补上。回填的记录把分数直接内联在 run 行里。
    两者靠 `submission`（同一个提交文件路径）关联；同一份提交可以多次打分，取最后一条。
    """
    if run.get("score") is not None:
        return run["score"]
    scored = [r for r in rows
              if r.get("event") == "scored"
              and r.get("submission") == run.get("submission")
              and r.get("score") is not None]
    if not scored:
        # 抛 KeyError 而不是返回 None：evidence_value 会把它报成"这个键取不到"，
        # 与"文件不存在"分开——两者要修的东西不同。
        raise KeyError(
            f"运行 {run.get('submission')!r} 既没有内联分数，也没有对应的 scored 事件"
        )
    return scored[-1]["score"]


def _sdt_runs(rows: list[dict], solver: str, ignore_safety_veto: bool,
              partial: bool) -> list[dict]:
    """台账是追加写的，列表顺序就是时间顺序；`chain_first` / `chain_last` 依赖这一点，
    不按时间戳排序（同一天多次运行的时间戳可能相同）。"""
    return [r for r in rows
            if r.get("event") == "run" and r.get("solver") == solver
            and bool(r.get("ignore_safety_veto")) is ignore_safety_veto
            and bool(r.get("partial")) is partial]


# 凭据记号能引用的键：键 → (文件相对 eval/ 的路径, 取值函数)。
# **这是唯一的注册表**：文档里写的 `文件名:键` 必须在这里，写错的键报"查不到"，不会静默跳过。
EVIDENCE: dict[str, tuple[str, object]] = {
    "epsilon_online.mean": (EPSILON_JSON, lambda d: d["epsilon_online"]["mean"]),
    "epsilon_online.p50": (EPSILON_JSON, lambda d: d["epsilon_online"]["p50"]),
    "epsilon_online.p95": (EPSILON_JSON, lambda d: d["epsilon_online"]["p95"]),
    "epsilon_online.n_queries_used": (EPSILON_JSON,
                                      lambda d: d["epsilon_online"]["n_queries_used"]),
    "epsilon_online.s3_thinking": (EPSILON_JSON, lambda d: d.get("s3_thinking")),
    "e3.change_rate": (E3_JSON, lambda d: _ablation(d, "swapped")["change_rate"]),
    "e4.change_rate": (E4_JSON, lambda d: _ablation(d, "none")["change_rate"]),
    "e9.change_rate": (E9_JSON, lambda d: _ablation(d, "react_on")["change_rate"]),
    "e8.output_difference_rate": (E8_JSON,
                                  lambda d: d["retriever_mode_effect"]["output_difference_rate"]),
    "e8.p50": (E8_JSON, lambda d: d["retriever_mode_effect"]["p50"]),
    "e8.p95": (E8_JSON, lambda d: d["retriever_mode_effect"]["p95"]),
    "hallucination.n": (E3_JSON, lambda d: d["hallucination"]["with_reference_cases"]["n"]),
    "hallucination.n_hallucinated": (
        E3_JSON, lambda d: d["hallucination"]["with_reference_cases"]["n_hallucinated"]),
    "hallucination.n_without_refs": (
        E3_JSON, lambda d: d["hallucination"]["without_reference_cases"]["n"]),
    # 逐条配对 ε 的分歧判决。同一个指标在不同报告里给出不同的值（本身带重复采样的抖动），
    # 引用时要写明来自哪一份报告。
    "e3.paired_real_divergence": (E3_JSON, lambda d: _paired_verdicts(d, "real_divergence")),
    "e3.paired_within_noise": (E3_JSON, lambda d: _paired_verdicts(d, "within_noise_floor")),
    "e3.paired_unusable": (E3_JSON, lambda d: _paired_verdicts(d, "unusable")),
    "e9.paired_real_divergence": (E9_JSON, lambda d: _paired_verdicts(d, "real_divergence")),
    "e9.paired_within_noise": (E9_JSON, lambda d: _paired_verdicts(d, "within_noise_floor")),
    # 「改变率超过闸门」与「有多少样本超出各自的噪声地板」是两个不同的判据，都要能核。
    "e3.rate_above_paired_epsilon": (
        E3_JSON, lambda d: _ablation(d, "swapped")["rate_above_paired_epsilon"]),
    "e4.rate_above_paired_epsilon": (
        E4_JSON, lambda d: _ablation(d, "none")["rate_above_paired_epsilon"]),
    "e9.rate_above_paired_epsilon": (
        E9_JSON, lambda d: _ablation(d, "react_on")["rate_above_paired_epsilon"]),
    "safety_veto.n_vetoed": (E3_JSON, lambda d: d["safety_veto"]["n_vetoed"]),
    "safety_veto.n_queries": (E3_JSON, lambda d: d["safety_veto"]["n_queries"]),
    # E2（师承内 vs 跨学派）在两份报告里给出方向相反的结论，两份都注册、都报出。
    "e8.school_lineage_mean": (E8_JSON, lambda d: _school(d, "lineage_mean")),
    "e8.school_cross_mean": (E8_JSON, lambda d: _school(d, "cross_school_mean")),
    "e8.school_n_cross_gt_lineage": (E8_JSON,
                                     lambda d: _school(d, "n_cross_school_gt_lineage")),
    "e9.school_lineage_mean": (E9_JSON, lambda d: _school(d, "lineage_mean")),
    "e9.school_cross_mean": (E9_JSON, lambda d: _school(d, "cross_school_mean")),
    "e9.school_n_cross_gt_lineage": (E9_JSON,
                                     lambda d: _school(d, "n_cross_school_gt_lineage")),
    "sdt.chain_first": (SDT_LEDGER,
                        lambda rows: _sdt_score(rows, _sdt_runs(rows, "chain", False, False)[0])),
    "sdt.chain_last": (SDT_LEDGER,
                       lambda rows: _sdt_score(rows, _sdt_runs(rows, "chain", False, False)[-1])),
    "sdt.baseline": (SDT_LEDGER,
                     lambda rows: _sdt_score(rows, _sdt_runs(rows, "baseline", False, False)[0])),
    "sdt.ignore_safety_veto": (SDT_LEDGER,
                               lambda rows: _sdt_score(rows, _sdt_runs(rows, "chain", True, True)[0])),
    # ε 分层：每个数都从 epsilon.json 的 per_query 现算（见 epsilon_stratification）。
    "epsilon.stratification.global_mean": (
        EPSILON_JSON, lambda d: _strat(d)["global_mean"]),
    "epsilon.stratification.n_queries_used": (
        EPSILON_JSON, lambda d: _strat(d)["n_queries_used"]),
    "epsilon.stratification.per_query_mean_min": (
        EPSILON_JSON, lambda d: _strat(d)["per_query_mean_min"]),
    "epsilon.stratification.per_query_mean_max": (
        EPSILON_JSON, lambda d: _strat(d)["per_query_mean_max"]),
    "epsilon.stratification.n_floor_below_global": (
        EPSILON_JSON, lambda d: _strat(d)["n_floor_below_global"]),
    "epsilon.stratification.n_floor_above_global": (
        EPSILON_JSON, lambda d: _strat(d)["n_floor_above_global"]),
    "epsilon.stratification.max_over_global": (
        EPSILON_JSON, lambda d: _strat(d)["max_over_global"]),
    "epsilon_s3_disabled.mean": (
        EPSILON_S3_DISABLED_JSON, lambda d: d["epsilon_online"]["mean"]),
    "epsilon_s3_disabled.p95": (
        EPSILON_S3_DISABLED_JSON, lambda d: d["epsilon_online"]["p95"]),
    "epsilon_s3_disabled.llm_calls": (
        EPSILON_S3_DISABLED_JSON, lambda d: d["epsilon_online"]["llm_calls"]),
    "epsilon_s3_disabled.s3_thinking": (
        EPSILON_S3_DISABLED_JSON, lambda d: d.get("s3_thinking")),
    # 知识库规模：来源是数据文件本身。
    "pharmacology.n_materia_medica": (MATERIA_MEDICA_JSONL, len),
    "pharmacology.n_formulary": (FORMULARY_JSONL, len),
    "patterns.n_patterns": (PRESCRIBING_PATTERNS_JSONL, len),
    "patterns.n_dose": (PRESCRIBING_PATTERNS_JSONL,
                        lambda d: sum(1 for r in d if r.get("kind") == "dose")),
    "patterns.n_physician_syndrome": (
        PRESCRIBING_PATTERNS_JSONL,
        lambda d: sum(1 for r in d if r.get("group_by") == "physician_syndrome")),
    "patterns.max_support": (PRESCRIBING_PATTERNS_JSONL,
                             lambda d: max((r.get("support") or 0) for r in d)),
}


def _strat(eps: dict) -> dict:
    """凭据取值函数用的分层算子；算法在 `_stratification_from`。"""
    return _stratification_from(eps, _rows_from_epsilon(eps))


def evidence_value(key: str, eval_dir: Path = EVAL_DIR):
    """凭据键 → (值, 文件路径)。值为 None 表示文件在、但这个键取不到，
    与"文件不存在"分开报——两者要修的东西不同。"""
    if key not in EVIDENCE:
        raise KeyError(f"凭据键 {key!r} 不在注册表里（scripts/collect_results.py 的 EVIDENCE）")
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
    """每个能从文件里读出来的量一行：键、值、文件、文件的生成时间、模型与后端。
    模型与后端也从文件里读；文件里没有就如实报 None，不替它猜。"""
    rows: list[dict] = []
    for key in EVIDENCE:
        value, path = evidence_value(key, eval_dir)
        rel = EVIDENCE[key][0]
        meta = _load_jsonl(path) if rel.endswith(".jsonl") else _load_json(path)
        generated_at = backend = model = None
        if isinstance(meta, dict):
            generated_at = meta.get("generated_at")
            raw_backend = meta.get("backend")
            # epsilon.json 里 backend 是字符串；run_eval 的报告里是 backend_tags() 产出的块。
            if isinstance(raw_backend, dict):
                backend = raw_backend.get("backends")
                model = (raw_backend.get("models") or [None])[0]
            else:
                backend = raw_backend
                model = meta.get("model")
        elif isinstance(meta, list) and meta and isinstance(meta[-1], dict):
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
    """不是数字、但必须跟着数字走的说明，原样从文件里带出来。"""
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
    """ε 逐条主诉的地板，从 epsilon.json 的 per_query 机读。

    一条主诉的地板 = 该条下所有（医家，重复对）Jaccard 距离的均值，与
    `epsilon_online.mean` 同一口径，所以逐条的数可以直接与全局均值比较。
    摊平 per_query 的三层结构只在 core/chain.py 一处实现。
    """
    return _rows_from_epsilon(_load_json(eval_dir / EPSILON_JSON))


def _stratification_from(eps: dict | None, rows: list[dict]) -> dict:
    """ε 分层的算法本体，与读文件分开：凭据取值函数拿到的是已经加载好的 dict。"""
    if not eps:
        return {}
    global_mean = (eps.get("epsilon_online") or {}).get("mean")
    usable = [r for r in rows if not r["skipped"] and r["mean"] is not None]
    below = [r for r in usable if r["mean"] < global_mean]
    above = [r for r in usable if r["mean"] > global_mean]
    return {
        "global_mean": global_mean,
        "n_queries_used": len(usable),
        "n_skipped": sum(1 for r in rows if r["skipped"]),
        "per_query_mean_min": min(r["mean"] for r in usable) if usable else None,
        "per_query_mean_max": max(r["mean"] for r in usable) if usable else None,
        "n_floor_below_global": len(below),
        "n_floor_above_global": len(above),
        "n_floor_zero": sum(1 for r in usable if r["mean"] == 0.0),
        "max_over_global": (round(max(r["mean"] for r in usable) / global_mean, 2)
                            if usable and global_mean else None),
    }


def _rows_from_epsilon(eps: dict | None) -> list[dict]:
    """per_query → 逐条地板的行。摊平走 core/chain.py 那一处。"""
    if not eps:
        return []
    rows = []
    for q in (eps.get("epsilon_online") or {}).get("per_query") or []:
        values = epsilon_values_in_query_record(q)
        rows.append({
            "query": q.get("query", ""),
            "skipped": bool(q.get("skipped")),
            "n_values": len(values),
            "mean": epsilon_floor_of_query_record(q),
            "min": round(min(values), 4) if values else None,
            "max": round(max(values), 4) if values else None,
        })
    return rows


def epsilon_stratification(eval_dir: Path = EVAL_DIR) -> dict:
    """用全局均值一刀切当阈值时，会在多少条主诉上判错、往哪个方向判错。

    地板低于全局均值的主诉上，落在"它的地板 ~ 全局均值"之间的真实分歧会被**漏判**；
    地板高于全局均值的主诉上，落在"全局均值 ~ 它的地板"之间的噪声会被**误判**为分歧。
    两个方向的条数分开报，不合成一个错判率。
    """
    eps = _load_json(eval_dir / EPSILON_JSON)
    return _stratification_from(eps, _rows_from_epsilon(eps))


# ---------- --check：核对文档 ----------

# 凭据记号：`文件名:键=值`。反引号可有可无（markdown 里通常包着）。
_TOKEN_RE = re.compile(r"([A-Za-z0-9_./-]+\.jsonl?):([A-Za-z0-9_.]+)=(-?\d+(?:\.\d+)?)")


def parse_evidence_tokens(text: str) -> list[dict]:
    """按行抓凭据记号，记住行号与整行内容：报错时要能说出是哪一行，
    还要检查同一行的正文里有没有这个数。"""
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _TOKEN_RE.finditer(line):
            out.append({"lineno": lineno, "line": line, "file": m.group(1),
                        "key": m.group(2), "stated": m.group(3),
                        "token": m.group(0)})
    return out


def _matches(stated: str, actual) -> bool:
    """按记号里写的小数位数四舍五入后比较：文档写 0.451、文件里是 0.4508，算一致。
    整数按整数比。"""
    if actual is None:
        return False
    if "." in stated:
        return round(float(actual), len(stated.split(".")[1])) == float(stated)
    return float(actual) == float(stated)


_MD_TITLE_TS_RE = re.compile(r"^#\s.*（(.+?)）\s*$")
_NUMBERED_ROW_RE = re.compile(r"^\| \d+(?:-\w+)? \| ")
# 分隔行：`|---|---|`。markdown 允许 `:---:` 对齐写法，所以只要求有 `---`。
_SEPARATOR_ROW_RE = re.compile(r"^\|[\s:|-]*-{3,}[\s:|-]*\|?\s*$")


def metric_rows(text: str) -> list[str]:
    """哪些表格行算「指标行」：只认表头里有「凭据」列的那张表里的编号行。
    别的表格（例如普通的编号清单）一律不管，它们本来也不该有凭据列。"""
    rows: list[str] = []
    in_metric_table = False
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _is_metric_table_header(lines, i):
            in_metric_table = True
            continue
        if not stripped.startswith("|"):
            in_metric_table = False
            continue
        if in_metric_table and _NUMBERED_ROW_RE.match(line):
            rows.append(line)
    return rows


def _is_metric_table_header(lines: list[str], i: int) -> bool:
    """第 i 行是不是带凭据列的那张表的**表头**：含「凭据」，且下一行是分隔行。
    正文单元格里提到「凭据」二字的行不满足后一个条件。"""
    stripped = lines[i].strip()
    if not (stripped.startswith("|") and "凭据" in stripped):
        return False
    if "---" in stripped:
        return False
    nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
    return bool(_SEPARATOR_ROW_RE.match(nxt))


def check_md_json_sync(eval_dir: Path = EVAL_DIR) -> list[dict]:
    """每份 `report_e*.json` 旁边的 `.md` 是它的渲染产物，标题里带 `generated_at`。
    两者对不上，说明一份旧的人读报告放在一份新的数据旁边——凭据记号只核 json 里的数，
    所以单独检查。"""
    out = []
    for name in (E3_JSON, E4_JSON, E8_JSON, E9_JSON):
        j, m = eval_dir / name, eval_dir / name.replace(".json", ".md")
        if not j.exists() or not m.exists():
            continue
        data = _load_json(j)
        json_ts = (data or {}).get("generated_at")
        first = m.read_text(encoding="utf-8").splitlines()[0] if m.stat().st_size else ""
        match = _MD_TITLE_TS_RE.match(first.strip())
        md_ts = match.group(1) if match else None
        if md_ts != json_ts:
            out.append({"json": name, "md": m.name, "json_ts": json_ts, "md_ts": md_ts})
    return out


def check(text: str, eval_dir: Path = EVAL_DIR) -> dict:
    """返回 {ok, checked, mismatches, unresolved, missing_in_line, md_json_drift, rows_unmarked}。"""
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
        body = t["line"].replace(t["token"], "")
        if t["stated"] not in body:
            missing_in_line.append({**t, "actual": actual})
    unmarked = [line for line in metric_rows(text) if not _TOKEN_RE.search(line)]
    md_drift = check_md_json_sync(eval_dir)
    return {
        "ok": not (mismatches or unresolved or missing_in_line or md_drift or unmarked),
        "checked": len(tokens),
        "mismatches": mismatches, "unresolved": unresolved,
        "missing_in_line": missing_in_line,
        "md_json_drift": md_drift,
        # 凭据表里的编号行没有记号：漏标，算失败
        "rows_unmarked": unmarked,
    }


def rerender_drifted_md(eval_dir: Path = EVAL_DIR) -> list[dict]:
    """把与自己的 `.json` 对不上的 `.md` 重新渲染一遍。零 LLM 调用：md 是 json 的
    确定性渲染产物。渲染逻辑只在 `eval.run_eval.render_markdown` 一处；在函数里
    import，因为那个模块顶层会加载 core.chain。"""
    from eval.run_eval import render_markdown

    done = []
    for d in check_md_json_sync(eval_dir):
        path = eval_dir / d["json"]
        report = _load_json(path)
        try:
            text = render_markdown(report)
        except KeyError as e:
            done.append({**d, "rendered": False, "reason": f"渲染缺键 {e}"})
            continue
        (eval_dir / d["md"]).write_text(text, encoding="utf-8")
        done.append({**d, "rendered": True, "reason": None})
    return done


def format_collected(rows: list[dict], notes: dict, strat: dict) -> str:
    lines = ["# eval/ 各报告文件里的实际数值", ""]
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
        lines += ["", "## 跟着数字走的说明（原样从文件里带出来）", ""]
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
    for d in result.get("md_json_drift") or []:
        lines.append(f"✗ {d['md']} 是 {d['md_ts']} 生成的，而 {d['json']} 是 "
                     f"{d['json_ts']}——人读报告与数据不是同一次渲染。"
                     f"重渲染：python -m scripts.collect_results --rerender")
    for line in result.get("rows_unmarked") or []:
        lines.append(f"✗ 凭据表里的这一行没有凭据记号：{line[:110]}")
    if result["ok"]:
        lines.append("✓ 全部一致。")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="机读 eval/ 各报告文件的数，并核对文档里的凭据记号")
    ap.add_argument("--eval-dir", type=Path, default=EVAL_DIR)
    ap.add_argument("--check", nargs="*", type=Path, default=None,
                    help="核对这些 markdown 里的凭据记号；不给路径就核对 eval/RESULTS.md "
                         "和 README.md（不一致退出码 1）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--rerender", action="store_true",
                    help="把与自己的 .json 对不上的 report_e*.md 重新渲染一遍（零 LLM 调用）")
    args = ap.parse_args(argv)

    if args.rerender:
        done = rerender_drifted_md(args.eval_dir)
        if not done:
            print("没有需要重渲染的 md（每份 .md 的时间戳都与它的 .json 一致）")
        for d in done:
            if d["rendered"]:
                print(f"已重渲染 {d['md']}：{d['md_ts']} → {d['json_ts']}")
            else:
                print(f"✗ {d['md']} 渲染失败：{d['reason']}")
        raise SystemExit(0 if all(d["rendered"] for d in done) else 1)

    rows = collect(args.eval_dir)
    notes = extra_notes(args.eval_dir)
    strat = epsilon_stratification(args.eval_dir)

    if args.check is not None:
        paths = args.check or DEFAULT_CHECK_PATHS
        results = {}
        for path in paths:
            results[str(path)] = check(path.read_text(encoding="utf-8"), args.eval_dir)
        if args.json:
            print(json.dumps(results, ensure_ascii=False, indent=2))
        else:
            for name, result in results.items():
                print(f"—— {name} ——")
                print(format_check(result))
                print()
        raise SystemExit(0 if all(r["ok"] for r in results.values()) else 1)

    if args.json:
        print(json.dumps({"collected": rows, "notes": notes,
                          "epsilon_stratification": strat,
                          "epsilon_by_query": epsilon_by_query(args.eval_dir)},
                         ensure_ascii=False, indent=2))
        return
    print(format_collected(rows, notes, strat))


if __name__ == "__main__":
    main()
