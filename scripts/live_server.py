"""在本进程里起一个真的监听 127.0.0.1 的 uvicorn，**只有一份实现**。

## 为什么必须是真服务器而不是 TestClient

`fastapi.testclient.TestClient` 底下的 httpx ASGITransport 会把整个 ASGI app
跑完才把 Response 交还给调用方（body 先整段收进 `body_parts`），所以
`client.stream()` 看着像流式、实际上第一个字节都要等整条 SSE 流跑完。

这件事对两类测量是致命的：

  · **SSE 分段耗时**：`sse_flush` 想量的是"每个事件推出去花多久"，全缓冲
    传输层下这个数没有意义；
  · **追问（need_input）**：流中途要另开一个请求把答案送进去才能继续。
    TestClient 下这条路直接**死锁**——流没跑完读不到 need_input，读不到就
    不会去送答案，不送答案流就永远跑不完。

tests/test_api_stream.py 出于同一个原因起真服务器。这个模块是那套机制的唯一
实现：**同一概念只能有一处实现**（docs/ARCHITECTURE.md §4），profiler、压测脚本
与那条测试共用这一份。

## 等待预算为什么跟着 WARMUP_TIMEOUT_SECONDS 走

服务先监听、预热在后台跑，预热完成前 `/health` 回 503 并带进度；
`api.main.WARMUP_TIMEOUT_SECONDS` 是压测、冒烟这类脚本等服务就绪的时间预算
（docs/DEPLOYMENT.md §2）。这里等 `/health` 应答的预算取同一个常量再加缓冲，
不写成独立的硬编码数字：两处各调各的，服务端把预热上限调大之后，这边的预算就会
小于它。额外 30 秒缓冲量的是预热之外的开销（import、socket 起停、CI 机器繁忙），
不是给预热本身留的。
"""
from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Iterator

#: 预热之外的开销缓冲（秒）。不是给预热留的余量——预热的预算全在
#: `WARMUP_TIMEOUT_SECONDS` 里。
HEALTH_CHECK_SLACK_SECONDS = 30.0


def health_check_budget() -> float:
    """等 `/health` 应答的预算。**必须现读** `api.main.WARMUP_TIMEOUT_SECONDS`
    而不是在 import 时抄一份：测试会 monkeypatch 它，抄一份就联动不上。"""
    import api.main as api_main

    return api_main.WARMUP_TIMEOUT_SECONDS + HEALTH_CHECK_SLACK_SECONDS


def free_port() -> int:
    """要一个空闲端口。bind(0) 再立刻关掉：有竞态窗口，但这是标准做法，
    替代方案（让 uvicorn 自己挑）拿不到它挑了哪个。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def live_server(*, app=None, log_level: str = "error",
                wait_health: bool = True) -> Iterator[str]:
    """起服务、`yield base_url`、退出时停掉。

    `app=None` 用 `api.main.app`——**在函数里 import**，这样调用方可以先打好
    插桩再进来（模块顶层 import 会让 api.main 在插桩之前就被拉起来）。
    """
    import httpx
    import uvicorn

    if app is None:
        import api.main as api_main

        app = api_main.app

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level=log_level)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="live-server", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        if wait_health:
            budget = health_check_budget()
            deadline = time.time() + budget
            while time.time() < deadline:
                try:
                    httpx.get(f"{base_url}/health", timeout=1.0)
                    break
                except httpx.TransportError:
                    time.sleep(0.05)
            else:
                raise RuntimeError(f"uvicorn 没能在 {budget:.0f} 秒内起来")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
