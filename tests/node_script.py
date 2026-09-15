"""前端测试跑 node 的共用入口。

原来 11 个前端测试各自写 `subprocess.run(["node", "-e", DOM_STUB + script + tail])`。
`-e` 是把整份脚本当**一个命令行参数**交出去，而 Linux 上单个参数的上限
（MAX_ARG_STRLEN）是 128KB。`web/index.html` 的内联脚本加上 DOM stub 和用例
自己的尾巴，在 D1/F1 这一轮越过了这条线，症状是：

    OSError: [Errno 7] Argument list too long: 'node'

看起来像 node 崩了，其实是 `exec` 根本没起来，跟被测代码毫无关系。把脚本写成
临时文件再 `node 文件` 就没有这个上限。

**文件刻意不删**：临时目录本来就会被系统清理，留着的好处是测试挂掉时可以直接
`node /tmp/xxx.js` 原样复现，不用再去拼一遍脚本。
"""
from __future__ import annotations

import tempfile


def js_tmp(source: str) -> str:
    """把脚本写进临时文件，返回路径，给 subprocess 当 node 的参数用。"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(source)
        return f.name
