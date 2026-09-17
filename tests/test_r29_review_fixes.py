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

def _reference_candidates(store, current_elements, physician=None, **posterior_kw):
    """**教科书写法**：先建两条 177 元素的分布字典，再各求一次熵、各求一次 argmax。

    这就是 R30 优化之前 `question_candidates` 内层循环的形状。放在测试里重写一遍
    是为了让"优化没改数"这件事有判据——不是靠 git 历史里那一版比对
    （那个比对跑过一次，但它不会在下一次改动时自动重跑）。

    返回值里 `nums_yes` / `nums_no` 是**未除以 p_yes / p_no 的分子**，
    下面那条判据要用它区分"真的选错了"和"两个候选差 1 个 ulp"。
    """
    index = tools._syndrome_index(store)
    symptom_weights = tools._symptom_index(store, physician)
    # `posterior_kw` 转发 asserted_symptoms / denied_symptoms / disease_hint
    # ——不转发的话参考实现算的是**没有追问答案的先验**，跟 `question_candidates`
    # 比出来会是"1056 条全不一样"，那不是分叉结论的问题，是比错了对象。
    posterior = tools.syndrome_posterior(
        current_elements, store, physician=physician,
        index=index, symptom_weights=symptom_weights, **posterior_kw,
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
        out[name] = {
            "ig": round(ig, 4),
            "raw_ig": ig,
            "p_yes": round(p_yes, 3),
            "if_yes_top": index[max(post_yes, key=post_yes.get)]["name"],
            "if_no_top": index[max(post_no, key=post_no.get)]["name"],
            "nums_yes": sorted((posterior[c] * p_yes_given[c] for c in posterior), reverse=True),
            "nums_no": sorted((posterior[c] * (1 - p_yes_given[c]) for c in posterior), reverse=True),
        }
    return out


@pytest.mark.parametrize("elements", [["肝", "胃"], ["脾", "湿"], ["心"], []])
def test_the_prefix_sum_ig_matches_the_textbook_formula(elements):
    """**这一条是 R30 融合循环 + R31 前缀和两次优化的共同验收。**

    R31 把内层从 O(证候数) 降到 O(这个症状实际指向的证候数)：未列出的证候一律取
    同一个常数 q，那一整块用两个前缀和（Σp·log2 p、Σp）两次乘加算完。
    **数学上等价，浮点上不逐位相同**（R30 那一版是逐位相同的，R31 起不是了）。
    所以判据从"逐位相同"改成三条：

      - `round(ig, 4)`、`p_yes`、**候选集合与整个排名**逐条相同；
      - 未取整的 ig 相对差 < 1e-10（实测最大 2.2e-11）；
      - 分叉结论（`if_yes_top` / `if_no_top`）相同，**除了并列到 1 ulp 的那几条**
        ——见下面那条单独的判据。

    门槛 `MIN_INFORMATION_GAIN` 是 1e-6，而扰动是 1e-14 量级，比门槛小 8 个数量级；
    候选集合逐条相同这一条就是在盯它。
    """
    store = get_graph_store()
    assert store is not None
    expected = _reference_candidates(store, elements)
    got = {c["symptom"]: c for c in tools.question_candidates(elements, k=10_000)}

    assert set(got) == set(expected), \
        f"候选集合变了：多 {sorted(set(got) - set(expected))[:5]}，少 {sorted(set(expected) - set(got))[:5]}"
    for name, exp in expected.items():
        assert got[name]["information_gain"] == exp["ig"], name
        assert got[name]["p_yes"] == exp["p_yes"], name
        # 对外只暴露取整到 4 位的 ig，所以这里只能比"取整值是不是参考裸值的
        # 忠实舍入"——**绝对界 5e-5（半个舍入步长）**，不能用相对差：
        # ig 小到 1e-4 量级时，舍入本身就是 50% 的相对差，跟算法差异无关。
        assert abs(got[name]["information_gain"] - exp["raw_ig"]) <= 5e-5, name

    # 排名（IG 降序、同分按症状名）逐条相同
    rank_exp = sorted(expected, key=lambda n: (-expected[n]["ig"], n))
    rank_got = sorted(got, key=lambda n: (-got[n]["information_gain"], n))
    assert rank_exp == rank_got, "排名变了"


@pytest.mark.parametrize("elements,kw", [
    (["肝", "胃"], {}),
    (["肝"], {"asserted_symptoms": ["口苦"], "denied_symptoms": ["口渴"]}),
])
def test_a_differing_branch_conclusion_only_happens_on_a_one_ulp_tie(elements, kw):
    """**分叉结论只在"两个候选差 1 个 ulp"时才会跟教科书写法不同。**

    实测那一组（证素「肝」+ 肯定「口苦」+ 否认「口渴」）：1056 个候选里有 3 条不同。
    逐条打出来之后是这个形状——「烦躁易怒」的前三名分子是

        肝胃郁热证 0.0099638125613346565507
        气郁发热证 0.0099638125613346565507
        肝阳上亢证 0.0099638125613346548159   ← 小 1 个 ulp

    除以 p_yes 之后三个**舍入到同一个 float**，教科书写法"按下标顺序扫、
    严格大于才换"于是选了下标最小的肝阳上亢证；R31 比的是**分子**
    （不含 p_yes，所以不受 p_yes 怎么加出来的影响），选了分子确实最大的那个。

    **两个答案都站得住，新的更贴近数学意图**；而真正该被记住的是：
    并列到 1 ulp 时"这个问题偏向哪个证候"本身不是一个有意义的区分
    ——那 3 条的正确读法是"好几个候选分不开"，而这个字段表达不了。
    记在 R31 报告第五节。
    """
    store = get_graph_store()
    posterior_kw = {k: v for k, v in kw.items()
                    if k in ("asserted_symptoms", "denied_symptoms", "disease_hint")}
    expected = _reference_candidates(store, elements, physician=kw.get("physician"),
                                     **posterior_kw)
    got = {c["symptom"]: c for c in tools.question_candidates(elements, k=10_000, **kw)}
    # 只比两边都有的：传了 asserted/denied 时 `question_candidates` 会把已经问过的
    # 症状从候选池里去掉（「口苦」把「口干或口苦」也匹配掉了），而参考实现不做这一步。
    shared = set(expected) & set(got)
    assert shared, "两边没有共同候选，这条判据测不到东西"
    differing = [n for n in shared
                 if (got[n]["if_yes_top"], got[n]["if_no_top"])
                 != (expected[n]["if_yes_top"], expected[n]["if_no_top"])]
    assert len(differing) <= 3, f"分叉结论不同的候选有 {len(differing)} 条：{differing[:6]}"
    for n in differing:
        # **只查真的变了的那一侧**：「烦躁易怒」只有 if_yes_top 变了，
        # 它的 nums_no 前两名差 0.89（完全不并列），一起查会假红。
        for key, field in (("nums_yes", "if_yes_top"), ("nums_no", "if_no_top")):
            if got[n][field] == expected[n][field]:
                continue
            top = expected[n][key][:2]
            assert len(top) == 2, n
            gap = (top[0] - top[1]) / top[0] if top[0] else 0.0
            assert gap < 1e-15, (
                f"{n} 的 {key} 前两名差 {gap:.2e}，不是 1 ulp 量级"
                "——这说明分叉结论真的选错了，不是并列"
            )


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
