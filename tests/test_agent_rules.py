"""R44：四能力的**规则表**（`core/agent.py`）。

## 这一层为什么存在

`consult()` 里对"什么时候停 / 问 / 取证 / 验"的判断原来是散落的 `if`，
而那份"被拦截的返回值"在同一个函数里有**四份拷贝**——其中一份带
`"coverage": None`、另一份没有，键的顺序也各不相同。前端按同一份契约读，
缺一个键就是 KeyError。

R44 把"什么时候"收成一张表，把"被拦截的返回值"收成一个函数。
**判断本身一行没动**：仍然在 `core.safety` / `core.followup` / `core.react` /
`core.formula_verifier` 里——这一层只决定"此刻该由谁上场"。

## 这个文件钉什么

1. 规则表的结构性质：每条都有 `why` 与 `gate`、id 不重、**安全排在最前**。
2. `gate` 指的模块/符号**真的存在**，而且这一层没有自己再实现一遍判断。
3. 四条返回路径的键集**逐字节一致**（那正是四份拷贝漂掉的地方）。
4. `EVAL_MODE` 只放行流程、不放行记录（`safety_flag` 照样如实记）。
"""
from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

from core import agent
from core.agent import (
    AGENT_RULES,
    CAPABILITIES,
    CAPABILITY_LABEL,
    RULES_BY_ID,
    STOP_KIND_LABEL,
    AgentTrace,
    decide,
    has_voting_language,
    rules_for,
    stop_rules_come_first,
)

ROOT = Path(__file__).resolve().parent.parent
CHAIN_SRC = (ROOT / "core" / "chain.py").read_text(encoding="utf-8")


# ---------- 一、规则表的结构 ----------

def test_the_four_capabilities_are_exactly_these_four():
    """"四能力"不是四个新模块，是给已经存在的四件事起了一个统一的名字。"""
    assert CAPABILITIES == ("stop", "ask", "gather", "verify")
    assert set(CAPABILITY_LABEL) == set(CAPABILITIES)


def test_every_capability_actually_has_at_least_one_rule():
    """表里列了一个能力却没有任何规则会用到它 = 那个能力在这套设计里不存在。"""
    for cap in CAPABILITIES:
        assert rules_for(cap), f"{cap} 一条规则都没有"


def test_rule_ids_are_unique():
    assert len({r.id for r in AGENT_RULES}) == len(AGENT_RULES)


def test_every_rule_can_say_why():
    """没有 `why` 的规则等于一个不能解释的行为。**这句话要进响应体给人看**，
    所以它得是一句人话，不是一个 id。"""
    for r in AGENT_RULES:
        assert r.why and len(r.why) >= 10, r.id
        assert not re.fullmatch(r"[a-z_]+", r.why), f"{r.id} 的 why 是个 id 不是人话"


def test_safety_rules_come_first_and_the_order_is_the_priority():
    """CLAUDE.md 的「安全否决在 S2 之前」在这张表里的具体形式，就是这几条的位置。"""
    assert stop_rules_come_first()
    first = AGENT_RULES[0]
    assert first.capability == "stop" and first.stop_kind == "safety"
    # 四条安全规则必须连在最前面（中间插一条别的 = 有一条安全判断排到后面去了）
    safety = [i for i, r in enumerate(AGENT_RULES) if r.stop_kind == "safety"]
    assert safety == list(range(len(safety))), f"安全规则不连续：{safety}"


def test_stop_kinds_are_three_and_each_has_a_label():
    """三类的产品含义完全不同，**不许合并成一个"失败"**：
    危重要的是让人立刻去急诊、依据不足要的是补信息、验证否决要的是重开或人工介入。"""
    kinds = {r.stop_kind for r in rules_for("stop")}
    assert kinds == {"safety", "evidence", "veto"}
    for k in kinds:
        assert STOP_KIND_LABEL[k]


@pytest.mark.parametrize("rule", AGENT_RULES, ids=lambda r: r.id)
def test_every_rules_gate_really_exists(rule):
    """`gate` 写的是判断的实现在哪。**不是注释**：这条测试去核实那个符号真的在。"""
    mod_name, _, sym = rule.gate.partition(":")
    mod = importlib.import_module(mod_name)
    assert hasattr(mod, sym), f"{rule.id} 的 gate 指向不存在的 {rule.gate}"


def test_the_agent_layer_does_not_reimplement_any_judgement():
    """**这一层不判断，只翻译。** 出现关键词表、正则、阈值就说明有人在这里
    又实现了一遍——那正是 CLAUDE.md 第 31 条说的第二处实现。"""
    src = inspect.getsource(agent)
    body = "\n".join(ln for ln in src.split("\n") if not ln.lstrip().startswith("#"))
    for banned in ("re.compile", "DANGER", "min_length", "threshold", "MAX_"):
        assert banned not in body, f"agent 层里出现了 {banned}，像是在这里又判了一遍"


def test_only_safety_rules_are_bypassable_and_that_is_on_purpose():
    """`EVAL_MODE` 要让被拦的主诉也走完一遍拿到分数，靠的是 `safety_flag`
    如实记录，不是让规则不触发。**放行的是流程，不是记录**。"""
    for r in AGENT_RULES:
        if r.bypassable:
            assert r.stop_kind == "safety", f"{r.id} 不是安全类却可被放行"


# ---------- 二、decide / trace ----------

def test_decide_returns_nothing_when_the_gate_did_not_hit():
    assert decide("danger_in_complaint", None) is None
    assert decide("danger_in_complaint", "") is None
    assert decide("no_elements", False) is None


def test_decide_carries_the_evidence_separately_from_the_reason():
    """`why` 是规则本身的理由（固定），`detail` 是这一次的证据（变的）。
    合成一个字段的话，界面上就分不清"制度是这样"和"这次是因为你说了这句话"。"""
    d = decide("danger_in_complaint", "吐血")
    assert d is not None
    assert d.detail == "吐血"
    assert d.why == RULES_BY_ID["danger_in_complaint"].why
    assert d.why != d.detail


def test_bypass_only_works_on_bypassable_rules():
    assert decide("danger_in_complaint", "吐血", bypass=True) is None
    # 证素为空不是安全类，EVAL_MODE 不该让它消失
    assert decide("no_elements", True, bypass=True) is not None


def test_an_unknown_rule_id_raises_instead_of_silently_doing_nothing():
    """打错一个 id 就静默什么都不做 = 那条规则从此不生效，而且不报错。"""
    with pytest.raises(KeyError):
        decide("no_such_rule", True)
    with pytest.raises(KeyError):
        AgentTrace().record("no_such_rule")


def test_the_trace_keeps_the_order_and_finds_the_first_stop():
    tr = AgentTrace()
    tr.record("ask_for_missing_symptoms", "问了 2 轮")
    tr.record("gather_evidence", "取证 3 步")
    tr.record("symbolic_veto", "甘草反甘遂")
    assert [d.rule_id for d in tr.decisions] == [
        "ask_for_missing_symptoms", "gather_evidence", "symbolic_veto"]
    assert tr.stopped().rule_id == "symbolic_veto"


def test_a_trace_without_a_stop_says_so():
    tr = AgentTrace()
    tr.record("gather_evidence")
    assert tr.stopped() is None


def test_the_serialised_decision_carries_the_chinese_labels():
    """前端不写死能力名/中止类型的中文（同 `CHAIN_LAYERS` 的层名那条）。"""
    d = AgentTrace().record("danger_in_complaint", "吐血").to_dict()
    assert d["capability_label"] == "中止"
    assert d["stop_kind_label"] == "危重拦截"
    assert d["rule"] == "danger_in_complaint"
    assert d["detail"] == "吐血"


# ---------- 三、consult 里那四份拷贝没了 ----------

def test_the_stopped_result_exists_exactly_once():
    """R44 之前这个 dict 在 `consult()` 里有四份拷贝，而且已经开始漂。"""
    assert "def _stopped(decision, **state) -> dict:" in CHAIN_SRC
    # 那几份拷贝的特征串：一行里同时出现这三个键
    dup = re.findall(r'"rejected":\s*True,\s*\n?\s*"reject_reason"', CHAIN_SRC)
    assert not dup, f"又出现了 {len(dup)} 份手写的拦截返回值"


def test_every_stop_point_goes_through_the_rule_table():
    """五个中止点都要走规则表，漏一个的表现是"那条路径上没有 agent_trace"。"""
    for rule_id in ("danger_in_complaint", "danger_in_followup_answer",
                    "danger_in_asserted", "danger_in_react_answer",
                    "no_elements", "symbolic_veto"):
        assert rule_id in CHAIN_SRC, f"chain 里没有用到规则 {rule_id}"


def test_every_return_point_of_consult_carries_the_trace():
    """键集不一致就是前端的 KeyError。**逐个返回点数**，不是抽查。"""
    body = CHAIN_SRC[CHAIN_SRC.index("def consult("):]
    body = body[:body.index("\ndef consult_many(")]
    # `_stopped` 统一带了 agent_trace，所以这里数的是**没走 _stopped 的**那些
    returns = len(re.findall(r'\n        return \{', body))
    traced = body.count('"agent_trace": trace.to_list()')
    assert traced >= returns, (
        f"consult 里有 {returns} 个手写的返回点，但只有 {traced} 个带 agent_trace")


def test_the_trace_is_shipped_to_the_client():
    api = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    assert '"agent_trace": outcome.get("agent_trace") or []' in api


def test_the_trace_is_given_to_every_role_including_patients():
    """患者最需要知道的正是"为什么让我去急诊"，而那句话就在这里。
    判据：`agent_trace` 不在任何一处按角色摘键的清单里。"""
    api = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    for pat in ('pop("agent_trace"', '"agent_trace"] =', "del response['agent_trace']"):
        assert pat not in api, f"agent_trace 被按角色摘掉了：{pat}"


# ---------- 四、消除投票痕迹 ----------

def test_the_voting_word_list_is_defined_once():
    """前端的判据、测试的判据、产品面的过滤都走同一个函数。"""
    assert has_voting_language("三位医家投票决定") == "投票"
    assert has_voting_language("五家综合的结论") == "五家综合"
    assert has_voting_language("肝胃不和证，疏肝和胃") is None


def test_the_structured_conclusion_is_no_longer_called_an_ensemble():
    """「五家综合」把这份结论说成"几个人拼出来的"——那是内部机制，不是产品形态。
    融合照旧在跑（`run_synthesis` 一行没改），变的是结论顶上那句话。"""
    from core.chain import SYNTHESIS_PHYSICIAN_ID, SYNTHESIS_PHYSICIAN_NAME

    assert SYNTHESIS_PHYSICIAN_ID == "synthesis", "内部 id 不许改（前端按它路由）"
    assert has_voting_language(SYNTHESIS_PHYSICIAN_NAME) is None, SYNTHESIS_PHYSICIAN_NAME
    assert SYNTHESIS_PHYSICIAN_NAME == "本次辨证"


def test_the_display_name_is_not_written_twice():
    """R44 改名时正是这种重复会让其中一处漏改，而漏改的表现是界面上两个地方
    叫法不一样。"""
    from core.physicians import SYNTHESIS_DISPLAY, synthesis_display

    assert SYNTHESIS_DISPLAY["name"] is None, "名字又在 physicians.py 里抄了一份"
    assert synthesis_display()["name"] == "本次辨证"
    # 配色等其余字段仍然在 physicians.py 一处定义
    assert synthesis_display()["color"] == SYNTHESIS_DISPLAY["color"]


def test_the_merging_capability_is_still_there():
    """**能力不删，产品面不露。** 改的是措辞，不是把融合去掉。"""
    assert "def run_synthesis(" in CHAIN_SRC
    assert "physicians_for_synthesis" in CHAIN_SRC
    assert "physician_influences" in CHAIN_SRC, "逐步归属还在，谁贡献了哪一步照样可查"
