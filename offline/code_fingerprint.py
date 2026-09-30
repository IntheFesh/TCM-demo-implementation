"""源代码指纹：忽略注释与文档字符串。

生成物的 manifest 用它记录"数据由哪一版代码生成"。注释与文档字符串不影响生成结果，
所以不进指纹：只改注释不需要重新生成数据；改了代码（包括打印的提示文字）指纹就会变。

只用 `ast` 的节点位置和 `tokenize` 的 COMMENT 记号定位要去掉的内容，
再对剩下的源码文本求 sha256。不直接哈希 `ast.dump()`：它的输出格式随 Python 版本变化。
"""
from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from pathlib import Path


def _docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def normalized_source(src: str) -> str:
    """去掉文档字符串、注释、行尾空白与空行之后的源码。"""
    drop = _docstring_lines(ast.parse(src))
    comment_at: dict[int, int] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            comment_at[tok.start[0]] = tok.start[1]
    out = []
    for lineno, line in enumerate(src.splitlines(), 1):
        if lineno in drop:
            continue
        if lineno in comment_at:
            line = line[:comment_at[lineno]]
        line = line.rstrip()
        if line:
            out.append(line)
    return "\n".join(out)


def code_fingerprint(path: Path) -> str:
    return hashlib.sha256(
        normalized_source(path.read_text(encoding="utf-8")).encode("utf-8")).hexdigest()
