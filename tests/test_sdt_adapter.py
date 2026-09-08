"""eval/sdt/ 的离线测试。不需要网络、不需要 TCMEval 仓库——SDT 的目录结构用
tmp_path 造一份最小的，LLM 用假后端。

真实数据上的格式验证不在这里（那需要 clone 官方仓库），结论记在
data/SOURCES.md 第 14 条：把金标准原样当提交，Test 得 50.0000/50（满分，
说明格式逐字节正确），Validation 得 48.9998/50，差的那 1.0002 已逐位解释。
"""
import json

import pytest

from eval.sdt import adapter, data


# ---------- 造一份最小 SDT 目录 ----------

REC_A = {
    "Medical Record ID": "病例1",
    "Clinical Data": "女性，50岁。呃逆嗳气频作，两胁胀满。舌苔薄白，脉弦。",
    "Clinical Information": "呃逆嗳气;两胁胀满;脉弦",
    "Answers of TCM Pathogenesis": "A;J",
    "Options of TCM Pathogenesis": "A:肝气横逆;J:胃气不得下降;C:热伤肺络",
    "Answers of TCM Syndrome": "B",
    "Options of TCM Syndrome": "A:脾胃不和;B:脾胃受制",
    "Explanatory Summary": "临证体会：肝气横逆。",
    "Syndrome Differentiation": "辨证：肝气横逆，脾胃受制",
}
REC_DANGER = {
    "Medical Record ID": "病例2",
    "Clinical Data": "男性，40岁。近日呕血两次，面色苍白。",
    "Clinical Information": "呕血;面色苍白",
    "Answers of TCM Pathogenesis": "C",
    "Options of TCM Pathogenesis": "A:肝气横逆;C:热伤肺络",
    "Answers of TCM Syndrome": "A",
    "Options of TCM Syndrome": "A:血热妄行;B:脾胃受制",
    "Explanatory Summary": "临证体会：热伤肺络。",
    "Syndrome Differentiation": "辨证：血热妄行",
}


def _gold_line(r, bom=False):
    return ("﻿" if bom else "") + "@".join([
        r["Medical Record ID"], r["Clinical Information"],
        r["Answers of TCM Pathogenesis"], r["Answers of TCM Syndrome"],
        r["Explanatory Summary"] + r["Syndrome Differentiation"],
    ])


@pytest.fixture
def sdt_dir(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "Results").mkdir()
    def blank(r):
        return {**r, "Clinical Information": "", "Answers of TCM Pathogenesis": "",
                "Answers of TCM Syndrome": "", "Explanatory Summary": "",
                "Syndrome Differentiation": ""}
    (tmp_path / "data" / "Train_TCM_Data_v1.json").write_text(
        json.dumps([REC_A, REC_DANGER], ensure_ascii=False), encoding="utf-8")
    (tmp_path / "data" / "Validation_TCM_Data_v1.json").write_text(
        json.dumps([blank(REC_A), blank(REC_DANGER)], ensure_ascii=False), encoding="utf-8")
    # 第一行带 BOM，跟官方 Validation 金标准一致
    (tmp_path / "Results" / "Validation_data_result.txt").write_text(
        _gold_line(REC_A, bom=True) + "\n" + _gold_line(REC_DANGER), encoding="utf-8")
    return tmp_path


# ---------- 数据层 ----------

def test_parse_options():
    assert data.parse_options("A:甲;B:乙") == {"A": "甲", "B": "乙"}
    assert data.parse_options("") == {}
    assert data.parse_options("坏数据没有冒号") == {}


def test_validation_json_has_no_gold(sdt_dir):
    """实测：Validation/Test 的 JSON 里答案字段全是空的，金标准在 Results/*.txt。
    照着 Train 的字段写代码会得到一份全空的金标准而且不报错。"""
    recs = data.load_split(sdt_dir, "Validation")
    assert len(recs) == 2
    assert all(not r.gold_clinical_information for r in recs)
    assert recs[0].pathogenesis_options and recs[0].syndrome_options


def test_train_json_has_gold(sdt_dir):
    recs = data.load_split(sdt_dir, "Train")
    assert recs[0].gold_clinical_information == ["呃逆嗳气", "两胁胀满", "脉弦"]
    assert recs[0].gold_summary.endswith("辨证：肝气横逆，脾胃受制")


def test_read_gold_preserves_bom_by_default(sdt_dir):
    """官方 evaluate.py 用默认方式读，BOM 粘在第一条 ID 上导致那条恒 0 分。
    默认保留这个行为——"修好"它，我们的分就跟论文里的 15 个模型不可比了。"""
    kept = data.read_gold(sdt_dir, "Validation")
    assert "﻿病例1" in kept and "病例1" not in kept
    stripped = data.read_gold(sdt_dir, "Validation", strip_bom=True)
    assert "病例1" in stripped


def test_attach_gold_fills_validation(sdt_dir):
    recs = data.load_split(sdt_dir, "Validation")
    n = data.attach_gold(recs, data.read_gold(sdt_dir, "Validation", strip_bom=True))
    assert n == 2
    assert recs[0].gold_pathogenesis_answers == ["A", "J"]


def test_sanitize_protects_the_field_separator():
    """@ 或换行漏进字段会让整行错位，评分脚本按位置取字段，Task2 会拿到 Task1
    的内容——分数就完全没有意义了。"""
    line = data.to_line("病例1", ["a@b"], ["A"], ["B"], "多\n行\r文本")
    assert len(line.split("@")) == 5
    assert "\n" not in line and "\r" not in line


def test_to_line_round_trips(sdt_dir):
    gold = data.read_gold(sdt_dir, "Validation", strip_bom=True)
    rid, f = next(iter(gold.items()))
    line = data.to_line(rid, f[0].split(";"), f[1].split(";"), f[2].split(";"), f[3])
    assert line.split("@") == [rid, f[0], f[1], f[2], f[3]]


def test_write_submission_has_no_bom(tmp_path):
    """官方金标准里的 BOM 是它的 bug，别复现到自己的提交文件上——
    带 BOM 的话我们自己的第一条也会匹配不上。"""
    p = tmp_path / "sub.txt"
    data.write_submission(p, ["病例1@a@A@B@c"])
    assert p.read_bytes()[:3] != b"\xef\xbb\xbf"


# ---------- 适配器 ----------

def test_filter_valid_options_drops_only_nonexistent_letters():
    opts = {"A": "甲", "B": "乙"}
    assert adapter.filter_valid_options(["b", "Z", "A", "A", ""], opts) == ["A", "B"]
    assert adapter.filter_valid_options([], opts) == []


class FakeSdtLLM:
    def __init__(self):
        self.systems = []

    def model_name(self): return "fake-model"
    def backend_id(self): return "fake"
    def comparability_warning(self): return None

    def generate(self, system, user, schema, temperature=0.0, **kwargs):
        self.systems.append((schema.__name__, system))
        if schema is adapter.ExtractedInfo:
            return adapter.ExtractedInfo(items=["呃逆嗳气", "两胁胀满"])
        if schema is adapter.SelectedOptions:
            # 故意混进一个不存在的字母，验证过滤
            return adapter.SelectedOptions(pathogenesis=["A", "Z"], syndrome=["B"])
        if schema is adapter.CaseSummary:
            return adapter.CaseSummary(summary="临证体会：肝气横逆。辨证：脾胃受制")
        raise AssertionError(f"未预期的 schema {schema}")


@pytest.fixture
def fake_llm(monkeypatch):
    llm = FakeSdtLLM()
    monkeypatch.setattr(adapter, "get_llm", lambda: llm)
    return llm


def _record(raw):
    return data.SdtRecord(
        record_id=raw["Medical Record ID"], clinical_data=raw["Clinical Data"],
        pathogenesis_options=data.parse_options(raw["Options of TCM Pathogenesis"]),
        syndrome_options=data.parse_options(raw["Options of TCM Syndrome"]),
    )


def test_baseline_solver_three_calls(fake_llm):
    ans = adapter.BaselineSolver().solve(_record(REC_A))
    assert ans.llm_calls == 3
    assert ans.clinical_information == ["呃逆嗳气", "两胁胀满"]
    assert ans.pathogenesis_answers == ["A"]      # Z 被过滤掉
    assert ans.syndrome_answers == ["B"]
    assert ans.summary.startswith("临证体会")
    assert ans.safety_rejected is None


def test_baseline_injects_no_reasoning(fake_llm):
    adapter.BaselineSolver().solve(_record(REC_A))
    assert all("结构化分析" not in s for _, s in fake_llm.systems)


def test_chain_solver_injects_reasoning_into_select_and_summary(monkeypatch, fake_llm):
    """只改「注不注入证素分析」这一个变量，跟 baseline 的差值才归因得清楚。
    Task1 刻意不注入——它要的是原文片段，喂归一化术语只会把它带偏。"""
    from core.schemas import ElementHit, S1Normalize, S2Elements

    monkeypatch.setattr("core.chain.normalize",
                        lambda c: S1Normalize(symptoms=["嗳气"], tongue="薄白", pulse="弦"))
    monkeypatch.setattr("core.chain.infer_elements", lambda s1: S2Elements(
        elements=[ElementHit(element="肝", kind="location",
                             supporting_symptoms=["嗳气"], confidence="high")]))
    ans = adapter.ChainSolver().solve(_record(REC_A))
    assert ans.llm_calls == 5  # 3 + S1 + S2
    by_schema = {name: sys for name, sys in fake_llm.systems}
    assert "结构化分析" not in by_schema["ExtractedInfo"]
    assert "结构化分析" in by_schema["SelectedOptions"]
    assert "结构化分析" in by_schema["CaseSummary"]


def test_safety_veto_produces_empty_answer_and_costs_nothing(fake_llm):
    """SDT 里有 6%(Validation)/16%(Test) 的记录会命中危重症状拦截。
    它们提交空答案、得 0 分——那是这套系统真实的行为，不许为了跑分关掉。"""
    ans = adapter.BaselineSolver().solve(_record(REC_DANGER))
    assert ans.safety_rejected and "呕血" in ans.safety_rejected
    assert ans.clinical_information == [] and ans.syndrome_answers == []
    assert ans.llm_calls == 0
    assert fake_llm.systems == []
    assert ans.to_line() == "病例2@@@@"


def test_ignore_safety_veto_is_opt_in(fake_llm):
    ans = adapter.BaselineSolver().solve(_record(REC_DANGER), ignore_safety_veto=True)
    assert ans.safety_rejected is None
    assert ans.llm_calls == 3


def test_both_solvers_registered():
    """只报「我们拿了 X 分」没有意义，必须有同模型同输出头的对照组。"""
    assert set(adapter.SOLVERS) == {"baseline", "chain"}


# ---------- 打分包装层 ----------

def test_missing_official_scorer_raises_instead_of_falling_back(tmp_path):
    """静默退回自己实现的计分会产出一个看起来正常、实际不可比的数。"""
    from eval.sdt.score import load_official_scorer

    with pytest.raises(FileNotFoundError, match="不可比"):
        load_official_scorer(tmp_path)


def test_score_submission_uses_the_official_entry_point(sdt_dir, tmp_path):
    """桩脚本只验证包装层的接线（有没有真的去调 automated_score），
    计分数学本身不在这里测——那是官方脚本的事，我们不重新实现。"""
    (sdt_dir / "evaluate.py").write_text(
        "called = []\n"
        "def automated_score(a, b):\n"
        "    called.append((a, b));  return 1.5\n"
        "def clinical_info_extraction_eval(x, y): return 1.0\n"
        "def score_proportional(x, y, m): return 1.0\n"
        "def rouge_l(a, b): return 1.0\n",
        encoding="utf-8")
    sub = tmp_path / "sub.txt"
    data.write_submission(sub, [_gold_line(REC_A), _gold_line(REC_DANGER)])

    from eval.sdt.score import score_submission

    r = score_submission(sdt_dir, "Validation", sub, diagnose_bom=True)
    assert r["official_total"] == 1.5
    assert r["n_records"] == 2 and r["n_submitted"] == 2
    assert r["official_per_record"] == 0.75
    assert r["task_totals"] == {"task1": 2.0, "task2": 2.0, "task3": 2.0, "task4": 2.0}
    assert r["weighted_from_breakdown"] == pytest.approx(2.0)


def test_run_only_ids_filters_and_rejects_unknown(sdt_dir, tmp_path, monkeypatch, fake_llm):
    """量化安全否决的代价时只需要重跑被拦的那几条——其余记录两次输入完全相同，
    重跑只是白花钱。ID 写错要立刻报错，不能静默跑一个空集合出来。"""
    from eval.sdt import run as run_mod

    out = tmp_path / "sub.txt"
    run_mod.main(["--sdt-dir", str(sdt_dir), "--split", "Validation",
                  "--solver", "baseline", "--out", str(out), "--only-ids", "病例2",
                  "--ignore-safety-veto"])
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("病例2@")

    with pytest.raises(SystemExit, match="不在 Validation 里"):
        run_mod.main(["--sdt-dir", str(sdt_dir), "--split", "Validation",
                      "--solver", "baseline", "--out", str(out), "--only-ids", "病例999"])


@pytest.mark.parametrize("chosen,expected", [
    (["A;B"], ["A", "B"]), (["A、B"], ["A", "B"]), (["AB"], ["A", "B"]),
    (["A:肝气横逆"], ["A"]), (["肝气横逆"], ["A"]), (["A;Z"], ["A"]), (["b", "", "Z"], ["B"]),
])
def test_filter_valid_options_tolerates_common_answer_shapes(chosen, expected):
    """模型把几个字母塞进一个元素、或回选项文本而不是字母，原来会被静默丢成空、
    该题记 0 分且无任何记录。"""
    opts = {"A": "肝气横逆", "B": "胃中失和", "C": "热伤肺络"}
    assert adapter.filter_valid_options(chosen, opts) == expected
