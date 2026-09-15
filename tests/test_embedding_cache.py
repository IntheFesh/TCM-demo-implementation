"""R12-C：语料向量的磁盘缓存。

省掉的是"给全部语料编码"这一段（941 条，实测占启动耗时的大头）。判据不是"快了"
而是"第二次根本没调 encode"——耗时会因为机器负载波动，调用次数不会。
"""
import json

import pytest

from core import retrieval
from core.retrieval import DenseRetriever, embedding_cache_dir
from core.schemas import CaseRecord


class CountingEncoder:
    """数 encode 被调了几次的假编码器。**缓存生没生效只能靠这个数判定**。"""

    calls = 0

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def encode(self, texts, **_kwargs):
        import numpy as np

        CountingEncoder.calls += 1
        if isinstance(texts, str):
            texts = [texts]
        return np.array([[float(len(t) % 7 + 1)] * 4 for t in texts], dtype="float32")


@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    """缓存指到 tmp_path 并打开（conftest 默认是关掉的，理由见那边的文档字符串）。"""
    import sys

    monkeypatch.setenv("EMBEDDING_CACHE", "1")
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", str(tmp_path / "cache"))
    module = sys.modules["sentence_transformers"]
    monkeypatch.setattr(module, "SentenceTransformer", CountingEncoder, raising=False)
    CountingEncoder.calls = 0


def _write_cases(tmp_path, n=3, marker="甲"):
    cases = [CaseRecord(case_id=f"c{i}", case_group_id=f"g{i}", physician="ye_tianshi",
                        visit_index=0, raw=f"{marker}医案{i}", symptoms=[f"症状{i}{marker}"],
                        raw_excerpt=f"{marker}医案{i}的原文").model_dump()
             for i in range(n)]
    path = tmp_path / f"cases_{marker}_{n}.json"
    path.write_text(json.dumps(cases, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def test_second_load_hits_the_cache_and_does_not_encode(tmp_path):
    a = DenseRetriever(_write_cases(tmp_path))
    a._ensure_encoded()
    assert CountingEncoder.calls == 1 and a.embeddings_from_cache is False

    b = DenseRetriever(_write_cases(tmp_path))
    b._ensure_encoded()
    assert CountingEncoder.calls == 1, "第二次不该再调 encode"
    assert b.embeddings_from_cache is True
    assert (b._embeddings == a._embeddings).all()


def test_changed_corpus_content_misses_the_cache(tmp_path):
    """指纹取的是**被编码的那些文本**：内容变了必须重编码，否则就是拿旧向量
    检索新语料，结果全错而且不报任何错。"""
    DenseRetriever(_write_cases(tmp_path, marker="甲"))._ensure_encoded()
    DenseRetriever(_write_cases(tmp_path, marker="乙"))._ensure_encoded()
    assert CountingEncoder.calls == 2


def test_changed_corpus_size_misses_the_cache(tmp_path):
    DenseRetriever(_write_cases(tmp_path, n=3))._ensure_encoded()
    DenseRetriever(_write_cases(tmp_path, n=5))._ensure_encoded()
    assert CountingEncoder.calls == 2


def test_changed_model_misses_the_cache(tmp_path, monkeypatch):
    """换模型必须重编码：旧模型的向量跟新模型编出来的查询不在同一个空间里。"""
    path = _write_cases(tmp_path)
    DenseRetriever(path)._ensure_encoded()
    monkeypatch.setattr(retrieval, "EMBEDDING_MODEL", "另一个模型")
    DenseRetriever(path)._ensure_encoded()
    assert CountingEncoder.calls == 2


def test_cache_directory_is_created_on_demand(tmp_path):
    cache_dir = embedding_cache_dir()
    assert not cache_dir.exists()
    DenseRetriever(_write_cases(tmp_path))._ensure_encoded()
    assert cache_dir.exists()
    sidecars = list(cache_dir.glob("*.json"))
    assert len(sidecars) == 1
    meta = json.loads(sidecars[0].read_text(encoding="utf-8"))
    assert meta["model"] == retrieval.EMBEDDING_MODEL and meta["n_cases"] == 3
    assert "generated_at" in meta and "sentence_transformers" in meta


def test_a_corrupt_cache_file_falls_back_to_encoding_and_says_so(tmp_path, capsys):
    """**静默回退比慢更糟**：人以为缓存生效了，实际每次都在重新编码，而"启动还是
    135 秒"这件事没有任何输出能解释。"""
    path = _write_cases(tmp_path)
    DenseRetriever(path)._ensure_encoded()
    npy = next(embedding_cache_dir().glob("*.npy"))
    npy.write_bytes("这不是一个 npy 文件".encode("utf-8"))
    capsys.readouterr()
    r = DenseRetriever(path)
    r._ensure_encoded()
    assert CountingEncoder.calls == 2 and r.embeddings_from_cache is False
    assert "读不出来" in capsys.readouterr().err


def test_a_cache_with_the_wrong_number_of_rows_is_rejected(tmp_path, capsys):
    """行数对不上意味着 `self._cases` 和向量的下标错位——那是**静默给错结果**，
    比重编码一次糟得多。指纹里已经带了条数，走到这里说明文件被人换过。"""
    import numpy as np

    path = _write_cases(tmp_path)
    DenseRetriever(path)._ensure_encoded()
    npy = next(embedding_cache_dir().glob("*.npy"))
    np.save(npy, np.zeros((99, 4), dtype="float32"))
    capsys.readouterr()
    r = DenseRetriever(path)
    r._ensure_encoded()
    assert CountingEncoder.calls == 2
    assert "对不上" in capsys.readouterr().err


def test_cache_can_be_turned_off_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_CACHE", "0")
    assert embedding_cache_dir() is None
    path = _write_cases(tmp_path)
    DenseRetriever(path)._ensure_encoded()
    DenseRetriever(path)._ensure_encoded()
    assert CountingEncoder.calls == 2, "关掉之后每次都该重新编码"


def test_writing_the_cache_never_breaks_the_run(tmp_path, monkeypatch, capsys):
    """只读挂载、磁盘满：写不了缓存不影响这次跑。"""
    monkeypatch.setenv("EMBEDDING_CACHE_DIR", "/proc/不可写的目录")
    r = DenseRetriever(_write_cases(tmp_path))
    r._ensure_encoded()
    assert r._embeddings is not None and r.embeddings_from_cache is False
    assert "写不出去" in capsys.readouterr().err
