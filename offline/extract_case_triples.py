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

用法：
    python -m offline.extract_case_triples
    python -m offline.extract_case_triples --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Callable

from core.llm import LLMTruncatedError, get_llm, load_prompt, render
from core.schemas import CaseRecord, CaseTripleExtraction, CaseTripleRecord

# 每处理完这么多条医案报一次进度。941 条要跑十几分钟，中途崩了不知道跑到
# 哪——这不是猜的，是真实踩过的坑（这次修的截断问题就是从"跑到第 1 条就
# 没输出"这个状态排查出来的）。50 条一报是"够密集看出卡在哪、又不会把
# 输出刷屏"之间的折中，不是精确调过的数字。
PROGRESS_EVERY = 50

OnProgress = Callable[[int, int, dict], None]

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
) -> tuple[list[CaseTripleRecord], dict[str, int], bool]:
    """对一条医案调一次 S5，返回 (核验通过的三元组记录, 按丢弃原因分类的计数,
    是否因输出被截断而跳过这条医案)。

    两种丢弃原因分开计数而不是合并成一个数：source_span 找不到说明模型编了
    出处，s/o 是指代词占位符说明模型没有把关系落到具体实体上——两类问题的
    修法不一样（前者是抄写要更忠实，后者是要认出"这个词不是一个实体"），
    分开报才看得出改 prompt 有没有真的改到点子上。

    截断（LLMTruncatedError）是第三种失败模式，跟前两种不是一回事：前两种
    是模型正常返回、内容有问题；截断是模型的输出在 max_tokens 处被砍断，
    根本没有完整内容可核验。重试没有意义——同样的输入会在同一处再次被
    截断——所以这里直接捕获，返回空结果加截断标记，让调用方跳过这条医案、
    计入统计，而不是让 LLMError 一路往上抛把整批全量跑崩掉（全量跑第一条
    医案就崩是实测踩过的真实后果）。"""
    text = _source_text(case)
    if text is None:
        return [], {"span_not_found": 0, "referential": 0}, False

    prompt = load_prompt("s5_extract_triples")
    system = render(prompt["system"], raw_text=text)
    try:
        extraction = get_llm().generate(
            system=system, user="", schema=CaseTripleExtraction,
            max_tokens=S5_MAX_TOKENS,
        )
    except LLMTruncatedError:
        return [], {"span_not_found": 0, "referential": 0}, True

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
    return records, rejected, False


def extract_all(
    cases: list[CaseRecord], on_progress: OnProgress | None = None,
) -> tuple[list[CaseTripleRecord], dict]:
    """on_progress(已处理条数, 总条数, 当前 stats 快照) 每处理完
    PROGRESS_EVERY 条医案调一次，外加处理完最后一条时必调一次（不满
    PROGRESS_EVERY 的尾巴不会被吃掉）。这里不直接 print——这个函数是
    "给什么输入、产出什么结果"的纯处理逻辑，输出去哪交给调用方决定，
    跟 core/chain.py 的 on_step 是同一个模式（那边是 SSE 分步事件，这边是
    批处理进度，机制一样：回调而不是硬编码某个具体的输出channel）。"""
    all_records: list[CaseTripleRecord] = []
    total = len(cases)
    stats = {
        "cases": total, "cases_no_text": 0, "cases_no_triples": 0,
        "cases_truncated": 0,
        "triples_extracted": 0,
        "triples_rejected_span_not_found": 0, "triples_rejected_referential": 0,
        "llm_calls": 0,
    }
    for i, case in enumerate(cases, start=1):
        text = _source_text(case)
        if text is None:
            stats["cases_no_text"] += 1
        else:
            records, rejected, truncated = extract_case(case)
            stats["llm_calls"] += 1  # 截断也是真的调用了一次，要计进去
            if truncated:
                stats["cases_truncated"] += 1
            else:
                stats["triples_rejected_span_not_found"] += rejected["span_not_found"]
                stats["triples_rejected_referential"] += rejected["referential"]
                if not records:
                    stats["cases_no_triples"] += 1
                stats["triples_extracted"] += len(records)
                all_records.extend(records)
        if on_progress is not None and (i % PROGRESS_EVERY == 0 or i == total):
            on_progress(i, total, dict(stats))
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


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="X3：从 cases.json 抽医案三元组（真实 LLM 调用）")
    ap.add_argument("--cases-path", type=Path, default=DEFAULT_CASES_PATH)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 条医案，调试/控成本用")
    ap.add_argument("--dry-run", action="store_true", help="只打印预估调用数，不真的调模型")
    args = ap.parse_args(argv)

    if not args.cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {args.cases_path}。先跑 offline/extract_cases.py 生成 cases.json。"
        )

    if args.dry_run:
        n_calls = _estimate_call_count(args.cases_path, args.limit)
        print(f"--dry-run：预估调用数 = {n_calls}（每条有原文的医案 1 次 S5 调用），不真的调模型")
        return

    raw = json.loads(args.cases_path.read_text(encoding="utf-8"))
    if args.limit is not None:
        raw = raw[: args.limit]
    cases = [CaseRecord.model_validate(r) for r in raw]

    def _print_progress(done: int, total: int, snapshot: dict) -> None:
        print(f"进度 {done}/{total}：抽出 {snapshot['triples_extracted']} 条三元组，"
              f"跳过 {snapshot['cases_no_text']} 条无 excerpt，"
              f"{snapshot['cases_truncated']} 条疑似截断")

    records, stats = extract_all(cases, on_progress=_print_progress)

    print(f"读入 {stats['cases']} 条医案（{stats['cases_no_text']} 条没有 raw_excerpt，已跳过，不退回整段 raw）")
    print(f"S5 调用 {stats['llm_calls']} 次，抽出 {stats['triples_extracted']} 条三元组通过核验，"
          f"{stats['triples_rejected_span_not_found']} 条因 source_span 在原文里找不到被丢弃，"
          f"{stats['triples_rejected_referential']} 条因主语/宾语是指代词占位符被丢弃")
    print(f"{stats['cases_no_triples']} 条医案调了模型但一条三元组都没抽出/全部核验未过")
    if stats["cases_truncated"]:
        print(f"警告：{stats['cases_truncated']} 条医案的输出疑似被截断（撞 max_tokens={S5_MAX_TOKENS}），"
              "已跳过、不写入任何三元组——不是重试三次以后才放弃，是探测到截断就直接跳过。")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")
    print(f"已写出 {args.out}")


if __name__ == "__main__":
    main()
