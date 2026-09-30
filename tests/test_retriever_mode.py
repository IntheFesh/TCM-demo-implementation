"""逐请求检索模式切换的离线测试。

这个模块的硬约束是"不设全局环境变量"：RETRIEVER_MODE 是进程级的，一个请求
设了它，同一进程里并发的另一个请求就跟着变了。所以这里最重要的一条不是
"模式传下去了没有"，而是 test_concurrent_consults_do_not_leak_modes——两个
线程各选一种模式同时跑，检索层收到的模式必须各是各的。

第二条硬约束是"graph 模式不静默降级"：graph 模式故意设计成拿不到证素/索引就
报错，悄悄退回 hybrid 的话调用方以为自己拿到的是证素路的结果，E8 消融那组数字
就失去意义。consult 只负责把这个错误翻译成人话（retrieval_error），
不负责把它变成"换个模式跑完当作成功"。
"""
import contextvars
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
    """不传 retriever_mode 时 search() 收到的关键字不带任何 mode 相关参数——
    一个 mode 都不带。这不是洁癖：抽象基类 Retriever.search 的签名里没有
    mode/query_elements，无条件传的话所有第三方实现（测试里的 FakeRetriever、
    别的检索后端）都得跟着改签名。

    每位医家只有一次 search() 调用：adaptive_min_score 的探测只对显式
    mode="dense" 才有意义，不传 mode 时直接跳过探测（core/retrieval.py 的
    文档字符串）。"""
    r = _setup(monkeypatch)
    chain.consult("纳差乏力")
    assert len(r.calls) == 2  # 两位医家各一次，没有探测调用
    for call in r.calls:
        assert set(call) == {"physician"}, f"多传了关键字：{call}"


def test_explicit_mode_is_passed_through(monkeypatch):
    """bm25 模式的排名函数不接受 min_score，adaptive_min_score 对它直接跳过探测
    （跟缺省/hybrid 同理），每位医家只有一次真正的 search() 调用。"""
    r = _setup(monkeypatch)
    chain.consult("纳差乏力", retriever_mode="bm25")
    assert [c["mode"] for c in r.calls] == ["bm25"] * 2
    for call in r.calls:
        assert "query_elements" not in call, "只有 graph 模式才需要证素"


def test_graph_mode_passes_query_elements_from_s2(monkeypatch):
    """graph 模式必须带上 S2 推断出的证素——这是它唯一的输入信号。
    FakeLLM 的 S2 固定返回证素「脾」，这里断言它确实被传下去了。

    graph 模式的排名函数不接受 min_score，adaptive_min_score 对它跳过探测，
    每位医家只有一次真正的查询调用。"""
    r = _setup(monkeypatch)
    chain.consult("纳差乏力", retriever_mode="graph")
    assert [c["mode"] for c in r.calls] == ["graph"] * 2
    for call in r.calls:
        assert call["query_elements"] == ["脾"]


def test_hybrid_explicit_does_not_silently_become_three_way(monkeypatch):
    """显式选 hybrid 也不传 query_elements。

    三路融合（dense+bm25+graph）只在传了 query_elements 时才启用。consult 不替
    hybrid 主动打开它——那样会在没人要求的情况下改掉 hybrid 的检索行为，也就改掉了
    E8 消融的对照基线。要启用三路融合，应当作为单独的改动、带对照数字地做。"""
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

    把一次 search 归到哪个 consult，用的是一个 ContextVar 标记 consult 的身份，
    而不是线程名：三位医家并发跑在 `physician_*` 工作线程里，检索发生在这些线程上，
    按调用方的线程名归属会找不到对应的 consult（docs/DESIGN_NOTES.md §10）。
    **这同时把 ContextVar 有没有传进 worker 也一起钉住了**：`_run_physicians_into`
    若忘了 `copy_context().run`，worker 里读到的就是默认值 `None`，这条会红。
    （`use_llm()` 的 BYOK 覆盖走的正是同一个机制，静默失效的后果是访问者的 key
    没被用上、额度照扣。）
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

    consult_tag: contextvars.ContextVar[str | None] = contextvars.ContextVar(
        "consult_tag", default=None)

    class BarrierRetriever(Retriever):
        def search(self, query, physician, k=3, min_score=0.0, **kwargs):
            mode = kwargs.get("mode")
            tag = consult_tag.get()
            assert tag is not None, (
                "ContextVar 没有传进医家工作线程——use_llm() 的逐请求覆盖（BYOK、"
                "降级到回放）走的是同一个机制，这里读不到就等于那些也全都静默失效了")
            with seen_lock:
                seen.setdefault(tag, []).append(mode)
            if physician == "ye_tianshi":
                # 第一位医家的检索处等两边都到齐，制造真正的同时在飞状态
                barrier.wait()
            return [(c, 0.9) for c in cases if c.physician == physician][:k]

    monkeypatch.setattr(chain, "get_retriever", lambda: BarrierRetriever())

    errors: list[BaseException] = []

    def run(mode: str):
        try:
            consult_tag.set(f"T-{mode}")
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
    # 每位医家一次调用（bm25/graph 模式不接受 min_score，adaptive_min_score
    # 跳过探测），两位医家共 2 次；泄漏检测的判据：只要 T-bm25 全程只看到
    # "bm25"、T-graph 全程只看到 "graph" 就没有串味。
    assert seen["T-bm25"] == ["bm25"] * 2, seen
    assert seen["T-graph"] == ["graph"] * 2, seen


# ---------- 硬约束二：graph 不可用要变人话，不是 500、也不是静默降级 ----------


class BrokenGraphRetriever(Retriever):
    """模拟 graph 模式的真实失败形态：ElementRetriever 在缺 data/element_index.json
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
    """retrieval_error 分支的键集必须跟别的分支完全一致——api/前端按同一份契约读，
    缺键就是 KeyError。retrieval_error 这个键所有分支都要带。"""
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
    """默认模式自己就跑不了时（例如没有 cases.json），文案不能还说"换用默认模式
    可以正常辨证"；只有显式选了别的模式才该这么建议。"""
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

    monkeypatch.delenv("RETRIEVER_MODE", raising=False)
    default = chain.consult("纳差乏力")["retrieval_error"]
    assert "换用默认模式" not in default
    # 报出来的是实际生效的模式（默认值），不是某个写死的模式名
    assert f"检索模式「{chain.DEFAULT_MODE}」" in default
    assert "哪个模式都跑不了" in default

    explicit = chain.consult("纳差乏力", retriever_mode="bm25")["retrieval_error"]
    assert "换用默认模式" in explicit
