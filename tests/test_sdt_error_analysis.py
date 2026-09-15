"""R2-1 失分分析 + R2-3 过拟合护栏的离线测试。

**这个文件里每一处碰台账的测试都必须 monkeypatch `runlog.LOG_PATH` 到
tmp_path。** `eval/sdt/test_run_log.jsonl` 是进版本控制的真实台账（Test 集被
跑过几次的唯一凭据），跑一次 pytest 就往里塞几行假记录的话，那份凭据就废了。

真实数据上的分析留给真机（`--error-analysis out/sdt_chain_v2.txt`）：这里造一份
最小 SDT 目录 + 一份提交文件 + 一个**按官方文档描述重建的** score_proportional
桩。桩只用来验聚合和排版的接线，**计分数学本身不在这里测**——官方公式在
TCMEval 仓库里，我们不复制它（复制了就不可比），所以"多选和少选各值多少分"
这个结论只能在有官方脚本的机器上得出，不能从这个桩的行为推出来。
"""
import json

import pytest

from eval.sdt import error_analysis as ea
from eval.sdt import run as sdt_run
from eval.sdt import runlog

# (id, 临床资料, 病机选项, 病机金标准, 证型选项, 证型金标准, 模型病机, 模型证型)
RECS = [
    ("病例1", "女性50岁。呃逆嗳气频作，两胁胀满，纳呆。舌苔薄白，脉弦。",
     "A:肝气横逆;D:胃气不降;J:食滞中焦", "D", "A:肝胃不和;B:脾胃受制", "A", "A;D;J", "A"),
    ("病例2", "男性40岁。胃脘灼痛，口干喜冷饮，便干。舌红少津，脉细数。",
     "A:胃热炽盛;B:胃阴不足;C:寒凝气滞", "A;B", "A:胃热证;B:胃阴虚证", "B", "A", "B"),
    ("病例3", "女性62岁。食少腹胀，大便溏薄，倦怠乏力。舌淡苔白，脉缓弱。",
     "A:脾失健运;B:湿浊内阻;C:肝气横逆", "A;B", "A:脾虚湿困;B:肝胃不和", "A", "A;B;C", "A;B"),
    ("病例4", "男性55岁。胸脘痞闷，呕吐痰涎，头晕目眩。舌苔白滑，脉弦滑。",
     "A:痰湿中阻;B:胃气上逆;C:胃热炽盛", "A;B", "A:痰饮内停;B:胃热证", "A", "", ""),
]

# 按 eval/sdt/score.py 文档里对官方行为的描述重建：correct / (len(ref) + wrong)。
# **这是桩，不是官方公式的副本**——它只需要满足"错选拉低分母、漏选拿不到那份分"
# 这两条可观察性质，好让聚合层的接线能被验证。
STUB_SCORER = (
    "def automated_score(gold_path, sub_path):\n"
    "    return 1.75\n"
    "def clinical_info_extraction_eval(sub, ref):\n"
    "    return 0.5\n"
    "def rouge_l(a, b):\n"
    "    return 0.4\n"
    "def score_proportional(sub, ref, m):\n"
    "    sub = [x for x in sub if x]; ref = [x for x in ref if x]\n"
    "    correct = len(set(sub) & set(ref)); wrong = len(set(sub) - set(ref))\n"
    "    return correct / (len(ref) + wrong) if ref else 0.0\n"
)


def _write_sdt(tmp_path, split="Test", submitted=None, bom=False):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "Results").mkdir(exist_ok=True)
    data = [{
        "Medical Record ID": r[0], "Clinical Data": r[1],
        "Options of TCM Pathogenesis": r[2], "Options of TCM Syndrome": r[4],
        "Clinical Information": "", "Answers of TCM Pathogenesis": "",
        "Answers of TCM Syndrome": "", "Explanatory Summary": "",
        "Syndrome Differentiation": "",
    } for r in RECS]
    (tmp_path / "data" / f"{split}_TCM_Data_v1.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    gold = [("﻿" if (bom and i == 0) else "")
            + "@".join([r[0], "症状甲;症状乙", r[3], r[5], "辨证小结原文"])
            for i, r in enumerate(RECS)]
    (tmp_path / "Results" / f"{split}_data_result.txt").write_text(
        "\n".join(gold) + "\n", encoding="utf-8")
    (tmp_path / "evaluate.py").write_text(STUB_SCORER, encoding="utf-8")
    chosen = submitted or {r[0]: (r[6], r[7]) for r in RECS}
    sub = tmp_path / "sub.txt"
    sub.write_text("\n".join(
        "@".join([r[0], "症状甲;症状乙", chosen[r[0]][0], chosen[r[0]][1], "模型写的小结"])
        for r in RECS if r[0] in chosen) + "\n", encoding="utf-8")
    return sub


@pytest.fixture(autouse=True)
def _isolate_runlog(tmp_path, monkeypatch):
    """台账一律写到 tmp_path。见模块文档字符串——真实台账不能被测试污染。"""
    monkeypatch.setattr(runlog, "LOG_PATH", tmp_path / "isolated_log.jsonl")


# ---------- 1. 零 LLM 调用 ----------


def test_error_analysis_makes_no_llm_calls(tmp_path, monkeypatch):
    """**这条是 --error-analysis 存在的前提**：分数早就算出来了，重新聚合必须
    零成本。把 get_llm / get_backend 都换成"一被调用就炸"，跑通说明这条路上
    确实没有任何 LLM 调用。"""
    import core.llm as llm_mod

    def boom(*a, **kw):
        raise AssertionError("--error-analysis 模式不许发起任何 LLM 调用")

    monkeypatch.setattr(llm_mod, "get_llm", boom)
    monkeypatch.setattr(llm_mod, "get_backend", boom)
    sub = _write_sdt(tmp_path)
    sdt_run.main(["--sdt-dir", str(tmp_path), "--split", "Test",
                  "--error-analysis", str(sub)])


def test_error_analysis_does_not_construct_a_solver(tmp_path, monkeypatch):
    """连 solver 都不该构造：ChainSolver 的 __init__ 现在很轻，但它是通往
    S1/S2 的入口，"顺手构造一个"这种写法迟早会带出调用。"""
    monkeypatch.setattr(sdt_run, "SOLVERS", {
        "chain": lambda: (_ for _ in ()).throw(AssertionError("不该构造 solver")),
    })
    sub = _write_sdt(tmp_path)
    sdt_run.main(["--sdt-dir", str(tmp_path), "--split", "Test",
                  "--error-analysis", str(sub)])


def test_error_analysis_writes_no_submission_file(tmp_path):
    """只看 .txt：动态加载官方桩脚本会顺手造一个 __pycache__，那不是这条
    要管的事。提交文件是 .txt，一个都不该新增。"""
    sub = _write_sdt(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.txt"))
    sdt_run.main(["--sdt-dir", str(tmp_path), "--split", "Test",
                  "--error-analysis", str(sub)])
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.txt")) == before


# ---------- 2. 多选 / 少选 ----------


def _analysis(tmp_path, **kw):
    sub = _write_sdt(tmp_path, **kw)
    return ea.analyze(tmp_path, kw.get("split", "Test"), sub)


def test_over_and_under_selection_are_reported_separately(tmp_path):
    """多选率和少选率必须分开报——官方计分对两者的处理不对称（到底怎么不
    对称由边际代价那两个数量出来），合成一个"选择错误率"就把判据丢了。

    造的数据：病例1 病机多选 2 个（A/J 错）、病例2 病机少选 1 个（漏 B）、
    病例3 病机多选 1 个（C 错）、病例4 两项都没选（漏全部）。
    """
    sel = _analysis(tmp_path)["selection"]
    t2 = sel["by_task"]["task2"]
    assert (t2["n_over"], t2["n_under"], t2["n_exact"]) == (2, 2, 0)
    assert t2["over_rate"] == 0.5 and t2["under_rate"] == 0.5
    assert t2["n_wrong_picks"] == 3 and t2["n_missed_picks"] == 3
    t3 = sel["by_task"]["task3"]
    assert (t3["n_over"], t3["n_under"], t3["n_exact"]) == (1, 1, 2)
    c = sel["combined"]
    assert c["n_cells"] == 8 and c["n_over"] == 3 and c["n_under"] == 3


def test_exact_count_is_not_the_same_as_correct(tmp_path):
    """选的数量对得上 ≠ 选对了。这一列的含义必须窄，否则会被读成正确率。"""
    sub = _write_sdt(tmp_path, submitted={
        "病例1": ("A", "A"),      # 病机金标准是 D：数量对得上（1=1），但选错了
        "病例2": ("A;B", "B"), "病例3": ("A;B", "A"), "病例4": ("A;B", "A"),
    })
    rows = ea.analyze(tmp_path, "Test", sub)["rows"]
    cell = next(r for r in rows if r["record_id"] == "病例1")["choices"]["task2"]
    assert cell["n_chosen"] == cell["n_gold"] == 1   # 数量恰好
    assert cell["wrong"] == ["A"] and cell["missed"] == ["D"]  # 但全错
    assert cell["score"] == 0.0
    sel = ea.analyze(tmp_path, "Test", sub)["selection"]
    assert sel["by_task"]["task2"]["n_exact"] >= 1   # 它算进"恰好"这一列


def test_marginal_cost_uses_the_official_scorer_not_our_own_formula(tmp_path):
    """两个反事实必须调官方的 score_proportional 算。用一个**恒返回固定值**的
    桩钉住这一点：如果代码自己按推测的公式算，这三个数不会都等于那个固定值。"""
    (tmp_path / "data").mkdir(exist_ok=True)
    sub = _write_sdt(tmp_path)
    (tmp_path / "evaluate.py").write_text(
        "def automated_score(a, b): return 0.0\n"
        "def clinical_info_extraction_eval(x, y): return 0.0\n"
        "def rouge_l(a, b): return 0.0\n"
        "def score_proportional(sub, ref, m): return 0.25\n", encoding="utf-8")
    rows = ea.analyze(tmp_path, "Test", sub)["rows"]
    cell = next(r for r in rows if r["record_id"] == "病例1")["choices"]["task2"]
    assert cell["score"] == cell["score_if_wrong_dropped"] == 0.25
    assert cell["score_if_missed_added"] == 0.25


def test_marginal_cost_is_weighted_into_total_score_points(tmp_path):
    """边际代价要换算成"总分里的分"（乘任务权重 0.3 / 0.4），不然 Task2 和
    Task3 的数没法相加，也没法跟 chain−baseline 那 0.77 分比大小。"""
    sel = _analysis(tmp_path)["selection"]
    t2, t3 = sel["by_task"]["task2"], sel["by_task"]["task3"]
    assert t2["weight"] == 0.3 and t3["weight"] == 0.4
    assert sel["combined"]["recoverable_from_dropping_wrong"] == pytest.approx(
        t2["recoverable_from_dropping_wrong"] + t3["recoverable_from_dropping_wrong"])


def test_verdict_refuses_to_pick_a_lever_when_the_gap_is_small(tmp_path):
    """**这条是 R2-2 的闸门**：两边差距不够时不许给出"主因"，报告要明说
    "不要改选择策略的 prompt"。凑不出主因就承认凑不出，不要为了"做点什么"
    去改 prompt——那正是这一轮要避免的事。"""
    verdict = ea._selection_verdict({
        "recoverable_from_dropping_wrong": 0.30,
        "recoverable_from_adding_missed": 0.28,
    })
    assert verdict["decided"] is False
    assert verdict["lever"] is None
    assert "不要改选择策略的 prompt" in verdict["reason"]


def test_verdict_needs_both_thresholds(tmp_path):
    """两道门槛都要过：比值够大但绝对值太小（0.4 分 vs 0.05 分）也不算——
    在 50 条样本、总增益 0.77 分的量级上，0.4 分的方向说不清是改动起作用
    还是噪声。"""
    ratio_only = ea._selection_verdict({
        "recoverable_from_dropping_wrong": 0.40,   # 8× 但不到 0.5 分
        "recoverable_from_adding_missed": 0.05,
    })
    assert ratio_only["decided"] is False
    both = ea._selection_verdict({
        "recoverable_from_dropping_wrong": 1.20,
        "recoverable_from_adding_missed": 0.30,
    })
    assert both["decided"] is True
    assert "多选" in both["lever"]
    reversed_case = ea._selection_verdict({
        "recoverable_from_dropping_wrong": 0.30,
        "recoverable_from_adding_missed": 1.20,
    })
    assert reversed_case["decided"] is True and "少选" in reversed_case["lever"]


def test_verdict_handles_a_zero_denominator(tmp_path):
    """一边是 0 时不能除零崩掉——"完全没有漏选"是真实会出现的情况。"""
    v = ea._selection_verdict({
        "recoverable_from_dropping_wrong": 0.9,
        "recoverable_from_adding_missed": 0.0,
    })
    assert v["decided"] is True and "多选" in v["lever"]


# ---------- 3. 分布 / 失分榜 / 分组 ----------


def test_correctness_distribution_marks_the_free_text_tasks(tmp_path):
    """Task1（字符串完全相等）和 Task4（ROUGE-L）拿到整 1.0 近乎不可能，
    它们的"完全对"那一列不能被读成对错——必须在数据里标出来，不能只靠
    打印时加一句话。"""
    dist = _analysis(tmp_path)["correctness"]
    assert dist["task2"]["discrete"] is True and dist["task3"]["discrete"] is True
    assert dist["task1"]["discrete"] is False and dist["task4"]["discrete"] is False
    # 桩让 task1 恒 0.5、task4 恒 0.4：全部落在"部分对"
    assert dist["task1"]["partial"] == 4 and dist["task1"]["full"] == 0


def test_top_losses_are_sorted_by_loss_and_capped(tmp_path):
    analysis = _analysis(tmp_path)
    losses = [r["loss"] for r in analysis["top_losses"]]
    assert losses == sorted(losses, reverse=True)
    assert len(analysis["top_losses"]) <= ea.TOP_LOSS_N
    # 病例4 两项都没选，失分最多
    assert analysis["top_losses"][0]["record_id"] == "病例4"


def test_report_prints_model_and_gold_side_by_side_with_clinical_data(tmp_path, capsys):
    analysis = _analysis(tmp_path)
    ea.print_report(analysis)
    out = capsys.readouterr().out
    assert "失分最多的" in out
    assert "临床资料：男性55岁。胸脘痞闷" in out           # Clinical Data 前 80 字
    assert "金标准 ['A', 'B']" in out                      # 并排打印
    assert "错选 ['A', 'J']" in out and "漏选 ['B']" in out


def test_clinical_data_is_truncated_to_the_declared_length(tmp_path, capsys):
    """截断长度是个常量，输出里不能超过它——失分榜是给人扫一眼定位问题的，
    整段医案贴上来就没法扫了。"""
    assert ea.CLINICAL_DATA_CHARS == 80
    ea.print_report(_analysis(tmp_path))
    for line in capsys.readouterr().out.splitlines():
        if "临床资料：" in line:
            body = line.split("临床资料：", 1)[1]
            assert len(body.rstrip("…")) <= ea.CLINICAL_DATA_CHARS


def test_groups_split_each_gold_option_into_its_own_bucket(tmp_path):
    """一条记录的金标准有多个选项时，每个选项各自成组（这条记录计入多组），
    不是把 "A;B" 当成一个组合类别——按组合分组会得到一堆 n=1 的组。"""
    groups = _analysis(tmp_path)["groups"]["task2"]
    labels = {g["label"] for g in groups}
    assert {"胃热炽盛", "胃阴不足"} <= labels      # 病例2 的 A;B 拆成两组
    assert all(g["n"] >= 1 for g in groups)
    assert groups == sorted(groups, key=lambda g: (g["mean"], -g["n"]))


def test_report_warns_that_n1_groups_are_not_evidence(tmp_path, capsys):
    ea.print_report(_analysis(tmp_path))
    out = capsys.readouterr().out
    assert "n=1 的组是一条记录、不是证据" in out


def test_group_label_falls_back_to_the_letter_when_not_in_the_options(tmp_path):
    """金标准里的字母不在这条记录的选项表里时（数据本身的问题），要显出来
    而不是静默丢掉——丢掉会让分组的分母悄悄变小。"""
    rows = [{"record_id": "病例1", "choices": {"task2": {"gold": ["Z"], "score": 0.0}}}]
    from eval.sdt.data import SdtRecord
    rec = SdtRecord(record_id="病例1", clinical_data="x",
                    pathogenesis_options={"A": "肝气横逆"})
    groups = ea.group_by_gold_option(rows, [rec], "task2")
    assert groups[0]["label"] == "Z（选项表里没有这个字母）"


# ---------- 4. 官方总分 vs 我们的加权求和 ----------


def test_report_shows_the_official_total_and_the_delta(tmp_path, capsys):
    """官方 automated_score 是唯一可跟论文比的数，必须跟我们逐条加权算的那个
    并排显示、并报差额——只报好看的那个就是在自己骗自己。"""
    analysis = _analysis(tmp_path)
    assert analysis["official_total"] == 1.75      # 桩里写死的值
    ea.print_report(analysis)
    out = capsys.readouterr().out
    assert "官方 automated_score：1.7500" in out
    assert "引用这个数（可跟论文比）" in out
    assert "两者差额：" in out


def test_report_flags_a_delta_too_big_to_be_explained_by_bom(tmp_path, capsys):
    """差额超过 unmatched 能解释的范围 = 聚合这一层可能有问题，要先查这个，
    不能让读的人拿着一份可能错的分析往下走。"""
    sub = _write_sdt(tmp_path)
    (tmp_path / "evaluate.py").write_text(
        STUB_SCORER.replace("return 1.75", "return 0.0"), encoding="utf-8")
    ea.print_report(ea.analyze(tmp_path, "Test", sub))
    assert "差额超过 unmatched 能解释的范围" in capsys.readouterr().out


def test_train_split_has_no_official_total_and_says_so(tmp_path, capsys):
    """Train 的金标准在 JSON 里、官方没给 Results 文件——这条路算不出
    automated_score，必须明说"不能直接跟论文比"，不能把我们自己算的那个数
    当成官方分往外报。"""
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "Results").mkdir(exist_ok=True)
    train = [{
        "Medical Record ID": r[0], "Clinical Data": r[1],
        "Options of TCM Pathogenesis": r[2], "Options of TCM Syndrome": r[4],
        "Clinical Information": "症状甲;症状乙",
        "Answers of TCM Pathogenesis": r[3], "Answers of TCM Syndrome": r[5],
        "Explanatory Summary": "辨证小结原文", "Syndrome Differentiation": "",
    } for r in RECS]
    (tmp_path / "data" / "Train_TCM_Data_v1.json").write_text(
        json.dumps(train, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "evaluate.py").write_text(STUB_SCORER, encoding="utf-8")
    sub = tmp_path / "sub_train.txt"
    sub.write_text("\n".join(
        "@".join([r[0], "症状甲;症状乙", r[6], r[7], "模型写的小结"]) for r in RECS
    ) + "\n", encoding="utf-8")

    analysis = ea.analyze(tmp_path, "Train", sub)
    assert analysis["official_total"] is None
    assert "内嵌字段" in analysis["gold_source"]
    assert analysis["n_scored"] == 4      # 金标准照样补齐了，分析照样能做
    ea.print_report(analysis)
    out = capsys.readouterr().out
    assert "不能直接跟论文里的 15 个模型比" in out


# ---------- 5. BOM caveat ----------


def test_validation_report_carries_the_bom_caveat(tmp_path, capsys):
    """Validation 的满分上限是 48.9998/50（官方金标准带 BOM，首条恒 0）。
    这个 caveat 必须跟着 Validation 的每一份输出走——引用一个没有这句话的
    Validation 分数就是在报一个错的满分基准。"""
    sub = _write_sdt(tmp_path, split="Validation", bom=True)
    analysis = ea.analyze(tmp_path, "Validation", sub)
    ea.print_report(analysis)
    out = capsys.readouterr().out
    assert "48.9998/50" in out
    assert "data/SOURCES.md 第 14 条" in out
    # 带 BOM 的首条匹配不上，要出现在 unmatched 里、并说明这不是模型失分
    assert analysis["unmatched"] == ["病例1"]
    assert "unmatched 里的第一条不是模型失分" in out


def test_test_split_report_has_no_bom_caveat(tmp_path, capsys):
    """Test 的金标准不带 BOM，满分就是 50.0——给它也挂上那个 caveat 会让人
    以为 Test 的分也有上限损失。"""
    ea.print_report(_analysis(tmp_path))
    assert "48.9998/50" not in capsys.readouterr().out


def test_diagnose_bom_recovers_the_first_record(tmp_path):
    """--diagnose-bom 剥掉 BOM 之后首条能匹配上，得到的是诊断值（不可跟论文
    比），所以它是开关而不是默认行为。"""
    sub = _write_sdt(tmp_path, split="Validation", bom=True)
    default = ea.analyze(tmp_path, "Validation", sub)
    diagnosed = ea.analyze(tmp_path, "Validation", sub, diagnose_bom=True)
    assert default["n_scored"] == 3 and diagnosed["n_scored"] == 4
    assert diagnosed["bom_diagnosed"] is True
    assert diagnosed["weighted_total"] > default["weighted_total"]


# ---------- 6. 过拟合护栏 ----------


def test_test_split_prints_the_overfitting_warning(tmp_path, monkeypatch, capsys):
    """跑 Test 时提醒必须打出来，并且报出已经跑过几次——数是从台账现算的，
    不是写死在提示语里。"""
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(runlog, "LOG_PATH", log)
    log.write_text("".join(json.dumps(
        {"event": "run", "split": "Test", "score": s}, ensure_ascii=False) + "\n"
        for s in (21.702, 22.068, 22.833)), encoding="utf-8")
    warning = runlog.test_split_warning(runlog.read_log(log))
    assert "⚠ Test 集应只在最终定型后跑一次" in warning
    assert "已跑过 3 次" in warning
    assert "过拟合" in warning and "外部可比性" in warning
    assert "先在 Train" in warning                      # 指出该去哪验证
    assert "48.9998/50" in warning                      # Validation 的 caveat
    assert str(runlog.LOG_PATH_DISPLAY) in warning       # 指出台账在哪


def test_warning_counts_full_and_partial_runs_separately(tmp_path):
    """--only-ids 局部重跑也看过 Test 的数据、也是一次暴露，但跟"完整 50 条
    又跑一遍"不是一回事。合成一个数字会让人分不清，所以两个都报。"""
    entries = [
        {"event": "run", "split": "Test", "partial": False},
        {"event": "run", "split": "Test", "partial": False},
        {"event": "run", "split": "Test", "partial": True},
        {"event": "run", "split": "Validation"},   # 别的 split 不算
        {"event": "scored", "split": "Test"},      # 算分不是暴露
    ]
    counts = runlog.count_test_runs(entries)
    assert (counts["total"], counts["full"], counts["partial"]) == (3, 2, 1)
    assert "完整跑 2 次" in runlog.test_split_warning(entries)


def test_committed_runlog_backfills_the_runs_that_already_happened():
    """**台账不能从 0 开始。** Test 已经跑过 3 次完整 + 1 次局部（见
    eval/RESULTS.md 第 5 行），台账建立那天如果从 0 开始，提醒就会说"已跑过
    0 次"——一句假话，而这个护栏的全部作用就是让人相信那个数。

    这一条读的是**真实提交进版本控制的那份台账**（不是 tmp_path），所以它同时
    钉住"这个文件真的在版本控制里"——`.gitignore` 里 `*.jsonl` 是整体忽略，
    少了那条例外它就会静默消失。
    """
    from pathlib import Path

    # 刻意不走 runlog.LOG_PATH：这个文件里的 autouse fixture 把它指向了
    # tmp_path。这条要读的就是真实提交进版本控制的那一份。
    committed = Path(__file__).resolve().parent.parent / "eval" / "sdt" / "test_run_log.jsonl"
    assert committed.exists(), "台账文件不见了——先查 .gitignore 的 *.jsonl 例外"
    entries = runlog.read_log(committed)
    counts = runlog.count_test_runs(entries)
    # 断言只针对**回填的那四条**，不针对全体。原来写的是全体
    # （`counts["full"] == 3` + `all(backfilled)`），于是 SDT Test 每真跑
    # 一次这条就红一次——而真跑正是它本该允许发生的事。这个护栏要钉的是
    # "台账不从 0 开始、回填那四条还在"，不是"从此再也不许跑"。
    backfilled = [e for e in counts["entries"] if e.get("backfilled")]
    assert len([e for e in backfilled if not e.get("partial")]) == 3
    assert len([e for e in backfilled if e.get("partial")]) == 1
    assert {21.702, 22.068, 22.833, 27.729} == {e["score"] for e in backfilled}
    assert all("回填" in e.get("source", "") for e in backfilled)
    # 台账是追加写的，只会变多，不会变少
    assert counts["full"] >= 3 and counts["partial"] >= 1


def test_log_run_only_records_the_test_split(tmp_path):
    """Train/Validation 本来就该反复跑，记进台账只会把真正要看的那几行埋掉。"""
    log = tmp_path / "log.jsonl"
    for split in ("Train", "Validation"):
        assert runlog.log_run(split, "chain", tmp_path / "o.txt", 50, partial=False,
                              ignore_safety_veto=False, model="m", backend="b",
                              path=log) is None
    assert not log.exists()
    entry = runlog.log_run("Test", "chain", tmp_path / "o.txt", 50, partial=False,
                           ignore_safety_veto=False, model="m", backend="b", path=log)
    assert entry["event"] == "run" and entry["split"] == "Test"
    assert entry["prompt_version"] == "v1"
    assert "timestamp" in entry and entry["score"] is None
    assert len(runlog.read_log(log)) == 1


def test_log_run_records_every_field_the_guardrail_needs(tmp_path):
    """台账要能回答"这一次跑的是哪份代码、哪份 prompt、什么配置"——少一个字段
    就会出现两条看起来一样、实际不同的记录。"""
    log = tmp_path / "log.jsonl"
    entry = runlog.log_run("Test", "baseline", tmp_path / "sub.txt", 8, partial=True,
                           ignore_safety_veto=True, model="deepseek-chat",
                           backend="api", path=log)
    assert set(entry) >= {
        "timestamp", "event", "split", "solver", "submission", "n_records",
        "partial", "ignore_safety_veto", "model", "backend", "prompt_version",
        "git_commit", "score",
    }
    assert entry["partial"] is True and entry["ignore_safety_veto"] is True


def test_run_events_are_never_deduped(tmp_path):
    """两次跑就是两次暴露，哪怕参数一模一样——去重会让护栏少数一次。"""
    log = tmp_path / "log.jsonl"
    for _ in range(2):
        runlog.log_run("Test", "chain", tmp_path / "o.txt", 50, partial=False,
                       ignore_safety_veto=False, model="m", backend="b", path=log)
    assert runlog.count_test_runs(runlog.read_log(log))["total"] == 2


def test_scored_events_are_deduped_and_do_not_count_as_exposure(tmp_path):
    """同一份提交可以反复算分（零调用），台账不该被相同的行灌满，也不该让
    "暴露次数"跟着涨。"""
    log = tmp_path / "log.jsonl"
    for _ in range(3):
        runlog.log_scored("Test", tmp_path / "sub.txt", 22.833, path=log)
    entries = runlog.read_log(log)
    assert len(entries) == 1
    assert entries[0]["score_kind"] == "official_automated_score"
    assert runlog.count_test_runs(entries)["total"] == 0


def test_scored_event_records_which_score_kind_it_is(tmp_path):
    """官方 automated_score 和我们逐条加权求和是两个口径，只记一个数字的话
    以后比较几次跑的人不知道自己在比什么。"""
    log = tmp_path / "log.jsonl"
    runlog.log_scored("Test", tmp_path / "a.txt", 2.0,
                      score_kind="weighted_from_breakdown", path=log)
    assert runlog.read_log(log)[0]["score_kind"] == "weighted_from_breakdown"


def test_error_analysis_appends_a_scored_event_for_test(tmp_path, monkeypatch, capsys):
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(runlog, "LOG_PATH", log)
    sub = _write_sdt(tmp_path)
    sdt_run.main(["--sdt-dir", str(tmp_path), "--split", "Test",
                  "--error-analysis", str(sub)])
    entries = runlog.read_log(log)
    assert [e["event"] for e in entries] == ["scored"]
    assert entries[0]["score"] == 1.75                      # 官方口径那个数
    assert entries[0]["score_kind"] == "official_automated_score"
    assert "scored 事件" in capsys.readouterr().out


def test_git_commit_is_none_rather_than_a_fake_value(monkeypatch):
    """拿不到 commit 就记 None。台账上一个假 commit 比没有 commit 更糟——
    它会让人以为自己能定位到那份代码。"""
    import subprocess as sp

    def boom(*a, **kw):
        raise OSError("没装 git")

    monkeypatch.setattr(sp, "run", boom)
    assert runlog.git_commit() is None


def test_read_log_surfaces_a_corrupted_line_instead_of_skipping_it(tmp_path):
    """坏行意味着有人手改过台账，要让它显出来——静默跳过等于让"跑过几次"
    这个数字悄悄变小。"""
    log = tmp_path / "log.jsonl"
    log.write_text('{"event": "run", "split": "Test"}\n不是 JSON\n', encoding="utf-8")
    entries = runlog.read_log(log)
    assert [e["event"] for e in entries] == ["run", "unparseable"]
    assert entries[1]["line_no"] == 2


def test_guardrail_does_not_touch_train_or_validation_runs(tmp_path, monkeypatch, capsys):
    """护栏不许影响 Train/Validation 的正常跑：不打提醒、不写台账。"""
    log = tmp_path / "log.jsonl"
    monkeypatch.setattr(runlog, "LOG_PATH", log)
    import core.llm as llm_mod
    from eval.sdt.adapter import SdtAnswer

    class _FakeBackend:
        def model_name(self):
            return "fake-model"

        def backend_id(self):
            return "fake"

        def comparability_warning(self):
            return None

    class _FakeSolver:
        name = "fake"

        def solve(self, record, ignore_safety_veto=None):
            return SdtAnswer(record_id=record.record_id, clinical_information=["症状甲"],
                             pathogenesis_answers=["A"], syndrome_answers=["A"],
                             summary="小结", llm_calls=3)

    monkeypatch.setattr(llm_mod, "_llm_singleton", _FakeBackend())
    monkeypatch.setattr(sdt_run, "SOLVERS", {"fake": _FakeSolver})
    _write_sdt(tmp_path, split="Validation")
    sdt_run.main(["--sdt-dir", str(tmp_path), "--split", "Validation",
                  "--solver", "fake", "--out", str(tmp_path / "out.txt")])
    out = capsys.readouterr().out
    assert "⚠ Test 集" not in out
    assert not log.exists()


def test_out_is_required_only_in_scoring_mode(tmp_path):
    """--out 从必填改成选填了（失分分析模式不需要它），跑分模式必须仍然
    拦住漏传——不然会在跑完 250 次调用之后才发现没地方写。"""
    _write_sdt(tmp_path)
    with pytest.raises(SystemExit):
        sdt_run.main(["--sdt-dir", str(tmp_path), "--split", "Validation",
                      "--solver", "baseline"])


def test_per_pick_marginal_cost_is_reported(tmp_path):
    """总量会被次数带偏（错选 30 个、漏选 3 个，总量当然错选大）。要回答
    "prompt 该不该说宁可不选"，需要的是**单个**选项的代价。"""
    sel = _analysis(tmp_path)["selection"]
    c = sel["combined"]
    assert c["n_wrong_picks"] == 4 and c["n_missed_picks"] == 4
    assert c["cost_per_wrong_pick"] == pytest.approx(
        c["recoverable_from_dropping_wrong"] / c["n_wrong_picks"])
    assert c["cost_per_missed_pick"] == pytest.approx(
        c["recoverable_from_adding_missed"] / c["n_missed_picks"])


def test_per_pick_cost_is_none_not_zero_when_there_is_nothing_to_average(tmp_path):
    """一个错选都没有时分母是 0：返回 None（不适用），不是 0.0（"一个错选不值钱"）
    ——跟分层 Jaccard 空层返 None 是同一条理由。"""
    sub = _write_sdt(tmp_path, submitted={r[0]: (r[3], r[5]) for r in RECS})  # 全答对
    sel = ea.analyze(tmp_path, "Test", sub)["selection"]
    assert sel["combined"]["n_wrong_picks"] == 0
    assert sel["combined"]["cost_per_wrong_pick"] is None
    assert sel["combined"]["cost_per_missed_pick"] is None


def test_report_warns_when_a_missed_pick_costs_more_than_a_wrong_pick(tmp_path, capsys):
    """**这条是 R2-2 的关键**：如果实测出来一个漏选比一个错选更贵，那么 prompt
    里现在那句「拿不准的宁可不选」方向就是反的，报告必须把这件事喊出来，
    而不是让人读着两个数自己去想。

    用一个"错选几乎不扣分、漏选丢一整份"的桩制造这个局面。
    """
    sub = _write_sdt(tmp_path)
    (tmp_path / "evaluate.py").write_text(
        "def automated_score(a, b): return 1.0\n"
        "def clinical_info_extraction_eval(x, y): return 0.0\n"
        "def rouge_l(a, b): return 0.0\n"
        "def score_proportional(sub, ref, m):\n"
        "    sub=[x for x in sub if x]; ref=[x for x in ref if x]\n"
        "    if not ref: return 0.0\n"
        "    # 错选完全不扣分，漏选按比例丢分\n"
        "    return len(set(sub) & set(ref)) / len(ref)\n", encoding="utf-8")
    ea.print_report(ea.analyze(tmp_path, "Test", sub))
    out = capsys.readouterr().out
    assert "一个漏选比一个错选更贵" in out
    assert "方向是反的" in out


def test_report_does_not_warn_when_wrong_picks_are_the_expensive_side(tmp_path, capsys):
    """反过来（错选更贵）时不能打那句警告——那会把一句对的 prompt 说成错的。

    **病例4 在这条里要改成答对**：一条完全没作答的记录是纯粹的少选（去掉错选
    捞不回任何分、补上漏选能捞回全部），无论计分公式是什么都会把"漏选"那一边
    顶高。这本身是对的（不作答就是少选），但要单独验"错选更贵"这个分支，
    就得把这个混杂因素拿掉。
    """
    sub = _write_sdt(tmp_path, submitted={
        "病例1": ("A;D;J", "A"), "病例2": ("A", "B"),
        "病例3": ("A;B;C", "A;B"), "病例4": ("A;B", "A"),   # 病例4 答对，不参与
    })
    (tmp_path / "evaluate.py").write_text(
        "def automated_score(a, b): return 1.0\n"
        "def clinical_info_extraction_eval(x, y): return 0.0\n"
        "def rouge_l(a, b): return 0.0\n"
        "def score_proportional(sub, ref, m):\n"
        "    sub=[x for x in sub if x]; ref=[x for x in ref if x]\n"
        "    if not ref: return 0.0\n"
        "    wrong = len(set(sub) - set(ref))\n"
        "    return max(0.0, len(set(sub) & set(ref)) / len(ref) - 0.9 * wrong)\n",
        encoding="utf-8")
    analysis = ea.analyze(tmp_path, "Test", sub)
    c = analysis["selection"]["combined"]
    assert c["cost_per_wrong_pick"] > c["cost_per_missed_pick"]
    ea.print_report(analysis)
    assert "方向是反的" not in capsys.readouterr().out


def test_an_unanswered_record_counts_as_under_selection(tmp_path):
    """一条完全没作答的记录是纯粹的少选：去掉错选捞不回任何分，补上漏选能捞回
    全部。这不是 bug（不作答确实是少选），但它会顶高"漏选"那一边，读边际代价
    时必须知道有这个成分——所以把它钉下来。"""
    sub = _write_sdt(tmp_path, submitted={
        "病例1": ("D", "A"), "病例2": ("A;B", "B"),
        "病例3": ("A;B", "A"), "病例4": ("", ""),   # 只有病例4 没作答
    })
    rows = ea.analyze(tmp_path, "Test", sub)["rows"]
    cell = next(r for r in rows if r["record_id"] == "病例4")["choices"]["task2"]
    assert cell["chosen"] == [] and cell["missed"] == ["A", "B"]
    assert cell["score"] == 0.0
    assert cell["score_if_wrong_dropped"] == 0.0      # 没有错选可去
    assert cell["score_if_missed_added"] == 1.0       # 补齐就满分
