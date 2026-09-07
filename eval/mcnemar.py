"""V1：自实现的 McNemar 检验，不依赖 scipy（这台环境的 .venv 里恰好装了 scipy——
sentence-transformers 的传递依赖——但项目本身不该依赖它：demo 环境不一定有，
AutoDL 上装的包版本也可能不同，自实现才能保证跟 core/setstats.py 的
"不依赖 numpy/scipy" 原则一致）。

用途：判断两个配置（比如两种检索 mode、开/关 ReAct、开/关某个安全否决规则）
在同一批查询上的**配对二元结果**是否有显著差异——不是比较两个独立样本的比例，
是比较同一批输入分别喂给 A、B 两个配置后，"结果不一致"的那些配对里，
"A 对 B 错" 和 "A 错 B 对" 是否明显不对称。只看不一致的配对（discordant
pairs），一致的配对（A、B 同对或同错）不提供信息，经典 McNemar 检验就是
这么定义的。

两种计算方式，按样本量自动选：
  - n = b+c < 25：精确二项检验（零假设下 min(b,c) ~ Binomial(n, 0.5)），
    小样本下卡方近似不可靠，这是标准做法。
  - n >= 25：连续性校正的卡方近似（1 自由度）。卡方(1) 的 CDF 用
    `erf(sqrt(x/2))` 算，不需要 scipy——这是卡方(1)分布和标准正态分布的
    解析关系（X~chi2(1) <=> X=Z^2, Z~N(0,1)），不是近似。

两个分支都已经拿 scipy.stats（开发环境里可用，仅用于离线验证，不是运行时
依赖）逐个用例核对过，浮点精度内完全一致。
"""
from __future__ import annotations

import math


def mcnemar_test(b: int, c: int) -> dict:
    """b = "A 对 B 错" 的配对数，c = "A 错 B 对" 的配对数（"对/错"是这次比较
    的二元结果，调用方自己定义——可以是"检索 top-1 命中预期"，也可以是
    "触发了安全否决"，McNemar 本身不关心具体语义，只关心配对的不一致方向）。

    返回 p_value：零假设是"不一致的配对里，两个方向出现概率相等"（即两个
    配置在总体上没有差异）。p_value 越小，越有证据说明 A、B 系统性不同，
    不是随机噪声。
    """
    if b < 0 or c < 0:
        raise ValueError(f"b/c 是配对计数，不能为负：b={b}, c={c}")
    n = b + c
    if n == 0:
        return {
            "b": b, "c": c, "n_discordant": 0, "statistic": None, "p_value": 1.0,
            "method": "no_discordant_pairs",
            "note": "A、B 在所有配对上结果一致，没有不一致的配对可供比较，无法拒绝原假设。",
        }
    if n < 25:
        k = min(b, c)
        p_value = min(
            1.0,
            2 * sum(math.comb(n, i) * (0.5 ** n) for i in range(k + 1)),
        )
        return {
            "b": b, "c": c, "n_discordant": n, "statistic": None,
            "p_value": round(p_value, 6), "method": "exact_binomial",
        }
    statistic = (abs(b - c) - 1) ** 2 / n
    p_value = 1 - math.erf(math.sqrt(statistic / 2))
    return {
        "b": b, "c": c, "n_discordant": n, "statistic": round(statistic, 6),
        "p_value": round(p_value, 6), "method": "chi2_continuity_corrected",
    }


def paired_outcomes_to_bc(
    outcomes_a: list[bool], outcomes_b: list[bool]
) -> tuple[int, int]:
    """把两组等长的配对二元结果（比如"这条查询在配置 A 下命中"）转成
    McNemar 需要的 (b, c)。b = A 命中且 B 未命中的条数，c 反过来。"""
    if len(outcomes_a) != len(outcomes_b):
        raise ValueError(
            f"两组结果长度必须一致（同一批查询才能配对）：{len(outcomes_a)} vs {len(outcomes_b)}"
        )
    b = sum(1 for a, bb in zip(outcomes_a, outcomes_b) if a and not bb)
    c = sum(1 for a, bb in zip(outcomes_a, outcomes_b) if not a and bb)
    return b, c
