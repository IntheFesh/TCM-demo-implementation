"""R57：四组消融，证明"不靠模仿也能推"。**本轮成败的唯一判据**——不达标就回
R51 补规则，不许把医案放回推导相凑数（那是回退，不是修复）。

    python -m eval.ablation.r57 --backend fake --sdt-dir <TCMEval>/evaluation/TCMEval-SDT
    python -m eval.ablation.r57 --backend real --sdt-dir <TCMEval>/evaluation/TCMEval-SDT

分组定义（A/B/C/D 各自设哪些环境变量）**唯一出处**是 `eval/ablation/spec.py`，
这里只负责"用那份定义去跑、去汇总"，不重复定义。

## 三条硬指标（`eval/ablation/spec.py` 的 GATE_* 常量）

1. C 组验证器一次过率 ≥ A 组——"不模仿医案，结论照样能一次通过符号验证"。
2. C 组 rule_refs 完整率（`derivation_completeness_ratio`）≥ 0.9——"推导链上
   每一步都真的挂着医理规则，不是空转"。
3. C 与 D 的证型/治法/主方一致率 ≥ 0.9——验的是 R54 的不变式："事后佐证
   不回流改推导"，C/D 唯一的差异就是要不要跑第三相佐证，结论不该跟着变。

## 五个指标，每组都报

`verifier_first_pass_rate`、`rule_refs_completeness_rate`、
`herbs_grounded_ratio_mean`、`hallucination_rate`、`cost`（调用数 + 墙钟）——
延续 R34/R38 那三个内容指标的做法（分母跟着比率一起报，不让读者自己数），
`rule_refs_completeness_rate` 是这一轮新加的（A 组这一格恒是"不适用"：
`run_synthesis` 根本不产出 `derivation_completeness_ratio` 这个键，不是
凑巧算出 0）。

## 沙盒里跑不出真机数

跟 R38 同一条诚实约束：这个沙盒没有真实 LLM 后端，`--backend fake` 只能验
管道（四组分别设对了环境变量、跑通了四条路径、报告格式对不对），**不能**
产出可信的内容指标——假后端的产出是固定假文本，"验证器一次过率" 算出来的
只是假数据长什么样。真机 20 条主诉 × 4 组 = 80 次问诊，按 R55 报告记录的
单次问诊墙钟量级（top3 档约 45 秒、full_context 档约 75 秒，derived 模式
经验上接近 top3 档），**80 次约 1~1.5 小时机器时间**；按当前主流 API 价格
（`docs/reports/R37-R39_acceptance.md` 记录的同类调用成本量级）预估**约
¥5 左右**——这两个数字需要用户在自己的 AutoDL 机器上实测确认，此处只给
量级、不假装精确。跑法见本文件顶部两行命令，`--sdt-dir` 指向用户自己的
TCMEval-SDT 本地checkout（数据集不随本仓库分发，见 `eval/sdt/data.py`）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from eval.ablation.spec import (
    GATE_C_RULE_REFS_COMPLETENESS_MIN,
    GATE_C_VERIFIER_FIRST_PASS_VS,
    GATE_CD_CONSISTENCY_MIN,
    GROUPS,
    R57Group,
    group_by_key,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = ROOT / "eval" / "report_ablation_r57.json"

#: 消融要动的三个旋钮。**全部先清空再按组设置**——同 R38 那条"三个旋钮全部
#: 先删掉再设"的理由：上一组留下的变量不清掉，下一组量出来的是叠加效应。
KNOBS = ("S3_MODE", "THEORY_LAYER", "CORROBORATION")


@contextmanager
def apply_group(group: R57Group):
    saved = {k: os.environ.get(k) for k in KNOBS}
    try:
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(group.env)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------- 主诉来源：SDT Train 脾胃门 20 条 ----------

def select_pi_wei_men_complaints(sdt_dir: Path, n: int = 20) -> list[dict]:
    """从 SDT Train 里挑 `n` 条脾胃门主诉。**判据是"证型的第一个词含脾/胃"**
    （`TCM Syndrome` 字段按 `;` 分隔多个证型，第一个通常是主证）——比在
    `Clinical Data` 原文里搜"胃"这类关键词更准：后者会把"食欲不振"这种
    在别的系统性疾病里顺带提一句的词也算进来（实测：关键词法命中 73 条，
    证型法命中 27 条，两者差出一倍不止）。

    **排序确定性**：按"证型词数升序、病案编号升序"排——词数少的证型更单纯
    （比如"脾胃虚弱"比"脾肾两虚;气化失运;湿邪阻滞"更适合当这一轮要验证的
    典型样本），病案编号兜底让相同词数时的顺序可复现，不依赖字典遍历顺序
    这种平台相关的隐藏状态。**这个函数是幂等的**：同一份 Train 数据、同一个
    n，每次跑出来的 20 条一模一样，可以被审计、被重放。

    SDT 数据集本身不随本仓库分发（见 `eval/sdt/data.py`），所以这里不缓存
    结果到仓库里的文件，每次调用都现读 `--sdt-dir` 指向的用户本地副本。

    **直接读原始 JSON，不走 `eval.sdt.data.load_split`**：`SdtRecord`
    （`load_split` 的返回类型）没有保留 `"TCM Syndrome"` 这个原始字段——
    那是金标准的一部分，`load_split` 已经把它拆进
    `gold_syndrome_answers`/`gold_pathogenesis_answers` 这几个跟官方评测
    格式对齐的字段，丢了"哪个证型排第一"这个筛选要用的顺序信息。这个函数
    要的正是原始顺序，所以绕过那层转换，直接读 `Train_TCM_Data_v1.json`。
    """
    def _rid(record_id: str) -> int:
        m = re.search(r"\d+", record_id)
        return int(m.group()) if m else 0

    raw = json.loads((Path(sdt_dir) / "data" / "Train_TCM_Data_v1.json")
                     .read_text(encoding="utf-8"))
    hits = []
    for row in raw:
        syn = row.get("TCM Syndrome", "")
        first = syn.split(";")[0] if syn else ""
        if "脾" in first or "胃" in first:
            hits.append(row)
    hits.sort(key=lambda row: (len(row.get("TCM Syndrome", "").split(";")),
                               _rid(row.get("Medical Record ID", ""))))
    picked = hits[:n]
    if len(picked) < n:
        raise ValueError(
            f"SDT Train 里按「证型含脾/胃」筛出 {len(picked)} 条，不够 {n} 条。")
    return [{"record_id": row["Medical Record ID"], "syndrome": row.get("TCM Syndrome", ""),
            "complaint": row["Clinical Data"]} for row in picked]


# ---------- 单次问诊 → 五指标的原料 ----------

def _syndrome_method_formula(s3_structured) -> tuple[str | None, str | None, str | None]:
    """`S3Structured`/`S3Derived` 字段名相同（`syndrome.name`/`method.principle`/
    `formula.candidate.name`），一份实现两种模式都能读——这是 R52 设计
    `S3Derived` 时刻意保留的字段名兼容性（见 `core/chain.py::run_derivation`
    文档字符串），这里直接复用，不为两种 schema 分别写一份取值逻辑。

    `result["results"][0]["s3_structured"]` 拿到的是**没有 `.model_dump()`
    过的 pydantic 对象**（`core/chain.py::run_derivation`/`run_synthesis`
    原样存的是 `raw`），不是 dict——`rule_refs`/`insufficient_notes` 那几个
    键是显式 `.model_dump()` 过的，`s3_structured` 本身不是，两者不统一，
    所以这里用属性访问，不假设它是 dict。经过 JSON 往返（比如从磁盘读回
    已经落盘的报告）之后它会变成 dict，`getattr(obj, name, None)` 对 dict
    不生效，所以两条路径都要接住。"""
    if not s3_structured:
        return None, None, None

    def _get(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    syn = _get(_get(s3_structured, "syndrome"), "name")
    method = _get(_get(s3_structured, "method"), "principle")
    formula = _get(_get(_get(s3_structured, "formula"), "candidate"), "name")
    return syn, method, formula


def metrics_from_result(result: dict | None) -> dict:
    results = (result or {}).get("results") or []
    r = results[0] if results else {}
    if not r or r.get("s3") is None:
        return {"has_output": False}
    vm = r.get("verifier_metrics") or {}
    completeness = r.get("derivation_completeness_ratio")  # None：A 组没有这个键，不适用
    syn, method, formula = _syndrome_method_formula(r.get("s3_structured"))
    return {
        "has_output": True,
        "herbs_grounded_ratio": r.get("herbs_grounded_ratio"),
        "verifier_first_pass": vm.get("verifier_first_pass"),
        "rule_refs_completeness": completeness,
        "n_hallucinated_ids": len(r.get("hallucinated") or []),
        "syndrome": syn, "method": method, "formula": formula,
        "llm_calls": ((result or {}).get("manifest") or {}).get("llm_calls"),
        "s3_mode": ((result or {}).get("manifest") or {}).get("s3_mode"),
    }


def _rate(hits: int, total: int) -> dict:
    return {"value": (round(hits / total, 4) if total else None),
            "n": hits, "denominator": total}


def run_group(group: R57Group, complaints: list[dict], backend, *, progress=None) -> list[dict]:
    """跑一组。每条主诉一次问诊，一次失败不丢整组（同 R9/R38 的失败容忍）。"""
    from scripts.bench_consult import run_once

    rows: list[dict] = []
    with apply_group(group):
        for i, c in enumerate(complaints, 1):
            if progress is not None:
                progress(f"  {group.key} {i}/{len(complaints)}　{c['complaint'][:18]}…")
            run = run_once(c["complaint"], use_react=False, retriever_mode=None,
                           backend=backend, keep_result=True)
            rows.append({
                "record_id": c["record_id"], "complaint": c["complaint"],
                "ok": run["ok"], "error": run["error"], "elapsed_s": run["elapsed_s"],
                "llm_calls": run["llm_calls"],
                "metrics": metrics_from_result(run.get("result")),
            })
    return rows


def aggregate(rows: list[dict], group: R57Group, *, content_valid: bool) -> dict:
    ok = [r for r in rows if r.get("ok")]
    with_output = [r for r in ok if r["metrics"].get("has_output")]
    grounded = [r["metrics"]["herbs_grounded_ratio"] for r in with_output
               if r["metrics"].get("herbs_grounded_ratio") is not None]
    first_pass = [bool(r["metrics"]["verifier_first_pass"]) for r in with_output
                  if r["metrics"].get("verifier_first_pass") is not None]
    completeness = [r["metrics"]["rule_refs_completeness"] for r in with_output
                    if r["metrics"].get("rule_refs_completeness") is not None]
    calls = [r["llm_calls"] for r in ok if isinstance(r.get("llm_calls"), (int, float))]
    wall = [r["elapsed_s"] for r in ok if isinstance(r.get("elapsed_s"), (int, float))]
    hallu_runs = sum(1 for r in with_output if r["metrics"].get("n_hallucinated_ids"))
    note = None if content_valid else "假后端：内容指标不出数（产出是固定假文本）"
    rule_refs_applicable = group.env["S3_MODE"] == "derived"
    return {
        "n_queries": len(rows), "n_ok": len(ok), "n_with_output": len(with_output),
        "content_metrics_valid": content_valid, "content_note": note,
        "herbs_grounded_ratio_mean": (
            round(statistics.fmean(grounded), 4) if (grounded and content_valid) else None),
        "verifier_first_pass_rate": (
            _rate(sum(first_pass), len(first_pass)) if (first_pass and content_valid) else None),
        "rule_refs_completeness_rate": (
            {"value": round(statistics.fmean(completeness), 4), "n": len(completeness)}
            if (completeness and content_valid) else None),
        "rule_refs_applicable": rule_refs_applicable,
        "rule_refs_note": (None if rule_refs_applicable else
                           "这一组走 S3_MODE=structured，没有 rule_refs/"
                           "derivation_completeness_ratio 这个键——不适用，不是 0"),
        "hallucination_rate": (_rate(hallu_runs, len(with_output)) if content_valid else None),
        "llm_calls_mean": (round(statistics.fmean(calls), 3) if calls else None),
        "elapsed_s_mean": (round(statistics.fmean(wall), 3) if wall else None),
    }


def pair_consistency(c_rows: list[dict], d_rows: list[dict], *, content_valid: bool) -> dict:
    """C 与 D **按同一条主诉配对**比证型/治法/主方——这是 R54"绝不回头改
    推导"这条不变式在消融层面的验证：C/D 唯一的旋钮差异是 `CORROBORATION`，
    如果结论跟着变了，说明第三相事后佐证在哪里悄悄回流影响了推导，R54 的
    不变式就被破坏了（`tests/test_corroboration.py` 用 sha256 在单次调用
    层面钉过这件事；这里是消融层面、多条主诉上的复核，两处判据不是一回事：
    单次调用层面测的是"这一次调用没有改"，这里测的是"跨样本看，会不会有
    某种输入模式系统性地让结论漂移"——理论上前者保证了后者，但"理论上"
    不是"实测过"，四组消融本来就是把"理论上"换成"实测过"这件事。"""
    if not content_valid:
        return {"value": None, "n": 0, "denominator": 0,
                "note": "假后端：C/D 结论恒同一份固定假文本，这个比率没有意义"}
    by_record_c = {r["record_id"]: r for r in c_rows if r.get("ok")}
    by_record_d = {r["record_id"]: r for r in d_rows if r.get("ok")}
    shared = sorted(set(by_record_c) & set(by_record_d))
    match = 0
    mismatches = []
    for rid in shared:
        cm, dm = by_record_c[rid]["metrics"], by_record_d[rid]["metrics"]
        c_tuple = (cm.get("syndrome"), cm.get("method"), cm.get("formula"))
        d_tuple = (dm.get("syndrome"), dm.get("method"), dm.get("formula"))
        if c_tuple == d_tuple and all(c_tuple):
            match += 1
        else:
            mismatches.append({"record_id": rid, "c": c_tuple, "d": d_tuple})
    return {**_rate(match, len(shared)), "mismatches": mismatches}


def build_report(rows_by_group: dict[str, list[dict]], *, backend_info: dict,
                 complaints: list[dict], content_valid: bool) -> dict:
    groups = {k: aggregate(v, group_by_key(k), content_valid=content_valid)
             for k, v in rows_by_group.items()}
    consistency = (pair_consistency(rows_by_group.get("C", []), rows_by_group.get("D", []),
                                    content_valid=content_valid)
                  if "C" in rows_by_group and "D" in rows_by_group else None)

    def _gate(name: str, ok: bool | None, detail: str) -> dict:
        return {"name": name, "passed": ok, "detail": detail}

    gates = []
    a_fp = (groups.get("A", {}).get("verifier_first_pass_rate") or {}).get("value")
    c_fp = (groups.get("C", {}).get("verifier_first_pass_rate") or {}).get("value")
    if content_valid and a_fp is not None and c_fp is not None:
        gates.append(_gate(
            f"C 组验证器一次过率 ≥ {GATE_C_VERIFIER_FIRST_PASS_VS} 组",
            c_fp >= a_fp, f"C={c_fp} vs A={a_fp}"))
    else:
        gates.append(_gate(
            f"C 组验证器一次过率 ≥ {GATE_C_VERIFIER_FIRST_PASS_VS} 组", None,
            "假后端或缺数据，量不到"))
    c_completeness = (groups.get("C", {}).get("rule_refs_completeness_rate") or {}).get("value")
    if content_valid and c_completeness is not None:
        gates.append(_gate(
            f"C 组 rule_refs 完整率 ≥ {GATE_C_RULE_REFS_COMPLETENESS_MIN}",
            c_completeness >= GATE_C_RULE_REFS_COMPLETENESS_MIN, f"C={c_completeness}"))
    else:
        gates.append(_gate(
            f"C 组 rule_refs 完整率 ≥ {GATE_C_RULE_REFS_COMPLETENESS_MIN}", None,
            "假后端或缺数据，量不到"))
    if consistency and consistency.get("value") is not None:
        gates.append(_gate(
            f"C 与 D 一致率 ≥ {GATE_CD_CONSISTENCY_MIN}",
            consistency["value"] >= GATE_CD_CONSISTENCY_MIN, f"实测={consistency['value']}"))
    else:
        gates.append(_gate(f"C 与 D 一致率 ≥ {GATE_CD_CONSISTENCY_MIN}", None,
                           (consistency or {}).get("note") or "缺数据，量不到"))

    # B→C 只差一个旋钮（THEORY_LAYER off→on，医案两组都不进推导），所以这个差值
    # 就是"医理规则层单独带来的净提升"——R51 存在的理由，用户要求单独报一次。
    b_fp = (groups.get("B", {}).get("verifier_first_pass_rate") or {}).get("value")
    theory_layer_net_contribution = (
        round(c_fp - b_fp, 4)
        if isinstance(c_fp, (int, float)) and isinstance(b_fp, (int, float)) else None)

    return {
        "kind": "ablation_r57",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": backend_info,
        "content_metrics_valid": content_valid,
        "n_queries": len(complaints),
        "complaints": complaints,
        "groups": {g.key: {"name": g.name, "env": g.env, "describe": g.describe(),
                           **groups[g.key]} for g in GROUPS if g.key in groups},
        "c_vs_d_consistency": consistency,
        "b_to_c_verifier_first_pass_delta": theory_layer_net_contribution,
        "gates": gates,
        "all_gates_passed": (all(g["passed"] for g in gates if g["passed"] is not None)
                             if any(g["passed"] is not None for g in gates) else None),
        "rows": rows_by_group,
    }


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "⏳"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def to_markdown(report: dict) -> str:
    lines = ["# R57 消融：四组 × 五指标——证明「不靠模仿也能推」", ""]
    b = report.get("backend") or {}
    lines.append(f"后端 `{b.get('id')}`（{b.get('model')}），{report.get('n_queries')} 条"
                 "脾胃门主诉（SDT Train）。")
    if not report.get("content_metrics_valid"):
        lines.append("")
        lines.append("> ⚠ **这一份是假后端跑的**：内容指标（带本体出处占比、验证器一次过率、"
                     "rule_refs 完整率、幻觉率、C/D 一致率）一律不出数，表里是 ⏳。"
                     "能读的只有管道本身：四组是否各自设对了环境变量、跑通了没有报错。")
    lines += ["", "| 组 | 医理层/医案/事后佐证 | 药味占比 | 验证器一次过 | rule_refs 完整率 "
                  "| 幻觉率 | 调用数 | 墙钟 |",
             "|---|---|---|---|---|---|---|---|"]
    for g in GROUPS:
        row = (report.get("groups") or {}).get(g.key)
        if not row:
            continue
        fp = row.get("verifier_first_pass_rate")
        rr = row.get("rule_refs_completeness_rate")
        rr_cell = "不适用" if not row.get("rule_refs_applicable", True) else _fmt((rr or {}).get("value"))
        hr = row.get("hallucination_rate")
        lines.append(
            f"| {g.key} {g.name} | {g.describe().split('：', 1)[-1]} "
            f"| {_fmt(row.get('herbs_grounded_ratio_mean'))} | {_fmt((fp or {}).get('value'))} "
            f"| {rr_cell} | {_fmt((hr or {}).get('value'))} "
            f"| {_fmt(row.get('llm_calls_mean'))} | {_fmt(row.get('elapsed_s_mean'), ' s')} |")
    lines.append("")
    cons = report.get("c_vs_d_consistency") or {}
    lines.append(f"**C 与 D 证型/治法/主方一致率**：{_fmt(cons.get('value'))}"
                f"（{_fmt(cons.get('n'))}/{_fmt(cons.get('denominator'))}）"
                + (f"——{cons['note']}" if cons.get("note") else ""))
    if cons.get("mismatches"):
        lines.append(f"不一致的 {len(cons['mismatches'])} 条：" +
                     "、".join(m["record_id"] for m in cons["mismatches"][:10]))
    lines += ["", "## 三条硬指标"]
    for g in report.get("gates") or []:
        mark = "⏳" if g["passed"] is None else ("✅" if g["passed"] else "❌")
        lines.append(f"- {mark} {g['name']}（{g['detail']}）")
    overall = report.get("all_gates_passed")
    lines.append("")
    lines.append("**总判定**：" + ("⏳ 还没有真机数，判不了" if overall is None
                                 else ("✅ 三条全过" if overall else "❌ 至少一条没过——回 R51 补规则")))
    for g in GROUPS:
        lines.append(f"- **{g.describe()}**")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdt-dir", type=Path, default=None,
                    help="TCMEval-SDT 本地路径。给了就自动按脾胃门筛 20 条 Train 主诉")
    ap.add_argument("--queries-path", default=None,
                    help="备选：一份纯文本主诉文件（一行一条），不依赖 SDT 数据集")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--backend", default="fake", choices=["fake", "real"])
    ap.add_argument("--groups", default="ABCD", help="跑哪几组，默认全跑")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=None)
    ap.add_argument("--no-warmup", dest="warmup", action="store_false")
    ap.set_defaults(warmup=True)
    args = ap.parse_args(argv)

    if args.sdt_dir is None and args.queries_path is None:
        print("需要 --sdt-dir（自动挑脾胃门 20 条）或 --queries-path（自备主诉文件）之一",
             file=sys.stderr)
        return 2

    if args.sdt_dir is not None:
        try:
            complaints = select_pi_wei_men_complaints(args.sdt_dir, n=20)
        except Exception as e:  # noqa: BLE001 - 数据集缺失/格式不对都要说清楚，不崩栈
            print(f"从 --sdt-dir 挑主诉失败：{type(e).__name__}: {e}", file=sys.stderr)
            return 2
    else:
        qpath = Path(args.queries_path)
        if not qpath.exists():
            print(f"主诉文件不在：{qpath}", file=sys.stderr)
            return 2
        lines = [ln.strip() for ln in qpath.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        complaints = [{"record_id": f"q{i}", "syndrome": None, "complaint": c}
                     for i, c in enumerate(lines, 1)]
    if args.limit > 0:
        complaints = complaints[:args.limit]
    if not complaints:
        print("一条主诉都没读到", file=sys.stderr)
        return 2

    from scripts.bench_consult import (
        AUTO_FAKE_CASES_PER_PHYSICIAN, build_backend, install_fake_cases,
    )
    from core.retrieval import cases_available

    backend = build_backend(args.backend, 0.0, False)
    content_valid = args.backend == "real"
    if args.backend == "fake" and not cases_available():
        install_fake_cases(AUTO_FAKE_CASES_PER_PHYSICIAN)

    if args.warmup:
        print("— 预热（这一次的数不计入任何一组）")
        try:
            run_group(GROUPS[0], complaints[:1], backend)
        except Exception as e:  # noqa: BLE001
            print(f"  预热失败（继续往下跑）：{type(e).__name__}: {e}", file=sys.stderr)

    t0 = time.perf_counter()
    rows_by_group: dict[str, list[dict]] = {}
    for key in args.groups.upper():
        group = group_by_key(key)
        print(f"— {group.describe()}")
        rows_by_group[group.key] = run_group(
            group, complaints, backend, progress=lambda line: print(line))
    report = build_report(
        rows_by_group,
        backend_info={"id": backend.backend_id(), "model": backend.model_name(),
                      "comparability_warning": backend.comparability_warning()},
        complaints=complaints, content_valid=content_valid)
    report["wall_s"] = round(time.perf_counter() - t0, 2)
    report["warmup"] = bool(args.warmup)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    md = Path(args.md) if args.md else out.with_suffix(".md")
    md.write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))
    print(f"→ {out}\n→ {md}")
    n_fail = sum(1 for rows in rows_by_group.values() for r in rows if not r["ok"])
    if n_fail:
        print(f"✗ {n_fail} 次问诊失败（详见 rows[].error）", file=sys.stderr)
    if content_valid and report.get("all_gates_passed") is False:
        print("✗ 至少一条硬指标没过——回 R51 补规则，不许把医案放回推导相凑数",
             file=sys.stderr)
        return 1
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
