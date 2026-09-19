"""R42：节点释义从四节扩到八节，新增的五节**每一节都必须说清它的对照基准**。

## 为什么这个文件跟 tests/test_node_explain.py 分开

那个文件钉的是"零 LLM"和"取不到就说取不到"——R37 立这个面板时的两条纪律，
它们跟节数无关。这个文件钉的是 R42 新增的那五节各自的**诚实性判据**：

| 节 | 这一节最容易犯的错 | 判据 |
|---|---|---|
| 病机 | 从 definition 里切一句话当病机 | 必须说明"证候表没有独立的病机字段" |
| 药理 | 只显示有的谓词 | **缺哪个谓词必须列出来**，并带全库同类缺口 |
| 名老中医经验 | 空着不出现 | 查过了没有 ≠ 没查——要说出来并带基准 |
| 验证结果 | 现编一个"这味药验过了" | 报的是**可验证性**，并指向问诊结果那一份 |
| 循证对照 | 把"教材里有"写成"有循证支持" | 必须带 `GUIDELINE_GAP_NOTE` |

外加一条**性能预算**（总纲 §12：性能预算进测试）：八节比四节多读方剂本体、
功效同义表、规律层三张全量表，热态 p95 ≤ `NODE_EXPLAIN_BUDGET_MS`。
"""
from __future__ import annotations

import statistics
import time

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core.node_explain import (
    FORMULARY_PREDICATES,
    GUIDELINE_GAP_NOTE,
    MATERIA_PREDICATES,
    SECTION_ORDER,
    explain_node,
)

#: 每一类节点用一个真实存在的样本。**不用假名字**：假名字走的是"覆盖缺口"
#: 那一支，测不到有数据时那几节长什么样。
SAMPLES = [
    ("herb::四君子汤::党参", None),
    ("formula::四君子汤", None),
    ("syn::x", "肝胃不和证"),
    ("organ::脾", None),
    ("nature::气虚", None),
    ("sym::纳差", None),
    ("mech::脾失健运，湿浊内生", None),
    ("principle::健脾化湿", None),
    ("method::益气健脾", None),
]


def _sections(node_id, name=None):
    return {s["heading"]: s for s in explain_node(node_id, name=name)["sections"]}


# ---------- 一、八节的骨架 ----------

def test_the_section_order_is_the_chain_not_an_alphabet():
    assert SECTION_ORDER == ("是什么", "病机", "相似证型与鉴别点", "药理", "出处原文",
                             "名老中医经验", "验证结果", "循证对照", "注意")


def test_every_sample_node_yields_at_least_three_sections():
    """一个节点只出一节 = 这一类节点的 builder 基本没写。"""
    for node_id, name in SAMPLES:
        secs = _sections(node_id, name)
        assert len(secs) >= 3, (node_id, list(secs))


def test_no_section_is_an_empty_shell():
    for node_id, name in SAMPLES:
        for head, sec in _sections(node_id, name).items():
            assert sec["lines"], (node_id, head)
            assert all(ln.strip() for ln in sec["lines"]), (node_id, head)


# ---------- 二、病机 ----------

def test_the_syndrome_pathogenesis_section_refuses_to_fake_a_statement():
    """证候表**没有独立的病机字段**，有的是病位/病性两列 + definition 那段话。
    从 definition 里切一句话当病机是伪造，所以这一节必须把这件事说出来。"""
    sec = _sections("syn::x", "肝胃不和证")["病机"]
    joined = "".join(sec["lines"])
    assert "病位" in joined and "病性" in joined
    assert "不是一段现成的病机陈述" in joined
    assert "伪造" in joined


def test_the_pathogenesis_node_only_matches_element_words_not_the_whole_sentence():
    """整句是模型的自由叙述，拿它去子串比没有意义
    （CLAUDE.md 第 31 条列的三次撞墙都是字面比造成的）。"""
    sec = _sections("mech::脾失健运，湿浊内生")["病机"]
    joined = "".join(sec["lines"])
    assert "只比要素词" in joined
    assert "脾" in joined, "连病位都没匹配出来，说明要素匹配没跑"


def test_a_symptom_reports_co_occurrence_counts_not_probabilities():
    """共现计数被读成概率是这一节最容易造成的误解。"""
    sec = _sections("sym::纳差")["病机"]
    joined = "".join(sec["lines"])
    assert "共现计数，不是概率" in joined


def test_an_element_says_which_of_the_nine_layers_it_sits_on():
    for node_id, want in [("organ::脾", "病位"), ("nature::气虚", "病性")]:
        joined = "".join(_sections(node_id)["病机"]["lines"])
        assert want in joined, (node_id, joined)


# ---------- 三、药理：缺哪个谓词必须列出来 ----------

def test_the_herb_pharmacology_section_lists_the_missing_predicates():
    """只显示有的那几条会让一味缺归经的药看起来跟一味齐全的药一样完整，
    而归经缺失恰恰意味着验证器的归经规则对它恒不可判。

    **R63 §3.3 第 4 步改了这一条的后半截。** 原来还要求那句话带上全库基准
    （"全库同类缺口：归经 598/1232 味"），理由是 CLAUDE.md 那条"任何数字都
    必须带对照"。现在整个数字都不出现了，那条铁律因此**不再适用**
    ——它管的是"摆出来的数字要有基准"，不是"必须摆一个数字出来"。
    改的原因是 R62 §7 明令产品面不出现统计口径，而实测医师读到那句话得到的
    印象正是"这系统案例不足"（用户原话），不是"本体覆盖率如此"。
    要看数字的人去 `python -m scripts.diagnose_ontology_gaps`。
    """
    # 找一味"本体里有、但至少缺一个谓词"的药——全库都齐全的话这条测不到东西
    from core.ontology import get_ontology

    ont = get_ontology()
    target = next((h.name for h in ont.herbs.values()
                   if any(not h.has(p) for p in MATERIA_PREDICATES)), None)
    if target is None:
        pytest.skip("本体里每味药的六个谓词都齐全，这条测不到东西")
    missing = [p for p in MATERIA_PREDICATES if not ont.herb(target).has(p)]
    sec = _sections(f"herb::某方::{target}")["药理"]
    joined = "".join(sec["lines"])
    # 缺哪几项要说得出**具体是哪几项**，不是一句笼统的"资料不全"
    for p in missing:
        assert p in joined, f"{target} 缺 {p} 却没说：{joined}"
    # 而且不许用占位符冒充内容
    assert "暂无" not in joined and "案例不足" not in joined
    # 也不许在产品面上报全库统计
    import re
    assert "全库" not in joined
    assert not re.search(r"\d+/\d+ 味", joined), f"释义里又出现了覆盖率数字：{joined}"


def test_the_pharmacology_section_is_not_a_second_copy_of_what_is_what():
    """R42 把性味/归经/功效从「是什么」挪到「药理」。**两节不许都有**
    ——同一份内容出现两遍，改一处就会漂。"""
    secs = _sections("herb::四君子汤::党参")
    what = "".join(secs["是什么"]["lines"])
    assert "性味" not in what and "归经" not in what
    assert "性味" in "".join(secs["药理"]["lines"])


def test_a_formula_missing_the_role_predicates_says_which_rule_that_breaks():
    """缺君臣佐使会让验证器的「君臣佐使结构」那条规则对这个方不可判。"""
    from core.ontology import get_ontology

    ont = get_ontology()
    target = next((f.name for f in ont.formulas.values()
                   if any(not any(r.span.strip() for r in (f.refs.get(p) or ()))
                          for p in FORMULARY_PREDICATES)), None)
    if target is None:
        pytest.skip("方剂本体里每个方的八个谓词都齐全")
    joined = "".join(_sections(f"formula::{target}")["药理"]["lines"])
    assert "本体里缺这几个谓词" in joined


def test_a_method_node_resolves_effects_through_the_shared_synonym_table(monkeypatch):
    """治法↔功效的等价判定只能有一处实现——就是验证器
    `check_effect_matches_method` 用的那张 `expand_effect`。

    **R56 之前这条测试靠断言正文里出现字面路径 `core/effect_synonyms.py`
    来证明"复用了同一处实现"**——R56 §6 第 5 条要求产品面不许出现文件路径，
    那条断言本身就在钉一个不该出现的东西，删文件路径的同时必须换一种
    真正测"复用"的方式：直接 monkeypatch 那个共享函数，断言 node_explain
    真的调用了它，而不是另外拼一份字面匹配。"""
    calls = []
    import core.effect_synonyms as es
    orig = es.expand_effect

    def spy(text):
        calls.append(text)
        return orig(text)

    monkeypatch.setattr(es, "expand_effect", spy)
    sec = _sections("method::益气健脾")["药理"]
    assert calls and all(c == "益气健脾" for c in calls), (
        "没有走 core.effect_synonyms.expand_effect，是另一处实现")
    joined = "".join(sec["lines"])
    assert "条" in joined and ("同义词表" in joined or "同义功效词" in joined)


def test_an_out_of_table_method_says_the_match_degrades_to_substring():
    """表外说法会退化成裸子串比——**命中少不代表没有对得上的药**，
    这句话必须说出来，否则读者会把"0 味"读成"无药可用"。"""
    joined = "".join(_sections("method::一个表里必定没有的治法说法")["药理"]["lines"])
    assert "退化成裸子串比" in joined


# ---------- 四、名老中医经验 ----------

def test_the_renamed_section_carries_the_non_textbook_caveat():
    """这是**本项目医案库的统计**，不是教材口径。少了这句话，
    "叶天士常用 12g" 会被当成一条教材依据。"""
    for node_id, name in [("herb::四君子汤::党参", None), ("syn::x", "肝胃不和证")]:
        sec = _sections(node_id, name).get("名老中医经验")
        if sec is None:
            continue
        assert "非教材" in (sec.get("source") or ""), (node_id, sec)


def test_an_unmentioned_herb_says_it_was_looked_up_and_not_found():
    """**空着不等于没查。** 一节整块不出现，在界面上跟"这一节我们没做"长得
    一模一样；而事实是"查过了、规律层里没有这味药"。"""
    from core.ontology import get_ontology

    ont = get_ontology()
    pats = ont.patterns_for("", physician=None)
    used = {h for p in pats for h in (p.get("herbs") or [])}
    target = next((h.name for h in ont.herbs.values() if h.name not in used), None)
    if target is None:
        pytest.skip("规律层覆盖了本体里每一味药")
    sec = _sections(f"herb::某方::{target}")["名老中医经验"]
    joined = "".join(sec["lines"])
    assert "没有一条提到" in joined
    assert "不是" in joined, "没有把「查不到」和「名医不用」区分开"
    # 带基准：规律层一共多少条
    assert any(ch.isdigit() for ch in joined)


def test_a_formula_says_the_regularity_layer_has_no_per_formula_bucket():
    """R35 的规律层按**药**和按**证**分档，没有按方的档位。
    这件事要说出来，而不是让那一节整块消失。"""
    joined = "".join(_sections("formula::四君子汤")["名老中医经验"]["lines"])
    assert "没有按方统计的档位" in joined


# ---------- 五、验证结果 ----------

def test_the_verification_section_reports_verifiability_not_a_verdict():
    """结论是对**一整张方**求值出来的，这个接口只有一个节点 id。
    在这里现算一个"这味药验过了"就得自己拼一个假 S3——那是第二处实现，
    而且算出来的结论可能跟问诊结果那一份不一致。"""
    joined = "".join(_sections("herb::四君子汤::党参")["验证结果"]["lines"])
    assert "可验证" in joined
    assert "不在这里重复一份" in joined
    # R56 §6 第 5 条：这句指向问诊结果「验证」段的话不许带文件路径
    # （原来断言过 "core/formula_verifier.py" 在正文里，那正是要删的东西）。
    assert "问诊结果的「验证」段" in joined


def test_the_rule_names_come_from_the_backend_label_table():
    """规则的中文名只有一张表（`RULE_LABELS`）。这里另写一份的话，
    加规则要改两处，而漏改那一处的表现是界面上冒出一个英文 id。"""
    from core.formula_verifier import RULE_LABELS

    joined = "".join(_sections("herb::四君子汤::党参")["验证结果"]["lines"])
    for rule, label in RULE_LABELS.items():
        assert label in joined, f"{rule} 的中文名没出现"
        assert rule not in joined, f"{rule} 的英文 id 漏到界面上了"


def test_a_missing_predicate_names_the_rule_it_makes_unverifiable():
    from core.ontology import get_ontology

    ont = get_ontology()
    target = next((h.name for h in ont.herbs.values() if not h.meridians), None)
    if target is None:
        pytest.skip("本体里每味药都有归经")
    joined = "".join(_sections(f"herb::某方::{target}")["验证结果"]["lines"])
    assert "不可验证——缺「归经」谓词" in joined


def test_a_syndrome_reports_the_two_rules_it_can_feed():
    joined = "".join(_sections("syn::x", "肝胃不和证")["验证结果"]["lines"])
    assert "归经覆盖病位" in joined and "寒热方向" in joined


# ---------- 六、循证对照 ----------

@pytest.mark.parametrize("node_id,name", SAMPLES)
def test_every_evidence_section_carries_the_guideline_gap_note(node_id, name):
    """**这一节最容易犯的错就是把"教材里有这一条"写成"有循证支持"。**
    那句口径声明不是免责话术，是这一节存在的前提。"""
    sec = _sections(node_id, name).get("循证对照")
    assert sec is not None, f"{node_id} 没有循证对照这一节"
    assert GUIDELINE_GAP_NOTE in sec["lines"]


def test_the_gap_note_says_coverage_is_not_the_same_as_evidence_grading():
    """R56 §6 第 6 条：产品面把这句口径声明收窄成一句干净的话，不逐段展开
    "全文不在项目内/没有可用授权文本"这些内部措辞——原来那段话里两处
    `**...**` markdown 星号没有渲染，读者看到的是字面星号，比没有这句话
    观感更差（见截图实证）。核心信息（教材收录 ≠ 循证等级）保留，
    R46 那段更完整的解释挪进模块文档（不印进响应体）。"""
    assert "循证等级" in GUIDELINE_GAP_NOTE
    assert "**" not in GUIDELINE_GAP_NOTE, "markdown 星号不会被前端渲染成粗体"


def test_the_herb_evidence_baseline_names_the_books_and_counts():
    """「这味药有出处」是句空话。基准是"本草层一共五部书 9776 条"。
    数字从 `Ontology.source_books()` 现算，不写死。"""
    from core.ontology import get_ontology

    books = get_ontology().source_books()["materia_medica"]
    joined = "".join(_sections("herb::四君子汤::党参")["循证对照"]["lines"])
    assert f"{len(books)} 部书" in joined
    assert f"{sum(books.values())} 条三元组" in joined
    for b in books:
        assert f"《{b}》" in joined


def test_the_syndrome_evidence_baseline_reports_the_source_label_distribution():
    from core.node_explain import _load_standard_rows

    rows = _load_standard_rows()
    joined = "".join(_sections("syn::x", "肝胃不和证")["循证对照"]["lines"])
    assert f"{len(rows)} 条" in joined
    assert "textbook" in joined, "来源标签分布没报出来"
    assert "ICD-11" in joined


def test_the_icd11_gap_is_stated_as_a_gap_not_as_silence():
    """带 ICD-11 的只有个别几条。"没有编码"要说成"还没对上"，
    不是"对上了没写"。"""
    joined = "".join(_sections("syn::x", "肝胃不和证")["循证对照"]["lines"])
    assert "还没有对上" in joined


def test_the_book_counts_are_not_hardcoded_in_any_user_facing_string():
    """数字写死在**会显示给人看的字符串里**，数据一换就变成假话。

    **只查非文档字符串的字面量**：模块文档里必须能举一个具体的例子
    （"本草层一共五部书 9776 条"那句话正是在解释这一节为什么存在），
    而文档不会显示给用户。这跟 CLAUDE.md 那条数 `Field(min_length=1)` 的
    规矩是同一个道理——数法要定死，不然判据自己会变成一条假绿。
    """
    import ast
    import pathlib as _pl

    src = (_pl.Path(__file__).resolve().parent.parent
           / "core" / "node_explain.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docstrings.add(id(body[0].value))
    shown = [n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and id(n) not in docstrings]
    for literal in ("9776", "3184", "1458", "235 首", "337 条"):
        bad = [s for s in shown if literal in s]
        assert not bad, f"「{literal}」被写死在会显示的字符串里：{bad[:2]}"


# ---------- 七、性能预算 ----------

def test_the_endpoint_stays_inside_its_budget_when_warm():
    """总纲 §12：性能预算进测试。八节比四节多读三张全量表（方剂本体、
    功效同义表、规律层），所以这条不是形式——它是这一轮唯一能挡住
    "顺手在释义里多查一张全表"的东西。

    **按热态量**（本体已加载）：冷启动是本体惰性初始化的一次性成本，
    跟这个接口的稳态延迟不是同一件事。"""
    client = TestClient(api_main.app)
    warm = {"node": "herb::四君子汤::党参"}
    for _ in range(3):                      # 预热：把三张表都读进来
        assert client.get("/api/node_explain", params=warm).status_code == 200
    samples = []
    for node_id, name in SAMPLES * 3:
        params = {"node": node_id}
        if name:
            params["name"] = name
        t0 = time.perf_counter()
        r = client.get("/api/node_explain", params=params)
        samples.append((time.perf_counter() - t0) * 1000)
        assert r.status_code == 200
    samples.sort()
    p95 = samples[min(len(samples) - 1, int(len(samples) * 0.95))]
    budget = api_main.NODE_EXPLAIN_BUDGET_MS
    assert p95 <= budget, (
        f"p95 {p95:.1f} ms 超过预算 {budget} ms"
        f"（中位数 {statistics.median(samples):.1f} ms，最大 {samples[-1]:.1f} ms）")


def test_the_budget_constant_is_documented_as_warm_state():
    import inspect

    src = inspect.getsource(api_main)
    i = src.index("NODE_EXPLAIN_BUDGET_MS")
    head = src[max(0, i - 700):i]
    assert "热态" in head, "预算没说清是冷态还是热态——那个数会被拿去量错的东西"
