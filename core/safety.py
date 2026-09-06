"""安全否决层：危重症状拦截，必须发生在 S2（证素推断）之前——CLAUDE.md
「改造期新增约定」明确要求。命中拦截的主诉直接返回拒绝辨证的提示，不进入
S2/S3，不产出任何方药，不是在结果的 note 字段里事后提一句"建议转诊"。

关键词表刻意跟 core/syndrome_norm.py 的 SYNONYMS 分开维护：那张表回答的是
"这个词属于哪个证候门类"，这里回答的是"要不要在辨证开始前拦截整个请求"，
是两个不同的判断——合并成一张表，以后改一边时看不出会不会连带影响另一边。

纯关键词匹配，不调用 LLM：安全层必须在没有网络/API key 的情况下也能跑，
而且必须是确定性的（同样的输入永远同样的拦截结果），不能依赖模型的不确定性。
"""
from __future__ import annotations

# 覆盖这个 demo 脾胃门范围内、临床上需要立即转诊而不是继续辨证的信号：
# 消化道出血（呕血/黑便/柏油样便）、意识改变、休克体征、持续剧痛。
DANGER_KEYWORDS: list[str] = [
    "呕血",
    "吐血",
    "便血",
    "黑便",
    "柏油样便",
    "大量出血",
    "昏迷",
    "神志不清",
    "休克",
    "剧烈腹痛",
    "持续剧痛",
]


# 口语化表述：患者不会说"吐血"，只会说"吐了血""吐了两次血"。
# 纯子串匹配对付不了中间插字，用正则允许关键动词与宾语之间有少量字符。
DANGER_PATTERNS: list[tuple[str, str]] = [
    (r"(吐|呕|咯|咳)[^，。；\s]{0,4}血", "呕血"),
    (r"(大?便|拉|排)[^，。；\s]{0,4}(黑|发黑|柏油)", "黑便"),
    (r"(便|拉|排)[^，。；\s]{0,4}血", "便血"),
    (r"(腹|肚)[^，。；\s]{0,3}(剧痛|绞痛|痛得|痛到)", "剧烈腹痛"),
    (r"(神志|意识)[^，。；\s]{0,4}(不清|模糊|丧失)", "意识改变"),
]


def check_safety(symptoms: list[str]) -> str | None:
    """symptoms 是 S1 标准化后的症状列表（在 S2 之前调用）。命中任一关键词
    就返回可以直接展示给用户的拒绝理由；没有命中则返回 None，放行进入 S2。"""
    import re

    # 关键词表和正则表可能对同一段文本双重命中（"呕血"既是字面词、
    # 也匹配 (吐|呕|咯|咳).{0,4}血）。正则的 label 用的是关键词表里的同名词，
    # 靠 dict.fromkeys 去重即可——不要因为两套机制都命中就报两遍。
    hits: list[str] = []
    for text in symptoms:
        hits.extend(kw for kw in DANGER_KEYWORDS if kw in text)
        for pattern, label in DANGER_PATTERNS:
            if re.search(pattern, text):
                hits.append(label)
    if not hits:
        return None
    matched = "、".join(dict.fromkeys(hits))  # 去重且保持命中顺序
    return (
        f"检测到危重症状信号（{matched}），本 demo 不适用于此类情况，"
        "请立即就医或拨打急救电话，本次不提供辨证结果。"
    )
