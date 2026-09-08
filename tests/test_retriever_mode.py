"""逐请求检索模式切换（模块8）的离线测试。

这个模块的硬约束是"不设全局环境变量"：RETRIEVER_MODE 是进程级的，一个请求
设了它，同一进程里并发的另一个请求就跟着变了。所以这里最重要的一条不是
"模式传下去了没有"，而是 test_concurrent_consults_do_not_leak_modes——两个
线程各选一种模式同时跑，检索层收到的模式必须各是各的。

第二条硬约束是"graph 模式不静默降级"：K3b 故意设计成拿不到证素/索引就报错，
悄悄退回 hybrid 的话调用方以为自己拿到的是证素路的结果，E8 消融那组数字
就失去意义。consult 只负责把这个错误翻译成人话（retrieval_error），
不负责把它变成"换个模式跑完当作成功"。
"""
import threading

import pytest

from core import chain
from core.retrieval import Retriever
from core.schemas import S3Syndrome
from tests.test_chain import FakeLLM, _fake_cases


class RecordingRetriever(Retriever):
    """记下每次 search() 收到的关键字参数。签名带 **kwargs 是有意的——真实的
    HybridRetriever.search 才认识 mode/query_elements，抽象基类的签名里没有，
    这个假实现要能同时接住"传了 mode"和"一个额外关键字都没传"两种调用。"""

    def __init__(self, cases):
        self.cases = cases
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def search(self, query, physician, k=3, min_score=0.0, **kwargs):
        with self.lock:
            self.calls.append({"physician": physician, **kwargs})
        return [(c, 0.9) for c in self.cases if c.physician == physician][:k]


def _setup(monkeypatch, retriever=None):
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=["ye_tianshi-001"], herbs=["党参"])
    fake_llm = FakeLLM({"叶天士": s3, "吴鞠通": s3})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    r = retriever or RecordingRetriever(_fake_cases())
    monkeypatch.setattr(chain, "get_retriever", lambda: r)
    return r


# ---------- 默认路径：一个额外关键字都不传 ----------


def test_default_path_passes_no_mode_kwargs_at_all(monkeypatch):
    """不传 retriever_mode 时 search() 收到的关键字必须跟改造前一模一样——
    一个 mode 都不带。这不是洁癖：抽象基类 Retriever.search 的签名里没有
    mode/query_elements，无条件传的话所有第三方实现（测试里的 FakeRetriever、
    将来别的检索后端）都得跟着改签名。"""
    r = _setup(monkeypatch)
    chain.consult("纳差乏力")
    assert len(r.calls) == 2  # 两位医家各一次
    for call in r.calls:
        assert set(call) == {"physician"}, f"多传了关键字：{call}"


def test_explicit_mode_is_passed_through(monkeypatch):
    r = _setup(monkeypatch)
    chain.consult("纳差乏力", retriever_mode="bm25")
    assert [c["mode"] for c in r.calls] == ["bm25", "bm25"]
    for call in r.calls:
        assert "query_elements" not in call, "只有 graph 模式才需要证素"


def test_graph_mode_passes_query_elements_from_s2(monkeypatch):
    """graph 模式必须带上 S2 推断出的证素——这是它唯一的输入信号。
    FakeLLM 的 S2 固定返回证素「脾」，这里断言它确实被传下去了。"""
    r = _setup(monkeypatch)
    chain.consult("纳差乏力", retriever_mode="graph")
    assert [c["mode"] for c in r.calls] == ["graph", "graph"]
    for call in r.calls:
        assert call["query_elements"] == ["脾"]


def test_hybrid_explicit_does_not_silently_become_three_way(monkeypatch):
    """显式选 hybrid 跟不选（默认）走的是同一条路：都不传 query_elements。

    K3b 的三路融合（dense+bm25+graph）只在传了 query_elements 时才启用。
    在这一轮顺手打开它 = 在没人要求的情况下改掉默认检索行为，也就改掉了
    E8 消融的对照基线。要开该是单独一轮、带对照数字地开。"""
    r = _setup(monkeypatch)
    chain.consult("纳差乏力", retriever_mode="hybrid")
    for call in r.calls:
        assert "query_elements" not in call


# ---------- 硬约束一：不设全局状态，并发不串味 ----------


def test_consult_never_touches_the_retriever_mode_env_var():
    """源码级断言：core/chain.py 的**代码**里不许出现 "RETRIEVER_MODE" 这个
    字符串字面量（读它意味着进程级状态又回来了，写它更糟——会污染并发的
    别的请求）。

    用 AST 只看代码里的字符串常量，不做整份源码的子串匹配：文档字符串和注释
    里正大光明写着"为什么不用这个环境变量"，粗暴地 `"RETRIEVER_MODE" not in
    src` 会把那段解释本身判成违规——那样的测试逼着人删掉解释才能过，是把
    测试写反了。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(chain))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and "RETRIEVER_MODE" in node.value and node.value not in docstrings
    ]
    assert offenders == [], f"chain.py 的代码里碰了这个进程级环境变量：{offenders}"


def test_env_var_is_untouched_after_consult_with_explicit_mode(monkeypatch):
    """行为级断言：显式传 graph 模式跑完之后，进程里的 RETRIEVER_MODE
    还是原来的样子（没设过就还是没设）。"""
    import os

    monkeypatch.delenv("RETRIEVER_MODE", raising=False)
    _setup(monkeypatch)
    chain.consult("纳差乏力", retriever_mode="graph")
    assert "RETRIEVER_MODE" not in os.environ


def test_concurrent_consults_do_not_leak_modes(monkeypatch):
    """**这个模块的核心测试。** 两个线程同时跑 consult()、各选一种模式，
    检索层收到的模式必须各是各的。

    用 barrier 强制两个线程真的在同一时刻都停在 search() 里——不这么做的话
    两次调用很可能一前一后串行发生，就算实现真的在写全局状态也测不出来。
    """
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})
    s3 = S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                    cited_case_ids=["ye_tianshi-001"], herbs=["党参"])
    monkeypatch.setattr(chain, "get_llm", lambda: FakeLLM({"叶天士": s3, "吴鞠通": s3}))

    barrier = threading.Barrier(2, timeout=10)
    seen: dict[str, list[str]] = {}
    seen_lock = threading.Lock()
    cases = _fake_cases()

    class BarrierRetriever(Retriever):
        def search(self, query, physician, k=3, min_score=0.0, **kwargs):
            mode = kwargs.get("mode")
            with seen_lock:
                seen.setdefault(threading.current_thread().name, []).append(mode)
            if physician == "ye_tianshi":
                # 第一位医家的检索处等两边都到齐，制造真正的同时在飞状态
                barrier.wait()
            return [(c, 0.9) for c in cases if c.physician == physician][:k]

    monkeypatch.setattr(chain, "get_retriever", lambda: BarrierRetriever())

    errors: list[BaseException] = []

    def run(mode: str):
        try:
            chain.consult("纳差乏力", retriever_mode=mode)
        except BaseException as e:  # noqa: BLE001 - 线程里的异常不会自动冒泡，收上来在主线程断言
            errors.append(e)

    t1 = threading.Thread(target=run, args=("bm25",), name="T-bm25")
    t2 = threading.Thread(target=run, args=("graph",), name="T-graph")
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, errors
    assert seen["T-bm25"] == ["bm25", "bm25"], seen
    assert seen["T-graph"] == ["graph", "graph"], seen


# ---------- 硬约束二：graph 不可用要变人话，不是 500、也不是静默降级 ----------


class BrokenGraphRetriever(Retriever):
    """模拟 K3b 的真实失败形态：ElementRetriever 在缺 data/element_index.json
    时抛 FileNotFoundError（不是返回空结果，那才是静默降级）。"""

    def search(self, query, physician, k=3, min_score=0.0, **kwargs):
        if kwargs.get("mode") == "graph":
            raise FileNotFoundError(
                "未找到 data/element_index.json。请先运行 "
                "`python -m offline.build_element_index` 生成证素索引，再使用 graph 检索模式。"
            )
        return []


def test_graph_mode_missing_index_becomes_readable_message_not_exception(monkeypatch):
    _setup(monkeypatch, retriever=BrokenGraphRetriever())
    outcome = chain.consult("纳差乏力", retriever_mode="graph")

    assert outcome["retrieval_error"] is not None
    assert "graph" in outcome["retrieval_error"]
    assert "element_index" in outcome["retrieval_error"]
    # 没有产出任何方药：一半医家用了这个模式、另一半没有的对照本身就是错的
    assert outcome["results"] == []
    assert outcome["divergence"] is None
    # 这不是安全拦截、也不是信息不足，别混进那两个字段
    assert outcome["rejected"] is False
    assert outcome["insufficient"] is False


def test_retrieval_error_branch_keeps_the_same_key_set(monkeypatch):
    """新分支的键集必须跟别的分支完全一致——api/前端按同一份契约读，
    缺键就是 KeyError。retrieval_error 是这一轮有意加的新键，所有分支都要带。"""
    normal_r = _setup(monkeypatch)
    normal = chain.consult("纳差乏力")

    _setup(monkeypatch, retriever=BrokenGraphRetriever())
    broken = chain.consult("纳差乏力", retriever_mode="graph")

    assert set(normal) == set(broken)
    assert "retrieval_error" in normal
    assert normal["retrieval_error"] is None
    assert normal_r.calls, "sanity：正常那次确实走了检索"


def test_unknown_mode_fails_fast_before_any_llm_call(monkeypatch):
    """模式名不认识时立刻抛，不能等到第一位医家检索才失败——那时候 S1/S2
    两次 LLM 调用已经白花了。"""
    r = _setup(monkeypatch)
    fake_llm = chain.get_llm()
    with pytest.raises(ValueError, match="未知的 retriever_mode"):
        chain.consult("纳差乏力", retriever_mode="没有这个模式")
    assert fake_llm.calls == [], "校验必须发生在任何 LLM 调用之前"
    assert r.calls == []


def test_allowed_modes_come_from_the_retrieval_layer():
    """合法模式集合只有 core/retrieval_hybrid.py 那一份，chain 里不许再抄一份
    字符串列表——抄一份的话加新模式时必然漏改一处。"""
    from core.retrieval_hybrid import ALLOWED_MODES

    assert chain.ALLOWED_MODES is ALLOWED_MODES


def test_default_mode_unavailable_does_not_suggest_switching_to_default(monkeypatch, tmp_path):
    """真实冒烟踩到的：沙箱没有 cases.json，默认模式自己就跑不了，文案却还说
    "换用默认模式可以正常辨证"。只有显式选了别的模式才该这么建议。"""
    from core import chain
    from core.retrieval import Retriever

    class Missing(Retriever):
        def search(self, *a, **kw):
            raise FileNotFoundError("未找到 cases.json。")

    from tests.test_chain import _s3
    fake_llm = FakeLLM({"叶天士": _s3("脾胃气虚", ["党参"], "ye_tianshi-001"),
                        "吴鞠通": _s3("脾胃气虚", ["党参"], "wu_jutong-001")})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: Missing())

    default = chain.consult("纳差乏力")["retrieval_error"]
    assert "换用默认模式" not in default
    assert "哪个模式都跑不了" in default

    explicit = chain.consult("纳差乏力", retriever_mode="bm25")["retrieval_error"]
    assert "换用默认模式" in explicit
