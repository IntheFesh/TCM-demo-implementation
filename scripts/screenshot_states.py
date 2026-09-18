"""R14：问诊页五种状态的真浏览器验收（docs/DESIGN.md §3.1 状态设计表）。

## 为什么不能只靠 node 测试

`tests/test_consult_layout.py` 测的是纯函数拼出来的 HTML 字符串——它证明不了
CSS 有没有把三列排成三列、`.state-first` 有没有真的把输入区居中、整页替换之后
DOM 里还剩什么。CLAUDE.md 那条硬约定（"涉及图层结构变更时 Playwright 是必需的
验收环节"）的理由在 M5 已经付过一次代价：后端 JSON 全对，前端 `nodesByLayer`
少初始化一个 key，JSON 结构测试根本不会调用渲染代码。

## 为什么不用真实 LLM 跑出这五种状态

跑一次真问诊要分钟级和真钱，而这五种状态的差别**完全在前端**：后端只是给出
不同形状的响应体。所以这里在页面里直接调它自己的渲染函数，喂五份构造好的
响应体——测的仍然是上线那份 `app.js` + 上线那份 `app.css` + 真的浏览器排版，
只是不花钱去要一份后端早就有确定形状的 JSON。

## 判据（不只是截图）

截图是给人看的，退出码是给机器看的。每种状态都带一条 DOM 断言，其中最硬的
一条是安全拦截：**整页替换之后 DOM 里不许还有 `herb` / `formula`**。
"面上看不见"和"DOM 里没有"是两件事，一次「检查元素」就能把前者拆穿。

    python -m scripts.screenshot_states
    python -m scripts.screenshot_states --only blocked
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from core.chain import SYNTHESIS_PHYSICIAN_NAME
from scripts.screenshot_ui import _chromium_path, _free_port, _wait_ready

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}

# 个别状态要在**更小的屏**上验。rings 那条"标签可读"的承诺是对最小的那块屏
# 讲的（1280×800 的投影仪），在 1440×900 上验等于放过一批在投影仪上糊掉的布局。
# 只覆盖需要的那几个，其余仍用统一视口——截图之间的可比性靠这一点。
# R37：单链九段要在**三种分辨率**上验（任务书原文）。1920×1080 是评审大屏、
# 1366×768 是会议室笔记本（竖向最紧的那个）、1280×800 是投影仪。
# 只验一种等于放过另外两种上的折行与压字。
VIEWPORT_OVERRIDES = {
    "rings": {"width": 1280, "height": 800},
    "chain_flow_1920": {"width": 1920, "height": 1080},
    "chain_flow_1366": {"width": 1366, "height": 768},
    "chain_flow_1280": {"width": 1280, "height": 800},
    # R42：释义抽屉的断点是 768px（iPad 竖屏，也是三甲最常见的移动查房设备）。
    # **在 1440 宽上验等于什么都没验**——那时它还是那张卡。
    "node_explain_drawer": {"width": 768, "height": 1024},
}

COMPLAINT = "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。"

# 每种状态属于哪一轮。截图文件名带轮次前缀是为了"这张图是哪一轮的验收物"
# 一眼可查——R14 的五种状态和 R15 的三种角色形态在同一个目录里。
PREFIX = {
    "first": "r14", "running": "r14", "insufficient": "r14",
    "followup": "r14", "blocked": "r14", "done": "r14",
    "patient": "r15", "patient_high": "r15", "doctor_conflict": "r15",
    "student_highlight": "r15",
    "consult_graph": "r16", "browser_home": "r16", "browser_expanded": "r16",
    "topbar_byok": "r17", "demo_mode": "r17",
    "reference_physicians": "r18",
    "epigraph": "r24", "select_open": "r24",
    "structured_single": "r33",
    "chain_flow": "r37", "chain_flow_1920": "r37", "chain_flow_1366": "r37",
    "chain_flow_1280": "r37", "chain_running": "r37", "node_explain": "r37",
    "cancel_button": "r37",
    "single_chain_graph": "r37",
    # R46：临床闭环的四块新界面。
    "intake_form": "r46", "guideline_compare": "r46",
    "knowledge_panel": "r46", "emr_draft": "r46",
    # R47：首次引导与帮助气泡。**跑在内部模式下**（跟其余 33 张一样）——
    # 这两块不是 internal-only，两种模式下长得一样，没必要再占一档分辨率循环。
    "onboarding": "r47", "help_popover": "r47",
    # R42：九层图 + 层名列头 + tooltip 钉住 + 窄屏抽屉 + 图谱浏览器聚焦
    "graph_layer_bands": "r42", "graph_tooltip_pinned": "r42",
    "node_explain_drawer": "r42", "browser_focus": "r42",
    "agent_trace": "r44",
    "advice_panel": "r24", "rings": "r24",
}


def _herb(name, role, dose):
    return {"name": name, "role": role, "dose": dose, "dose_unit": "g",
            "processing": None, "decoction": None, "function_in_formula": None}


def _result(pid, name, syndrome, principle, formula, herbs, items):
    return {
        "physician": pid, "physician_name": name, "color": None,
        "s3": {
            "syndrome": syndrome, "treatment_principle": principle, "formula": formula,
            "herbs": herbs, "reasoning": "依据检索到的医案，" + principle,
            "cited_case_ids": [f"{pid}-0001-p0-0"], "note": None, "western_drugs": [],
            "selected": 0,
            "formula_candidates": [{"name": formula, "rationale": "与本证相合",
                                    "herb_items": items}],
        },
        "refs": [{"case_id": f"{pid}-0001-p0-0", "visit_label": "初诊", "score": "0.81",
                  "symptoms": ["胃脘痛", "嗳气"], "syndrome": syndrome,
                  "excerpt": "脘痛嗳气，脉弦，用疏肝和胃法。"}],
        "hallucinated": [], "safety_output": {}, "react_trace": None,
        "no_reference_cases": False,
    }


RESULTS = [
    _result("ye_tianshi", "叶天士", "胃痛 · 肝胃不和证", "疏肝理气，和胃止痛", "柴胡疏肝散加减",
            ["柴胡", "白芍", "香附", "陈皮", "枳壳", "川芎", "甘草"],
            [_herb("柴胡", "君", 6), _herb("白芍", "臣", 12), _herb("香附", "臣", 9),
             _herb("陈皮", "佐", 9), _herb("枳壳", "佐", 9), _herb("川芎", "佐", 6),
             _herb("甘草", "使", 3)]),
    _result("wu_jutong", "吴鞠通", "胃痛 · 肝胃气滞证", "苦辛通降，和胃制酸", "左金丸合金铃子散",
            ["黄连", "吴茱萸", "川楝子", "延胡索", "甘草"],
            [_herb("黄连", "君", 3), _herb("吴茱萸", "臣", 1), _herb("川楝子", "臣", 9),
             _herb("延胡索", "佐", 9), _herb("甘草", "使", 3)]),
    _result("zhang_xichun", "张锡纯", "胃痛 · 肝气犯胃证", "降逆平肝，和胃安中", "旋覆代赭汤加减",
            ["生赭石", "旋覆花", "清半夏", "生山药", "甘草"],
            [_herb("生赭石", "君", 18), _herb("旋覆花", "臣", 9), _herb("清半夏", "臣", 9),
             _herb("生山药", "佐", 15), _herb("甘草", "使", 3)]),
]

DIVERGENCE = {
    "same": False, "method": "nway_jaccard+pairwise",
    "herb_jaccard": 0.88, "shared_herbs": ["甘草"],
    "unique_herbs": {
        "ye_tianshi": ["柴胡", "白芍", "香附", "陈皮", "枳壳", "川芎"],
        "wu_jutong": ["黄连", "吴茱萸", "川楝子", "延胡索"],
        "zhang_xichun": ["赭石", "旋覆花", "半夏", "山药"],
    },
    "core_jaccard": 1.0, "adjunct_jaccard": 0.9,
    "shared_core_herbs": [], "shared_adjunct_herbs": ["甘草"],
    "n_unroled": {"ye_tianshi": 0, "wu_jutong": 0, "zhang_xichun": 0},
    "layer_note": "core_jaccard/adjunct_jaccard 为 null 表示这一层没有可比数据",
    "pairs": [], "pairs_mean": 0.53, "lineage_mean": 0.48, "cross_school_mean": 0.56,
    "n_lineage_pairs": 1, "n_cross_school_pairs": 2, "cross_school_gt_lineage": True,
    "treatment_principle_same": False, "western_drug_overlap": None,
    "epsilon_online": 0.2611, "epsilon_core": 0.19, "epsilon_adjunct": 0.31,
    "epsilon_for_query": {"value": 0.3954, "scope": "query"},
}

GRAPH = {"nodes": [], "edges": [], "dropped_edges": 0}

# 学生模式的三跳高亮要一张真有层次的图：症状 → 证素 → 证型 → 方剂。
# 节点少但层齐——高亮判据看的是"淡化了几个"，不是"图有多大"。
# ---------- R42：图的 fixture **由真后端生成，不再手写** ----------
#
# 这两张图（三列问诊图、学生模式高亮用的那张）原来是手抄的 dict。R42 把层数
# 从 5 改成 9 之后，手抄的那两份**跟真接口漂了**：判据在验一个不存在的形状
# （`elem::肝郁` / `layer <= 2` 这类），于是"真浏览器跑一遍"这件事的意义被抽空
# ——它跑的不是上线的那份数据。
#
# 所以改成调 `api.main.to_graph` 现造。代价是 fixture 不再能随手改一个字段来
# 造边界情况；收益是**它永远不会跟契约漂**，而这正是 CLAUDE.md 那条"改了层结构
# 必须过 Playwright"要的东西。要造边界情况就改喂进去的 S1/S2/S3，那也更接近真实。
def _three_physician_graph(role="researcher"):
    from api.main import to_graph
    from core.schemas import (ElementHit, FormulaCandidate, HerbItem, S1Normalize,
                              S2Elements, S3Syndrome)

    s1 = S1Normalize(symptoms=["胃脘胀痛", "口苦", "嗳气泛酸"])
    s2 = S2Elements(elements=[
        ElementHit(element="肝", kind="location",
                   supporting_symptoms=["胃脘胀痛"], confidence="high"),
        ElementHit(element="胃", kind="location",
                   supporting_symptoms=["嗳气泛酸"], confidence="high"),
        ElementHit(element="气滞", kind="nature",
                   supporting_symptoms=["胃脘胀痛"], confidence="high"),
        ElementHit(element="湿热", kind="nature",
                   supporting_symptoms=["口苦"], confidence="medium"),
    ])

    def _s3(syndrome, principle, formula, source, herbs):
        # `source="modified"` 必须带 base_formula（schema 的硬约束：不写原方就
        # 无法追溯改了什么）——fixture 也得守这条，不然它造的就不是合法响应。
        cand = {"name": formula, "source": source, "confidence": "high",
                "rationale": "主治" + syndrome,
                "herb_items": [HerbItem(name=n, role=r, dose=d, dose_unit="g")
                               for n, r, d in herbs]}
        if source == "modified":
            cand["base_formula"] = "柴胡疏肝散"
        return S3Syndrome(
            syndrome=syndrome, disease="胃痛", reasoning="肝气犯胃，胃失和降",
            treatment_principle=principle, cited_case_ids=["ye_tianshi-0001-p0-0"],
            formula_candidates=[FormulaCandidate(**cand)], selected=0)

    specs = [
        ("ye_tianshi", "叶天士", "肝胃不和证", "疏肝和胃", "柴胡疏肝散加减", "modified",
         [("柴胡", "君", 6.0), ("白芍", "臣", 12.0), ("甘草", "使", 3.0)]),
        ("wu_jutong", "吴鞠通", "肝胆湿热证", "清利肝胆湿热", "左金丸", "classic",
         [("黄连", "君", 3.0), ("吴茱萸", "臣", 1.0)]),
        ("zhang_xichun", "张锡纯", "肝气犯胃证", "降胃镇逆", "自拟和胃汤", "composed",
         [("生赭石", "君", 18.0), ("生姜", "佐", 6.0)]),
    ]
    results = [{"physician": pid, "physician_name": pname, "s2": s2,
                "s3": _s3(syn, prin, formula, source, herbs),
                "refs": [], "hallucinated": []}
               for pid, pname, syn, prin, formula, source, herbs in specs]
    return json.loads(json.dumps(to_graph(s1, results, s2, role=role)))


#: 三位医家的九层图。R42 之前叫 SIX_LAYER_GRAPH（手抄的五层）。
NINE_LAYER_GRAPH = _three_physician_graph()
def _disjoint_graph():
    """两条**互不相交**的链：胃脘胀痛→肝/气滞→肝胃不和证→…，
    口苦→湿热→肝胆湿热证→…。

    学生模式那条"点一个症状，它那条链全亮、另一条全淡"的判据需要这个形状，
    而上面那张三医家图给不了：证素层是**全局共享**的那一份，于是每位医家的
    证型都连回全部证素，从任一症状出发都能走到所有东西——一个节点都不会被淡化。

    做法是给每位医家**各自的 s2**（`to_graph` 画 证素→证型 那条边时读的正是
    `r["s2"]`），而全局 s2 仍然含两边的证素（症状层与证素层由它生成）。
    仍然是**真后端生成**，不是手抄。
    """
    from api.main import to_graph
    from core.schemas import (ElementHit, FormulaCandidate, HerbItem, S1Normalize,
                              S2Elements, S3Syndrome)

    gan = ElementHit(element="肝", kind="location",
                     supporting_symptoms=["胃脘胀痛"], confidence="high")
    shi = ElementHit(element="湿热", kind="nature",
                     supporting_symptoms=["口苦"], confidence="high")
    s1 = S1Normalize(symptoms=["胃脘胀痛", "口苦"])
    s2_all = S2Elements(elements=[gan, shi])

    def _s3(syndrome, principle, formula):
        return S3Syndrome(
            syndrome=syndrome, reasoning="…", treatment_principle=principle,
            cited_case_ids=["ye_tianshi-0001-p0-0"],
            formula_candidates=[FormulaCandidate(
                name=formula, source="classic", confidence="high",
                rationale="主治" + syndrome,
                herb_items=[HerbItem(name="柴胡", role="君", dose=6.0, dose_unit="g")])],
            selected=0)

    results = [
        {"physician": "ye_tianshi", "physician_name": "叶天士",
         "s2": S2Elements(elements=[gan]),
         "s3": _s3("肝胃不和证", "疏肝和胃", "柴胡疏肝散"),
         "refs": [], "hallucinated": []},
        {"physician": "wu_jutong", "physician_name": "吴鞠通",
         "s2": S2Elements(elements=[shi]),
         "s3": _s3("肝胆湿热证", "清利湿热", "龙胆泻肝汤"),
         "refs": [], "hallucinated": []},
    ]
    return json.loads(json.dumps(to_graph(s1, results, s2_all)))


#: 学生模式高亮用的那张（两条互不相交的链，见上）。
STUDENT_GRAPH = _disjoint_graph()
#: 患者模式的图**由后端按 role 生成**，不是在前端把方药层过滤掉
#: （M6 的原话："根本不生成"，不是"生成了再删"）。
PATIENT_GRAPH = _three_physician_graph(role="patient")


# 患者模式：后端在 role=patient 时摘掉 divergence、摘掉 s3 的方剂/药材字段、
# refs 清空，另外下发 triage / food_therapy / patent_medicines。这里照那个形状造。
PATIENT_PAYLOAD = {
    "results": [{"physician": r["physician"], "physician_name": r["physician_name"],
                 "s3": {"syndrome": r["s3"]["syndrome"],
                        "treatment_principle": r["s3"]["treatment_principle"],
                        "reasoning": r["s3"]["reasoning"], "cited_case_ids": [],
                        "note": None, "selected": 0},
                 "refs": [], "hallucinated": [], "safety_output": {"flagged": False}}
                for r in RESULTS],
    "graph": PATIENT_GRAPH,
    "rejected": False, "reject_reason": None, "retrieval_error": None,
    "insufficient": False, "insufficient_reason": None, "safety_flag": None,
    "followup": None, "residual": None, "demo_mode": None, "manifest": None,
    "triage": {"disease": "胃痛", "dept": "消化内科", "urgency": "medium",
               "red_flags": ["疼痛剧烈持续不缓解", "痛引肩背或颈部",
                             "伴冷汗、面色苍白", "呕血或解黑便"],
               "advice": "胃痛类症状，建议就诊消化内科，建议近期就诊"},
    "food_therapy": [], "patent_medicines": [],
}

# 紧急度高：食疗与中成药一律不给（闸门在服务端 _apply_medication_gate）。
PATIENT_HIGH_PAYLOAD = {
    **PATIENT_PAYLOAD,
    "triage": {**PATIENT_PAYLOAD["triage"], "disease": "胸痹", "dept": "心血管内科",
               "urgency": "high",
               "advice": "胸痹类症状需要提高警惕，建议就诊心血管内科，建议尽快就诊"},
}

# 医生模式：甘草 + 海藻 = 十八反，后端校验回这个形状。
DOCTOR_SAFETY = {
    "incompatible": [["甘草", "海藻"]],
    "dose_violations": [],
    "thermal_warning": None,
    "decoction_missing": [],
    "toxic_herbs": ["半夏"],
    "blocking": True,
}

DONE_PAYLOAD = {
    "results": RESULTS, "divergence": DIVERGENCE, "graph": GRAPH,
    "rejected": False, "reject_reason": None, "retrieval_error": None,
    "insufficient": False, "insufficient_reason": None, "safety_flag": None,
    "followup": None, "residual": None, "demo_mode": None,
    "manifest": {"model": "deepseek-v4-pro", "prompt_version": "v1",
                 "cases_sha256": "abc1234", "llm_calls": 6, "elapsed_ms": 88000},
}

# R33：结构化模式的终态 payload——**一个 result**，physician 是保留 id
# `synthesis`，`divergence` 是 None（一份结论没有"两两分歧"这回事）。
#
# **为什么必须有这一条 Playwright 状态。** CLAUDE.md 那条：涉及渲染层结构变更时
# 真实浏览器渲染是必需的验收环节。R33 把 results 从三个元素变成一个，
# 后端 JSON 测试全绿说明不了三列布局在只有一列时还成立——M5 那次
# 正是"数据对了、前端 nodesByLayer 漏了新 key"。
S33_RESULT = json.loads(json.dumps(RESULTS[0]))
S33_RESULT.update({
    "physician": "synthesis",
    # R44：名字从后端常量取，不手抄——改名时手抄的那份会漏。
    "physician_name": SYNTHESIS_PHYSICIAN_NAME,
    "color": "#3B4A6B",
    "school": None,
    "years": None,
    "physicians_cited": ["ye_tianshi", "li_ke"],
    "herbs_grounded_ratio": 0.0,
    "physician_influences": [
        {"physician": "ye_tianshi", "step": "formula",
         "contribution": "用药轻灵，剂量偏小", "cited_case_ids": ["ye_tianshi-0001-p0-0"]},
        {"physician": "li_ke", "step": "herbs",
         "contribution": "温阳一路敢用重剂", "cited_case_ids": ["ye_tianshi-0001-p0-0"]},
    ],
})
S33_DONE_PAYLOAD = {
    **json.loads(json.dumps(DONE_PAYLOAD)),
    "results": [S33_RESULT],
    # 一份结论没有两两分歧可言。**None 不是 0**——0 会被读成"五家完全一致"。
    "divergence": None,
    "manifest": {**DONE_PAYLOAD["manifest"], "s3_mode": "structured",
                 "llm_calls": 3,
                 "synthesis": {"physicians_available": 5,
                               "physicians_cited": ["ye_tianshi", "li_ke"],
                               "n_physicians_cited": 2,
                               "herbs_grounded_ratio": 0.0,
                               "n_ontology_refs": 0, "n_herbs": 6,
                               "chain_steps": ["organ", "syndrome", "method",
                                               "formula", "herbs"]}},
}

# ---------- R37：单链九段 + 单链图 的 fixture ----------
#
# **用真的 schema 类构造，不手写 JSON。** 手写的那份会跟 `core/schemas.py` 漂
# ——而这一轮要验的恰恰是"九段界面读得懂五步链的每一个字段"。跨步引用
# （from_organs / from_syndrome / from_method / herb_choices 的双向药名集合）
# 由 schema 自己校验，构造得出来就说明这份 fixture 是合法的五步链；
# 单链图同样由真的 `api.main.to_graph` 生成，不手摆节点。


def _r37_structured():
    from core.schemas import (
        ElementHit,
        FormulaCandidate,
        FormulaStep,
        HerbChoice,
        HerbItem,
        MethodStep,
        OntologyRef,
        OrganLocus,
        PhysicianInfluence,
        S1Normalize,
        S2Elements,
        S3Structured,
        SyndromeStep,
    )

    def it(n, role, dose):
        return HerbItem(name=n, role=role, dose=dose, dose_unit="g")

    items = [it("柴胡", "君", 12), it("白芍", "臣", 12), it("枳壳", "佐", 9),
             it("甘草", "使", 6)]
    st = S3Structured(
        organs=[
            OrganLocus(organ="肝", supporting_symptoms=["胃脘胀痛"],
                       pathogenesis="肝气郁结，横逆犯胃"),
            OrganLocus(organ="胃", supporting_symptoms=["嗳气泛酸"],
                       pathogenesis="胃失和降"),
        ],
        syndrome=SyndromeStep(name="肝胃不和证", disease="胃痛", from_organs=["肝", "胃"],
                              reasoning="脘痛随情志而发，肝气犯胃，胃失和降。",
                              reasoning_plain="情绪一紧张就胃痛胀气，是肝气不顺连累了胃。"),
        method=MethodStep(principle="疏肝理气，和胃止痛", from_syndrome="肝胃不和证",
                          targets=["肝", "胃"]),
        formula=FormulaStep(
            candidate=FormulaCandidate(
                name="柴胡疏肝散加减", source="modified", base_formula="柴胡疏肝散",
                rationale="与肝胃不和、气机郁滞相合", confidence="high", herb_items=items),
            from_method="疏肝理气，和胃止痛",
            ontology_refs=[OntologyRef(kind="formula", name="柴胡疏肝散", predicate="主治",
                                       span="肝气郁滞，胸胁胀痛", book="方剂学")]),
        herb_choices=[
            HerbChoice(item=items[0], for_element="肝", effect_cited="疏肝解郁",
                       ontology_refs=[OntologyRef(kind="herb", name="柴胡", predicate="功效",
                                                  span="疏肝解郁、和解表里", book="中药学")],
                       physician_source="ye_tianshi"),
            HerbChoice(item=items[1], for_element="肝", effect_cited="柔肝止痛"),
            HerbChoice(item=items[2], for_element="胃", effect_cited="理气和胃"),
            HerbChoice(item=items[3], for_element="胃", effect_cited="调和诸药"),
        ],
        physician_influences=[
            PhysicianInfluence(physician="ye_tianshi", step="formula",
                               contribution="用药轻灵，剂量偏小",
                               cited_case_ids=["ye_tianshi-0001-p0-0"]),
            PhysicianInfluence(physician="li_ke", step="herbs",
                               contribution="温阳一路敢用重剂",
                               cited_case_ids=["ye_tianshi-0001-p0-0"]),
        ],
        cited_case_ids=["ye_tianshi-0001-p0-0"])
    s1 = S1Normalize(symptoms=["胃脘胀痛", "嗳气泛酸", "纳差"],
                     tongue="淡红苔薄白", pulse="弦", unmapped=["三年前有胃病史"])
    s2 = S2Elements(elements=[
        ElementHit(element="肝", kind="location", supporting_symptoms=["胃脘胀痛"],
                   confidence="high"),
        ElementHit(element="胃", kind="location", supporting_symptoms=["嗳气泛酸"],
                   confidence="high"),
        ElementHit(element="气滞", kind="nature", supporting_symptoms=["胃脘胀痛"],
                   confidence="medium"),
    ], unexplained_symptoms=["纳差"])
    return st, s1, s2


def _r37_payload():
    from api.main import _serialize_followup, to_graph
    from core.formula_verifier import Unverifiable, VerificationResult
    from core.schemas import FollowupResult

    st, s1, s2 = _r37_structured()
    flat = st.to_s3_syndrome()
    # **`s3_structured` 必须一起传**：`flat` 是扁平化之后的那一份，病机与治法
    # 两层的原件只在结构化那一份里（见 api.main.to_graph 里那段注释）。
    graph = to_graph(s1, [{"physician": "synthesis",
                           "physician_name": SYNTHESIS_PHYSICIAN_NAME,
                           "s3": flat, "s3_structured": st, "s2": s2}], s2=s2)
    result = {
        **json.loads(json.dumps(S33_RESULT)),
        "s3": json.loads(flat.model_dump_json()),
        "s3_structured": json.loads(st.model_dump_json()),
        "physician_influences": [json.loads(i.model_dump_json())
                                 for i in st.physician_influences],
        "physicians_cited": ["ye_tianshi", "li_ke"],
        "herbs_grounded_ratio": st.herbs_grounded_ratio(),
        # R34 的验证结果。**`unverifiable` 非空**：九段界面必须把"判不了"显示出来
        # （查不到依据 ≠ 查到了且通过），这条 fixture 就是为了钉住那一行。
        # **用真的 VerificationResult 序列化，不手写这个 dict**：手写的那一版
        # 漏掉了 `rule_label` / `status_label`（后端随结论下发的中文名），
        # 而截图判据查的正是"页面上不许出现 id"——fixture 自己先跟真接口漂了，
        # 判据就在验一个不存在的形状。
        "verification": VerificationResult(
            unverifiable=(
                Unverifiable(rule="meridian_coverage", herbs=("枳壳",),
                             missing_predicate="归经", reason="枳壳 在本体里没有归经条目"),
                Unverifiable(rule="nature_conflict", herbs=("甘草",),
                             missing_predicate="性味", reason="甘草 在本体里没有性味条目"),
            ),
            ontology_available=True,
            checked_rules=("incompatible_pair", "dose_exceeds", "herb_grounded"),
        ).to_dict(),
        "verifier_metrics": {"revise_rounds": 1, "first_pass_status": "revise_needed"},
    }
    return {
        **json.loads(json.dumps(S33_DONE_PAYLOAD)),
        "results": [result],
        "graph": graph,
        "s1": json.loads(s1.model_dump_json()),
        "s2": json.loads(s2.model_dump_json()),
        # 同 `verification` 那条：**用真的序列化函数造**，不手写 dict——
        # 手写的那份缺 `stopped_by_label`（后端随结果下发的中文名），
        # 而截图判据查的正是"页面上不许出现 id"。
        "followup": _serialize_followup(FollowupResult(
            rounds=1, stopped_by="max_rounds", asserted=["善叹息"], denied=["口苦"])),
    }


R37_DONE_PAYLOAD = _r37_payload()

# R47：这几份手写的响应体要带 `record_id`——真实的 `_consult_response` 每次
# 都下发它（页脚那一行「本次记录编号」）。fixture 里缺了它，产品模式截图的
# 判据就会红在"页脚没有记录编号"上，而那是 fixture 过时、不是页面写错了。
# 固定值而不是随机：截图要可复现，两次跑出来的 PNG 不该只差一个编号。
for _payload in (PATIENT_PAYLOAD, PATIENT_HIGH_PAYLOAD, DONE_PAYLOAD,
                 S33_DONE_PAYLOAD, R37_DONE_PAYLOAD):
    _payload.setdefault("record_id", "K7M3QX92")


def _agent_trace():
    """四条决策各一类。**用真的 `AgentTrace` 造，不手写 dict**——手写的那份
    会漏掉后端随决策一起下发的中文名（`capability_label` / `stop_kind_label`），
    而截图判据查的正是"界面上显示的是中文名不是 id"。"""
    from core.agent import AgentTrace

    tr = AgentTrace()
    tr.record("ask_for_missing_symptoms", "问了 2 轮，确认 1 条、否认 1 条；停因：信息增益不足")
    tr.record("gather_evidence", "取证 3 步（1 条推理链）")
    tr.record("verify_and_revise", "验了 1 份处方，重开 1 次")
    tr.record("symbolic_veto", "甘草 与 甘遂 属配伍禁忌")
    return tr.to_list()


AGENT_TRACE = _agent_trace()


# R46：循证对照 / 个体化 / 病历文书的 fixture。**用真模块算出来，不手捏 dict**
# ——手捏的那份会在字段改名之后继续绿，而界面早就读不到了（这正是 R44
# 「改一个显示名之前先确认它只写了一处」那条教训的同一个形状）。
def _r46_guideline():
    from core.guideline_compare import compare, load_guidelines

    e = load_guidelines()[0]
    # 主方给一个不同的，好让 aligned 与 deviations 都非空——截图要能同时
    # 看到"一致"和"不一致"两种条目长什么样。
    return compare(e.syndrome, e.recommended_principle, "一个不在教材里的方", [])


def _r46_individualization():
    from core.individualize import individualize
    from core.schemas import PatientProfile

    return individualize(
        PatientProfile(age_years=68, sex="女", life_stage="老年",
                       current_medications=["华法林"]),
        ["细辛", "甘草"], "肝胃不和证").model_dump()


def _r46_emr():
    from core.emr_writer import build_emr
    from core.intake import IntakeForm
    from core.schemas import PatientProfile

    return build_emr(
        record_id="K7M3QX92",
        form=IntakeForm(chief_complaint="胃脘胀痛三月，食后加重",
                        present_illness="近三月加重，情志不畅时尤甚",
                        tongue="舌淡红苔薄白", pulse="脉弦", sleep="多梦易醒"),
        profile=PatientProfile(age_years=45, sex="女"),
        s2={"elements": [{"element": "肝", "kind": "location"},
                         {"element": "气滞", "kind": "nature"}]},
        s3={"disease": "胃脘痛", "syndrome": "肝胃不和证", "method": "疏肝和胃",
            "pathogenesis": "肝气犯胃，胃失和降",
            "reasoning": "由两胁胀满、情志诱发与脉弦推得肝气犯胃"},
        formula={"name": "柴胡疏肝散",
                 "herb_items": [{"name": "柴胡", "dose": "6", "unit": "g"},
                                {"name": "白芍", "dose": "9", "unit": "g"},
                                {"name": "枳壳", "dose": "6", "unit": "g"}]},
        doses=7,
    ).model_dump()


R46_GUIDELINE = _r46_guideline()
R46_INDIVIDUALIZATION = _r46_individualization()
R46_EMR = _r46_emr()

# R24：建议层 + token 面板的 fixture。三档 severity 各一条（三档画成一样就等于
# 没渲染），另带一条"没跑的规则"（那一行的存在本身就是 R23 的判据）。
R24_ADVICE = [
    {"kind": "incompatible", "herbs": ["甘草", "甘遂"],
     "reason": "甘草 与 甘遂 属配伍禁忌，同方相见须改方",
     "source_span": "十八反", "severity": "blocking"},
    {"kind": "thermal_mismatch", "herbs": [],
     "reason": "证型「肝胃不和证」属热，但主方前 6 味中有 4 味温热药，寒热方向可能相悖",
     "source_span": None, "severity": "warning"},
    {"kind": "duplicate_effect", "herbs": ["白术", "苍术"],
     "reason": "白术 与 苍术 性味功效重合 100%（健脾益气、燥湿利水），考虑去其一",
     "source_span": None, "severity": "suggestion"},
]
R24_ADVICE_SKIPPED = [
    {"rule": "missing_channel_guide", "available": False,
     "reason": "药理层本草表还没建出来（AutoDL 上跑 run_pharmacology_extraction 才有）"},
]
R24_MANIFEST = {
    "model": "deepseek-v4-pro", "prompt_version": "v1", "cases_sha256": "abc1234",
    "llm_calls": 11, "elapsed_ms": 96000,
    "retriever_mode": "full_context",
    "prefix_tokens_by_section": {"§2 本草速查表": 77, "§3 方剂速查表": 65,
                                 "§1 辨证指令与输出 schema": 2943, "§4 医案全量": 180412,
                                 "§5 本医家用过的药材与方剂条目": 58},
    "cache_hit_tokens": 179000, "cache_miss_tokens": 1200, "cache_hit_ratio": 0.993,
    "reasoning_tokens": 4096, "best_of_n": 3, "reasoning_effort": "max",
}
# 三位医家的结果各带一份建议层（第一位带满三档，另两位各带一条，
# 让"三列都有这一块"这件事在截图上看得见）。
R24_DONE_PAYLOAD = json.loads(json.dumps(DONE_PAYLOAD))
for _i, _r in enumerate(R24_DONE_PAYLOAD["results"]):
    _r["advice"] = R24_ADVICE if _i == 0 else R24_ADVICE[_i:_i + 1]
    _r["advice_skipped"] = R24_ADVICE_SKIPPED
    _r["formula_score"] = [0.0, 0.7, 0.9][_i]
    _r["candidates_scored"] = [
        {"index": 0, "score": [0.0, 0.7, 0.9][_i], "chosen": True,
         "formula": _r["s3"]["formula"], "syndrome": _r["s3"]["syndrome"],
         "advice_kinds": ["incompatible"], "n_advice": 1},
    ]
R24_DONE_PAYLOAD["manifest"] = R24_MANIFEST
R24_USAGE = {"mode": "shared", "remaining_calls": 44, "ip_limit_calls": 55,
             "remaining_consults_estimate": 4, "calls_per_consult": 11,
             "since": "2026-09-16T00:00:00", "warn": False, "degraded": False,
             "tokens_today": {"cache_hit": 537000, "cache_miss": 3600, "output": 18400,
                              "cache_hit_ratio": 0.993}}

# 每种状态：截图文件名 + 把页面推进那个状态的 JS + 一条 DOM 断言（返回 null 表示通过）。
# R18-I 的两个 fixture。医家列表带 enabled=false 两位——REFERENCE_PHYSICIANS
# 就是从这个字段算出来的，写死一份名单在前端是第二处实现（CLAUDE.md 第 31 条）。
REFERENCE_HEALTH = {
    "physicians": [
        {"id": "ye_tianshi", "name": "叶天士", "color": "#2C5F5A", "color_bg": "#E8EFEE",
         "enabled": True},
        {"id": "wu_jutong", "name": "吴鞠通", "color": "#9C6B16", "color_bg": "#F6EFE1",
         "enabled": True},
        {"id": "zhang_xichun", "name": "张锡纯", "color": "#8A4736", "color_bg": "#F4E9E5",
         "enabled": True},
        {"id": "li_ke", "name": "李可", "color": "#4A6B4E", "color_bg": "#E9EFE9",
         "enabled": False},
        {"id": "wang_yunqi", "name": "王云启", "color": "#5B5470", "color_bg": "#EBEAEF",
         "enabled": False},
    ],
}

# 一条带反药配对、一条不带——「只在真有的时候才出现」这件事要有对照才验得出来。
REFERENCE_FIXTURE = [
    ["li_ke", {"available": True, "physician": {"id": "li_ke", "name": "李可"}, "cases": [
        {"case_id": "li_ke-014", "score": 0.72, "visit_index": 1,
         "syndrome": "脾胃阳虚，寒湿内盛", "treatment_principle": "温阳散寒，健脾化湿",
         "formula": "附子理中汤加减", "herbs": ["制附子", "干姜", "炙甘草", "海藻"],
         "incompatible_pairs": ["炙甘草-海藻"],
         "note": "【配伍提示】本例含十八反十九畏配伍，是名家在特定病情下的用法，"
                 "不是常规配伍；照搬前须核对剂量、炮制与煎法，并说明用此配伍的理由。"},
        {"case_id": "li_ke-021", "score": 0.61, "visit_index": 2,
         "syndrome": "中气不足", "treatment_principle": "补中益气",
         "formula": "补中益气汤", "herbs": ["黄芪", "党参", "白术", "陈皮"],
         "incompatible_pairs": [], "note": None},
    ]}],
    ["wang_yunqi", {"available": True, "physician": {"id": "wang_yunqi", "name": "王云启"},
                    "cases": [], "note": "该医家（wang_yunqi）医案中没有相似度达标的匹配项，已查 77 条医案"}],
]


# R37：九段界面的判据。四个分辨率共用同一份——**判据只有一处**，
# 不然三种分辨率会各自跑偏（同 CLAUDE.md 第 31 条）。
CHAIN_FLOW_CHECK = r"""() => {
          const flow = document.getElementById('chain-flow');
          if (!flow || !flow.classList.contains('show')) return '单链区没显示出来';
          // **类名对不等于看得见**：`.is-hidden` 带 `!important`，跟 `.show`
          // 同时挂着就永远 display:none；祖先要是折叠的 <details>，自己也量不出高。
          // R37 实测：只查 `.show` 的那一版判据全绿，而四张截图全是空白。
          if (flow.offsetParent === null) return '单链区有 .show 但根本没在版面上';
          const flowBox = flow.getBoundingClientRect();
          if (flowBox.height < 100)
            return '单链区高 ' + Math.round(flowBox.height) + 'px——它没真的铺开';
          if (flow.closest('details'))
            return '单链是这次问诊的结论，不许藏在折叠区里（祖先有 <details>）';
          const secs = [...flow.querySelectorAll('.chain-sec')];
          if (secs.length !== 9) return '不是九段，是 ' + secs.length;
          const keys = secs.map(x => x.dataset.key).join(',');
          const want = 'complaint,elements,followup,organs,syndrome,method,formula,herbs,checks';
          if (keys !== want) return '九段顺序不对：' + keys;
          if (document.querySelectorAll('#columns .col').length)
            return '三列没清掉——两种形态不许同时在 DOM 里';
          const txt = flow.textContent;
          // 五步链每一步的关键内容都要真的渲染出来（不是只有标题）
          for (const need of ['肝气郁结', '肝胃不和证', '疏肝理气，和胃止痛',
                              '柴胡疏肝散加减', '柴胡', '判不了'])
            if (!txt.includes(need)) return '九段里缺内容：' + need;
          // R34a：判不了那一行必须带具体缺了什么
          if (!txt.includes('归经')) return '「判不了」没说清缺哪一项';
          // **展示层不许出现 id**（CLAUDE.md 的标识符规范）。R37 的第一版截图上
          // 同时印出了 `ye_tianshi`、`modified`、`high`、`meridian_coverage` 四种。
          // 医案号是例外：它本来就是要显示的凭据（`ye_tianshi-0001-p0-0`），
          // 所以先把"医家 id + 连字符 + 数字"这种写法摘掉再查。
          const bare = txt.replace(/[a-z_]+-\d[\w-]*/g, '');
          for (const bad of ['ye_tianshi', 'wu_jutong', 'li_ke', 'partially_verified',
                             'meridian_coverage', 'nature_conflict', 'modified',
                             'classic', 'composed', 'high', 'medium',
                             'max_rounds', 'converged', 'no_candidate', 'fast_mode',
                             'organ', 'syndrome', 'method', 'formula', 'herbs'])
            if (bare.includes(bad)) return '页面上印出了 id：' + bad;
          // 分母口径必须跟着那个百分比一起显示（R34b）
          if (txt.includes('%') && !txt.includes('分母')) return '百分比没带分母口径';
          // 字号下限：投影仪上 12px 以下就糊了
          for (const el of secs) {
            const fs = parseFloat(getComputedStyle(el.querySelector('.chain-body')).fontSize);
            if (fs < 12) return '正文字号 ' + fs + 'px < 12px';
            if (el.getBoundingClientRect().height < 20)
              return '第' + el.dataset.key + '段塌成了 '
                + Math.round(el.getBoundingClientRect().height) + 'px';
          }
          // 横向不许溢出（三种分辨率都验）
          if (document.documentElement.scrollWidth > window.innerWidth + 1)
            return '横向溢出：' + document.documentElement.scrollWidth + ' > ' + window.innerWidth;
          return null;
        }"""


STATES = {
    "first": (
        "renderExamples(EXAMPLE_COMPLAINTS); setConsultState('first');",
        """() => {
          const page = document.getElementById('consult-page');
          if (!page.classList.contains('state-first')) return '不在 first 状态';
          const n = document.querySelectorAll('#examples .example').length;
          if (n !== 3) return '首屏示例不是三条，是 ' + n;
          if (getComputedStyle(document.getElementById('columns')).display !== 'none')
            return '首屏不该已经摆出三列';
          return null;
        }""",
    ),
    "running": (
        "renderComplaintBody(COMPLAINT); setConsultState('running'); resetColumnProgress();"
        " setColumnStep(null, 's2'); setColumnStep('zhang_xichun', 's3');",
        """() => {
          const cols = document.querySelectorAll('#columns .col');
          if (cols.length !== 3) return '不是三列，是 ' + cols.length;
          const boxes = [...cols].map(c => Math.round(c.getBoundingClientRect().width));
          if (new Set(boxes).size !== 1) return '三列不等宽：' + boxes.join('/');
          const zhang = document.querySelector('.col[data-physician="zhang_xichun"] .step-active');
          if (!zhang || zhang.dataset.step !== 's3') return '张锡纯那一列没走到 s3';
          const ye = document.querySelector('.col[data-physician="ye_tianshi"] .step-active');
          if (!ye || ye.dataset.step !== 's2') return '叶天士那一列被别人的事件带跑了';
          return null;
        }""",
    ),
    "insufficient": (
        "renderConsultResult({...DONE_PAYLOAD, results: [], divergence: null,"
        " insufficient: true, insufficient_reason: '请补充舌象、脉象与二便情况。'});",
        """() => {
          const cols = document.querySelectorAll('#columns .col[data-state="insufficient"]');
          if (cols.length !== 3) return '信息不足时没有摆满三列，只有 ' + cols.length;
          if (!document.body.innerText.includes('请补充舌象')) return '后端给的理由没显示出来';
          return null;
        }""",
    ),
    "followup": (
        "setConsultState('running'); resetColumnProgress();"
        " showNeedInput('有没有解黑色柏油样便？', 'wu_jutong');",
        """() => {
          const asking = document.querySelectorAll('.col[data-state="asking"]');
          const waiting = document.querySelectorAll('.col[data-state="waiting"]');
          if (asking.length !== 1) return '提问的列不是一列，是 ' + asking.length;
          if (waiting.length !== 2) return '等待中的列不是两列，是 ' + waiting.length;
          if (asking[0].dataset.physician !== 'wu_jutong') return '问题弹错列了';
          if (!asking[0].querySelector('.ask-input')) return '那一列里没有回答输入框';
          return null;
        }""",
    ),
    "blocked": (
        "renderConsultResult({...DONE_PAYLOAD, results: [], divergence: null,"
        " rejected: true, reject_reason: '主诉含危重症状：解黑色柏油样便'});",
        """() => {
          if (document.getElementById('safety-block').hidden) return '拦截页没显示';
          if (!document.getElementById('consult-page').hidden) return '问诊页没被替换掉';
          const html = document.getElementById('tab-consult').innerHTML.toLowerCase();
          for (const bad of ['herb', 'formula']) {
            if (html.includes(bad)) return 'DOM 里还留着 ' + bad;
          }
          for (const bad of ['柴胡', '黄连', '赭石', '柴胡疏肝散']) {
            if (html.includes(bad)) return 'DOM 里还留着药名/方名 ' + bad;
          }
          return null;
        }""",
    ),
    # ---- R17：部署层 ----
    "topbar_byok": (
        "document.getElementById('byok-box').open = true;"
        " renderUsage({mode: 'shared', remaining_consults_estimate: 1, warn: true,"
        "   remaining_calls: 5, ip_limit_calls: 25, since: '2026-09-15T00:00:00Z'});",
        """() => {
          const box = document.getElementById('byok-box');
          const bar = box.getBoundingClientRect();
          // §5.1：BYOK 在**顶栏里**，不是输入区里、不是弹窗。
          const top = document.getElementById('topbar').getBoundingClientRect();
          if (!(bar.top >= top.top - 1 && bar.top <= top.bottom + 1))
            return 'BYOK 不在顶栏里';
          if (!box.querySelector('#byok-key')) return '展开之后没有输入框';
          if (!box.textContent.includes('不会存储在服务器上'))
            return '安全边界那句原话不在';
          const chip = document.getElementById('quota-chip');
          if (!chip.classList.contains('show')) return '额度 chip 没显示';
          if (!chip.classList.contains('q-warn')) return '80% 没走 warn 档';
          if (!chip.textContent.includes('今日约剩 1 次')) return '次数没显示';
          return null;
        }""",
    ),
    "demo_mode": (
        "renderDemoMode({recorded_at: '2026-09-15T10:00:00Z', model: 'deepseek-v4-pro'});"
        " renderUsage({mode: 'shared', degraded: true,"
        "   reason: '站点共享额度已用完，已切换到回放模式：结果来自预先录制的真实推理。'});",
        """() => {
          const demo = document.getElementById('demo-mode-banner');
          const deg = document.getElementById('degrade-banner');
          if (!demo.classList.contains('show')) return '演示模式提示没显示';
          if (!demo.textContent.includes('非实时调用')) return '文案不对';
          if (!deg.classList.contains('show')) return '降级说明没显示';
          // §5.2：两条都**不是警告色**——降级不是错误，是换了个后端继续跑。
          for (const [el, name] of [[demo, '演示模式'], [deg, '降级']]) {
            const bg = getComputedStyle(el).backgroundColor;
            if (bg === getComputedStyle(document.getElementById('offline-banner')).backgroundColor
                && bg !== 'rgba(0, 0, 0, 0)')
              return name + '那一条用了跟"服务未连接"一样的底色';
          }
          // 两条都在顶栏下方、主内容之上
          const top = document.getElementById('topbar').getBoundingClientRect();
          const main = document.querySelector('main').getBoundingClientRect();
          for (const [el, name] of [[demo, '演示模式'], [deg, '降级']]) {
            const r = el.getBoundingClientRect();
            if (!(r.top >= top.bottom - 1 && r.bottom <= main.top + 1))
              return name + '那一条不在顶栏下方';
          }
          const chip = document.getElementById('quota-chip');
          if (!chip.classList.contains('q-degraded')) return '额度 chip 没走 degraded 档';
          return null;
        }""",
    ),
    # ---- R18-I：参考医家那一栏 ----
    #
    # 它是**真实浏览器里唯一能验的那件事**：REFERENCE_PHYSICIANS 从 /health 的
    # enabled 字段来、身份色走 CSS 变量、反药提示只在真有的时候出现。
    # 纯函数测试（tests/test_reference_physicians_ui.py）验的是 HTML 字符串，
    # 验不出「这块 details 到底有没有出现在三列下面」——R14 那次 #results→#columns
    # 改名漏掉两处 addEventListener、整份 app.js 加载时就抛，就是这么发现的。
    "reference_physicians": (
        "renderComplaintBody(COMPLAINT);"
        " injectPhysicianColors(REFERENCE_HEALTH.physicians);"
        " renderConsultResult(DONE_PAYLOAD);"
        " document.getElementById('reference-physicians').hidden = false;"
        " document.getElementById('reference-physicians').open = true;"
        " document.getElementById('reference-body').innerHTML ="
        "   REFERENCE_FIXTURE.map(([pid, data]) => referenceBlockHtml(pid, data)).join('');",
        """() => {
          const box = document.getElementById('reference-physicians');
          if (box.hidden) return '参考医家那一栏没出现';
          const blocks = box.querySelectorAll('.ref-phys');
          if (blocks.length !== 2) return '不是两位参考医家，是 ' + blocks.length;
          // 它必须在三列**下面**：它是旁证，摆到三列上面就成了主角
          const cols = document.getElementById('columns').getBoundingClientRect();
          const r = box.getBoundingClientRect();
          if (!(r.top >= cols.bottom - 1)) return '参考医家那一栏跑到三列上面去了';
          // 身份色真的从 CSS 变量解析出来了（变量没注入时这里会拿到空串）
          const head = box.querySelector('.ref-phys .ref-head');
          const border = getComputedStyle(head).borderLeftColor;
          if (!border || border === 'rgba(0, 0, 0, 0)') return '身份色没解析出来：' + border;
          // 石绿 #4A6B4E = rgb(74, 107, 78)
          if (!border.split(' ').join('').includes('74,107,78'))
            return '李可那一块不是石绿，是 ' + border;
          // 反药提示只在真有的时候出现：两条医案里只有一条有
          const notes = box.querySelectorAll('.ref-incompat');
          if (notes.length !== 1) return '反药提示出现 ' + notes.length + ' 次，应该只有 1 次';
          if (!notes[0].textContent.includes('配伍提示')) return '提示语不对';
          // 提示用 --caution 而不是 --danger：它不是"这次开错了药"
          const bg = getComputedStyle(notes[0]).backgroundColor;
          const danger = getComputedStyle(document.getElementById('error-box')).backgroundColor;
          if (bg === danger && bg !== 'rgba(0, 0, 0, 0)')
            return '反药提示用了跟错误一样的底色';
          return null;
        }""",
    ),
    # ---- R16：两张图 ----
    "consult_graph": (
        "renderComplaintBody(COMPLAINT);"
        " renderConsultResult({...DONE_PAYLOAD, graph: NINE_LAYER_GRAPH});"
        " document.getElementById('detail-zone').open = true;"
        " skipAnimation();",
        """async () => {
          await new Promise(r => setTimeout(r, 1200));
          if (!cy) return '画布没建起来';
          if (cy.nodes().length < 20)
            return '图没长全，只有 ' + cy.nodes().length + ' 个节点';
          // R42：**九层都要真的画出来**（缺的两层是 legacy S3 本来就没有的
          // 病机/治法，它们在 missing_layers 里）。M5 那次事故就是前端漏了
          // 新加的那一层 key，而后端 JSON 测试全绿。
          const got = new Set(cy.nodes().map(n => Number(n.data('layer'))));
          for (const L of [0, 1, 2, 3, 5, 7, 8]) {
            if (!got.has(L)) return '第 ' + L + ' 层一个节点都没画出来';
          }
          // 层名列头：九格都在，缺的两层标成"本次没有"
          const bands = window.__graphPerf.bands();
          if (bands.length !== 9) return '层名列头不是九格，是 ' + bands.length;
          const missing = bands.filter(b => b.missing).map(b => b.layer).sort();
          if (String(missing) !== '4,6')
            return '缺层没有如实标出来：' + JSON.stringify(bands);
          // dagre 真的在用（退到 fallback 的话下面的零重叠没有意义）
          if (!window.__graphPerf.dagreAvailable())
            return 'dagre 没加载上，布局退到了等距铺开：'
                   + window.__graphPerf.layoutStats.fallback_reason;
          if (window.__graphPerf.layoutStats.fallback_reason)
            return '布局回落了：' + window.__graphPerf.layoutStats.fallback_reason;
          // **零重叠**：真实包围盒两两比（估算偏大也不算过，这条量的是渲染结果）
          const ov = window.__graphPerf.overlapStats();
          if (ov.n_overlaps) return '有 ' + ov.n_overlaps + ' 对节点压在一起：'
                                    + JSON.stringify(ov.pairs.slice(0, 3));
          // **交叉数不许超过数据逼出来的下界。**
          //
          // "零交叉"对这张图是做不到的，而且不是布局的错：证素层是全局共享的
          // 那一份，三位医家给出三个不同证型 → 证素→证型是**全连接**，
          // 其中每一个 K₂,₂ 都逼出一对必然交叉（换排序只换哪两条交叉）。
          // 实测 forced = 6（脏腑 3 对 + 病性 3 对）。所以判据是 excess == 0。
          const cr = window.__graphPerf.crossingStats();
          if (cr.excess > 0)
            return '布局多出了 ' + cr.excess + ' 对交叉（实测 ' + cr.n_crossings
                   + '，数据逼出来的下界 ' + cr.forced + '）：'
                   + JSON.stringify(cr.pairs.slice(0, 3));
          if (cr.forced === 0 && cr.n_crossings)
            return '没有被逼出来的交叉，却量到 ' + cr.n_crossings + ' 对';
          // §3.2 规格 1：方剂框按来源区分边框（R42 之后 id 不带医家段）
          const modified = cy.getElementById('formula::柴胡疏肝散加减');
          const composed = cy.getElementById('formula::自拟和胃汤');
          const classic = cy.getElementById('formula::左金丸');
          for (const [n, name] of [[modified,'modified'],[composed,'composed'],[classic,'classic']]) {
            if (!n.length) return '找不到 ' + name + ' 那个方剂节点';
          }
          if (modified.style('border-style') !== 'dashed') return 'modified 不是虚线';
          if (composed.style('border-style') !== 'dotted') return 'composed 不是点线';
          // §3.2 规格 2：λ1 说明必须在图上。
          const note = document.getElementById('cy-lambda1-note');
          if (!note.classList.contains('show') || !note.textContent.trim())
            return '图上没有 λ1 说明';
          // R42 视觉层级：证型层（焦点档）的字号要大过症状层（输入档）
          const syn = cy.getElementById('syn::肝胃不和证');
          const sym = cy.getElementById('sym::胃脘胀痛');
          if (!(parseFloat(syn.style('font-size')) > parseFloat(sym.style('font-size'))))
            return '证型层的字号没有大过症状层——视觉层级没生效';
          if (parseFloat(syn.style('border-width')) < 2)
            return '证型层（这张图的结论）边框没有加粗';
          // taxi 边：问诊图用正交折线
          if (cy.edges()[0].style('curve-style') !== 'taxi')
            return '边不是 taxi，是 ' + cy.edges()[0].style('curve-style');
          return null;
        }""",
    ),
    # ---------- R42：层名列头 + 导出 PNG ----------
    "graph_layer_bands": (
        "renderComplaintBody(COMPLAINT);"
        " renderConsultResult({...DONE_PAYLOAD, graph: NINE_LAYER_GRAPH});"
        " document.getElementById('detail-zone').open = true;"
        " skipAnimation();",
        """async () => {
          await new Promise(r => setTimeout(r, 1000));
          const host = document.getElementById('graph-layers');
          if (!host || !host.children.length) return '层名列头没渲染';
          const bands = [...host.querySelectorAll('.layer-band')];
          if (bands.length !== 9) return '不是九格，是 ' + bands.length;
          // 九个层名都要有字（中文名由后端下发，前端不写死）
          for (const b of bands) {
            if (!(b.textContent || '').trim()) return '有一格没有层名';
          }
          // 缺层那两格要看得出来跟别的不一样（不是灰掉，是朱砂虚线 + 一句话）
          const gone = bands.filter(b => b.classList.contains('is-missing'));
          if (gone.length !== 2) return '标成"本次没有"的不是两格，是 ' + gone.length;
          for (const g of gone) {
            if (!(g.textContent || '').includes('本次没有'))
              return '缺层那一格没写"本次没有"';
            const cs = getComputedStyle(g);
            if (cs.borderStyle !== 'dashed') return '缺层那一格不是虚线';
          }
          // 列头要在画布上方、在首屏里（写在屏外等于没写）
          const hb = host.getBoundingClientRect();
          const cb = document.getElementById('cy').getBoundingClientRect();
          if (hb.bottom > cb.top + 4) return '层名列头没在画布上方';
          // 导出 PNG 按钮：有图时可点，点了要真的产出一个 dataURL
          const btn = document.getElementById('png-btn');
          if (!btn) return '没有导出 PNG 按钮';
          if (btn.disabled) return '有图了导出按钮还是禁用的';
          const uri = cy.png({full: true, scale: 2});
          if (!uri.startsWith('data:image/png;base64,')) return '导出的不是 PNG';
          if (uri.length < 5000) return '导出的 PNG 太小（' + uri.length + '），像是空图';
          return null;
        }""",
    ),
    # ---------- R42：tooltip 钉住 + 无障碍 ----------
    "graph_tooltip_pinned": (
        "renderComplaintBody(COMPLAINT);"
        " renderConsultResult({...DONE_PAYLOAD, graph: NINE_LAYER_GRAPH});"
        " document.getElementById('detail-zone').open = true;"
        " skipAnimation();",
        """async () => {
          await new Promise(r => setTimeout(r, 1000));
          const tip = document.getElementById('graph-tooltip');
          if (tip.getAttribute('role') !== 'status') return 'tooltip 没有 role=status';
          if (tip.getAttribute('aria-live') !== 'polite')
            return 'aria-live 不是 polite（它不是警报，不该打断读屏）';
          const canvas = document.getElementById('cy');
          if (canvas.getAttribute('tabindex') !== '0') return '画布不能用键盘聚焦';
          if (!(canvas.getAttribute('aria-label') || '').trim()) return '画布没有 aria-label';
          // 点一个节点 → 钉住
          const node = cy.getElementById('herb::左金丸::黄连');
          if (!node.length) return '找不到那个药材节点';
          node.emit('tap', [{clientX: 100, clientY: 100}]);
          await new Promise(r => setTimeout(r, 120));
          if (!tip.classList.contains('show')) return '点了节点 tooltip 没出来';
          if (!tip.classList.contains('pinned')) return 'tooltip 没有钉住';
          if (tip.getAttribute('aria-hidden') !== 'false')
            return '钉住了但 aria-hidden 还是 true，读屏软件读不到';
          // 钉住之后要能选中里面的字（抄剂量进病历是这个功能存在的理由）
          if (getComputedStyle(tip).pointerEvents === 'none')
            return '钉住的 tooltip 还是 pointer-events:none，选不中里面的字';
          const pinnedHtml = tip.innerHTML;
          if (!pinnedHtml.includes('黄连')) return 'tooltip 里没有这个节点的名字';
          if (!pinnedHtml.includes('君臣佐使')) return 'tooltip 的标题不是层名';
          // 钉住期间 hover 别的节点不许改它
          const other = cy.getElementById('sym::口苦');
          other.emit('mouseover', [{clientX: 300, clientY: 300}]);
          await new Promise(r => setTimeout(r, 120));
          if (tip.innerHTML !== pinnedHtml) return '钉住了还是被 hover 改掉了';
          // Esc 取消
          document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));
          await new Promise(r => setTimeout(r, 120));
          if (tip.classList.contains('show')) return 'Esc 没有取消钉住';
          if (tip.getAttribute('aria-hidden') !== 'true')
            return '关掉了但 aria-hidden 还是 false';
          return null;
        }""",
    ),
    # ---------- R42：图谱浏览器的聚焦 + 面包屑 ----------
    "browser_focus": (
        "switchTab('graph-browser'); await loadGraphBrowserData();"
        " await new Promise(r => setTimeout(r, 400));"
        " window.__hub = gbCy.nodes()[0].id();"
        " await gbExpandNode(window.__hub);"
        " await new Promise(r => setTimeout(r, 400));"
        " window.__before = gbCy.nodes().length;"
        " window.__focus = window.__gbPerf.focus(window.__hub);",
        """async () => {
          await new Promise(r => setTimeout(r, 800));
          if (!window.__focus) return '聚焦没返回任何东西（gbIndex 没建？）';
          if (window.__focus.layout !== 'dagre')
            return '聚焦用的不是 dagre，是 ' + window.__focus.layout;
          const crumbs = document.getElementById('gb-breadcrumb');
          if (crumbs.hidden) return '聚焦了面包屑还是隐藏的';
          const items = [...crumbs.querySelectorAll('[data-gb-crumb]')];
          if (items.length < 2) return '面包屑不足两格（少了"全图"那一格？）';
          if (!items.some(el => el.classList.contains('is-current')))
            return '面包屑没标出当前那一步';
          // 聚焦之后**按跳距分列**：同一跳的节点 x 应该接近，跳距越大越靠右
          const byHop = new Map();
          gbCy.nodes().forEach(n => {
            const L = Number(n.data('layer'));
            if (!Number.isFinite(L)) return;
            if (!byHop.has(L)) byHop.set(L, []);
            byHop.get(L).push(n.position('x'));
          });
          const hops = [...byHop.keys()].sort((a, b) => a - b);
          if (hops.length < 2) return '聚焦之后只有一列，分层没生效';
          const avg = hops.map(h => byHop.get(h).reduce((a, b) => a + b, 0) / byHop.get(h).length);
          for (let i = 1; i < avg.length; i++) {
            if (!(avg[i] > avg[i - 1])) return '跳距大的那一列没有更靠右：' + JSON.stringify(avg);
          }
          // 聚焦模式下环图例要换成聚焦自己的说明
          const legend = document.getElementById('gb-ring-legend');
          if (!(legend.textContent || '').includes('聚焦'))
            return '环图例没换成聚焦说明：' + legend.textContent;
          // 两种布局在同一批节点上的实测耗时（R42 报告要这两个数）
          const bench = window.__gbPerf.benchLayouts(window.__hub);
          if (!bench || bench.dagre_ms === undefined) return '布局基准跑不出来';
          window.__bench = bench;
          // 退出聚焦：恢复的是**进聚焦前那批节点**，不是重铺首屏
          window.__gbPerf.exitFocus();
          await new Promise(r => setTimeout(r, 600));
          if (!crumbs.hidden) return '退出聚焦了面包屑还在';
          if (gbCy.nodes().length !== window.__before)
            return '退出聚焦恢复的节点数不对：' + gbCy.nodes().length
                   + ' vs 进去之前 ' + window.__before;
          return null;
        }""",
    ),
    "browser_home": (
        "switchTab('graph-browser'); await loadGraphBrowserData();",
        """async () => {
          await new Promise(r => setTimeout(r, 800));
          if (!gbCy) return '浏览器画布没建起来';
          const types = new Set(gbCy.nodes().map(n => n.data('node_type')));
          // §3.2 规格 5：首屏只有证素。
          if (types.size !== 1 || !types.has('element'))
            return '首屏不是只有证素：' + [...types].join('/');
          if (gbCy.nodes().length < 10)
            return '首屏证素太少（' + gbCy.nodes().length + '），像是没加载出来';
          // §3.2 规格 9：按门类浏览下拉存在且有选项。
          const sel = document.getElementById('gb-category-select');
          if (sel.hidden) return '按门类浏览下拉没出现';
          if (sel.options.length < 2) return '门类下拉里一个门类都没有';
          if (document.getElementById('gb-more')) return '「加载更多证型」还在';
          return null;
        }""",
    ),
    "browser_expanded": (
        "switchTab('graph-browser'); await loadGraphBrowserData();"
        " await new Promise(r => setTimeout(r, 400));"
        " window.__hubIds = gbCy.nodes().map(n => n.id());"
        " window.__hub = gbCy.nodes().filter(n => n.data('category') === 'location')[0].id();"
        " await gbExpandNode(window.__hub);",
        """async () => {
          await new Promise(r => setTimeout(r, 800));
          const types = new Set(gbCy.nodes().map(n => n.data('node_type')));
          // §3.2 规格 6：点证素 → 它的证型长在外圈。
          if (!types.has('syndrome')) return '展开之后没有证型';
          const hub = gbCy.getElementById(window.__hub);
          if (!hub.length) return '枢纽节点不在画布上：' + window.__hub;
          const syns = gbCy.nodes('[node_type = "syndrome"]');
          if (!syns.length) return '一个证型都没有';
          // concentric：枢纽在内圈——离画布中心比展开出来的近。
          const box = gbCy.extent();
          const cx = (box.x1 + box.x2) / 2, cy2 = (box.y1 + box.y2) / 2;
          const d = (n) => Math.hypot(n.position('x') - cx, n.position('y') - cy2);
          const outer = syns.map(d).reduce((a, b) => a + b, 0) / syns.length;
          if (!(d(hub) < outer))
            return '枢纽没在内圈（枢纽 ' + Math.round(d(hub)) + ' vs 证型均值 '
                   + Math.round(outer) + '）';
          // 内圈要看得见。concentric 的圈半径 ≈ 节点数 × minNodeSpacing / 2π，
          // 间距给小了会把 20 个枢纽挤成中间一个点——图上"枢纽"这件事就不存在了。
          const hubs = gbCy.nodes().filter(n => window.__hubIds.includes(n.id()));
          const inner = hubs.map(d).reduce((a, b) => a + b, 0) / hubs.length;
          if (!(inner > outer * 0.15))
            return '内圈被压扁了（内 ' + Math.round(inner) + ' / 外 '
                   + Math.round(outer) + '）';
          // 再点一次收起。
          const before = gbCy.nodes().length;
          await gbExpandNode(window.__hub);
          await new Promise(r => setTimeout(r, 400));
          if (gbCy.nodes().length >= before) return '再点一次没有收起';
          // 收起之后再展开回来，截图要留展开的那张
          await gbExpandNode(window.__hub);
          await new Promise(r => setTimeout(r, 400));
          return null;
        }""",
    ),
    # ---- R15：三种角色形态 ----
    "patient": (
        "document.getElementById('role-select').value = 'patient';"
        " renderComplaintBody(COMPLAINT); renderConsultResult(PATIENT_PAYLOAD);",
        """() => {
          const pv = document.getElementById('patient-view');
          if (pv.hidden) return '患者形态没显示';
          if (document.querySelectorAll('#columns .col').length !== 0)
            return '患者模式还摆着三列——那是裁剪版，不是独立形态';
          const flags = pv.querySelectorAll('.pv-flags li');
          if (flags.length !== 4) return '红旗症状不是 4 条，是 ' + flags.length;
          // 红旗必须在首屏、不折叠：既不许藏在 <details> 里，也不许被推到
          // 首屏之外（900px 视口）。
          if (pv.querySelector('.pv-flags-title').closest('details'))
            return '红旗被折叠了';
          const y = flags[flags.length - 1].getBoundingClientRect().bottom;
          if (y > 900) return '最后一条红旗掉到首屏外了（' + Math.round(y) + 'px）';
          const html = pv.innerHTML;
          for (const bad of ['柴胡', '黄连', '赭石', '柴胡疏肝散']) {
            if (html.includes(bad)) return '患者形态里出现了药名/方名 ' + bad;
          }
          if (!html.includes('消化内科') || !html.includes('胃痛'))
            return '病名或科室没显示';
          // 患者形态里不该再顶一个紧凑版导诊框——那是"三列裁剪版 + 导诊面板"
          // 的老形状（总纲 §1 的 F5）。
          if (document.getElementById('triage-box').classList.contains('show'))
            return '患者模式还顶着一个导诊框，又变回裁剪版了';
          return null;
        }""",
    ),
    "patient_high": (
        "document.getElementById('role-select').value = 'patient';"
        " renderComplaintBody('胸闷胸痛，冷汗'); renderConsultResult(PATIENT_HIGH_PAYLOAD);",
        """() => {
          const pv = document.getElementById('patient-view');
          if (!pv.querySelector('.pv-gated')) return 'high 没有走"不给用药建议"那条';
          if (pv.querySelector('.pv-care')) return 'high 还显示了食疗/中成药栏';
          return null;
        }""",
    ),
    "doctor_conflict": (
        "document.getElementById('role-select').value = 'doctor';"
        " updateDoctorFieldsVisibility();"
        " renderComplaintBody(COMPLAINT); renderConsultResult(DONE_PAYLOAD);"
        " DOCTOR_STATE.ye_tianshi.herb_items.push({name:'海藻',dose:9,dose_unit:'g',"
        "   processing:null,decoction:null,role:'佐',function_in_formula:null});"
        " DOCTOR_STATE.ye_tianshi.safety = DOCTOR_SAFETY;"
        " renderDoctorTable('ye_tianshi'); renderDoctorSafety('ye_tianshi');"
        " DOCTOR_STATE.ye_tianshi.exportError = {message:'该方存在拦截级安全问题，拒绝导出。',"
        "   problems:['配伍禁忌：甘草 反/畏 海藻']};"
        " renderDoctorExportPanel('ye_tianshi');",
        """() => {
          const col = document.querySelector('.col[data-physician="ye_tianshi"]');
          if (!col.querySelector('.doctor-disclaimer')) return '免责条没出现';
          const warn = col.querySelector('.safety-incompatible');
          if (!warn || !warn.textContent.includes('海藻')) return '配伍红条没出现';
          if (!col.querySelector('.safety-thermal')) return '毒性药材条没出现';
          const btn = col.querySelector('[data-rx-export]');
          if (!btn) return '导出按钮不见了';
          // §3.3：**导出被拒绝时不要禁用按钮**——禁用按钮不告诉人为什么。
          if (btn.disabled) return '导出被拒时按钮被禁用了';
          const box = col.querySelector('.rx-override-box');
          if (!box) return '拒绝原因框没出现';
          if (!box.querySelector('textarea')) return '没有"坚持导出的理由"输入框';
          return null;
        }""",
    ),
    "student_highlight": (
        # R42：九层之后生长动画比五层长得多（每层一次 stagger + 360ms 等边），
        # 原来那句"等 1200ms 再点"在新层数下会在**节点还没加完**的时候点下去，
        # 表现是"那条链上靠后的节点既不在亮的里也不在淡的里"。
        # **跳过动画**再点——这条状态验的是高亮路径，不是生长动画。
        "document.getElementById('role-select').value = 'student';"
        " renderComplaintBody(COMPLAINT);"
        " renderConsultResult({...DONE_PAYLOAD, graph: STUDENT_GRAPH});"
        " document.getElementById('detail-zone').open = true;"
        " skipAnimation();",
        """async () => {
          await new Promise(r => setTimeout(r, 1200));
          if (!cy) return '画布没建起来';
          handleSymptomClick('sym::胃脘胀痛');
          await new Promise(r => setTimeout(r, 400));
          const faded = cy.nodes('.gt-faded').map(n => n.id());
          const lit = cy.nodes().not('.gt-faded').map(n => n.id());
          // 三跳：症状 → 证素 → 证型 → 方剂。起点那条链全亮，另一条链全淡。
          // R42 九层之后这条链长了两步：症状→脏腑→证型→**治则**→方剂→君臣佐使。
          for (const id of ['sym::胃脘胀痛', 'organ::肝', 'syn::肝胃不和证',
                            'principle::疏肝和胃', 'formula::柴胡疏肝散']) {
            if (!lit.includes(id)) return id + ' 应该亮着，实际被淡化了';
          }
          for (const id of ['sym::口苦', 'nature::湿热', 'syn::肝胆湿热证',
                            'principle::清利湿热', 'formula::龙胆泻肝汤']) {
            if (!faded.includes(id)) return id + ' 应该被淡化，实际亮着';
          }
          // **先确认节点真的在**：cytoscape 的空集合 .style() 返回 undefined，
          // parseFloat(undefined) 是 NaN，而 Math.abs(NaN - x) > eps 恒为 false
          // ——判据会静默空过。R16 实测发现的正是这件事。
          const faded1 = cy.getElementById('sym::口苦');
          if (!faded1.length) return '找不到 sym::口苦 这个节点';
          const op = parseFloat(faded1.style('opacity'));
          if (!(Math.abs(op - 0.25) <= 0.001))
            return '淡化透明度不是 0.25，是 ' + faded1.style('opacity');
          // §3.4 第三条：学生模式推理过程默认展开
          const open = document.querySelector('.col-reasoning[open]');
          if (!open) return '学生模式推理过程没有默认展开';
          return null;
        }""",
    ),
    # 第六张：终态。不在 §3.1 的五种状态表里（那张表列的是"非终态怎么办"），
    # 但三列集注 + 用药对照带这两个 R14 的主要交付物只有在这张图上看得见。
    # ---------- R37：单链九段（三种分辨率各验一次） ----------
    #
    # **为什么三种**：1920×1080 评审大屏、1366×768 会议室笔记本（竖向最紧）、
    # 1280×800 投影仪。九段是纵向长版面，竖向空间最紧的那块屏最容易出折行与压字，
    # 而"在 1440×900 上看着挺好"正是 rings 那条教训要防的事。
    "chain_flow": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);",
        CHAIN_FLOW_CHECK,
    ),
    "chain_flow_1920": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);",
        CHAIN_FLOW_CHECK,
    ),
    "chain_flow_1366": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);",
        CHAIN_FLOW_CHECK,
    ),
    "chain_flow_1280": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);",
        CHAIN_FLOW_CHECK,
    ),
    # 跑到一半的样子：九段骨架 + 当前那一段高亮。**问诊一开始就摆九段**，
    # 不先摆三列再换掉（那一下闪烁正是"界面在猜"的表现）。
    "chain_running": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " setConsultState('running'); renderChainSkeleton('s2');",
        """() => {
          const flow = document.getElementById('chain-flow');
          if (!flow || flow.offsetParent === null) return '跑起来了但九段骨架没在版面上';
          const secs = [...document.querySelectorAll('#chain-flow .chain-sec')];
          if (secs.length !== 9) return '骨架不是九段，是 ' + secs.length;
          const states = secs.map(x => x.dataset.state);
          if (states[0] !== 'done') return '第①段（S1 已完成）状态不是 done：' + states[0];
          if (states[1] !== 'active') return '第②段（正在跑 S2）不是 active：' + states[1];
          if (states[8] !== 'todo') return '第⑨段不该已经亮：' + states[8];
          if (document.querySelectorAll('#columns .col').length)
            return '三列不该在 structured 下摆出来';
          return null;
        }""",
    ),
    # R37：**跑起来之后取消按钮必须是可见的、可点的**，而且那条 300 秒的空闲
    # 兜底要跟界面上说的一致。一次问诊要等几十秒，没有出口的等待是这一轮
    # 点名要修的弊端之一（"转圈转到底"）。
    "cancel_button": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " setConsultState('running'); renderChainSkeleton('s2');"
        " document.getElementById('cancel-btn').classList.remove('is-hidden');",
        """() => {
          const btn = document.getElementById('cancel-btn');
          if (!btn) return '没有取消按钮';
          if (btn.classList.contains('is-hidden')) return '跑起来了但取消按钮还藏着';
          const box = btn.getBoundingClientRect();
          if (box.width < 40 || box.height < 20)
            return '取消按钮太小：' + Math.round(box.width) + '×' + Math.round(box.height);
          if (btn.disabled) return '取消按钮是禁用的';
          // 300 秒那个兜底值必须跟界面上说的一致（两处各写一个数就会漂）
          if (typeof SSE_IDLE_TIMEOUT_MS === 'undefined' || SSE_IDLE_TIMEOUT_MS !== 300000)
            return '空闲兜底不是 300 秒：' + SSE_IDLE_TIMEOUT_MS;
          return null;
        }""",
    ),
    # 节点释义：点一个药名，面板弹出四节里真有内容的那几节。
    # **走真的 /api/node_explain**（这个脚本起的是真 uvicorn），不是塞一份假数据。
    "node_explain": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);"
        " const hit = [...document.querySelectorAll('#chain-flow .explainable')]"
        "   .find(x => x.dataset.node.startsWith('herb::'));"
        " await openNodeExplain(hit.dataset.node, hit.dataset.name);",
        """() => {
          const box = document.getElementById('node-explain');
          if (!box || !box.classList.contains('show')) return '释义面板没弹出来';
          // 同 CHAIN_FLOW_CHECK 那条：类名对 ≠ 看得见。**但 `offsetParent === null`
          // 这条判据本身在 R56 §6 之后不再成立**：桌面端（>768px）改成了
          // `position: fixed` 的吸顶侧栏，而 `position: fixed` 的元素按规范
          // `offsetParent` 恒为 null——跟"有没有显示"无关。改用
          // `getComputedStyle` 直接查 display/visibility，这是唯一一处不会
          // 被定位方式影响的判据。
          const cs0 = getComputedStyle(box);
          if (cs0.display === 'none' || cs0.visibility === 'hidden')
            return '释义面板有 .show 但没在版面上';
          if (box.getBoundingClientRect().height < 40)
            return '释义面板高 ' + Math.round(box.getBoundingClientRect().height) + 'px';
          const heads = [...box.querySelectorAll('.ne-head')].map(x => x.textContent);
          if (!heads.length) return '面板里一节都没有';
          // R42：四节扩到八节，顺序仍然是**链**（先是什么、再凭什么、
          // 再别人怎么用、再验过什么、再跟基准比、最后说风险）。
          const ORDER = ['是什么', '病机', '药理', '出处原文',
                         '名老中医经验', '验证结果', '循证对照', '注意'];
          const idx = heads.map(h => ORDER.indexOf(h));
          if (idx.some(i => i < 0)) return '出现了八节之外的节：' + heads.join('/');
          for (let i = 1; i < idx.length; i++)
            if (idx[i] < idx[i - 1]) return '八节顺序乱了：' + heads.join('/');
          if (!box.textContent.includes('出处')) return '没有出处那一行——释义必须能回指';
          // R42 新增的那几节要真的出现（只把标题加进 ORDER、没有 builder 产出它，
          // 上面那两条照样绿）
          for (const want of ['药理', '验证结果', '循证对照']) {
            if (!heads.includes(want)) return 'R42 新增的「' + want + '」这一节没出现';
          }
          // GUIDELINE_GAP_NOTE（core/node_explain.py）的措辞早就改成了
          // "对照基准是仓库内已收录的典籍与参考表，不是循证等级评定。"——
          // 不再逐字提《中医药循证临床实践指南》这个书名（那句话现在只在
          // 模块文档字符串里，不在渲染输出里）。这条判据没跟着改过，一直
          // 断言一个已经不存在的字面量，这次先跑起来的 Playwright 才第一次
          // 抓到。
          if (!box.textContent.includes('对照基准是仓库内已收录的典籍与参考表'))
            return '循证对照少了那句口径声明——"教材里有"会被读成"有循证支持"';
          return null;
        }""",
    ),
    # 单链图：一条链（一个证型、一个方剂、药材是方剂的 compound 子节点）。
    # **CLAUDE.md 点名的那条**：改了图的层结构/含义就必须真的喂给浏览器跑一遍。
    "single_chain_graph": (
        # 图区默认折叠、生长动画是分批 add 的——**要展开 + 跳过动画 + 等一会儿**，
        # 跟 consult_graph 那条同一套（那条也是这么等的）。
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);"
        " document.getElementById('detail-zone').open = true;"
        " skipAnimation();",
        """async () => {
          await new Promise(r => setTimeout(r, 1200));
          if (typeof cy === 'undefined' || !cy) return 'cytoscape 画布没建起来';
          const byLayer = {};
          cy.nodes().forEach(n => {
            const L = String(n.data('layer'));
            byLayer[L] = (byLayer[L] || 0) + 1;
          });
          // R42：结构化 S3 有病机(4)和治法(6)，所以这张图**九层都该在**。
          // 层号写死在这里是有意的：这条状态验的就是"层号改了前端跟没跟上"，
          // 从后端现取会让它永远绿。
          for (const L of ['0', '1', '3', '4', '5', '6', '7', '8']) {
            if (!byLayer[L]) return '第 ' + L + ' 层一个节点都没画出来：'
                                    + JSON.stringify(byLayer);
          }
          if (byLayer['3'] !== 1) return '证型层不是一个节点，是 ' + byLayer['3'];
          if (byLayer['7'] !== 1) return '方剂层不是一个节点，是 ' + byLayer['7'];
          // 药材必须挂在方剂下面（compound），不是另画一条边
          const herbs = cy.nodes().filter(n => String(n.data('layer')) === '8');
          const bad = herbs.filter(n => !n.parent().length);
          if (bad.length) return bad.length + ' 味药没有 parent——compound 关系断了';
          // **方名必须看得见**：方剂是 compound 父节点，label 画在框的上沿
          // （`text-valign: top`），而 `fit` 只保证节点本体在视口里——父节点的
          // label 是"框外"的东西，很容易被切掉。
          const parent = cy.nodes().filter(n => n.isParent())[0];
          if (!parent) return '没有 compound 父节点（方剂层不见了）';
          if (!(parent.data('label') || '').trim()) return '方剂节点没有 label';
          const pb = parent.renderedBoundingBox({includeLabels: true});
          if (pb.y1 < 0 || pb.x1 < 0 || pb.x2 > cy.width() || pb.y2 > cy.height())
            return '方名被切掉了：label 框 ' + JSON.stringify({
              x1: Math.round(pb.x1), y1: Math.round(pb.y1),
              x2: Math.round(pb.x2), y2: Math.round(pb.y2)})
              + ' 超出画布 ' + Math.round(cy.width()) + '×' + Math.round(cy.height());
          // R42：dagre 真的在用 + 零重叠 + 零穿越（用真实包围盒量）
          if (!window.__graphPerf.dagreAvailable())
            return 'dagre 没加载上：' + window.__graphPerf.layoutStats.fallback_reason;
          if (window.__graphPerf.layoutStats.fallback_reason)
            return '布局回落了：' + window.__graphPerf.layoutStats.fallback_reason;
          const ov = window.__graphPerf.overlapStats();
          if (ov.n_overlaps) return '有 ' + ov.n_overlaps + ' 对节点压在一起：'
                                    + JSON.stringify(ov.pairs.slice(0, 3));
          // **单链：这里要的是真正的零交叉。** 单链没有 K₂,₂（每层一列），
          // 所以 forced 必然是 0，任何一对交叉都是布局的错。
          const cr = window.__graphPerf.crossingStats();
          if (cr.forced !== 0) return '单链图竟然有被逼出来的交叉：' + cr.forced;
          if (cr.n_crossings) return '有 ' + cr.n_crossings + ' 对边交叉：'
                                     + JSON.stringify(cr.pairs.slice(0, 3));
          // 单链下**每一层都只有一列**，所以层号越大越靠右这条必须成立
          const xs = {};
          cy.nodes().filter(n => !n.isParent()).forEach(n => {
            const L = Number(n.data('layer'));
            xs[L] = Math.min(xs[L] === undefined ? Infinity : xs[L], n.position('x'));
          });
          const ks = Object.keys(xs).map(Number).sort((a, b) => a - b);
          for (let i = 1; i < ks.length; i++) {
            if (!(xs[ks[i]] > xs[ks[i - 1]]))
              return '第 ' + ks[i] + ' 层没有排在第 ' + ks[i - 1] + ' 层右边';
          }
          return null;
        }""",
    ),
    # ---------- R44：代理决策（本次处理经过） ----------
    "agent_trace": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult({...R37_DONE_PAYLOAD, agent_trace: AGENT_TRACE});",
        """() => {
          const box = document.querySelector('.agent-trace');
          if (!box) return '「本次处理经过」这一块没渲染';
          if (box.offsetParent === null) return '有这一块但没在版面上';
          const steps = [...box.querySelectorAll('.agent-step')];
          if (steps.length !== 4) return '不是四条决策，是 ' + steps.length;
          // 四能力的中文名由后端下发，前端不写死——这里验它们真的显示出来了
          const caps = steps.map(s => s.querySelector('.agent-cap').textContent.trim());
          for (const want of ['中止', '追问', '取证', '自验']) {
            if (!caps.includes(want)) return '少了「' + want + '」这一类：' + caps.join('/');
          }
          // 中止那一条要看得出来跟别的不一样（左边竖线是朱砂）
          const stop = box.querySelector('.agent-step[data-capability="stop"]');
          if (!stop) return '中止那一条没有 data-capability 标记';
          const cs = getComputedStyle(stop);
          const other = getComputedStyle(
            box.querySelector('.agent-step:not([data-capability="stop"])'));
          if (cs.borderLeftColor === other.borderLeftColor)
            return '中止那一条跟其余几条长得一样';
          // `why`（制度）与 `detail`（这一次的证据）必须是两行
          const withDetail = steps.find(s => s.querySelector('.agent-detail'));
          if (!withDetail) return '没有一条显示了本次的证据（detail）';
          if (getComputedStyle(withDetail.querySelector('.agent-detail')).display !== 'block')
            return 'detail 没有单独一行，跟 why 混在一起了';
          // 链顶要在这一块上面（先说「这是谁的结论」，再说「怎么得出来的」）
          const head = document.querySelector('#chain-flow .chain-head');
          if (!head) return '链顶不见了';
          if (head.getBoundingClientRect().top > box.getBoundingClientRect().top)
            return '决策块摆到链顶上面去了';
          // R44：产品面不许出现投票措辞
          const text = document.getElementById('chain-flow').innerText || '';
          for (const bad of ['投票', '表决', '五家综合', '得票']) {
            if (text.includes(bad)) return '产品面上出现了「' + bad + '」';
          }
          if (!text.includes('本次辨证')) return '链顶不是「本次辨证」';
          return null;
        }""",
    ),
    # ---------- R42：窄屏（768px）释义抽屉 ----------
    "node_explain_drawer": (
        "renderComplaintBody(COMPLAINT); SERVER_S3_MODE = 'structured';"
        " renderConsultResult(R37_DONE_PAYLOAD);"
        " await openNodeExplain('herb::四君子汤::党参', '党参');",
        """async () => {
          await new Promise(r => setTimeout(r, 600));
          const el = document.getElementById('node-explain');
          if (!el.classList.contains('show')) return '释义面板没打开';
          const cs = getComputedStyle(el);
          if (cs.position !== 'fixed') return '窄屏下释义面板不是底部抽屉';
          const box = el.getBoundingClientRect();
          // **贴底判据按布局视口算**：`window.innerHeight` 含滚动条那一条，
          // 而 fixed 元素的 bottom 对齐的是布局视口（documentElement.clientHeight）。
          // 两者混用会在有横向滚动条时差十几像素——那不是"没贴底"。
          const vh = document.documentElement.clientHeight;
          if (Math.round(box.bottom) < vh - 2)
            return '抽屉没贴在屏幕底部：bottom=' + Math.round(box.bottom)
                   + ' 布局视口=' + vh + ' innerHeight=' + window.innerHeight;
          // 768px 下不许出现横向滚动（R41 那条首屏判据在窄屏上同样成立）
          const de = document.documentElement;
          if (de.scrollWidth > de.clientWidth + 1)
            return '768px 下出现了横向滚动：' + de.scrollWidth + ' > ' + de.clientWidth;
          if (box.height > document.documentElement.clientHeight * 0.65)
            return '抽屉占了 ' + Math.round(box.height / window.innerHeight * 100)
                   + '% 屏高，图就看不见了';
          // 八节里有内容的那几节都要渲染出来，每节都要有出处
          const secs = [...el.querySelectorAll('.ne-sec')];
          if (secs.length < 4) return '只渲染了 ' + secs.length + ' 节';
          for (const s of secs) {
            if (!s.querySelector('.ne-head')) return '有一节没有标题';
            if (!s.querySelector('.ne-src')) return '有一节没有出处';
          }
          const text = el.innerText || '';
          for (const want of ['药理', '验证结果', '循证对照']) {
            if (!text.includes(want)) return 'R42 新增的「' + want + '」这一节没出现';
          }
          // 同上：GUIDELINE_GAP_NOTE 现在的措辞不含书名，见 node_explain 那条的注释。
          if (!text.includes('对照基准是仓库内已收录的典籍与参考表'))
            return '循证对照少了那句口径声明';
          // 关闭按钮要够大能用手指点到（WCAG 最小 44px）
          const close = el.querySelector('.ne-close');
          const cb = close.getBoundingClientRect();
          if (cb.width < 44 || cb.height < 44)
            return '关闭按钮只有 ' + Math.round(cb.width) + '×' + Math.round(cb.height);
          return null;
        }""",
    ),
    # R33 那一版的判据是"结构化模式只有一列"；**R37 把终态换成了九段单链**，
    # 所以这条状态跟着换形状。它留着不是为了再验一遍九段（chain_flow 那四条在验
    # 那个），而是因为它喂的是 R33 那份 payload：五家归因、`divergence` 为 null、
    # `herbs_grounded_ratio` 为 0——问的还是同一件事：只有一份结论时页面不许摆出
    # 一张空的处方对照表，也不许留下三列的壳，而"这是谁的结论"必须写在链顶上。
    "structured_single": (
        "renderComplaintBody(COMPLAINT); renderConsultResult(S33_DONE_PAYLOAD);",
        """() => {
          const cols = document.querySelectorAll('#columns .col');
          if (cols.length) return '结构化模式下还留着三列的壳：' + cols.length;
          const flow = document.getElementById('chain-flow');
          if (!flow || !flow.classList.contains('show')) return '单链没画出来';
          if (flow.offsetParent === null) return '单链有 .show 但没在版面上';
          const secs = [...flow.querySelectorAll('.chain-sec')];
          if (secs.length !== 9) return '不是九段，是 ' + secs.length;
          const box = flow.getBoundingClientRect();
          if (box.height < 100) return '单链几乎没有内容，高 ' + Math.round(box.height);
          const who = flow.querySelector('.chain-head .chain-who');
          if (!who) return '链顶没有"这是谁的结论"';
          // R44：链顶写的是「本次辨证」，不是「五家综合」——后者把这份结论
          // 说成"几个人拼出来的"，那是内部机制不是产品形态（消除投票痕迹）。
          if (!(who.textContent || '').includes('本次辨证'))
            return '链顶写的不是「本次辨证」，是 ' + who.textContent;
          // 引到几位照实数：这份 payload 的 physicians_cited 是 ye_tianshi/li_ke 两位,
          // **不许拿"五家"这个名字当数**（名字是配置，数是这次真跑出来的）
          const note = flow.querySelector('.chain-head .chain-note');
          if (!note || !(note.textContent || '').includes('2 家'))
            return '引用的名老中医经验家数不对：' + (note ? note.textContent : '没这一行');
          const text = flow.innerText || '';
          if (!text.includes('肝胃不和证')) return '证型没有渲染出来';
          // divergence 为 null 时处方对照区不该摆出一张空表
          const cmp = document.getElementById('rx-compare');
          if (cmp && cmp.offsetParent !== null && (cmp.innerText || '').trim())
            return '只有一份结论时还摆出了处方对照：' + cmp.innerText.slice(0, 40);
          return null;
        }""",
    ),
    # R46 §7.1：结构化四诊录入。字段由后端下发，所以要等那一趟 fetch 回来。
    "intake_form": (
        "document.getElementById('intake-box').open = true;"
        " await new Promise(r => setTimeout(r, 400));",
        """() => {
          const box = document.getElementById('intake-fields');
          if (!box) return '表单容器不在';
          const parts = [...box.querySelectorAll('.intake-part legend')].map(l => l.textContent);
          if (parts.join('') !== '望闻问切') return '四诊不全：' + parts.join('/');
          const inputs = box.querySelectorAll('[data-intake]');
          if (inputs.length < 10) return '字段太少：' + inputs.length;
          // 常用词一键选：点一下要真的填进去
          const qp = box.querySelector('.qp');
          if (!qp) return '没有常用词';
          qp.click();
          const target = box.querySelector('[data-intake="' + qp.dataset.field + '"]');
          if (!target || !target.value) return '点了常用词但没填进字段';
          // §0.4 的输入侧边界要摆在表单上，不只写在文档里
          const note = document.getElementById('intake-note');
          if (!note || !note.textContent.includes('照片')) return '表单上没有输入边界说明';
          // 人维那几个下拉要有选项
          if (document.getElementById('pf-stage').options.length < 5) return '生理阶段下拉是空的';
          if (document.getElementById('pf-const').options.length < 5) return '体质下拉是空的';
          return null;
        }"""),
    # R46 §7.3：循证对照那一行。**措辞里不许出现"指南"**——底本是教材。
    "guideline_compare": (
        "renderComplaintBody(COMPLAINT);"
        " renderConsultResult({...R37_DONE_PAYLOAD, guideline: R46_GUIDELINE,"
        " individualization: R46_INDIVIDUALIZATION});"
        # 两块默认折叠（对照是"点击查看"，个体化在没有条目时也折着）。
        # **截图与判据都要展开的那一版**——判据读的是 innerText，而折叠起来的
        # `<details>` 内容不参与渲染，innerText 里根本没有它。
        " document.querySelector('.gl-box').open = true;"
        " document.querySelector('.iv-box').open = true;",
        """() => {
          const gl = document.getElementById('guideline-box');
          if (!gl || !gl.classList.contains('show')) return '对照那一行没出来';
          const text = gl.innerText || '';
          if (!text.includes('教材推荐方案')) return '没写清底本是什么：' + text.slice(0, 40);
          if (text.includes('指南')) return '把教材说成了指南：' + text.slice(0, 60);
          if (!text.includes('出处')) return '对照条目没有出处';
          if (!text.includes('不用于择优')) return '少了"不用于择优"那句口径';
          const iv = document.getElementById('individualization-box');
          if (!iv || !iv.classList.contains('show')) return '个体化那一块没出来';
          const ivText = iv.innerText || '';
          if (!ivText.includes('依据：')) return '个体化条目没有依据';
          if (!ivText.includes('已核查')) return '没有列出已核查的维度';
          return null;
        }"""),
    # R46 §7.5：知识速查（Ctrl/⌘ + K）。
    "knowledge_panel": (
        "kpOpen(); document.getElementById('kp-input').value = '柴胡';"
        " await kpSearch('柴胡');",
        r"""() => {
          const ov = document.getElementById('kp-overlay');
          if (!ov || ov.hidden) return '速查面板没打开';
          const groups = ov.querySelectorAll('.kp-group');
          if (!groups.length) return '一条结果都没有';
          const text = ov.innerText || '';
          if (!text.includes('本草')) return '没有本草这一类';
          if (!text.includes('出处')) return '结果没有出处';
          const st = document.getElementById('kp-status').textContent || '';
          if (!/\d+ ms/.test(st)) return '没有报响应耗时：' + st;
          const ms = parseInt(st, 10);
          if (ms > 200) return '速查超出 200 ms 预算：' + ms;
          const card = document.getElementById('kp-card').getBoundingClientRect();
          if (card.bottom > window.innerHeight + 1) return '面板超出屏幕';
          return null;
        }"""),
    # R46 §7.4：病历文书草稿。**只在医师模式下出现**。
    "emr_draft": (
        "document.getElementById('role-select').value = 'doctor';"
        " renderComplaintBody(COMPLAINT); renderConsultResult(R37_DONE_PAYLOAD);"
        " renderEMR(R46_EMR); document.getElementById('emr-box').open = true;",
        """() => {
          const box = document.getElementById('emr-box');
          if (!box || box.hidden) return '医师模式下病历文书没出现';
          const notice = document.getElementById('emr-notice');
          if (!notice || !notice.textContent.includes('草稿'))
            return '没有"这是草稿"的声明';
          const secs = box.querySelectorAll('.emr-sec');
          if (secs.length < 8) return '文书段数太少：' + secs.length;
          const editable = box.querySelectorAll('textarea[data-emr]');
          if (!editable.length) return '一段都不能编辑';
          // 签名栏不可编辑
          const labels = [...secs].map(s => s.querySelector('label').textContent);
          if (!labels.includes('医师签名')) return '没有医师签名栏';
          const text = box.innerText || '';
          for (const banned of ['建议患者服用', '推荐采用该方治疗']) {
            if (text.includes(banned)) return '文书里出现了诊疗建议式措辞：' + banned;
          }
          if (!box.querySelector('#emr-print')) return '没有打印处方笺入口';
          return null;
        }"""),
    # R47 §8.4 第 34 条：首次引导。**截图里它是被显式打开的**——跑图前
    # `_seed_onboarding()` 把"已看过"预置进了 localStorage（否则每一张截图上
    # 都盖着这张卡），所以这里要自己开一次。
    "onboarding": (
        "openOnboarding();",
        """() => {
          const ob = document.getElementById('onboarding');
          if (!ob || ob.hidden) return '引导没打开';
          const steps = ob.querySelectorAll('#ob-steps li');
          if (steps.length !== 3) return '不是三步，是 ' + steps.length;
          const card = document.getElementById('ob-card').getBoundingClientRect();
          if (card.height > window.innerHeight)
            return '引导卡比屏幕还高：' + Math.round(card.height);
          // 两个出口都要在：只能靠一个按钮关的对话框在平板上很容易变成死路
          if (!document.getElementById('ob-skip')) return '没有跳过';
          if (!document.getElementById('ob-done')) return '没有知道了';
          return null;
        }"""),
    # R47 §8.4 第 35 条：帮助气泡。点「?」，一句话说明 + 一个例子。
    "help_popover": (
        "document.querySelector('.help-btn[data-help=\"complaint\"]').click();",
        """() => {
          const pop = document.getElementById('help-pop');
          if (!pop || pop.hidden) return '帮助气泡没出来';
          const text = pop.innerText || '';
          if (!text.includes('例')) return '说明里没有例子——没有例子的说明等于换个说法';
          const box = pop.getBoundingClientRect();
          const de = document.documentElement;
          if (box.right > de.clientWidth + 1) return '气泡右边出屏了';
          if (box.left < 0) return '气泡左边出屏了';
          return null;
        }"""),
    "done": (
        "renderComplaintBody(COMPLAINT); renderConsultResult(DONE_PAYLOAD);",
        """() => {
          const cols = document.querySelectorAll('#columns .col[data-state="done"]');
          if (cols.length !== 3) return '终态不是三列';
          const heights = [...cols].map(c => Math.round(c.getBoundingClientRect().height));
          if (Math.max(...heights) - Math.min(...heights) > 2)
            return '三列不等高：' + heights.join('/');
          const dots = document.querySelectorAll('#rx-compare .dot').length;
          if (dots !== 1 + 6 + 4 + 4) return '对照带点数不对：' + dots;
          // R24：带子从两个 div 段换成一张 SVG（斜纹 = 噪声地板、实心 = 真实分歧）。
          // **判据跟着结构改，但问的还是同一件事**：两段在不在、噪声段是不是更长。
          // 另加两条 R24 特有的：带高（要能隔着几米看清）和斜纹 pattern 在不在
          // ——斜纹是这次改动的全部意义（纹理不依赖亮度，投影压暗了也还在），
          // 只查"有两个 rect"的话，把 fill 换回第二种灰也会绿。
          const band = document.querySelector('#rx-compare svg.rx-band');
          if (!band) return '对照带不是 SVG';
          if (!band.querySelector('pattern#rx-hatch line')) return '噪声段没有斜纹 pattern';
          const rects = band.querySelectorAll('rect.rx-band-noise, rect.rx-band-real');
          if (rects.length !== 2) return 'ε 参考线不是两段';
          if (band.getBoundingClientRect().height < 12) return '对照带太矮，隔远了看不见';
          const noise = rects[0].getBoundingClientRect().width;
          const real = rects[1].getBoundingClientRect().width;
          if (!(noise > real)) return 'ε=0.3954 差异=0.53，噪声段该比真实段长';
          if (rects[0].getAttribute('fill') !== 'url(#rx-hatch)') return '噪声段不是斜纹填充';
          if (!document.body.innerText.includes('0.3954')) return '没显示这条主诉的 ε';
          const folds = document.querySelectorAll('.herb-fold').length;
          if (folds < 1) return '药材一处都没折叠';
          return null;
        }""",
    ),
    # ---------- R24：八项前端改造的真浏览器验收 ----------
    #
    # 为什么这四张是必须的（CLAUDE.md 那条硬约定的具体落点）：
    #   · 题记/去卡片化是纯 CSS 效果，node 测试连 CSS 都不加载；
    #   · 自绘下拉的弹出层是运行时建的 DOM，字符串断言看不到它；
    #   · 建议层三档颜色要真的分得开（computedStyle 才知道）；
    #   · 两环的半径关系只有真布局跑完才有坐标。
    "epigraph": (
        "renderComplaintBody(''); setConsultState('first');",
        """() => {
          const eg = document.getElementById('epigraph');
          if (!eg) return '题记不在 DOM 里';
          if (getComputedStyle(eg).display === 'none') return '首屏没显示题记';
          // R47：DOM 里是 5 个 `.eg-line`（第二、三行各有产品面/研究面两套措辞），
          // **任何一种模式下真正显示出来的仍然是三行**。判据改成数"看得见的"
          // ——这条判据跑在真浏览器里，本来就该问"屏幕上有几行"，
          // 而不是"DOM 里有几个节点"。
          const lines = [...eg.querySelectorAll('.eg-line')]
            .filter(l => l.offsetParent !== null);
          if (lines.length !== 3) return '题记不是三行：' + lines.length;
          // 三行三种字号/颜色：它们是三句不同性质的话，不是一段话的三行
          const sizes = [...lines].map(l => parseFloat(getComputedStyle(l).fontSize));
          if (new Set(sizes).size !== 3) return '三行字号没区分：' + sizes.join('/');
          if (!(sizes[0] > sizes[1] && sizes[1] > sizes[2]))
            return '三行字号不是递减：' + sizes.join('/');
          // 宋体说中医的话：第一行必须是 classic 栈
          if (!getComputedStyle(lines[0]).fontFamily.includes('Serif'))
            return '题记第一行不是宋体栈';
          // 去卡片化：输入区不再是圆角框
          const panel = getComputedStyle(document.getElementById('input-panel'));
          if (parseFloat(panel.borderTopLeftRadius) > 0) return '输入区还是圆角卡片';
          // 自绘下拉：按钮吃到了页面字体（原生 select 的弹出层不吃）
          const btn = document.querySelector('#role-select-label + .cs-wrap .cs-button')
                   || document.querySelector('.cs-wrap .cs-button');
          if (!btn) return '自绘下拉按钮没建出来';
          if (!getComputedStyle(btn).fontFamily.includes('Noto'))
            return '自绘按钮没用页面字体：' + getComputedStyle(btn).fontFamily;
          return null;
        }""",
    ),
    "select_open": (
        # 用键盘打开：自绘之后键盘契约要自己实现，而键盘是最容易漏的那一半
        "document.querySelector('.cs-wrap .cs-button').focus();"
        " document.querySelector('.cs-wrap .cs-button')"
        ".dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));",
        """() => {
          const wrap = document.querySelector('.cs-wrap');
          const btn = wrap.querySelector('.cs-button');
          const list = wrap.querySelector('.cs-list');
          if (list.hidden) return '↓ 没有打开列表';
          if (btn.getAttribute('aria-expanded') !== 'true') return 'aria-expanded 没跟上';
          const opts = list.querySelectorAll('.cs-option');
          const native = wrap.querySelector('select');
          if (!native) return '原生 select 被移除了（值就没有唯一来源了）';
          if (opts.length !== native.options.length)
            return '选项数跟原生不一致：' + opts.length + ' vs ' + native.options.length;
          if (list.getBoundingClientRect().height < 20) return '列表没有实际高度';
          // 选中项要看得出是选中的
          const sel = list.querySelector('.cs-option[aria-selected="true"]');
          if (!sel) return '没有标出当前选中项';
          // 键盘移动 + 回车提交：值要真的写回原生元素
          const before = native.value;
          btn.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
          btn.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
          if (native.value === before) return '回车没有把值写回原生 select';
          if (!list.hidden) return '选完没有关闭列表';
          // 截图要留打开的那一张
          btn.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
          return null;
        }""",
    ),
    "advice_panel": (
        "renderComplaintBody(COMPLAINT); renderUsage(R24_USAGE);"
        " renderConsultResult(R24_DONE_PAYLOAD);",
        """() => {
          const rows = document.querySelectorAll('#columns .adv-row');
          if (rows.length < 5) return '建议行太少：' + rows.length;
          const byClass = (c) => document.querySelectorAll('#columns .adv-row.adv-' + c);
          for (const c of ['blocking', 'warning', 'suggestion']) {
            if (!byClass(c).length) return '缺 ' + c + ' 档的建议行';
          }
          // 三档颜色必须真的分得开（三档画成一样就等于没渲染）
          const colors = ['blocking', 'warning', 'suggestion']
            .map(c => getComputedStyle(byClass(c)[0]).color);
          if (new Set(colors).size !== 3) return '三档颜色没分开：' + colors.join(' / ');
          // 没跑的规则要看得见、且没被折叠
          const skipped = document.querySelectorAll('#columns .adv-skipped-row');
          if (!skipped.length) return '没显示"没跑的规则"';
          if (skipped[0].closest('details')) return '没跑的规则被折叠了';
          if (!document.body.innerText.includes('不是疗效评分')
              && !document.querySelector('.adv-score[title*="不是疗效评分"]'))
            return '评分没带口径说明';
          // token 面板：本次 + 今日两段
          const tp = document.getElementById('token-panel');
          if (!tp || !tp.querySelector('.tp-block')) return 'token 面板是空的';
          // **祖先的 <details> 也要打开**：token 面板挂在 manifest 旁边，而那块在
          // 「图谱与细节」这个折叠区里。innerText 只返回**渲染出来的**文字，
          // 外层折叠着的话读回来是空串——这一条本身就是"面板在 DOM 里"和
          // "面板看得见"两件事的区别（这次就是被它绊了一下）。
          for (let el = tp; el; el = el.parentElement) {
            if (el.tagName === 'DETAILS') el.open = true;
          }
          tp.querySelector('details').open = true;
          const text = tp.innerText;
          if (!text.includes('本次前缀各段 token')) return 'token 面板缺"本次"那一段';
          if (!text.includes('今日累计')) return 'token 面板缺"今日累计"那一段';
          if (!text.includes('180,412')) return '前缀 token 没按千分位显示';
          if (!text.includes('99.3%')) return '没显示本次命中率';
          // 君臣佐使两列密排
          const grid = document.querySelector('#columns .herb-grid');
          if (!grid) return '君臣佐使不是两列网格';
          if (getComputedStyle(grid).display !== 'grid') return 'herb-grid 没有真的 grid';
          const roleCells = grid.querySelectorAll('.hg-role');
          const herbCells = grid.querySelectorAll('.hg-herbs');
          if (roleCells.length !== herbCells.length) return '两列数量不对齐';
          // 四组药味的左边界要对齐（两列排版的全部意义）
          const lefts = new Set([...herbCells].map(
            c => Math.round(c.getBoundingClientRect().left)));
          if (lefts.size !== 1) return '药味左边界没对齐：' + [...lefts].join('/');
          // 去卡片化：对照带不再是圆角框
          const rx = getComputedStyle(document.getElementById('rx-compare'));
          if (parseFloat(rx.borderTopLeftRadius) > 0) return '对照带还是圆角卡片';
          return null;
        }""",
    ),
    "rings": (
        "switchTab('graph-browser'); await loadGraphBrowserData();"
        " await new Promise(r => setTimeout(r, 400));"
        " window.__hubIds = gbCy.nodes().map(n => n.id());"
        " window.__hub = gbCy.nodes().filter(n => n.data('category') === 'location')[0].id();"
        " await gbExpandNode(window.__hub);",
        """async () => {
          await new Promise(r => setTimeout(r, 800));
          const legend = document.getElementById('gb-ring-legend');
          if (!legend || !legend.textContent.trim()) return '两环图例是空的';
          if (!legend.textContent.includes('内圈') || !legend.textContent.includes('外圈'))
            return '图例没说清哪个是内圈';
          // R24 补丁①：一次展开最多 20 个，**多出来的要说出来**。
          const status = (document.getElementById('gb-search-status') || {}).textContent || '';
          const outerCount = gbCy.nodes().length - window.__hubIds.length;
          if (outerCount > GB_EXPAND_CAP) return '一次展开超过上限（' + outerCount
            + ' > ' + GB_EXPAND_CAP + '）';
          if (!status.includes('还有') || !status.includes('搜索直达'))
            return '状态栏没说还剩多少个：' + status;
          // R24 补丁②：**标签要能读**。这两条是这一轮新加的，也是上一版
          // 全绿却拍出一张糊图的原因——旧判据只看半径聚类，不看字。
          const nodes = gbCy.nodes();
          const fonts = nodes.map(n => parseFloat(n.renderedStyle('font-size')));
          const minFont = Math.min.apply(null, fonts);
          if (!(minFont >= 12)) return '字号被 fit 缩到 ' + minFont.toFixed(1) + 'px（要 ≥ 12）';
          // 两两比包围盒（含标签）。留 2px 容差：renderedBoundingBox 把标签的
          // 抗锯齿边也算进去，两个框贴着但没压字时会报出 1px 级的相交，
          // 那不是"压字"，把它算成失败会让判据变成一个永远红的东西。
          const boxes = nodes.map(n => ({label: n.data('label'),
                                         bb: n.renderedBoundingBox({includeLabels: true})}));
          for (let i = 0; i < boxes.length; i += 1) {
            for (let j = i + 1; j < boxes.length; j += 1) {
              const a = boxes[i].bb, c = boxes[j].bb;
              const ox = Math.min(a.x2, c.x2) - Math.max(a.x1, c.x1);
              const oy = Math.min(a.y2, c.y2) - Math.max(a.y1, c.y1);
              if (ox > 2 && oy > 2) {
                return '标签压字：「' + boxes[i].label + '」和「' + boxes[j].label
                  + '」重叠 ' + Math.round(ox) + '×' + Math.round(oy) + 'px';
              }
            }
          }
          // R24 补丁③：内环不许塌成一点。**从枢纽自己的重心量**，不是从
          // 画布 extent 的中心量——扇面只在一侧，extent 的中心被它拽偏了，
          // 量出来的"半径"会小一半（实测 0.068 vs 0.21，同一张图）。
          const hubs = gbCy.nodes().filter(n => window.__hubIds.includes(n.id()));
          const others = gbCy.nodes().filter(n => !window.__hubIds.includes(n.id()));
          if (!others.length) return '展开之后外圈是空的';
          const hx = hubs.map(n => n.position('x')).reduce((a, b) => a + b, 0) / hubs.length;
          const hy = hubs.map(n => n.position('y')).reduce((a, b) => a + b, 0) / hubs.length;
          const d = (n) => Math.hypot(n.position('x') - hx, n.position('y') - hy);
          const short = Math.min(gbCy.width(), gbCy.height());
          const innerMin = Math.min.apply(null, hubs.map(d));
          if (!(innerMin >= short * 0.18))
            return '内环塌了：最小半径 ' + Math.round(innerMin) + 'px = 短边的 '
              + (innerMin / short).toFixed(3) + '（要 ≥ 0.18）';
          // 两环：每个外圈节点都要比最里面那个枢纽远。
          if (!others.every(n => d(n) > innerMin))
            return '有外圈节点跑进内环里了';
          // 扇形而不是整圈：外圈节点的角度跨度要明显小于 360°。
          const angs = others.map(n => Math.atan2(n.position('y') - hy, n.position('x') - hx));
          const span = Math.max.apply(null, angs) - Math.min.apply(null, angs);
          if (!(span < Math.PI * 1.9))
            return '外圈摊成了整圈（跨度 ' + Math.round((span * 180) / Math.PI) + '°）';
          return null;
        }""",
    ),
}


def _start_server(port: int, *, product: bool) -> subprocess.Popen:
    """起一台 uvicorn。产品形态由环境变量决定，**不是由请求参数决定**
    ——它决定的是整台服务的形状（见 core/product_mode.py）。"""
    env = dict(os.environ)
    env["PRODUCT_MODE"] = "1" if product else "0"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _seed_onboarding(page) -> None:  # noqa: ANN001 - playwright Page
    """把「首次引导已看过」预置进 localStorage。

    **不这么做的话每一张截图上都盖着那张引导卡**——它按设计就是首次进入时
    自动弹的，而每个 Playwright 页面都是全新的浏览器上下文、localStorage 是
    空的，所以每一次都算"首次"。引导本身另有一张专门的截图验它。"""
    page.add_init_script(
        "try { localStorage.setItem('tcm.onboardingSeen', '1'); } catch (e) {}")


# ---------- R47 §8.5：产品模式全套截图 ----------
#
# **三档分辨率 × 三种角色 × 五种状态 = 45 张**，与内部模式的截图分开存放
# （`docs/screenshots/product/`）。判据是**共用的产品面判据 + 每态一条**：
# 共用那条扫的是"十六条清单里的东西一样都没露出来"，它才是这一套的意义；
# 每态那条只确认这一态确实渲染出来了，不是一张空白页。
PRODUCT_OUT_DIR = OUT_DIR / "product"

#: 1920×1080 评审大屏、1366×768 诊室常见（竖向最紧）、768×1024 平板。
PRODUCT_VIEWPORTS = {
    "1920": {"width": 1920, "height": 1080},
    "1366": {"width": 1366, "height": 768},
    "768": {"width": 768, "height": 1024},
}

#: 产品面只剩三种角色（§8.2 第 5 条）。
PRODUCT_SCREENSHOT_ROLES = ("doctor", "student", "patient")

#: 五种状态的构造。`$PAYLOAD` 由 role 决定——患者角色下后端下发的是裁剪过的
#: 那一份（不含方药），拿医师那份去渲染患者界面等于把这条安全边界绕过去。
PRODUCT_STATE_SETUP = {
    "first": "setConsultState('first');",
    "running": "renderComplaintBody(COMPLAINT); setConsultState('running');"
               " resetColumnProgress();",
    "insufficient": "renderConsultResult({...$PAYLOAD, results: [], divergence: null,"
                    " insufficient: true, insufficient_reason: '请补充舌象、脉象与二便情况。'});",
    "followup": "setConsultState('running'); resetColumnProgress();"
                " showNeedInput('有没有解黑色柏油样便？', 'wu_jutong');",
    "done": "renderComplaintBody(COMPLAINT); renderConsultResult($PAYLOAD);",
}

#: 角色 → 用哪份响应体。患者走 PATIENT_PAYLOAD（后端裁剪过的那一份）。
PRODUCT_PAYLOAD_BY_ROLE = {
    "doctor": "R37_DONE_PAYLOAD",
    "student": "R37_DONE_PAYLOAD",
    "patient": "PATIENT_PAYLOAD",
}

#: 共用的产品面判据。**这一条才是这套截图的意义**——它在真实 DOM 上验
#: §8.2 那十六条，而不是在源码里验标记。静态那层在
#: tests/test_no_demo_artifacts.py，两层都要。
PRODUCT_FACE_CHECK = r"""() => {
  if (document.documentElement.dataset.productMode !== '1')
    return '这台服务不是产品模式';
  const banned = ['噪声地板', '分歧度', 'Jaccard', 'ε=', '演示模式', '评测',
                  'SDT', 'traceback', 'Traceback', 'LLMError', 'demo',
                  '实验性', '原型', 'TODO', '⏳', 'hybrid', 'bm25'];
  const text = document.body.innerText || '';
  for (const w of banned) {
    if (text.includes(w)) return '产品面上出现了禁词「' + w + '」';
  }
  if (/\bR\d{2}\b/.test(text)) return '产品面上出现了内部轮次编号';
  const roleSel = document.getElementById('role-select');
  if (!roleSel) return '没有角色下拉';
  if ([...roleSel.options].some(o => o.value === 'researcher'))
    return '研究者角色还留在下拉里';
  for (const id of ['byok-box', 'quota-chip', 'retriever-mode',
                    'rx-compare', 'manifest-footer', 'token-panel']) {
    const el = document.getElementById(id);
    if (el && el.offsetParent !== null) return '内部块「' + id + '」还看得见';
  }
  const disc = document.getElementById('footer-disclaimer');
  if (!disc || disc.offsetParent === null) return '页脚免责声明不见了';
  if (!(disc.textContent || '').includes('不作为医疗器械管理'))
    return '免责声明少了产品性质界定那一句';
  const ver = document.getElementById('footer-version');
  if (!ver || !(ver.textContent || '').trim()) return '页脚没有版本号';
  const de = document.documentElement;
  if (de.scrollWidth > de.clientWidth + 1)
    return '出现了横向滚动：' + de.scrollWidth + ' > ' + de.clientWidth;
  return null;
}"""

#: 每态一条：确认这一态真的渲染出来了。
PRODUCT_STATE_CHECK = {
    "first": """() => {
      const page = document.getElementById('consult-page');
      if (!page.classList.contains('state-first')) return '不是首屏态';
      const how = document.getElementById('how-to-use');
      if (!how || how.offsetParent === null) return '首屏没有使用说明';
      if (how.querySelectorAll('li').length !== 3) return '使用说明不是三行';
      return null;
    }""",
    "running": """() => {
      const sk = document.getElementById('chain-skeleton');
      if (!sk || sk.offsetParent === null) return '加载态没有骨架屏';
      const eta = document.getElementById('eta-note');
      if (!eta || !(eta.textContent || '').trim()) return '加载态没有预计剩余那一行';
      return null;
    }""",
    "insufficient": """() => {
      if (!document.body.innerText.includes('请补充舌象'))
        return '后端给的理由没显示出来';
      return null;
    }""",
    "followup": """() => {
      const asking = document.querySelectorAll('.col[data-state="asking"]');
      if (asking.length !== 1) return '提问的列不是一列，是 ' + asking.length;
      if (!asking[0].querySelector('.ask-input')) return '那一列里没有回答输入框';
      return null;
    }""",
    "done": """() => {
      const text = document.body.innerText || '';
      if (text.length < 200) return '终态几乎没有内容';
      const rec = document.getElementById('footer-record');
      if (!rec || !(rec.textContent || '').includes('本次记录编号'))
        return '页脚没有本次记录编号';
      return null;
    }""",
}


def run_product(wait_ms: int) -> int:
    """产品模式全套截图：三档分辨率 × 三种角色 × 五种状态。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（浏览器本机已有，不要跑 "
              "playwright install）", file=sys.stderr)
        return 2

    port = _free_port()
    server = _start_server(port, product=True)
    failures: list[str] = []
    n = 0
    try:
        if not _wait_ready(f"http://127.0.0.1:{port}/health", time.monotonic() + 60):
            print("服务 60 秒没起来", file=sys.stderr)
            return 1
        PRODUCT_OUT_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            for vp_name, viewport in PRODUCT_VIEWPORTS.items():
                for role in PRODUCT_SCREENSHOT_ROLES:
                    for state, setup in PRODUCT_STATE_SETUP.items():
                        n += 1
                        name = f"{vp_name}_{role}_{state}"
                        page = browser.new_page(viewport=viewport)
                        _seed_onboarding(page)
                        errors: list[str] = []
                        page.on("pageerror", lambda e: errors.append(str(e)))
                        page.goto(f"http://127.0.0.1:{port}/app/index.html",
                                  wait_until="networkidle")
                        for var, value in (("R37_DONE_PAYLOAD", R37_DONE_PAYLOAD),
                                           ("PATIENT_PAYLOAD", PATIENT_PAYLOAD),
                                           ("COMPLAINT", COMPLAINT)):
                            page.evaluate(
                                f"window.{var} = {json.dumps(value, ensure_ascii=False)};")
                        # 角色在**页面上选**，不是往 payload 里塞一个字段：
                        # 这一套验的正是"产品面上这三个角色都长什么样"。
                        page.evaluate(
                            "(r) => { const s = document.getElementById('role-select');"
                            " if (s) { s.value = r;"
                            " if (typeof refreshSelect === 'function') refreshSelect(s);"
                            " updateDisclaimer(); updateDoctorFieldsVisibility(); } }", role)
                        js = setup.replace("$PAYLOAD", PRODUCT_PAYLOAD_BY_ROLE[role])
                        page.evaluate(f"(async () => {{ {js} }})()")
                        page.wait_for_timeout(wait_ms)
                        out = PRODUCT_OUT_DIR / f"p_{name}.png"
                        page.screenshot(path=str(out), full_page=True)
                        verdict = (page.evaluate(f"({PRODUCT_FACE_CHECK})()")
                                   or page.evaluate(f"({PRODUCT_STATE_CHECK[state]})()"))
                        if verdict:
                            failures.append(f"{name}：{verdict}")
                        if errors:
                            failures.append(f"{name}：页面里有 JS 错误 " + "；".join(errors))
                        print(f"→ {out}{'  ✗ ' + verdict if verdict else '  ✓'}")
                        page.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    if failures:
        print("\n判据不过：\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"\n产品模式 {n} 张全部通过")
    return 0


def run(only: str | None, wait_ms: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（浏览器本机已有，不要跑 "
              "playwright install）", file=sys.stderr)
        return 2

    names = [only] if only else list(STATES)
    port = _free_port()
    # R47：**这 34 态跑在内部模式下**（`PRODUCT_MODE=0`）。它们验的是研究面的
    # 形状——三列集注、分歧读数、运行清单、研究者角色——产品模式下这些本来就
    # 该看不见。产品形态另有一套（`--product`，三档分辨率 × 三种角色 × 五种
    # 状态），两套分开存放，理由跟 tests/conftest.py 钉 PRODUCT_MODE=0 一样。
    server = _start_server(port, product=False)
    failures: list[str] = []
    try:
        if not _wait_ready(f"http://127.0.0.1:{port}/health", time.monotonic() + 60):
            print("服务 60 秒没起来", file=sys.stderr)
            return 1
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=_chromium_path())
            for name in names:
                setup, check = STATES[name]
                page = browser.new_page(viewport=VIEWPORT_OVERRIDES.get(name, VIEWPORT))
                _seed_onboarding(page)
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/app/index.html", wait_until="networkidle")
                # 把这一批构造好的响应体注进页面的全局作用域，setup 脚本直接引用。
                for var, value in (("DONE_PAYLOAD", DONE_PAYLOAD),
                                   ("PATIENT_PAYLOAD", PATIENT_PAYLOAD),
                                   ("PATIENT_HIGH_PAYLOAD", PATIENT_HIGH_PAYLOAD),
                                   ("STUDENT_GRAPH", STUDENT_GRAPH),
                                   ("DOCTOR_SAFETY", DOCTOR_SAFETY),
                                   ("NINE_LAYER_GRAPH", NINE_LAYER_GRAPH),
                                   ("PATIENT_GRAPH", PATIENT_GRAPH),
                                   ("REFERENCE_HEALTH", REFERENCE_HEALTH),
                                   ("REFERENCE_FIXTURE", REFERENCE_FIXTURE),
                                   # R24：建议层 + token 面板 + 今日用量
                                   ("R24_DONE_PAYLOAD", R24_DONE_PAYLOAD),
                                   ("R24_USAGE", R24_USAGE),
                                   # R33：结构化模式的单列终态
                                   ("S33_DONE_PAYLOAD", S33_DONE_PAYLOAD),
                                   # R37：单链九段 + 单链图（用真 schema 构造）
                                   ("R37_DONE_PAYLOAD", R37_DONE_PAYLOAD),
                                   # R44：代理决策
                                   ("AGENT_TRACE", AGENT_TRACE),
                                   # R46：循证对照 / 个体化 / 病历文书
                                   ("R46_GUIDELINE", R46_GUIDELINE),
                                   ("R46_INDIVIDUALIZATION", R46_INDIVIDUALIZATION),
                                   ("R46_EMR", R46_EMR),
                                   ("COMPLAINT", COMPLAINT)):
                    page.evaluate(f"window.{var} = {json.dumps(value, ensure_ascii=False)};")
                # setup 里可能有 await（图谱浏览器要先把数据拉回来），
                # 统一包成 async IIFE——page.evaluate 会 await 返回的 Promise。
                page.evaluate(f"(async () => {{ {setup} }})()")
                page.wait_for_timeout(wait_ms)
                out = OUT_DIR / f"{PREFIX[name]}_{name}.png"
                page.screenshot(path=str(out), full_page=True)
                # 判据可能是 async（学生模式要等动画跑完）。page.evaluate 会
                # 自动 await 返回的 Promise，两种写法都能用同一行接。
                verdict = page.evaluate(f"({check})()")
                if verdict:
                    failures.append(f"{name}：{verdict}")
                if errors:
                    failures.append(f"{name}：页面里有 JS 错误 " + "；".join(errors))
                print(f"→ {out}{'  ✗ ' + verdict if verdict else '  ✓'}")
                page.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)

    if failures:
        print("\n判据不过：\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print(f"\n{len(names)} 种状态全部通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=list(STATES), help="只跑其中一种状态")
    ap.add_argument("--wait-ms", type=int, default=500, help="截图前再等多久（字体 swap）")
    ap.add_argument("--product", action="store_true",
                    help="改跑产品模式全套（三档分辨率 × 三种角色 × 五种状态）")
    args = ap.parse_args(argv)
    if args.product:
        return run_product(args.wait_ms)
    return run(args.only, args.wait_ms)


if __name__ == "__main__":
    raise SystemExit(main())
