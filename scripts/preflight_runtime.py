"""运行时自检：一条命令回答「当前环境能不能按默认配置对外展示」。

环境变量残留不会报错，只会让系统的行为与默认配置不一致（例如 `RETRIEVER_MODE`
残留时运行的不是默认检索模式，页面上也没有任何提示）。这个脚本把这类检查做成
带退出码的检查器：

    python -m scripts.preflight_runtime            # 退出码 0 = 可以开始
    python -m scripts.preflight_runtime --strict   # 提醒项也算失败
    python -m scripts.preflight_runtime --json     # 机读输出

每条检查有三种结果：

  ok    —— 查过了，没问题
  warn  —— 查过了，有隐患但不影响运行（默认不影响退出码，`--strict` 下影响）
  fail  —— 不能运行，每条 fail 都带一句具体怎么修

没有"跳过"这一档：查不了的项（比如远端缓存）归 warn 并说明原因。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 按默认配置运行时不该留在环境里的变量，每个都配一句"留着会怎样"。
LEFTOVER_ENV = {
    "RETRIEVER_MODE": "运行的不是默认的 full_context 检索模式，页面上也没有任何提示",
    "USE_REACT": "调用数和耗时都会成倍增加，与默认配置不一致",
    "EVAL_MODE": "安全否决不再中止链路，危重主诉会照常出方",
    "S3_BEST_OF_N": "采样次数与默认值不一致，额度折算也会出错",
    "S3_REASONING_EFFORT": "推理档位与默认不一致，耗时和费用都会变化",
    "S3_THINKING": "关闭思考的结果与默认配置不可比（manifest 会带警告）",
    "LORA_DIR": "本地后端会加载 adapter，运行的不是基座模型",
}

# 对外展示建议的设置。值不对只提醒，不拦截：需要实时调用时照常可以运行。
EXPECTED_ENV = {
    "LLM_MODE": ("replay", "对外展示建议使用录制回放：零成本、可离线、每次结果一致"),
    "FAST_MODE": ("1", "FAST_MODE 下追问 0 轮、ReAct 2 步、采样 1 次，响应更快"),
}

STATUS_ORDER = {"fail": 0, "warn": 1, "ok": 2}


@dataclass
class Check:
    name: str
    status: str          # ok / warn / fail
    detail: str
    fix: str = ""

    def as_dict(self) -> dict:
        out = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.fix:
            out["fix"] = self.fix
        return out


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *args, **kwargs) -> None:
        self.checks.append(Check(*args, **kwargs))

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c.status == "fail")

    @property
    def n_warn(self) -> int:
        return sum(1 for c in self.checks if c.status == "warn")

    def exit_code(self, strict: bool = False) -> int:
        if self.n_fail:
            return 1
        return 1 if (strict and self.n_warn) else 0


def check_leftover_env(env: dict | None = None) -> list[Check]:
    env = os.environ if env is None else env
    out = []
    for var, why in LEFTOVER_ENV.items():
        if env.get(var):
            out.append(Check(f"环境残留 {var}", "fail",
                             f"{var}={env[var]!r}：{why}",
                             f"unset {var}"))
    if not out:
        out.append(Check("环境残留", "ok", "七个会改变默认行为的变量都没设"))
    return out


def check_expected_env(env: dict | None = None) -> list[Check]:
    env = os.environ if env is None else env
    out = []
    for var, (want, why) in EXPECTED_ENV.items():
        got = env.get(var)
        if got == want:
            out.append(Check(f"展示设置 {var}", "ok", f"{var}={want}"))
        else:
            out.append(Check(f"展示设置 {var}", "warn",
                             f"{var}={got!r}，建议 {want}：{why}",
                             f"export {var}={want}"))
    return out


def check_fixtures(root: Path = ROOT) -> Check:
    """回放模式要有录制好的 fixture。这里只查在不在；回放与录制是否一致由
    `verify_replay` 检查，那要完整回放一遍，不适合放进秒级自检。"""
    d = root / "fixtures"
    files = sorted(p for p in d.glob("*.json")) if d.is_dir() else []
    if files:
        return Check("录制 fixture", "ok", f"{d.name}/ 下 {len(files)} 份录制")
    return Check("录制 fixture", "warn",
                 "fixtures/ 下没有任何录制——回放模式会当场失败",
                 "python -m scripts.record_fixtures（要真实 key），"
                 "或改用实时调用（LLM_MODE=api）")


def check_graph(root: Path = ROOT) -> Check:
    p = root / "data" / "graph.json"
    if p.exists() and p.stat().st_size > 0:
        return Check("知识图谱", "ok", f"data/graph.json 在（{p.stat().st_size // 1024} KB）")
    return Check("知识图谱", "fail", "data/graph.json 不在——图谱浏览器页会是空的",
                 "python -m offline.build_graph && python -m offline.graph_stats")


def check_fonts(root: Path = ROOT) -> Check:
    """子集字体在不在。不在也能运行（CDN 兜底），但离线时会退回系统字体，
    而回放模式的用途之一就是离线运行。"""
    d = root / "web" / "vendor" / "fonts"
    subsets = sorted(d.glob("*-subset.woff2")) if d.is_dir() else []
    css = (root / "web" / "app.css").read_text(encoding="utf-8")
    local = "vendor/fonts/" in css
    if subsets and local:
        return Check("离线字体", "ok", f"{len(subsets)} 个子集字体已接进 app.css")
    if subsets and not local:
        return Check("离线字体", "warn",
                     f"生成了 {len(subsets)} 个子集字体，但 app.css 还指着 CDN",
                     "把 subset_fonts 打印的四段 @font-face 贴进 web/app.css")
    return Check("离线字体", "warn",
                 "还在用 CDN 字体：离线时会退回系统字体（宋/黑两族的区分没了）",
                 "python -m scripts.subset_fonts --download && python -m scripts.subset_fonts")


def check_physicians() -> Check:
    from core.llm import s3_mode
    from core.physicians import physicians_all, physicians_enabled, physicians_for_mode

    enabled = physicians_enabled()
    total = physicians_all()
    if not enabled:
        return Check("医家注册表", "fail", "一位启用的医家都没有",
                     "检查 core/physicians.py 的 enabled 字段")
    # 两个数都要报：`enabled` 回答"谁算三列集注的一员"（legacy），`in_synthesis`
    # 回答"谁参与综合分析"（structured，五位全在），一个字段答不了两个问题
    # （见 physicians_for_synthesis 的文档）。产品默认是 structured，只报"启用 3 位"
    # 会与界面上的「五家综合」对不上。
    mode = s3_mode()
    in_mode = physicians_for_mode(mode)
    return Check("医家注册表", "ok",
                 f"本次模式 {mode} 参与 {len(in_mode)} 位（"
                 + "、".join(info["name"] for info in in_mode.values())
                 + f"）；三列集注启用 {len(enabled)} 位 / 注册表共 {len(total)} 位")


def check_cases(root: Path = ROOT) -> Check:
    p = root / "cases.json"
    if not p.exists():
        return Check("医案语料", "fail",
                     "cases.json 不在——检索层一开口就 RetrievalUnavailable，"
                     "三位医家的 S3 一次都跑不了",
                     "python -m offline.extract_cases")
    try:
        n = len(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        return Check("医案语料", "fail", f"cases.json 读不了：{type(exc).__name__}",
                     "重新生成：python -m offline.extract_cases")
    return Check("医案语料", "ok", f"{n} 条诊次")


def check_quota() -> Check:
    """额度设置与当前采样次数是否匹配。一次问诊的调用数由 `calls_per_consult()` 按
    S3 模式、医家数与采样次数折算，`.env` 里写死的值会让"今日约剩 N 次"与实际不符。"""
    from core.usage import calls_per_consult

    per_consult = calls_per_consult()
    raw = os.environ.get("QUOTA_PER_IP_DAILY_CALLS")
    if not raw:
        return Check("额度折算", "ok",
                     f"没写死每 IP 额度，代码按 calls_per_consult()={per_consult} 现算")
    try:
        per_ip = int(raw)
    except ValueError:
        return Check("额度折算", "warn", f"QUOTA_PER_IP_DAILY_CALLS={raw!r} 不是整数",
                     "改成整数或直接不设（代码会现算）")
    consults = per_ip // per_consult
    if consults < 1:
        return Check("额度折算", "fail",
                     f"每 IP {per_ip} 次调用 ÷ 每次问诊 {per_consult} 次 = {consults} 次问诊",
                     f"至少设成 {per_consult * 5}（≈5 次问诊）或不设")
    return Check("额度折算", "ok",
                 f"每 IP {per_ip} 次调用 ≈ {consults} 次问诊（每次 {per_consult} 次调用）")


def check_prefix_cache() -> Check:
    """前缀缓存由服务商管理，一段时间不用会被清掉，对外展示前需要预热。
    本地查不到远端缓存的状态，所以归 warn 并给出预热方法。"""
    from core.retrieval_hybrid import DEFAULT_MODE, effective_mode

    mode = effective_mode()
    if mode != DEFAULT_MODE:
        return Check("前缀缓存预热", "warn",
                     f"当前检索模式是 {mode}，不是 {DEFAULT_MODE}，没有前缀缓存这回事",
                     f"unset RETRIEVER_MODE（回到 {DEFAULT_MODE}）")
    return Check("前缀缓存预热", "warn",
                 "远端缓存的状态在本地查不到（由服务商管理），一段时间不用会被清掉。"
                 "对外展示前先跑一次问诊预热",
                 "先跑一条主诉（第一次必然全部未命中，较慢），第二次起才是正常速度")


def check_credentials() -> Check:
    """文档里的评测数字与报告文件是否一致（与 `collect_results --check` 同一个核对器）。"""
    from scripts.collect_results import DEFAULT_CHECK_PATHS, check

    bad = []
    for path in DEFAULT_CHECK_PATHS:
        if not path.exists():
            continue
        result = check(path.read_text(encoding="utf-8"))
        if not result["ok"]:
            bad.append(path.name)
    if bad:
        return Check("凭据核对", "fail", "这几份文档里的数跟文件对不上：" + "、".join(bad),
                     "python -m scripts.collect_results --check 看具体哪一行")
    return Check("凭据核对", "ok", f"{len(DEFAULT_CHECK_PATHS)} 份文档的凭据记号全部一致")


def check_screenshots(root: Path = ROOT) -> Check:
    """界面状态截图（docs/screenshots/）。缺图不影响运行，但文档里的图会缺。"""
    d = root / "docs" / "screenshots"
    shots = sorted(d.glob("*.png")) if d.is_dir() else []
    if len(shots) >= 20:
        return Check("状态截图", "ok", f"{len(shots)} 张")
    return Check("状态截图", "warn", f"只有 {len(shots)} 张，文档里的图可能缺",
                 "python -m scripts.screenshot_states")


def run_all(root: Path = ROOT, env: dict | None = None) -> Report:
    report = Report()
    for c in check_leftover_env(env):
        report.checks.append(c)
    for c in check_expected_env(env):
        report.checks.append(c)
    report.checks.append(check_cases(root))
    report.checks.append(check_graph(root))
    report.checks.append(check_physicians())
    report.checks.append(check_fixtures(root))
    report.checks.append(check_fonts(root))
    report.checks.append(check_quota())
    report.checks.append(check_prefix_cache())
    report.checks.append(check_credentials())
    report.checks.append(check_screenshots(root))
    return report


ICON = {"ok": "✓", "warn": "!", "fail": "×"}


def print_report(report: Report, strict: bool) -> None:
    for c in sorted(report.checks, key=lambda c: STATUS_ORDER[c.status]):
        print(f"{ICON[c.status]} {c.name}：{c.detail}")
        if c.fix:
            print(f"    修：{c.fix}")
    print()
    if report.n_fail:
        print(f"{report.n_fail} 条不通过，先修复再启动。")
    elif report.n_warn:
        word = "--strict 下视为不通过" if strict else "不影响运行"
        print(f"{report.n_warn} 条提醒（{word}）。")
    else:
        print("全部通过，可以开始。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strict", action="store_true", help="连提醒也算失败")
    ap.add_argument("--json", action="store_true", help="机读输出")
    args = ap.parse_args(argv)

    report = run_all()
    if args.json:
        print(json.dumps({"checks": [c.as_dict() for c in report.checks],
                          "n_fail": report.n_fail, "n_warn": report.n_warn},
                         ensure_ascii=False, indent=2))
    else:
        print_report(report, args.strict)
    return report.exit_code(args.strict)


if __name__ == "__main__":
    sys.exit(main())
