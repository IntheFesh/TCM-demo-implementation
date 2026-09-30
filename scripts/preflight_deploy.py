"""部署前自检：一条命令检查目标机器能否运行本服务，每项要么通过、要么说清为什么不通过。

运行时的故障排查见 `docs/DEPLOYMENT.md`；这个脚本处理它前面那一步：服务还没启动，
先确认这台机器具备运行条件。下面这些问题都不会在启动时报错，只会在第一次问诊时暴露：

- 数据文件缺一个（`data/graph.json` 没有一起部署）→ 图谱页一片空白；
- vendor 目录不全（字体 / cytoscape / dagre）→ 离线时页面没有字体、图画不出来；
- 反向代理缓冲了 SSE → 流式输出变成长时间等待后一次性出现；
- 模型后端不可达或模型名不对 → 第一次调用才报错；
- 磁盘空间不足 → 审计日志追加失败；
- 端口被占用 / 时钟偏差 / 目录不可写。

每一项都能单独解释：不通过的项会说明"查的是什么、测到的是什么、该怎么办"。

## 退出码

    0  全部通过
    1  有阻断项不通过（缺数据文件、端口被占、目录不可写…）——不要上线
    2  只有警告项不通过（模型后端未配置、磁盘偏紧…）——能启动，但要知道代价

用法：

    python3 -m scripts.preflight_deploy                 # 全部
    python3 -m scripts.preflight_deploy --json          # 机器可读
    python3 -m scripts.preflight_deploy --skip-network  # 内网无外网时跳过可达性
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: 磁盘余量的两档：低于 500 MB 阻断（审计日志等运行时数据需要追加写入），
#: 低于 2 GB 警告。
DISK_BLOCK_MB = 500
DISK_WARN_MB = 2048

#: 时钟偏差上限。审计日志与病历系统对时间，偏几分钟就对不上号。
CLOCK_SKEW_WARN_S = 120


@dataclass
class Check:
    """一项。`blocking=True` 的红了就不要上线。"""

    id: str
    what: str            # 查的是什么
    blocking: bool
    ok: bool = False
    measured: str = ""   # 测到的是什么
    fix: str = ""        # 该怎么办
    skipped: str = ""    # 非空 = 这一项没查，原因写在这


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, c: Check) -> Check:
        self.checks.append(c)
        return c

    def exit_code(self) -> int:
        if any(not c.ok and not c.skipped and c.blocking for c in self.checks):
            return 1
        if any(not c.ok and not c.skipped for c in self.checks):
            return 2
        return 0


# ---------- 各项 ----------

#: 少一个就有一块功能静默失效的文件。**路径写全，不用通配**——
#: 通配匹配到 0 个跟匹配到 3 个在结果里长得一样。
REQUIRED_DATA = [
    ("data/graph.json", "知识图谱（图谱浏览器 + graph 检索模式）"),
    ("data/standard/syndromes.jsonl", "证候表（证型定义、节点释义）"),
    ("data/standard/materia_medica.jsonl", "本草本体（药理层、符号验证）"),
    ("data/standard/formulary.jsonl", "方剂本体"),
    ("data/standard/prescribing_patterns.jsonl", "名老中医用药规律层"),
    ("data/standard/effect_synonyms.tsv", "治法↔功效同义表（验证器要用）"),
    ("data/element_index.json", "证素索引（graph/hybrid 检索模式）"),
]

#: 前端的本地副本，离线运行（录制回放）时需要。
REQUIRED_WEB = [
    ("web/vendor/cytoscape.min.js", "图谱库"),
    ("web/vendor/dagre/dagre.min.js", "布局库"),
    ("web/vendor/fonts", "中文字体子集（断网时没有它页面是系统字体）"),
    ("web/index.html", "页面骨架"),
    ("web/app.js", "前端逻辑"),
    ("web/graph.js", "图谱逻辑"),
    ("web/app.css", "样式"),
]


def check_python(rep: Report) -> None:
    c = rep.add(Check("python", "Python 版本 ≥ 3.10（项目用 `X | None` 语法）", True))
    v = sys.version_info
    c.ok = (v.major, v.minor) >= (3, 10)
    c.measured = f"{v.major}.{v.minor}.{v.micro}"
    c.fix = "装 Python 3.10+；3.9 会在 import 时就 SyntaxError"


def check_deps(rep: Report) -> None:
    c = rep.add(Check("deps", "运行时依赖装齐（fastapi / uvicorn / pydantic / httpx）", True))
    missing = []
    for mod in ("fastapi", "uvicorn", "pydantic", "httpx", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    c.ok = not missing
    c.measured = "全部就位" if c.ok else f"缺 {'、'.join(missing)}"
    c.fix = "pip install -r requirements.txt"


def check_data_files(rep: Report) -> None:
    for rel, why in REQUIRED_DATA:
        c = rep.add(Check(f"data:{rel}", f"{rel}（{why}）", True))
        p = ROOT / rel
        c.ok = p.exists() and p.stat().st_size > 0
        c.measured = (f"{p.stat().st_size / 1024:.0f} KB" if p.exists()
                      else "不存在")
        c.fix = ("这个文件是生成物（生成方法见 docs/DEPLOYMENT.md），需要随代码一起部署；"
                 "缺了对应那块功能会退化（不是报错，是返回空）")


def check_web_assets(rep: Report) -> None:
    for rel, why in REQUIRED_WEB:
        c = rep.add(Check(f"web:{rel}", f"{rel}（{why}）", True))
        p = ROOT / rel
        if p.is_dir():
            n = len(list(p.glob("*")))
            c.ok = n > 0
            c.measured = f"{n} 个文件"
        else:
            c.ok = p.exists() and p.stat().st_size > 0
            c.measured = f"{p.stat().st_size / 1024:.0f} KB" if p.exists() else "不存在"
        c.fix = "整个 web/ 目录一起传，不要只传改动的那几个文件"


def check_writable(rep: Report) -> None:
    for rel, why in (("data", "审计日志与缓存"), ("data/cache", "向量缓存")):
        c = rep.add(Check(f"writable:{rel}", f"{rel}/ 可写（{why}）", True))
        p = ROOT / rel
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".preflight_probe"
        try:
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            c.ok = True
            c.measured = "可写"
        except OSError as e:
            c.ok = False
            c.measured = str(e)
        c.fix = "chown 给运行服务的那个用户；只读的话审计日志会**静默**写不进去"


def check_disk(rep: Report) -> None:
    c = rep.add(Check("disk", f"磁盘余量（阻断 <{DISK_BLOCK_MB} MB，告警 <{DISK_WARN_MB} MB）",
                      True))
    free_mb = shutil.disk_usage(ROOT).free / 1024 / 1024
    c.measured = f"{free_mb:.0f} MB 可用"
    c.ok = free_mb >= DISK_BLOCK_MB
    if c.ok and free_mb < DISK_WARN_MB:
        c.ok = False
        c.blocking = False
    c.fix = "磁盘写满时审计日志写不进去，append 会被吞掉——**没有报错，只是没有记录**"


def check_port(rep: Report, port: int) -> None:
    c = rep.add(Check("port", f"端口 {port} 空闲", True))
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        c.ok = True
        c.measured = "空闲"
    except OSError as e:
        c.ok = False
        c.measured = f"被占：{e}"
    finally:
        s.close()
    c.fix = f"换端口（PORT={port + 1}），或者 ss -lntp | grep {port} 看是谁占着"


def check_clock(rep: Report) -> None:
    c = rep.add(Check("clock", "系统时钟（审计日志要跟病历系统对时间）", False))
    # 没有外网时对不了 NTP，这里只查"时钟不是明显错的"（不是 1970、不是未来）
    now = time.time()
    c.ok = 1_700_000_000 < now < 4_000_000_000
    c.measured = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(now))
    c.fix = (f"跟内网 NTP 对时；偏差超过 {CLOCK_SKEW_WARN_S} 秒时，"
             "审计日志的时间戳跟病历系统对不上号")


def check_llm_config(rep: Report, skip_network: bool) -> None:
    from core.llm import get_backend

    mode = os.environ.get("LLM_MODE", "api")
    c = rep.add(Check("llm_mode", f"模型后端配置（LLM_MODE={mode}）", False))
    # `backend_id()` 而不是自己按 LLM_MODE 再写一遍映射：后端到底是哪个由
    # `get_backend()` 说了算，这里复述一遍的话，以后加了后端这里会悄悄说错。
    try:
        c.measured = f"backend={get_backend().backend_id()}"
    except Exception as e:  # noqa: BLE001
        # 构造失败本身就是要报的结论（例如 local_inproc 没给 LLM_MODEL_PATH）
        c.measured = f"后端构造失败：{type(e).__name__}: {e}"
        c.ok = False
        c.fix = "按 .env.example 补齐这个 LLM_MODE 需要的变量"
        return
    c.ok = True
    if mode == "api" and not os.environ.get("LLM_API_KEY"):
        c.ok = False
        c.measured += "，但 LLM_API_KEY 是空的"
        c.fix = ("填 .env 的 LLM_API_KEY；或者 LLM_MODE=replay 走录制回放"
                 "（离线运行），或者 LLM_MODE=local 指向内网的 vLLM 服务")
    else:
        c.fix = ""

    c2 = rep.add(Check("llm_reachable", "模型后端可达", False))
    if skip_network:
        c2.skipped = "--skip-network"
        return
    base = os.environ.get("LLM_BASE_URL", "")
    if mode != "api" or not base:
        c2.skipped = f"LLM_MODE={mode}，不走外部 HTTP"
        return
    try:
        import httpx

        r = httpx.get(base.rstrip("/") + "/models", timeout=5.0,
                      headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}"})
        c2.ok = r.status_code < 500
        c2.measured = f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        c2.ok = False
        c2.measured = f"{type(e).__name__}: {e}"
    c2.fix = ("内网无法访问外部 API 时用 LLM_MODE=replay 或 LLM_MODE=local。"
              "服务端不认识的模型名可能返回 200 + 空响应体而不是 404，"
              "可用 `curl $LLM_BASE_URL/models` 核对 LLM_MODEL")


def check_replay_fixtures(rep: Report) -> None:
    mode = os.environ.get("LLM_MODE", "api")
    c = rep.add(Check("replay", "回放 fixture（LLM_MODE=replay 时必需）", mode == "replay"))
    from core.llm_replay import fixtures_dir

    d = fixtures_dir()
    if mode != "replay":
        c.skipped = f"LLM_MODE={mode}，不走回放"
        return
    # 下划线开头的是录制附带的元文件（如 _baseline.json），不是 fixture
    n = (sum(1 for f in d.glob("**/*.json") if not f.name.startswith("_"))
         if d.exists() else 0)
    c.ok = n > 0
    c.measured = f"{n} 个 fixture"
    c.fix = ("python -m scripts.record_fixtures（需要真实 API），"
             "或把已录制的 fixture 放进 REPLAY_FIXTURES_DIR 指向的目录")


def check_concurrency_config(rep: Report) -> None:
    c = rep.add(Check("concurrency", "并发上限配置", False))
    n = int(os.environ.get("MAX_CONCURRENT_CONSULTS", "4") or 4)
    c.measured = f"MAX_CONCURRENT_CONSULTS={n}"
    c.ok = 1 <= n <= 8
    c.fix = ("MAX_CONCURRENT_CONSULTS 建议 1–8（默认 4）。并发调大不会提高单机吞吐，"
             "只会让排队更长、内存占用更高；可以用 scripts/loadtest.py 在目标机器上测拐点")


def check_eval_mode_off(rep: Report) -> None:
    c = rep.add(Check("eval_mode", "EVAL_MODE 必须关（它会让安全否决不中止）", True))
    v = os.environ.get("EVAL_MODE", "0")
    c.ok = v in ("", "0", "false", "False")
    c.measured = f"EVAL_MODE={v!r}"
    c.fix = ("**对外服务的机器上绝对不能开。** 打开之后危重症状不再中止链路，"
             "系统会继续给一个被拦截的请求开方——那是评测用的开关")


def check_safety_bypass_off(rep: Report) -> None:
    c = rep.add(Check("safety_bypass", "安全否决没有被环境变量绕过", True))
    from core.safety import safety_bypassed

    c.ok = not safety_bypassed()
    c.measured = "生效" if c.ok else "**被绕过了**"
    c.fix = "把 EVAL_MODE 关掉；这一项红的话不要上线"


def run(port: int, skip_network: bool) -> Report:
    rep = Report()
    check_python(rep)
    check_deps(rep)
    check_data_files(rep)
    check_web_assets(rep)
    check_writable(rep)
    check_disk(rep)
    check_port(rep, port)
    check_clock(rep)
    check_llm_config(rep, skip_network)
    check_replay_fixtures(rep)
    check_concurrency_config(rep)
    check_eval_mode_off(rep)
    check_safety_bypass_off(rep)
    return rep


def render(rep: Report) -> str:
    lines = []
    for c in rep.checks:
        if c.skipped:
            mark, tail = "—", f"（跳过：{c.skipped}）"
        elif c.ok:
            mark, tail = "✓", f"　{c.measured}"
        else:
            mark = "✗" if c.blocking else "!"
            tail = f"　{c.measured}\n      → {c.fix}" if c.fix else f"　{c.measured}"
        lines.append(f"  {mark} {c.what}{tail}")
    code = rep.exit_code()
    verdict = {0: "全部通过，可以上线。",
               1: "**有阻断项**，不要上线。",
               2: "有告警项：能起来，但要知道代价。"}[code]
    return "\n".join(lines) + f"\n\n{verdict}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--skip-network", action="store_true",
                    help="内网无外网时跳过模型后端可达性")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    rep = run(args.port, args.skip_network)
    if args.json:
        print(json.dumps({"exit_code": rep.exit_code(),
                          "checks": [vars(c) for c in rep.checks]},
                         ensure_ascii=False, indent=2))
    else:
        print(render(rep))
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
