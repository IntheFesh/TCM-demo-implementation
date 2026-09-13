"""跑 SDT 并写提交文件。需要真实 LLM，所以在 eval/ 不在 tests/。

    python -m eval.sdt.run --sdt-dir <TCMEval>/evaluation/TCMEval-SDT \\
        --split Validation --solver chain --out out/sdt_chain.txt

成本（每条记录的调用数）：baseline 3 次（摘录/选项/小结），chain 5 次（多 S1+S2）。
Validation 50 条 => baseline 150 次、chain 250 次。两组都要跑才有对照，
所以一个 split 是 400 次调用——这个规模只该在能连 DeepSeek 的机器上跑。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.batch import classify_llm_failure, warn_if_failure_rate_high
from core.safety import safety_bypassed
from eval.sdt.adapter import SOLVERS, SdtAnswer
from eval.sdt.data import load_split, write_submission


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="在 TCMEval-SDT 上跑本项目的推理链")
    ap.add_argument("--sdt-dir", type=Path, required=True)
    ap.add_argument("--split", default="Validation", choices=["Train", "Validation", "Test"])
    ap.add_argument("--solver", default="chain", choices=sorted(SOLVERS))
    ap.add_argument("--out", type=Path, required=True)
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
    for i, r in enumerate(records, 1):
        # solver.solve() 内部三次 generate() 调用没有异常捕获——core.llm.generate()
        # 重试 3 次仍失败会把 LLMError 一路抛到这里。50 条 × 4 次调用一次抖动就
        # 崩掉整批、前面跑完的全丢，代价很大（跟 offline/extract_case_triples.py、
        # eval/run_eval.py、offline/estimate_epsilon.py 是同一类坑）。
        try:
            answer = solver.solve(r, ignore_safety_veto=bypass_arg)
        except Exception as e:  # noqa: BLE001 - 单条记录失败不能拖累其余记录
            call_failed.append(r.record_id)
            print(
                f"[eval.sdt.run] 第 {i}/{len(records)} 条（{r.record_id}）调用失败："
                f"{classify_llm_failure(e)}: {e}", file=sys.stderr,
            )
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
        if i % 10 == 0:
            print(f"  {i}/{len(records)}  已用调用 {calls} 次  {time.time() - t0:.0f}s")

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


if __name__ == "__main__":
    main()
