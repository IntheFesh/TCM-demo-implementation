"""MTCMB 的 TCM-PR（方剂推荐）子任务：读数据 + 探字段。

## 这个文件最重要的一句话

**这台机器上没有 MTCMB 的数据，字段名是按公开说明写的、没有实测过。**
`eval/sdt/` 那一套之所以敢写"格式已实测验证"，是因为当时真的把
TCMEval 拉下来跑了一遍（README 里那四条读数注意全部是实测出来的）。
这里做不到同一件事，所以走另一条路：**把字段映射写成可探测的**，
上机第一步是

    python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --probe

它会打印文件里真实存在的键、当前映射能不能解析、解析不了时列出候选。
**不要跳过这一步直接跑评测**——一个字段名猜错的结果不是报错，是一份
"金标准全空、所有人都得 0 分"的漂亮报告（SDT 那边就踩过：JSON 里的答案
字段是空的，照着 Train 的字段名写代码会得到一份全空的金标准而且不报错）。

## 形状

一条记录 = 一段病人描述（症状/证型/病史）+ 一张参考方（药味列表）。
模型要给出它开的方（药味列表），按药味集合打分（见 `score.py`）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: 每个字段可能叫什么。**按优先级排**，第一个命中的胜出。
#: 写成表而不是写死一个名字：这份数据的字段名没有在这台机器上核过，
#: 而"多认几个同义名"跟"猜一个名字"是两回事——前者能在 probe 里被看见。
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "record_id": ("id", "ID", "index", "case_id", "uid"),
    "question": ("question", "input", "prompt", "query", "text", "instruction"),
    "answer": ("answer", "output", "label", "target", "response", "gold"),
}

#: 药味分隔符。中文顿号/逗号/分号/空格/加号都见过，统一在这里切。
_SPLIT = re.compile(r"[、,，;；\s/+]+")

#: 一条记录里参考方的药味数上限——**超过它十有八九是把整段话当成了药名**。
#: 不是为了"过滤脏数据"，是为了让 probe 能报出"这个字段多半映射错了"。
MAX_PLAUSIBLE_HERBS = 60


@dataclass(frozen=True)
class PrescriptionRecord:
    """一条 TCM-PR 记录。`raw` 原样留着——打分时要回看原文。"""

    record_id: str
    question: str
    gold_herbs: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


def split_herbs(text: str) -> list[str]:
    """把一段药味文本切成药名列表。**不做归一**——归一在 `score.py` 里
    统一走 `core.herbs.normalize_herb`（同一个概念只能有一处实现）。"""
    if not text:
        return []
    parts = [p.strip() for p in _SPLIT.split(str(text))]
    return [p for p in parts if p]


def _pick(row: dict, key: str) -> tuple[str | None, str]:
    """返回 (字段名, 值)。找不到时字段名为 None——**调用方要区分"没有这个
    字段"和"字段是空的"**，两者的修法完全不同。"""
    for name in FIELD_CANDIDATES[key]:
        if name in row:
            return name, row[name]
    return None, ""


def _iter_rows(path: Path):
    """读 .json（数组或 {data:[...]}）或 .jsonl。"""
    text = path.read_text(encoding="utf-8-sig")   # BOM：SDT 那边踩过，这里先剥
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if line:
                yield json.loads(line)
        return
    data = json.loads(text)
    if isinstance(data, dict):
        for key in ("data", "records", "examples", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise ValueError(f"{path.name}：读出来的不是记录数组，是 {type(data).__name__}")
    yield from data


def data_files(directory: Path, pattern: str = "*") -> list[Path]:
    files = sorted(p for p in Path(directory).glob(pattern)
                   if p.suffix in (".json", ".jsonl") and p.is_file())
    return files


def load_records(directory: str | Path, *, pattern: str = "*",
                 limit: int = 0) -> list[PrescriptionRecord]:
    """读一个目录下的所有记录。字段解析不了就**抛异常并说清缺哪个**，
    不返回一份半空的列表——半空的列表会一路跑到打分那一步才露馅。"""
    directory = Path(directory)
    files = data_files(directory, pattern)
    if not files:
        raise FileNotFoundError(f"{directory} 下没有 .json / .jsonl")
    out: list[PrescriptionRecord] = []
    for path in files:
        for i, row in enumerate(_iter_rows(path)):
            if not isinstance(row, dict):
                raise ValueError(f"{path.name} 第 {i} 条不是对象：{type(row).__name__}")
            qf, question = _pick(row, "question")
            if qf is None:
                raise KeyError(
                    f"{path.name} 第 {i} 条里找不到题面字段（试过 "
                    f"{'/'.join(FIELD_CANDIDATES['question'])}）。"
                    f"这条记录实际有的键：{sorted(row)}。"
                    f"先跑 `--probe` 看清楚，再决定改 FIELD_CANDIDATES 还是换目录。")
            rid_field, rid = _pick(row, "record_id")
            af, answer = _pick(row, "answer")
            out.append(PrescriptionRecord(
                record_id=str(rid) if rid_field else f"{path.stem}-{i}",
                question=str(question),
                gold_herbs=tuple(split_herbs(answer)) if af else (),
                raw=row))
            if limit and len(out) >= limit:
                return out
    return out


def probe(directory: str | Path, *, pattern: str = "*", sample: int = 3) -> dict:
    """看这份数据到底长什么样。**零 LLM、零打分**，只回答三件事：
    文件里有哪些键、当前映射解析得出什么、有没有明显不对劲的地方。"""
    directory = Path(directory)
    files = data_files(directory, pattern)
    report: dict = {"dir": str(directory), "n_files": len(files),
                    "files": [f.name for f in files], "problems": [], "samples": []}
    if not files:
        report["problems"].append(f"{directory} 下没有 .json / .jsonl")
        return report
    keys: set[str] = set()
    n_rows = 0
    n_with_answer = 0
    n_suspicious = 0
    for path in files:
        for row in _iter_rows(path):
            if not isinstance(row, dict):
                report["problems"].append(f"{path.name} 里有非对象记录")
                continue
            n_rows += 1
            keys |= set(row)
            af, answer = _pick(row, "answer")
            herbs = split_herbs(answer) if af else []
            if herbs:
                n_with_answer += 1
            if len(herbs) > MAX_PLAUSIBLE_HERBS:
                n_suspicious += 1
            if len(report["samples"]) < sample:
                qf, question = _pick(row, "question")
                report["samples"].append({
                    "question_field": qf, "answer_field": af,
                    "question_head": str(question)[:120],
                    "n_gold_herbs": len(herbs), "gold_head": herbs[:8]})
    report.update({"n_rows": n_rows, "keys": sorted(keys),
                   "n_with_answer": n_with_answer,
                   "answer_coverage": (round(n_with_answer / n_rows, 4) if n_rows else None)})
    # 三种"多半映射错了"的形状，各报各的话——合成一句"数据有问题"没法照着修
    if n_rows and n_with_answer == 0:
        report["problems"].append(
            "**一条参考方都没读到**：答案字段多半不叫 "
            f"{'/'.join(FIELD_CANDIDATES['answer'])} 里的任何一个，"
            f"实际的键是 {sorted(keys)}。照这份数据跑评测，所有人都会得 0 分。")
    if n_suspicious:
        report["problems"].append(
            f"{n_suspicious} 条记录的参考方超过 {MAX_PLAUSIBLE_HERBS} 味"
            "——多半是把整段话当成了药名（分隔符不对，或者答案字段指错了）。")
    if n_rows and n_with_answer and n_with_answer < n_rows:
        report["problems"].append(
            f"{n_rows - n_with_answer} 条没有参考方（{n_rows} 条里）。"
            "如果这是测试集的正常形态（答案不公开），**不要在这份数据上算分**。")
    return report
