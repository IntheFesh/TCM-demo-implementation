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


class _DeterministicEncoder:
    """8 维、按字符码点算出来的假句向量。确定性、零依赖、不联网、不占内存。

    **不是为了测检索质量**（那需要真模型，标 `@pytest.mark.real_embedding`），
    是为了让绝大多数测试**根本不加载 400MB 的模型**：无卡模式 2GB 上实测全量测试
    有 34% 被 OOM 杀掉（退出码 137，只留一个 `Killed`，看不出是哪条测试）。
    真正需要真模型的用例自己标记，其余一律走这个。
    """

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def encode(self, texts, **_kwargs):
        import numpy as np

        if isinstance(texts, str):
            texts = [texts]
        rows = []
        for text in texts:
            vec = np.array([sum(ord(c) for c in text[i::8]) % 97 + 1 for i in range(8)],
                           dtype="float32")
            rows.append(vec / np.linalg.norm(vec))
        return np.array(rows, dtype="float32")


@pytest.fixture(autouse=True)
def _fake_embedding_model(request, monkeypatch):
    """默认把 `sentence_transformers.SentenceTransformer` 换成假编码器。

    标了 `@pytest.mark.real_embedding` 的用例跳过这层替换，用真模型。判据写在
    marker 上而不是"文件名里有 embedding 就用真的"：哪些用例真的需要真模型是用例
    自己知道的事，从外面猜必然猜错。

    自己装 fake sentence_transformers 的用例（tests/test_concurrency_init.py）不受
    影响：它们的 monkeypatch 在用例体里执行，排在这条 autouse 之后，后写的赢。
    """
    if request.node.get_closest_marker("real_embedding"):
        return
    import sys
    import types

    module = sys.modules.get("sentence_transformers")
    if module is None:
        try:
            import sentence_transformers as module  # noqa: PLC0415
        except ImportError:
            # 这台机器压根没装：塞一个桩，好让 `from sentence_transformers import ...`
            # 能过——不这么做的话"没装这个库"会把一批本来跟它无关的测试一起拖红。
            module = types.ModuleType("sentence_transformers")
            monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(module, "SentenceTransformer", _DeterministicEncoder, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _disable_embedding_cache():
    """**整个测试会话关掉语料向量的磁盘缓存。**

    两个理由，第二个是加缓存当天就踩到的：
    ① 测试不该往仓库的 `data/cache/` 里写东西；
    ② 上一条测试写的缓存会让下一条测试**跳过编码**，于是
       `tests/test_concurrency_init.py` 那几条专门测"编码进行中并发读取"的用例
       永远等不到 encode 被调用，直接超时红掉。

    要测缓存本身的用例（tests/test_embedding_cache.py）自己用 monkeypatch 把
    `EMBEDDING_CACHE_DIR` 指到 tmp_path 再打开，不依赖这里的默认值。
    """
    import os

    before = os.environ.get("EMBEDDING_CACHE")
    os.environ["EMBEDDING_CACHE"] = "0"
    yield
    if before is None:
        os.environ.pop("EMBEDDING_CACHE", None)
    else:
        os.environ["EMBEDDING_CACHE"] = before


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch):
    # EVAL_MODE 会让安全否决不中止、RETRIEVER_MODE 会改检索默认路——都是
    # consult() 的行为开关，跟 USE_REACT/FAST_MODE 一样要跟外面的 shell 隔开。
    # R22 加了后三个：它们决定 S3 采几次、想多久、开不开思考，
    # 开发者 shell 里留一个 `S3_BEST_OF_N=5` 会让一堆数调用数的测试变红，
    # 而红的地方跟改动毫无关系（这条夹具当初就是为 USE_REACT=1 这种情况加的）。
    for var in ("USE_REACT", "FAST_MODE", "EVAL_MODE", "RETRIEVER_MODE",
                "S3_BEST_OF_N", "S3_REASONING_EFFORT", "S3_THINKING",
                # R33：S3_MODE 决定 S3 产出哪种 schema，也就决定 results 有
                # 几个元素。开发者 shell 里留一个值会让一整批测试红在跟改动
                # 无关的地方（跟 S3_BEST_OF_N 那条完全同理）。
                "S3_MODE", "KNOWLEDGE_IN_PROMPT", "FOCUSED_KNOWLEDGE_MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    # 清掉之后**再钉成 legacy**。这一句跟上面那一行做的是两件不同的事。
    #
    # **为什么要钉。** R33 把 `s3_mode()` 的默认改成了 `structured`（五家融合成
    # 一份结论，`results` 恰好一个元素）。此前写下的约 115 条测试断言的是 legacy
    # 那个形状：三位医家、三个 results、两两配对的分歧度、三列事件序列。
    # 它们测的机制在两种模式下都存在，**要测的就是 legacy 那一支**，
    # 所以这里钉住模式、让它们继续测自己本来要测的东西——跟
    # `tests/test_chain.py::_pin_two_physicians` 钉住两位医家是同一个做法
    # （"钉住 X，让 X 的演进与这批测试解耦"）。
    #
    # **为什么这不会把新默认藏起来。** R32 的教训正是"演示跑的那个配置从来没被
    # 测过"。所以：
    #   1. `tests/test_s3_mode.py` 有一条专门断言**产品默认是 structured**
    #      （它显式 delenv 之后调 `s3_mode()`），这个钉子改不掉那条；
    #   2. R33 三个新测试文件全部显式 `S3_MODE=structured`，走的是真的结构化路径；
    #   3. 断言"发给 LLM 的 system 出自 s3_structured.yaml"的测试也在那里面。
    # 钉子只影响"没有明说自己要哪一种"的那批测试，而它们的答案本来就是 legacy。
    monkeypatch.setenv("S3_MODE", "legacy")


@pytest.fixture(autouse=True)
def _no_llm_retry_backoff(monkeypatch):
    """generate() 在传输错误重试之间会退避 1s、2s（core/llm.py）。测试里的假后端
    故意抛超时来测重试语义，真等的话一条测试就多 3 秒、整个 tests/ 不再"秒级"。
    这里全局清零；退避本身有专门的测试（test_llm_backend.py）在子类上显式设回
    非零值验证。"""
    from core.llm import LLMBackend

    monkeypatch.setattr(LLMBackend, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
