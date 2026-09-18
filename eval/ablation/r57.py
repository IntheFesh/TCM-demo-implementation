"""R57：四组消融，证明"不靠模仿也能推"。**本轮成败的唯一判据**——不达标就回
R51 补规则，不许把医案放回推导相凑数（那是回退，不是修复）。

    python -m eval.ablation.r57 --backend fake --sdt-dir <TCMEval>/evaluation/TCMEval-SDT
    python -m eval.ablation.r57 --backend real --sdt-dir <TCMEval>/evaluation/TCMEval-SDT

分组定义（A/B/C/D 各自设哪些环境变量）**唯一出处**是 `eval/ablation/spec.py`，
这里只负责"用那份定义去跑、去汇总"，不重复定义。

## 三条硬指标（`eval/ablation/spec.py` 的 GATE_* 常量）

1. C 组验证器一次过率 ≥ A 组——"不模仿医案，结论照样能一次通过符号验证"。
2. C 组 rule_refs 完整率（`derivation_completeness_ratio`）≥ 0.9——"推导链上
   每一步都真的挂着医理规则，不是空转"。
3. C 与 D 的证型/治法/主方一致率**不明显超过噪声地板**——验的是 R54 的
   不变式："事后佐证不回流改推导"，C/D 唯一的差异就是要不要跑第三相佐证，
   结论不该跟着变。**R61 改掉了这条门的判法**：原来跟绝对值 0.9 比，用户
   真机实测出这条门在自由文本（`method`/治法）上必然不达标——两次独立
   采样的措辞几乎不可能逐字相同，逐字相等量到的是采样方差，不是不变式
   本身。现在跟 E 组（C 组的噪声地板复测，见 spec.py）比：
   `C-D 分歧率 ≤ C-E 分歧率 + margin`（`GATE_CD_CONSISTENCY_MARGIN`）。
   而且这条门**拆成三项分开判**（证型/主方/治法），不再合成一个布尔——
   三项的可比较性不一样：证型是受控词表可以判"相等"，主方名归一后
   （统一"加减/加味"后缀）也可以判"相等"，治法是自由文本只能判"相似度"
   （字符级 Jaccard），把三者硬凑成一个"一致/不一致"会把"措辞不同"和
   "真的推出了不同结论"混在一起——详见 `pair_consistency` 的文档字符串。

**门都要求有效样本 ≥ `GATE_MIN_SAMPLE_SIZE`（R59，见 spec.py）**，低于这
个数一律判 `passed=None`（⏳ 样本不足），不管比率算出来是多少——`--limit 2`
这类小样本探针把某条门的分母量成 1、比率算出 100%，那不是"测出来过了"，是
"根本没测够"，真机 20 条 × 4 组才够格判定。

## 三分总判定，不是二元 True/False（R59）

`all_gates_passed` 只有在**三条门都是 True** 时才是 `True`；**任何一条是
`False` 就整体 `False`**（不管别的门有没有测出来）；**没有 False、但至少
一条是 `None`（⏳ 没测出来）就整体 `None`**——⏳ 不许被悄悄算成过了。这是
真机探针（`--limit 2`）实测过的假阳性：旧逻辑先把 `None` 过滤掉再对剩下的
门做 `all()`，两条门过、一条没测出来（C-D 一致率因为没跑 D 组）会被判成
"三条全过"。

## 五个指标，每组都报

`verifier_first_pass_rate`、`rule_refs_completeness_rate`、
`herbs_grounded_ratio_mean`、`hallucination_rate`、`cost`（调用数 + 墙钟）——
延续 R34/R38 那三个内容指标的做法（**每个指标都带样本量**，不让读者自己数，
也不让一个 `1（1/1）`这种量级的数字看起来跟真测过没区别）。
`rule_refs_completeness_rate` 是 R57 新加的：A 组这一格恒是"不适用"
（`run_synthesis` 根本不产出 `derivation_completeness_ratio` 这个键，不是
凑巧算出 0）；**B 组这一格 R59 起也恒是"不适用"**——B 组 `THEORY_LAYER=off`，
演绎链上根本没有医理规则可引，"完整率"在这一组结构上未定义，跟 A 组不
是同一个原因（A 是"没有这个概念"，B 是"这个概念存在但这组关掉了"），两组
各自的 `rule_refs_note` 说清楚是哪一种。真机探针实测出过"B 组满分、C 组
0.375"这种反直觉数字——不是 C 比 B 差，是 B 那个满分本身没有意义。

## 沙盒里跑不出真机数

跟 R38 同一条诚实约束：这个沙盒没有真实 LLM 后端，`--backend fake` 只能验
管道（四组分别设对了环境变量、跑通了四条路径、报告格式对不对），**不能**
产出可信的内容指标——假后端的产出是固定假文本，"验证器一次过率" 算出来的
只是假数据长什么样。真机 20 条主诉 × 4 组 = 80 次问诊，**R61 加了 E 组
（噪声地板对照）之后是 20 条 × 5 组 = 100 次**——多出的 20 次问诊全部是
E 组（跟 C 组同配置的复测），只用来量 C-D 一致率闸门的噪声地板，不是新增
一条独立的实验维度。**单次墙钟没有一个可信数字**：R55 commit 留下两处
互相矛盾的记录（"top3 档约 45 秒、full_context 档约 75 秒" vs 另一处
"三档墙钟实测 264.6/276.3/208.9 秒"，量级差 3~4 倍），量出后者的工具
`scripts/compare_reasoning_tiers.py` 在仓库里不存在，两处谁准核实不到。
**不要用这两个数字估算时间/费用**，先跑 `--limit 2`（2 条 × 5 组 =
10 次问诊）拿到本机真实的 `elapsed_s_mean`，按比例估算 100 次的时长，
具体方法见 `docs/ONSITE_R57_R58.md`。跑法见本文件顶部两行命令，
`--sdt-dir` 指向用户自己的 TCMEval-SDT 本地 checkout（数据集不随本仓库
分发，见 `eval/sdt/data.py`）。断点续跑、结果判读、C 组不达标时的诊断
命令同样见 `docs/ONSITE_R57_R58.md`，不在这里重复。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from eval.ablation.spec import (
    CONSISTENCY_PAIR,
    GATE_C_RULE_REFS_COMPLETENESS_MIN,
    GATE_C_VERIFIER_FIRST_PASS_VS,
    GATE_CD_CONSISTENCY_MARGIN,
    GATE_MIN_SAMPLE_SIZE,
    GROUPS,
    NOISE_FLOOR_PAIR,
    R57Group,
    group_by_key,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OUT = ROOT / "eval" / "report_ablation_r57.json"

#: 消融要动的三个旋钮。**全部先清空再按组设置**——同 R38 那条"三个旋钮全部
#: 先删掉再设"的理由：上一组留下的变量不清掉，下一组量出来的是叠加效应。
KNOBS = ("S3_MODE", "THEORY_LAYER", "CORROBORATION")


@contextmanager
def apply_group(group: R57Group):
    saved = {k: os.environ.get(k) for k in KNOBS}
    try:
        for k in KNOBS:
            os.environ.pop(k, None)
        os.environ.update(group.env)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------- 主诉来源：SDT Train 脾胃门 20 条 ----------

def select_pi_wei_men_complaints(sdt_dir: Path, n: int = 20) -> list[dict]:
    """从 SDT Train 里挑 `n` 条脾胃门主诉。**判据是"证型的第一个词含脾/胃"**
    （`TCM Syndrome` 字段按 `;` 分隔多个证型，第一个通常是主证）——比在
    `Clinical Data` 原文里搜"胃"这类关键词更准：后者会把"食欲不振"这种
    在别的系统性疾病里顺带提一句的词也算进来（实测：关键词法命中 73 条，
    证型法命中 27 条，两者差出一倍不止）。

    **排序确定性**：按"证型词数升序、病案编号升序"排——词数少的证型更单纯
    （比如"脾胃虚弱"比"脾肾两虚;气化失运;湿邪阻滞"更适合当这一轮要验证的
    典型样本），病案编号兜底让相同词数时的顺序可复现，不依赖字典遍历顺序
    这种平台相关的隐藏状态。**这个函数是幂等的**：同一份 Train 数据、同一个
    n，每次跑出来的 20 条一模一样，可以被审计、被重放。

    SDT 数据集本身不随本仓库分发（见 `eval/sdt/data.py`），所以这里不缓存
    结果到仓库里的文件，每次调用都现读 `--sdt-dir` 指向的用户本地副本。

    **直接读原始 JSON，不走 `eval.sdt.data.load_split`**：`SdtRecord`
    （`load_split` 的返回类型）没有保留 `"TCM Syndrome"` 这个原始字段——
    那是金标准的一部分，`load_split` 已经把它拆进
    `gold_syndrome_answers`/`gold_pathogenesis_answers` 这几个跟官方评测
    格式对齐的字段，丢了"哪个证型排第一"这个筛选要用的顺序信息。这个函数
    要的正是原始顺序，所以绕过那层转换，直接读 `Train_TCM_Data_v1.json`。
    """
    def _rid(record_id: str) -> int:
        m = re.search(r"\d+", record_id)
        return int(m.group()) if m else 0

    raw = json.loads((Path(sdt_dir) / "data" / "Train_TCM_Data_v1.json")
                     .read_text(encoding="utf-8"))
    hits = []
    for row in raw:
        syn = row.get("TCM Syndrome", "")
        first = syn.split(";")[0] if syn else ""
        if "脾" in first or "胃" in first:
            hits.append(row)
    hits.sort(key=lambda row: (len(row.get("TCM Syndrome", "").split(";")),
                               _rid(row.get("Medical Record ID", ""))))
    picked = hits[:n]
    if len(picked) < n:
        raise ValueError(
            f"SDT Train 里按「证型含脾/胃」筛出 {len(picked)} 条，不够 {n} 条。")
    return [{"record_id": row["Medical Record ID"], "syndrome": row.get("TCM Syndrome", ""),
            "complaint": row["Clinical Data"]} for row in picked]


# ---------- 单次问诊 → 五指标的原料 ----------

def _syndrome_method_formula(s3_structured) -> tuple[str | None, str | None, str | None]:
    """`S3Structured`/`S3Derived` 字段名相同（`syndrome.name`/`method.principle`/
    `formula.candidate.name`），一份实现两种模式都能读——这是 R52 设计
    `S3Derived` 时刻意保留的字段名兼容性（见 `core/chain.py::run_derivation`
    文档字符串），这里直接复用，不为两种 schema 分别写一份取值逻辑。

    `result["results"][0]["s3_structured"]` 拿到的是**没有 `.model_dump()`
    过的 pydantic 对象**（`core/chain.py::run_derivation`/`run_synthesis`
    原样存的是 `raw`），不是 dict——`rule_refs`/`insufficient_notes` 那几个
    键是显式 `.model_dump()` 过的，`s3_structured` 本身不是，两者不统一，
    所以这里用属性访问，不假设它是 dict。经过 JSON 往返（比如从磁盘读回
    已经落盘的报告）之后它会变成 dict，`getattr(obj, name, None)` 对 dict
    不生效，所以两条路径都要接住。"""
    if not s3_structured:
        return None, None, None

    def _get(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    syn = _get(_get(s3_structured, "syndrome"), "name")
    method = _get(_get(s3_structured, "method"), "principle")
    formula = _get(_get(_get(s3_structured, "formula"), "candidate"), "name")
    return syn, method, formula


#: R61 §1.3：治法方名里最常见的两种"在经典方基础上化裁"写法。只统一这两种，
#: 不臆测更多写法（比如"化裁"）——CLAUDE.md 别过度设计，够用就好。
_FORMULA_MODIFIER_SUFFIXES = ("加减", "加味")


def _split_formula_suffix(name: str | None) -> tuple[str, str | None]:
    """去掉方名两端与内部空白，拆出「基础方名」与「化裁后缀」（`_FORMULA_MODIFIER_SUFFIXES`
    之一，没有则 `None`）。**两个不同的问题共用这一步拆分，但各自只取自己
    要的那一半，不是同一次比较**（CLAUDE.md「同一概念只能有一处实现」的
    例外情形，写清楚两者的区别）：

      - C-D/C-E 一致率比较（`pair_consistency`）要回答"这两次采样说的是不是
        同一张方"——"柴胡疏肝散加减"跟"柴胡疏肝散加味"该判成相同（只是
        换了个近义后缀），"柴胡疏肝散"跟"黄芪建中汤加减"该判成不同（基础
        方名本身不同）。这里要**同时看基础方名与后缀**（`_normalize_formula_for_comparison`
        把后缀统一成"加减"一种写法再比，不丢掉"有没有化裁"这件事本身）。
      - 本体命中率（`_formula_ontology_hit`，R61 §4）要回答"这个方名对应
        本体 235 首方剂里的哪一条"——本体收录的是经典方**原名**，不带任何
        化裁后缀，所以这里只要**基础方名**去查，后缀直接丢弃。
    """
    s = re.sub(r"\s+", "", name or "")
    for suf in _FORMULA_MODIFIER_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)], suf
    return s, None


def _normalize_formula_for_comparison(name: str | None) -> str:
    """比较用：基础方名不变，把 `_FORMULA_MODIFIER_SUFFIXES` 里任意一种后缀
    统一成"加减"这一种写法——"柴胡疏肝散加味"与"柴胡疏肝散加减"应该被
    C-D 一致率判成相同（只是模型换了个近义词描述"化裁"这件事），不该被
    字面不等拖累进"分歧"里。没有后缀的（`source=classic` 的原方）保持原样，
    不强行补一个"加减"——"柴胡疏肝散"跟"柴胡疏肝散加减"仍然算不同
    （一个是原方，一个明确说了做过加减，这个区别是有意义的，不能抹掉）。"""
    if not name:
        return ""
    base, suf = _split_formula_suffix(name)
    return base + "加减" if suf else base


#: 治法/证型文本里常见的标点与空白，字符级 Jaccard 之前先剔掉——比的是
#: "用词重叠程度"，标点不携带这个信息，留着只会稀释相似度。
_PUNCT_RE = re.compile(r"[，。、；：“”‘’（）()\s]+")


def _char_jaccard(a: str | None, b: str | None) -> float | None:
    """治法（`method.principle`）是自由文本，两次独立采样几乎不可能逐字
    相同——字符串相等在这上面测的是采样方差，不是"结论变了没变"（R61 §1.2，
    跟 CLAUDE.md 已经记录过的两次教训——覆盖检查比字面子串、分歧度比证型
    名字面相等——是同一类问题的第三次出现：**字面比较测不出"意思相不相同"**）。

    改用字符级 Jaccard **相似度**（`|交集|/|并集|`，元素是去标点空白之后的
    单字），报"有多像"而不是"是否相等"。**方向跟 `core/chain.py` 的
    `herb_jaccard` 是反的**：那边报的是**距离**（`1 - 相似度`，语义是"两位
    医家用药差多远"，好跟 ε 噪声地板的"差异上限"比大小）；这里报的是
    **相似度本身**（语义是"这两段治法描述有多接近"，好跟 C-E 噪声地板的
    "平均相似度"比大小）——都是同一个 Jaccard 系数，只是这里不取
    `1 -`，别被两处名字都叫"jaccard"搞混方向。"""
    if a is None or b is None:
        return None
    sa = set(_PUNCT_RE.sub("", a))
    sb = set(_PUNCT_RE.sub("", b))
    union = sa | sb
    if not union:
        return None
    return round(len(sa & sb) / len(union), 4)


def metrics_from_result(result: dict | None) -> dict:
    """R60 §2.5：一次问诊被 `SymbolicVeto` 拦下时 `result["results"]` 恒为
    `[]`（`core/chain.py::_stopped` 的既有设计——被拦的请求不产出任何方药），
    这个函数原来一见 `results` 空就直接 `return {"has_output": False}`，
    把 `result` 顶层的 `verification_veto`（拦截当时的 `final_violations`：
    rule/herbs/reason/counterexample，`core/chain.py` 那条 `except SymbolicVeto`
    分支已经在存）连同 `has_output=False` 一起扔了——**排障因此必须重跑一次
    真机才能看到"到底是哪条规则、哪条反例"**，这正是用户实测抓到的记账问题。
    现在两条分支都把它带出来（有就带、没有不硬造 None 占位）。"""
    results = (result or {}).get("results") or []
    r = results[0] if results else {}
    veto = (result or {}).get("verification_veto")
    veto_kw = {"verification_veto": veto} if veto else {}
    if not r or r.get("s3") is None:
        return {"has_output": False, **veto_kw}
    vm = r.get("verifier_metrics") or {}
    completeness = r.get("derivation_completeness_ratio")  # None：A 组没有这个键，不适用
    syn, method, formula = _syndrome_method_formula(r.get("s3_structured"))
    return {
        "has_output": True,
        "herbs_grounded_ratio": r.get("herbs_grounded_ratio"),
        "verifier_first_pass": vm.get("verifier_first_pass"),
        "rule_refs_completeness": completeness,
        "n_hallucinated_ids": len(r.get("hallucinated") or []),
        "syndrome": syn, "method": method, "formula": formula,
        **veto_kw,
        "llm_calls": ((result or {}).get("manifest") or {}).get("llm_calls"),
        "s3_mode": ((result or {}).get("manifest") or {}).get("s3_mode"),
    }


def _rate(hits: int, total: int) -> dict:
    return {"value": (round(hits / total, 4) if total else None),
            "n": hits, "denominator": total}


def run_group(group: R57Group, complaints: list[dict], backend, *, progress=None,
             keep_raw_results: bool = False) -> list[dict]:
    """跑一组。每条主诉一次问诊，一次失败不丢整组（同 R9/R38 的失败容忍）。

    `keep_raw_results=True` 时每行多带一个 `result` 键（完整 `consult()`
    返回值，含 `insufficient_notes`/`rule_refs` 明细）——**默认不带**，理由
    跟 `run_once(keep_result=...)` 一样：正式报告只用得上 `metrics`
    算出来的汇总比率，带上完整结果会让 `eval/report_ablation_r57.json`
    涨几十倍。`scripts/diagnose_r57_group.py` 要看"缺的是哪一类规则"这种
    明细，才需要打开它。"""
    from scripts.bench_consult import run_once

    rows: list[dict] = []
    with apply_group(group):
        for i, c in enumerate(complaints, 1):
            if progress is not None:
                progress(f"  {group.key} {i}/{len(complaints)}　{c['complaint'][:18]}…")
            run = run_once(c["complaint"], use_react=False, retriever_mode=None,
                           backend=backend, keep_result=True)
            rows.append({
                "record_id": c["record_id"], "complaint": c["complaint"],
                "ok": run["ok"], "error": run["error"], "elapsed_s": run["elapsed_s"],
                **({"result": run.get("result")} if keep_raw_results else {}),
                "llm_calls": run["llm_calls"],
                "metrics": metrics_from_result(run.get("result")),
            })
    return rows


def _formula_ontology_hit_rate(rows: list[dict], *, content_valid: bool, ontology=None) -> dict | None:
    """R61 §4：B→C 的净贡献不能只靠"看两条主诉肉眼觉得像不像真方"——
    真机实测 B 组给出「疏肝和胃汤」「温中健脾和胃方」这类不存在的方名（按
    治法现凑的字），C 组给出「柴胡疏肝散加减」「黄芪建中汤加减」这类真实
    存在的经典方加减，这个差异**可以量化**：方名（去掉"加减/加味"这类
    化裁后缀，见 `_split_formula_suffix`）能不能在方剂本体（235 首，
    `core.ontology.get_ontology().formulas`）里查到。B/C 两组都算这个数，
    报告里并排放，"模型是在编方名还是在真方基础上加减"就不再是定性描述。

    **本体不可用**（`ontology.available` False，沙盒/新 clone 的常态）时
    返回 `None`——不强行算出一个"0% 命中"，那会被误读成"这一组编的方名
    特别多"，实际是"本体压根不在，谁也查不了"，跟 `rule_refs_applicable`
    区分"不适用"与"0"是同一条诚实约束。"""
    if not content_valid:
        return None
    from core.ontology import get_ontology

    ont = ontology if ontology is not None else get_ontology()
    if not ont.available:
        return None
    names = [r["metrics"]["formula"] for r in rows
            if r.get("ok") and r["metrics"].get("has_output") and r["metrics"].get("formula")]
    if not names:
        return None
    hits = sum(1 for name in names if ont.formula(_split_formula_suffix(name)[0]) is not None)
    return _rate(hits, len(names))


def aggregate(rows: list[dict], group: R57Group, *, content_valid: bool, ontology=None) -> dict:
    ok = [r for r in rows if r.get("ok")]
    with_output = [r for r in ok if r["metrics"].get("has_output")]
    grounded = [r["metrics"]["herbs_grounded_ratio"] for r in with_output
               if r["metrics"].get("herbs_grounded_ratio") is not None]
    first_pass = [bool(r["metrics"]["verifier_first_pass"]) for r in with_output
                  if r["metrics"].get("verifier_first_pass") is not None]
    completeness = [r["metrics"]["rule_refs_completeness"] for r in with_output
                    if r["metrics"].get("rule_refs_completeness") is not None]
    calls = [r["llm_calls"] for r in ok if isinstance(r.get("llm_calls"), (int, float))]
    wall = [r["elapsed_s"] for r in ok if isinstance(r.get("elapsed_s"), (int, float))]
    hallu_runs = sum(1 for r in with_output if r["metrics"].get("n_hallucinated_ids"))
    note = None if content_valid else "假后端：内容指标不出数（产出是固定假文本）"
    # R59：rule_refs 是否适用不能只看 S3_MODE——B 组也走 derived，但
    # THEORY_LAYER=off 时演绎链上根本没有规则可引，"完整率" 在这一组结构上
    # 未定义（不是模型不够好，是这一组的设计就不给它可引的规则）。B 组之前
    # 被算成跟 C/D 同一档"适用"，实测出现过"B=1.0、C=0.375"这种反直觉的
    # 数字——不是 C 比 B 差，是 B 那个 1.0 本身没有意义（真机探针报的原始
    # bug，见 SOURCES.md）。判据跟 A 组同一条：结构上不适用就标 False，
    # 不强行算出一个数字。
    rule_refs_applicable = (group.env["S3_MODE"] == "derived"
                            and group.env.get("THEORY_LAYER") != "off")
    if group.env["S3_MODE"] != "derived":
        rule_refs_note = ("这一组走 S3_MODE=structured，没有 rule_refs/"
                          "derivation_completeness_ratio 这个键——不适用，不是 0")
    elif group.env.get("THEORY_LAYER") == "off":
        rule_refs_note = ("这一组 THEORY_LAYER=off，演绎链上没有医理规则可引"
                          "（prompt 里根本不摆规则表）——rule_refs 完整率在这一组"
                          "结构上未定义，不适用，不是满分也不是零分")
    else:
        rule_refs_note = None
    return {
        "n_queries": len(rows), "n_ok": len(ok), "n_with_output": len(with_output),
        "content_metrics_valid": content_valid, "content_note": note,
        "herbs_grounded_ratio_mean": (
            {"value": round(statistics.fmean(grounded), 4), "n": len(grounded)}
            if (grounded and content_valid) else None),
        "verifier_first_pass_rate": (
            _rate(sum(first_pass), len(first_pass)) if (first_pass and content_valid) else None),
        "rule_refs_completeness_rate": (
            {"value": round(statistics.fmean(completeness), 4), "n": len(completeness)}
            if (completeness and content_valid and rule_refs_applicable) else None),
        "rule_refs_applicable": rule_refs_applicable,
        "rule_refs_note": rule_refs_note,
        "hallucination_rate": (_rate(hallu_runs, len(with_output)) if content_valid else None),
        "formula_ontology_hit_rate": _formula_ontology_hit_rate(
            rows, content_valid=content_valid, ontology=ontology),
        "llm_calls_mean": (round(statistics.fmean(calls), 3) if calls else None),
        "elapsed_s_mean": (round(statistics.fmean(wall), 3) if wall else None),
    }


def pair_consistency(rows_a: list[dict], rows_b: list[dict], *, content_valid: bool) -> dict:
    """两组**按同一条主诉配对**比证型/治法/主方——**不合成一个单一布尔**
    （R61 §1，替换了 R57 原来的实现）。这个函数不知道、也不关心自己在跟谁
    比：`build_report` 既用它比 C 与 D（R54"绝不回头改推导"这条不变式在
    消融层面的验证），也用它比 C 与 E（`spec.NOISE_FLOOR_PAIR`，C 组的
    噪声地板复测）——同一份比较逻辑，不因为对象不同另写一套。

    **为什么不能再判"三项都相等才算一致"**：用户真机实测，q1 在 C/D 两组
    的 `syndrome`（肝胃气滞证）与 `formula`（柴胡疏肝散加减）字面完全相同，
    整条记录仍被判"不一致"，唯一的原因是 `method`（治法）措辞不同——
    "疏肝理气，和胃止痛" vs "疏肝解郁，理气和胃"。`method` 是自由文本，
    两次独立采样几乎不可能逐字相同，逐字相等在这上面测的是**采样方差**，
    不是"结论变了没变"——这是 CLAUDE.md 已经记两次的教训（覆盖检查比字面
    子串、分歧度比证型名字面相等）第三次在新地方出现，判据是"这个判断此前
    有没有人做过"，不是"这次的实现有没有 bug"。

    三项因此**分开判、分开报**，用各自能承受的比较方式：
      - `syndrome`：受控词表，逐字相等有意义，报命中率
      - `formula`：主方名，归一后（`_normalize_formula_for_comparison`
        统一"加减/加味"后缀）逐字相等，报命中率——化裁后缀的近义写法不该
        被判成"选了不同的方"
      - `method`：自由文本，逐字相等没有意义，报字符级 Jaccard **相似度**
        均值（`_char_jaccard`），不报"相等/不相等"这种布尔

    `pairs` 里带每条配对的三项原文，供报告并排列出（R61 §6 要求的
    "q1 两组三项原文并排"），不是只报一个汇总数字。"""
    if not content_valid:
        return {"syndrome": None, "formula": None, "method_similarity": None,
                "n_shared": 0, "pairs": [],
                "note": "假后端：两组结论恒同一份固定假文本，这些数字没有意义"}
    by_record_a = {r["record_id"]: r for r in rows_a if r.get("ok")}
    by_record_b = {r["record_id"]: r for r in rows_b if r.get("ok")}
    shared = sorted(set(by_record_a) & set(by_record_b))
    syn_match = 0
    formula_match = 0
    method_sims: list[float] = []
    pairs = []
    for rid in shared:
        ma, mb = by_record_a[rid]["metrics"], by_record_b[rid]["metrics"]
        syn_a, syn_b = ma.get("syndrome"), mb.get("syndrome")
        formula_a, formula_b = ma.get("formula"), mb.get("formula")
        method_a, method_b = ma.get("method"), mb.get("method")
        syn_ok = bool(syn_a) and syn_a == syn_b
        norm_a = _normalize_formula_for_comparison(formula_a)
        norm_b = _normalize_formula_for_comparison(formula_b)
        formula_ok = bool(norm_a) and norm_a == norm_b
        sim = _char_jaccard(method_a, method_b)
        if syn_ok:
            syn_match += 1
        if formula_ok:
            formula_match += 1
        if sim is not None:
            method_sims.append(sim)
        pairs.append({
            "record_id": rid,
            "syndrome": {"a": syn_a, "b": syn_b, "match": syn_ok},
            "formula": {"a": formula_a, "b": formula_b, "match": formula_ok},
            "method": {"a": method_a, "b": method_b, "similarity": sim},
        })
    return {
        "syndrome": _rate(syn_match, len(shared)),
        "formula": _rate(formula_match, len(shared)),
        "method_similarity": (
            {"value": round(statistics.fmean(method_sims), 4), "n": len(method_sims)}
            if method_sims else None),
        "n_shared": len(shared),
        "pairs": pairs,
    }


def _gate(name: str, ok: bool | None, detail: str) -> dict:
    return {"name": name, "passed": ok, "detail": detail}


def _too_small(*sizes: int | None) -> bool:
    """R59：任一份样本量缺失或低于 `GATE_MIN_SAMPLE_SIZE` 就判"样本不足"。
    `--limit 2` 这类探针跑出来的 1/1、2/2 不该被拿去跟阈值比——那不是
    "测出来通过了"，是"根本没测够"。R61 抽到模块级：`_relative_consistency_gate`
    也要用同一条判据，不因为噪声地板对照是新加的就另写一遍。"""
    return any(s is None or s < GATE_MIN_SAMPLE_SIZE for s in sizes)


def _relative_consistency_gate(label: str, cd: dict | None, noise: dict | None,
                               *, margin: float) -> dict:
    """R61 §1.3：C-D 一致率不跟绝对值比，跟"同配置复测"的噪声地板比——
    `cd`/`noise` 都是 `_rate()`（syndrome/formula）或均值（method_similarity）
    出来的 `{"value", "n"[, "denominator"]}` 形状。`cd 的值 ≥ noise 的值
    − margin` 才算过：C-D 的分歧不能明显比"两次独立采样、什么旋钮都没动"
    还大。噪声地板本身没测出来（没跑 E 组，或 E 组样本不足）就整体判不了
    （`None`），不能拿绝对阈值顶替——那正是这条闸门本来要移除的东西。"""
    cd_v = (cd or {}).get("value")
    noise_v = (noise or {}).get("value")
    cd_n = (cd or {}).get("denominator", (cd or {}).get("n"))
    noise_n = (noise or {}).get("denominator", (noise or {}).get("n"))
    if cd_v is None or noise_v is None:
        return _gate(label, None, "假后端或缺数据（没跑 E 组），量不到")
    if _too_small(cd_n, noise_n):
        return _gate(label, None,
                     f"样本不足（C-D n={cd_n}, 噪声地板 C-E n={noise_n}，"
                     f"都要 ≥{GATE_MIN_SAMPLE_SIZE}）——算出来的数字不代表任何东西")
    passed = cd_v >= noise_v - margin
    return _gate(label, passed,
                 f"C-D={cd_v}（n={cd_n}） vs 噪声地板 C-E={noise_v}（n={noise_n}），"
                 f"margin={margin}")


def build_report(rows_by_group: dict[str, list[dict]], *, backend_info: dict,
                 complaints: list[dict], content_valid: bool, ontology=None) -> dict:
    """`ontology` 默认惰性用 `core.ontology.get_ontology()`（CLAUDE.md 的既有
    约定）；测试注入一个 `available=False` 的替身，避免"零 LLM、零网络"的
    判定测试在跑的时候意外解析磁盘上几千行的真实药理层数据。"""
    groups = {k: aggregate(v, group_by_key(k), content_valid=content_valid, ontology=ontology)
             for k, v in rows_by_group.items()}
    cd_a, cd_b = CONSISTENCY_PAIR
    nf_a, nf_b = NOISE_FLOOR_PAIR
    c_vs_d = (pair_consistency(rows_by_group.get(cd_a, []), rows_by_group.get(cd_b, []),
                               content_valid=content_valid)
             if cd_a in rows_by_group and cd_b in rows_by_group else None)
    c_vs_noise = (pair_consistency(rows_by_group.get(nf_a, []), rows_by_group.get(nf_b, []),
                                   content_valid=content_valid)
                 if nf_a in rows_by_group and nf_b in rows_by_group else None)

    gates = []
    a_fp_d = groups.get("A", {}).get("verifier_first_pass_rate")
    c_fp_d = groups.get("C", {}).get("verifier_first_pass_rate")
    a_fp = (a_fp_d or {}).get("value")
    c_fp = (c_fp_d or {}).get("value")
    a_n = (a_fp_d or {}).get("denominator")
    c_n = (c_fp_d or {}).get("denominator")
    if content_valid and a_fp is not None and c_fp is not None and not _too_small(a_n, c_n):
        gates.append(_gate(
            f"C 组验证器一次过率 ≥ {GATE_C_VERIFIER_FIRST_PASS_VS} 组",
            c_fp >= a_fp, f"C={c_fp}（n={c_n}） vs A={a_fp}（n={a_n}）"))
    elif content_valid and a_fp is not None and c_fp is not None:
        gates.append(_gate(
            f"C 组验证器一次过率 ≥ {GATE_C_VERIFIER_FIRST_PASS_VS} 组", None,
            f"样本不足（A n={a_n}, C n={c_n}，都要 ≥{GATE_MIN_SAMPLE_SIZE}）——"
            "算出来的比率不代表任何东西，不能拿去跟阈值比"))
    else:
        gates.append(_gate(
            f"C 组验证器一次过率 ≥ {GATE_C_VERIFIER_FIRST_PASS_VS} 组", None,
            "假后端或缺数据，量不到"))
    c_comp_d = groups.get("C", {}).get("rule_refs_completeness_rate")
    c_completeness = (c_comp_d or {}).get("value")
    c_comp_n = (c_comp_d or {}).get("n")
    if content_valid and c_completeness is not None and not _too_small(c_comp_n):
        gates.append(_gate(
            f"C 组 rule_refs 完整率 ≥ {GATE_C_RULE_REFS_COMPLETENESS_MIN}",
            c_completeness >= GATE_C_RULE_REFS_COMPLETENESS_MIN,
            f"C={c_completeness}（n={c_comp_n}）"))
    elif content_valid and c_completeness is not None:
        gates.append(_gate(
            f"C 组 rule_refs 完整率 ≥ {GATE_C_RULE_REFS_COMPLETENESS_MIN}", None,
            f"样本不足（n={c_comp_n}，要 ≥{GATE_MIN_SAMPLE_SIZE}）"))
    else:
        gates.append(_gate(
            f"C 组 rule_refs 完整率 ≥ {GATE_C_RULE_REFS_COMPLETENESS_MIN}", None,
            "假后端或缺数据，量不到"))
    # R61：三项分开判——不再合成一个"C 与 D 一致率"布尔（理由见
    # `pair_consistency` 文档字符串），每一项各自跟 C-E 噪声地板比。
    gates.append(_relative_consistency_gate(
        "C-D 证型一致率不明显低于噪声地板",
        (c_vs_d or {}).get("syndrome"), (c_vs_noise or {}).get("syndrome"),
        margin=GATE_CD_CONSISTENCY_MARGIN))
    gates.append(_relative_consistency_gate(
        "C-D 主方一致率不明显低于噪声地板",
        (c_vs_d or {}).get("formula"), (c_vs_noise or {}).get("formula"),
        margin=GATE_CD_CONSISTENCY_MARGIN))
    gates.append(_relative_consistency_gate(
        "C-D 治法相似度不明显低于噪声地板",
        (c_vs_d or {}).get("method_similarity"), (c_vs_noise or {}).get("method_similarity"),
        margin=GATE_CD_CONSISTENCY_MARGIN))

    # B→C 只差一个旋钮（THEORY_LAYER off→on，医案两组都不进推导），所以这个差值
    # 就是"医理规则层单独带来的净提升"——R51 存在的理由，用户要求单独报一次。
    b_fp = (groups.get("B", {}).get("verifier_first_pass_rate") or {}).get("value")
    theory_layer_net_contribution = (
        round(c_fp - b_fp, 4)
        if isinstance(c_fp, (int, float)) and isinstance(b_fp, (int, float)) else None)

    # R61 §4：B→C 的净贡献不只是"验证器一次过率"这一个数——B 组编方名、
    # C 组真方加减这件事，量化成"方名能在方剂本体里查到的比例"，同一个
    # B→C 差值口径，跟上面那个并排报。
    b_hit = (groups.get("B", {}).get("formula_ontology_hit_rate") or {}).get("value")
    c_hit = (groups.get("C", {}).get("formula_ontology_hit_rate") or {}).get("value")
    theory_layer_formula_hit_delta = (
        round(c_hit - b_hit, 4)
        if isinstance(c_hit, (int, float)) and isinstance(b_hit, (int, float)) else None)

    # R59：三分判定，不是拿 True/False 硬凑。**⏳ 不许被悄悄算成通过**——
    # 之前的写法是先把 passed=None 的门过滤掉再对剩下的做 all()，两条门过、
    # 一条没测出来，会被判成"全过"（真机探针实测过这个假阳性）。正确顺序：
    # 先看有没有真的 ❌（任何一条不达标，总判定就是不达标，不管别的门测没测
    # 出来）；再看有没有 ⏳（没有 ❌ 但还有没测出来的，总判定是"判不了"，
    # 不是"过了"）；只有三条都 ✅ 才是 ✅。
    passed_values = [g["passed"] for g in gates]
    if any(v is False for v in passed_values):
        all_gates_passed = False
    elif any(v is None for v in passed_values):
        all_gates_passed = None
    else:
        all_gates_passed = True

    return {
        "kind": "ablation_r57",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": backend_info,
        "content_metrics_valid": content_valid,
        "n_queries": len(complaints),
        "complaints": complaints,
        "groups": {g.key: {"name": g.name, "env": g.env, "describe": g.describe(),
                           **groups[g.key]} for g in GROUPS if g.key in groups},
        "c_vs_d_consistency": c_vs_d,
        "c_vs_noise_floor": c_vs_noise,
        "b_to_c_verifier_first_pass_delta": theory_layer_net_contribution,
        "b_to_c_formula_ontology_hit_delta": theory_layer_formula_hit_delta,
        "gates": gates,
        "all_gates_passed": all_gates_passed,
        "rows": rows_by_group,
    }


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "⏳"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def _fmt_rate(d: dict | None, suffix: str = "") -> str:
    """R59：格式化一个 `{"value", "n"[, "denominator"]}` 形状的指标——**样本量
    永远跟比率一起亮出来**，不是只给一个孤零零的数字。有 `denominator` 的
    （`_rate()` 出来的命中率类：`n`=命中数、`denominator`=总数）显示
    "命中/总数"；没有的（均值类，`n` 本身就是样本数）显示"n=样本数"。
    样本量低于 `GATE_MIN_SAMPLE_SIZE` 时附一句"样本不足"——`1（1/1）`这种
    量级的数字看起来跟真测过没区别，必须在数字旁边就说清楚它靠不住。"""
    if not d or d.get("value") is None:
        return "⏳"
    value = d["value"]
    text = f"{value:g}{suffix}" if isinstance(value, float) else f"{value}{suffix}"
    if "denominator" in d:
        sample_n = d["denominator"]
        detail = f"{d['n']}/{sample_n}"
    elif "n" in d:
        sample_n = d["n"]
        detail = f"n={sample_n}"
    else:
        return text
    if sample_n is None or sample_n < GATE_MIN_SAMPLE_SIZE:
        detail += "，样本不足"
    return f"{text}（{detail}）"


def _fmt_pair_cell(pair: dict, key: str) -> str:
    """report 里"配对明细"表的一个单元格：两组各自的原文，逐字相同就不用
    都印一遍。"""
    a, b = pair[key]["a"], pair[key]["b"]
    return f"「{a}」" if a == b else f"「{a}」 vs 「{b}」"


def to_markdown(report: dict) -> str:
    lines = ["# R57 消融：五组 × 六指标——证明「不靠模仿也能推」", ""]
    b = report.get("backend") or {}
    lines.append(f"后端 `{b.get('id')}`（{b.get('model')}），{report.get('n_queries')} 条"
                 "脾胃门主诉（SDT Train）。硬指标要求有效样本 "
                 f"≥{GATE_MIN_SAMPLE_SIZE}，低于这个数一律判「样本不足」，"
                 "不当作测出来了。E 组是 C 组的噪声地板复测（配置逐一相同），"
                 "不是第五种实验条件。")
    if not report.get("content_metrics_valid"):
        lines.append("")
        lines.append("> ⚠ **这一份是假后端跑的**：内容指标（带本体出处占比、验证器一次过率、"
                     "rule_refs 完整率、幻觉率、方名本体命中率、C/D 一致率）一律不出数，"
                     "表里是 ⏳。能读的只有管道本身：各组是否各自设对了环境变量、跑通了"
                     "没有报错。")
    lines += ["", "| 组 | 医理层/医案/事后佐证 | 药味占比 | 验证器一次过 | rule_refs 完整率 "
                  "| 方名本体命中率 | 幻觉率 | 调用数 | 墙钟 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for g in GROUPS:
        row = (report.get("groups") or {}).get(g.key)
        if not row:
            continue
        fp = row.get("verifier_first_pass_rate")
        rr = row.get("rule_refs_completeness_rate")
        rr_cell = "不适用" if not row.get("rule_refs_applicable", True) else _fmt_rate(rr)
        hr = row.get("hallucination_rate")
        hit = row.get("formula_ontology_hit_rate")
        lines.append(
            f"| {g.key} {g.name} | {g.describe().split('：', 1)[-1]} "
            f"| {_fmt_rate(row.get('herbs_grounded_ratio_mean'))} | {_fmt_rate(fp)} "
            f"| {rr_cell} | {_fmt_rate(hit)} | {_fmt_rate(hr)} "
            f"| {_fmt(row.get('llm_calls_mean'))} | {_fmt(row.get('elapsed_s_mean'), ' s')} |")
    # R59：表格单元格里放不下长解释，"不适用"这三个字为什么不适用（A 组是
    # structured 没这个键、B 组是 THEORY_LAYER=off 没规则可引，两组理由不同）
    # 单独列在表下面——不让读者拿着一张"两组都写不适用"的表去猜两组是不是
    # 同一个原因。
    notes = [(g.key, g.name, (report.get("groups") or {}).get(g.key, {}).get("rule_refs_note"))
            for g in GROUPS]
    notes = [(k, n, note) for k, n, note in notes if note]
    if notes:
        lines.append("")
        for k, n, note in notes:
            lines.append(f"- rule_refs「不适用」（{k} {n}）：{note}")

    b_hit = (report.get("b_to_c_formula_ontology_hit_delta"))
    if b_hit is not None:
        lines.append("")
        lines.append(f"**B→C 方名本体命中率净提升**：{b_hit:+.4f}——"
                     "B 组按治法现凑方名，C 组从经典方出发加减，这是医理规则层"
                     "带来的净贡献第二个可统计指标（第一个是验证器一次过率，见下）。")

    # R61：C-D 一致率不再合成一个布尔，三项（证型/主方/治法）分开报，
    # 每项都跟 C-E 噪声地板并排列出——单看 C-D 的数字看不出"这个分歧是不是
    # 正常的采样噪声"，必须跟地板比才有意义。
    lines += ["", "## C-D 一致率 vs C-E 噪声地板"]
    c_vs_d = report.get("c_vs_d_consistency") or {}
    c_vs_noise = report.get("c_vs_noise_floor") or {}
    if c_vs_d.get("note"):
        lines.append(f"⏳ {c_vs_d['note']}")
    else:
        lines += ["", "| 维度 | C-D | C-E（噪声地板） |", "|---|---|---|"]
        for key, label in (("syndrome", "证型一致率"), ("formula", "主方一致率"),
                          ("method_similarity", "治法相似度（字符级 Jaccard 均值）")):
            lines.append(f"| {label} | {_fmt_rate(c_vs_d.get(key))} "
                        f"| {_fmt_rate(c_vs_noise.get(key))} |")
        # §6 要求：把不一致的配对原文并排列出来，不是只报一个汇总数字。
        mismatched = [p for p in c_vs_d.get("pairs") or []
                     if not p["syndrome"]["match"] or not p["formula"]["match"]]
        if mismatched:
            lines += ["", f"不一致的 {len(mismatched)} 条（C vs D，三项原文并排）：", ""]
            for p in mismatched[:10]:
                lines.append(f"- **{p['record_id']}**")
                lines.append(f"  - 证型：{_fmt_pair_cell(p, 'syndrome')}")
                lines.append(f"  - 主方：{_fmt_pair_cell(p, 'formula')}")
                sim = p["method"]["similarity"]
                lines.append(f"  - 治法（相似度 {sim if sim is not None else '⏳'}）："
                            f"「{p['method']['a']}」 vs 「{p['method']['b']}」")

    lines += ["", "## 硬指标"]
    for g in report.get("gates") or []:
        mark = "⏳" if g["passed"] is None else ("✅" if g["passed"] else "❌")
        lines.append(f"- {mark} {g['name']}（{g['detail']}）")
    overall = report.get("all_gates_passed")
    lines.append("")
    lines.append("**总判定**：" + ("⏳ 还没有真机数，判不了" if overall is None
                                 else ("✅ 全过" if overall else "❌ 至少一条没过——回 R51 补规则")))
    for g in GROUPS:
        lines.append(f"- **{g.describe()}**")
    return "\n".join(lines) + "\n"


def load_complaints(sdt_dir: Path | None, queries_path: str | None, limit: int,
                    *, n: int = 20) -> list[dict]:
    """`--sdt-dir`/`--queries-path` 二选一挑主诉——**唯一实现**，`main()` 与
    `scripts/diagnose_r57_group.py`（诊断某一组用同一批主诉重跑）共用，不各自
    抄一份。失败或一条都没挑到时 `raise SystemExit(2)`，不返回空列表让调用方
    自己判断——CLAUDE.md「同一概念只有一处实现」在 CLI 参数解析这一层的应用。
    """
    if sdt_dir is None and queries_path is None:
        raise SystemExit("需要 --sdt-dir（自动挑脾胃门 20 条）或 --queries-path"
                         "（自备主诉文件）之一")

    if sdt_dir is not None:
        try:
            complaints = select_pi_wei_men_complaints(sdt_dir, n=n)
        except Exception as e:  # noqa: BLE001 - 数据集缺失/格式不对都要说清楚，不崩栈
            raise SystemExit(f"从 --sdt-dir 挑主诉失败：{type(e).__name__}: {e}") from e
    else:
        qpath = Path(queries_path)
        if not qpath.exists():
            raise SystemExit(f"主诉文件不在：{qpath}")
        lines = [ln.strip() for ln in qpath.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
        complaints = [{"record_id": f"q{i}", "syndrome": None, "complaint": c}
                     for i, c in enumerate(lines, 1)]
    if limit > 0:
        complaints = complaints[:limit]
    if not complaints:
        raise SystemExit("一条主诉都没读到")
    return complaints


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sdt-dir", type=Path, default=None,
                    help="TCMEval-SDT 本地路径。给了就自动按脾胃门筛 20 条 Train 主诉")
    ap.add_argument("--queries-path", default=None,
                    help="备选：一份纯文本主诉文件（一行一条），不依赖 SDT 数据集")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--backend", default="fake", choices=["fake", "real"])
    ap.add_argument("--groups", default="ABCDE",
                    help="跑哪几组，默认全跑（E 是 C 组的噪声地板复测，R61 新增）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--md", default=None)
    ap.add_argument("--no-warmup", dest="warmup", action="store_false")
    ap.set_defaults(warmup=True)
    args = ap.parse_args(argv)

    try:
        complaints = load_complaints(args.sdt_dir, args.queries_path, args.limit)
    except SystemExit as e:
        print(e.code, file=sys.stderr)
        return 2

    from scripts.bench_consult import (
        AUTO_FAKE_CASES_PER_PHYSICIAN, build_backend, install_fake_cases,
    )
    from core.retrieval import cases_available

    backend = build_backend(args.backend, 0.0, False)
    content_valid = args.backend == "real"
    if args.backend == "fake" and not cases_available():
        install_fake_cases(AUTO_FAKE_CASES_PER_PHYSICIAN)

    if args.warmup:
        print("— 预热（这一次的数不计入任何一组）")
        try:
            run_group(GROUPS[0], complaints[:1], backend)
        except Exception as e:  # noqa: BLE001
            print(f"  预热失败（继续往下跑）：{type(e).__name__}: {e}", file=sys.stderr)

    t0 = time.perf_counter()
    rows_by_group: dict[str, list[dict]] = {}
    for key in args.groups.upper():
        group = group_by_key(key)
        print(f"— {group.describe()}")
        rows_by_group[group.key] = run_group(
            group, complaints, backend, progress=lambda line: print(line))
    report = build_report(
        rows_by_group,
        backend_info={"id": backend.backend_id(), "model": backend.model_name(),
                      "comparability_warning": backend.comparability_warning()},
        complaints=complaints, content_valid=content_valid)
    report["wall_s"] = round(time.perf_counter() - t0, 2)
    report["warmup"] = bool(args.warmup)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    md = Path(args.md) if args.md else out.with_suffix(".md")
    md.write_text(to_markdown(report), encoding="utf-8")
    print(to_markdown(report))
    print(f"→ {out}\n→ {md}")
    n_fail = sum(1 for rows in rows_by_group.values() for r in rows if not r["ok"])
    if n_fail:
        print(f"✗ {n_fail} 次问诊失败（详见 rows[].error）", file=sys.stderr)
    if content_valid and report.get("all_gates_passed") is False:
        print("✗ 至少一条硬指标没过——回 R51 补规则，不许把医案放回推导相凑数",
             file=sys.stderr)
        return 1
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
