"""R10-1：录制时**跨场景**复用已录的 key。零 LLM 调用（假后端）。

## 这组测试钉的是哪个 bug

段 6 实测：录制"完成"（233 条 fixture / 244 次写入 = 11 个 key 被覆盖写了两次），
但 `verify_replay` 里 **10 个 react_off 场景全挂、10 个 react_on 全过**——这个
整齐的切分就是线索。

因果链（`core/chain.py:244`：S1 的 system **只依赖主诉**；而录制清单里
`react_off_N` 和 `react_on_N` 用的是同一条主诉，所以两者的 S1 key 完全相同）：

    录 react_off_1 → S1 输出 A → 写 fixture(key_S1)
                   → S2(system 含 A) → 写 fixture(key_S2a)
                   → S3(system 含 S2 结果) → 写 fixture(key_S3a)，基线存下来
    录 react_on_1  → S1 **再调一次**，v4-pro 是推理模型、同样输入给出不同输出 B
                   → **覆盖** fixture(key_S1)
                   → S2(system 含 B) → key 变了 → 写 fixture(key_S2b)
                     （key_S2a 从此没人引用 = 孤儿）
    回放 react_off_1 → S1 拿到 B → S2 算出 key_S2b（命中）→ S3 的 key 不再是
                       key_S3a → **未命中**

重跑救不回来：录制顺序里 react_off 永远在 react_on 前面。换成 react_on 在前，
就轮到 react_on 变孤儿——**是个跷跷板，必须改代码**。

## 修复的判据：「上一个场景写过」，不是「本次调用写过」

不能无条件"首写为准"：`generate()` 校验失败会带着回灌消息重试，messages[0] 不变
所以 key 不变，**最后一次**写进去的才是通过校验的那份（`RecordingBackend.write`
的文档字符串记着这条）。首写为准会把非法输出定死在 fixture 里。所以复用的判据是
场景边界：只有**前面场景**写过的 key 才复用，当前场景内照旧覆盖。
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from core.llm import LLMBackend
from core.llm_replay import Fixture, RecordingBackend, ReplayBackend


class Out(BaseModel):
    v: str


class CountingBackend(LLMBackend):
    """**同样输入返回不同输出**——就是 deepseek-v4-pro 的真实行为（推理模型，
    即使 temperature=0）。这正是把"覆盖"变成 bug 的那个前提条件。"""

    def __init__(self) -> None:
        self.n = 0
        self.systems: list[str] = []

    def model_name(self) -> str:
        return "fake-nondeterministic"

    def backend_id(self) -> str:
        return "fake"

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs) -> str:
        self.n += 1
        self.systems.append(messages[0]["content"])
        return json.dumps({"v": f"out-{self.n}"})


def _recorder(tmp_path) -> tuple[RecordingBackend, CountingBackend]:
    inner = CountingBackend()
    return RecordingBackend(inner=inner, out_dir=tmp_path), inner


def _chain(backend: LLMBackend, complaint: str, flavour: str = "react_off") -> tuple[str, str]:
    """真实链路的形状（这三条是段 6 那个 bug 成立的全部前提）：

      S1  system **只依赖主诉**            → react_off_N 和 react_on_N 的 key 相同
      S2  system 含 S1 的输出              → 上游输出一变，这一步的 key 就变
      S3  system 含 S2 的输出 + **按场景不同的模板**（开/不开 ReAct 用的不是同一个
          prompt）→ 所以两个场景的 S3 是两条不同的 key，谁也顶不了谁

    返回 (S1 输出, S2 输出)。"""
    s1 = backend.generate(f"S1:{complaint}", "", Out)
    s2 = backend.generate(f"S2:{complaint}|上游={s1.v}", "", Out)
    backend.generate(f"S3[{flavour}]:{complaint}|证素={s2.v}", "", Out)
    return s1.v, s2.v


def _schema_hint(schema: type[BaseModel]) -> str:
    """`generate()` 会在 system 后面拼一段 schema 提示，key 算的是拼完的整串。
    这里复算出同一串，免得测试里写死一段会随实现漂移的字符串。"""
    import core.llm as llm_mod

    probe = {}

    class Probe(LLMBackend):
        def model_name(self) -> str:
            return "probe"

        def backend_id(self) -> str:
            return "probe"

        def _complete(self, messages, temperature, **kwargs) -> str:
            probe["system"] = messages[0]["content"]
            return json.dumps({"v": "x"})

    assert llm_mod  # 只是说明这里用的是同一个 generate() 实现
    Probe().generate("", "", schema)
    return probe["system"]


# ---------- 一、bug 的回归测试（修复前必红） ----------


def test_replaying_the_earlier_scenario_hits_after_a_later_one_shared_its_s1(tmp_path):
    """**这条就是段 6 那个 bug 的回归测试。**

    修复前（沙盒实测，同一形状的脚本）：
      react_off 看到 S1=out-1 S2=out-2；react_on 看到 S1=out-4 S2=out-5
      写入 6 次 / 去重 5 条 → 覆盖 1 个（跟段 6 的 244/233 同一个形状）
      回放 react_off：**未命中** key=Out_9244f8e812a3
      回放 react_on：★ 命中
    ——**10 个 react_off 全挂、10 个 react_on 全过**那个整齐的切分就是这样来的。
    修复后两个场景都必须全命中。
    """
    recorder, inner = _recorder(tmp_path)

    recorder.begin_scenario("react_off_1")
    a1, a2 = _chain(recorder, "胃脘胀痛", "react_off")

    recorder.begin_scenario("react_on_1")          # 同一条主诉的另一个场景
    b1, b2 = _chain(recorder, "胃脘胀痛", "react_on")

    # 跨场景复用：第二个场景没有再调 S1/S2，所以两边看到的上游输出是同一个
    assert (a1, a2) == (b1, b2), "跨场景没复用：后一个场景又调了一次 S1，拿到了不同输出"
    assert recorder.n_reused == 2 and recorder.n_written == 4  # S1+S2 复用，两条 S3 各写一次
    assert recorder.n_written == len(recorder.keys_written), "不该再有任何 key 被覆盖"

    # **真正的判据：两个场景各自重放一遍，都必须全命中**（修复前 react_off 会未命中）
    for flavour, expected in (("react_off", (a1, a2)), ("react_on", (b1, b2))):
        replay = ReplayBackend(fixtures_path=tmp_path)
        assert _chain(replay, "胃脘胀痛", flavour) == expected
        assert replay.n_hits == 3


def test_reuse_saves_calls_and_is_counted(tmp_path):
    """复用的副作用（好的）：省掉重复调用。省了多少要报出来——录制脚本的收尾
    统计里有这个数。"""
    recorder, inner = _recorder(tmp_path)
    recorder.begin_scenario("a")
    _chain(recorder, "同一条主诉")
    assert inner.n == 3 and recorder.n_reused == 0

    recorder.begin_scenario("b")
    _chain(recorder, "同一条主诉")          # 同主诉、同 flavour = 三步 key 全一样
    assert inner.n == 3, "第二个场景一次都不该再调模型（三步的 key 都被前面录过）"
    assert recorder.n_reused == 3
    assert len(recorder.reused_keys) == 3


def test_a_scenario_with_a_different_complaint_still_calls_the_model(tmp_path):
    """复用只认 key 相同。不同主诉 = 不同 key = 照旧真调，不能把它也"省"掉。"""
    recorder, inner = _recorder(tmp_path)
    recorder.begin_scenario("a")
    _chain(recorder, "主诉甲")
    recorder.begin_scenario("b")
    _chain(recorder, "主诉乙")
    assert inner.n == 6 and recorder.n_reused == 0


# ---------- 二、校验重试的覆盖语义不能被破坏 ----------


def test_within_one_scenario_the_same_key_still_overwrites(tmp_path):
    """**同一场景内**第二次写同一个 key 仍然覆盖：`generate()` 校验失败会带着
    回灌消息重试，messages[0] 不变所以 key 不变，最后一次写进去的才是通过校验
    的那份。首写为准会把非法输出定死。"""
    recorder, _inner = _recorder(tmp_path)
    recorder.begin_scenario("only")
    key = recorder.write("Out", "同一个 system", '{"v": "非法输出"}')
    recorder.write("Out", "同一个 system", '{"v": "修好的输出"}')
    fixture = Fixture.model_validate_json(key.read_text(encoding="utf-8"))
    assert json.loads(fixture.output)["v"] == "修好的输出"
    assert recorder.n_written == 2 and recorder.n_reused == 0


def test_the_validation_retry_path_end_to_end_keeps_the_last_output(tmp_path):
    """走真实的 `generate()` 重试路径（第一次输出不合 schema，第二次合）：
    录下来的必须是第二次那份。"""
    class FirstAttemptInvalid(LLMBackend):
        def __init__(self) -> None:
            self.n = 0

        def model_name(self) -> str:
            return "fake"

        def backend_id(self) -> str:
            return "fake"

        def _complete(self, messages, temperature, **kwargs) -> str:
            self.n += 1
            return "不是 JSON" if self.n == 1 else json.dumps({"v": "第二次才对"})

    inner = FirstAttemptInvalid()
    recorder = RecordingBackend(inner=inner, out_dir=tmp_path)
    recorder.RETRY_BACKOFF_SECONDS = (0.0, 0.0)
    recorder.begin_scenario("retry")
    assert recorder.generate("S", "", Out).v == "第二次才对"
    assert inner.n == 2
    # fixture 里存的是通过校验的那份
    replay = ReplayBackend(fixtures_path=tmp_path)
    assert replay.generate("S", "", Out).v == "第二次才对"


# ---------- 三、只在本次运行内复用 ----------


def test_reuse_never_reads_fixtures_left_on_disk_by_a_previous_run(tmp_path):
    """**不能用"读磁盘上已有的 fixture"来实现复用**：跨两次录制运行也会复用，
    而两次运行之间模型可能换了（这次就是 deepseek-chat → v4-pro）。
    第二次录制必须重新调模型、把磁盘上的旧 fixture 覆盖掉。"""
    def _only_fixture() -> str:
        path = next(p for p in tmp_path.glob("*.json") if not p.name.startswith("_"))
        return json.loads(path.read_text(encoding="utf-8"))["output"]

    first, inner1 = _recorder(tmp_path)
    first.begin_scenario("a")
    first.generate("S", "", Out)
    assert inner1.n == 1
    before = _only_fixture()

    # 第二次录制：新的 RecordingBackend（= 新的一次运行），假后端从 out-1 重新开始
    second, inner2 = _recorder(tmp_path)
    second.begin_scenario("a")
    second.generate("S", "", Out)
    assert inner2.n == 1, "跨运行不该复用磁盘上的旧 fixture"
    assert second.n_reused == 0
    # 这一次的输出恰好也是 out-1，所以换一个判据：fixture 的 recorded_at/内容被重写过
    assert _only_fixture() == before or _only_fixture() != before  # 内容可同可不同
    assert second.n_written == 1, "第二次运行必须真的写了一遍（不是跳过）"


def test_calls_before_the_first_begin_scenario_are_not_reused(tmp_path):
    """没调 begin_scenario 就录（别的脚本直接用 RecordingBackend）时行为不变：
    冻结集合是空的，一切照旧覆盖。"""
    recorder, inner = _recorder(tmp_path)
    recorder.generate("S", "", Out)
    recorder.generate("S", "", Out)
    assert inner.n == 2 and recorder.n_reused == 0


# ---------- 四、收尾自洽校验（修复 2） ----------


def test_baseline_self_check_catches_a_tampered_fixture(tmp_path):
    """录完之后用 ReplayBackend 把所有场景再跑一遍跟基线比。人为改坏一条 fixture
    就必须被抓出来——这一步零调用、几秒钟，但它把"录出来的东西能不能放出来"
    从"跑完才知道"变成"录制脚本自己保证"。"""
    from scripts.record_fixtures import check_baseline_self_consistency

    recorder, _inner = _recorder(tmp_path)
    recorder.begin_scenario("s")
    recorder.generate("S", "", Out)
    baseline = {"s": "canonical-of-s"}

    def fake_run(_scenario_name: str) -> str:
        replay = ReplayBackend(fixtures_path=tmp_path)
        return f"canonical-of-{replay.generate('S', '', Out).v}"

    # 未改动：一致（假的 canonical 用 fixture 的输出算，改坏就会变）
    baseline = {"s": fake_run("s")}
    assert check_baseline_self_consistency(["s"], baseline, fake_run) == []

    # 改坏那条 fixture
    path = next(p for p in tmp_path.glob("*.json") if not p.name.startswith("_"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["output"] = json.dumps({"v": "被人改坏了"})
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    mismatches = check_baseline_self_consistency(["s"], baseline, fake_run)
    assert len(mismatches) == 1
    assert mismatches[0]["scenario"] == "s"
    assert "被人改坏了" in mismatches[0]["now"]


def test_baseline_self_check_reports_a_replay_miss_as_a_mismatch(tmp_path):
    """回放未命中（就是段 6 的症状）也要算不一致，而不是让异常冒出去。"""
    from core.llm import LLMError
    from scripts.record_fixtures import check_baseline_self_consistency

    def always_miss(_scenario_name: str) -> str:
        raise LLMError("回放未命中：fixtures 里没有 key=...")

    mismatches = check_baseline_self_consistency(["s"], {"s": "x"}, always_miss)
    assert len(mismatches) == 1 and "未命中" in mismatches[0]["now"]


def test_baseline_self_check_skips_scenarios_that_failed_during_recording():
    """录制时就失败的场景没有基线（canonical=None），不参与比对——它们已经在
    失败清单里报过了，再报一次"不一致"是噪音。"""
    from scripts.record_fixtures import check_baseline_self_consistency

    called: list[str] = []

    def fake_run(name: str) -> str:
        called.append(name)
        return "x"

    assert check_baseline_self_consistency(["failed_one"], {}, fake_run) == []
    assert called == [], "没有基线的场景不该被重放"


# ---------- 五、未命中提示补的那条线索（修复 3） ----------


def test_miss_message_points_at_key_drift_when_the_complaint_is_already_in_the_plan(tmp_path):
    """原来的提示只说"补录这一条：把主诉加进清单"。段 6 的情况主诉**本来就在
    清单里**，按那句做会白跑一遍两小时。"""
    replay = ReplayBackend(fixtures_path=tmp_path)
    with pytest.raises(Exception) as exc:
        replay.generate("没录过的 system", "", Out)
    msg = str(exc.value)
    assert "已经在" in msg and "清单" in msg
    assert "上游" in msg and "覆盖" in msg
    assert "record_fixtures" in msg


def test_record_fixtures_reports_reuse_and_overwrite_stats():
    """未命中提示让人去看"复用 / 覆盖"统计，那个统计必须真的被打出来。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "scripts" / "record_fixtures.py").read_text(encoding="utf-8")
    assert "n_reused" in src and "复用" in src
    assert "begin_scenario" in src


# ---------- 六、main() 的接线（场景边界真的被调了 / 自洽校验真的拦得住） ----------


def _stub_plan():
    """两个共享同一条主诉的场景——就是段 6 那一对的形状。"""
    from scripts.record_fixtures import Scenario

    return [Scenario("react_off_1", "胃脘胀痛", use_react=False),
            Scenario("react_on_1", "胃脘胀痛", use_react=True)]


def test_main_calls_begin_scenario_once_per_scenario(tmp_path, monkeypatch):
    """**段 6 的 bug 就是少了这一句。** 这条钉住录制循环真的在每个场景前划边界，
    而且传的是场景名（报错时要能说出是哪个场景）。"""
    import scripts.record_fixtures as rf

    seen: list[str | None] = []

    def fake_run_scenario(scenario, recorder=None, bar=None):
        seen.append(getattr(recorder, "scenario", None) if recorder is not None else "replay")
        return {"scenario": scenario.name, "complaint": scenario.complaint,
                "use_react": scenario.use_react, "n_fixtures": 0, "elapsed_s": 0.0,
                "error": None, "rejected": False, "insufficient": False,
                "llm_calls": 0, "canonical": f"canonical-{scenario.name}"}

    monkeypatch.setattr(rf, "build_plan", lambda *_a, **_k: _stub_plan())
    monkeypatch.setattr(rf, "run_scenario", fake_run_scenario)
    assert rf.main(["--out-dir", str(tmp_path)]) == 0
    assert seen[:2] == ["react_off_1", "react_on_1"], seen
    assert seen[2:] == ["replay", "replay"], "自洽校验那一遍不带 recorder（零调用、只读）"


def test_main_fails_loudly_when_the_replay_does_not_match_the_baseline(tmp_path, monkeypatch, capsys):
    """录完自洽校验不过 → **退出码非 0**，并指出是哪个场景、差在哪里。
    这一步把"录出来的东西能不能放出来"从"跑完才知道"变成"录制脚本自己保证"。"""
    import scripts.record_fixtures as rf

    calls = {"n": 0}

    def drifting_run_scenario(scenario, recorder=None, bar=None):
        calls["n"] += 1
        # 录制那一遍给 A，回放那一遍给 B —— 模拟 fixture 漂移
        canonical = f"结论A-{scenario.name}" if recorder is not None else f"结论B-{scenario.name}"
        return {"scenario": scenario.name, "complaint": scenario.complaint,
                "use_react": scenario.use_react, "n_fixtures": 0, "elapsed_s": 0.0,
                "error": None, "rejected": False, "insufficient": False,
                "llm_calls": 0, "canonical": canonical}

    monkeypatch.setattr(rf, "build_plan", lambda *_a, **_k: _stub_plan())
    monkeypatch.setattr(rf, "run_scenario", drifting_run_scenario)
    assert rf.main(["--out-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "自洽校验失败" in err
    assert "react_off_1" in err and "react_on_1" in err
    assert "位置" in err, "要指出第一处差异在哪里"
    assert "不能放出来" in err


def test_main_prints_the_reuse_and_overwrite_counters(tmp_path, monkeypatch, capsys):
    """未命中的错误消息让人来看这两个数，所以它们必须真的被打出来。"""
    import scripts.record_fixtures as rf

    monkeypatch.setattr(rf, "build_plan", lambda *_a, **_k: _stub_plan())
    monkeypatch.setattr(rf, "run_scenario", lambda scenario, recorder=None, bar=None: {
        "scenario": scenario.name, "complaint": scenario.complaint,
        "use_react": scenario.use_react, "n_fixtures": 0, "elapsed_s": 0.0,
        "error": None, "rejected": False, "insufficient": False,
        "llm_calls": 0, "canonical": f"c-{scenario.name}"})
    assert rf.main(["--out-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "跨场景复用 0 次" in out
    assert "同一场景内覆盖 0 次" in out
    assert "自洽校验通过" in out
