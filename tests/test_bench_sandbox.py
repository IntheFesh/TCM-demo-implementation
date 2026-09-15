"""R19：沙盒性能基准脚本 + 性能那一节的凭据。

这些测试**不跑基准本身**（全量测试和 Playwright 各几十秒，跑在 pytest 里就等于
每次全量测试都套一层全量测试）。测的是：落盘形状、对照是不是成对的、
注册表里的键、还原副作用。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.bench_sandbox import (
    BASELINE_PHYSICIANS,
    HEALTH_REQUESTS,
    IMPORT_REPEATS,
    OUT_PATH,
    bench_health,
    bench_import,
    collect,
    main,
)
from scripts.collect_results import EVIDENCE

ROOT = Path(__file__).resolve().parent.parent


def test_import_is_measured_in_a_subprocess():
    """同进程里第二次 import 走 sys.modules 缓存，量出来是 0——那个 0 什么也不说明。"""
    src = (ROOT / "scripts" / "bench_sandbox.py").read_text(encoding="utf-8")
    body = src[src.index("def bench_import"):src.index("def bench_health")]
    assert "subprocess.run" in body
    assert "sys.executable" in body


def test_import_bench_reports_every_repeat_not_just_the_median():
    """只报中位数看不出抖动。三次值都在文件里，量级可疑时能自己判断。"""
    got = bench_import()
    assert got["import_api_main_repeats"] == IMPORT_REPEATS
    assert len(got["import_api_main_all_s"]) == IMPORT_REPEATS
    assert got["import_api_main_s"] > 0


def test_health_bench_measures_both_registry_sizes():
    """单报五位那个数说明不了任何事——必须有三位那一组做对照。"""
    got = bench_health()
    assert got["health_requests"] == HEALTH_REQUESTS
    assert got["health_n_physicians_five"] == 5
    assert got["health_n_physicians_three"] == len(BASELINE_PHYSICIANS) == 3
    for k in ("health_p50_ms_five", "health_p95_ms_five",
              "health_p50_ms_three", "health_p95_ms_three"):
        assert got[k] > 0, k


def test_health_bench_restores_the_registry():
    """这个脚本可能被别的东西 import。改完不还原会让后面所有读注册表的代码
    看到一份被砍掉两位的表——而且不报错。"""
    import api.main as main_mod

    before = dict(main_mod.PHYSICIANS)
    bench_health()
    assert main_mod.PHYSICIANS == before
    assert len(main_mod.PHYSICIANS) == 5


def test_health_bench_restores_the_registry_even_if_it_throws(monkeypatch):
    """还原写在 finally 里，不是在正常路径末尾。"""
    import api.main as main_mod
    import scripts.bench_sandbox as bench

    before = dict(main_mod.PHYSICIANS)
    calls = {"n": 0}

    class Boom(Exception):
        pass

    real_median = bench.statistics.median

    def flaky(vals):
        calls["n"] += 1
        # one_round 每轮调 median 一次：第一次是五位那一组，第二次是三位那一组
        # ——让第二次炸，也就是注册表已经被砍成三位、还没还原的那个时刻。
        if calls["n"] > 1:
            raise Boom("测试注入")
        return real_median(vals)

    monkeypatch.setattr(bench.statistics, "median", flaky)
    with pytest.raises(Boom):
        bench_health()
    assert main_mod.PHYSICIANS == before


def test_not_passing_all_leaves_the_slow_keys_absent(tmp_path, monkeypatch):
    """没量 ≠ 量到 0。缺键时 collect_results 会如实报「键取不到」。"""
    import scripts.bench_sandbox as bench

    monkeypatch.setattr(bench, "IMPORT_REPEATS", 1)
    monkeypatch.setattr(bench, "HEALTH_REQUESTS", 3)
    out = collect(run_all=False)
    for k in ("pytest_passed", "pytest_wall_s", "playwright_states_passed"):
        assert k not in out, f"{k} 不该在没跑 --all 的结果里"
    assert out["ran_all"] is False
    assert "import_api_main_s" in out and "health_p50_ms_five" in out


def test_the_output_is_not_merged_with_a_previous_run(tmp_path, monkeypatch):
    """合并的话，`--all` 跑过一次之后再跑一次不带 --all 的，文件里会留着上一次的
    pytest_wall_s，而它量的是另一份代码——一个看起来是这次的、实际是上次的数。"""
    import scripts.bench_sandbox as bench

    monkeypatch.setattr(bench, "IMPORT_REPEATS", 1)
    monkeypatch.setattr(bench, "HEALTH_REQUESTS", 3)
    out = tmp_path / "sandbox.json"
    out.write_text(json.dumps({"pytest_passed": 999, "pytest_wall_s": 1.0}),
                   encoding="utf-8")
    assert main(["--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "pytest_passed" not in data, "旧的 pytest 数被带过来了"


def test_show_on_a_missing_file_exits_nonzero(tmp_path, capsys):
    assert main(["--show", "--out", str(tmp_path / "nope.json")]) == 1
    assert "还没量过" in capsys.readouterr().out


def test_every_bench_number_has_an_evidence_key():
    """性能那一节的每个数都要能被 --check 核对，或者标 ⏳。"""
    for key in ("bench.import_api_main_s", "bench.health_p50_ms_five",
                "bench.health_p50_ms_three", "bench.pytest_passed",
                "bench.pytest_skipped", "bench.pytest_failed",
                "bench.pytest_wall_s", "bench.playwright_states_passed",
                "bench.playwright_wall_s"):
        assert key in EVIDENCE, key
        assert EVIDENCE[key][0] == "bench/sandbox.json"


def test_the_committed_bench_file_is_where_the_registry_says():
    assert OUT_PATH.parent.name == "bench"
    assert OUT_PATH.parent.parent.name == "eval"


def test_results_md_performance_section_pairs_five_with_three():
    """P2/P3 是一对。只留 P2 那一行的话，「多两位医家的代价」这个问题就
    没有基准了——而这一轮的实测结论恰恰是"这个代价量不出来"。"""
    text = (ROOT / "eval" / "RESULTS.md").read_text(encoding="utf-8")
    sect = text[text.index("## 性能：沙盒能量的四项"):]
    sect = sect[:sect.index("\n## ")]
    assert "bench.health_p50_ms_five" in sect
    assert "bench.health_p50_ms_three" in sect
    # 两次测量方向相反这件事必须写在里面
    assert "方向相反" in sect
    assert "落在测量噪声里" in sect


def test_results_md_performance_section_marks_the_unmeasurable_ones():
    """热启动 / 一次问诊 / ε 两套都要真机。标 ⏳ 并附上机命令，不编一个数。"""
    text = (ROOT / "eval" / "RESULTS.md").read_text(encoding="utf-8")
    sect = text[text.index("## 性能：沙盒能量的四项"):]
    sect = sect[:sect.index("\n## ")]
    for row, cmd in (("热启动", "scripts.bench_startup"),
                     ("一次问诊", "scripts.bench_consult"),
                     ("ε 两套设置", "epsilon_s3_disabled.json")):
        line = next(ln for ln in sect.splitlines() if row in ln)
        assert "⏳" in line, f"{row} 那一行没标 ⏳"
        assert cmd in line, f"{row} 那一行没给上机命令"


def test_replay_scenario_count_is_pinned():
    """段 6 的验收判据是「23/23 全部命中」。这个 23 写在剧本、README、报告三处，
    从代码里数一遍——不然改了场景表三处文档一起过期。"""
    from scripts.record_fixtures import build_plan

    assert len(build_plan()) == 23
    onsite = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    seg6 = next(ln for ln in onsite.splitlines() if ln.startswith('  "6|'))
    assert "278" in seg6, "段 6 的预估调用数跟 record_fixtures --dry-run 的 278 对不上"
