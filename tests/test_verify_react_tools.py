"""scripts/verify_react_tools.py 的离线测试：monkeypatch core.chain.consult
（collect_react_process_samples 惰性 import 的就是这个名字），不需要真实
LLM/cases.json——那部分（P1 验证）留给 AutoDL 跑这个脚本本身。

1.1b 起判据是两道闸门：医案层占比 > 35% **且**医案层返回非空率 > 50%。
上一轮只看占比，9 次医案层调用全空也能过 55%——这里的假 consult 因此
必须给出真实形状的 observation（json.dumps 之后的文本，跟 core/react.py
一致），空/非空是靠解析它判的。
"""
import json
from types import SimpleNamespace

from scripts import verify_react_tools as vrt

NONEMPTY_SEARCH = json.dumps({"available": True, "cases": [{"case_id": "ye_tianshi-0001-p1-0"}]},
                             ensure_ascii=False)
EMPTY_SEARCH = json.dumps({"available": True, "cases": []}, ensure_ascii=False)
NONEMPTY_TRIPLES = json.dumps({"available": True, "total_matched": 1,
                               "triples": [{"case_id": "x", "s": "脘痛", "p": "治以", "o": "疏肝"}]},
                              ensure_ascii=False)
EMPTY_TRIPLES = json.dumps({"available": True, "total_matched": 0, "triples": []}, ensure_ascii=False)


def _step(step, action, action_input=None, thought="t", observation="{}"):
    return SimpleNamespace(step=step, action=action, action_input=action_input or {},
                           thought=thought, observation=observation)


def _trace(steps, terminated_by="finish"):
    return SimpleNamespace(steps=steps, terminated_by=terminated_by, llm_calls=len(steps))


def _outcome(complaint, steps):
    return {
        "rejected": False, "insufficient": False,
        "results": [{"physician": "ye_tianshi", "react_trace": _trace(steps)}],
    }


def _fake_consult_case_layer_heavy(complaint, **kwargs):
    """4 次动作里 2 次是医案层（50% > 35%），且两次都返回了数据（非空率 100%）。"""
    return _outcome(complaint, [
        _step(1, "search_cases", {"query": complaint, "physician": "ye_tianshi"},
              observation=NONEMPTY_SEARCH),
        _step(2, "query_case_graph", {"symptom": complaint}, observation=NONEMPTY_TRIPLES),
        _step(3, "lookup_standard", {"query": "SP-01"}),
        _step(4, "finish"),
    ])


def _fake_consult_case_layer_light(complaint, **kwargs):
    """医案层 0 次——占比那道闸门就过不了。"""
    return _outcome(complaint, [
        _step(1, "query_graph", {"node": "纳呆"}),
        _step(2, "lookup_standard", {"query": "SP-01"}),
        _step(3, "finish"),
    ])


def _fake_consult_case_layer_heavy_but_all_empty(complaint, **kwargs):
    """1.1b 抓到的那个形状：占比够（2/4=50%），但医案层全部返回空——上一轮
    的判据会放它过闸门，新判据必须拦住。"""
    return _outcome(complaint, [
        _step(1, "search_cases", {"query": complaint, "physician": "叶天士"},
              observation=EMPTY_SEARCH),
        _step(2, "query_case_graph", {"symptom": complaint, "physician": "叶天士"},
              observation=EMPTY_TRIPLES),
        _step(3, "lookup_standard", {"query": "SP-01"}),
        _step(4, "finish"),
    ])


def _queries(tmp_path, n=1):
    path = tmp_path / "q.txt"
    path.write_text("".join(f"主诉{i}\n" for i in range(n)), encoding="utf-8")
    return str(path)


# ---------- _case_layer_returned_data：纯函数 ----------


def test_case_layer_returned_data_distinguishes_empty_and_nonempty():
    assert vrt._case_layer_returned_data("search_cases", NONEMPTY_SEARCH) is True
    assert vrt._case_layer_returned_data("search_cases", EMPTY_SEARCH) is False
    assert vrt._case_layer_returned_data("query_case_graph", NONEMPTY_TRIPLES) is True
    assert vrt._case_layer_returned_data("query_case_graph", EMPTY_TRIPLES) is False


def test_case_layer_returned_data_is_none_for_non_case_layer_tools():
    assert vrt._case_layer_returned_data("lookup_standard", '{"found": true}') is None
    assert vrt._case_layer_returned_data("finish", "（模型判断证据已足够，结束取证）") is None


def test_case_layer_returned_data_handles_truncated_observation():
    """core/react.py 会把过长的 observation 截断成非法 JSON——截断只发生在
    返回很长的时候，那正说明有数据，按文本里有没有 case_id 判。"""
    truncated = NONEMPTY_SEARCH[:40] + "…（结果过长，已截断，共 999 字）"
    assert vrt._case_layer_returned_data("search_cases", truncated) is True
    assert vrt._case_layer_returned_data("search_cases", "not json at all") is False


def test_case_layer_returned_data_error_payload_counts_as_empty():
    """参数错了（1.1b 之后工具返回 error + 空列表）不算"返回了数据"。"""
    err = json.dumps({"available": True, "cases": [], "error": "physician='华佗' 不是已注册的医家"},
                     ensure_ascii=False)
    assert vrt._case_layer_returned_data("search_cases", err) is False


# ---------- main：两道闸门 ----------


def test_main_returns_zero_when_both_gates_pass(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy)
    code = vrt.main(["--queries-path", _queries(tmp_path), "--limit", "1"])
    out = capsys.readouterr().out

    assert code == 0
    assert "★命中" in out
    assert "search_cases         调用 1 次，其中返回非空 1 次（1/1）" in out
    assert "query_case_graph     调用 1 次，其中返回非空 1 次（1/1）" in out
    assert "合计返回非空率：2/2 = 100.0%" in out


def test_main_returns_one_when_case_layer_ratio_below_gate(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_light)
    code = vrt.main(["--queries-path", _queries(tmp_path), "--limit", "1"])
    err = capsys.readouterr().err

    assert code == 1
    assert "✗ 未命中" in err
    assert "医案层占比" in err


def test_main_returns_one_when_case_layer_ratio_passes_but_returns_are_all_empty(
    monkeypatch, tmp_path, capsys,
):
    """1.1b 的核心判据：占比 50% 过了第一道闸门，但 0/2 非空——必须判未命中，
    而且 stderr 要说清楚是哪道闸门没过。"""
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy_but_all_empty)
    code = vrt.main(["--queries-path", _queries(tmp_path), "--limit", "1"])
    captured = capsys.readouterr()

    assert code == 1
    assert "合计返回非空率：0/2 = 0.0%" in captured.out
    assert "返回非空率" in captured.err
    assert "医案层占比" not in captured.err  # 占比那道是过了的，不能冤枉它


def test_main_prints_top_n_full_traces(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy)
    vrt.main(["--queries-path", _queries(tmp_path, n=4), "--limit", "4"])
    out = capsys.readouterr().out

    assert "trace 1" in out and "trace 2" in out and "trace 3" in out
    assert "trace 4" not in out  # TRACE_SAMPLE_COUNT=3，第 4 条主诉不该出现


def test_main_reports_terminated_by_distribution(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", _fake_consult_case_layer_heavy)
    vrt.main(["--queries-path", _queries(tmp_path), "--limit", "1"])
    out = capsys.readouterr().out

    assert "terminated_by 分布" in out
    assert "'finish': 1" in out


def test_main_returns_one_when_no_usable_records(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("core.chain.consult", lambda complaint, **kw: {
        "rejected": True, "insufficient": False, "results": [],
    })
    code = vrt.main(["--queries-path", _queries(tmp_path)])
    err = capsys.readouterr().err

    assert code == 1
    assert "没有可用样本" in err


def test_main_respects_limit(monkeypatch, tmp_path):
    seen_queries = []

    def recording_consult(complaint, **kwargs):
        seen_queries.append(complaint)
        return _fake_consult_case_layer_heavy(complaint, **kwargs)

    monkeypatch.setattr("core.chain.consult", recording_consult)
    vrt.main(["--queries-path", _queries(tmp_path, n=3), "--limit", "2"])
    assert seen_queries == ["主诉0", "主诉1"]
