"""R55 §5.2：事件契约表——后端全部 `on_step(`/`emit(`/`_sse(` 的事件名，
跟前端全部处理它们的地方（两个粗粒度进度函数的 `case "..."`，加主 SSE 读取
循环里的 `name === "..."` 分支），**源码级比对，缺一个红一个**。

为什么是源码级而不是真的起一条 SSE 流跑一遍：真机上那个 bug
（`verify_revise` 后端发了、前端一个 `case` 都没有）在单元测试里完全测不出
来——`describeProgressEvent`/`columnStepForEvent` 对不认识的事件名默认
返回 `null`，不抛异常、不报错，跑功能测试一切正常，只有把两份名单摆在一起
比对才看得出少了哪个。这正是本轮报告 §5.2 引用的 CLAUDE.md 教训
（JSON 结构测试测不出前端渲染层的问题）在事件契约这个具体场景下的写法。

跟 `tests/test_chain_progressive_render.py` 的分工：这份测的是"两边的事件
名单对不对得上"（静态、不跑 SSE）；那份测的是"事件送到之后九段的状态到底
变没变"（动态、真的走一遍 consult()）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


#: 这几个事件是 SSE 协议本身的控制帧，不是"推理链某一步完成了"这种进度
#: 事件——`stream_id`（流刚建立）、`usage`（额度快照）、`error`（异常收尾）、
#: `done`（终值）。前端处理它们的地方是主读取循环最前面几条 `if`，不经过
#: `describeProgressEvent`/`columnStepForEvent` 这两个"进度事件"专用函数，
#: 所以从"进度事件名单"里摘出来单独核对，不跟其余的混在一起扫。
PROTOCOL_EVENTS = frozenset({"stream_id", "usage", "error", "done"})


def _backend_emitted_events() -> set[str]:
    """扫 core/chain.py、core/react.py、api/main.py 里全部字面量事件名。

    四种写法都要认：`on_step("x"`、`_on_step("x"`、`emit("x"`（含
    `stream.emit("x"`）、`_sse("x"`（api/main.py 的 SSE 帧发送函数）。
    **只认字符串字面量**，不跟踪变量（`yield _sse(name, data)` 那个 `name`
    是转发 `events_q` 里已经记过名字的事件，不是新引入的字面量，本来就不该
    被这个扫描器当成新事件——它转发的名字已经在别处以字面量形式出现过一次）。
    """
    sources = (
        _read("core/chain.py"), _read("core/react.py"), _read("api/main.py"),
        # R55：`agent_step` 是从 core/agent.py::AgentTrace.append() 里广播的
        # ——不是 core/chain.py 自己 emit，扫描源必须包含它，否则这个事件
        # 永远会被这份测试自己误判成"后端没发"。
        _read("core/agent.py"),
    )
    pattern = re.compile(r'(?:on_step|_on_step|emit|_sse)\(\s*"([a-z_]+)"')
    names: set[str] = set()
    for src in sources:
        names |= set(pattern.findall(src))
    return names


def _frontend_handled_events() -> set[str]:
    """扫 web/app.js 里全部 `case "x"` 与 `name === "x"`。

    两种写法对应两条不同的分派路径（见 app.js 里 `describeProgressEvent`/
    `columnStepForEvent` 的 `switch` 和主读取循环的 `if/else if` 链），
    合并统计因为二者都算"这个事件名被认出来了"。"""
    src = _read("web/app.js")
    names: set[str] = set()
    names |= set(re.findall(r'case\s+"([a-z_]+)"\s*:', src))
    names |= set(re.findall(r'name\s*===\s*"([a-z_]+)"', src))
    return names


def test_every_backend_emitted_progress_event_has_a_frontend_handler():
    """核心断言：后端发的每一个进度事件名，前端都至少有一处认得它——
    `describeProgressEvent`/`columnStepForEvent` 的 `case`，或者主读取循环
    的 `name === "..."` 分支，任一处即可（不要求两处都有：比如 `s3_delta`
    只需要在主循环里特殊处理，不需要进日志）。"""
    backend = _backend_emitted_events() - PROTOCOL_EVENTS
    frontend = _frontend_handled_events()
    missing = backend - frontend
    assert not missing, (
        f"后端发了这些事件，前端没有任何 case/if 认得：{sorted(missing)}——"
        "会被静默丢进 describeProgressEvent 的 default 分支，进度条上悄悄"
        "少一行，用户会把这段时间读成「卡住了」（R55 §5.2 的真机 bug 正是这个）。"
    )


def test_the_three_named_r55_events_are_all_covered():
    """本轮报告 §5.2 原文点名的三个事件——`verify_revise` 是确认存在的真实
    bug（前端 0 处 case），`heartbeat`/`agent_step` 分别核实为"已经在别处
    正确处理"（心跳）和"这一轮新增的事件"（四能力决策实时广播）。三个都得
    在后端发出的名单里，也都得在前端认得的名单里——两头都占，才是"真的被
    接上了"，不是"两份名单碰巧都提到这个词"。"""
    backend = _backend_emitted_events()
    frontend = _frontend_handled_events()
    for name in ("verify_revise", "heartbeat", "agent_step"):
        assert name in backend, f"{name} 应该由后端发出，但源码扫描没找到"
        assert name in frontend, f"{name} 后端发了，但前端没有任何处理"


def test_verify_revise_gets_its_own_dedicated_branch_not_just_the_generic_switch():
    """`verify_revise` 不能只是掉进 `describeProgressEvent` 的 `default`
    ——它要能推进⑨（校验与出处）段的状态（`chainChecksState`），这需要
    主读取循环里一条专门的 `name === "verify_revise"` 分支，光靠
    `describeProgressEvent` 那个 `case` 只能出一行日志，改不了状态。"""
    src = _read("web/app.js")
    assert 'name === "verify_revise"' in src
    assert "chainChecksState" in src


def test_protocol_events_are_all_handled_in_the_main_dispatch_chain():
    """`stream_id`/`usage`/`error`/`done` 这四个不是"进度事件"，是 SSE 协议
    本身的控制帧——它们不经过 describeProgressEvent，但主读取循环里必须
    有对应的 `name === "..."` 分支，不能只是碰巧被 default 分支绕过去。"""
    src = _read("web/app.js")
    for name in sorted(PROTOCOL_EVENTS):
        assert f'name === "{name}"' in src, f"{name} 在主 SSE 读取循环里没有对应分支"


def test_early_veto_is_covered_too_not_just_the_three_named_events():
    """§5.2 原文点名的是 verify_revise/heartbeat/agent_step 三个，但同一次
    源码审计顺手发现 early_veto（早期止损提示）也是后端发了、前端零处理的
    同一类问题——这条测试把它也钉住，不因为报告没点名就漏掉。"""
    backend = _backend_emitted_events()
    frontend = _frontend_handled_events()
    assert "early_veto" in backend
    assert "early_veto" in frontend


def test_the_fallback_result_endpoint_path_matches_between_backend_and_frontend():
    """120 秒兜底 GET 打的那个 URL，后端路由声明和前端 fetch 调用必须是
    同一条路径——这种"两边各写一份、其中一份打错"的 bug，源码扫描能直接
    抓到，不用真的起服务器发一次请求。"""
    backend_src = _read("api/main.py")
    frontend_src = _read("web/app.js")
    path = "/api/consult/stream/{stream_id}/result"
    assert f'@app.get("{path}")' in backend_src
    assert "/api/consult/stream/${streamId}/result" in frontend_src


def test_agent_step_is_actually_emitted_by_agent_trace_not_just_documented():
    """`agent_step` 不是只在注释里提一句——`core/agent.py` 的 `AgentTrace`
    真的接了 `on_step` 回调并在 `append()`/`record()` 里广播它，
    `core/chain.py` 真的把 `consult()` 的 `on_step` 参数传给了
    `AgentTrace(on_step=...)`。"""
    agent_src = _read("core/agent.py")
    assert 'self.on_step("agent_step"' in agent_src
    chain_src = _read("core/chain.py")
    assert "AgentTrace(on_step=on_step)" in chain_src
