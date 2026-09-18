"""消融实验包。**两轮各自的开关只动一件事，两份互不相干，所以分文件**：

- `eval/ablation/r38.py`：R38 四组（S3_MODE=legacy / S3_BEST_OF_N=3 /
  S1S2_MERGED=1），R33~R51 的评测基线。R57 之前这是仓库里唯一一份
  `eval/ablation.py`，这次拆包时原样搬过来，逻辑一字未动。
- `eval/ablation/spec.py`：R57 四组（theory_layer / corroboration /
  cases_in_derivation 三个布尔），**A/B/C/D 的定义只在这一个文件里写一次**
  ——别的地方（`core/theory.py`/`core/corroboration.py` 的旋钮注释、报告、
  这个模块的运行脚本）一律引用它，不再各自复述一遍分组定义（CLAUDE.md
  「同一概念的匹配逻辑只能有一处实现」，这里的"匹配逻辑"是"这一组该设哪些
  环境变量"）。
- `eval/ablation/r57.py`：R57 的运行/汇总脚本，读 `spec.py` 的分组表。

这一层不再对外重新导出 R38 的名字——`from eval.ablation import GROUPS` 这类
写法在两轮消融并存之后天然有歧义（是 R38 的四组还是 R57 的四组）。旧代码只有
`tests/test_ablation.py` 一处引用，已经改成 `from eval.ablation.r38 import
...`，往后新代码也按这条路径 import，不留一个"看起来通用、实际固定指向 R38"
的兼容层。

CLI 入口靠 `__main__.py` 转发到 `r38.main()`——`python -m eval.ablation`
这行命令在 README/scripts/run_onsite.sh 里已经写死好几处，拆包不改外部契约。
R57 有自己的入口：`python -m eval.ablation.r57`。
"""
from __future__ import annotations
