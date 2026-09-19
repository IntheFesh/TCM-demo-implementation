"""R62 §13.1：产品面前端的单元测试。

§0 明确取消一切效果评测，所以这份**不测任何指标**。它测三件事：
  1. 不崩——三份 JS 语法正确、只引用真实存在的 DOM id、只打真实存在的端点；
  2. 角色裁剪的入口真的按角色消失（服务端那一半由 `tests/test_api_r62.py`
     与 `tests/test_patient_view.py` 覆盖，两层不是重复：那层管"拿不到"，
     这层管"看不到入口"）；
  3. 产品面文案的硬约束——措辞红线、不出现内部术语、不出现旧形态的字眼。

**真实渲染由 `scripts/verify_r62_ui.py` 验**（真浏览器、两种分辨率、
五档缩放）。CLAUDE.md 那条"JSON 结构测试测不出渲染层的问题"在这一轮又应验
了一次：`.overlay{display:flex}` 盖过 `[hidden]`，字符串扫描一辈子也扫不出来。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = ROOT / "web" / "product"
JS_FILES = ("app.js", "lab.js", "knowledge.js")


def _read(name: str) -> str:
    return (PRODUCT / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return _read("index.html")


@pytest.fixture(scope="module")
def css() -> str:
    return _read("app.css")


@pytest.fixture(scope="module")
def js() -> str:
    return "\n".join(_read(n) for n in JS_FILES)


# ---------- 一、结构完整性 ----------


def test_the_old_frontend_is_untouched():
    """§4.0「旧文件可保留但产品模式下不加载」。R1~R61 的截图、演示脚本与
    32 份前端测试全指着旧那一份——就地换掉等于把此前所有轮次的验收一次作废。"""
    import subprocess

    r = subprocess.run(
        ["git", "diff", "--name-only", "16eb20e", "--", "web/index.html", "web/app.css"],
        cwd=ROOT, capture_output=True, text=True)
    assert r.stdout.strip() == "", f"旧前端被改动了：{r.stdout}"


def test_every_id_the_scripts_touch_exists_in_the_html(html, js):
    """`$("xxx")` 打不中任何元素时返回 null，之后的 `.value` 会抛——
    而那是一条只在某个分支上才走到的路径，功能测试不一定碰得到。
    这条把它变成静态可查的。"""
    ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    used = set(re.findall(r'\$\("([a-zA-Z0-9_-]+)"\)', js))
    # 运行时才创建的元素（追问输入框）不在 HTML 里，单列出来而不是放宽判据。
    runtime = {"fu-answer", "fu-send"}
    missing = used - ids - runtime
    assert not missing, f"脚本引用了 HTML 里没有的 id：{sorted(missing)}"


def test_every_endpoint_the_scripts_call_exists_on_the_server(js):
    """前端打一个不存在的路径得到 404/405，而 405 在界面上长成"保存失败"
    ——这一轮真的踩过（设置是 PUT，前端用 POST 打）。"""
    import api.main as api_main

    routes = {r.path for r in api_main.app.routes if hasattr(r, "path")}
    called = set(re.findall(r'["`](/api/[a-z_/]+)', js))
    for path in called:
        ok = path in routes or any(
            r.startswith(path.rstrip("/")) and "{" in r for r in routes)
        assert ok, f"前端打了一条服务端没有的路径：{path}"


def test_the_settings_endpoint_is_called_with_put_not_post(js):
    """这一条是上面那个 bug 的回归钉子：`/api/preferences` 的写入是 PUT。"""
    m = re.search(r'(\w+)\("/api/preferences", \{ changes \}\)', js)
    assert m and m.group(1) == "put", "设置写入必须走 put()，用 post() 会得到 405"


def test_hidden_really_hides(css):
    """`.overlay{display:flex}` 盖得过 UA 的 `[hidden]{display:none}`
    ——一个带着 hidden 的透明覆盖层会铺满整屏、吃掉所有点击，而页面
    看起来一切正常。这条钉住那一行全局兜底。"""
    assert "[hidden] { display: none !important; }" in css


# ---------- 二、布局与缩放的硬约束 ----------


def test_the_three_columns_add_up_to_the_smaller_target_resolution(css):
    """§4.2：左 280 + 中自适应 + 右 380 = 1366。右栏用 `min()` 而不是定值，
    再窄的屏上按视口比例收，而不是把中栏挤没。"""
    assert "--left-w: 280px;" in css
    assert "--right-w: min(380px, 32vw);" in css


def test_no_viewport_height_is_used_for_any_fixed_height(css):
    """§4.4：浏览器缩放改的是 CSS 像素与设备像素之比，`vh` 写死的高度在
    200% 下会把一个 600px 的容器变成占满两屏。所以图谱容器的高度由内容
    驱动 + `min-height`，全文件不许出现 `height: NNvh`。"""
    bad = re.findall(r"(?<!min-)(?<!max-)height:\s*[\d.]+vh", css)
    assert not bad, f"用 vh 写死了高度：{bad}"


def test_the_knowledge_graph_container_grows_instead_of_being_pinned(css):
    block = css[css.index(".kb-graph"):css.index(".kb-graph") + 200]
    assert "min-height" in block and "flex: 1 1 auto" in block


# ---------- 三、角色裁剪的入口 ----------


@pytest.mark.parametrize("selector", [
    "#tab-lab", "#sec-mods", "#sec-self", "#op-record", "#rx-add", "#records-block",
])
def test_the_patient_loses_the_entries_the_spec_says_they_lose(css, selector):
    """§8 那张表：患者没有加减建议、没有自评、不能改方、没有组方实验室、
    不能生成记录。服务端另有一层字段裁剪——两层不是重复。"""
    rule = re.search(r'html\[data-role="patient"\][^{]*\{[^}]*\}', css, re.S)
    assert rule, "没有患者角色的裁剪规则"
    block = css[css.index('html[data-role="patient"] .pro-only'):]
    block = block[:block.index("}") + 1]
    assert selector in block, f"患者角色下没有藏掉 {selector}"


def test_the_student_loses_export_and_record(css):
    block = css[css.index('html[data-role="student"]'):]
    block = block[:block.index("}") + 1]
    assert "#op-record" in block and "#op-export" in block


def test_the_patient_prescription_table_is_not_editable(css):
    assert 'html[data-role="patient"] table.rx input' in css


# ---------- 四、文案红线 ----------


#: §1.4 与 docs/glossary.md 的措辞红线。系统输出的是知识整理与依据呈现，
#: 不是"建议对该患者采用 X 治疗"——后者会让产品落进第三类医疗器械。
BANNED_WORDING = ("建议服用", "推荐处方", "治疗建议", "建议患者服用",
                  "推荐采用", "诊断为")

#: §5.1 要求删除的旧形态字眼，以及 §7.2「绝不出现」那一批。
BANNED_PRODUCT_TERMS = ("名医各自", "分道", "并列三列", "图谱浏览器",
                        "trace_id", "分歧度", "噪声地板", "消融")


def test_no_banned_wording_anywhere_in_the_product_surface(html, js):
    for text, where in ((html, "index.html"), (js, "脚本")):
        for word in BANNED_WORDING:
            assert word not in text, f"{where} 里出现了措辞红线「{word}」"


def test_no_leftovers_from_the_previous_product_shape(html):
    for word in BANNED_PRODUCT_TERMS:
        assert word not in html, f"index.html 里还留着「{word}」"


def test_no_file_paths_or_internal_codes_in_the_user_facing_html(html):
    """§7.2「绝不出现」：文件路径、`.py`、`.jsonl`、内部编码、字段名。

    **只扫使用者看得到的文本**，不扫属性值——`data-term="herb::柴胡"` 里的
    那个前缀是机器用的 id，它不会被印到屏幕上。"""
    visible = re.sub(r"<[^>]+>", " ", html)
    for word in (".py", ".jsonl", ".tsv", "core/", "data/", "SP-01"):
        assert word not in visible, f"使用者看得到的文本里出现了「{word}」"


def test_the_footer_carries_both_required_lines(html):
    """§1.4 页脚固定那一句 + §2 对标传神素问的那句定位话术。"""
    assert "不替代医生，只帮医生省时间" in html
    assert 'id="ft-disclaimer"' in html


def test_the_compliance_boundary_is_stated_on_the_first_screen(html):
    """§5.1：输入侧的边界要在首屏就说清楚——文字描述，不收照片与仪器信号。
    这不是免责话术，是这个产品不作为医疗器械管理的前提。"""
    block = html[html.index('id="io-spec"'):html.index("</dl>")]
    assert "照片" in block and ("脉诊仪" in block or "信号" in block)
    assert "检验数值" in block


def test_the_examples_say_what_they_are_clinically_not_how_they_are_implemented(js):
    """§5.5：标题就是「示例」，说明只写临床含义，不出现实现术语。"""
    block = js[js.index("const EXAMPLES"):]
    block = block[:block.index("];")]
    for word in ("切到患者模式", "整页拦截", "三家辨证并列"):
        assert word not in block
    assert "典型表现" in block and "征象" in block


def test_the_case_corroboration_section_says_it_did_not_feed_the_derivation(html):
    """§6.8 ⑩：标题下那句小字是 R54「绝不回头改推导」在界面上的唯一体现。"""
    assert "仅作对照，不参与上面的推导" in html


# ---------- 五、时间预算与判据一处定义 ----------


def test_the_fallback_matches_the_spec_and_is_shorter_than_the_old_one(js):
    """§11.3：`s3_done` 后 60 秒无 `done` 就主动查一次。旧界面是 120 秒
    ——那是 R55 定的，R62 把它收紧了一半。"""
    m = re.search(r"const DONE_FALLBACK_MS = (\d+);", js)
    assert m and int(m.group(1)) == 60000


def test_the_two_debounce_windows_match_the_spec(js):
    """§5.4 与 §7.3 都写的是「停 1.5 秒」。"""
    assert js.count("}, 1500);") >= 2, "问诊要点与编辑助手都应当是 1.5 秒"


def test_the_progress_panel_never_shows_internal_telemetry(js):
    """§11.4：不显示 token 数、帧数、首字耗时、样本数、模型名、调用次数。"""
    block = js[js.index("function progressLine"):js.index("const PROGRESS_ORDER")]
    for word in ("chars_content", "chars_reasoning", "first_delta_s", "events",
                 "llm_calls", "model"):
        assert word not in block, f"进度面板里印了内部读数 {word}"
