"""`docs/DESIGN.md`（设计总纲）的结构性测试。

文档也要有测试，判据是"这份文档是后面几轮的实施依据"：R13 的令牌表、R14 的五种状态、
R16 的图谱规格全部按它的章节号落地。少一节、订正被后来的编辑冲掉，下一轮就会照着
一份缺页的规格去实施——而那种错要到写完前端才看得出来。

**只测结构与关键锚点，不测文字措辞**：措辞会改，结构和判据不该悄悄消失。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "DESIGN.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DESIGN.exists(), "docs/DESIGN.md 不见了——它是 R13–R17 的实施依据"
    body = DESIGN.read_text(encoding="utf-8")
    assert len(body) > 5000, f"只有 {len(body)} 字符，总纲不可能这么短"
    return body


def test_revision_log_exists_and_records_the_round_that_added_it(text):
    """修订记录是"原文 vs 后来查到的事实"这条分工的载体。没有它，下一轮就会
    直接改原文，两周后没人说得清当初是怎么想的。"""
    assert "# 修订记录" in text
    log = text[text.index("# 修订记录"):text.index("# 第零部分")]
    assert "R11" in log and "订正" in log
    assert "只增不删" in log


def test_every_part_of_the_original_outline_is_present(text):
    """原文七部分一节不少。抽查的是标题，不是内容——内容有 9 万字符没法逐字比。"""
    for heading in ("# 第零部分 · 产品定位", "# 第一部分 · 设计方向：集注",
                    "# 第二部分 · 设计系统", "# 第三部分 · 页面设计",
                    "# 第四部分 · 后端架构", "# 第五部分 · 部署形态",
                    "# 第六部分 · 实施顺序", "# 第七部分 · 不可妥协的设计约束"):
        assert heading in text, heading


def test_the_two_new_sections_are_there(text):
    """§8（第四/第五位医家）和 §9（性能预算）是 R11 新增，R18/R12 依赖它们。"""
    assert "§8" in text and "§9" in text
    assert "# 第八部分" in text and "# 第九部分" in text
    s8 = text[text.index("# 第八部分"):text.index("# 第九部分")]
    assert "enabled" in s8 and "physicians_enabled()" in s8 and "physicians_all()" in s8
    s9 = text[text.index("# 第九部分"):]
    for budget in ("≤ 20 s", "≤ 90 s", "≤ 240 s", "≤ 1 s"):
        assert budget in s9, budget
    assert "bench_startup" in s9 and "bench_consult" in s9


def test_all_four_endpoints_added_after_the_outline_was_written_are_listed(text):
    """总纲写的是八个端点（标题还写着"九个"），实际现在 13 个。少列一个的后果是
    前端按旧契约写，到联调才发现。"""
    for endpoint in ("/api/usage", "/api/usage/validate-key",
                     "/api/graph/neighbors", "/api/graph/search"):
        assert endpoint in text, endpoint
    assert "node_types" in text and "cursor" in text and "`page` 块" in text


def test_every_endpoint_in_the_doc_actually_exists_in_the_api(text):
    """文档里列的端点必须真的在 `api/main.py` 里。这条防的是"文档写了一个不存在的
    端点"——比"文档漏了一个端点"更糟，因为前端会照着它去调。"""
    api = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'@app\.(?:get|post)\("([^"]+)"\)', api))
    # 文档里带花括号参数的写法跟代码一致，直接比字面
    for path in re.findall(r"(/api/[\w/{}-]+)", text):
        path = path.rstrip("`）。，").rstrip("/")
        if path in ("/api", "/api/graph"):
            continue
        assert any(d.rstrip("/") == path for d in declared), f"文档写了 {path}，但 api/main.py 里没有"


def test_the_six_corrections_are_all_present(text):
    """六处订正逐条钉住。每一条订正的是一个**会让下一轮做错事**的具体判据，
    所以查的是那个判据的关键词，不是"有没有订正块"。"""
    corrections = {
        "§0 的 ε 可比性": ["thinking", "temperature 不生效", "0.2611", "0.2409",
                          "(model, thinking 设置)"],
        "§2.1 身份色唯一来源": ["core/physicians.py", "setProperty", "写死的常量也算一处实现"],
        "§2.2 字体离线策略": ["font-display: swap", "系统栈", "断网"],
        "§4.7 端点补全": ["13 个", "physician` 字段路由"],
        "§5.1 已实施": ["TRUSTED_PROXY_HOPS", "QUOTA_PER_IP_DAILY_CALLS", "FORCE_REPLAY"],
        "§6 前端第 1 步": ["死变量", "删掉"],
    }
    missing = {name: [k for k in keys if k not in text] for name, keys in corrections.items()}
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, f"这几处订正的关键判据丢了：{missing}"
    assert text.count("⚠ **订正") >= 6


def test_the_three_identity_colors_are_specified_exactly_once_as_values(text):
    """三家身份色的值写在总纲里（它是规格），但**实现里唯一的来源是注册表**——
    §2.1 的订正说的就是这件事。这里只钉规格里的三个值没被改动。"""
    for color in ("#2C5F5A", "#9C6B16", "#8A4736"):
        assert color in text, color


def test_the_seven_non_negotiable_constraints_survive(text):
    """第七部分是这个产品的骨架。改动它必须是显式决定，不能被一次编辑顺手冲掉。"""
    block = text[text.index("# 第七部分"):text.index("# 第八部分")]
    numbered = re.findall(r"^\d+\. \*\*", block, re.M)
    assert len(numbered) == 7, f"只剩 {len(numbered)} 条"
    for keyword in ("三家平等", "分歧必须带 ε", "患者模式不出方药", "整页替换",
                    "可追溯", "演示模式必须自报", "色只承担语义"):
        assert keyword in block, keyword


def test_the_design_tokens_table_is_complete_enough_to_implement_from(text):
    """R13 要"逐字按 §2.1–2.4"落令牌。落之前先确认这四节里该有的东西都在——
    缺一档间距、缺一个语义色，R13 就只能自己编一个值。"""
    tokens = text[text.index("## 2.1 色彩"):text.index("# 第三部分")]
    for name in ("--paper", "--surface", "--surface-2", "--ink", "--ink-2", "--muted",
                 "--rule", "--rule-soft", "--danger", "--caution", "--verified",
                 "--noise", "--real", "--font-classic", "--font-ui", "--font-num",
                 "--r-sm", "--r-md", "--r-lg", "--edge", "--edge-soft"):
        assert name in tokens, name
    assert len(re.findall(r"--s[1-8]:", tokens)) == 8, "间距应该是 8 档"


def test_the_five_consult_states_are_all_specified(text):
    """R14 要给五种状态各自做设计。规格缺一种，那一种就会变成一片空白。"""
    block = text[text.index("### 状态设计"):text.index("## 3.2 图谱视图")]
    for state in ("首次进入", "辨证中", "信息不足", "安全拦截", "追问"):
        assert state in block, state
    assert "整页替换" in block
