"""R47 §8.4 第 30 条：全站术语统一，扫不一致用词。

判据只对**第一类（同义异写）**成立——那一类是"说的是同一件事，只是写法
不同"，所以能靠词表判。第二类（证型/证候、治则/治法、方剂/处方）是近义但
不同义，两个词都要用、只是不许互换，那没法靠扫词判，`docs/glossary.md`
里写清楚区别供评审时用。**这条测试不假装能判第二类。**

扫的范围跟 `tests/test_no_demo_artifacts.py` 一致：产品可见的 HTML 文字 +
JS 里含中文的字符串（去注释之后）。源码注释不在约束内——注释里写「医生模式」
不会让任何患者看到。
"""
import re
from pathlib import Path

import pytest

from tests.test_no_demo_artifacts import (
    chinese_strings,
    js_chunks,
    product_visible_html_text,
)
from tests.web_harness import load_app_js, load_html

ROOT = Path(__file__).resolve().parent.parent
GLOSSARY = (ROOT / "docs" / "glossary.md").read_text(encoding="utf-8")

#: 第一类：只许用左边那个。表来自 docs/glossary.md，两边对不上时以那份为准
#: （有一条测试钉住"表里每一条都在 glossary 里"）。
CANONICAL = {
    "医师": ("医生", "大夫"),
    "患者": ("病人",),
    "主诉": ("主述",),
    "舌象": ("舌相", "舌苔象"),
    "脉象": ("脉相",),
    "证型": ("症型", "证形"),
}

HTML_TEXT = product_visible_html_text()
JS_STRINGS = [lit for chunk in js_chunks(load_app_js()).values()
              for lit in chinese_strings(chunk)]


def _hits(bad: str) -> list[str]:
    out = []
    if bad in HTML_TEXT:
        out.append(f"index.html: …{HTML_TEXT[max(0, HTML_TEXT.index(bad) - 12):][:36]}…")
    out += [f"app.js: {lit[:50]}" for lit in JS_STRINGS if bad in lit]
    return out


@pytest.mark.parametrize("good,bad", [(g, b) for g, bads in CANONICAL.items() for b in bads])
def test_only_the_canonical_word_appears_in_user_visible_text(good, bad):
    hits = _hits(bad)
    assert not hits, (f"用户可见文字里出现了「{bad}」，全站只用「{good}」"
                      f"（见 docs/glossary.md）：\n  " + "\n  ".join(hits))


def test_the_scan_actually_sees_the_user_visible_text():
    """自检：扫描范围要是空的，上面那一批会全部假绿。"""
    assert len(HTML_TEXT) > 200
    assert len(JS_STRINGS) > 100
    assert any("医师" in lit for lit in JS_STRINGS) or "医师" in HTML_TEXT


def test_every_rule_in_the_table_is_written_down_in_the_glossary():
    """词表与术语表不许分叉。分叉之后改一边、另一边悄悄失效，
    而失效的方向总是"少扫一条"。"""
    for good, bads in CANONICAL.items():
        assert good in GLOSSARY, f"术语表里没有「{good}」"
        for bad in bads:
            assert bad in GLOSSARY, f"术语表里没写明不用「{bad}」"


def test_the_glossary_separates_the_two_kinds():
    assert "第一类" in GLOSSARY and "第二类" in GLOSSARY
    assert "同义异写" in GLOSSARY and "不许互换" in GLOSSARY


def test_the_glossary_defines_the_pairs_that_are_easy_to_confuse():
    for a, b in (("证候", "证型"), ("治则", "治法"), ("方剂", "处方")):
        assert a in GLOSSARY and b in GLOSSARY


def test_the_glossary_carries_the_compliance_red_lines():
    """§0.4 第 2 条：输出侧的措辞决定这个产品算不算"辅助决策"。
    红线表跟术语表放在一起——两者都是"这个词能不能写"的问题。"""
    assert "建议患者服用" in GLOSSARY and "推荐采用该方治疗" in GLOSSARY
    assert "本证型的教材治法为" in GLOSSARY or "教材代表方" in GLOSSARY


def test_the_triage_wording_is_explicitly_exempt():
    """「建议尽快就诊」不是诊疗建议，是分诊导引，而且是安全边界的一部分
    （§8.3 第 21 条要求保留且更醒目）。术语表必须写明这条例外，
    否则下一轮会有人把它当红线一起删掉。"""
    assert "就医指引不在红线内" in GLOSSARY


def test_the_advice_section_is_not_called_a_recommendation():
    """§0.4：「方剂建议」把一层客观校验说成了一条诊疗意见。"""
    assert "方剂核查" in load_app_js()
    assert not any("方剂建议" in lit for lit in JS_STRINGS)


def test_the_disclaimer_states_the_device_classification():
    """§8.3 第 17 条：免责声明必须写明"不作为医疗器械管理、不提供诊断结论、
    须由执业医师审核"。少一句都不行——这是法律与安全要求。"""
    m = re.search(r'id="footer-disclaimer">(.*?)</p>', load_html(), re.DOTALL)
    assert m, "页脚没有免责声明"
    text = m.group(1)
    for must in ("不作为医疗器械管理", "不提供诊断结论", "执业医师"):
        assert must in text, f"免责声明缺「{must}」"
