"""R3 录制回放的离线测试：ReplayBackend / RecordingBackend / manifest / 前端提示。

**这一轮的验证全部能在沙盒做完**（真机那一步只是"用真实 API 录一次"）：索引命中
与未命中、未命中消息里的定位信息、不退回真实 API、fixture 元信息完整性、
环境变量变化的提示、manifest 三个字段、前端那行小字的渲染。

不用真实 LLM：录制那一侧用一个"返回写死文本"的假内层后端，回放那一侧用手写的
fixture 文件。
"""
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from core.llm import LLMBackend, LLMError
from core.llm_replay import (
    BASELINE_FILENAME, RecordingBackend, ReplayBackend, canonical_outcome,
    current_env, fixture_key, fixtures_dir,
)

ROOT = Path(__file__).resolve().parent.parent


class Probe(BaseModel):
    syndrome: str = Field(min_length=1)
    confidence: int = Field(ge=0, le=100)


class Other(BaseModel):
    """跟 Probe 字段一样，只为验"schema 名进了索引"。"""

    syndrome: str = Field(min_length=1)
    confidence: int = Field(ge=0, le=100)


PROBE_OUTPUT = '{"syndrome": "肝胃不和证", "confidence": 70}'


class _FakeInner(LLMBackend):
    """假的"真实后端"：记下收到的 messages，返回写死的输出。"""

    def __init__(self, output: str = PROBE_OUTPUT, model: str = "deepseek-chat"):
        self.output = output
        self._model = model
        self.calls: list[list[dict]] = []

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs):
        self.calls.append(messages)
        return self.output

    def model_name(self):
        return self._model

    def backend_id(self):
        return "api"

    def comparability_warning(self):
        return None


def _record(tmp_path, system="你是叶天士。", schema=Probe, inner=None):
    inner = inner or _FakeInner()
    recorder = RecordingBackend(inner=inner, out_dir=tmp_path)
    out = recorder.generate(system=system, user="", schema=schema)
    return recorder, inner, out


# ---------- 1. 索引 ----------


def test_index_is_schema_name_plus_system_sha(tmp_path):
    """索引 = (schema 名, sha256(送进模型的 system))。文件名就是这个键。"""
    recorder, inner, _ = _record(tmp_path)
    system_sent = inner.calls[0][0]["content"]
    expected = fixture_key("Probe", system_sent)
    assert (tmp_path / f"{expected}.json").exists()
    assert recorder.keys_written == [expected]
    assert expected.startswith("Probe_") and len(expected.rsplit("_", 1)[1]) == 12


def test_same_schema_different_system_are_different_fixtures(tmp_path):
    _record(tmp_path, system="你是叶天士。")
    _record(tmp_path, system="你是吴鞠通。")
    assert len(list(tmp_path.glob("Probe_*.json"))) == 2


def test_same_system_different_schema_are_different_fixtures(tmp_path):
    """schema 换了就是不同的输入（输出形状本来就不一样）——schema 名必须进索引。
    这里两个 schema 字段完全一样，只有类名不同。"""
    _record(tmp_path, schema=Probe)
    _record(tmp_path, schema=Other)
    assert len(list(tmp_path.glob("Probe_*.json"))) == 1
    assert len(list(tmp_path.glob("Other_*.json"))) == 1


def test_index_is_not_call_order(tmp_path):
    """**刻意不用"第 N 次调用"当索引。** 这条用"调用顺序颠倒"来钉住：按内容
    索引时顺序无关，按次序索引时两条会全部错位。"""
    _record(tmp_path, system="第一步：症状标准化")
    _record(tmp_path, system="第二步：证素推断")
    backend = ReplayBackend(tmp_path)
    # 反着放出来，照样各自命中
    assert backend.generate(system="第二步：证素推断", user="", schema=Probe).confidence == 70
    assert backend.generate(system="第一步：症状标准化", user="", schema=Probe).confidence == 70
    assert backend.n_hits == 2


def test_replay_hits_and_returns_the_recorded_output(tmp_path):
    _record(tmp_path)
    backend = ReplayBackend(tmp_path)
    out = backend.generate(system="你是叶天士。", user="", schema=Probe)
    assert out.syndrome == "肝胃不和证" and out.confidence == 70
    assert backend.n_hits == 1


def test_replay_makes_no_network_calls(tmp_path, monkeypatch):
    """回放不许发任何网络请求。把 OpenAI 客户端构造函数换成"一碰就炸"——
    ReplayBackend 压根不继承 OpenAICompatBackend，这条是防止以后有人"顺手"
    让它 fallback。"""
    import core.llm as llm_mod

    def boom(*a, **kw):
        raise AssertionError("回放模式不许创建任何 API 客户端")

    monkeypatch.setattr(llm_mod.OpenAICompatBackend, "client", property(boom))
    _record(tmp_path)
    assert ReplayBackend(tmp_path).generate(
        system="你是叶天士。", user="", schema=Probe).confidence == 70


# ---------- 2. 未命中 ----------


def test_miss_raises_llm_error_and_does_not_fall_back(tmp_path):
    """未命中抛 LLMError。**不返回空、不退回真实 API**——静默退化会让
    "这是录制的结果"这个声称变成假的，而且录漏了必须暴露出来。"""
    _record(tmp_path, system="录过的")
    backend = ReplayBackend(tmp_path)
    with pytest.raises(LLMError) as e:
        backend.generate(system="没录过的 prompt", user="", schema=Probe)
    assert "不会退回真实 API" in str(e.value)
    assert backend.n_hits == 0


def test_miss_message_has_enough_to_locate_the_problem(tmp_path):
    """消息里要有：schema 名、prompt 前 100 字、sha12、fixtures 目录、已装载条数、
    以及补录办法。够到不用再跑一遍就能判断怎么办。"""
    _record(tmp_path, system="录过的")
    long_system = "没录过的提示词" * 40
    with pytest.raises(LLMError) as e:
        ReplayBackend(tmp_path).generate(system=long_system, user="", schema=Probe)
    msg = str(e.value)
    assert "Probe" in msg
    assert "sha256 前 12 位" in msg
    assert str(tmp_path) in msg
    assert "已装载 1 条" in msg
    assert "record_fixtures" in msg          # 说清怎么补录
    # prompt 预览要截断，不是整段贴上来
    preview_line = next(ln for ln in msg.splitlines() if "system 前 100 字" in ln)
    assert len(preview_line) < 200


def test_miss_message_says_the_directory_is_empty_when_it_is(tmp_path):
    with pytest.raises(LLMError) as e:
        ReplayBackend(tmp_path / "nope").generate(system="x", user="", schema=Probe)
    assert "fixtures 目录是空的或不存在" in str(e.value)


def test_miss_message_reports_env_values_never_seen_while_recording(tmp_path, monkeypatch):
    """**最常见的未命中原因不是"忘了录"，是环境变量跟录制时不一样**（链路走了
    另一条路 -> prompt 跟着变）。差异要直接打出来。"""
    monkeypatch.setenv("USE_REACT", "0")
    _record(tmp_path, system="录过的")
    monkeypatch.setenv("USE_REACT", "1")
    with pytest.raises(LLMError) as e:
        ReplayBackend(tmp_path).generate(system="没录过的", user="", schema=Probe)
    msg = str(e.value)
    assert "录制时从未出现过的取值" in msg
    assert "USE_REACT：录制过的取值 ['0']，现在是 '1'" in msg


def test_env_diff_does_not_false_alarm_on_deliberately_mixed_recordings(
    tmp_path, monkeypatch,
):
    """录制清单**刻意**把开 ReAct 和不开 ReAct 各录一遍，所以同一个目录里
    USE_REACT 天然既有 "1" 又有 "0"。拿其中一份当基准会对另一半误报"环境变了"，
    把人引到错的方向——判据必须是集合归属。"""
    monkeypatch.setenv("USE_REACT", "0")
    _record(tmp_path, system="不开 ReAct 录的")
    monkeypatch.setenv("USE_REACT", "1")
    _record(tmp_path, system="开 ReAct 录的")

    for value in ("0", "1"):
        monkeypatch.setenv("USE_REACT", value)
        with pytest.raises(LLMError) as e:
            ReplayBackend(tmp_path).generate(system="没录过的", user="", schema=Probe)
        assert "USE_REACT" not in str(e.value), f"USE_REACT={value} 被误报成环境差异"


def test_miss_message_surfaces_unloadable_fixture_files(tmp_path):
    """一份因为元信息缺字段而加载失败的 fixture，症状是"明明录过却未命中"。
    静默跳过会让人去重录一遍（白花钱），所以要在未命中消息里点名。"""
    _record(tmp_path)
    (tmp_path / "Probe_deadbeef0000.json").write_text('{"meta": {}}', encoding="utf-8")
    with pytest.raises(LLMError) as e:
        ReplayBackend(tmp_path).generate(system="没录过的", user="", schema=Probe)
    msg = str(e.value)
    assert "有 fixture 文件加载失败" in msg
    assert "Probe_deadbeef0000.json" in msg


def test_filename_and_meta_key_mismatch_is_reported(tmp_path):
    """文件名是给人看的、meta.key 是给程序用的。有人手改了文件名时按 meta.key
    装载，但要说出来——否则"我改了文件名怎么还能命中"会变成一个谜。"""
    recorder, inner, _ = _record(tmp_path)
    original = next(tmp_path.glob("Probe_*.json"))
    original.rename(tmp_path / "Probe_000000000000.json")
    backend = ReplayBackend(tmp_path)
    assert backend.generate(system="你是叶天士。", user="", schema=Probe).confidence == 70
    with pytest.raises(LLMError) as e:
        backend.generate(system="没录过的", user="", schema=Probe)
    assert "文件名与 meta.key" in str(e.value)


def test_non_empty_user_is_refused_on_both_sides(tmp_path):
    """索引不含 user 消息（全项目 11 个调用点的 user 都是空串）。哪天有人加了
    非空 user，两条只有 user 不同的调用会撞同一个键、回放拿出另一条的输出而且
    看起来正常——那种错比未命中难查，所以两侧都宁可炸掉。"""
    recorder = RecordingBackend(inner=_FakeInner(), out_dir=tmp_path)
    with pytest.raises(LLMError, match="只覆盖 system 消息"):
        recorder.generate(system="x", user="病人说他胃疼", schema=Probe)
    _record(tmp_path, system="x")
    with pytest.raises(LLMError, match="只覆盖 system 消息"):
        ReplayBackend(tmp_path).generate(system="x", user="病人说他胃疼", schema=Probe)


def test_underscore_files_are_not_treated_as_fixtures(tmp_path):
    """_baseline.json 是录制附带的元文件。不跳过的话它每次都进 _bad_files，
    让每条未命中消息都带一行假的"加载失败"噪音。"""
    _record(tmp_path)
    (tmp_path / BASELINE_FILENAME).write_text('{"scenario": "x"}', encoding="utf-8")
    with pytest.raises(LLMError) as e:
        ReplayBackend(tmp_path).generate(system="没录过的", user="", schema=Probe)
    assert BASELINE_FILENAME not in str(e.value)


# ---------- 3. fixture 元信息 ----------


def test_fixture_carries_every_piece_of_provenance(tmp_path, monkeypatch):
    """每份 fixture 要能独立回答"这段输出是怎么来的"：模型、后端、prompt 版本、
    语料指纹、录制时间、commit、以及录制时的相关环境变量。"""
    monkeypatch.setenv("USE_REACT", "1")
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    _record(tmp_path)
    data = json.loads(next(tmp_path.glob("Probe_*.json")).read_text(encoding="utf-8"))
    meta = data["meta"]
    assert meta["schema_name"] == "Probe"
    assert meta["model"] == "deepseek-chat" and meta["backend"] == "api"
    assert meta["prompt_version"] == "v1"
    assert meta["recorded_at"].endswith("Z") and meta["recorded_at"].count("-") == 2
    assert set(meta["env"]) == set(current_env())
    assert meta["env"]["USE_REACT"] == "1" and meta["env"]["RETRIEVER_MODE"] == "hybrid"
    assert "cases_sha256" in meta and "git_commit" in meta
    assert meta["system_preview"] and len(meta["system_preview"]) <= 100
    # 存的是模型返回的**原始文本**，不是解析后的对象
    assert data["output"] == PROBE_OUTPUT


def test_env_records_none_for_unset_rather_than_empty_string(tmp_path, monkeypatch):
    """"没设置"和"设成空串"在 os.environ.get 之后就分不开了，而前者是默认行为、
    后者是有人显式设成了空——回放未命中时这个区别要看得见。"""
    monkeypatch.delenv("USE_REACT", raising=False)
    monkeypatch.setenv("FAST_MODE", "")
    _record(tmp_path)
    meta = json.loads(next(tmp_path.glob("Probe_*.json")).read_text(encoding="utf-8"))["meta"]
    assert meta["env"]["USE_REACT"] is None
    assert meta["env"]["FAST_MODE"] == ""


def test_api_key_is_never_recorded(tmp_path, monkeypatch):
    """**这一轮的动机之一就是不想让 key 跟着部署走**，fixture 是要提交进版本
    控制的，绝不能把 key 录进去。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-must-not-appear")
    _record(tmp_path)
    text = next(tmp_path.glob("Probe_*.json")).read_text(encoding="utf-8")
    assert "sk-must-not-appear" not in text
    assert "LLM_API_KEY" not in text


def test_recording_forwards_provenance_to_the_real_backend(tmp_path):
    """录制是包装：manifest 里记的必须是**真实**跑出这些输出的那个模型，
    不是 "recording"。"""
    recorder, inner, _ = _record(tmp_path, inner=_FakeInner(model="claude-sonnet-5"))
    assert recorder.model_name() == "claude-sonnet-5"
    assert recorder.backend_id() == "api"


def test_retry_overwrites_with_the_output_that_finally_validated(tmp_path):
    """generate() 校验失败会带回灌消息重试，messages[0] 不变所以键不变——
    最后写进去的必须是最终通过校验的那份输出，先写进去的非法输出被覆盖。"""
    class _FlakyInner(_FakeInner):
        def __init__(self):
            super().__init__()
            self.n = 0

        def _complete(self, messages, temperature, max_tokens=None, schema=None,
                      physician=None, **kwargs):
            self.n += 1
            return "这不是 JSON" if self.n == 1 else PROBE_OUTPUT

    recorder = RecordingBackend(inner=_FlakyInner(), out_dir=tmp_path)
    recorder.generate(system="你是叶天士。", user="", schema=Probe)
    assert recorder.n_written == 2 and len(recorder.keys_written) == 1
    data = json.loads(next(tmp_path.glob("Probe_*.json")).read_text(encoding="utf-8"))
    assert data["output"] == PROBE_OUTPUT
    # 回放这条 fixture 能过校验（存非法输出的话这里会红）
    assert ReplayBackend(tmp_path).generate(
        system="你是叶天士。", user="", schema=Probe).confidence == 70


# ---------- 4. manifest 与 comparability_warning ----------


def test_backend_id_is_replay_and_model_does_not_impersonate(tmp_path):
    """**不许伪装成实时调用。** backend 是 "replay"，model 如实说这是回放、
    录制时用的模型写在括号里。"""
    _record(tmp_path)
    backend = ReplayBackend(tmp_path)
    assert backend.backend_id() == "replay"
    assert backend.model_name() == "replay(deepseek-chat)"


def test_comparability_warning_states_it_is_not_live(tmp_path):
    _record(tmp_path)
    warning = ReplayBackend(tmp_path).comparability_warning()
    assert "非实时调用" in warning
    assert "录制" in warning


def test_replay_info_reports_the_recording_provenance(tmp_path):
    _record(tmp_path)
    info = ReplayBackend(tmp_path).replay_info()
    assert info["model"] == "deepseek-chat"
    assert info["n_fixtures"] == 1 and info["n_batches"] == 1
    assert info["recorded_at"] and info["fixtures_dir"] == str(tmp_path)


def test_replay_info_lists_every_model_when_batches_are_mixed(tmp_path):
    """混了几次录制（模型可能不一样）时全列出来、不挑一个当代表，
    recorded_at 取**最早**那份——报最新的会让"这份 demo 有多旧"看起来比实际新。"""
    _record(tmp_path, system="甲", inner=_FakeInner(model="deepseek-chat"))
    _record(tmp_path, system="乙", inner=_FakeInner(model="claude-sonnet-5"))
    info = ReplayBackend(tmp_path).replay_info()
    assert "deepseek-chat" in info["model"] and "claude-sonnet-5" in info["model"]
    assert info["recorded_at"] <= info["recorded_at_latest"]


def test_replay_info_is_none_without_fixtures(tmp_path):
    backend = ReplayBackend(tmp_path / "nope")
    assert backend.replay_info() is None
    assert "跑不起来" in backend.comparability_warning()


def test_live_backends_report_no_replay_info():
    """实时后端的 replay_info() 是 None——manifest 里那个字段就是靠它区分
    "这次是回放"和"这次是现场跑"。"""
    from core.llm import OpenAICompatBackend

    assert OpenAICompatBackend().replay_info() is None


def test_manifest_carries_replayed_from(tmp_path, monkeypatch):
    """manifest 的三件事：backend=replay、replayed_from 非空、
    comparability_warning 说明非实时。"""
    import core.llm as llm_mod
    from core import chain

    _record(tmp_path)
    monkeypatch.setattr(llm_mod, "_llm_singleton", ReplayBackend(tmp_path))
    manifest = chain._build_manifest(elapsed_ms=1, llm_calls=0)
    assert manifest["backend"] == "replay"
    assert manifest["replayed_from"]["model"] == "deepseek-chat"
    assert "非实时调用" in manifest["comparability_warning"]
    assert manifest["model"].startswith("replay(")


def test_get_backend_dispatches_replay_mode(monkeypatch, tmp_path):
    from core.llm import get_backend

    monkeypatch.setenv("LLM_MODE", "replay")
    monkeypatch.setenv("REPLAY_FIXTURES_DIR", str(tmp_path))
    backend = get_backend()
    assert isinstance(backend, ReplayBackend)
    assert backend.backend_id() == "replay"


def test_fixtures_dir_defaults_to_repo_fixtures(monkeypatch):
    monkeypatch.delenv("REPLAY_FIXTURES_DIR", raising=False)
    assert fixtures_dir() == ROOT / "fixtures"
    monkeypatch.setenv("REPLAY_FIXTURES_DIR", "/tmp/somewhere")
    assert fixtures_dir() == Path("/tmp/somewhere")


# ---------- 5. canonical：录制与验证共用同一个定义 ----------


def test_canonical_excludes_timing_so_the_comparison_is_stable():
    """manifest 的 elapsed_ms 每次都不同，算进比对会让"逐字节一致"永远失败、
    这个验证也就废了。"""
    from core.schemas import S1Normalize

    base = {"s1": S1Normalize(symptoms=["纳差"], unmapped=[]), "rejected": False,
            "results": [], "divergence": None,
            "manifest": {"elapsed_ms": 1, "llm_calls": 6}}
    slow = {**base, "manifest": {"elapsed_ms": 99999, "llm_calls": 6}}
    assert canonical_outcome(base) == canonical_outcome(slow)


def test_canonical_is_sensitive_to_the_model_output():
    from core.schemas import S1Normalize

    a = {"s1": S1Normalize(symptoms=["纳差"], unmapped=[]), "results": []}
    b = {"s1": S1Normalize(symptoms=["乏力"], unmapped=[]), "results": []}
    assert canonical_outcome(a) != canonical_outcome(b)


def test_canonical_is_defined_in_exactly_one_place():
    """录制和验证必须用同一个定义——各写一份的话"逐字节一致"就取决于两段代码
    有没有漂移。用源码检查钉住：两个脚本都只能 import 它。"""
    for script in ("record_fixtures.py", "verify_replay.py"):
        src = (ROOT / "scripts" / script).read_text(encoding="utf-8")
        assert "canonical_outcome" in src
        assert "def canonical_outcome" not in src, f"{script} 里不许再写一份"


# ---------- 6. 录制脚本的清单 ----------


def test_record_plan_covers_everything_the_round_asked_for():
    from scripts.record_fixtures import build_plan

    plan = build_plan()
    names = [s.name for s in plan]
    queries = (ROOT / "tests" / "queries.txt").read_text(encoding="utf-8").splitlines()
    queries = [q for q in queries if q.strip()]
    # queries.txt 全部 10 条（含第 10 条安全拦截），开 ReAct 和不开各一遍
    assert sum(1 for n in names if n.startswith("react_off_")) == len(queries)
    assert sum(1 for n in names if n.startswith("react_on_")) == len(queries)
    assert "insufficient" in names and "followup" in names
    off = [s for s in plan if s.name.startswith("react_off_")]
    on = [s for s in plan if s.name.startswith("react_on_")]
    assert all(not s.use_react for s in off) and all(s.use_react for s in on)
    # 同一批主诉两种 ReAct 状态都录到了（prompt 不同，fixture 不共用）
    assert {s.complaint for s in off} == {s.complaint for s in on} == set(queries)
    # 安全拦截那条（黑便）在清单里
    assert any("黑色柏油样便" in s.complaint for s in plan)
    # 信息不足那条
    assert any(s.complaint == "胸闷气短" for s in plan)
    # 追问那条带 ScriptedPatient（确定性回答，下游 prompt 才可复现）
    followup = next(s for s in plan if s.name == "followup")
    assert followup.followup_present
    from eval.patient_sim import ScriptedPatient

    assert isinstance(followup.ask_fn(), ScriptedPatient)
    assert next(s for s in plan if s.name == "insufficient").ask_fn() is None


def test_record_dry_run_makes_no_calls_and_reports_the_budget(capsys, monkeypatch):
    """--dry-run 要能在没有 API key 的机器上跑，并报出预估调用数——300 次调用
    之前先让人看一眼清单。"""
    import core.llm as llm_mod
    from scripts import record_fixtures

    def boom():
        raise AssertionError("--dry-run 不许构造任何后端")

    monkeypatch.setattr(llm_mod, "get_backend", boom)
    assert record_fixtures.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "预估" in out and "次调用" in out
    assert "react_off_1" in out and "react_on_1" in out
    assert "不发起任何调用" in out


def test_record_refuses_to_record_while_in_replay_mode(monkeypatch, tmp_path):
    """LLM_MODE=replay 时录制等于拿回放当录制源，录出来是空壳。"""
    from scripts import record_fixtures

    monkeypatch.setenv("LLM_MODE", "replay")
    assert record_fixtures.main(["--out-dir", str(tmp_path)]) == 1


def test_record_only_rejects_unknown_scenario_names(tmp_path):
    from scripts import record_fixtures

    with pytest.raises(SystemExit, match="不存在"):
        record_fixtures.main(["--only", "nope", "--dry-run"])


def test_verify_replay_returns_2_when_nothing_is_recorded(tmp_path, monkeypatch, capsys):
    """没录过 ≠ 验失败。退出码 2 跟 1 分开，CI 里能区分"忘了录"和"回放不一致"。"""
    from scripts import verify_replay

    monkeypatch.setenv("REPLAY_FIXTURES_DIR", str(tmp_path))
    assert verify_replay.main([]) == 2
    assert "还没录" in capsys.readouterr().err


# ---------- 7. 前端那行小字 ----------

DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
globalThis.fetch = () => Promise.reject(new Error("no net"));
"""


def _demo_text(demo_mode) -> str | None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    tail = ("\nconsole.log(JSON.stringify(demoModeText("
            + json.dumps(demo_mode, ensure_ascii=False) + ")));")
    proc = subprocess.run(["node", "-e", DOM_STUB + script + tail],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_frontend_shows_the_demo_mode_line():
    """这一条**不是可选的**：不能让人以为是现场跑的。"""
    text = _demo_text({
        "recorded_at": "2026-09-14T03:04:05Z", "model": "deepseek-chat", "n_fixtures": 120,
        "notice": "演示模式：结果来自 2026-09-14 录制的真实推理（deepseek-chat），非实时调用",
    })
    assert text == ("演示模式：结果来自 2026-09-14 录制的真实推理"
                   "（deepseek-chat），非实时调用")


def test_frontend_line_is_absent_in_live_mode():
    """实时调用时不显示——没有人需要被告知默认行为。"""
    assert _demo_text(None) is None


def test_frontend_falls_back_to_its_own_wording_without_notice():
    """服务端漏给 notice 时兜底句也必须说清"非实时调用"。"""
    text = _demo_text({"recorded_at": "2026-09-14T03:04:05Z", "model": "deepseek-chat"})
    assert "2026-09-14" in text and "deepseek-chat" in text
    assert "非实时调用" in text


def test_frontend_fallback_does_not_invent_a_date_or_model():
    text = _demo_text({})
    assert "未记录日期" in text and "未记录模型" in text
    assert "非实时调用" in text


def test_demo_banner_is_outside_the_role_filtered_area():
    """横幅挂在 header 之后、tab-bar 之前——不在 results 里面，也不依赖
    manifest（manifest 只给 researcher，挂它等于对真正的观众隐身）。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert '<div id="demo-mode-banner"></div>' in html
    assert html.index('id="demo-mode-banner"') < html.index('id="tab-bar"')
    assert html.index('id="demo-mode-banner"') > html.index("</header>")


def test_api_demo_mode_field_survives_every_role_filter():
    """demo_mode 必须活过所有角色的裁剪——它是给 patient/doctor/student 看的。"""
    from api.main import _filter_response_by_role

    for role in ("researcher", "student", "doctor", "patient"):
        response = _filter_response_by_role(
            {"demo_mode": {"notice": "演示模式：……"}, "manifest": {"x": 1},
             "results": [], "divergence": None, "s1": {"symptoms": []}},
            role, [],
        )
        assert response["demo_mode"], f"role={role} 把 demo_mode 裁掉了"


def test_api_demo_mode_is_none_on_a_live_backend(monkeypatch):
    import core.llm as llm_mod
    from api.main import demo_mode_info

    monkeypatch.setattr(llm_mod, "_llm_singleton", _FakeInner())
    assert demo_mode_info() is None


def test_api_demo_mode_notice_is_built_server_side(monkeypatch, tmp_path):
    """措辞由服务端给：措辞是诚实性的一部分，不该有两个版本。"""
    import core.llm as llm_mod
    from api.main import demo_mode_info

    _record(tmp_path)
    monkeypatch.setattr(llm_mod, "_llm_singleton", ReplayBackend(tmp_path))
    info = demo_mode_info()
    assert "非实时调用" in info["notice"] and "deepseek-chat" in info["notice"]
    assert info["model"] == "deepseek-chat" and info["n_fixtures"] == 1


def test_health_reports_demo_mode(monkeypatch, tmp_path):
    """页面一加载就该知道这是演示模式，不是跑完一次问诊才被告知。"""
    from fastapi.testclient import TestClient

    import core.llm as llm_mod
    from api.main import app

    _record(tmp_path)
    monkeypatch.setattr(llm_mod, "_llm_singleton", ReplayBackend(tmp_path))
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "非实时调用" in body["demo_mode"]["notice"]


# ---------- 8. 端到端：真的录一次 consult，再回放，逐字节一致 ----------
#
# 上面那些是单元层。这一条是这一轮唯一能在沙盒里做的**端到端**验证：用假的
# "真实后端"跑完整条 consult()（S1 + S2 + 两位医家 S3 + 残差），把 fixture 录下来，
# 换成 ReplayBackend 再跑一遍，断言 canonical_outcome 逐字节相同。
#
# 它能抓到单元测试抓不到的一类问题：链路里某一步的 prompt 带了录制时才有的东西
# （时间戳、随机序、并发顺序），录进去了但回放时算出的 key 不一样 -> 未命中。
# 真机那一步（用真实 API 录）验的是同一件事，只是换成真模型。

_RAW_BY_SCHEMA = {
    "S1Normalize": '{"symptoms": ["纳差", "乏力"], "tongue": "淡红", "pulse": "细弱", "unmapped": []}',
    "S2Elements": ('{"elements": [{"element": "脾", "kind": "location", '
                   '"supporting_symptoms": ["纳差"], "confidence": "high"}], '
                   '"unexplained_symptoms": []}'),
}


def _s3_raw(system: str) -> str:
    """按 system 里出现的医家名给不同的方——两位医家的输出必须不同，否则
    "回放有没有把两位医家的 fixture 弄混"这件事测不出来。"""
    herbs = ["党参", "白术"] if "叶天士" in system else ["半夏", "茯苓"]
    items = ", ".join(
        f'{{"name": "{h}", "role": "{"君" if i == 0 else "臣"}"}}'
        for i, h in enumerate(herbs))
    return json.dumps(json.loads(
        '{"syndrome": "脾胃气虚", "reasoning": "从证素到证型", '
        '"reasoning_plain": "通俗版", "treatment_principle": "健脾益气", '
        '"formula_candidates": [{"name": "四君子汤", "source": "classic", '
        '"confidence": "high", "rationale": "对应本证病机", '
        '"herb_items": [' + items + ']}], '
        '"cited_case_ids": ["ye_tianshi-001"]}'), ensure_ascii=False)


class _FakeChainBackend(LLMBackend):
    """假的"真实后端"：按 schema 名返回合法的原始 JSON 文本。**返回文本而不是
    对象**——录制录的就是原始文本，走对象会绕过这条路。"""

    def __init__(self):
        self.n_calls = 0

    def _complete(self, messages, temperature, max_tokens=None, schema=None,
                  physician=None, **kwargs):
        self.n_calls += 1
        name = schema.__name__ if schema else ""
        if name in _RAW_BY_SCHEMA:
            return _RAW_BY_SCHEMA[name]
        if name.startswith("S3Syndrome"):
            return _s3_raw(messages[0]["content"])
        raise AssertionError(f"这个假后端没有为 {name} 准备输出")

    def model_name(self):
        return "deepseek-chat"

    def backend_id(self):
        return "api"


@pytest.fixture
def _two_physicians(monkeypatch):
    from core import chain
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})


def test_end_to_end_record_then_replay_is_byte_identical(tmp_path, monkeypatch,
                                                         _two_physicians):
    import core.llm as llm_mod
    from core import chain
    from tests.test_chain import FakeRetriever, _fake_cases

    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.setenv("USE_REACT", "0")

    # --- 录 ---
    inner = _FakeChainBackend()
    recorder = RecordingBackend(inner=inner, out_dir=tmp_path)
    monkeypatch.setattr(llm_mod, "_llm_singleton", recorder)
    recorded = chain.consult("纳差乏力", use_react=False)
    recorded_canonical = canonical_outcome(recorded)
    assert inner.n_calls > 0 and recorder.n_written == inner.n_calls
    assert recorded["manifest"]["backend"] == "api"          # 录制时如实是真后端
    assert recorded["manifest"]["replayed_from"] is None

    # --- 放 ---
    replay = ReplayBackend(tmp_path)
    monkeypatch.setattr(llm_mod, "_llm_singleton", replay)
    replayed = chain.consult("纳差乏力", use_react=False)

    assert canonical_outcome(replayed) == recorded_canonical   # 逐字节一致
    assert replay.n_hits == inner.n_calls                     # 每一次调用都命中
    assert replayed["manifest"]["backend"] == "replay"
    assert replayed["manifest"]["replayed_from"]["model"] == "deepseek-chat"
    assert "非实时调用" in replayed["manifest"]["comparability_warning"]
    # 两位医家的方没有被弄混
    by_physician = {r["physician"]: r["s3"].herbs for r in replayed["results"]}
    assert by_physician["ye_tianshi"] == ["党参", "白术"]
    assert by_physician["wu_jutong"] == ["半夏", "茯苓"]


def test_end_to_end_replay_of_an_unrecorded_complaint_raises(tmp_path, monkeypatch,
                                                             _two_physicians):
    """录了 A 主诉、回放 B 主诉：必须在第一步（S1）就报未命中，不是悄悄给个
    看起来正常的结果。"""
    import core.llm as llm_mod
    from core import chain
    from tests.test_chain import FakeRetriever, _fake_cases

    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.setenv("USE_REACT", "0")
    monkeypatch.setattr(llm_mod, "_llm_singleton",
                        RecordingBackend(inner=_FakeChainBackend(), out_dir=tmp_path))
    chain.consult("纳差乏力", use_react=False)

    monkeypatch.setattr(llm_mod, "_llm_singleton", ReplayBackend(tmp_path))
    with pytest.raises(LLMError) as e:
        chain.consult("完全没录过的主诉", use_react=False)
    assert "回放未命中" in str(e.value)
    assert "S1Normalize" in str(e.value)


def test_end_to_end_react_on_and_off_do_not_share_fixtures(tmp_path, monkeypatch,
                                                           _two_physicians):
    """开 ReAct 走 s3_react 模板、prompt 完全不同，所以 fixture 不共用——
    这就是录制清单要"各录一遍"的原因。只录了不开 ReAct 的，开着回放必须未命中。"""
    import core.llm as llm_mod
    from core import chain
    from tests.test_chain import FakeRetriever, _fake_cases

    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    monkeypatch.setattr(llm_mod, "_llm_singleton",
                        RecordingBackend(inner=_FakeChainBackend(), out_dir=tmp_path))
    chain.consult("纳差乏力", use_react=False)

    monkeypatch.setattr(llm_mod, "_llm_singleton", ReplayBackend(tmp_path))
    with pytest.raises(LLMError, match="回放未命中"):
        chain.consult("纳差乏力", use_react=True)


# ---------- R6-3：录制清单必须盖住 DEMO 让人粘贴的每一条主诉 ----------


def test_every_complaint_demo_tells_you_to_paste_is_in_the_record_plan():
    """**回放按主诉原文的哈希索引，演示时打的字跟录的字差一个标点都是未命中**
    （`ReplayBackend` 会抛 LLMError，不会静默退回真实 API）。所以 DEMO.md 那个
    「复制粘贴用」代码块里的每一条，都必须在录制清单里。

    方向只查 DEMO → 清单，不查反向：清单里有些场景（`insufficient` 那条）是为了
    覆盖回放路径录的，不是给演示用的，要求它们出现在 DEMO 里没有道理。

    `triage`（患者模式导诊）那条就是这条测试的由来——它不在 tests/queries.txt 里，
    漏加进清单就会在演示到第 5 点时当场 LLMError。
    """
    import re
    from pathlib import Path

    import scripts.record_fixtures as rf

    demo = (Path(__file__).resolve().parent.parent / "DEMO.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(A: .+?)\n```", demo, re.S)
    assert block, "DEMO.md 里找不到「复制粘贴用」的主诉代码块"
    pasted = [line.split(": ", 1)[1].strip()
              for line in block.group(1).splitlines() if ": " in line]
    assert len(pasted) == 3, pasted
    recorded = {s.complaint for s in rf.build_plan()}
    for complaint in pasted:
        assert complaint in recorded, f"DEMO 让人粘贴 {complaint!r}，但录制清单里没有它"


def test_triage_complaint_is_not_in_queries_txt_and_must_be_added_explicitly():
    """导诊那条不在 tests/queries.txt 里（那 10 条都是脾胃门的辨证主诉），
    所以 build_plan 必须显式把它加进去——这条测试钉住「显式」这件事，
    哪天有人把它塞进 queries.txt 或者删掉这个场景，都会红。"""
    import scripts.record_fixtures as rf

    assert rf.TRIAGE_COMPLAINT not in rf._queries()
    assert rf.TRIAGE_COMPLAINT in {s.complaint for s in rf.build_plan()}
    assert "triage" in {s.name for s in rf.build_plan()}
