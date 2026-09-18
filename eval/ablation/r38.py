"""R38：四组消融 + 内部三指标。需要真实 LLM，所以在 `eval/` 不在 `tests/`。

    python -m eval.ablation --backend real --queries-path tests/queries.txt
    python -m eval.ablation --backend fake            # 只验管道，内容指标不出数

## 四组是什么，各回答什么问题

每一组**只动一个开关**，其余全部是产品默认——同时动两个就没法归因。
开关本身早就在代码里（R33/R34/R36 留的旋钮），这一轮只是把它们摆成对照：

| 组 | 开关 | 回答的问题 |
|---|---|---|
| A | （产品默认） | 基线。别的组都跟它比 |
| B | `S3_MODE=legacy` | 五家融合成一条链 vs 三家各说各的，结论质量差多少 |
| C | `S3_BEST_OF_N=3` | 采三次挑最好的那次，值不值三倍的钱 |
| D | `S1S2_MERGED=1` | S1+S2 合一省一次往返，代价是什么（**默认关，理由见 SOURCES 第 93 条**） |

**D 组只在消融里开。** 合一会把证素推断挪到安全否决之前（CLAUDE.md 那条铁律），
所以它在产品里默认关着；这一组量的是"关着它到底损失了多少"，
而不是"要不要打开"——那个问题的答案已经是"不打开"。

## 三指标是哪三个

沿用 R34 定下的那三个（`core/formula_verifier.py` 的「三指标」一节），不另起炉灶：

1. **带本体出处的药味占比**（`herbs_grounded_ratio`）——分母是**这张方的药味数**；
2. **验证器一次过率**（`verifier_first_pass`）——第一轮就"无 veto、无 revise、
   且没有判不了的"；
3. **本体对语料的覆盖率**（`ontology_coverage_of_corpus`）——分母是**医案语料里
   出现过的药名**。它是**语料侧的数、不随组变**，所以整份报告只报一次
   （R34b 那两个分母的第二个，摆在这里是为了让读者一眼看到它们不是一回事）。

外加两个成本数（调用数、墙钟）和一个诚实数（幻觉率：引了检索结果里没有的医案号）。

## 两条不许省的诚实约束

**一、legacy 组没有验证器，那一格是"不适用"，不是 0。** 符号验证器挂在
structured 这一支上（`S3Structured` 才有 `ontology_refs`）。把不适用写成 0，
B 组看起来就像"一次都没过"——那是在用一个不存在的失败去抹黑对照组。

**二、假后端跑出来的内容指标一律不出数。** `--backend fake` 的产出是固定假文本，
它的"带本体出处占比"只反映假数据长什么样。这份报告里内容指标因此带一个
`content_metrics_valid` 标记，假后端下为 False，Markdown 里印 ⏳ 而不是数字。
能跑的只有管道本身（几组、各跑几条、调用数对不对得上）——那也确实值得在沙盒里跑，
R33 的教训就是"演示配置从来没被跑过"。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: R57 把这个文件从 `eval/ablation.py` 挪进了 `eval/ablation/r38.py`——
#: 多了一层目录，`.parent` 要多跳一次才能回到仓库根，不然 DEFAULT_OUT
#: 会算成 `eval/eval/report_ablation.json`（这个 bug 挪文件那次真的踩过）。
ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = ROOT / "eval" / "report_ablation.json"

#: 这一轮要摆平的三个开关。**产品默认值不写在这里**——它们是 `core/llm.py`
#: 的 `s3_mode()` / `s3_best_of_n()` / `s1s2_merged()` 读的那几个环境变量，
#: 各组只在这三个上做差异，A 组一个都不设（= 用产品默认）。
KNOBS = ("S3_MODE", "S3_BEST_OF_N", "S1S2_MERGED")


@dataclass(frozen=True)
class AblationGroup:
    key: str
    name: str
    env: dict[str, str] = field(default_factory=dict)
    asks: str = ""

    def describe(self) -> str:
        knobs = "、".join(f"{k}={v}" for k, v in sorted(self.env.items())) or "（产品默认）"
        return f"{self.key} {self.name}：{knobs}"


GROUPS: tuple[AblationGroup, ...] = (
    AblationGroup("A", "产品默认", {},
                  "基线：structured 单链、best-of-1、S1/S2 分两次跑"),
    AblationGroup("B", "三列集注", {"S3_MODE": "legacy"},
                  "五家融合 vs 三家并置：结论质量与调用数各差多少"),
    AblationGroup("C", "best-of-3", {"S3_BEST_OF_N": "3"},
                  "采三次挑最好的那次，值不值三倍的钱"),
    AblationGroup("D", "S1+S2 合一", {"S1S2_MERGED": "1"},
                  "省一次往返的代价（产品里默认关，见 SOURCES 第 93 条）"),
)


def group_by_key(key: str) -> AblationGroup:
    for g in GROUPS:
        if g.key == key.upper():
            return g
    raise KeyError(f"没有这一组：{key}（可选 {'/'.join(g.key for g in GROUPS)}）")


@contextmanager
def apply_group(group: AblationGroup):
    """把这一组的开关设进环境，跑完还原。

    **三个旋钮全部先删掉再按组设置**：跑第二组时如果只"设自己那一个"，
    上一组留下的变量还在，量出来的就是两个开关叠加的结果。这类串味在消融里
    尤其致命——它不报错，只是让某一组的数字莫名其妙地好看或难看。
    """
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


# ---------- 单次问诊 → 三指标的原料 ----------

def metrics_from_result(result: dict | None) -> dict:
    """一次问诊能贡献给三指标的原料。**不做平均**，聚合在 `aggregate()` 里。

    每一项都可能是 None，而 None 和 0 在这里差别很大：
      · `grounded` 为 None = 这次没产出方（被拦截 / 信息不足），不进分母；
      · `first_pass` 为 None = 这一支根本不跑验证器（legacy），不进分母。
    """
    results = (result or {}).get("results") or []
    grounded: list[float] = []
    first_pass: list[bool] = []
    n_hallucinated = 0
    n_with_output = 0
    for r in results:
        if r.get("s3") is None:
            continue
        n_with_output += 1
        ratio = r.get("herbs_grounded_ratio")
        if ratio is not None:
            grounded.append(float(ratio))
        vm = r.get("verifier_metrics") or {}
        if "verifier_first_pass" in vm:
            first_pass.append(bool(vm["verifier_first_pass"]))
        if r.get("hallucinated"):
            n_hallucinated += len(r["hallucinated"])
    return {
        "n_results_with_output": n_with_output,
        "herbs_grounded": grounded,
        "verifier_first_pass": first_pass,
        "n_hallucinated_ids": n_hallucinated,
        "any_hallucinated": n_hallucinated > 0,
        "llm_calls": ((result or {}).get("manifest") or {}).get("llm_calls"),
        "s3_mode": ((result or {}).get("manifest") or {}).get("s3_mode"),
        "s1s2_merged": ((result or {}).get("manifest") or {}).get("s1s2_merged"),
        "best_of_n": ((result or {}).get("manifest") or {}).get("best_of_n"),
    }


def _rate(hits: int, total: int) -> dict:
    """比率 + 它的分子分母。**分母一起返回**，不让调用方再数一遍
    （分母写两处就会有一处忘了改——R35 那个百分比就是这么错的）。"""
    return {"value": (round(hits / total, 4) if total else None),
            "n": hits, "denominator": total}


def aggregate(rows: list[dict], *, content_valid: bool) -> dict:
    """把一组里每次问诊的原料汇总成这一组的读数。

    `content_valid=False`（假后端）时内容指标一律为 None 并带 `note`——
    假数据算出来的"带本体出处占比"只反映假文本长什么样。
    """
    ok = [r for r in rows if r.get("ok")]
    grounded = [x for r in ok for x in r["metrics"]["herbs_grounded"]]
    first_pass = [x for r in ok for x in r["metrics"]["verifier_first_pass"]]
    calls = [r["llm_calls"] for r in ok if isinstance(r.get("llm_calls"), (int, float))]
    wall = [r["elapsed_s"] for r in ok if isinstance(r.get("elapsed_s"), (int, float))]
    hallu_runs = sum(1 for r in ok if r["metrics"]["any_hallucinated"])
    note = None if content_valid else "假后端：内容指标不出数（产出是固定假文本）"
    # 「这一格为什么没有数」有三种完全不同的原因，**不能混成一句**：
    #   ①这一支根本不跑验证器（legacy）——不适用；
    #   ②假后端——量不到；
    #   ③跑了但一次结论都没产出——那是这一组真的失败了，最该被看见的一种。
    observed_mode = next((r["metrics"]["s3_mode"] for r in ok), None)
    verifier_applicable = observed_mode != "legacy"
    if not verifier_applicable:
        verifier_note = ("这一组不跑符号验证器（它挂在 structured 这一支上），"
                         "所以这一格是「不适用」，不是 0")
    elif not content_valid:
        verifier_note = "假后端：验证器一次过率量不到"
    elif not first_pass:
        verifier_note = "这一组没有一次问诊产出结论——不是「一次都没过」，是根本没跑到"
    else:
        verifier_note = None
    return {
        "n_queries": len(rows),
        "n_ok": len(ok),
        "content_metrics_valid": content_valid,
        "content_note": note,
        # 指标①：带本体出处的药味占比（分母＝本次方的药味数，已在单次里算好）
        "herbs_grounded_ratio_mean": (
            round(statistics.fmean(grounded), 4) if (grounded and content_valid) else None),
        "herbs_grounded_n": len(grounded),
        "herbs_grounded_denominator": "本次方的药味数（每次问诊一个比值，这里取均值）",
        # 指标②：验证器一次过率。**legacy 那一支不跑验证器 → None + 原因**
        "verifier_first_pass_rate": (
            _rate(sum(first_pass), len(first_pass)) if (first_pass and content_valid) else None),
        "verifier_applicable": verifier_applicable,
        "verifier_note": verifier_note,
        # 诚实数：引了检索结果里没有的医案号
        "hallucination_rate": (_rate(hallu_runs, len(ok)) if content_valid else None),
        "n_hallucinated_ids": (sum(r["metrics"]["n_hallucinated_ids"] for r in ok)
                               if content_valid else None),
        # 成本：这两项跟后端真假无关（假后端的墙钟只是管道开销，标在报告里）
        "llm_calls_mean": (round(statistics.fmean(calls), 3) if calls else None),
        "elapsed_s_mean": (round(statistics.fmean(wall), 3) if wall else None),
        # 这一组**实际**跑成了什么形状（从 manifest 读回来，不信自己设的环境变量）
        "observed": {
            "s3_mode": next((r["metrics"]["s3_mode"] for r in ok), None),
            "best_of_n": next((r["metrics"]["best_of_n"] for r in ok), None),
            "s1s2_merged": next((r["metrics"]["s1s2_merged"] for r in ok), None),
        },
    }


def compare_to_baseline(groups: dict[str, dict], baseline_key: str = "A") -> dict:
    """每组跟 A 组的差。**差值也带分母口径**：没有基线值的项报 None 不报 0。"""
    base = groups.get(baseline_key) or {}
    out: dict[str, dict] = {}
    for key, g in groups.items():
        if key == baseline_key:
            continue
        row: dict[str, float | None] = {}
        for metric in ("herbs_grounded_ratio_mean", "llm_calls_mean", "elapsed_s_mean"):
            a, b = base.get(metric), g.get(metric)
            row[f"delta_{metric}"] = (round(b - a, 4)
                                      if isinstance(a, (int, float))
                                      and isinstance(b, (int, float)) else None)
        a_rate = (base.get("verifier_first_pass_rate") or {}).get("value")
        b_rate = (g.get("verifier_first_pass_rate") or {}).get("value")
        row["delta_verifier_first_pass_rate"] = (
            round(b_rate - a_rate, 4)
            if isinstance(a_rate, (int, float)) and isinstance(b_rate, (int, float)) else None)
        out[key] = row
    return out


# ---------- 跑 ----------

def run_group(group: AblationGroup, complaints: list[str], backend, *,
              progress=None) -> list[dict]:
    """跑一组。每条主诉一次问诊，**一次失败不丢整组**（同 R9 的失败容忍）。"""
    from scripts.bench_consult import run_once

    rows: list[dict] = []
    with apply_group(group):
        for i, complaint in enumerate(complaints, 1):
            if progress is not None:
                progress(f"  {group.key} {i}/{len(complaints)}　{complaint[:18]}…")
            run = run_once(complaint, use_react=False, retriever_mode=None,
                           backend=backend, keep_result=True)
            rows.append({
                "complaint": complaint,
                "ok": run["ok"],
                "error": run["error"],
                "elapsed_s": run["elapsed_s"],
                "llm_calls": run["llm_calls"],
                "metrics": metrics_from_result(run.get("result")),
            })
    return rows


def corpus_coverage() -> dict | None:
    """指标③：本体对医案语料的覆盖率。**不随组变**，整份报告只算一次。
    语料不在这台机器上时返回 None（不是 0——"没数据"和"覆盖率 0"是两回事）。"""
    from core.formula_verifier import ontology_coverage_of_corpus

    try:
        return ontology_coverage_of_corpus()
    except Exception as e:  # noqa: BLE001 - 缺语料/缺本体都不该把整份报告带崩
        return {"available": False, "reason": f"{type(e).__name__}: {e}"}


def build_report(rows_by_group: dict[str, list[dict]], *, backend_info: dict,
                 complaints: list[str], content_valid: bool) -> dict:
    groups = {k: aggregate(v, content_valid=content_valid) for k, v in rows_by_group.items()}
    return {
        "kind": "ablation",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": backend_info,
        "content_metrics_valid": content_valid,
        "n_queries": len(complaints),
        "queries": complaints,
        "groups": {g.key: {"name": g.name, "env": g.env, "asks": g.asks,
                           **groups[g.key]}
                   for g in GROUPS if g.key in groups},
        "vs_baseline": compare_to_baseline(groups),
        # 指标③单独摆：它是语料侧的数，跟组无关（R34b 的第二个分母）
        "ontology_coverage_of_corpus": corpus_coverage(),
        "rows": rows_by_group,
    }


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "⏳"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def to_markdown(report: dict) -> str:
    """一张表 + 每组一句"它回答什么"。**⏳ 是量不到，不是 0。**"""
    lines = ["# R38 消融：四组 × 三指标", ""]
    b = report.get("backend") or {}
    lines.append(f"后端 `{b.get('id')}`（{b.get('model')}），"
                 f"{report.get('n_queries')} 条主诉。")
    if not report.get("content_metrics_valid"):
        lines.append("")
        lines.append("> ⚠ **这一份是假后端跑的**：内容指标（带本体出处占比、"
                     "验证器一次过率、幻觉率）一律不出数，表里是 ⏳。"
                     "能读的只有管道本身与调用数。")
    lines += ["", "| 组 | 开关 | 带本体出处的药味占比 | 验证器一次过 | 幻觉率 |"
                  " 调用数 | 墙钟 |", "|---|---|---|---|---|---|---|"]
    for g in GROUPS:
        row = (report.get("groups") or {}).get(g.key)
        if not row:
            continue
        knobs = "、".join(f"`{k}={v}`" for k, v in sorted(g.env.items())) or "默认"
        fp = row.get("verifier_first_pass_rate")
        fp_cell = ("不适用" if not row.get("verifier_applicable", True)
                   else _fmt((fp or {}).get("value")))
        hr = row.get("hallucination_rate")
        lines.append(
            f"| {g.key} {g.name} | {knobs} | {_fmt(row.get('herbs_grounded_ratio_mean'))} "
            f"| {fp_cell} | {_fmt((hr or {}).get('value'))} "
            f"| {_fmt(row.get('llm_calls_mean'))} | {_fmt(row.get('elapsed_s_mean'), ' s')} |")
    cov = report.get("ontology_coverage_of_corpus") or {}
    if cov.get("available"):
        lines += ["", "**指标③（语料侧、不随组变）**：本体对医案语料的覆盖率 "
                  f"按**种数** {_fmt(cov.get('coverage_by_name'))}"
                  f"（{_fmt(cov.get('n_covered_names'))}/"
                  f"{_fmt(cov.get('n_corpus_herb_names'))} 种）、"
                  f"按**出现次数** {_fmt(cov.get('coverage_by_occurrence'))}"
                  f"（{_fmt(cov.get('n_covered_occurrences'))}/"
                  f"{_fmt(cov.get('n_corpus_occurrences'))} 次）。"
                  "**两个都要报**：常用药覆盖得好、古籍特有写法的长尾覆盖差，"
                  "只报一个会各自误导一个方向。它跟表里那个「带本体出处的药味占比」"
                  "**不是同一个分母**——前者问「本体够不够全」，后者问「这张方引没引」。", ""]
    else:
        lines += ["", "**指标③（语料侧）**：⏳ 算不了——"
                  + str(cov.get("note") or cov.get("reason") or "语料不在这台机器上"), ""]
    for g in GROUPS:
        lines.append(f"- **{g.key} {g.name}**：{g.asks}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries-path", default=str(ROOT / "tests" / "queries.txt"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--backend", default="fake", choices=["fake", "real"],
                    help="fake=只验管道（内容指标不出数）；real=走 LLM_MODE 配的那个")
    ap.add_argument("--groups", default="ABCD", help="跑哪几组，默认全跑")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=None, help="同时写一份 Markdown（默认 <out>.md）")
    ap.add_argument("--no-warmup", dest="warmup", action="store_false",
                    help="不要预热。默认预热一次（否则第一组会替所有人付惰性初始化的钱）")
    ap.set_defaults(warmup=True)
    args = ap.parse_args(argv)

    qpath = Path(args.queries_path)
    if not qpath.exists():
        print(f"主诉文件不在：{qpath}", file=sys.stderr)
        return 2
    complaints = [ln.strip() for ln in qpath.read_text(encoding="utf-8").splitlines()
                  if ln.strip() and not ln.startswith("#")]
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
    # 没有 cases.json 时给假后端造合成医案——理由与标注方式同 bench_consult
    if args.backend == "fake" and not cases_available():
        install_fake_cases(AUTO_FAKE_CASES_PER_PHYSICIAN)

    # **先空跑一次再开始计时。** 第一次 consult 要把本体、证候表、检索索引这些
    # 惰性对象建起来（沙盒实测：第一组的墙钟 1.44s，后面几组 0.05~0.09s——
    # 差的那 1.4 秒全是初始化）。不预热的话 A 组永远"最慢"，而那是排序造成的，
    # 不是这一组的性质。预热跑不通不算失败：那时第一组自己会如实报错。
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
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
