"""企业化整改里合并掉的几处"同一个判断两处实现"（CLAUDE.md 那堵撞过三次的墙），
用测试钉住合并后的形状：哪个函数是唯一实现、两个调用方拿到的是不是同一个答案、
键集是不是一致。全部离线。
"""
import json
import re
from pathlib import Path

import pytest

import api.main as api_main
from core import chain, safety, tools
from core.graph.store import NetworkXStore
from core.herbs import normalized_herb_set
from core.schemas import S1Normalize
from tests.test_api import _fake_outcome, _fake_rejected_outcome

ROOT = Path(__file__).resolve().parent.parent
SRC_DIRS = ("core", "api", "offline", "eval")


def _grep_sources(needle: str) -> list[str]:
    hits = []
    for d in SRC_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            if needle in p.read_text(encoding="utf-8"):
                hits.append(str(p.relative_to(ROOT)))
    return sorted(hits)


# ---------- 拒绝文案只拼一次 ----------


def test_veto_message_literal_exists_only_in_safety_py():
    """之前 check_safety、chain.py 的 ReAct 路径、followup.py 的十问歌兜底各拼了
    一份一字不差的字符串。现在源码里这句话只能出现在 core/safety.py。"""
    assert _grep_sources("请立即就医或拨打急救电话") == ["core/safety.py"]


def test_check_safety_and_veto_message_agree():
    reason = safety.check_safety(["解黑色柏油样便"])
    assert reason is not None
    assert reason == safety.veto_message(reason.split("（")[1].split("）")[0])


# ---------- "问的是危重症状、患者没否认" 只判一次 ----------


@pytest.mark.parametrize("verdict", ["yes", "unknown"])
def test_danger_question_not_denied_is_vetoed(verdict):
    reason = safety.danger_confirmed_by_answer("有没有便血？", verdict)
    assert reason is not None and "便血" in reason


def test_danger_question_explicitly_denied_passes():
    assert safety.danger_confirmed_by_answer("有没有便血？", "no") is None


def test_non_danger_question_never_vetoes():
    assert safety.danger_confirmed_by_answer("有没有口苦？", "yes") is None
    assert safety.danger_confirmed_by_answer("有没有口苦？", "unknown") is None


def test_symptom_and_question_are_both_checked():
    """G3 追问带着候选症状名，ReAct 只有问题文本——两个都看，取并集。"""
    assert safety.danger_confirmed_by_answer("最近大便怎么样？", "yes", symptom="黑便") is not None
    assert safety.danger_confirmed_by_answer("有没有黑便？", "yes", symptom="纳差") is not None
    assert safety.danger_confirmed_by_answer("最近大便怎么样？", "yes", symptom="纳差") is None


def test_chain_and_followup_both_call_the_shared_judgment():
    """两条追问路径都必须经过同一个函数，而不是各自 `mentions_danger(...) != "no"`。"""
    for path in ("core/chain.py", "core/followup.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "danger_confirmed_by_answer(" in src, path
        # 旧写法的残留：自己拼 asked + verdict 的判断
        assert not re.search(r"asked\w*\s*=\s*mentions_danger\(", src), path


# ---------- ε 只读一次 ----------


def test_epsilon_loader_is_defined_only_in_chain():
    defs = [p for p in _grep_sources("def load_epsilon_online") + _grep_sources("def _load_epsilon_online")]
    assert defs == ["core/chain.py"]
    import eval.run_eval as run_eval

    assert run_eval.load_epsilon_online is chain.load_epsilon_online
    assert run_eval.EPSILON_PATH is chain.EPSILON_PATH


# ---------- 方 → 药物集合 只定义一次 ----------


def test_normalized_herb_set_drops_empties_and_normalizes():
    assert normalized_herb_set(["炙甘草三钱", "云苓", "", "（包煎）"]) == {"甘草", "茯苓"}
    assert normalized_herb_set(None) == set()


def test_herb_set_construction_not_reimplemented_inline():
    """分歧度（chain）和 ε（estimate_epsilon）之前各写一遍集合推导式；现在这个
    推导式只允许出现在唯一实现 core/herbs.py 里。"""
    assert _grep_sources("normalize_herb(x) for x in") == ["core/herbs.py"]


# ---------- 症状文本匹配：query_case_graph 跟 _match_graph_symptoms 同一把尺 ----------


def test_symptom_text_matcher_is_fragment_aware():
    assert tools._symptom_text_matches("嗳气、泛酸", "饭后泛酸明显") is True
    assert tools._symptom_text_matches("嗳气、泛酸", "泛酸") is True
    assert tools._symptom_text_matches("嗳气、泛酸", "口苦") is False
    assert tools._symptom_text_matches("", "泛酸") is False


def test_query_case_graph_uses_the_shared_matcher(monkeypatch, tmp_path):
    """之前这里是裸的双向子串：「饭后泛酸明显」查不到 s=「嗳气、泛酸」的三元组，
    而同模块的 _match_graph_symptoms 对同样的词能匹配上。"""
    path = tmp_path / "triples.jsonl"
    path.write_text(json.dumps({
        "case_id": "ye_tianshi-001", "physician": "ye_tianshi",
        "s": "嗳气、泛酸", "p": "见于", "o": "肝胃不和", "source_span": "嗳气泛酸",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    monkeypatch.setattr(tools, "CASE_TRIPLES_PATH", path)
    tools.reset_tool_caches()
    try:
        out = tools.query_case_graph(symptom="饭后泛酸明显")
        assert out["available"] is True and out["total_matched"] == 1
        assert tools.query_case_graph(symptom="口苦")["total_matched"] == 0
    finally:
        tools.reset_tool_caches()


# ---------- 追问候选：两条产出路径同一套键 ----------


def test_shiwen_fallback_and_graph_ig_candidates_share_the_same_keys():
    fallback = tools._shiwen_fallback(1, set(), "test")[0]
    src = (ROOT / "core" / "tools.py").read_text(encoding="utf-8")
    # graph_ig 那一支的字典字面量：从 "source": "graph_ig" 往前后各找键名
    block = src[src.index('"source": "graph_ig"') - 400: src.index('"source": "graph_ig"') + 900]
    graph_ig_keys = set(re.findall(r'^\s*"([a-z_]+)":', block, re.M))
    assert set(fallback) == graph_ig_keys, (set(fallback) ^ graph_ig_keys)
    assert fallback["safety_relevant"] is False


# ---------- /api/consult 四个分支同一套键 ----------


def test_consult_response_branches_share_one_key_set():
    s1 = S1Normalize(symptoms=["纳差"], tongue="淡", pulse="细", unmapped=[])
    branches = {
        "success": _fake_outcome(),
        "rejected": _fake_rejected_outcome(),
        "retrieval_error": {**_fake_outcome(), "results": [], "divergence": None,
                            "retrieval_error": "检索模式「graph」在这台机器上不可用：缺文件"},
        "insufficient": {"s1": s1, "rejected": False, "results": [], "divergence": None,
                         "insufficient": True, "insufficient_reason": "症状太少"},
    }
    key_sets = {name: set(api_main._consult_response(o)) for name, o in branches.items()}
    assert len({frozenset(v) for v in key_sets.values()}) == 1, key_sets
    assert api_main._consult_response(branches["rejected"])["rejected"] is True
    assert api_main._consult_response(branches["insufficient"])["insufficient"] is True


# ---------- 批量跑：一条挂了不拖累其余 ----------


def test_consult_many_isolates_failures(capsys):
    def fake(q):
        if q == "坏":
            raise RuntimeError("LLM 挂了")
        return {"q": q}

    results, failures = chain.consult_many(["好1", "坏", "好2"], consult_fn=fake)
    assert results == [{"q": "好1"}, None, {"q": "好2"}]
    assert failures == [{"index": 2, "query": "坏", "error": "RuntimeError: LLM 挂了"}]
    assert "第 2 条失败" in capsys.readouterr().err


def test_eval_batch_entrypoints_use_consult_many():
    for path in ("eval/run_eval.py", "eval/mes/export.py"):
        src = (ROOT / path).read_text(encoding="utf-8")
        assert "consult_many(" in src, path
        assert "[consult(q) for q in queries]" not in src, path


# ---------- 图存储：类型词表在建图时就校验 ----------


def test_store_rejects_unknown_node_and_edge_types():
    store = NetworkXStore()
    with pytest.raises(ValueError, match="node_type"):
        store.add_node("x", node_type="Symptom")  # 大小写手误
    store.add_node("a", node_type="symptom")
    store.add_node("b", node_type="element")
    with pytest.raises(ValueError, match="edge_type"):
        store.add_edge("a", "b", edge_type="indicate")  # 少个 s
    store.add_edge("a", "b", edge_type="indicates")
    assert store.get_node("a") == {"node_type": "symptom"}


def test_store_still_accepts_untyped_nodes():
    """node_type 不是必填：没给就不校验，老的调用方不受影响。"""
    store = NetworkXStore()
    store.add_node("plain")
    assert store.get_node("plain") == {}


# ---------- export_sft：排除非公有领域医案要看得见 ----------


def test_filter_public_domain_reports_exclusions(capsys):
    from offline.export_sft import filter_public_domain
    from tests.test_export_sft import _case

    kept = filter_public_domain([_case(), _case(case_id="modern-001", copyright_status="copyrighted")])
    assert [c.case_id for c in kept] != ["modern-001"]
    assert "modern-001" in capsys.readouterr().err
