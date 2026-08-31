"""offline/export_sft.py 的离线测试：版权过滤、按字段是否为空决定是否生成对应任务样本。"""
from core.schemas import CaseRecord
from offline.export_sft import filter_public_domain, to_samples


def _case(**overrides) -> CaseRecord:
    base = dict(
        case_id="ye_tianshi-001",
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
