"""R63 §1：经典方候选与剂量照带。

**这一轮修的是"按钮像摆设"**：点「从经典方开始」，脾胃门的证弹出一串解表剂，
点进去处方表还是空的。所以这里盯的就是这两件事——候选说得出理由、组成带得出
剂量——而不是"端点返回 200"。
"""
from __future__ import annotations

import pytest

from core.classic_formulas import candidates
from core.ontology import Ontology, parse_dose_g


def _ont(formulary_rows):
    """只喂方剂行的本体。本草行给空表：候选筛选一条都不看本草，
    而 `_composition_items` 的碎片过滤要靠"本草层查不到"这个判据——
    空本草层正好把那条判据压到最严，能看出它会不会把真药一起丢掉。"""
    return Ontology(materia_rows=[], formulary_rows=formulary_rows)


def _row(s, p, o, span="见原书"):
    return {"s": s, "p": p, "o": o, "book": "方剂学", "source": "t", "source_span": span}


ROWS = [
    _row("柴胡疏肝散", "组成", "柴胡 12g"),
    _row("柴胡疏肝散", "组成", "白芍 9g"),
    _row("柴胡疏肝散", "组成", "香附"),
    _row("柴胡疏肝散", "主治", "肝气郁滞证"),
    _row("柴胡疏肝散", "功用", "疏肝解郁"),
    _row("麻黄汤", "组成", "麻黄 9g"),
    _row("麻黄汤", "主治", "外感风寒表实证"),
    _row("麻黄汤", "功用", "发汗解表，宣肺平喘"),
    _row("参苓白术散", "组成", "人参 15g"),
    _row("参苓白术散", "主治", "脾虚湿盛证"),
    _row("参苓白术散", "功用", "益气健脾，渗湿止泻"),
]


# ---------- 根因二：候选与当前证型不匹配 ----------

def test_the_candidates_are_the_formulas_whose_indication_names_this_syndrome():
    """**这一条就是"弹出来全是解表剂"那个 bug 的回归**：选肝气郁滞证时
    麻黄汤不该出现在候选里。"""
    got = candidates(syndrome="肝气郁滞证", ontology=_ont(ROWS))
    names = [it["name"] for it in got["items"]]
    assert "柴胡疏肝散" in names
    assert "麻黄汤" not in names, "主治跟这个证无关的方进了候选——正是 R62 的毛病"


def test_a_method_matches_through_the_synonym_table_not_by_substring():
    """治法「疏肝理气」与功用「疏肝解郁」是同一件事的两种说法。裸子串比
    一个字都对不上——判据走 `principle_matches`（治法↔功效同义表），
    跟 R34 的 `effect_matches_method` 同一处实现。"""
    got = candidates(method="疏肝理气", ontology=_ont(ROWS))
    assert [it["name"] for it in got["items"]] == ["柴胡疏肝散"]


def test_every_candidate_says_why_it_is_a_candidate():
    """§1.3 第 3 条：chip 下面要有匹配理由。没有理由的候选跟 R62 的
    方名相似度排序没有区别。"""
    got = candidates(syndrome="脾虚湿盛证", method="益气健脾", ontology=_ont(ROWS))
    assert got["items"], "至少要匹配到参苓白术散"
    for it in got["items"]:
        assert it["reason"].strip()


def test_the_syndrome_beats_the_method_when_both_match_the_same_formula():
    """同一张方两条理由都成立时留最强那条——主治点名这个证，比功用对得上
    治法更直接。"""
    got = candidates(syndrome="肝气郁滞证", method="疏肝理气", ontology=_ont(ROWS))
    hit = next(it for it in got["items"] if it["name"] == "柴胡疏肝散")
    assert "主治" in hit["reason"]


def test_a_named_search_beats_every_inference():
    """用户点名要找哪张方，就不该再被"这张方跟你的证不搭"挡回去。"""
    got = candidates(syndrome="肝气郁滞证", query="麻黄", ontology=_ont(ROWS))
    assert got["items"][0]["name"] == "麻黄汤"


def test_nothing_matched_gives_no_list_at_all_and_says_how_many_were_searched():
    """§1.3 第 3 条最后一句：**一个都匹配不到时不要给不相关的列表**。
    给一串无关的方看起来像系统的建议，而那是没有依据的建议。"""
    got = candidates(syndrome="子虚乌有证", ontology=_ont(ROWS))
    assert got["items"] == []
    assert str(len(ROWS and ["柴胡疏肝散", "麻黄汤", "参苓白术散"])) in got["note"]
    assert "方名" in got["note"], "要告诉使用者还能按方名找"


def test_an_unavailable_ontology_is_not_the_same_as_nothing_matched():
    """数据文件不在 ≠ 查了没有。两者在界面上都是空列表，含义完全不同
    （CLAUDE.md：工具返回空必须能区分三种情况）。"""
    got = candidates(syndrome="肝气郁滞证", ontology=_ont([]))
    assert got["available"] is False


def test_the_candidate_list_is_capped():
    rows = [r for i in range(30) for r in (
        _row(f"某汤{i}", "主治", "肝气郁滞证"), _row(f"某汤{i}", "组成", "柴胡 9g"))]
    got = candidates(syndrome="肝气郁滞证", limit=12, ontology=_ont(rows))
    assert len(got["items"]) == 12


# ---------- 根因一：剂量被有意抹掉 ----------

def test_the_composition_carries_the_dose_and_says_it_came_from_the_book():
    """**R62 把剂量抹掉了**，理由是"怕人误以为那是本次判断"。顾虑对、结论错：
    空剂量的方只省了打药名。正解是带上并标 `dose_is_original`。"""
    got = candidates(syndrome="肝气郁滞证", ontology=_ont(ROWS))
    compo = next(it for it in got["items"] if it["name"] == "柴胡疏肝散")["composition"]
    by_name = {c["name"]: c for c in compo}
    assert by_name["柴胡"]["dose"] == 12.0
    assert by_name["柴胡"]["dose_is_original"] is True


def test_a_herb_without_a_dose_does_not_drag_the_others_down():
    """§1.3 第 1 条第三小点：解析不出剂量的那一味留空，不影响其他味。"""
    got = candidates(syndrome="肝气郁滞证", ontology=_ont(ROWS))
    compo = next(it for it in got["items"] if it["name"] == "柴胡疏肝散")["composition"]
    xiangfu = next(c for c in compo if c["name"] == "香附")
    assert xiangfu["dose"] is None and xiangfu["dose_is_original"] is False
    assert any(c["dose"] == 12.0 for c in compo)


def test_a_classical_unit_keeps_the_original_text_and_leaves_the_number_empty():
    """古制不换算（§1.3 第 1 条第二小点）。本项目的方剂本体出自古籍原文，
    594 条剂量**一条都不是克**——所以这不是边角情况，是常态。"""
    rows = [_row("小柴胡汤", "组成", "柴胡半斤"), _row("小柴胡汤", "主治", "伤寒少阳证")]
    got = candidates(syndrome="伤寒少阳证", ontology=_ont(rows))
    c = got["items"][0]["composition"][0]
    assert c["dose"] is None, "钱两斤升不许换算成克"
    assert c["dose_text"] == "半斤"
    assert c["dose_is_original"] is True, "原文剂量在，标记要在"


def test_dose_fragments_and_processing_words_do_not_become_herbs():
    """方剂本体的组成里混着「各」「去皮」「一升」这类碎片（实测 137 条）。
    它们要是进了处方表，一键导入之后满屏是空行。"""
    rows = [_row("某汤", "主治", "某证"),
            _row("某汤", "组成", "各"), _row("某汤", "组成", "去皮"),
            _row("某汤", "组成", "一升"), _row("某汤", "组成", "党参 9g")]
    got = candidates(syndrome="某证", ontology=_ont(rows))
    names = [c["name"] for c in got["items"][0]["composition"]]
    assert names == ["党参"]


@pytest.mark.parametrize("text,want", [
    ("12g", 12.0), ("9克", 9.0), ("3~9g", 9.0), ("二两", None), ("", None), ("十二枚", None),
])
def test_parse_dose_g_reads_grams_and_refuses_to_convert_classical_units(text, want):
    assert parse_dose_g(text) == want


# ---------- 端点与前端接线 ----------

def test_the_endpoint_takes_all_three_targets_and_a_name_query():
    from fastapi.testclient import TestClient

    import api.main as api_main
    c = TestClient(api_main.app)
    r = c.get("/api/classic_formulas",
              params={"syndrome": "肝气郁滞证", "method": "疏肝理气", "locus": "肝", "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and len(body["items"]) <= 5
    for it in body["items"]:
        assert it["reason"] and "composition" in it


def test_both_pages_call_the_same_endpoint_and_share_one_adopt_path():
    """§1.3 第 5 条：问诊页的「调用模板」与组方实验室的「从经典方开始」
    **同一套逻辑一处实现**。两处各写一份就会一处带原方剂量一处不带，
    而那正是这一轮要修的毛病。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "web" / "product"
    app_js = (root / "app.js").read_text(encoding="utf-8")
    lab_js = (root / "lab.js").read_text(encoding="utf-8")
    assert "/api/classic_formulas" in app_js and "/api/classic_formulas" in lab_js
    # 组成→处方行、「原方」小字都只有 app.js 那一份实现
    assert "function classicToRx" in app_js and "function doseTag" in app_js
    assert "function classicToRx" not in lab_js
    assert "classicToRx, doseTag" in app_js, "共用的两件事要挂到 TCMApp 上"
    assert "window.TCMApp.classicToRx" in lab_js and "window.TCMApp.doseTag" in lab_js


def test_editing_a_dose_drops_the_original_marker_on_both_pages():
    """「原方」的意思是"这个数来自原书"。医师改过之后它就不再成立，
    标记必须消失——否则界面在说一句假话。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "web" / "product"
    for name in ("app.js", "lab.js"):
        src = (root / name).read_text(encoding="utf-8")
        assert "dose_is_original = false" in src, f"{name} 改剂量后没有清掉原方标记"


def test_the_load_confirmation_names_the_formula_and_the_herb_count():
    """§1.3 第 4 条：点完 chip 要有一行反馈，说清载入了哪张方、几味、
    剂量是什么来路。"""
    from pathlib import Path
    lab = (Path(__file__).resolve().parent.parent
           / "web" / "product" / "lab.js").read_text(encoding="utf-8")
    assert "已载入" in lab and "共 ${n} 味" in lab
    assert "原方剂量" in lab and "原书未记剂量" in lab, "两种来路都要说得出"


# ---------- 根因三（这一轮查出来的）：证候下拉把展示名当证型名 ----------

def test_syndrome_nodes_carry_a_canonical_name_next_to_the_display_label():
    """**R62 的组方实验室整条证型链是断的**，而症状不是"候选少几个"：
    174 个证型里 157 个的 label 带「\\n（病名）」后缀（52 个证型重名，
    显示层靠病名区分）。下拉把 label 当证型名传出去，
    `/api/node_explain`、`/api/textbook_formula`、经典方候选三个端点全部
    匹配不到——病位病性恒显「—」、治法不自动带出、候选恒空。

    **跟 SOURCES 第 31 条同一个形状**（展示名当 id 用）：每个端点单独测都对，
    错的是传进去那个字符串，所以单元测试测不出来。这条把规范名钉在载荷里。
    """
    from fastapi.testclient import TestClient

    import api.main as api_main
    c = TestClient(api_main.app)
    nodes = c.get("/api/graph", params={"node_types": "syndrome", "limit": 400}).json()
    rows = [n["data"] for n in nodes["graph"]["nodes"]]
    assert rows, "图里应当有证型节点"
    for d in rows:
        assert d.get("syndrome_name"), f"{d.get('label')!r} 没有规范证型名"
        assert "\n" not in d["syndrome_name"], "规范名里不许有显示用的换行"
    # 至少有一条的 label 跟规范名不同——否则这条测试没在测任何东西
    assert any(d["label"] != d["syndrome_name"] for d in rows)


def test_the_canonical_name_is_not_called_name_in_the_payload():
    """字段刻意不叫 `name`：叫 `name` 的话下一个写渲染的人会拿它当显示名，
    52 个证型重名那个问题就又回来了（`_node_payload` 原本就是为此不下发它）。"""
    from fastapi.testclient import TestClient

    import api.main as api_main
    c = TestClient(api_main.app)
    d = c.get("/api/graph", params={"node_types": "syndrome", "limit": 2}
              ).json()["graph"]["nodes"][0]["data"]
    assert "name" not in d and "syndrome_name" in d


def test_the_lab_dropdown_sends_the_canonical_name_not_the_shown_text():
    from pathlib import Path
    lab = (Path(__file__).resolve().parent.parent
           / "web" / "product" / "lab.js").read_text(encoding="utf-8")
    assert "syndrome_name" in lab, "下拉的 value 要取规范名"
