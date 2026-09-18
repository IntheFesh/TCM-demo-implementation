"""R45：**部署前自检**。一条命令，十二项，每项要么绿要么说清为什么红。

## 为什么要这个

上机失败预案（`docs/onsite_troubleshooting.md`）解决的是"跑起来之后哪一段挂了"。
这个脚本解决的是它前面那一步：**东西还没跑，先确认这台机器能跑**。

三甲内网的部署跟开发机的差别集中在几件事上，而它们全都**不会在启动时报错**，
只会在第一个患者点下「辨证」时才暴露：

- 数据文件缺一个（`data/graph.json` 没跟着传过去）→ 图谱页一片空白；
- vendor 目录没传全（字体 / cytoscape / dagre）→ 断网时页面没有字、图画不出来；
- 反代把 SSE 缓冲了 → 「一边推理一边出字」变成「转圈两分钟然后一次性出现」；
- 模型后端不可达或换了模型 → 第一次调用才报错，而那时医生已经在等；
- 磁盘只剩几百兆 → 审计日志写满之后**静默失败**（append 失败被吞）；
- 端口被占 / 时钟偏了 / 目录不可写。

**每一项都必须能单独解释**：红的那一项要说出"查的是什么、实测是什么、该怎么办"，
而不是一个 `FAILED`。这跟这个项目一直在防的"静默"是同一条纪律。

## 退出码

    0  全绿
    1  有**阻断项**红（缺数据文件、端口被占、目录不可写…）——不要上线
    2  只有**警告项**红（模型后端没配、磁盘偏紧…）——能起来，但要知道代价

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

#: 磁盘余量的两档。**下限不是拍的**：审计日志一次问诊约 4 KB
#: （`data/audit.jsonl` 实测行长），一天 200 诊次 ≈ 0.8 MB/天；
#: 2 GB 够两年多，500 MB 是"够跑但该清理了"。
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
    measured: str = ""   # 实测是什么
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

#: 前端的本地副本。**断网可用**正是录制回放这条路线存在的理由。
REQUIRED_WEB = [
    ("web/vendor/cytoscape.min.js", "图谱库"),
    ("web/vendor/dagre/dagre.min.js", "布局库（R42）"),
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
        c.fix = ("这个文件是生成物，随交付包一起传；缺了对应那块功能会静默退化"
                 "（不是报错，是返回空）")


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
    c.fix = ("审计日志一次问诊约 4 KB，一天 200 诊次 ≈ 0.8 MB/天。"
             "写不进去时 append 会被吞掉——**没有报错，只是没有记录**")


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
                 "（断网演示），或者 LLM_MODE=local_server 指向内网的推理服务")
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
    c2.fix = ("内网出不去就用 LLM_MODE=replay 或 local_server。"
              "**注意 deepseek-chat 已下线**：用它发请求得到 200 + 空响应体，"
              "不是 404（见 .env.example 的警告）")


def check_replay_fixtures(rep: Report) -> None:
    mode = os.environ.get("LLM_MODE", "api")
    c = rep.add(Check("replay", "回放 fixture（LLM_MODE=replay 时必需）", mode == "replay"))
    d = ROOT / "eval" / "fixtures"
    if mode != "replay":
        c.skipped = f"LLM_MODE={mode}，不走回放"
        return
    n = len(list(d.glob("**/*.json"))) if d.exists() else 0
    c.ok = n > 0
    c.measured = f"{n} 个 fixture"
    c.fix = "python -m scripts.record_fixtures（需要真实 API），或者从交付包里恢复"


def check_concurrency_config(rep: Report) -> None:
    c = rep.add(Check("concurrency", "并发上限配置（实测拐点在 4）", False))
    n = int(os.environ.get("MAX_CONCURRENT_CONSULTS", "4") or 4)
    c.measured = f"MAX_CONCURRENT_CONSULTS={n}"
    # R40 实测：并发 4 时 9.03 rps，16 时掉到 5.31——**调大会更慢**
    c.ok = 1 <= n <= 8
    c.fix = ("R40 实测吞吐拐点在 4（并发 4：9.03 rps；并发 16：5.31 rps）。"
             "调大不会更快，只会让每个人等更久，并且内存按 ~10 MB/问诊涨")


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
