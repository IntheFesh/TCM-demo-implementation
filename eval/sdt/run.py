"""跑 SDT 并写提交文件。需要真实 LLM，所以在 eval/ 不在 tests/。

    python -m eval.sdt.run --sdt-dir <TCMEval>/evaluation/TCMEval-SDT \\
        --split Validation --solver chain --out out/sdt_chain.txt

成本（每条记录的调用数）：baseline 3 次（摘录/选项/小结），chain 5 次（多 S1+S2）。
Validation 50 条 => baseline 150 次、chain 250 次。两组都要跑才有对照，
所以一个 split 是 400 次调用——这个规模只该在能连 DeepSeek 的机器上跑。

**失分分析（R2-1）：零 LLM 调用，对已有提交文件重新聚合。**

    python -m eval.sdt.run --sdt-dir $SDT --split Test \
        --error-analysis out/sdt_chain_v2.txt

带了 --error-analysis 就只做聚合：不构造 solver、不碰 get_llm、不写提交文件。
逐条四项得分、多选率 vs 少选率及两者的边际代价、完全对/部分对/完全错分布、
失分最多的 10 条、按病机/证型分组的得分——见 eval/sdt/error_analysis.py。

**过拟合护栏（R2-3）：--split Test 时会打醒目提醒并报出本项目已经跑过几次**
（台账 eval/sdt/test_run_log.jsonl，每次跑 Test 追加一条）。prompt 改动先在
Train 上验证方向，Test 只在最终定型后跑一次。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.batch import classify_llm_failure, warn_if_failure_rate_high
from core.progress import Progress
from core.safety import safety_bypassed
from eval.sdt import runlog
from eval.sdt.adapter import SOLVERS, SdtAnswer
from eval.sdt.data import load_split, write_submission


def _run_error_analysis(args) -> None:
    """R2-1 模式。**这个函数里不许出现任何 LLM 调用**——它的全部价值就是零成本
    地把已经算出来的分拆开看（tests/test_sdt_error_analysis.py 有一条测试用
    "一调用就抛异常"的假后端钉住这一点）。"""
    from eval.sdt.error_analysis import analyze, print_report

    analysis = analyze(args.sdt_dir, args.split, args.error_analysis,
                       diagnose_bom=args.diagnose_bom)
    print_report(analysis)

    # 算分也是一次 Test 暴露的凭据（虽然零调用、不构成新的暴露），记进台账让
    # 分数跟那次跑对得上——run 事件发生时分还没算出来。
    # 台账记官方口径那个分（可跟论文比）；Train 那条路没有官方总分时退回我们
    # 逐条加权算的数，并在台账里标明是哪一种——两个口径混在一栏里会让以后
    # 对比几次跑的人不知道自己在比什么。
    official = analysis["official_total"]
    entry = runlog.log_scored(
        args.split, args.error_analysis,
        official if official is not None else analysis["weighted_total"],
        score_kind="official_automated_score" if official is not None else "weighted_from_breakdown",
    )
    if entry:
        print(f"已记入 Test 跑次台账 {runlog.LOG_PATH_DISPLAY}（scored 事件，"
              f"零 LLM 调用、不计入暴露次数）。")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="在 TCMEval-SDT 上跑本项目的推理链")
    ap.add_argument("--sdt-dir", type=Path, required=True)
    ap.add_argument("--split", default="Validation", choices=["Train", "Validation", "Test"])
    ap.add_argument("--solver", default="chain", choices=sorted(SOLVERS))
    ap.add_argument("--out", type=Path, default=None,
                    help="提交文件写到哪（跑分模式必填；--error-analysis 模式不需要）")
    ap.add_argument(
        "--error-analysis", type=Path, default=None, metavar="SUBMISSION",
        help="R2-1 失分分析模式：对这份已有的提交文件重新聚合，**不发起任何 LLM "
             "调用**。取路径而不是做成 flag 是刻意的——这样在结构上就不可能"
             "一边分析一边又去跑 solver。",
    )
    ap.add_argument(
        "--diagnose-bom", action="store_true",
        help="失分分析时剥掉官方金标准的 BOM（只影响 Validation）。默认不剥，"
             "跟官方 evaluate.py 一致；剥掉算出来的是诊断值，不可跟论文比。",
    )
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，先看质量")
    ap.add_argument(
        "--only-ids", default="",
        help="只跑这些病案 ID（逗号分隔）。量化安全否决的代价时用：被拦下的记录"
             "才需要用 --ignore-safety-veto 重跑一遍，其余记录两次的输入完全相同，"
             "重跑只是白花钱。跑完把这几行并回主提交文件即可。",
    )
    ap.add_argument(
        "--ignore-safety-veto", action="store_true",
        help="绕过危重症状拦截。默认不绕——被拦的记录产出空答案得 0 分，那是这套"
             "系统真实的行为。用这个开关跑出来的分必须单独标注，不能混进主结果。"
             "不传这个开关时回落到环境变量 EVAL_MODE（同样默认关）。",
    )
    args = ap.parse_args(argv)

    if args.error_analysis is not None:
        # **在构造 solver / import get_llm 之前就 return**：这个模式的全部价值
        # 在于"零成本"，任何一次调用都会破坏它。
        return _run_error_analysis(args)

    if args.out is None:
        ap.error("--out 是跑分模式的必填参数（只做失分分析请用 --error-analysis）")

    if args.split == "Test":
        # 过拟合护栏：把"已经跑过几次"摆在人眼前。只提醒、不拦——真到了最终
        # 定型那一次，拦住就没法跑了；判断该不该跑是人的事，这里负责让人
        # 在有信息的情况下判断。
        print(runlog.test_split_warning(runlog.read_log()))
        print()

    records = load_split(args.sdt_dir, args.split)
    # 不在这里 attach_gold：solver 只读 clinical_data 和选项，金标准谁也不看，
    # 白读一遍不说，Results/*.txt 缺失还会让整次跑直接挂。金标准是官方
    # evaluate.py 计分时用的，不是生成答案时用的。
    if args.only_ids:
        wanted = {x.strip() for x in args.only_ids.split(",") if x.strip()}
        missing = wanted - {r.record_id for r in records}
        if missing:
            raise SystemExit(f"--only-ids 里这些病案 ID 不在 {args.split} 里：{sorted(missing)}")
        records = [r for r in records if r.record_id in wanted]
    if args.limit:
        records = records[: args.limit]

    # 传了开关就一定旁路；没传则交给 safety_bypassed 读 EVAL_MODE。传 False 会
    # 显式压掉环境变量，那样 EVAL_MODE 对 SDT 永远不生效，不是想要的行为。
    bypass_arg = True if args.ignore_safety_veto else None
    bypass_effective = safety_bypassed(bypass_arg)
    if bypass_effective:
        source = "--ignore-safety-veto" if args.ignore_safety_veto else "环境变量 EVAL_MODE"
        print(f"【警告】已绕过安全否决层（来自 {source}）。这一轮的分数不代表本系统的"
              "实际行为，引用时必须标注 ignore_safety_veto=True。")

    solver = SOLVERS[args.solver]()
    lines, rejected, call_failed, calls = [], [], [], 0
    t0 = time.time()
    # R9：原来是每 10 条打一行（`i % 10`），换成统一进度组件——粒度到每一条，
    # 而且带速度和剩余时间（50 条 × 4 次调用的批次，"还要多久"是真问题）。
    bar = Progress(total=len(records), label=f"SDT {args.split}（{args.solver}）", unit="条")
    for i, r in enumerate(records, 1):
        # solver.solve() 内部三次 generate() 调用没有异常捕获——core.llm.generate()
        # 重试 3 次仍失败会把 LLMError 一路抛到这里。50 条 × 4 次调用一次抖动就
        # 崩掉整批、前面跑完的全丢，代价很大（跟 offline/extract_case_triples.py、
        # eval/run_eval.py、offline/estimate_epsilon.py 是同一类坑）。
        try:
            answer = solver.solve(r, ignore_safety_veto=bypass_arg)
            bar.advance(note=f"{r.record_id}")
        except Exception as e:  # noqa: BLE001 - 单条记录失败不能拖累其余记录
            call_failed.append(r.record_id)
            print(
                f"[eval.sdt.run] 第 {i}/{len(records)} 条（{r.record_id}）调用失败："
                f"{classify_llm_failure(e)}: {e}", file=sys.stderr,
            )
            bar.note(f"第 {i}/{len(records)} 条（{r.record_id}）调用失败："
                     f"{classify_llm_failure(e)}")
            # 官方评分脚本按记录数算总分，提交文件少一行就会跟金标准错位——
            # 必须占位提交一条空答案（跟安全否决同样的空壳，得 0 分），但绝不
            # 写进 answer.safety_rejected：那个字段代表"系统真的拦截了这条"，
            # 调用失败是基础设施抖动，不是系统的真实行为，两者混在一起会让
            # 读分数的人误以为这条是被安全层拦下的。call_failed 单独记录，
            # 跟 rejected 分开报——见 main() 末尾打的警告和 manifest 里的
            # n_call_failed。
            answer = SdtAnswer(record_id=r.record_id)
        lines.append(answer.to_line())
        calls += answer.llm_calls
        if answer.safety_rejected:
            rejected.append(r.record_id)
    bar.close(f"已用调用 {calls} 次")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_submission(args.out, lines)

    from core.llm import get_llm
    manifest = {
        "solver": solver.name,
        "split": args.split,
        "n_records": len(records),
        "llm_calls": calls,
        "elapsed_s": round(time.time() - t0, 1),
        "model": get_llm().model_name(),
        "backend": get_llm().backend_id(),
        "comparability_warning": get_llm().comparability_warning(),
        # 记实际生效值，不是 args 值：EVAL_MODE 生效而没传开关时，args 是 False，
        # 照抄进 manifest 就等于让"这个数字怎么来的"这份唯一凭据撒谎。
        "ignore_safety_veto": bypass_effective,
        "ignore_safety_veto_source": (
            "--ignore-safety-veto" if args.ignore_safety_veto
            else ("EVAL_MODE" if bypass_effective else None)
        ),
        # 被安全层拦下的记录：它们提交的是空答案、得 0 分。这个数必须跟分数
        # 一起报，否则读的人会以为是模型答错了。
        "safety_rejected": rejected,
        # 调用失败（跟安全否决不是一回事，见上面循环里的注释）也提交了空答案、
        # 也得 0 分——但这些 0 分不代表模型的真实表现，是基础设施抖动。
        # 单独记数，不跟 safety_rejected 混在一起，报告消费方要能分清楚
        # "这批分数里有多少是系统真的拦下的、多少是这次跑巧合失败的"。
        "call_failed": call_failed,
        "n_call_failed": len(call_failed),
    }
    manifest_path = args.out.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写出 {args.out}")
    print(f"manifest：{manifest_path}")
    print(f"安全否决拦下 {len(rejected)}/{len(records)} 条（这些条得 0 分）：{rejected}")
    if call_failed:
        print(
            f"【注意】{len(call_failed)}/{len(records)} 条因 LLM 调用失败提交了空答案、"
            f"得 0 分——这些 0 分不是模型的真实表现，是这次跑的基础设施抖动。"
            f"本次总分不能跟一次完整无失败的跑直接比较：{call_failed}"
        )
    warn_if_failure_rate_high("SDT", len(call_failed), len(records))

    # 台账只记 Test（log_run 内部判断）：Train/Validation 本来就该反复跑。
    # --only-ids / --limit 跑的不是完整 50 条，标成 partial，跟完整跑分开数。
    logged = runlog.log_run(
        args.split, solver.name, args.out, len(records),
        partial=bool(args.only_ids or args.limit),
        ignore_safety_veto=bypass_effective,
        model=manifest["model"], backend=manifest["backend"],
    )
    if logged:
        print(f"\n已记入 Test 跑次台账 {runlog.LOG_PATH_DISPLAY}："
              f"第 {runlog.count_test_runs(runlog.read_log())['total']} 次"
              f"（commit {logged['git_commit']}，prompt {logged['prompt_version']}）。"
              f"分数由 --error-analysis 或 eval/sdt/score.py 算出后另记一条。")


if __name__ == "__main__":
    main()
