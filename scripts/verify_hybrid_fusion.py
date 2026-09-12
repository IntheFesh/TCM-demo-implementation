"""P0-13 验证 C：hybrid 融合修复在真实语料上是否生效。

沙盒（本次改代码的环境）没有 cases.json/embedding 模型/网络，验证不了这个——
这个脚本就是留给有真实语料的机器（AutoDL）跑的，写好参数化，不用每次现写
python -c。

用法（默认参数就是 P0-13 报告里那条"情志不畅"的真实案例，直接跑）：
    python scripts/verify_hybrid_fusion.py

也可以换一条主诉验证别的案例：
    python scripts/verify_hybrid_fusion.py \\
        --query "其他主诉文本" --physician wu_jutong \\
        --expect-case-id wu_jutong-0001-p0-0

退出码：期望的 case_id 出现在 hybrid 模式 top-3 里 -> 0；没出现 -> 1
（能直接接进 CI 或 shell 里的 `&&` 判断）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 直接跑 `python scripts/verify_hybrid_fusion.py`（不是 `python -m
# scripts.verify_hybrid_fusion`）时，Python 只把这个脚本自己所在的目录
# （scripts/）放进 sys.path，项目根目录（core/ 所在的地方）不在里面，
# `from core.retrieval import get_retriever` 会直接 ModuleNotFoundError。
# 用户给的运行说明就是直接跑脚本路径这种形式，脚本要能扛住这种调用方式，
# 不能要求对方记住换成 -m 调用。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 默认参数：P0-13 报告里那条实测失败的真实案例——「情志不畅」这条主诉，
# 全库唯一精确命中"情志诱因"的医案。改完 core/retrieval_hybrid.py 的融合
# 准入逻辑之后，这条应该出现在 hybrid 模式的 top-3 里。
DEFAULT_QUERY = "胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦"
DEFAULT_PHYSICIAN = "ye_tianshi"
DEFAULT_EXPECT_CASE_ID = "ye_tianshi-0030-p0-0"

# top-3 之外再探一次更宽的排名，给"没命中"的诊断信息——跟 P0-13 报告里
# "这条不在 top-50 里"是同一种诊断方式，不是脚本必须要求的行为，只是让
# 排查更快。
DIAGNOSTIC_RANK_DEPTH = 50

MODES = ["dense", "bm25", "hybrid"]


def _rank_and_top(retriever, query: str, physician: str, mode: str, expect_case_id: str,
                   top_n: int, depth: int) -> tuple[list[tuple[str, float]], int | None]:
    """返回 (top_n 条 (case_id, score)，expect_case_id 在更宽的 depth 名单里的排名
    （1-based；不在里面则 None））。"""
    wide = retriever.search(query, physician, k=depth, mode=mode)
    top = [(c.case_id, score) for c, score in wide[:top_n]]
    rank = None
    for i, (case, _score) in enumerate(wide, start=1):
        if case.case_id == expect_case_id:
            rank = i
            break
    return top, rank


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P0-13 验证 C：hybrid 融合修复在真实语料上是否生效"
    )
    ap.add_argument("--query", default=DEFAULT_QUERY, help="主诉文本")
    ap.add_argument("--physician", default=DEFAULT_PHYSICIAN, help="医家 id")
    ap.add_argument("--expect-case-id", default=DEFAULT_EXPECT_CASE_ID,
                     help="期望出现在 hybrid 模式 top-3 里的 case_id")
    ap.add_argument("--top-n", type=int, default=3, help="并排展示的 top-n（默认 3）")
    args = ap.parse_args(argv)

    from core.retrieval import get_retriever

    try:
        retriever = get_retriever()
    except FileNotFoundError as e:
        print(f"检索器初始化失败：{e}", file=sys.stderr)
        return 1

    print(f"主诉：{args.query}")
    print(f"医家：{args.physician}")
    print(f"期望命中：{args.expect_case_id}")
    print()

    results: dict[str, tuple[list[tuple[str, float]], int | None]] = {}
    for mode in MODES:
        top, rank = _rank_and_top(
            retriever, args.query, args.physician, mode, args.expect_case_id,
            args.top_n, DIAGNOSTIC_RANK_DEPTH,
        )
        results[mode] = (top, rank)
        print(f"[{mode}] top-{args.top_n}：")
        for case_id, score in top:
            mark = " ★" if case_id == args.expect_case_id else ""
            print(f"  {score:.3f}  {case_id}{mark}")
        if not any(case_id == args.expect_case_id for case_id, _ in top):
            if rank is not None:
                print(f"  （{args.expect_case_id} 未进 top-{args.top_n}，"
                      f"在前 {DIAGNOSTIC_RANK_DEPTH} 里排第 {rank} 名）")
            else:
                print(f"  （{args.expect_case_id} 不在前 {DIAGNOSTIC_RANK_DEPTH} 里）")
        print()

    hybrid_top, _ = results["hybrid"]
    hit = any(case_id == args.expect_case_id for case_id, _ in hybrid_top)

    print("=" * 60)
    if hit:
        print(f"★命中：{args.expect_case_id} 出现在 hybrid 模式 top-{args.top_n} 里，"
              "P0-13 的融合准入修复生效。")
    else:
        # 命中/未命中的最终结论打 stderr：CI 里 stdout 常被吞掉或另作日志用，
        # 退出码之外还要有一行肉眼可读的失败原因不依赖 stdout 是否被留下来。
        print(f"✗ 未命中，本次修复未生效：{args.expect_case_id} 没有出现在 "
              f"hybrid 模式 top-{args.top_n} 里。", file=sys.stderr)
    return 0 if hit else 1


if __name__ == "__main__":
    sys.exit(main())
