"""R29 收尾复查：三处实测出来的问题各配一条判据。

这一轮的"全仓库检查"不是读一遍代码点头，是**先量再改**。量出来三件事：

  1. `/api/graph` 一页 200 个节点要 29.6ms，其中一半花在同一个循环不变量上：
     `ambiguous_syndrome_keys(store)`（要扫全图 1312 个节点）写在推导式里，
     于是**每个节点扫一遍全图**——200 × 1312 = 26 万次，而它的结果跟节点无关。
  2. `question_candidates` 119ms，两张索引各建两遍（自己建一遍 + 传给
     `syndrome_posterior` 时它又建一遍），内层循环为 1115 个候选各分配
     4 条 177 元素的列表。
  3. `core/react.GRAPH_MISS_HINT` 把"国标的 1282 个症状节点"写死在**喂给模型的
     文本**里。那个数每重建一次图谱就变（93 → 1282 → 1117 → 1115），
     写死的数字在某一轮之后就是一句假话，而且没有任何东西会报错。

优化必须**不改任何输出**：第 2 条那个内层循环的浮点运算次序是逐位保留的，
下面 `test_the_fused_ig_loop_matches_the_textbook_formula_bit_for_bit`
把教科书公式在这里重写一遍，逐位比。
"""
from __future__ import annotations

import math
import re

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core import react, tools
from core.tools import get_graph_store


def _client():
    return TestClient(api_main.app)


# ---------- 1. 循环不变量提到循环外 ----------

@pytest.mark.parametrize("path", [
    "/api/graph?limit=200",
    "/api/graph?node_types=syndrome&limit=200",
    "/api/graph/search?q=证&limit=100",
    "/api/graph/neighbors?node=element::肝&limit=50",
])
def test_ambiguous_syndrome_keys_is_computed_once_per_request(monkeypatch, path):
    """**一次请求只许算一次。** 它要扫一遍全图，写在推导式里就是每个节点扫一遍。

    这条判据盯的是"调用次数"而不是"响应时间"——时间会被机器负载淹掉
    （见 eval/RESULTS.md P2/P3 那两行十二次量下来方向来回翻），次数不会。
    """
    calls = []
    real = api_main.ambiguous_syndrome_keys

    def counting(store):
        calls.append(1)
        return real(store)

    monkeypatch.setattr(api_main, "ambiguous_syndrome_keys", counting)
    resp = _client().get(path)
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1, f"{path} 算了 {len(calls)} 次"


def test_the_page_still_carries_the_disease_qualified_labels():
    """提循环不变量不许改输出：带病名限定的 label 还在。"""
    body = _client().get("/api/graph?node_types=syndrome&limit=200").json()
    labels = [n["data"]["label"] for n in body["graph"]["nodes"]]
    assert sum(1 for lb in labels if "\n（" in lb) >= 60


# ---------- 2. 两张索引各建一次 ----------

def test_question_candidates_builds_each_index_exactly_once(monkeypatch):
    """`_symptom_index` 要扫 1115 个症状节点的全部 indicates 边（实测 6.6ms）。
    原来一次追问里它跑两遍：`question_candidates` 自己建一遍，
    `syndrome_posterior` 内部又建一遍。传参而不是加缓存——
    两张索引的内容完全由 (store, physician) 决定，调用方手里就有。
    """
    counts = {"symptom": 0, "syndrome": 0}
    real_sym, real_syn = tools._symptom_index, tools._syndrome_index

    def c_sym(store, physician):
        counts["symptom"] += 1
        return real_sym(store, physician)

    def c_syn(store):
        counts["syndrome"] += 1
        return real_syn(store)

    monkeypatch.setattr(tools, "_symptom_index", c_sym)
    monkeypatch.setattr(tools, "_syndrome_index", c_syn)
    out = tools.question_candidates(["肝", "胃"], k=3)
    assert out, "没有候选的话这条判据测不到东西"
    assert counts == {"symptom": 1, "syndrome": 1}, counts


def test_syndrome_posterior_still_builds_its_own_indexes_when_not_given():
    """传参是**可选**的。单独调 syndrome_posterior 的地方（core/followup.py、
    eval/）不传，它必须自己建——否则这个函数就只能在 question_candidates 里用。"""
    store = get_graph_store()
    assert store is not None
    a = tools.syndrome_posterior(["肝", "胃"], store)
    b = tools.syndrome_posterior(
        ["肝", "胃"], store,
        index=tools._syndrome_index(store),
        symptom_weights=tools._symptom_index(store, None),
    )
    assert a == b, "传索引和不传索引算出来的后验必须逐位相同"


# ---------- 3. 融合后的内层循环跟教科书公式逐位相同 ----------

def _reference_candidates(store, current_elements, physician=None):
    """**教科书写法**：先建两条分布的字典，再各求一次熵、各求一次 argmax。

    这就是 R29 优化之前 `question_candidates` 内层循环的形状。放在测试里重写一遍
    是为了让"优化没改数"这件事有判据——不是靠 git 历史里那一版比对
    （那个比对跑过一次，但它不会在下一次改动时自动重跑）。
    """
    index = tools._syndrome_index(store)
    symptom_weights = tools._symptom_index(store, physician)
    posterior = tools.syndrome_posterior(
        current_elements, store, physician=physician,
        index=index, symptom_weights=symptom_weights,
    )
    prior_entropy = tools._entropy(posterior.values())
    out = {}
    for name, weights in symptom_weights.items():
        p_yes_given = {
            code: min(max(weights.get(code, tools.P_UNLISTED), tools.P_UNLISTED), tools.P_MAX)
            for code in posterior
        }
        p_yes = sum(posterior[c] * p_yes_given[c] for c in posterior)
        p_no = 1.0 - p_yes
        if p_yes <= 0 or p_no <= 0:
            continue
        post_yes = {c: posterior[c] * p_yes_given[c] / p_yes for c in posterior}
        post_no = {c: posterior[c] * (1 - p_yes_given[c]) / p_no for c in posterior}
        ig = (prior_entropy
              - p_yes * tools._entropy(post_yes.values())
              - p_no * tools._entropy(post_no.values()))
        if ig <= tools.MIN_INFORMATION_GAIN:
            continue
        out[name] = (
            round(ig, 4), round(p_yes, 3),
            index[max(post_yes, key=post_yes.get)]["name"],
            index[max(post_no, key=post_no.get)]["name"],
        )
    return out


@pytest.mark.parametrize("elements", [["肝", "胃"], ["脾", "湿"], ["心"], []])
def test_the_fused_ig_loop_matches_the_textbook_formula_bit_for_bit(elements):
    """**这一条是那次优化的验收。**

    融合后的循环把两条后验分布、两个熵、两个 argmax 在同一遍里算完，省掉每个
    候选 4 次 177 元素的列表分配。每个元素的浮点运算和求和次序跟教科书写法一样
    （`-sum(v·log2 v)` 展开成逐项 `-= v·log2(v)`，IEEE 下取负是精确的），
    所以 `ig` 不会在 MIN_INFORMATION_GAIN 门槛上跳变——**逐位相同，不是约等于**。
    用 `==` 比浮点在这里是对的：判据正是"一位都不许差"。
    """
    store = get_graph_store()
    assert store is not None
    expected = _reference_candidates(store, elements)
    got = {
        c["symptom"]: (c["information_gain"], c["p_yes"], c["if_yes_top"], c["if_no_top"])
        for c in tools.question_candidates(elements, k=10_000)
    }
    # 安全症状可能被插队到队首，但它本来就在 scored 里，键集不受影响
    assert set(got) == set(expected), \
        f"候选集合变了：多 {sorted(set(got) - set(expected))[:5]}，少 {sorted(set(expected) - set(got))[:5]}"
    diff = {k: (expected[k], got[k]) for k in expected if expected[k] != got[k]}
    assert not diff, f"{len(diff)} 条的 IG / p_yes / 分叉结论变了：{list(diff.items())[:3]}"


def test_the_inner_loop_has_no_per_candidate_list_allocation():
    """回归判据：别人把融合的循环改回"先建两条列表再求熵"时这条要红。
    盯的是源码形状——这件事没法从输出上看出来（输出本来就一样）。"""
    src = open("core/tools.py", encoding="utf-8").read()
    body = src[src.index("    codes = list(posterior)"):src.index("        if ig <= MIN_INFORMATION_GAIN")]
    assert "ent_yes = ent_no = 0.0" in body
    assert "post_yes = [" not in body and "post_yes = {" not in body
    assert "log2 = math.log2" in src, "内层循环里 math.log2 要局部绑定"


def test_entropy_of_a_negated_sum_is_exact():
    """融合那一步依赖的等式：`-(a+b+c) == (-a)+(-b)+(-c)`。
    IEEE 754 下取负是精确运算，所以这是恒等式，不是近似。
    拿真实量级的概率值验一遍——万一哪天有人把 `_entropy` 换成别的实现，
    这条会提醒他融合的循环也得跟着改。"""
    vals = [0.1, 0.03, 0.007, 0.9, 1e-9, 0.25]
    forward = -sum(v * math.log2(v) for v in vals)
    fused = 0.0
    for v in vals:
        fused -= v * math.log2(v)
    assert forward == fused


# ---------- 4. 提示里的数字现数 ----------

def test_the_graph_miss_hint_constant_no_longer_hardcodes_a_count():
    """**这句提示是喂给模型的文本。** 写死的 1282 在 R29 之后是 1115，
    中间还经过 1117——一个每轮都会变、又出现在 prompt 里的数字不许写死。"""
    assert not re.search(r"\d", react.GRAPH_MISS_HINT), react.GRAPH_MISS_HINT


def test_graph_miss_hint_reports_the_measured_count():
    store = get_graph_store()
    assert store is not None
    n = len(store.find_nodes("symptom"))
    text = react.graph_miss_hint()
    assert react.GRAPH_MISS_HINT in text, "不带数字那句要原样在里面（测试按它断言）"
    assert f"共 {n} 个症状节点" in text


def test_graph_miss_hint_invents_nothing_when_the_graph_is_missing(monkeypatch):
    """图谱取不到时只给不带数字的那句，**不编一个数**。

    `graph_miss_hint` 刻意不收 store 参数：那样 `store=None` 要同时表达
    "没传"和"图谱不可用"两件事。造这个场景就打 `core.tools.get_graph_store`。
    """
    monkeypatch.setattr("core.tools.get_graph_store", lambda: None)
    assert react.graph_miss_hint() == react.GRAPH_MISS_HINT


# ---------- 5. -O 下不许 raise None ----------

def test_best_of_n_does_not_use_assert_to_narrow_the_exception():
    """`python -O` 会把 assert 整行删掉。原来的写法是
    `assert last_exc is not None` + `raise last_exc`——-O 下变成 `raise None`,
    抛「exceptions must derive from BaseException」，把真正的根因盖掉。"""
    src = open("core/chain.py", encoding="utf-8").read()
    body = src[src.index("def _best_of_n_s3("):]
    body = body[:body.index("\ndef ")]
    assert "assert last_exc" not in body
    assert "if last_exc is None:" in body


def test_best_of_n_raises_the_real_exception_when_every_sample_fails(monkeypatch):
    """N 次全失败时抛的是**最后那个真实异常**，不是"没有候选方"的假结果。"""
    from core.llm import LLMError

    class Boom:
        def generate(self, **kwargs):
            raise LLMError("429 限流（第 N 次）")

    monkeypatch.setattr("core.chain.get_llm", lambda: Boom())
    monkeypatch.setattr("core.chain.s3_best_of_n", lambda: 3)
    monkeypatch.setattr("core.chain.thinking_for", lambda _stage: {})
    with pytest.raises(LLMError, match="429 限流"):
        from core.chain import _best_of_n_s3
        _best_of_n_s3("system", None, "ye_tianshi")
