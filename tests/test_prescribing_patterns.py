"""R35：名医用药规律层的挖掘端（`offline/mine_prescribing_patterns.py`）。

**这个文件里最重要的一组是剂量抽取。** R35 第一版让正则自己猜药名
（`([一-龥]{1,6}?)` + 数量 + 单位），懒惰量词只会吃到最短前缀，抓出来的是
「少」「用」「得」而不是药名——751 张含单位字的方里只抓到 69 张（9%），
而单元测试如果只测「柴胡三钱」这种理想写法会全绿。所以这里的用例全部取自
《临证指南医案》《吴鞠通医案》的真实写法：括号剂量、炮制字夹在中间、
「一两二钱」累加、「钱半」倒装、丸方的两级用量。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.data_paths import CANONICAL_DIR
from core.schemas import PrescribingPattern
from offline.mine_prescribing_patterns import (
    MAX_PLAUSIBLE_DOSE_G,
    MIN_DOSE_SAMPLES,
    MIN_SUPPORT,
    _has_incompatible,
    _parse_dose_at,
    _pattern_id,
    _to_number,
    extract_doses,
    herb_spellings,
    mine,
    mine_dose_patterns,
    mine_herb_patterns,
    mine_modification_patterns,
    mine_pair_patterns,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------- 夹具 ----------

def _case(cid: str, physician: str, herbs: list[str], *, raw: str = "",
          syndrome: str = "", group: str | None = None, visit: int = 1) -> dict:
    return {
        "case_id": cid, "physician": physician, "herbs": herbs, "raw": raw,
        "syndrome": syndrome, "case_group_id": group, "visit_index": visit,
    }


# ---------- 数字与剂量串 ----------

@pytest.mark.parametrize("raw,want", [
    ("3", 3.0), ("12", 12.0), ("1.5", 1.5),
    ("三", 3.0), ("半", 0.5), ("十", 10.0), ("十二", 12.0), ("二十", 20.0),
    ("三十五", 35.0),
])
def test_chinese_and_arabic_numbers_both_parse(raw, want):
    assert _to_number(raw) == want


@pytest.mark.parametrize("raw", ["", "甲", "两", "三剂"])
def test_unparseable_numbers_return_none_instead_of_guessing(raw):
    assert _to_number(raw) is None


@pytest.mark.parametrize("tail,want", [
    ("三钱", 11.1),          # 清代一钱 ≈ 3.7g
    ("一两", 37.0),
    ("八分", 2.96),
    ("12g", 12.0),
    ("6克", 6.0),
    ("一两二钱", 44.4),      # **累加**，不是 37
    ("三钱五分", 12.95),
    ("钱半", 5.55),          # 倒装 = 一钱半
    ("两半", 55.5),
])
def test_dose_strings_from_real_case_text(tail, want):
    assert _parse_dose_at(tail, 0) == pytest.approx(want)


@pytest.mark.parametrize("tail", ["三片", "半酒杯", "三小匙", "服三剂", "", "分二次服"])
def test_non_weight_quantities_are_not_doses(tail):
    """「生姜(三片)」「白蜜(半酒杯)」「姜汁(三小匙)」都是真实写法，
    片/杯/匙不是重量单位，抓成药量就错了。"""
    assert _parse_dose_at(tail, 0) is None


def test_compound_dose_requires_the_two_parts_to_be_adjacent():
    """「二钱 三钱」是两味药各自的量（中间有空白），不能加到同一味头上。"""
    assert _parse_dose_at("二钱 三钱", 0) == pytest.approx(7.4)


# ---------- 剂量锚在已知药名上 ----------

def test_doses_are_anchored_on_the_prescriptions_own_herb_names():
    """R35 的根因回归：原文「少麻黄四钱」，让正则猜药名会猜出「少」，
    归一失败 → 这条剂量丢掉。锚在「麻黄」上才抓得到。"""
    assert extract_doses("少麻黄四钱", {"麻黄": "麻黄"}) == {"麻黄": [14.8]}


def test_a_dose_is_not_credited_to_the_herb_in_front_of_it():
    """「生石膏 防己(三钱)」里三钱是防己的。药名与剂量之间不许夹任意汉字，
    否则生石膏也会记上三钱。"""
    got = extract_doses("生石膏 防己(三钱)", {"生石膏": "生石膏", "防己": "防己"})
    assert got == {"防己": [11.1]}


@pytest.mark.parametrize("raw,want", [
    ("川桂枝尖(生五钱)", 18.5),      # 炮制字夹在括号里
    ("五灵脂(炒一两)", 37.0),
    ("蜣螂虫(炙一两)", 37.0),
    ("茯苓块(五钱,连皮)", 18.5),     # 剂量后面还有小字
    ("小枳实(\n二钱)", 7.4),         # 括号里换行（原文断行）
])
def test_preparation_words_and_punctuation_between_name_and_dose(raw, want):
    herb = raw.split("(")[0].strip()
    got = extract_doses(raw, {herb: herb})
    assert got.get(herb) == [pytest.approx(want)]


def test_the_same_number_is_counted_once_per_normalized_herb():
    """「姜半夏(五钱)」里「半夏」也能对上，归一后是同一味，只能算一次。"""
    assert extract_doses("姜半夏(五钱)", {"姜半夏": "半夏", "半夏": "半夏"}) == {"半夏": [18.5]}


def test_numbers_that_are_not_about_this_prescription_are_ignored():
    """「小温中丸三钱」「服妙应丸六分」里的数字不是这张方任一味药的量。"""
    assert extract_doses("小温中丸三钱。十服。服妙应丸六分。", {"柴胡": "柴胡"}) == {}


def test_doses_above_the_plausible_ceiling_are_dropped():
    assert MAX_PLAUSIBLE_DOSE_G == 500.0
    assert extract_doses("某药二十两", {"某药": "某药"}) == {}      # 740g
    assert extract_doses("灶中黄土(四两)", {"灶中黄土": "灶中黄土"}) == {"灶中黄土": [148.0]}


def test_herb_spellings_keeps_both_the_written_form_and_the_normalized_name():
    sp = herb_spellings(_case("c1", "wu_jutong", ["姜半夏", "云苓块"]))
    assert sp["姜半夏"] == "半夏" and sp["半夏"] == "半夏"
    assert sp["云苓块"] == "茯苓" and sp["茯苓"] == "茯苓"


# ---------- id 与十八反 ----------

def test_pattern_id_is_content_addressed_and_stable():
    """产物进版本控制，id 变了整份文件的 diff 就没法看——所以是内容哈希，
    不是自增序号。"""
    a = _pattern_id("herb", "ye_tianshi", "", ["茯苓"])
    b = _pattern_id("herb", "ye_tianshi", "", ["茯苓"])
    assert a == b and a.startswith("herb-")
    assert _pattern_id("herb", "ye_tianshi", "", ["半夏"]) != a
    assert _pattern_id("herb", "wu_jutong", "", ["茯苓"]) != a
    # 药序无关：sorted 之后哈希
    assert (_pattern_id("herb_pair", "x", "", ["甲", "乙"])
            == _pattern_id("herb_pair", "x", "", ["乙", "甲"]))


def test_incompatible_pairs_come_from_the_safety_table_only():
    """判据整个来自 `core.safety_output.INCOMPATIBLE_PAIRS`，不另写一套。"""
    assert _has_incompatible(["甘草", "甘遂"]) is True
    assert _has_incompatible(["甘草", "茯苓"]) is False
    # 归一也要走 safety 那一处：「炙甘草」属于甘草
    assert _has_incompatible(["炙甘草", "甘遂"]) is True


# ---------- 四类挖掘 ----------

def test_herb_patterns_need_min_support():
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"]) for i in range(3)]
    cases.append(_case("c9", "ye_tianshi", ["柴胡"]))
    out = mine_herb_patterns(("physician", "ye_tianshi", ""), cases, min_support=3)
    names = {p.herbs[0] for p in out}
    assert names == {"茯苓", "半夏"}, "支持数 1 的柴胡不该成为规律"
    assert all(p.support == 3 and len(p.case_ids) == 3 for p in out)


def test_herb_pattern_percentage_denominator_is_the_cases_that_have_a_prescription():
    """分母只数"真的抄了方的诊次"。1075 诊次里 324 条没有 herbs，
    用 len(cases) 当分母会把百分比压低约三分之一——**一个分母说错的百分比
    比没有百分比更坏**，它看起来是可核的。"""
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓"]) for i in range(3)]
    cases += [_case(f"e{i}", "ye_tianshi", []) for i in range(3)]
    out = mine_herb_patterns(("physician", "ye_tianshi", ""), cases, min_support=3)
    assert len(out) == 1
    assert "有方的 3 诊次里出现 3 次（100%" in out[0].note
    assert "这一组共 6 诊次" in out[0].note


def test_pair_patterns_are_exactly_two_herbs():
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏", "陈皮"]) for i in range(3)]
    out = mine_pair_patterns(("physician", "ye_tianshi", ""), cases, min_support=3)
    assert len(out) == 3      # C(3,2)
    assert all(len(p.herbs) == 2 and p.kind == "herb_pair" for p in out)


def test_dose_patterns_need_enough_samples_and_report_median_min_max():
    raw = "茯苓(三钱) 半夏(五钱)"
    cases = [_case(f"c{i}", "wu_jutong", ["茯苓", "半夏"], raw=raw) for i in range(3)]
    cases[0]["raw"] = "茯苓(一两) 半夏(五钱)"
    out = {p.herbs[0]: p for p in mine_dose_patterns(
        ("physician", "wu_jutong", ""), cases, min_support=3)}
    assert MIN_DOSE_SAMPLES == 3
    assert out["茯苓"].dose_median_g == pytest.approx(11.1)
    assert out["茯苓"].dose_min_g == pytest.approx(11.1)
    assert out["茯苓"].dose_max_g == pytest.approx(37.0)
    assert out["半夏"].dose_median_g == pytest.approx(18.5)


def test_dose_patterns_report_min_and_max_so_the_pill_doses_stay_visible():
    """丸方的「姜半夏(十两)」是一料总量而不是一次用量。原文里真有这个数，
    删掉等于篡改语料；中位数不受它影响，而 max 会露出来——**所以 min/max
    必须一起报**，只报中位数就把这个局限藏起来了。"""
    cases = [_case(f"c{i}", "wu_jutong", ["姜半夏"], raw="姜半夏(五钱)") for i in range(3)]
    cases.append(_case("c9", "wu_jutong", ["姜半夏"], raw="姜半夏(十两)"))
    out = mine_dose_patterns(("physician", "wu_jutong", ""), cases, min_support=3)
    assert len(out) == 1
    assert out[0].dose_median_g == pytest.approx(18.5)
    assert out[0].dose_max_g == pytest.approx(370.0)


def test_modification_patterns_read_the_visit_sequence():
    cases = []
    for g in range(3):
        cases.append(_case(f"g{g}-1", "ye_tianshi", ["茯苓", "半夏"],
                           group=f"grp{g}", visit=1))
        cases.append(_case(f"g{g}-2", "ye_tianshi", ["茯苓", "陈皮"],
                           group=f"grp{g}", visit=2))
    out = mine_modification_patterns(cases, min_support=3)
    added = {p.herbs[0] for p in out if "复诊加" in (p.note or "")}
    removed = {p.herbs[0] for p in out if "复诊去" in (p.note or "")}
    assert added == {"陈皮"} and removed == {"半夏"}
    assert all(p.kind == "modification" and p.group_by == "physician" for p in out)


def test_single_visit_groups_produce_no_modification_pattern():
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓"], group=f"grp{i}") for i in range(5)]
    assert mine_modification_patterns(cases, min_support=1) == []


def test_both_grouping_levels_are_produced_and_labelled():
    """按医家与按医家+证型两档都产出：1075 诊次只有 116 条标了证型，
    只按证型分组的话九成医案进不了规律层。"""
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"], syndrome="脾胃气虚")
             for i in range(3)]
    pats, stats = mine(cases, min_support=3)
    levels = {p.group_by for p in pats}
    assert levels == {"physician", "physician_syndrome"}
    assert stats["by_group_by"]["physician"] > 0
    assert stats["by_group_by"]["physician_syndrome"] > 0
    assert all(p.group_value == "" for p in pats if p.group_by == "physician")
    assert all(p.group_value == "脾胃气虚" for p in pats
               if p.group_by == "physician_syndrome")


def test_mining_is_deterministic_byte_for_byte():
    """同一份语料重跑必须得到同一份产物（含顺序）——产物进版本控制，
    顺序不定的话 diff 没法看。"""
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏", "陈皮"],
                   raw="茯苓(三钱) 半夏(五钱)", syndrome="脾胃气虚" if i % 2 else "")
             for i in range(6)]
    a = [p.model_dump_json() for p in mine(cases, min_support=3)[0]]
    b = [p.model_dump_json() for p in mine(cases, min_support=3)[0]]
    assert a == b and len(a) > 3


def test_stats_report_the_denominators_not_just_the_yield():
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"]) for i in range(3)]
    cases.append(_case("e1", "ye_tianshi", []))
    _pats, stats = mine(cases, min_support=3)
    assert stats["n_cases"] == 4 and stats["n_cases_with_herbs"] == 3
    assert stats["min_support"] == 3 and stats["max_support"] == 3
    assert set(stats["by_kind"]) <= {"herb", "herb_pair", "dose", "modification"}


# ---------- CLI 与落盘 ----------

def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    from offline.mine_prescribing_patterns import main

    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"]) for i in range(3)]
    cp = tmp_path / "cases.json"
    cp.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "p.jsonl"
    assert main(["--cases-path", str(cp), "--out", str(out), "--dry-run"]) == 0
    assert not out.exists()
    assert "没有落盘" in capsys.readouterr().out


def test_cli_rejects_a_missing_cases_file_and_a_bad_min_support(tmp_path):
    from offline.mine_prescribing_patterns import main

    assert main(["--cases-path", str(tmp_path / "nope.json")]) == 2
    cp = tmp_path / "cases.json"
    cp.write_text("[]", encoding="utf-8")
    assert main(["--cases-path", str(cp), "--min-support", "0"]) == 2


def test_cli_writes_one_json_object_per_line(tmp_path):
    from offline.mine_prescribing_patterns import main

    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"]) for i in range(3)]
    cp = tmp_path / "cases.json"
    cp.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "p.jsonl"
    assert main(["--cases-path", str(cp), "--out", str(out)]) == 0
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert rows and all(PrescribingPattern(**r) for r in rows)


# ---------- 落在版本控制里的那份产物 ----------

def test_the_generated_file_lives_in_data_standard():
    """`.gitignore` 对 `*.jsonl` 是整体忽略、只对 `data/standard/*.jsonl` 开例外。
    这个坑踩过四次，这里钉住路径。"""
    from offline.mine_prescribing_patterns import DEFAULT_OUT_PATH

    assert DEFAULT_OUT_PATH.parent == CANONICAL_DIR
    assert DEFAULT_OUT_PATH.name == "prescribing_patterns.jsonl"
    rel = DEFAULT_OUT_PATH.relative_to(ROOT).as_posix()
    assert rel == "data/standard/prescribing_patterns.jsonl"


def test_the_committed_file_is_not_gitignored():
    """光"落在正确目录"不够——真去问一次 git，它是不是没被忽略。"""
    from offline.mine_prescribing_patterns import DEFAULT_OUT_PATH

    if not DEFAULT_OUT_PATH.exists():
        pytest.skip("还没生成 prescribing_patterns.jsonl")
    r = subprocess.run([sys.executable and "git", "check-ignore", "-v",
                        DEFAULT_OUT_PATH.relative_to(ROOT).as_posix()],
                       cwd=ROOT, capture_output=True, text=True)
    assert "!data/standard/*.jsonl" in r.stdout or r.stdout.strip() == "", r.stdout


def test_every_committed_row_validates_and_its_support_matches_its_case_ids():
    from offline.mine_prescribing_patterns import DEFAULT_OUT_PATH

    if not DEFAULT_OUT_PATH.exists():
        pytest.skip("还没生成 prescribing_patterns.jsonl")
    rows = [json.loads(x) for x in
            DEFAULT_OUT_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) > 100
    ids = set()
    for r in rows:
        p = PrescribingPattern(**r)
        assert p.support >= MIN_SUPPORT
        assert p.support == len(p.case_ids)
        assert p.pattern_id not in ids, f"{p.pattern_id} 重复"
        ids.add(p.pattern_id)
    kinds = {r["kind"] for r in rows}
    assert kinds == {"herb", "herb_pair", "dose", "modification"}


def test_no_test_in_this_file_writes_to_the_version_controlled_data_dir():
    """这条是 R31/R33 那个坑的复用：测试改了版本控制里的生成物，
    下一次失败会指错方向。所以源码层面禁止在本文件里对 CANONICAL_DIR 落盘。

    **扫描时要把本函数自己的函数体切掉**——否则这个守卫会因为自己提到了
    那些字样而永远失败（R31、R33 各踩过一次）。
    """
    src = Path(__file__).read_text(encoding="utf-8")
    guard = src.index("def test_no_test_in_this_file_writes_to_the_version_controlled")
    body = src[:guard]
    assert "write_text" in body, "夹具里确实用到了 write_text，扫描范围没错"
    for line in body.splitlines():
        if "write_text" in line or "model_dump_json()" in line and "open(" in line:
            assert "tmp_path" in line or "cp." in line or "out." in line or "-> " in line, line


# ---------- 定位外医案 ----------

def test_out_of_scope_cases_are_excluded_by_default_and_counted():
    """李可肿瘤医案 57 例 + 王云启治癌验案 77 例标了 `out_of_scope`。
    本项目的证候表、检索语料、评测主诉全在脾胃门，肿瘤科的用药规律混进知识块
    会让模型照着开出评测覆盖不到的方——跟 `export_sft.filter_out_of_scope`
    同一个取舍，而且**排除了几条要报出来**。"""
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"]) for i in range(3)]
    onc = [_case(f"o{i}", "li_ke", ["附子", "生黄芪"]) for i in range(3)]
    for c in onc:
        c["out_of_scope"] = True
    pats, stats = mine(cases + onc, min_support=3)
    assert stats["n_out_of_scope_flagged"] == 3
    assert stats["out_of_scope_included"] is False
    assert stats["n_cases"] == 3, "分母也要是过滤之后的数"
    assert {p.physician for p in pats} == {"ye_tianshi"}


def test_out_of_scope_can_be_included_on_purpose_and_that_is_recorded():
    cases = [_case(f"c{i}", "ye_tianshi", ["茯苓", "半夏"]) for i in range(3)]
    onc = [_case(f"o{i}", "li_ke", ["附子", "生黄芪"]) for i in range(3)]
    for c in onc:
        c["out_of_scope"] = True
    pats, stats = mine(cases + onc, min_support=3, include_out_of_scope=True)
    assert stats["out_of_scope_included"] is True
    assert stats["n_out_of_scope_flagged"] == 3
    assert {p.physician for p in pats} == {"ye_tianshi", "li_ke"}


def test_the_committed_file_has_no_out_of_scope_physicians_patterns():
    """落盘那份里不能有肿瘤医案的规律。**这条是对产物的断言**，
    不是对函数的断言——默认值写对了但生成时手动加了开关，只有这条查得出来。"""
    from offline.mine_prescribing_patterns import DEFAULT_OUT_PATH

    if not DEFAULT_OUT_PATH.exists():
        pytest.skip("还没生成 prescribing_patterns.jsonl")
    cases = json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))
    flagged = {c["physician"] for c in cases if c.get("out_of_scope")}
    rows = [json.loads(x) for x in
            DEFAULT_OUT_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    got = {r["physician"] for r in rows}
    assert flagged, "cases.json 里应该有被标定位外的医家（李可/王云启）"
    assert not (got & flagged), f"落盘产物里混进了定位外医家：{got & flagged}"
