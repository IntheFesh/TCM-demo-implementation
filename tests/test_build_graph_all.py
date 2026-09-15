"""R12-E：`python -m offline.build_graph --all` 一条命令跑完建图三步。

三步是一件事：build_graph → graph_stats（**写回**医家层权重）→ build_element_index。
漏一步不报错，但 λ1 全为 0、追问的后验退化成先验、graph 检索模式不可用——**而且
都不报错**。这个坑 SOURCES.md 记过，README 第 5 步旁边也有一段警告；R12 把它变成
一个默认就不会踩的命令。
"""
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from offline import build_graph


def _run(argv, out=None):
    """跑一次 main，把 stdout/stderr 收上来。out 不传就写临时文件。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        build_graph.main(argv)
    return stdout.getvalue(), stderr.getvalue()


def test_all_runs_the_two_downstream_steps(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("offline.graph_stats.main", lambda argv: calls.append("graph_stats"))
    monkeypatch.setattr("offline.build_element_index.main",
                        lambda argv: calls.append("element_index"))
    out, _err = _run(["--cases-path", "/dev/null", "--out", str(tmp_path / "g.json")])
    assert calls == [], "不加 --all 不该跑后两步"
    out, _err = _run(["--all", "--cases-path", "/dev/null", "--out", str(tmp_path / "g.json")])
    assert calls == ["graph_stats", "element_index"], "顺序不能乱：权重要先写回去"
    assert "graph_stats" in out and "build_element_index" in out


def test_running_build_graph_alone_warns_about_the_silent_degradation(tmp_path):
    """单跑时的警告必须点名**三个具体后果**，不能只说"建议加 --all"——
    一句没有后果的建议没人会当回事。"""
    _out, err = _run(["--cases-path", "/dev/null", "--out", str(tmp_path / "g.json")])
    assert "⚠" in err
    for consequence in ("λ1", "先验", "graph 检索模式", "不报错", "--all"):
        assert consequence in err, consequence


def test_a_failing_downstream_step_stops_the_rest_and_propagates(tmp_path, monkeypatch):
    """"图建好了但权重没写回"这种半成品比彻底失败更危险：后面每一步都能跑，
    只是结果悄悄退化。所以任一步失败就往上抛，不吞、不继续。"""
    later = []

    def boom(argv):
        raise RuntimeError("graph_stats 挂了")

    monkeypatch.setattr("offline.graph_stats.main", boom)
    monkeypatch.setattr("offline.build_element_index.main", lambda argv: later.append(1))
    with pytest.raises(RuntimeError, match="graph_stats 挂了"):
        _run(["--all", "--cases-path", "/dev/null", "--out", str(tmp_path / "g.json")])
    assert later == [], "前一步挂了就不该继续跑后一步"


def test_a_downstream_sys_exit_is_also_treated_as_failure(tmp_path, monkeypatch):
    """下游脚本用 SystemExit 报错（argparse 就是这么退的）也算失败，
    不能因为它不是 Exception 就漏过去。"""
    monkeypatch.setattr("offline.graph_stats.main",
                        lambda argv: (_ for _ in ()).throw(SystemExit(2)))
    with pytest.raises(SystemExit):
        _run(["--all", "--cases-path", "/dev/null", "--out", str(tmp_path / "g.json")])


def test_readme_tells_people_to_use_all(tmp_path):
    """文档和代码得说同一件事：README 的复现步骤里必须是 `--all` 那条命令。"""
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert "offline.build_graph --all" in readme
