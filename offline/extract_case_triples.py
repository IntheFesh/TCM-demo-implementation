"""X3：从 cases.json 里每一诊的原文抽取三元组，写入 data/case_triples.jsonl，
一行一条 `{case_id, physician, s, p, o, source_span}`——这个格式不是这里定的，
是 core/tools.py 的 `query_case_graph()` 已经在读的既有契约（那份代码和它的
测试 tests/test_tools.py 是先写好的），这里只是把契约要求的文件真的产出来。

**这是一次真实的 LLM 抽取，不是确定性转换。** CaseRecord 的 syndrome/
treatment_principle/formula/herbs 这几个字段虽然 S0 阶段已经抽过一次，但那是
"这一诊的证候是什么"这种粗粒度归纳，不是"哪句原文具体证明了哪条关系"这种
细粒度、带精确出处的标注——后者是 query_case_graph 的 source_span 承诺要给
的东西（"这条结论出自医案哪一句"），S0 的字段做不到这个粒度，必须重新过一遍
原文。prompts/v1/s5_extract_triples.yaml 就是这次抽取用的 prompt。

**source_span 的防幻觉核验在这里做，不在 pydantic schema 里。** schema
（core.schemas.CaseTripleItem）只能校验"这个字段非空"，校验不了"这段文字
是不是真的在原文里"——那需要拿模型返回的 source_span 去原文里做逐字查找。
查不到就整条三元组丢弃，不写进输出文件；丢了多少条要在统计里如实报出来，
不能静默吞掉（吞了的话，"三元组文件的 source_span 都可核验"这句话就是假的）。

**主语/宾语的指代词核验也在这里做，同样不在 schema 里。** "此症""患者"
这类词是合法的非空字符串，pydantic 拦不住；核验逻辑跟 source_span 一样，
抽取后立刻查，命中黑名单就丢弃、计入统计，不能静默吞。谓词受控词表（六选一）
不一样——那条 schema 层能拦，见 core.schemas.CaseTriplePredicate，模型给了
表外谓词会在 core.llm 的重试机制里被回灌校验错误，不需要这里再查一遍。

**941 次调用跑十几分钟，批处理本身要扛得住单条失败。** 两类失败分开处理：
截断（LLMTruncatedError）重试没有意义，跳过、计数；网络抖动/超时/限流/
非截断的格式错误这些普通 LLMError，core.llm.generate() 已经自己重试 3 次
仍失败——大概率是暂时性的，值得事后单独重跑，所以记下 case_id、支持
`--only-ids` 只重跑这几条（形状照抄 eval/sdt/run.py 已有的同名参数，
不另发明一套）。结果也不再等全部跑完才写：每 PROGRESS_EVERY 条落盘一次，
中途崩了已经跑完的不会全部陪葬。

用法：
    python -m offline.extract_case_triples
    python -m offline.extract_case_triples --dry-run --limit 5
    python -m offline.extract_case_triples --only-ids ye_tianshi-1-p0-0,wu_jutong-2-p0-1
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from core.llm import LLMError, LLMTruncatedError, get_llm, load_prompt, render
from core.schemas import CaseRecord, CaseTripleExtraction, CaseTripleRecord

# 每处理完这么多条医案报一次进度、落盘一次。941 条要跑十几分钟，中途崩了不
# 知道跑到哪、也不该把已经跑完的结果一起丢掉——这不是猜的，是真实踩过的坑
# （这次修的截断问题就是从"跑到第 1 条就没输出"这个状态排查出来的）。50 条
# 一报是"够密集看出卡在哪、又不会把输出刷屏"之间的折中，不是精确调过的数字。
PROGRESS_EVERY = 50

OnProgress = Callable[[int, int, dict], None]
# on_case_done 在每条**真正尝试过抽取**的医案（有 raw_excerpt、调用过一次
# S5）处理完后立刻调用——不管成功、截断还是失败都调用。没有 raw_excerpt、
# 根本没尝试的医案不触发：没有内容可落盘，触发了也没有信息量。
OnCaseDone = Callable[[CaseRecord, list[CaseTripleRecord], "str | None"], None]

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CASES_PATH = ROOT / "cases.json"
DEFAULT_OUT_PATH = ROOT / "data" / "case_triples.jsonl"

# 默认 8192 在真实数据上撞过：一张九味药的方子，九条「含」关系的 source_span
# 如果模型把整张方子重抄一遍（约 200 字 × 9 ≈ 1800 字纯重复），很容易顶到默认
# 上限被截断。prompts/v1/s5_extract_triples.yaml 已经加了"含关系的 source_span
# 只抄那一味药"的指令（治本，从源头减少重复输出）；这里把上限翻倍是兜底
# （治标）——两条都要：只靠 prompt 指令不能保证模型 100% 遵守，只提高上限
# 不能省掉本来就不该有的重复内容浪费的 token。
S5_MAX_TOKENS = 16384

# R2 --limit 5 试水实测出现过的指代词（"此症""此病"），另外几个是同一类问题
# 会出现的变体，一并列进来防患于未然。exact match（strip 后整串相等），不用
# 子串匹配——"患者"是子串的话会连"患者自述"这种合法症状描述里的词一起拦掉，
# 这里只拦"s 或 o 整个就是这个指代词"的情况。
_REFERENTIAL_PLACEHOLDERS = frozenset({
    "此症", "此病", "此证", "本例", "该患者", "该病人", "该症",
    "患者", "病家", "病者", "病人", "其人",
})


def _source_text(case: CaseRecord) -> str | None:
    """喂给模型、也是 source_span 核验基准的原文。**只用诊次原文片段
    （raw_excerpt），不退回整段 raw。** raw 是同一病人所有诊次共享的整段
    粗段——R2 --limit 5 试水实测过退回 raw 的后果：wu_jutong-0000 三诊共享
    同一段 raw，三次都喂整段文本，模型三次读到同样的内容却抽出三套不一致的
    三元组，p0-1 那次甚至混进了后面诊次才出现的方剂（麻黄附子甘草汤/桂枝汤/
    五苓散）——这不是模型出错，是喂给它的原文本来就包含了它不该看到的内容。
    raw_excerpt 覆盖率 97%（M1 那轮做的诊次级片段），缺失的 3% 直接跳过、
    不编、不退化到 raw，调用方据此计入 stats 里的跳过数。"""
    return case.raw_excerpt or None


def _is_referential(text: str) -> bool:
    return text.strip() in _REFERENTIAL_PLACEHOLDERS


def extract_case(
    case: CaseRecord,
) -> tuple[list[CaseTripleRecord], dict[str, int], str | None]:
    """对一条医案调一次 S5，返回 (核验通过的三元组记录, 按丢弃原因分类的计数,
    失败原因)。

    两种丢弃原因（source_span、referential）分开计数而不是合并成一个数：
    source_span 找不到说明模型编了出处，s/o 是指代词占位符说明模型没有把
    关系落到具体实体上——两类问题的修法不一样，分开报才看得出改 prompt
    有没有真的改到点子上。

    失败原因三态：
      - None：正常完成——可能一条三元组都没抽出，那不算失败，是这一诊
        原文确实信息不够。
      - "truncated"：输出在 max_tokens 上限处被砍断，core.llm 已经判断过
        "重试没有意义"（同样的输入会在同一处再次被截断），这里直接跳过、
        不重试。
      - 其余字符串（真实异常类型名，如 "TimeoutError"／"RateLimitError"）：
        网络抖动/超时/限流/非截断的格式错误，core.llm.generate() 自己已经
        重试 3 次仍失败——这一类跟"内容有问题"不是一回事，是这次调用本身
        没成，大概率是暂时性的，值得事后单独重跑。类型名取
        `type(err.__cause__).__name__`（core.llm 的 generate() 在最终
        raise 时做了 `from last_error`，保留了真实的底层异常），不解析
        错误信息里那句拼好的文本——文本格式以后一变，解析就错。
    """
    text = _source_text(case)
    if text is None:
        return [], {"span_not_found": 0, "referential": 0}, None

    prompt = load_prompt("s5_extract_triples")
    system = render(prompt["system"], raw_text=text)
    try:
        extraction = get_llm().generate(
            system=system, user="", schema=CaseTripleExtraction,
            max_tokens=S5_MAX_TOKENS,
        )
    except LLMTruncatedError:
        return [], {"span_not_found": 0, "referential": 0}, "truncated"
    except LLMError as e:
        # 这个 except 必须排在 LLMTruncatedError 后面——它是 LLMError 的子类，
        # 顺序反了会让截断也被这里接住，丢失"截断不值得重跑"这条区分。
        cause = e.__cause__
        reason = type(cause).__name__ if cause is not None else type(e).__name__
        return [], {"span_not_found": 0, "referential": 0}, reason

    records = []
    rejected = {"span_not_found": 0, "referential": 0}
    for item in extraction.triples:
        if item.source_span not in text:
            rejected["span_not_found"] += 1
            continue
        if _is_referential(item.s) or _is_referential(item.o):
            rejected["referential"] += 1
            continue
        records.append(CaseTripleRecord(
            case_id=case.case_id, physician=case.physician,
            s=item.s, p=item.p, o=item.o, source_span=item.source_span,
        ))
    return records, rejected, None


def extract_all(
    cases: list[CaseRecord],
    on_progress: OnProgress | None = None,
    on_case_done: OnCaseDone | None = None,
) -> tuple[list[CaseTripleRecord], dict]:
    """on_progress(已处理条数, 总条数, 当前 stats 快照) 每处理完
    PROGRESS_EVERY 条医案调一次，外加处理完最后一条时必调一次（不满
    PROGRESS_EVERY 的尾巴不会被吃掉）。这里不直接 print、不直接写文件——
    这个函数是"给什么输入、产出什么结果"的纯处理逻辑，输出去哪交给调用方
    决定，跟 core/chain.py 的 on_step 是同一个模式（那边是 SSE 分步事件，
    这边是批处理进度/落盘，机制一样：回调而不是硬编码某个具体的输出通道）。

    传给两个回调的容器都是那一刻的快照（浅拷贝 + 逐个拷贝里面的可变字段），
    不是后续还会被原地修改的同一个引用——不然调用方存下来的"第 50 条时的
    统计"会被第 51-100 条的处理悄悄改掉。"""
    all_records: list[CaseTripleRecord] = []
    total = len(cases)
    stats = {
        "cases": total, "cases_no_text": 0, "cases_no_triples": 0,
        "cases_truncated": 0, "cases_failed": 0,
        "failed_case_ids": [],
        "failure_reasons": {},
        "triples_extracted": 0,
        "triples_rejected_span_not_found": 0, "triples_rejected_referential": 0,
        "llm_calls": 0,
    }
    for i, case in enumerate(cases, start=1):
        text = _source_text(case)
        if text is None:
            stats["cases_no_text"] += 1
        else:
            records, rejected, failure = extract_case(case)
            stats["llm_calls"] += 1  # 失败也是真的调用了一次，要计进去
            if failure is not None:
                stats["failure_reasons"][failure] = stats["failure_reasons"].get(failure, 0) + 1
                if failure == "truncated":
                    stats["cases_truncated"] += 1
                else:
                    stats["cases_failed"] += 1
                    stats["failed_case_ids"].append(case.case_id)
            else:
                stats["triples_rejected_span_not_found"] += rejected["span_not_found"]
                stats["triples_rejected_referential"] += rejected["referential"]
                if not records:
                    stats["cases_no_triples"] += 1
                stats["triples_extracted"] += len(records)
                all_records.extend(records)
            if on_case_done is not None:
                on_case_done(case, records, failure)
        if on_progress is not None and (i % PROGRESS_EVERY == 0 or i == total):
            snapshot = dict(stats)
            snapshot["failed_case_ids"] = list(stats["failed_case_ids"])
            snapshot["failure_reasons"] = dict(stats["failure_reasons"])
            on_progress(i, total, snapshot)
    return all_records, stats


def _estimate_call_count(cases_path: Path, limit: int | None) -> int:
    """跟 _source_text 用同一条判据：只数有 raw_excerpt 的医案，不数只有 raw
    的——不然 dry-run 报的调用数会比实际多跑的次数大，用来算成本会算高。"""
    if not cases_path.exists():
        return 0
    raw = json.loads(cases_path.read_text(encoding="utf-8"))
    if limit is not None:
        raw = raw[:limit]
    return sum(1 for c in raw if c.get("raw_excerpt"))


def _load_existing_rows(out_path: Path) -> dict[str, list[dict]]:
    """按 case_id 分组读已有的输出文件，作为增量落盘/`--only-ids` 重跑的合并
    基准。文件不存在就是空字典，不是错误——第一次跑、或者 --out 指了个新
    路径都是正常情况。"""
    if not out_path.exists():
        return {}
    grouped: dict[str, list[dict]] = {}
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        grouped.setdefault(row["case_id"], []).append(row)
    return grouped


def _write_rows(out_path: Path, grouped: dict[str, list[dict]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for rows in grouped.values():
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="X3：从 cases.json 抽医案三元组（真实 LLM 调用）")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条医案，调试/控成本用")
    ap.add_argument("--dry-run", action="store_true", help="只打印预估调用数，不真的调模型")
    ap.add_argument(
        "--only-ids", default="",
        help="只跑这些医案 case_id（逗号分隔）。用于重跑上一轮失败（非截断）的"
             "那几条——失败列表会在跑完后打印。跟 eval/sdt/run.py 的同名参数是"
             "同一个形状。结果按 case_id 合并进 --out（见 _load_existing_rows/"
             "_write_rows）：这几条的新结果替换旧的，其余 case_id 的内容原样"
             "保留，不需要跑完再手动拼文件。",
    )
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.cases_path}。先跑 offline/extract_cases.py 生成 cases.json。"
        )

    raw = json.loads(args.cases_path.read_text(encoding="utf-8"))
    all_ids = {r.get("case_id") for r in raw}
    if args.only_ids:
        wanted = {x.strip() for x in args.only_ids.split(",") if x.strip()}
        missing = wanted - all_ids
        if missing:
            raise SystemExit(f"--only-ids 里这些医案 case_id 不在 {args.cases_path} 里：{sorted(missing)}")
        raw = [r for r in raw if r.get("case_id") in wanted]
    if args.limit is not None:
        raw = raw[: args.limit]

    if args.dry_run:
        n_calls = sum(1 for c in raw if c.get("raw_excerpt"))
        print(f"--dry-run：预估调用数 = {n_calls}（每条有原文的医案 1 次 S5 调用），不真的调模型")
        return

    cases = [CaseRecord.model_validate(r) for r in raw]

    # 合并基准：已有输出文件里，本次没有重新处理到的 case_id 原样保留；
    # 本次真正尝试过的 case_id（不管成功、截断还是失败）由 _on_case_done
    # 决定要不要替换——只有"正常完成"（failure is None）才替换，截断/失败
    # 不动旧数据，不能让一次失败的重试把之前成功的结果抹掉。
    by_case_id = _load_existing_rows(args.out)

    def _on_case_done(case: CaseRecord, records: list[CaseTripleRecord], failure: str | None) -> None:
        if failure is None:
            by_case_id[case.case_id] = [r.model_dump() for r in records]

    def _on_progress(done: int, total: int, snapshot: dict) -> None:
        print(f"进度 {done}/{total}：抽出 {snapshot['triples_extracted']} 条三元组，"
              f"跳过 {snapshot['cases_no_text']} 条无 excerpt，"
              f"{snapshot['cases_truncated']} 条疑似截断，"
              f"{snapshot['cases_failed']} 条调用失败")
        _write_rows(args.out, by_case_id)  # 每 PROGRESS_EVERY 条落盘一次

    records, stats = extract_all(cases, on_progress=_on_progress, on_case_done=_on_case_done)

    print(f"读入 {stats['cases']} 条医案（{stats['cases_no_text']} 条没有 raw_excerpt，已跳过，不退回整段 raw）")
    print(f"S5 调用 {stats['llm_calls']} 次，抽出 {stats['triples_extracted']} 条三元组通过核验，"
          f"{stats['triples_rejected_span_not_found']} 条因 source_span 在原文里找不到被丢弃，"
          f"{stats['triples_rejected_referential']} 条因主语/宾语是指代词占位符被丢弃")
    print(f"{stats['cases_no_triples']} 条医案调了模型但一条三元组都没抽出/全部核验未过")
    if stats["cases_truncated"]:
        print(f"警告：{stats['cases_truncated']} 条医案的输出疑似被截断（撞 max_tokens={S5_MAX_TOKENS}），"
              "已跳过、不写入任何三元组——不是重试三次以后才放弃，是探测到截断就直接跳过。"
              "重跑没有意义（同样的输入会在同一处再次被截断），要改的是 prompt。")
    if stats["cases_failed"]:
        ids = ",".join(stats["failed_case_ids"])
        print(f"警告：{stats['cases_failed']} 条医案调用失败（非截断：网络抖动/超时/限流/"
              f"格式错误，core.llm 已重试 3 次），失败原因分布：{stats['failure_reasons']}")
        print(f"这些医案值得重跑（core.llm 已经排除了「重试也没用」的截断那一类）："
              f"\n    python -m offline.extract_case_triples --only-ids {ids} "
              f"--cases-path {args.cases_path} --out {args.out}")

    _write_rows(args.out, by_case_id)  # 收尾再落一次盘——on_progress 的最后一次已经等价于这行，这里是明确写出来，不隐式依赖回调时机
    print(f"已写出 {args.out}（{sum(len(v) for v in by_case_id.values())} 条三元组，"
          f"含本次未重新处理、从旧文件原样保留的部分）")


if __name__ == "__main__":
    main()
