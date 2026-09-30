"""消融实验包：两套实验，各动各的开关，互不覆盖，所以分文件。

- `eval/ablation/knobs.py`：工程开关消融。四组各动一个环境变量旋钮（A 产品默认 /
  B `S3_MODE=legacy` / C `S3_BEST_OF_N=3` / D `S1S2_MERGED=1`），量三个内容指标与成本。
- `eval/ablation/spec.py`：推理消融的分组定义。五组（A/B/C/D/E）在 theory_layer /
  cases_in_derivation / corroboration 三个布尔上取值，**A–E 的定义只在这一个文件里
  写一次**——`core/theory.py`、`core/corroboration.py` 的旋钮注释、报告和运行脚本都以它
  为准，不各自另记一份（同一概念只有一处实现，见 docs/ARCHITECTURE.md §4；这里的
  "概念"是"这一组该设哪些环境变量"）。
- `eval/ablation/reasoning.py`：推理消融的运行与汇总脚本，读 `spec.py` 的分组表。
- `eval/ablation/merge.py`：把按组分别跑出来的局部报告合并成一份完整报告。

包本身不重新导出任何名字：两套消融并存时，`from eval.ablation import GROUPS` 有歧义
（工程开关的四组还是推理消融的五组），所以调用方从具体模块导入，例如
`from eval.ablation.knobs import GROUPS`。

`python -m eval.ablation` 由 `__main__.py` 转发到 `knobs.main()`（`eval/RESULTS.md` 与
`scripts/run_pipeline.sh` 都用这条命令）；推理消融的入口是
`python -m eval.ablation.reasoning`。
"""
from __future__ import annotations
