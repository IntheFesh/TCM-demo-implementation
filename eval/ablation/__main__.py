"""`python -m eval.ablation ...` 转发到 R38 的入口——这行命令在
README/scripts/run_onsite.sh 里已经写死好几处，拆包成 `eval/ablation/`
之后不改这份外部契约。R57 走 `python -m eval.ablation.r57`，不共用这个入口
（两轮消融的参数形状不一样，硬凑一个入口反而要在里面分派，多一层不必要的
判断）。
"""
from __future__ import annotations

from eval.ablation.r38 import main

if __name__ == "__main__":
    raise SystemExit(main())
