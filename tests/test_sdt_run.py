"""eval/sdt/run.py 的离线测试：main() 端到端跑一遍，SOLVERS/get_llm 都换成假的，
不碰真实 LLM/网络。

核心是失败容忍：solver.solve() 原来没有任何异常捕获，一条记录的一次调用失败
（真实场景是 core.llm.generate() 重试 3 次仍失败抛出的 LLMError）会让整批
（Validation 50 条 × 4 次调用）崩掉、前面跑完的全丢——这里复现这个场景。
"""
import json

import core.llm as llm_mod
from eval.sdt import run as sdt_run
from eval.sdt.adapter import SdtAnswer


def _write_split(tmp_path, record_ids):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "Medical Record ID": rid,
            "Clinical Data": f"{rid} 的临床资料原文",
            "Options of TCM Pathogenesis": "A:肝气犯胃;B:脾胃虚寒",
            "Options of TCM Syndrome": "C:肝胃不和证;D:脾胃虚寒证",
            "Clinical Information": "", "Answers of TCM Pathogenesis": "",
            "Answers of TCM Syndrome": "", "Explanatory Summary": "",
            "Syndrome Differentiation": "",
        }
        for rid in record_ids
    ]
    (data_dir / "Validation_TCM_Data_v1.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


class _FakeBackend:
    def model_name(self):
        return "fake-model"

    def backend_id(self):
        return "fake"

    def comparability_warning(self):
        return None


def _install_fake_llm(monkeypatch):
    """run.py 的 manifest 段在函数体内 `from core.llm import get_llm`（不是
    模块级绑定），monkeypatch 模块级的 get_llm 名字对它没用——直接换掉单例
    本身，reset_llm_singleton 由 monkeypatch 在测试结束时不需要手动还原
    （每个测试独立跑，字段作用域只在这次调用里）。"""
    monkeypatch.setattr(llm_mod, "_llm_singleton", _FakeBackend())


class _FakeSolver:
    """跟真实 BaselineSolver/ChainSolver 一样有 name/solve()，但 solve() 对
    指定的 record_id 直接抛异常，模拟 core.llm.generate() 重试耗尽后的
    LLMError——不真的调 LLM。"""

    name = "fake"

    def __init__(self, failing_ids=frozenset()):
        self.failing_ids = failing_ids
        self.calls = []

    def solve(self, record, ignore_safety_veto=None):
        self.calls.append(record.record_id)
        if record.record_id in self.failing_ids:
            raise RuntimeError(f"模拟 {record.record_id} 调用失败")
        return SdtAnswer(
            record_id=record.record_id,
            clinical_information=["症状甲"], pathogenesis_answers=["A"],
            syndrome_answers=["C"], summary="辨证小结", llm_calls=3,
        )


def test_main_survives_a_single_solve_failure_without_crashing_the_batch(tmp_path, monkeypatch):
    """核心回归：4 条记录，第 2 条 solve() 失败——main() 之前没有任何异常
    捕获，这条会直接把整批崩掉（IndexError/RuntimeError 一路冒出 main()），
    另外 3 条已经跑完的结果也会丢。修复后要能跑完全部 4 条。"""
    sdt_dir = _write_split(tmp_path, ["case-1", "case-2", "case-3", "case-4"])
    fake_solver = _FakeSolver(failing_ids={"case-2"})
    monkeypatch.setattr(sdt_run, "SOLVERS", {"fake": lambda: fake_solver})
    _install_fake_llm(monkeypatch)

    out_path = tmp_path / "out" / "submission.txt"
    sdt_run.main([
        "--sdt-dir", str(sdt_dir), "--split", "Validation",
        "--solver", "fake", "--out", str(out_path),
    ])

    assert fake_solver.calls == ["case-1", "case-2", "case-3", "case-4"]  # 全部跑完，没有中途崩掉

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4  # 官方评分按记录数对齐，提交文件不能少一行
    assert lines[1].startswith("case-2@")  # 失败的那条也占了位，不是被跳过

    manifest = json.loads(out_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["call_failed"] == ["case-2"]
    assert manifest["n_call_failed"] == 1
    assert manifest["safety_rejected"] == []  # 调用失败不能被算成安全否决


def test_main_call_failed_record_submits_empty_answer_not_safety_rejected(tmp_path, monkeypatch):
    """失败记录提交的是空答案（跟安全否决一样的空壳，得 0 分——官方评分
    脚本需要每条都有提交行），但绝不能出现在 safety_rejected 列表里：那个
    字段专门代表"系统真的拦截了"，调用失败是基础设施抖动，两者混在一起会
    让读分数的人误判系统的安全否决率。"""
    sdt_dir = _write_split(tmp_path, ["case-1"])
    fake_solver = _FakeSolver(failing_ids={"case-1"})
    monkeypatch.setattr(sdt_run, "SOLVERS", {"fake": lambda: fake_solver})
    _install_fake_llm(monkeypatch)

    out_path = tmp_path / "out" / "submission.txt"
    sdt_run.main([
        "--sdt-dir", str(sdt_dir), "--split", "Validation",
        "--solver", "fake", "--out", str(out_path),
    ])

    lines = out_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["case-1@@@@"]  # 四个字段全空的空壳提交行

    manifest = json.loads(out_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["call_failed"] == ["case-1"]
    assert manifest["safety_rejected"] == []


def test_main_prints_call_failed_warning_and_does_not_crash_when_none_fail(tmp_path, monkeypatch, capsys):
    """没有任何记录失败时不该打失败警告——警告只在真的有失败时才有意义。"""
    sdt_dir = _write_split(tmp_path, ["case-1", "case-2"])
    fake_solver = _FakeSolver(failing_ids=set())
    monkeypatch.setattr(sdt_run, "SOLVERS", {"fake": lambda: fake_solver})
    _install_fake_llm(monkeypatch)

    out_path = tmp_path / "out" / "submission.txt"
    sdt_run.main([
        "--sdt-dir", str(sdt_dir), "--split", "Validation",
        "--solver", "fake", "--out", str(out_path),
    ])
    out = capsys.readouterr().out
    assert "注意" not in out
    assert "警告" not in out
    manifest = json.loads(out_path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_call_failed"] == 0


def test_main_warns_when_failure_rate_exceeds_threshold(tmp_path, monkeypatch, capsys):
    """5 条记录里 2 条失败（40%）超过 core.batch.FAILURE_RATE_WARNING_THRESHOLD
    （20%），stdout 要打出醒目警告——用一个会在指定 record_id 上失败的假
    solver 跑 5 条主诉，这是这一条的"N 次调用失败"复现。"""
    ids = ["case-1", "case-2", "case-3", "case-4", "case-5"]
    sdt_dir = _write_split(tmp_path, ids)
    fake_solver = _FakeSolver(failing_ids={"case-2", "case-4"})
    monkeypatch.setattr(sdt_run, "SOLVERS", {"fake": lambda: fake_solver})
    _install_fake_llm(monkeypatch)

    out_path = tmp_path / "out" / "submission.txt"
    sdt_run.main([
        "--sdt-dir", str(sdt_dir), "--split", "Validation",
        "--solver", "fake", "--out", str(out_path),
    ])
    out = capsys.readouterr().out
    assert "警告" in out
    assert "2/5" in out
