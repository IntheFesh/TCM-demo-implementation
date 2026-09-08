"""EVAL_MODE 旁路的离线测试：跨 core/safety.py、core/chain.py、api/main.py、
eval/sdt/ 四处，放一个文件里，因为它们验的是同一条契约。

红线：**demo 模式（不设 EVAL_MODE、不传参数）必须仍然拦截**。这个文件里
每一组"打开旁路"的用例，都配一条"默认关时照样拦"的对照，不允许只测打开的那半边。
"""
import json

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import chain
from core.safety import safety_bypassed
from core.schemas import FollowupResult, S1Normalize, S3Syndrome
from tests.test_chain import FakeLLM, FakeRetriever, ReActFakeLLM, _fake_cases
# 直接借用 SDT 那边已有的夹具，pytest 会把 import 进来的 fixture 注册到本模块，
# 不重复实现一份假后端/假数据集目录。
from tests.test_sdt_adapter import fake_llm, sdt_dir  # noqa: F401

DANGER_COMPLAINT = "胃脘疼痛数月，近日解黑色柏油样便，头晕心慌，面色苍白，倦怠乏力，舌淡，脉细数。"
DANGER_S1 = S1Normalize(
    symptoms=["胃脘疼痛", "解黑色柏油样便", "头晕心慌"], tongue="淡", pulse="细数", unmapped=[]
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """每条用例都从"没设过 EVAL_MODE"开始——否则用例之间会经由环境变量串味，
    而串味的方向恰好是"安全层被意外关掉"，是最不能出的那种脏测试。"""
    monkeypatch.delenv("EVAL_MODE", raising=False)


@pytest.fixture(autouse=True)
def _pin_two_physicians(monkeypatch):
    """跟 tests/test_chain.py 同一个理由：期望值写死在两位医家上。"""
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})


def _s3(cid):
    return S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                      cited_case_ids=[cid], herbs=["党参", "白术"])


def _setup(monkeypatch, s1=None, llm_cls=FakeLLM):
    fake_llm = llm_cls({"叶天士": _s3("ye_tianshi-001"), "吴鞠通": _s3("wu_jutong-001")}, s1=s1)
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    return fake_llm


# ---------- core/safety.py：唯一的判定实现 ----------


def test_safety_bypassed_defaults_to_off():
    """红线的第一道：什么都不设时不旁路。"""
    assert safety_bypassed() is False
    assert safety_bypassed(None) is False


def test_safety_bypassed_reads_env_var(monkeypatch):
    # 用 monkeypatch 而不是直接写 os.environ：中途一条断言失败的话，直接写的
    # 那个 EVAL_MODE 会漏到后面的用例里，把安全否决静默关掉。
    for truthy in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("EVAL_MODE", truthy)
        assert safety_bypassed() is True, truthy
    for falsy in ("0", "no", "", "off"):
        monkeypatch.setenv("EVAL_MODE", falsy)
        assert safety_bypassed() is False, falsy


def test_explicit_argument_beats_env_var(monkeypatch):
    """显式参数优先是并发安全的关键：评测脚本设了全局 EVAL_MODE 时，
    同进程里显式传 False 的 demo 请求仍然必须拦。"""
    monkeypatch.setenv("EVAL_MODE", "1")
    assert safety_bypassed(False) is False
    assert safety_bypassed(True) is True
    monkeypatch.setenv("EVAL_MODE", "0")
    assert safety_bypassed(True) is True


# ---------- 中止点 1：初始主诉 ----------


def test_demo_mode_still_blocks_initial_complaint(monkeypatch):
    """红线：不设 EVAL_MODE 时，第 10 条测试主诉照样被拦、S2/S3 零调用。"""
    fake_llm = _setup(monkeypatch, s1=DANGER_S1)
    outcome = chain.consult(DANGER_COMPLAINT)

    assert outcome["rejected"] is True
    assert outcome["results"] == []
    assert "柏油样便" in outcome["reject_reason"]
    assert outcome["safety_flag"] is not None and "柏油样便" in outcome["safety_flag"]
    assert fake_llm.calls == ["S1Normalize"], "拦截后 S2/S3 一次都不能调"


def test_eval_mode_lets_the_same_complaint_through_and_records_the_flag(monkeypatch):
    fake_llm = _setup(monkeypatch, s1=DANGER_S1)
    monkeypatch.setenv("EVAL_MODE", "1")
    outcome = chain.consult(DANGER_COMPLAINT)

    assert outcome["rejected"] is False
    assert outcome["results"] != []
    # 检测本身照跑，本该拦截的原因如实记着——不是"跳过检测"
    assert outcome["safety_flag"] is not None and "柏油样便" in outcome["safety_flag"]
    assert "S3Syndrome" in fake_llm.calls


def test_eval_mode_via_explicit_parameter(monkeypatch):
    """不碰环境变量也能开，给单个调用点用（并发安全）。"""
    _setup(monkeypatch, s1=DANGER_S1)
    outcome = chain.consult(DANGER_COMPLAINT, eval_mode=True)
    assert outcome["rejected"] is False and outcome["safety_flag"] is not None


def test_explicit_false_still_blocks_even_with_env_var_on(monkeypatch):
    """红线：全局 EVAL_MODE=1 时，显式 eval_mode=False 的请求仍然被拦。"""
    _setup(monkeypatch, s1=DANGER_S1)
    monkeypatch.setenv("EVAL_MODE", "1")
    outcome = chain.consult(DANGER_COMPLAINT, eval_mode=False)
    assert outcome["rejected"] is True and outcome["results"] == []


def test_clean_complaint_has_none_flag_in_both_modes(monkeypatch):
    """没命中危重症状时两种模式都是 None——safety_flag 的语义是
    "本来会不会被拦"，不是"评测模式开没开"。"""
    _setup(monkeypatch)
    assert chain.consult("纳差乏力")["safety_flag"] is None
    monkeypatch.setenv("EVAL_MODE", "1")
    assert chain.consult("纳差乏力")["safety_flag"] is None


def test_key_sets_identical_across_modes(monkeypatch):
    """两种模式、拦与不拦，返回的键集必须完全一致——api/前端按同一份契约读。"""
    _setup(monkeypatch, s1=DANGER_S1)
    blocked = chain.consult(DANGER_COMPLAINT)
    monkeypatch.setenv("EVAL_MODE", "1")
    passed = chain.consult(DANGER_COMPLAINT)

    _setup(monkeypatch)
    normal = chain.consult("纳差乏力")

    assert set(blocked) == set(passed) == set(normal)
    assert "safety_flag" in set(normal)


# ---------- 中止点 2：G3 追问问出危重症状 ----------


def test_demo_mode_still_blocks_dangerous_followup_answer(monkeypatch):
    fake_llm = _setup(monkeypatch, llm_cls=ReActFakeLLM)
    monkeypatch.delenv("FAST_MODE", raising=False)
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有，昨天开始解黑便")
    assert outcome["rejected"] is True and outcome["results"] == []
    assert outcome["safety_flag"] is not None and "黑便" in outcome["safety_flag"]
    assert fake_llm.calls.count("S3Syndrome") == 0


def test_eval_mode_continues_past_dangerous_followup_answer(monkeypatch):
    """旁路必须覆盖追问这条路——只覆盖初始主诉的话，带患者模拟器的评测
    照样会被拦在半路，"安全否决花了多少分"仍然算不出来。"""
    fake_llm = _setup(monkeypatch, llm_cls=ReActFakeLLM)
    monkeypatch.delenv("FAST_MODE", raising=False)
    monkeypatch.setenv("EVAL_MODE", "1")
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有，昨天开始解黑便")
    assert outcome["rejected"] is False
    assert outcome["results"] != []
    assert outcome["safety_flag"] is not None and "黑便" in outcome["safety_flag"]
    assert fake_llm.calls.count("S3Syndrome") == 2


# ---------- 中止点 3：followup.asserted 的双保险 ----------


def _force_dangerous_asserted(monkeypatch):
    """run_followup 正常情况下会把危重症状挡在 asserted 之外，所以这道双保险
    平时打不着。这里强制构造一个"判据被改坏了"的 followup 结果来验证它。"""
    def fake_run_followup(symptoms, elements, ask_fn, **kw):
        return FollowupResult(asserted=["解黑便"], denied=[], rounds=1,
                              stopped_by="converged", history=[])

    monkeypatch.setattr(chain, "run_followup", fake_run_followup)


def test_demo_mode_still_blocks_dangerous_asserted_symptom(monkeypatch):
    fake_llm = _setup(monkeypatch)
    _force_dangerous_asserted(monkeypatch)
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有")
    assert outcome["rejected"] is True and outcome["results"] == []
    assert "黑便" in outcome["safety_flag"]
    assert fake_llm.calls.count("S3Syndrome") == 0


def test_eval_mode_continues_past_dangerous_asserted_symptom(monkeypatch):
    fake_llm = _setup(monkeypatch)
    _force_dangerous_asserted(monkeypatch)
    monkeypatch.setenv("EVAL_MODE", "1")
    outcome = chain.consult("纳差乏力", ask_fn=lambda q: "有")
    assert outcome["rejected"] is False
    assert "黑便" in outcome["safety_flag"]
    assert fake_llm.calls.count("S3Syndrome") == 2


# ---------- 中止点 4：ReAct 的 ask_user 问出危重症状 ----------


def _asking_react_setup(monkeypatch):
    from core.schemas import ReActStep
    import core.react as react_mod

    class AskingLLM(ReActFakeLLM):
        def generate(self, system, user, schema, temperature=0.0, **kwargs):
            if schema is ReActStep:
                self.calls.append("ReActStep")
                return ReActStep(thought="分不开", action="ask_user",
                                 action_input={"question": "有没有口苦？", "reason": "r"})
            return super().generate(system, user, schema, temperature, **kwargs)

    fake_llm = AskingLLM({"叶天士": _s3("ye_tianshi-001"), "吴鞠通": _s3("wu_jutong-001")})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(react_mod, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.setenv("FAST_MODE", "1")  # 关掉 G3 追问，只走 ReAct 这条追问路径
    return fake_llm


def test_demo_mode_still_raises_safety_veto_from_react_ask_user(monkeypatch):
    fake_llm = _asking_react_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", use_react=True, ask_fn=lambda q: "有，而且解了黑便")
    assert outcome["rejected"] is True and outcome["results"] == []
    assert "黑便" in outcome["safety_flag"]
    assert fake_llm.s3_systems == [], "被拦截后 S3 一次都不能调"


def test_eval_mode_continues_past_react_ask_user_and_reports_flag(monkeypatch):
    """这条路的 flag 是经由 run_physician 的返回值带回来的（异常没抛，
    走不了 except 分支），单独验一遍它没有在半路掉了。"""
    _asking_react_setup(monkeypatch)
    monkeypatch.setenv("EVAL_MODE", "1")
    outcome = chain.consult("纳差乏力", use_react=True, ask_fn=lambda q: "有，而且解了黑便")
    assert outcome["rejected"] is False
    assert outcome["results"] != []
    assert outcome["safety_flag"] is not None and "黑便" in outcome["safety_flag"]
    assert any(r["safety_flag"] for r in outcome["results"])


def test_run_physician_safety_flag_is_none_on_the_normal_path(monkeypatch):
    _asking_react_setup(monkeypatch)
    outcome = chain.consult("纳差乏力", use_react=True, ask_fn=lambda q: "没有口苦")
    assert outcome["rejected"] is False
    assert all(r["safety_flag"] is None for r in outcome["results"])
    assert outcome["safety_flag"] is None


# ---------- api/main.py 透传 ----------


def test_api_passes_safety_flag_through_on_rejection(monkeypatch):
    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: {
        "s1": S1Normalize(symptoms=["解黑便"]), "results": [], "divergence": None,
        "rejected": True, "reject_reason": "危重", "safety_flag": "危重",
        "followup": None, "manifest": {"llm_calls": 1},
    })
    body = TestClient(api_main.app).post("/api/consult", json={"complaint": "x"}).json()
    assert body["safety_flag"] == "危重"


def test_api_passes_safety_flag_through_on_insufficient(monkeypatch):
    from core.schemas import S2Elements

    monkeypatch.setattr(api_main, "consult", lambda complaint, **kw: {
        "s1": S1Normalize(symptoms=["胸闷"]), "results": [], "divergence": None,
        "rejected": False, "reject_reason": None, "safety_flag": None,
        "s2": S2Elements(), "residual": None, "followup": None,
        "insufficient": True, "insufficient_reason": "信息不足", "coverage": 0.0,
        "manifest": {"llm_calls": 2},
    })
    body = TestClient(api_main.app).post("/api/consult", json={"complaint": "胸闷"}).json()
    assert "safety_flag" in body and body["safety_flag"] is None


def test_api_passes_safety_flag_through_on_normal_path(monkeypatch):
    _setup(monkeypatch, s1=DANGER_S1)
    monkeypatch.setenv("EVAL_MODE", "1")
    body = TestClient(api_main.app).post("/api/consult", json={"complaint": DANGER_COMPLAINT}).json()
    assert body["rejected"] is False
    assert body["safety_flag"] is not None and "柏油样便" in body["safety_flag"]


# ---------- eval/sdt/：同一个判定函数，参数式接口保留 ----------


def test_sdt_solver_still_blocks_by_default():
    """红线：SDT 那条链路默认也拦。"""
    from tests.test_sdt_adapter import REC_DANGER, _record
    from eval.sdt import adapter

    ans = adapter.BaselineSolver().solve(_record(REC_DANGER))
    assert ans.safety_rejected is not None
    assert ans.llm_calls == 0


def test_sdt_solver_bypasses_via_env_var(monkeypatch, fake_llm):
    """默认值从 False 改成 None 之后，EVAL_MODE 对 SDT 才真的生效
    ——留 False 的话环境变量永远被压掉。"""
    from tests.test_sdt_adapter import REC_DANGER, _record
    from eval.sdt import adapter

    monkeypatch.setenv("EVAL_MODE", "1")
    ans = adapter.BaselineSolver().solve(_record(REC_DANGER))
    assert ans.safety_rejected is None
    assert ans.llm_calls == 3


def test_sdt_explicit_flag_beats_env_var(monkeypatch, fake_llm):
    from tests.test_sdt_adapter import REC_DANGER, _record
    from eval.sdt import adapter

    monkeypatch.setenv("EVAL_MODE", "1")
    ans = adapter.BaselineSolver().solve(_record(REC_DANGER), ignore_safety_veto=False)
    assert ans.safety_rejected is not None, "显式 False 必须压过环境变量"


def test_sdt_run_cli_records_effective_bypass_value_in_manifest(
    sdt_dir, tmp_path, monkeypatch, fake_llm
):
    """manifest 是"这个数字怎么来的"的唯一凭据：EVAL_MODE 生效而没传 flag 时，
    照抄 args（False）进 manifest 就等于让它撒谎。"""
    from eval.sdt import run as run_mod

    out = tmp_path / "sub.txt"
    monkeypatch.setenv("EVAL_MODE", "1")
    run_mod.main(["--sdt-dir", str(sdt_dir), "--split", "Validation",
                  "--solver", "baseline", "--out", str(out)])
    manifest = json.loads(out.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["ignore_safety_veto"] is True
    assert manifest["ignore_safety_veto_source"] == "EVAL_MODE"
    assert manifest["safety_rejected"] == [], "旁路生效时不该有记录被拦"


def test_sdt_run_cli_flag_source_recorded(sdt_dir, tmp_path, fake_llm):
    from eval.sdt import run as run_mod

    out = tmp_path / "sub.txt"
    run_mod.main(["--sdt-dir", str(sdt_dir), "--split", "Validation",
                  "--solver", "baseline", "--out", str(out), "--ignore-safety-veto"])
    manifest = json.loads(out.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["ignore_safety_veto"] is True
    assert manifest["ignore_safety_veto_source"] == "--ignore-safety-veto"


def test_sdt_run_cli_default_records_no_bypass(sdt_dir, tmp_path, fake_llm):
    from eval.sdt import run as run_mod

    out = tmp_path / "sub.txt"
    run_mod.main(["--sdt-dir", str(sdt_dir), "--split", "Validation",
                  "--solver", "baseline", "--out", str(out)])
    manifest = json.loads(out.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["ignore_safety_veto"] is False
    assert manifest["ignore_safety_veto_source"] is None
    assert manifest["safety_rejected"], "默认模式下危重记录必须被拦下并记名"
