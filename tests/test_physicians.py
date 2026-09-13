"""core/physicians.py 的离线测试：resolve_physician_id 是全项目唯一的
name→id 入口（P1-1.1b，见 SOURCES.md 第 31 条），四种输入都要钉住。"""
import pytest

from core.physicians import PHYSICIANS, physician_choices_text, resolve_physician_id


def test_resolve_physician_id_returns_id_unchanged():
    for pid in PHYSICIANS:
        assert resolve_physician_id(pid) == pid


def test_resolve_physician_id_maps_every_registered_chinese_name_to_its_id():
    for pid, info in PHYSICIANS.items():
        assert resolve_physician_id(info["name"]) == pid


def test_resolve_physician_id_unregistered_name_is_none():
    assert resolve_physician_id("华佗") is None
    assert resolve_physician_id("ye-tianshi") is None  # 拼错的 id 也不猜


@pytest.mark.parametrize("value", ["", "   ", None])
def test_resolve_physician_id_empty_is_none(value):
    assert resolve_physician_id(value) is None


def test_resolve_physician_id_strips_surrounding_whitespace():
    assert resolve_physician_id(" 叶天士 ") == "ye_tianshi"
    assert resolve_physician_id(" wu_jutong\n") == "wu_jutong"


def test_physician_choices_text_lists_every_registered_physician_with_id_and_name():
    text = physician_choices_text()
    for pid, info in PHYSICIANS.items():
        assert f"{pid}({info['name']})" in text
