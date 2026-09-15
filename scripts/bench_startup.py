"""启动耗时基准：`import api.main` + 预热（加载模型 / 编码语料 / 建 BM25 索引）分段计时。

## 为什么要分段

"启动 135 秒"这一个数没法指导优化——不知道是模型加载慢还是编码慢。R12 要做的
embedding 磁盘缓存只能省掉**编码**那一段（模型还是要加载），所以必须先把这两段
分开量，不然改完没法说清"省下的是哪一段"。

四段的边界：

    import        import api.main（含 FastAPI 建应用、各 core 模块 import）
    construct     get_retriever()：读 cases.json、逐条 CaseRecord 校验、算 _case_texts
    model_load    SentenceTransformer("BAAI/bge-small-zh-v1.5")
    encode        model.encode(全部医案文本)

`model_load` / `encode` 都在 `DenseRetriever._load()` 里面，而它是函数内 import
（惰性加载是这个项目的硬约定），所以在**模块属性**上包一层就能把这两段分开
——不改 `core/retrieval.py` 一行。

## 冷热两次

`--repeat 2` 会在同一个进程里量两次：第二次 `_embeddings` 已经就位，`construct` 之后
三段全是 0。R12 的缓存要验的是**跨进程**的热启动，所以真机上跑两次进程：

    python -m scripts.bench_startup                 # 冷
    python -m scripts.bench_startup                 # 热（缓存命中）

## 沙盒自检模式

`cases.json` 是生成物、不进版本控制，沙盒里没有；模型也下不动。`--self-test N`
造 N 条合成医案 + 一个确定性的假编码器，只为验证**这个脚本自己**的分段逻辑。
输出里 `synthetic: true` 时那几个秒数不代表任何真实性能，报告里不许引用。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import types
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "eval" / "bench"

SEGMENTS = ("import", "construct", "model_load", "encode")


class _Stopwatch:
    """按段累计耗时。同一段被进入多次就累加（编码分批时会发生）。"""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = {}

    def add(self, name: str, seconds: float) -> None:
        self.seconds[name] = round(self.seconds.get(name, 0.0) + seconds, 4)

    def timed(self, name: str, fn, *args, **kwargs):
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.add(name, time.perf_counter() - t0)


def instrument_encoder(watch: _Stopwatch) -> None:
    """把 `sentence_transformers.SentenceTransformer` 换成一层计时壳。

    `_load()` 里是 `from sentence_transformers import SentenceTransformer`——函数内
    import 每次都会去模块属性上取，所以在这里替换模块属性就能拦到，不需要碰
    core/retrieval.py。
    """
    import sentence_transformers as st

    real_cls = st.SentenceTransformer

    def timed_ctor(*args, **kwargs):
        model = watch.timed("model_load", real_cls, *args, **kwargs)
        real_encode = model.encode

        def encode(*a, **kw):
            return watch.timed("encode", real_encode, *a, **kw)

        model.encode = encode
        return model

    st.SentenceTransformer = timed_ctor


def install_self_test(n_cases: int) -> Path:
    """造 N 条合成医案 + 一个确定性假编码器，让沙盒也能跑通这个脚本。

    假编码器按字符码点算一个 8 维向量：确定性、零依赖、不联网。它**不产生任何
    有意义的检索结果**，只用来走通"加载 → 编码 → 发布"这条路径。
    """
    import numpy as np

    from core import retrieval
    from core.physicians import PHYSICIANS

    cases = []
    for pid in PHYSICIANS:
        for i in range(n_cases):
            cases.append({
                "case_id": f"{pid}-bench-{i:04d}", "case_group_id": f"{pid}-bench-g{i:04d}",
                "physician": pid, "visit_index": 0,
                "raw": "合成医案：胃脘胀痛，嗳气泛酸，脉弦。",
                "symptoms": ["胃脘胀痛", "嗳气"], "herbs": ["柴胡", "白芍"],
                "raw_excerpt": "合成医案：胃脘胀痛，嗳气泛酸，脉弦。",
            })
    # 写临时目录不写 eval/bench/：合成语料是自检用的一次性产物，落在仓库里
    # 迟早有人把它当成真的 cases.json。
    path = Path(tempfile.mkdtemp(prefix="bench_startup_")) / "cases.json"
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    class FakeST:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def encode(self, texts, **_kw):
            rows = [[sum(ord(c) for c in t[i::8]) % 97 / 97.0 for i in range(8)] for t in texts]
            return np.array(rows, dtype="float32")

    stub = types.ModuleType("sentence_transformers")
    stub.SentenceTransformer = FakeST
    sys.modules["sentence_transformers"] = stub

    # 构造函数的默认参数在 def 那一刻就绑好了，改模块里的 CASES_PATH 已经晚了，
    # 所以这里直接把两个构造函数的默认值换掉。
    for cls in (retrieval.DenseRetriever,):
        cls.__init__.__defaults__ = (path,)
    from core.retrieval_hybrid import HybridRetriever

    HybridRetriever.__init__.__defaults__ = (path,)
    return path


def measure(watch: _Stopwatch, warm: bool) -> dict:
    """量一次。warm=True 表示这是同一进程里的第二次，用来看"还剩多少是真的重复劳动"。"""
    from core.retrieval import get_retriever

    t0 = time.perf_counter()
    retriever = watch.timed("construct", get_retriever)
    retriever._ensure_encoded()
    total = time.perf_counter() - t0
    embeddings = getattr(retriever, "_embeddings", None)
    return {
        "warm": warm,
        "total_s": round(total, 4),
        "n_cases": len(getattr(retriever, "_cases", []) or []),
        "n_embeddings": int(getattr(embeddings, "shape", [0])[0]) if embeddings is not None else 0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", type=int, default=0, metavar="N",
                    help="每位医家造 N 条合成医案 + 假编码器（沙盒里验脚本自己用，数字无意义）")
    ap.add_argument("--repeat", type=int, default=1,
                    help="同一进程里量几次。第二次起 embeddings 已就位，用来看重复劳动还剩多少")
    ap.add_argument("--skip-import", action="store_true",
                    help="不量 import api.main（只想看检索层那三段时用）")
    ap.add_argument("--out", default=None, help="默认 eval/bench/startup_<时间戳>.json")
    args = ap.parse_args(argv)

    watch = _Stopwatch()
    synthetic = args.self_test > 0
    cases_path = install_self_test(args.self_test) if synthetic else None
    instrument_encoder(watch)

    import_error = None
    if not args.skip_import:
        try:
            watch.timed("import", __import__, "api.main")
        except Exception as e:  # noqa: BLE001 - import 失败也要把已量到的段写出来
            import_error = f"{type(e).__name__}: {e}"

    runs, error = [], None
    try:
        for i in range(max(1, args.repeat)):
            runs.append(measure(watch, warm=i > 0))
    except Exception as e:  # noqa: BLE001 - 没有 cases.json 是沙盒的常态，如实记不崩
        error = f"{type(e).__name__}: {e}"

    report = {
        "kind": "startup",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "synthetic": synthetic,
        "synthetic_note": ("合成医案 + 假编码器，这些秒数只证明分段逻辑能跑，"
                           "**不代表任何真实性能**，不许写进报告") if synthetic else None,
        "cases_path": str(cases_path) if cases_path else None,
        "config": {"self_test": args.self_test, "repeat": args.repeat,
                   "skip_import": args.skip_import},
        "segments_s": {name: watch.seconds.get(name) for name in SEGMENTS},
        "runs": runs,
        "import_error": import_error,
        "error": error,
    }
    # 合成跑的文件名带 synthetic，被 .gitignore 挡掉——理由同 bench_consult。
    prefix = "startup_synthetic" if synthetic else "startup_real"
    out = Path(args.out) if args.out else BENCH_DIR / f"{prefix}_{int(time.time())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for name in SEGMENTS:
        value = report["segments_s"][name]
        print(f"  {name:12s} {'—' if value is None else f'{value}s'}")
    for r in runs:
        print(f"  {'热' if r['warm'] else '冷'}　总 {r['total_s']}s　"
              f"{r['n_cases']} 条医案 / {r['n_embeddings']} 条向量")
    if error:
        print(f"✗ 量不到检索层：{error}", file=sys.stderr)
    if import_error:
        print(f"✗ import api.main 失败：{import_error}", file=sys.stderr)
    print(f"→ {out}")
    return 0 if (error is None and import_error is None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
