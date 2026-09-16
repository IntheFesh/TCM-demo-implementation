"""R26 蒸馏脚本（offline/distill_from_v4.py）的离线测试。

**一次 LLM 调用都不发。** 这个脚本的风险全在两件事上：花多少钱、样本选得对不对，
两件都能在沙盒里判。真跑那一步是 ⏳ 上机项。
"""
from __future__ import annotations

import json

import pytest

from core.schemas import CaseRecord, DistillRecord
from offline import distill_from_v4 as dv4


def _case(case_id, physician="ye_tianshi", symptoms=("胃痛", "纳差"), **kw):
    return CaseRecord(case_id=case_id, physician=physician, raw="原文",
                      case_group_id=f"{physician}-1", symptoms=list(symptoms), **kw)


# ---------- 样本选择 ----------


def test_case_complaint_uses_only_what_a_patient_could_say():
    """主诉只能由症状/舌/脉拼——把 syndrome / treatment_principle / formula 放进主诉
    等于把答案塞进问题里，蒸出来的样本全是复读。"""
    case = _case("c1", tongue="淡红", pulse="细弱", syndrome="脾胃气虚",
                 treatment_principle="健脾益气", formula="四君子汤")
    complaint = dv4.case_complaint(case)
    assert "胃痛" in complaint and "舌淡红" in complaint and "脉细弱" in complaint
    for leaked in ("脾胃气虚", "健脾益气", "四君子汤", "原文"):
        assert leaked not in complaint


def test_case_complaint_is_empty_when_there_is_nothing_to_say():
    assert dv4.case_complaint(_case("c1", symptoms=())) == ""


def test_case_samples_skip_empty_complaints_and_are_sorted():
    samples = dv4.case_samples([_case("c2"), _case("c1", symptoms=()), _case("c0")])
    assert [s.sample_id for s in samples] == ["case:c0", "case:c2"]
    assert all(s.source == "case" for s in samples)


def test_case_samples_are_deterministic():
    cases = [_case(f"c{i}") for i in range(20)]
    assert dv4.case_samples(cases) == dv4.case_samples(list(reversed(cases)))


def test_stride_pick_is_deterministic_and_spreads_over_the_list():
    items = list(range(100))
    first = dv4.stride_pick(items, 10)
    assert first == dv4.stride_pick(items, 10)
    assert len(first) == 10 and first[0] == 0 and first[-1] > 80
    assert dv4.stride_pick(items, 0) == []
    assert dv4.stride_pick(items, 500) == items


def test_build_samples_fills_sdt_first_then_cases(monkeypatch):
    """SDT 那一半是教师没见过的病案（真生成），医案那一半是教师抄自己的语料。
    cap 砍人的时候必须砍后者——这条判据钉的就是这个顺序。"""
    fake = [dv4.Sample(sample_id=f"sdt:{i}", source="sdt", complaint="x") for i in range(3)]
    monkeypatch.setattr(dv4, "sdt_samples", lambda _d: fake)
    out = dv4.build_samples(cases=[_case(f"c{i}") for i in range(10)], cap=5)
    assert [s.source for s in out] == ["sdt", "sdt", "sdt", "case", "case"]


def test_build_samples_never_exceeds_the_cap(monkeypatch):
    fake = [dv4.Sample(sample_id=f"sdt:{i}", source="sdt", complaint="x") for i in range(50)]
    monkeypatch.setattr(dv4, "sdt_samples", lambda _d: fake)
    assert len(dv4.build_samples(cases=[], cap=8)) == 8


def test_sdt_samples_returns_empty_when_the_directory_is_missing(tmp_path):
    """SDT 目录没给 / 读不到不是错误：只蒸医案那一半是合法用法。"""
    assert dv4.sdt_samples(None) == []
    assert dv4.sdt_samples(tmp_path / "nope") == []


def test_only_the_train_split_is_ever_read():
    """Validation/Test 是评测集。蒸它们就是把评测数据喂进训练。"""
    src = (dv4.__file__ and open(dv4.__file__, encoding="utf-8").read()) or ""
    assert "SDT_TRAIN_SPLIT" in src
    for forbidden in ('"Validation"', '"Test"', "'Validation'", "'Test'"):
        assert forbidden not in src


# ---------- 成本估算 ----------


def test_the_price_table_is_not_duplicated_here():
    """价格表在 core/usage.py（第 31 条）。这个脚本里出现任何一个价格数字，
    就意味着官方调价之后有一处会继续按旧价估——而按旧价估出来的数正好是
    ¥30 那道闸门要看的数。"""
    src = open(dv4.__file__, encoding="utf-8").read()
    for price in ("1.32", "0.044", "3.96", "7.2"):
        assert price not in src


def test_estimate_scales_with_sample_count():
    prefix = {"ye_tianshi": 100_000, "wu_jutong": 100_000}
    small = dv4.estimate_cost(10, prefix_tokens=prefix, complaint_tokens_mean=100)
    big = dv4.estimate_cost(100, prefix_tokens=prefix, complaint_tokens_mean=100)
    assert big.cny_peak > small.cny_peak
    assert big.calls == 10 * small.calls


def test_off_peak_is_half_of_peak():
    est = dv4.estimate_cost(50, prefix_tokens={"a": 50_000}, complaint_tokens_mean=50)
    assert est.cny_off_peak == pytest.approx(est.cny_peak / 2)


def test_the_warmup_is_paid_once_not_per_sample():
    """前缀缓存的全部意义：第一次未命中、之后命中。如果估算把每次都按未命中算，
    这一跑的预算会高一个量级，而 ¥30 闸门会因此把一次本该放行的跑拦下来。"""
    prefix = {"a": 200_000}
    est = dv4.estimate_cost(100, prefix_tokens=prefix, complaint_tokens_mean=0)
    assert est.warmup_miss_tokens == 200_000
    assert est.hit_tokens > est.warmup_miss_tokens


def test_estimate_uses_the_shared_calls_per_consult():
    from core.usage import calls_per_consult

    est = dv4.estimate_cost(7, prefix_tokens={"a": 1000})
    assert est.calls == 7 * calls_per_consult(est.n_physicians, est.best_of_n)


def test_the_estimate_says_when_it_cannot_know_the_prefix_size():
    est = dv4.estimate_cost(100, prefix_tokens={})
    assert est.prefix_known is False
    text = dv4.format_estimate(est, n_total=100)
    assert "算不出来" in text and "不给一个编的数" in text


# ---------- ¥30 / ¥40 两道闸门 ----------


def _estimate_worth(cny: float) -> dv4.CostEstimate:
    """造一个"按高峰价正好 cny 元"的估算对象，只为测闸门。"""
    est = dv4.estimate_cost(1, prefix_tokens={"a": 1000})
    return dv4.CostEstimate(**{**est.__dict__, "cny_peak": cny, "cny_off_peak": cny / 2})


def test_cheap_runs_go_straight_through():
    ok, why = dv4.gate(_estimate_worth(12.0), yes_spend=False)
    assert ok is True and "直接跑" in why


def test_over_thirty_needs_explicit_confirmation():
    """§0.5 第 2 条第三款：单次真钱动作超 ¥30 要先问一声。"""
    ok, why = dv4.gate(_estimate_worth(33.0), yes_spend=False)
    assert ok is False and "--yes-spend" in why
    ok2, _ = dv4.gate(_estimate_worth(33.0), yes_spend=True)
    assert ok2 is True


def test_over_the_budget_cap_refuses_even_with_confirmation():
    """>¥40 不是"确认一下"能解决的事：R26 的预算上限就是 ¥40。"""
    ok, why = dv4.gate(_estimate_worth(41.0), yes_spend=True)
    assert ok is False and "--limit" in why


def test_an_unknown_prefix_size_blocks_the_run():
    """算不出前缀就算不出钱，那时估算是 0——"0 < 30 所以放行"是最坏的一种通过。"""
    est = dv4.estimate_cost(100, prefix_tokens={})
    ok, why = dv4.gate(est, yes_spend=True)
    assert ok is False and "extract_cases" in why


def test_the_gate_reads_peak_price_not_off_peak():
    """闸门看高峰价。看谷段价等于把"如果我们记得等到谷段"当成保证。"""
    est = _estimate_worth(38.0)          # 谷段 19 元，在 ¥30 以内
    ok, _ = dv4.gate(est, yes_spend=False)
    assert ok is False


# ---------- 断点续跑 ----------


def test_done_keys_reads_sample_id_and_physician_pairs(tmp_path):
    path = tmp_path / "out.jsonl"
    path.write_text(
        json.dumps({"sample_id": "sdt:1", "physician": "ye_tianshi"}, ensure_ascii=False) + "\n"
        + json.dumps({"sample_id": "sdt:1", "physician": "wu_jutong"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    assert dv4.done_keys(path) == {("sdt:1", "ye_tianshi"), ("sdt:1", "wu_jutong")}


def test_done_keys_treats_a_broken_line_as_not_done(tmp_path):
    """坏行跳过但**不算完成**——重跑一条比漏一条好，而且重跑是幂等的。"""
    path = tmp_path / "out.jsonl"
    path.write_text('{"sample_id": "a", "physician": "ye_tianshi"}\n{坏行\n', encoding="utf-8")
    assert dv4.done_keys(path) == {("a", "ye_tianshi")}


def test_done_keys_on_a_missing_file_is_empty(tmp_path):
    assert dv4.done_keys(tmp_path / "nope.jsonl") == set()


# ---------- 记录形状 ----------


class _Herb:
    def __init__(self, name):
        self.name = name


class _Cand:
    def __init__(self):
        self.name = "四君子汤"
        self.rationale = "益气健脾"
        self.herb_items = [_Herb("党参"), _Herb("白术")]


class _S3:
    def __init__(self, cited=("ye-001",), disease="胃痞"):
        self.disease = disease
        self.syndrome = "脾胃气虚"
        self.reasoning = "中气不足"
        self.treatment_principle = "健脾益气"
        self.formula_candidates = [_Cand()]
        self.selected = 0
        self.cited_case_ids = list(cited)


def test_record_carries_the_teacher_and_whether_it_saw_the_source_case():
    """医案那一半的主诉来自教师自己的语料（前缀里就有），SDT 那一半不是。
    **下游必须能分开统计**——教师抄自己语料抄得准，不等于它会推理。"""
    sdt = dv4.Sample(sample_id="sdt:1", source="sdt", complaint="胃痛")
    case = dv4.Sample(sample_id="case:c1", source="case", complaint="胃痛",
                      origin_case_id="c1")
    a = dv4.record_for_physician(sdt, "ye_tianshi", _S3(), teacher_model="deepseek-v4-pro")
    b = dv4.record_for_physician(case, "叶天士", _S3(), teacher_model="deepseek-v4-pro")
    assert a.teacher_saw_source_case is False
    assert b.teacher_saw_source_case is True
    assert b.physician == "ye_tianshi", "中文名要在边界上过 resolve_physician_id"
    assert a.teacher_model == "deepseek-v4-pro"
    assert a.case_refs == ["ye-001"]


def test_step_names_come_from_the_export_sft_table():
    """步骤名只有一处定义（export_sft.CHAIN_STEPS）。另写一套的后果是下游按
    步骤名分组统计时静默多出一类，报出来的覆盖率是错的。"""
    from offline.export_sft import CHAIN_STEPS

    steps = dv4.chain_steps_from_s3(_S3(), cited=["ye-001"])
    assert [s["step"] for s in steps] == ["病名→证型", "证型→治法", "治法→方剂", "方剂→药材"]
    assert all(s["step"] in CHAIN_STEPS for s in steps)


def test_a_case_without_a_disease_name_uses_the_symptom_to_syndrome_step():
    steps = dv4.chain_steps_from_s3(_S3(disease=None), cited=["ye-001"])
    assert steps[0]["step"] == "症状→证型"


def test_the_record_validates_as_a_pydantic_model_with_nonempty_fields():
    """`DistillRecord` 是**新建**的 schema，不是放松 S3Syndrome 的约束。
    空串在训练集里就是"没有出处的样本"，那是蒸馏最容易悄悄引入的脏数据。"""
    with pytest.raises(Exception):
        DistillRecord(sample_id="", source="sdt", physician="ye_tianshi",
                      complaint="胃痛", steps=[], teacher_model="m",
                      teacher_saw_source_case=False)


# ---------- 落盘位置 ----------


def test_the_output_goes_to_a_generated_data_directory_not_data_standard():
    """`data/standard/` 放人工整理的静态参考表，蒸馏产物是脚本生成物。
    判据是"这份文件是人整理的还是脚本生成的"，不是"它是不是 .jsonl"。"""
    assert dv4.OUT_PATH.parts[-2:] == ("sft", "distill_v4.jsonl")
    assert "standard" not in str(dv4.OUT_PATH)


def test_the_output_is_gitignored_on_purpose():
    from pathlib import Path

    ignore = (Path(dv4.ROOT) / ".gitignore").read_text(encoding="utf-8")
    assert "*.jsonl" in ignore
    assert "!data/sft/" not in ignore, "蒸馏产物**不该**开例外：几十 MB、每次重跑都变"


def test_the_sample_cap_matches_the_recipe():
    assert dv4.MAX_SAMPLES == 8000


def test_the_two_cost_lines_are_distinct_numbers():
    """ASK（停下来问一句）和 CAP（这一跑不该发生）不是一回事。"""
    assert dv4.COST_ASK_CNY == 30.0 and dv4.COST_CAP_CNY == 40.0


def test_the_output_token_assumption_is_labelled_as_an_assumption():
    """估算里最不准的一项要自己说出来，并且可以被实测值替掉。"""
    src = open(dv4.__file__, encoding="utf-8").read()
    assert "这是假设不是实测" in src
    assert "--out-tokens-per-call" in src


# ---------- main 的零调用路径 ----------


def test_estimate_only_mode_makes_no_llm_call_and_exits_zero(tmp_path, capsys, monkeypatch):
    """`--estimate` 是这个脚本在沙盒里唯一能真跑的路径。把 get_llm 换成会爆的桩：
    真发生了调用测试会炸。"""
    import core.llm as llm_mod

    def _boom():
        raise AssertionError("--estimate 不该调 LLM")

    monkeypatch.setattr(llm_mod, "get_llm", _boom)
    rc = dv4.main(["--estimate", "--cases", str(tmp_path / "nope.json"),
                   "--out", str(tmp_path / "out.jsonl")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "样本" in out and ("不跑：" in out or "放行：" in out)


def test_a_blocked_run_exits_two_not_zero(tmp_path, monkeypatch, capsys):
    """闸门拦下来要用一个**非 0** 退出码——上机剧本按退出码判这一段过没过，
    退 0 会让"没跑"被当成"跑完了"。"""
    monkeypatch.setattr(dv4, "gate", lambda _e, **_kw: (False, "测试拦住"))
    rc = dv4.main(["--cases", str(tmp_path / "nope.json"), "--out", str(tmp_path / "o.jsonl")])
    assert rc == 2
    assert "测试拦住" in capsys.readouterr().out


# ---------- 预算 → 条数（这一轮真正的发现） ----------


_PREFIX = {"ye_tianshi": 180_000, "wu_jutong": 180_000, "zhang_xichun": 180_000}


def test_max_samples_for_budget_stays_within_the_budget():
    """买得起的那个条数，按高峰价估出来必须真的不超预算。"""
    n = dv4.max_samples_for_budget(40.0, prefix_tokens=_PREFIX, complaint_tokens_mean=120)
    assert n > 0
    assert dv4.estimate_cost(n, prefix_tokens=_PREFIX,
                             complaint_tokens_mean=120).cny_peak <= 40.0
    assert dv4.estimate_cost(n + 1, prefix_tokens=_PREFIX,
                             complaint_tokens_mean=120).cny_peak > 40.0


def test_a_bigger_budget_buys_more_samples():
    small = dv4.max_samples_for_budget(30.0, prefix_tokens=_PREFIX)
    big = dv4.max_samples_for_budget(40.0, prefix_tokens=_PREFIX)
    assert big > small


def test_the_budget_never_buys_more_than_the_recipe_asks_for():
    """预算再多也只要 8000 条——配方就是 8000 条，多蒸的没有用处。"""
    assert dv4.max_samples_for_budget(1e9, prefix_tokens={"a": 10}) == dv4.MAX_SAMPLES


def test_the_recipe_scale_does_not_fit_the_round_budget():
    """R26 的前提（8000 条 ≤ ¥40）在 R21/R22 的架构下**不成立**，差两个量级。

    这条测试不是在测代码，是在钉住一个事实：R21 之后每次 S3 调用都要带
    ~180K token 的知识前缀，R22 之后 S3 在 effort=max 下思考 token 按输出计费，
    于是"一次问诊很便宜"和"八千次问诊很便宜"是两件事。数字变了这条会红，
    那时要改的是报告里的结论，不是这条判据。
    """
    est = dv4.estimate_cost(dv4.MAX_SAMPLES, prefix_tokens=_PREFIX, complaint_tokens_mean=120)
    assert est.cny_peak > 20 * dv4.COST_CAP_CNY


def test_distillation_pins_best_of_n_to_one():
    """产品路径采 3 次挑 1 张；蒸馏的输出本身就是产物，采 3 次就是烧掉 2/3 的钱。"""
    assert dv4.DISTILL_BEST_OF_N == 1
    est = dv4.estimate_cost(10, prefix_tokens=_PREFIX)
    assert est.best_of_n == 1


def test_the_estimate_does_not_follow_the_product_best_of_n(monkeypatch):
    """估算**不问** `core.llm.s3_best_of_n()`：那个函数回答的是产品路径采几次。
    跟着它走的话，把 S3_BEST_OF_N 调到 3 会让蒸馏预算凭空涨三倍，而真跑并不会
    ——估算和真跑各说一套就是这么来的。"""
    monkeypatch.setenv("S3_BEST_OF_N", "3")
    assert dv4.estimate_cost(10, prefix_tokens=_PREFIX).best_of_n == 1


def test_the_run_pins_the_sampling_env_var():
    """真跑那一步也要按死成 1，否则估算按 1 算、实际按 3 花。"""
    src = open(dv4.__file__, encoding="utf-8").read()
    assert 'os.environ["S3_BEST_OF_N"] = str(DISTILL_BEST_OF_N)' in src


def test_the_gate_cap_follows_the_budget_flag():
    """`--budget-cny` 既决定条数，也就是 CAP——两处用同一个数，不然
    "按预算挑了条数" 和 "按另一个上限判该不该跑" 会互相矛盾。"""
    est = dv4.estimate_cost(1, prefix_tokens={"a": 1000})
    cheap = dv4.CostEstimate(**{**est.__dict__, "cny_peak": 25.0, "cny_off_peak": 12.5})
    assert dv4.gate(cheap, yes_spend=True, cap_cny=20.0)[0] is False
    assert dv4.gate(cheap, yes_spend=True, cap_cny=40.0)[0] is True
