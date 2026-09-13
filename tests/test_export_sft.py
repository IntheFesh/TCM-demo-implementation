"""offline/export_sft.py 的离线测试：版权过滤、按字段是否为空决定是否生成对应任务样本。"""
from core.schemas import CaseRecord
from offline.export_sft import filter_public_domain, to_samples


def _case(**overrides) -> CaseRecord:
    base = dict(
        case_id="ye_tianshi-001",
        case_group_id="ye_tianshi-001",
        physician="ye_tianshi",
        raw="朱 初因面肿……",
        symptoms=["面肿", "喘"],
        tongue="舌绛",
        pulse=None,
        syndrome="湿热布散三焦",
        pathogenesis="邪干阳位，气壅不通",
        treatment_principle="清肃上焦",
        formula=None,
        herbs=["飞滑石", "杏仁"],
        copyright_status="public_domain",
    )
    base.update(overrides)
    return CaseRecord(**base)


def test_filter_public_domain_excludes_copyrighted():
    cases = [_case(), _case(case_id="modern-001", copyright_status="copyrighted")]
    kept = filter_public_domain(cases)
    assert [c.case_id for c in kept] == ["ye_tianshi-001"]


# ---------- 总纲 2.5：含十八反配伍的医案不进训练集 ----------


def test_filter_incompatible_pairs_excludes_flagged_cases_and_reports_to_stderr(capsys):
    from offline.export_sft import filter_incompatible_pairs

    cases = [_case(), _case(case_id="like-001", has_incompatible_pair=True)]
    kept = filter_incompatible_pairs(cases)
    assert [c.case_id for c in kept] == ["ye_tianshi-001"]
    err = capsys.readouterr().err
    assert "排除 1 条含十八反十九畏配伍" in err and "like-001" in err


def test_filter_incompatible_pairs_is_silent_when_nothing_to_exclude(capsys):
    from offline.export_sft import filter_incompatible_pairs

    assert len(filter_incompatible_pairs([_case(), _case(case_id="b")])) == 2
    assert capsys.readouterr().err == ""


# ---------- 总纲 5.1（M15）：六层链路格式 ----------


def _triple(case_id, p, o, span, s="x"):
    return {"case_id": case_id, "physician": "ye_tianshi", "s": s, "p": p, "o": o, "source_span": span}


def test_to_chain_sample_builds_steps_from_available_fields_with_rationale_from_source_span():
    from offline.export_sft import to_chain_sample

    case = _case(formula="逍遥散")
    triples = [
        _triple("ye_tianshi-001", "提示", "邪干阳位", "面肿而喘，此邪干阳位"),
        _triple("ye_tianshi-001", "治以", "清肃上焦", "治以清肃上焦"),
        _triple("ye_tianshi-001", "含", "杏仁", "杏仁三钱", s="逍遥散"),
    ]
    sample = to_chain_sample(case, triples)
    assert sample["input"] == "面肿；喘，舌绛"
    steps = {s["step"]: s for s in sample["chain"]}
    assert list(steps) == ["症状→病机", "病机→证型", "证型→治法", "治法→方剂", "方剂→药材"]
    assert steps["症状→病机"]["rationale"] == "面肿而喘，此邪干阳位"
    assert steps["病机→证型"]["rationale"] is None      # 没有「属于」三元组：不编，None
    assert steps["证型→治法"]["rationale"] == "治以清肃上焦"
    assert steps["治法→方剂"]["rationale"] is None
    herbs = steps["方剂→药材"]["output"]
    assert herbs == [{"name": "飞滑石", "rationale": None}, {"name": "杏仁", "rationale": "杏仁三钱"}]
    assert all(s["source"] == "case:ye_tianshi-001" for s in sample["chain"])
    assert sample["meta"]["case_group_id"] == "ye_tianshi-001"


def test_to_chain_sample_herb_rationale_falls_back_to_用药_when_no_含():
    from offline.export_sft import to_chain_sample

    case = _case(formula=None)
    sample = to_chain_sample(case, [_triple("ye_tianshi-001", "用药", "杏仁", "喘加杏仁", s="喘")])
    herb_step = sample["chain"][-1]
    assert herb_step["step"] == "治法→药材"
    assert herb_step["output"][1] == {"name": "杏仁", "rationale": "喘加杏仁"}


def test_to_chain_sample_returns_none_when_fewer_than_two_steps():
    from offline.export_sft import to_chain_sample

    assert to_chain_sample(_case(syndrome=None, pathogenesis=None, treatment_principle=None,
                                 herbs=[]), []) is None
    # 只有证型一步——不成链
    assert to_chain_sample(_case(pathogenesis=None, treatment_principle=None, herbs=[]), []) is None


def test_split_by_case_group_keeps_all_visits_of_a_patient_on_the_same_side():
    from offline.export_sft import split_by_case_group

    cases = [_case(case_id=f"g{i}-{v}", case_group_id=f"g{i}") for i in range(200) for v in (0, 1)]
    split = split_by_case_group(cases, heldout_ratio=0.2)
    assert set(split.values()) == {"train", "heldout"}
    heldout = sum(1 for v in split.values() if v == "heldout")
    assert 20 <= heldout <= 60  # 200 组按 20% 切，允许哈希抖动，但不能一边倒
    assert split == split_by_case_group(list(reversed(cases)), heldout_ratio=0.2)  # 确定性，跟顺序无关


def test_split_by_case_group_rejects_bad_ratio():
    import pytest
    from offline.export_sft import split_by_case_group

    with pytest.raises(ValueError):
        split_by_case_group([_case()], heldout_ratio=1.0)


def test_export_chain_reports_rationale_coverage_with_its_complement():
    from offline.export_sft import export_chain

    cases = [_case(), _case(case_id="ye_tianshi-002", case_group_id="ye_tianshi-002")]
    triples = {"ye_tianshi-001": [_triple("ye_tianshi-001", "治以", "清肃上焦", "治以清肃上焦")]}
    samples, stats = export_chain(cases, triples, heldout_ratio=0.0)
    assert stats["samples"] == 2 and stats["cases_not_chainable"] == 0
    assert stats["split"] == {"train": 2}
    # 每条 4 步（病机/证型/治法/两味药=2）→ 5 个计数单位 ×2 = 10；只有 1 步有依据
    assert stats["steps"] == 10
    assert stats["steps_with_rationale"] == 1
    assert stats["steps_without_rationale"] == 9
    assert all(s["meta"]["split"] == "train" for s in samples)


def test_main_chain_format_writes_samples_and_excludes_incompatible(tmp_path, capsys):
    import json
    from offline import export_sft

    rows = [
        _case().model_dump(),
        _case(case_id="like-001", case_group_id="like-001", has_incompatible_pair=True).model_dump(),
    ]
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "chain.jsonl"

    export_sft.main(["--format", "chain", "--cases-path", str(cases_path),
                     "--triples-path", str(tmp_path / "missing.jsonl"), "--out", str(out)])
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [s["meta"]["case_id"] for s in lines] == ["ye_tianshi-001"]
    captured = capsys.readouterr()
    assert "rationale 都会是 None" in captured.err
    assert "链路样本数：1" in captured.out


def test_to_samples_generates_all_four_tasks_when_fields_present():
    case = _case()
    samples = to_samples(case)
    tasks = {s["meta"]["task"] for s in samples}
    assert tasks == {"T1_辨证", "T2_立法", "T3_处方", "T7_抽取"}


def test_to_samples_skips_t1_when_syndrome_missing():
    case = _case(syndrome=None)
    samples = to_samples(case)
    tasks = {s["meta"]["task"] for s in samples}
    assert "T1_辨证" not in tasks
    # T2/T3 依赖 syndrome，也应一并跳过
    assert "T2_立法" not in tasks
    assert "T3_处方" not in tasks
    # T7 只依赖原文，仍应生成
    assert "T7_抽取" in tasks


def test_to_samples_skips_t3_when_herbs_empty():
    case = _case(herbs=[])
    samples = to_samples(case)
    tasks = {s["meta"]["task"] for s in samples}
    assert "T3_处方" not in tasks
    assert "T1_辨证" in tasks
    assert "T2_立法" in tasks


def test_sample_meta_carries_physician_and_case_id():
    case = _case()
    samples = to_samples(case)
    for s in samples:
        assert s["meta"]["physician_id"] == "ye_tianshi"
        assert s["meta"]["case_id"] == "ye_tianshi-001"
        assert s["meta"]["copyright_status"] == "public_domain"


# ---------- 审查修复：T7 的输入必须是这一诊的原文 ----------

def _visit(case_group_id, visit_index, raw_excerpt=None, raw="整段粗段原文，两个病人共享"):
    from core.schemas import CaseRecord

    return CaseRecord(
        case_id=f"{case_group_id}-{visit_index}", case_group_id=case_group_id,
        physician="ye_tianshi", raw=raw, raw_excerpt=raw_excerpt,
        visit_index=visit_index, symptoms=["纳差"], syndrome="脾虚",
    )


def _t7_inputs(case):
    return [s["input"] for s in to_samples(case) if s["meta"]["task"] == "T7_抽取"]


def test_t7_prefers_the_visit_excerpt_over_the_whole_segment():
    assert _t7_inputs(_visit("ye_tianshi-0001-p0", 0, raw_excerpt="本诊片段")) == ["本诊片段"]


def test_t7_skips_follow_up_visits_without_excerpt():
    """复诊没有本诊片段时不能退回整段 raw：整段是初诊+复诊共享的，会导出
    「同一输入、互相矛盾的输出」的样本。"""
    assert _t7_inputs(_visit("ye_tianshi-0001-p0", 1)) == []


def test_t7_skips_second_patient_without_excerpt():
    """同一粗段里的第二个病人同理：整段 raw 属于两个人。"""
    assert _t7_inputs(_visit("ye_tianshi-0001-p1", 0)) == []


def test_t7_keeps_whole_segment_for_first_patient_initial_visit():
    assert _t7_inputs(_visit("ye_tianshi-0001-p0", 0)) == ["整段粗段原文，两个病人共享"]
