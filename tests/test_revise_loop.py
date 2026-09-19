"""R34 闭环：验 → 带本体原文反例重开 → 再验，最多 3 轮；veto 残余不下发。

这个文件测的是**闭环行为**（轮数、关思考、调用数、veto 残余的去处），
规则本身在 `tests/test_formula_verifier.py`。
"""
from __future__ import annotations

import pytest

from core import chain
from core.formula_verifier import MAX_REVISE_ROUNDS
from core.ontology import Ontology
from core.physicians import PHYSICIANS, physicians_for_synthesis
from core.schemas import S3Structured

from tests.test_chain import FakeRetriever
from tests.test_s3_mode import StructuredFakeLLM, _case


def _row(s, p, o):
    return {"s": s, "p": p, "o": o, "book": "中药学", "source": "modern",
            "source_span": f"【{p}】{o}"}


@pytest.fixture
def ont() -> Ontology:
    return Ontology(materia_rows=[
        _row("党参", "性味", "甘，平"), _row("党参", "归经", "归脾、肺经"),
        _row("党参", "功效", "补中益气、健脾益肺"), _row("党参", "用量", "9~30g"),
        _row("白术", "性味", "苦、甘，温"), _row("白术", "归经", "归脾、胃经"),
        _row("白术", "功效", "健脾益气、燥湿利水"), _row("白术", "用量", "6~12g"),
    ], formulary_rows=[], patterns=[])


class ScriptedLLM(StructuredFakeLLM):
    """按脚本逐轮返回不同的方：第 i 次 S3 调用返回 `scripts[i]`（最后一个重复用）。

    另记下每次调用的 `thinking` 参数——闭环那一步必须关思考，而"关了没关"
    只能从传给后端的参数看出来（那正是这条约束唯一可核的地方）。
    """

    def __init__(self, *args, scripts, **kwargs):
        super().__init__(*args, **kwargs)
        self.scripts = scripts
        self.s3_thinking_args: list = []
        self.s3_systems_full: list[str] = []

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        if schema in (S3Structured,):
            i = min(len(self.s3_systems_full), len(self.scripts) - 1)
            self.s3_systems_full.append(system)
            self.s3_thinking_args.append(kwargs.get("thinking"))
            self.calls.append("S3Structured")
            return _mk(**self.scripts[i])
        return super().generate(system, user, schema, temperature=temperature, **kwargs)


def _mk(herbs=("党参", "白术"), roles=None, doses=None, case_ids=("ye_tianshi-001",)):
    roles = roles if roles is not None else ["君"] + ["臣"] * (len(herbs) - 1)
    doses = doses if doses is not None else [9.0] * len(herbs)
    items = [{"name": h, "dose": d, "role": r} for h, d, r in zip(herbs, doses, roles)]
    return S3Structured(
        organs=[{"organ": "脾", "supporting_symptoms": ["纳差"],
                 "pathogenesis": "脾失健运"}],
        syndrome={"name": "脾胃气虚证", "from_organs": ["脾"], "reasoning": "x",
                  "reasoning_plain": "y"},
        method={"principle": "健脾益气", "from_syndrome": "脾胃气虚证",
                "targets": ["脾失健运"]},
        formula={"from_method": "健脾益气", "candidate": {
            "name": "方", "source": "composed", "confidence": "high",
            "rationale": "x", "herb_items": items}},
        herb_choices=[{"item": it, "for_element": "脾", "effect_cited": "补中益气"}
                      for it in items],
        physician_influences=[{"physician": "ye_tianshi", "step": "formula",
                               "contribution": "x", "cited_case_ids": list(case_ids)}],
        cited_case_ids=list(case_ids))


@pytest.fixture
def run(monkeypatch, ont):
    """跑一次 structured consult，S3 按脚本逐轮返回。返回 (out, llm)。"""
    def _run(scripts, **env):
        monkeypatch.setenv("S3_MODE", "structured")
        monkeypatch.setenv("S3_BEST_OF_N", "1")
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        cases = [_case(pid) for pid in physicians_for_synthesis(PHYSICIANS)]
        llm = ScriptedLLM({}, scripts=scripts,
                          case_ids=[c.case_id for c in cases])
        monkeypatch.setattr(chain, "get_llm", lambda: llm)
        monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
        monkeypatch.setattr("core.ontology.get_ontology", lambda: ont)
        monkeypatch.setattr("core.formula_verifier.get_ontology", lambda: ont)
        return chain.consult("胃脘胀痛，纳差乏力", retriever_mode="hybrid"), llm
    return _run


# ---------- 轮数 ----------

def test_a_clean_formula_runs_exactly_one_round(run):
    out, llm = run([dict(herbs=("党参", "白术"))])
    m = out["results"][0]["verifier_metrics"]
    assert m["n_rounds"] == 1 and m["revise_rounds"] == 0
    assert len(llm.s3_systems_full) == 1, "没问题就不该重开"


def test_a_revisable_violation_triggers_exactly_one_reopen_when_the_fix_lands(run):
    """第一轮没有君药 → 重开一次 → 第二轮结构对了 → 停。"""
    out, llm = run([
        dict(herbs=("党参", "白术"), roles=["臣", "臣"]),   # 没有君药
        dict(herbs=("党参", "白术")),                        # 改对了
    ])
    m = out["results"][0]["verifier_metrics"]
    assert m["n_rounds"] == 2 and m["revise_rounds"] == 1
    assert m["verifier_first_pass"] is False
    assert m["first_pass_status"] == "revise_needed"
    assert m["statuses"][1] != "revise_needed"
    assert len(llm.s3_systems_full) == 2


def test_the_loop_stops_at_max_revise_rounds(run):
    """模型一直改不好：跑满 `MAX_REVISE_ROUNDS` 就停，**不无限循环**
    ——`llm_calls` 要可预测（manifest 里那个数是额度结算的依据）。"""
    out, llm = run([dict(herbs=("党参", "白术"), roles=["臣", "臣"])])
    m = out["results"][0]["verifier_metrics"]
    assert m["revise_rounds"] == MAX_REVISE_ROUNDS == 1
    assert m["n_rounds"] == 1 + MAX_REVISE_ROUNDS, "1 次首验 + MAX_REVISE_ROUNDS 次重开后各验一次"
    assert len(llm.s3_systems_full) == 1 + MAX_REVISE_ROUNDS


def test_max_revise_rounds_zero_turns_the_loop_off(run):
    """0 = 关掉闭环。R38 的消融要拿它当对照组（有闭环 vs 没闭环）。"""
    out, llm = run([dict(herbs=("党参", "白术"), roles=["臣", "臣"])],
                   MAX_REVISE_ROUNDS=0)
    m = out["results"][0]["verifier_metrics"]
    assert m["revise_rounds"] == 0 and m["n_rounds"] == 1
    assert len(llm.s3_systems_full) == 1
    assert m["final_status"] == "revise_needed", "问题照实报，只是不重开"


def test_the_env_var_can_raise_the_limit(run):
    out, llm = run([dict(herbs=("党参", "白术"), roles=["臣", "臣"])],
                   MAX_REVISE_ROUNDS=5)
    assert out["results"][0]["verifier_metrics"]["revise_rounds"] == 5


# ---------- 关思考 ----------

def test_the_reopen_turns_thinking_off(run):
    """这一步是照着反例做**局部修补**，不是重新辨一遍证。开思考在这一步是纯浪费
    （§0.4 实测单次 S3 开思考 260–479 秒），而且更容易把已经对的部分想坏。"""
    _out, llm = run([
        dict(herbs=("党参", "白术"), roles=["臣", "臣"]),
        dict(herbs=("党参", "白术")),
    ])
    assert len(llm.s3_thinking_args) == 2
    assert llm.s3_thinking_args[1] == "disabled", "重开那一次必须关思考"
    assert llm.s3_thinking_args[0] != "disabled", "首轮照常开（那才是真的辨证）"


# ---------- 回灌内容 ----------

def test_the_reopen_prompt_carries_the_counterexample(run):
    """回灌给模型的是**能照着改的反例**，不是一句"验证失败"。"""
    _out, llm = run([
        dict(herbs=("党参", "白术"), roles=["臣", "臣"]),
        dict(herbs=("党参", "白术")),
    ])
    second = llm.s3_systems_full[1]
    assert "【符号验证不通过】" in second
    assert "role_structure" in second and "没有君药" in second
    assert "依据：" in second
    assert "君 0 味" in second


def test_the_reopen_prompt_keeps_the_original_system_prompt(run):
    """只**追加**，不换 prompt——五步链与引用要求一条都不许在重开时丢。"""
    _out, llm = run([
        dict(herbs=("党参", "白术"), roles=["臣", "臣"]),
        dict(herbs=("党参", "白术")),
    ])
    first, second = llm.s3_systems_full[0], llm.s3_systems_full[1]
    assert second.startswith(first), "重开的 prompt 必须以原 system 开头"
    assert "五步推理链，一步都不许跳" in second
    assert "五步链、逐字引用上一步的结论" in second, "结尾要重申约束"


def test_the_reopen_prompt_does_not_carry_unverifiable_items(run, ont):
    """本体缺数据时模型改方也改不出数据来——写进去只会让它去改一个本来对的地方。

    这里的"缺数据"场景是"模型没给 `ontology_refs`"（`herb_source_fabricated`
    的 unverifiable 分支）——**R59 之后不能再拿"药不在本体里"当这个例子**：
    那件事现在是 `herb_not_in_ontology`，一条真正会回灌的 revise（见
    `tests/test_formula_verifier.py::
    test_a_herb_absent_from_the_ontology_actually_reaches_the_revise_feedback`，
    这条改动的意义正是让它从"判不了、不说"变成"说了、给机会改"）。"""
    _out, llm = run([
        dict(herbs=("党参", "白术"), roles=["臣", "臣"]),   # 缺君药；两味药都在
                                                          # 本体里，但都没给 ontology_refs
        dict(herbs=("党参", "白术")),
    ])
    second = llm.s3_systems_full[1]
    assert "role_structure" in second
    feedback = second.split("【符号验证不通过】")[1]
    assert "没有可核的引用" not in feedback, "判不了的条目不该出现在回灌段落里"


# ---------- veto 残余不下发 ----------

def test_a_remaining_veto_marks_the_consult_rejected_and_carries_the_draft(run):
    """R65 改了这条的契约：以前推理链在 veto 残余时把草稿直接丢掉
    （`results == []`），于是**任何角色**都只能看到一句"本页不提供方药内容"。
    医师是专业人员，需要的是完整推理链 + 指出哪一味有问题，自己判断要不要
    用——把方药整个抹掉等于把医师当成需要被保护的患者。

    所以草稿现在**一定**要带出推理链，交给 `api/main.py::_consult_response`
    按角色决定给谁看（患者仍然什么方药都不给）。分角色是展示层的事，推理链
    不认识角色——它只负责如实说"这次没通过核查"并把证据带上。
    「不下发」这件事本身改由 `tests/test_veto_presentation.py` 在 API 层钉住。"""
    out, _llm = run([dict(herbs=("甘草", "甘遂"))])
    assert out["rejected"] is True
    # R65：`reject_reason` 改成人话了（`core.veto_text`）。这里断言的是**新契约**
    # ——说清是哪两味药、犯了哪条医理，而**不许**出现规则 id：这句话会经
    # `agent_trace` 发给所有角色，印 id 就是产品面漏 id（那正是这轮修的事）。
    assert "甘草" in out["reject_reason"] and "甘遂" in out["reject_reason"]
    assert "十八反" in out["reject_reason"] or "十九畏" in out["reject_reason"]
    assert "incompatible_pair" not in out["reject_reason"]
    assert out["results"], "草稿要带出来，不能在推理链里就丢掉"
    r = out["results"][0]
    assert r["verification_failed"] is True, "要明确标成没通过核查，不能看起来像正常结果"
    assert r["s3_structured"] is not None, "医师要看的是五步链，不只是结论卡"
    assert r["corroboration"] is None, "没过核查就不给医案佐证，那会抬高它的可信度"


def test_the_veto_response_lists_the_violations_with_counterexamples(run):
    out, _llm = run([dict(herbs=("甘草", "甘遂"))])
    vv = out["verification_veto"]
    assert vv and vv[0]["rule"] == "incompatible_pair"
    assert vv[0]["counterexample"].strip()
    assert set(vv[0]) == {"rule", "herbs", "reason", "counterexample"}


def test_the_veto_does_not_reuse_the_safety_flag(run):
    """`safety_flag` 的语义是"危重症状"，这里的原因是"方不合规"——
    合并之后患者分不出是"你该立刻就医"还是"系统改不出合规的方"。"""
    out, _llm = run([dict(herbs=("甘草", "甘遂"))])
    assert out["safety_flag"] is None
    assert out["verification_veto"], "原因在自己的字段里"


def test_a_veto_fixed_within_the_rounds_is_delivered(run):
    """veto 在轮数内被改掉 → 照常下发。"""
    out, llm = run([
        dict(herbs=("甘草", "甘遂")),      # 配伍禁忌
        dict(herbs=("党参", "白术")),      # 改掉了
    ])
    assert out["rejected"] is False
    assert len(out["results"]) == 1
    m = out["results"][0]["verifier_metrics"]
    assert m["revise_rounds"] == 1 and m["first_pass_status"] == "vetoed"
    assert out["results"][0]["verification"]["n_veto"] == 0


def test_a_revisable_residue_is_still_delivered(run):
    """revise 级残余**照常下发**并如实标出来：那是"拟得不够好"，不是"不能用"。
    压着不发等于因为一条归经覆盖建议就不给患者任何东西。"""
    out, _llm = run([dict(herbs=("党参", "白术"), roles=["臣", "臣"])])
    assert out["rejected"] is False
    r = out["results"][0]
    assert r["verification"]["status"] == "revise_needed"
    assert r["verification"]["n_revise"] >= 1
    assert r["s3"].herbs, "方还在"


# ---------- 调用数与事件 ----------

def test_the_reopens_are_counted_in_llm_calls(run):
    """漏算的话 manifest 报的调用数低于实际花费，拿它算成本就是错的。"""
    clean, _ = run([dict(herbs=("党参", "白术"))])
    n_clean = clean["manifest"]["llm_calls"]
    dirty, llm = run([dict(herbs=("党参", "白术"), roles=["臣", "臣"])])
    n_dirty = dirty["manifest"]["llm_calls"]
    assert n_dirty == n_clean + MAX_REVISE_ROUNDS, (
        f"{n_dirty} 应当比 {n_clean} 多出 {MAX_REVISE_ROUNDS} 次重开"
    )


def test_each_reopen_emits_a_progress_event(monkeypatch, ont):
    """前端要能显示"正在按本体原文重开第 2 轮"——不然那 3 次调用是一段静默的等待。"""
    monkeypatch.setenv("S3_MODE", "structured")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    cases = [_case(pid) for pid in physicians_for_synthesis(PHYSICIANS)]
    llm = ScriptedLLM({}, scripts=[dict(herbs=("党参", "白术"), roles=["臣", "臣"])],
                      case_ids=[c.case_id for c in cases])
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
    monkeypatch.setattr("core.ontology.get_ontology", lambda: ont)
    monkeypatch.setattr("core.formula_verifier.get_ontology", lambda: ont)
    seen: list[tuple[str, dict]] = []
    chain.consult("胃脘胀痛", retriever_mode="hybrid",
                  on_step=lambda n, d: seen.append((n, d)))
    events = [d for n, d in seen if n == "verify_revise"]
    assert len(events) == MAX_REVISE_ROUNDS
    assert [e["round"] for e in events] == list(range(1, MAX_REVISE_ROUNDS + 1))
    assert events[0]["status"] == "revise_needed"
    assert "role_structure" in events[0]["rules"]


# ---------- 只有一条重开路径 ----------

def test_the_safety_reopen_no_longer_runs_in_structured_mode():
    """R34 起 structured 模式只有**一条**重开路径（验证器）。安全层的
    `incompatible` / `dose` 两条判据由验证器委托给同一个 safety_output 去查
    ——两个循环各自决定"要不要重开"的话，同一张方可能被改两遍。"""
    import inspect

    src = inspect.getsource(chain.run_synthesis)
    assert "_verify_and_revise" in src
    assert "format_blocking_issues" not in src, "不该再有安全层那条重开分支"
    assert src.count("get_llm().generate(") == 0, (
        "structured 路径里不该有第二处直接调 generate 的重开——都走 _verify_and_revise"
    )


def test_cand_safety_is_still_filled_for_the_frontend(run):
    """`cand.safety` 仍然要填：前端按它挂红/黄标签，M2 那套字段一个没变。
    填它跟"要不要重开"是两件事。"""
    out, _llm = run([dict(herbs=("党参", "白术"))])
    cand = out["results"][0]["s3"].formula_candidates[0]
    assert cand.safety is not None
    assert out["results"][0]["safety_output"] is not None


def test_revised_flag_reflects_the_loop(run):
    clean, _ = run([dict(herbs=("党参", "白术"))])
    assert clean["results"][0]["safety_output"]["revised"] is False
    dirty, _ = run([dict(herbs=("党参", "白术"), roles=["臣", "臣"]),
                    dict(herbs=("党参", "白术"))])
    assert dirty["results"][0]["safety_output"]["revised"] is True


# ---------- legacy 不受影响 ----------

def test_legacy_mode_has_no_verifier_loop(monkeypatch):
    """§0.6 保留：legacy 一个字没变，也就没有符号验证这一层。"""
    from core.physicians import physicians_enabled
    from core.schemas import S3Syndrome

    from tests.test_chain import FakeLLM

    monkeypatch.setenv("S3_MODE", "legacy")
    monkeypatch.setenv("S3_BEST_OF_N", "1")
    cases = [_case(pid) for pid in physicians_enabled(PHYSICIANS)]
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=[cases[0].case_id])
    llm = FakeLLM({i["name"]: s3 for i in physicians_enabled(PHYSICIANS).values()})
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(cases))
    out = chain.consult("胃脘胀痛", retriever_mode="hybrid")
    assert len(out["results"]) == 3
    for r in out["results"]:
        assert "verification" not in r
        assert "verifier_metrics" not in r
    assert out["manifest"]["synthesis"] is None
