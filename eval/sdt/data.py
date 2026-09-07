"""TCMEval-SDT 的数据读写。**格式全部是从官方文件实测出来的，不是照描述写的。**

数据来源：github.com/zhuyan166/TCMEval，CC BY 4.0，
`evaluation/TCMEval-SDT/`。不随本仓库分发（跟 books/ 同理，可独立获取的公开
数据不入版本控制），路径由 --sdt-dir 传入。

实测出来的四件事，写代码前必须知道：

1. **提交文件是 `@` 分隔的纯文本，一行一条**：
   `病案ID@Task1@Task2@Task3@Task4`
   Task1 是 `;` 分隔的症状串，Task2/3 是 `;` 分隔的选项字母，Task4 是自由文本。

2. **Validation / Test 的 JSON 里答案字段全是空的**（`Clinical Information`、
   `Answers of *`、`Explanatory Summary`、`Syndrome Differentiation` 都是 ""），
   它们只提供 `Clinical Data` 和两组选项。金标准在 `Results/*_data_result.txt`
   里，**两个 split 都有**——不是只有 Validation 可评。

3. **`Results/Validation_data_result.txt` 开头有 UTF-8 BOM，Test 没有。**
   官方 evaluate.py 用 `open(path)` 默认方式读，BOM 会粘在第一条的病案 ID 上
   （变成 `﻿病例123`），于是那一条永远匹配不上、恒得 0 分。**这个行为要
   照原样保留**：论文里 15 个模型的分大概率是在同一份带 BOM 的文件上跑出来的，
   我们"修好"它只会让自己的数不可比。本模块的 read_gold 提供 strip_bom 开关，
   默认 False（跟官方一致），修正后的分只作为诊断值另行报告。

4. Test 的金标准文件末尾多一个空行（文件以换行结尾），不是多一条记录。
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

FIELD_SEP = "@"
LIST_SEP = ";"
BOM = "﻿"


class SdtRecord(BaseModel):
    """一条 SDT 记录。gold_* 字段在 Validation/Test 的 JSON 里是空的，
    要从 Results/*.txt 补进来——所以它们有默认值，不是防幻觉约束松了。"""

    record_id: str
    clinical_data: str
    pathogenesis_options: dict[str, str] = Field(default_factory=dict)
    syndrome_options: dict[str, str] = Field(default_factory=dict)

    gold_clinical_information: list[str] = Field(default_factory=list)
    gold_pathogenesis_answers: list[str] = Field(default_factory=list)
    gold_syndrome_answers: list[str] = Field(default_factory=list)
    gold_summary: str = ""


def parse_options(text: str) -> dict[str, str]:
    """"A:肝气横逆;B:胃中失和" -> {"A": "肝气横逆", "B": "胃中失和"}"""
    out: dict[str, str] = {}
    for chunk in (text or "").split(LIST_SEP):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        key, _, value = chunk.partition(":")
        out[key.strip()] = value.strip()
    return out


def load_split(sdt_dir: Path, split: str) -> list[SdtRecord]:
    """读 data/{split}_TCM_Data_v1.json。Train 自带全部金标准，
    Validation/Test 只有输入，金标准要再调 attach_gold。"""
    path = Path(sdt_dir) / "data" / f"{split}_TCM_Data_v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = []
    for r in raw:
        records.append(SdtRecord(
            record_id=r["Medical Record ID"],
            clinical_data=r["Clinical Data"],
            pathogenesis_options=parse_options(r.get("Options of TCM Pathogenesis", "")),
            syndrome_options=parse_options(r.get("Options of TCM Syndrome", "")),
            gold_clinical_information=_split_list(r.get("Clinical Information", "")),
            gold_pathogenesis_answers=_split_list(r.get("Answers of TCM Pathogenesis", "")),
            gold_syndrome_answers=_split_list(r.get("Answers of TCM Syndrome", "")),
            gold_summary=(r.get("Explanatory Summary") or "") + (r.get("Syndrome Differentiation") or ""),
        ))
    return records


def _split_list(text: str) -> list[str]:
    return [x for x in (text or "").split(LIST_SEP) if x.strip()]


def read_gold(sdt_dir: Path, split: str, strip_bom: bool = False) -> dict[str, list[str]]:
    """读 Results/{split}_data_result.txt，返回 {病案ID: [Task1..Task4 原文]}。

    strip_bom 默认 False = 跟官方 evaluate.py 一致（BOM 粘在第一条 ID 上，
    那条恒得 0）。想看"修正 BOM 之后的分"就传 True，但那个数不能跟论文比。
    """
    path = Path(sdt_dir) / "Results" / f"{split}_data_result.txt"
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if strip_bom:
            line = line.lstrip(BOM)
        fields = line.split(FIELD_SEP)
        if len(fields) < 5:
            continue
        out[fields[0]] = fields[1:5]
    return out


def attach_gold(records: list[SdtRecord], gold: dict[str, list[str]]) -> int:
    """把 Results/*.txt 里的金标准补进 Validation/Test 的记录。返回补上的条数。"""
    n = 0
    for r in records:
        fields = gold.get(r.record_id)
        if not fields:
            continue
        r.gold_clinical_information = _split_list(fields[0])
        r.gold_pathogenesis_answers = _split_list(fields[1])
        r.gold_syndrome_answers = _split_list(fields[2])
        r.gold_summary = fields[3]
        n += 1
    return n


def sanitize_field(text: str) -> str:
    """@ 和换行是这个格式的结构字符，出现在字段里会把整行错位——错位之后
    评分脚本按位置取字段，Task2 会拿到 Task1 的内容，分数就完全没有意义了。
    统一替换掉，不要指望"模型不会输出这些字符"。"""
    return (text or "").replace(FIELD_SEP, "＠").replace("\n", " ").replace("\r", " ")


def to_line(record_id: str, task1: list[str], task2: list[str],
            task3: list[str], task4: str) -> str:
    return FIELD_SEP.join([
        sanitize_field(record_id),
        sanitize_field(LIST_SEP.join(task1)),
        sanitize_field(LIST_SEP.join(task2)),
        sanitize_field(LIST_SEP.join(task3)),
        sanitize_field(task4),
    ])


def write_submission(path: Path, lines: list[str]) -> None:
    """不写 BOM：官方 Validation 金标准里的那个 BOM 是个 bug，我们没必要复现到
    自己的提交文件上——提交文件带 BOM 会让我们自己的第一条也匹配不上。"""
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
