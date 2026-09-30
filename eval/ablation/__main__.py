"""`python -m eval.ablation ...` 转发到工程开关消融的入口 `knobs.main()`。
推理消融走 `python -m eval.ablation.reasoning`，不共用这个入口：两套消融的参数形状
不同，硬凑一个入口反而要在里面再分派一层。
"""
from __future__ import annotations

from eval.ablation.knobs import main

if __name__ == "__main__":
    raise SystemExit(main())
