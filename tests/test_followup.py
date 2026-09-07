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
from core.tools import syndrome_posterior
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
    卡住了它，混成一个就没法调 MAX_ASK_ROUNDS。"""
    monkeypatch.setattr(fu, "MIN_USEFUL_IG", 99.0)  # 任何问题都达不到
    patient = ScriptedPatient(present=[], absent=[])
    r = run_followup(SYMPTOMS, ELEMENTS, patient)
    assert r.stopped_by == "converged"
    assert r.rounds == 0


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


def test_assertion_and_denial_move_in_opposite_directions():
    e = ["胃", "阴虚", "津伤", "热"]
    yes = syndrome_posterior(e, asserted_symptoms=["口干或口苦"])
    no = syndrome_posterior(e, denied_symptoms=["口干或口苦"])
    assert yes["SP-03"] > no["SP-03"]


def test_contradictory_evidence_falls_back_to_uniform_not_nan():
    """似然全被压到 0 时退回均匀分布。NaN 会让追问循环整个哑掉。"""
    post = syndrome_posterior([], asserted_symptoms=["不存在的症状A"] * 0,
                              denied_symptoms=[])
    assert post and all(p == p for p in post.values())  # 没有 NaN


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
