"""scripts/verify_role_fill.py 的离线测试。

脚本本身要真实 LLM 才有意义（填充率是真实产出的性质），但**闸门判断逻辑必须
在这里测**——不然"闸门写错了导致永远绿"这种问题只能在 AutoDL 上撞到，而那正是
这个脚本要替我们发现问题的场合（跟 tests/test_verify_local_backend.py 同一条
理由）。假 consult 给出几种真实会出现的形状：全标、半标、全不标、调用失败。
"""
from unittest import mock

from core.schemas import FormulaCandidate, HerbItem, S3Syndrome
from scripts import verify_role_fill as vrf


def _s3(specs, formula="柴胡疏肝散") -> S3Syndrome:
    """specs: (药名, role, function, dose) 四元组。"""
    return S3Syndrome(
        syndrome="肝胃不和证", reasoning="x", treatment_principle="疏肝和胃",
        formula_candidates=[FormulaCandidate(
            name=formula, source="classic", confidence="high", rationale="x",
            herb_items=[HerbItem(name=n, role=r, function_in_formula=f, dose=d)
                        for n, r, f, d in specs],
        )],
        cited_case_ids=["ye_tianshi-001"],
    )


FULLY_ROLED = [("半夏", "君", "降逆", 9.0), ("茯苓", "臣", "健脾", 12.0),
               ("神曲", "佐", "消食", 9.0)]
HALF_ROLED = [("半夏", "君", "降逆", 9.0), ("茯苓", None, None, None),
              ("神曲", None, None, None)]
NONE_ROLED = [("半夏", None, None, None), ("茯苓", None, None, None)]


def _consult(specs_by_physician: dict[str, list]):
    def fake(complaint, **kwargs):
        return {"rejected": False, "insufficient": False,
                "results": [{"physician": p, "s3": _s3(specs)}
                            for p, specs in specs_by_physician.items()]}
    return fake


def _queries(tmp_path, n=1):
    path = tmp_path / "q.txt"
    path.write_text("".join(f"主诉{i}\n" for i in range(n)), encoding="utf-8")
    return str(path)


def _run(monkeypatch, tmp_path, consult_fn, n=1, extra_argv=()):
    monkeypatch.setattr("core.chain.consult", consult_fn)
    return vrf.main(["--queries-path", _queries(tmp_path, n), "--limit", str(n), *extra_argv])


# ---------- summarize：纯函数 ----------


def test_summarize_computes_per_physician_and_overall_rates():
    samples = [
        {"physician": "ye_tianshi", "n_items": 4, "n_role": 4, "n_function": 2, "n_dose": 1,
         "n_total_set": 4, "n_core_set": 2, "n_adjunct_set": 2, "roles": [], "query": "q",
         "formula": "甲"},
        {"physician": "wu_jutong", "n_items": 6, "n_role": 3, "n_function": 6, "n_dose": 0,
         "n_total_set": 6, "n_core_set": 1, "n_adjunct_set": 2, "roles": [], "query": "q",
         "formula": "乙"},
    ]
    s = vrf.summarize(samples)
    assert s["by_physician"]["ye_tianshi"]["role_fill"] == 1.0
    assert s["by_physician"]["wu_jutong"]["role_fill"] == 0.5
    # 合计是按药味数加权的（4+3)/(4+6)，不是两位医家各自率的算术平均（0.75）
    assert s["overall"]["role_fill"] == 0.7
    assert s["overall"]["mean_herbs"] == 5.0
    assert s["overall"]["mean_core"] == 1.5


def test_summarize_returns_none_not_zero_for_an_empty_denominator():
    """一味药都没有 ≠ 一味都没填 role。0.0 会被读成"全都没标"，
    跟分层 Jaccard 空层返 None 同一条理由。"""
    samples = [{"physician": "ye_tianshi", "n_items": 0, "n_role": 0, "n_function": 0,
                "n_dose": 0, "n_total_set": 0, "n_core_set": 0, "n_adjunct_set": 0,
                "roles": [], "query": "q", "formula": None}]
    assert vrf.summarize(samples)["overall"]["role_fill"] is None


# ---------- 采集：西药与占位符 ----------


def test_collect_excludes_western_drugs_from_the_denominator(monkeypatch):
    """分层指标不把西药算进任何一层，填充率的分母要跟它一致——否则这个率
    跟它要验证的那个指标不是在说同一批药。"""
    specs = [("黄芪", "君", "补气", 30.0), ("阿斯匹林", None, None, None)]
    monkeypatch.setattr("core.chain.consult", _consult({"zhang_xichun": specs}))
    got = vrf.collect_role_fill_samples(["主诉"])
    assert got["samples"][0]["n_items"] == 1
    assert got["samples"][0]["n_role"] == 1


def test_collect_counts_dose_and_function_separately(monkeypatch):
    """dose / function_in_formula 是 M1 加的、从没在真实产出上核过的两个字段，
    要跟 role 分开报（它们不参与闸门，但"是不是摆设"这件事要有数）。"""
    monkeypatch.setattr("core.chain.consult", _consult({"ye_tianshi": HALF_ROLED}))
    sample = vrf.collect_role_fill_samples(["主诉"])["samples"][0]
    assert (sample["n_items"], sample["n_role"], sample["n_function"], sample["n_dose"]) \
        == (3, 1, 1, 1)


def test_collect_tolerates_one_failing_complaint(monkeypatch):
    """真实 API 会抖动：一条主诉失败不能让整批停下，失败要计数（跟
    offline/estimate_epsilon.py 同一个模式）。"""
    from core.llm import LLMError

    calls = {"n": 0}

    def flaky(complaint, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("模拟：限流")
        return _consult({"ye_tianshi": FULLY_ROLED})(complaint)

    monkeypatch.setattr("core.chain.consult", flaky)
    got = vrf.collect_role_fill_samples(["主诉甲", "主诉乙"])
    assert got["n_failed"] == 1
    assert len(got["samples"]) == 1


def test_collect_separates_rejected_and_insufficient_from_failures(monkeypatch):
    """被安全否决 / 信息不足是 consult() 跑成了、结论如实是"不开方"，跟调用
    没跑成不是一回事，不能混成同一个计数。"""
    def mixed(complaint, **kwargs):
        if complaint == "主诉0":
            return {"rejected": True, "insufficient": False, "results": []}
        return {"rejected": False, "insufficient": True, "results": []}

    monkeypatch.setattr("core.chain.consult", mixed)
    got = vrf.collect_role_fill_samples(["主诉0", "主诉1"])
    assert (got["n_rejected"], got["n_insufficient"], got["n_failed"]) == (1, 1, 0)


# ---------- 闸门：三个退出码 ----------


def test_main_returns_zero_when_role_fill_is_above_gate(monkeypatch, tmp_path, capsys):
    code = _run(monkeypatch, tmp_path, _consult({"ye_tianshi": FULLY_ROLED}))
    out = capsys.readouterr().out
    assert code == 0
    assert "★命中" in out
    assert "100.0%" in out


def test_main_returns_one_when_role_fill_is_below_gate(monkeypatch, tmp_path, capsys):
    """**这条是这个脚本存在的理由**：填充率 1/3 时必须判未命中，并且
    stderr 要写明"R1-2 的分层指标要等 prompt 修好再用"这条依赖。"""
    code = _run(monkeypatch, tmp_path, _consult({"ye_tianshi": HALF_ROLED}))
    err = capsys.readouterr().err
    assert code == 1
    assert "✗ 未命中" in err
    assert "33.3%" in err
    assert "分层指标要等 prompt 修好再用" in err
    assert "s3_syndrome.yaml" in err  # 要说清去哪儿改


def test_main_returns_one_when_nothing_is_roled(monkeypatch, tmp_path, capsys):
    code = _run(monkeypatch, tmp_path, _consult({"ye_tianshi": NONE_ROLED}))
    assert code == 1
    assert "0.0%" in capsys.readouterr().err


def test_main_returns_two_when_there_is_nothing_to_measure(monkeypatch, tmp_path, capsys):
    """全部被拦截 → 退出码 2（没测出来），不是 1（测了没过）。CI 里这两件事
    要能分开：一个是 prompt 要改，一个是这批主诉根本不该用来测。"""
    code = _run(monkeypatch, tmp_path,
                lambda complaint, **kw: {"rejected": True, "insufficient": False, "results": []})
    err = capsys.readouterr().err
    assert code == 2
    assert "没测出来" in err


def test_main_gate_is_exactly_ninety_percent(monkeypatch, tmp_path):
    """边界：正好 90% 算过（>=，不是 >）。闸门的方向写反过一次代价很大，
    用刚好压在线上的输入钉住。"""
    assert vrf.ROLE_FILL_GATE == 0.90
    nine_of_ten = [("半夏", "君", None, None)] * 9 + [("桑叶", None, None, None)]
    assert _run(monkeypatch, tmp_path, _consult({"ye_tianshi": nine_of_ten})) == 0
    eight_of_ten = [("半夏", "君", None, None)] * 8 + [("桑叶", None, None, None)] * 2
    assert _run(monkeypatch, tmp_path, _consult({"ye_tianshi": eight_of_ten})) == 1


def test_main_reports_mean_herb_counts_for_the_r1_3_comparison(monkeypatch, tmp_path, capsys):
    """R1-3 的佐使克制约束要靠"平均药味数"辨真假改善：ε 降了而药味数从 9 掉到
    5，那是"药少了所以碰巧一样"。这三个均值必须在输出里。"""
    _run(monkeypatch, tmp_path, _consult({"ye_tianshi": FULLY_ROLED}))
    out = capsys.readouterr().out
    assert "均药味" in out and "均君臣" in out and "均佐使" in out


def test_main_respects_limit(monkeypatch, tmp_path):
    seen = []

    def recording(complaint, **kwargs):
        seen.append(complaint)
        return _consult({"ye_tianshi": FULLY_ROLED})(complaint)

    monkeypatch.setattr("core.chain.consult", recording)
    vrf.main(["--queries-path", _queries(tmp_path, 3), "--limit", "2"])
    assert seen == ["主诉0", "主诉1"]


def test_runnable_as_a_single_module_command():
    """上机清单里这一条是 `python -m scripts.verify_role_fill`——脚本必须能
    以这个形式跑起来（有 __main__ 入口、import 路径自洽），不能要求对方先
    记住 PYTHONPATH 怎么设。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(vrf.__file__).resolve().parent.parent
    proc = subprocess.run([sys.executable, "-m", "scripts.verify_role_fill", "--help"],
                          capture_output=True, text=True, cwd=root, timeout=60)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "role" in proc.stdout


def test_summarize_is_explicit_about_its_denominator():
    """汇总里必须把分母（药味数）和样本数一起报出来，不能只给一个率——一个
    没有分母的百分比在这个项目里等于没有意义（CLAUDE.md「任何数字都必须带
    对照」）。输出表格也是照这两个字段打的表头。"""
    samples = [{"physician": "p", "n_items": 4, "n_role": 2, "n_function": 0, "n_dose": 0,
                "n_total_set": 4, "n_core_set": 1, "n_adjunct_set": 1, "roles": [],
                "query": "q", "formula": "甲"}]
    agg = vrf.summarize(samples)["overall"]
    assert agg["n_items"] == 4 and agg["n_samples"] == 1
    assert agg["role_fill"] == 0.5


def test_script_does_not_touch_react_or_followup():
    """跟 estimate_epsilon_online 同一条隔离：这里量的是 prompt 让模型填了多少
    字段，ReAct/追问是额外变量，混进来就说不清是哪一边的影响。"""
    import inspect

    src = inspect.getsource(vrf.collect_role_fill_samples)
    assert "use_react=False" in src and "ask_fn=None" in src


def test_consult_is_called_with_the_default_isolated_path(monkeypatch):
    """上一条只看源码文本，这一条真的跑一次、看传下去的关键字——源码里写了
    但传错参数名的话上一条抓不到。"""
    seen = {}

    def recording(complaint, **kwargs):
        seen.update(kwargs)
        return _consult({"ye_tianshi": FULLY_ROLED})(complaint)

    monkeypatch.setattr("core.chain.consult", recording)
    vrf.collect_role_fill_samples(["主诉"])
    assert seen == {"use_react": False, "ask_fn": None}


def test_mock_patch_target_is_the_real_consult():
    """这个文件全靠 monkeypatch core.chain.consult 生效——脚本必须是在函数里
    惰性 import 它（`from core.chain import consult`），不是在模块顶层绑定。
    顶层绑定的话补丁打不上，所有测试会假绿。"""
    import inspect

    src = inspect.getsource(vrf.collect_role_fill_samples)
    assert "from core.chain import consult" in src
    with mock.patch("core.chain.consult") as patched:
        patched.return_value = {"rejected": True, "insufficient": False, "results": []}
        got = vrf.collect_role_fill_samples(["主诉"])
    assert got["n_rejected"] == 1
