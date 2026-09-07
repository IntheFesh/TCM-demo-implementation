"""附属：证素轨迹（trajectory-only，不含转移核）。

CLAUDE.md「改造期新增约定」早就把 `case_group_id`/`visit_index`/`prev_case_id`
定成数据模型的一部分——同一病人的复诊序列不再是互相独立的快照。这个模块
做的是把这条序列摆出来（按 visit_index 排序，配上每一诊的证素状态），
**不做转移概率矩阵、不拟合马尔可夫核之类的统计模型**。

为什么现在不做统计建模：demo 阶段样本量（各 30 案）连
`offline/quota.py` 里"每位医家 ≥50 例带复诊序列"这个门槛都够不上——此时
去拟合一个转移核，参数比数据点还多，吐出来的"转移概率"没有任何统计意义，
只会制造一个看着像结论、实际是噪声的数字。先把可核验的原始轨迹摆出来，
统计建模等样本量够了再做，这不是漏做，是刻意不做。

证素状态来自 data/element_index.json（K3b 的产出），不是在这里重新推断——
同一个 case_id 在 K3b 的检索路径和这里的轨迹路径上必须是同一份证素集合，
两处各算一次、算出两个不一样的答案，就是 CLAUDE.md 那条"同一概念的匹配
逻辑只能有一处实现"要防的事。
"""
from __future__ import annotations

import json
from pathlib import Path

from core.retrieval import CASES_PATH
from core.retrieval_graph import ELEMENT_INDEX_PATH
from core.schemas import CaseRecord


def build_trajectories(
    cases: list[CaseRecord], element_index: dict
) -> dict[str, list[dict]]:
    """按 physician 分组，组内再按 case_group_id 分组、按 visit_index 排序。

    **只保留有 >= 2 诊的病人组。** 单诊的"序列"只是一个点，没有"轨迹"可言
    ——这是故意的过滤，不是遗漏；调用方如果想看单诊病人，直接查
    cases.json 本身即可，不需要这个模块。
    """
    by_group: dict[tuple[str, str], list[CaseRecord]] = {}
    for c in cases:
        by_group.setdefault((c.physician, c.case_group_id), []).append(c)

    trajectories: dict[str, list[dict]] = {}
    for (physician, group_id), group_cases in by_group.items():
        if len(group_cases) < 2:
            continue
        group_cases = sorted(group_cases, key=lambda c: c.visit_index or 0)
        visits = []
        for c in group_cases:
            entry = element_index.get(c.case_id) or {}
            visits.append({
                "case_id": c.case_id,
                "visit_index": c.visit_index,
                "elements": entry.get("elements") or [],
                "symptoms": c.symptoms,
                "syndrome": c.syndrome,
            })
        trajectories.setdefault(physician, []).append({
            "case_group_id": group_id,
            "n_visits": len(visits),
            "visits": visits,
        })

    for physician in trajectories:
        trajectories[physician].sort(key=lambda t: t["case_group_id"])
    return trajectories


def load_trajectories(
    cases_path: Path = CASES_PATH,
    element_index_path: Path = ELEMENT_INDEX_PATH,
) -> dict[str, list[dict]]:
    """真正给 API/CLI 用的入口：加载两份数据文件再调 build_trajectories()。
    文件缺失时明确报错，不是悄悄返回空字典——"数据还没生成"和"生成了但
    没有符合条件的轨迹"是两个不同的信号，静默返回空会把两者混为一谈。
    """
    if not cases_path.exists():
        raise FileNotFoundError(
            f"未找到 {cases_path}。请先运行 `python -m offline.extract_cases` 生成 cases.json。"
        )
    if not element_index_path.exists():
        raise FileNotFoundError(
            f"未找到 {element_index_path}。请先运行 "
            "`python -m offline.build_element_index` 生成证素索引。"
        )
    cases = [
        CaseRecord.model_validate(r)
        for r in json.loads(cases_path.read_text(encoding="utf-8"))
    ]
    element_index = json.loads(element_index_path.read_text(encoding="utf-8"))
    return build_trajectories(cases, element_index)
