"""R46 §7.5 第 14 条：问诊历史、收藏与模板、统计。"""
import json

from fastapi.testclient import TestClient

import api.main as api_main
from core import history


def test_a_consult_is_recorded_with_its_record_number(tmp_path):
    p = tmp_path / "h.jsonl"
    row = history.record_consult(doctor_id="dr_a", record_id="AB12CD34",
                                 complaint="胃脘胀痛", syndrome="肝胃不和证",
                                 formula="柴胡疏肝散", path=p)
    assert row["record_id"] == "AB12CD34" and row["at"]
    assert history.list_consults(doctor_id="dr_a", path=p)[0]["formula"] == "柴胡疏肝散"


def test_the_prescription_is_stored_but_the_reasoning_trace_is_not(tmp_path):
    """R62 §6.2 改了原来那条"不记方药全文"。

    原来的理由是"完整轨迹在审计链里，按 record_id 回查"。但审计链是按
    record_id 的哈希链，它回答的是"这一份有没有被改过"，没有按患者检索的
    索引；而 §6.2 要求复诊时点 `[载入此方]` 能把上次那张方载回处方表。
    所以**整张方要记**。

    没有改的是另一半：推理过程（五步链、规则引用、验证轮次）仍然不进这里，
    那部分在审计链里——两处各存一份推理过程，才是原来那条注释真正要防的事。
    主诉仍然截到 120 字（它是列表里的一行摘要，不是原文存档）。
    """
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="dr_a", record_id="X", complaint="x" * 500,
                           herb_items=[{"name": "柴胡", "dose": 10.0, "dose_unit": "g"}],
                           path=p)
    row = json.loads(p.read_text(encoding="utf-8").strip())
    assert len(row["complaint"]) <= 120
    assert row["herb_items"][0]["name"] == "柴胡"
    for traced in ("organs", "rule_refs", "reasoning", "verification", "herb_choices"):
        assert traced not in row, f"{traced} 属于推理轨迹，它的存档在审计链里，不该在这里再存一份"


def test_a_visit_can_be_deleted_but_the_original_line_stays_on_disk(tmp_path):
    """§6.2「可导出可删除」。删除写一行墓碑，不改原来那一行——
    这份文件是追加写的，改成可改写的格式要么整份重写（写到一半掉电全没了），
    要么定长记录（中文记录没法定长）。墓碑还让"什么时候删的"查得出来。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="R1", complaint="c",
                           patient_ref="张三", formula="柴胡疏肝散", path=p)
    assert len(history.list_consults(path=p)) == 1
    assert history.delete_consult("R1", path=p) is True
    assert history.list_consults(path=p) == []
    assert "柴胡疏肝散" in p.read_text(encoding="utf-8"), "原始行被抹掉了，删除就不可审计了"


def test_records_group_by_patient_ref_and_count_the_visit_number(tmp_path):
    """§6.2 左栏「既往记录」按备注/编号分组；复诊时标题要显示「第 N 诊」。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="R1", complaint="c1",
                           patient_ref="张三", at="2026-09-01T00:00:00+00:00", path=p)
    history.record_consult(doctor_id="a", record_id="R2", complaint="c2",
                           patient_ref="张三", at="2026-09-12T00:00:00+00:00", path=p)
    history.record_consult(doctor_id="a", record_id="R3", complaint="c3",
                           patient_ref="李四", at="2026-09-05T00:00:00+00:00", path=p)
    groups = {g["patient_ref"]: g for g in history.consults_by_patient(doctor_id="a", path=p)}
    assert groups["张三"]["n"] == 2 and groups["李四"]["n"] == 1
    # 最近的在前，而"第几诊"按时间正序数：最新那条是第 2 诊
    assert groups["张三"]["items"][0]["visit_index"] == 2
    assert history.next_visit_index("张三", doctor_id="a", path=p) == 3


def test_an_unlabelled_visit_never_counts_as_someone_else_s_second_visit(tmp_path):
    """没填备注的记录归在一个空串组里照常看得见，但 `next_visit_index` 对空
    备注恒返回 1——按空串归组会把当天所有没填备注的患者算成同一个人的连续
    复诊，而"第 5 诊"这三个字会直接出现在界面标题上。"""
    p = tmp_path / "h.jsonl"
    for i in range(3):
        history.record_consult(doctor_id="a", record_id=f"R{i}", complaint="c", path=p)
    assert history.consults_by_patient(doctor_id="a", path=p)[0]["n"] == 3
    assert history.next_visit_index("", doctor_id="a", path=p) == 1


def test_history_filters_by_doctor_syndrome_formula_and_date(tmp_path):
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c",
                           syndrome="肝胃不和证", formula="柴胡疏肝散", path=p)
    history.record_consult(doctor_id="b", record_id="2", complaint="c",
                           syndrome="脾虚证", formula="四君子汤", path=p)
    assert len(history.list_consults(doctor_id="a", path=p)) == 1
    assert len(history.list_consults(syndrome="脾虚", path=p)) == 1
    assert len(history.list_consults(formula="四君子", path=p)) == 1
    assert len(history.list_consults(since="2999-01-01", path=p)) == 0


def test_the_newest_consult_comes_first(tmp_path):
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c",
                           at="2020-01-01T00:00:00+00:00", path=p)
    history.record_consult(doctor_id="a", record_id="2", complaint="c",
                           at="2030-01-01T00:00:00+00:00", path=p)
    assert history.list_consults(doctor_id="a", path=p)[0]["record_id"] == "2"


def test_a_half_written_line_does_not_break_the_whole_history(tmp_path):
    """写到一半掉电：跳过那一行，**不让整份历史读不出来**。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c", path=p)
    with p.open("a", encoding="utf-8") as f:
        f.write('{"record_id": "brok\n')
    assert len(history.list_consults(doctor_id="a", path=p)) == 1


def test_favorites_carry_the_doctors_own_note(tmp_path):
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="我的柴胡疏肝散",
                         herbs=["柴胡", "白芍"], note="脾虚者去枳壳", path=p)
    rows = history.list_favorites(doctor_id="a", path=p)
    assert rows[0]["note"] == "脾虚者去枳壳"


def test_favorites_are_per_doctor(tmp_path):
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="甲", herbs=[], path=p)
    history.add_favorite(doctor_id="b", name="乙", herbs=[], path=p)
    assert len(history.list_favorites(doctor_id="a", path=p)) == 1


def test_every_statistic_carries_its_denominator(tmp_path):
    """「肝胃不和证 3 次」在 5 次里和在 50 次里是两件完全不同的事
    （CLAUDE.md：任何数字都必须带对照）。"""
    p = tmp_path / "h.jsonl"
    for i in range(3):
        history.record_consult(doctor_id="a", record_id=str(i), complaint="c",
                               syndrome="肝胃不和证", path=p)
    s = history.stats(doctor_id="a", path=p)
    assert s["n"] == 3
    assert s["syndromes"][0] == {"name": "肝胃不和证", "count": 3, "of": 3}


def test_the_statistics_say_they_are_not_a_performance_metric(tmp_path):
    """**给医师自己反思用的，不是考核指标**——一个会被拿去考核的统计，
    医师会开始为它而开方。"""
    p = tmp_path / "h.jsonl"
    history.record_consult(doctor_id="a", record_id="1", complaint="c", path=p)
    note = history.stats(doctor_id="a", path=p)["note"]
    assert "不是考核" in note and "不排名" in note


def test_statistics_on_an_empty_history_say_so(tmp_path):
    s = history.stats(doctor_id="nobody", path=tmp_path / "h.jsonl")
    assert s["n"] == 0 and "还没有问诊记录" in s["note"]


def test_the_emr_store_returns_the_latest_version(tmp_path):
    p = tmp_path / "e.jsonl"
    history.save_emr("R1", {"v": 1}, path=p)
    history.save_emr("R1", {"v": 2}, path=p)
    assert history.get_emr("R1", path=p)["draft"] == {"v": 2}
    assert len(history.emr_versions("R1", path=p)) == 2


def test_a_missing_emr_is_none_not_an_empty_document(tmp_path):
    assert history.get_emr("NOPE", path=tmp_path / "e.jsonl") is None


def test_the_endpoints_are_reachable():
    client = TestClient(api_main.app)
    assert client.get("/api/history").status_code == 200
    assert client.get("/api/history/favorites").status_code == 200
    assert client.get("/api/history/stats").status_code == 200


def test_a_favorite_without_a_name_is_rejected():
    client = TestClient(api_main.app)
    r = client.post("/api/history/favorite", json={"doctor_id": "a", "name": "  "})
    assert r.status_code == 400


# ---------- R62 §6.5：个人模板 ----------


def test_a_template_keeps_doses_and_processing_not_just_herb_names(tmp_path):
    """§6.5 要的模板是"我惯用的那个加减"，不带剂量的话填进处方表还得再填
    一遍——那就不叫模板。同时 R46 起就有的只有药名的 `herbs` 仍然要在，
    删掉等于为了加字段改掉既有契约。"""
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="我的疏肝方", herbs=[], syndrome="肝胃不和证",
                         herb_items=[{"name": "柴胡", "dose": 10.0, "dose_unit": "g",
                                      "processing": "醋炙", "role": "君"}],
                         doses_count=7, usage="水煎服，每日1剂，分2次温服", path=p)
    row = history.list_favorites(doctor_id="a", path=p)[0]
    assert row["syndrome"] == "肝胃不和证" and row["doses_count"] == 7
    assert row["herb_items"][0]["processing"] == "醋炙"
    assert row["herbs"] == ["柴胡"], "只有药名的那一份要从 herb_items 现算，两者不能分叉"


def test_saving_a_template_twice_under_one_name_leaves_one_entry(tmp_path):
    """医师改了模板会再存一次同名的。列出两份会让「调用模板」下拉里出现
    两个一模一样的名字，而点哪一个结果不同。"""
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="甲", herbs=["柴胡"], path=p)
    history.add_favorite(doctor_id="a", name="甲", herbs=["柴胡", "白芍"], path=p)
    rows = history.list_favorites(doctor_id="a", path=p)
    assert len(rows) == 1 and rows[0]["herbs"] == ["柴胡", "白芍"]


def test_a_template_can_be_deleted_and_the_original_line_stays(tmp_path):
    p = tmp_path / "f.jsonl"
    history.add_favorite(doctor_id="a", name="甲", herbs=["柴胡"], path=p)
    assert history.delete_favorite("甲", doctor_id="a", path=p) is True
    assert history.list_favorites(doctor_id="a", path=p) == []
    assert "柴胡" in p.read_text(encoding="utf-8")


# ---------- R62 §6.6 / §4.3：设置与用药习惯 ----------


def test_preferences_start_at_the_documented_defaults(tmp_path):
    from core import preferences as prefs
    got = prefs.get_preferences(path=tmp_path / "p.jsonl")
    assert got == {**prefs.DEFAULTS, "avoid_herbs": []}


def test_a_misspelled_preference_key_raises_instead_of_being_stored(tmp_path):
    """跟 `core/product_mode.py::require_internal` 同一条纪律。一个静默接受
    `defualt_doses` 的接口会让"我明明设了 14 剂"永远查不出原因。"""
    import pytest as _pytest

    from core import preferences as prefs
    with _pytest.raises(KeyError):
        prefs.set_preferences(path=tmp_path / "p.jsonl", defualt_doses=7)


def test_out_of_range_preference_values_raise(tmp_path):
    import pytest as _pytest

    from core import preferences as prefs
    p = tmp_path / "p.jsonl"
    for kwargs in ({"doses_count": 6}, {"dosage_form": "汤剂"},
                   {"role": "researcher"}, {"font_size": "huge"}):
        with _pytest.raises(ValueError):
            prefs.set_preferences(path=p, **kwargs)


def test_one_bad_value_in_a_batch_writes_nothing(tmp_path):
    """设置面板是一次提交多项的。写一半会让"我改了三项，只生效了两项"
    发生得悄无声息。"""
    import pytest as _pytest

    from core import preferences as prefs
    p = tmp_path / "p.jsonl"
    with _pytest.raises(ValueError):
        prefs.set_preferences(path=p, signature="李某", doses_count=6)
    assert prefs.get_preferences(path=p)["signature"] == ""


def test_avoid_herbs_accepts_the_separator_the_form_actually_uses(tmp_path):
    """界面上这是一个顿号分隔的输入框。切法散成两份之后，前端用顿号、
    后端用逗号，会出现"存进去了但匹配不上"。"""
    from core import preferences as prefs
    p = tmp_path / "p.jsonl"
    got = prefs.set_preferences(path=p, avoid_herbs="附子，细辛、川乌")
    assert got["avoid_herbs"] == ["附子", "细辛", "川乌"]


def test_preferences_become_a_constraint_the_model_can_read(tmp_path):
    """§6.6：忌用药不出现在建议里、常用 12–16 味就不要给 8 味的方。"""
    from core import preferences as prefs
    p = tmp_path / "p.jsonl"
    prefs.set_preferences(path=p, avoid_herbs=["附子"], herb_count_band="16+")
    text = prefs.preferences_prompt_text(prefs.get_preferences(path=p))
    assert "附子" in text and "16+" in text
    assert prefs.preferences_prompt_text({}) == ""


def test_changing_the_dosage_form_carries_the_usage_with_it(tmp_path):
    """饮片是水煎服、颗粒是开水冲服。换了剂型用法还写着"水煎服"，
    那张方拿到药房是错的。但医师手写过的用法不该被冲掉。"""
    from core import preferences as prefs
    p = tmp_path / "p.jsonl"
    assert "冲服" in prefs.set_preferences(path=p, dosage_form="颗粒")["usage"]
    prefs.set_preferences(path=p, usage="我自己写的煎法")
    assert prefs.set_preferences(path=p, dosage_form="膏方")["usage"] == "我自己写的煎法"


def test_none_of_this_writes_into_the_repo_data_dir(tmp_path):
    """全部读写都能接 `path=`。漏一个的话测试会在开发者的 `data/` 里
    留下文件，而那个文件会被下一次测试读到（测试之间不再独立）。"""
    from core import history as h
    from core import preferences as prefs
    before = sorted(x.name for x in h.DATA_DIR.iterdir()) if h.DATA_DIR.exists() else []
    p = tmp_path / "x.jsonl"
    h.record_consult(doctor_id="a", record_id="R", complaint="c", path=p)
    h.add_favorite(doctor_id="a", name="甲", herbs=["柴胡"], path=p)
    h.delete_consult("R", path=p)
    h.delete_favorite("甲", doctor_id="a", path=p)
    prefs.set_preferences(path=p, doses_count=14)
    after = sorted(x.name for x in h.DATA_DIR.iterdir()) if h.DATA_DIR.exists() else []
    assert before == after
