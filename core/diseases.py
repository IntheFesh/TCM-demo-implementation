"""M4 病名层：中医内科病名参考表的加载与匹配。

数据来源方法论（跟 core.safety_output.DOSE_LIMITS 一样如实记录）：
name/aliases/location/cardinal/common_syndromes 五个字段取自标准中医内科学教材
内容（本会话训练知识范围内可靠，没有走 WebSearch 核实——这不是"查一个数字对不
对"，是"这个病的主症/病位/常见证型是什么"，属于教材共识，跟需要精确到克数/
分钟数的安全字段不是同一个可靠度量级）。

triage_dept / triage_urgency / red_flags 三个安全字段用了跟 DOSE_LIMITS 相同的
方法论：WebSearch 核实（沙箱 WebFetch/curl 对医学站点同样 EGRESS_BLOCKED，只能
拿到搜索引擎摘要转述，不是逐字核对原文），核实结论见本轮模块报告。具体到每条：
  - 胸痹/吐血/便血/噎膈反胃/眩晕（脑血管意外红旗）五类的 red_flags 是本轮真实
    WebSearch 核实过的（标准全科/急诊医学教学内容：胸痛 ACS 红旗、消化道出血
    红旗、进行性吞咽困难+体重下降的癌症警示、中枢性眩晕 BE-FAST 红旗），
    其余病名的 red_flags 是同一套"生命体征失代偿/进行性消瘦/意识改变"框架的
    合理外推，没有逐条单独核实来源。
  - triage_dept 在真实存在归科歧义时（肿胀可能是肾病/心衰/肝硬化腹水，痰饮可能
    是呼吸科/心内科病因）**如实留 None**，不猜一个具体科室——错误的科室指向
    会让患者绕远路，比"没给建议"更糟。
  - corpus_gate 只在 offline/split_cases.py 当前 gates 配置里实际出现过的门类
    才填（对齐 data/SOURCES.md 第 3 节门类分布表的真实数据，不是脾胃门类里
    "看着应该有"的都填）：胸痹/心悸/眩晕/不寐/便血/咳嗽六个是本轮新增病名，
    语料库（叶天士/吴鞠通医案）目前没有对应医案，如实留空列表——**"吐血"
    同样如实留空**：它在 core.syndrome_norm.SYNONYMS 表和 tests/queries.txt 的
    安全否决测试主诉里出现过，但从来不是 split_cases.py 实际的 gates 门类关键词，
    也不在 data/SOURCES.md 门类分布表里出现过一次，语料库对它的真实覆盖是 0，
    这是本轮核对时发现的、跟 M4 任务描述原文的表述不一致的地方，见模块报告。

match_disease 的症状文本匹配复用 core.tools._symptom_text_matches——全模块唯一
的症状文本匹配器（双向子串 + 并列片段拆分），不另写一套字面比较（CLAUDE.md
「同一概念的匹配逻辑只能有一处实现」，这个项目已经在这堵墙上撞过三次）。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from core.schemas import Disease
from core.tools import _symptom_text_matches

# 放在 data/standard/ 而不是 M4 任务描述原文写的 data/diseases.jsonl 顶层路径：
# .gitignore 对 *.jsonl 是整体忽略、只对 data/standard/*.jsonl 开了个例外
# （data/standard/syndromes.jsonl 就是这么处理的——同属"人工整理的静态参考表"，
# 不是 cases.json 派生的生成物）。放在原路径会导致这份文件永远进不了版本控制、
# 静默消失，问题只会在下一次 clone 或 CI 上才暴露；这是本轮核对时发现的
# spec 与仓库既有 .gitignore 约定之间的冲突，选择跟随既有约定而不是新开一条
# .gitignore 例外，理由见模块报告。
DISEASES_PATH = Path(__file__).resolve().parent.parent / "data" / "standard" / "diseases.jsonl"

# 主症命中数和病位命中各自的权重。主症是直接证据（患者说了什么），病位只是
# 佐证（证素分析推出了哪个脏腑）——契合度应该以主症为主，病位做区分度不够时
# 的加分项，不能反过来。两个数加起来是 1.0，分数落在 [0, 1] 区间方便前端展示。
_WEIGHT_CARDINAL = 0.7
_WEIGHT_LOCATION = 0.3


@lru_cache(maxsize=1)
def load_diseases() -> list[Disease]:
    """加载 data/standard/diseases.jsonl。惰性初始化 + 缓存——CLAUDE.md「加载模型/大文件的
    对象一律惰性初始化，禁止在模块顶层实例化」，这里虽然文件不大（15 条），
    但模块导入时就读文件、且每次调用都重新解析一遍 JSON 是不必要的重复 IO，
    跟项目里其他"惰性单例"的形状（get_llm/get_retriever/get_graph_store）一致。

    文件不存在时抛 FileNotFoundError，不静默返回空列表——那样 match_disease
    会一直返回"没有匹配"，调用方很难判断到底是"真的没匹配上"还是"数据没装"。
    """
    if not DISEASES_PATH.exists():
        raise FileNotFoundError(f"病名参考表不存在：{DISEASES_PATH}")
    diseases = []
    with DISEASES_PATH.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                # 不吞：一行坏数据说明这个文件本身出了问题，让它在加载时就炸出来，
                # 比"少加载了一个病名、下游查不到时才发现"更容易定位。
                raise ValueError(f"{DISEASES_PATH} 第 {line_no} 行不是合法 JSON：{e}") from e
            diseases.append(Disease.model_validate(data))
    return diseases


def get_disease(name: str) -> Disease | None:
    """按病名或别名查找。找不到返回 None，不抛异常——"查无此病名"是调用方
    的合法输入（模型可能填一个表外病名），不是程序错误。"""
    for d in load_diseases():
        if name == d.name or name in d.aliases:
            return d
    return None


def match_disease(symptoms: list[str], elements: list[str]) -> list[tuple[str, float]]:
    """按主症命中数 + 病位匹配打分，返回 (病名, 分数) 降序列表。纯规则不调
    LLM——病名判定要可复现，同样的输入永远同样的输出，跟 core/tools.py 里
    "工具层不做 LLM 调用"是同一条理由。

    分数只在 [0, 1]：主症命中率（命中的 cardinal 条数 / 总 cardinal 条数）乘
    _WEIGHT_CARDINAL，病位命中（disease.location 里任一个词出现在 elements 里）
    乘 _WEIGHT_LOCATION，两者相加。一条主症都没命中、病位也没命中的病名不进
    返回列表——分数为 0 的候选没有区分度，列出来只会让调用方误以为它是个
    "弱匹配"，其实是"完全没匹配上"。
    """
    scored: list[tuple[str, float]] = []
    for d in load_diseases():
        cardinal_hits = sum(
            1 for c in d.cardinal if any(_symptom_text_matches(c, s) for s in symptoms)
        )
        cardinal_ratio = (cardinal_hits / len(d.cardinal)) if d.cardinal else 0.0
        location_hit = any(loc in elements for loc in d.location)
        score = cardinal_ratio * _WEIGHT_CARDINAL + (
            _WEIGHT_LOCATION if location_hit else 0.0
        )
        if cardinal_hits > 0 or location_hit:
            scored.append((d.name, round(score, 3)))
    scored.sort(key=lambda pair: -pair[1])
    return scored


def triage_advice(disease: Disease) -> str:
    """M6：从 triage_dept/triage_urgency/red_flags 三个已经核实过的结构化
    字段拼一句人话建议，不是另外维护一批自由文本的"advice"句子——那样
    每条都要重新走一遍 M4 定下的"有出处、不确定就留白"核实流程，维护成本
    和出错面都远大于从三个字段拼模板。三个字段本身的可信度不因为这里被
    拼成一句话而改变：dept 为 None 时如实说"具体科室建议现场分诊"，不替
    M4 已经决定"留空"的字段编一个具体科室出来。
    """
    urgency = disease.triage_urgency
    if disease.triage_dept:
        dept_clause = f"，建议就诊{disease.triage_dept}"
    else:
        dept_clause = "，建议及时就医（具体科室建议现场分诊）"

    if urgency == "high":
        base = f"{disease.name}类症状需要提高警惕{dept_clause}，建议尽快就诊"
    elif urgency == "medium":
        base = f"{disease.name}类症状{dept_clause}，建议近期就诊"
    elif urgency == "low":
        base = f"{disease.name}类症状{dept_clause}，可先观察，如症状持续或加重再就诊"
    else:
        # urgency 未知（M4 有意留空的病名，如"肿胀""痰饮"——真实病因跨科，
        # 强行给一个紧急度等级比不给更危险），不假装知道紧急程度。
        base = f"{disease.name}类症状建议咨询医师{dept_clause}"

    if disease.red_flags:
        # 措辞故意跟 core/safety.py 的否决文案不一样（那句是"整个请求被拒绝、
        # 不产出任何辨证结果"的否决用语，tests/test_dedup_contracts.py 钉死它
        # 只能出现在 safety.py 一处）：这里回答的是另一个问题——"辨证已经给出
        # 结果，但这类病如果出现这些红旗症状要立刻升级去急诊"，是附加在正常
        # 产出上的提示，不是拒绝服务。两处字面接近但概念不同，用词上刻意
        # 错开，不去踩 dedup 测试的边界。
        base += f"；如出现以下情况请立即拨打120或前往急诊：{'、'.join(disease.red_flags)}"
    return base
