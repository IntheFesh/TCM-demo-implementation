"""`scripts/onsite_plan.py` 的测试：段表里那一格「预估调用数」怎么解析。

这个模块存在的理由就是"别把一个文件里的数抄到剧本里"，所以测试的重点是：
现读的确实是那份文件（换一份文件，解析结果跟着变），以及读不出来的时候
**喊出来而不是静默给个数**。
"""
import json

import pytest

from scripts.onsite_plan import (
    REPO_ROOT,
    EPSILON_STAGES,
    epsilon_llm_calls,
    main,
    resolve_calls,
)


def _epsilon_doc(online: int = 148, s2: int = 37, extract: int = 30) -> dict:
    return {
        "epsilon_online": {"llm_calls": online},
        "epsilon_s2": {"llm_calls": s2},
        "epsilon_extract": {"llm_calls": extract},
    }


def _write(root, data: dict) -> None:
    (root / "eval").mkdir(parents=True, exist_ok=True)
    (root / "eval" / "epsilon.json").write_text(json.dumps(data), encoding="utf-8")


def test_epsilon_llm_calls_sums_the_three_stages():
    assert epsilon_llm_calls(_epsilon_doc(148, 37, 30)) == 215


def test_epsilon_llm_calls_counts_a_missing_stage_as_zero():
    """中途挂掉的那次跑只花了跑到的那几段的钱——缺的段按 0 补比按上一次的数补
    更接近事实（也免得 KeyError 把整张清单打坏）。"""
    assert epsilon_llm_calls({"epsilon_online": {"llm_calls": 148}}) == 148
    assert epsilon_llm_calls({"epsilon_online": None, "epsilon_s2": {}}) == 0


def test_every_epsilon_stage_key_is_one_the_real_file_has():
    """三个阶段 key 不是手写的常量，要跟仓库里那份文件对得上。"""
    real = json.loads((REPO_ROOT / "eval" / "epsilon.json").read_text(encoding="utf-8"))
    for stage in EPSILON_STAGES:
        assert stage in real, stage


def test_a_plain_cell_is_just_its_own_number():
    assert resolve_calls("278") == 278
    assert resolve_calls(" 0 ") == 0


def test_an_auto_cell_reads_the_file_at_call_time(tmp_path):
    """同一个段表格子，文件里的数变了，解析结果就变——这正是不写死的意义。"""
    _write(tmp_path, _epsilon_doc(148, 37, 30))
    assert resolve_calls("auto:eval/epsilon.json", root=tmp_path) == 215
    _write(tmp_path, _epsilon_doc(146, 36, 30))
    assert resolve_calls("auto:eval/epsilon.json", root=tmp_path) == 212


def test_an_unknown_auto_path_fails_loudly_and_lists_the_known_ones(tmp_path):
    """剧本里把路径拼错，要立刻报错而不是静默拿到 0。"""
    with pytest.raises(ValueError) as e:
        resolve_calls("auto:eval/epsilon.jsonl", root=tmp_path)
    assert "eval/epsilon.json" in str(e.value)


def test_a_missing_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        resolve_calls("auto:eval/epsilon.json", root=tmp_path)


def test_cli_prints_the_resolved_number(capsys):
    assert main(["--calls", "60"]) == 0
    assert capsys.readouterr().out.strip() == "60"


def test_cli_still_prints_a_number_when_resolution_fails_but_warns_and_exits_nonzero(capsys, monkeypatch, tmp_path):
    """调用方（剧本的 print_plan）拿这个数做算术，没数会把整张清单打坏。所以
    stdout 照样给 0、原因喊到 stderr、退出码非 0——清单上那一段显示 0 次调用，
    人一眼能看出不对。"""
    monkeypatch.setattr("scripts.onsite_plan.REPO_ROOT", tmp_path)
    assert main(["--calls", "auto:eval/epsilon.json"]) == 3
    out = capsys.readouterr()
    assert out.out.strip() == "0"
    assert "解析失败" in out.err and "epsilon.json" in out.err
