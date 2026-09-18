"""版本号。语义化版本，**一处定义**：页脚、`/health`、审计记录、交付包的
文件名全部从这里取。

为什么是 `1.0.0-rc.1` 而不是 `1.0.0`：正式版的验收在 R50（全量验收与最终
演练），那一轮通过之前叫 `1.0.0` 是在给自己发一张还没考的及格证。rc 号
随每一轮收口递增，R50 通过时去掉 `-rc.N`。

为什么不从 git 描述里算：医院内网装的是离线包，那里没有 .git；一个在开发
机上能算出版本、在客户现场算不出的实现等于没有版本号。
"""
from __future__ import annotations

MAJOR = 1
MINOR = 0
PATCH = 0
#: 预发布标记。R50 全量验收通过后置空。
PRERELEASE = "rc.1"

VERSION = f"{MAJOR}.{MINOR}.{PATCH}" + (f"-{PRERELEASE}" if PRERELEASE else "")

#: 产品名。页面标题、处方笺页眉、交付包同名——**不带内部轮次编号**。
PRODUCT_NAME = "中医辨证推理辅助系统"


def version_string() -> str:
    return VERSION


def display_version() -> str:
    """页脚那一行。给使用者看的，所以带产品名、不带 commit。"""
    return f"{PRODUCT_NAME} {VERSION}"
