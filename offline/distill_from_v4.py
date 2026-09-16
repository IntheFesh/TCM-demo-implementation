"""R26：从 `deepseek-v4-pro` 蒸馏六步链样本（总纲 K5 的低成本版本）。

**这一轮的产物是研究证据，不是产品路径。** 最终推理后端就是 v4-pro（§1），
蒸出来的小模型只跑一次 SDT Test 作为"我们也训了"的旁证，**不进演示**。

做法：拿训练集主诉（SDT Train + 医案抽样，共 ≤ 8000 条）走 `full_context` 管道，
让教师模型在**全部知识都在上下文里**的条件下生成带原文引用的链路，落
`data/sft/distill_v4.jsonl`，再喂 `scripts/train_lora.py`。

## 三件必须先说清楚的事

**一、钱是这个脚本的主要风险，所以估算先于跑。** `--estimate` 零调用，按
`core/usage.py` 的价格表（全项目一处）算出这一跑要多少钱。超过 ¥30 要
`--yes-spend` 才肯动（§0.5 第 2 条第三款：单次真钱动作超 ¥30 要先问一声），
超过 ¥40（R26 预算上限）**给了 `--yes-spend` 也不跑**——那不是"确认一下"能
解决的事，是要用 `--limit` 把规模砍下来。

**二、估算里最不准的一项是"每次调用输出多少 token"**，因为 S3 在
`effort=max` 下把思考 token 也计在输出里。`OUT_TOKENS_PER_CALL_ESTIMATE` 是一个
**假设**，来历写在它旁边，可以用 `--out-tokens-per-call` 改。真机跑完第一批之后
按 `usage` 实测值回填这个数再估剩下的——不要拿这个假设去当账单。

**三、医案那一半是"教师抄自己语料"，SDT 那一半不是。** 医案主诉是从
`cases.json` 反推出来的，而那条医案本身就在教师的前缀里（`full_context`
把该医家全部医案都给了），教师完全可以照抄。这不是 bug：蒸馏要学的是
**格式和引用习惯**，抄得准反而好。但它意味着**不能拿医案那一半的链路质量
去说"模型会推理"**——所以每条记录都带 `teacher_saw_source_case` 标记，
下游要分开统计。SDT Train 的病案不在任何医家语料里，那一半是真生成的。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from core import usage as usage_mod
from core.physicians import resolve_physician_id
from core.schemas import CaseRecord, DistillRecord

ROOT = Path(__file__).resolve().parent.parent
#: 落盘位置。`data/sft/` 是**生成物**目录，不是 `data/standard/`（那里放人工整理的
#: 静态参考表）。`.gitignore` 的 `*.jsonl` 整体忽略正好覆盖它，这是**想要的**结果：
#: 蒸馏产物几十 MB、每次重跑都变，不该进版本控制。
#: CLAUDE.md 那条「新增 .jsonl 一律放 data/standard/」说的是参考表，
#: 判据是"这份文件是人整理的还是脚本生成的"——这份是脚本生成的。
OUT_PATH = ROOT / "data" / "sft" / "distill_v4.jsonl"
CASES_PATH = ROOT / "cases.json"

#: 总纲 R26：训练集主诉共 ≤ 8000 条（社区那套"8000 条把 V4 蒸进 9B"的配方）。
MAX_SAMPLES = 8000
#: §0.5 第 2 条第三款的那条线，和 R26 给的预算上限。两个数不是一回事：
#: 超过 ASK 是"停下来问一句、默认跑"，超过 CAP 是"这一跑不该发生"。
COST_ASK_CNY = 30.0
COST_CAP_CNY = 40.0

#: 蒸馏时每位医家只采一次。
#:
#: 产品路径下 S3 采 3 次按分挑一张（R22 的 best-of-N），因为**只有一张会被用**。
#: 蒸馏的输出**本身就是产物**：采 3 次挑 1 张等于把 2/3 的钱直接烧掉，
#: 而这一轮的全部约束就是钱（预算 ¥40）。所以这个脚本把 `S3_BEST_OF_N` 按死成 1，
#: 并且在估算里也按 1 算——估算和真跑用同一个数，否则估出来的预算是假的。
DISTILL_BEST_OF_N = 1

#: 每次 S3 调用的输出 token 估算。**这是假设不是实测**：来历是 R21 报告里
#: 单次 S3 的输出规模（一张方 + 六步链 + reasoning，约 1.2K 可见 token），
#: 乘以 `effort=max` 下思考 token 的经验倍数 3——思考 token 按输出价计费，
#: 而它比可见输出大得多。真机第一批跑完要用 `usage.completion_tokens`
#: 的实测中位数替掉它（`--out-tokens-per-call`）。
OUT_TOKENS_PER_CALL_ESTIMATE = 3600
#: S1/S2 两步的输入输出都很小（不带知识前缀、不开思考），按这个数一起算掉。
#: 同样是估算，但它对总数的影响在 1% 量级，不值得单独做一个开关。
FIXED_STEP_TOKENS_ESTIMATE = 1200


@dataclass(frozen=True)
class Sample:
    """一条待蒸馏的主诉。`sample_id` 是断点续跑的唯一键。"""

    sample_id: str
    source: str          # "sdt" | "case"
    complaint: str
    origin_case_id: str | None = None   # 医案来源那一半：教师前缀里就有这一条


def case_complaint(case: CaseRecord) -> str:
    """从一条医案反推主诉：症状 + 舌 + 脉。

    **不用 `raw_excerpt`**：那是原文（含医家的辨证与处方），拿它当主诉等于
    把答案塞进问题里。症状/舌/脉是病人能说出口的部分，其余字段（syndrome、
    treatment_principle、formula）全是要蒸馏出来的东西，一个都不能进主诉。
    """
    parts = list(case.symptoms)
    if case.tongue:
        parts.append(f"舌{case.tongue}")
    if case.pulse:
        parts.append(f"脉{case.pulse}")
    return "，".join(p.strip() for p in parts if p and p.strip())


def case_samples(cases: list[CaseRecord]) -> list[Sample]:
    """医案那一半。按 case_id 排序（确定性），主诉为空的跳过。"""
    out: list[Sample] = []
    for case in sorted(cases, key=lambda c: c.case_id):
        complaint = case_complaint(case)
        if not complaint:
            continue
        out.append(Sample(sample_id=f"case:{case.case_id}", source="case",
                          complaint=complaint, origin_case_id=case.case_id))
    return out


def sdt_samples(sdt_dir: Path | None) -> list[Sample]:
    """SDT Train 那一半。**写死 Train**——Validation/Test 是评测集，
    拿它们蒸馏就是把评测数据喂进训练，跟 `export_sft.py` 同一条理由、同一个写法。
    目录没给或读不到就返回空列表（不是错误：只跑医案那一半是合法用法）。
    """
    if sdt_dir is None:
        return []
    try:
        from eval.sdt.data import load_split
        from offline.export_sft import SDT_TRAIN_SPLIT

        records = load_split(Path(sdt_dir), SDT_TRAIN_SPLIT)
    except Exception:  # noqa: BLE001 —— 缺数据不该让 --estimate 崩掉
        return []
    out: list[Sample] = []
    for rec in sorted(records, key=lambda r: r.record_id):
        text = (rec.clinical_data or "").strip()
        if not text:
            continue
        out.append(Sample(sample_id=f"sdt:{rec.record_id}", source="sdt", complaint=text))
    return out


def stride_pick(items: list, k: int) -> list:
    """定距抽 k 条（确定性，不用随机）。

    为什么不用 `random.sample(seed=…)`：种子一样也要求 Python 版本间
    `random` 的实现不变，而这个脚本的产物要能"同一份输入两次跑出同一份数据"。
    定距抽样只依赖列表顺序，顺序由调用方的排序保证。
    """
    if k <= 0:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def build_samples(*, cases: list[CaseRecord] | None = None, sdt_dir: Path | None = None,
                  cap: int = MAX_SAMPLES) -> list[Sample]:
    """SDT 先满，医案定距补齐到 cap。

    **顺序不是随手定的**：SDT 那一半是教师没见过的病案（真生成），医案那一半
    是教师抄自己的语料。样本被 cap 砍的时候该砍掉可替代性更强的那一半。
    """
    sdt = sdt_samples(sdt_dir)
    if len(sdt) >= cap:
        return stride_pick(sdt, cap)
    room = cap - len(sdt)
    return sdt + stride_pick(case_samples(cases or []), room)


@dataclass(frozen=True)
class CostEstimate:
    n_samples: int
    n_physicians: int
    best_of_n: int
    calls: int
    prefix_tokens_total: int
    warmup_miss_tokens: int
    hit_tokens: int
    miss_tokens: int
    out_tokens: int
    cny_peak: float
    cny_off_peak: float
    prefix_known: bool

    @property
    def cny_worst(self) -> float:
        """按高峰价报。谷段五折是**可以选的**，高峰价是**保证不会超过**的那个数
        ——闸门要卡在保证值上，不能卡在"如果我们记得等到谷段"上。"""
        return self.cny_peak


def estimate_cost(n_samples: int, *, prefix_tokens: dict[str, int] | None = None,
                  complaint_tokens_mean: int = 0,
                  out_tokens_per_call: int = OUT_TOKENS_PER_CALL_ESTIMATE,
                  best_of_n: int = DISTILL_BEST_OF_N) -> CostEstimate:
    """这一跑要多少钱。零调用。

    模型：每条样本走一次完整问诊（S1 + S2 + 每位医家 best_of_n 次 S3），
    调用数问 `core.usage.calls_per_consult()`（那是全项目唯一的折算系数）。
    S3 的输入 = 知识前缀（**第一次未命中、之后命中**）+ 变化部分（永远未命中）。

    `best_of_n` 默认 `DISTILL_BEST_OF_N`（=1），**不问 `core.llm.s3_best_of_n()`**：
    那个函数回答的是"产品路径这次问诊采几次"，这里回答的是"蒸馏采几次"，
    两个问题的答案本来就不同（见 DISTILL_BEST_OF_N 的注释）。
    """
    from core.physicians import physicians_enabled

    n_phys = max(1, len(physicians_enabled()))
    best_of = max(1, best_of_n)
    calls_per = usage_mod.calls_per_consult(n_phys, best_of)
    if prefix_tokens is None:
        prefix_tokens = _prefix_tokens_or_none()
    known = bool(prefix_tokens)
    per_phys = prefix_tokens or {}
    total_prefix = sum(per_phys.values())
    s3_calls = n_samples * n_phys * best_of
    # 预热：每位医家的前缀第一次送上去是未命中价，一次就够（之后全命中）。
    warmup = total_prefix
    hit = max(0, s3_calls - n_phys) * (total_prefix // max(1, n_phys))
    # 变化部分（主诉 + S1/S2 结果 + 输出要求）每次都是新的，永远未命中。
    miss = s3_calls * complaint_tokens_mean + n_samples * FIXED_STEP_TOKENS_ESTIMATE
    out = s3_calls * out_tokens_per_call
    kw = dict(miss_tokens=warmup + miss, hit_tokens=hit, out_tokens=out)
    return CostEstimate(
        n_samples=n_samples, n_physicians=n_phys, best_of_n=best_of,
        calls=n_samples * calls_per, prefix_tokens_total=total_prefix,
        warmup_miss_tokens=warmup, hit_tokens=hit, miss_tokens=warmup + miss,
        out_tokens=out,
        cny_peak=usage_mod.cost_cny(**kw, peak=True),
        cny_off_peak=usage_mod.cost_cny(**kw, peak=False),
        prefix_known=known,
    )


def max_samples_for_budget(budget_cny: float, *, prefix_tokens: dict[str, int] | None = None,
                           complaint_tokens_mean: int = 0,
                           out_tokens_per_call: int = OUT_TOKENS_PER_CALL_ESTIMATE,
                           best_of_n: int = DISTILL_BEST_OF_N,
                           peak: bool = True) -> int:
    """这笔预算买得起多少条。

    **光有闸门不够。** 闸门只回答"太贵了"，人接着要问的是"那能跑多少条"，
    而那个数才是决定。没有这个函数的话，人只能拿 --limit 反复试，
    每试一次都要读一遍估算——那是让人去做二分查找。

    成本对 n 是线性的（预热是常数项，其余按条数走），所以取两点算斜率直接解，
    不做二分；解完再验一次（把边界上的舍入误差压掉）。
    """
    def cost_of(n: int) -> float:
        est = estimate_cost(n, prefix_tokens=prefix_tokens,
                            complaint_tokens_mean=complaint_tokens_mean,
                            out_tokens_per_call=out_tokens_per_call, best_of_n=best_of_n)
        return est.cny_peak if peak else est.cny_off_peak

    c1, c101 = cost_of(1), cost_of(101)
    marginal = (c101 - c1) / 100
    if marginal <= 0:
        return MAX_SAMPLES
    base = c1 - marginal
    n = int((budget_cny - base) // marginal)
    n = max(0, min(n, MAX_SAMPLES))
    while n > 0 and cost_of(n) > budget_cny:
        n -= 1
    return n


def _prefix_tokens_or_none() -> dict[str, int] | None:
    """每位医家的前缀多少 token。取不到（没有 cases.json / 药理层文件）返回 None
    ——**不给一个编的数**。一个编出来的成本估算比不给更糟：它会让 ¥30 那道闸门
    在错的数上开或关。"""
    try:
        from core.context_prefix import budget_plan

        plan = budget_plan()
        return {pid: sum(v.values()) for pid, v in plan.tokens_by_physician_after.items()}
    except Exception:  # noqa: BLE001
        return None


def gate(estimate: CostEstimate, *, yes_spend: bool,
         cap_cny: float = COST_CAP_CNY) -> tuple[bool, str]:
    """能不能开跑。返回 (放行, 说明)。

    三档：≤¥30 直接跑；¥30~¥40 要 `--yes-spend`；>¥40 **一律不跑**。
    前缀大小算不出来时也不跑——那时估算是 0，"0 < 30 所以放行"是最坏的一种通过。
    """
    if not estimate.prefix_known:
        return False, ("算不出知识前缀有多大（缺 cases.json 或药理层文件），"
                       "于是也算不出这一跑多少钱。先把语料准备好再跑："
                       "python -m offline.extract_cases")
    cny = estimate.cny_worst
    if cny > cap_cny:
        return False, (f"按高峰价估 ¥{cny:.1f}，超过预算上限 ¥{cap_cny:.0f}。"
                       f"这不是确认一下的事：用 --limit 把条数砍下来"
                       f"（当前 {estimate.n_samples} 条）。")
    if cny > COST_ASK_CNY:
        if not yes_spend:
            return False, (f"按高峰价估 ¥{cny:.1f}（谷段 ¥{estimate.cny_off_peak:.1f}），"
                           f"超过 ¥{COST_ASK_CNY:.0f} 这条线。确认要花就加 --yes-spend；"
                           f"谷段（北京 12–14、18–09）跑是五折。")
        return True, f"按高峰价估 ¥{cny:.1f}，已带 --yes-spend。"
    return True, f"按高峰价估 ¥{cny:.1f}，在 ¥{COST_ASK_CNY:.0f} 以内，直接跑。"


def done_keys(path: Path = OUT_PATH) -> set[tuple[str, str]]:
    """已经蒸好的 (sample_id, physician)。断点续跑靠它跳过。

    坏行**跳过但不静默**：返回的是"确定已完成"的集合，读不出来的行不算完成，
    于是会被重跑——重跑一条比漏一条好，而且重跑是幂等的（同一个键不会两份有效）。
    """
    out: set[tuple[str, str]] = set()
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            out.add((str(row["sample_id"]), str(row["physician"])))
        except Exception:  # noqa: BLE001
            continue
    return out


def chain_steps_from_s3(s3, *, cited: list[str]) -> list[dict]:
    """把一次 S3 输出转成链路步骤。

    步骤名和步骤字典的形状**都复用 `offline/export_sft.py`**（`CHAIN_STEPS` +
    `_step`）：那里已经定义了"这个项目的链路有哪几步、一步长什么样"，
    再写一套的后果是下游按步骤名分组统计时静默多出一类，而报出来的覆盖率是错的
    （R5 那轮已经踩过一次，见 export_sft 里 ITEMIZED_STEPS 的注释）。
    """
    from offline.export_sft import _step

    source = f"distill:{','.join(cited)}" if cited else "distill:"
    steps: list[dict] = []
    if s3.disease:
        steps.append(_step("病名→证型", s3.syndrome, s3.reasoning, source, source))
    else:
        steps.append(_step("症状→证型", s3.syndrome, s3.reasoning, source, source))
    steps.append(_step("证型→治法", s3.treatment_principle, None, source, None))
    chosen = s3.formula_candidates[s3.selected]
    steps.append(_step("治法→方剂", chosen.name, chosen.rationale, source, source))
    items = [{"name": h.name, "rationale": None, "rationale_source": None}
             for h in chosen.herb_items]
    steps.append(_step("方剂→药材", items, None, source, None))
    return steps


def record_for_physician(sample: Sample, physician: str, s3, *, teacher_model: str) -> DistillRecord:
    pid = resolve_physician_id(physician)
    cited = list(getattr(s3, "cited_case_ids", []) or [])
    return DistillRecord(
        sample_id=sample.sample_id, source=sample.source, physician=pid,
        complaint=sample.complaint, steps=chain_steps_from_s3(s3, cited=cited),
        teacher_model=teacher_model, case_refs=cited,
        # 医案那一半：这条医案就在教师自己的前缀里（见模块注释第三条）。
        teacher_saw_source_case=sample.source == "case",
    )


def format_estimate(est: CostEstimate, *, n_total: int) -> str:
    lines = [
        f"样本 {est.n_samples} 条（可用 {n_total} 条，上限 {MAX_SAMPLES}）",
        f"调用 {est.calls} 次 = {est.n_samples} × calls_per_consult("
        f"{est.n_physicians} 位医家, best_of_n={est.best_of_n})",
    ]
    if est.prefix_known:
        lines += [
            f"知识前缀合计 {est.prefix_tokens_total:,} token"
            f"（预热按未命中价付一次：{est.warmup_miss_tokens:,}）",
            f"未命中输入 {est.miss_tokens:,} / 命中输入 {est.hit_tokens:,} / "
            f"输出 {est.out_tokens:,} token",
            f"**高峰 ¥{est.cny_peak:.1f}，谷段 ¥{est.cny_off_peak:.1f}**"
            f"（闸门看高峰价：ASK ¥{COST_ASK_CNY:.0f} / CAP ¥{COST_CAP_CNY:.0f}）",
            f"输出 token 按每次 {OUT_TOKENS_PER_CALL_ESTIMATE} 估——**这是假设**，"
            "真机第一批跑完按实测改 --out-tokens-per-call",
        ]
    else:
        lines.append("知识前缀算不出来（缺 cases.json 或药理层文件），"
                     "所以钱也算不出来——不给一个编的数")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从 deepseek-v4-pro 蒸馏六步链样本（R26）")
    ap.add_argument("--sdt-dir", type=Path, default=None,
                    help="TCMEval-SDT 根目录；不给就只用医案那一半")
    ap.add_argument("--cases", type=Path, default=CASES_PATH)
    ap.add_argument("--out", type=Path, default=OUT_PATH)
    ap.add_argument("--limit", type=int, default=None,
                    help=f"最多蒸多少条（上限 {MAX_SAMPLES}）。不给就按 --budget-cny 现算"
                         f"能买得起多少条")
    ap.add_argument("--budget-cny", type=float, default=COST_CAP_CNY,
                    help=f"这一跑的预算（默认 R26 给的 ¥{COST_CAP_CNY:.0f}）。"
                         f"--limit 不给时由它决定条数")
    ap.add_argument("--estimate", action="store_true", help="只估钱，零调用")
    ap.add_argument(
        "--json", action="store_true",
        help="跟 --estimate 一起用：把估算结果按机器可读的形状打一行 JSON，"
             "**是 stdout 的最后一行**（前面那几行人读的估算说明照旧打）。"
             "报告里的 ¥ 数字从这里取，不手抄——手抄的数字下一次改单价就对不上了",
    )
    ap.add_argument("--yes-spend", action="store_true",
                    help=f"确认花超过 ¥{COST_ASK_CNY:.0f}（>¥{COST_CAP_CNY:.0f} 仍然不跑）")
    ap.add_argument("--out-tokens-per-call", type=int, default=OUT_TOKENS_PER_CALL_ESTIMATE)
    args = ap.parse_args(argv)

    cases: list[CaseRecord] = []
    if args.cases.exists():
        cases = [CaseRecord.model_validate(r) for r in json.loads(
            args.cases.read_text(encoding="utf-8"))]
    all_samples = build_samples(cases=cases, sdt_dir=args.sdt_dir, cap=MAX_SAMPLES)
    mean_complaint = 0
    if all_samples:
        from core.context_prefix import count_tokens

        sub = all_samples[:200]      # 200 条够估平均长度，全量算一遍纯属浪费
        mean_complaint = sum(count_tokens(s.complaint) for s in sub) // len(sub)
    prefix = _prefix_tokens_or_none()
    affordable = MAX_SAMPLES
    if prefix:
        affordable = max_samples_for_budget(
            args.budget_cny, prefix_tokens=prefix, complaint_tokens_mean=mean_complaint,
            out_tokens_per_call=args.out_tokens_per_call)
        print(f"预算 ¥{args.budget_cny:.0f}（高峰价）买得起 **{affordable}** 条；"
              f"配方要 {MAX_SAMPLES} 条——差 {MAX_SAMPLES / max(1, affordable):.0f} 倍。"
              "这个差不是可以忽略的零头，见 docs/reports/R26_report.md 第三节。")
    limit = args.limit if args.limit is not None else min(affordable, MAX_SAMPLES)
    picked = all_samples[:max(0, min(limit, MAX_SAMPLES))]
    est = estimate_cost(len(picked), prefix_tokens=prefix,
                        complaint_tokens_mean=mean_complaint,
                        out_tokens_per_call=args.out_tokens_per_call)
    print(format_estimate(est, n_total=len(all_samples)))
    print(usage_mod.peak_note())

    ok, why = gate(est, yes_spend=args.yes_spend, cap_cny=args.budget_cny)
    print(("放行：" if ok else "不跑：") + why)
    if args.estimate:
        if args.json:
            # **一行 JSON，`estimate_as_dict` 是唯一的转换处。**
            # R26 那轮这个函数的文档字符串写的是"给 `--json` 之外的调用方读的形状",
            # 而当时根本没有 --json，也没有任何调用方——一个函数的文档字符串
            # 声明了一个不存在的消费方，等于这件事只做了一半。
            print(json.dumps(estimate_as_dict(est), ensure_ascii=False, sort_keys=True))
        return 0
    if not ok:
        return 2

    skip = done_keys(args.out)
    if skip:
        print(f"断点续跑：已完成 {len(skip)} 条 (sample_id, physician)，跳过")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    return _run(picked, args.out, skip)


def _run(samples: list[Sample], out_path: Path, skip: set[tuple[str, str]]) -> int:
    """真跑。**每条样本跑完立刻追加落盘**（不攒在内存里最后统一写）：
    这一跑要几十分钟到几小时，中间任何一次失败都不该让已经花掉的钱白花。
    """
    import os

    from core.chain import consult
    from core.llm import get_llm
    from core.progress import Progress

    # 按死采样次数（见 DISTILL_BEST_OF_N）。**在 import consult 之后、跑之前设**：
    # `s3_best_of_n()` 每次调用现读环境变量，所以这里设了就生效；估算那边用的是
    # 同一个常量，两边不会各说一套。
    os.environ["S3_BEST_OF_N"] = str(DISTILL_BEST_OF_N)
    teacher = get_llm().model_name()
    bar = Progress(total=len(samples), label="蒸馏（每条一次完整问诊）", unit="条")
    written = failed = 0
    with out_path.open("a", encoding="utf-8") as f:
        for sample in samples:
            try:
                result = consult(sample.complaint)
            except Exception as e:  # noqa: BLE001 —— 一条失败不该终止整批
                failed += 1
                bar.advance(note=f"{sample.sample_id} 失败：{type(e).__name__}")
                print(f"  × {sample.sample_id}：{type(e).__name__}: {e}", file=sys.stderr)
                continue
            for row in result.get("results", []):
                pid = resolve_physician_id(row.get("physician", ""))
                if (sample.sample_id, pid) in skip:
                    continue
                s3 = row.get("syndrome")
                if s3 is None:
                    continue
                rec = record_for_physician(sample, pid, s3, teacher_model=teacher)
                f.write(json.dumps(rec.model_dump(), ensure_ascii=False) + "\n")
                f.flush()
                written += 1
            bar.advance(note=sample.sample_id)
    bar.close(f"写出 {written} 条记录，{failed} 条样本失败")
    print(f"写出 {written} 条记录，{failed} 条样本失败 → {out_path}")
    return 0 if written else 1


def estimate_as_dict(est: CostEstimate) -> dict:
    """`CostEstimate` 的机器可读形状。`--estimate --json` 和测试都走这一处。

    **不在 main() 里直接 `asdict(est)`**：那样"这个估算对外长什么样"就散在
    调用点上，将来给 CostEstimate 加字段时，报告读到的键和测试断言的键会分叉。
    """
    return asdict(est)


if __name__ == "__main__":
    raise SystemExit(main())
