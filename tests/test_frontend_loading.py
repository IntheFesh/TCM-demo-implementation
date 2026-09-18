"""R41：首屏加载路径的判据。**这一轮量到的第一个数就在这里。**

R41 基线（`scripts/profile_frontend.py`，真 Chromium）：首屏 **10 个请求、
1,458,054 字节、2 个渲染阻塞**，其中 `cytoscape.min.js` 是 `<head>` 里一个同步
的 CDN `<script>`，`renderBlockingStatus: "blocking"`、**240 ms**，而那 373 KB
只有图谱页要用。三甲内网取不到 cdnjs，那条请求会一直挂到超时。

这个文件把"怎么加载"变成机器判据：哪些东西不许在首屏取、哪些必须并行取、
哪些必须能被缓存住。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from tests.web_harness import SCRIPT_FILES, load_html

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(api_main.app)


# ---------- 首屏不取什么 ----------


def test_no_external_origin_is_referenced_at_all():
    """**一个外网地址都不许有。** 断网演示（回放模式）是这条路线存在的理由，
    而三甲内网根本出不去。一个 `https://` 引用就够让首屏挂到超时。"""
    html = load_html()
    for bad in ("https://cdnjs", "https://cdn.", "http://cdn", "//fonts.googleapis",
                "//unpkg.com", "//jsdelivr"):
        assert bad not in html, f"index.html 又引了外网资源：{bad}"


def test_the_head_has_no_script_tag():
    """`<head>` 里的同步 `<script>` 会**挡住解析**。实测那一个挡了 240 ms。"""
    head = load_html().split("<body")[0]
    assert "<script" not in head


def test_cytoscape_is_only_loaded_on_demand_from_the_local_copy():
    graph_js = (WEB / "graph.js").read_text(encoding="utf-8")
    assert 'el.src = "vendor/cytoscape.min.js"' in graph_js
    # 判据是 HTML 里没有**引用** cytoscape 的标签，不是"没出现这个词"
    # （注释里提它正是在解释为什么不在这里加载）。
    html = load_html()
    assert "cytoscape.min.js" not in html.replace(
        "<!--", "\n<!--").split("<!--")[0] + "".join(
        seg.split("-->", 1)[1] for seg in html.split("<!--")[1:] if "-->" in seg), (
        "HTML 的非注释部分又直接引 cytoscape 了")


def test_the_local_cytoscape_copy_actually_exists_and_is_not_a_stub():
    """按需加载的前提是本地副本真的在。它不在的话图**永远**画不出来
    （CDN 那一路已经去掉了），而那比"慢"严重得多。"""
    f = WEB / "vendor" / "cytoscape.min.js"
    assert f.exists(), "web/vendor/cytoscape.min.js 不在——图永远画不出来"
    assert f.stat().st_size > 200_000, f"只有 {f.stat().st_size} 字节，不像完整的库"


def test_the_graph_library_missing_message_no_longer_blames_the_cdn():
    """错误提示要指向真正的原因。还说"CDN 不可达"会把人引去查网络，
    而 CDN 那一路已经不存在了。"""
    graph_js = (WEB / "graph.js").read_text(encoding="utf-8")
    i = graph_js.index("CYTOSCAPE_MISSING_MSG")
    msg = graph_js[i:i + 400]
    assert "vendor/cytoscape.min.js" in msg
    assert "CDN 不可达" not in msg


# ---------- 首屏怎么取 ----------


@pytest.mark.parametrize("name", SCRIPT_FILES)
def test_every_script_is_deferred(name):
    html = load_html()
    i = html.index(f'<script src="{name}"')
    tag = html[i:html.index(">", i) + 1]
    assert " defer" in tag


def test_exactly_two_fonts_are_preloaded_and_they_are_the_first_screen_ones():
    """四个字重共 1.13 MB（首屏字节数的 77%）。preload **只给首屏那两个**：
    preload 是"现在就下"，四个全 preload 会把带宽从要用的那两个身上抢走。"""
    preloaded = [ln for ln in load_html().splitlines() if 'rel="preload"' in ln]
    assert len(preloaded) == 2
    assert any("noto-serif-sc-400" in ln for ln in preloaded)
    assert any("noto-sans-sc-400" in ln for ln in preloaded)


def test_font_preloads_carry_crossorigin():
    """字体请求本身是匿名 CORS 模式。preload 不带 `crossorigin` 的话两个请求的
    缓存键不一样，浏览器会**再下一遍**——那是净亏。"""
    for ln in load_html().splitlines():
        if 'rel="preload"' in ln:
            assert "crossorigin" in ln and 'as="font"' in ln


def test_all_four_font_faces_use_display_swap():
    """`font-display: swap`：取不到字体时默认的 `block` 会留最多 3 秒空白，
    而这个项目要能断网演示。"""
    css = (WEB / "app.css").read_text(encoding="utf-8")
    # 数**声明**（行首的 `@font-face {`），不数注释里提到这个词的那一处
    # ——两种数法在这个文件里差 1，跟 CLAUDE.md 那条 min_length 的数法纪律同理。
    n_faces = sum(1 for ln in css.splitlines() if ln.strip().startswith("@font-face"))
    assert n_faces == 4, f"字体面数变了（{n_faces}），preload 那条判据要跟着看"
    # `>=` 而不是 `==`：注释里也提到这个词（那段注释解释的正是为什么必须 swap）。
    assert css.count("font-display: swap") >= n_faces


def test_the_fonts_are_local_not_from_a_cdn():
    css = (WEB / "app.css").read_text(encoding="utf-8")
    assert css.count('url("vendor/fonts/') == 4
    assert "fonts.googleapis" not in css and "fonts.gstatic" not in css


# ---------- 缓存 ----------


def test_vendor_assets_are_immutable_for_a_year(client):
    """字体 1.13 MB + cytoscape 373 KB = 首屏字节数的 84%。内容跟文件名绑定
    （子集是 subset_fonts.py 的产物、cytoscape 带版本），所以可以 immutable。
    二次访问实测只取 3,941 字节（**−99.7%**）。"""
    for path in ("/app/vendor/cytoscape.min.js",
                 "/app/vendor/fonts/noto-sans-sc-400-subset.woff2"):
        cc = client.get(path).headers.get("cache-control", "")
        assert "immutable" in cc and "max-age=31536000" in cc, f"{path} → {cc}"


@pytest.mark.parametrize("path", ["/app/index.html", "/app/app.js", "/app/app.css",
                                  "/app/graph.js"])
def test_our_own_code_is_no_cache_so_an_upgrade_takes_effect(client, path):
    """**自家代码必须每次问服务器。** 给 max-age 的话，升级之后医生刷新页面
    还是旧的 JS 配新的后端——那是一类最难查的故障（R45 的升级回滚靠这条）。
    `no-cache` 不是"不缓存"，是"缓存但每次带 ETag 问一句"，304 只有几十字节。"""
    assert client.get(path).headers.get("cache-control") == "no-cache"


def test_a_304_still_carries_the_cache_control_header(client):
    """条件请求的响应也要带策略头——否则浏览器在 304 之后又回到"自己猜新鲜期"。"""
    first = client.get("/app/vendor/cytoscape.min.js")
    etag = first.headers.get("etag")
    assert etag
    second = client.get("/app/vendor/cytoscape.min.js", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert "immutable" in second.headers.get("cache-control", "")


def test_the_cache_prefix_list_is_explicit_not_a_guess():
    """哪些路径算"不变的"是一张**明确的表**，不是按扩展名猜。
    按扩展名猜的话 app.js 也是 .js，会被当成 immutable，升级就失效了。"""
    assert api_main.CACHE_IMMUTABLE_PREFIXES == ("vendor/",)
    assert api_main.CACHE_IMMUTABLE_SECONDS == 31536000


def test_api_responses_are_not_touched_by_the_static_cache_policy(client):
    """缓存策略只管静态挂载点。问诊结果**绝不能**被缓存——那会让第二个患者
    看到第一个患者的方。"""
    resp = client.get("/health")
    assert "immutable" not in resp.headers.get("cache-control", "")


def test_the_web_root_mount_uses_the_caching_subclass():
    """接线判据：策略类写了但没挂上去，上面那些 header 断言会红——
    这一条是为了让"为什么红"一眼看得出来。"""
    mounts = [r for r in api_main.app.routes if getattr(r, "name", None) == "web"]
    assert mounts, "/app 挂载点不见了"
    assert isinstance(mounts[0].app, api_main._CachingStatic)
