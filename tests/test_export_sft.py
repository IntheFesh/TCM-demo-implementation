"""offline/export_sft.py 的离线测试：版权过滤、按字段是否为空决定是否生成对应任务样本。"""
from core.safety_output import INCOMPATIBLE_TRAINING_NOTE
from core.schemas import CaseRecord
from offline.export_sft import filter_public_domain, to_samples


def _case(**overrides) -> CaseRecord:
    base = dict(
        case_id="ye_tianshi-001",
        case_group_id="ye_tianshi-001",
        physician="ye_tianshi",
        raw="朱 初因面肿……",
        symptoms=["面肿", "喘"],
        tongue="舌绛",
        pulse=None,
        syndrome="湿热布散三焦",
        pathogenesis="邪干阳位，气壅不通",
        treatment_principle="清肃上焦",
        formula=None,
        herbs=["飞滑石", "杏仁"],
        copyright_status="public_domain",
    )
    base.update(overrides)
    return CaseRecord(**base)




def test_filter_public_domain_excludes_copyrighted():
    cases = [_case(), _case(case_id="modern-001", copyright_status="copyrighted")]
    kept = filter_public_domain(cases)
    assert [c.case_id for c in kept] == ["ye_tianshi-001"]


# ---------- 总纲 2.5：含十八反配伍的医案不进训练集 ----------


def test_filter_incompatible_pairs_excludes_flagged_cases_and_reports_to_stderr(capsys):
    from offline.export_sft import filter_incompatible_pairs

    cases = [_case(), _case(case_id="like-001", has_incompatible_pair=True)]
    kept = filter_incompatible_pairs(cases)
    assert [c.case_id for c in kept] == ["ye_tianshi-001"]
    err = capsys.readouterr().err
    assert "排除 1 条含十八反十九畏配伍" in err and "like-001" in err


def test_filter_incompatible_pairs_is_silent_when_nothing_to_exclude(capsys):
    from offline.export_sft import filter_incompatible_pairs

    assert len(filter_incompatible_pairs([_case(), _case(case_id="b")])) == 2
    assert capsys.readouterr().err == ""


# ---------- 总纲 5.1（M15）：六层链路格式 ----------


def _triple(case_id, p, o, span, s="x"):
    return {"case_id": case_id, "physician": "ye_tianshi", "s": s, "p": p, "o": o, "source_span": span}


def test_to_chain_sample_builds_steps_from_available_fields_with_rationale_from_source_span():
    from offline.export_sft import to_chain_sample

    case = _case(formula="逍遥散")
    triples = [
        _triple("ye_tianshi-001", "提示", "邪干阳位", "面肿而喘，此邪干阳位"),
        _triple("ye_tianshi-001", "治以", "清肃上焦", "治以清肃上焦"),
        _triple("ye_tianshi-001", "含", "杏仁", "杏仁三钱", s="逍遥散"),
    ]
    sample = to_chain_sample(case, triples)
    assert sample["input"] == "面肿；喘，舌绛"
    steps = {s["step"]: s for s in sample["chain"]}
    assert list(steps) == ["症状→病机", "病机→证型", "证型→治法", "治法→方剂", "方剂→药材"]
    assert steps["症状→病机"]["rationale"] == "面肿而喘，此邪干阳位"
    assert steps["病机→证型"]["rationale"] is None      # 没有「属于」三元组：不编，None
    assert steps["证型→治法"]["rationale"] == "治以清肃上焦"
    assert steps["治法→方剂"]["rationale"] is None
    herbs = steps["方剂→药材"]["output"]
    # 契约变更（R5-1 三源合并）：每味药多了 rationale_source。原来这里断言的是
    # 两个键的字典，现在是三个——因为依据可以来自医案三元组，也可以来自药理层的
    # 《中药学》原文，两者出处不同。少了这个字段，药理层给的依据就会被当成医案的
    # source_span，"每条结论必须引用它所依据的真实医案 id"这条就断了。
    assert herbs == [
        {"name": "飞滑石", "rationale": None, "rationale_source": None},
        {"name": "杏仁", "rationale": "杏仁三钱", "rationale_source": "case:ye_tianshi-001"},
    ]
    assert all(s["source"] == "case:ye_tianshi-001" for s in sample["chain"])
    assert sample["meta"]["case_group_id"] == "ye_tianshi-001"
    assert sample["meta"]["source_kind"] == "case"


def test_to_chain_sample_herb_rationale_falls_back_to_用药_when_no_含():
    from offline.export_sft import to_chain_sample

    case = _case(formula=None)
    sample = to_chain_sample(case, [_triple("ye_tianshi-001", "用药", "杏仁", "喘加杏仁", s="喘")])
    herb_step = sample["chain"][-1]
    assert herb_step["step"] == "治法→药材"
    # 同一处契约变更：多了 rationale_source（理由见上一条测试的注释）。这一条
    # 依据来自医案的「用药」三元组，所以出处是 case:*，不是药理层。
    assert herb_step["output"][1] == {
        "name": "杏仁", "rationale": "喘加杏仁", "rationale_source": "case:ye_tianshi-001"}


def test_to_chain_sample_returns_none_when_fewer_than_two_steps():
    from offline.export_sft import to_chain_sample

    assert to_chain_sample(_case(syndrome=None, pathogenesis=None, treatment_principle=None,
                                 herbs=[]), []) is None
    # 只有证型一步——不成链
    assert to_chain_sample(_case(pathogenesis=None, treatment_principle=None, herbs=[]), []) is None


def test_split_by_case_group_keeps_all_visits_of_a_patient_on_the_same_side():
    from offline.export_sft import split_by_case_group

    cases = [_case(case_id=f"g{i}-{v}", case_group_id=f"g{i}") for i in range(200) for v in (0, 1)]
    split = split_by_case_group(cases, heldout_ratio=0.2)
    assert set(split.values()) == {"train", "heldout"}
    heldout = sum(1 for v in split.values() if v == "heldout")
    assert 20 <= heldout <= 60  # 200 组按 20% 切，允许哈希抖动，但不能一边倒
    assert split == split_by_case_group(list(reversed(cases)), heldout_ratio=0.2)  # 确定性，跟顺序无关


def test_split_by_case_group_rejects_bad_ratio():
    import pytest
    from offline.export_sft import split_by_case_group

    with pytest.raises(ValueError):
        split_by_case_group([_case()], heldout_ratio=1.0)


def test_export_chain_reports_rationale_coverage_with_its_complement():
    from offline.export_sft import export_chain

    cases = [_case(), _case(case_id="ye_tianshi-002", case_group_id="ye_tianshi-002")]
    triples = {"ye_tianshi-001": [_triple("ye_tianshi-001", "治以", "清肃上焦", "治以清肃上焦")]}
    samples, stats = export_chain(cases, triples, heldout_ratio=0.0)
    assert stats["samples"] == 2 and stats["cases_not_chainable"] == 0
    assert stats["split"] == {"train": 2}
    # 每条 4 步（病机/证型/治法/两味药=2）→ 5 个计数单位 ×2 = 10；只有 1 步有依据
    assert stats["steps"] == 10
    assert stats["steps_with_rationale"] == 1
    assert stats["steps_without_rationale"] == 9
    assert all(s["meta"]["split"] == "train" for s in samples)


def test_main_chain_format_writes_samples_and_excludes_incompatible(tmp_path, capsys):
    import json
    from offline import export_sft

    rows = [
        _case().model_dump(),
        _case(case_id="like-001", case_group_id="like-001", has_incompatible_pair=True).model_dump(),
    ]
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "chain.jsonl"

    export_sft.main(["--format", "chain", "--cases-path", str(cases_path),
                     "--triples-path", str(tmp_path / "missing.jsonl"), "--out", str(out)])
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # R18-G **有意的契约变更**：含反药配伍的医案默认不再被排除。
    # 原断言是 == ["ye_tianshi-001"]（like-001 被排掉）。改的理由：按原默认，
    # 李可 57 例里的 21 例和王云启的肿瘤例全部进不来，R18-B/C 两个抽取脚本
    # 贡献的样本数是 0。带上它们的代价由链末那一步「配伍提示」补偿
    # （见下面那条断言），要回到原行为传 --exclude-incompatible（另有一条测试钉它）。
    assert [s["meta"]["case_id"] for s in lines] == ["ye_tianshi-001", "like-001"]
    like = next(s for s in lines if s["meta"]["case_id"] == "like-001")
    assert like["chain"][-1]["step"] == "配伍提示"
    assert like["chain"][-1]["output"] == INCOMPATIBLE_TRAINING_NOTE
    assert like["meta"]["has_incompatible_pair"] is True
    captured = capsys.readouterr()
    assert "rationale 都会是 None" in captured.err
    # 同一处契约变更的另一端：样本数 1 → 2。
    assert "链路样本数：2" in captured.out
    assert "配伍提示步：1 条" in captured.out


def test_main_chain_format_exclude_incompatible_restores_the_old_behaviour(tmp_path, capsys):
    """R18-G 把默认反过来了，原来的行为要仍然拿得到——否则"改默认"就变成了
    "删功能"。这一条钉住 --exclude-incompatible 排掉那一例、也不再有配伍提示步。"""
    import json
    from offline import export_sft

    rows = [
        _case().model_dump(),
        _case(case_id="like-001", case_group_id="like-001", has_incompatible_pair=True).model_dump(),
    ]
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "chain.jsonl"
    export_sft.main(["--format", "chain", "--cases-path", str(cases_path),
                     "--triples-path", str(tmp_path / "missing.jsonl"),
                     "--out", str(out), "--exclude-incompatible"])
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert [s["meta"]["case_id"] for s in lines] == ["ye_tianshi-001"]
    assert "配伍提示步：0 条" in capsys.readouterr().out


def test_to_samples_generates_all_four_tasks_when_fields_present():
    case = _case()
    samples = to_samples(case)
    tasks = {s["meta"]["task"] for s in samples}
    assert tasks == {"T1_辨证", "T2_立法", "T3_处方", "T7_抽取"}


def test_to_samples_skips_t1_when_syndrome_missing():
    case = _case(syndrome=None)
    samples = to_samples(case)
    tasks = {s["meta"]["task"] for s in samples}
    assert "T1_辨证" not in tasks
    # T2/T3 依赖 syndrome，也应一并跳过
    assert "T2_立法" not in tasks
    assert "T3_处方" not in tasks
    # T7 只依赖原文，仍应生成
    assert "T7_抽取" in tasks


def test_to_samples_skips_t3_when_herbs_empty():
    case = _case(herbs=[])
    samples = to_samples(case)
    tasks = {s["meta"]["task"] for s in samples}
    assert "T3_处方" not in tasks
    assert "T1_辨证" in tasks
    assert "T2_立法" in tasks


def test_sample_meta_carries_physician_and_case_id():
    case = _case()
    samples = to_samples(case)
    for s in samples:
        assert s["meta"]["physician_id"] == "ye_tianshi"
        assert s["meta"]["case_id"] == "ye_tianshi-001"
        assert s["meta"]["copyright_status"] == "public_domain"


# ---------- 审查修复：T7 的输入必须是这一诊的原文 ----------

def _visit(case_group_id, visit_index, raw_excerpt=None, raw="整段粗段原文，两个病人共享"):
    from core.schemas import CaseRecord

    return CaseRecord(
        case_id=f"{case_group_id}-{visit_index}", case_group_id=case_group_id,
        physician="ye_tianshi", raw=raw, raw_excerpt=raw_excerpt,
        visit_index=visit_index, symptoms=["纳差"], syndrome="脾虚",
    )


def _t7_inputs(case):
    return [s["input"] for s in to_samples(case) if s["meta"]["task"] == "T7_抽取"]


def test_t7_prefers_the_visit_excerpt_over_the_whole_segment():
    assert _t7_inputs(_visit("ye_tianshi-0001-p0", 0, raw_excerpt="本诊片段")) == ["本诊片段"]


def test_t7_skips_follow_up_visits_without_excerpt():
    """复诊没有本诊片段时不能退回整段 raw：整段是初诊+复诊共享的，会导出
    「同一输入、互相矛盾的输出」的样本。"""
    assert _t7_inputs(_visit("ye_tianshi-0001-p0", 1)) == []


def test_t7_skips_second_patient_without_excerpt():
    """同一粗段里的第二个病人同理：整段 raw 属于两个人。"""
    assert _t7_inputs(_visit("ye_tianshi-0001-p1", 0)) == []


def test_t7_keeps_whole_segment_for_first_patient_initial_visit():
    assert _t7_inputs(_visit("ye_tianshi-0001-p0", 0)) == ["整段粗段原文，两个病人共享"]


# ---------- R5-1：三源合并（SDT / 医案三元组 / 药理层） ----------


def test_step_rejects_a_name_not_in_the_vocabulary():
    """步骤名只有 CHAIN_STEPS 一处定义。拼错箭头方向不会报错、只会让下游按步骤名
    分组的覆盖率统计静默多出一类——那个数就成了假的，所以这里必须硬失败。"""
    import pytest
    from offline.export_sft import _step

    with pytest.raises(ValueError, match="CHAIN_STEPS"):
        _step("证型←治法", "x", None, "case:a", None)


def test_step_rejects_rationale_source_without_rationale():
    import pytest
    from offline.export_sft import _step

    with pytest.raises(ValueError, match="rationale_source"):
        _step("证型→治法", "清肃上焦", None, "case:a", "case:a")


def test_target_chain_is_six_steps_and_a_subset_of_the_vocabulary():
    from offline.export_sft import CHAIN_STEPS, TARGET_CHAIN

    assert len(TARGET_CHAIN) == 6
    assert set(TARGET_CHAIN) <= set(CHAIN_STEPS)


def _mm_row(s, p, o, span, book="中药学"):
    import json
    return json.dumps({"s": s, "p": p, "o": o, "source_span": span,
                       "source": "modern", "book": book}, ensure_ascii=False)


def test_materia_medica_index_prefers_功效_and_ignores_file_order(tmp_path):
    """谓词优先级不能由文件里的行序决定：换一次抽取顺序，训练数据里的 rationale
    就会从功效变成性味，而两次导出都"没报错"。"""
    from offline.export_sft import load_materia_medica_rationales

    a = tmp_path / "a.jsonl"
    a.write_text("\n".join([
        _mm_row("麻黄", "性味", "辛，微苦，温", "麻黄性味辛微苦温"),
        _mm_row("麻黄", "功效", "发汗解表", "麻黄发汗解表，宣肺平喘"),
    ]), encoding="utf-8")
    b = tmp_path / "b.jsonl"
    b.write_text("\n".join([
        _mm_row("麻黄", "功效", "发汗解表", "麻黄发汗解表，宣肺平喘"),
        _mm_row("麻黄", "性味", "辛，微苦，温", "麻黄性味辛微苦温"),
    ]), encoding="utf-8")
    assert load_materia_medica_rationales(a) == load_materia_medica_rationales(b)
    assert load_materia_medica_rationales(a)["麻黄"] == (
        "麻黄发汗解表，宣肺平喘", "materia_medica:中药学")


def test_materia_medica_index_keys_go_through_the_one_herb_normalizer(tmp_path):
    """索引的键过 core.herbs.normalize_herb（query_materia_medica 用的同一个），
    不在这里另写一套前缀剥离——「云苓」和「茯苓」必须落在同一个键上。"""
    from offline.export_sft import load_materia_medica_rationales

    path = tmp_path / "mm.jsonl"
    path.write_text(_mm_row("云苓", "功效", "利水渗湿", "茯苓利水渗湿，健脾安神"), encoding="utf-8")
    assert set(load_materia_medica_rationales(path)) == {"茯苓"}


def test_materia_medica_index_drops_rows_without_source_span(tmp_path):
    from offline.export_sft import load_materia_medica_rationales

    path = tmp_path / "mm.jsonl"
    path.write_text("\n".join([
        _mm_row("麻黄", "功效", "发汗解表", "   "),
        "{ not json",
        _mm_row("桂枝", "功效", "发汗解肌", "桂枝发汗解肌，温通经脉"),
    ]), encoding="utf-8")
    assert set(load_materia_medica_rationales(path)) == {"桂枝"}


def test_reference_index_is_empty_when_the_file_does_not_exist(tmp_path):
    from offline.export_sft import load_formulary_rationales, load_materia_medica_rationales

    assert load_materia_medica_rationales(tmp_path / "nope.jsonl") == {}
    assert load_formulary_rationales(tmp_path / "nope.jsonl") == {}


def test_formulary_index_prefers_功用_over_主治(tmp_path):
    import json

    from offline.export_sft import load_formulary_rationales

    path = tmp_path / "fm.jsonl"
    path.write_text("\n".join(
        json.dumps(r, ensure_ascii=False) for r in [
            {"s": "逍遥散", "p": "主治", "o": "肝郁血虚", "source_span": "主治肝郁血虚脾弱证",
             "source": "modern", "book": "方剂学"},
            {"s": "逍遥散", "p": "功用", "o": "疏肝解郁", "source_span": "功用疏肝解郁，养血健脾",
             "source": "modern", "book": "方剂学"},
        ]), encoding="utf-8")
    assert load_formulary_rationales(path)["逍遥散"] == (
        "功用疏肝解郁，养血健脾", "formulary:方剂学")


def test_herb_rationale_falls_back_to_the_pharmacology_layer_with_its_own_source():
    """医案三元组里没有这味药的依据时退到药理层。**出处要跟着换**——把《中药学》
    的原文记成 case:* 就是把教材说的话按在医案头上。"""
    from offline.export_sft import to_chain_sample

    sample = to_chain_sample(
        _case(), [],
        materia_medica={"杏仁": ("杏仁降气止咳平喘", "materia_medica:中药学")},
    )
    herbs = sample["chain"][-1]["output"]
    assert herbs[1] == {"name": "杏仁", "rationale": "杏仁降气止咳平喘",
                        "rationale_source": "materia_medica:中药学"}
    assert herbs[0]["rationale"] is None      # 飞滑石 两个来源都没有：不编


def test_case_triple_wins_over_the_pharmacology_layer_for_the_same_herb():
    """这一诊的原文比教材通论更贴近这一步的判断，所以医案优先。"""
    from offline.export_sft import to_chain_sample

    sample = to_chain_sample(
        _case(), [_triple("ye_tianshi-001", "用药", "杏仁", "喘加杏仁", s="喘")],
        materia_medica={"杏仁": ("杏仁降气止咳平喘", "materia_medica:中药学")},
    )
    assert sample["chain"][-1]["output"][1]["rationale_source"] == "case:ye_tianshi-001"


def test_formula_rationale_falls_back_to_the_formulary_with_its_own_source():
    from offline.export_sft import to_chain_sample

    sample = to_chain_sample(
        _case(formula="逍遥散"), [],
        formulary={"逍遥散": ("功用疏肝解郁，养血健脾", "formulary:方剂学")},
    )
    step = [s for s in sample["chain"] if s["step"] == "治法→方剂"][0]
    assert step["source"] == "case:ye_tianshi-001"          # 方名是医案里的
    assert step["rationale_source"] == "formulary:方剂学"     # 依据是《方剂学》的


# ---------- R5-1：教材证候表填目标链路的前三步 ----------


_TB_ENTRY = {
    "code": "TB-001", "name": "风寒束表证", "definition": "风寒外束，卫阳被郁，肺气不宣。",
    "location": ["肺"], "nature": ["寒"], "disease": "感冒", "source": "textbook",
}


def _fake_lookup(entry, note=None):
    def lookup(q):
        out = {"found": True, "definition": entry}
        if note:
            out["note"] = note
        return out
    return lookup


def test_textbook_prefix_steps_builds_the_three_head_steps():
    from offline.export_sft import textbook_prefix_steps

    steps, miss = textbook_prefix_steps("风寒束表证", lookup=_fake_lookup(_TB_ENTRY))
    assert miss is None
    assert [s["step"] for s in steps] == ["症状→证素", "证素→病名", "病名→证型"]
    assert steps[0]["output"] == ["肺", "寒"]
    assert steps[1]["output"] == "感冒"
    assert steps[2]["output"] == "风寒束表证"
    assert all(s["source"] == "standard:TB-001" for s in steps)
    assert all(s["rationale"] == _TB_ENTRY["definition"] for s in steps)
    assert all(s["rationale_source"] == "standard:TB-001" for s in steps)


def test_textbook_prefix_steps_refuses_a_partial_name_match():
    """lookup_standard 最后一档是「按名称部分匹配到唯一一条」——「湿热」能匹配上
    「湿热布散三焦证」。那一档给模型自我纠正用是对的，批量造训练数据用是错的：
    一条错配会把另一个病的证素和病名写进样本，而 rationale 是教材原文、看起来
    完全正常。这条测试就是钉住"离线批量导出只接受精确命中"。"""
    from offline.export_sft import textbook_prefix_steps

    steps, miss = textbook_prefix_steps(
        "风寒", lookup=_fake_lookup(_TB_ENTRY, note="按名称部分匹配到唯一一条"))
    assert steps == [] and miss == "syndrome_matched_only_partially"


def test_textbook_prefix_steps_accepts_the_code_plus_name_spelling():
    from offline.export_sft import textbook_prefix_steps

    steps, miss = textbook_prefix_steps(
        "TB-001 风寒束表证", lookup=_fake_lookup(_TB_ENTRY, note="按「编码+名称」的合写形式匹配"))
    assert miss is None and len(steps) == 3


def test_textbook_prefix_steps_reports_each_reason_it_found_nothing():
    from offline.export_sft import textbook_prefix_steps

    assert textbook_prefix_steps(None, lookup=_fake_lookup(_TB_ENTRY))[1] == "no_syndrome"
    assert textbook_prefix_steps("  ", lookup=_fake_lookup(_TB_ENTRY))[1] == "no_syndrome"
    assert textbook_prefix_steps("查不到证", lookup=lambda q: {"found": False})[1] == \
        "syndrome_not_in_standard_table"
    bare = dict(_TB_ENTRY, location=[], nature=[], disease="")
    assert textbook_prefix_steps("风寒束表证", lookup=_fake_lookup(bare))[1] == \
        "standard_entry_has_no_usable_fields"


def test_textbook_element_step_has_no_rationale_when_the_definition_lacks_the_words():
    """证候表里的 location/nature 是从证机概要子串匹配出来的，所以那些词**应该**
    在 definition 里。不在的条目（另一套来源的手工条目）说明这段原文撑不起这一步,
    rationale 填 None，不硬塞一段不含那些词的文本冒充依据。"""
    from offline.export_sft import textbook_prefix_steps

    entry = dict(_TB_ENTRY, definition="外邪犯表，营卫失和。", location=["肺"], nature=["寒"])
    steps, miss = textbook_prefix_steps("风寒束表证", lookup=_fake_lookup(entry))
    assert miss is None
    element_step = steps[0]
    assert element_step["step"] == "症状→证素"
    assert element_step["rationale"] is None and element_step["rationale_source"] is None
    # 病名/证型两步的依据仍是这一条的证机概要原文（那两步不依赖证素词出现在原文里）
    assert steps[1]["rationale"] == "外邪犯表，营卫失和。"


def test_to_chain_sample_reaches_all_six_target_steps_when_all_three_sources_are_present():
    """三源合并的验收点：教材接前三步，医案接治法/方剂，药理层补药材依据——
    目标六步全齐，而且每一步的出处各自不同。

    `materia_medica` 字典的 key 必须是**归一之后**的正名——生产环境
    `load_materia_medica_rationales` 建这张表时就是拿 `normalize_herb` 处理过
    的写法当 key（`offline/export_sft.py::_herb_item` 查表时同样先归一再查），
    这里直接写死原串"飞滑石"曾经在 HERB_ALIASES 还没收这个写法时凑巧能对上，
    R59 把"飞滑石"→"滑石"收进别名表之后原串就查不到了——这是 fixture 没有
    模拟真实建表流程，不是生产代码的 bug，所以用 `normalize_herb()` 现算 key，
    不管别名表以后再扩，这条测试都还是在验证真实的建表方式。"""
    from core.herbs import normalize_herb
    from offline.export_sft import TARGET_CHAIN, to_chain_sample

    case = _case(syndrome="风寒束表证", formula="逍遥散")
    sample = to_chain_sample(
        case,
        [_triple("ye_tianshi-001", "治以", "清肃上焦", "治以清肃上焦")],
        materia_medica={normalize_herb("杏仁"): ("杏仁降气止咳平喘", "materia_medica:中药学"),
                        normalize_herb("飞滑石"): ("滑石利水通淋", "materia_medica:中药学")},
        formulary={"逍遥散": ("功用疏肝解郁，养血健脾", "formulary:方剂学")},
        lookup=_fake_lookup(_TB_ENTRY),
    )
    names = [s["step"] for s in sample["chain"]]
    assert set(TARGET_CHAIN) <= set(names)
    assert {s["rationale_source"] for s in sample["chain"] if s["rationale_source"]} == {
        "standard:TB-001", "case:ye_tianshi-001", "formulary:方剂学"}
    assert {h["rationale_source"] for h in sample["chain"][-1]["output"]} == \
        {"materia_medica:中药学"}
    assert sample["meta"]["standard_prefix_miss"] is None


# ---------- R5-1：SDT Train 作为第二个来源 ----------


def _sdt_record(**overrides):
    """用真实的 eval.sdt.data.SdtRecord，不自己搭一个假壳——SDT 的字段名是从官方
    文件实测出来的（那个模块的文档字符串记了四件实测事实），假壳会跟真实形状漂移。"""
    from eval.sdt.data import SdtRecord

    base = dict(
        record_id="病例30",
        clinical_data="患者胃脘胀痛，嗳气，苔薄白，脉弦。",
        pathogenesis_options={"A": "肝气横逆", "B": "胃中失和", "C": "痰热内扰"},
        syndrome_options={"A": "肝胃不和证", "B": "脾胃湿热证"},
        gold_pathogenesis_answers=["A", "B"],
        gold_syndrome_answers=["A"],
        gold_summary="患者情志不遂，肝气横逆犯胃。胃中失和则胀痛嗳气。故辨为肝胃不和证。",
    )
    base.update(overrides)
    return SdtRecord(**base)


def test_sdt_sample_outputs_option_texts_not_letters():
    """金标准给的是选项字母，训练样本要的是模型该说出来的话。导出字母等于训练
    模型背选项编号——换一份题目就全错。"""
    from offline.export_sft import to_sdt_chain_sample

    sample = to_sdt_chain_sample(_sdt_record())
    steps = {s["step"]: s for s in sample["chain"]}
    assert steps["症状→病机"]["output"] == "肝气横逆；胃中失和"
    assert steps["病机→证型"]["output"] == "肝胃不和证"
    assert sample["input"] == "患者胃脘胀痛，嗳气，苔薄白，脉弦。"
    assert all(s["source"] == "sdt:病例30" for s in sample["chain"])


def test_sdt_rationale_is_a_verbatim_single_sentence_from_the_official_summary():
    """依据必须能在原文里逐字找到。**只取一句**，不把分散的几句拼起来——拼出来的
    字符串在原文里并不存在，"这段字能在原文里找到"这个判据就失效了。"""
    from offline.export_sft import to_sdt_chain_sample

    record = _sdt_record()
    sample = to_sdt_chain_sample(record)
    for step in sample["chain"]:
        assert step["rationale"] in record.gold_summary
        assert step["rationale_source"] == "sdt:病例30"
    steps = {s["step"]: s for s in sample["chain"]}
    assert steps["症状→病机"]["rationale"] == "患者情志不遂，肝气横逆犯胃"
    assert steps["病机→证型"]["rationale"] == "故辨为肝胃不和证"


def test_sdt_rationale_is_none_when_the_summary_never_mentions_the_option():
    from offline.export_sft import to_sdt_chain_sample

    sample = to_sdt_chain_sample(_sdt_record(gold_summary="辨证依据从略。"))
    assert all(s["rationale"] is None and s["rationale_source"] is None for s in sample["chain"])


def test_sdt_sample_physician_is_none_meaning_shared_by_both():
    """SDT 是通用辨证知识，不属于某位医家。按医家过滤时 None 必须当"两家共用"，
    不是"没有医家所以丢掉"——丢掉会让两个 LoRA 都少掉这 200 条专家标注。"""
    from offline.export_sft import to_sdt_chain_sample

    assert to_sdt_chain_sample(_sdt_record())["meta"]["physician_id"] is None


def test_sdt_sample_is_none_when_it_cannot_form_two_steps():
    from offline.export_sft import to_sdt_chain_sample

    assert to_sdt_chain_sample(_sdt_record(gold_pathogenesis_answers=[],
                                           gold_syndrome_answers=[])) is None
    # 只有证型一步：不成链（这时那一步是 症状→证型，不是 病机→证型）
    assert to_sdt_chain_sample(_sdt_record(gold_pathogenesis_answers=[])) is None


def test_sdt_sample_carries_its_own_split_and_says_who_decided_it():
    from offline.export_sft import to_sdt_chain_sample

    meta = to_sdt_chain_sample(_sdt_record())["meta"]
    assert meta["split"] == "train" and meta["split_source"] == "sdt:Train"


def _write_sdt_dir(tmp_path, split, record_ids):
    import json
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    rows = [{
        "Medical Record ID": rid,
        "Clinical Data": "胃脘胀痛。",
        "Options of TCM Pathogenesis": "A:肝气横逆;B:胃中失和",
        "Options of TCM Syndrome": "A:肝胃不和证",
        "Answers of TCM Pathogenesis": "A;B",
        "Answers of TCM Syndrome": "A",
        "Explanatory Summary": "肝气横逆犯胃。",
        "Syndrome Differentiation": "辨为肝胃不和证。",
    } for rid in record_ids]
    (d / f"{split}_TCM_Data_v1.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_load_sdt_chain_samples_reads_only_train(tmp_path):
    """Validation/Test 是评测集，导进训练数据就是泄漏——所以这里没有"换 split"的
    开关。目录里同时摆着 Validation 也只读 Train。"""
    from offline.export_sft import load_sdt_chain_samples

    _write_sdt_dir(tmp_path, "Train", ["训练1", "训练2"])
    _write_sdt_dir(tmp_path, "Validation", ["验证1"])
    samples, stats = load_sdt_chain_samples(tmp_path)
    assert stats["available"] is True and stats["records"] == 2 and stats["samples"] == 2
    assert [s["meta"]["record_id"] for s in samples] == ["训练1", "训练2"]


def test_load_sdt_chain_samples_is_a_no_op_without_the_dir(tmp_path):
    """SDT 数据不随本仓库分发（CC BY 4.0，可独立获取的公开数据不入版本控制），
    沙盒里本来就没有——这时跳过并报出来，不是错误。"""
    from offline.export_sft import load_sdt_chain_samples

    assert load_sdt_chain_samples(None) == ([], {"available": False,
                                                 "note": "未传 --sdt-dir，跳过 SDT 源"})
    samples, stats = load_sdt_chain_samples(tmp_path / "nope")
    assert samples == [] and stats["available"] is False and "不存在" in stats["note"]


# ---------- R5-2：划分与泄漏防护 ----------


def test_export_chain_does_not_resplit_sdt_samples():
    from offline.export_sft import export_chain, to_sdt_chain_sample

    sdt = [to_sdt_chain_sample(_sdt_record())]
    samples, stats = export_chain([_case()], {}, heldout_ratio=0.99, sdt_samples=sdt)
    assert stats["samples_by_source"] == {"case": 1, "sdt": 1}
    by_kind = {s["meta"]["source_kind"]: s["meta"] for s in samples}
    # 医案那条按 0.99 的比例几乎必然落到 heldout；SDT 那条仍是它自带的 train
    assert by_kind["sdt"]["split"] == "train"
    assert by_kind["sdt"]["split_source"] == "sdt:Train"
    assert by_kind["case"]["split_source"] == "case_group_id"


def test_train_and_heldout_case_group_sets_are_disjoint():
    """R5-2 的泄漏判据。用现在的 split_by_case_group 这是结构上保证的（它返回
    case_group_id → 侧的字典），这条测试钉住的是"将来有人改成按 case_id 或按诊次
    切"——那一改，同一病人的初诊和复诊会分到两侧，而复诊跟初诊内容高度重复，
    heldout 上的 gap 就不再说明任何事。"""
    from offline.export_sft import export_chain, heldout_case_groups

    cases = [_case(case_id=f"g{i}-{v}", case_group_id=f"g{i}") for i in range(50) for v in (0, 1)]
    samples, _ = export_chain(cases, {}, heldout_ratio=0.3)
    groups = heldout_case_groups(samples)
    assert groups["train"] and groups["heldout"]              # 两边都非空，否则这条测试是空转
    assert groups["train"] & groups["heldout"] == set()
    # 同一病人的两个诊次必须同侧
    for gid in {s["meta"]["case_group_id"] for s in samples}:
        sides = {s["meta"]["split"] for s in samples if s["meta"]["case_group_id"] == gid}
        assert len(sides) == 1


def test_leakage_report_counts_shared_inputs_against_its_baseline():
    """不同 case_group_id 也可能有逐字相同的 input（同一粗段两个病人、症状列表凑巧
    一样）。这些落在两侧时 gap 对它们没意义，所以要报数——而且旁边带 heldout 总数
    做对照，光报"有 3 条重复"看不出严不严重。**不自动去重**。"""
    from offline.export_sft import export_chain, leakage_report

    # 两组内容逐字相同，只有 case_group_id 不同；比例取 0.5 让两边都有东西
    cases = [_case(case_id=f"g{i}", case_group_id=f"g{i}") for i in range(40)]
    samples, stats = export_chain(cases, {}, heldout_ratio=0.5)
    leak = stats["leakage"]
    assert leak == leakage_report(samples)
    assert leak["group_overlap"] == []
    assert leak["heldout_samples"] > 0
    assert leak["heldout_samples_with_input_also_in_train"] == leak["heldout_samples"]
    assert len(leak["shared_input_examples"]) <= 5


def test_export_chain_reports_rationale_by_source_and_target_step_coverage():
    """"有多少依据是药理层给的"和"总覆盖率"是两个问题。只报后者看不出药理层
    到底接上没有，也看不出目标六步缺哪几步。"""
    from offline.export_sft import TARGET_CHAIN, export_chain

    cases = [_case(syndrome="风寒束表证", formula="逍遥散")]
    samples, stats = export_chain(
        cases, {"ye_tianshi-001": [_triple("ye_tianshi-001", "治以", "清肃上焦", "治以清肃上焦")]},
        heldout_ratio=0.0,
        materia_medica={"杏仁": ("杏仁降气止咳平喘", "materia_medica:中药学")},
        formulary={"逍遥散": ("功用疏肝解郁，养血健脾", "formulary:方剂学")},
        lookup=_fake_lookup(_TB_ENTRY),
    )
    assert stats["rationale_by_source"] == {
        "standard": 3, "case": 1, "formulary": 1, "materia_medica": 1}
    assert set(stats["target_chain_coverage"]) == set(TARGET_CHAIN)
    assert all(v == 1 for v in stats["target_chain_coverage"].values())
    assert stats["standard_prefix"] == {"matched": 1}
    # 覆盖率必须带补集：6 个计数单位（4 个标量步 + 2 味药）里 6 个有依据、0 个没有
    assert stats["steps"] == stats["steps_with_rationale"] + stats["steps_without_rationale"]
    assert samples[0]["meta"]["split"] == "train"


def test_main_chain_format_prints_the_three_source_breakdown(tmp_path, capsys):
    import json

    from offline import export_sft

    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([_case().model_dump()], ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "chain.jsonl"
    export_sft.main(["--format", "chain", "--cases-path", str(cases_path),
                     "--triples-path", str(tmp_path / "missing.jsonl"),
                     "--materia-medica-path", str(tmp_path / "no_mm.jsonl"),
                     "--formulary-path", str(tmp_path / "no_fm.jsonl"),
                     "--out", str(out)])
    captured = capsys.readouterr()
    assert "链路样本数：1 = 医案 1 + SDT 0" in captured.out
    assert "目标六步各自的步数" in captured.out
    assert "教材前三步命中情况" in captured.out
    assert "泄漏检查" in captured.out
    assert "未传 --sdt-dir" in captured.err
    assert "药理层两个文件" in captured.err
