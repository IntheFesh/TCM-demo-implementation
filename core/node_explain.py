"""R37/R42：图上任一节点的「释义」——**零 LLM 调用**，八节固定顺序，取不到就说取不到。

## 八节，顺序固定

R37 立这个面板时是四节。R42 把图重做成九层单链之后，图上多了病机 / 治则 / 治法
三类节点，而"点开一个节点想知道什么"也随之变了：三甲的主治医师看一个方，
问的不是"这个方是什么"，而是**"这个方的药理凭什么、名老中医怎么用、
系统验过没有、跟指南比怎么样"**。所以加四节，共八节：

| # | 节 | 回答 | 数据来源 |
|---|---|---|---|
| 1 | 是什么 | 这个东西本身的定义/身份 | 证候表 / 本草本体 / 方剂本体 / 证素词表 |
| 2 | 病机 | 它在病机链上处在哪一步 | 证候表的病位+病性；病机节点自身 |
| 3 | 药理 | 结构化药理属性值，**缺哪个谓词如实列出** | 药理层三元组（性味/归经/功效/用量/炮制/禁忌） |
| 4 | 出处原文 | 上面那些值各自出自哪一句 | 三元组的 `source_span` + `book` |
| 5 | 名老中医经验 | 五家在医案里怎么用它 | R35 的 `prescribing_patterns.jsonl` |
| 6 | 验证结果 | 符号验证器的十三条规则里哪几条**能**对它求值 | `core/formula_verifier.py` 的 `RULE_LABELS` |
| 7 | 循证对照 | 它的依据落在哪几部书、对照基准是什么 | `Ontology.source_books()` / 证候表 `source` |
| 8 | 注意 | 剂量上限 / 十八反 / 要单煎先煎 | `core.safety_output`（同一张表） |

**固定顺序不是排版偏好**：它是"先说是什么、再说它在病机链的哪一步、再说药理凭什么、
再说那句凭据的原文、再说别人怎么用、再说系统验过什么、再说跟基准比怎么样、
最后说风险"这条链。顺序一乱，读者会把"名医这么用过"当成"所以可以这么用"。

R37 的「名医怎么用」在 R42 更名为「名老中医经验」——**同一节，不是新增一节**：
产品面上这是一个有分量的词（对标黄煌经方 AI 的「名医经验」条目），而"名医怎么用"
读起来像一句口语。改名只改标题，数据来源和统计口径一个字没动。

## 证型节点多一节：相似证型与鉴别点（R56 §6）

「本例知识地图」要能回答"跟这个证容易混的是哪几个、怎么区分"，这不属于上面
八节里任何一节——不是"这个证是什么"（那是身份），也不是"它在病机链哪一步"
（那是单个证自己的定位），是**跟别的证的关系**。所以只给证型节点单独加了
第九节，加在「病机」之后、「药理」之前：先说清楚这个证自己是什么、在哪一步，
再说它跟谁容易混，然后才进到治法方药那半段。别的节点种类不加这一节——
药材/方剂之间没有这种"鉴别诊断"关系。

## 四条纪律

**一、零 LLM。** 释义全部来自本地数据。让模型现编一段解释是这个项目从头到尾
在防的那件事——一句没有出处的解释在这种场合的代价最大。

**二、取不到就 `available=False`，前端整块隐藏。** 不编一句"暂无更多信息"：
那句话占着位置、看起来像是查过了。八节里某一节空着就只是不返回那一节
（`sections` 里没有它），不是返回一个空壳。

**三、判据全部复用**。药名归一走 `core.herbs.normalize_herb`，剂量上限走
`core.safety_output.dose_limit_entry`，十八反走 `INCOMPATIBLE_PAIRS`，
治法↔功效的等价判定走 `core.effect_synonyms.expand_effect`（跟验证器
`check_effect_matches_method` 同一处实现），证候定义走
`core.tools._load_standard()` 读的那一份表——一个都不另写（CLAUDE.md 第 31 条）。

**四、任何数字都必须带对照。** 「循证对照」这一节存在的理由就是这条铁律：
"这味药有出处"是句空话，"这味药的性味出自《本草备要》，而本项目的本草层
一共五部书 9776 条三元组、覆盖 1458 味药"才是一个可核的说法。
**《中医药循证临床实践指南》全文不在本仓库内**（没有可用的授权文本），
所以这一节明确说出"对照基准是仓库里已有的这几部书，不是那部指南"，
而不是把"教材有这一条"写成"有循证支持"——后者是这一节最容易犯的那个错。
"""
from __future__ import annotations

from typing import Literal

NodeKind = Literal[
    "symptom", "element", "syndrome", "pathogenesis", "principle", "method",
    "formula", "herb", "case", "unknown",
]

#: 节点 id 前缀 → 种类。
#:
#: **三张表必须说同一套词**：问诊图 `api.main.LAYER_PREFIX`、图谱浏览器
#: `offline/build_graph.py` 写进 graph.json 的那套、以及这一张。
#: R42 之前这里漏了持久图那一套（`element::` / `syndrome::` / `symptom::`），
#: 于是在图谱浏览器里点一个证素节点，`parse_node_id` 判成 `unknown`、
#: 面板整块隐藏——而"隐藏"在界面上跟"这个节点没有释义"长得一模一样。
#: 判据见 tests/test_node_explain.py 那条比三张表的用例。
_PREFIX_KIND: dict[str, NodeKind] = {
    # 问诊图（api.main.LAYER_PREFIX，R42 九层）
    "sym": "symptom",
    "organ": "element",        # 病位证素（layer 1）
    "nature": "element",       # 病性证素（layer 2）
    "syn": "syndrome",
    "mech": "pathogenesis",
    "principle": "principle",
    "method": "method",
    "formula": "formula",
    "herb": "herb",
    # 持久知识图谱（offline/build_graph.py）
    "element": "element",
    "syndrome": "syndrome",
    "symptom": "symptom",
    "case": "case",
    # R42 之前问诊图用的合并证素层。**留着**：审计日志与已导出的 SFT 样本里
    # 有这个前缀，去掉之后回看旧记录会变成 unknown。
    "elem": "element",
}

#: 节的顺序与标题。**顺序是链条，不是排版偏好**（见模块文档）。「相似证型与
#: 鉴别点」只有证型节点会产出（R56 §6），但顺序表只有一张，不按节点种类分叉。
SECTION_ORDER = (
    "是什么", "病机", "相似证型与鉴别点", "药理", "出处原文",
    "名老中医经验", "验证结果", "循证对照", "注意",
)

#: 本草层的六个谓词。**缺哪个要列出来**，不是只显示有的那几个——
#: "只显示有的"会让一味缺归经的药看起来跟一味齐全的药一样完整，
#: 而归经缺失恰恰意味着验证器的 `meridian_coverage` 那条规则对它恒不可判。
MATERIA_PREDICATES = ("性味", "归经", "功效", "用量", "炮制", "禁忌")

#: 方剂层的八个谓词，同理。
FORMULARY_PREDICATES = ("组成", "功用", "主治", "君药", "臣药", "佐药", "使药", "加减")

#: 「循证对照」这一节永远要带的那句话。**不是免责话术，是一条口径声明**：
#: 少了它，这一节列出的"五部书 9776 条"会被读成"有循证依据"。
#: R56 §6 第 6 条：产品面只留一句干净的口径声明，不逐段展开"全文不在项目内/
#: 没有可用授权文本/两者不许混着说"这些内部措辞——那段完整解释原来带着两处
#: 没有渲染的 markdown 星号（`**...**` 印成字面星号），读者看到的是"星号乱码
#: + 一堆内部术语"，比没有这句话观感更差。核心信息（教材收录 ≠ 循证等级）
#: 一句话说完，保留在这一句里；R46 原文那段更长的解释挪进模块文档（上面
#: 第四条纪律），给看代码的人看，不再印进响应体。
GUIDELINE_GAP_NOTE = "对照基准是仓库内已收录的典籍与参考表，不是循证等级评定。"


def parse_node_id(node_id: str) -> tuple[NodeKind, str]:
    """`herb::四君子汤::党参` → `("herb", "党参")`。

    **取最后一段作为名字**：问诊图的 id 里带方名（那是为了同一味药在
    不同方里不被去重合并，见 `to_graph` 的注释），而释义要的是那味药本身。
    认不出前缀时返回 `("unknown", 原串)`——不猜，让上层如实报 available=False。
    """
    raw = (node_id or "").strip()
    if not raw:
        return "unknown", ""
    parts = raw.split("::")
    if len(parts) < 2:
        return "unknown", raw
    kind = _PREFIX_KIND.get(parts[0])
    if kind is None:
        return "unknown", raw
    return kind, parts[-1].strip()


def syndrome_row(name: str) -> dict | None:
    """证候表里那一条。**先精确匹配名字，再去掉尾「证」匹配一次**——
    跟 `Ontology.formulas_for_syndrome` 同一条规矩（那里也是这么去尾字的）。"""
    from core.tools import _load_standard

    rows = _load_standard() or []
    if not rows:
        return None
    exact = [r for r in rows if (r.get("name") or "") == name]
    if exact:
        return exact[0]
    stem = name.removesuffix("证")
    loose = [r for r in rows if (r.get("name") or "").removesuffix("证") == stem]
    return loose[0] if loose else None


#: R56 §6 第 5 条：「出处：core/elements.py 词表」这类文件路径不许出现在
#: 产品面。**这张表是唯一的映射实现**——`_section()`/`_evidence_section()`
#: 全部经它转一道，不在 45 处调用点各自决定怎么措辞（CLAUDE.md 第 31 条）。
#: 只收精确匹配；表外的兜底交给 `_display_source()` 的正则那一步。
_SOURCE_LABELS: dict[str, str] = {
    "core/formula_verifier.py 的九条规则": "符号验证规则表",
    "core/formula_verifier.py::check_effect_matches_method": "符号验证规则表·治法与功效匹配",
    "core/safety_output.py 同一张表": "剂量与配伍安全表",
    "data/standard/syndromes.jsonl": "标准证候表",
    "data/standard/syndromes.jsonl 的 source 列": "标准证候表·来源标签",
    "core/elements.py 词表": "证素词表",
    "证候表 + core/elements.py 词表": "标准证候表 + 证素词表",
    "core/elements.py + 证候表": "证素词表 + 标准证候表",
    "data/standard/effect_synonyms.tsv": "治法功效同义词表",
}

#: 兜底正则：万一新加一处 `source=` 忘了进上面那张表，也不能让明显的
#: 路径样式字符串（`core/`、`data/`、`.py`、`.jsonl`、`.tsv`）原样递给用户
#: ——`tests/test_no_demo_artifacts.py` 的禁词表会拿真实节点释义核对这一点。
import re as _re  # noqa: E402 - 只在这个小函数里用，不提到模块顶层

_PATH_LEAK_RE = _re.compile(r"(?:^|[\s（(])(?:core|data)/|\.py\b|\.jsonl\b|\.tsv\b")


def _display_source(raw: str | None) -> str | None:
    """把内部实现措辞的 `source` 翻成医师读得懂的出处标签（R56 §6 第 5 条）。"""
    if not raw:
        return raw
    mapped = _SOURCE_LABELS.get(raw)
    if mapped is not None:
        return mapped
    return "内部数据表" if _PATH_LEAK_RE.search(raw) else raw


def _section(heading: str, lines: list[str], source: str | None = None,
            codes: list[str] | None = None) -> dict | None:
    """一节。**空行全部滤掉；滤完没内容就返回 None**（那一节不出现）。

    `codes`（R56 §6 第 7 条）：证候编码（SP-01/B04.xxx 这类）不进正文
    `lines`，单独一个字段带给前端做 tooltip——正文只显示证候名，编码是
    给想深挖的人看的，不该占正文的视觉权重（用户原话："只显示证候名，
    编码进 tooltip"）。空列表/None 时不带这个键，跟 `source` 同一条纪律。
    """
    # R56 §6 禁词表：`**` 是 markdown 加粗语法，前端只显示纯文本、不渲染
    # markdown，字面星号印出来比没有强调更难读（GUIDELINE_GAP_NOTE 那条
    # 截图实证）。这里统一剥掉，**唯一一处实现**——不在每条拼字符串的地方
    # 各自记得别用 `**`。
    kept = [ln.replace("**", "") for ln in (lines or []) if ln and ln.strip()]
    if not kept:
        return None
    out = {"heading": heading, "lines": kept}
    source = _display_source(source)
    if source:
        out["source"] = source
    if codes:
        out["codes"] = codes
    return out


def _load_standard_rows() -> list[dict]:
    from core.tools import _load_standard

    return _load_standard() or []


def _all_patterns(ont) -> list[dict]:
    """本体里那份规律表。走 `patterns_for("")`——空证名在那个接口里恒命中
    （见它的文档），所以这是"全部规律"的正规取法，不去碰它的私有字段。"""
    return ont.patterns_for("", physician=None)


# ---------- R42：「循证对照」这一节的共同部分 ----------

def _books_line(ont, key: str, label: str) -> str:
    """「本项目的 X 层一共几部书、多少条」。**这一句是那一节的基准**。"""
    books = (ont.source_books() or {}).get(key) or {}
    if not books:
        return ""
    total = sum(books.values())
    detail = "、".join(f"《{b}》{n} 条" for b, n in sorted(books.items(), key=lambda kv: -kv[1]))
    return f"{label}对照基准：{len(books)} 部书 {total} 条三元组（{detail}）"


def _evidence_section(lines: list[str], source: str) -> dict | None:
    """循证对照一节。**口径声明总是跟在数字后面**，不作为可选项。"""
    return _section("循证对照", [*lines, GUIDELINE_GAP_NOTE], source=source)


# ---------- R42：「验证结果」这一节 ----------
#
# 这一节报的是**可验证性**，不是本次处方的验证结论。
#
# 为什么不报结论：结论是 `core/formula_verifier.py` 对**一整张方**求值出来的
# （本体九条规则里有六条要同时看方里所有药），而这个接口拿到的只有一个节点 id，
# 没有本次问诊的上下文。硬要在这里现算一个"这味药的验证结果"就得自己拼一个
# 假的 S3——那是同一个概念的第二处实现，而且算出来的结论跟问诊结果那一份
# 可能不一致（两处实现的典型症状）。
#
# 所以这一节回答的是另一个问题：**这个节点身上的数据够不够让本体那九条规则求值？**
# 这个问题只需要本地数据，答案确定，而且正是"缺哪个谓词"这件事的直接呈现。
# 本次处方的实际结论在问诊响应的 `verification` 段里，这一节末尾指过去。

_VERIFICATION_POINTER = (
    "本次处方的实际验证结论不在这里重复一份（那会是同一个判据的第二处实现），"
    "见问诊结果的「验证」段——同一套符号验证规则。"
)


def _herb_verifiability(herb, norm: str) -> list[str]:
    """本体那九条规则对**这一味药**能不能求值，缺什么就说缺什么；R53 加的
    四条医理一致性规则不按单味药判（跟证型/治法/脏腑相关，不跟某一味药相关），
    所以只报"按什么判"，不重复本体那八条的"可验证/不可验证"判法——
    每条规则的中文名都出自 `RULE_LABELS`（唯一一张表），不在这里另抄一份。"""
    from core.formula_verifier import rule_label
    from core.safety_output import INCOMPATIBLE_PAIRS, dose_limit_entry, normalize_for_incompat

    out: list[str] = []
    key = normalize_for_incompat(norm)
    n_pairs = sum(1 for pair in INCOMPATIBLE_PAIRS if key in pair)
    out.append(f"{rule_label('incompatible_pair')}：可验证"
               f"（十八反十九畏表里与它相关的配伍对 {n_pairs} 组）" if n_pairs
               else f"{rule_label('incompatible_pair')}：可验证（表里没有与它相关的配伍对）")
    hit = dose_limit_entry(norm)
    out.append(f"{rule_label('dose_exceeds')}：可验证（常用量上限 {hit[0]}g）" if hit and hit[0]
               else f"{rule_label('dose_exceeds')}：不可验证——安全表里没有它的剂量上限")
    if herb is None:
        out.append(f"{rule_label('herb_not_in_ontology')}：命中——本草本体里没有这味药")
        for r in ("meridian_coverage", "nature_conflict", "effect_matches_method"):
            out.append(f"{rule_label(r)}：不可验证——本草本体里没有这味药")
    else:
        out.append(f"{rule_label('herb_not_in_ontology')}：不命中（本体里有这味药）")
        # R60：herb_source_fabricated（张冠李戴）跟 herb_source_paraphrased（转述未照抄）
        # 判定"能不能求值"的前提完全一样——都要这味药至少一个谓词有非空出处，
        # 区别只在对不上之后往哪条规则登记，不在"能不能查"这一步，所以两条
        # 共用同一个可验证性判据，不重复算一遍。
        has_source = any(herb.has(p) for p in MATERIA_PREDICATES)
        out.append(f"{rule_label('herb_source_fabricated')}：可验证（本体条目带非空出处）"
                   if has_source
                   else f"{rule_label('herb_source_fabricated')}：不可验证——条目在但每个谓词的出处都是空的")
        out.append(f"{rule_label('herb_source_paraphrased')}：可验证（本体条目带非空出处）"
                   if has_source
                   else f"{rule_label('herb_source_paraphrased')}：不可验证——条目在但每个谓词的出处都是空的")
        out.append(f"{rule_label('meridian_coverage')}：可验证（归经 {'、'.join(sorted(herb.meridians))}）"
                   if herb.meridians
                   else f"{rule_label('meridian_coverage')}：不可验证——缺「归经」谓词")
        out.append(f"{rule_label('nature_conflict')}：可验证（性 {herb.nature}）" if herb.nature
                   else f"{rule_label('nature_conflict')}：不可验证——缺「性味」谓词里的寒热方向")
        out.append(f"{rule_label('effect_matches_method')}：可验证（功效 {len(herb.effects)} 条）"
                   if herb.effects
                   else f"{rule_label('effect_matches_method')}：不可验证——缺「功效」谓词")
    out.append(f"{rule_label('role_structure')}：按整张方判，跟单味药无关")
    # R53：医理一致性四条按脏腑/证型/治法判，不按单味药判——这里只报判据
    # 落在哪，不重复本体那八条"可验证/不可验证"的判法（这四条的数据源是
    # 医理规则层，不是本草本体，"这味药有没有被本体收录"这件事对它们不适用）。
    out.append(f"{rule_label('principle_matches_syndrome')}：按治法与辨出的脏腑判，跟单味药无关")
    out.append(f"{rule_label('method_not_contraindicated')}：按治法与辨出的脏腑判，跟单味药无关")
    out.append(f"{rule_label('pathomechanism_consistent')}：按辨出的多个脏腑判，跟单味药无关")
    out.append(f"{rule_label('role_structure_by_rule')}：按这味药的角色（君/臣/佐/使）"
              "与辨出的脏腑一起判，这里看不到本次问诊的脏腑，见问诊结果的「验证」段")
    out.append(_VERIFICATION_POINTER)
    return out


# ---------- 各类节点的节 ----------

def _herb_sections(name: str, *, ontology=None, patterns_limit: int = 3) -> list[dict]:
    from core.herbs import normalize_herb
    from core.ontology import get_ontology
    from core.safety_output import INCOMPATIBLE_PAIRS, dose_limit_entry, normalize_for_incompat

    ont = ontology if ontology is not None else get_ontology()
    norm = normalize_herb(name) or name
    herb = ont.herb(norm)
    stats = ont.stats()
    out: list[dict | None] = []

    if herb is not None:
        ident = [f"中药「{norm}」，本草本体已收录"]
        if herb.aliases:
            ident.append(f"别名：{'、'.join(herb.aliases)}")
        out.append(_section("是什么", ident, source="本草本体"))
        # R42：性味/归经/功效从「是什么」挪到「药理」，**不是两处各写一份**——
        # 「是什么」现在只回答身份，「药理」回答属性值。两节内容不重叠。
        pharm = []
        if herb.nature or herb.flavor:
            pharm.append(f"性味：{herb.nature or '-'}｜{'、'.join(herb.flavor) or '-'}")
        if herb.meridians:
            pharm.append(f"归经：{'、'.join(sorted(herb.meridians))}")
        if herb.effects:
            pharm.append(f"功效：{'、'.join(herb.effects)}")
        if herb.dose_max_g is not None:
            pharm.append(f"本体记载用量上限：{herb.dose_max_g}g")
        if herb.preparation:
            pharm.append(f"炮制：{'、'.join(herb.preparation)}")
        if herb.contraindications:
            pharm.append(f"禁忌：{'、'.join(herb.contraindications)}")
        missing = [p for p in MATERIA_PREDICATES if not herb.has(p)]
        if missing:
            # **缺哪个谓词必须列出来**：缺归经意味着验证器的归经规则对它恒不可判，
            # 而只显示"有的那几条"会让它看起来跟一味齐全的药一样完整。
            pharm.append(f"本体里缺这几个谓词：{'、'.join(missing)}"
                         f"（全库同类缺口：" + "、".join(
                             f"{p} {stats['missing_predicate_counts'].get(p, 0)}/{stats['n_herbs']} 味"
                             for p in missing) + "）")
        out.append(_section("药理", pharm, source="药理层三元组（本草）"))
        spans = []
        for pred in MATERIA_PREDICATES:
            for ref in (herb.refs.get(pred) or [])[:1]:
                if ref.span.strip():
                    spans.append(f"{pred}（{ref.book or '本草'}）：{ref.span.strip()}")
        out.append(_section("出处原文", spans, source="药理层三元组的 source_span"))
    else:
        # 本体里没有这味药**不是"没有这味药"**，是本体覆盖不全（实测按种数 39.8%）。
        # 这句话必须说出来，否则读者会以为系统认为这味药不存在。
        out.append(_section(
            "是什么",
            [f"「{norm}」不在本项目的本草本体里（本体收 {stats['n_herbs']} 味，"
             "覆盖医案语料里的药名约四成，见 eval/RESULTS.md 的「本体对医案语料的覆盖」一行）。"
             "这不代表这味药不存在，只代表这里查不到它的性味归经。"],
            source="本体覆盖缺口"))
        out.append(_section("药理", [
            f"药理层里没有「{norm}」的任何谓词（应有 {len(MATERIA_PREDICATES)} 个："
            f"{'、'.join(MATERIA_PREDICATES)}）。"],
            source="药理层覆盖缺口"))

    # 名老中医经验（R35 的规律层）
    used = []
    pats = [p for p in _all_patterns(ont) if norm in (p.get("herbs") or [])]
    pats.sort(key=lambda p: -int(p.get("support") or 0))
    for p in pats[:patterns_limit]:
        who = p.get("physician_name") or p.get("physician")
        kind = p.get("kind")
        if kind == "dose" and p.get("dose_median_g") is not None:
            used.append(f"{who}：剂量中位数 {p['dose_median_g']}g"
                        f"（区间 {p.get('dose_min_g')}–{p.get('dose_max_g')}g，"
                        f"{p.get('support')} 张方）")
        elif kind == "herb_pair":
            other = [h for h in (p.get("herbs") or []) if h != norm]
            used.append(f"{who}：常与{'、'.join(other)}同用（{p.get('support')} 张方）")
        else:
            used.append(f"{who}：{p.get('note') or kind}（{p.get('support')} 张方）")
    if used:
        used.append(f"（对照：规律层共 {stats['n_patterns']} 条，"
                    f"其中提到这味药的 {len(pats)} 条——这是本项目医案库的统计，不是教材口径）")
    else:
        # **空着不等于没查。** 一节整块不出现，在界面上跟"这一节我们没做"长得
        # 一模一样；而这里的事实是"查过了、规律层里没有这味药"，两者对读者
        # 完全不同。所以把那句话说出来，并带上基准（规律层一共多少条）。
        used.append(f"R35 的名医用药规律层共 {stats['n_patterns']} 条，"
                    f"没有一条提到「{norm}」——这五位医家的医案里这味药出现得太少，"
                    "达不到规律挖掘的支持度下限（不是「名医不用这味药」）。")
    out.append(_section("名老中医经验", used, source="本项目医案库统计（非教材）"))

    # 验证结果（可验证性）
    out.append(_section("验证结果", _herb_verifiability(herb, norm),
                        source="core/formula_verifier.py 的九条规则"))

    # 循证对照
    ev = [_books_line(ont, "materia_medica", "本草层")]
    if herb is not None:
        got = sorted({r.book for refs in herb.refs.values() for r in refs
                      if r.span.strip() and r.book})
        ev.append(f"这味药的条目落在：{'、'.join(f'《{b}》' for b in got)}"
                  if got else "这味药的条目没有标注书名（source_span 为空）")
    out.append(_evidence_section(ev, source="Ontology.source_books()"))

    # 注意
    notes = []
    hit = dose_limit_entry(norm)
    if hit is not None:
        limit, reason = hit
        notes.append(f"常用量上限 {limit}g（{reason}）" if limit
                     else f"剂量另有规定：{reason}")
    key = normalize_for_incompat(norm)
    partners = sorted({h for pair in INCOMPATIBLE_PAIRS if key in pair
                       for h in pair if h != key})
    if partners:
        notes.append(f"十八反十九畏：不与{'、'.join(partners)}同用")
    if not notes:
        notes.append("本项目的安全表里没有这味药的剂量上限或配伍禁忌条目——"
                     "**这是「查不到」，不是「没有风险」**。")
    out.append(_section("注意", notes, source="core/safety_output.py 同一张表"))
    return [s for s in out if s]


def _differentiation_section(row: dict, rows: list[dict], *, limit: int = 4) -> dict | None:
    """相似证型与鉴别点——「本例知识地图」（R56 §6）问的第一个问题："跟这个证
    容易混的是哪几个、怎么区分"。

    临床鉴别诊断真正比的维度是**同一个病名下**的其他证型（"胃痛"底下到底是
    肝胃不和还是脾胃虚寒），不是随便两个病位病性沾边的证——同病名条目不够
    `limit` 个时才退到共享病位/病性兜底，这样退化路径不会让"同病异证"这个
    临床上最要紧的对比被稀释掉。

    鉴别点是两条主症集合的对称差，不调用 LLM、不新写一套匹配——跟上面
    「病机」用的是同一份证候表、同一组字段，只是换了个角度问。
    """
    name = row.get("name") or ""
    if not name:
        return None
    disease = row.get("disease")
    loc = set(row.get("location") or [])
    nat = set(row.get("nature") or [])
    cand = [r for r in rows if r.get("name") and r.get("name") != name and not r.get("is_category")]
    same_disease = [r for r in cand if disease and r.get("disease") == disease]
    if len(same_disease) >= limit:
        picked = same_disease[:limit]
    else:
        seen = {r.get("code") for r in same_disease}
        overlapping = [r for r in cand if r.get("code") not in seen
                       and ((set(r.get("location") or []) & loc)
                            or (set(r.get("nature") or []) & nat))]
        picked = (same_disease + overlapping)[:limit]
    if not picked:
        return None
    mine = set(row.get("cardinal_symptoms") or [])
    lines = []
    for r in picked:
        theirs = set(r.get("cardinal_symptoms") or [])
        only_mine = sorted(mine - theirs)
        only_theirs = sorted(theirs - mine)
        same_dis = "同病" if r.get("disease") and r.get("disease") == disease else "不同病"
        detail = f"{r.get('name')}（{same_dis}）"
        if only_mine or only_theirs:
            detail += "——鉴别点："
            if only_mine:
                detail += f"本证独有「{'、'.join(only_mine)}」"
            if only_mine and only_theirs:
                detail += "，"
            if only_theirs:
                detail += f"对方独有「{'、'.join(only_theirs)}」"
        else:
            detail += "——主症记载相同，证候表这一层区分不出，需结合病机与舌脉"
        lines.append(detail)
    return _section("相似证型与鉴别点", lines, source="证候表（同病名优先，否则按病位/病性）")


def _syndrome_sections(name: str, *, ontology=None) -> list[dict]:
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    row = syndrome_row(name)
    rows = _load_standard_rows()
    out: list[dict | None] = []
    if row is not None:
        what = [row.get("definition") or ""]
        if row.get("cardinal_symptoms"):
            what.append(f"主症：{'、'.join(row['cardinal_symptoms'])}")
        if row.get("secondary_symptoms"):
            what.append(f"次症：{'、'.join(row['secondary_symptoms'])}")
        if row.get("tongue_pulse"):
            what.append(f"舌脉：{row['tongue_pulse']}")
        out.append(_section("是什么", what, source=f"证候表 {row.get('code') or ''}".strip()))
        # R42 新增：病机一节。**证候表没有独立的「病机」字段**，有的是病位与病性
        # 两个结构化列 + definition 那段话。所以这一节报的是"病位 + 病性 = 这个证
        # 落在病机链的哪一格"，并说清它不是一段被拆好的病机陈述——
        # 从 definition 里切一句话当病机是伪造（同 to_graph 那条注释）。
        loc = row.get("location") or []
        nat = row.get("nature") or []
        mech = []
        if loc or nat:
            mech.append(f"病位：{'、'.join(loc) or '-'}；病性：{'、'.join(nat) or '-'}")
            mech.append("这两列是证候表里结构化的病机要素（病位=病在哪，病性=寒热虚实），"
                        "**不是一段现成的病机陈述**——证候表没有独立的病机字段，"
                        "从上面的定义里切一句话当病机是伪造。")
        out.append(_section("病机", mech, source="证候表的 location / nature 两列"))
        # R56 §6：相似证型与鉴别点——「本例知识地图」的核心一节。同一个病名下
        # 的其他证型是临床鉴别诊断真正比的维度（"胃痛"底下到底是哪个证），
        # 同病名条目不够时退到共享病位/病性；鉴别点是主症集合的对称差，
        # 不是另起一套判断——跟前面「病机」用的是同一份证候表、同一组字段。
        out.append(_differentiation_section(row, rows))
        # 药理：这个证在方剂本体里对得上哪些方
        formulas = [f.name for f in ont.formulas_for_syndrome(name)][:8]
        out.append(_section("药理", [
            f"方剂本体里主治含这个证的方：{'、'.join(formulas)}" if formulas
            else f"方剂本体（{ont.stats()['n_formulas']} 首）里没有主治标到这个证的方——"
                 "这是覆盖缺口，不是「这个证没有对应方」。",
        ], source="方剂本体"))
        # 证候表的 `source` 是**来源标签**（official_consensus / textbook…），
        # 不是可以当引文读的原话——把它当"出处原文"显示出来，读者会以为那就是
        # 教材的原文。所以这一节报的是"这一条来自哪、编码是什么"，措辞照实。
        src = row.get("source") or "未标注"
        icd = row.get("icd11_code")
        # R56 §6 第 7 条：编码（SP-01、ICD-11 的 B04.xxx）不进正文，走
        # `codes=` 单独带给前端做 tooltip——正文只留来源标签这句话。
        code_list = [c for c in (row.get("code"), icd) if c]
        out.append(_section("出处原文", [
            f"来源标签：{src}",
            "（这一条是人工整理的证候参考表，不是逐字引文；教材原文见 books/ 下的源书）",
        ], source="data/standard/syndromes.jsonl", codes=code_list))
    else:
        out.append(_section("是什么", [
            f"「{name}」不在 data/standard/syndromes.jsonl 里。证候表现在收 "
            f"{len(rows)} 条，教材扩充还在进行（总纲阶段三）。"],
            source="证候表覆盖缺口"))
    # 名老中医经验（证型档规律）
    used = []
    for p in ont.patterns_for(name, physician=None):
        if p.get("group_by") != "physician_syndrome":
            continue
        who = p.get("physician_name") or p.get("physician")
        used.append(f"{who}：{'、'.join(p.get('herbs') or [])}"
                    f"（{p.get('kind')}，{p.get('support')} 张方）")
        if len(used) >= 4:
            break
    out.append(_section("名老中医经验", used, source="本项目医案库统计（非教材）"))
    # 验证结果：这个证能支撑哪两条规则
    if row is not None:
        from core.formula_verifier import rule_label

        loc = row.get("location") or []
        nat = row.get("nature") or []
        hot_cold = [n for n in nat if n in ("寒", "热", "凉", "温", "火")]
        out.append(_section("验证结果", [
            f"{rule_label('meridian_coverage')}：可验证（病位 {'、'.join(loc)}，"
            "验证器据此检查方里有没有药归到这些经）" if loc
            else f"{rule_label('meridian_coverage')}：不可验证——证候表这一条没有病位",
            f"{rule_label('nature_conflict')}：可验证（寒热方向 {'、'.join(hot_cold)}）" if hot_cold
            else f"{rule_label('nature_conflict')}：不可验证——"
                 f"证候表这一条的病性（{'、'.join(nat) or '空'}）里没有寒热方向",
            _VERIFICATION_POINTER,
        ], source="core/formula_verifier.py 的九条规则"))
    # 循证对照
    by_source: dict[str, int] = {}
    for r in rows:
        k = r.get("source") or "未标注"
        by_source[k] = by_source.get(k, 0) + 1
    n_icd = sum(1 for r in rows if r.get("icd11_code"))
    ev = [
        f"证候表对照基准：{len(rows)} 条，来源标签分布 "
        + "、".join(f"{k} {v} 条" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1])),
        f"其中带 ICD-11 编码的只有 {n_icd} 条——"
        "**这一条如果没有 ICD-11，就说明它跟国际疾病分类还没有对上**，不是对上了没写。",
    ]
    if row is not None:
        ev.insert(1, f"这一条的来源标签：{row.get('source') or '未标注'}"
                     + (f"，ICD-11：{row['icd11_code']}" if row.get("icd11_code")
                        else "，没有 ICD-11 编码"))
    out.append(_evidence_section(ev, source="data/standard/syndromes.jsonl 的 source 列"))
    # 注意
    out.append(_section("注意", [
        "证候表的定义是**教材口径**，医案里的用法可能更宽——两者不一致时以"
        "「出处原文」那一节为准。",
    ], source="口径说明"))
    return [s for s in out if s]


def _formula_sections(name: str, *, ontology=None) -> list[dict]:
    from core.formula_verifier import rule_label
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    f = ont.formula(name)
    stats = ont.stats()
    out: list[dict | None] = []
    if f is not None:
        ident = [f"方剂「{f.name}」，方剂本体已收录"]
        if f.indications:
            ident.append(f"主治：{'、'.join(f.indications)}")
        out.append(_section("是什么", ident, source="方剂本体"))
        mech = []
        if f.indications:
            # 方的病机 = 它主治那些证的病机。**走证候表，不自己编**。
            for ind in f.indications[:3]:
                row = syndrome_row(ind)
                if row is None:
                    continue
                mech.append(f"主治「{ind}」的病机要素：病位 "
                            f"{'、'.join(row.get('location') or []) or '-'}；病性 "
                            f"{'、'.join(row.get('nature') or []) or '-'}")
            if not mech:
                mech.append(f"主治（{'、'.join(f.indications)}）在证候表里查不到对应条目，"
                            "所以这里给不出病机要素——这是证候表的覆盖缺口。")
        out.append(_section("病机", mech, source="证候表（按主治反查）"))
        pharm = []
        if f.composition:
            pharm.append("组成：" + "；".join(
                f"{n} {d}".strip() for n, d in f.composition))
        if f.functions:
            pharm.append(f"功用：{'、'.join(f.functions)}")
        for role in ("君药", "臣药", "佐药", "使药"):
            got = f.roles.get(role) or ()
            if got:
                pharm.append(f"{role}：{'、'.join(got)}")
        missing = [p for p in FORMULARY_PREDICATES
                   if not any(r.span.strip() for r in (f.refs.get(p) or ()))]
        if missing:
            pharm.append(f"本体里缺这几个谓词：{'、'.join(missing)}"
                         "（缺君臣佐使会让验证器的「君臣佐使结构」那条规则对这个方不可判）")
        out.append(_section("药理", pharm, source="药理层三元组（方剂）"))
        spans = []
        for pred in FORMULARY_PREDICATES:
            for ref in (f.refs.get(pred) or [])[:1]:
                if ref.span.strip():
                    spans.append(f"{pred}（{ref.book or '方剂'}）：{ref.span.strip()}")
        out.append(_section("出处原文", spans, source="药理层三元组的 source_span"))
        # 名老中医经验：规律层没有"按方"的档位，这件事要说出来而不是留空
        out.append(_section("名老中医经验", [
            f"R35 的名医用药规律层（{stats['n_patterns']} 条）是按**药**和按**证**"
            "分档统计的，没有按方统计的档位——所以这里查不到「某位名医怎么用这个方」。"
            "点方里的某一味药，那一节有那味药的名医剂量区间。",
        ], source="规律层档位说明"))
        roles_have = [r for r in ("君药", "臣药", "佐药", "使药") if f.roles.get(r)]
        out.append(_section("验证结果", [
            f"{rule_label('role_structure')}："
            + (f"可验证（本体标了 {'、'.join(roles_have)}）" if roles_have
               else "不可验证——本体里这个方没有君臣佐使的标注"),
            f"{rule_label('effect_matches_method')}："
            + (f"可验证（功用 {len(f.functions)} 条，"
               "治法↔功效的等价判定走同一张功效同义词表）" if f.functions
               else "不可验证——本体里这个方没有「功用」谓词"),
            f"{rule_label('herb_grounded')}、{rule_label('dose_exceeds')}、"
            f"{rule_label('incompatible_pair')}：按方里每一味药逐个判，点那味药看它那一节",
            _VERIFICATION_POINTER,
        ], source="core/formula_verifier.py 的九条规则"))
        got_books = sorted({r.book for refs in f.refs.values() for r in refs
                            if r.span.strip() and r.book})
        out.append(_evidence_section([
            _books_line(ont, "formulary", "方剂层"),
            f"这个方的条目落在：{'、'.join(f'《{b}》' for b in got_books)}" if got_books
            else "这个方的条目没有标注书名（source_span 为空）",
        ], source="Ontology.source_books()"))
        out.append(_section("注意", [
            f"加减：{'；'.join(f.modifications)}" if f.modifications else "",
            f"方剂本体现收 {stats['n_formulas']} 首（`python -m core.ontology --stats` 可核）——"
            "查不到一个方名**不等于这个方不存在**。",
        ], source="方剂本体 + 覆盖说明"))
    else:
        out.append(_section("是什么", [
            f"「{name}」不在方剂本体里（现收 {stats['n_formulas']} 首）。"
            "这不代表这个方不存在，只代表这里查不到它的组成与主治。"],
            source="本体覆盖缺口"))
        out.append(_section("药理", [
            f"方剂层里没有「{name}」的任何谓词（应有 {len(FORMULARY_PREDICATES)} 个："
            f"{'、'.join(FORMULARY_PREDICATES)}）。"],
            source="药理层覆盖缺口"))
        out.append(_evidence_section([_books_line(ont, "formulary", "方剂层")],
                                     source="Ontology.source_books()"))
    return [s for s in out if s]


def _element_sections(name: str, *, ontology=None) -> list[dict]:
    from core.elements import LOCATIONS, NATURES
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    kind = "病位证素" if name in LOCATIONS else ("病性证素" if name in NATURES else None)
    rows = [r for r in _load_standard_rows()
            if name in (r.get("location") or []) or name in (r.get("nature") or [])]
    out: list[dict | None] = [_section("是什么", [
        f"{name}：{kind}" if kind else
        f"「{name}」不在证素词表里（病位 {len(LOCATIONS)} 个、病性 {len(NATURES)} 个）",
        # R56 §6 第 8 条：「常见于 N 种证候」——原文「证候表里有 N 条证候用到它」
        # 读起来像在描述一张数据表，不是在描述这个证素本身。
        f"常见于 {len(rows)} 种证候" if rows else "",
    ], source="core/elements.py 词表")]
    # R42 新增：病机一节。证素**就是**病机的要素，所以这一节报它在哪些证的
    # 病机里出现、以及它是病位还是病性（这两者在病机链上的位置不同）。
    out.append(_section("病机", [
        f"它在病机链上的位置：{kind}——"
        + ("病位回答「病在哪」，是九层图的第 1 层" if kind == "病位证素"
           else "病性回答「病的性质是什么」（寒热虚实），是九层图的第 2 层"
           if kind == "病性证素" else "词表里没有它，位置判不了"),
        "；".join(f"{r.get('name')}" for r in rows[:8]) or "",
    ], source="证候表 + core/elements.py 词表"))
    # 药理：病位证素能接到归经，病性证素能接到药性——这是验证器那两条规则的入口
    if kind == "病位证素":
        herbs = ont.herbs_by_meridian(name)[:10]
        out.append(_section("药理", [
            f"本草本体里归{name}经的药 {len(ont.herbs_by_meridian(name))} 味，"
            f"例如：{'、'.join(h.name for h in herbs)}" if herbs
            else f"本草本体里没有归{name}经的药——"
                 "这是「归经」谓词的覆盖缺口，不是没有这样的药。",
        ], source="药理层三元组（归经）"))
    elif kind == "病性证素":
        herbs = ont.herbs_by_nature(name)[:10]
        out.append(_section("药理", [
            f"本草本体里药性为「{name}」的药 {len(ont.herbs_by_nature(name))} 味，"
            f"例如：{'、'.join(h.name for h in herbs)}" if herbs
            else f"本草本体里没有药性标为「{name}」的药——"
                 "病性证素与药性不是同一套词（证候说「气虚」，药性说「温/寒」），"
                 "这一格查不到是正常的。",
        ], source="药理层三元组（性味）"))
    # R56 §6 第 7 条：这里只列证候**名**，不带编码（SP-01/B04.xxx 这类内部
    # 编码进 tooltip，不进正文——见 web/app.js renderNodeExplain 里读
    # `s.codes` 拼 title= 属性那一段，`lines` 只放名字）。
    out.append(_section("出处原文", [
        "；".join((r.get("name") or "") for r in rows[:6]),
    ], source="证候表", codes=[r.get("code") for r in rows[:6] if r.get("code")]))
    # R56 §6 第 8 条：「病位 10 个、病性 12 个（core/elements.py，人工整理）」
    # 这句话整句删掉（不是改措辞）——它回答的是"这张词表本身有多大"，跟
    # "这个证素在这次问诊里意味着什么"没有关系，是纯粹的内部实现细节。
    # 循证对照这一节仍然保留（GUIDELINE_GAP_NOTE 那句口径声明对证素也适用），
    # 只是不再带这条内部统计。
    out.append(_evidence_section([], source="证候表"))
    return [s for s in out if s]


def _symptom_sections(name: str) -> list[dict]:
    rows = [r for r in _load_standard_rows()
            if name in (r.get("cardinal_symptoms") or [])
            or name in (r.get("secondary_symptoms") or [])]
    cardinal = [r for r in rows if name in (r.get("cardinal_symptoms") or [])]
    out: list[dict | None] = [_section("是什么", [
        f"症状条目「{name}」",
        f"证候表里 {len(rows)} 条证候提到它，其中 {len(cardinal)} 条把它列为主症"
        if rows else "证候表里没有证候提到它（可能是主诉里的自由表述，"
                     "S1 保留原样、没有并入标准条目）",
    ], source="证候表")]
    # 病机：这个症状指向哪些病位/病性。**这是"症状→证素"那条边在数据里的样子**。
    locs: dict[str, int] = {}
    nats: dict[str, int] = {}
    for r in rows:
        for x in (r.get("location") or []):
            locs[x] = locs.get(x, 0) + 1
        for x in (r.get("nature") or []):
            nats[x] = nats.get(x, 0) + 1
    top = lambda d: "、".join(  # noqa: E731 - 就地用一次，起名反而更难读
        f"{k}({v})" for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:6])
    out.append(_section("病机", [
        f"它在证候表里指向的病位：{top(locs)}" if locs else "",
        f"指向的病性：{top(nats)}" if nats else "",
        "括号里是「多少条证候用到这个组合」——**这是共现计数，不是概率**，"
        "不要把它读成「这个症状有多大可能是这个病位」。" if (locs or nats) else "",
    ], source="证候表的 location / nature 两列"))
    out.append(_section("名老中医经验", [
        "；".join(f"{r.get('name')}（{'主症' if name in (r.get('cardinal_symptoms') or []) else '次症'}）"
                  for r in rows[:6]),
    ], source="证候表"))
    out.append(_evidence_section([
        f"证候表对照基准：{len(_load_standard_rows())} 条证候，其中 {len(rows)} 条提到这个症状"
        f"（{len(cardinal)} 条列为主症）",
    ], source="data/standard/syndromes.jsonl"))
    return [s for s in out if s]


# ---------- R42：病机 / 治则 / 治法 三类新节点 ----------
#
# 这三类节点上的文字是**本次辨证的结构化输出**（`S3Structured.organs[].pathogenesis`
# / `.method.principle` / `.method.targets`），不是典籍条目。所以它们的释义必须
# 先把这件事说清楚，再给能查到的东西：
#
#   - 治则/治法：走 `core.effect_synonyms.expand_effect` 反查"哪些药的功效对得上它"
#     ——**跟验证器 `check_effect_matches_method` 同一处实现**，不另写字面匹配
#     （CLAUDE.md 第 31 条；那一条列的三次撞墙都是这么来的）。
#   - 病机：它是上游证型推出来的一句话，能查的是"证候表里哪些证的病位病性与它
#     的字面要素对得上"。这里**只做要素级匹配**（病位词/病性词），不拿整句话去
#     子串比——整句是模型的自由叙述，子串比的结果没有意义。

_ORIGIN_NOTE = {
    "pathogenesis": "这段文字是本次辨证里模型标定的病机"
                    "（`S3Structured.organs[].pathogenesis`），**不是典籍条目**——"
                    "它的教材口径要看上游的证型节点。",
    "principle": "这段文字是本次辨证的治则（`S3Structured.method.principle`），"
                 "**不是典籍条目**——治则由证型推出，典籍口径看上游证型节点。",
    "method": "这段文字是本次辨证的具体治法（`S3Structured.method.targets` 之一），"
              "**不是典籍条目**。",
}


def _method_like_sections(kind: str, text: str, *, ontology=None) -> list[dict]:
    """治则 / 治法。**功效匹配走 expand_effect，不自己写字面比。**"""
    from core.effect_synonyms import expand_effect, table_stats
    from core.formula_verifier import rule_label
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    out: list[dict | None] = [_section("是什么", [
        f"{'治则' if kind == 'principle' else '治法'}：{text}",
        _ORIGIN_NOTE[kind],
    ], source="本次辨证的结构化输出")]
    syns = expand_effect(text)
    herbs = ont.herbs_by_effect(text)
    tstats = table_stats()
    pharm = [
        f"同义功效词（走治法功效同义词表的 {tstats['n_rows']} 条）："
        f"{'、'.join(syns)}" if len(syns) > 1
        else f"功效同义词表（{tstats['n_rows']} 条）里没有「{text}」这条，"
             "所以下面的匹配退化成裸子串比——**命中少不代表没有对得上的药**。",
        f"本草本体里功效对得上的药 {len(herbs)} 味，例如："
        f"{'、'.join(h.name for h in herbs[:10])}" if herbs
        else "本草本体里没有功效对得上的药——这是覆盖缺口或表外说法，不是「无药可用」。",
    ]
    out.append(_section("药理", pharm, source="core/effect_synonyms.py + 药理层三元组"))
    out.append(_section("验证结果", [
        f"{rule_label('effect_matches_method')}：这一条规则**就是拿这段治法去比方里每味药的功效**，"
        f"用的是同一个 `expand_effect`（{tstats['n_rows']} 条表，"
        f"{tstats['n_textbook']} 条教材口径 / {tstats['n_common']} 条常见说法）。",
        _VERIFICATION_POINTER,
    ], source="core/formula_verifier.py::check_effect_matches_method"))
    out.append(_evidence_section([
        f"治法↔功效等价关系的对照基准：{tstats['n_rows']} 条同义表"
        f"（{tstats['n_effect_terms']} 个功效词），人工整理，"
        "不是从指南抽的——表外的说法会退化成裸子串比。",
    ], source="data/standard/effect_synonyms.tsv"))
    return [s for s in out if s]


def _pathogenesis_sections(text: str) -> list[dict]:
    """病机节点。**只做要素级匹配**，不拿整句话去子串比（见上面那段注释）。"""
    from core.elements import LOCATIONS, NATURES

    rows = _load_standard_rows()
    hit_loc = sorted({x for x in LOCATIONS if x and x in text})
    hit_nat = sorted({x for x in NATURES if x and x in text})
    out: list[dict | None] = [_section("是什么", [
        f"病机：{text}", _ORIGIN_NOTE["pathogenesis"],
    ], source="本次辨证的结构化输出")]
    out.append(_section("病机", [
        f"这段文字里能对上证素词表的病位：{'、'.join(hit_loc) or '（一个都没有）'}",
        f"能对上的病性：{'、'.join(hit_nat) or '（一个都没有）'}",
        "**只比要素词，不拿整句去子串匹配**：整句是模型的自由叙述，"
        "子串比的结果没有意义（CLAUDE.md 第 31 条列的三次撞墙都是字面比造成的）。",
    ], source="core/elements.py 词表"))
    same = [r for r in rows
            if hit_loc and hit_nat
            and set(hit_loc) & set(r.get("location") or [])
            and set(hit_nat) & set(r.get("nature") or [])]
    out.append(_evidence_section([
        f"证候表 {len(rows)} 条里，病位病性都与这段病机的要素相交的有 {len(same)} 条"
        + (f"：{'、'.join((r.get('name') or '') for r in same[:6])}" if same else ""),
        "相交**不等于同一个病机**——它只说明这段病机的要素在教材里有对应的证。",
    ], source="data/standard/syndromes.jsonl"))
    return [s for s in out if s]


def explain_node(node_id: str, *, name: str | None = None, ontology=None) -> dict:
    """一个节点的八节释义。**零 LLM**。

    返回 `{"available": bool, "node": …, "kind": …, "title": …, "sections": [...]}`。
    `available=False` 时 `sections` 为空且带 `note` 说明为什么——前端整块隐藏，
    不显示一个"暂无信息"的空壳（见模块文档第二条纪律）。

    `name` 是**显示名覆盖**，不是可选的装饰：问诊图的证型节点 label 是
    「病名 · 证型」拼出来的，而证候表里存的是证型名；而且节点 id 里的那一段
    可能带方名（`herb::四君子汤::党参`）。所以调用方（前端）把它手上的 label
    一起传过来，这里优先用它。
    **种类仍然从 id 的前缀判**：名字可以覆盖，"这是什么东西"不行——
    让调用方同时决定这两件事的话，一个写错的前缀会静默走到另一条查询分支上。
    """
    kind, id_name = parse_node_id(node_id)
    # 覆盖名去掉「病名 · 证型」这种拼接（问诊图证型层的 label 是拼出来的）：
    # 取最后一段——证候表里存的是证型名，不含病名前缀。
    override = (name or "").strip()
    if override:
        override = override.split("·")[-1].strip()
    name = override or id_name
    if not name or kind in ("unknown", "case"):
        note = ("认不出这个节点 id" if kind == "unknown"
                else "医案节点的释义就是医案本文，走证据链侧栏，不在这里重复一份")
        return {"available": False, "node": node_id, "kind": kind, "title": name,
                "sections": [], "note": note}
    builders = {
        "herb": lambda: _herb_sections(name, ontology=ontology),
        "formula": lambda: _formula_sections(name, ontology=ontology),
        "syndrome": lambda: _syndrome_sections(name, ontology=ontology),
        "element": lambda: _element_sections(name, ontology=ontology),
        "symptom": lambda: _symptom_sections(name),
        "pathogenesis": lambda: _pathogenesis_sections(name),
        "principle": lambda: _method_like_sections("principle", name, ontology=ontology),
        "method": lambda: _method_like_sections("method", name, ontology=ontology),
    }
    sections = builders[kind]()
    # 固定顺序（见 SECTION_ORDER）：各 builder 自己的顺序已经是它，这里再排一次
    # 是为了"新增一节的人不必记得放对位置"。
    order = {h: i for i, h in enumerate(SECTION_ORDER)}
    sections.sort(key=lambda s: order.get(s["heading"], len(order)))
    return {
        "available": bool(sections),
        "node": node_id,
        "kind": kind,
        "title": name,
        "sections": sections,
        "note": None if sections else "本地数据里查不到这个节点的任何一节释义",
    }
