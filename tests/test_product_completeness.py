"""R47 §8.4：产品完成度里**可以自动验证**的那些条。

这一节决定"像不像成品"。能自动验的验，验不了的（视觉一致性、文案是否顺口）
由 Playwright 的产品模式全套截图人工过一遍——那部分不在这里假装成绿点。
"""
import re
from pathlib import Path

import pytest

from tests.web_harness import load_app_js, load_css, load_html

ROOT = Path(__file__).resolve().parent.parent
HTML = load_html()
CSS = load_css()
APP_JS = load_app_js()


# ---------- 第 25 条：空状态与首次进入 ----------

def test_the_empty_state_says_what_to_type_what_you_get_and_where_the_limit_is():
    """不是光秃秃的输入框：输入什么 / 得到什么 / 边界在哪，三行都要在。"""
    assert 'id="how-to-use"' in HTML
    block = HTML[HTML.index('id="how-to-use"'):HTML.index("</ul>", HTML.index('id="how-to-use"'))]
    assert block.count("<li>") == 3, "使用说明不是三行"
    for word in ("输入", "得到", "边界"):
        assert word in block


def test_the_empty_state_names_the_regulatory_boundary():
    """§0.4 第 1 条：输入侧的边界要在首屏就说清楚——文字描述，不收照片与
    仪器信号。这不是免责话术，是这个产品不作为医疗器械管理的前提。"""
    block = HTML[HTML.index('id="how-to-use"'):HTML.index("</ul>", HTML.index('id="how-to-use"'))]
    assert "照片" in block and ("脉诊仪" in block or "信号" in block)
    assert "检验数值" in block


def test_the_empty_state_is_hidden_once_you_have_asked_once():
    assert "#consult-page:not(.state-first) #how-to-use" in CSS


def test_the_first_screen_still_offers_examples():
    assert 'id="examples"' in HTML


# ---------- 第 26 条：加载态 ----------

def test_the_loading_state_is_a_skeleton_not_a_spinner():
    assert 'id="chain-skeleton"' in HTML
    assert "#consult-page.state-running #chain-skeleton" in CSS
    # 五行长短不一：骨架要画"待会儿这里有什么"，一条等长的灰带说不出这个
    assert HTML.count('class="sk-row') >= 5


def test_the_skeleton_does_not_animate_at_all():
    """§2.4 只允许三处动效，其中只有一处需要关键帧；而骨架屏的信息量全在
    **形状**上，扫光只是装饰。所以它是静态的，整份 CSS 里仍然只有一段关键帧。

    这条断言数的是关键帧块数，所以这段说明里**不能写出那个 at-rule 的字面量**
    ——写了就自己把计数加一，测试红在自己的注释上。同一个坑本轮已经踩过两次
    （test_no_demo_artifacts 的禁词、这里），判据是一样的：**扫源码文本的测试，
    它自己的说明文字也在被扫的范围里。**"""
    i = CSS.index(".sk-row {")
    block = CSS[i:CSS.index("}", i)]
    assert "animation" not in block
    assert CSS.count("@" + "keyframes") <= 1


def test_the_eta_comes_from_measured_durations_not_a_hardcoded_number():
    """**基于实测 p50**。写死一个秒数在另一台机器上就是错的，而一个错的
    倒计时比没有倒计时更伤信任。"""
    assert "durationP50" in APP_JS and "DURATION_KEY" in APP_JS
    # 取的是本机跑过的历时，不是某份报告里的数
    assert "localStorage.getItem(DURATION_KEY)" in APP_JS


def test_the_eta_says_it_has_no_samples_instead_of_guessing():
    """R56 §6 第 4 条：样本不足时**不说"样本"这个词**——「这台服务还没有
    足够的历时样本（0/3）」读起来像在说"这台服务没人用过"，不是给患者/医师
    看的产品文案。改成按 S3 推理档位给一个经验估计区间，并且**在文案里
    明说这是经验估计**（不是本机实测中位数）——不给一个不带说明的编造秒数，
    这才是「任何数字都必须带对照」那条铁律的反面用法：不能不给对照，
    但更不能连"这是估计还是实测"都不说清楚。"""
    assert "DURATION_MIN_SAMPLES" in APP_JS
    assert "样本" not in APP_JS.split("function etaText(")[1][:APP_JS.split("function etaText(")[1].index("\n}\n")]
    assert "按当前档位的经验估计" in APP_JS


def test_only_completed_consults_feed_the_median():
    """追问/信息不足/被拦截都没跑完整条链，混进中位数会让预计时间越来越短，
    而那不是它变快了。"""
    assert 'stopEta(state === "done")' in APP_JS


# ---------- 第 27 条：错误态 ----------

def test_there_is_a_retry_path_out_of_every_error_box():
    assert 'id="error-box"' in HTML
    assert 'id="submit-btn"' in HTML  # 重试入口就是同一个按钮，不另开一个


def test_no_traceback_or_exception_name_reaches_the_error_box():
    src = APP_JS
    i = src.index("function showError")
    body = src[i:i + 1200]
    for word in ("traceback", "Traceback", "stack", "LLMError"):
        assert word not in body


# ---------- 第 28 条：空结果 ----------

def test_the_empty_result_states_the_coverage_and_a_next_step():
    assert 'id="empty-result"' in HTML
    assert "renderEmptyResult" in APP_JS
    assert "当前覆盖" in APP_JS and "建议：" in APP_JS


def test_the_empty_result_does_not_swallow_the_other_states():
    """被安全层拦下、信息不足各有各的界面，不该被这一块盖住。"""
    assert "!data.rejected && !data.insufficient" in APP_JS


# ---------- 第 29 条：打印 ----------

def test_print_is_a4_portrait_with_margins():
    assert "@page" in CSS and "A4 portrait" in CSS
    assert re.search(r"@page\s*\{[^}]*margin:", CSS)


def test_print_hides_the_chrome_and_keeps_the_disclaimer():
    i = CSS.index("@media print")
    block = CSS[i:CSS.index("\n}", CSS.index("#footer-disclaimer", i))]
    for sel in ("#tab-bar", "#topbar", "#input-panel", "#graph-panel"):
        assert sel in block, f"打印时没藏掉 {sel}"
    assert "#footer-disclaimer" in block, "打印时把免责声明也藏掉了"


# ---------- 第 31 条：版本与发布 ----------

def test_the_version_is_semantic_and_defined_once():
    from core.version import VERSION

    assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", VERSION), VERSION
    # 唯一定义：别处不许再写一个版本号字面量。
    # **先去注释再扫**——graph.js 的注释里写着 `dagre.version === "0.8.5"`
    # （那是第三方库的版本，是一条 bug 记录），不去注释这条会红在一句说明上。
    # 这个形状从 R42 起已经踩过四次了。
    from tests.test_no_demo_artifacts import strip_js_comments

    others = [p for p in (ROOT / "web").glob("*.js")
              if re.search(r'"\d+\.\d+\.\d+(-rc\.\d+)?"',
                           strip_js_comments(p.read_text(encoding="utf-8")))]
    assert not others, f"这些文件里另写了一个版本号：{[p.name for p in others]}"


def test_the_footer_shows_the_version_from_health_not_a_literal():
    assert 'id="footer-version"' in HTML
    assert "health.version" in APP_JS


def test_the_changelog_is_written_for_users():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    from core.version import VERSION

    assert VERSION in text, "当前版本在更新日志里没有条目"
    # 内部轮次编号不进面向使用者的日志
    assert not re.search(r"\bR\d{2}\b", text.split("---", 1)[-1])


# ---------- 第 32 条：品牌与图标 ----------

def test_there_is_a_favicon_and_it_is_not_a_placeholder():
    m = re.search(r'<link rel="icon" href="([^"]+)"', HTML)
    assert m, "没有 favicon"
    assert m.group(1).startswith("data:image/svg+xml"), "favicon 不是内联 SVG"


# ---------- 第 33 条：响应式 ----------

@pytest.mark.parametrize("query", ["(max-width: 768px)", "(max-height: 800px)"])
def test_the_two_clinic_form_factors_have_their_own_rules(query):
    """1366×768 是诊室里最常见的那块屏（竖向最紧），768px 是平板。"""
    assert f"@media {query}" in CSS


# ---------- 第 34 条：首次引导 ----------

def test_the_guide_has_three_steps_and_both_exits():
    i = HTML.index('id="onboarding"')
    block = HTML[i:HTML.index("</div>", HTML.index("ob-actions", i))]
    assert block.count("<li>") == 3
    assert 'id="ob-skip"' in block and 'id="ob-done"' in block


def test_the_guide_shows_once_and_can_be_reopened():
    assert "ONBOARDING_KEY" in APP_JS
    assert "onboardingSeen()" in APP_JS
    assert 'id="guide-open-btn"' in HTML


def test_the_guide_can_be_closed_with_escape_and_the_backdrop():
    """一个只能靠按钮关的对话框在平板上很容易变成死路。"""
    i = APP_JS.index("const ob = document.getElementById(\"onboarding\")")
    block = APP_JS[i:i + 900]
    assert "Escape" in block and "e.target === ob" in block


# ---------- 第 35 条：帮助入口 ----------

def test_every_help_entry_has_a_sentence_and_an_example():
    """没有例子的说明等于把术语换了一个说法。"""
    i = APP_JS.index("const HELP_TEXT")
    block = APP_JS[i:APP_JS.index("\n};", i)]
    assert block.count("title:") >= 2
    assert block.count("body:") == block.count("title:")
    assert block.count("eg:") == block.count("title:")


def test_the_help_button_meets_the_touch_target_size():
    i = CSS.index(".help-btn {")
    block = CSS[i:CSS.index("}", i)]
    assert "width: 44px" in block and "height: 44px" in block


def test_every_help_button_in_the_html_has_a_matching_entry():
    keys = set(re.findall(r'data-help="([^"]+)"', HTML))
    i = APP_JS.index("const HELP_TEXT")
    block = APP_JS[i:APP_JS.index("\n};", i)]
    for k in keys:
        assert f'"{k}"' in block or f"{k}:" in block, f"「?」按钮 {k} 没有对应文案"
