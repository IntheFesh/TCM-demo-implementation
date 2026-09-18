"""R55 §5.2：`s3_done` 后 120 秒仍未收到 `done`，前端主动 GET 兜底。

真机症状：worker 线程早就跑完、`_done` 已经算出来、`stream.finish()` 也执行
过了——但这一帧在网络上丢了（nginx/CDN 截断、代理挤占空闲连接），前端只能
干等到 300 秒的整体看门狗超时才报错，而实际上结果几分钟前就已经有了。

后端一半：`api/main.py` 的 `_cache_finished_result`/`_get_finished_result`
——这张表**跟 `_streams` 分开**，因为 `_ConsultStream.finish()` 一收尾就把
stream_id 从 `_streams` 里 pop 掉了，兜底恰恰发生在流大概率已经跑完之后。
前端一半：`web/app.js` 的 `armS3DoneFallback`/`fetchDoneFallback`——收到
`s3_done` 就挂一个 120 秒定时器，`done`/`error` 到达或者定时器自己触发时
清掉。
"""
from __future__ import annotations

import json
import subprocess
import time

from fastapi.testclient import TestClient

import api.main as api_main
from tests.web_harness import DOM_STUB, js_tmp, load_app_js


# ---------- 后端：短期缓存 + 兜底 GET 端点 ----------


def test_a_cached_result_can_be_fetched_back_by_stream_id():
    api_main._cache_finished_result("sid-1", {"ok": True, "syndrome": "脾胃气虚"})
    client = TestClient(api_main.app)
    resp = client.get("/api/consult/stream/sid-1/result")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "syndrome": "脾胃气虚"}


def test_an_unknown_stream_id_is_404_not_500():
    client = TestClient(api_main.app)
    resp = client.get("/api/consult/stream/never-existed/result")
    assert resp.status_code == 404


def test_an_expired_entry_is_treated_as_not_found():
    """`_FINISHED_RESULT_TTL_SECONDS` 之后的条目不该还能查到——这不是一个
    通用的历史问诊存档，是一次性的兜底缓存。"""
    api_main._cache_finished_result("sid-old", {"ok": True})
    # 直接改写记录的时间戳到很久以前，不用真的睡 TTL 那么久。
    with api_main._finished_results_lock:
        _, done = api_main._finished_results["sid-old"]
        api_main._finished_results["sid-old"] = (
            time.monotonic() - api_main._FINISHED_RESULT_TTL_SECONDS - 1, done,
        )
    assert api_main._get_finished_result("sid-old") is None
    client = TestClient(api_main.app)
    resp = client.get("/api/consult/stream/sid-old/result")
    assert resp.status_code == 404


def test_writing_a_new_entry_prunes_expired_ones():
    """`_cache_finished_result` 顺手清过期条目——不为这张表单开一个定时
    任务，问诊频率本来就不高，搭在下一次写入时扫一遍足够。"""
    api_main._cache_finished_result("sid-stale", {"ok": True})
    with api_main._finished_results_lock:
        _, done = api_main._finished_results["sid-stale"]
        api_main._finished_results["sid-stale"] = (
            time.monotonic() - api_main._FINISHED_RESULT_TTL_SECONDS - 1, done,
        )
    api_main._cache_finished_result("sid-fresh", {"ok": True})
    with api_main._finished_results_lock:
        assert "sid-stale" not in api_main._finished_results
        assert "sid-fresh" in api_main._finished_results


def test_the_result_written_to_cache_is_the_same_shape_the_done_event_carries():
    """兜底 GET 拿到的必须跟正常路径的 `done` 事件是**同一份数据**——不是
    另算一份、形状可能对不上的东西。这里只断言"写进去的是什么就原样拿
    得出来"，真正的写入调用点（stream worker 里 `_cache_finished_result
    (stream.stream_id, _done)`）由源码位置核对，见下面那条测试。"""
    payload = {"results": [{"physician": "ye_tianshi"}], "manifest": {"llm_calls": 5}}
    api_main._cache_finished_result("sid-shape", payload)
    client = TestClient(api_main.app)
    assert client.get("/api/consult/stream/sid-shape/result").json() == payload


def test_the_cache_write_happens_before_the_done_event_is_queued():
    """源码位置断言：`_cache_finished_result(...)` 必须出现在
    `stream.events_q.put(("done", _done))` 之前——顺序反过来的话，
    `done` 发出去和缓存写入之间有一个窗口，前端的 120 秒兜底如果恰好落在
    这个窗口里会扑空，得到一个本不该有的 404。"""
    import inspect

    src = inspect.getsource(api_main.api_consult_stream)
    cache_idx = src.index("_cache_finished_result(stream.stream_id, _done)")
    done_idx = src.index('stream.events_q.put(("done", _done))')
    assert cache_idx < done_idx


# ---------- 前端：120 秒定时器的挂起/清除 ----------


def _run(js_tail: str) -> str:
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + load_app_js() + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _json(expr: str):
    return json.loads(_run(f"process.stdout.write(JSON.stringify({expr}));"))


def _json_async(expr: str):
    """跟 `_json` 一样，但 `expr` 是个返回 Promise 的表达式——纯 CommonJS
    脚本（`node file.js`）不支持顶层 `await`，用 `.then()` 接住结果再写
    stdout。`fetchDoneFallback` 本身是 async 函数，测它必须走这条。"""
    return json.loads(_run(
        f"Promise.resolve({expr}).then(v => process.stdout.write(JSON.stringify(v)));"
    ))


def test_the_fallback_delay_is_120_seconds():
    assert _json("S3_DONE_FALLBACK_MS") == 120000


def test_arming_with_a_stream_id_sets_a_pending_timer():
    # **必须在同一个 node 进程退出前 clear 掉**：`setTimeout` 排的是一个真实
    # 120 秒的定时器，Node 的事件循环不会因为脚本主体跑完就退出，会一直挂到
    # 定时器触发或被 clearTimeout——不清掉的话这条测试会真的挂 120 秒（被
    # subprocess 的 30 秒超时杀掉，看起来像"node 挂了"，其实是这一个原因）。
    got = _json("""(() => {
      clearS3DoneFallback();
      armS3DoneFallback("sid-x");
      const wasArmed = s3DoneFallbackTimer !== null;
      clearS3DoneFallback();
      return wasArmed;
    })()""")
    assert got is True


def test_arming_without_a_stream_id_does_not_set_a_timer():
    """理论上到不了——`stream_id` 事件总在 `s3_done` 之前到达——但如果哪天
    这个前提被破坏，不该在没有 id 的情况下挂一个注定失败的定时器。"""
    got = _json("""(() => {
      clearS3DoneFallback();
      armS3DoneFallback(null);
      return s3DoneFallbackTimer;
    })()""")
    assert got is None


def test_clearing_removes_the_pending_timer():
    got = _json("""(() => {
      armS3DoneFallback("sid-y");
      clearS3DoneFallback();
      return s3DoneFallbackTimer;
    })()""")
    assert got is None


def test_fetch_done_fallback_calls_the_correct_url_and_renders_the_result():
    """不等真的 120 秒——直接调 `fetchDoneFallback`（`armS3DoneFallback`
    到时间之后调的就是这个函数），核对它打对了 URL、拿到结果之后真的调了
    `renderConsultResult`、并且设置了 `resolvedByFallback`。"""
    got = _json_async("""(async () => {
      const calls = [];
      globalThis.fetch = (url) => {
        calls.push(url);
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ marker: "x" }) });
      };
      let rendered = null;
      renderConsultResult = (data) => { rendered = data; };
      // appendProgress 摸真实的 el.textContent（字符串拼接），DOM_STUB 的
      // Proxy 对 `+=` 这种需要把属性值转成原始类型的操作会抛
      // TypeError（这是 DOM_STUB 本身的已知边界，见 tests/web_harness.py
      // 的文档：它接得住"查/赋值"，接不住"读出来再参与运算"）——这里只测
      // fetchDoneFallback 自己的分支逻辑，不测日志真写了什么，所以直接
      // 桩掉，不依赖 DOM_STUB 兜底这类操作。
      appendProgress = () => {};
      currentStreamId = "sid-z";
      resolvedByFallback = false;
      await fetchDoneFallback("sid-z");
      return { calls, rendered, resolvedByFallback };
    })()""")
    assert got["calls"] == ["/api/consult/stream/sid-z/result"]
    assert got["rendered"] == {"marker": "x"}
    assert got["resolvedByFallback"] is True


def test_fetch_done_fallback_does_nothing_on_a_stale_stream_id():
    """兜底触发的时候用户可能已经手动开始了下一次问诊——`currentStreamId`
    这时候已经变了，旧的兜底结果不该覆盖新问诊的界面。"""
    got = _json_async("""(async () => {
      globalThis.fetch = () => Promise.resolve({
        ok: true, json: () => Promise.resolve({ marker: "stale" }),
      });
      let rendered = null;
      renderConsultResult = (data) => { rendered = data; };
      currentStreamId = "sid-new";  // 已经是下一次问诊的 id 了
      resolvedByFallback = false;
      await fetchDoneFallback("sid-old");
      return { rendered, resolvedByFallback };
    })()""")
    assert got["rendered"] is None
    assert got["resolvedByFallback"] is False


def test_fetch_done_fallback_is_silent_on_a_404():
    """还没跑完（或者压根没有这个 id）——不额外报错，继续按原计划让主看门狗
    （300 秒）兜底。"""
    got = _json_async("""(async () => {
      globalThis.fetch = () => Promise.resolve({ ok: false, status: 404 });
      let rendered = null;
      renderConsultResult = (data) => { rendered = data; };
      resolvedByFallback = false;
      await fetchDoneFallback("sid-not-ready");
      return { rendered, resolvedByFallback };
    })()""")
    assert got["rendered"] is None
    assert got["resolvedByFallback"] is False
