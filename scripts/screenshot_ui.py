"""用 Playwright 给前端截图。**改了图层结构就必须跑这个**（CLAUDE.md 的硬约定）。

M5 的教训：`to_graph()` 的 Python 单测（六层节点/边的 JSON 结构断言）全部通过，
但前端 `growGraph()` 的 `nodesByLayer` 初始化漏了新加的 layer 4 这个 key——数据是
对的，是渲染层的初始化没跟上。JSON 结构测试根本不会调用前端渲染代码，只有真的把
数据喂给浏览器跑一遍才会暴露。

这个脚本也是 R13–R17 每轮"贴截图"那一项的产出工具：

    python -m scripts.screenshot_ui --out docs/screenshots/r13_home.png
    python -m scripts.screenshot_ui --state consult --role doctor --out ...

**它起一个真实的 uvicorn**（不是 file:// 打开 HTML）：`app.js` 一加载就会 fetch
`/health` 注入身份色、拉额度，file:// 下这些全是 CORS 错误，截出来的图跟线上不是
一个东西。没有 chromium / playwright 时退出码非 0 并说清缺什么，不静默产出一张空图。
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "screenshots" / "home.png"
# 桌面优先（总纲开头一句），1440×900 是评委笔记本最常见的那一档。
VIEWPORT = {"width": 1440, "height": 900}


def _chromium_path() -> str | None:
    """预装 chromium 的可执行文件。找不到就返回 None，让 playwright 走它自己的
    默认（那条路上会提示装浏览器，而不是静默截出一张空图）。"""
    import os

    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    for pattern in ("chromium-*/chrome-linux/chrome",
                    "chromium_headless_shell-*/chrome-linux/headless_shell"):
        for candidate in sorted(root.glob(pattern), reverse=True):
            if candidate.is_file():
                return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(url: str, deadline: float) -> bool:
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def capture(out: Path, *, role: str, full_page: bool, wait_ms: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（浏览器本机已有，不要跑 "
              "playwright install）", file=sys.stderr)
        return 2

    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait_ready(f"http://127.0.0.1:{port}/health", time.monotonic() + 60):
            print("服务 60 秒没起来（预热要加载 embedding，慢机器上可能更久）", file=sys.stderr)
            return 1
        out.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            # **显式指定浏览器路径**：这台机器上 chromium 是预装的
            # （PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers），但预装的版本号跟 pip
            # 装的 playwright 期望的不一定对得上，默认启动会让它去下载——而这个
            # 环境不许下（`playwright install` 是明确禁止的动作）。
            browser = pw.chromium.launch(executable_path=_chromium_path())
            page = browser.new_page(viewport=VIEWPORT)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/app/index.html?role={role}",
                      wait_until="networkidle")
            page.wait_for_timeout(wait_ms)
            page.screenshot(path=str(out), full_page=full_page)
            browser.close()
        # **页面里有 JS 错误就算失败**：一张"看起来还行"的截图掩盖不了控制台里的
        # 报错，而那正是 M5 那次真正出问题的地方。
        if errors:
            print("页面里有 JS 错误：\n  " + "\n  ".join(errors), file=sys.stderr)
            return 1
        print(f"→ {out}")
        return 0
    finally:
        server.terminate()
        server.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--role", default="researcher",
                    choices=["patient", "doctor", "student", "researcher"])
    ap.add_argument("--full-page", action="store_true", help="整页而不是首屏")
    ap.add_argument("--wait-ms", type=int, default=800,
                    help="截图前再等多久（字体 swap、图谱首帧）")
    args = ap.parse_args(argv)
    return capture(args.out, role=args.role, full_page=args.full_page, wait_ms=args.wait_ms)


if __name__ == "__main__":
    raise SystemExit(main())
