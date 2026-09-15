"""R16 §3.2 规格 1：方剂框按来源区分边框，靠的是方剂候选上的那个受控字段。

## 为什么这个文件不叫 source_kind 里面也没有 source_kind

R16 的清单写的是「后端 `formula_candidates[].source_kind` 字段（S3 schema 加
`Literal["classic","modified","composed"]`）」。**这个字段已经存在了，叫
`source`**（`core/schemas.py::FormulaCandidate.source`，M1 那轮加的，值域一模一样）。

再加一个 `source_kind` 就是同一个概念的第二处实现——CLAUDE.md 第 31 条撞过三次的
那堵墙。两个字段一旦并存，S3 的 prompt 要填哪个、前端读哪个、导出写哪个，
每一处都是一次选择，而选错不会报错（两个字段都是合法的 Literal）。

所以这一轮做的是：**确认既有字段满足规格、把它接到图上、用测试钉住**。
文件名保留清单里的名字，方便对着清单核。
"""
import json

import pytest
from pydantic import ValidationError

from core.schemas import FormulaCandidate, HerbItem
from tests.web_harness import load_app_js


def _candidate(**over):
    payload = {
        "name": "柴胡疏肝散", "source": "classic", "confidence": "high",
        "rationale": "与本证相合",
        "herb_items": [HerbItem(name="柴胡", dose=6, dose_unit="g")],
    }
    payload.update(over)
    return FormulaCandidate(**payload)


def test_the_source_field_is_a_three_valued_literal_not_a_free_string():
    """三档来源是**受控值**：classic（经典方原方）/ modified（经典方加减）/
    composed（自拟方）。自由字符串的话模型迟早会吐出"经典方"、"classical"、
    "改良方"——前端的三种边框只认这三个值，别的一律落到默认实线，
    于是"这是仲景的方还是他自己拟的"在图上就不可辨了，而且不报错。"""
    for value in ("classic", "modified", "composed"):
        assert _candidate(source=value, base_formula="柴胡疏肝散"
                          if value == "modified" else None).source == value
    with pytest.raises(ValidationError):
        _candidate(source="经典方")
    with pytest.raises(ValidationError):
        _candidate(source="")


def test_modified_must_name_the_formula_it_modifies():
    """`modified` = 在某张经典方基础上加减——**基础方是哪张必须说**。
    不说的话它跟 `composed`（自拟）在信息上没有区别，那这一档就白分了。"""
    with pytest.raises(ValidationError, match="base_formula"):
        _candidate(source="modified", base_formula=None)
    assert _candidate(source="modified", base_formula="柴胡疏肝散").base_formula == "柴胡疏肝散"


def test_classic_and_composed_must_not_name_a_base_formula():
    """反向也要卡：`classic` 是原方、`composed` 是自拟，两者都不该有基础方。
    有的话说明模型其实想说 modified，而标签跟内容对不上。"""
    for value in ("classic", "composed"):
        with pytest.raises(ValidationError):
            _candidate(source=value, base_formula="柴胡疏肝散")


def test_the_graph_styles_exactly_these_three_values():
    """前端那三条规则读的是同一个字段。**classic 不另写规则**——它走默认实线，
    多写一条 `border-style: solid` 只是把默认值重复一遍，而重复的默认值正是
    下一次"有人改了默认、只改了一处"的入口。"""
    src = load_app_js()
    assert 'node[node_type = "formula"][source = "modified"]' in src
    assert '"border-style": "dashed"' in src
    assert 'node[node_type = "formula"][source = "composed"]' in src
    assert '"border-style": "dotted"' in src
    assert 'source = "classic"' not in src, "classic 不该另写规则（默认就是实线）"


def test_there_is_no_second_field_meaning_the_same_thing():
    """这条是这个文件的由来。`source_kind` 一旦出现，S3 prompt 填哪个、前端读
    哪个、导出写哪个，每一处都是一次选择，而选错不报错——两个字段都是合法的
    Literal。"""
    schema = json.dumps(FormulaCandidate.model_json_schema(), ensure_ascii=False)
    assert "source_kind" not in schema
    # 前端一条 `[source_kind = ...]` 选择器都不该有。
    #
    # 只查选择器不查整份源码：`offline/export_sft.py` 的样本 meta 里另有一个
    # `source_kind`（值是 case / sdt / materia_medica / …），那回答的是
    # **"这条训练样本是从哪一路语料来的"**，跟方剂来源是两件事，重名而已。
    # 合并它们才是错的——一个是训练数据的出处，一个是方子的体例。
    assert "[source_kind" not in load_app_js()


def test_the_formula_node_carries_the_source_through_to_the_front_end():
    """schema 有这个字段、样式表认这个字段，中间那一段（to_graph 把它放进
    节点 data）断了的话前面两条照样全绿，图上却什么都看不出来。"""
    import inspect

    import api.main as api_main
    src = inspect.getsource(api_main.to_graph)
    assert "source=cand.source" in src
