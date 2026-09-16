"""R21：缓存前缀生成器。

零 LLM 调用、零网络。`cases.json` 和药理层两个文件在沙盒里都没有，所以除了
标了 real_corpus 的那几条，这里全部用**显式传入**的合成数据——不依赖磁盘，
两台机器上跑出来一样。
"""
from __future__ import annotations

import hashlib

import pytest

from core.context_prefix import (
    CUT_ORDER,
    EMIT_ORDER,
    FORMULARY_ALL_PREDICATES,
    FORMULARY_QUICK_PREDICATES,
    MATERIA_ALL_PREDICATES,
    MATERIA_QUICK_PREDICATES,
    PREFIX_TOKEN_BUDGET,
    REFS_POINTER,
    REFS_POINTER_EMPTY,
    SECTION_CASES,
    SECTION_ENTRIES,
    SECTION_FORMULARY,
    SECTION_INSTRUCTIONS,
    SECTION_MATERIA,
    SECTION_VARIABLE,
    SHARED_SECTIONS,
    assemble,
    budget_plan,
    build_cases_section,
    build_entries_section,
    build_entry_index,
    build_formulary_quick_table,
    build_materia_quick_table,
    build_physician_prefix,
    build_shared_prefix,
    count_tokens,
    formulary_quick_line,
    materia_quick_line,
    quick_line,
    split_s3_template,
    synthetic_cases,
    tokenizer_name,
)
from core.llm import load_prompt

MATERIA_ROWS = [
    {"s": "黄芪", "p": "性味", "o": "甘，微温"},
    {"s": "黄芪", "p": "归经", "o": "脾、肺"},
    {"s": "黄芪", "p": "功效", "o": "补气升阳"},
    {"s": "黄芪", "p": "用量", "o": "9~30g"},
    {"s": "黄芪", "p": "炮制", "o": "蜜炙"},
    {"s": "白术", "p": "性味", "o": "苦甘，温"},
    {"s": "白术", "p": "功效", "o": "健脾燥湿"},
]
FORMULARY_ROWS = [
    {"s": "四君子汤", "p": "组成", "o": "人参、白术、茯苓、甘草"},
    {"s": "四君子汤", "p": "主治", "o": "脾胃气虚"},
    {"s": "四君子汤", "p": "君药", "o": "人参"},
    {"s": "补中益气汤", "p": "组成", "o": "黄芪、人参、白术"},
]


def _materia():
    return build_entry_index("materia_medica", MATERIA_ROWS)


def _formulary():
    return build_entry_index("formulary", FORMULARY_ROWS)


def _cases(n=3, pid="ye_tianshi"):
    return synthetic_cases([pid], n)


# ---------- 四段各自生成 ----------

def test_quick_line_keeps_a_fixed_column_count():
    """缺的谓词写 `-` 不跳过：列数固定，模型（和 R24 的 tooltip）才能靠位置读。"""
    line = materia_quick_line("白术", _materia()["白术"])
    assert line.split("｜")[0] == "白术"
    assert len(line.split("｜")) == 1 + len(MATERIA_QUICK_PREDICATES)
    assert "-" in line  # 归经/用量/禁忌都缺


def test_quick_line_is_the_single_definition_of_the_row_format():
    """R24 的 tooltip 走同一个函数，不是照抄格式。两处各写一遍的话，
    "模型看到的那行"和"界面显示的那行"会不一致。"""
    preds = _materia()["黄芪"]
    assert materia_quick_line("黄芪", preds) == quick_line("黄芪", preds, MATERIA_QUICK_PREDICATES)
    fp = _formulary()["四君子汤"]
    assert formulary_quick_line("四君子汤", fp) == quick_line("四君子汤", fp, FORMULARY_QUICK_PREDICATES)


def test_quick_tables_are_sorted_by_name():
    """排序固定 = 生成确定 = 缓存命中。"""
    table = build_materia_quick_table(_materia())
    rows = [ln for ln in table.splitlines() if "｜" in ln and not ln.startswith("##")]
    names = [r.split("｜")[0] for r in rows]
    assert names == sorted(names)


def test_missing_pharmacology_files_say_so_instead_of_being_silently_empty():
    """空表和"这本书里确实没有"是两件事。"""
    assert "不在这台机器上" in build_materia_quick_table({})
    assert "不在这台机器上" in build_formulary_quick_table({})


def test_cases_section_uses_raw_excerpt_and_not_raw():
    """`raw` 是整个粗段、多诊共享、大量重复，不给；`raw_excerpt` 全给。"""
    cases = _cases(2)
    text = build_cases_section(cases)
    for c in cases:
        assert c.raw_excerpt in text
    # raw 里那句重复六遍的话不该整段出现在前缀里
    assert cases[0].raw not in text


def test_cases_section_keeps_the_order_it_was_given():
    """排序**不在这里做**——`physician_cases` / `full_context_hits` 负责排序，
    这个函数只格式化。两处都排的话，哪一处排错了看不出来是谁的责任。"""
    cases = list(reversed(_cases(4)))
    text = build_cases_section(cases)
    positions = [text.index(f"【参考医案】{c.case_id}") for c in cases]
    assert positions == sorted(positions), "给什么顺序就按什么顺序摆"


def test_the_sorting_happens_where_the_corpus_is_selected():
    from core.retrieval import full_context_hits

    cases = list(reversed(_cases(4)))
    hits = full_context_hits(cases, "ye_tianshi")
    assert [c.case_id for c, _ in hits] == sorted(c.case_id for c in cases)


def test_cases_section_reuses_format_case_block():
    """参考医案块的格式是 E3/E4 闸门验过的，不许有第二份实现。"""
    from core.chain import _format_case_block

    cases = _cases(2)
    assert build_cases_section(cases) == "\n\n".join(
        ["## 医案全量（2 诊次，按 case_id 排序）", *[_format_case_block(c) for c in cases]])


def test_entries_section_reports_the_denominator():
    """命中率低的时候要看得见，而不是只看到"有 3 味药的条目"。"""
    cases = _cases(2)
    text = build_entries_section(cases, _materia(), _formulary())
    # 合成医案用了 人参/白术/茯苓/甘草 四味，本草表里只有 白术
    assert "药材 1/4" in text
    assert "方剂 1/1" in text
    assert "### 白术" in text


def test_entries_section_gives_all_predicates():
    cases = _cases(1)
    text = build_entries_section(cases, _materia(), _formulary())
    # 白术在表里只有性味/功效两条，其余谓词没有值就不写行（不写"-"，
    # 完整条目跟速查表不同：速查表要对齐列，条目是逐行给事实）
    assert "性味：苦甘，温" in text
    assert "功效：健脾燥湿" in text
    assert len(MATERIA_ALL_PREDICATES) == 6
    assert len(FORMULARY_ALL_PREDICATES) == 8


# ---------- s3 模板一个字都没改 ----------

def test_split_s3_template_is_lossless():
    template = load_prompt("s3_syndrome")["system"]
    head, tail = split_s3_template(template)
    assert head + tail == template, "切开再拼回去必须逐字节等于原模板"
    assert head and tail


def test_split_puts_instructions_in_head_and_this_consults_data_in_tail():
    head, tail = split_s3_template()
    # 指令和 schema 在 head
    assert "$name" in head
    assert '"selected"' in head
    # 本次问诊的三个占位符在 tail
    for ph in ("$elements_summary", "$symptoms", "$refs"):
        assert ph in tail and ph not in head


def test_split_raises_when_the_marker_is_gone():
    """找不到分界标志时抛异常，不静默把整份模板当 head——那会让本次问诊的
    症状和参考医案一起进"稳定前缀"，缓存永远不命中，而且看日志看不出来。"""
    with pytest.raises(ValueError, match="分界标志"):
        split_s3_template("完全不含标志的模板")


# ---------- 确定性 ----------

def test_physician_prefix_is_deterministic():
    cases = _cases(5)
    a = build_physician_prefix("ye_tianshi", cases=cases, materia=_materia(), formulary=_formulary())
    b = build_physician_prefix("ye_tianshi", cases=cases, materia=_materia(), formulary=_formulary())
    assert hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest()


def test_changing_one_character_in_a_case_changes_the_sha():
    """对 cases.json 的任何一处改动 sha 必变——否则"语料换了但缓存还命中旧的"
    这件事没有任何东西拦得住。"""
    cases = _cases(3)
    before = build_physician_prefix("ye_tianshi", cases=cases, materia={}, formulary={})
    mutated = [c.model_copy(update={"raw_excerpt": (c.raw_excerpt or "") + "改"})
               if i == 1 else c for i, c in enumerate(cases)]
    after = build_physician_prefix("ye_tianshi", cases=mutated, materia={}, formulary={})
    assert before != after


def test_synthetic_cases_are_deterministic():
    """`--synthetic` 跑两次报出来的 token 数要一样，不然这个功能自己就在漂。"""
    a = synthetic_cases(["ye_tianshi"], 4)
    b = synthetic_cases(["ye_tianshi"], 4)
    assert [c.model_dump() for c in a] == [c.model_dump() for c in b]
    assert all("合成" in (c.raw_excerpt or "") for c in a)


def test_shared_prefix_is_byte_identical_across_physicians():
    """这一段就是跨医家共享缓存的全部。按医家分叉的话共享就没了。"""
    plan = None
    a = build_shared_prefix(plan, materia=_materia(), formulary=_formulary())
    b = build_shared_prefix(plan, materia=_materia(), formulary=_formulary())
    assert a == b
    # 共享段里不许出现任何医家名/医家 id
    from core.physicians import PHYSICIANS, physicians_all

    for pid, info in physicians_all(PHYSICIANS).items():
        assert pid not in a and info["name"] not in a


# ---------- 发送顺序与共享档 ----------

def test_emit_order_puts_the_shared_sections_first():
    """指令段带医家名（模板第一行就是「你正在模拟清代医家「$name」」），
    放最前面会让三位医家的前缀在第一个字节就分叉，后面再相同也不会命中。"""
    assert EMIT_ORDER[:2] == SHARED_SECTIONS
    assert EMIT_ORDER.index(SECTION_INSTRUCTIONS) > EMIT_ORDER.index(SECTION_MATERIA)
    assert EMIT_ORDER[-1] == SECTION_VARIABLE


def test_assemble_order_matches_emit_order():
    cases = _cases(2)
    text = assemble("ye_tianshi", case_block=cases, symptoms="纳差",
                    elements_summary="脾（病位）", materia=_materia(), formulary=_formulary())
    i_materia = text.index("## 本草速查表")
    i_instructions = text.index("你正在模拟清代医家")
    i_cases = text.index("## 医案全量")
    i_variable = text.index("证素分析：")
    assert i_materia < i_instructions < i_cases < i_variable


# ---------- §6 不重复医案 ----------

def test_variable_section_points_at_the_cases_instead_of_repeating_them():
    """医案已经在 §4 的稳定前缀里，§6 再重复一遍等于把 18 万 token 又按
    未命中价付一次。"""
    cases = _cases(3)
    text = assemble("ye_tianshi", case_block=cases, symptoms="纳差", materia={}, formulary={})
    assert REFS_POINTER in text
    for c in cases:
        # 数的是**医案块的表头**，不是 case_id 这个字符串——合成语料的
        # raw_excerpt 里本来就写着自己的 id，按字符串数会把那一次也算进来。
        assert text.count(f"【参考医案】{c.case_id}") == 1, f"{c.case_id} 的医案块出现了不止一次"


def test_empty_case_block_says_so():
    text = assemble("ye_tianshi", case_block=[], symptoms="纳差", materia={}, formulary={})
    assert REFS_POINTER_EMPTY in text
    assert REFS_POINTER not in text


# ---------- 预算与裁剪 ----------

def test_budget_default_is_500k():
    assert PREFIX_TOKEN_BUDGET == 500_000


def test_cut_order_never_touches_the_cases_section():
    """§4 医案永远全量——它是这个系统的立身之本。"""
    assert SECTION_CASES not in CUT_ORDER
    assert SECTION_INSTRUCTIONS not in CUT_ORDER
    assert CUT_ORDER == (SECTION_FORMULARY, SECTION_MATERIA, SECTION_ENTRIES)


def test_budget_plan_cuts_in_the_declared_order():
    cases = _cases(3)
    kw = {"cases": cases, "materia": _materia(), "formulary": _formulary()}
    full = budget_plan(["ye_tianshi"], budget=10**9, **kw)
    assert full.dropped == ()
    total = sum(full.tokens_by_physician["ye_tianshi"].values())
    # 预算刚好比合计小一点 → 只裁第一顺位（方剂速查表）
    formulary_tokens = full.tokens_by_physician["ye_tianshi"][SECTION_FORMULARY]
    one = budget_plan(["ye_tianshi"], budget=total - 1, **kw)
    assert one.dropped == (SECTION_FORMULARY,)
    # 再紧一点 → 连本草速查表一起裁
    two = budget_plan(["ye_tianshi"], budget=total - formulary_tokens - 1, **kw)
    assert two.dropped[:2] == (SECTION_FORMULARY, SECTION_MATERIA)


def test_shared_section_cuts_are_decided_globally_not_per_physician():
    """共享段一旦按医家分叉，它就不再共享，跨医家缓存复用整个失效。
    所以决定用"最大的那位医家"来定。"""
    cases = synthetic_cases(["ye_tianshi"], 2) + synthetic_cases(["wu_jutong"], 12)
    kw = {"cases": cases, "materia": _materia(), "formulary": _formulary()}
    full = budget_plan(["ye_tianshi", "wu_jutong"], budget=10**9, **kw)
    biggest = max(sum(v.values()) for v in full.tokens_by_physician.values())
    plan = budget_plan(["ye_tianshi", "wu_jutong"], budget=biggest - 1, **kw)
    assert plan.dropped, "最大的那位超预算就该裁"
    # 裁了之后两位医家的共享段还是同一份
    a = build_shared_prefix(plan, materia=_materia(), formulary=_formulary())
    b = build_shared_prefix(plan, materia=_materia(), formulary=_formulary())
    assert a == b


def test_dropped_sections_really_disappear_from_the_prefix():
    cases = _cases(3)
    kw = {"cases": cases, "materia": _materia(), "formulary": _formulary()}
    full = budget_plan(["ye_tianshi"], budget=10**9, **kw)
    total = sum(full.tokens_by_physician["ye_tianshi"].values())
    plan = budget_plan(["ye_tianshi"], budget=total - 1, **kw)
    text = build_shared_prefix(plan, materia=_materia(), formulary=_formulary())
    assert "## 本草速查表" in text
    assert "## 方剂速查表" not in text


# ---------- token 尺 ----------

def test_tokenizer_name_is_reported_and_the_estimate_is_conservative():
    """报告里引这些数的地方必须带上 `tokenizer_name()`。保守估算是**上界**：
    宁可把预算算得更紧，也不要因为估小了而静默超预算。"""
    name = tokenizer_name()
    assert name in ("tiktoken:cl100k_base", "conservative-estimate")
    cjk = "脘腹痞满纳谷不香"
    assert count_tokens(cjk) >= len(cjk) or name.startswith("tiktoken")


def test_the_token_ruler_is_not_loaded_at_import_time():
    """`tiktoken.get_encoding()` 首次调用会去网络拉 BPE 表。这个模块被 core.chain
    导入、chain 被 api.main 导入，所以在模块顶层求值 = `import api.main` 联网。
    判据直接钉模块里那个哨兵：新装一份之后它必须还是「没试过」的状态。

    用 spec_from_file_location 在**另一个名字**下装一份，跑完就摘掉，不动
    `core.context_prefix` 本身：`importlib.reload` 会把同一个模块对象的字典换掉，
    而别的模块（core.chain）在 import 时就抓走了里面的函数对象，测试之间会留下
    看不见的耦合。临时名字必须先进 sys.modules——模块里有 `@dataclass`，
    它要按 `cls.__module__` 回查模块字典，查不到直接抛 AttributeError。
    """
    import importlib.util
    import sys

    import core.context_prefix as mod

    name = "_ctxprefix_fresh_for_test"
    spec = importlib.util.spec_from_file_location(name, mod.__file__)
    fresh = importlib.util.module_from_spec(spec)
    sys.modules[name] = fresh
    try:
        spec.loader.exec_module(fresh)
        assert fresh._encoder is fresh._ENCODER_UNSET
        fresh.tokenizer_name()         # 问一次才允许去加载
        assert fresh._encoder is not fresh._ENCODER_UNSET
    finally:
        sys.modules.pop(name, None)


def test_count_tokens_grows_with_text():
    assert count_tokens("脾胃") < count_tokens("脾胃气虚，中焦运化失司")
