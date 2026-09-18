"""R47 §8.1–8.2：产品模式的分派点，以及 §8.2 那张十六条清单的逐条验收。

**这个文件是唯一显式跑在产品模式下的测试集。** `tests/conftest.py` 把
`PRODUCT_MODE` 钉成 0（理由见那里），所以这里每一条要么显式 `setenv("1")`、
要么显式 `delenv` 之后验默认值——**第一条就是验默认值**，那是 R32 那条教训
（"演示跑的那个配置从来没被测过"）的防线。

十六条清单的判据分两种：
  - 后端的（内部端点、角色）：真的发请求，看状态码；
  - 前端的（要藏的那些块）：看 `internal-only` 标记在不在、CSS 藏不藏得住。
前端那几条**不靠"截图里看不见"来判**——那是 Playwright 的活（产品模式全套
截图另有一套），这里钉的是契约本身。
"""
import re

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from core.product_mode import (
    ALL_ROLES,
    INTERNAL_FEATURES,
    INTERNAL_ROLES,
    PRODUCT_ROLES,
    InternalOnly,
    available_roles,
    default_role,
    is_product_mode,
    product_flags,
    require_internal,
    resolve_role,
)
from tests.web_harness import load_app_js, load_css, load_html


@pytest.fixture
def product(monkeypatch):
    monkeypatch.setenv("PRODUCT_MODE", "1")


@pytest.fixture
def internal(monkeypatch):
    monkeypatch.setenv("PRODUCT_MODE", "0")


# ---------- 分派点本身 ----------

def test_the_default_is_product_mode(monkeypatch):
    """**产品模式是默认形态。** conftest 把它钉成 0 让几千条研究侧测试继续
    测自己本来要测的东西，这一条显式 delenv 之后验真正的默认值——钉子改不掉它。"""
    monkeypatch.delenv("PRODUCT_MODE", raising=False)
    assert is_product_mode() is True


def test_zero_turns_it_off(internal):
    assert is_product_mode() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "ON", " True "])
def test_true_words_are_all_product_mode(monkeypatch, raw):
    monkeypatch.setenv("PRODUCT_MODE", raw)
    assert is_product_mode() is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "OFF"])
def test_false_words_are_all_internal_mode(monkeypatch, raw):
    monkeypatch.setenv("PRODUCT_MODE", raw)
    assert is_product_mode() is False


def test_an_unrecognised_value_falls_back_to_product_mode_and_says_so(monkeypatch, capsys):
    """认不出的值不抛异常（一个拼错的环境变量不该让服务起不来），但也不能
    悄悄生效——按默认走 **并打一句 stderr**。"""
    monkeypatch.setenv("PRODUCT_MODE", "maybe")
    assert is_product_mode() is True
    assert "认不出" in capsys.readouterr().err


def test_require_internal_raises_in_product_mode(product):
    with pytest.raises(InternalOnly) as e:
        require_internal("usage_dashboard")
    assert e.value.feature == "usage_dashboard"
    # 异常消息给的是中文说明，不是 feature id
    assert "用量看板" in str(e.value)


def test_require_internal_passes_in_internal_mode(internal):
    assert require_internal("usage_dashboard") is None


def test_require_internal_rejects_an_unregistered_feature(product):
    """写错名字当场炸。一个永远放行的守卫比没有守卫更糟——它看起来像守着。"""
    with pytest.raises(KeyError) as e:
        require_internal("usage_dashbord")
    assert "未登记" in str(e.value)


def test_every_internal_feature_has_a_chinese_description():
    for fid, what in INTERNAL_FEATURES.items():
        assert re.search(r"[一-鿿]", what), f"{fid} 的说明不是中文"


# ---------- §8.2 第 5 条：研究者角色 ----------

def test_product_roles_are_exactly_three(product):
    assert available_roles() == PRODUCT_ROLES
    assert len(PRODUCT_ROLES) == 3
    assert "researcher" not in PRODUCT_ROLES


def test_internal_mode_keeps_all_four_roles(internal):
    assert available_roles() == ALL_ROLES
    assert set(INTERNAL_ROLES) <= set(ALL_ROLES)


def test_default_role_differs_by_mode(monkeypatch):
    monkeypatch.setenv("PRODUCT_MODE", "1")
    assert default_role() == "doctor"
    monkeypatch.setenv("PRODUCT_MODE", "0")
    assert default_role() == "researcher"


def test_resolve_role_maps_empty_to_the_default(product):
    assert resolve_role(None) == "doctor"
    assert resolve_role("") == "doctor"
    assert resolve_role("   ") == "doctor"


def test_resolve_role_blocks_researcher_in_product_mode(product):
    with pytest.raises(InternalOnly):
        resolve_role("researcher")


def test_resolve_role_allows_researcher_in_internal_mode(internal):
    assert resolve_role("researcher") == "researcher"


def test_resolve_role_lists_the_available_values_on_a_typo(product):
    """三种结果分得清清楚楚（CLAUDE.md「边界上统一解析」那条的第三种）：
    认不出的字符串要把可用值列出来，让调用方自我纠正。"""
    with pytest.raises(ValueError) as e:
        resolve_role("physician")
    msg = str(e.value)
    assert "医师" not in msg  # 列的是 id，不是展示名——填进请求的是 id
    for r in PRODUCT_ROLES:
        assert r in msg


def test_product_flags_shape(product):
    flags = product_flags()
    assert flags["product_mode"] is True
    assert flags["default_role"] == "doctor"
    assert [r["id"] for r in flags["roles"]] == list(PRODUCT_ROLES)
    # 展示名要有：前端不写死角色中文名（写死的话加一个角色它不会跟着长出来）
    assert all(r["label"] for r in flags["roles"])


# ---------- §8.1 第 2–3 条：内部端点在产品模式下 404 ----------

#: §8.1 第 2 条点名的那几类。`/api/eval/*` 与 `/api/debug/*` 这台服务上
#: 压根没有——**这条测试仍然要覆盖它们**：将来有人加了这两个前缀的端点，
#: 这里会立刻红，而不是等某次评审时被人在产品界面上点出来。
INTERNAL_PATHS = ("/api/usage", "/api/usage/validate-key",
                  "/api/eval/run", "/api/debug/state")


@pytest.mark.parametrize("path", INTERNAL_PATHS)
def test_internal_endpoints_are_404_in_product_mode(product, path):
    client = TestClient(api_main.app)
    resp = client.get(path) if path == "/api/usage" else client.post(path)
    assert resp.status_code == 404, f"{path} 在产品模式下回了 {resp.status_code}"


def test_the_404_body_has_no_english_exception_name(product):
    """404 的正文是使用者能看懂的中文。`InternalOnly` 这个词只留在服务端日志里
    ——§8.2 第 12 条：英文技术词与报错原文不上产品面。"""
    client = TestClient(api_main.app)
    body = client.get("/api/usage").text
    assert "InternalOnly" not in body and "Traceback" not in body
    assert "没有这个功能" in body


def test_internal_endpoints_are_reachable_in_internal_mode(internal):
    """**能力不删。** 同一个端点在 PRODUCT_MODE=0 下照常应答。"""
    client = TestClient(api_main.app)
    resp = client.get("/api/usage")
    assert resp.status_code == 200
    assert "mode" in resp.json()


def test_enumerating_routes_finds_no_internal_prefix_open_in_product_mode(product):
    """把全部路由枚举一遍，凡是内部前缀的都必须不可达。
    **枚举而不是列白名单**：列表会漏掉以后新加的端点。"""
    client = TestClient(api_main.app)
    opened = []
    for route in api_main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(("/api/usage", "/api/eval", "/api/debug")):
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            r = client.request(method, path)
            if r.status_code != 404:
                opened.append(f"{method} {path} → {r.status_code}")
    assert not opened, "产品模式下这些内部端点还能打通：" + "；".join(opened)


def test_consult_rejects_the_researcher_role_in_product_mode(product):
    client = TestClient(api_main.app)
    r = client.post("/api/consult", json={"complaint": "胃脘胀痛", "role": "researcher"})
    assert r.status_code == 404


def test_stream_rejects_the_researcher_role_before_opening_the_stream(product):
    """**开流之前**就拒。先把流开起来、跑几十秒之后在 done 事件里才说给不了，
    比一个干脆的 404 更像半成品。"""
    client = TestClient(api_main.app)
    r = client.post("/api/consult/stream",
                    json={"complaint": "胃脘胀痛", "role": "researcher"})
    assert r.status_code == 404


def test_health_reports_the_product_shape(product):
    client = TestClient(api_main.app)
    h = client.get("/health").json()
    assert h["product_mode"] is True
    assert [r["id"] for r in h["roles"]] == list(PRODUCT_ROLES)
    assert h["default_role"] == "doctor"
    assert h["version"] and h["product_name"]


def test_health_reports_the_internal_shape(internal):
    client = TestClient(api_main.app)
    h = client.get("/health").json()
    assert h["product_mode"] is False
    assert "researcher" in [r["id"] for r in h["roles"]]


def test_the_app_title_has_no_demo_word():
    """§8.2 第 16 条。这个字符串会出现在 OpenAPI 文档与将来给 HIS 的接口说明里。"""
    assert "demo" not in api_main.app.title.lower()


# ---------- §8.2 十六条清单：前端那一半 ----------
#
# 判据是**契约**，不是截图：要藏的那一块带 `internal-only`，而 CSS 在产品模式
# 下把这个类藏掉。两头都钉住，少一头都能出现"标记在、但没人管它"。

HTML = load_html()
CSS = load_css()
APP_JS = load_app_js()


def test_the_css_hides_internal_only_under_product_mode():
    assert 'html[data-product-mode="1"] .internal-only' in CSS
    assert 'html[data-product-mode="0"] .product-only' in CSS


def test_the_root_element_defaults_to_product_mode():
    """默认值站在正式版这边：靠 JS 在 /health 之后一处处摘，访问者会先看到
    半秒的内部界面再看它们消失——那半秒就是全部印象。"""
    assert 'data-product-mode="1"' in HTML


@pytest.mark.parametrize("anchor", [
    'id="quota-chip" class="internal-only"',      # 第 2 条：额度显示
    'id="byok-box" class="internal-only"',        # 第 1 条：BYOK
    'id="retriever-mode" class="internal-only"',  # 第 14 条：检索方式下拉
    'id="rx-compare" class="internal-only"',      # 第 7 条：用药对照带 + ε
    'id="divergence-detail" class="internal-only"',  # 第 7 条：分层读数
    'id="manifest-footer" class="internal-only"',    # 第 8 条：运行清单
    'id="token-panel" class="internal-only"',        # 第 8 条：上下文面板
    'id="cy-lambda1-note" class="internal-only"',    # R56 §6 第 2 条：λ1 技术注记（问诊图）
    'id="gb-lambda1-note" class="internal-only"',    # R56 §6 第 2 条：λ1 技术注记（图谱浏览器）
])
def test_each_internal_block_carries_the_marker(anchor):
    assert anchor in HTML, f"这一块没标 internal-only：{anchor}"


def test_the_researcher_option_is_removed_from_the_dom_not_hidden_by_css():
    """一个被 CSS 藏起来的 `<option>` 仍然是 select 的 value：页面一加载就会
    以研究者身份去请求，而后端在产品模式下对它回 404——界面看着好好的，
    一点就报错。所以标记是 `data-internal-role`，处理方式是 `opt.remove()`。"""
    assert 'data-internal-role="1"' in HTML
    assert "opt.remove()" in APP_JS


def test_the_product_face_has_a_replacement_for_the_divergence_band():
    """第 7 条要求"对医师有价值的部分改写为「本方的依据强度」一句话"。
    **不是删掉了事**——删掉的话产品面就少了一整块信息。"""
    assert 'id="evidence-strength" class="product-only"' in HTML
    assert "evidenceStrengthHtml" in APP_JS


def test_the_record_number_is_not_called_trace_id_on_the_product_face():
    """第 9 条。后端字段叫 `record_id`，产品面那一行写「本次记录编号」。"""
    assert "本次记录编号" in APP_JS
    body = re.sub(r"//[^\n]*", "", APP_JS)
    assert "trace_id" not in body


def test_apply_product_mode_is_the_only_place_that_writes_the_dom_flag():
    """前端也只能有一个应用点。全文件里写 `dataset.productMode` 的地方
    只允许有一处（读的地方随便）。"""
    writes = re.findall(r"dataset\.productMode\s*=", APP_JS)
    assert len(writes) == 1, f"写产品模式标记的地方有 {len(writes)} 处，只许一处"
