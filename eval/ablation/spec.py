"""R57 四组消融的分组定义——**只在这一个文件里写一次**。

## 出处

任务书原文（上下文压缩前的原始指令，仓库其余地方找不到逐字留痕）：

    A = 无医理层 + 医案进推导相（现状模仿机制）
    B = 无医理层 + 无医案
    C = 有医理层 + 无医案   ← 核心组
    D = 有医理层 + 第三相佐证  ← 最终形态

`core/corroboration.py` 曾经有一条早前写的注释说"A/B 组关、C/D 组开"，跟这份
定义矛盾——那条注释是错的，已经改成引用这里。`docs/reports/
R55_followup_checkpoint.md`「R57 消融若 C 组不达标」一节里的说法
（C 组关掉医案佐证）是对的，跟这里一致。

## 三个布尔，不是四个独立开关

四组只在**三个维度**上取值，不是四组各自随便设一堆开关——这正是"能不能
归因"的前提（同 R38 那条"一组只动一个开关"的精神，这里是"每个维度只有
两个取值，四组是这三维立方体的四个顶点，不是随意组合"）：

| 组 | theory_layer（医理规则层） | cases_in_derivation（医案进推导相） | corroboration（第三相事后佐证） |
|---|---|---|---|
| A | 关 | **开**（现状模仿机制） | 关（该相在这条代码路径里从不跑） |
| B | 关 | 关 | 关 |
| C | **开** | 关 | 关 |
| D | 开 | 关 | **开** |

## R61：E 组不是第五种组合，是 C 组的复测

E 组三个布尔跟 C **逐一相同**——它不是三维立方体上的第五个顶点，是同一个
顶点上的第二次独立采样。任务书原文只有四组，E 是 R61 为了量出"两次独立
采样在自由文本上天然会有多少分歧"而加的噪声地板对照，不改变实验设计本身
的三个维度，见 `GATE_CD_CONSISTENCY_MARGIN` 与下面 `GROUPS` 的说明。

## 为什么不是自由的 2×2×2＝8 种组合

`cases_in_derivation` 与 `theory_layer`/`corroboration` 不是独立的三个旋钮——
`cases_in_derivation=True` 目前只有一种实现路径（`S3_MODE=structured`），
而这条路径从设计上就没有 R51 医理规则层的位置（`core/theory.py` 的模块
文档字符串："这是 R52 演绎推导的地基"）、也没有独立的第三相佐证调用
（`corroborate()` 只在 `core/chain.py::run_derivation` 里被调用一次）。
所以 A 组设 `THEORY_LAYER=off`/`CORROBORATION=off` 时，这两个旋钮对
`S3_MODE=structured` 这条路径其实是**空操作**——不是"故意关掉"，是"这条
路径本来就没有这两件事"。仍然显式设置它们（而不是留空猜测默认值），是为
了让每一组的实际环境变量组合在报告里**可核对**，不依赖"structured 模式下
这两个变量天然无效"这条隐藏知识。

## 跟 R38 消融是两回事

R38（`eval/ablation/r38.py`）的四组回答的是"生成流水线的工程旋钮"（五家
融合 vs 三家并置、best-of-N、S1+S2 合一）——四组**全部**走
`S3_MODE=structured`。R57 这四组回答的是"要不要模仿医案才能推出正确结论"
——轴心是 `S3_MODE=structured`（模仿）与 `S3_MODE=derived`（演绎）之间的
切换，以及演绎这一侧医理规则层/事后佐证各自的贡献。两组消融动的是不同的
旋钮子集，互不覆盖，所以分成两个文件、两条命令，不合并成一次"八组"消融——
合并了会两件事一起变，谁也说不清是哪个旋钮的功劳。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class R57Group:
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


#: **A/B/C/D，顺序即定义顺序**。C 是核心组（"不靠模仿也能推"的直接证据），
#: D 是最终形态（核心组 + 事后佐证，不回头改推导）。
#:
#: **R61 新增 E：C 组的噪声地板对照**（跟 C 组三个布尔逐一相同，配置上
#: 是 C 的"复测"）。C 与 D 是两次**独立**的 LLM 采样，唯一的旋钮差异是
#: `CORROBORATION`——但自由文本（尤其是 `method` 治法措辞）在两次独立采样
#: 之间几乎不可能逐字相同，拿"逐字相等"去比较，量到的是**采样方差**，不是
#: R54 那条"事后佐证不回流改推导"的不变式本身。E 组存在的意义就是量出
#: "同一份配置，不跑第三相佐证，纯粹重复采样一次"的分歧率作为噪声地板——
#: C 与 D 的分歧率只有明显**超过**这个地板，才说明是 `CORROBORATION` 这个
#: 旋钮本身带来的差异，不是采样噪声（见 `eval/ablation/r57.py`
#: `pair_consistency`/`GATE_CD_CONSISTENCY_MARGIN`）。同一套比较逻辑
#: （`pair_consistency`）既用于 C-D，也用于 C-E，不是另写一套。
GROUPS: tuple[R57Group, ...] = (
    R57Group("A", "现状模仿机制", theory_layer=False,
             cases_in_derivation=True, corroboration=False),
    R57Group("B", "既无医理也无医案", theory_layer=False,
             cases_in_derivation=False, corroboration=False),
    R57Group("C", "纯演绎（核心组）", theory_layer=True,
             cases_in_derivation=False, corroboration=False),
    R57Group("D", "演绎+事后佐证（最终形态）", theory_layer=True,
             cases_in_derivation=False, corroboration=True),
    R57Group("E", "纯演绎复测（C 噪声地板对照，配置同 C）", theory_layer=True,
             cases_in_derivation=False, corroboration=False),
)

#: C-D/C-E 一致率比较的两端——**只在这里写一次**，`r57.py` 用这两个常量
#: 取行，不各自硬编码 "C"/"D"/"E" 字符串。
CONSISTENCY_PAIR = ("C", "D")
NOISE_FLOOR_PAIR = ("C", "E")


def group_by_key(key: str) -> R57Group:
    for g in GROUPS:
        if g.key == key:
            return g
    raise KeyError(f"没有这一组：{key!r}，只有 {[g.key for g in GROUPS]}")


#: R57 §「R57 是本轮成败的唯一判据」定下的三条硬指标，数字与比较对象都钉死
#: 在这里——报告生成、验收脚本都读这几个常量，不各自重复写一遍阈值。
GATE_C_VERIFIER_FIRST_PASS_VS = "A"          # C 组一次通过率 ≥ A 组
GATE_C_RULE_REFS_COMPLETENESS_MIN = 0.9      # C 组 rule_refs 完整率 ≥ 0.9

#: **R61**：C 与 D 的一致率不再跟绝对值 0.9 比（`GATE_CD_CONSISTENCY_MIN`
#: 已删除）——用户真机实测：q1 两组的证型、主方字面完全相同，唯独治法措辞
#: 不同（"疏肝理气，和胃止痛" vs "疏肝解郁，理气和胃"），就被判"不一致"。
#: `method` 是自由文本，两次独立采样几乎不可能逐字相同，逐字相等在这上面
#: 测的是采样方差，不是不变式，拿它跟固定阈值比没有意义（详见
#: `eval/ablation/r57.py::pair_consistency` 的文档字符串）。
#:
#: 改成**相对判据**：C-D 的分歧率不能明显超过 C-E（同配置复测，见 spec.py
#: 顶部 GROUPS 的 R61 说明）测出来的噪声地板——`C-D 的一致率 ≥ C-E 的
#: 一致率 − margin` 才算过。这条 margin 只留一点容差（用户建议 0.1），
#: 不是把整条闸门变宽松：C-E 本身已经是"纯采样噪声"的上限，C-D 只要不明显
#: 比它还分散就算过。
GATE_CD_CONSISTENCY_MARGIN = 0.1

#: R59：闸门判定要求的最小有效样本。**唯一出处**——`--limit 2` 这类小样本
#: 探针把 C 组一次通过率量成 1（1/1）也会通过阈值比较，但那不是"判过了"，
#: 是"样本太小、比较没有意义"。低于这个数时，不管比率算出来是多少，
#: 那条闸门都判 `passed=None`（⏳ 样本不足），不判 True 也不判 False——
#: CLAUDE.md 的三分返回值在这里的实例：没测出来 / 测出来没过 / 测出来过了。
GATE_MIN_SAMPLE_SIZE = 5
