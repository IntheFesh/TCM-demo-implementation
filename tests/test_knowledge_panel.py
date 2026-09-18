"""R46 §7.5 第 13 条：诊中知识速查（四类 + 响应时间）。"""
import time

from fastapi.testclient import TestClient

import api.main as api_main
from core.knowledge_panel import KIND_LABEL, search

#: §7.5 第 13 条的预算。**算的是本体装载之后的每次查询**——本体是惰性加载的，
#: 第一次查会把它装进来（实测约 2.4 秒）。生产部署由 api/warmup.py 在起服务时
#: 就装好，所以医师遇不到那一次冷启动；这里先预热再计时，跟生产一致。
SEARCH_BUDGET_MS = 200


def _warm() -> None:
    search("柴胡")


def test_the_four_kinds_are_all_there():
    assert set(KIND_LABEL) == {"herb", "formula", "guideline", "pattern"}


def test_each_group_is_returned_separately_not_merged():
    """混成一个列表之后"我在查药"就变成了"系统返回了一堆东西"。"""
    out = search("柴胡")
    kinds = [g["kind"] for g in out["groups"]]
    assert kinds == list(KIND_LABEL)


def test_a_herb_query_finds_the_herb_with_its_source():
    out = search("柴胡", kind="herb")
    items = out["groups"][0]["items"]
    assert items and any(i["title"] == "柴胡" for i in items)
    assert all(i["kind"] == "herb" for i in items)


def test_a_formula_query_finds_the_formula():
    out = search("小柴胡汤", kind="formula")
    items = out["groups"][0]["items"]
    assert items and items[0]["title"] == "小柴胡汤"
    assert "功用" in items[0]["summary"] or "主治" in items[0]["summary"]


def test_a_guideline_query_names_the_basis_not_a_guideline():
    from core.guideline_compare import BASIS_LABEL, load_guidelines

    e = load_guidelines()[0]
    out = search(e.recommended_formula, kind="guideline")
    items = out["groups"][0]["items"]
    assert items and BASIS_LABEL in items[0]["summary"]


def test_a_pattern_query_filters_by_physician_id_not_the_chinese_name():
    """SOURCES.md 第 31 条那个坑：过滤用 id，人和模型填的是中文名。
    这里传中文名，`resolve_physician_id` 应该把它解析成 id。"""
    out = search("叶天士", kind="pattern", physician="叶天士")
    assert out["groups"][0]["kind"] == "pattern"  # 不炸、不返回错的类别


def test_an_empty_query_explains_what_to_type():
    out = search("   ")
    assert out["groups"] == [] and out["note"]


def test_a_query_with_no_hits_says_it_checked_all_four():
    """三分法：查到了 / 查了但没有 / 这一类数据不在。"""
    out = search("这个词不可能在任何一张表里出现")
    assert sum(g["n"] for g in out["groups"]) == 0
    assert "已查" in out["note"] or "都没有匹配" in out["note"]


def test_the_limit_is_respected():
    out = search("汤", kind="formula", limit=3)
    assert out["groups"][0]["n"] <= 3


def test_a_warm_query_is_within_the_budget():
    _warm()
    t0 = time.perf_counter()
    search("黄连")
    ms = (time.perf_counter() - t0) * 1000
    assert ms <= SEARCH_BUDGET_MS, f"速查 {ms:.0f} ms，预算 {SEARCH_BUDGET_MS} ms"


def test_the_endpoint_is_within_the_budget_too():
    client = TestClient(api_main.app)
    client.get("/api/knowledge/search?q=柴胡")  # 预热
    t0 = time.perf_counter()
    r = client.get("/api/knowledge/search?q=白芍")
    ms = (time.perf_counter() - t0) * 1000
    assert r.status_code == 200
    assert ms <= SEARCH_BUDGET_MS, f"端点 {ms:.0f} ms，预算 {SEARCH_BUDGET_MS} ms"


def test_the_endpoint_clamps_an_absurd_limit():
    client = TestClient(api_main.app)
    r = client.get("/api/knowledge/search?q=汤&limit=100000")
    assert r.status_code == 200
    for g in r.json()["groups"]:
        assert g["n"] <= 50


def test_the_panel_does_not_call_the_model():
    """速查要的是快。任何一次模型调用都到不了 200 ms。"""
    import ast
    from pathlib import Path

    src = Path("core/knowledge_panel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "get_llm" not in names and "generate" not in names
