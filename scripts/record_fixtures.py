"""R3-2：用真实后端跑一遍录制清单，把每次 `generate()` 的
(schema, system, 原始输出) 录进 `fixtures/`。

    python -m scripts.record_fixtures --dry-run      # 先看清单和预估调用数，不花钱
    python -m scripts.record_fixtures                # 真录（需要 LLM_API_KEY）
    python -m scripts.record_fixtures --only react_off_1,insufficient

录一次能用很久：之后 `LLM_MODE=replay` 就是零成本、零延迟、断网可用、每次结果
完全一致。退出码 0 = 全部场景跑完；1 = 有场景失败（失败的照样记下来，不中断
其余场景——一条主诉失败不该让 300 次调用的录制全丢）。

## 清单为什么长这样

**开 ReAct 和不开 ReAct 各录一遍**：开 ReAct 走 `s3_react` 模板、还多出工具
调用那几步，prompt 跟不开的完全不同，fixture 不共用。录制时把 `USE_REACT`
真的设成对应值（而不是只给 `consult()` 传参数），这样 fixture 元信息里的
`env` 是诚实的——回放时环境不一致才能被 `_env_diff` 抓出来。

**三种角色（patient / doctor / student）不需要各录一遍。** 实测确认：`consult()`
根本不知道 role 这个概念，role 只在 `api/main.py::_consult_response` 那一层裁剪
字段（`_filter_response_by_role`）、以及 `to_graph(role=...)` 少生成两层节点，
**没有一次 LLM 调用跟 role 有关**。所以一条主诉的 fixture 三种角色共用。
`scripts/verify_replay.py` 会真的把三种角色各跑一遍来证明这一点，不是嘴上说说。

**追问那条用 `ScriptedPatient` 而不是 `SimulatedPatient`**：后者每次追问要自己
调一次 LLM，它的回答又取决于那次调用，录制/回放的确定性就多了一层依赖；
`ScriptedPatient` 的回答是写死的，下游 S3 的 prompt 因此可复现。

**⚠ 追问路径的回放有个固有限制**：回答文本变了 -> 下游 prompt 变了 -> 未命中。
真实演示里访问者自己打字回答，跟录制时的回答一字不差的概率很低。所以对外演示
建议 `FAST_MODE=1`（追问 0 轮），追问那条 fixture 的用途是让这条代码路径在
回放下也能被验证，不是让任意回答都能命中。这一条写进了 README，不要当成 bug。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERIES_PATH = ROOT / "tests" / "queries.txt"

# 信息不足那条：S2 推不出任何证素，consult 在 S3 之前就返回 insufficient。
# 录它是为了让"系统承认自己信息不够"这条路径在演示里也走得通。
INSUFFICIENT_COMPLAINT = "胸闷气短"

# 追问场景：这条主诉信息少、会触发追问；ScriptedPatient 对"纳差/腹胀"答有，
# 其余答没有。present 里的词要跟 core/tools.py::question_candidates 生成的问题
# 对得上才有意义（ScriptedPatient 是按子串匹配作答的）。
FOLLOWUP_COMPLAINT = "胃脘胀痛"
FOLLOWUP_PRESENT = ["纳差", "腹胀", "嗳气"]

# 每条主诉的预估调用数，用来在 --dry-run 里报预算。实测量级，不是精确值：
#   不开 ReAct：S1 1 + S2 1 + 每位医家 S3 1 + 残差 1
#   开 ReAct：每位医家多出 MAX_STEPS 量级的工具调用 + 一次收束
CALLS_PER_CONSULT_NO_REACT = 6
CALLS_PER_CONSULT_REACT = 20


@dataclass(frozen=True)
class Scenario:
    """一个录制场景。**record 和 verify 共用这份定义**（verify 直接 import
    RECORD_PLAN）：两边各写一份清单的话，"录了什么"和"验了什么"会漂移，
    而回放的全部价值就在于两者一致。"""

    name: str
    complaint: str
    use_react: bool
    followup_present: tuple[str, ...] | None = None

    @property
    def estimated_calls(self) -> int:
        return CALLS_PER_CONSULT_REACT if self.use_react else CALLS_PER_CONSULT_NO_REACT

    def ask_fn(self):
        """不追问就返回 None（`consult()` 收到 None 就不进追问循环）。"""
        if self.followup_present is None:
            return None
        from eval.patient_sim import ScriptedPatient

        return ScriptedPatient(present=list(self.followup_present))


def _queries(path: Path = DEFAULT_QUERIES_PATH) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def build_plan(queries: list[str] | None = None) -> list[Scenario]:
    """录制清单。queries 不传就读 tests/queries.txt 全部 10 条（含第 10 条
    黑便——那条会被安全否决拦在 S2 之前，0 次调用，录它是为了证明拦截路径在
    回放下照样拦）。"""
    queries = queries if queries is not None else _queries()
    plan = [Scenario(f"react_off_{i}", q, use_react=False)
            for i, q in enumerate(queries, start=1)]
    plan += [Scenario(f"react_on_{i}", q, use_react=True)
             for i, q in enumerate(queries, start=1)]
    plan.append(Scenario("insufficient", INSUFFICIENT_COMPLAINT, use_react=False))
    plan.append(Scenario("followup", FOLLOWUP_COMPLAINT, use_react=False,
                         followup_present=tuple(FOLLOWUP_PRESENT)))
    return plan


RECORD_PLAN = build_plan


def run_scenario(scenario: Scenario, recorder) -> dict:
    """跑一个场景。**USE_REACT 真的设进环境**，理由见模块文档字符串。

    consult() 的 use_react 也显式传：环境变量是给 fixture 元信息用的诚实记录，
    显式参数是给这次调用用的确定条件，两者一致但各有各的用处（只设环境变量的话
    这个脚本的行为就依赖"consult 会去读环境"这个间接事实）。
    """
    from core import chain

    previous = os.environ.get("USE_REACT")
    os.environ["USE_REACT"] = "1" if scenario.use_react else "0"
    before = recorder.n_written
    t0 = time.time()
    try:
        outcome = chain.consult(scenario.complaint, use_react=scenario.use_react,
                               ask_fn=scenario.ask_fn())
        error = None
    except Exception as e:  # noqa: BLE001 - 一个场景失败不能让整批录制丢掉
        outcome, error = None, f"{type(e).__name__}: {e}"
    finally:
        if previous is None:
            os.environ.pop("USE_REACT", None)
        else:
            os.environ["USE_REACT"] = previous
    from core.llm_replay import canonical_outcome

    return {
        "scenario": scenario.name,
        "complaint": scenario.complaint,
        "use_react": scenario.use_react,
        "n_fixtures": recorder.n_written - before,
        "elapsed_s": round(time.time() - t0, 1),
        "error": error,
        "rejected": bool(outcome and outcome.get("rejected")),
        "insufficient": bool(outcome and outcome.get("insufficient")),
        "llm_calls": (outcome or {}).get("manifest", {}).get("llm_calls"),
        # 录制时的结论快照，写进 _baseline.json 给 verify_replay 验"逐字节一致"。
        # 用 core/llm_replay.canonical_outcome 而不是在这里另写一套摘要：
        # "什么算同一个输出"只能有一处定义。
        "canonical": canonical_outcome(outcome) if outcome else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="R3-2：录制回放 fixture")
    ap.add_argument("--queries-path", type=Path, default=DEFAULT_QUERIES_PATH)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="fixture 目录，默认 fixtures/（也可用 REPLAY_FIXTURES_DIR）")
    ap.add_argument("--only", default="",
                    help="只跑这些场景名（逗号分隔），逐条补录时用")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印清单和预估调用数，不发任何请求")
    args = ap.parse_args(argv)

    plan = build_plan(_queries(args.queries_path))
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        unknown = wanted - {s.name for s in plan}
        if unknown:
            raise SystemExit(f"--only 里这些场景名不存在：{sorted(unknown)}")
        plan = [s for s in plan if s.name in wanted]

    from core.llm_replay import REPLAY_RELEVANT_ENV, current_env, fixtures_dir

    out_dir = args.out_dir or fixtures_dir()
    total = sum(s.estimated_calls for s in plan)
    print(f"录制清单 {len(plan)} 个场景，预估 {total} 次调用（实测量级，不是精确值）")
    print(f"fixture 目录：{out_dir}")
    print(f"录制时的相关环境变量（会写进每份 fixture 的 meta.env）：{current_env()}")
    print(f"（只记这几个：{', '.join(REPLAY_RELEVANT_ENV)}——"
          "它们会改变链路或 prompt。LLM_API_KEY 这类既不影响 prompt 又敏感的一律不记）")
    print()
    for s in plan:
        tag = "ReAct" if s.use_react else "     "
        follow = "　+追问" if s.followup_present else ""
        print(f"  {s.name:<16}{tag}  ≈{s.estimated_calls:>3} 次{follow}  {s.complaint[:34]}")
    print()

    if args.dry_run:
        print("--dry-run：不发起任何调用。确认无误后去掉这个参数重跑。")
        return 0

    from core import llm as llm_mod
    from core.llm_replay import RecordingBackend

    if os.environ.get("LLM_MODE") == "replay":
        print("LLM_MODE=replay 时不能录制（那是拿回放当录制源，录出来的是空壳）。"
              "把它改成 api / local / claude_cli 再跑。", file=sys.stderr)
        return 1

    recorder = RecordingBackend(out_dir=out_dir)
    # 换掉单例而不是设环境变量：录制要包在**真实后端**外面，而 get_backend()
    # 按 LLM_MODE 返回的是那个真实后端，包装这件事没有对应的环境变量。
    llm_mod._llm_singleton = recorder
    print(f"内层真实后端：{recorder.backend_id()}　模型：{recorder.model_name()}")
    print()

    results = []
    t0 = time.time()
    for i, scenario in enumerate(plan, start=1):
        r = run_scenario(scenario, recorder)
        results.append(r)
        state = ("失败" if r["error"] else
                 "安全否决" if r["rejected"] else
                 "信息不足" if r["insufficient"] else "正常")
        print(f"  [{i}/{len(plan)}] {scenario.name:<16}{state:<6}"
              f"新增/覆盖 {r['n_fixtures']:>3} 条 fixture　{r['elapsed_s']:>5}s")
        if r["error"]:
            print(f"      错误：{r['error']}", file=sys.stderr)

    # 写基线：没有它，verify_replay 只能验"没未命中"，验不了"逐字节一致"。
    from core.llm_replay import BASELINE_FILENAME

    baseline = {r["scenario"]: r["canonical"] for r in results if r["canonical"]}
    baseline_path = out_dir / BASELINE_FILENAME
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [r for r in results if r["error"]]
    print()
    print(f"录制基线：{baseline_path}（{len(baseline)} 个场景，verify_replay 用它验逐字节一致）")
    print(f"共写入 {recorder.n_written} 次（去重后 {len(recorder.keys_written)} 条 fixture）"
          f"，用时 {time.time() - t0:.0f}s")
    print(f"fixture 目录：{out_dir}")
    if failed:
        print(f"\n【失败】{len(failed)}/{len(results)} 个场景失败："
              f"{[r['scenario'] for r in failed]}。这些场景的 fixture 不全，"
              "回放到它们会未命中——用 --only 补录。", file=sys.stderr)
        return 1
    print("\n全部场景跑完。下一步：`python -m scripts.verify_replay`（退出码 0 = 回放"
          "逐字节一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
