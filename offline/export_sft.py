"""SFT 训练样本导出。两种格式：

- `alpaca`（默认）：四个任务的 instruction/input/output 样本，只从 cases.json 来。
- `chain`（总纲 5.1 / M15）：六层链路样本，**三个来源合并**，每步带原文依据和出处。

三个来源（chain 格式）：

| 来源 | 填哪几步 | 依据原文 | 出处标签 |
|---|---|---|---|
| 医案（cases.json + data/case_triples.jsonl） | 症状→病机 / 病机→证型 / 证型→治法 / 治法→方剂 / 方剂→药材 | 三元组的 source_span | `case:{case_id}` |
| TCMEval-SDT Train（--sdt-dir） | 症状→病机 / 病机→证型 | 官方金标准里专家撰写的辨证说明 | `sdt:{病案ID}` |
| 药理层（data/materia_medica.jsonl、data/formulary.jsonl） | 给 方剂→药材 / 治法→方剂 补依据 | 本草/教材的 source_span | `materia_medica:{书名}`、`formulary:{书名}` |

外加 data/standard/syndromes.jsonl（教材证候表）填目标链路的前三步
症状→证素 / 证素→病名 / 病名→证型——这三步上面三个来源一个都填不出来
（CaseRecord 没有证素字段也没有病名字段，SDT 的两个任务是病机和证型），
不接这一份，"六步链路"就只能是五步。见 textbook_prefix_steps。

**每步的 rationale 只能是原文，拿不到就 None，不编。**

用法：
    python -m offline.export_sft                       # alpaca
    python -m offline.export_sft --format chain \
        --sdt-dir $SDT --out sft_chain.jsonl           # chain，三源合并
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from core.herbs import normalize_herb
from core.data_paths import pharmacology_read_path_or_canonical
from core.safety_output import INCOMPATIBLE_TRAINING_NOTE
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

    include=True 把它们带上。**R18-G 起 CLI 默认就是 include=True**（要排除传
    --exclude-incompatible），因为按原来的默认李可 57 例里的 21 例进不来、
    R18-B 白写；带上的代价由链末那一步「配伍提示」补偿（见
    core/safety_output.INCOMPATIBLE_TRAINING_NOTE 和 incompatible_note）。
    这个函数本身没变：它只回答"带不带"，默认参数仍是不带——一个纯函数的默认值
    跟 CLI 的默认行为是两件事，把函数默认也翻过来会让直接调它的测试静默改变含义。
    带上时照样把条数和药对打出来并警告：它们是名家在特定病情下的用法，
    不是常规配伍。"""
    if include:
        tagged = [c.case_id for c in cases if c.has_incompatible_pair]
        if tagged:
            print(f"[export_sft] 把 {len(tagged)} 条含十八反十九畏配伍的医案也导出了"
                  f"（R18-G 起这是默认；--exclude-incompatible 可以排掉）。"
                  f"这些样本的链末会附一句固定的配伍提示——没有那一句，模型学到的是"
                  f"「这种配伍可以开」，而输出侧 check_incompatible 又会拦住它，"
                  f"训练出来的模型在自己的安全层面前跑不通："
                  f"{tagged[:10]}{'…' if len(tagged) > 10 else ''}", file=sys.stderr)
        return list(cases)
    kept = [c for c in cases if not c.has_incompatible_pair]
    excluded = [c.case_id for c in cases if c.has_incompatible_pair]
    if excluded:
        print(f"[export_sft] 排除 {len(excluded)} 条含十八反十九畏配伍的医案（不教模型开反药）："
              f"{excluded[:10]}{'…' if len(excluded) > 10 else ''}", file=sys.stderr)
    return kept


def filter_out_of_scope(cases: list[CaseRecord], include: bool = False) -> list[CaseRecord]:
    """R8-2 选项 ②：不在脾胃门定位内的医案（`CaseRecord.out_of_scope`，来源是
    data/local_corpora/MANIFEST.json 的人工声明）默认不进训练集——demo 的证候表、
    检索语料、评测主诉全在脾胃门，肿瘤科样本混进去训出来的东西跟评测对不上，
    而"要不要扩定位"是产品决定，不该由导出脚本顺手做掉。
    跟 filter_incompatible_pairs 同一个形状：只按标记过滤，条数打到 stderr，
    `--include-out-of-scope` 显式打开时照样把条数打出来并警告。"""
    tagged = [c.case_id for c in cases if c.out_of_scope]
    if include:
        if tagged:
            print(f"[export_sft] **--include-out-of-scope：把 {len(tagged)} 条定位外（out_of_scope）"
                  f"的医案也导出了**（默认是排除的）。这些样本不在脾胃门评测集覆盖的范围里，"
                  f"训练效果对不上现有评测——只有在明确要扩定位时才用这个开关："
                  f"{tagged[:10]}{'…' if len(tagged) > 10 else ''}", file=sys.stderr)
        return list(cases)
    if tagged:
        print(f"[export_sft] 排除 {len(tagged)} 条定位外（out_of_scope）的医案："
              f"{tagged[:10]}{'…' if len(tagged) > 10 else ''}", file=sys.stderr)
    return [c for c in cases if not c.out_of_scope]


def filter_by_scope(cases: list[CaseRecord],
                    exclude_scopes: tuple[str, ...] = ()) -> list[CaseRecord]:
    """R18-G：按 `CaseRecord.scope`（这一例的门类）排除。**默认一个都不排。**

    跟 `filter_out_of_scope` 的区别就是 `scope` 和 `out_of_scope` 两个字段的区别
    （见 core/schemas.py 那两段注释）：这里按**一例**的门类排，那里按**整本书**
    的人工声明排。李可 57 例里有 2 例不是肿瘤，整本书一刀切会把那 2 例一起切掉。

    `scope is None`（没判过）**不算命中**：叶天士/吴鞠通那批抽取脚本不产出这个
    字段，把 None 当成"未知所以排掉"会把原本的两位医家整个清空。
    """
    if not exclude_scopes:
        return list(cases)
    excluded = [c.case_id for c in cases if c.scope in exclude_scopes]
    if excluded:
        print(f"[export_sft] --exclude-scope {','.join(exclude_scopes)}：排除 "
              f"{len(excluded)} 条该门类的医案：{excluded[:10]}"
              f"{'…' if len(excluded) > 10 else ''}", file=sys.stderr)
    return [c for c in cases if c.scope not in exclude_scopes]


def incompatible_note(case: CaseRecord) -> str | None:
    """这一例要不要附配伍提示。**判断只有这一处**，两种导出格式都调它。

    R18-G 之前含反药配伍的医案是直接排除的（R5 那轮的决定）。改成"带上但附
    这一句"，理由见 core/safety_output.INCOMPATIBLE_TRAINING_NOTE：排除等于把
    李可 57 例里的 21 例——他最有辨识度的那部分——整个删掉，而 R18 的目的正是
    把他接进训练。那一句话的**文本**也只有一处定义，在 safety_output 里。
    """
    return INCOMPATIBLE_TRAINING_NOTE if case.has_incompatible_pair else None


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


# ---------- 总纲 5.1（M15）：六层链路格式，三源合并 ----------
#
# 每条样本是 {input, chain:[{step, output, rationale, source, rationale_source}], meta}。
#
# **每步的 rationale 必须从原文来**，拿不到就是 None，不编——这是"推理"和"匹配"
# 的区别所在。原文有三个来源，每一处都有可追溯的出处标签：
#
#   case:{case_id}           医案三元组的 source_span（offline/extract_case_triples.py）
#   sdt:{病案ID}             TCMEval-SDT Train 的专家撰写辨证说明（官方金标准原文）
#   standard:{证候编码}       data/standard/syndromes.jsonl 的证机概要原文（教材）
#   materia_medica:{书名}     data/materia_medica.jsonl 的 source_span（阶段二药理层）
#   formulary:{书名}          data/formulary.jsonl 的 source_span（同上）
#
# `source` 是**输出**的出处，`rationale_source` 是**依据文本**的出处。两者会不同：
# 方名是医案里的，但"为什么这个治法用这张方"的原文依据来自《方剂学》。合成一个
# 字段就必然有一半在撒谎——要么把教材原文记成医案出处，要么反过来。
# rationale 是 None 时 rationale_source 也是 None（没有依据就没有依据的出处）。

# 目标链路（总纲阶段五 M15 指定的六步）。样本不必六步全有：每一步只在它的来源
# 能从原文填出来时才出现，缺哪步就没有哪步，**不用占位文字补齐**——统计里按这个
# 元组逐步报覆盖，缺口是看得见的数字，不是被占位符盖住的空白。
TARGET_CHAIN: tuple[str, ...] = (
    "症状→证素", "证素→病名", "病名→证型",
    "证型→治法", "治法→方剂", "方剂→药材",
)
# 实际会出现的全部步骤名 = 目标六步 + 三个数据形态决定的变体。变体不是"额外发明
# 的步骤"，是数据本身的形状：医案记的是病机（一段自由文本）而不是证素词表，
# SDT 的 Task2/Task3 也是病机→证型；没有方名时药材直接挂在治法下。
# **步骤名只有这一处定义**，_step() 会拒绝不在表里的名字——拼错一个箭头方向，
# 下游按步骤名分组统计就会静默多出一类，报出来的覆盖率是错的。
# 「配伍提示」不进 TARGET_CHAIN（它不是辨证链的一环，是附在链末的安全说明），
# 但必须进 CHAIN_STEPS——_step 会拒绝不在这个元组里的步骤名。
CHAIN_STEPS: tuple[str, ...] = TARGET_CHAIN + (
    "症状→病机", "病机→证型", "症状→证型", "治法→药材", "配伍提示")
# 逐项带依据的步骤：output 是 [{name, rationale, rationale_source}]，一味药一个依据。
# 其它步骤的 output 也可能是列表（症状→证素 就是一串证素词），但那种列表共用步骤
# 这一层的依据。**判据只能是步骤名，不能是"output 是不是 list"**——加进证素那一步的
# 时候，按类型判断的统计代码会把每个证素词当成一味药去取它的 rationale 然后炸掉
# （AttributeError: 'str' object has no attribute 'get'，写这一轮时真的撞到了，
# 是新加的测试 test_export_chain_reports_rationale_by_source_and_target_step_coverage
# 先炸出来的）。
ITEMIZED_STEPS: tuple[str, ...] = ("方剂→药材", "治法→药材")

# 药理层里"为什么这味药治这个证"优先取功效，取不到退到性味归经；方剂层优先取
# 功用（这张方干什么），退到主治（治什么证）。顺序写在这里不散落在调用处。
MATERIA_MEDICA_PREDICATES = ("功效", "性味")
FORMULARY_PREDICATES = ("功用", "主治")

# 路径走 core.data_paths 那一处：R18-F 之后这两个文件在 data/standard/ 下
# （进版本控制），旧位置仍可读。三个调用方各写一份常量就是三处实现。
MATERIA_MEDICA_PATH = pharmacology_read_path_or_canonical("materia_medica")
FORMULARY_PATH = pharmacology_read_path_or_canonical("formulary")
# 第五源：《脾胃论》立论层（R18-D，确定性抽取、进版本控制）。
RATIONALE_PWL_PATH = Path(__file__).resolve().parent.parent / "data" / "standard" / "rationale_pwl.jsonl"
PWL_BOOK = "脾胃论"
# 短于这个长度的论断结论（「虚」「病」）拿去做子串匹配几乎必然命中，
# 那不是"找到了依据"，是噪声。
_PWL_MIN_MATCH_CHARS = 2
# 哪几个谓词能给哪一步当依据。混用会让「加药」那 81 条去给证型步当依据。
PWL_PREDICATES_FOR_PATHOGENESIS = ("病机",)   # 28 条，o 是「脾病」「胃实而肠虚」这类结论
PWL_PREDICATES_FOR_FORMULA = ("用方",)         # 32 条，o 是方名
# **「治法」那 21 条不接进治法步**，这不是漏了：数过了，它们的 o **全是单字**
# （升/降/补/泻/汗/下……，`grep` 出来 21/21 条长度都是 1）。拿单字去子串匹配
# 「健脾和胃」这种治法描述，「和」会命中、「补中益气」里的「补」会命中——那不是
# 找到了依据，是任意命中。所以这一层只给病机步和方剂步补依据；
# 治法步的依据继续只从医案三元组的「治以」来。
# 这条不是"以后再做"：单字论断在这个匹配方式下根本不可用，要用得换一种匹配
# （比如把治法词表接进证候归一），那是另一件事。
# SDT 只导出 Train。Validation/Test 是评测集，导进训练数据就是泄漏——而且
# eval/sdt/data.py 第 2 条实测记着：那两个 split 的 JSON 里答案字段全是空的，
# 本来也拼不出链路。所以这里不给"换 split"的开关，写死 Train。
SDT_TRAIN_SPLIT = "Train"

_SENTENCE_SPLIT = re.compile(r"[。！？；;!?\n]")


def _step(step: str, output, rationale: str | None, source: str,
          rationale_source: str | None) -> dict:
    if step not in CHAIN_STEPS:
        raise ValueError(f"步骤名 {step!r} 不在 CHAIN_STEPS 里；新增步骤要先加进那个元组")
    if rationale is None and rationale_source is not None:
        raise ValueError(f"{step}：没有 rationale 却带了 rationale_source={rationale_source!r}")
    return {"step": step, "output": output, "rationale": rationale,
            "source": source, "rationale_source": rationale_source}


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


def _load_reference_rationales(path: Path, predicates: tuple[str, ...],
                              tag: str, normalize) -> dict[str, tuple[str, str]]:
    """药理层/方剂层三元组 → {归一后的主语: (source_span, "tag:书名")}。

    谓词优先级按 predicates 的顺序，**同一主语先到的高优先级谓词不被后面的覆盖**
    ——否则同一味药的记录在文件里的先后顺序会决定 rationale 是功效还是性味，
    换一次抽取顺序训练数据就变了。只收带 source_span 的行：没有出处的三元组
    在这里跟编的没区别。文件不存在返回空字典（阶段二还没跑），统计里报出来。
    """
    if not path.exists():
        return {}
    best: dict[str, tuple[int, str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        p = row.get("p")
        if p not in predicates:
            continue
        span = (row.get("source_span") or "").strip()
        key = normalize(row.get("s") or "")
        if not span or not key:
            continue
        rank = predicates.index(p)
        prev = best.get(key)
        if prev is None or rank < prev[0]:
            best[key] = (rank, span, f"{tag}:{row.get('book') or '未标书名'}")
    return {k: (span, src) for k, (_, span, src) in best.items()}


def load_materia_medica_rationales(path: Path = MATERIA_MEDICA_PATH) -> dict[str, tuple[str, str]]:
    """药名归一只有 core.herbs.normalize_herb 一处实现（query_materia_medica 用的
    也是它），这里两边都过它，不另写一套前缀剥离。"""
    return _load_reference_rationales(path, MATERIA_MEDICA_PREDICATES, "materia_medica", normalize_herb)


def load_rationale_pwl(path: Path | None = None) -> list[tuple[str, str, str]]:
    """R18-G 的**第五源**：《脾胃论》立论层（R18-D 抽的 278 条）。

    返回 [(谓词, o, source_span)]，按 o 的长度**降序**——查的时候拿 o 当子串去
    比对医案的病机/治法文本，先比长的才不会让「虚」抢在「脾胃虚寒」之前命中。

    跟另外四源（医案三元组 / 本草 / 方剂 / SDT）的区别：那四源都按主语建索引、
    精确命中；这一源是**古籍论断**，医案里的病机写法跟《脾胃论》的原句不会逐字
    相同，只能拿论断的结论去子串匹配。命中率因此低得多，所以 export_chain 的统计
    里单独报它命中了几步——低命中率要看得见，不是悄悄接受。
    """
    p = path or RATIONALE_PWL_PATH
    if not p.exists():
        return []
    rows: list[tuple[str, str, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        o, span, pred = (row.get("o") or "").strip(), (row.get("source_span") or "").strip(), row.get("p")
        if len(o) >= _PWL_MIN_MATCH_CHARS and span and pred:
            rows.append((pred, o, span))
    rows.sort(key=lambda r: -len(r[1]))
    return rows


def pwl_rationale(index: list[tuple[str, str, str]], predicates: tuple[str, ...],
                  text: str | None) -> tuple[str, str] | None:
    """《脾胃论》立论层查一步的依据。**匹配逻辑只有这一处**，三个调用点都走它
    （CLAUDE.md 第 31 条——这个项目在"同一个词两处给出相反答案"上撞过三次）。"""
    if not text:
        return None
    for pred, o, span in index:
        if pred in predicates and o in text:
            return span, f"rationale_pwl:{PWL_BOOK}"
    return None


def load_formulary_rationales(path: Path = FORMULARY_PATH) -> dict[str, tuple[str, str]]:
    """方名**没有归一器**（药名有 normalize_herb，证型有 syndrome_norm，方名这一层
    本项目还没有）。所以这里只做 strip，「加味逍遥散」匹配不上《方剂学》的
    「逍遥散」——这不是悄悄接受的损失，export_chain 的统计里报未命中方名数，
    数大了就说明该建这个归一器了。不在这里现写一套字面裁剪：那正是
    CLAUDE.md「同一概念的匹配逻辑只能有一处实现」撞过三次的墙。"""
    return _load_reference_rationales(path, FORMULARY_PREDICATES, "formulary", lambda s: s.strip())


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


def _first_sentence_containing(text: str, needles: list[str]) -> str | None:
    """summary 原文里第一个包含任一 needle 的句子。**只取一句**，不把分散的几句
    拼起来：拼接出来的字符串在原文里并不存在，就不再是"原文依据"了（防幻觉的
    判据是"这段字能在原文里找到"，拼接会让这个判据失效）。"""
    for raw in _SENTENCE_SPLIT.split(text or ""):
        s = raw.strip()
        if s and any(n and n in s for n in needles):
            return s
    return None


def textbook_prefix_steps(syndrome: str | None,
                          lookup=None) -> tuple[list[dict], str | None]:
    """证型名 → 目标链路的前三步（症状→证素 / 证素→病名 / 病名→证型），以及
    "为什么没有"的判据标签（None = 有）。依据是 data/standard/syndromes.jsonl
    的证机概要原文。

    匹配复用 core.tools.lookup_standard（唯一实现，不另写一套字面比对），但
    **只接受精确命中**：lookup_standard 最后一档是"按名称部分匹配到唯一一条"，
    「湿热」能匹配上「湿热布散三焦证」。那一档用在模型自我纠正的场景里是对的
    （模型看得到 note，能判断要不要改查询词），用在离线批量造训练数据上是错的
    ——一条错配会把另一个病的证素和病名写进这条样本，而 rationale 是教材原文、
    看起来完全正常，没有任何一道闸门拦得住。判据不看 note 的文字（那是给人读
    的，改一个字判据就失效），看返回条目的名字/编码是不是就等于查询词。

    证素这一步的 rationale 还要再验一次：证候表里的 location/nature 是
    build_syndrome_textbook.py 从证机概要里子串匹配出来的，所以那些词**应该**
    在 definition 里；不在的条目（手工条目、国标条目可能是另一套来源）说明这段
    原文撑不起这一步，rationale 填 None 而不是硬塞一段不含那些词的文本。
    """
    if not syndrome or not syndrome.strip():
        return [], "no_syndrome"
    if lookup is None:
        from core.tools import lookup_standard as lookup
    q = syndrome.strip()
    res = lookup(q)
    if not res.get("found"):
        return [], "syndrome_not_in_standard_table"
    d = res.get("definition") or {}
    code, name = (d.get("code") or "").strip(), (d.get("name") or "").strip()
    if q not in {name, code, f"{code} {name}", f"{code}{name}", f"{name}（{code}）"}:
        return [], "syndrome_matched_only_partially"
    elements = [x for x in (list(d.get("location") or []) + list(d.get("nature") or [])) if x]
    disease = (d.get("disease") or "").strip()
    definition = (d.get("definition") or "").strip() or None
    src = f"standard:{code or name}"
    steps: list[dict] = []
    if elements:
        grounded = definition if definition and all(e in definition for e in elements) else None
        steps.append(_step("症状→证素", elements, grounded, src, src if grounded else None))
    if disease:
        if elements:
            steps.append(_step("证素→病名", disease, definition, src, src if definition else None))
        if name:
            steps.append(_step("病名→证型", name, definition, src, src if definition else None))
    if not steps:
        return [], "standard_entry_has_no_usable_fields"
    return steps, None


def _herb_item(name: str, triples: list[dict], case_source: str,
               materia_medica: dict[str, tuple[str, str]]) -> dict:
    """一味药的依据：先医案（「含」= 这张方含这味药，退到「用药」= 这个症加这味
    药），医案里没有才退到药理层的功效/性味原文。两者出处不同，所以
    rationale_source 跟着走——药理层的原文不能记成医案的 source_span。"""
    span = _rationale(triples, "含", o=name) or _rationale(triples, "用药", o=name)
    if span:
        return {"name": name, "rationale": span, "rationale_source": case_source}
    hit = materia_medica.get(normalize_herb(name))
    if hit:
        return {"name": name, "rationale": hit[0], "rationale_source": hit[1]}
    return {"name": name, "rationale": None, "rationale_source": None}


def to_chain_sample(case: CaseRecord, triples: list[dict],
                    materia_medica: dict[str, tuple[str, str]] | None = None,
                    formulary: dict[str, tuple[str, str]] | None = None,
                    lookup=None,
                    pwl: list[tuple[str, str, str]] | None = None) -> dict | None:
    """一条医案 → 一条链路样本；能派生的步骤不足两步（不成链）返回 None。
    步骤只按字段存在与否生成，缺哪层就没有哪层，不用占位文字补齐。

    三源在这里汇到一条链上：证型能在教材证候表里精确命中时，前面接上教材的
    证素/病名三步（source=standard:*）；药材和方剂的依据在医案三元组里找不到时，
    退到药理层（source=materia_medica:*/formulary:*）。每一步各自带出处。
    """
    materia_medica = materia_medica or {}
    formulary = formulary or {}
    pwl = pwl or []
    source = f"case:{case.case_id}"
    prefix, prefix_miss = textbook_prefix_steps(case.syndrome, lookup=lookup)
    steps: list[dict] = list(prefix)
    if case.pathogenesis:
        span = _rationale(triples, "提示")
        rat_src = source if span else None
        if not span:
            # 第五源兜底：医案三元组没抽到「提示」时，用《脾胃论》的病机论断。
            hit = pwl_rationale(pwl, PWL_PREDICATES_FOR_PATHOGENESIS, case.pathogenesis)
            if hit:
                span, rat_src = hit
        steps.append(_step("症状→病机", case.pathogenesis, span, source, rat_src))
    if case.syndrome:
        span = _rationale(triples, "属于")
        steps.append(_step("病机→证型" if case.pathogenesis else "症状→证型",
                           case.syndrome, span, source, source if span else None))
    if case.treatment_principle and case.syndrome:
        span = _rationale(triples, "治以")
        # 这一步不走第五源：见 PWL_PREDICATES_FOR_PATHOGENESIS 上面那段
        # ——《脾胃论》的治法论断 o 全是单字，子串匹配等于任意命中。
        steps.append(_step("证型→治法", case.treatment_principle, span, source,
                           source if span else None))
    if case.formula and case.treatment_principle:
        span = _rationale(triples, "用方")
        rat_src = source if span else None
        if not span:
            hit = formulary.get(case.formula.strip()) or pwl_rationale(
                pwl, PWL_PREDICATES_FOR_FORMULA, case.formula)
            if hit:
                span, rat_src = hit
        steps.append(_step("治法→方剂", case.formula, span, source, rat_src))
    if case.herbs:
        steps.append(_step(
            "方剂→药材" if case.formula else "治法→药材",
            [_herb_item(h, triples, source, materia_medica) for h in case.herbs],
            # 列表步骤的依据逐味药挂在 output 里（每味药的依据不同），步骤这一层
            # 没有单一依据，填 None 而不是拿第一味药的凑一个
            None, source, None,
        ))
    note = incompatible_note(case)
    if note and steps:
        # 配伍提示做成**链上的最后一步**，不是塞在 meta 里：塞 meta 模型学不到它，
        # 而不学它就会学成"这种配伍可以开"，跟输出侧 check_incompatible 直接冲突
        # （见 core/safety_output.INCOMPATIBLE_TRAINING_NOTE）。
        # 依据就是这一例本身——是这一例的处方含反药，不是某本书说的。
        steps.append(_step("配伍提示", note, case.raw_excerpt or case.raw[:120],
                           source, source))
    if len(steps) < 2:
        return None
    symptoms = "；".join(case.symptoms) if case.symptoms else ""
    tongue_pulse = "，".join(x for x in (case.tongue, case.pulse) if x)
    return {
        "input": "，".join(x for x in (symptoms, tongue_pulse) if x),
        "chain": steps,
        "meta": {
            "source_kind": "case",
            "physician_id": case.physician, "case_id": case.case_id,
            "case_group_id": case.case_group_id, "copyright_status": case.copyright_status,
            "standard_prefix_miss": prefix_miss,
            # R18-G：两个筛选维度都落在 meta 里，训练集里"这条是哪个门类/有没有
            # 反药"事后可查——不落的话只能靠重跑导出去反推。
            "scope": case.scope, "has_incompatible_pair": case.has_incompatible_pair,
        },
    }


def to_sdt_chain_sample(record) -> dict | None:
    """一条 TCMEval-SDT Train 记录 → 一条链路样本（症状→病机 / 病机→证型）。

    金标准给的是选项字母，训练样本要的是模型该说出来的话，所以输出是选项**正文**
    （多选用「；」连），字母只在评测提交格式里用。依据取官方 `Explanatory Summary`
    + `Syndrome Differentiation`（专家撰写的辨证说明，就是原文）里**第一句含到
    该步任一选项正文的句子**；一句都没有就是 None——不拿整段说明当两步共用的
    依据，那等于说"这两步的依据一样"，而它们不是同一个判断。

    physician_id 是 None：SDT 是通用辨证知识，不属于某位医家。训练脚本按医家
    过滤时必须把 None 当"两位医家共用"，不是"没有医家所以丢掉"。
    """
    def _texts(letters: list[str], options: dict[str, str]) -> list[str]:
        return [options[x] for x in letters if x in options]

    pathogenesis = _texts(record.gold_pathogenesis_answers, record.pathogenesis_options)
    syndromes = _texts(record.gold_syndrome_answers, record.syndrome_options)
    summary = record.gold_summary or ""
    source = f"sdt:{record.record_id}"
    steps: list[dict] = []
    if pathogenesis:
        span = _first_sentence_containing(summary, pathogenesis)
        steps.append(_step("症状→病机", "；".join(pathogenesis), span, source,
                           source if span else None))
    if syndromes:
        span = _first_sentence_containing(summary, syndromes)
        steps.append(_step("病机→证型" if pathogenesis else "症状→证型",
                           "；".join(syndromes), span, source, source if span else None))
    if len(steps) < 2:
        return None
    return {
        "input": record.clinical_data,
        "chain": steps,
        "meta": {
            "source_kind": "sdt", "physician_id": None, "record_id": record.record_id,
            # SDT 自带 train/val/test，**不重切**：这里只导 Train，所以恒为 train，
            # split_source 记清楚这个 train 是谁定的（不是我们按 case_group_id 切的）
            "split": "train", "split_source": f"sdt:{SDT_TRAIN_SPLIT}",
        },
    }


def load_sdt_chain_samples(sdt_dir: Path | None) -> tuple[list[dict], dict]:
    """读 TCMEval-SDT Train，返回 (样本, 统计)。sdt_dir 为 None 或目录不存在时
    返回空——SDT 数据不随本仓库分发（CC BY 4.0，可独立获取的公开数据不入版本
    控制），沙盒里本来就没有。格式解析复用 eval/sdt/data.py（那份是从官方文件
    实测出来的唯一实现），不在这里另写一套 JSON 字段名。"""
    if sdt_dir is None:
        return [], {"available": False, "note": "未传 --sdt-dir，跳过 SDT 源"}
    path = Path(sdt_dir) / "data" / f"{SDT_TRAIN_SPLIT}_TCM_Data_v1.json"
    if not path.exists():
        return [], {"available": False, "note": f"{path} 不存在，跳过 SDT 源"}
    from eval.sdt.data import load_split

    records = load_split(Path(sdt_dir), SDT_TRAIN_SPLIT)
    samples = [s for s in (to_sdt_chain_sample(r) for r in records) if s is not None]
    return samples, {
        "available": True, "records": len(records), "samples": len(samples),
        "records_not_chainable": len(records) - len(samples),
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


def _count_rationales(samples: list[dict]) -> dict:
    """统计单位 = "一个需要依据的判断"：标量步骤算 1 个，药材列表按味数算。
    按出处分桶报，因为"有多少依据是药理层给的"和"有多少是医案给的"是两个不同的
    问题——只报一个总覆盖率看不出药理层到底接上了没有。"""
    n = with_rationale = 0
    by_source: Counter = Counter()
    by_step: Counter = Counter()
    for s in samples:
        for step in s["chain"]:
            by_step[step["step"]] += 1
            if step["step"] in ITEMIZED_STEPS:
                for item in step["output"]:
                    n += 1
                    if item.get("rationale") is not None:
                        with_rationale += 1
                        by_source[item["rationale_source"].split(":")[0]] += 1
            else:
                n += 1
                if step["rationale"] is not None:
                    with_rationale += 1
                    by_source[step["rationale_source"].split(":")[0]] += 1
    return {
        "steps": n, "steps_with_rationale": with_rationale,
        "steps_without_rationale": n - with_rationale,
        "rationale_by_source": dict(by_source),
        "step_counts": dict(by_step),
        # 目标六步逐步报覆盖：缺口是数字，不是被占位符盖住的空白
        "target_chain_coverage": {name: by_step.get(name, 0) for name in TARGET_CHAIN},
    }


def export_chain(cases: list[CaseRecord], triples_by_case: dict[str, list[dict]],
                 heldout_ratio: float = DEFAULT_HELDOUT_RATIO,
                 sdt_samples: list[dict] | None = None,
                 materia_medica: dict[str, tuple[str, str]] | None = None,
                 formulary: dict[str, tuple[str, str]] | None = None,
                 lookup=None,
                 pwl: list[tuple[str, str, str]] | None = None) -> tuple[list[dict], dict]:
    """返回 (样本列表, 统计)。统计里 rationale 覆盖率必须带对照报——
    "有多少步是带原文依据的"跟"有多少步是 None"并列，只报前者会显得都有依据。

    医案样本按 case_group_id 切 train/heldout；SDT 样本自带 split（恒 train，
    见 to_sdt_chain_sample），**不参与这次切分**。
    """
    split = split_by_case_group(cases, heldout_ratio)
    samples: list[dict] = []
    prefix_miss: Counter = Counter()
    for case in cases:
        sample = to_chain_sample(case, triples_by_case.get(case.case_id, []),
                                 materia_medica=materia_medica, formulary=formulary,
                                 lookup=lookup, pwl=pwl)
        if sample is None:
            continue
        sample["meta"]["split"] = split[case.case_group_id]
        sample["meta"]["split_source"] = "case_group_id"
        prefix_miss[sample["meta"]["standard_prefix_miss"] or "matched"] += 1
        samples.append(sample)
    n_case_samples = len(samples)
    samples.extend(sdt_samples or [])
    stats = {
        "samples": len(samples),
        "samples_by_source": {"case": n_case_samples, "sdt": len(sdt_samples or [])},
        "cases_not_chainable": len(cases) - n_case_samples,
        "split": dict(Counter(s["meta"]["split"] for s in samples)),
        # 教材前三步的命中情况：命中数旁边就是没命中的原因分布，不是只报命中数
        "standard_prefix": dict(prefix_miss),
        # R18-G 五源汇总：每一源给了多少步依据。第五源（脾胃论）是子串匹配、
        # 命中率天然低，单独报出来才看得见——不报的话"接了但一条没命中"跟
        # "接了且有用"分不出来。
        "rationale_pwl": {
            "index_size": len(pwl or []),
            "steps": sum(1 for smp in samples for st in smp["chain"]
                         if (st.get("rationale_source") or "").startswith("rationale_pwl:")),
        },
        # 配伍提示步的条数：它等于"带了反药配伍的医案样本数"，跟 --exclude-incompatible
        # 的过滤数是同一件事的两端，对不上就是有一头漏了。
        "incompatible_note_steps": sum(
            1 for smp in samples for st in smp["chain"] if st["step"] == "配伍提示"),
        "samples_by_scope": dict(Counter(
            smp["meta"].get("scope") or "未判" for smp in samples
            if smp["meta"].get("source_kind") == "case")),
        **_count_rationales(samples),
    }
    # 泄漏检查跟统计一起交出去，不只在 CLI 里算：任何调用方（训练脚本、测试）
    # 拿到 stats 就该看得见"这次切分泄没泄"，而不是各自再算一遍
    stats["leakage"] = leakage_report(samples)
    return samples, stats


def heldout_case_groups(samples: list[dict]) -> dict[str, set[str]]:
    """{"train": {case_group_id…}, "heldout": {…}}。只含医案样本（SDT 没有
    case_group_id）。R5-2 的泄漏判据就是这两个集合必须无交集。"""
    out: dict[str, set[str]] = {"train": set(), "heldout": set()}
    for s in samples:
        gid = s["meta"].get("case_group_id")
        if gid is not None:
            out.setdefault(s["meta"]["split"], set()).add(gid)
    return out


def leakage_report(samples: list[dict]) -> dict:
    """两个不同的泄漏问题，分开报，**不要合成一个"泄漏了/没泄漏"的布尔值**。

    1. `group_overlap`：同一个 case_group_id 出现在两侧。用现在的
       split_by_case_group **结构上不可能**（它返回的是 case_group_id → 侧的
       字典，一个组只有一个值），所以这一项今天恒为空集。它不是在抓当前的 bug，
       是给"将来有人改成按 case_id / 按诊次切"留的报警器——那一改，同一病人的
       初诊和复诊就会分到两侧，而复诊跟初诊内容高度重复，heldout 就废了。
       写清楚它今天抓不到东西，比让人以为它在保护什么更有用。
    2. `shared_inputs`：**这一项今天就会非零**。不同的 case_group_id 也可能有
       逐字相同的 input——同一粗段里两个病人共享 raw、不同病人症状列表凑巧一样。
       这些样本落在两侧时，heldout 上的 gap 对它们就没有意义了。报出现数和它的
       对照（heldout 样本总数），让人能判断 gap 可信到什么程度。**不自动去重**：
       去掉哪一侧是一个取舍（去 train 损失训练数据，去 heldout 缩小评测集），
       代码替人决定会让"gap 是多少"变成一个不知道怎么算出来的数。
    """
    groups = heldout_case_groups(samples)
    train_inputs = {s["input"] for s in samples if s["meta"].get("split") == "train"}
    heldout = [s for s in samples if s["meta"].get("split") == "heldout"]
    shared = [s for s in heldout if s["input"] in train_inputs]
    return {
        "train_groups": len(groups["train"]),
        "heldout_groups": len(groups["heldout"]),
        "group_overlap": sorted(groups["train"] & groups["heldout"]),
        "heldout_samples": len(heldout),
        "heldout_samples_with_input_also_in_train": len(shared),
        "shared_input_examples": [s["meta"].get("case_id") for s in shared[:5]],
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="从 cases.json 派生 SFT 训练样本")
    ap.add_argument("--format", choices=("alpaca", "chain"), default="alpaca",
                    help="alpaca=原来的四任务样本；chain=总纲 5.1 的六层链路样本（三源合并，每步带原文依据和出处）")
    ap.add_argument("--cases-path", type=Path, default=CASES_PATH)
    ap.add_argument("--triples-path", type=Path, default=TRIPLES_PATH, help="chain 格式用的医案三元组")
    ap.add_argument("--out", type=Path, default=None, help="默认 sft.jsonl（alpaca）/ sft_chain.jsonl（chain）")
    ap.add_argument("--heldout-ratio", type=float, default=DEFAULT_HELDOUT_RATIO)
    ap.add_argument("--sdt-dir", type=Path, default=None,
                    help="TCMEval-SDT 目录（chain 格式的第二个来源）。只读 Train，"
                         "Validation/Test 是评测集，导进训练数据就是泄漏。不传就跳过这一源。")
    ap.add_argument("--materia-medica-path", type=Path, default=MATERIA_MEDICA_PATH,
                    help="chain 格式的第三个来源：药材层三元组，给方剂→药材这一步补原文依据")
    ap.add_argument("--formulary-path", type=Path, default=FORMULARY_PATH,
                    help="同上，方剂层三元组，给治法→方剂这一步补原文依据")
    # R18-G **把这两个开关的方向反过来了**，这是有意的契约变更：
    #
    # R18 把李可（57 例，55 例肿瘤、21 例含反药配对）和王云启（77 例，全肿瘤）
    # 接进训练。按原来的默认（排除肿瘤、排除反药），这两位医家贡献的样本数是
    # **0**——R18-B/C 两个抽取脚本白写。所以默认改成"都带上"：
    #   - 反药配伍带上，但每条样本链末附一句固定的配伍提示
    #     （core/safety_output.INCOMPATIBLE_TRAINING_NOTE），模型学到的是
    #     "名家这样用过、且这是反药配伍要说明"，跟输出侧 check_incompatible 不冲突；
    #   - 肿瘤门类带上，因为李可/王云启的语料**本来就是肿瘤**，排掉等于不接。
    # 要回到原来的行为，显式传 --exclude-incompatible / --exclude-scope oncology。
    ap.add_argument("--exclude-incompatible", action="store_true",
                    help="排除含十八反十九畏配伍的医案。**R18-G 起默认是带上的**"
                         "（带上时链末附固定的配伍提示）。传这个开关会让李可 57 例里的"
                         "21 例不进训练集。")
    ap.add_argument("--exclude-scope", default="",
                    help="按门类排除，逗号分隔（如 oncology）。默认一个都不排。"
                         "传 oncology 会让王云启 77 例全部、李可 55/57 例不进训练集。")
    ap.add_argument("--rationale-pwl-path", type=Path, default=RATIONALE_PWL_PATH,
                    help="第五源：《脾胃论》立论层（R18-D 产出，进版本控制）")
    # 下面两个是 R18-G 之前的开关。**留着不删**：README、run_onsite.sh、
    # onsite_troubleshooting.md 里都写了它们，删掉会让照文档敲命令的人吃一个
    # "unrecognized arguments"。现在它们是默认行为，传了只说明一句、不改变结果。
    ap.add_argument("--include-incompatible", action="store_true",
                    help="（R18-G 起这已经是默认行为，这个开关不再改变任何结果）")
    ap.add_argument("--include-out-of-scope", action="store_true",
                    help="（R18-G 起这已经是默认行为，这个开关不再改变任何结果）")
    args = ap.parse_args(argv)

    for legacy, now in (("--include-incompatible", "默认就带上了"),
                        ("--include-out-of-scope", "默认就带上了")):
        if getattr(args, legacy.lstrip("-").replace("-", "_")):
            print(f"[export_sft] {legacy} 从 R18-G 起是默认行为（{now}），"
                  f"这个开关不再改变任何结果。要排除请用 --exclude-incompatible / "
                  f"--exclude-scope。", file=sys.stderr)
    exclude_scopes = tuple(x.strip() for x in args.exclude_scope.split(",") if x.strip())

    cases = load_cases(args.cases_path)
    cases = filter_public_domain(cases)
    cases = filter_incompatible_pairs(cases, include=not args.exclude_incompatible)
    cases = filter_by_scope(cases, exclude_scopes)

    if args.format == "chain":
        out_path = args.out or CHAIN_OUT_PATH
        triples_by_case = load_triples_by_case(args.triples_path)
        if not triples_by_case:
            print(f"[export_sft] 注意：{args.triples_path} 不存在或为空，所有步骤的 rationale 都会是 None——"
                  "先跑 offline/extract_case_triples.py", file=sys.stderr)
        sdt_samples, sdt_stats = load_sdt_chain_samples(args.sdt_dir)
        if not sdt_stats.get("available"):
            print(f"[export_sft] {sdt_stats['note']}", file=sys.stderr)
        materia_medica = load_materia_medica_rationales(args.materia_medica_path)
        formulary = load_formulary_rationales(args.formulary_path)
        pwl = load_rationale_pwl(args.rationale_pwl_path)
        if not pwl:
            print(f"[export_sft] 注意：{args.rationale_pwl_path} 不存在或为空，"
                  "第五源（《脾胃论》立论层）不参与补依据——先跑 "
                  "python -m offline.extract_rationale_pwl", file=sys.stderr)
        if not materia_medica and not formulary:
            print(f"[export_sft] 注意：药理层两个文件（{args.materia_medica_path}、"
                  f"{args.formulary_path}）都不存在或为空，方剂→药材 / 治法→方剂 只能靠医案"
                  "三元组补依据——先跑 offline/extract_materia_medica.py 和 "
                  "offline/extract_formulary.py", file=sys.stderr)
        samples, stats = export_chain(
            cases, triples_by_case, args.heldout_ratio,
            sdt_samples=sdt_samples, materia_medica=materia_medica, formulary=formulary,
            pwl=pwl,
        )
        stats["sdt"] = sdt_stats
        stats["pharmacology_index"] = {
            "materia_medica_herbs": len(materia_medica), "formulary_formulas": len(formulary),
        }
        leak = stats["leakage"]
        if leak["group_overlap"]:
            raise SystemExit("train 和 heldout 的 case_group_id 有交集，训练数据泄漏，拒绝写出："
                             f"{leak['group_overlap'][:5]}")
        with out_path.open("w", encoding="utf-8") as f:
            for s in samples:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"链路样本数：{stats['samples']} = 医案 {stats['samples_by_source']['case']}"
              f" + SDT {stats['samples_by_source']['sdt']}"
              f"（另 {stats['cases_not_chainable']} 条医案不足两步、不成链）")
        print(f"train/heldout：{stats['split']}（医案按 case_group_id 切；"
              f"SDT 用它自带的 {SDT_TRAIN_SPLIT}，不重切）")
        print(f"步骤 {stats['steps']}，带原文依据 {stats['steps_with_rationale']}，"
              f"无依据（rationale=None）{stats['steps_without_rationale']}")
        print(f"依据按出处：{stats['rationale_by_source']}")
        print(f"目标六步各自的步数：{stats['target_chain_coverage']}")
        print(f"教材前三步命中情况：{stats['standard_prefix']}")
        print(f"药理层索引：药材 {len(materia_medica)} 味 / 方剂 {len(formulary)} 张")
        print(f"第五源《脾胃论》立论层：索引 {stats['rationale_pwl']['index_size']} 条，"
              f"给了 {stats['rationale_pwl']['steps']} 步依据"
              "（子串匹配，命中率天然低，报出来才看得见）")
        print(f"配伍提示步：{stats['incompatible_note_steps']} 条"
              f"（= 含反药配伍的医案样本数；--exclude-incompatible 可以排掉它们）")
        print(f"样本按门类：{stats['samples_by_scope']}")
        print(f"泄漏检查：train {leak['train_groups']} 组 / heldout {leak['heldout_groups']} 组，"
              f"case_group_id 交集 {len(leak['group_overlap'])} 组"
              "（split_by_case_group 结构上保证为 0，这行是改切法时的报警器）")
        print(f"  heldout 里 input 跟 train 逐字相同的样本：{leak['heldout_samples_with_input_also_in_train']}"
              f" / {leak['heldout_samples']}（不自动去重，见 leakage_report 的文档字符串）")
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
