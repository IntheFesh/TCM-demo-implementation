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
# README 里的评测数字也要能被核。**同一套凭据记号，同一个核对器**——README 手抄一份
# 数字出来漂了，跟 RESULTS.md 漂了是同一个问题，不该有两套机制。
DEFAULT_CHECK_PATHS = (DEFAULT_RESULTS_MD, ROOT / "README.md")

EPSILON_JSON = "epsilon.json"
E3_JSON = "report_e3.json"
E4_JSON = "report_e4.json"
E8_JSON = "report_e8.json"
E9_JSON = "report_e9.json"
SDT_LEDGER = "sdt/test_run_log.jsonl"
# 修复前那一轮的四份 report。它们被修复后的那一轮**覆盖**了（f1d5520），所以从
# git 历史里取回来归档——「修复前 0.335 → 修复后 0.451」是这个项目最有说服力的
# 叙事之一，两端都该可核，不能一端有文件一端只有一句话。
ARCHIVE_2026_09_12 = "archive/2026-09-12"


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
    """`divergence_per_query` 里某个判决的条数。这一段是**逐条配对** ε 的判决
    （每条主诉跟它自己的地板比），跟 `divergence_vs_epsilon` 里拿全局 ε 一刀切
    算出来的数不是一回事——两者在同一份文件里都有，引用时必须说清是哪一个。"""
    return sum(1 for x in (report.get("divergence_per_query") or [])
               if x.get("verdict") == verdict)


def _school(report: dict, field: str):
    return (report.get("school_pairs") or {})[field]


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
    # 逐条配对 ε 的分歧判决（RESULTS.md 第 2 行）。**同一个指标在四份 report 里
    # 给出 9/9、9/9、8/9、8/9 四个值**——它本身就带重复采样的抖动，所以第 2 行
    # 引用时必须点明是哪一轮的哪一份。
    "e3.paired_real_divergence": (E3_JSON, lambda d: _paired_verdicts(d, "real_divergence")),
    "e3.paired_within_noise": (E3_JSON, lambda d: _paired_verdicts(d, "within_noise_floor")),
    "e3.paired_unusable": (E3_JSON, lambda d: _paired_verdicts(d, "unusable")),
    "e9.paired_real_divergence": (E9_JSON, lambda d: _paired_verdicts(d, "real_divergence")),
    "e9.paired_within_noise": (E9_JSON, lambda d: _paired_verdicts(d, "within_noise_floor")),
    "archive.e3.paired_real_divergence": (
        f"{ARCHIVE_2026_09_12}/{E3_JSON}", lambda d: _paired_verdicts(d, "real_divergence")),
    "archive.e3.paired_within_noise": (
        f"{ARCHIVE_2026_09_12}/{E3_JSON}", lambda d: _paired_verdicts(d, "within_noise_floor")),
    # 闸门是否通过（gate_pass）不是数字，但 rate_above_paired_epsilon 是——
    # 「改变率超过闸门」和「有多少条超出各自的噪声地板」是两个不同的判据，都要能核
    "e3.rate_above_paired_epsilon": (
        E3_JSON, lambda d: _ablation(d, "swapped")["rate_above_paired_epsilon"]),
    "e4.rate_above_paired_epsilon": (
        E4_JSON, lambda d: _ablation(d, "none")["rate_above_paired_epsilon"]),
    "e9.rate_above_paired_epsilon": (
        E9_JSON, lambda d: _ablation(d, "react_on")["rate_above_paired_epsilon"]),
    "e8.p50": (E8_JSON, lambda d: d["retriever_mode_effect"]["p50"]),
    "e8.p95": (E8_JSON, lambda d: d["retriever_mode_effect"]["p95"]),
    "safety_veto.n_vetoed": (E3_JSON, lambda d: d["safety_veto"]["n_vetoed"]),
    "safety_veto.n_queries": (E3_JSON, lambda d: d["safety_veto"]["n_queries"]),
    # E2（总纲 1.3）师承内 vs 跨学派。**同一个指标在两份 report 里给出不同的值**
    # （e8 那一轮 holds=false，e9 那一轮 holds=true），所以两份都注册、两份都报——
    # 只挑一份写进文档就是在挑对自己有利的那一次。见 RESULTS.md 第 9 行。
    "e8.school_lineage_mean": (E8_JSON, lambda d: _school(d, "lineage_mean")),
    "e8.school_cross_mean": (E8_JSON, lambda d: _school(d, "cross_school_mean")),
    "e8.school_n_cross_gt_lineage": (E8_JSON,
                                     lambda d: _school(d, "n_cross_school_gt_lineage")),
    "e9.school_lineage_mean": (E9_JSON, lambda d: _school(d, "lineage_mean")),
    "e9.school_cross_mean": (E9_JSON, lambda d: _school(d, "cross_school_mean")),
    "e9.school_n_cross_gt_lineage": (E9_JSON,
                                     lambda d: _school(d, "n_cross_school_gt_lineage")),
    # 归档的修复前那一轮（= 上面四个指标「修复前 / 旧检索」那一列的凭据）
    "archive.e3.change_rate": (f"{ARCHIVE_2026_09_12}/{E3_JSON}",
                               lambda d: _ablation(d, "swapped")["change_rate"]),
    "archive.e4.change_rate": (f"{ARCHIVE_2026_09_12}/{E4_JSON}",
                               lambda d: _ablation(d, "none")["change_rate"]),
    "archive.e9.change_rate": (f"{ARCHIVE_2026_09_12}/{E9_JSON}",
                               lambda d: _ablation(d, "react_on")["change_rate"]),
    "archive.e8.output_difference_rate": (
        f"{ARCHIVE_2026_09_12}/{E8_JSON}",
        lambda d: d["retriever_mode_effect"]["output_difference_rate"]),
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
# 没有凭据记号但**刻意**如此的标记。⏳ = 真机数据还不存在（不是文件丢了，见
# RESULTS.md「凭据的三种状态」）；见上表 = 这一行是上面某行的示意副本。
# 两者之外的空凭据列算**漏标**并让 --check 失败：一个新加的行如果凭据列留空，
# 它读起来跟有凭据的行一样，而没有任何东西会提醒。
_PENDING_MARKERS = ("⏳", "见上表")


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


_MD_TITLE_TS_RE = re.compile(r"^#\s.*（(.+?)）\s*$")
_NUMBERED_ROW_RE = re.compile(r"^\| \d+(?:-\w+)? \| ")


def metric_rows(text: str) -> list[str]:
    r"""哪些表格行算「指标行」——**只认带「凭据」表头的那张表里的编号行**。

    判据一路收紧过两次，每次都是被真实的误伤逼的：
      1. 最初 `^\| [\w-]+ \| ` 把表头（`| 凭据键 | …`）也算进来；
      2. 改成编号行之后，别的文档里「1/2/3」开头的普通表格（README 的已知局限清单
         就是）又会被当成漏标凭据的指标行。
    所以现在的判据是结构性的：表头里有「凭据」这一列 → 这张表是指标表 → 表里的
    编号行是指标行。别的表格一律不管，它们本来也不该有凭据列。
    """
    rows: list[str] = []
    in_metric_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and "凭据" in stripped and "---" not in stripped:
            in_metric_table = True
            continue
        if not stripped.startswith("|"):
            in_metric_table = False
            continue
        if in_metric_table and _NUMBERED_ROW_RE.match(line):
            rows.append(line)
    return rows


def check_md_json_sync(eval_dir: Path = EVAL_DIR) -> list[dict]:
    """每份 `report_e*.json` 旁边的 `.md` 是它的渲染产物，标题里带 `generated_at`。
    两者对不上 = **一份旧的人读报告躺在一份新的数据旁边**，而人只会读 md。

    这个检查是被真实情况逼出来的：修复后那一轮提交进来时，`report_e8.md` /
    `report_e9.md` 没跟着同步（只同步了 e3/e4），于是 2026-09-12 的 md 躺在
    2026-09-13 的 json 旁边。凭据记号只管 json 里的数，管不到 md——所以要单独查。
    """
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
    no_token = [line for line in metric_rows(text) if not _TOKEN_RE.search(line)]
    pending = [line for line in no_token if any(m in line for m in _PENDING_MARKERS)]
    unmarked = [line for line in no_token if line not in pending]
    md_drift = check_md_json_sync(eval_dir)
    return {
        "ok": not (mismatches or unresolved or missing_in_line or md_drift or unmarked),
        "checked": len(tokens),
        "mismatches": mismatches, "unresolved": unresolved,
        "missing_in_line": missing_in_line,
        "md_json_drift": md_drift,
        # 刻意没有文件凭据的行（⏳ 还没跑过 / 示意副本）：列出来但**不算失败**
        "rows_pending_measurement": pending,
        # 凭据列既没有记号也没有标记：**漏标，算失败**
        "rows_unmarked": unmarked,
    }


def rerender_drifted_md(eval_dir: Path = EVAL_DIR) -> list[dict]:
    """把漂了的 `.md` 从它自己的 `.json` 重新渲染一遍。零 LLM 调用——md 是 json 的
    确定性渲染产物，重渲染不会造出任何新数字。

    用 `eval.run_eval.render_markdown`（渲染逻辑的唯一实现），不在这里另写一套
    格式。函数里 import：那个模块顶层会拉起 core.chain 那一串。

    **重渲染出来的 md 顶部会多一行 `**后端**：未知（旧版报告没有这一行）`**——
    那是实话：这几份 json 生成于 R5-4 给报告加 `backend` 块之前，文件里确实没有
    后端信息。下一次真机跑 run_eval 出来的报告会自带。
    """
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
    for d in result.get("md_json_drift") or []:
        lines.append(f"✗ {d['md']} 是 {d['md_ts']} 生成的，而 {d['json']} 是 "
                     f"{d['json_ts']}——旧的人读报告躺在新数据旁边，而人只读 md。"
                     f"重渲染：python -m scripts.collect_results --rerender")
    if result["ok"]:
        lines.append("✓ 全部一致。")
    for line in result.get("rows_unmarked") or []:
        lines.append(f"✗ 这一行的凭据列既没有记号也没有 ⏳ 标记（漏标）：{line[:110]}")
    pending = result.get("rows_pending_measurement") or []
    lines.append("")
    lines.append(f"另有 {len(pending)} 行**刻意**没有文件凭据——不是错误。凭据列写的是哪一种"
                 "决定了怎么补：📦 能从 git 历史归档补上，⏳ 只能等上机跑那一项：")
    for line in pending:
        lines.append(f"  · {line[:110]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        description="机读 eval/ 各 report 文件的数，并核对 RESULTS.md 没抄错")
    ap.add_argument("--eval-dir", type=Path, default=EVAL_DIR)
    ap.add_argument("--check", nargs="*", type=Path, default=None,
                    help="核对这些 markdown 里的凭据记号；不给路径就核对 eval/RESULTS.md "
                         "和 README.md 两份（不一致退出码 1）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--rerender", action="store_true",
                    help="把跟自己的 .json 对不上的 report_e*.md 重新渲染一遍（零 LLM 调用）")
    args = ap.parse_args(argv)

    if args.rerender:
        done = rerender_drifted_md(args.eval_dir)
        if not done:
            print("没有需要重渲染的 md（每份 .md 的时间戳都跟它的 .json 一致）")
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
