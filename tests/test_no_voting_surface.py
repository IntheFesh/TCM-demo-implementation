"""R44：**产品面上不许有投票痕迹。**

## 这条约束是什么

这个系统的内部机制里确实有"几位医家各出一份结论再合起来"这一步（legacy 三列
模式、`run_synthesis` 的五家融合）。**那是研究能力，不是产品形态。**
总纲 §12 的原话：**能力不删，产品面不露**。

具体到措辞：产品面（患者 / 医生 / 学生）上不许把结论说成"投票 / 表决 / 几家
综合"——读者要看到的是**一位医师的推理过程**，名老中医经验是它引用的**依据**，
不是投票人。研究面（researcher 角色）照旧给全部三列与分歧读数，一个字段都不少。

## 为什么这不是"把能力藏起来"

- `run_synthesis` 一行没改，五家各出一份再融合照旧在跑；
- 谁贡献了哪一步仍然在 `physician_influences` 里逐条可查，九段界面照样显示
  「叶天士·取象」这种归属；
- `divergence`（分歧读数）与三列对照在 researcher 角色下照旧完整下发。

变的只是**框架**：从"几个人投票"改成"一位医师引用了几家的经验"。

## 判据从哪来

词表只有一处（`core.agent.VOTING_WORDS` / `ENSEMBLE_WORDS`），测试从它取，
不手抄——手抄的那份会在加词时漏掉。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.agent import ENSEMBLE_WORDS, VOTING_WORDS, has_voting_language

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
GRAPH_JS = (WEB / "graph.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


def _js_strings(src: str) -> list[str]:
    """源码里的字符串字面量（含模板串）。**只看会显示出去的那些**——
    注释里必须能提到"五家综合"（那是在解释为什么改掉它）。"""
    no_line_comments = "\n".join(ln.split("//")[0] for ln in src.split("\n"))
    out: list[str] = []
    for pat in (r'"((?:[^"\\]|\\.)*)"', r"'((?:[^'\\]|\\.)*)'", r"`((?:[^`\\]|\\.)*)`"):
        out.extend(re.findall(pat, no_line_comments, re.S))
    return out


# ---------- 一、词表 ----------

def test_the_word_list_is_not_empty_and_covers_the_obvious_ones():
    assert "投票" in VOTING_WORDS and "表决" in VOTING_WORDS
    assert "五家综合" in ENSEMBLE_WORDS


def test_the_matcher_is_the_single_implementation():
    """前端的判据、测试的判据、产品面的过滤都走这一个函数。"""
    for w in (*VOTING_WORDS, *ENSEMBLE_WORDS):
        assert has_voting_language(f"这里有{w}这个词") == w
    assert has_voting_language("") is None
    assert has_voting_language(None) is None


# ---------- 二、前端产品面 ----------

def test_no_voting_language_in_any_frontend_string_literal():
    """**这一条是这一轮的验收。** 界面上的字全在这些字面量里。"""
    bad = []
    for name, src in (("app.js", APP_JS), ("graph.js", GRAPH_JS)):
        for s in _js_strings(src):
            hit = has_voting_language(s)
            if hit:
                bad.append(f"{name}: …{s[:60]}… （命中「{hit}」）")
    assert not bad, "产品面上出现了投票措辞：\n" + "\n".join(bad)


def test_no_voting_language_in_the_html_shell():
    body = re.sub(r"<!--.*?-->", "", INDEX_HTML, flags=re.S)
    hit = has_voting_language(body)
    assert hit is None, f"index.html 里出现了「{hit}」"


def test_the_chain_header_frames_physicians_as_cited_evidence():
    """「引到 N 位医家」读起来像"有 N 个人参与了这次判断"（投票）；
    「引用名老中医经验 N 家」说的是"这一份判断引用了 N 家的经验"（依据）。
    **同一个数、同一份数据，改的是它在产品面上表达的关系。**"""
    assert "引用名老中医经验" in APP_JS
    assert "本次引到" not in APP_JS


def test_the_attribution_is_still_there_per_step():
    """**能力不删**：谁贡献了哪一步照样逐条显示。"""
    assert "physician_influences" in APP_JS
    assert "influenceStepLabel" in APP_JS


# ---------- 三、后端产品面 ----------

def test_the_conclusions_display_name_has_no_voting_language():
    from core.chain import SYNTHESIS_PHYSICIAN_NAME

    assert has_voting_language(SYNTHESIS_PHYSICIAN_NAME) is None


def test_the_agent_rule_reasons_have_no_voting_language():
    """规则表里的 `why` 会原样显示给人看。"""
    from core.agent import AGENT_RULES

    for r in AGENT_RULES:
        assert has_voting_language(r.why) is None, r.id


def test_the_capability_and_stop_labels_have_no_voting_language():
    from core.agent import CAPABILITY_LABEL, STOP_KIND_LABEL

    for label in (*CAPABILITY_LABEL.values(), *STOP_KIND_LABEL.values()):
        assert has_voting_language(label) is None, label


@pytest.mark.parametrize("path", [
    "prompts/v1/s3_structured.yaml",
    "prompts/v1/s3_syndrome.yaml",
])
def test_no_voting_language_in_the_prompts_that_shape_the_output(path):
    """prompt 里的措辞会被模型学进输出。这里出现"综合几家的意见"，
    模型就会在 `reasoning` 里写出来——那句话直接落在产品面上。"""
    f = ROOT / path
    if not f.exists():
        pytest.skip(f"{path} 不在这个仓库里")
    hit = has_voting_language(f.read_text(encoding="utf-8"))
    assert hit is None, f"{path} 里出现了「{hit}」"


# ---------- 四、研究面照旧完整 ----------

def test_the_research_surface_still_gets_divergence():
    """**能力不删。** researcher 角色的响应里 `divergence` 一个字段都不少。"""
    import api.main as api_main

    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    # 只有 patient/doctor 那一支摘掉 divergence（student/researcher 保留）
    assert 'response.pop("divergence", None)' in src
    i = src.index('response.pop("divergence", None)')
    head = src[max(0, i - 800):i]
    assert "student" in head and "return response" in head, (
        "摘 divergence 的位置变了，researcher/student 可能也被摘掉了")
    assert hasattr(api_main, "_filter_response_by_role")


def test_the_pairwise_divergence_machinery_is_untouched():
    chain = (ROOT / "core" / "chain.py").read_text(encoding="utf-8")
    for want in ("def run_synthesis(", "herb_jaccard", "epsilon_online"):
        assert want in chain, f"{want} 不见了——那是研究能力，不该被这一轮删掉"
