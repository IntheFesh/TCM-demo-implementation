"""全局测试夹具。

两件事，都是审查时实测出来的环境依赖：

1. data/graph.json 是 gitignore 的生成物。29 条测试读它，新 clone 上 pytest 直接红
   （HANDOFF 步骤 0 让人先跑 pytest 再建图，顺序反了）。这里缺了就现建——纯计算、
   0 次 LLM 调用、不到一秒，产物跟 offline/build_graph.py + graph_stats.py 一致。
   data/ 不可写（只读挂载、CI 沙箱）时退到临时目录，并把 core.tools.GRAPH_PATH 指过去，
   不让一个 OSError 把整个 session 的测试全部标成 ERROR。

2. USE_REACT / FAST_MODE 这两个环境变量会改变 consult() 的默认行为。开发者 shell 里
   `USE_REACT=1`（HANDOFF 步骤 4 的 A/B 实验正是要切它）时 6 条 chain 测试会失败。
   测试必须跟外面的 shell 无关，进来先清掉。
"""
import pytest


def _build_graph_store():
    from core.graph.weights import apply_weights
    from offline.build_graph import (
        DEFAULT_FILTER_KEYWORDS,
        build_graph,
        filter_by_keywords,
        load_syndrome_definitions,
    )

    store = build_graph(filter_by_keywords(load_syndrome_definitions(), DEFAULT_FILTER_KEYWORDS))
    apply_weights(store)
    return store


@pytest.fixture(scope="session", autouse=True)
def _ensure_graph_json(tmp_path_factory):
    from core import tools

    if tools.GRAPH_PATH.exists():
        yield
        return
    store = _build_graph_store()
    try:
        tools.GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
        store.save(tools.GRAPH_PATH)
        yield
        return
    except OSError:
        pass
    # data/ 不可写：建到临时目录，整个 session 把 GRAPH_PATH 指过去
    fallback = tmp_path_factory.mktemp("graph") / "graph.json"
    store.save(fallback)
    mp = pytest.MonkeyPatch()
    mp.setattr(tools, "GRAPH_PATH", fallback)
    tools.reset_tool_caches()
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch):
    # EVAL_MODE 会让安全否决不中止、RETRIEVER_MODE 会改检索默认路——都是
    # consult() 的行为开关，跟 USE_REACT/FAST_MODE 一样要跟外面的 shell 隔开。
    for var in ("USE_REACT", "FAST_MODE", "EVAL_MODE", "RETRIEVER_MODE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_llm_retry_backoff(monkeypatch):
    """generate() 在传输错误重试之间会退避 1s、2s（core/llm.py）。测试里的假后端
    故意抛超时来测重试语义，真等的话一条测试就多 3 秒、整个 tests/ 不再"秒级"。
    这里全局清零；退避本身有专门的测试（test_llm_backend.py）在子类上显式设回
    非零值验证。"""
    from core.llm import LLMBackend

    monkeypatch.setattr(LLMBackend, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
