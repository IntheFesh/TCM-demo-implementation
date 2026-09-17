"""R32：药名归一只能有一处实现（CLAUDE.md 第 31 条）。

这个项目已经在"同一概念两处实现"这堵墙上撞过三次（覆盖检查的字面子串、
分歧度的字符串相等、query_graph 与 check_residual 对「口苦」给出相反答案）。
本体层引入了四个新的药名入口（`Ontology.herb` / `herbs_by_effect` /
`is_incompatible` / `dose_limit`），任何一个自己写一套字面匹配，都会重演
第三次那种最难查的情形：每个接口单独测都正常，只有放进同一条推理链才看得出矛盾。

所以这里测的是**一致性**而不是正确性：同一个写法，从哪个入口进去都得到同一个答案。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.herbs import normalize_herb
from core.ontology import Ontology
from core.safety_output import DOSE_LIMITS, INCOMPATIBLE_PAIRS, normalize_for_incompat

ROOT = Path(__file__).resolve().parent.parent

#: 同一味药的几种真实写法。来自医案与教材里实际出现过的形式。
SAME_HERB_SPELLINGS = [
    ("黄芪", "炙黄芪", "生黄芪", "黄芪三钱"),
    ("甘草", "炙甘草", "生甘草", "甘草 6g"),
    ("茯苓", "云苓", "云苓块", "茯苓块"),
    ("陈皮", "广皮", "炒广皮"),
]


def _row(s, p, o):
    return {"s": s, "p": p, "o": o, "book": "中药学", "source": "modern",
            "source_span": f"{s}，{p}：{o}"}


@pytest.mark.parametrize("spellings", SAME_HERB_SPELLINGS,
                         ids=[s[0] for s in SAME_HERB_SPELLINGS])
def test_the_ontology_entry_point_agrees_with_normalize_herb(spellings):
    """本体层查药必须跟 `normalize_herb` 给出同一个答案——它自己不持别名表。"""
    canonical = normalize_herb(spellings[0])
    ont = Ontology(materia_rows=[_row(spellings[0], "功效", "补气升阳")],
                   formulary_rows=[], patterns=[])
    hits = {s: ont.herb(s) for s in spellings}
    assert all(h is not None for h in hits.values()), \
        f"这些写法没能归到同一味药：{[s for s, h in hits.items() if h is None]}"
    assert {h.name for h in hits.values()} == {canonical}


def test_the_incompatible_check_agrees_with_normalize_for_incompat():
    """配伍判断走 `normalize_for_incompat`（先查别名再归一，顺序不能反——
    通用归一会把「天花粉」剥成「天花」）。本体层只是转发。"""
    ont = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    for a, b in INCOMPATIBLE_PAIRS:
        expect = "-".join(sorted((normalize_for_incompat(a), normalize_for_incompat(b))))
        assert ont.is_incompatible(a, b) == expect
        assert ont.is_incompatible(b, a) == expect, "对称性：谁写在前面不该改变结论"


def test_the_dose_limit_agrees_with_dose_limits_for_every_entry():
    """62 味药全过一遍，不抽样：抽样能放过的正是那几味别名归一有坑的
    （「黑顺片」→「黑顺」这类）。"""
    ont = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    bad = [n for n in DOSE_LIMITS if ont.dose_limit(n) != float(DOSE_LIMITS[n][0])]
    assert bad == [], f"这些药从本体层查到的上限跟 DOSE_LIMITS 不一致：{bad}"


def test_a_processed_spelling_still_hits_the_dose_limit():
    """医案里写的是「炙甘草 30g」，安全层的表里存的是「甘草」。中间那一跳
    归一断了，剂量闸门就静默失效——而单测 DOSE_LIMITS 本身看不出来。"""
    ont = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    for raw in ("甘草", "炙甘草", "生甘草"):
        if normalize_for_incompat("甘草") in DOSE_LIMITS:
            assert ont.dose_limit(raw) is not None, f"「{raw}」查不到剂量上限"


def test_the_ontology_module_holds_no_herb_alias_table_of_its_own():
    """判据是"这个判断此前有没有人做过"，不是"我这个实现有没有 bug"。
    所以直接读 AST：本体层里不许出现第二张 {写法: 正名} 的映射。"""
    tree = ast.parse((ROOT / "core" / "ontology.py").read_text(encoding="utf-8"))
    dict_literals = [n for n in ast.walk(tree) if isinstance(n, ast.Dict) and n.keys]
    for d in dict_literals:
        keys = [k.value for k in d.keys if isinstance(k, ast.Constant)
                and isinstance(k.value, str)]
        # 一张别名表的特征：键值都是中文药名。这里允许的只有 stats() 里那种
        # 「谓词 → 计数」的字典，值是数字不是药名。
        values = [v.value for v in d.values if isinstance(v, ast.Constant)
                  and isinstance(v.value, str)]
        if len(keys) >= 3 and len(values) >= 3:
            pytest.fail(f"core/ontology.py 里出现了疑似别名表：{dict(zip(keys, values))}")


def test_the_ontology_module_imports_the_shared_tables_rather_than_redefining_them():
    src = (ROOT / "core" / "ontology.py").read_text(encoding="utf-8")
    assert "from core.herbs import normalize_herb" in src
    assert "from core.safety_output import (" in src
    assert "INCOMPATIBLE_PAIRS," in src, "配伍表要从安全层 import"
    assert "dose_limit_entry," in src, "剂量要走安全层的唯一查法，不自己查表"
    assert "INCOMPATIBLE_PAIRS = " not in src, "不许在本体层里重新定义配伍表"
    assert "DOSE_LIMITS = " not in src, "不许在本体层里重新定义剂量表"


def test_the_focused_knowledge_block_also_goes_through_normalize_herb():
    """知识块选药那一步（`_focused_candidate_herbs`）也要归一，否则医案里写
    「炙黄芪」而本体里存「黄芪」，这味药就永远进不了知识块——模型看不到它的
    性味归经，却在医案里看到了它，正是"知道用什么、不知道为什么"。"""
    from core.context_prefix import build_focused_knowledge
    from core.schemas import CaseRecord, S1Normalize, S2Elements

    ont = Ontology(
        materia_rows=[_row("黄芪", "功效", "补气升阳"), _row("黄芪", "性味", "甘，微温")],
        formulary_rows=[], patterns=[])
    case = CaseRecord(case_id="ye_tianshi-001", case_group_id="ye_tianshi-001",
                      physician="ye_tianshi", raw="原文", visit_index=0,
                      herbs=["炙黄芪三钱"])
    s1 = S1Normalize(symptoms=["乏力"], tongue=None, pulse=None, unmapped=[])
    s2 = S2Elements(elements=[], unexplained_symptoms=[])
    text, stats = build_focused_knowledge(s1, s2, [(case, 0.9)], [], ontology=ont)
    assert stats["n_herbs"] == 1, "「炙黄芪三钱」没归一到「黄芪」，这味药漏出了知识块"
    assert "补气升阳" in text


def test_normalize_herb_is_the_only_entry_point_used_across_the_new_code():
    """R32 新增的两个文件里，药名归一只能通过 `normalize_herb`——
    不许出现另一套 strip/replace 拼出来的归一。"""
    for rel in ("core/ontology.py", "core/effect_synonyms.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for forbidden in (".replace(\"炙\"", ".replace('炙'", ".strip(\"炒\"",
                          ".removeprefix(\"炙\"", ".removeprefix('炙'"):
            assert forbidden not in src, f"{rel} 里自己剥了炮制前缀，该走 normalize_herb"


def test_the_sheng_prefix_is_deliberately_not_stripped():
    """「生」**不是**可以剥的炮制前缀，这条是刻意的，不是遗漏。

    `DOSE_LIMITS` 里同时收录着「生附子 0.0g / 附子 15.0g」「生川乌 0.0g / 川乌 3.0g」
    「生草乌 0.0g / 草乌 3.0g」「生半夏 3.0g / 半夏 9.0g」四对。把「生」当别名剥掉，
    严格的生品限量会被宽松的制品限量盖掉——这是安全层，不是归一的便利问题。

    这条测试存在是因为"补全别名表"看上去总是对的：本轮审计 cases.json 时
    「生石膏」「生牡蛎」这类写法有 230 次，很容易被当成 30 个漏掉的别名补进去。
    """
    from core.herbs import normalize_herb as _n

    pairs = [(k, k[1:]) for k in DOSE_LIMITS if k.startswith("生") and k[1:] in DOSE_LIMITS]
    assert len(pairs) >= 4, "表里应当有若干「生X / X」成对的不同限量"
    for raw, processed in pairs:
        assert _n(raw) != _n(processed), f"「{raw}」不该被归一成「{processed}」"
        assert DOSE_LIMITS[raw][0] <= DOSE_LIMITS[processed][0], \
            f"生品限量不该宽于制品：{raw}={DOSE_LIMITS[raw][0]} vs {processed}={DOSE_LIMITS[processed][0]}"


def test_dose_limit_keeps_the_most_specific_entry_in_the_table():
    """查表顺序错一次的实际后果：「巴豆霜」被归到「巴豆」，0.3g 的限量换成 0.0g；
    「黑顺片」被归到「乌头」，表里那条 15.0g 查不到。**先原名后归一**，
    `normalize_for_incompat`（十八反的类目，比剂量粗）绝不能用来查剂量。"""
    from core.safety_output import dose_limit_entry

    ont = Ontology(materia_rows=[], formulary_rows=[], patterns=[])
    for name in ("巴豆霜", "黑顺片", "白附片", "淡附片", "熟附片", "生半夏"):
        assert name in DOSE_LIMITS, f"{name} 应当是表里的独立键"
        assert ont.dose_limit(name) == float(DOSE_LIMITS[name][0]), \
            f"「{name}」查到的不是它自己那条限量"
        assert dose_limit_entry(name) == DOSE_LIMITS[name]
    assert ont.dose_limit("巴豆霜") != ont.dose_limit("巴豆")


def test_there_is_exactly_one_dose_limit_lookup_implementation():
    """查表顺序此前被抄在三处，本体层那一处抄错了。抽成 `dose_limit_entry`
    之后，`DOSE_LIMITS.get(` 这个写法只应出现在它自己的定义里。"""
    src = (ROOT / "core" / "safety_output.py").read_text(encoding="utf-8")
    assert src.count("DOSE_LIMITS.get(") == 2, \
        "DOSE_LIMITS.get( 只该出现在 dose_limit_entry 里那两次（原名 + 归一）"
    ont_src = (ROOT / "core" / "ontology.py").read_text(encoding="utf-8")
    assert "DOSE_LIMITS.get(" not in ont_src, "本体层不许自己查表，走 dose_limit_entry"
