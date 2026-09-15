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
    后者测不了。

    干净机器上 conftest 会塞一个桩模块进 sys.modules，所以这里 import 得到；
    但仍然用 importorskip 兜底，不把"装没装"混进这条断言（见下面那条 marker
    测试的注释，同一条理由）。"""
    sentence_transformers = pytest.importorskip("sentence_transformers")

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
    # **importorskip 而不是 import**：这条测的是"marker 能让真类回来"，不是
    # "这台机器装没装 sentence-transformers"。干净机器上直接 import 会
    # ModuleNotFoundError，把一条跟依赖无关的断言变成环境检查。
    sentence_transformers = pytest.importorskip("sentence_transformers")

    from tests.conftest import _DeterministicEncoder

    assert sentence_transformers.SentenceTransformer is not _DeterministicEncoder


def test_no_test_imports_an_optional_dependency_unguarded():
    """**第四次撞同一堵墙了**（docx 时间戳 / bench_startup 无条件 import /
    这条）。判据：测试文件里 import 可选依赖必须走 `pytest.importorskip`，
    不能裸 import——裸 import 在干净机器上是 ModuleNotFoundError，把一条跟依赖
    无关的断言变成了环境检查。

    只查"可选依赖"这张小表：requirements.txt 里的必装依赖不在其列。
    """
    import re

    optional = ("sentence_transformers", "vllm", "docx", "playwright")
    offenders = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "importorskip" in stripped:
                continue
            m = re.match(r"(?:import|from)\s+([\w.]+)", stripped)
            if m and m.group(1).split(".")[0] in optional:
                # 装到 sys.modules 里的桩、或者 monkeypatch 用的引用不算——
                # 判据是"这一行会不会在干净机器上抛 ModuleNotFoundError"，
                # 所以只认真正的 import 语句，且允许写在 try 里。
                offenders.append(f"{path.name}:{lineno} {stripped}")
    assert not offenders, "这些测试在干净机器上会因为缺可选依赖而红：\n" + "\n".join(offenders)


def test_the_runbook_splits_segment_zero_into_two_passes():
    """段 0 的全量测试必须分两段跑，中间留出回收内存的时间——一次跑完正是
    2GB 上被 OOM 杀掉的那条路径。"""
    script = (ROOT / "scripts" / "run_onsite.sh").read_text(encoding="utf-8")
    assert '-m "not real_embedding"' in script
    assert "-m real_embedding" in script
