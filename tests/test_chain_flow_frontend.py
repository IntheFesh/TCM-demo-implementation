"""R37：单链九段那几个纯函数的 node 测试（判据、顺序、口径）。

**真实渲染在 Playwright 那边**（`scripts/screenshot_states.py` 的 chain_flow
四个分辨率 + chain_running + node_explain + single_chain_graph + cancel_button）。
这里测的是"喂什么数据出什么文字"这一层：九段的顺序、哪一段该显示什么、
以及**百分比必须带分母口径**这类诚实约束——它们是纯函数，node 里逐条断言最省。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.web_harness import DOM_STUB, js_tmp, load_app_js


def _run(js_tail: str) -> str:
    proc = subprocess.run(["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js_tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 失败：\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _eval(expr: str):
    out = _run(f"process.stdout.write(JSON.stringify({expr}));")
    return json.loads(out)


def test_the_nine_sections_are_in_the_declared_order():
    """九段的顺序 = 申报书 2.1 的五步链 + 前三步 + 后一步。**不许重排**：
    顺序本身是"先说依据、再说结论"。"""
    keys = _eval("CHAIN_SECTIONS.map(s => s.key)")
    assert keys == ["complaint", "elements", "followup", "organs",
                    "syndrome", "method", "formula", "herbs", "checks"]
    nos = _eval("CHAIN_SECTIONS.map(s => s.no)")
    assert nos == ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨"]


def test_each_section_declares_which_step_it_belongs_to():
    """段 → 步的映射复用 COLUMN_STEPS 那套（s1/s2/s3），**不另编一套**：
    两套的话进度会各走各的。"""
    steps = set(_eval("CHAIN_SECTIONS.map(s => s.step)"))
    col = set(_eval("COLUMN_STEPS.map(s => s.key)"))
    assert steps <= col, f"九段用到了 COLUMN_STEPS 之外的步：{steps - col}"


def test_the_skeleton_state_follows_the_reached_step():
    got = _eval("CHAIN_SECTIONS.map(s => chainStateFor(s, 's2'))")
    assert got[0] == "done", "S1 那一段应该已完成"
    assert got[1] == "active" and got[2] == "active", "S2 的两段应该在跑"
    assert set(got[3:]) == {"todo"}, "S3 的六段还没开始"


def test_is_single_chain_prefers_the_manifest_over_the_server_default():
    """一次问诊结束后以 manifest 为准（那份记的是真的跑了哪一条），
    开始之前才用 /health 报的服务端默认值。"""
    assert _eval('isSingleChain({s3_mode: "structured"})') is True
    assert _eval('isSingleChain({s3_mode: "legacy"})') is False
    assert _eval('(SERVER_S3_MODE = "structured", isSingleChain(null))') is True
    assert _eval('(SERVER_S3_MODE = "legacy", isSingleChain(null))') is False
    # 拿不到（老服务 / /health 失败）时回落到 legacy 三列，跟 R36 及以前一致
    assert _eval('(SERVER_S3_MODE = null, isSingleChain(null))') is False


def test_a_multi_physician_response_is_never_drawn_as_one_chain():
    """**模式是可能猜的，结论条数是事实**。`manifest` 里没有 `s3_mode` 的响应
    （回放的老响应、fixture）会回落到这台服务的默认模式——要是那台服务默认
    structured，一份三医家的响应就会走进单链分支，而 `renderChainFlow()` 只读
    `results[0]`，另外两位的结论被静默丢掉。这条就是 Playwright 全量跑里
    `doctor_conflict` 报 `.col` 为 null 的根因。"""
    three = ('{results: [{physician: "a"}, {physician: "b"}, {physician: "c"}],'
             ' manifest: {model: "m"}}')
    one = '{results: [{physician: "synthesis"}], manifest: {model: "m"}}'
    assert _eval(f'(SERVER_S3_MODE = "structured", isSingleChainResult({three}))') is False
    assert _eval(f'(SERVER_S3_MODE = "structured", isSingleChainResult({one}))') is True
    # 一条结论 + manifest 明说 legacy：仍然按 legacy 画（manifest 记的是事实）
    assert _eval('(SERVER_S3_MODE = "structured",'
                 ' isSingleChainResult({results: [{physician: "ye_tianshi"}],'
                 ' manifest: {s3_mode: "legacy"}}))') is False
    # 三条结论 + manifest 明说 structured：这是自相矛盾的响应，按事实（三条）画
    assert _eval('isSingleChainResult({results: [{}, {}, {}],'
                 ' manifest: {s3_mode: "structured"}})') is False


def test_render_consult_result_branches_on_the_shape_not_the_mode():
    """判据只能有一处实现：三列/单链的分支必须问 `isSingleChainResult(data)`，
    不许在这里直接问 `isSingleChain(data.manifest)`——绕过形状判据就是绕过
    上一条那个静默丢结论的闸。"""
    src = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    body = src[src.index("function renderConsultResult("):]
    body = body[:body.index("\n// ---------- SSE 分步进度 ----------")]
    assert "isSingleChainResult(data)" in body
    assert "isSingleChain(data.manifest)" not in body


def test_a_physician_id_never_reaches_the_display_layer():
    """展示层只认中文名。structured 模式下 `results` 只有「五家综合」一条，
    而 §⑧ 的用药归属、§⑨ 的「引到的医家」要显示别的医家——只查这次问诊那份
    映射就会把 `ye_tianshi` 这个 id 直接印到页面上（R37 截图上真印出来了）。
    解析只有一处：`physicianName()`，两级回落。"""
    assert _eval('(PHYSICIAN_NAMES = {"ye_tianshi": "叶天士"},'
                 ' PHYSICIAN_META = {}, physicianName("ye_tianshi"))') == "叶天士"
    # 这次问诊的结果里没有它 → 回落到 /health 那份注册表
    assert _eval('(PHYSICIAN_NAMES = {"synthesis": "五家综合"},'
                 ' PHYSICIAN_META = {"li_ke": {name: "李可"}},'
                 ' physicianName("li_ke"))') == "李可"
    # 两处都没有才回落到 id 本身（回落到 id 好过显示空白，但它是最后一级）
    assert _eval('(PHYSICIAN_NAMES = {}, PHYSICIAN_META = {},'
                 ' physicianName("wu_jutong"))') == "wu_jutong"
    assert _eval('physicianName("")') == ""


def test_the_display_layer_has_exactly_one_physician_name_lookup():
    """「同一概念的匹配逻辑只能有一处实现」。直接写 `PHYSICIAN_NAMES[x] || x`
    的地方会绕过第二级回落——那正是 id 漏到界面上的那条路。
    赋值的两处（/health 填表、每次问诊刷新）不算查表。"""
    src = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    # 解析器自己那一段当然要查表——**按函数体排除，不靠"这一行里有没有等号"
    # 这种碰巧成立的写法**（写法一换判据就默默失效）。
    start = src.index("function physicianName(")
    resolver = src[start:src.index("\n}", start)]
    others = src[:start] + src[start + len(resolver):]
    reads = [ln.strip() for ln in others.splitlines()
             if "PHYSICIAN_NAMES[" in ln and "] =" not in ln]
    assert not reads, "还有绕过 physicianName() 的查表：" + "；".join(reads)
    assert resolver.count("PHYSICIAN_NAMES[") == 1


def test_no_english_identifier_reaches_the_chain_text():
    """页面上不许出现 `modified` / `high` / `meridian_coverage` 这类 id。
    展示层用中文名，id 只在数据层——R37 的第一版截图上三种都印出来了。
    规则名与结论名的中文由后端随结论下发（`rule_label` / `status_label`），
    方剂来源与置信度这两张纯展示表在前端，**各只有一处**。"""
    assert _eval('formulaSourceLabel("modified")') == "加减方"
    assert _eval('formulaSourceLabel("classic")') == "经典方"
    assert _eval('formulaSourceLabel("composed")') == "自拟方"
    # 认不出的值原样回落（显示一个陌生词好过显示空白）
    assert _eval('formulaSourceLabel("unknown_kind")') == "unknown_kind"
    assert _eval('formulaSourceLabel(null)') == ""
    assert [_eval(f'confidenceLabel("{x}")') for x in ("high", "medium", "low")] == ["高", "中", "低"]
    web = Path(__file__).resolve().parent.parent / "web"
    src = "\n".join((web / n).read_text(encoding="utf-8") for n in ("graph.js", "app.js"))
    assert src.count('classic: "经典方"') == 1, "方剂来源的中文名有第二处实现"
    assert src.count('high: "高"') == 1, "置信度的中文名有第二处实现"


def test_the_verifier_labels_come_from_the_backend_not_a_second_table():
    """规则清单在 `core/formula_verifier.ALL_RULES`，中文名就跟着它走。
    前端另建一张表 = 以后加规则要改两处，漏改的表现是界面上冒出英文 id。"""
    from core.formula_verifier import ALL_RULES, RULE_LABELS, STATUS_LABELS
    assert set(RULE_LABELS) == set(ALL_RULES), "规则表和中文名表对不上"
    assert set(STATUS_LABELS) == {"vetoed", "revise_needed",
                                  "partially_verified", "verified"}
    src = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    for rule in ALL_RULES:
        # 只查"把规则名当值/当键写下来"这三种写法。裸子串查法会误伤
        # `c.incompatible_pairs`（参考医家那一栏的字段，跟规则表无关）——
        # 一条会误伤的判据迟早会被人加白名单绕过，那就白立了。
        for spelling in (f'"{rule}"', f"'{rule}'", f"{rule}:"):
            assert spelling not in src, f"前端又写了一遍规则名 {rule}（{spelling}）"
    assert "rule_label" in src and "status_label" in src


def test_the_influence_step_reuses_the_nine_section_titles():
    """「叶天士·formula」这种写法把 schema 的枚举直接印给了人看。段名复用九段
    自己的标题——**同一个概念在页面上两处出现时必须同名**，另建一张表就是
    "改了段名、这里还是老词"的开始。`PhysicianInfluence.step` 用的是单数
    `organ`，段的 key 是 `organs`，这是唯一要照顾的差别。"""
    from core.schemas import PhysicianInfluence
    steps = PhysicianInfluence.model_fields["step"].annotation.__args__
    assert set(steps) == {"organ", "syndrome", "method", "formula", "herbs"}
    for step in steps:
        label = _eval(f'influenceStepLabel("{step}")')
        assert label and not label.isascii(), f"{step} 没映射成中文：{label}"
    assert _eval('influenceStepLabel("organ")') == "病变脏腑"
    assert _eval('influenceStepLabel("herbs")') == "药物组成"
    # 认不出的值原样回落
    assert _eval('influenceStepLabel("whatever")') == "whatever"


def test_every_followup_stop_reason_has_a_chinese_label():
    """六种停因都要有中文名，而且这张表跟 `FollowupResult.stopped_by` 的 Literal
    一一对应——枚举加一种、中文名漏一条，这条先红。中文名由后端随结果下发
    （`_serialize_followup` 的 `stopped_by_label`），前端不另建表。"""
    from core.followup import STOP_LABELS, stop_label
    from core.schemas import FollowupResult
    literals = set(FollowupResult.model_fields["stopped_by"].annotation.__args__)
    assert set(STOP_LABELS) == literals, f"停因表跟枚举对不上：{set(STOP_LABELS) ^ literals}"
    assert all(not v.isascii() for v in STOP_LABELS.values())
    assert stop_label("max_rounds") == "问满轮次"
    assert stop_label("不认识的") == "不认识的"
    src = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    # 前端可以**按停因分支**（`no_answer` / `fast_mode` 各有一句更具体的话，
    # 那不是翻译），但**不许把 id 直接插进要显示的字符串**——两处显示点都要
    # 走后端下发的 `stopped_by_label`。
    import re
    bare = [m.group(0) for m in re.finditer(r"\$\{[^}]*stopped_by\}", src)
            if "stopped_by_label" not in m.group(0)]   # 有中文名兜底的那种写法是对的
    assert not bare, f"有地方把停因 id 直接显示出来：{bare}"
    assert src.count("stopped_by_label") == 2, "两个显示点（九段③、进度日志）都要用中文名"


def test_the_api_puts_the_stop_label_into_the_payload():
    """后端下发这件事本身也要有判据：序列化少带一个键，前端就只能回落到 id，
    而回落是"看起来正常"的那种坏（不报错、不空白，只是印了个英文词）。"""
    from api.main import _serialize_followup
    from core.schemas import FollowupResult

    out = _serialize_followup(FollowupResult(rounds=2, stopped_by="converged"))
    assert out["stopped_by"] == "converged"
    assert out["stopped_by_label"] == "再问也问不出新信息"
    assert _serialize_followup(None) is None


def test_an_explainable_word_carries_both_the_node_id_and_the_label():
    """证型节点的 id 是 `syn::{physician}`，名字只在 label 里——两个都要带，
    否则释义接口只能拿到医家 id（见 core/node_explain.explain_node 的文档）。"""
    html = _eval('explainable("syn::synthesis", "胃痛 · 肝胃不和证", "肝胃不和证")')
    assert 'data-node="syn::synthesis"' in html
    assert 'data-name="肝胃不和证"' in html
    assert "胃痛 · 肝胃不和证" in html


def test_explainable_escapes_its_text():
    html = _eval('explainable("herb::x", "<img src=x onerror=alert(1)>")')
    assert "<img" not in html and "&lt;img" in html


def test_the_kind_labels_cover_every_node_kind():
    """后端 `core/node_explain.NodeKind` 有几种，前端就要有几个中文名——
    少一种的表现是面板标题后面跟着一个英文单词（R37 的截图上那个
    「meridian_coverage缺归经」是同一类事故）。

    **清单从后端的 Literal 现取，不手抄**：R42 加了 pathogenesis/principle/method
    三种，手抄的那份会跟后端漂，而漂了的表现正是这条测试要抓的那个英文单词。
    `unknown` 不在其中——它压根不会走到释义面板（explain_node 直接返回
    available=False）。"""
    import typing

    from core.node_explain import NodeKind

    want = set(typing.get_args(NodeKind)) - {"unknown"}
    labels = _eval("NODE_KIND_LABEL")
    assert set(labels) == want, f"前后端的种类清单不一致：缺 {want - set(labels)}，多 {set(labels) - want}"


def test_the_streaming_render_is_throttled_to_at_least_50ms():
    """R37 的验收项之一。rAF 的节奏跟刷新率绑（120Hz 屏上是 8ms），
    而这块区域是模型吐的 JSON——人眼分辨不出，重排却要付全价。"""
    assert _eval("S3_STREAM_RENDER_MS") >= 50


def test_the_idle_watchdog_is_300_seconds():
    """一次问诊要等几十秒，没有出口的等待是这一轮点名要修的弊端。
    取消按钮 + 300 秒空闲兜底，两者都要在。"""
    assert _eval("SSE_IDLE_TIMEOUT_MS") == 300000


def test_strip_dose_keeps_the_herb_name_only():
    assert _eval('stripDose("柴胡 12g")') == "柴胡"
    assert _eval('stripDose("生石膏（先煎）30g")') == "生石膏"
    assert _eval('stripDose("党参")') == "党参"
