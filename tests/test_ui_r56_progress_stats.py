"""R56 §6 第 3 条：`describeProgressEvent` 的 `s3_done` 分支不该在产品面
印出流式遥测（N 帧流式 / 首字 Xs / 正文 N 字 / 思考 N 字）——那是给排查
问题看的实现细节，医师读不出信息，只会觉得系统在讲听不懂的话。

产品面只报"完成"；研究模式（`test_r36_acceptance.py` 已经钉住）保留全部数字，
那边是给调优、排查卡顿用的，两边回答的不是同一个问题（CLAUDE.md「同一概念
只能有一处实现」的例外条款）。
"""
from __future__ import annotations

import json
import subprocess

from tests.web_harness import DOM_STUB, js_tmp, load_app_js

APP = load_app_js()


def _describe(data: dict, product_mode: bool) -> str:
    js = (
        f"isProductMode = () => {str(product_mode).lower()};\n"
        f'process.stdout.write(JSON.stringify(describeProgressEvent("s3_done", '
        f"{json.dumps(data, ensure_ascii=False)})));"
    )
    proc = subprocess.run(["node", js_tmp(DOM_STUB + APP + "\n" + js)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout)


STREAMED = {"physician_name": "叶天士", "events": 7, "first_delta_s": 1.2,
            "chars_content": 300, "chars_reasoning": 40}
NOSTREAM = {"physician_name": "叶天士", "events": 0,
            "streaming_note": "后端 fake 不支持流式输出"}


def test_product_mode_drops_the_telemetry_numbers():
    out = _describe(STREAMED, product_mode=True)
    assert "叶天士" in out and "完成" in out
    for leak in ("帧流式", "首字", "正文", "思考", "chars_content"):
        assert leak not in out, f"产品面泄漏了遥测字段：{leak}"


def test_product_mode_no_stream_case_also_stays_simple():
    """没流式（fake 后端 / 不支持）时产品面同样不解释原因——那句"无增量：
    后端 fake 不支持流式输出"是讲给排查问题的人听的。"""
    out = _describe(NOSTREAM, product_mode=True)
    assert "叶天士" in out and "完成" in out
    assert "无增量" not in out and "不支持流式" not in out


def test_research_mode_keeps_the_full_numbers():
    out = _describe(STREAMED, product_mode=False)
    assert "7 帧" in out and "1.2s" in out and "300 字" in out and "思考 40 字" in out


def test_research_mode_still_explains_why_there_was_no_stream():
    out = _describe(NOSTREAM, product_mode=False)
    assert "无增量" in out and "不支持流式" in out
