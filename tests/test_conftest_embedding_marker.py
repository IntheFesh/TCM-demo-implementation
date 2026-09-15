"""R12-F：`real_embedding` marker 与默认的假编码器。

无卡模式 2GB 上实测全量测试 34% 被 OOM 杀掉（退出码 137，只留一个 `Killed`，
看不出是哪条）。根因是一批用例会把 400MB 的 sentence-transformers 模型真加载进来，
而它们中的绝大多数根本不关心向量的语义质量。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_the_marker_is_registered_in_pytest_ini():
    """没注册的 marker 在 `--strict-markers` 下是错、平时只是一条告警——
    而一条没人看的告警等于没有。"""
    ini = (ROOT / "pytest.ini").read_text(encoding="utf-8")
    assert "markers =" in ini and "real_embedding" in ini
    assert 'pytest -m "not real_embedding"' in ini, "怎么用它必须写在注册它的地方"


def test_by_default_the_encoder_is_the_deterministic_fake():
    """默认不加载真模型。判据是"拿到的类就是那个假的"，不是"内存没涨"——
    后者测不了。"""
    import sentence_transformers

    from tests.conftest import _DeterministicEncoder

    assert sentence_transformers.SentenceTransformer is _DeterministicEncoder


def test_the_fake_encoder_returns_usable_normalised_vectors():
    """假向量也得是能用的向量：形状对、已归一化、同样的文本给同样的结果。
    不然它会在"检索能不能跑"这件事上骗过测试，而在真机上换成真模型才暴露。"""
    import numpy as np

    from tests.conftest import _DeterministicEncoder

    vecs = _DeterministicEncoder().encode(["纳差乏力", "胃脘胀痛"])
    assert vecs.shape == (2, 8)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0)
    again = _DeterministicEncoder().encode(["纳差乏力"])
    assert np.allclose(again[0], vecs[0]), "同样的文本必须给同样的向量"


@pytest.mark.real_embedding
def test_a_marked_test_gets_the_real_class_back():
    """标了 marker 的用例拿到的是真类。**这条自己就是那个 marker 的用例**：
    `pytest -m "not real_embedding"` 会跳过它，`-m real_embedding` 会跑它，
    两条命令的分界线因此是可验证的，不是口头约定。"""
    import sentence_transformers

    from tests.conftest import _DeterministicEncoder

    assert sentence_transformers.SentenceTransformer is not _DeterministicEncoder


def test_the_runbook_splits_segment_zero_into_two_passes():
    """段 0 的全量测试必须分两段跑，中间留出回收内存的时间——一次跑完正是
    2GB 上被 OOM 杀掉的那条路径。"""
    script = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    assert '-m "not real_embedding"' in script
    assert "-m real_embedding" in script
