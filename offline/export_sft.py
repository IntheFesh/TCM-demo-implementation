"""从 cases.json 派生 SFT 训练样本（alpaca 格式），写出 sft.jsonl。

现在数据量不够训练，这一步只是把管道建好。

用法：
    python -m offline.export_sft
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

from core.schemas import CaseRecord

CASES_PATH = Path(__file__).resolve().parent.parent / "cases.json"
OUT_PATH = Path(__file__).resolve().parent.parent / "sft.jsonl"
# 总纲 5.1（M15）：六层链路格式的 rationale 只从医案三元组的 source_span 来
# （offline/extract_case_triples.py 的产物），模型不能编。
TRIPLES_PATH = Path(__file__).resolve().parent.parent / "data" / "case_triples.jsonl"
CHAIN_OUT_PATH = Path(__file__).resolve().parent.parent / "sft_chain.jsonl"
# 按 case_group_id 切 train/heldout：同一病人的所有诊次必须在同一侧，不然
# 复诊跟初诊内容高度重复，heldout 会泄漏（总纲 5.2 要求"必须报 gap"，gap 的
# 前提是 heldout 真的没见过）。比例是训练前的默认值，不是调过的数。
DEFAULT_HELDOUT_RATIO = 0.1


def load_cases(path: Path = CASES_PATH) -> list[CaseRecord]:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return [CaseRecord.model_validate(r) for r in raw]


def filter_public_domain(cases: list[CaseRecord]) -> list[CaseRecord]:
    """版权合规的代码级强制点：现代出版书籍将来接入时若标了 copyrighted，
    这里必须把它挡在训练集之外。过滤掉的条数打到 stderr，不静默——违规数据
    要在开发期就被看见。之前这里有一句 assert，断言的是过滤之后的列表全是
    public_domain：那是过滤的定义本身，永远为真，而且 python -O 会把它删掉。"""
    kept = [c for c in cases if c.copyright_status == "public_domain"]
    excluded = [c.case_id for c in cases if c.copyright_status != "public_domain"]
    if excluded:
        print(f"[export_sft] 排除 {len(excluded)} 条非公有领域医案：{excluded[:10]}"
              f"{'…' if len(excluded) > 10 else ''}", file=sys.stderr)
    return kept


def filter_incompatible_pairs(cases: list[CaseRecord],
                             include: bool = False) -> list[CaseRecord]:
    """总纲 2.5：处方含十八反十九畏配伍的医案（李可医案那类敢用反药的名家）
    不进训练集——系统的安全层会拦这类配伍，进了训练集等于教模型开反药，
    跟 M2 的检查直接冲突。判定在抽取边界上做好（CaseRecord.has_incompatible_
    pair，core.safety_output.check_incompatible 唯一实现），这里只按标记过滤。
    过滤掉的条数打到 stderr，不静默——README 里要说明这个取舍，数字得有。

    include=True（CLI 的 --include-incompatible）把它们带上。**默认是排除**：
    一个要显式打开的开关，才能保证"没人主动要求"时训练集里不会混进反药样本。
    打开时照样把条数和药对打出来并明确警告——带上它们是一个研究选择
    （比如专门研究"名家为什么敢用反药"），不是一个可以顺手做的默认动作。"""
    if include:
        tagged = [c.case_id for c in cases if c.has_incompatible_pair]
        if tagged:
            print(f"[export_sft] **--include-incompatible：把 {len(tagged)} 条含十八反十九畏"
                  f"配伍的医案也导出了**（默认是排除的）。这些样本会教模型开反药，"
                  f"而输出侧 check_incompatible 又会拦住它——训练出来的模型在自己的"
                  f"安全层面前跑不通。只有在明确知道自己在做什么时才用这个开关："
                  f"{tagged[:10]}{'…' if len(tagged) > 10 else ''}", file=sys.stderr)
        return list(cases)
    kept = [c for c in cases if not c.has_incompatible_pair]
    excluded = [c.case_id for c in cases if c.has_incompatible_pair]
    if excluded:
        print(f"[export_sft] 排除 {len(excluded)} 条含十八反十九畏配伍的医案（不教模型开反药）："
              f"{excluded[:10]}{'…' if len(excluded) > 10 else ''}", file=sys.stderr)
    return kept


def _sample(task: str, instruction: str, input_text: str, output: str, case: CaseRecord) -> dict:
    return {
        "instruction": instruction,
        "input": input_text,
        "output": output,
        "meta": {
            "physician_id": case.physician,
            "case_id": case.case_id,
            "task": task,
            "copyright_status": case.copyright_status,
        },
    }


def to_samples(case: CaseRecord) -> list[dict]:
    samples: list[dict] = []

    if case.syndrome:
        tongue = case.tongue or "未记"
        pulse = case.pulse or "未记"
        symptoms = "；".join(case.symptoms) if case.symptoms else "（无记录症状）"
        output = case.syndrome
        if case.pathogenesis:
            output = f"{case.syndrome}。病机：{case.pathogenesis}"
        samples.append(
            _sample(
                task="T1_辨证",
                instruction="根据以下症状、舌象、脉象，给出中医证型与病机。",
                input_text=f"症状：{symptoms}\n舌象：{tongue}\n脉象：{pulse}",
                output=output,
                case=case,
            )
        )

    if case.treatment_principle and case.syndrome:
        samples.append(
            _sample(
                task="T2_立法",
                instruction="根据以下中医证型，给出相应的治法。",
                input_text=f"证型：{case.syndrome}",
                output=case.treatment_principle,
                case=case,
            )
        )

    if case.herbs and case.syndrome and case.treatment_principle:
        formula_line = f"方名：{case.formula}\n" if case.formula else ""
        samples.append(
            _sample(
                task="T3_处方",
                instruction="根据以下中医证型与治法，给出方名（如有）与药物组成。",
                input_text=f"证型：{case.syndrome}\n治法：{case.treatment_principle}",
                output=f"{formula_line}药物：{'、'.join(case.herbs)}",
                case=case,
            )
        )

    # T7 的输入必须是**这一诊**对应的原文。raw 是整个粗段（同段多个病人、多诊共享、内容
    # 完全相同），拿它当输入而输出只有一个病人一诊的字段，会导出 N 条"同一输入、互相
    # 矛盾的输出"的训练样本，而且输出刻意省略输入里明明存在的信息——跟指令里
    # "原文中没有出现的信息填 null、禁止推断"自相矛盾。有 raw_excerpt 用 raw_excerpt；
    # 没有的话只在能确定整段就是这一诊（段内第 0 个病人的初诊）时才退回 raw。
    t7_input = case.raw_excerpt
    # 没有 -p 后缀的 case_group_id 是旧格式/单病人记录，整段就是这一个病人
    first_patient = case.case_group_id.endswith("-p0") or "-p" not in case.case_group_id
    if not t7_input and case.raw and (case.visit_index or 0) == 0 and first_patient:
        t7_input = case.raw
    if t7_input:
        structured = {
            "symptoms": case.symptoms,
            "tongue": case.tongue,
            "pulse": case.pulse,
            "syndrome": case.syndrome,
            "pathogenesis": case.pathogenesis,
            "treatment_principle": case.treatment_principle,
            "formula": case.formula,
            "herbs": case.herbs,
        }
        samples.append(
            _sample(
                task="T7_抽取",
                instruction="把下面这条古籍医案原文抽取成结构化字段（JSON），"
                "原文中没有出现的信息填 null 或空列表，禁止推断。",
                input_text=t7_input,
                output=json.dumps(structured, ensure_ascii=False),
                case=case,
            )
        )

    return samples


# ---------- 总纲 5.1（M15）：六层链路格式 ----------
#
# 每条样本是 {input, chain:[{step, output, rationale, source}], meta}。**每步的
# rationale 必须从原文来**（医案三元组的 source_span），找不到就是 None，不编
# ——这是"推理"和"匹配"的区别所在。这里能从 cases.json + case_triples.jsonl
# 派生的只有医案层这几步（症状→病机→证型→治法→方剂→药材）；总纲里
# "证素→病名"（SDT）、"病名→证型"（教材）、药材 role/性味（药理层）三个来源
# 要等 SDT 数据、教材扩充、阶段二的 materia_medica.jsonl 就位再接——接的
# 方式就是往 chain 里再加 step，格式不用改。


def load_triples_by_case(path: Path = TRIPLES_PATH) -> dict[str, list[dict]]:
    """data/case_triples.jsonl 按 case_id 分组。文件不存在返回空字典——那时
    每一步的 rationale 都是 None，导出照常进行但会在统计里报出来，不是错误。"""
    if not path.exists():
        return {}
    grouped: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("case_id"):
            grouped.setdefault(row["case_id"], []).append(row)
    return grouped


def _rationale(triples: list[dict], predicate: str, o: str | None = None) -> str | None:
    """这一步的原文依据：同一诊三元组里谓词匹配（要求宾语时宾语也匹配）的第一
    条 source_span。没有就 None——不退回整段原文冒充依据。"""
    for t in triples:
        if t.get("p") != predicate:
            continue
        if o is not None and (t.get("o") or "") != o:
            continue
        span = (t.get("source_span") or "").strip()
        if span:
            return span
    return None


def to_chain_sample(case: CaseRecord, triples: list[dict]) -> dict | None:
    """一条医案 → 一条六层链路样本；能派生的步骤不足两步（不成链）返回 None。
    步骤只按字段存在与否生成，缺哪层就没有哪层，不用占位文字补齐。"""
    source = f"case:{case.case_id}"
    steps: list[dict] = []
    if case.pathogenesis:
        steps.append({"step": "症状→病机", "output": case.pathogenesis,
                      "rationale": _rationale(triples, "提示"), "source": source})
    if case.syndrome:
        steps.append({"step": "病机→证型" if case.pathogenesis else "症状→证型",
                      "output": case.syndrome,
                      "rationale": _rationale(triples, "属于"), "source": source})
    if case.treatment_principle and case.syndrome:
        steps.append({"step": "证型→治法", "output": case.treatment_principle,
                      "rationale": _rationale(triples, "治以"), "source": source})
    if case.formula and case.treatment_principle:
        steps.append({"step": "治法→方剂", "output": case.formula,
                      "rationale": _rationale(triples, "用方"), "source": source})
    if case.herbs:
        steps.append({
            "step": "方剂→药材" if case.formula else "治法→药材",
            "output": [
                {"name": h,
                 # 「含」（方→药）优先，没有就退到「用药」（症→药）；role/性味归经
                 # 要等阶段二药理层，这里不猜
                 "rationale": _rationale(triples, "含", o=h) or _rationale(triples, "用药", o=h)}
                for h in case.herbs
            ],
            "source": source,
        })
    if len(steps) < 2:
        return None
    symptoms = "；".join(case.symptoms) if case.symptoms else ""
    tongue_pulse = "，".join(x for x in (case.tongue, case.pulse) if x)
    return {
        "input": "，".join(x for x in (symptoms, tongue_pulse) if x),
        "chain": steps,
        "meta": {
            "physician_id": case.physician, "case_id": case.case_id,
            "case_group_id": case.case_group_id, "copyright_status": case.copyright_status,
        },
    }


def split_by_case_group(cases: list[CaseRecord], heldout_ratio: float = DEFAULT_HELDOUT_RATIO) -> dict[str, str]:
    """case_group_id → "train" | "heldout"。同一病人的所有诊次同侧；用
    sha1(case_group_id) 定侧，不用随机——同一份 cases.json 任何时候切出来都
    一样，训练和评测拿到的是同一个 heldout。"""
    if not 0.0 <= heldout_ratio < 1.0:
        raise ValueError(f"heldout_ratio 要在 [0, 1) 内，收到 {heldout_ratio}")
    out: dict[str, str] = {}
    for c in cases:
        if c.case_group_id in out:
            continue
        bucket = int(hashlib.sha1(c.case_group_id.encode("utf-8")).hexdigest(), 16) % 1000
        out[c.case_group_id] = "heldout" if bucket < heldout_ratio * 1000 else "train"
    return out


def export_chain(cases: list[CaseRecord], triples_by_case: dict[str, list[dict]],
                 heldout_ratio: float = DEFAULT_HELDOUT_RATIO) -> tuple[list[dict], dict]:
    """返回 (样本列表, 统计)。统计里 rationale 覆盖率必须带对照报——
    "有多少步是带原文依据的"跟"有多少步是 None"并列，只报前者会显得都有依据。"""
    split = split_by_case_group(cases, heldout_ratio)
    samples: list[dict] = []
    n_steps = n_with_rationale = 0
    for case in cases:
        sample = to_chain_sample(case, triples_by_case.get(case.case_id, []))
        if sample is None:
            continue
        sample["meta"]["split"] = split[case.case_group_id]
        samples.append(sample)
        for step in sample["chain"]:
            if isinstance(step["output"], list):
                for herb in step["output"]:
                    n_steps += 1
                    n_with_rationale += herb["rationale"] is not None
            else:
                n_steps += 1
                n_with_rationale += step["rationale"] is not None
    stats = {
        "samples": len(samples),
        "cases_not_chainable": len(cases) - len(samples),
        "split": dict(Counter(s["meta"]["split"] for s in samples)),
        "steps": n_steps, "steps_with_rationale": n_with_rationale,
        "steps_without_rationale": n_steps - n_with_rationale,
    }
    return samples, stats


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="从 cases.json 派生 SFT 训练样本")
    ap.add_argument("--format", choices=("alpaca", "chain"), default="alpaca",
                    help="alpaca=原来的四任务样本；chain=总纲 5.1 的六层链路样本（rationale 来自医案三元组）")
    ap.add_argument("--cases-path", type=Path, default=CASES_PATH)
    ap.add_argument("--triples-path", type=Path, default=TRIPLES_PATH, help="chain 格式用的医案三元组")
    ap.add_argument("--out", type=Path, default=None, help="默认 sft.jsonl（alpaca）/ sft_chain.jsonl（chain）")
    ap.add_argument("--heldout-ratio", type=float, default=DEFAULT_HELDOUT_RATIO)
    ap.add_argument("--include-incompatible", action="store_true",
                    help="把含十八反十九畏配伍的医案也导出（默认排除，见 "
                         "filter_incompatible_pairs 的文档字符串）。这些样本会教模型"
                         "开反药，而输出侧安全检查又会拦住它，自相矛盾。")
    args = ap.parse_args(argv)

    cases = load_cases(args.cases_path)
    cases = filter_public_domain(cases)
    cases = filter_incompatible_pairs(cases, include=args.include_incompatible)

    if args.format == "chain":
        out_path = args.out or CHAIN_OUT_PATH
        triples_by_case = load_triples_by_case(args.triples_path)
        if not triples_by_case:
            print(f"[export_sft] 注意：{args.triples_path} 不存在或为空，所有步骤的 rationale 都会是 None——"
                  "先跑 offline/extract_case_triples.py", file=sys.stderr)
        samples, stats = export_chain(cases, triples_by_case, args.heldout_ratio)
        with out_path.open("w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"链路样本数：{stats['samples']}（另 {stats['cases_not_chainable']} 条医案不足两步、不成链）")
        print(f"train/heldout（按 case_group_id 切）：{stats['split']}")
        print(f"步骤 {stats['steps']}，带原文依据 {stats['steps_with_rationale']}，"
              f"无依据（rationale=None）{stats['steps_without_rationale']}")
        print(f"已写出 {out_path}")
        return

    out_path = args.out or OUT_PATH
    samples: list[dict] = []
    for case in cases:
        samples.extend(to_samples(case))

    with out_path.open("w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    task_dist = Counter(s["meta"]["task"] for s in samples)
    physician_dist = Counter(s["meta"]["physician_id"] for s in samples)

    print(f"总样本数：{len(samples)}")
    print(f"按 task 分布：{dict(task_dist)}")
    print(f"按 physician 分布：{dict(physician_dist)}")
    print(f"已写出 {out_path}")


if __name__ == "__main__":
    main()
