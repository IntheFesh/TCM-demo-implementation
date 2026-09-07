"""全局测试夹具。

两件事，都是审查时实测出来的环境依赖：

1. data/graph.json 是 gitignore 的生成物。29 条测试读它，新 clone 上 pytest 直接红
   （HANDOFF 步骤 0 让人先跑 pytest 再建图，顺序反了）。这里缺了就现建——纯计算、
   0 次 LLM 调用、不到一秒，产物跟 offline/build_graph.py + graph_stats.py 一致。

2. USE_REACT / FAST_MODE 这两个环境变量会改变 consult() 的默认行为。开发者 shell 里
   `USE_REACT=1`（HANDOFF 步骤 4 的 A/B 实验正是要切它）时 6 条 chain 测试会失败。
   测试必须跟外面的 shell 无关，进来先清掉。
"""
import pytest


@pytest.fixture(scope="session", autouse=True)
def _ensure_graph_json():
    from core import tools

    if tools.GRAPH_PATH.exists():
        return
    from core.graph.weights import apply_weights
    from offline.build_graph import (
        DEFAULT_FILTER_KEYWORDS,
        build_graph,
        filter_by_keywords,
        load_syndrome_definitions,
    )

    store = build_graph(filter_by_keywords(load_syndrome_definitions(), DEFAULT_FILTER_KEYWORDS))
    apply_weights(store)
    tools.GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    store.save(tools.GRAPH_PATH)


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch):
    for var in ("USE_REACT", "FAST_MODE"):
        monkeypatch.delenv(var, raising=False)
