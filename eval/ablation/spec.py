"""推理消融的分组定义——**只在这一个文件里写一次**。

## 五组各是什么

    A = 无医理层 + 医案进推导相（医案模仿机制，`S3_MODE=structured`）
    B = 无医理层 + 无医案
    C = 有医理层 + 无医案   ← 核心组
    D = 有医理层 + 第三相佐证  ← 产品默认形态
    E = C 的复测（三个布尔与 C 逐一相同，量噪声地板）

`core/theory.py`、`core/corroboration.py` 的旋钮注释和 `eval/ablation/reasoning.py`
都以这里为准，不各自另记一份分组定义。

## 三个布尔，不是五个独立开关

五组只在**三个维度**上取值，不是每组各自随便设一堆开关——这正是"能不能归因"的前提
（每个维度只有两个取值，各组是这个三维立方体的顶点，不是随意组合）：

| 组 | theory_layer（医理规则层） | cases_in_derivation（医案进推导相） | corroboration（第三相事后佐证） |
|---|---|---|---|
| A | 关 | **开**（医案模仿机制） | 关（该相在这条代码路径里从不跑） |
| B | 关 | 关 | 关 |
| C | **开** | 关 | 关 |
| D | 开 | 关 | **开** |
| E | 开 | 关 | 关（同 C） |

## E 组不是第五种组合，是 C 组的复测

E 组三个布尔跟 C **逐一相同**——它不是三维立方体上的第五个顶点，是同一个
顶点上的第二次独立采样。它用来量出"两次独立采样在自由文本上天然会有多少
分歧"，作为 C-D 一致率闸门的噪声地板对照，不改变实验设计本身的三个维度，
见 `GATE_CD_CONSISTENCY_MARGIN` 与下面 `GROUPS` 的说明。

## 为什么不是自由的 2×2×2＝8 种组合

`cases_in_derivation` 与 `theory_layer`/`corroboration` 不是独立的三个旋钮——
`cases_in_derivation=True` 目前只有一种实现路径（`S3_MODE=structured`），
而这条路径从设计上就没有医理规则层的位置（医理规则层是演绎推导的地基，见
`core/theory.py`）、也没有独立的第三相佐证调用（`corroborate()` 只在
`core/chain.py::run_derivation` 里被调用一次）。
所以 A 组设 `THEORY_LAYER=off`/`CORROBORATION=off` 时，这两个旋钮对
`S3_MODE=structured` 这条路径其实是**空操作**——不是"故意关掉"，是"这条
路径本来就没有这两件事"。仍然显式设置它们（而不是留空猜测默认值），是为
了让每一组的实际环境变量组合在报告里**可核对**，不依赖"structured 模式下
这两个变量天然无效"这条隐藏知识。

## 跟工程开关消融是两回事

工程开关消融（`eval/ablation/knobs.py`）回答的是"生成流水线的工程旋钮值不值"
（单结论链 vs 三家并置、best-of-N、S1+S2 合一），每组只动 `S3_MODE` /
`S3_BEST_OF_N` / `S1S2_MERGED` 中的一个，其余保持产品默认。本文件的分组回答的是
"要不要模仿医案才能推出正确结论"——轴心是 `S3_MODE=structured`（模仿）与
`S3_MODE=derived`（演绎）之间的切换，以及演绎这一侧医理规则层、事后佐证各自的
贡献。两套消融动的是不同的旋钮子集，所以分成两个文件、两条命令，不合并成一次
大消融——合并之后两类旋钮同时变化，无从归因。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReasoningGroup:
    key: str
    name: str
    theory_layer: bool
    cases_in_derivation: bool
    corroboration: bool

    @property
    def env(self) -> dict[str, str]:
        """算出这一组要设的环境变量。**唯一一处从三个布尔翻译成实际旋钮**
        ——运行脚本、报告表头都调用这个属性，不是各自把 on/off 拼一遍字符串。"""
        return {
            "S3_MODE": "structured" if self.cases_in_derivation else "derived",
            "THEORY_LAYER": "on" if self.theory_layer else "off",
            "CORROBORATION": "on" if self.corroboration else "off",
        }

    def describe(self) -> str:
        dims = []
        dims.append("医理层" + ("开" if self.theory_layer else "关"))
        dims.append("医案" + ("进推导" if self.cases_in_derivation else "不进推导"))
        dims.append("事后佐证" + ("开" if self.corroboration else "关"))
        return f"{self.key} {self.name}：{' / '.join(dims)}"


#: **A/B/C/D/E，顺序即定义顺序**。C 是核心组（"不靠模仿也能推"的直接证据），
#: D 是产品默认形态（核心组 + 事后佐证，不回头改推导）。
#:
#: **E 是 C 组的噪声地板对照**（三个布尔与 C 逐一相同，配置上是 C 的"复测"）。
#: C 与 D 是两次**独立**的 LLM 采样，唯一的旋钮差异是 `CORROBORATION`——但自由
#: 文本（尤其是 `method` 治法措辞）在两次独立采样之间几乎不可能逐字相同，拿
#: "逐字相等"去比较，量到的是**采样方差**，不是"事后佐证不回流改推导"这条
#: 不变式本身（见 `core/corroboration.py`）。E 组量出"同一份配置，不跑第三相
#: 佐证，纯粹重复采样一次"的分歧率作为噪声地板——C 与 D 的分歧率只有明显
#: **超过**这个地板，才说明是 `CORROBORATION` 这个旋钮本身带来的差异，不是
#: 采样噪声（见 `eval/ablation/reasoning.py` 的 `pair_consistency` 与
#: `GATE_CD_CONSISTENCY_MARGIN`）。同一套比较逻辑（`pair_consistency`）既用于
#: C-D，也用于 C-E，不另写一套。
GROUPS: tuple[ReasoningGroup, ...] = (
    ReasoningGroup("A", "医案模仿机制", theory_layer=False,
             cases_in_derivation=True, corroboration=False),
    ReasoningGroup("B", "既无医理也无医案", theory_layer=False,
             cases_in_derivation=False, corroboration=False),
    ReasoningGroup("C", "纯演绎（核心组）", theory_layer=True,
             cases_in_derivation=False, corroboration=False),
    ReasoningGroup("D", "演绎+事后佐证（产品默认形态）", theory_layer=True,
             cases_in_derivation=False, corroboration=True),
    ReasoningGroup("E", "纯演绎复测（C 噪声地板对照，配置同 C）", theory_layer=True,
             cases_in_derivation=False, corroboration=False),
)

#: C-D/C-E 一致率比较的两端——**只在这里写一次**，`reasoning.py` 用这两个常量
#: 取行，不各自硬编码 "C"/"D"/"E" 字符串。
CONSISTENCY_PAIR = ("C", "D")
NOISE_FLOOR_PAIR = ("C", "E")


def group_by_key(key: str) -> ReasoningGroup:
    for g in GROUPS:
        if g.key == key:
            return g
    raise KeyError(f"没有这一组：{key!r}，只有 {[g.key for g in GROUPS]}")


#: 三条硬指标的阈值与比较对象都钉死在这里——报告生成（`eval/ablation/reasoning.py`）
#: 读这几个常量，不各自重复写一遍阈值。第三条（C 与 D 的一致率）不跟固定阈值比，
#: 见下面的 `GATE_CD_CONSISTENCY_MARGIN`。
GATE_C_VERIFIER_FIRST_PASS_VS = "A"          # C 组一次通过率 ≥ A 组
GATE_C_RULE_REFS_COMPLETENESS_MIN = 0.9      # C 组 rule_refs 完整率 ≥ 0.9

#: C 与 D 的一致率不跟固定的绝对值比：`method` 是自由文本，两次独立采样几乎不可能
#: 逐字相同，逐字相等在这上面测的是采样方差，不是"结论变了没变"，拿它跟固定阈值比
#: 没有意义（详见 `eval/ablation/reasoning.py::pair_consistency` 的文档字符串）。
#:
#: 判据是**相对**的：C-D 的一致率不能明显低于 C-E（同配置复测，见上面 `GROUPS`
#: 的说明）测出来的噪声地板——`C-D 的一致率 ≥ C-E 的一致率 − margin` 才算过。
#: margin 只留一点容差，不是把整条闸门变宽松：C-E 本身已经是"纯采样噪声"下的
#: 一致率，C-D 只要不明显比它更分散就算过。
GATE_CD_CONSISTENCY_MARGIN = 0.1

#: 闸门判定要求的最小有效样本。**唯一出处**——`--limit 2` 这类小样本把 C 组
#: 一次通过率量成 1（1/1）也会通过阈值比较，但那不是"判过了"，是"样本太小、
#: 比较没有意义"。低于这个数时，不管比率算出来是多少，那条闸门都判
#: `passed=None`（样本不足），不判 True 也不判 False——三分返回值：没测出来 /
#: 测出来没过 / 测出来过了。
GATE_MIN_SAMPLE_SIZE = 5
