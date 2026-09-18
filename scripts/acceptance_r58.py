"""R58：真机验收——这一轮多轮改造（R51-R57）交付前的最后一道闸门，
**只能在用户自己的真机上跑出真实结果**。这个沙盒没有真实 LLM 后端、也没有
真实浏览器能跑的显示器（headless chromium 能跑，但没有真实推理内容可截），
所以这份脚本在这里只能验证"代码能不能跑到"，出不了真实的验收数字。

    python -m scripts.acceptance_r58
    python -m scripts.acceptance_r58 --out docs/screenshots/r58

**前置条件**：`eval/ablation/r57.py` 的 C 组必须先通过三条硬指标
（`eval/ablation/spec.py::GATE_*`）——R57 是这一轮改造成败的唯一判据，
R58 验收的是"这套已经证明能不靠模仿推理的系统，端到端跑起来是否可用"，
不是另一条独立判据。跑之前脚本会找 `eval/report_ablation_r57.json`，
读到 `all_gates_passed: true` 才继续；读不到就打印提示、要求先跑 R57。

## 这份脚本验四件事，全部是**真实端到端**，不是拿构造好的响应体喂渲染函数

跟 `scripts/screenshot_states.py`（构造响应体、验证渲染是否正确）不是
同一层：那份脚本快、稳定、每次 review 都能跑，但验证不了"真的问一句话，
真的等模型答完，答得对不对、快不快、露没露不该露的东西"——这四件事只有
真机能答。

1. **3 条主诉 × 3 个角色 = 9 次真实问诊**（`ACCEPTANCE_COMPLAINTS` ×
   `ACCEPTANCE_ROLES`）：耗时/token/追问轮数/验证轮数，走
   `scripts/bench_consult.py::run_once` 同一套后端封装（不重复实现一份
   计时逻辑），角色差异走真实 `api/main.py::_filter_response_by_role`
   （不猜它的行为，直接调用同一个函数）。
2. **响应形状按角色断言**（`assert_response_shape`）——patient/doctor/
   student/researcher 四个角色互不相同的字段可见性，这里选
   patient/doctor/researcher 三档（覆盖"最收敛""带建议的中间档""完全
   不裁剪的基线"，student 的形状只比 researcher 少一个 manifest，覆盖
   增量最小，三选一时优先级最低）。
3. **cases.json 原文摘录溯源**（`audit_case_excerpt_grounding`）——
   扫真实发给模型的 system prompt，凡是出现「【参考医案】xxx」这种
   医案引用块，摘录部分必须是 `cases.json` 里那条医案 `raw_excerpt`
   的真实子串（`core/chain.py::_format_case_block` 的产出格式），
   不是模型自己编的、也不是流水线拼错的。**production 默认
   `S3_MODE=derived`（R52 起）不检索任何医案**，这条审计在默认配置下
   恒是"0 处引用、0 处需要验证"——这是诚实答案，不是"这条检查没跑"；
   真正会命中的是 A 组（`S3_MODE=structured`）那种把医案摆进 prompt
   的路径。
4. **5 张 1920×1080 截图 + 截图对应页面的违禁词 DOM 扫描**
   （`PRODUCT_BANNED_WORDS`，唯一出处 `tests/test_no_demo_artifacts.py::
   BANNED` 的词表——不在这里另抄一份，抄一份就是又一处"同一张词表两处
   实现"）——驱动**真实浏览器**提交一条真实主诉、等真实回复渲染完，
   而不是像 `screenshot_states.py --product` 那样注入构造好的响应体。
   两者验的不是同一件事：那份验"给定这份响应体，渲染对不对"；这份验
   "端到端跑一次，模型说的话本身有没有漏出禁词"——后一件事只有真的
   跑一次模型才测得出来（模型可能在思考过程或自由文本字段里说出
   "根据医案..."这类措辞，源码扫描和固定 fixture 都扫不到）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "docs" / "screenshots" / "r58"
R57_REPORT_PATH = ROOT / "eval" / "report_ablation_r57.json"
VIEWPORT_1920 = {"width": 1920, "height": 1080}

#: 3 条主诉。沿用 `tests/queries.txt` 前三条——它们已经是这个项目里"日常
#: 拿来验收"的那一批（R38/R41 等多轮都用过同一份文件），不再另挑三条、
#: 制造"主诉从哪来"这个新的不确定性。
ACCEPTANCE_COMPLAINTS_PATH = ROOT / "tests" / "queries.txt"

#: 3 个角色。四个角色（patient/doctor/student/researcher）里选这三个：
#: 覆盖"最收敛"（patient）、"带建议的中间档"（doctor）、"完全不裁剪的
#: 基线"（researcher）——student 的响应形状只比 researcher 少一个
#: manifest 字段，增量信息最小，三选一时优先级最低（见模块文档第 2 条）。
ACCEPTANCE_ROLES: tuple[str, ...] = ("patient", "doctor", "researcher")

#: 医案引用块的格式，跟 `core/chain.py::_format_case_block` 的产出**逐字对应**
#: ——那边改了格式这里要跟着改，两处不对应的话这条审计会一条都扫不到，
#: 而且不会报错，只会安静地"0 处引用"，看起来像是通过了。
#:
#: **摘录本身可能带换行**（`case.raw_excerpt` 是医案原文，`_format_case_block`
#: 只是把 `【参考医案】.../原文：.../结构化：...` 三段用 `\n` 接起来，中间
#: 那段"原文："后面的内容不保证不含换行）。第一版这里写的是 `[^\n]*`
#: （只到本行结尾），真的用真实 cases.json 跑一遍才发现：多行摘录被从第一个
#: 换行处截断，截出来的 `prompt_excerpt` 天然比 `raw_excerpt[:300]` 短，
#: 于是**审计工具自己**把"摘录被截断"错判成"摘录跟原文对不上"——审计代码
#: 本身也要用真实数据核验过，不能假设自己第一次写对了。改成非贪婪匹配到
#: 下一个"\n结构化："为止（`re.DOTALL` 让 `.` 吃得下换行）。
CASE_BLOCK_RE = re.compile(
    r"【参考医案】(?P<case_id>\S+?)（[^）]*）\s*\n原文：(?P<excerpt>.*?)\n结构化：",
    re.DOTALL,
)


def _acceptance_complaints() -> list[str]:
    lines = [ln.strip() for ln in ACCEPTANCE_COMPLAINTS_PATH.read_text(encoding="utf-8")
            .splitlines() if ln.strip() and not ln.startswith("#")]
    if len(lines) < 3:
        raise ValueError(f"{ACCEPTANCE_COMPLAINTS_PATH} 里不够 3 条主诉")
    return lines[:3]


# ---------- 前置条件：R57 C 组先过闸门 ----------

def check_r57_gate_passed(report_path: Path = R57_REPORT_PATH) -> tuple[bool, str]:
    """读 R57 的报告，确认三条硬指标全过了。**读不到报告 ≠ 没过**——
    读不到是"还没跑"，判 False 但理由要说清楚是哪一种，不能让两种情况
    在调用方那边长得一样（CLAUDE.md 的"三分返回值"纪律，这里是二分：
    没跑 vs 跑了但没过）。"""
    if not report_path.exists():
        return False, f"找不到 {report_path}——R57 还没跑过（或跑了但没有用默认输出路径）"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"{report_path} 读不出来：{type(e).__name__}: {e}"
    if not report.get("content_metrics_valid"):
        return False, "R57 报告是假后端跑的（content_metrics_valid=false），不能当真机闸门用"
    passed = report.get("all_gates_passed")
    if passed is True:
        return True, "R57 三条硬指标全过"
    if passed is False:
        return False, "R57 三条硬指标至少一条没过——回 R51 补规则，R58 不该在这种状态下跑"
    return False, "R57 报告里 all_gates_passed 是 None（缺数据，判不了）"


# ---------- 1/2：真实问诊 + 响应形状断言 ----------

@dataclass
class RoleShapeExpectation:
    must_have: tuple[str, ...]
    must_not_have: tuple[str, ...]
    result_must_have: tuple[str, ...] = ()
    result_must_not_have: tuple[str, ...] = ()


#: 四档角色的响应形状契约，逐字对照 `api/main.py::_filter_response_by_role`
#: 的实现抄一份**断言**（不是重新实现一份过滤逻辑）。这张表本身也是
#: 一份文档：改了 `_filter_response_by_role` 忘了改这里，R58 会红，
#: 比"改完过阵子才发现角色裁剪跟文档描述的不一样"更早发现问题。
ROLE_SHAPE: dict[str, RoleShapeExpectation] = {
    "researcher": RoleShapeExpectation(
        must_have=("manifest", "divergence", "results"), must_not_have=()),
    "student": RoleShapeExpectation(
        must_have=("divergence", "results"), must_not_have=("manifest",)),
    "doctor": RoleShapeExpectation(
        must_have=("results", "triage", "food_therapy", "patent_medicines"),
        must_not_have=("manifest", "divergence"),
        result_must_not_have=("react_trace",)),
    "patient": RoleShapeExpectation(
        must_have=("results", "triage", "food_therapy", "patent_medicines"),
        must_not_have=("manifest", "divergence", "individualization", "guideline"),
        result_must_not_have=("react_trace", "advice", "advice_skipped", "formula_score")),
}


def assert_response_shape(role: str, response: dict) -> list[str]:
    """按角色核对响应形状，返回问题列表（空列表 = 通过）。**纯函数**，
    不碰网络——喂一份真实响应体或一份构造的响应体都能测，`tests/
    test_acceptance_r58.py` 用构造的响应体测这个函数本身对不对，
    这个脚本用真实响应体测系统对不对，两者用的是同一份判据代码。"""
    exp = ROLE_SHAPE.get(role)
    if exp is None:
        return [f"没有登记角色 {role!r} 的形状契约"]
    problems = []
    for k in exp.must_have:
        if k not in response:
            problems.append(f"角色 {role} 的响应里缺 {k!r}")
    for k in exp.must_not_have:
        if k in response:
            problems.append(f"角色 {role} 的响应不该有 {k!r}，但有")
    if role == "patient":
        for r in response.get("results") or []:
            if r.get("refs"):
                problems.append("patient 角色的 refs 应该恒为空列表")
    for r in response.get("results") or []:
        for k in exp.result_must_not_have:
            if k in r:
                problems.append(f"角色 {role} 的 results[] 不该有 {k!r}，但有")
    return problems


# ---------- 3：cases.json 原文摘录溯源 ----------

def audit_case_excerpt_grounding(prompt_text: str, raw_excerpt_by_case_id: dict[str, str | None],
                                 *, truncate_chars: int = 300) -> dict:
    """扫 `prompt_text` 里所有「【参考医案】」块，核对摘录是不是那条医案
    `raw_excerpt` 的真实前缀（`core/chain.py::CASE_EXCERPT_TRUNCATE_CHARS`
    截到多少字，这里就按同样长度截，不然真实摘录被截断之后天然不等于
    完整 `raw_excerpt`，会把"截断"误判成"编造"）。

    `raw_excerpt_by_case_id` **必须覆盖 cases.json 里的全部医案 id**，
    值可以是 `None`（那条医案确实没有 raw_excerpt）——不能只塞有摘录的
    那些。传一份"只收有摘录的"字典进来，会把"这条医案本来就没有原文，
    `_format_case_block` 正确地写了『（原文缺失）』"误判成"cases.json
    里根本没有这条 id"（真实测出的一个坑：`core.retrieval.load_cases()`
    读回来的 `CaseRecord` 列表里，`raw_excerpt` 是 `None` 的记录如果被
    过滤掉不放进字典，这条审计自己会把"诚实缺失"的那些全部错判成
    "编造的 id"，44/1060 那批全部属于这种误判，没有一条是真的编造）。

    返回 `{n_refs, n_grounded, ungrounded}`——`n_refs=0` 是**诚实的 0**，
    不是"没跑这条检查"：`S3_MODE=derived` 的 prompt 里天然没有这种块，
    见模块文档第 3 条。
    """
    refs = CASE_BLOCK_RE.findall(prompt_text)
    ungrounded = []
    for case_id, excerpt in refs:
        if case_id not in raw_excerpt_by_case_id:
            ungrounded.append({"case_id": case_id, "reason": "cases.json 里没有这条医案 id"})
            continue
        real = raw_excerpt_by_case_id[case_id]
        if not real:
            if excerpt != "（原文缺失）":
                ungrounded.append({"case_id": case_id,
                                  "reason": "这条医案没有 raw_excerpt，但 prompt 里没写「原文缺失」",
                                  "prompt_excerpt": excerpt[:80]})
            continue
        want = real[:truncate_chars]
        if excerpt != want:
            ungrounded.append({"case_id": case_id, "reason": "摘录跟 raw_excerpt 对不上",
                              "prompt_excerpt": excerpt[:80], "real_prefix": want[:80]})
    return {"n_refs": len(refs), "n_grounded": len(refs) - len(ungrounded),
           "ungrounded": ungrounded}


class PromptCapture:
    """包住 `backend._complete`，**只为了拿到真实发给模型的 messages 文本**
    ——跟 `scripts/bench_consult.py::CallRecorder` 是同一种包法（monkey-patch
    `_complete`），但回答的不是同一个问题：那边问"这次调用花了多久、
    用了多少 token"（性能基准），这里问"模型到底看到了什么"（防幻觉审计）。
    `CallRecorder` 刻意不存 messages 全文（存了会让基准 JSON 文件涨几十倍，
    而基准脚本用不上这段文本），所以这里单独包一层，不改 `CallRecorder`
    本身——两个包法目的不同，硬合成一个只会让"这次包是为了测速还是测
    内容"变得含糊。
    """

    def __init__(self, backend) -> None:
        self.backend = backend
        self.system_prompts: list[str] = []
        self._orig_complete = backend._complete
        backend._complete = self._wrapped

    def _wrapped(self, messages, *args, **kwargs):
        for m in messages:
            if m.get("role") == "system":
                self.system_prompts.append(m.get("content") or "")
        return self._orig_complete(messages, *args, **kwargs)

    def restore(self) -> None:
        self.backend._complete = self._orig_complete


def run_role_complaint(role: str, complaint: str, backend) -> dict:
    """一次真实问诊：跑通、按角色裁剪、核对形状、核对医案溯源、收集三项
    成本指标（耗时/调用数/token，token 走 manifest 里已有的用量记账，
    不在这里另算一遍——`core/llm.py::record_usage` 是唯一记账点）。"""
    from scripts.bench_consult import run_once
    from api.main import _filter_response_by_role
    from core.retrieval import load_cases

    capture = PromptCapture(backend)
    t0 = time.perf_counter()
    try:
        run = run_once(complaint, use_react=False, retriever_mode=None,
                       backend=backend, keep_result=True)
    finally:
        capture.restore()
    elapsed = round(time.perf_counter() - t0, 3)

    result = run.get("result")
    if not run["ok"] or result is None:
        return {"role": role, "complaint": complaint, "ok": False, "error": run["error"],
               "elapsed_s": elapsed}

    # **已知的范围限制**：`run_once` 走的是 `core.chain.consult()` 直接调用，
    # `eval_mode` 传的是 None（读 EVAL_MODE 环境变量，默认关）——真实 HTTP
    # 请求走 `api/main.py::api_consult()` 时会按
    # `role_sees_full_reasoning_on_red_flag(role)` 显式选 eval_mode（危重信号
    # 命中后医师/学生/研究者仍能看到完整推理，患者不能）。这条脚本不测那条
    # 分支：`ACCEPTANCE_COMPLAINTS` 用的是 `tests/queries.txt` 前三条，全是
    # 平常的脾胃门主诉，不会触发安全否决，所以这个简化在**这次跑**里不影响
    # 结果——但如果以后换了会触发红旗的主诉，这条脚本量不到角色分流这一层，
    # 得走真实 HTTP 请求（`tests/test_api_stream.py` 已经覆盖了那条分支的
    # 判据，这里不重复）。
    response = {"results": result.get("results"), "divergence": result.get("divergence"),
               "manifest": result.get("manifest"), "individualization": result.get("individualization"),
               "guideline": result.get("guideline")}
    filtered = _filter_response_by_role(response, role, result.get("results") or [])
    shape_problems = assert_response_shape(role, filtered)

    excerpt_by_id: dict[str, str | None] = {}
    try:
        cases, _texts, _skipped = load_cases()
        # **覆盖全部医案 id，不只是有摘录的那些**——见
        # `audit_case_excerpt_grounding` 文档字符串：漏了没有摘录的那些，
        # 会把"诚实缺失"错判成"编造的 id"。
        excerpt_by_id = {c.case_id: c.raw_excerpt for c in cases}
    except FileNotFoundError:
        pass  # 没有 cases.json 时这条审计恒 0 处引用，属于诚实的"跑不了"

    audits = [audit_case_excerpt_grounding(p, excerpt_by_id) for p in capture.system_prompts]
    total_refs = sum(a["n_refs"] for a in audits)
    total_ungrounded = [u for a in audits for u in a["ungrounded"]]

    manifest = result.get("manifest") or {}
    return {
        "role": role, "complaint": complaint, "ok": True,
        "elapsed_s": elapsed, "llm_calls": run["llm_calls"],
        "n_system_prompts_captured": len(capture.system_prompts),
        "case_excerpt_audit": {"n_refs": total_refs, "n_ungrounded": len(total_ungrounded),
                               "ungrounded": total_ungrounded},
        "shape_problems": shape_problems,
        "s3_mode": manifest.get("s3_mode"),
        "ask_rounds": manifest.get("ask_rounds"),
        "verify_revise_rounds": manifest.get("revise_rounds"),
        "cache_hit_ratio": manifest.get("cache_hit_ratio"),
    }


# ---------- 4：真机截图 + 产品面违禁词扫描 ----------

def _banned_words() -> tuple[str, ...]:
    """**唯一出处** `tests/test_no_demo_artifacts.py::BANNED`——那张表本身
    已经是"§8.2 十七条 + R56 §6 审计发现的一批"的完整版本，这里不重抄，
    重抄的话两张表迟早不同步（R56 那一轮刚扩容过一次这张表）。"""
    sys.path.insert(0, str(ROOT))
    from tests.test_no_demo_artifacts import BANNED

    return tuple(BANNED.keys())


#: 每张截图要走到哪个真实页面状态，state 的语义见函数体注释。
SCREENSHOT_PLAN = (
    {"name": "01_first", "state": "first"},
    {"name": "02_running", "state": "running"},
    {"name": "03_done_consult", "state": "done"},
    {"name": "04_graph_browser", "state": "graph_browser"},
    {"name": "05_node_explain", "state": "node_explain"},
)


def capture_live_screenshots(out_dir: Path, complaint: str, role: str) -> list[dict]:
    """驱动真实浏览器，提交一条真实主诉，等真的答完，截 5 张图，每张都用
    `document.body.innerText` 扫一遍违禁词。**跟 `screenshot_states.py
    --product` 的区别只有一处、但是关键的一处**：那边把响应体直接注射
    进渲染函数；这里让页面自己发 `fetch`/EventSource，等真实回复自然
    渲染出来。两条路径共用同一套产品面判据（违禁词表、角色下拉），
    但只有这一条能测出"模型说的话本身漏没漏禁词"。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [{"name": p["name"], "ok": False,
                "error": "缺 playwright：pip install playwright"} for p in SCREENSHOT_PLAN]

    from scripts.screenshot_ui import _chromium_path, _free_port, _wait_ready
    import subprocess

    out_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=_product_mode_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results: list[dict] = []
    try:
        if not _wait_ready(f"http://127.0.0.1:{port}/health", time.monotonic() + 60):
            return [{"name": p["name"], "ok": False, "error": "服务 60 秒没起来"}
                   for p in SCREENSHOT_PLAN]
        banned = _banned_words()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            page = browser.new_page(viewport=VIEWPORT_1920)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/app/index.html?role={role}",
                      wait_until="networkidle")

            out = out_dir / f"{SCREENSHOT_PLAN[0]['name']}.png"
            page.screenshot(path=str(out))
            results.append(_scan_page(page, "01_first", out, banned))

            page.fill("#complaint", complaint)
            page.click("#submit-btn")
            page.wait_for_timeout(800)
            out = out_dir / f"{SCREENSHOT_PLAN[1]['name']}.png"
            page.screenshot(path=str(out))
            results.append(_scan_page(page, "02_running", out, banned))

            # 等真实推理跑完——真机上这一步是分钟级的，不设一个短超时假装
            # 跑完了。R55 记录的量级是单次问诊 45~280 秒（按推理档位），
            # 这里给一个宽裕上限，跑不完就如实报"超时"，不是当场判失败退出
            # （后面几步还想拿到"卡在哪"这条信息）。
            done = page.wait_for_selector(
                "#chain-flow.show, #safety-block:not([hidden])", timeout=360_000,
                state="visible") if _selector_exists_soon(page) else None
            out = out_dir / f"{SCREENSHOT_PLAN[2]['name']}.png"
            page.screenshot(path=str(out))
            results.append(_scan_page(page, "03_done_consult", out, banned,
                                      note=None if done else "360 秒没等到完成态"))

            page.click("#tab-btn-graph-browser")
            page.wait_for_timeout(1500)
            out = out_dir / f"{SCREENSHOT_PLAN[3]['name']}.png"
            page.screenshot(path=str(out))
            results.append(_scan_page(page, "04_graph_browser", out, banned))

            hit = page.query_selector("#chain-flow .explainable")
            if hit:
                hit.click()
                page.wait_for_timeout(600)
            out = out_dir / f"{SCREENSHOT_PLAN[4]['name']}.png"
            page.screenshot(path=str(out))
            results.append(_scan_page(page, "05_node_explain", out, banned,
                                      note=None if hit else "没有可点的释义节点"))
            if errors:
                results.append({"name": "_page_errors", "ok": False, "error": "；".join(errors)})
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
    return results


def _selector_exists_soon(page) -> bool:
    try:
        page.wait_for_selector("#chain-flow", timeout=5000)
        return True
    except Exception:  # noqa: BLE001 - 等不到就走超时截图分支，不让这里的异常盖过主流程
        return False


def _scan_page(page, name: str, screenshot_path: Path, banned: tuple[str, ...],
               *, note: str | None = None) -> dict:
    text = page.evaluate("() => document.body.innerText || ''")
    found = [w for w in banned if w in text]
    return {"name": name, "ok": not found, "screenshot": str(screenshot_path),
           "banned_words_found": found, "note": note}


def _product_mode_env() -> dict:
    import os

    env = dict(os.environ)
    env["PRODUCT_MODE"] = "1"
    return env


# ---------- 汇总 + CLI ----------

def build_report(gate_ok: bool, gate_reason: str, role_runs: list[dict],
                 screenshots: list[dict]) -> dict:
    shape_problems = [p for r in role_runs for p in r.get("shape_problems", [])]
    ungrounded = [u for r in role_runs for u in r.get("case_excerpt_audit", {}).get("ungrounded", [])]
    banned_hits = [s for s in screenshots if s.get("banned_words_found")]
    return {
        "kind": "acceptance_r58",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "r57_gate": {"passed": gate_ok, "reason": gate_reason},
        "role_runs": role_runs,
        "screenshots": screenshots,
        "shape_problems": shape_problems,
        "case_excerpt_ungrounded": ungrounded,
        "banned_word_hits": banned_hits,
        "overall_ok": (gate_ok and not shape_problems and not ungrounded
                      and not banned_hits and all(r.get("ok") for r in role_runs)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--report", type=Path, default=ROOT / "eval" / "report_acceptance_r58.json")
    ap.add_argument("--backend", default="real", choices=["fake", "real"],
                    help="real=真机验收（默认）；fake 只用来验这份脚本自己能不能跑通")
    ap.add_argument("--skip-gate-check", action="store_true",
                    help="跳过 R57 闸门检查（调试脚本本身时用，真验收不许加这个开关）")
    args = ap.parse_args(argv)

    gate_ok, gate_reason = (True, "已跳过") if args.skip_gate_check else check_r57_gate_passed()
    print(f"R57 闸门：{'✓' if gate_ok else '✗'} {gate_reason}")
    if not gate_ok:
        print("R58 不该在 R57 没过的状态下宣布验收通过——继续跑完仍会给出真实数字，"
             "但报告里 overall_ok 恒为 False，直到 R57 过闸门。", file=sys.stderr)

    if args.backend == "fake":
        print("⚠ --backend fake：这一轮的数字不代表任何真实推理，只验证脚本本身能不能跑通。",
             file=sys.stderr)
    else:
        print("⚠ 这是真机验收：9 次问诊（3 主诉 × 3 角色）+ 5 张真实截图，"
             "按 R55 记录的量级预计 10~40 分钟、真实 API 费用，请确认已经配好"
             "真实 LLM 后端（LLM_MODE 环境变量）。", file=sys.stderr)

    from scripts.bench_consult import build_backend

    backend = build_backend(args.backend, 0.0, False)
    complaints = _acceptance_complaints()

    role_runs = []
    for role in ACCEPTANCE_ROLES:
        for complaint in complaints:
            print(f"— {role}　{complaint[:20]}…")
            role_runs.append(run_role_complaint(role, complaint, backend))

    screenshots: list[dict] = []
    if args.backend == "real":
        screenshots = capture_live_screenshots(args.out, complaints[0], "doctor")
    else:
        print("— 跳过真机截图（--backend fake 下截不出有意义的内容）")

    report = build_report(gate_ok, gate_reason, role_runs, screenshots)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                           encoding="utf-8")
    print(f"\n→ {args.report}")
    print(f"总判定：{'✅ 通过' if report['overall_ok'] else '❌ 未通过（看上面各项明细）'}")
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
