"""R29：OCR 修正表的**作用范围**——覆盖到 `name` / `disease` 两列。

R28 第七节第 5 条把"加病名限定之后仍有 15 组重名"归因为「名字列跟定义列错位」。
逐条打出来之后发现错的不是一类东西，而是四类，而其中两类**本来就该被 OCR 修正表
修掉**：

  - `disease=黄疽` 应为 `黄疸`（形近字；全文「疸」0 次、「疽」109 次）
  - `name=温疤证` 应为 `温疟证`（同上；「疤」153 次全部是「疟」的误识）

`data/standard/ocr_fixes.tsv` 有 20 条、每条都有依据，**但它的表头写明瞄的是
`cardinal_symptoms` / `tongue_pulse`**，`name` / `disease` 两列从来没有一条规则
指向过。于是：**表在、错还在，而且所有测试全绿**——这是这一轮要钉住的形状。

两条新约束：
  1. 第四列 `scope`（`name|disease|symptom|all`）。旧行没有这一列，默认 `all`，
     行为逐字节不变。单字规则（「疽→疸」）只有限定到一列才安全。
  2. 「掉字类」错误（`name=阻心脉证` 少了「瘀」、`disease=逆` 少了「呃」）
     **不进这张表**——那不是形近字，教材原文本身就少了那个字。它们进
     `--report` 的「可疑条目」清单，**不自动改**（改错比不改坏）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from offline.build_syndrome_textbook import (
    LAYOUTS,
    OCR_ALL_BOOKS,
    OCR_FIXES_PATH,
    OCR_SCOPES,
    OcrFix,
    apply_ocr_fixes,
    find_suspicious_entries,
    load_ocr_fixes,
    parse_textbook,
)

ROOT = Path(__file__).resolve().parent.parent
SYNDROMES = ROOT / "data" / "standard" / "syndromes.jsonl"

# 重名组数（(name, disease) 撞在一起的组）的上限。**这个数只能降不能升。**
# R29 之前 30 → R29 重新生成之后 28 → **R31 实测 5**（R31 把扫描件里四种证型
# 标题写法统一成一处判定，33 条条目拿回了自己的名字）。
# 升了说明解析器退化了——不是把这个上限抬高，是去查哪一步把名字弄丢了。
AMBIGUOUS_GROUPS_CEILING = 5


def _write(tmp_path, text: str) -> Path:
    p = tmp_path / "t.md"
    p.write_text(text, encoding="utf-8")
    return p


def _table(tmp_path, rows: str) -> Path:
    p = tmp_path / "f.tsv"
    p.write_text("错\t对\t说明\tscope\tbooks\n" + rows, encoding="utf-8")
    return p


# ---------- scope 列的解析 ----------

def test_the_scope_column_is_parsed(tmp_path):
    fixes = load_ocr_fixes(_table(tmp_path, "黄疽\t黄疸\t形近字\tdisease\n"))
    assert [f.scope for f in fixes] == ["disease"]
    assert fixes[0].wrong == "黄疽" and fixes[0].right == "黄疸"
    assert fixes[0].why == "形近字", "为什么会错这一列要读出来，不然表头的规矩没人核"


def test_a_row_without_a_scope_column_defaults_to_all(tmp_path):
    """旧行只有三列。默认 `all` 是为了**行为逐字节不变**——
    默认成别的值会让已经落盘的 320 条条目重跑出不一样的结果。"""
    fixes = load_ocr_fixes(_table(tmp_path, "便唐\t便溏\t同上\n"))
    assert [f.scope for f in fixes] == ["all"]


def test_every_committed_row_has_a_legal_scope():
    for f in load_ocr_fixes():
        assert f.scope in OCR_SCOPES, f"{f.wrong} 的 scope 是 {f.scope!r}"
        assert f.why.strip(), f"{f.wrong} 没写为什么会错 / 为什么这条安全"


def test_an_unknown_scope_is_rejected_and_the_error_lists_the_legal_ones(tmp_path):
    """写错列名要当场报错，而且要把四个合法值列出来——
    静默当成 `all` 会让一条本该限定在病名列的单字规则全局生效。"""
    with pytest.raises(ValueError) as exc:
        load_ocr_fixes(_table(tmp_path, "疽\t疸\t形近字\ttongue_pulse\n"))
    msg = str(exc.value)
    assert "tongue_pulse" in msg
    for legal in OCR_SCOPES:
        assert legal in msg


# ---------- 旧行的行为逐字节不变 ----------

def test_the_twenty_old_rows_still_fix_exactly_what_they_fixed_before():
    """**这一条是"不许回归"的判据**：20 条旧行逐条过一遍，每条的左列
    仍然被换成右列。scope 列引入之后它们全部落在 `all`，默认那一遍照旧生效。"""
    old = [f for f in load_ocr_fixes() if f.scope == "all"]
    assert len(old) >= 20, f"all 作用域的条目只剩 {len(old)} 条"
    for f in old:
        assert apply_ocr_fixes(f.wrong) == f.right, f"{f.wrong} 不再被修正"


def test_the_default_scope_applies_only_the_all_rows():
    """不传 scope 时**只施加 `all` 行**——这正是旧行为。
    限定到某一列的规则（单字的「疽→疸」）不许在这一遍里生效，否则列限定等于没写。

    用「诸疽」而不是「黄疽」举例：「黄疽」另有一条 `all` 作用域的整词规则
    （症状文本里的「黄疽迅速加深」也要修），拿它举例证不出列限定这件事。
    """
    assert apply_ocr_fixes("痈疽") == "痈疽", "全局那一遍不该动「痈疽」（7 处都是对的）"
    assert apply_ocr_fixes("诸疽") == "诸疽", "病名列的单字规则不该在全局那一遍生效"
    assert apply_ocr_fixes("诸疽", scope="disease") == "诸疸"


# ---------- 列限定真的限定住了 ----------

def test_the_disease_rule_fixes_the_disease_column_only():
    """「疽→疸」是**单字规则**：病名列是 51 个值的闭集合，里面没有一个合法含「疽」；
    正文里「痈疽》」是对的（1 处，已核）。所以它只有限定在 disease 列才安全。"""
    assert apply_ocr_fixes("黄疽", scope="disease") == "黄疸"
    assert apply_ocr_fixes("诸疽", scope="disease") == "诸疸", "单字规则要覆盖没列举的复合词"
    assert apply_ocr_fixes("痈疽", scope="symptom") == "痈疽"
    assert apply_ocr_fixes("痈疽", scope="name") == "痈疽"


def test_the_name_rule_fixes_the_syndrome_name_column_only():
    assert apply_ocr_fixes("温疤", scope="name") == "温疟"
    assert apply_ocr_fixes("温疤", scope="disease") == "温疤"


def test_a_scoped_pass_still_applies_the_all_rows():
    """按列施加时 `all` 行也要生效——判据是「这条规则管不管这一列」，
    不是「这条规则是不是专门为这一列写的」。"""
    assert apply_ocr_fixes("大便唐薄", scope="disease") == "大便溏薄"


def test_applying_the_table_twice_changes_nothing():
    """**幂等**。`str.replace` 一旦有某条规则的右列含着自己的左列
    （「面唇发→面唇发绀」），跑两遍就会变成「面唇发绀绀」。
    这条跟下面那条校验是同一件事的两个方向。"""
    for scope in OCR_SCOPES:
        for f in load_ocr_fixes():
            once = apply_ocr_fixes(f.wrong, scope=scope)
            assert apply_ocr_fixes(once, scope=scope) == once, f"{f.wrong} 在 {scope} 下不幂等"


def test_no_rule_output_contains_another_rules_input(tmp_path):
    """右列里含着某条左列的规则会在多次替换下累加。加载时就要拒绝。"""
    with pytest.raises(ValueError, match="右列"):
        load_ocr_fixes(_table(tmp_path, "面唇发\t面唇发绀\t掉字\tall\n"))


# ---------- 修正仍在切分之前 ----------

def test_the_all_pass_still_runs_before_splitting(tmp_path):
    """R18 那条变异验证守着的规矩：错字落在**标签**上会让整块解析不到，
    切完再修就晚了。这条用一条修标签的规则证明全局那一遍仍在切分之前。"""
    fixes = load_ocr_fixes(_table(tmp_path, "证机概伤\t证机概要\t标签被误识\tall\n"))
    text = ("# 第一节 胃痛\n\n# 1.肝气犯胃\n\n"
            "临床表现：胃脘胀痛，舌淡红，脉弦。\n"
            "证机概伤：肝气犯胃，胃失和降。\n"
            "常用药：柴胡。\n")
    entries, _ = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"], ocr_fixes=fixes)
    assert len(entries) == 1, "标签没被修好，整块会被跳过"
    assert entries[0].definition == "肝气犯胃，胃失和降。"


def test_parse_textbook_fixes_the_disease_column(tmp_path):
    """**这一条是这一轮的验收**：`# 第五节 黄疽` 抽出来的病名是「黄疸」。"""
    text = ("# 第五节 黄疽\n\n# 1.疫毒炽盛\n\n"
            "临床表现：发病急骤，身目俱黄，舌红，脉弦数。\n"
            "证机概要：湿热疫毒炽盛，深入营血。\n"
            "常用药：犀角。\n")
    entries, _ = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert entries[0].disease == "黄疸"


def test_parse_textbook_fixes_the_syndrome_name_column(tmp_path):
    text = ("# 第六节 疟疾\n\n# 2.温疤\n\n"
            "临床表现：热多寒少，舌红，脉弦数。\n"
            "证机概要：阳热素盛，热炽于里。\n"
            "常用药：知母。\n")
    entries, _ = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert entries[0].name == "温疟证"


def test_a_symptom_scoped_rule_does_not_leak_into_the_name(tmp_path):
    """scope=symptom 的规则只动症状/舌脉。左列在证型名里出现时不许被改——
    否则「限定到症状列」这句话就不成立。"""
    fixes = load_ocr_fixes(_table(tmp_path, "胃脘\t胃院\t反向的假规则，只为测范围\tsymptom\n"))
    text = ("# 第一节 胃痛\n\n# 1.胃脘不适\n\n"
            "临床表现：胃脘胀痛，舌淡红，脉弦。\n"
            "证机概要：肝气犯胃。\n"
            "常用药：柴胡。\n")
    entries, _ = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"], ocr_fixes=fixes)
    assert entries[0].name == "胃脘不适证", "证型名被症状列的规则改掉了"
    assert "胃院胀痛" in entries[0].cardinal_symptoms


# ---------- 可疑条目：报出来，不自动改 ----------

def test_a_one_character_disease_is_reported_as_suspicious(tmp_path):
    """`# 第五节 逆` 少了「呃」。**教材原文就缺那个字**，不是切分吃掉的
    （原文第 5968 行写的就是「# 第五节 逆」）——所以只报不改。"""
    text = ("# 第五节 逆\n\n# 1.胃中寒冷\n\n"
            "临床表现：呃声沉而有力，舌淡，脉迟。\n"
            "证机概要：寒蓄中焦，胃气上逆。\n"
            "常用药：丁香。\n")
    entries, stats = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert entries[0].disease == "逆", "不许自动补字"
    reasons = [s.reason for s in stats["suspicious"]]
    assert any("病名" in r for r in reasons), reasons
    assert stats["n_suspicious"] == len(stats["suspicious"])


def test_a_reused_syndrome_name_is_reported_as_suspicious():
    """同一 (name, disease) 出现两次以上 → 名字疑似被上一条复用。
    这是 30 组重名的主导机制：教材 markdown 里有些编号标题掉了行首的 `#`
    （比如 `7.痰火扰心`），`_SYN_HEADING_RE` 认不出来，上一条的名字就一直沿用。"""
    entries = _committed_textbook_entries()
    sus = find_suspicious_entries(entries)
    reused = [s for s in sus if "复用" in s.reason]
    assert reused, "重名一条都没报出来"
    assert all(s.code for s in reused), "每条都要带 code，不然没法去原文查"


def test_the_suspicious_report_carries_the_source_lineno(tmp_path):
    text = ("# 第五节 逆\n\n# 1.胃中寒冷\n\n"
            "临床表现：呃声沉而有力，舌淡，脉迟。\n"
            "证机概要：寒蓄中焦，胃气上逆。\n"
            "常用药：丁香。\n")
    _, stats = parse_textbook(_write(tmp_path, text), LAYOUTS["neike"])
    assert stats["suspicious"][0].lineno == 1, "病名标题在第 1 行"


def test_find_suspicious_entries_works_without_linenos():
    """从已落盘的 jsonl 复查时拿不到原文行号。**不能因此拒绝复查**——
    教材 markdown 不在版本控制里，clone 出来的人只有 jsonl。"""
    sus = find_suspicious_entries(_committed_textbook_entries())
    assert sus, "committed jsonl 里应该还有可疑条目"
    assert all(s.lineno is None for s in sus)


def test_the_short_name_whitelist_keeps_the_legitimate_ones():
    """「闭证」（中风分闭证/脱证）、「实证」「虚证」（厥证分实证/虚证）
    去掉「证」只剩一个字，但教材里确实这么叫。白名单挡住它们，
    否则这份清单会被一堆正常条目冲淡到没人看。"""
    sus = find_suspicious_entries(_committed_textbook_entries())
    short = [s for s in sus if "证型名疑似掉字" in s.reason]
    assert all(s.name not in {"闭证", "实证", "虚证", "脱证"} for s in short), \
        [s.name for s in short]


def test_the_report_subcommand_prints_the_suspicious_list():
    """`--report` 不写文件、只报清单——验收要把它的输出贴进报告。
    不传 `--md-path` 时从已落盘的 jsonl 复查，并**说明行号取不到**。"""
    r = subprocess.run([sys.executable, "-m", "offline.build_syndrome_textbook", "--report"],
                       cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "可疑条目" in r.stdout
    assert "原文行号取不到" in r.stdout
    assert "TB-" in r.stdout


def test_the_report_does_not_rewrite_the_syndromes_file():
    before = SYNDROMES.read_bytes()
    subprocess.run([sys.executable, "-m", "offline.build_syndrome_textbook", "--report"],
                   cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert SYNDROMES.read_bytes() == before, "--report 写了文件"


# ---------- 表头 / 重名组数 ----------

def test_the_table_header_says_which_fields_it_applies_to():
    """SOURCES.md 新增那一条的判据：一张修正表的**作用范围**跟它的内容同样重要。
    表头必须写明它作用于哪些字段，而且这件事要有一条测试盯着。"""
    header = "\n".join(ln for ln in OCR_FIXES_PATH.read_text(encoding="utf-8").splitlines()
                       if ln.lstrip().startswith("#"))
    for field in ("name", "disease", "cardinal_symptoms", "tongue_pulse"):
        assert field in header, f"表头没说它管不管 {field}"
    assert "scope" in header


def test_the_ambiguous_group_count_did_not_go_up():
    """**只能降不能升。** 升了说明解析器退化了。"""
    entries = _committed_textbook_entries()
    groups = Counter((e.name, e.disease) for e in entries)
    dupes = {k: c for k, c in groups.items() if c > 1}
    assert len(dupes) <= AMBIGUOUS_GROUPS_CEILING, \
        f"重名组数 {len(dupes)} 超过上限 {AMBIGUOUS_GROUPS_CEILING}"


def test_the_committed_table_no_longer_has_a_wrong_disease():
    """回归判据：落盘的 jsonl 里不许再出现「黄疽」。"""
    names = {e.disease for e in _committed_textbook_entries()}
    assert "黄疽" not in names
    assert "黄疸" in names
    assert "温疤证" not in {e.name for e in _committed_textbook_entries()}


def _committed_textbook_entries():
    from core.schemas import SyndromeDefinition
    out = []
    for line in SYNDROMES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        if d.get("source") == "textbook":
            out.append(SyndromeDefinition(**d))
    return out


def test_ocr_fix_is_a_named_tuple_so_the_columns_have_names():
    """`(错, 对)` 二元组扩到五列之后，位置解包（`for w, r in fixes`）会静默错位。
    用命名元组：字段名写在代码里，错位当场报错。"""
    f = OcrFix(wrong="a", right="b", why="c", scope="all", books=frozenset({OCR_ALL_BOOKS}))
    assert f.wrong == "a" and f.scope == "all"
    assert tuple(f) == ("a", "b", "c", "all", frozenset({OCR_ALL_BOOKS}))


# ---------- R31：第五列 books ----------

def test_a_row_without_a_books_column_applies_to_every_textbook(tmp_path):
    """旧行只有四列。默认 `*` 是为了**行为逐字节不变**。"""
    fixes = load_ocr_fixes(_table(tmp_path, "便唐\t便溏\t同上\tall\n"))
    assert fixes[0].books == frozenset({OCR_ALL_BOOKS})
    assert fixes[0].applies_to_book("waike") and fixes[0].applies_to_book(None)


def test_the_two_single_character_rules_are_pinned_to_neike():
    """**这一条是 R31 那件事的验收。**

    「疽→疸」和「疤→疟」的安全依据是"《中医内科学》的这一列是闭集合、逐个核过"。
    那个依据对外科教材**不成立**——那本书里「疽」（附骨疽、痈疽）本身就是病名。
    R29 只把这件事写在说明列里，而说明列不拦任何东西。
    """
    single_char = [f for f in load_ocr_fixes() if len(f.wrong) == 1]
    assert {f.wrong for f in single_char} == {"疽", "疤"}, [f.wrong for f in single_char]
    for f in single_char:
        assert f.books == frozenset({"neike"}), f"{f.wrong} 的 books 是 {sorted(f.books)}"


def test_every_whole_word_rule_applies_to_all_textbooks():
    """整词错字在任何中医文本里都是错的，安全性不依赖哪本书——只有单字规则才钉书名。
    这条反过来盯着"别给整词规则也钉上书名"（那会让五本专科教材白白留着错字）。"""
    for f in load_ocr_fixes():
        if len(f.wrong) > 1:
            assert f.books == frozenset({OCR_ALL_BOOKS}), f"{f.wrong} 被钉在 {sorted(f.books)}"


def test_the_neike_only_rule_does_not_fire_on_another_textbook():
    assert apply_ocr_fixes("诸疽", scope="disease", book="neike") == "诸疸"
    assert apply_ocr_fixes("诸疽", scope="disease", book="waike") == "诸疽"
    assert apply_ocr_fixes("痈疽", scope="disease", book="waike") == "痈疽"
    # 整词规则在任何一本下都生效
    assert apply_ocr_fixes("大便唐薄", book="waike") == "大便溏薄"


def test_not_passing_a_book_means_no_book_filtering():
    """`book=None` = 不限教材，所有条目都参与。限定了教材的条目在不传教材的
    调用里**静默失效**比误伤更糟——单测和历史调用方都不传。"""
    assert apply_ocr_fixes("诸疽", scope="disease") == "诸疸"


def test_an_unknown_book_is_rejected_at_load_time(tmp_path):
    with pytest.raises(ValueError, match="认不出的教材"):
        load_ocr_fixes(_table(tmp_path, "疽\t疸\t形近字\tdisease\tzhongyi\n"))


def test_an_unknown_book_is_rejected_at_apply_time():
    with pytest.raises(ValueError, match="LAYOUTS"):
        apply_ocr_fixes("黄疽", scope="disease", book="zhongyi")


def test_parse_textbook_passes_the_layout_key_as_the_book():
    """`parse_textbook` 必须把教材 key 传下去——不传的话 books 这一列等于没写。"""
    src = open("offline/build_syndrome_textbook.py", encoding="utf-8").read()
    body = src[src.index("def parse_textbook("):src.index("\ndef load_committed_textbook_entries")]
    assert body.count("apply_ocr_fixes(") == body.count("book=lay.key") == 4


@pytest.mark.parametrize("key", sorted(LAYOUTS))
def test_every_layout_key_is_a_legal_book_value(key):
    assert apply_ocr_fixes("测试", book=key) == "测试"
