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
import pytest

from core import llm as llm_mod
from core import retrieval as rmod
from core import retrieval_hybrid as rh
from core import tools
from core.retrieval_hybrid import HybridRetriever
from core.schemas import CaseRecord


@pytest.fixture(autouse=True)
def _isolate_process_level_retriever_state():
    """本文件的测试往 `sys.modules` 里塞假 sentence_transformers、起后台线程、
    碰模块级单例——三样都是**进程级**状态，不隔离就会互相串。跟
    `tests/test_chain.py::_pin_two_physicians` 同一个形状：autouse，只管本文件。

    **它修的是一个真实发生过的串扰**（AutoDL 上）：
    `tests/test_api_stream.py`（收集序第 5）起真实 uvicorn，lifespan 的 `_warmup`
    线程在**单例**检索器上编码 941 条语料要几十秒。那时 `_encode_lock` 还是
    `DenseRetriever` 的**类属性**（进程级），于是本文件（收集序第 11）里那些用
    自己实例的测试全被那把锁挡住：
      - `entered.wait(5)` 超时（loader 压根没走进编码），
      - 而第一条测试留下的 daemon 线程晚一步拿到锁、跑进**下一条测试**刚装好的
        假模块里，给那条测试的 `constructed` 多记了一笔。
    根因已经在 `core/retrieval.py` 修掉（锁改成每实例一把）。这个夹具补的是第二
    重保险：**不让任何一条测试的残留线程活到下一条测试里**。

    jieba 的全局 trie 这里不用管——本文件只走 dense 一路，不碰 bm25，
    `_ensure_jieba` 根本不会被调到。写清楚"为什么不管"，免得下次有人以为漏了。
    """
    _assert_no_leftover_fake("进这条测试时")
    before = set(threading.enumerate())
    rmod._retriever_singleton = None
    yield
    rmod._retriever_singleton = None
    # 残留线程 join 掉再进下一条测试。**不 assert**：这一条要是失败了，它会盖住
    # 测试本身的失败原因，而隔离才是这里的职责，检测不是。
    leaked = [t for t in threading.enumerate() if t not in before]
    for t in leaked:
        t.join(3)
    still_alive = [t.name for t in leaked if t.is_alive()]
    if still_alive:
        print(f"[warn] 这条测试留下了还活着的线程：{still_alive}——"
              "它们可能写进下一条测试的夹具里", file=sys.stderr)
    # **不在这里再断言一次假模块已还原。** monkeypatch 的还原发生在本夹具 teardown
    # **之后**（实测：在这个时点上假模块还在 sys.modules 里），所以"跑完之后还在"
    # 是正常的、不是泄漏。真正要防的是它活到**下一条测试**——那由下一条测试 setup
    # 时的 _assert_no_leftover_fake 抓，每条测试进来都查一次，一条都漏不掉。


def _assert_no_leftover_fake(when: str) -> None:
    """`sys.modules` 里不许留着我们塞的**假** sentence_transformers。

    判据是假模块身上的标记，不是"跟进来时是不是同一个对象"——真模块会在测试
    过程中被别的代码惰性 import 进来（`_load()` 里那句 `from sentence_transformers
    import ...` 在 monkeypatch 还原之后再跑一次就会拉真的），那是正常的、无害的；
    **有害的只有"上一条测试的假模块活到了下一条"**，因为那会让下一条测试往上一条
    的 `constructed` 列表里记账——AutoDL 上那个"多了一份"就是这么来的。
    """
    mod = sys.modules.get("sentence_transformers")
    assert not getattr(mod, "_tcm_fake", False), (
        f"{when}，sys.modules 里还留着上一条测试塞的假 sentence_transformers")


def _write_cases(tmp_path, n=3):
    tmp_path.mkdir(parents=True, exist_ok=True)
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
    # 打个标记，让 _assert_no_leftover_fake 能认出"这是我们塞的假的"
    mod._tcm_fake = True
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)


# ---------- 编码锁的作用域：每实例一把，不是进程级 ----------


def test_encode_lock_is_per_instance_not_shared_across_the_class(tmp_path):
    """**这条钉住的是 AutoDL 上那两条挂掉的根因。**

    `_encode_lock` 保护的是 `self._model` / `self._embeddings`——per-instance 的
    状态，锁的作用域就该是 per-instance。它原来是 `DenseRetriever` 的**类属性**，
    于是 api/main.py 的预热线程在**单例**上编码 941 条语料的几十秒里，任何别的
    实例调 `_ensure_encoded` 都得干等。沙盒上看不出来（没有 cases.json，预热几毫秒
    就跳过），AutoDL 上就挂。
    """
    a = HybridRetriever(_write_cases(tmp_path))
    b = HybridRetriever(_write_cases(tmp_path))
    assert a._encode_lock is not b._encode_lock
    assert "_encode_lock" not in vars(type(a)), "锁又变回类属性了"


def test_one_retrievers_encoding_does_not_block_another_instance(tmp_path, monkeypatch):
    """行为判据，比"是不是同一个对象"更硬：A 正在编码（锁被占着）时，
    B 必须能自己编码完，不用等 A。"""
    entered_a, gate_a, constructed = threading.Event(), threading.Event(), []
    _install_fake_sentence_transformers(monkeypatch, entered_a, gate_a, constructed)
    a = HybridRetriever(_write_cases(tmp_path, n=3))
    # B 的语料只有 1 条：假模型只在 len(texts) > 1 时卡 gate，所以 B 自己的编码
    # 不会被 gate 挡住——这样"B 卡住了"就只剩一个可能的原因：它在等 A 的锁。
    b = HybridRetriever(_write_cases(tmp_path / "b", n=1))

    t_a = threading.Thread(target=a._ensure_encoded, daemon=True)
    t_a.start()
    assert entered_a.wait(5), "A 没走进编码"          # A 此刻持着它自己的锁

    done_b = threading.Event()

    def encode_b():
        b._ensure_encoded()
        done_b.set()

    threading.Thread(target=encode_b, daemon=True).start()
    assert done_b.wait(5), "B 被 A 的锁挡住了——锁的作用域又变回进程级了"
    assert b._embeddings is not None

    gate_a.set()
    t_a.join(5)


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
