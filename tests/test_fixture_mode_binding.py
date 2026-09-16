"""R28-C：fixture 与检索模式绑定。

**为什么必须绑定**：fixture 的键是 `sha256(送进模型的 system)`
（`core/llm_replay.fixture_key`），而 full_context 下 S3 的 system 由
`core/context_prefix.assemble()` 拼出、含 18 万 token 的知识前缀。
所以**同一条主诉在两个检索模式下算出的键完全不同**——在 full_context 下录完，
一旦退回 hybrid，278 条 fixture 一条都命中不了，演示保险归零，
而症状是"演示到一半开始报未命中"，不是任何地方报错。

R21 把默认模式换成 full_context 之后这件事就成立了，只是没人撞上：
段 6（录制）在剧本里不钉模式，段 9 的闸门不过时脚本又提示退回 hybrid。
这个文件把"录的时候是什么模式"写进 fixture 目录，并在回放前比对。
"""
from __future__ import annotations

import json

import pytest

from core.llm import LLMError
from core.llm_replay import (
    MANIFEST_FILENAME,
    ReplayBackend,
    read_manifest,
    require_matching_mode,
    write_manifest,
)


def _fixture_dir(tmp_path, mode: str | None):
    d = tmp_path / "fixtures"
    d.mkdir()
    if mode is not None:
        write_manifest(d, retriever_mode=mode, n_fixtures=3)
    return d


def test_write_manifest_records_what_the_batch_was_recorded_under(tmp_path):
    d = _fixture_dir(tmp_path, "hybrid")
    manifest = read_manifest(d)
    assert manifest is not None
    assert manifest.retriever_mode == "hybrid"
    assert manifest.prompt_version == "v1"
    assert manifest.recorded_at, "没有录制时间的 manifest 说不清这批是什么时候录的"
    assert manifest.n_fixtures == 3
    # cases_sha256 可能取不到（没有 cases.json），但字段必须在
    assert "cases_sha256" in manifest.model_dump()


def test_the_manifest_file_is_underscore_prefixed_so_it_is_not_loaded_as_a_fixture(
        tmp_path, monkeypatch):
    """下划线开头的文件被 ReplayBackend._load 跳过——跟 `_baseline.json` 同一条规矩。
    不跳过的话它每次都会进 _bad_files，让每条未命中消息都带一行假的"加载失败"。"""
    assert MANIFEST_FILENAME.startswith("_")
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")   # 跟这批的录制模式一致
    d = _fixture_dir(tmp_path, "hybrid")
    assert (d / MANIFEST_FILENAME).exists()
    assert ReplayBackend(d).fixtures == {}


def test_a_mode_mismatch_raises_and_names_both_modes(tmp_path):
    """**这一条是这个文件存在的理由。** 不一致必须立刻报错，
    而且错误信息要写清是哪两个模式、为什么必然不命中、两条出路各是什么。"""
    d = _fixture_dir(tmp_path, "full_context")
    with pytest.raises(LLMError) as exc:
        require_matching_mode(d, current_mode="hybrid")
    msg = str(exc.value)
    assert "full_context" in msg and "hybrid" in msg
    assert "key" in msg or "键" in msg
    assert "重录" in msg


def test_the_replay_backend_refuses_to_start_on_a_mismatch(tmp_path, monkeypatch):
    """回放后端启动时就要炸，不是等到某一条未命中才炸——后者会先跑掉半场演示。"""
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    d = _fixture_dir(tmp_path, "full_context")
    backend = ReplayBackend(d)
    with pytest.raises(LLMError):
        _ = backend.fixtures


def test_a_matching_mode_loads_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("RETRIEVER_MODE", "hybrid")
    d = _fixture_dir(tmp_path, "hybrid")
    assert ReplayBackend(d).fixtures == {}
    assert require_matching_mode(d) is None


def test_a_fixture_dir_without_a_manifest_warns_but_still_works(tmp_path, capsys):
    """旧的 fixture 目录（R28 之前录的）没有 manifest。**不能因此拒绝回放**
    ——那会让一批本来能用的录音在演示前一刻变成不可用；但要给一句可操作的提示。"""
    d = _fixture_dir(tmp_path, None)
    assert require_matching_mode(d, current_mode="hybrid") is None
    err = capsys.readouterr().err
    assert MANIFEST_FILENAME in err
    assert "record_fixtures" in err, "提示里要有写 manifest 的命令"


def test_the_current_mode_comes_from_the_one_place_that_resolves_it(monkeypatch, tmp_path):
    """当前模式问 `core.retrieval_hybrid.effective_mode()`，不自己读环境变量——
    默认值只有那一处解析（R21 把默认换成 full_context 就是改的那一行）。"""
    import core.llm_replay as mod

    d = _fixture_dir(tmp_path, "full_context")
    monkeypatch.setenv("RETRIEVER_MODE", "full_context")
    assert require_matching_mode(d) is None
    monkeypatch.setenv("RETRIEVER_MODE", "bm25")
    with pytest.raises(LLMError):
        require_matching_mode(d)
    src = open(mod.__file__, encoding="utf-8").read()
    assert "effective_mode" in src


def test_record_fixtures_writes_the_manifest_after_recording(tmp_path):
    """录完就写，不靠人记得补。参数形状跟脚本里调用的一致。"""
    import scripts.record_fixtures as rf

    assert hasattr(rf, "write_run_manifest")
    path = rf.write_run_manifest(tmp_path, n_fixtures=7)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["n_fixtures"] == 7
    assert data["retriever_mode"]


def test_verify_replay_exits_with_its_own_code_on_a_mode_mismatch():
    """`verify_replay` 在模式不一致时退出码 **3**，跟"还没录"（2）分开。

    两者要做的事完全不同：3 要切模式或重录，2 要去录。共用一个退出码的话，
    上机剧本段 6 挂了之后人会按错的方向查。
    """
    src = open("scripts/verify_replay.py", encoding="utf-8").read()
    body = src[src.index("require_matching_mode(fixtures_dir())"):]
    assert "return 3" in body[:400]
    assert "return 2" in src, "「还没录」那条路还在"


def test_record_fixtures_can_backfill_a_manifest_without_recording(tmp_path, capsys):
    """R28 之前录的那批没有 manifest。补一份不该要重录一遍（278 次调用）。
    但**补的是"当前模式"**，所以脚本要提醒这一点——补错了等于把错的模式钉上去。"""
    import scripts.record_fixtures as rf

    (tmp_path / "S3Syndrome_abc123456789.json").write_text("{}", encoding="utf-8")
    rc = rf.main(["--write-manifest", "--out-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert MANIFEST_FILENAME in out
    assert "只在你确定的时候用" in out
    manifest = read_manifest(tmp_path)
    assert manifest is not None and manifest.n_fixtures == 1
