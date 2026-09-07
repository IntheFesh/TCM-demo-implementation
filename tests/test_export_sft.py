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
