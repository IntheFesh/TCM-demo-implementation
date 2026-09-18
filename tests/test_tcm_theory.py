"""R51：医理规则层（`data/standard/tcm_theory.jsonl` + `core/theory.py`）。

四类规则各测：schema 形状、`span` 非空、`confidence` 三档之一、
五个查询接口、id 唯一、`applies_to` 生效、复用安全表不另抄十八反十九畏。
"""
import json
from pathlib import Path

import pytest

from core import theory
from offline.extract_tcm_theory import (
    COMPATIBILITIES,
    ORGAN_RELATIONS,
    PATHOMECHANISMS,
    TREATMENT_PRINCIPLES,
    build_rows,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "standard" / "tcm_theory.jsonl"


@pytest.fixture(autouse=True)
def _reset():
    theory.reset_for_tests()
    yield
    theory.reset_for_tests()


# ---------- 数据文件本身 ----------

def test_the_file_lives_in_the_canonical_directory_and_is_tracked():
    """CLAUDE.md：新增 `.jsonl` 一律放 `data/standard/`，且要真的进版本控制
    （这个坑踩过两次）。"""
    import subprocess

    assert DATA_PATH.parent == ROOT / "data" / "standard"
    out = subprocess.run(["git", "check-ignore", str(DATA_PATH)],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode != 0, f"{DATA_PATH} 被 .gitignore 吞了"


def test_every_row_has_the_five_common_fields_and_a_nonempty_span():
    rows = [json.loads(line) for line in DATA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows
    for r in rows:
        for key in ("id", "kind", "source", "span", "confidence", "applies_to"):
            assert key in r, f"{r.get('id')} 少了字段 {key}"
        assert r["span"].strip(), f"{r['id']} 的 span 是空的"
        assert r["source"].strip(), f"{r['id']} 的 source 是空的"


def test_confidence_is_one_of_three_tiers():
    rows = [json.loads(line) for line in DATA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        assert r["confidence"] in ("classic", "derived", "curated"), r["id"]


def test_ids_are_unique():
    rows = [json.loads(line) for line in DATA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))


def test_ids_follow_the_per_kind_prefix():
    rows = [json.loads(line) for line in DATA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    prefix = {"organ_relation": "ZX-", "pathomechanism": "BJ-",
             "treatment_principle": "ZZ-", "compatibility": "PW-"}
    for r in rows:
        assert r["id"].startswith(prefix[r["kind"]]), r["id"]


def test_the_four_kinds_meet_the_minimum_counts():
    """§1.2 的四条底线：藏象 ≥40、病机 ≥50、治则 ≥30、配伍 ≥25。"""
    assert len(ORGAN_RELATIONS) >= 40
    assert len(PATHOMECHANISMS) >= 50
    assert len(TREATMENT_PRINCIPLES) >= 30
    assert len(COMPATIBILITIES) >= 25


def test_compatibility_spans_are_real_substrings_of_the_books():
    """D 类的出处校验在 `build_compatibilities()` 里做（抽不到就崩），
    这里再从数据文件角度钉一遍：写出来的 span 确实能在书里找到。"""
    zhongyao = (ROOT / "books" / "中药学.md").read_text(encoding="utf-8")
    fangji = (ROOT / "books" / "方剂学.md").read_text(encoding="utf-8")
    rows = build_rows()
    compat = [r for r in rows if r["kind"] == "compatibility"]
    for r in compat:
        book = r["source"]
        text = zhongyao if "中药学" in book else fangji
        assert r["span"] in text, f"{r['id']} 的 span 在《{book}》里找不到"


def test_compatibility_does_not_duplicate_the_incompatible_pairs_table():
    """十八反十九畏不复制——只留一条指针型规则，不重复枚举 24 对药名。"""
    from core.safety_output import INCOMPATIBLE_PAIRS

    rows = build_rows()
    compat_definitions = " ".join(r["definition"] for r in rows if r["kind"] == "compatibility")
    # 不该出现"甘草"与"甘遂"同时被列为配伍禁忌药对的字样（那是抄表的症状）
    sample_pair = sorted(next(iter(INCOMPATIBLE_PAIRS)))
    assert f"{sample_pair[0]}反{sample_pair[1]}" not in compat_definitions
    assert f"{sample_pair[0]}与{sample_pair[1]}" not in compat_definitions
    pointer = [r for r in rows if r["kind"] == "compatibility" and "INCOMPATIBLE_PAIRS" in r["definition"]]
    assert pointer, "没有指向 core.safety_output 的指针规则"


# ---------- 五个查询接口 ----------

def test_organ_relations_matches_by_trigger_element():
    hits = theory.organ_relations("肝")
    assert hits
    assert all("肝" in h.trigger_elements for h in hits)


def test_organ_relations_on_unknown_element_is_empty_not_error():
    assert theory.organ_relations("不存在的脏腑") == []


def test_organ_relations_on_empty_string_is_empty():
    assert theory.organ_relations("") == []


def test_transitions_matches_by_subset_of_from_elements():
    hits = theory.transitions(["气滞"])
    assert hits
    for h in hits:
        assert set(h.payload["from"]) <= {"气滞"}


def test_transitions_with_extra_elements_still_matches():
    """给定的证素比规则要求的更多，规则只要求其子集出现就算命中。"""
    hits = theory.transitions(["脾", "气虚", "肝"])
    assert any(set(h.payload["from"]) == {"脾", "气虚"} for h in hits)


def test_transitions_on_empty_input_is_empty():
    assert theory.transitions([]) == []


def test_principles_for_matches_nature_and_location_specific_rules():
    hits = theory.principles_for("湿", "脾")
    names = [h.principle for h in hits]
    assert "健脾化湿" in names


def test_principles_for_falls_back_to_universal_rules():
    """通用治则（虚则补之这类，when_nature 为空）对任意有效证素都可见。"""
    hits = theory.principles_for("气虚", "肺")
    assert any(h.principle == "虚则补之" for h in hits)


def test_principles_for_accepts_a_list_of_natures():
    hits = theory.principles_for(["气虚", "湿"], "脾")
    names = {h.principle for h in hits}
    assert "健脾益气" in names and "健脾化湿" in names


def test_principles_for_on_empty_input_is_empty():
    assert theory.principles_for([], []) == []


def test_compatibility_by_relation():
    hits = theory.compatibility(relation="君药")
    assert len(hits) == 1
    assert hits[0].payload["relation"] == "君药"


def test_compatibility_by_herb_pair():
    hits = theory.compatibility(herbs=["柴胡", "黄芩"])
    assert hits
    assert any(set(p) == {"柴胡", "黄芩"} for r in hits for p in r.payload["example_pairs"])


def test_compatibility_by_herb_pair_order_independent():
    a = theory.compatibility(herbs=["柴胡", "黄芩"])
    b = theory.compatibility(herbs=["黄芩", "柴胡"])
    assert [r.id for r in a] == [r.id for r in b]


def test_compatibility_on_an_uncatalogued_pair_is_empty_not_a_negative_claim():
    """查不到不代表"这两味药不能配"，只代表这版规则表没收录这个例子。"""
    assert theory.compatibility(herbs=["生姜", "大枣"]) == []


def test_rule_looks_up_by_id():
    r = theory.rule("ZX-001")
    assert r is not None and r.kind == "organ_relation"


def test_rule_on_unknown_id_is_none():
    assert theory.rule("ZX-999999") is None


def test_role_construction_rules_covers_all_four_roles():
    rules = theory.role_construction_rules()
    relations = {r.payload["relation"] for r in rules}
    assert {"君药", "臣药", "佐药", "使药"} <= relations


def test_applies_to_is_set_and_meaningful():
    rows = build_rows()
    values = {r["applies_to"] for r in rows}
    assert values == {"脾胃门", "组方通则"}


def test_a_e_type_annotation_getattr_reads_payload_fields():
    r = theory.rule("PW-001")
    assert r.relation == "单行"  # 走 __getattr__ 落到 payload


def test_getattr_raises_on_unknown_field():
    r = theory.rule("ZX-001")
    with pytest.raises(AttributeError):
        _ = r.not_a_real_field


def test_load_theory_is_lazy_and_cached(monkeypatch):
    """惰性加载：模块导入时不该读文件；第一次查询才读，且缓存复用。"""
    theory.reset_for_tests()
    calls = []
    real_open = Path.open

    def spy_open(self, *a, **kw):
        calls.append(self)
        return real_open(self, *a, **kw)

    monkeypatch.setattr(Path, "open", spy_open)
    theory.organ_relations("肝")
    theory.organ_relations("脾")
    reads = [c for c in calls if c == theory.TCM_THEORY_PATH]
    assert len(reads) == 1, "缓存没生效，重复读了文件"
