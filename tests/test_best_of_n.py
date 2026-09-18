"""R22：best-of-N 采样与挑选。

这一层的每条判据都能被一句话说清，而它们各自对应一个真实会犯的错：
  · 采 N 次 → 少采一次就是"配置没生效"，而它表现为"结果好了一点点"，看不出来；
  · **按 R23 的 `score_formula` 挑** → 另写一把尺就有了两把尺，改权重只改一处这条就破了；
  · 平手取下标最小 → 不定的挑选让 fixture 回放和 ε 都不可复现；
  · N 次里失败几次仍然出结果 → 429/超时是常态，全灭才该抛；
  · **调用数按真实采样次数计** → 漏算的表现是账本持续少扣，而少扣不会报错；
  · 安全层重开发生在挑选**之后** → 反过来会让"被拦掉的恰好是分最高的那张"静默消失。
"""
from __future__ import annotations

import threading

import pytest

from core import chain
from core.formula_check import score_formula
from core.schemas import FormulaCandidate, HerbItem, S3Syndrome
from tests.test_chain import FakeLLM, FakeRetriever, _fake_cases


def _s3(name: str, herbs: list[HerbItem], syndrome: str = "脾胃气虚证") -> S3Syndrome:
    return S3Syndrome(
        syndrome=syndrome,
        reasoning="脾胃气虚，运化失司。",
        treatment_principle="健脾益气",
        formula_candidates=[FormulaCandidate(
            name=name, source="classic", confidence="high",
            rationale="经典方。", herb_items=herbs,
        )],
        cited_case_ids=["ye_tianshi-001"],
    )


CLEAN = [HerbItem(name="党参", dose=9.0), HerbItem(name="白术", dose=9.0)]
# 甘草 + 甘遂 = 十八反，score 扣满 1.0 → 0.0
FORBIDDEN = [HerbItem(name="甘草", dose=6.0), HerbItem(name="甘遂", dose=3.0)]
# 附子 30g 超 15g 上限，扣 0.5 → 0.5
OVERDOSE = [HerbItem(name="附子", dose=30.0)]


class ScriptedS3LLM(FakeLLM):
    """每次 S3 按顺序吐一个预设结果，**计数器按医家分开**。

    按医家分开是必须的：三位医家并发，每位各采 N 次。用一个全局计数器的话，
    「第 4 次调用」到底是某位医家的安全重开、还是另一位医家的第一次采样，
    取决于线程调度——那样写出来的测试会随机红，而红的原因跟被测代码无关。

    **注意采样之间的顺序仍然是不定的**（同一位医家的 N 次采样是并发发出的），
    所以下面的断言都不依赖"第 i 次采样拿到的是 scripted[i]"，只依赖
    「这一位医家总共拿到了这 N 个预设结果」这个集合性质。
    """

    def __init__(self, scripted: list, s1=None):
        super().__init__({}, s1=s1)
        self.scripted = list(scripted)
        self.s3_calls = 0
        self._per_physician: dict[str | None, int] = {}
        self._lock = threading.Lock()

    def generate(self, system: str, user: str, schema, temperature: float = 0.0, **kwargs):
        from core.schemas import _S3Base

        if isinstance(schema, type) and issubclass(schema, _S3Base):
            physician = kwargs.get("physician")
            with self._lock:
                self.calls.append(schema.__name__)
                i = self._per_physician.get(physician, 0)
                self._per_physician[physician] = i + 1
                self.s3_calls += 1
            item = self.scripted[i % len(self.scripted)]
            if isinstance(item, Exception):
                raise item
            return item
        return super().generate(system, user, schema, temperature, **kwargs)


def _run(monkeypatch, scripted, n="3", **env):
    monkeypatch.setenv("S3_BEST_OF_N", n)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    llm = ScriptedS3LLM(scripted)
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    out = chain.consult("纳差乏力")
    return out, llm


def test_it_really_samples_n_times_per_physician(monkeypatch):
    out, llm = _run(monkeypatch, [_s3("四君子汤", CLEAN)], n="3")
    n_phys = len(out["results"])
    assert llm.s3_calls == 3 * n_phys, f"每位医家应采 3 次，实际共 {llm.s3_calls} 次"
    for r in out["results"]:
        assert r["best_of_n"] == 3
        assert len(r["candidates_scored"]) == 3


def test_n_equals_one_takes_the_single_sample_path(monkeypatch):
    """N=1 时逐字节走回 R21 之前那条路径——一次调用、一条 candidates_scored。
    把"关掉"做成独立代码路径会让两条路径慢慢分叉，而这个旋钮正是要做对照的。"""
    out, llm = _run(monkeypatch, [_s3("四君子汤", CLEAN)], n="1")
    assert llm.s3_calls == len(out["results"])
    for r in out["results"]:
        assert r["best_of_n"] == 1
        assert len(r["candidates_scored"]) == 1
        assert r["candidates_scored"][0]["chosen"] is True


def test_it_picks_the_highest_scoring_sample(monkeypatch):
    """三次采样：十八反（0 分）/ 超剂量（中间）/ 干净方（满分）→ 选第三个。

    **中间那个分数不钉具体值。** 它原来写死 0.5（只扣超剂量那一条），但
    2026-09-17 药理层入库、本草表从 0 条变成 9776 条之后，「缺引经药」规则
    第一次能取到归经依据、开始生效，同一张超量方变成 0.35（0.5 − 0.15）。
    那是规则按设计工作，不是缺陷；而这条测试要验的是**选最高分那个**，
    不是某张方恰好扣多少分。钉死中间值等于每次药理层数据变动就误报一次。
    扣分权重的正确性由 tests/test_formula_check.py 负责。
    """
    scripted = [_s3("禁忌方", FORBIDDEN), _s3("超量方", OVERDOSE), _s3("四君子汤", CLEAN)]
    out, _ = _run(monkeypatch, scripted, n="3")
    for r in out["results"]:
        rows = r["candidates_scored"]
        scores = sorted(row["score"] for row in rows)
        assert len(scores) == 3
        assert scores[0] == 0.0, f"十八反那张必须 0 分，实际 {scores[0]}"
        assert scores[2] == 1.0, f"干净方必须满分，实际 {scores[2]}"
        assert 0.0 < scores[1] < 1.0, f"超量方应落在两者之间，实际 {scores[1]}"
        assert r["s3"].formula_candidates[r["s3"].selected].name == "四君子汤"
        chosen = [c for c in rows if c["chosen"]]
        assert len(chosen) == 1 and chosen[0]["score"] == 1.0


def test_the_scores_come_from_the_shared_ruler_not_a_second_one(monkeypatch):
    """`candidates_scored` 里的分必须等于 R23 的 `score_formula` 算出来的。
    这条抓"best-of-N 自己又写了一把尺"——那种退化下排序看着也合理。"""
    from core.formula_check import check_formula

    scripted = [_s3("禁忌方", FORBIDDEN), _s3("超量方", OVERDOSE), _s3("四君子汤", CLEAN)]
    out, _ = _run(monkeypatch, scripted, n="3")
    by_formula = {"禁忌方": FORBIDDEN, "超量方": OVERDOSE, "四君子汤": CLEAN}
    for r in out["results"]:
        for row in r["candidates_scored"]:
            expected = check_formula("脾胃气虚证", by_formula[row["formula"]]).score
            assert row["score"] == expected == score_formula(
                check_formula("脾胃气虚证", by_formula[row["formula"]]).advice)


def test_a_tie_takes_the_lowest_index(monkeypatch):
    """三次一样分时选下标最小的那次。**不定的挑选让 fixture 回放和 ε 都不可复现**，
    所以平手规则本身就是契约的一部分。"""
    scripted = [_s3("甲方", CLEAN), _s3("乙方", CLEAN), _s3("丙方", CLEAN)]
    out, _ = _run(monkeypatch, scripted, n="3")
    for r in out["results"]:
        rows = r["candidates_scored"]
        assert len({row["score"] for row in rows}) == 1, "这条的前提是三次同分"
        chosen = [c for c in rows if c["chosen"]]
        assert len(chosen) == 1
        assert chosen[0]["index"] == 0, "平手取下标最小"
        # 断言"选中的就是 index 0 那一行"，而不是某个固定方名：同一位医家的 N 次
        # 采样是并发发出的，哪个预设落在 index 0 本身是不定的（见 ScriptedS3LLM
        # 的文档）——依赖方名的写法会随机红。
        assert chosen[0]["formula"] == rows[0]["formula"]
        assert r["s3"].formula_candidates[r["s3"].selected].name == rows[0]["formula"]


def test_one_failed_sample_does_not_sink_the_physician(monkeypatch):
    """N 次里有一次撞 429/超时是常态。失败那次在 candidates_scored 里留一行
    `score: None` + `error`，**不是从列表里消失**——消失的话 N 就对不上，
    而"为什么这次只有两条"没人能回答。"""
    scripted = [RuntimeError("429 too many requests"), _s3("四君子汤", CLEAN),
                _s3("超量方", OVERDOSE)]
    out, _ = _run(monkeypatch, scripted, n="3")
    for r in out["results"]:
        rows = r["candidates_scored"]
        assert len(rows) == 3
        failed = [c for c in rows if c["score"] is None]
        assert len(failed) == 1 and failed[0]["error"] == "RuntimeError"
        assert failed[0]["chosen"] is False
        assert r["s3"].formula_candidates[r["s3"].selected].name == "四君子汤"


def test_all_samples_failing_raises_instead_of_faking_a_result(monkeypatch):
    """全灭才抛，而且抛的是真实异常——返回一个"没有候选方"的假结果会在下游
    变成 IndexError，离根因十几帧远。"""
    scripted = [RuntimeError("boom-1"), RuntimeError("boom-2"), RuntimeError("boom-3")]
    with pytest.raises(RuntimeError, match="boom"):
        _run(monkeypatch, scripted, n="3")


def test_llm_calls_counts_every_sample(monkeypatch):
    """**manifest 的调用数是额度结算的依据。** 按医家数计（漏掉 N−1 次）的表现是
    账本持续少扣，而少扣不会报错——这条是那个 bug 的回归测试。"""
    out, llm = _run(monkeypatch, [_s3("四君子汤", CLEAN)], n="3")
    assert out["manifest"]["llm_calls"] == llm.calls.count("S1Normalize") \
        + llm.calls.count("S2Elements") + llm.s3_calls
    assert out["manifest"]["llm_calls"] == 2 + 3 * len(out["results"])


def test_the_manifest_records_n_and_the_effort(monkeypatch):
    """两项都让数字不可比（N 从 1 换到 3、effort 从 high 换到 max 都是换实验条件），
    所以是 manifest 的一等字段，不是可选调试信息。"""
    out, _ = _run(monkeypatch, [_s3("四君子汤", CLEAN)], n="3")
    assert out["manifest"]["best_of_n"] == 3
    assert out["manifest"]["reasoning_effort"] in ("low", "medium", "high", "max")


def test_effort_is_none_in_the_manifest_when_thinking_is_off(monkeypatch):
    """关了思考 effort 不生效。记一个不生效的实验条件比不记更糟——
    引用这个 manifest 的人会以为这次真的想了那么久。"""
    out, _ = _run(monkeypatch, [_s3("四君子汤", CLEAN)], n="1", S3_THINKING="disabled")
    assert out["manifest"]["reasoning_effort"] is None


def test_the_safety_rerun_happens_after_the_pick_not_before(monkeypatch):
    """三次采样全是禁忌方 → 挑出来的那张仍然 blocking → 安全层重开一次。

    顺序反过来（先拦截再挑）会让"被拦掉的恰好是分最高的那张"静默消失，
    而那正是要看到的信息。判据：`candidates_scored` 记的是**挑选时**的分
    （0.0），而重开后最终留下的方由 `formula_score` 反映——两个字段分开就是
    为了让这一前一后看得见。
    """
    # 前 3 次（采样）全禁忌，第 4 次（安全重开）给干净方
    scripted = [_s3("禁忌方", FORBIDDEN), _s3("禁忌方", FORBIDDEN),
                _s3("禁忌方", FORBIDDEN), _s3("四君子汤", CLEAN)]
    out, llm = _run(monkeypatch, scripted, n="3")
    for r in out["results"]:
        assert r["safety_output"]["revised"] is True, "该重开一次"
        assert all(c["score"] in (0.0, None) for c in r["candidates_scored"])
        assert r["formula_score"] == 1.0, "重开之后留下的是干净方"
    # 每位医家 3 次采样 + 1 次重开
    assert llm.s3_calls == 4 * len(out["results"])


def test_fast_mode_cuts_sampling_to_one(monkeypatch):
    """FAST_MODE 的第四处降级。不降的话 FAST_MODE 名不副实——
    "用户以为省了预算、实际还在花"正是 fast_mode_enabled() 点名要防的事。"""
    monkeypatch.setenv("FAST_MODE", "1")
    monkeypatch.delenv("S3_BEST_OF_N", raising=False)
    llm = ScriptedS3LLM([_s3("四君子汤", CLEAN)])
    monkeypatch.setattr(chain, "get_llm", lambda: llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    out = chain.consult("纳差乏力")
    assert llm.s3_calls == len(out["results"])
    for r in out["results"]:
        assert r["best_of_n"] == 1


def test_an_explicit_n_beats_fast_mode(monkeypatch):
    """显式 > 兜底（跟 EVAL_MODE 那条一致）：明确写了 N 就按 N 采，
    不因为 FAST_MODE 在就悄悄改成 1。"""
    monkeypatch.setenv("FAST_MODE", "1")
    out, llm = _run(monkeypatch, [_s3("四君子汤", CLEAN)], n="2")
    assert llm.s3_calls == 2 * len(out["results"])
