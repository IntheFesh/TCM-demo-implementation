"""R13：`/health` 下发三家身份信息，前端据此注入 CSS 变量。

**身份色只有一处来源**（docs/DESIGN.md §2.1 的订正 + CLAUDE.md 第 31 条前端小节：
写死的常量也算一处实现）。这个文件同时钉住接口这一端和前端那一端，因为这两端
任何一端悄悄写死，症状都是"注册表加了第四位医家，界面上他没有颜色"——不报错。
"""
import re

from fastapi.testclient import TestClient

import api.main as api_main
from core.physicians import PHYSICIANS
from tests.web_harness import load_app_js


def _health() -> dict:
    return TestClient(api_main.app).get("/health").json()


def test_health_lists_every_registered_physician():
    data = _health()
    assert [p["id"] for p in data["physicians"]] == list(PHYSICIANS), \
        "顺序也要跟注册表一致——前端三列按它排"


def test_each_entry_carries_the_fields_the_frontend_needs():
    for entry in _health()["physicians"]:
        info = PHYSICIANS[entry["id"]]
        assert entry["name"] == info["name"]
        assert entry["years"] == info["years"] and entry["school"] == info["school"]
        assert entry["color"] == info["color"] and entry["color_bg"] == info["color_bg"]
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", entry["color"]), entry


def test_the_registry_uses_the_design_doc_colours():
    """总纲 §2.1 的三个身份色取自实测用药特征（青黛 / 黄芩 / 赭石），
    不是随手挑的。改了要连总纲一起改，所以两边都钉。"""
    expected = {"ye_tianshi": "#2C5F5A", "wu_jutong": "#9C6B16", "zhang_xichun": "#8A4736"}
    assert {k: PHYSICIANS[k]["color"] for k in expected} == expected
    design = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    for value in expected.values():
        assert value in design, f"总纲里没有 {value}"


def test_every_physician_has_a_background_colour():
    """`color_bg` 是三列顶部那三行的底色（§3.1：身份色只用在顶部三行）。
    少一个的话那一位的表头就是白底，三列看起来不等价。"""
    missing = [pid for pid, info in PHYSICIANS.items() if not info.get("color_bg")]
    assert not missing, f"这几位没有 color_bg：{missing}"


def test_the_frontend_injects_them_instead_of_hard_coding():
    """前端那一端：必须从 /health 拿到之后 setProperty 注入，不能在 JS 里写死色值。"""
    script = load_app_js()
    assert "function injectPhysicianColors" in script
    assert "root.style.setProperty" in script
    assert "health.physicians" in script, "要从 /health 的响应里取，不是别处"
    for value in ("#2C5F5A", "#9C6B16", "#8A4736"):
        assert value not in script, f"JS 里写死了身份色 {value}"


def test_injection_covers_a_physician_the_css_has_never_heard_of():
    """**这条是整件事的目的**：注册表加第四位医家时，不改 CSS 也要有颜色。
    判据是注入用的变量名按 id 生成（`--phys-<id>`），不是一张写死的映射表。"""
    script = load_app_js()
    assert "`--phys-${p.id}`" in script, "变量名要按 id 生成，不能只认已知的三位"
