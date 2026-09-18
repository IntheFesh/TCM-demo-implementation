"""R46 §7.5 第 15 条：HIS 集成接口的鉴权——API Key + IP 白名单。

等保 2.0 三级要求身份鉴别与访问控制。**这一层守的是"谁能调这两个接口"**，
跟产品模式（`core.product_mode`，守的是"哪些功能露不露"）是两件不同的事，
所以是两个模块，不合并。

## 三条

1. **默认拒绝。** 没配 `HIS_API_KEYS` 时集成接口一律 401——不是"没配就放行"。
   一个默认放行的鉴权在内网里等于没有鉴权，而医院内网并不比公网干净。
2. **key 比较用 `secrets.compare_digest`**，不用 `==`：字符串相等是短路比较，
   逐字节的耗时差可以被用来把 key 试出来。
3. **IP 白名单为空 = 不限 IP**（跟 key 不同）。理由：HIS 的出口地址在不同医院
   差别很大，强制配置会让人填一个 `0.0.0.0/0` 了事——那比不配更糟，因为它
   看起来像配过了。**key 是必配的，IP 是加固项**，两者的默认值方向不同。
"""
from __future__ import annotations

import os
import secrets

API_KEYS_ENV = "HIS_API_KEYS"
IP_ALLOWLIST_ENV = "HIS_IP_ALLOWLIST"


class IntegrationDenied(PermissionError):
    """集成接口鉴权不过。`reason` 只进日志，**不回给调用方**——
    "key 不对"和"IP 不在白名单"的区别会告诉试探者下一步该试什么。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def configured_keys() -> tuple[str, ...]:
    raw = os.environ.get(API_KEYS_ENV, "")
    return tuple(k.strip() for k in raw.split(",") if k.strip())


def ip_allowlist() -> tuple[str, ...]:
    raw = os.environ.get(IP_ALLOWLIST_ENV, "")
    return tuple(ip.strip() for ip in raw.split(",") if ip.strip())


def check(api_key: str | None, client_ip: str | None) -> None:
    """通过就返回 None，不通过抛 `IntegrationDenied`。"""
    keys = configured_keys()
    if not keys:
        raise IntegrationDenied(f"没有配置 {API_KEYS_ENV}，集成接口默认关闭")
    supplied = (api_key or "").strip()
    if not supplied:
        raise IntegrationDenied("没有提供 API key")
    if not any(secrets.compare_digest(supplied, k) for k in keys):
        raise IntegrationDenied("API key 不匹配")
    allow = ip_allowlist()
    if allow and (client_ip or "") not in allow:
        raise IntegrationDenied(f"IP {client_ip} 不在白名单里")


def status() -> dict:
    """给部署自检用：配没配、配了几个。**不回显 key 本身。**"""
    return {
        "keys_configured": len(configured_keys()),
        "ip_allowlist": len(ip_allowlist()),
        "enabled": bool(configured_keys()),
    }
