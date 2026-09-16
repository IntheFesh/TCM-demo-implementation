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
import subprocess
import sys
import time
from pathlib import Path

from scripts.screenshot_ui import _chromium_path, _free_port, _wait_ready

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}

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
STUDENT_GRAPH = {
    "nodes": [
        {"data": {"id": "sym::胃脘胀痛", "label": "胃脘胀痛", "layer": 0}},
        {"data": {"id": "sym::口苦", "label": "口苦", "layer": 0}},
        {"data": {"id": "el::肝郁", "label": "肝郁", "layer": 1}},
        {"data": {"id": "el::湿热", "label": "湿热", "layer": 1}},
        {"data": {"id": "syn::ye_tianshi", "label": "肝胃不和证", "layer": 2,
                  "phys": "ye_tianshi"}},
        {"data": {"id": "syn::wu_jutong", "label": "肝胆湿热证", "layer": 2,
                  "phys": "wu_jutong"}},
        {"data": {"id": "formula::ye_tianshi::柴胡疏肝散", "label": "柴胡疏肝散",
                  "layer": 3, "phys": "ye_tianshi", "source": "classic"}},
        {"data": {"id": "formula::wu_jutong::龙胆泻肝汤", "label": "龙胆泻肝汤",
                  "layer": 3, "phys": "wu_jutong", "source": "classic"}},
    ],
    "edges": [
        {"data": {"source": "sym::胃脘胀痛", "target": "el::肝郁"}},
        {"data": {"source": "sym::口苦", "target": "el::湿热"}},
        {"data": {"source": "el::肝郁", "target": "syn::ye_tianshi"}},
        {"data": {"source": "el::湿热", "target": "syn::wu_jutong"}},
        {"data": {"source": "syn::ye_tianshi", "target": "formula::ye_tianshi::柴胡疏肝散"}},
        {"data": {"source": "syn::wu_jutong", "target": "formula::wu_jutong::龙胆泻肝汤"}},
    ],
    "dropped_edges": 0,
}

# R16：一张有六层的问诊图，三种 source 各一个方剂。node_type 由 to_graph()
# 按 layer 填，这里照它的输出形状写。
SIX_LAYER_GRAPH = {
    "nodes": [
        {"data": {"id": "sym::胃脘胀痛", "label": "胃脘胀痛", "layer": 0,
                  "node_type": "symptom", "state": "explained"}},
        {"data": {"id": "elem::肝郁", "label": "肝郁", "layer": 1,
                  "node_type": "element", "kind": "nature"}},
        {"data": {"id": "syn::ye_tianshi", "label": "胃痛 · 肝胃不和证", "layer": 2,
                  "node_type": "syndrome", "phys": "ye_tianshi", "pname": "叶天士"}},
        {"data": {"id": "syn::wu_jutong", "label": "胃痛 · 肝胃气滞证", "layer": 2,
                  "node_type": "syndrome", "phys": "wu_jutong", "pname": "吴鞠通"}},
        {"data": {"id": "syn::zhang_xichun", "label": "胃痛 · 肝气犯胃证", "layer": 2,
                  "node_type": "syndrome", "phys": "zhang_xichun", "pname": "张锡纯"}},
        {"data": {"id": "formula::ye_tianshi::柴胡疏肝散", "label": "柴胡疏肝散加减",
                  "layer": 3, "node_type": "formula", "phys": "ye_tianshi",
                  "source": "modified", "selected": True}},
        {"data": {"id": "formula::wu_jutong::左金丸", "label": "左金丸", "layer": 3,
                  "node_type": "formula", "phys": "wu_jutong", "source": "classic",
                  "selected": True}},
        {"data": {"id": "formula::zhang_xichun::自拟和胃汤", "label": "自拟和胃汤",
                  "layer": 3, "node_type": "formula", "phys": "zhang_xichun",
                  "source": "composed", "selected": True}},
        {"data": {"id": "herb::ye_tianshi::柴胡", "label": "柴胡 6g", "layer": 4,
                  "node_type": "herb", "phys": "ye_tianshi",
                  "parent": "formula::ye_tianshi::柴胡疏肝散"}},
        {"data": {"id": "herb::wu_jutong::黄连", "label": "黄连 3g", "layer": 4,
                  "node_type": "herb", "phys": "wu_jutong",
                  "parent": "formula::wu_jutong::左金丸"}},
        {"data": {"id": "herb::zhang_xichun::赭石", "label": "生赭石 18g", "layer": 4,
                  "node_type": "herb", "phys": "zhang_xichun",
                  "parent": "formula::zhang_xichun::自拟和胃汤"}},
    ],
    "edges": [
        {"data": {"source": "sym::胃脘胀痛", "target": "elem::肝郁"}},
        {"data": {"source": "elem::肝郁", "target": "syn::ye_tianshi", "phys": "ye_tianshi"}},
        {"data": {"source": "elem::肝郁", "target": "syn::wu_jutong", "phys": "wu_jutong"}},
        {"data": {"source": "elem::肝郁", "target": "syn::zhang_xichun", "phys": "zhang_xichun"}},
        {"data": {"source": "syn::ye_tianshi", "target": "formula::ye_tianshi::柴胡疏肝散"}},
        {"data": {"source": "syn::wu_jutong", "target": "formula::wu_jutong::左金丸"}},
        {"data": {"source": "syn::zhang_xichun", "target": "formula::zhang_xichun::自拟和胃汤"}},
    ],
    "dropped_edges": 0,
}

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
    "graph": {"nodes": [n for n in STUDENT_GRAPH["nodes"] if n["data"]["layer"] <= 2],
              "edges": [e for e in STUDENT_GRAPH["edges"]
                        if not e["data"]["target"].startswith("formula::")],
              "dropped_edges": 0},
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
        " renderConsultResult({...DONE_PAYLOAD, graph: SIX_LAYER_GRAPH});"
        " document.getElementById('detail-zone').open = true;"
        " skipAnimation();",
        """async () => {
          await new Promise(r => setTimeout(r, 1200));
          if (!cy) return '画布没建起来';
          if (cy.nodes().length < 11)
            return '图没长全，只有 ' + cy.nodes().length + ' 个节点';
          // §3.2 规格 1：方剂框按来源区分边框。用的是既有的 source 字段。
          const modified = cy.getElementById('formula::ye_tianshi::柴胡疏肝散');
          const composed = cy.getElementById('formula::zhang_xichun::自拟和胃汤');
          const classic = cy.getElementById('formula::wu_jutong::左金丸');
          for (const [n, name] of [[modified,'modified'],[composed,'composed'],[classic,'classic']]) {
            if (!n.length) return '找不到 ' + name + ' 那个方剂节点';
          }
          if (modified.style('border-style') !== 'dashed') return 'modified 不是虚线';
          if (composed.style('border-style') !== 'dotted') return 'composed 不是点线';
          if (classic.style('border-style') !== 'solid') return 'classic 不是实线';
          // §3.2 规格 2：λ1 说明必须在图上。
          const note = document.getElementById('cy-lambda1-note');
          if (!note.classList.contains('show') || !note.textContent.trim())
            return '图上没有 λ1 说明';
          // §3.2 规格 11：证素比别的节点大一号。
          const el = cy.getElementById('elem::肝郁');
          const sym = cy.getElementById('sym::胃脘胀痛');
          if (!(parseFloat(el.style('font-size')) > parseFloat(sym.style('font-size'))))
            return '证素字号没有比症状大';
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
        "document.getElementById('role-select').value = 'student';"
        " renderComplaintBody(COMPLAINT);"
        " renderConsultResult({...DONE_PAYLOAD, graph: STUDENT_GRAPH});",
        """async () => {
          await new Promise(r => setTimeout(r, 1200));
          if (!cy) return '画布没建起来';
          handleSymptomClick('sym::胃脘胀痛');
          await new Promise(r => setTimeout(r, 400));
          const faded = cy.nodes('.gt-faded').map(n => n.id());
          const lit = cy.nodes().not('.gt-faded').map(n => n.id());
          // 三跳：症状 → 证素 → 证型 → 方剂。起点那条链全亮，另一条链全淡。
          for (const id of ['sym::胃脘胀痛', 'el::肝郁', 'syn::ye_tianshi',
                            'formula::ye_tianshi::柴胡疏肝散']) {
            if (!lit.includes(id)) return id + ' 应该亮着，实际被淡化了';
          }
          for (const id of ['sym::口苦', 'el::湿热', 'syn::wu_jutong',
                            'formula::wu_jutong::龙胆泻肝汤']) {
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
          const lines = eg.querySelectorAll('.eg-line');
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
          // 正好两环：所有节点到中心的距离聚成两簇
          const box = gbCy.extent();
          const cx = (box.x1 + box.x2) / 2, cy2 = (box.y1 + box.y2) / 2;
          const d = (n) => Math.hypot(n.position('x') - cx, n.position('y') - cy2);
          const hubs = gbCy.nodes().filter(n => window.__hubIds.includes(n.id()));
          const others = gbCy.nodes().filter(n => !window.__hubIds.includes(n.id()));
          if (!others.length) return '展开之后外圈是空的';
          const inner = hubs.map(d).reduce((a, b) => a + b, 0) / hubs.length;
          const outer = others.map(d).reduce((a, b) => a + b, 0) / others.length;
          if (!(inner < outer)) return '枢纽没在内圈（内 ' + Math.round(inner)
            + ' / 外 ' + Math.round(outer) + '）';
          // 0.25 而不是 R16 那条 0.15：**这一轮当场量过**，两环在模型坐标里是
          // 内 649 / 外 1972（比值 0.329）。判据卡在实测值下面一点点，
          // 既能抓住"内圈被压回中心"的回归，又不会因为节点数变化而假红。
          if (!(inner > outer * 0.25)) return '内圈被压扁了（内 ' + Math.round(inner)
            + ' / 外 ' + Math.round(outer) + '，比值 ' + (inner / outer).toFixed(3) + '）';
          // 外圈是一簇而不是好几圈：半径的相对标准差要小
          const rs = others.map(d);
          const mean = rs.reduce((a, b) => a + b, 0) / rs.length;
          const sd = Math.sqrt(rs.reduce((s, r) => s + (r - mean) ** 2, 0) / rs.length);
          if (sd / mean > 0.35) return '外圈散成了好几环（相对标准差 '
            + (sd / mean).toFixed(2) + '）';
          return null;
        }""",
    ),
}


def run(only: str | None, wait_ms: int) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("缺 playwright：pip install playwright（浏览器本机已有，不要跑 "
              "playwright install）", file=sys.stderr)
        return 2

    names = [only] if only else list(STATES)
    port = _free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                page = browser.new_page(viewport=VIEWPORT)
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/app/index.html", wait_until="networkidle")
                # 把这一批构造好的响应体注进页面的全局作用域，setup 脚本直接引用。
                for var, value in (("DONE_PAYLOAD", DONE_PAYLOAD),
                                   ("PATIENT_PAYLOAD", PATIENT_PAYLOAD),
                                   ("PATIENT_HIGH_PAYLOAD", PATIENT_HIGH_PAYLOAD),
                                   ("STUDENT_GRAPH", STUDENT_GRAPH),
                                   ("DOCTOR_SAFETY", DOCTOR_SAFETY),
                                   ("SIX_LAYER_GRAPH", SIX_LAYER_GRAPH),
                                   ("REFERENCE_HEALTH", REFERENCE_HEALTH),
                                   ("REFERENCE_FIXTURE", REFERENCE_FIXTURE),
                                   # R24：建议层 + token 面板 + 今日用量
                                   ("R24_DONE_PAYLOAD", R24_DONE_PAYLOAD),
                                   ("R24_USAGE", R24_USAGE),
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
    args = ap.parse_args(argv)
    return run(args.only, args.wait_ms)


if __name__ == "__main__":
    raise SystemExit(main())
