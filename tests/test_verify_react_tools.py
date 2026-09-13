"""scripts/verify_react_tools.py 的离线测试：monkeypatch core.chain.consult
（collect_react_process_samples 惰性 import 的就是这个名字），不需要真实
LLM/cases.json——那部分（P1 验证）留给 AutoDL 跑这个脚本本身。
"""
from types import SimpleNamespace

from scripts import verify_react_tools as vrt


def _step(step, action, action_input=None, thought="t", observation="{}"):
    return SimpleNamespace(step=step, action=action, action_input=action_input or {},
                           thought=thought, observation=observation)


def _trace(steps, terminated_by="finish"):
    return SimpleNamespace(steps=steps, terminated_by=terminated_by, llm_calls=len(steps))


def _fake_consult_case_layer_heavy(complaint, **kwargs):
    """3 次动作里 1 次是 search_cases——1/3 ≈ 33%，仍然低于 35% 的闸门，
    专门再垫一次 query_case_graph 让占比过 35%。"""
    steps = [
        _step(1, "search_cases", {"query": complaint, "physician": "ye_tianshi"}),
        _step(2, "query_case_graph", {"symptom": complaint}),
        _step(3, "lookup_standard", {"query": "SP-01"}),
        _step(4, "finish"),
    ]
    return {
        "rejected": False, "insufficient": False,
        "results": [{"physician": "ye_tianshi", "react_trace": _trace(steps)}],
    }


def _fake_consult_case_layer_light(complaint, **kwargs):
    steps = [
        _step(1, "query_graph", {"node": "纳呆"}),
        _step(2, "lookup_standard", {"query": "SP-01"}),
        _step(3, "finish"),
    ]
    return {
        "rejected": False, "insufficient": False,
        "results": [{"physician": "ye_tianshi", "react_trace": _trace(steps)}],
    }


def test_main_returns_zero_when_case_layer_ratio_above_gate(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy)
    queries_path = tmp_path / "q.txt"
    queries_path.write_text("主诉甲\n", encoding="utf-8")

    code = vrt.main(["--queries-path", str(queries_path), "--limit", "1"])
    out = capsys.readouterr().out

    assert code == 0
    assert "★命中" in out
    assert "search_cases" in out
    assert "query_case_graph" in out


def test_main_returns_one_when_case_layer_ratio_below_gate(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_light)
    queries_path = tmp_path / "q.txt"
    queries_path.write_text("主诉甲\n", encoding="utf-8")

    code = vrt.main(["--queries-path", str(queries_path), "--limit", "1"])
    err = capsys.readouterr().err

    assert code == 1
    assert "✗ 未命中" in err


def test_main_prints_top_n_full_traces(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy)
    queries_path = tmp_path / "q.txt"
    queries_path.write_text("主诉甲\n主诉乙\n主诉丙\n主诉丁\n", encoding="utf-8")

    vrt.main(["--queries-path", str(queries_path), "--limit", "4"])
    out = capsys.readouterr().out

    assert "trace 1" in out
    assert "trace 2" in out
    assert "trace 3" in out
    assert "trace 4" not in out  # TRACE_SAMPLE_COUNT=3，第 4 条主诉不该出现


def test_main_reports_terminated_by_distribution(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy)
    queries_path = tmp_path / "q.txt"
    queries_path.write_text("主诉甲\n", encoding="utf-8")

    vrt.main(["--queries-path", str(queries_path), "--limit", "1"])
    out = capsys.readouterr().out

    assert "terminated_by 分布" in out
    assert "'finish': 1" in out


def test_main_returns_one_when_no_usable_records(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", lambda complaint, **kw: {
        "rejected": True, "insufficient": False, "results": [],
    })
    queries_path = tmp_path / "q.txt"
    queries_path.write_text("主诉甲\n", encoding="utf-8")

    code = vrt.main(["--queries-path", str(queries_path)])
    err = capsys.readouterr().err

    assert code == 1
    assert "没有可用样本" in err


def test_main_respects_limit(monkeypatch, tmp_path):
    seen_queries = []

    def recording_consult(complaint, **kwargs):
        seen_queries.append(complaint)
        return _fake_consult_case_layer_heavy(complaint, **kwargs)

    monkeypatch.setattr("core.chain.consult", recording_consult)
    queries_path = tmp_path / "q.txt"
    queries_path.write_text("主诉甲\n主诉乙\n主诉丙\n", encoding="utf-8")

    vrt.main(["--queries-path", str(queries_path), "--limit", "2"])
    assert seen_queries == ["主诉甲", "主诉乙"]
