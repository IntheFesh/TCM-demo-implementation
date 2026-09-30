"""问诊历史记下的是证型名、病名与方名这几个字符串，不管结论是哪一种 S3 形状。

structured / derived 的五步链里证型是一个对象、方名在 `formula.candidate` 里，
legacy 的扁平结论是裸字符串。历史列表与统计按字符串展示和计数，两种形状都要
落成同样的字段。
"""
from __future__ import annotations

from types import SimpleNamespace

import api.main as api_main
from core import history


def _req():
    return SimpleNamespace(doctor_id="dr1", complaint="胃脘胀痛，脉弦")


def _structured_response():
    return {
        "record_id": "AB12CD34",
        "results": [{
            "s3_structured": {
                "syndrome": {"name": "肝胃不和证", "disease": "胃痛", "from_organs": ["肝", "胃"]},
                "method": {"principle": "疏肝理气，和胃止痛"},
                "formula": {"candidate": {"name": "柴胡疏肝散加减",
                                          "herb_items": [{"name": "柴胡"}, {"name": "白芍"}]}},
            },
            "s3": {"syndrome": "肝胃不和证", "disease": "胃痛"},
            "advice": [{"kind": "dose_exceeds"}],
        }],
    }


def _legacy_response():
    return {
        "record_id": "EF56GH78",
        "results": [{
            "s3": {"syndrome": "脾胃虚寒证", "disease": "胃痛", "formula": "黄芪建中汤",
                   "herbs": ["黄芪", "桂枝"]},
            "advice": [],
        }],
    }


def test_structured_result_records_names_not_objects():
    api_main._record_history(_structured_response(), _req())
    row = history.list_consults()[0]
    assert row["syndrome"] == "肝胃不和证"
    assert row["disease"] == "胃痛"
    assert row["formula"] == "柴胡疏肝散加减"
    assert row["advice_kinds"] == ["dose_exceeds"]


def test_legacy_result_records_the_flat_fields():
    api_main._record_history(_legacy_response(), _req())
    row = history.list_consults()[0]
    assert row["syndrome"] == "脾胃虚寒证"
    assert row["disease"] == "胃痛"
    assert row["formula"] == "黄芪建中汤"
