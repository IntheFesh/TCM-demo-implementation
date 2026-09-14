"""全项目源码级告警扫描：每个 `.py` 从**源文件**编译一遍，编译期告警一律算失败。

**为什么不能只靠 pytest.ini 的 filterwarnings**：那条 filter 只在告警真的被抛出来时
生效，而 CPython 把 `.py` 编译成 `.pyc` 之后**不会再编译第二次**——`__pycache__` 是
热的时候 `import` 根本不触发编译，非法转义序列那条告警就不会出现。这是实测过的：
清 `__pycache__` 之前 `pytest` 报 "1 warning"（starlette 那条第三方告警），清掉之后
才报 2 条。**一条只在冷缓存下才会触发的闸门，等于没有闸门。**

所以这里的判据不走 import，直接读源文件 `compile()`，跟缓存状态无关。
"""
import pathlib
import warnings

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {"__pycache__", ".venv", "venv", ".git", "books", "out"}


def _python_sources() -> list[pathlib.Path]:
    return [p for p in sorted(ROOT.rglob("*.py"))
            if not any(part in SKIP_DIRS for part in p.parts)]


def test_there_are_sources_to_scan():
    """扫描器本身不能空转：一个扫到 0 个文件的扫描永远是绿的。"""
    assert len(_python_sources()) > 50


def test_no_source_compiles_with_a_warning():
    """`\\|` 这类非法转义序列在 3.11 是 DeprecationWarning、3.12 起是 SyntaxWarning、
    3.14 会变成 SyntaxError。现在红，好过那天整个包 import 不进来。

    要在注释/文档字符串里写正则，用 raw string（`r\"\"\"…\"\"\"`），不要把反斜杠
    写成 `\\\\|` ——那段文字本身就是给人读的正则，多一层转义会读错。
    """
    problems: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(source, str(path), "exec")
        for w in caught:
            rel = path.relative_to(ROOT)
            problems.append(f"{rel}: {w.category.__name__}: {w.message}")
    assert not problems, "源码编译时有告警：\n" + "\n".join(problems)
