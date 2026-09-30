"""`docs/DESIGN.md`（界面与产品设计）的结构性测试。

只测结构与关键锚点，不测文字措辞：措辞会改，结构和判据不该悄悄消失。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "DESIGN.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DESIGN.exists(), "docs/DESIGN.md 不见了"
    body = DESIGN.read_text(encoding="utf-8")
    assert len(body) > 5000, f"只有 {len(body)} 字符"
    return body


def test_every_part_is_present(text):
    for heading in ("# 第零部分 · 产品定位", "# 第一部分 · 设计方向：集注",
                    "# 第二部分 · 设计系统", "# 第三部分 · 页面设计",
                    "# 第四部分 · 后端架构", "# 第五部分 · 部署形态",
                    "# 第六部分 · 模块对照", "# 第七部分 · 不可妥协的设计约束",
                    "# 第八部分", "# 第九部分"):
        assert heading in text, heading


def test_reference_physicians_and_performance_budget_sections(text):
    """docs/DESIGN.md 的 §8（参考医家）与 §9（性能预算）有代码与脚本引用它们。"""
    assert "§8" in text and "§9" in text
    s8 = text[text.index("# 第八部分"):text.index("# 第九部分")]
    assert "enabled" in s8 and "physicians_enabled()" in s8 and "physicians_all()" in s8
    s9 = text[text.index("# 第九部分"):]
    for budget in ("≤ 20 s", "≤ 90 s", "≤ 240 s", "≤ 1 s"):
        assert budget in s9, budget
    assert "bench_startup" in s9 and "bench_consult" in s9


def test_the_paged_graph_contract_is_documented(text):
    for endpoint in ("/api/usage", "/api/usage/validate-key",
                     "/api/graph/neighbors", "/api/graph/search"):
        assert endpoint in text, endpoint
    assert "node_types" in text and "cursor" in text and "`page` 块" in text


def test_every_endpoint_in_the_doc_actually_exists_in_the_api(text):
    """文档里列的端点必须真的在 `api/main.py` 里：前端会照着文档去调。"""
    api = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'@app\.(?:get|post|put|delete)\("([^"]+)"\)', api))
    for path in re.findall(r"(/api/[\w/{}-]+)", text):
        path = path.rstrip("`）。，").rstrip("/")
        if path in ("/api", "/api/graph"):
            continue
        assert any(d.rstrip("/") == path for d in declared), f"文档写了 {path}，但 api/main.py 里没有"


def test_the_identity_colors_come_from_the_registry(text):
    """身份色的规格值写在文档里，实现里唯一的来源是注册表。"""
    for color in ("#2C5F5A", "#9C6B16", "#8A4736"):
        assert color in text, color
    assert "core/physicians.py" in text and "setProperty" in text
    assert "写死的常量也算一处实现" in text


def test_the_offline_font_strategy_is_specified(text):
    for key in ("font-display: swap", "系统栈", "断网"):
        assert key in text, key


def test_the_seven_non_negotiable_constraints_survive(text):
    """第七部分是这个产品的骨架。改动它必须是显式决定。"""
    block = text[text.index("# 第七部分"):text.index("# 第八部分")]
    numbered = re.findall(r"^\d+\. \*\*", block, re.M)
    assert len(numbered) == 7, f"只剩 {len(numbered)} 条"
    for keyword in ("三家平等", "分歧必须带 ε", "患者模式不出方药", "整页替换",
                    "可追溯", "回放模式必须自报", "色只承担语义"):
        assert keyword in block, keyword


def test_the_design_tokens_table_is_complete_enough_to_implement_from(text):
    tokens = text[text.index("## 2.1 色彩"):text.index("# 第三部分")]
    for name in ("--paper", "--surface", "--surface-2", "--ink", "--ink-2", "--muted",
                 "--rule", "--rule-soft", "--danger", "--caution", "--verified",
                 "--noise", "--real", "--font-classic", "--font-ui", "--font-num",
                 "--r-sm", "--r-md", "--r-lg", "--edge", "--edge-soft"):
        assert name in tokens, name
    assert len(re.findall(r"--s[1-8]:", tokens)) == 8, "间距应该是 8 档"


def test_the_five_consult_states_are_all_specified(text):
    block = text[text.index("### 状态设计"):text.index("## 3.2 图谱视图")]
    for state in ("首次进入", "辨证中", "信息不足", "安全拦截", "追问"):
        assert state in block, state
    assert "整页替换" in block


def test_access_control_settings_are_named(text):
    for key in ("TRUSTED_PROXY_HOPS", "QUOTA_PER_IP_DAILY_CALLS", "FORCE_REPLAY"):
        assert key in text, key
