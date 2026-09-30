"""版本号。语义化版本，**一处定义**：页脚、`/health`、审计记录与导出文件都从这里取。

不从 git 描述里计算：离线部署的环境里没有 `.git`，版本号必须随代码一起分发。
"""
from __future__ import annotations

MAJOR = 1
MINOR = 0
PATCH = 0
#: 预发布标记（例如 "rc.1"）。正式版为空。
PRERELEASE = ""

VERSION = f"{MAJOR}.{MINOR}.{PATCH}" + (f"-{PRERELEASE}" if PRERELEASE else "")

#: 产品名。页面标题、处方笺页眉与导出文件同名。
PRODUCT_NAME = "中医辨证推理辅助系统"


def version_string() -> str:
    return VERSION


def display_version() -> str:
    """页脚那一行。给使用者看的，所以带产品名、不带 commit。"""
    return f"{PRODUCT_NAME} {VERSION}"
