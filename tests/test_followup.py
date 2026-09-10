"""core/followup.py 的离线测试。用 ScriptedPatient 当提问渠道，不调 LLM。

重点在三条硬约束：安全检查排在解析之前、否定回答进后验、每轮 0 次 LLM 调用。
"""
import pytest

from core import followup as fu
from core.followup import (
    MAX_ASK_ROUNDS,
    MIN_USEFUL_IG,
    fast_mode_enabled,
    format_followup_for_s3,
    parse_answer,
    run_followup,
)
from core.tools import is_safety_relevant, syndrome_posterior
from eval.patient_sim import ScriptedPatient

ELEMENTS = ["胃", "肝", "气滞"]
SYMPTOMS = ["胃脘胀痛", "嗳气泛酸", "纳差"]


@pytest.fixture(autouse=True)
def _no_fast_mode(monkeypatch):
    monkeypatch.delenv("FAST_MODE", raising=False)


# ---------- 答案解析 ----------

@pytest.mark.parametrize("answer,expected", [
    ("有", "yes"), ("有的", "yes"), ("是的，经常", "yes"), ("偶尔有", "yes"),
    ("嗯", "yes"), ("确实有点", "yes"),
    ("没有", "no"), ("没", "no"), ("不苦", "no"), ("无", "no"),
    ("从来没有过", "no"), ("口不渴", "no"),
    ("不知道", "unknown"), ("说不清", "unknown"), ("时有时无", "unknown"),
    ("", "unknown"), ("   ", "unknown"),
])
def test_parse_answer(answer, expected):
    assert parse_answer(answer) == expected


def test_negation_checked_before_affirmation():
    """「没有」里含「有」。先查肯定的话所有否定都会被读成肯定——
    这是这套规则里唯一一处顺序真的要命的地方。"""
    assert parse_answer("没有") == "no"


def test_uncertain_beats_both():
    assert parse_answer("说不清，有时候有有时候没有") == "unknown"


# ---------- 循环基本行为 ----------

def test_followup_asks_and_records_four_fields():
    patient = ScriptedPatient(present=["两胁胀满"], absent=["脘腹痞满"])
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    assert r.rounds >= 1
    first = r.history[0]
    assert first.question and first.answer
    assert first.symptom
    # 四字段契约：asserted / denied 必须结构化存下来，不能只留 answer 原文
    assert set(first.asserted) | set(first.denied)
    assert "两胁胀满" in r.asserted


def test_denied_answer_goes_into_denied_not_asserted():
    patient = ScriptedPatient(present=[], absent=[], default="没有")
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    assert r.asserted == []
    assert len(r.denied) == r.rounds
    assert all(item.denied and not item.asserted for item in r.history)


def test_unknown_answer_records_neither():
    """归错成 yes/no 会把一条假证据写进后验，宁可当没问到。"""
    patient = ScriptedPatient(present=[], absent=[], default="说不清")
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    assert r.asserted == [] and r.denied == []
    assert all(not item.asserted and not item.denied for item in r.history)


def test_never_asks_the_same_symptom_twice():
    patient = ScriptedPatient(present=[], absent=[], default="没有")
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    symptoms = [item.symptom for item in r.history]
    assert len(symptoms) == len(set(symptoms))


def test_respects_max_rounds():
    patient = ScriptedPatient(present=[], absent=[], default="没有")
    r = run_followup(SYMPTOMS, ELEMENTS, patient, max_rounds=2)
    assert r.rounds <= 2
    assert r.stopped_by in ("max_rounds", "converged", "no_candidate")


def test_default_max_rounds_is_three():
    assert MAX_ASK_ROUNDS == 3


def test_no_ask_channel_is_not_an_error():
    r = run_followup(SYMPTOMS, ELEMENTS, None)
    assert r.stopped_by == "no_answer"
    assert r.rounds == 0


def test_patient_stops_answering():
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: None)
    assert r.stopped_by == "no_answer"


def test_fast_mode_skips_everything(monkeypatch):
    monkeypatch.setenv("FAST_MODE", "1")
    assert fast_mode_enabled() is True
    calls = []
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: calls.append(q) or "有")
    assert r.stopped_by == "fast_mode"
    assert calls == [], "开了 FAST_MODE 就一个问题都不该问"


def test_converged_stops_before_max_rounds(monkeypatch):
    """收敛退出和问满轮次要分开记：前者说明追问设计有效，后者说明轮次上限
    卡住了它，混成一个就没法调 MAX_ASK_ROUNDS。

    R2 教材扩表前这里是 0 轮就收敛：候选池里没有安全相关症状，MIN_USEFUL_IG
    一设到 99 每个问题都立刻不合格。扩表后候选池里出现了合法的安全相关症状
    （ELEMENTS=["胃","肝","气滞"] 下能匹配到胃热壅盛证的吐血——这是原始 17 条
    手工条目之一，disease_hint 收窄也不会把它排除，见 core.tools._scope_by_disease）。
    Category 2 的修复保证这条症状不管诊断信息增益多低都会被问一次
    （core/followup.py 里"安全相关候选不受 MIN_USEFUL_IG 收敛门槛约束"那段
    说明），问完之后再收敛——所以"收敛"仍然成立，只是不再是 0 轮，而是恰好
    1 轮（安全screening 用掉的那一轮）。"""
    monkeypatch.setattr(fu, "MIN_USEFUL_IG", 99.0)  # 任何非安全问题都达不到
    patient = ScriptedPatient(present=[], absent=[])
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    assert r.stopped_by == "converged"
    assert r.rounds == 1
    assert is_safety_relevant(r.history[0].symptom)


def test_min_useful_ig_is_a_small_positive_threshold():
    assert 0 < MIN_USEFUL_IG < 1.0


# ---------- 安全否决（CLAUDE.md 硬约束） ----------

def test_dangerous_answer_stops_the_loop_before_parsing():
    """追问是安全否决层的后门。回答里出现危重信号就整轮终止，
    答案不进后验、不产出方药。"""
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: "有，这两天还拉了黑便")
    assert r.stopped_by == "safety"
    assert r.reject_reason and "黑便" in r.reject_reason
    assert r.history[-1].safety_hit
    # 危险回答不许被当成一条普通症状写进后验
    assert r.asserted == [] and r.denied == []


def test_safety_check_runs_on_every_round_not_just_the_first():
    answers = iter(["没有", "没有", "最近吐了两次血"])
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: next(answers))
    assert r.stopped_by == "safety"
    assert r.rounds == 3


def test_safe_answer_does_not_trigger():
    r = run_followup(SYMPTOMS, ELEMENTS, ScriptedPatient(present=[], absent=[]))
    assert r.stopped_by != "safety"
    assert r.reject_reason is None


# ---------- 否定回答进后验 ----------

def test_denial_shifts_the_posterior():
    """患者说「没有口干或口苦」是一条真证据，必须把以它为主症的证候压下去。
    只从候选池里去重、不更新后验，等于把一半的追问收益扔掉。"""
    e = ["胃", "阴虚", "津伤", "热"]
    before = syndrome_posterior(e)
    after = syndrome_posterior(e, denied_symptoms=["口干或口苦"])
    assert after["SP-03"] < before["SP-03"]      # 脾胃湿热：口干或口苦是主症
    assert after["SP-09"] > before["SP-09"]      # 胃阴虚：不以它为主症


def test_denial_moves_top1_probability_by_more_than_five_percent():
    """R2 教材扩表把候选证候池从 17 撑到几百条之后，钉住"追问真的有用"这条
    验收标准：同一组证素，追问一个高信息量症状前后，top-1 证候的概率变化
    幅度必须 > 5%——不能是小数点第 15 位才有差异的浮点噪声量级（float64
    epsilon 约 2.2e-16，这正是本轮修复要防止退化成的样子）。"""
    e = ["胃", "阴虚", "津伤", "热"]
    before = syndrome_posterior(e)
    top1_code = max(before, key=before.get)
    after = syndrome_posterior(e, denied_symptoms=["口干或口苦"])
    p0, p1 = before[top1_code], after[top1_code]
    relative_change = abs(p1 - p0) / p0
    assert relative_change > 0.05, (
        f"{top1_code} 的概率 {p0} -> {p1}，相对变化 {relative_change:.2%}，"
        "追问一轮答案对后验几乎没有影响"
    )


def test_assertion_and_denial_move_in_opposite_directions():
    e = ["胃", "阴虚", "津伤", "热"]
    yes = syndrome_posterior(e, asserted_symptoms=["口干或口苦"])
    no = syndrome_posterior(e, denied_symptoms=["口干或口苦"])
    assert yes["SP-03"] > no["SP-03"]


def test_contradictory_evidence_stays_a_valid_distribution():
    """同一条症状既被肯定又被否认（患者改口）时，后验仍然是合法分布：没有 NaN、
    和为 1、没有负数。审查时发现原来这条测试传的是空列表，声称要测的分支一次都没进。"""
    post = syndrome_posterior(["胃", "阴虚"], asserted_symptoms=["口干或口苦"],
                              denied_symptoms=["口干或口苦"])
    assert post
    assert all(p == p and p >= 0 for p in post.values())
    assert abs(sum(post.values()) - 1.0) < 1e-9


def test_total_zero_likelihood_falls_back_to_uniform(monkeypatch):
    """似然真的全被压到 0 的那条分支：把钳位放开让 P_MAX=1，肯定+否认同一主症
    就会得到 0×1，全部证候归零，此时必须退回均匀分布而不是除零。"""
    from core import tools as tl

    monkeypatch.setattr(tl, "P_MAX", 1.0)
    monkeypatch.setattr(tl, "P_UNLISTED", 0.0)
    post = syndrome_posterior([], asserted_symptoms=["两胁胀满"], denied_symptoms=["两胁胀满"])
    assert post
    n = len(post)
    assert all(abs(p - 1 / n) < 1e-9 for p in post.values())


# ---------- 传给 S3 的摘要 ----------

def test_format_followup_keeps_denials_visible_to_s3():
    """肯定的症状会并进症状表传下去，否认的不会——S3 看不到就照样可能
    按那条症状去开方。"""
    patient = ScriptedPatient(present=[], absent=[], default="没有")
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    text = format_followup_for_s3(r)
    assert "患者明确否认" in text
    assert "阴性证据" in text
    for s in r.denied:
        assert s in text


def test_format_followup_empty_history_is_empty_string():
    assert format_followup_for_s3(run_followup(SYMPTOMS, ELEMENTS, None)) == ""


# ---------- 十问歌后备不许伪造证据 ----------

def test_shiwen_fallback_answer_is_not_attributed_to_a_symptom(monkeypatch):
    """后备问的是一个话题不是一条症状，答案归不到某条国标症状上。
    硬归会把"问了个宽泛问题"当成"确认了某条症状"，那是凭空造证据。"""
    monkeypatch.setattr(fu, "question_candidates", lambda *a, **k: [{
        "question": "平时怕冷还是怕热？", "symptom": None, "topic": "寒热",
        "information_gain": None, "source": "shiwen_fallback",
    }])
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: "有点怕冷", max_rounds=1)
    assert r.asserted == [] and r.denied == []
    assert r.history[0].topic == "寒热"
    assert r.history[0].symptom is None


# ---------- 审查修复：问的本身是危重症状、患者只答「有」 ----------

def test_yes_to_a_dangerous_symptom_question_is_a_safety_stop(monkeypatch):
    """「有没有便血？」→「有」。回答原文没有危重词，但被问的症状本身是危重信号。
    审查时实测这条路是通的：safety_relevant 标记算好了却没人消费，「便血」直接
    写进 asserted，S2/S3 照常开方。"""
    monkeypatch.setattr(fu, "question_candidates", lambda *a, **k: [{
        "question": "有没有便血？", "symptom": "便血", "topic": None,
        "information_gain": 0.5, "source": "graph_ig", "safety_relevant": True,
    }])
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: "有")
    assert r.stopped_by == "safety"
    assert "便血" in r.reject_reason
    assert r.asserted == []


def test_no_to_a_dangerous_symptom_question_is_fine(monkeypatch):
    monkeypatch.setattr(fu, "question_candidates", lambda *a, **k: [{
        "question": "有没有便血？", "symptom": "便血", "topic": None,
        "information_gain": 0.5, "source": "graph_ig", "safety_relevant": True,
    }])
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: "没有", max_rounds=1)
    assert r.stopped_by != "safety"
    assert r.denied == ["便血"]


def test_safety_relevance_is_rechecked_even_without_the_flag(monkeypatch):
    """候选里没带 safety_relevant 字段（比如别的调用方拼的候选）也要按症状名再查一次。"""
    monkeypatch.setattr(fu, "question_candidates", lambda *a, **k: [{
        "question": "有没有呕血？", "symptom": "呕血", "topic": None,
        "information_gain": 0.5, "source": "graph_ig",
    }])
    assert run_followup(SYMPTOMS, ELEMENTS, lambda q: "有").stopped_by == "safety"


# ---------- 第二轮复核：带限定语的肯定 / unknown 回答 ----------

@pytest.mark.parametrize("answer", ["有一点，不多", "有，但不严重", "是的，偶尔有", "有点，不太明显"])
def test_qualified_affirmatives_are_yes_not_no(answer):
    """句首肯定、后半句的「不」是程度限定不是否认。归成 no 的话，危重症状会被
    写进 denied，S3 收到「患者明确否认：便血」照常开方。"""
    assert parse_answer(answer) == "yes"


@pytest.mark.parametrize("answer", ["有一点，不多", "时有时无", "拉过两次", "吐了"])
def test_dangerous_question_stops_unless_explicitly_denied(monkeypatch, answer):
    """问的本身是危重症状时，只有明确否认才放行。unknown 也要拦——
    「时有时无」既不是否认也不构成排除，安全侧该是非对称的。"""
    monkeypatch.setattr(fu, "question_candidates", lambda *a, **k: [{
        "question": "有没有便血？", "symptom": "便血", "topic": None,
        "information_gain": 0.5, "source": "graph_ig", "safety_relevant": True,
    }])
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: answer)
    assert r.stopped_by == "safety"
    assert r.asserted == [] and r.denied == []


def test_shiwen_fallback_answer_mentioning_danger_is_caught(monkeypatch):
    """十问歌后备问的是话题（symptom 为 None），只有回答自由文本这一道防线。"""
    monkeypatch.setattr(fu, "question_candidates", lambda *a, **k: [{
        "question": "大小便怎么样？", "symptom": None, "topic": "二便",
        "information_gain": None, "source": "shiwen_fallback",
    }])
    r = run_followup(SYMPTOMS, ELEMENTS, lambda q: "解的是黑的，像柏油一样")
    assert r.stopped_by == "safety"
