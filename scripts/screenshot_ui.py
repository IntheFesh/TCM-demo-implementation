"""用 Playwright 给前端首页截一张图。**改了图层结构就必须在真浏览器里看一遍**
（docs/ARCHITECTURE.md §8）。

`to_graph()` 的 Python 单测只断言节点/边的 JSON 结构，而前端 `growGraph()` 的
`nodesByLayer` 少初始化一个层的 key 时，数据是对的、渲染是错的——JSON 结构测试
根本不会调用前端渲染代码，只有真的把数据喂给浏览器跑一遍才会暴露。
各种界面状态的逐条验收在 `scripts/screenshot_states.py`，这个脚本只截首页。

    python -m scripts.screenshot_ui                               # → docs/screenshots/home.png
    python -m scripts.screenshot_ui --role doctor --out /tmp/doctor_home.png

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
# 桌面优先，1440×900 是笔记本最常见的那一档。
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
        print("缺 playwright：pip install playwright && playwright install chromium"
              "（已装好 chromium 的机器用 PLAYWRIGHT_BROWSERS_PATH 指过去即可）",
              file=sys.stderr)
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
            # **显式指定浏览器路径**：预装的 chromium（PLAYWRIGHT_BROWSERS_PATH，
            # 默认 /opt/pw-browsers）的版本号跟 pip 装的 playwright 期望的不一定
            # 对得上，不指定的话 playwright 只认它自己那个版本、找不到就启动失败。
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
        # 报错，而渲染层初始化出错时往往只有控制台里有痕迹。
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
