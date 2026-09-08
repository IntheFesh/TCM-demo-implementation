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

import os
import re

# 覆盖这个 demo 脾胃门范围内、临床上需要立即转诊而不是继续辨证的信号：
# 消化道出血（呕血/黑便/柏油样便/咖啡渣样呕吐物）、意识改变（昏迷/晕厥/不省人事）、
# 休克体征、持续剧痛。
DANGER_KEYWORDS: list[str] = [
    "呕血",
    "吐血",
    "咯血",
    "便血",
    "血便",
    "黑便",
    "柏油样便",
    "咖啡渣",
    "肛门出血",
    "大量出血",
    "昏迷",
    "晕厥",
    "不省人事",
    "叫不醒",
    "神志不清",
    "休克",
    "剧烈腹痛",
    "持续剧痛",
]

# 同一件事的两种写法在拒绝文案里只该出现一次：关键词「吐血」和正则的「呕血」
# 标签会对同一句话双双命中，dict.fromkeys 去不掉这种"不同字面、同一含义"。
_LABEL_CANON: dict[str, str] = {
    "吐血": "呕血", "神志不清": "意识改变", "持续剧痛": "剧烈腹痛",
    "晕厥": "昏迷", "不省人事": "昏迷", "叫不醒": "昏迷", "血便": "便血",
}

# 口语化表述：患者不会说"吐血"，只会说"吐了血""吐了两次血"。
# 纯子串匹配对付不了中间插字，用正则允许关键动词与宾语之间有少量字符。
#
# 每条正则用命名组 obj 标出「宾语」（血/黑/剧烈/不清……）。否定判断只看紧挨在 obj
# 前面的那两三个字（见 _object_negated），不看整个间隔——这是两轮实测换来的边界：
#   - 把 不/未/无/没 整个排除出间隔（上一版的做法）会漏掉「大便不成形发黑」这类
#     教科书式的柏油便描述（不成形 + 发黑），是真回归；
#   - 完全不看否定又会把追问的阴性回答「大便不带血」「腹痛不剧烈」「意识不模糊」
#     整链否决，parse_answer 判 no、check_safety 判拦，同一句话两处答案相反。
#   - 「吐了不少血」「痛得不行」里的 不少/不行 是数量/程度词，不是否定——
#     _NEG_QUANTITY 把它们排除在否定之外。
# 「血」后面跟 压/虚/糖/脂 的是血压/血虚/血糖/血脂，前面是「气」的是气血；
# 「便」前面是 小/顺/即/方/随 的不是大便。这两条是上一轮实测出来的误报边界。
_GAP = r"[^。；;！？!?]{0,6}"
DANGER_PATTERNS: list[tuple[str, str]] = [
    (rf"(吐|呕|咯|咳)(?!血)(?P<gap>{_GAP})(?P<obj>(?<!气)血(?![压虚糖脂]))", "呕血"),
    (rf"(?<![小顺即方随])(大?便|拉|排|解)(?P<gap>{_GAP})(?P<obj>发黑|黑|柏油)", "黑便"),
    (r"黑色(?P<gap>[^，。；\s]{0,2})(?P<obj>大便|便|粪|屎)", "黑便"),
    (r"柏油(?P<gap>[^，。；\s]{0,3})(?P<obj>便|粪|屎)", "黑便"),
    (rf"(?<![小顺即方随])(便|拉|排|解)(?P<gap>{_GAP})(?P<obj>(?<!气)血(?![压虚糖脂]))", "便血"),
    (r"大便(?P<gap>[^，。；\s]{0,3})(?P<obj>暗红|鲜红|紫黑)", "便血"),
    (r"痰(?P<gap>[^，。；\s]{0,2})(?P<obj>血)", "咯血"),
    (r"(腹|肚|胃|脘)(?P<gap>[^，。；\s]{0,3})"
     r"(?P<obj>剧烈|剧痛|绞痛|难忍|(?:痛|疼)(?:得|到)(?:不行|不了|受不了|厉害|难受|要命)|(?:痛|疼)(?:得|到)(?![不没未]))", "剧烈腹痛"),
    (r"剧烈(?P<gap>[^，。；\s]{0,3})(?P<obj>腹痛|肚子疼|肚痛|胃痛|腹部|胃脘)", "剧烈腹痛"),
    (r"(神志|意识)(?P<gap>[^，。；\s]{0,4})(?P<obj>不清|模糊|丧失)", "意识改变"),
    (r"(昏|晕)(?P<obj>过去|倒|厥)", "昏迷"),
]

# 紧挨在宾语前面的否定词。「不少/不止/不断/不停/不行」是数量/程度词，排除。
_NEG_BEFORE_OBJ = ("不带", "不出", "不见", "不是", "没有", "未见", "未曾", "没", "无", "未", "不")
_NEG_QUANTITY = ("不少", "不止", "不断", "不停", "不行", "不了")
# 宾语前面是这些字时说的不是大便/出血：舌苔黑、面黑、排尿黑
_NON_STOOL_CONTEXT = ("苔", "舌", "面", "唇", "甲", "尿", "溲")
# 望诊描述里「色黑」前面若出现这些字，说的是舌苔/面色不是大便
_NON_STOOL_WORDS = ("舌苔", "苔色", "面色", "唇色", "甲色", "肤色", "舌质", "小便", "溲")

# 紧挨在整个命中前面的否定词：「无黑便」「否认呕血」「没吐过血」「从未便血」是阴性
# 陈述。只认前置否定，不认命中内部的（那由 _object_negated 按宾语判断）。
_NEGATION_PREFIXES: tuple[str, ...] = (
    "无", "否认", "没有", "没", "未见", "不见", "未曾", "从无", "从未", "从没", "不曾",
    "从不", "无明显", "无明确", "未",
)


def _negated(text: str, start: int) -> bool:
    before = text[max(0, start - 4):start]
    return any(before.endswith(n) for n in _NEGATION_PREFIXES)


def _object_negated(text: str, obj_start: int) -> bool:
    """宾语（血/黑/剧烈……）紧前面是否定词或非大便语境。"""
    before = text[max(0, obj_start - 3):obj_start]
    if any(before.endswith(q) for q in _NEG_QUANTITY):
        return False
    if any(before.endswith(n) for n in _NEG_BEFORE_OBJ):
        return True
    if any(before.endswith(c) for c in _NON_STOOL_CONTEXT):
        return True
    # 「大便正常，面色黑」：obj 前是「色」，要再往前看一格才认得出「面色」
    return any(w in text[max(0, obj_start - 5):obj_start] for w in _NON_STOOL_WORDS)


def _keyword_context_ok(text: str, m) -> bool:
    """关键词裸子串扫描也要守同样的语境边界：「呕吐血压偏高」里的「吐血」不是吐血。"""
    kw = m.group(0)
    nxt = text[m.end():m.end() + 1]
    if kw.endswith("血") and nxt in ("压", "虚", "糖", "脂"):
        return False
    return not _negated(text, m.start())


# 表里存的是字符串（读起来是表，改起来也是表），编译一次放这里。之前 _scan 在
# 循环里对每个起始位置都 re.compile 一次：200 字的主诉 × 12 条模式 ≈ 2400 次
# 编译缓存查找，而这是每个请求都要过的热路径。
_DANGER_KEYWORD_RES: list[tuple[str, re.Pattern]] = [
    (kw, re.compile(re.escape(kw))) for kw in DANGER_KEYWORDS
]
_DANGER_PATTERN_RES: list[tuple[re.Pattern, str]] = [
    (re.compile(pattern), label) for pattern, label in DANGER_PATTERNS
]


def _scan(text: str, honor_negation: bool) -> list[str]:
    hits: list[str] = []
    for kw, kw_re in _DANGER_KEYWORD_RES:
        if any(not honor_negation or _keyword_context_ok(text, m)
               for m in kw_re.finditer(text)):
            hits.append(_LABEL_CANON.get(kw, kw))
    for pattern_re, label in _DANGER_PATTERN_RES:
        # 不能命中一次就 break：finditer 是非重叠的，被否定跳过的那个最左匹配会把
        # 后面真正的危重表述一起吞掉（「无便血但大便黑」旧写法整句放行）。
        # 用 overlapped 扫法——每个位置都起一次匹配。
        for i in range(len(text)):
            m = pattern_re.match(text, i)
            if not m:
                continue
            if honor_negation and (_negated(text, m.start()) or _object_negated(text, m.start("obj"))):
                continue
            hits.append(label)
            break
    return hits


def mentions_danger(text: str) -> str | None:
    """文本里**提到**了危重信号（不管是肯定、否定还是提问）。返回标签或 None。

    给追问用：「有没有便血？」这句问题本身含否定式问法，check_safety 会当成阴性
    陈述放行；提问方需要知道的是"我问的是不是一个危重症状"，跟"这句话是不是在
    陈述危重症状"是两个问题。
    """
    hits = _scan(text, honor_negation=False)
    return "、".join(dict.fromkeys(hits)) if hits else None


def safety_bypassed(explicit: bool | None = None) -> bool:
    """**这次调用要不要跳过"命中后中止"这个动作**——注意跳过的只是中止，
    `check_safety()` 本身照跑、命中原因照记，不是不检测了。

    唯一的判定实现，两条链路（`core.chain.consult` 和 `eval.sdt.adapter`）
    都调它，不各写一套。判定顺序刻意是"显式参数优先，未指定才读环境变量"：

    - 显式参数并发安全。同一个进程里两个请求可以各自指定，互不影响；
      env var 是全局的，一个评测脚本设了它，同进程跑的 demo 请求会跟着变——
      那正是安全红线最不能出的事。
    - 环境变量兜底是为了让整批评测（`eval/run_eval.py` 调 consult、
      `eval/sdt/run.py` 调 solver）不必逐个调用点改签名。

    形状照抄同文件外 `core.react.react_enabled()` / `consult(use_react=None)`
    这条本项目已有的约定，不新发明一种。

    **默认关**：不设 EVAL_MODE、不传参数时返回 False，demo 行为一字不变。
    """
    if explicit is not None:
        return explicit
    return os.environ.get("EVAL_MODE", "0").lower() in ("1", "true", "yes")


def check_safety(symptoms: list[str]) -> str | None:
    """symptoms 是 S1 标准化后的症状列表（在 S2 之前调用）。命中任一关键词
    就返回可以直接展示给用户的拒绝理由；没有命中则返回 None，放行进入 S2。"""
    hits: list[str] = []
    for text in symptoms:
        hits.extend(_scan(text, honor_negation=True))
    if not hits:
        return None
    return veto_message("、".join(dict.fromkeys(hits)))  # 去重且保持命中顺序


def veto_message(matched: str) -> str:
    """拒绝辨证的文案，**全项目只在这里拼一次**。之前 check_safety、chain.py 的
    ReAct ask_user 路径、followup.py 的十问歌兜底各拼了一份一字不差的字符串——
    改措辞时必然漏改一处，而这三处恰恰是 CLAUDE.md 点名不能分叉的安全后门。"""
    return (
        f"检测到危重症状信号（{matched}），本 demo 不适用于此类情况，"
        "请立即就医或拨打急救电话，本次不提供辨证结果。"
    )


def danger_confirmed_by_answer(
    question: str, answer_verdict: str, symptom: str | None = None
) -> str | None:
    """追问的第二道门：**问的本身是危重症状、患者没有明确否认** → 返回拒绝理由，
    否则 None。回答原文里往往没有危重词（「有没有便血？」→「有」），check_safety
    单独看回答是放行的，这条判据补的就是这个缺口。

    只有明确否认（answer_verdict == "no"）才放行。yes 固然要拦，**unknown 也要拦**：
    「时有时无」「拉过两次」既不是否认也不构成排除，按危重处理是安全侧该有的
    非对称——漏拦一次的代价远大于多拦一次。

    symptom 是提问方知道的候选症状名（G3 追问带着它，ReAct 的 ask_user 只有问题
    文本）。给了就先看它，再看问题原文——两个都看是取并集，比任一单独看都保守。
    这条判据此前在 core/chain.py 和 core/followup.py 各写了一套，两边看的文本
    不同、否定语义也不同（一边 honor_negation=False 一边 True）——正是 CLAUDE.md
    "同一个判断两处实现、各自测都对、放进同一条链才看出矛盾"那堵墙。
    """
    asked = (mentions_danger(symptom) if symptom else None) or mentions_danger(question)
    if asked and answer_verdict != "no":
        return veto_message(asked)
    return None
