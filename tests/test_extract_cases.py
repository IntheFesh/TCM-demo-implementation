"""offline/extract_cases.py 的离线测试：假 LLM 后端，不需要网络。

手写 3 个 CaseSequence 当模型返回，端到端跑一遍 main()，覆盖单诊、三诊、
以及 LLM 切诊次数与正则估计对不上（触发交叉校验警告）三种场景。
"""
import json

import offline.extract_cases as extract_cases
from core.schemas import CaseSequence, VisitStructured


class FakeLLM:
    """按 system 提示词里嵌的标记文本，分派预设的 CaseSequence 返回值。"""

    def __init__(self, sequences_by_marker: dict[str, CaseSequence]):
        self.sequences_by_marker = sequences_by_marker
        self.calls = 0

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.calls += 1
        for marker, seq in self.sequences_by_marker.items():
            if marker in system:
                return seq
        raise AssertionError("无法从 system 提示词匹配到预设的 CaseSequence")


def _write_case(data_root, physician, stem, text):
    d = data_root / physician
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.txt").write_text(text, encoding="utf-8")


def test_full_pipeline_expands_visits_and_flags_mismatch(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    out_path = tmp_path / "cases.json"
    warnings_path = tmp_path / "extract_warnings.json"

    # 案 001：单诊，原文没有复诊标记 —— regex 估计 1 诊，LLM 也说 1 诊，一致
    single_raw = "TESTCASE001 朱 初诊胃脘痛。柴胡 白芍。"
    _write_case(data_root, "ye_tianshi", "001", single_raw)
    seq_single = CaseSequence(visits=[
        VisitStructured(visit_index=0, symptoms=["胃脘痛"], herbs=["柴胡", "白芍"]),
    ])

    # 案 002：三诊，原文有两个「又」标记 —— regex 估计 3 诊，LLM 也说 3 诊，一致
    triple_raw = (
        "TESTCASE002 李 初诊呕吐。半夏 生姜。\n"
        "又 呕吐减轻。茯苓。\n"
        "又 诸症皆平。甘草。"
    )
    _write_case(data_root, "ye_tianshi", "002", triple_raw)
    seq_triple = CaseSequence(visits=[
        VisitStructured(visit_index=0, symptoms=["呕吐"], herbs=["半夏", "生姜"]),
        VisitStructured(visit_index=1, visit_marker="又", response_to_prior="呕吐减轻", herbs=["茯苓"]),
        VisitStructured(visit_index=2, visit_marker="又", response_to_prior="诸症皆平", herbs=["甘草"]),
    ])

    # 案 003：原文没有任何复诊标记（regex 估计 1 诊），但 LLM 判断为 3 诊 —— 应触发交叉校验警告
    mismatch_raw = "TESTCASE003 王 反复呕吐三年。半夏 陈皮。"
    _write_case(data_root, "wu_jutong", "003", mismatch_raw)
    seq_mismatch = CaseSequence(visits=[
        VisitStructured(visit_index=0, symptoms=["呕吐"]),
        VisitStructured(visit_index=1),
        VisitStructured(visit_index=2),
    ])

    fake_llm = FakeLLM({
        "TESTCASE001": seq_single,
        "TESTCASE002": seq_triple,
        "TESTCASE003": seq_mismatch,
    })

    monkeypatch.setattr(extract_cases, "DATA_ROOT", data_root)
    monkeypatch.setattr(extract_cases, "OUT_PATH", out_path)
    monkeypatch.setattr(extract_cases, "WARNINGS_PATH", warnings_path)
    monkeypatch.setattr(extract_cases, "get_llm", lambda: fake_llm)

    extract_cases.main(argv=[])

    records = json.loads(out_path.read_text(encoding="utf-8"))
    assert len(records) == 1 + 3 + 3  # 三个案分别展开成 1、3、3 条 CaseRecord

    ids = {r["case_id"] for r in records}
    # case_id 格式 {physician}-{案号}-{诊次}
    assert "ye_tianshi-001-0" in ids
    assert {"ye_tianshi-002-0", "ye_tianshi-002-1", "ye_tianshi-002-2"} <= ids

    # prev_case_id 链式关系：第 0 诊为 None，第 i 诊指向第 i-1 诊
    by_id = {r["case_id"]: r for r in records}
    assert by_id["ye_tianshi-002-0"]["prev_case_id"] is None
    assert by_id["ye_tianshi-002-1"]["prev_case_id"] == "ye_tianshi-002-0"
    assert by_id["ye_tianshi-002-2"]["prev_case_id"] == "ye_tianshi-002-1"

    # case_group_id 同序列内一致
    group_ids = {r["case_group_id"] for cid, r in by_id.items() if cid.startswith("ye_tianshi-002-")}
    assert group_ids == {"ye_tianshi-002"}

    # 交叉校验不一致的案写进 extract_warnings.json，而不是静默通过或丢弃
    warnings = json.loads(warnings_path.read_text(encoding="utf-8"))
    assert len(warnings) == 1
    assert warnings[0]["case_group_id"] == "wu_jutong-003"
    assert warnings[0]["llm_visit_count"] == 3
    assert warnings[0]["regex_visit_estimate"] == 1
    # 不一致不等于丢弃：003 案的三诊记录仍然都写进了 cases.json
    assert {"wu_jutong-003-0", "wu_jutong-003-1", "wu_jutong-003-2"} <= ids


def test_no_warning_when_visit_counts_agree(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    out_path = tmp_path / "cases.json"
    warnings_path = tmp_path / "extract_warnings.json"

    single_raw = "TESTCASE 张 单诊胃痛。陈皮 半夏。"
    _write_case(data_root, "wu_jutong", "010", single_raw)
    seq = CaseSequence(visits=[VisitStructured(visit_index=0, symptoms=["胃痛"])])

    fake_llm = FakeLLM({"TESTCASE": seq})

    monkeypatch.setattr(extract_cases, "DATA_ROOT", data_root)
    monkeypatch.setattr(extract_cases, "OUT_PATH", out_path)
    monkeypatch.setattr(extract_cases, "WARNINGS_PATH", warnings_path)
    monkeypatch.setattr(extract_cases, "get_llm", lambda: fake_llm)

    extract_cases.main(argv=[])

    warnings = json.loads(warnings_path.read_text(encoding="utf-8"))
    assert warnings == []
