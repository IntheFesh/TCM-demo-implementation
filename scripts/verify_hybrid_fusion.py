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

未命中时会额外打印目标医案在 dense/bm25 两路的精确排名和分数，以及用
RRF_K 手算出的融合分、跟排在它前面那几条的同款分解——只看"进没进
top-3"看不出 RRF 融合本身把它压到第几名、又是被谁挤下去的（P0-13 续，
真实案例：目标 dense 排名 100 开外、bm25 排第 2，进了候选池但被 RRF
压到第 31 名）。这个分解本身是脚本的交付物：下次再遇到"某条该进没进"
的问题，一条命令就能看到分解，不用现写 python -c。
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

# 排名分解要看清楚目标真实排第几、是被谁挤下去的，不能只探测到某个固定
# 深度就停——用一个远超任何医家医案总数的 k 顶格查，等价于拿到整份排名，
# 不用再猜"是不是恰好卡在探测深度外面"这种半信息。search() 内部只会
# min(k, 候选总数) 截断，传大 k 不会报错也不会因为语料变大就要跟着调。
FULL_RANK_K = 100_000

# 排名分解只展示排在目标前面的前几条，不是全部——真实案例目标排第 31，
# 前面 30 条全打出来是噪音，不是诊断。
BREAKDOWN_CAP = 10

MODES = ["dense", "bm25", "hybrid"]


def _rank_of(
    ranking: list[tuple[object, float]], case_id: str
) -> tuple[int | None, float | None]:
    """在按分数降序的 (case, score) 列表里找 case_id 的 (1-based 排名, 分数)；
    不在里面则 (None, None)。"""
    for i, (case, score) in enumerate(ranking, start=1):
        if case.case_id == case_id:
            return i, score
    return None, None


def _print_breakdown(
    rankings: dict[str, list[tuple[object, float]]],
    expect_case_id: str,
) -> None:
    """打印目标医案在 dense/bm25 两路的精确排名和分数，加上用 RRF_K 手算的
    融合分，以及排在它前面那几条的同款分解——这是排查"RRF 把它排到第几"
    这类问题要看的东西，不是"进没进 top-3"这一句话能回答的。"""
    from core.retrieval_hybrid import RRF_K

    def _rrf_score(case_id: str) -> float | None:
        d_rank, _ = _rank_of(rankings["dense"], case_id)
        b_rank, _ = _rank_of(rankings["bm25"], case_id)
        if d_rank is None or b_rank is None:
            return None
        return 1.0 / (RRF_K + d_rank) + 1.0 / (RRF_K + b_rank)

    def _row(case_id: str, mark: str = "") -> None:
        d_rank, d_score = _rank_of(rankings["dense"], case_id)
        b_rank, b_score = _rank_of(rankings["bm25"], case_id)
        rrf = _rrf_score(case_id)
        d_part = f"dense #{d_rank} ({d_score:.3f})" if d_rank is not None else "dense 未命中"
        b_part = f"bm25 #{b_rank} ({b_score:.3f})" if b_rank is not None else "bm25 未命中"
        rrf_part = f"RRF={rrf:.5f}" if rrf is not None else "RRF=N/A"
        print(f"  {case_id}{mark}  {d_part}  {b_part}  {rrf_part}")

    hybrid_rank, _ = _rank_of(rankings["hybrid"], expect_case_id)

    print(f"关键医案排名分解（dense 排名 / bm25 排名 -> RRF 分，RRF_K={RRF_K}）：")
    _row(expect_case_id, mark=" ★")

    if hybrid_rank is not None and hybrid_rank > 1:
        n_ahead = min(hybrid_rank - 1, BREAKDOWN_CAP)
        omitted = hybrid_rank - 1 - n_ahead
        print(f"  ------ hybrid 排在它前面的 {n_ahead} 条"
              f"（共 {hybrid_rank - 1} 条{f'，只列前 {n_ahead} 条' if omitted > 0 else ''}）------")
        for case, _score in rankings["hybrid"][:n_ahead]:
            _row(case.case_id)
    elif hybrid_rank is None:
        print("  （hybrid 排名里完全没找到这条医案——不是排名靠后，是真的没进候选池）")
    print()


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

    rankings: dict[str, list[tuple[object, float]]] = {}
    for mode in MODES:
        wide = retriever.search(args.query, args.physician, k=FULL_RANK_K, mode=mode)
        rankings[mode] = wide
        total = len(wide)
        rank, _score = _rank_of(wide, args.expect_case_id)

        print(f"[{mode}] top-{args.top_n}：")
        for case, score in wide[: args.top_n]:
            mark = " ★" if case.case_id == args.expect_case_id else ""
            print(f"  {score:.3f}  {case.case_id}{mark}")
        if rank is None or rank > args.top_n:
            if rank is not None:
                print(f"  （{args.expect_case_id} 未进 top-{args.top_n}，"
                      f"在前 {total} 里排第 {rank} 名）")
            else:
                print(f"  （{args.expect_case_id} 不在前 {total} 里）")
        print()

    hybrid_rank, _ = _rank_of(rankings["hybrid"], args.expect_case_id)
    hit = hybrid_rank is not None and hybrid_rank <= args.top_n

    print("=" * 60)
    if hit:
        print(f"★命中：{args.expect_case_id} 出现在 hybrid 模式 top-{args.top_n} 里，"
              "P0-13 的融合准入修复生效。")
    else:
        # 命中/未命中的最终结论打 stderr：CI 里 stdout 常被吞掉或另作日志用，
        # 退出码之外还要有一行肉眼可读的失败原因不依赖 stdout 是否被留下来。
        print(f"✗ 未命中，本次修复未生效：{args.expect_case_id} 没有出现在 "
              f"hybrid 模式 top-{args.top_n} 里。", file=sys.stderr)
        print()
        _print_breakdown(rankings, args.expect_case_id)
    return 0 if hit else 1


if __name__ == "__main__":
    sys.exit(main())
