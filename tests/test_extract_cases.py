"""offline/extract_cases.py 的离线测试：假 LLM 后端，不需要网络。

数据现在是"粗段"（data/{physician}/*.json，由 offline.split_cases 生成），一段
可能有 0/1/多个病人。手写几个 SegmentPatients 当模型返回，端到端跑一遍 main()，
覆盖：单段单病人单诊、单段单病人多诊、单段多病人、零病人段、两种交叉校验。
"""
import json

import offline.extract_cases as extract_cases
from core.schemas import CaseSequence, SegmentPatients, VisitStructured


class FakeLLM:
    """按 system 提示词里嵌的标记文本，分派预设的 SegmentPatients 返回值。"""

    def __init__(self, results_by_marker: dict[str, SegmentPatients]):
        self.results_by_marker = results_by_marker
        self.calls = 0

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        for marker, result in self.results_by_marker.items():
            if marker in system:
                return result
        raise AssertionError("无法从 system 提示词匹配到预设的 SegmentPatients")


def _write_segment(data_root, physician, seg_id, text, head_hints=None, follow_hints=None):
    d = data_root / physician
    d.mkdir(parents=True, exist_ok=True)
    segment = {
        "seg_id": seg_id,
        "physician": physician,
        "text": text,
        "char_len": len(text),
        "head_hints": head_hints or [],
        "follow_hints": follow_hints or [],
    }
    (d / f"{seg_id}.json").write_text(json.dumps(segment, ensure_ascii=False), encoding="utf-8")


def test_full_pipeline_multi_patient_segment_and_cross_validation(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    out_path = tmp_path / "cases.json"
    warnings_path = tmp_path / "extract_warnings.json"

    # 段 0000：单病人单诊，head_hints=1 与 LLM 病人数一致
    _write_segment(
        data_root, "ye_tianshi", "ye_tianshi-0000",
        "TESTSEG0000 朱 初诊胃脘痛。柴胡 白芍。",
        head_hints=[{"pos": 12, "matched": "朱 ", "kind": "单字姓氏"}],
    )
    result_0000 = SegmentPatients(patients=[
        CaseSequence(visits=[VisitStructured(visit_index=0, symptoms=["胃脘痛"], herbs=["柴胡", "白芍"])]),
    ])

    # 段 0001：单病人三诊（两个复诊标记）
    _write_segment(
        data_root, "ye_tianshi", "ye_tianshi-0001",
        "TESTSEG0001 李 初诊呕吐。半夏 生姜。\n又 呕吐减轻。茯苓。\n又 诸症皆平。甘草。",
        head_hints=[{"pos": 12, "matched": "李 ", "kind": "单字姓氏"}],
        follow_hints=[{"pos": 30, "matched": "又"}, {"pos": 45, "matched": "又"}],
    )
    result_0001 = SegmentPatients(patients=[
        CaseSequence(visits=[
            VisitStructured(visit_index=0, symptoms=["呕吐"], herbs=["半夏", "生姜"]),
            VisitStructured(visit_index=1, visit_marker="又", response_to_prior="呕吐减轻", herbs=["茯苓"]),
            VisitStructured(visit_index=2, visit_marker="又", response_to_prior="诸症皆平", herbs=["甘草"]),
        ]),
    ])

    # 段 0002：粘连段，两个病人各一诊（head_hints=2，LLM 也切出 2 个病人，一致）
    _write_segment(
        data_root, "wu_jutong", "wu_jutong-0002",
        "TESTSEG0002 车 五十五岁 肠痈误下。乌药散。\n乙酉年 治通廷尉久疝。巴豆霜。",
        head_hints=[
            {"pos": 12, "matched": "车 五十五岁", "kind": "姓名岁数"},
            {"pos": 30, "matched": "乙酉年", "kind": "纪年"},
        ],
    )
    result_0002 = SegmentPatients(patients=[
        CaseSequence(visits=[VisitStructured(visit_index=0, symptoms=["肠痈"], herbs=["乌药散"])]),
        CaseSequence(visits=[VisitStructured(visit_index=0, symptoms=["久疝"], herbs=["巴豆霜"])]),
    ])

    # 段 0003：纯按语，零病人 —— 合法输出，不应被强迫编一个病人
    _write_segment(
        data_root, "wu_jutong", "wu_jutong-0003",
        "TESTSEG0003 徐评：此案议论精当，足资取法，别无新意。",
        head_hints=[],
    )
    result_0003 = SegmentPatients(patients=[])

    # 段 0004：patient_count 交叉校验不一致（head_hints=4，LLM 只切出 1 个病人）
    _write_segment(
        data_root, "wu_jutong", "wu_jutong-0004",
        "TESTSEG0004 一段密集提及多个称谓但 LLM 判断只有一个病人的文本。",
        head_hints=[
            {"pos": 1, "matched": "一人", "kind": "称谓"},
            {"pos": 5, "matched": "族婶", "kind": "族称"},
            {"pos": 9, "matched": "堂侄", "kind": "族称"},
            {"pos": 13, "matched": "太守", "kind": "头衔"},
        ],
    )
    result_0004 = SegmentPatients(patients=[
        CaseSequence(visits=[VisitStructured(visit_index=0, symptoms=["泄泻"])]),
    ])

    fake_llm = FakeLLM({
        "TESTSEG0000": result_0000,
        "TESTSEG0001": result_0001,
        "TESTSEG0002": result_0002,
        "TESTSEG0003": result_0003,
        "TESTSEG0004": result_0004,
    })

    monkeypatch.setattr(extract_cases, "DATA_ROOT", data_root)
    monkeypatch.setattr(extract_cases, "OUT_PATH", out_path)
    monkeypatch.setattr(extract_cases, "WARNINGS_PATH", warnings_path)
    monkeypatch.setattr(extract_cases, "get_llm", lambda: fake_llm)

    extract_cases.main(argv=[])

    records = json.loads(out_path.read_text(encoding="utf-8"))
    # 1 诊 + 3 诊 + (1+1) 诊 + 0 诊 + 1 诊 = 7 条 CaseRecord
    assert len(records) == 1 + 3 + 2 + 0 + 1

    ids = {r["case_id"] for r in records}
    # case_id 格式 {physician}-{seg_id}-p{病人序号}-{诊次}
    assert "ye_tianshi-ye_tianshi-0000-p0-0" in ids
    assert {"ye_tianshi-ye_tianshi-0001-p0-0", "ye_tianshi-ye_tianshi-0001-p0-1", "ye_tianshi-ye_tianshi-0001-p0-2"} <= ids

    # 粘连段里的两个病人必须有不同的 case_group_id，不能被合并成一个人
    by_id = {r["case_id"]: r for r in records}
    p0_group = by_id["wu_jutong-wu_jutong-0002-p0-0"]["case_group_id"]
    p1_group = by_id["wu_jutong-wu_jutong-0002-p1-0"]["case_group_id"]
    assert p0_group != p1_group

    # prev_case_id 链式关系：第 0 诊为 None，第 i 诊指向第 i-1 诊
    assert by_id["ye_tianshi-ye_tianshi-0001-p0-0"]["prev_case_id"] is None
    assert by_id["ye_tianshi-ye_tianshi-0001-p0-1"]["prev_case_id"] == "ye_tianshi-ye_tianshi-0001-p0-0"
    assert by_id["ye_tianshi-ye_tianshi-0001-p0-2"]["prev_case_id"] == "ye_tianshi-ye_tianshi-0001-p0-1"

    # 零病人段：不报错、不编病人，直接没有对应的 CaseRecord
    assert not any("0003" in cid for cid in ids)

    # 交叉校验：段 0004 应该触发 patient_count 不一致（LLM=1 vs head_hints=4，差 3 >= 2）
    warnings = json.loads(warnings_path.read_text(encoding="utf-8"))
    checks = {(w["seg_id"], w["check"]) for w in warnings}
    assert ("wu_jutong-0004", "patient_count") in checks
    w0004 = next(w for w in warnings if w["seg_id"] == "wu_jutong-0004" and w["check"] == "patient_count")
    assert w0004["llm_count"] == 1
    assert w0004["regex_count"] == 4

    # 一致的段不应该产生任何警告
    assert not any(w["seg_id"] == "ye_tianshi-0000" for w in warnings)
    assert not any(w["seg_id"] == "wu_jutong-0002" for w in warnings)


def test_visit_total_cross_validation_triggers_independently(tmp_path, monkeypatch):
    """诊次总量校验要能单独触发，即使病人数校验通过。"""
    data_root = tmp_path / "data"
    out_path = tmp_path / "cases.json"
    warnings_path = tmp_path / "extract_warnings.json"

    _write_segment(
        data_root, "wu_jutong", "wu_jutong-0005",
        "TESTSEG0005 王 乙酉五月二十一日 呕吐不食。\n二十三日 呕止。\n廿五日 能食。\n廿七日 诸症皆平。",
        head_hints=[{"pos": 12, "matched": "王 ", "kind": "单字姓氏"}],
        # 段内标了 3 个复诊标记，head_hints(1) + follow_hints(3) = 4
        follow_hints=[{"pos": 30, "matched": "二十三日"}, {"pos": 40, "matched": "廿五日"}, {"pos": 50, "matched": "廿七日"}],
    )
    # LLM 只切出 1 诊（漏了三次复诊），总诊次 1 vs 正则估计 4，差 3 >= 2
    result = SegmentPatients(patients=[
        CaseSequence(visits=[VisitStructured(visit_index=0, symptoms=["呕吐"])]),
    ])

    fake_llm = FakeLLM({"TESTSEG0005": result})
    monkeypatch.setattr(extract_cases, "DATA_ROOT", data_root)
    monkeypatch.setattr(extract_cases, "OUT_PATH", out_path)
    monkeypatch.setattr(extract_cases, "WARNINGS_PATH", warnings_path)
    monkeypatch.setattr(extract_cases, "get_llm", lambda: fake_llm)

    extract_cases.main(argv=[])

    warnings = json.loads(warnings_path.read_text(encoding="utf-8"))
    w = next(w for w in warnings if w["seg_id"] == "wu_jutong-0005" and w["check"] == "visit_total")
    assert w["llm_count"] == 1
    assert w["regex_count"] == 4
    # patient_count 这一项应该是一致的（LLM=1 vs head_hints=1），不该出现
    assert not any(w["seg_id"] == "wu_jutong-0005" and w["check"] == "patient_count" for w in warnings)
