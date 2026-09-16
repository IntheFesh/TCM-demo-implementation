"""R21：把全部中医药知识做成**缓存前缀**喂给模型，而不是靠 top-3 检索喂片段。

## 为什么是缓存前缀而不是微调

最终推理后端是 `deepseek-v4-pro`（1M 上下文）。它的前缀缓存让"每次都把全部知识
带上"从不可能变成便宜：

- **缓存机制**（官方文档，2026-09-15 核对，不是凭印象）：
  磁盘前缀缓存**默认开启**、不需要改代码；**以 64 token 为存储单位，不足 64 token
  的内容不会被缓存**；每个缓存前缀是一个独立完整的单元，后续请求**只有完全匹配
  某个前缀单元**才算命中；不再被使用的缓存会自动清除，**通常几小时到几天**；
  缓存存储本身不收费。
  https://api-docs.deepseek.com/guides/kv_cache/
  https://api-docs.deepseek.com/news/news0802/
- 命中数可以直接观测：响应 `usage` 里有 `prompt_cache_hit_tokens` /
  `prompt_cache_miss_tokens`（本仓库 09-15 的 curl 实测已经看到这两个字段）。
- 官方给的用法就是：可复用的部分（指令、上下文文档）放最前，变化的部分放最后。

「几小时到几天」这条直接决定了演示流程：**演示前要预热**（三条主诉各跑一次），
不能指望前一天跑过的缓存还在。这一条写进 DEMO.md（R25）。

## 段的顺序：为什么不是提示词里 1→6 的自然顺序

提示词 §1.2 把「system 指令 + 输出 schema」放在"所有医家相同 → 跨医家共享缓存"
那一档。**实际做不到**：`prompts/v1/s3_syndrome.yaml` 的第一行就是
「你正在模拟清代医家「$name」的辨证思路」——指令段里带医家名，三位医家的这一段
逐字节不同，放最前面等于把后面所有共享内容都挡在各自的缓存单元里
（官方原文：只有**完全匹配**某个前缀单元才命中，前缀一旦分叉，后面再相同也白搭）。

所以真正共享的那两段（本草速查表、方剂速查表）排到最前，指令段排在它们之后：

    发送顺序 = §2 本草速查表 → §3 方剂速查表 → §1 指令与 schema
             → §4 该医家全部医案 → §5 该医家用过的药材/方剂完整条目
             → §6 本次问诊（变化部分）

段号沿用提示词里的 1–6（裁剪优先级「4 > 5 > 2 > 3」说的就是这些号），
只是**发送顺序**按缓存的实际行为排。这个偏离是有依据的：依据就是 §1.2 自己那句
"所有医家相同"。

## `prompts/v1/s3_syndrome.yaml` 一个字都没改

这个 yaml 的模板在 0..4060 是指令 + 输出 schema（只含 `$name`），
从「证素分析：」起才是本次问诊的数据（`$elements_summary` / `$symptoms` /
`$refs`）。这里按这个边界把它切成 head（→ §1）和 tail（→ §6），
**切开再拼回去逐字节等于原模板**，有一条测试钉住这件事。

## token 估算用的是哪个尺

`tiktoken` 装了就用 `cl100k_base`（提示词允许的近似），没装就用
`_conservative_token_estimate`：**每个汉字算 1 token、其余每字符算 0.5 token**。
这不是从哪份文档抄来的换算率——它是一个**刻意偏大**的上界（真实 BPE 对中文
通常低于 1 token/字），宁可把预算算得更紧，也不要因为估小了而静默超预算。
`tokenizer_name()` 会跟着报告一起打出来，任何引用这些数的地方都必须带上它。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from core.data_paths import pharmacology_read_path
from core.llm import load_prompt
from core.physicians import PHYSICIANS, physicians_all, resolve_physician_id

ROOT = Path(__file__).resolve().parent.parent

# 每位医家的**稳定前缀**（§1..§5）token 上限。提示词给的是 500K；
# v4-pro 的窗口是 1M，留一半给变化部分和输出。
PREFIX_TOKEN_BUDGET = 500_000

# 段号沿用提示词 §1.2 的编号；EMIT_ORDER 是**实际发送顺序**，理由见模块文档字符串。
SECTION_INSTRUCTIONS = 1
SECTION_MATERIA = 2
SECTION_FORMULARY = 3
SECTION_CASES = 4
SECTION_ENTRIES = 5
SECTION_VARIABLE = 6
EMIT_ORDER = (SECTION_MATERIA, SECTION_FORMULARY, SECTION_INSTRUCTIONS,
              SECTION_CASES, SECTION_ENTRIES, SECTION_VARIABLE)
# 跨医家共享的段（放最前面才有意义）。
SHARED_SECTIONS = (SECTION_MATERIA, SECTION_FORMULARY)
# 超预算时的裁剪顺序 = 保留优先级「4 > 5 > 2 > 3」的反序。
# §4（医案）**永远全量**——它是这个系统的立身之本；§1/§6 也不可裁。
CUT_ORDER = (SECTION_FORMULARY, SECTION_MATERIA, SECTION_ENTRIES)

SECTION_TITLES = {
    SECTION_INSTRUCTIONS: "辨证指令与输出 schema",
    SECTION_MATERIA: "本草速查表",
    SECTION_FORMULARY: "方剂速查表",
    SECTION_CASES: "医案全量",
    SECTION_ENTRIES: "本医家用过的药材与方剂条目",
    SECTION_VARIABLE: "本次问诊",
}

# s3 模板里「指令+schema」和「本次问诊数据」的分界。用这一行的**字面**去找，
# 不用写死的字符偏移——yaml 改一个标点偏移就变，而这一行是它的结构标志。
_S3_DATA_MARKER = "证素分析：\n$elements_summary"

# 速查表的谓词取哪几个、按什么顺序排。顺序固定 = 生成结果确定。
MATERIA_QUICK_PREDICATES = ("性味", "归经", "功效", "用量", "禁忌")
FORMULARY_QUICK_PREDICATES = ("组成", "主治")
# 完整条目给全部谓词（六个 / 八个），顺序同样固定。
MATERIA_ALL_PREDICATES = ("性味", "归经", "功效", "用量", "禁忌", "炮制")
FORMULARY_ALL_PREDICATES = ("组成", "君药", "臣药", "佐药", "使药", "主治", "功用", "加减")

_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


# ---------- token 估算 ----------

def _conservative_token_estimate(text: str) -> int:
    """汉字 1 token、其余 0.5 token，向上取整。**刻意偏大**，见模块文档字符串。"""
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 1) // 2


def _load_tiktoken():
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        # 装了 tiktoken 但拉不到编码表（离线环境）——**不静默当成"没装"**，
        # 说一句再退回估算，否则报告里那个 tokenizer_name() 会让人以为用的是真尺。
        print("[context_prefix] tiktoken 装了但取不到 cl100k_base（离线？），"
              "退回保守估算。", file=sys.stderr)
        return None


# 惰性：`tiktoken.get_encoding()` **首次调用会去网络拉 BPE 表**（拉完才有本地缓存）。
# 这个模块被 core.chain 导入、chain 被 api.main 导入，所以在模块顶层求值等于
# 「`import api.main` 时联网」——冷启动那个数（RESULTS.md P1）会变成一次下载耗时，
# 而断网的机器上连 import 都要等它超时。哨兵对象而不是 None 判断，是因为
# 「还没试过」和「试过了、没有」必须分开：后者不该每次 count_tokens 都重试一遍。
_ENCODER_UNSET = object()
_encoder: object = _ENCODER_UNSET


def _encoder_or_none():
    global _encoder
    if _encoder is _ENCODER_UNSET:
        _encoder = _load_tiktoken()
    return _encoder


def tokenizer_name() -> str:
    """用的是哪把尺。**函数而不是模块常量**：常量要在 import 时就把编码表拉下来，
    见上面 `_encoder` 的注释。一处实现——报告、BudgetPlan、测试都问这里。"""
    return "tiktoken:cl100k_base" if _encoder_or_none() is not None else "conservative-estimate"


def count_tokens(text: str) -> int:
    encoder = _encoder_or_none()
    if encoder is not None:
        return len(encoder.encode(text, disallowed_special=()))
    return _conservative_token_estimate(text)


# ---------- 药理层索引 ----------

def _load_triples(kind: str) -> list[dict]:
    """读 data/standard/{kind}.jsonl。文件不存在返回空列表——沙盒里就是这样，
    `--report` 会如实说"这一段是空的"，不编数据。"""
    path = pharmacology_read_path(kind)  # type: ignore[arg-type]
    if path is None:
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("s") and row.get("p") and row.get("o"):
            rows.append(row)
    return rows


def build_entry_index(kind: str, rows: list[dict] | None = None) -> dict[str, dict[str, list[str]]]:
    """{主语: {谓词: [值…]}}。同一 (s,p) 多条时**全留**并按值排序去重——
    本草里一味药常有多个来源的功效描述，只留第一条等于挑了一个没有理由的赢家。"""
    out: dict[str, dict[str, list[str]]] = {}
    for row in (rows if rows is not None else _load_triples(kind)):
        out.setdefault(row["s"], {}).setdefault(row["p"], []).append(row["o"])
    for preds in out.values():
        for p, vals in preds.items():
            preds[p] = sorted(dict.fromkeys(vals))
    return out


# ---------- 速查表的行格式：**只在这里定义一次** ----------

def quick_line(name: str, preds: dict[str, list[str]], predicates: tuple[str, ...]) -> str:
    """一行一条「名｜谓词1｜谓词2…」。缺的谓词写 `-`，不跳过——列数固定，
    模型（和 R24 的 tooltip）才能靠位置读，而不是靠猜。

    R24 的前端 tooltip 走同一个函数（不是照抄格式）：格式写两处的话，
    改了一处另一处不会跟着改，而"模型看到的那行"和"界面显示的那行"不一致，
    等于界面在替模型背书它没看过的内容（CLAUDE.md 第 31 条）。
    """
    cells = [name]
    for p in predicates:
        vals = preds.get(p) or []
        cells.append("；".join(vals) if vals else "-")
    return "｜".join(cells)


def materia_quick_line(name: str, preds: dict[str, list[str]]) -> str:
    return quick_line(name, preds, MATERIA_QUICK_PREDICATES)


def formulary_quick_line(name: str, preds: dict[str, list[str]]) -> str:
    return quick_line(name, preds, FORMULARY_QUICK_PREDICATES)


def _quick_table(kind: str, index: dict[str, dict[str, list[str]]],
                 predicates: tuple[str, ...], title: str, unit: str) -> str:
    header = f"## {title}（{unit}｜{'｜'.join(predicates)}，缺项写 -）"
    if not index:
        # **说出来**：空表和"这本书里确实没有"是两件事。
        return (f"{header}\n（{kind}.jsonl 不在这台机器上，这一段为空。"
                f"上机跑 offline/extract_{kind} 后它才有内容。）")
    lines = [quick_line(name, index[name], predicates) for name in sorted(index)]
    return "\n".join([header, *lines])


# ---------- §2 / §3：共享的两张速查表 ----------

def build_materia_quick_table(index: dict | None = None) -> str:
    return _quick_table("materia_medica", index if index is not None else build_entry_index("materia_medica"),
                        MATERIA_QUICK_PREDICATES, SECTION_TITLES[SECTION_MATERIA], "药名")


def build_formulary_quick_table(index: dict | None = None) -> str:
    return _quick_table("formulary", index if index is not None else build_entry_index("formulary"),
                        FORMULARY_QUICK_PREDICATES, SECTION_TITLES[SECTION_FORMULARY], "方名")


# ---------- §1 / §6：把 s3 模板切成 head / tail ----------

def split_s3_template(template: str | None = None) -> tuple[str, str]:
    """(head, tail)，`head + tail == template` 逐字节成立。

    找不到分界标志时**抛异常**，不静默把整份模板当 head——那会让本次问诊的
    症状和参考医案一起进"稳定前缀"，缓存永远不命中，而且看日志看不出来。
    """
    t = template if template is not None else load_prompt("s3_syndrome")["system"]
    idx = t.find(_S3_DATA_MARKER)
    if idx < 0:
        raise ValueError(
            f"在 s3_syndrome.yaml 的 system 模板里找不到分界标志 {_S3_DATA_MARKER!r}。"
            "模板结构变了就要在 core/context_prefix.py 里同步这个标志——"
            "不能让本次问诊的数据混进稳定前缀（那会让缓存永远不命中）。"
        )
    return t[:idx], t[idx:]


# ---------- §4 / §5：医家专属 ----------

def _default_case_formatter():
    """`core.chain._format_case_block`。**函数内 import**：chain 要 import 这个
    模块（run_physician 组装 prompt），模块顶层互相 import 会循环。

    刻意复用那个私有函数而不是另写一个格式化器：参考医案块的格式是 E3/E4 闸门
    验过的，两处格式一旦分叉，`full_context` 下的医案块跟 `top3` 下的就不是
    同一种东西，两组数也就不可比了。
    """
    from core.chain import _format_case_block

    return _format_case_block


def physician_cases(pid: str, cases: list | None = None) -> list:
    """该医家全部医案，按 case_id 排序。排序固定 = 前缀确定。"""
    if cases is None:
        # 函数内 import + 复用 core.retrieval.load_cases：**"哪些医案算可用"
        # 这个判断只能有一处**（P0-6 的过滤规则），不能在这里再读一遍 cases.json。
        from core.retrieval import load_cases

        cases, _, _ = load_cases()
    return sorted([c for c in cases if c.physician == pid], key=lambda c: c.case_id)


def build_cases_section(selected: list, format_case=None) -> str:
    """§4。**参数是已经选好的医案列表**，不是医家 id。

    这样 E3/E4 的三种 refs_mode 不用在这里再实现一遍：
      own     → 传本医家的全量语料
      swapped → 传**另一位**医家的全量语料（指令段仍然是本医家，正是 E3 的条件）
      none    → 传空列表（正是 E4 的条件）
    选谁由 `core/chain.py::_search_cases` 一处决定（它已经按 refs_mode 换过
    physician 了），这里只负责格式化。
    """
    fmt = format_case or _default_case_formatter()
    header = f"## {SECTION_TITLES[SECTION_CASES]}（{len(selected)} 诊次，按 case_id 排序）"
    if not selected:
        return f"{header}\n（本次不带医案块。）"
    return "\n\n".join([header, *[fmt(c) for c in selected]])


def herbs_and_formulas_used(selected: list) -> tuple[list[str], list[str]]:
    """这批医案里用过的药名与方名，去重排序。**用原始写法**，不归一：
    速查表和完整条目的主语都是药理层里的原始 `s`，归一之后对不上。

    参数是**已经选好的医案列表**，跟 build_cases_section 一致：refs_mode=swapped
    时 §4 摆的是另一位医家的语料，§5 就该是那批语料里用到的药——两段对不上
    会让模型看到"医案里用了 A 药，而条目表里列的是 B 药"。
    """
    herbs = {h for c in selected for h in (c.herbs or [])}
    formulas = {c.formula for c in selected if c.formula}
    return sorted(herbs), sorted(formulas)


def _entry_block(name: str, preds: dict[str, list[str]], predicates: tuple[str, ...]) -> str:
    lines = [f"### {name}"]
    for p in predicates:
        for v in preds.get(p) or []:
            lines.append(f"{p}：{v}")
    return "\n".join(lines)


def build_entries_section(selected: list, materia: dict | None = None,
                          formulary: dict | None = None) -> str:
    herbs, formulas = herbs_and_formulas_used(selected)
    m = materia if materia is not None else build_entry_index("materia_medica")
    f = formulary if formulary is not None else build_entry_index("formulary")
    hit_h = [h for h in herbs if h in m]
    hit_f = [x for x in formulas if x in f]
    header = (f"## {SECTION_TITLES[SECTION_ENTRIES]}"
              f"（药材 {len(hit_h)}/{len(herbs)} 味、方剂 {len(hit_f)}/{len(formulas)} 张有条目）")
    parts = [header]
    # 分母写出来：命中率低的时候要看得见，而不是只看到"有 3 味药的条目"。
    parts += [_entry_block(h, m[h], MATERIA_ALL_PREDICATES) for h in hit_h]
    parts += [_entry_block(x, f[x], FORMULARY_ALL_PREDICATES) for x in hit_f]
    if len(parts) == 1:
        parts.append("（药理层文件不在这台机器上，或这位医家用的药都没有条目。）")
    return "\n\n".join(parts)


# ---------- 预算与裁剪 ----------

@dataclass(frozen=True)
class BudgetPlan:
    """哪些段被裁掉了。**共享段的裁剪对所有医家一致**——共享段一旦按医家分叉，
    它就不再共享，跨医家缓存复用整个失效（那是这套设计最大的一块收益）。
    所以共享段的决定用"最大的那位医家"来定，而不是各自算各自的。
    """

    dropped: tuple[int, ...]
    tokens_by_physician: dict[str, dict[int, int]]
    tokens_by_physician_after: dict[str, dict[int, int]]
    budget: int
    tokenizer: str

    def kept(self, section: int) -> bool:
        return section not in self.dropped


def _section_texts(pid: str, *, cases=None, materia=None, formulary=None,
                   format_case=None) -> dict[int, str]:
    head, _ = split_s3_template()
    return {
        SECTION_MATERIA: build_materia_quick_table(materia),
        SECTION_FORMULARY: build_formulary_quick_table(formulary),
        SECTION_INSTRUCTIONS: head,
        SECTION_CASES: build_cases_section(physician_cases(pid, cases), format_case),
        SECTION_ENTRIES: build_entries_section(physician_cases(pid, cases), materia, formulary),
    }


def budget_plan(physician_ids: list[str] | None = None, *, budget: int = PREFIX_TOKEN_BUDGET,
                cases=None, materia=None, formulary=None, format_case=None) -> BudgetPlan:
    pids = physician_ids if physician_ids is not None else sorted(physicians_all(PHYSICIANS))
    before: dict[str, dict[int, int]] = {}
    texts: dict[str, dict[int, str]] = {}
    for pid in pids:
        texts[pid] = _section_texts(pid, cases=cases, materia=materia,
                                    formulary=formulary, format_case=format_case)
        before[pid] = {s: count_tokens(t) for s, t in texts[pid].items()}

    dropped: list[int] = []
    for section in CUT_ORDER:
        worst = max((sum(v for s, v in b.items() if s not in dropped) for b in before.values()),
                    default=0)
        if worst <= budget:
            break
        dropped.append(section)
    after = {pid: {s: v for s, v in b.items() if s not in dropped} for pid, b in before.items()}
    return BudgetPlan(tuple(dropped), before, after, budget, tokenizer_name())


# ---------- 三个公开入口 ----------

def build_shared_prefix(plan: BudgetPlan | None = None, *, materia=None, formulary=None) -> str:
    """§2 + §3。**对所有医家逐字节相同**——这一段就是跨医家共享缓存的全部。"""
    parts = []
    if plan is None or plan.kept(SECTION_MATERIA):
        parts.append(build_materia_quick_table(materia))
    if plan is None or plan.kept(SECTION_FORMULARY):
        parts.append(build_formulary_quick_table(formulary))
    return "\n\n".join(parts)


def build_physician_prefix(pid: str, plan: BudgetPlan | None = None, *, cases=None,
                           materia=None, formulary=None, format_case=None) -> str:
    """§1 + §4 + §5（指令段带医家名，所以它在这里而不在共享段，见模块文档字符串）。"""
    resolved = resolve_physician_id(pid)
    if resolved is None:
        raise ValueError(f"认不出医家 {pid!r}")
    head, _ = split_s3_template()
    name = physicians_all(PHYSICIANS)[resolved]["name"]
    parts = [head.replace("$name", name),
             build_cases_section(physician_cases(resolved, cases), format_case)]
    if plan is None or plan.kept(SECTION_ENTRIES):
        parts.append(build_entries_section(physician_cases(resolved, cases), materia, formulary))
    return "\n\n".join(parts)


#: §6 里 `$refs` 的占位文本。**不在 §6 重复一遍医案**——医案已经在 §4 的稳定
#: 前缀里，重复一遍等于把 18 万 token 又按未命中价付一次，而且两份内容一旦
#: 因为格式化路径不同而有出入，模型会看到自相矛盾的两套参考。
REFS_POINTER = "（本医家医案已全量列在上文【医案全量】一节，按 case_id 排序，直接引用其中的 id）"
REFS_POINTER_EMPTY = "（本次不带参考医案块）"


def assemble(pid: str, s1=None, s2=None, complaint: str = "", *,
             case_block: list | None = None,
             elements_summary: str = "", symptoms: str = "", plan: BudgetPlan | None = None,
             cases=None, materia=None, formulary=None, format_case=None) -> str:
    """整份 system prompt：共享前缀 → 医家前缀 → §6 变化部分。

    `case_block` 是 §4 要摆的医案列表（`None` = 用本医家全量）。E3/E4 的
    swapped/none 就靠传别人的列表 / 传空列表实现，见 build_cases_section。

    §6 是 s3 模板的 tail 原样渲染（`$elements_summary` / `$symptoms` / `$name` /
    `$refs`），其中 `$refs` 填的是**指针**而不是医案正文，理由见 REFS_POINTER。
    """
    resolved = resolve_physician_id(pid)
    if resolved is None:
        raise ValueError(f"认不出医家 {pid!r}")
    name = physicians_all(PHYSICIANS)[resolved]["name"]
    head, tail = split_s3_template()
    if not elements_summary and s2 is not None:
        from core.chain import _format_elements_summary

        elements_summary = _format_elements_summary(s2)
    if not symptoms:
        symptoms = "；".join(s1.symptoms) if s1 is not None else complaint
    selected = physician_cases(resolved, cases) if case_block is None else case_block
    variable = (tail.replace("$elements_summary", elements_summary)
                    .replace("$symptoms", symptoms)
                    .replace("$name", name)
                    .replace("$refs", REFS_POINTER if selected else REFS_POINTER_EMPTY))
    parts = [
        build_shared_prefix(plan, materia=materia, formulary=formulary),
        head.replace("$name", name),
        build_cases_section(selected, format_case),
    ]
    if plan is None or plan.kept(SECTION_ENTRIES):
        # §5 从 §4 摆的那批医案算，不重新读盘：两段必须对得上（见
        # herbs_and_formulas_used 的文档字符串），而且 full_context 下
        # 调用方已经把医案传进来了，再读一次盘纯属浪费。
        parts.append(build_entries_section(selected, materia, formulary))
    parts.append(variable)
    return "\n\n".join(x for x in parts if x)


def prefix_sha256(pid: str, **kw) -> str:
    return hashlib.sha256(build_physician_prefix(pid, **kw).encode("utf-8")).hexdigest()


def prefix_tokens_by_section(pid: str, plan: BudgetPlan | None = None, **kw) -> dict[str, int]:
    """进 manifest 的那份读数。键是段名而不是段号——manifest 是给人读的。"""
    texts = _section_texts(pid, **kw)
    out = {}
    for section in EMIT_ORDER:
        if section == SECTION_VARIABLE or section not in texts:
            continue
        if plan is not None and not plan.kept(section):
            continue
        out[SECTION_TITLES[section]] = count_tokens(texts[section])
    return out


# ---------- 合成语料（只给 --report 在沙盒里看量级用） ----------

def synthetic_cases(physician_ids: list[str], n_per: int) -> list:
    """每位医家造 n_per 条医案。**确定性**（没有随机、没有时间戳），所以
    `--synthetic` 跑两次报出来的 token 数一样——不然这个功能自己就在漂。

    造出来的内容刻意写成"合成"字样：万一有人把这份输出当真机数贴进报告，
    那几个字会在报告里露出来。
    """
    from core.schemas import CaseRecord

    out = []
    for pid in physician_ids:
        for i in range(n_per):
            cid = f"{pid}-synthetic-{i:04d}"
            out.append(CaseRecord(
                case_id=cid, case_group_id=f"{pid}-synthetic-g{i // 3}", physician=pid,
                raw=f"（合成语料 {cid}）" + "某患者初诊，脘腹痞满，纳谷不香。" * 6,
                raw_excerpt=f"（合成语料 {cid}）某患者初诊，脘腹痞满，纳谷不香，"
                            "舌淡红苔薄白，脉弦细。予健脾和胃之品。",
                symptoms=["脘腹痞满", "纳差", "乏力"], tongue="淡红", pulse="弦细",
                syndrome="脾胃气虚", pathogenesis="中气不足，运化失司",
                treatment_principle="健脾益气", formula="四君子汤",
                herbs=["人参", "白术", "茯苓", "甘草"],
                visit_index=i % 3,
            ))
    return out


# ---------- --report ----------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="打印每位医家四段各占多少 token")
    ap.add_argument("--budget", type=int, default=PREFIX_TOKEN_BUDGET)
    ap.add_argument("--physician", default="", help="只看某一位（默认全部）")
    ap.add_argument("--synthetic", type=int, default=0, metavar="N",
                    help="没有 cases.json 时用 N 条合成医案跑，好在沙盒里也能看到"
                         "四段的量级关系。**合成数不是真机数**，报告里会标出来")
    args = ap.parse_args(argv)
    if not args.report:
        ap.print_help()
        return 0

    pids = [resolve_physician_id(args.physician) or args.physician] if args.physician \
        else sorted(physicians_all(PHYSICIANS))
    cases = synthetic_cases(pids, args.synthetic) if args.synthetic else None
    plan = budget_plan(pids, budget=args.budget, cases=cases)
    print(f"token 尺：{plan.tokenizer}｜预算：{plan.budget:,}/医家")
    if cases is not None:
        print(f"⚠ **合成语料**：每位医家 {args.synthetic} 条造出来的医案，"
              "量级关系可看、绝对值不是真机数（真机跑不传 --synthetic）")
    if plan.dropped:
        print(f"**裁掉的段**：{'、'.join(SECTION_TITLES[s] for s in plan.dropped)}"
              f"（裁剪顺序固定 {[SECTION_TITLES[s] for s in CUT_ORDER]}，医案永不裁）")
    else:
        print("没有段被裁（全部在预算内）")
    for pid in pids:
        before, after = plan.tokens_by_physician[pid], plan.tokens_by_physician_after[pid]
        print(f"\n=== {pid} ===")
        for s in EMIT_ORDER:
            if s not in before:
                continue
            mark = "" if s in after else "  ← 已裁"
            print(f"  §{s} {SECTION_TITLES[s]:<28} {before[s]:>9,}{mark}")
        print(f"  {'稳定前缀合计（裁后）':<32} {sum(after.values()):>9,}")
        print(f"  sha256(医家前缀)[:12] = {prefix_sha256(pid, plan=plan, cases=cases)[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
