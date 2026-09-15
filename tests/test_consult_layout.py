"""R14：问诊页的三列集注、用药对照带、五种状态（docs/DESIGN.md §3.1）。

## 这些测试为什么测的是"返回的 HTML 字符串"而不是 DOM

`tests/web_harness.py` 的 `DOM_STUB` 是个什么都接住的 Proxy，**刻意**不做成像样的
DOM（做得越像，测试越容易在"其实没渲染"的情况下绿）。所以这里测的是纯函数：
给一份数据，它拼出来的 HTML 对不对。真实 DOM、真实 CSS、真实 cytoscape 的验收
走 `scripts/screenshot_states.py`（真浏览器，五种状态各一张图，页面里有 JS 错误
就算失败）——两条路各管一段，不互相替代。

## 为什么状态要一个一个测

总纲 §1 列的"明确不做的"第四条是"五种状态没有各自设计"：首次进入是空白、
辨证中是一行进度日志、安全拦截是红框。这些都不会报错，只会让页面看起来像
没做完。没有断言的话，改版三轮之后它们会一个一个退化回去。
"""
import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js

# 三列的表头数据。node 里没有 /health，直接把 PHYSICIAN_META 灌进去——
# 它在 app.js 里是个 let，加载后可写，这正是"三列不依赖问诊结果"的落法。
META_JS = """
PHYSICIAN_META = {
  ye_tianshi: { name: "叶天士", years: "1667-1746", school: "温病" },
  wu_jutong: { name: "吴鞠通", years: "1758-1836", school: "温病" },
  zhang_xichun: { name: "张锡纯", years: "1860-1933", school: "衷中参西" },
};
PHYSICIAN_NAMES = { ye_tianshi: "叶天士", wu_jutong: "吴鞠通", zhang_xichun: "张锡纯" };
"""


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + META_JS + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _html(expr: str) -> str:
    return _run(f"process.stdout.write({expr});")


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


def _herb(name, role, dose=None):
    return {"name": name, "role": role, "dose": dose, "dose_unit": "g",
            "processing": None, "decoction": None, "function_in_formula": None}


# ---------- 状态一：首次进入 ----------


def test_the_five_states_are_declared_in_one_place():
    """状态名散在各处写字符串的话，`state-runing` 这种拼错永远不会报错——
    CSS 里没有那个类，页面只是"什么都没发生"。"""
    assert _json("CONSULT_STATES") == ["first", "running", "insufficient", "followup", "done"]


def test_first_screen_offers_the_three_demo_complaints_with_a_reason_to_click():
    """§3.1：首次进入时输入区居中，**下方三条示例主诉可点击直接填入**。
    每条带一句"这条是用来看什么的"——三条主诉长得都像一串症状，不写这行
    没人知道该点哪条。"""
    examples = [
        {"label": "A", "text": "胃脘胀痛，脉弦", "hint": "看用药对照"},
        {"label": "B", "text": "胸闷胸痛，冷汗", "hint": "看导诊"},
        {"label": "C", "text": "解黑色柏油样便", "hint": "看安全拦截"},
    ]
    out = _html(f"exampleListHtml({json.dumps(examples, ensure_ascii=False)})")
    assert out.count('class="example"') == 3
    for e in examples:
        assert e["text"] in out and e["hint"] in out and f">{e['label']}<" in out


def test_each_example_carries_the_complaint_on_the_button_itself():
    """点一下就填进输入框，靠的是 data-complaint。**原文一个标点都不能差**：
    回放按主诉原文的哈希索引，差一个字就是 LLMError（core/examples.py 的由来）。"""
    text = "胃脘胀痛，食后加重，嗳气泛酸。"
    out = _html(f'exampleListHtml([{{label: "A", text: {json.dumps(text, ensure_ascii=False)}, hint: ""}}])')
    assert f'data-complaint="{text}"' in out


# ---------- 状态二：辨证中 ----------


def test_running_columns_show_three_named_steps_not_one_spinner():
    """§3.1 状态表："三列各自显示分步进度（S1 → S2 → S3），**不是一个转圈**"。
    一个转圈只说明"在跑"，分步说明"跑到哪了"——一次问诊是分钟级，这两者
    对等待的人完全不是一回事。"""
    out = _html('columnRunningHtml("ye_tianshi", "s2")')
    assert "症状标准化" in out and "证素推断" in out and "证型与方药" in out
    assert 'data-step="s1"' in out and 'data-step="s3"' in out
    # 走到 s2：s1 已完成、s2 进行中、s3 还没开始
    assert 'class="step step-done" data-step="s1"' in out
    assert 'class="step step-active" data-step="s2"' in out
    assert 'class="step step-todo" data-step="s3"' in out


def test_progress_is_routed_by_the_physician_field_not_by_arrival_order():
    """**R12 之后三位医家并发跑**，physician_start / s3_start / physician_done
    的到达顺序完全是乱的（docs/DESIGN.md §4.7 的订正）。按到达次序推"现在轮到谁"
    会把张锡纯的进度画到叶天士那一列上——而这种错不会报任何错。"""
    assert _json('columnStepForEvent("physician_start", {physician: "zhang_xichun"})') == {
        "physician": "zhang_xichun", "step": "s3"}
    assert _json('columnStepForEvent("s3_start", {physician: "wu_jutong"})') == {
        "physician": "wu_jutong", "step": "s3"}
    assert _json('columnStepForEvent("react_step", {physician: "ye_tianshi", step: 2})') == {
        "physician": "ye_tianshi", "step": "s3"}
    # done / need_input / 未知事件不动进度条
    assert _json('columnStepForEvent("physician_done", {physician: "ye_tianshi"})') is None
    assert _json('columnStepForEvent("need_input", {})') is None


def test_s1_and_s2_advance_all_three_columns_because_they_run_once_globally():
    """S1/S2 是全局跑一次、三列共用的（CLAUDE.md：**S1 全局只跑一次**，
    对每位医家各跑一次会让症状节点 id 对不上）。所以这两步的进度不带
    physician，三列一起走——这不是偷懒，是如实反映后端的执行形状。"""
    assert _json('columnStepForEvent("s1_done", {})') == {"physician": None, "step": "s2"}
    assert _json('columnStepForEvent("s2_done", {})') == {"physician": None, "step": "s3"}


# ---------- 状态三：追问 ----------


def test_the_question_goes_into_the_asking_physicians_column():
    """§3.1：**在对应医家列内弹出问题，其余两列继续等待**。谁在问由后端
    need_input 事件的 physician 字段说了算（core/chain.py 的 ContextVar →
    api/main.py::_ConsultStream.ask）。"""
    out = _html('columnFollowupHtml("wu_jutong", "有没有便血？")')
    assert 'data-state="asking"' in out and "有没有便血？" in out
    assert "吴鞠通" in out and "class=\"ask-submit\"" in out


def test_the_other_columns_say_what_they_are_waiting_for():
    """"等待中"三个字单独出现会被读成"这一列卡住了"。说明是谁在占用问答
    渠道，才看得出这是设计如此——追问是串行的（_serialize_ask），因为
    ask_fn 背后是一个人，同一时刻只能回答一个问题。"""
    out = _html('columnWaitingHtml("ye_tianshi")')
    assert 'data-state="waiting"' in out and "等待中" in out and "确认信息" in out


# ---------- 状态四：信息不足 ----------


def test_insufficient_columns_carry_the_backend_reason_not_a_frontend_guess():
    """§3.1："三列位置显示一句话 + 建议补充的信息，**不留空白**"。
    留空白的话页面看起来像坏了，而它其实是好好地拒绝了。补什么的提示原样
    来自后端 insufficient_reason——前端编一句会跟后端的判据脱节。"""
    out = _html('columnInsufficientHtml("ye_tianshi", "请补充舌象与脉象")')
    assert 'data-state="insufficient"' in out
    assert "请补充舌象与脉象" in out and "不足以推断证素" in out


def test_insufficient_falls_back_to_a_generic_hint_when_the_backend_gave_none():
    """后端可能不带 reason（老响应体、或某些分支）。这时候仍然要说"该补什么"，
    不能只剩一句"信息不足"——那等于把问题丢回给用户自己猜。"""
    out = _html('columnInsufficientHtml("ye_tianshi", "")')
    assert "舌象" in out and "脉象" in out


# ---------- 状态五：安全拦截（整页替换） ----------


def test_the_safety_page_gives_advice_and_says_the_stop_was_by_design():
    """§3.1：**整页替换**为拦截说明 + 就医指引。就医指引是固定文案不是模型
    生成的——拦截的整个意义就是"不让模型继续说话"。"""
    out = _html('safetyBlockHtml("解黑色柏油样便")')
    assert "解黑色柏油样便" in out
    assert "120" in out and "急诊" in out
    assert "设计如此" in out and "不是出错" in out


def test_the_safety_page_contains_no_prescription_identifiers_at_all():
    """"不显示任何方药区域"要能被机器检查，判据是**这一页的 HTML 里搜不到
    `herb` / `formula` 这两个标识**。整页替换的实现（showSafetyBlock）
    是清空三列和对照带而不是 CSS 隐藏——隐藏起来的东西仍然在 DOM 里，
    一次复制粘贴、一次"检查元素"就露出来了。"""
    out = _html('safetyBlockHtml("解黑色柏油样便")').lower()
    assert "herb" not in out and "formula" not in out
    src = load_app_js()
    body = src[src.index("function showSafetyBlock"):src.index("function hideSafetyBlock")]
    assert "clearColumns()" in body, "整页替换必须真的清空三列，不是 CSS 隐藏"
    assert 'rx-compare").innerHTML = ""' in body, "对照带也要清掉"


def test_the_safety_page_offers_a_way_back():
    """整页替换之后必须有路走回去。没有这颗按钮，拦截页是个死胡同——唯一的
    出路是刷新整个页面，而那会把 BYOK、角色、检索模式一起清掉。
    "打断必须彻底"说的是不给方药，不是把人困在这一页。"""
    out = _html('safetyBlockHtml("解黑色柏油样便")')
    assert 'id="sb-back"' in out and "换一条主诉" in out


# ---------- 用药对照带 ----------


def _divergence(shared, unique, eps, eps_scope, pairs_mean):
    return json.dumps({
        "shared_herbs": shared, "unique_herbs": unique,
        "epsilon_for_query": {"value": eps, "scope": eps_scope},
        "pairs_mean": pairs_mean,
        "core_jaccard": None, "adjunct_jaccard": None,
    }, ensure_ascii=False)


RESULTS_JS = '[{physician: "ye_tianshi"}, {physician: "wu_jutong"}, {physician: "zhang_xichun"}]'


def test_the_band_has_exactly_one_dot_per_herb_shared_plus_each_unique():
    """§3.1 第三条：点阵对照带——共用药实心点、各家独有药按各自身份色。
    一眼看出"共同的少、各自的多"，比 0.53 这个数直观得多。

    集合本身在后端算（divergence.shared_herbs / unique_herbs）：判定两味药
    是不是同一味走 core/herbs.py::normalized_herb_set（"炙甘草三钱" = "甘草"），
    前端拿 s3.herbs 自己做集合差就是第二套匹配实现（CLAUDE.md 第 31 条）。"""
    d = _divergence(["甘草", "茯苓"], {"ye_tianshi": ["柴胡", "白芍", "香附"],
                                      "wu_jutong": ["黄连", "吴茱萸"],
                                      "zhang_xichun": ["赭石"]}, 0.26, "query", 0.53)
    out = _html(f"rxCompareHtml({d}, {RESULTS_JS})")
    assert out.count('class="dot dot-shared"') == 2
    assert out.count("dot dot-own") == 3 + 2 + 1
    assert "共用 2" in out and "叶天士独有 3" in out and "张锡纯独有 1" in out


def test_the_band_splits_the_line_at_epsilon_with_noise_left_and_real_right():
    """ε 段用 --noise 灰、超出段用 --real 黑——视觉上直接说明"这一截是真的"。
    比值 = ε :(差异 - ε)，所以 ε=0.26 / 差异=0.52 应当正好一半一半。"""
    assert _json("bandSegments(0.26, 0.52)") == {"epsPct": 50.0, "realPct": 50.0, "exceeds": True}
    # 差异没超过地板：超出段宽度为 0，但两个读数照旧都显示——"没超出"本身
    # 就是结论，把带子藏起来等于把结论也藏了。
    assert _json("bandSegments(0.40, 0.20)") == {"epsPct": 100.0, "realPct": 0.0, "exceeds": False}
    # 任一端未测就不画线（画一条没有刻度的线比不画更误导）
    assert _json("bandSegments(null, 0.5)") is None
    assert _json("bandSegments(0.3, null)") is None


def test_the_band_says_it_fell_back_to_the_global_floor():
    """ε 取**当前这条主诉**的地板（实测 9 条可用主诉里 4 条高于全局均值，
    最高一条 0.3954 对 0.2611）。取不到才退回全局值，**而且必须标出来**——
    一个没说明来源的对照基准跟没有对照一样（CLAUDE.md「任何数字都必须带对照」）。"""
    assert "（全局）" in _html('epsilonLabel({value: 0.2611, scope: "global"})')
    assert "（全局）" not in _html('epsilonLabel({value: 0.3954, scope: "query"})')
    assert _html('epsilonLabel({value: 0.3954, scope: "query"})') == "噪声地板 ε=0.3954"
    assert _html('epsilonLabel({value: null, scope: "none"})') == "噪声地板 未测"


def test_the_band_refuses_to_interpret_a_number_without_a_floor():
    """ε 没测过时不下"这是真分歧"的结论。这条是 CLAUDE.md 那条铁律在界面上的
    落点：一个没有基准的数字在这个项目里等于没有意义。"""
    d = _divergence(["甘草"], {"ye_tianshi": ["柴胡"]}, None, "none", 0.53)
    out = _html(f"rxCompareHtml({d}, {RESULTS_JS})")
    assert "没有噪声地板可比" in out and "未测" in out
    assert "真实分歧" not in out


def test_the_band_names_the_verdict_when_the_difference_clears_the_floor():
    d = _divergence(["甘草"], {"ye_tianshi": ["柴胡"]}, 0.26, "query", 0.53)
    assert "超出噪声地板的部分是真实分歧" in _html(f"rxCompareHtml({d}, {RESULTS_JS})")
    d2 = _divergence(["甘草"], {"ye_tianshi": ["柴胡"]}, 0.60, "query", 0.20)
    assert "三家实质一致" in _html(f"rxCompareHtml({d2}, {RESULTS_JS})")


# ---------- 药材折叠 ----------


CAND_JS = json.dumps({"name": "柴胡疏肝散", "rationale": "r", "herb_items": [
    _herb("柴胡", "君", 6), _herb("白芍", "臣", 12), _herb("香附", "臣", 9),
    _herb("陈皮", "佐", 9), _herb("枳壳", "佐", 9), _herb("川芎", "佐", 6),
    _herb("延胡索", "佐", 9), _herb("甘草", "使", 3),
]}, ensure_ascii=False)


def test_herbs_default_to_jun_chen_plus_the_first_two_zuo():
    """§3.1 第四条：三列并排时垂直空间是稀缺资源，而**君臣决定了这一路是
    什么打法**，佐使是加减。"""
    out = _html(f"herbGroupsHtml({CAND_JS})")
    head = out[:out.index("herb-fold")]
    for name in ("柴胡", "白芍", "香附", "陈皮", "枳壳"):
        assert name in head, f"{name} 应该默认可见"
    for name in ("川芎", "延胡索", "甘草"):
        assert name not in head, f"{name} 应该被折叠"


def test_the_fold_counts_herbs_not_groups():
    """**按味折叠不是按组折叠**：佐药一组就有六味时，整组留下等于没折叠。
    展开按钮上的 N 也必须是味数——跟折叠区里实际有几味对不上的话，
    这个数就是在骗人。"""
    assert "⋯ 展开 3 味" in _html(f"herbGroupsHtml({CAND_JS})")
    assert _json(f"splitHerbGroupsForFold(groupHerbsByRole({CAND_JS}.herb_items)).nFolded") == 3


def test_a_short_formula_has_no_fold_at_all():
    """五味以内的方子折不出东西来，这时候不该出现一个写着"展开 0 味"的按钮。"""
    short = json.dumps({"name": "x", "rationale": "r", "herb_items": [
        _herb("柴胡", "君"), _herb("白芍", "臣"), _herb("陈皮", "佐")]}, ensure_ascii=False)
    out = _html(f"herbGroupsHtml({short})")
    assert "herb-fold" not in out and "柴胡" in out and "陈皮" in out


def test_doctor_mode_gets_every_herb_unfolded():
    """折叠是阅读密度的手段，不是信息裁剪。医生要编辑全量药味，折叠会让
    "下面还有几味"变成一次多余的点击。"""
    out = _html(f"herbGroupsHtml({CAND_JS}, false)")
    assert "herb-fold" not in out
    for name in ("柴胡", "川芎", "延胡索", "甘草"):
        assert name in out


# ---------- 引用折叠 ----------


def test_references_collapse_into_one_line_with_a_count():
    """§3.1 第九条：「引自 N 条医案 ▾」，展开后才是原文块。N 从 refs.length
    现算，不另存一个计数——两处各存一份必然有一处忘了更新。"""
    refs = json.dumps([{"case_id": f"ye-{i}", "visit_label": "初诊", "score": "0.8",
                        "symptoms": ["胃痛"], "excerpt": "原文", "syndrome": "肝胃不和"}
                       for i in range(3)], ensure_ascii=False)
    out = _html(f"refFoldHtml({refs})")
    assert "引自 3 条医案" in out and "<details" in out and "ye-2" in out


def test_no_reference_says_so_instead_of_showing_an_empty_fold():
    """一个写着「引自 0 条医案」的折叠框比一句话更难读懂。没有引用是一条
    结论（这条结论没有医案支撑），要直说。"""
    out = _html("refFoldHtml([])")
    assert "无相关医案" in out and "<details" not in out


# ---------- 三列外壳 ----------


def test_a_column_is_not_a_card():
    """§3.1 第一条：三家是对同一段主诉的三种读法，**卡片会暗示它们是三个
    独立的东西**。列与列之间只有一条细线。"""
    result = json.dumps({
        "physician": "ye_tianshi", "physician_name": "叶天士",
        "s3": {"syndrome": "肝胃不和证", "treatment_principle": "疏肝理气",
               "formula": "柴胡疏肝散", "herbs": ["柴胡"], "reasoning": "r",
               "cited_case_ids": ["ye-1"]},
        "refs": [], "hallucinated": [],
    }, ensure_ascii=False)
    out = _html(f"columnHtml({result})")
    assert 'class="card"' not in out
    assert 'class="col"' in out and 'data-physician="ye_tianshi"' in out


def test_identity_colour_only_reaches_the_head_and_the_top_border():
    """§3.1 第二条：身份色只用在顶部三行 + 3px 顶边，**列内正文全部 --ink**
    ——整列染色会让人看颜色而不看内容。

    色值走 `var(--phys-<id>)`（由 /health 注入），这里一个十六进制都不许出现：
    写死的话注册表加第四位医家时这份副本不会跟着长出来（CLAUDE.md 第 31 条
    前端小节，这个坑已经踩过一次）。"""
    out = _html('columnShellHtml("ye_tianshi", "叶天士", PHYSICIAN_META.ye_tianshi, "done", "<p>正文</p>")')
    assert "var(--phys-ye_tianshi" in out
    assert "#" not in out.replace("&#39;", ""), f"列里出现了写死的颜色：{out}"
    assert "1667-1746" in out and "温病" in out
