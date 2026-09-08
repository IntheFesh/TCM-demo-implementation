"""审计里实测出来的冷启动竞态：惰性单例/惰性加载在两个并发请求下的行为。

全部不下载模型、不联网——SentenceTransformer 用 sys.modules 里塞的假模块挡掉，
HybridRetriever/后端/图存储用会"睡一下"的假类换掉，把竞态窗口撑开到肉眼可见。
"""
import json
import sys
import threading
import time
import types

import numpy as np

from core import llm as llm_mod
from core import retrieval as rmod
from core import retrieval_hybrid as rh
from core import tools
from core.retrieval_hybrid import HybridRetriever
from core.schemas import CaseRecord


def _write_cases(tmp_path, n=3):
    cases = [
        CaseRecord(case_id=f"ye_tianshi-{i:03d}", case_group_id=f"g{i}", physician="ye_tianshi",
                   raw="原文", symptoms=["纳差"])
        for i in range(n)
    ]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([c.model_dump() for c in cases]), encoding="utf-8")
    return path


class _GatedModel:
    """encode(整份语料) 会先置位 entered、再卡在 gate 上等测试放行——把"模型对象
    已经建好、语料向量还没算完"这个窗口撑开。单条 query 的 encode 不卡。"""

    def __init__(self, entered: threading.Event, gate: threading.Event):
        self.entered = entered
        self.gate = gate

    def encode(self, texts, normalize_embeddings=True, convert_to_numpy=True):
        if len(texts) > 1:
            self.entered.set()
            assert self.gate.wait(timeout=5), "测试没有放行 gate"
        return np.full((len(texts), 4), 0.5)


def _install_fake_sentence_transformers(monkeypatch, entered, gate, constructed):
    mod = types.ModuleType("sentence_transformers")

    def _ctor(name):
        constructed.append(name)
        return _GatedModel(entered, gate)

    mod.SentenceTransformer = _ctor
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)


# ---------- DenseRetriever：半初始化状态不能被别的线程看到 ----------


def test_search_during_corpus_encoding_waits_instead_of_reading_half_built_state(tmp_path, monkeypatch):
    """修之前的交错：线程 A 持锁、_model 已赋值、正在给语料编码；线程 B 锁外
    看到 _model 非 None 直接放行，走到 self._embeddings[i] 时是 None，TypeError。
    修之后 B 必须在锁上等 A 编码完，拿到完整结果。"""
    entered, gate, constructed = threading.Event(), threading.Event(), []
    _install_fake_sentence_transformers(monkeypatch, entered, gate, constructed)
    r = HybridRetriever(_write_cases(tmp_path))

    errors, results = [], []

    def loader():
        r._ensure_encoded()

    def searcher():
        try:
            results.append(r.search("纳差", "ye_tianshi", k=1, mode="dense"))
        except Exception as e:  # noqa: BLE001 - 就是要把任何异常抓出来当失败证据
            errors.append(e)

    t_load = threading.Thread(target=loader, daemon=True)
    t_load.start()
    assert entered.wait(5), "loader 没有走进语料编码"
    # 编码进行中：两样东西都不该对外可见——发布顺序是先建好再一起赋值
    assert r._model is None and r._embeddings is None

    t_search = threading.Thread(target=searcher, daemon=True)
    t_search.start()
    t_search.join(0.3)
    assert t_search.is_alive(), "编码没完成时 search 应该在锁上等，不该已经返回或抛错"
    assert errors == []

    gate.set()
    t_load.join(5)
    t_search.join(5)
    assert errors == [], errors
    assert len(results) == 1 and len(results[0]) == 1
    assert constructed == ["BAAI/bge-small-zh-v1.5"], "模型只能加载一次"


def test_ensure_encoded_fast_path_checks_embeddings_not_model(tmp_path, monkeypatch):
    """守住修法本身：只灌 _model 不灌 _embeddings 时，_ensure_encoded 不能短路。"""
    entered, gate, constructed = threading.Event(), threading.Event(), []
    gate.set()
    _install_fake_sentence_transformers(monkeypatch, entered, gate, constructed)
    r = HybridRetriever(_write_cases(tmp_path))
    r._model = object()  # 模拟"模型有了、向量没有"的半成品
    r._ensure_encoded()
    assert r._embeddings is not None
    assert constructed == ["BAAI/bge-small-zh-v1.5"]


# ---------- 模块级单例：并发下只建一份 ----------


def _hammer(fn, n=8):
    barrier = threading.Barrier(n)
    got = []

    def go():
        barrier.wait()
        got.append(fn())

    ts = [threading.Thread(target=go, daemon=True) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert len(got) == n
    return got


def test_get_retriever_builds_exactly_one_instance_under_concurrency(monkeypatch):
    built = []

    class SlowHybrid:
        def __init__(self):
            built.append(self)
            time.sleep(0.1)  # 把"两个线程都看到 None"的窗口撑开

    monkeypatch.setattr(rh, "HybridRetriever", SlowHybrid)
    monkeypatch.setattr(rmod, "_retriever_singleton", None)
    got = _hammer(rmod.get_retriever)
    assert len(built) == 1
    assert all(g is built[0] for g in got)


def test_get_llm_builds_exactly_one_backend_under_concurrency(monkeypatch):
    built = []

    def slow_backend():
        obj = object()
        built.append(obj)
        time.sleep(0.1)
        return obj

    monkeypatch.setattr(llm_mod, "get_backend", slow_backend)
    llm_mod.reset_llm_singleton()
    try:
        got = _hammer(llm_mod.get_llm)
        assert len(built) == 1
        assert all(g is built[0] for g in got)
    finally:
        llm_mod.reset_llm_singleton()


def test_get_graph_store_loads_exactly_once_under_concurrency(monkeypatch):
    loads = []

    class SlowStore:
        def load(self, path):
            loads.append(path)
            time.sleep(0.1)

    monkeypatch.setattr(tools, "NetworkXStore", SlowStore)
    tools.reset_tool_caches()
    try:
        got = _hammer(tools.get_graph_store)
        assert len(loads) == 1
        assert all(g is got[0] for g in got)
    finally:
        tools.reset_tool_caches()  # 别把假的 store 留给后面的用例


# ---------- jieba：写全局词典那一步要在模块级的锁里 ----------


def test_jieba_userdict_is_loaded_under_the_module_level_lock(tmp_path, monkeypatch):
    import jieba

    dict_path = tmp_path / "dict.txt"
    dict_path.write_text("脘痞 100\n", encoding="utf-8")
    monkeypatch.setattr(rh, "JIEBA_DICT_PATH", dict_path)
    seen_locked = []

    def fake_load_userdict(path):
        seen_locked.append(rh._JIEBA_GLOBAL_LOCK.locked())

    monkeypatch.setattr(jieba, "load_userdict", fake_load_userdict)
    r = HybridRetriever(_write_cases(tmp_path))
    r._ensure_jieba()
    assert seen_locked == [True], "load_userdict 改的是进程级的 trie，必须在模块级锁里调"
