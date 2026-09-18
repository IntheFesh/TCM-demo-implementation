"""R18-A：医家注册表扩到五位，其中两位 `enabled=False`。

## 为什么要两个入口而不是一个 `PHYSICIANS`

「谁算集注的一员」和「这个 id 合法吗、他的语料在哪」是**两个问题**。
R18 之前它们恰好同一个答案（注册表里的都参与集注），所以一个 `PHYSICIANS`
够用；李可/王云启进来之后不再是。

漏改一处的后果不会报错：分歧度会把李可和三家一起算 n 方交并比（那个数直接
失去意义），ε 的对照基准跟着变，而页面上只是多了两列。
"""
import inspect
from pathlib import Path

from core.physicians import PHYSICIANS, physicians_all, physicians_enabled

ROOT = Path(__file__).resolve().parent.parent


def test_five_registered_three_take_part_in_the_collation():
    assert len(physicians_all()) == 5
    assert list(physicians_enabled()) == ["ye_tianshi", "wu_jutong", "zhang_xichun"]
    assert {"li_ke", "wang_yunqi"} == set(physicians_all()) - set(physicians_enabled())


def test_the_two_new_ones_have_no_invented_years_or_school():
    """**查不到就留空，不编。** 两份语料的前言/书名页里都没有生卒年和学派归属
    （王云启那份的「代序」说的是"省级名中医""湖湘中医文化"、学术思想列了七八条
    ——那是文字描述，不是一个可用于配对的离散学派标签）。

    给一个猜的学派然后让它去参与「跨学派分歧大于师承内」的统计，比留空危险
    得多：留空时 `pairwise_divergence` 会把这一对判成 unknown，那是对的。"""
    for pid in ("li_ke", "wang_yunqi"):
        assert PHYSICIANS[pid]["years"] is None
        assert PHYSICIANS[pid]["school"] is None


def test_every_physician_says_where_its_corpus_came_from():
    """`source` 是新加的：公有领域的三位随仓库分发，李可/王云启那两份版权受限、
    不分发。混在一起的话，"为什么我 clone 下来跑不出李可的医案"没有答案。"""
    for pid, info in physicians_all().items():
        assert info.get("source"), f"{pid} 没写语料出处"
    assert "不随仓库分发" in PHYSICIANS["li_ke"]["source"]
    assert "公有领域" in PHYSICIANS["ye_tianshi"]["source"]


def test_no_module_filters_the_registry_by_itself():
    """**新增的遍历一律走两个入口，不要在别处自己 filter。** 判据：全项目
    除了 core/physicians.py，没有第二处写 `info["enabled"]` 的筛选。"""
    hits = []
    for path in list(ROOT.glob("core/**/*.py")) + list(ROOT.glob("api/*.py")) \
            + list(ROOT.glob("offline/*.py")) + list(ROOT.glob("scripts/*.py")):
        if path.name == "physicians.py":
            continue
        text = path.read_text(encoding="utf-8")
        # api/main.py 把 enabled 原样下发给前端，那是**传递**不是筛选
        if path.name == "main.py" and 'info.get("enabled", True)}' in text:
            continue
        # **"enabled"这个键名不是医家注册表独占的，所以要逐行看。**
        # R62 给 `corroborate_done` 事件加了一个 `enabled` 字段（这一相
        # 跑没跑），而 core/chain.py 又恰好 import 了 PHYSICIANS，于是整份
        # 文件被这条扫描误报成"自己筛了注册表"——它碰的根本不是注册表。
        # 判据改成：**读 enabled 的那一行上要同时出现医家的影子**
        # （`info` / `physician` / `PHYSICIANS`）。真去筛注册表的代码
        # 一定是在遍历它，那一行上必然有这几个词之一。
        for line in text.splitlines():
            if 'get("enabled"' not in line and '["enabled"]' not in line:
                continue
            if any(w in line for w in ("info", "physician", "PHYSICIANS")):
                hits.append(f"{path.relative_to(ROOT)}: {line.strip()}")
    assert not hits, f"这些模块自己筛了注册表：{hits}"


def test_the_helpers_take_a_registry_so_existing_stubs_keep_working():
    """`registry` 参数不是装饰：全项目有近百条测试用
    `monkeypatch.setattr(某模块, "PHYSICIANS", {...})` 换一个两位医家的小注册表。
    不接这个参数的话这些桩全部失效——函数读的是 core.physicians 自己那份，
    桩打在别的模块上。**筛选逻辑仍然只有一处**，调用方传的只是数据。"""
    tiny = {"a": {"name": "甲", "enabled": True}, "b": {"name": "乙", "enabled": False}}
    assert list(physicians_enabled(tiny)) == ["a"]
    assert list(physicians_all(tiny)) == ["a", "b"]
    for fn in (physicians_enabled, physicians_all):
        assert "registry" in inspect.signature(fn).parameters


def test_a_physician_without_the_enabled_key_defaults_to_taking_part():
    """默认 True：老注册表（没有这个键）的行为一个字节都不变。"""
    assert list(physicians_enabled({"x": {"name": "某"}})) == ["x"]


def test_the_disabled_ones_still_get_a_colour_distinct_from_the_three():
    """他们不占列，但会出现在「参考医家」引用区，需要一个能跟集注三家区分开
    的颜色。跟总纲 §2.1 的三个身份色不能撞——撞了就等于在说"这条引用是
    叶天士的"。"""
    core_colors = {physicians_all()[p]["color"] for p in physicians_enabled()}
    for pid in ("li_ke", "wang_yunqi"):
        assert physicians_all()[pid]["color"] not in core_colors


def test_consult_only_runs_the_enabled_ones():
    """接线判据：`core/chain.py` 的那一行遍历必须是 enabled 那一支。"""
    import core.chain as chain
    src = inspect.getsource(chain._run_physicians_into)
    assert "physicians_enabled(PHYSICIANS)" in src
    assert "list(PHYSICIANS.items())" not in src
