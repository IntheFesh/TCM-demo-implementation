"""R3-2 续：用 `ReplayBackend` 重跑录制清单，断言输出跟录制时**逐字节一致**。

    python -m scripts.verify_replay                 # 退出码 0 = 全部一致
    python -m scripts.verify_replay --only followup

退出码：0 = 全部场景命中且逐字节一致；1 = 有未命中或不一致；2 = 没得验
（fixtures 目录空/不存在——那不是"验失败"，是"还没录"）。

**为什么要有这个脚本**：录制那一步只证明"调用发生过"，不证明"回放能把同样的
链路走完"。真实会踩的坑是链路里某一步的 prompt 带了录制时才有的东西（时间戳、
随机排序、并发顺序），录进去了但回放时算出的 key 不一样 -> 未命中。这种问题
只有真的用 ReplayBackend 跑一遍才会暴露，读 fixture 文件看不出来。

**演示前必须跑一次。** 未命中在演示现场表现成一个报错弹窗；在这里表现成一个
退出码 1 和一条写清了 schema / prompt 前 100 字 / sha12 / 环境差异的错误消息。

三种角色（patient / doctor / student）在这里真的各跑一遍 `api/main.py` 的响应
裁剪，用来证明"role 不需要各录一遍 fixture"这个判断（role 只在响应层裁剪字段，
没有一次 LLM 调用跟它有关）——这条不靠注释声称，靠跑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 三种角色都要能渲染出结果。researcher 是默认角色，另外三种是会裁字段的。
ROLES = ("patient", "doctor", "student")


def _run_once(scenario, use_replay: bool) -> tuple[str | None, str | None]:
    """跑一个场景，返回 (canonical 文本, 错误)。use_replay 决定用哪个后端。"""
    from core import chain
    from core import llm as llm_mod

    previous = os.environ.get("USE_REACT")
    os.environ["USE_REACT"] = "1" if scenario.use_react else "0"
    # 回放后端每个场景新建一个：n_hits 计数要按场景报，而且不该让上一个场景的
    # 惰性缓存影响下一个（缓存本身是对的，但"每次都重新装载也能命中"是更强的
    # 性质，值得顺带验到）。
    if use_replay:
        from core.llm_replay import ReplayBackend

        llm_mod._llm_singleton = ReplayBackend()
    try:
        outcome = chain.consult(scenario.complaint, use_react=scenario.use_react,
                               ask_fn=scenario.ask_fn())
        from core.llm_replay import canonical_outcome

        return canonical_outcome(outcome), None
    except Exception as e:  # noqa: BLE001 - 未命中/不一致都要作为结果报出，不崩
        return None, f"{type(e).__name__}: {e}"
    finally:
        if previous is None:
            os.environ.pop("USE_REACT", None)
        else:
            os.environ["USE_REACT"] = previous


def _check_roles(scenario) -> list[str]:
    """把一个场景的结论过一遍三种角色的响应裁剪。返回问题清单（空 = 通过）。

    走的是 api/main.py 真实那个函数，不是另写一遍裁剪逻辑——要验的就是"演示时
    那条路"在回放下能不能跑通。"""
    from api.main import _consult_response
    from core import chain
    from core import llm as llm_mod
    from core.llm_replay import ReplayBackend

    problems: list[str] = []
    previous = os.environ.get("USE_REACT")
    os.environ["USE_REACT"] = "1" if scenario.use_react else "0"
    try:
        llm_mod._llm_singleton = ReplayBackend()
        outcome = chain.consult(scenario.complaint, use_react=scenario.use_react,
                               ask_fn=scenario.ask_fn())
        for role in ROLES:
            try:
                response = _consult_response(outcome, role=role)
            except Exception as e:  # noqa: BLE001
                problems.append(f"role={role} 渲染失败：{type(e).__name__}: {e}")
                continue
            if role == "researcher" and not response.get("manifest"):
                problems.append(f"role={role} 少了 manifest")
    except Exception as e:  # noqa: BLE001
        problems.append(f"三角色检查跑不起来：{type(e).__name__}: {e}")
    finally:
        if previous is None:
            os.environ.pop("USE_REACT", None)
        else:
            os.environ["USE_REACT"] = previous
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="R3-2：验证回放跟录制逐字节一致")
    ap.add_argument("--queries-path", type=Path, default=None)
    ap.add_argument("--only", default="", help="只验这些场景名（逗号分隔）")
    ap.add_argument("--baseline", type=Path, default=None,
                    help="录制时存下来的基线文件，默认 fixtures 目录下的 "
                         "_baseline.json（record_fixtures 自动写）。基线缺失时"
                         "只验「回放能跑通且不未命中」，并在结尾如实说明。")
    args = ap.parse_args(argv)

    from core.llm import LLMError
    from core.llm_replay import ReplayBackend, fixtures_dir, require_matching_mode
    from scripts.record_fixtures import _queries, build_plan

    # R28：**先比对检索模式，再干别的**。fixture 的键含 system 全文，
    # 模式不一致时每一条都不会命中——那时"验证失败"的原因不是漏录，
    # 而是这批录音根本不属于当前配置。退出码 3 跟"还没录"（2）分开：
    # 前者要切模式或重录，后者要去录。
    try:
        require_matching_mode(fixtures_dir())
    except LLMError as e:
        print(str(e), file=sys.stderr)
        return 3

    queries = _queries(args.queries_path) if args.queries_path else None
    plan = build_plan(queries)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        unknown = wanted - {s.name for s in plan}
        if unknown:
            raise SystemExit(f"--only 里这些场景名不存在：{sorted(unknown)}")
        plan = [s for s in plan if s.name in wanted]

    probe = ReplayBackend()
    info = probe.replay_info()
    print(f"fixtures 目录：{fixtures_dir()}")
    if info is None:
        print("fixtures 目录空或不存在——先跑 `python -m scripts.record_fixtures`。"
              "这不是「验失败」，是「还没录」，所以退出码是 2。", file=sys.stderr)
        return 2
    print(f"已装载 {info['n_fixtures']} 条 fixture，"
          f"录制于 {info['recorded_at']}（最新 {info['recorded_at_latest']}），"
          f"模型 {info['model']}，commit {info['git_commit']}，{info['n_batches']} 个批次")
    print(f"comparability_warning：{probe.comparability_warning()}")
    print()

    from core.llm_replay import BASELINE_FILENAME

    baseline_path = args.baseline or (fixtures_dir() / BASELINE_FILENAME)
    baseline: dict[str, str] = {}
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        print(f"录制基线：{baseline_path}（{len(baseline)} 个场景）")
    else:
        print(f"没有录制基线（{baseline_path} 不存在）——只能验「回放跑得通、"
              "不未命中」，验不了逐字节一致。基线由 record_fixtures 自动写。")
    print()

    failures: list[str] = []
    for i, scenario in enumerate(plan, start=1):
        canonical, error = _run_once(scenario, use_replay=True)
        if error:
            print(f"  [{i}/{len(plan)}] {scenario.name:<16}✗ 跑不通")
            print(f"      {error}", file=sys.stderr)
            failures.append(f"{scenario.name}：{error.splitlines()[0]}")
            continue
        expected = baseline.get(scenario.name)
        if expected is None:
            print(f"  [{i}/{len(plan)}] {scenario.name:<16}★ 命中（无基线，只验跑通）")
        elif expected == canonical:
            print(f"  [{i}/{len(plan)}] {scenario.name:<16}★ 逐字节一致")
        else:
            print(f"  [{i}/{len(plan)}] {scenario.name:<16}✗ 与基线不一致")
            print(f"      基线长度 {len(expected)}，本次 {len(canonical)}；"
                  f"首个不同的位置 {_first_diff(expected, canonical)}", file=sys.stderr)
            failures.append(f"{scenario.name}：与录制基线不一致")

    print()
    role_problems: list[str] = []
    for scenario in plan[:1]:  # 一条就够：role 不进 prompt，跑全部只是重复
        role_problems = _check_roles(scenario)
        print(f"三角色响应裁剪（{scenario.name}，走 api/main.py::_consult_response）："
              f"{'全部通过' if not role_problems else '有问题'}")
        for p in role_problems:
            print(f"  ✗ {p}", file=sys.stderr)
    print("  （role 不需要各录一遍 fixture：它只在响应层裁剪字段，"
          "没有一次 LLM 调用跟它有关——这一行就是在证明这件事）")
    print()

    print("=" * 66)
    if not failures and not role_problems:
        print(f"★命中：{len(plan)} 个场景全部回放成功"
              + ("、且跟录制基线逐字节一致。" if baseline else
                 "。（没传 --baseline，只验到了「不未命中」；要验逐字节一致，"
                 "录制时把 canonical 基线存下来再传进来。）"))
        return 0
    print(f"✗ 未通过：{len(failures)} 个场景有问题" +
          ("，另外三角色检查也没过。" if role_problems else "。"), file=sys.stderr)
    for f in failures:
        print(f"  - {f}", file=sys.stderr)
    return 1


def _first_diff(a: str, b: str) -> int:
    """第一个不同字符的下标；完全相同返回 -1。给"不一致"报个能定位的数，
    不是只说"不一样"。"""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    return -1 if a == b else min(len(a), len(b))


if __name__ == "__main__":
    sys.exit(main())
