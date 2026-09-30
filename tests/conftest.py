"""全局测试夹具：让测试结果与运行环境无关。

1. data/graph.json 是 gitignore 的生成物，而有一批测试读它。这里缺了就现建——纯计算、
   0 次 LLM 调用，产物跟 offline/build_graph.py + graph_stats.py 一致，所以新 clone 上
   直接跑 pytest 也不会整批变红。data/ 不可写（只读挂载、受限的 CI 环境）时退到临时
   目录，并把 core.tools.GRAPH_PATH 指过去，不让一个 OSError 把整个测试运行里的
   用例全部标成 ERROR。

2. USE_REACT / FAST_MODE 这类环境变量会改变 consult() 的默认行为（开发者 shell 里
   留着 `USE_REACT=1`，chain 测试就会失败）。测试必须跟外面的 shell 无关，进来先清掉；
   `S3_MODE` 与 `PRODUCT_MODE` 清掉之后还会被钉住，理由见 `_isolate_runtime_env`。

其余夹具（假编码器、向量缓存、运行时数据目录、重试退避）的作用见各自的说明。
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
    # data/ 不可写：建到临时目录，整个测试运行期间把 GRAPH_PATH 指过去
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
    是为了让绝大多数测试**根本不加载 400MB 的模型**：内存受限的环境里，全量测试
    会被 OOM 杀掉（退出码 137，只留一个 `Killed`，看不出是哪条测试）。
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
            # 环境里没装这个库：塞一个桩，好让 `from sentence_transformers import ...`
            # 能过——不这么做的话"没装这个库"会把一批本来跟它无关的测试一起拖红。
            module = types.ModuleType("sentence_transformers")
            monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    monkeypatch.setattr(module, "SentenceTransformer", _DeterministicEncoder, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _disable_embedding_cache():
    """**整个测试运行期间关掉语料向量的磁盘缓存。**

    两个理由：
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
    # 下面这些环境变量都是 consult() 的行为开关，必须跟外面的 shell 隔开：
    # USE_REACT 决定走不走 ReAct，FAST_MODE 是降级开关（不追问、关残差检查等），
    # EVAL_MODE 让安全否决不中止，RETRIEVER_MODE 改检索默认路；S3_BEST_OF_N /
    # S3_REASONING_EFFORT / S3_THINKING 决定 S3 采几次、想多久、开不开思考。
    # 开发者 shell 里留一个 `S3_BEST_OF_N=5` 就会让一堆数调用数的测试红在
    # 跟被测代码无关的地方。
    for var in ("USE_REACT", "FAST_MODE", "EVAL_MODE", "RETRIEVER_MODE",
                "S3_BEST_OF_N", "S3_REASONING_EFFORT", "S3_THINKING",
                # S3_MODE 决定 S3 产出哪种 schema，也就决定 results 有几个元素；
                # 留一个值会让一整批测试红在跟被测代码无关的地方（同 S3_BEST_OF_N）。
                "S3_MODE", "KNOWLEDGE_IN_PROMPT", "FOCUSED_KNOWLEDGE_MAX_TOKENS",
                # S1S2_MERGED 决定 S1/S2 是一次调用还是两次，直接改 llm_calls；
                # FOCUSED_MAX_PATTERNS_PER_PHYSICIAN 决定知识块放几条规律。
                # 同上：留一个值会让一批数调用数/数条数的测试红在跟被测代码无关的地方。
                "S1S2_MERGED", "FOCUSED_MAX_PATTERNS_PER_PHYSICIAN"):
        monkeypatch.delenv(var, raising=False)
    # 清掉之后**再钉成 legacy**。这一句跟上面那一行做的是两件不同的事。
    #
    # **为什么要钉。** 产品默认的 `s3_mode()` 是 `derived`（按医理药理演绎推导，
    # 推导之前不把医案摆进 prompt），而大量测试断言的是 legacy 那个形状：每位医家
    # 各一份 results、两两配对的分歧度、按医家并列的事件序列。它们**要测的就是
    # legacy 那一支**，所以这里钉住模式、让它们继续测自己本来要测的东西——跟各测试
    # 文件里钉住两位医家的 `_pin_two_physicians` 是同一个做法（钉住 X，让 X 的
    # 演进与这批测试解耦）。
    #
    # **为什么这不会把产品默认藏起来。** 被测的配置必须包括产品实际跑的那一档，所以：
    #   1. `tests/test_s3_mode.py::test_the_product_default_is_derived` 显式 delenv
    #      之后调 `s3_mode()`，断言**产品默认是 derived**，这个钉子改不掉那条；
    #   2. 测 derived / structured 路径的用例（`tests/test_s3_mode.py`、
    #      `tests/test_derived_no_cases.py`、`tests/test_corroboration.py` 等）
    #      都显式设置 `S3_MODE`，走的是真实的那条路径；
    #   3. 断言"发给 LLM 的 system 出自 s3_structured.yaml"的测试
    #      （`tests/test_s3_mode.py`）也显式设置了模式。
    # 钉子只影响"没有明说自己要哪一种"的那批测试，而它们的答案本来就是 legacy。
    monkeypatch.setenv("S3_MODE", "legacy")
    # 同一个钉法，同一条理由。`PRODUCT_MODE` 默认是 1（产品界面是默认形态），而大量
    # 测试断言的是研究面的形状：默认角色是 researcher、`/api/usage` 可达、响应里带
    # manifest、三列并列与分歧读数都在。它们**要测的就是研究面那一支**，所以钉成 0。
    #
    # **为什么这不会把产品默认藏起来**：
    #   1. `tests/test_product_mode.py::test_the_default_is_product_mode` 显式 delenv
    #      之后调 `is_product_mode()`，断言**产品默认是 True**，这个钉子改不掉它；
    #   2. 那个文件里的其余用例显式选定模式，测产品路径的都显式 `PRODUCT_MODE=1`；
    #   3. `tests/test_ui_banned_terms.py` 扫的是源码文本，跟环境变量无关；
    #   4. Playwright 的产品模式截图（`python -m scripts.screenshot_states --product`）
    #      起的服务器是 `PRODUCT_MODE=1`，各分辨率、各角色、各状态都在产品形态下跑。
    monkeypatch.setenv("PRODUCT_MODE", "0")


@pytest.fixture(autouse=True)
def _isolate_runtime_data(tmp_path, monkeypatch):
    """运行时数据（问诊历史、收藏、病历文书、偏好、审计日志）写进每条用例自己的临时
    目录，不写进仓库的 data/。要读写这些文件的用例照常传 `path=`，或在用例里自己
    monkeypatch（用例体里的 monkeypatch 排在这条之后，后写的赢）。"""
    from core import audit, history, preferences

    runtime = tmp_path / "runtime-data"
    monkeypatch.setattr(history, "HISTORY_PATH", runtime / "consult_history.jsonl")
    monkeypatch.setattr(history, "FAVORITES_PATH", runtime / "favorites.jsonl")
    monkeypatch.setattr(history, "EMR_PATH", runtime / "emr_drafts.jsonl")
    monkeypatch.setattr(preferences, "PREFERENCES_PATH", runtime / "preferences.jsonl")
    monkeypatch.setattr(audit, "AUDIT_PATH", runtime / "audit.jsonl")


@pytest.fixture(autouse=True)
def _no_llm_retry_backoff(monkeypatch):
    """generate() 在传输错误重试之间会退避 1s、2s（core/llm.py）。测试里的假后端
    故意抛超时来测重试语义，真等的话一条测试就多 3 秒、整个 tests/ 不再"秒级"。
    这里全局清零；退避本身有专门的测试（test_llm_backend.py）在子类上显式设回
    非零值验证。"""
    from core.llm import LLMBackend

    monkeypatch.setattr(LLMBackend, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
