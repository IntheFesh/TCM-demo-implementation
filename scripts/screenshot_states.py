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

DONE_PAYLOAD = {
    "results": RESULTS, "divergence": DIVERGENCE, "graph": GRAPH,
    "rejected": False, "reject_reason": None, "retrieval_error": None,
    "insufficient": False, "insufficient_reason": None, "safety_flag": None,
    "followup": None, "residual": None, "demo_mode": None,
    "manifest": {"model": "deepseek-v4-pro", "prompt_version": "v1",
                 "cases_sha256": "abc1234", "llm_calls": 6, "elapsed_ms": 88000},
}

# 每种状态：截图文件名 + 把页面推进那个状态的 JS + 一条 DOM 断言（返回 null 表示通过）。
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
          const segs = document.querySelectorAll('#rx-compare .rx-seg');
          if (segs.length !== 2) return 'ε 参考线不是两段';
          const noise = segs[0].getBoundingClientRect().width;
          const real = segs[1].getBoundingClientRect().width;
          if (!(noise > real)) return 'ε=0.3954 差异=0.53，噪声段该比真实段长';
          if (!document.body.innerText.includes('0.3954')) return '没显示这条主诉的 ε';
          const folds = document.querySelectorAll('.herb-fold').length;
          if (folds < 1) return '药材一处都没折叠';
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
                page.evaluate(f"window.DONE_PAYLOAD = {json.dumps(DONE_PAYLOAD, ensure_ascii=False)};"
                              f"window.COMPLAINT = {json.dumps(COMPLAINT, ensure_ascii=False)};")
                page.evaluate(setup)
                page.wait_for_timeout(wait_ms)
                out = OUT_DIR / f"r14_{name}.png"
                page.screenshot(path=str(out), full_page=True)
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
