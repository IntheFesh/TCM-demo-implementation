"""web/index.html 里 SSE 分步进度这部分前端代码的离线测试。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_western_drugs.py /
test_graph.py 里的 _run_* helper 同一个模式），测的是真实上线的代码，不是在
测试里另抄一份实现。不测真实网络请求——那是 curl -N 对真实 uvicorn 的事，
这里只测两段跟传输方式无关、可以纯函数化验证的逻辑：
  1. describeProgressEvent()：SSE 事件 -> 人看得懂的一行日志
  2. readSSE()：SSE 帧（含被拆成多个 chunk 的情况）-> 有序的 (event, data) 回调
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
"""


def _run_node(js_tail: str) -> str:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    proc = subprocess.run(
        ["node", "-e", DOM_STUB + script + "\n" + js_tail],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


# ---------- describeProgressEvent ----------


def test_describe_progress_event_covers_every_event_consult_emits():
    """core/chain.py::consult() 的 on_step 会发这七种事件（不含 stream_id /
    need_input / done / error，那四种在 submitConsult 里单独处理，不走这个
    翻译函数）——这里逐一喂一遍，钉住"新加一种事件却忘了在前端翻译"这类漏改。
    """
    cases = [
        ("s1_done", {"symptoms": ["纳差", "乏力"]}, "纳差、乏力"),
        ("s2_done", {"elements": [{"element": "脾"}], "unexplained_symptoms": []}, "脾"),
        ("followup_done", {"stopped_by": "max_rounds", "rounds": 3}, "3 轮"),
        ("followup_done", {"stopped_by": "no_answer", "rounds": 0}, "无提问渠道"),
        ("followup_done", {"stopped_by": "fast_mode", "rounds": 0}, "FAST_MODE"),
        ("residual_done", {"newly_explained": ["乏力"]}, "乏力"),
        ("physician_start", {"physician": "ye_tianshi", "physician_name": "叶天士"}, "叶天士"),
        ("react_step", {"physician_name": "叶天士", "step": 2, "action": "query_graph"}, "第 2 步"),
        ("s3_start", {"physician": "ye_tianshi", "physician_name": "叶天士"}, "叶天士"),
        ("physician_done", {"physician": "ye_tianshi", "physician_name": "叶天士", "syndrome": "脾虚"}, "脾虚"),
        ("followup_answered", {"question": "有没有口苦？", "answer": "没有"}, "没有"),
    ]
    js = "\n".join(
        f'process.stdout.write(JSON.stringify(describeProgressEvent({json.dumps(name)}, '
        f'{json.dumps(data, ensure_ascii=False)}))+"\\n");'
        for name, data, _ in cases
    )
    out = _run_node(js).strip().splitlines()
    assert len(out) == len(cases)
    for line, (_, _, expect_substr) in zip(out, cases):
        rendered = json.loads(line)
        assert rendered is not None, f"{expect_substr} 这条没有翻译出文字"
        assert expect_substr in rendered, f"{rendered!r} 里没有 {expect_substr!r}"


def test_describe_progress_event_returns_null_for_transport_level_events():
    """stream_id / need_input / done / error 这四种不该被这个函数处理——
    它们在 submitConsult 里单独分支，不进 progress-log。返回 null 而不是
    抛异常或返回空字符串：submitConsult 用 `if (line) appendProgress(line)`
    判断要不要追加，null 和空字符串对这个判断是等价的，但 null 更明确地
    表达"这个事件本来就不该走这条翻译"，不是"翻译出来是空的"。"""
    js = "\n".join(
        f'process.stdout.write(JSON.stringify(describeProgressEvent({json.dumps(n)}, {{}}))+"\\n");'
        for n in ["stream_id", "need_input", "done", "error"]
    )
    out = _run_node(js).strip().splitlines()
    assert [json.loads(line) for line in out] == [None, None, None, None]


# ---------- readSSE ----------


def _fake_response_js(chunks: list[str]) -> str:
    """构造一个最小的、跟 fetch Response 形状一致的假对象：body.getReader() 按
    传入的 chunks 数组逐个 read()。用来测"一帧被拆成两个 chunk"这种真实网络
    场景——如果 readSSE 按"\\n\\n"简单 split 一次就完事，这种情况会漏帧或
    把半帧当整帧解析崩掉。"""
    encoded = json.dumps(chunks, ensure_ascii=False)
    return f"""
    const _chunks = {encoded}.map((s) => new TextEncoder().encode(s));
    let _i = 0;
    const fakeResponse = {{
      body: {{
        getReader() {{
          return {{
            read: async () => {{
              if (_i < _chunks.length) return {{ done: false, value: _chunks[_i++] }};
              return {{ done: true, value: undefined }};
            }},
          }};
        }},
      }},
    }};
    """


def test_read_sse_parses_multiple_events_in_one_chunk():
    js = _fake_response_js([
        'event: s1_done\ndata: {"symptoms":["纳差"]}\n\n'
        'event: s2_done\ndata: {"elements":[]}\n\n'
    ]) + """
    (async () => {
      const seen = [];
      await readSSE(fakeResponse, (name, data) => seen.push([name, data]));
      process.stdout.write(JSON.stringify(seen));
    })();
    """
    out = json.loads(_run_node(js))
    assert out == [["s1_done", {"symptoms": ["纳差"]}], ["s2_done", {"elements": []}]]


def test_read_sse_reassembles_a_frame_split_across_chunks():
    """真实网络里一帧完全可能被 TCP 分段切断在任意位置——这里故意切在
    "data: " 这个字段名中间，逼 readSSE 的缓冲区拼接逻辑真的生效。"""
    frame = 'event: need_input\ndata: {"question":"有没有口苦？"}\n\n'
    cut = frame.index("data")
    js = _fake_response_js([frame[:cut], frame[cut:]]) + """
    (async () => {
      const seen = [];
      await readSSE(fakeResponse, (name, data) => seen.push([name, data]));
      process.stdout.write(JSON.stringify(seen));
    })();
    """
    out = json.loads(_run_node(js))
    assert out == [["need_input", {"question": "有没有口苦？"}]]


def test_read_sse_handles_multibyte_utf8_split_across_chunk_boundary():
    """中文字符在 UTF-8 里是多字节，切分点如果恰好落在一个汉字的字节中间，
    TextDecoder 不用 {stream:true} 的话会直接把半个字符解码成乱码或抛异常。
    这条专门切在"叶天士"这个词的字节中间。"""
    payload = json.dumps({"physician_name": "叶天士"}, ensure_ascii=False)
    frame = f"event: physician_start\ndata: {payload}\n\n"
    encoded = frame.encode("utf-8")
    # 找一个多字节字符内部的切点：定位到 "叶" 的第一个字节之后一个字节处
    idx_in_payload = frame.index("叶")
    byte_offset = len(frame[:idx_in_payload].encode("utf-8")) + 1  # 切在"叶"字节中间
    js = f"""
    const _chunks = [
      new Uint8Array({list(encoded[:byte_offset])}),
      new Uint8Array({list(encoded[byte_offset:])}),
    ];
    let _i = 0;
    const fakeResponse = {{
      body: {{ getReader() {{
        return {{ read: async () => {{
          if (_i < _chunks.length) return {{ done: false, value: _chunks[_i++] }};
          return {{ done: true, value: undefined }};
        }} }};
      }} }},
    }};
    (async () => {{
      const seen = [];
      await readSSE(fakeResponse, (name, data) => seen.push([name, data]));
      process.stdout.write(JSON.stringify(seen));
    }})();
    """
    out = json.loads(_run_node(js))
    assert out == [["physician_start", {"physician_name": "叶天士"}]]


# ---------- 模块8：检索模式跟着请求走 ----------


def _build_body(mode_value: str, role_value: str = "researcher") -> dict:
    """真实跑 index.html 里的 buildConsultRequestBody()，用一个只回答
    #retriever-mode / #role-select 的 document 桩喂给它。role_value 默认
    "researcher"——M7 之前的测试只关心 retriever_mode，这个默认值让那些
    既有断言不用逐个改就能继续只盯 retriever_mode 那一个字段（role 这时
    固定是 "researcher"，下面统一在期望值里带上）。"""
    js = f"""
    globalThis.document = {{
      getElementById: (id) => {{
        if (id === "retriever-mode") return {{ value: {json.dumps(mode_value)} }};
        if (id === "role-select") return {{ value: {json.dumps(role_value)} }};
        return null;
      }},
    }};
    process.stdout.write(JSON.stringify(buildConsultRequestBody("纳差乏力")));
    """
    return json.loads(_run_node(js))


def test_request_body_omits_retriever_mode_when_default_selected():
    """选"默认"时 retriever_mode 整个字段都不带——不是传空字符串。空字符串
    不在 ALLOWED_MODES 里，传过去会被判成非法模式名直接 400。role 字段
    是 M7 新加的，跟 retriever_mode 不是同一个契约：role-select 永远有个
    合法选中值（不存在"用服务端默认，所以不传这个字段"的语义），所以
    始终显式带上，这条断言只钉 retriever_mode 被省略，不要求 role 也被省略。"""
    body = _build_body("")
    assert body == {"complaint": "纳差乏力", "role": "researcher"}
    assert "retriever_mode" not in body


def test_request_body_carries_the_selected_mode():
    for mode in ["hybrid", "dense", "bm25", "graph"]:
        body = _build_body(mode)
        assert body["retriever_mode"] == mode
        assert body["complaint"] == "纳差乏力"


def test_request_body_carries_the_selected_role():
    """M7：role 跟 retriever_mode 一样逐请求带上，不落进程状态（同一条理由，
    见 buildConsultRequestBody 里的注释）。"""
    for role in ["researcher", "student"]:
        body = _build_body("", role_value=role)
        assert body["role"] == role
        assert body["complaint"] == "纳差乏力"


def test_request_body_survives_a_page_without_the_selector():
    """选择框（含 role-select）都不存在时（比如将来某个精简页面）不该抛异常，
    role 退回代码里写死的默认值 "researcher"——跟 getSelectedRole() 的
    `sel ? sel.value : "researcher"` 兜底是同一处逻辑，这里验证的就是这条
    兜底真的生效，不是页面缺个选择框就直接抛异常把整个提交流程打断。"""
    js = """
    globalThis.document = { getElementById: () => null };
    process.stdout.write(JSON.stringify(buildConsultRequestBody("纳差")));
    """
    assert json.loads(_run_node(js)) == {"complaint": "纳差", "role": "researcher"}


# ---------- 企业化整改：XSS 与 EVAL_MODE 横幅 ----------


def test_card_html_escapes_hallucinated_case_ids():
    """hallucinated 里的 id 是模型原样吐出来的字符串（core/chain.py 里
    cited_case_ids 减去检索结果），之前是整个卡片里唯一一处没过 escapeHtml
    就进 innerHTML 的插值——恰恰因为它是"不可信输出"的警告。"""
    result = {
        "physician": "ye_tianshi", "physician_name": "叶天士",
        "s3": {"syndrome": "脾虚", "reasoning": "r", "treatment_principle": "t",
               "herbs": ["党参"], "cited_case_ids": ["<img src=x onerror=alert(1)>"]},
        "hallucinated": ["<img src=x onerror=alert(1)>"],
        "refs": [],
    }
    js = f"process.stdout.write(cardHtml({json.dumps(result, ensure_ascii=False)}));"
    out = _run_node(js)
    assert "<img" not in out
    assert "&lt;img src=x onerror=alert(1)&gt;" in out


def test_escape_html_also_escapes_quotes():
    js = 'process.stdout.write(escapeHtml(`a"b\'c<d>&e`));'
    assert _run_node(js) == "a&quot;b&#39;c&lt;d&gt;&amp;e"


def test_describe_safety_flag():
    """safety_flag 非空 = 服务端开着 EVAL_MODE、这条主诉本该被拦。之前 JSON 里
    带着这个字段但页面上什么都不显示，方药照常开。"""
    js = """
    process.stdout.write(JSON.stringify([
      describeSafetyFlag(null), describeSafetyFlag(""), describeSafetyFlag("柏油样便"),
    ]));
    """
    out = json.loads(_run_node(js))
    assert out[0] is None and out[1] is None
    assert "EVAL_MODE" in out[2] and "柏油样便" in out[2] and "不产出任何方药" in out[2]
