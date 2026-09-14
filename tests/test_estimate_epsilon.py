"""offline/estimate_epsilon.py 的离线测试。不调用真实 LLM——consult_fn 直接注入
（estimate_epsilon_online 的设计就是为了让调用方能这么做），S2/S0 路径用
monkeypatch 换掉。
"""
import json


from core.schemas import (
    ElementHit, S1Normalize, S2Elements, S3Syndrome, SegmentPatients,
    CaseSequence, VisitStructured,
)
from offline import estimate_epsilon as ee

QUERIES = ["主诉甲", "主诉乙", "主诉丙"]


def _s3(herbs):
    return S3Syndrome(syndrome="x", reasoning="x", treatment_principle="x",
                      herbs=herbs, cited_case_ids=["a"])


def _outcome(herbs_ye, herbs_wu, llm_calls=4):
    return {
        "rejected": False, "insufficient": False,
        "results": [
            {"physician": "ye_tianshi", "s3": _s3(herbs_ye)},
            {"physician": "wu_jutong", "s3": _s3(herbs_wu)},
        ],
        "manifest": {"llm_calls": llm_calls},
    }


# ---------- estimate_epsilon_online ----------

def test_epsilon_online_identical_repeats_give_zero_noise():
    def consult_fn(_):
        return _outcome(["党参", "白术"], ["茯苓"])

    r = ee.estimate_epsilon_online(QUERIES, n_repeats=3, consult_fn=consult_fn)
    assert r["mean"] == 0.0
    assert r["n_queries_used"] == len(QUERIES)
    assert r["by_physician"]["ye_tianshi"]["mean"] == 0.0


def test_epsilon_online_detects_real_variation():
    calls = {"n": 0}

    def consult_fn(_):
        calls["n"] += 1
        # 每次的用药不同，制造真实抖动
        return _outcome([f"药{calls['n']}"], ["茯苓"])

    r = ee.estimate_epsilon_online(["单条主诉"], n_repeats=3, consult_fn=consult_fn)
    assert r["by_physician"]["ye_tianshi"]["mean"] == 1.0  # 完全不重叠
    assert r["by_physician"]["wu_jutong"]["mean"] == 0.0   # 吴鞠通每次都一样


def test_epsilon_online_skips_query_rejected_every_repeat():
    def consult_fn(_):
        return {"rejected": True, "manifest": {"llm_calls": 1}}

    r = ee.estimate_epsilon_online(["危重主诉"], n_repeats=2, consult_fn=consult_fn)
    assert r["n_queries_used"] == 0
    assert r["per_query"][0]["skipped"] is True
    assert "拦截" in r["per_query"][0]["reason"]
    assert r["mean"] is None


def test_epsilon_online_skips_query_insufficient_every_repeat():
    def consult_fn(_):
        return {"rejected": False, "insufficient": True, "manifest": {"llm_calls": 2}}

    r = ee.estimate_epsilon_online(["信息不足主诉"], n_repeats=2, consult_fn=consult_fn)
    assert r["n_queries_used"] == 0
    assert "信息不足" in r["per_query"][0]["reason"]


def test_epsilon_online_partial_rejection_still_uses_the_rest():
    """部分重复被拦截、部分正常时，仍用正常的那些估计噪声，不整条跳过。"""
    responses = iter([
        {"rejected": True, "manifest": {"llm_calls": 1}},
        _outcome(["党参"], ["茯苓"]),
        _outcome(["党参"], ["茯苓"]),
    ])

    def consult_fn(_):
        return next(responses)

    r = ee.estimate_epsilon_online(["混合主诉"], n_repeats=3, consult_fn=consult_fn)
    assert r["n_queries_used"] == 1
    q = r["per_query"][0]
    assert q["n_rejected"] == 1
    assert q["by_physician"]["ye_tianshi"]["n_pairs"] == 1  # 只有 2 次可用 -> 1 对


def test_epsilon_online_llm_calls_summed():
    def consult_fn(_):
        return _outcome(["a"], ["b"], llm_calls=5)

    r = ee.estimate_epsilon_online(["q"], n_repeats=3, consult_fn=consult_fn)
    assert r["llm_calls"] == 15


# ---------- 失败容忍：一次 consult_fn 调用崩不能让整批噪声估算停下 ----------
#
# 这一层原来没有任何异常捕获——真实 API 抖动一次就会让 estimate_epsilon_online
# 整个崩掉，10 条主诉 × 3 次重复 ≈ 150 次调用，崩一次代价很大。跟
# eval/run_eval.py 的三个收集器、core.chain.consult_many 是同一类坑。


def test_epsilon_online_survives_call_failure_and_excludes_it_from_denominator():
    """一次重复调用失败：不崩，失败次数记进 n_call_failures，且不参与
    Jaccard 计算的分母（herb_sets_by_physician 里少了这一次，pairwise_
    jaccard_stats 用剩下的有效次数算，不是拿 n_repeats 硬除）。"""
    responses = iter([
        RuntimeError("模拟 API 抖动"),
        _outcome(["党参"], ["茯苓"]),
        _outcome(["党参"], ["茯苓"]),
    ])

    def consult_fn(_):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    r = ee.estimate_epsilon_online(["主诉甲"], n_repeats=3, consult_fn=consult_fn)
    assert r["n_call_failures"] == 1
    assert r["n_attempts"] == 3
    q = r["per_query"][0]
    assert q["skipped"] is False
    assert q["n_call_failures"] == 1
    assert q["n_completed"] == 2  # 3 次重复只成功 2 次，不是当成完整的 3 次
    # 两次成功的重复用药完全一致 -> 噪声为 0，证明确实是拿"完成的 2 次"在算，
    # 不是把失败那次悄悄当成了一次抖动样本混进去。
    assert q["by_physician"]["ye_tianshi"]["mean"] == 0.0
    assert q["by_physician"]["ye_tianshi"]["n_pairs"] == 1  # 2 个有效集合 -> 1 对


def test_epsilon_online_all_repeats_failing_is_skipped_not_silently_dropped():
    """一条主诉的全部重复都调用失败：跟"全部被安全否决"一样单独标记跳过，
    不能让它悄悄从统计里消失、也不能跟"被拒答"混成一个原因。"""
    def consult_fn(_):
        raise RuntimeError("整条主诉全炸")

    r = ee.estimate_epsilon_online(["主诉甲", "主诉乙"], n_repeats=2, consult_fn=consult_fn)
    assert r["n_queries_used"] == 0
    assert r["n_call_failures"] == 4  # 2 条主诉 × 2 次重复全部失败
    assert r["n_attempts"] == 4
    for q in r["per_query"]:
        assert q["skipped"] is True
        assert q["reason"] == "全部重复调用失败"
        assert q["n_call_failures"] == 2


def test_epsilon_online_does_not_crash_the_whole_batch_on_a_single_failure(capsys):
    """跟一次 LLMError 崩掉 offline/extract_case_triples.py 之前的坑同一类：
    4 条主诉里第 3 次调用（整体第几次调用，不分主诉/重复）失败，其余主诉/
    重复要正常产出，不能整批跟着崩。"""
    calls = {"n": 0}

    def consult_fn(_):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟第 3 次调用失败")
        return _outcome(["党参"], ["茯苓"])

    queries = ["主诉一", "主诉二", "主诉三", "主诉四"]
    r = ee.estimate_epsilon_online(queries, n_repeats=1, consult_fn=consult_fn)
    assert len(r["per_query"]) == 4  # 不崩：4 条主诉都产出了记录
    assert r["n_call_failures"] == 1
    assert "调用失败" in capsys.readouterr().err


# ---------- estimate_epsilon_s2 ----------

def test_epsilon_s2_reuses_the_same_s1(monkeypatch):
    """S1 只跑一次——CLAUDE.md 的硬约束。这里验证 normalize 只被调一次。"""
    calls = {"normalize": 0, "infer": 0}

    def fake_normalize(complaint):
        calls["normalize"] += 1
        return S1Normalize(symptoms=["纳差"], tongue="淡", pulse="细")

    def fake_infer(s1):
        calls["infer"] += 1
        return S2Elements(elements=[ElementHit(
            element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")])

    monkeypatch.setattr(ee, "normalize", fake_normalize)
    monkeypatch.setattr(ee, "infer_elements", fake_infer)
    monkeypatch.setattr(ee, "check_safety", lambda symptoms: None)

    r = ee.estimate_epsilon_s2(["单条"], n_repeats=3)
    assert calls["normalize"] == 1
    assert calls["infer"] == 3
    assert r["mean"] == 0.0  # 每次都返回同一个证素


def test_epsilon_s2_skips_rejected_query(monkeypatch):
    monkeypatch.setattr(ee, "normalize", lambda c: S1Normalize(symptoms=["黑便"]))
    monkeypatch.setattr(ee, "infer_elements", lambda s1: S2Elements())
    monkeypatch.setattr(ee, "check_safety", lambda symptoms: "危重")

    r = ee.estimate_epsilon_s2(["危重主诉"], n_repeats=2)
    assert r["n_queries_used"] == 0
    assert r["per_query"][0]["skipped"] is True


def test_epsilon_s2_detects_variation(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(ee, "normalize", lambda c: S1Normalize(symptoms=["纳差"]))
    monkeypatch.setattr(ee, "check_safety", lambda symptoms: None)

    def fake_infer(s1):
        calls["n"] += 1
        elem = "脾" if calls["n"] % 2 else "胃"
        return S2Elements(elements=[ElementHit(
            element=elem, kind="location", supporting_symptoms=["纳差"], confidence="high")])

    monkeypatch.setattr(ee, "infer_elements", fake_infer)
    r = ee.estimate_epsilon_s2(["单条"], n_repeats=4)
    assert r["mean"] > 0.0


def test_epsilon_s2_survives_s1_failure_without_crashing_other_queries(monkeypatch):
    """normalize()（S1）失败：这条主诉没法做 S2 重复实验，单独跳过、原因
    标"S1 调用失败"，不跟 check_safety 拦截混在一起；其它主诉不受影响。"""
    def flaky_normalize(complaint):
        if complaint == "主诉甲":
            raise RuntimeError("S1 调用炸了")
        return S1Normalize(symptoms=["纳差"])

    monkeypatch.setattr(ee, "normalize", flaky_normalize)
    monkeypatch.setattr(ee, "check_safety", lambda symptoms: None)
    monkeypatch.setattr(ee, "infer_elements", lambda s1: S2Elements(elements=[ElementHit(
        element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")]))

    r = ee.estimate_epsilon_s2(["主诉甲", "主诉乙"], n_repeats=2)
    by_query = {q["query"]: q for q in r["per_query"]}
    assert by_query["主诉甲"]["skipped"] is True
    assert by_query["主诉甲"]["reason"] == "S1 调用失败"
    assert by_query["主诉乙"]["skipped"] is False
    assert r["n_call_failures"] == 1


def test_epsilon_s2_survives_single_infer_elements_failure_and_excludes_it(monkeypatch):
    """infer_elements()（S2）单次重复失败：不影响已经算出来的 S1、不影响
    这条主诉的其它重复，失败的这一次不进 elem_sets，分母是真正跑成的次数
    （n_completed），不是 n_repeats。"""
    monkeypatch.setattr(ee, "normalize", lambda c: S1Normalize(symptoms=["纳差"]))
    monkeypatch.setattr(ee, "check_safety", lambda symptoms: None)

    responses = iter([
        RuntimeError("模拟 S2 抖动"),
        S2Elements(elements=[ElementHit(
            element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")]),
        S2Elements(elements=[ElementHit(
            element="脾", kind="location", supporting_symptoms=["纳差"], confidence="high")]),
    ])

    def flaky_infer(s1):
        r = next(responses)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(ee, "infer_elements", flaky_infer)
    r = ee.estimate_epsilon_s2(["单条"], n_repeats=3)
    q = r["per_query"][0]
    assert q["skipped"] is False
    assert q["n_call_failures"] == 1
    assert q["n_completed"] == 2
    assert q["stats"]["n_pairs"] == 1  # 2 个有效集合 -> 1 对，不是 3 次的组合数
    assert r["n_call_failures"] == 1


def test_epsilon_s2_all_repeats_failing_is_skipped_not_silently_dropped(monkeypatch):
    monkeypatch.setattr(ee, "normalize", lambda c: S1Normalize(symptoms=["纳差"]))
    monkeypatch.setattr(ee, "check_safety", lambda symptoms: None)
    monkeypatch.setattr(ee, "infer_elements",
                        lambda s1: (_ for _ in ()).throw(RuntimeError("S2 全炸")))

    r = ee.estimate_epsilon_s2(["单条"], n_repeats=3)
    q = r["per_query"][0]
    assert q["skipped"] is True
    assert q["reason"] == "全部重复调用失败"
    assert q["n_call_failures"] == 3


# ---------- estimate_epsilon_extract ----------

def test_epsilon_extract_unavailable_when_no_cases_json(tmp_path):
    r = ee.estimate_epsilon_extract(cases_path=tmp_path / "cases.json", n_repeats=2)
    assert r["available"] is False
    assert "不存在" in r["note"]


def test_epsilon_extract_unavailable_when_no_eligible_records(tmp_path):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([{"case_id": "x", "visit_index": 1, "raw_excerpt": "有"}]), encoding="utf-8")
    r = ee.estimate_epsilon_extract(cases_path=p, n_repeats=2)
    assert r["available"] is False
    assert "没有带 raw_excerpt" in r["note"]


def test_epsilon_extract_reuses_extract_segment(tmp_path, monkeypatch):
    """必须走 offline.extract_cases.extract_segment 这条路径，不是另写一套。"""
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([
        {"case_id": "ye_tianshi-0001-p0-0", "visit_index": 0, "raw_excerpt": "脘痛不食。"},
    ]), encoding="utf-8")

    seen_segments = []

    def fake_extract_segment(segment):
        seen_segments.append(segment)
        return SegmentPatients(patients=[CaseSequence(visits=[
            VisitStructured(symptoms=["脘痛", "不食"]),
        ])])

    monkeypatch.setattr("offline.extract_cases.extract_segment", fake_extract_segment)
    r = ee.estimate_epsilon_extract(cases_path=p, n_repeats=2, n_samples=5)
    assert r["available"] is True
    assert r["mean"] == 0.0
    assert seen_segments[0]["text"] == "脘痛不食。"
    assert seen_segments[0]["head_hints"] == [] and seen_segments[0]["follow_hints"] == []


def test_epsilon_extract_survives_single_extraction_failure(tmp_path, monkeypatch):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([
        {"case_id": "c1", "visit_index": 0, "raw_excerpt": "原文"},
    ]), encoding="utf-8")

    calls = {"n": 0}

    def flaky(segment):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("LLM 调用失败")
        return SegmentPatients(patients=[CaseSequence(visits=[VisitStructured(symptoms=["纳差"])])])

    monkeypatch.setattr("offline.extract_cases.extract_segment", flaky)
    r = ee.estimate_epsilon_extract(cases_path=p, n_repeats=3, n_samples=5)
    assert r["available"] is True
    assert r["n_extraction_failures"] == 1


def test_epsilon_extract_sampling_is_reproducible(tmp_path, monkeypatch):
    p = tmp_path / "cases.json"
    cases = [
        {"case_id": f"c{i}", "visit_index": 0, "raw_excerpt": f"原文{i}"} for i in range(20)
    ]
    p.write_text(json.dumps(cases), encoding="utf-8")
    monkeypatch.setattr("offline.extract_cases.extract_segment",
                        lambda seg: SegmentPatients(patients=[]))
    r1 = ee.estimate_epsilon_extract(cases_path=p, n_repeats=1, n_samples=5)
    r2 = ee.estimate_epsilon_extract(cases_path=p, n_repeats=1, n_samples=5)
    ids1 = [c["case_id"] for c in r1["per_case"]]
    ids2 = [c["case_id"] for c in r2["per_case"]]
    assert ids1 == ids2


# ---------- CLI ----------

def test_dry_run_does_not_call_consult(tmp_path, monkeypatch, capsys):
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n主诉二\n", encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("--dry-run 不该真的调用")

    monkeypatch.setattr(ee, "estimate_epsilon_online", boom)
    monkeypatch.setattr(ee, "estimate_epsilon_s2", boom)
    monkeypatch.setattr(ee, "estimate_epsilon_extract", boom)
    ee.main(["--queries-path", str(queries_path), "--dry-run",
             "--cases-path", str(tmp_path / "nope.json")])
    out = capsys.readouterr().out
    assert "预估调用数" in out
    assert "--dry-run" in out


def test_main_writes_epsilon_json(tmp_path, monkeypatch):
    """monkeypatch 的假返回字典要带上 n_call_failures/n_attempts——这两个
    字段是失败容忍机制加的（main() 现在无条件读取它们去判断要不要打失败率
    警告），不是原来就有的可选字段，缺了会 KeyError（真实实现里
    estimate_epsilon_online/estimate_epsilon_s2 现在总是带这两个键）。

    R1 起还要带 layers（君臣/佐使两层的结果对象）：main() 现在无条件把它摘出来
    平铺成 epsilon.json 顶层的 epsilon_core/epsilon_adjunct。这里刻意用
    `online.pop("layers")` 的严格取法、不给默认值——假返回值必须跟着真实实现的
    形状走（跟 tests/test_chain.py::FakeLLM 要跟着后端接口补方法是同一条约定），
    给默认值会让"实现忘了产出分层"这种问题在测试里静默通过。"""
    queries_path = tmp_path / "queries.txt"
    queries_path.write_text("主诉一\n", encoding="utf-8")
    out_path = tmp_path / "epsilon.json"

    def _layer(mean: float) -> dict:
        return {"mean": mean, "p50": mean, "p95": mean, "by_physician": {}, "per_query": [],
                "n_queries": 1, "n_queries_used": 1, "n_repeats": 2, "llm_calls": 4,
                "n_call_failures": 0, "n_attempts": 2,
                "mean_herbs_per_formula": 5.0, "n_formulas_counted": 2,
                "role_fill_rate": 0.95, "n_herb_items": 20, "n_unroled": 1}

    monkeypatch.setattr(ee, "estimate_epsilon_online", lambda q, n_repeats: {
        "mean": 0.1, "p50": 0.1, "p95": 0.1, "by_physician": {}, "per_query": [],
        "n_queries": 1, "n_queries_used": 1, "n_repeats": n_repeats, "llm_calls": 4,
        "n_call_failures": 0, "n_attempts": len(q) * n_repeats,
        "mean_herbs_per_formula": 8.0, "n_formulas_counted": 2,
        "layers": {"core": _layer(0.05), "adjunct": _layer(0.3)}})
    monkeypatch.setattr(ee, "estimate_epsilon_s2", lambda q, n_repeats: {
        "mean": 0.05, "p50": 0.05, "p95": 0.05, "per_query": [],
        "n_queries": 1, "n_queries_used": 1, "n_repeats": n_repeats, "llm_calls": 4,
        "n_call_failures": 0})

    ee.main(["--queries-path", str(queries_path), "--out", str(out_path),
             "--cases-path", str(tmp_path / "nope.json"), "--n-repeats", "2"])

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["epsilon_online"]["mean"] == 0.1
    # 分层跟 epsilon_online 平级、不嵌在它里面，且 online 里不再留一份 layers
    assert data["epsilon_core"]["mean"] == 0.05
    assert data["epsilon_adjunct"]["mean"] == 0.3
    assert "layers" not in data["epsilon_online"]
    assert data["epsilon_s2"]["mean"] == 0.05
    assert data["epsilon_extract"]["available"] is False
    assert "model" in data and "backend" in data and "generated_at" in data


# ---------- R1：分层 ε（君臣 / 佐使）----------
#
# 这一段的核心陷阱是空集：分层的空集含义是"这张方的药没标 role"，跟整方层的
# 空集（"这位医家真的没开方"）相反。两个空集算 Jaccard 距离是 0.0（
# core/setstats.py 把"双方都没提到任何东西"定义为一致），照整方那套算下去
# epsilon_core 会被一堆未标注的方压成 0——一个凭空好看的假数字。

def _s3_roled(specs):
    """specs: (药名, role)。role 传 None 就是没标注。"""
    from core.schemas import FormulaCandidate, HerbItem

    return S3Syndrome(
        syndrome="x", reasoning="x", treatment_principle="x", cited_case_ids=["a"],
        formula_candidates=[FormulaCandidate(
            name="某方", source="composed", confidence="high", rationale="x",
            herb_items=[HerbItem(name=n, role=r) for n, r in specs],
        )],
    )


def _roled_consult(runs: list[list[tuple[str, str | None]]]):
    """每次重复返回 runs 里的下一张方（单医家）。"""
    it = iter(runs)

    def consult_fn(complaint):
        return {"rejected": False, "insufficient": False, "manifest": {"llm_calls": 3},
                "results": [{"physician": "ye_tianshi", "s3": _s3_roled(next(it))}]}
    return consult_fn


def test_epsilon_layers_separate_stable_core_from_varying_adjunct():
    """实测形状：君臣骨架三次全在、佐使三次全不同。分层必须把这件事分开——
    epsilon_core=0、epsilon_adjunct=1，整方那个数落在两者之间。"""
    runs = [
        [("半夏", "君"), ("茯苓", "臣"), ("神曲", "佐")],
        [("半夏", "君"), ("茯苓", "臣"), ("黄连", "佐")],
        [("半夏", "君"), ("茯苓", "臣"), ("桑叶", "佐")],
    ]
    r = ee.estimate_epsilon_online(["主诉甲"], n_repeats=3, consult_fn=_roled_consult(runs))
    layers = r.pop("layers")
    assert layers["core"]["mean"] == 0.0
    assert layers["adjunct"]["mean"] == 1.0
    assert layers["core"]["mean"] < r["mean"] < layers["adjunct"]["mean"]
    # 平均药味数按归一去重后的集合大小算：整方 3、君臣 2、佐使 1
    assert r["mean_herbs_per_formula"] == 3.0
    assert layers["core"]["mean_herbs_per_formula"] == 2.0
    assert layers["adjunct"]["mean_herbs_per_formula"] == 1.0


def test_epsilon_layer_is_none_not_zero_when_nothing_is_roled():
    """**这条是分层最容易骗人的地方**：全部没标 role 时两层都是空集，如果照整方
    那套算下去会得出 mean=0.0（"完全一致"）。必须是 None（没数据）。"""
    runs = [[("半夏", None), ("茯苓", None)], [("黄连", None), ("桑叶", None)]]
    r = ee.estimate_epsilon_online(["主诉甲"], n_repeats=2, consult_fn=_roled_consult(runs))
    layers = r.pop("layers")
    assert layers["core"]["mean"] is None
    assert layers["adjunct"]["mean"] is None
    assert layers["core"]["role_fill_rate"] == 0.0
    assert layers["core"]["n_unroled"] == 4 and layers["core"]["n_herb_items"] == 4
    # 整方层照旧有数：没标 role 不影响它
    assert r["mean"] == 1.0


def test_epsilon_layer_skips_only_the_unroled_repeats():
    """一次重复标了 role、另两次没标：分层只拿标了的那些算，标了的只剩一次
    就出不了数（少于两次重复没有"两两"可言）——不能把没标的那两次当成空集
    参与进来凑出一个 0。"""
    runs = [
        [("半夏", "君"), ("神曲", "佐")],
        [("半夏", None), ("神曲", None)],
        [("半夏", None), ("神曲", None)],
    ]
    r = ee.estimate_epsilon_online(["主诉甲"], n_repeats=3, consult_fn=_roled_consult(runs))
    layers = r.pop("layers")
    assert layers["core"]["mean"] is None      # 只有 1 次有效，估不出噪声
    assert layers["core"]["n_formulas_counted"] == 1
    assert layers["core"]["role_fill_rate"] == round(2 / 6, 4)


def test_epsilon_layers_record_the_same_skip_reason_as_the_online_layer():
    """整条主诉被拦截/信息不足时，三层记的是同一条跳过原因——分层不是另一次
    采样，它跟整方层看的是同一批 consult() 结果。"""
    def rejected(complaint):
        return {"rejected": True, "insufficient": False, "manifest": {"llm_calls": 1},
                "results": []}

    r = ee.estimate_epsilon_online(["危重主诉"], n_repeats=2, consult_fn=rejected)
    layers = r.pop("layers")
    for record in (r["per_query"][0], layers["core"]["per_query"][0],
                   layers["adjunct"]["per_query"][0]):
        assert record["skipped"] is True
        assert record["reason"] == "全部重复被安全否决拦截"


def test_report_layers_states_whether_the_acceptance_relation_holds(capsys):
    """验收关系 epsilon_core < epsilon_online < epsilon_adjunct 只报出、不当闸门
    （不成立本身是留给下一轮的信息，不是拿来调参凑的）。"""
    def _layer(mean, fill=0.97):
        return {"mean": mean, "p50": mean, "p95": mean, "mean_herbs_per_formula": 4.0,
                "n_formulas_counted": 3, "role_fill_rate": fill,
                "n_herb_items": 30, "n_unroled": 1}

    online = {"mean": 0.4, "mean_herbs_per_formula": 9.0, "n_formulas_counted": 3}
    ee._report_layers(online, {"core": _layer(0.05), "adjunct": _layer(0.8)})
    out = capsys.readouterr().out
    assert "0.05 < 0.4 < 0.8 → 成立" in out
    assert "role 填充率 97.0%" in out

    ee._report_layers(online, {"core": _layer(0.6), "adjunct": _layer(0.2)})
    out = capsys.readouterr().out
    assert "→ 不成立" in out
    assert "不成立不改参数去凑" in out


def test_report_layers_says_undeterminable_instead_of_guessing(capsys):
    """某一层没有可比样本（mean 是 None）时不能拿 0 去比——要说"无法判定"，
    并指出是哪一层缺数。"""
    empty = {"mean": None, "p50": None, "p95": None, "mean_herbs_per_formula": None,
             "n_formulas_counted": 0, "role_fill_rate": None,
             "n_herb_items": 0, "n_unroled": 0}
    ee._report_layers({"mean": 0.4, "mean_herbs_per_formula": 9.0, "n_formulas_counted": 3},
                      {"core": dict(empty), "adjunct": dict(empty)})
    out = capsys.readouterr().out
    assert "验收关系无法判定：core、adjunct 没有数" in out
    assert "role 填充率 未知（没有任何药材条目）" in out
