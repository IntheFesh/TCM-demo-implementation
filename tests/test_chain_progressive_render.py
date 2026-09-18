"""R55 §5.2：九层状态按事件递进，不是全部等 `done`。

真机复现的 bug：`s3_delta`/`s3_done` 已经把④-⑧（病变脏腑…药物组成）渲成
"推理中"，⑨（校验与出处）也一样显示"推理中"——但⑨真正该反映的是验证/
佐证/个体化这几步**还没有任何一个事件为它更新过状态**，用户读到的是
"这一段跟其它几段一样在跑"，实际是"这一段压根没人告诉前端它在做什么"。
R55 把⑨的状态从 `sec.step === "s3"` 那个粗桶里摘出来，改成只认
`verify_revise`（核对中）和 `done`（定稿，走 `renderChainFlow` 整体替换，
不经过这里）两个事件——这份文件测的就是这条状态机本身：给定一串事件，
`chainChecksState` 和 `chainStateFor("checks", ...)` 该是什么。

跟 `tests/test_event_contract.py` 的分工：那份测"两边名单对不对得上"
（静态源码扫描）；这份测"事件真送到之后状态机走对了没有"（动态跑一遍
被测的 JS 函数本身）。跟 CLAUDE.md 那条"涉及图层结构变更要跑 Playwright"
的关系：这里的 node 测试覆盖的是**状态机的纯逻辑**（给定输入，输出对不
对），真实渲染出来是不是真的按这个状态显示，仍然要靠 Playwright 兜
（这一轮跑过，见报告）——这份文件本身不能替代那一步。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


def _html(expr: str) -> str:
    return _run(f"process.stdout.write({expr});")


# ---------- chainChecksState 状态机本身 ----------


def test_checks_state_starts_pending():
    assert _json("(resetChainChecksState(), chainChecksState)") == {"phase": "pending"}


def test_reaching_s3_flips_pending_to_active_not_verifying():
    """S3 一开始（`s3_start`/`s3_delta`，`reached==="s3"`）⑨就该变成"推理中"
    ——不用等 s3_done，验证器已经在排队了，继续显示"还没轮到"不诚实。"""
    got = _json("""(() => {
      resetChainChecksState();
      renderChainSkeleton("s3");
      return chainChecksState;
    })()""")
    assert got == {"phase": "active"}


def test_reaching_s3_again_does_not_reset_an_already_verifying_state():
    """`s3_delta` 会连续触发很多次 `renderChainSkeleton("s3")`（每收到一条
    增量都调一次）——如果这期间 `verify_revise` 已经把状态推进到
    "verifying"，后续的 `renderChainSkeleton("s3")` 调用不能把它冲回
    "active"（那样"核对中（第 N 轮）"这行字会被流式增量的重渲染盖掉）。"""
    got = _json("""(() => {
      resetChainChecksState();
      renderChainSkeleton("s3");
      chainChecksState = { phase: "verifying", round: 1 };
      renderChainSkeleton("s3");
      return chainChecksState;
    })()""")
    assert got == {"phase": "verifying", "round": 1}


def test_reset_clears_a_verifying_state_back_to_pending():
    """两次连续问诊之间不该有残留状态——上一次的"核对中（第 3 轮）"不能
    在下一次问诊还没跑到 S3 之前就出现在⑨里。"""
    got = _json("""(() => {
      chainChecksState = { phase: "verifying", round: 3 };
      resetChainChecksState();
      return chainChecksState;
    })()""")
    assert got == {"phase": "pending"}


# ---------- chainStateFor：⑨ 不再跟④-⑧共用 reached 这个粗桶 ----------


def test_checks_section_state_is_todo_while_pending():
    got = _json("""(() => {
      resetChainChecksState();
      return chainStateFor({ key: "checks", step: "s3" }, "s3");
    })()""")
    assert got == "todo"


def test_checks_section_state_is_active_once_s3_is_reached():
    got = _json("""(() => {
      resetChainChecksState();
      chainChecksState = { phase: "active" };
      return chainStateFor({ key: "checks", step: "s3" }, "s3");
    })()""")
    assert got == "active"


def test_checks_section_state_ignores_the_reached_argument_entirely():
    """这是⑨真正的行为分界：给它一个"还没到 s3"的 reached（比如 "s1"），
    只要 chainChecksState 自己已经 active，chainStateFor 仍然要报 active
    ——它不该再从 `reached` 反推自己的状态，那正是 R55 要摘掉的那条耦合。"""
    got = _json("""(() => {
      chainChecksState = { phase: "active" };
      return chainStateFor({ key: "checks", step: "s3" }, "s1");
    })()""")
    assert got == "active"


def test_other_sections_still_use_the_reached_based_logic_unchanged():
    """④-⑧（比如「病变脏腑」）不受这次改动影响——依然是"跑到哪一步就在哪一步
    停"的老逻辑，R55 只摘掉了⑨这一个特例。"""
    assert _json('chainStateFor({ key: "organs", step: "s3" }, "s2")') == "todo"
    assert _json('chainStateFor({ key: "organs", step: "s3" }, "s3")') == "active"
    assert _json('chainStateFor({ key: "syndrome", step: "s3" }, "done")') == "todo", (
        '"done" 不在 COLUMN_STEPS 里，indexOf 返回 -1，理论上不会被传进来——'
        "这条只是确认它不会意外撞上 active/done 的判据"
    )


# ---------- chainChecksBodyHtml：⑨那一段实际显示的文案 ----------


def test_checks_body_is_empty_while_pending():
    got = _json('(resetChainChecksState(), chainChecksBodyHtml())')
    assert got == ""


def test_checks_body_shows_thinking_while_active():
    got = _json('(chainChecksState = { phase: "active" }, chainChecksBodyHtml())')
    assert "推理中" in got


def test_checks_body_shows_the_revise_round_while_verifying():
    got = _json('(chainChecksState = { phase: "verifying", round: 2 }, chainChecksBodyHtml())')
    assert "核对中" in got and "第 2 轮" in got


# ---------- describeProgressEvent：三个新增事件的日志行 ----------


def test_verify_revise_produces_a_log_line_naming_the_round_and_rules():
    got = _html('describeProgressEvent("verify_revise", '
                '{round: 2, rules: ["principle_matches_syndrome"]})')
    assert "第 2 轮" in got and "principle_matches_syndrome" in got


def test_early_veto_produces_a_log_line():
    got = _html('describeProgressEvent("early_veto", '
                '{reason: "配伍相反", rule_label: "配伍禁忌"})')
    assert "配伍相反" in got


def test_agent_step_produces_a_log_line_naming_the_capability_and_why():
    got = _html('describeProgressEvent("agent_step", '
                '{capability_label: "追问", why: "信息不足"})')
    assert "追问" in got and "信息不足" in got


def test_these_three_events_do_not_advance_the_coarse_column_step():
    """`verify_revise`/`early_veto`/`agent_step` 不该触发 setColumnStep/
    renderChainSkeleton 那条粗粒度进度——它们各自有自己的处理方式
    （verify_revise 走专门分支改 chainChecksState，另外两个只进日志），
    不是"跑到了 s1/s2/s3 里的哪一步"这件事的一部分。"""
    for name in ("verify_revise", "early_veto", "agent_step"):
        got = _json(f'columnStepForEvent("{name}", {{physician: "ye_tianshi"}})')
        assert got is None, f"{name} 不该有 columnStepForEvent 返回值"
