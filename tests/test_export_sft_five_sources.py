"""R18-G：五源合并 + 门类过滤 + 配伍提示。

五源指链路样本的依据来自哪五个地方：
  1. 医案三元组（data/case_triples.jsonl，X3）
  2. 本草（data/standard/materia_medica.jsonl，S6）
  3. 方剂（data/standard/formulary.jsonl，S7）
  4. TCMEval-SDT 的专家说明
  5. 《脾胃论》立论层（data/standard/rationale_pwl.jsonl，R18-D）—— 这一轮新接的
"""
from __future__ import annotations

import json

from core.safety_output import INCOMPATIBLE_TRAINING_NOTE
from core.schemas import CaseRecord
from offline.export_sft import (
    CHAIN_STEPS,
    PWL_PREDICATES_FOR_FORMULA,
    PWL_PREDICATES_FOR_PATHOGENESIS,
    RATIONALE_PWL_PATH,
    TARGET_CHAIN,
    export_chain,
    filter_by_scope,
    incompatible_note,
    load_rationale_pwl,
    pwl_rationale,
    to_chain_sample,
)


def _case(**kw) -> CaseRecord:
    base = dict(
        case_id="c1", case_group_id="g1", physician="li_ke", raw="某，男，56 岁……",
        symptoms=["胃脘胀痛"], tongue="舌淡", pulse="脉弦",
        syndrome="脾胃虚寒", pathogenesis="形体劳役则脾病，中阳不振",
        treatment_principle="温中健脾", formula="平胃散", herbs=["苍术", "厚朴"],
        copyright_status="public_domain",
    )
    base.update(kw)
    return CaseRecord(**base)


# ---------- 第五源 ----------

def test_the_committed_pwl_file_is_loadable_and_sorted_longest_first():
    """拿 o 当子串去比对，必须先比长的：不然「虚」会抢在「脾胃虚寒」之前命中。"""
    idx = load_rationale_pwl()
    assert idx, "data/standard/rationale_pwl.jsonl 在版本控制里，应该读得到"
    lengths = [len(o) for _, o, _ in idx]
    assert lengths == sorted(lengths, reverse=True)


def test_pwl_index_drops_single_character_conclusions():
    """单字论断（治法那 21 条，o 全是 升/降/补/泻）拿去子串匹配几乎必然命中，
    那不是找到依据，是噪声。"""
    idx = load_rationale_pwl()
    assert all(len(o) >= 2 for _, o, _ in idx)
    assert not any(pred == "治法" for pred, _, _ in idx)


def test_pwl_rationale_matches_on_the_conclusion_substring():
    idx = [("病机", "脾病", "形体劳役则脾病，脾病则怠惰嗜卧")]
    hit = pwl_rationale(idx, PWL_PREDICATES_FOR_PATHOGENESIS, "形体劳役则脾病，中阳不振")
    assert hit is not None
    span, src = hit
    assert span.startswith("形体劳役")
    assert src == "rationale_pwl:脾胃论"


def test_pwl_rationale_respects_the_predicate_whitelist():
    """混用谓词会让「加药」那 81 条去给证型步当依据。"""
    idx = [("加药", "防风", "腹中急缩，或脉弦，加防风")]
    # 匹配比的是 o（「防风」）在不在文本里，所以文本要含「防风」才可能命中；
    # 命中与否**只由谓词白名单决定**这件事才是这条要钉的。
    assert pwl_rationale(idx, PWL_PREDICATES_FOR_PATHOGENESIS, "方中加防风") is None
    assert pwl_rationale(idx, ("加药",), "方中加防风") is not None


def test_pwl_rationale_on_empty_text_is_none():
    """case.pathogenesis 可以是 None（教材前三步没命中的医案）。"""
    idx = load_rationale_pwl()
    assert pwl_rationale(idx, PWL_PREDICATES_FOR_PATHOGENESIS, None) is None
    assert pwl_rationale(idx, PWL_PREDICATES_FOR_PATHOGENESIS, "") is None


def test_pwl_is_the_last_fallback_for_the_pathogenesis_step():
    """医案三元组有「提示」时用它，没有才退到《脾胃论》——顺序反了会让古籍论断
    盖掉这一例自己的原文出处。"""
    case = _case()
    pwl = [("病机", "脾病", "形体劳役则脾病，脾病则怠惰嗜卧")]
    triples = [{"p": "提示", "source_span": "这一例自己的原文"}]
    with_triples = to_chain_sample(case, triples, pwl=pwl)
    step = next(s for s in with_triples["chain"] if s["step"] == "症状→病机")
    assert step["rationale"] == "这一例自己的原文"
    assert step["rationale_source"] == f"case:{case.case_id}"

    without = to_chain_sample(case, [], pwl=pwl)
    step = next(s for s in without["chain"] if s["step"] == "症状→病机")
    assert step["rationale_source"] == "rationale_pwl:脾胃论"


def test_pwl_also_backs_the_formula_step():
    case = _case(formula="平胃散")
    pwl = [("用方", "平胃散", "如脉缓，病怠惰嗜卧，此湿胜，从平胃散")]
    sample = to_chain_sample(case, [], pwl=pwl)
    step = next(s for s in sample["chain"] if s["step"] == "治法→方剂")
    assert step["rationale_source"] == "rationale_pwl:脾胃论"
    assert PWL_PREDICATES_FOR_FORMULA == ("用方",)


def test_export_chain_reports_how_many_steps_the_fifth_source_gave():
    """命中率天然低——报出来才看得见"接了但一条没命中"和"接了且有用"的区别。"""
    pwl = [("病机", "脾病", "形体劳役则脾病，脾病则怠惰嗜卧")]
    _, stats = export_chain([_case()], {}, pwl=pwl)
    assert stats["rationale_pwl"] == {"index_size": 1, "steps": 1}

    _, stats0 = export_chain([_case()], {}, pwl=[])
    assert stats0["rationale_pwl"] == {"index_size": 0, "steps": 0}


# ---------- 门类过滤 ----------

def test_filter_by_scope_excludes_nothing_by_default():
    """R18-G 的默认：一个都不排。默认排肿瘤会让王云启 77 例全部进不来。"""
    cases = [_case(case_id="a", scope="oncology"), _case(case_id="b", scope="spleen_stomach")]
    assert [c.case_id for c in filter_by_scope(cases)] == ["a", "b"]


def test_filter_by_scope_excludes_the_named_scope(capsys):
    cases = [_case(case_id="a", scope="oncology"), _case(case_id="b", scope="spleen_stomach")]
    kept = filter_by_scope(cases, ("oncology",))
    assert [c.case_id for c in kept] == ["b"]
    assert "排除 1 条" in capsys.readouterr().err


def test_filter_by_scope_keeps_cases_whose_scope_was_never_judged():
    """scope is None ≠ "未知所以排掉"：叶天士/吴鞠通那批抽取脚本不产出这个字段，
    把 None 当命中会把原本的两位医家整个清空。"""
    cases = [_case(case_id="ye", scope=None), _case(case_id="lk", scope="oncology")]
    assert [c.case_id for c in filter_by_scope(cases, ("oncology",))] == ["ye"]


def test_scope_and_incompatible_flags_land_in_meta():
    """训练集里"这条是哪个门类/有没有反药"事后可查——不落 meta 只能靠重跑反推。"""
    sample = to_chain_sample(_case(scope="oncology", has_incompatible_pair=True), [])
    assert sample["meta"]["scope"] == "oncology"
    assert sample["meta"]["has_incompatible_pair"] is True


def test_export_chain_reports_samples_by_scope():
    _, stats = export_chain([_case(case_id="a", scope="oncology"),
                             _case(case_id="b", case_group_id="g2", scope=None)], {})
    assert stats["samples_by_scope"] == {"oncology": 1, "未判": 1}


# ---------- 配伍提示 ----------

def test_incompatible_note_only_fires_for_tagged_cases():
    assert incompatible_note(_case(has_incompatible_pair=True)) == INCOMPATIBLE_TRAINING_NOTE
    assert incompatible_note(_case(has_incompatible_pair=False)) is None


def test_the_note_is_a_chain_step_not_a_meta_field():
    """塞 meta 模型学不到它，而不学它就会学成"这种配伍可以开"。"""
    sample = to_chain_sample(_case(has_incompatible_pair=True), [])
    last = sample["chain"][-1]
    assert last["step"] == "配伍提示"
    assert last["output"] == INCOMPATIBLE_TRAINING_NOTE
    # 依据是这一例本身——是这一例的处方含反药，不是某本书说的
    assert last["rationale_source"] == f"case:{sample['meta']['case_id']}"
    assert last["rationale"]


def test_the_note_step_is_not_part_of_the_target_chain():
    """它不是辨证链的一环，是附在链末的安全说明——进 TARGET_CHAIN 会让
    "目标六步覆盖率"这个指标凭空多一步。"""
    assert "配伍提示" in CHAIN_STEPS
    assert "配伍提示" not in TARGET_CHAIN


def test_note_text_has_a_single_definition():
    """那一句话只在 core/safety_output.py 里定义一次：export_sft、前端「参考医家」
    那一栏用的必须是同一句，各写一句的话界面会替模型背书它没学过的话。"""
    import re
    root = RATIONALE_PWL_PATH.parent.parent.parent
    hits = []
    for rel in ("offline/export_sft.py", "api/main.py", "core/chain.py"):
        f = root / rel
        if not f.exists():
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"【配伍提示】", line.split("#", 1)[0]):
                hits.append(f"{rel}:{lineno}")
    assert hits == [], f"那句话被复制到了别处：{hits}"


def test_export_chain_counts_the_note_steps():
    """配伍提示步数 == 带反药配伍的医案样本数，两头对不上就是有一头漏了。"""
    cases = [_case(case_id="a", has_incompatible_pair=True),
             _case(case_id="b", case_group_id="g2", has_incompatible_pair=False)]
    _, stats = export_chain(cases, {})
    assert stats["incompatible_note_steps"] == 1


# ---------- 划分仍按 case_group_id ----------

def test_split_is_still_by_case_group_id_after_the_new_sources():
    """多诊是一个序列：同一 case_group_id 的复诊不能一半在 train 一半在 heldout。"""
    cases = [_case(case_id=f"c{i}", case_group_id=f"g{i % 3}") for i in range(9)]
    samples, stats = export_chain(cases, {}, heldout_ratio=0.4)
    assert stats["leakage"]["group_overlap"] == []
    assert all(s["meta"]["split_source"] == "case_group_id" for s in samples)


def test_committed_pwl_file_rows_have_the_fields_the_loader_needs():
    rows = [json.loads(ln) for ln in
            RATIONALE_PWL_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows
    for r in rows:
        assert r["o"] and r["source_span"] and r["p"] and r["chapter"] and r["book"]
