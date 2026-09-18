"""R62 §3.3：三个**轻量**能力——问诊要点提示、编辑助手、组方检验。

这三件事跟问诊主链（S1→S2→S3）不是一回事，所以不放进 `core/chain.py`：

| | 主链 | 这三个 |
|---|---|---|
| 什么时候跑 | 医师点「开始辨证」 | 医师打字停了、改了一味药、按了「检验组方」 |
| 跑多久 | 45 秒可以接受 | 3/5/8 秒，超了就等于没有 |
| 失败了怎么办 | 整次问诊失败 | **静默降级**，主界面照常用 |

最后一行是这三个能力的设计核心。**它们全部是"锦上添花"**：编辑助手超时，
医师照样能改方（第一层规则核查是零 LLM 的，`core/formula_check.py` 那一层
永远在）；组方检验超时，方还在表里。所以每一个都有明确的超时上限，
超了返回一份**说明自己超时了**的结果，不抛异常、不让界面红。

## 问诊要点提示为什么是零 LLM

§3.3 的表把它列成一个 `thinking=disabled, effort=low` 的模型能力，目标 3 秒。
但这个判断**这个项目已经做过了**——`core/tools.py::question_candidates` 按
信息增益算"接下来最该问什么"，用的是证候图上的 indicates 边，每条候选自带
"答'有'最可能是哪个证、答'没有'最可能是哪个证"。那正是 §5.4 那个例子里
破折号后面那半句（「区分肝胃气滞与肝胃郁热」）。

CLAUDE.md 第 31 条的判据是"**这个判断此前有没有人做过**"，不是"我这个实现
有没有 bug"。做过了，所以复用，不另开一条模型路径。实测 0.18 秒（下面
`intake_hints` 的注释里有测法），比 3 秒的预算快一个数量级，而且是确定性的
——同一段主诉两次得到同一组提示，不会这次问口苦、下次问大便。

这不是降级：换成让模型凭记忆想"还该问什么"，每条提示背后就没有可追溯的
依据了，而"每一步注明所依据的医理规则与教材原文"是这个产品的核心主张。

## 另外两个为什么必须是模型

编辑助手要评价的是"**这一次改动**"——医师加了黄连，而本例无热象、方中又有
生姜。这是一段需要把患者情况、证型治法、方中已有药味、这一味药的药性放在
一起说的话，规则层给不出（规则层能说"寒热相悖"这四个字，说不出"恐减弱疏肝
之力，若兼有郁热可保留并减生姜至 3g"）。组方检验的君臣佐使分析同理。

所以这两个是模型调用，但都**关思考、低努力**：它们不是重新辨一次证，是就
着已经定下来的证型治法评价一次局部改动（跟 `core/chain.py::_verify_and_revise`
重开时关思考是同一条理由）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.llm import get_llm, load_prompt, render
from core.ontology import parse_effects
from core.schemas import ComposeAnalysis, EditAdvice, HerbItem
from core.tools import question_candidates

# ---------- 时间预算（§11 那张表） ----------
#
# **一处定义**：API 层的超时、前端的等待提示、测试里的断言都问这里。
# 散成三份的话调一处漏两处，而漏掉的那两处会继续按旧数等。
HINTS_BUDGET_S = 3.0
ADVICE_BUDGET_S = 5.0
COMPOSE_BUDGET_S = 8.0

#: 问诊要点最多给几条。§5.4 原文「≤5 条」。再多医师不会看，
#: 而且候选列表越长，排在后面的那几条信息增益已经很低了。
HINTS_K = 5

#: 编辑助手给几条可选处置。§7.3「2–3 条」——**不是越多越好**：
#: 每条都要能一键采纳写回表格，给五条等于把决定重新推回给医师。
ADVICE_OPTIONS_MIN = 2
ADVICE_OPTIONS_MAX = 3


@dataclass(frozen=True)
class AssistResult:
    """三个能力共用的返回外壳。

    **`timed_out` / `error` 跟"结果为空"必须分得开**（CLAUDE.md：工具返回空
    必须能区分三种情况）。界面上三者长得可以一样（右栏没内容），含义却完全
    不同：超时是"等会儿再试"，出错是"这台机器有问题"，空是"这次改动没什么
    可说的"。合成一种的话，一个配错了 API key 的部署会一直显示"没什么可说的"。
    """

    ok: bool
    data: dict = field(default_factory=dict)
    elapsed_s: float = 0.0
    timed_out: bool = False
    error: str | None = None
    #: 这一次用没用模型。零 LLM 的那条路径要能被界面和测试认出来。
    used_llm: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "elapsed_s": round(self.elapsed_s, 3),
            "timed_out": self.timed_out, "error": self.error,
            "used_llm": self.used_llm, **self.data,
        }


# ---------- 1. 问诊要点提示（零 LLM） ----------


def _why_ask(candidate: dict) -> str:
    """这条问题为什么值得问。

    三种情况分开说，**不要合成一句模板**：
      - 危重相关：它不是拿来分辨证型的，是拿来决定要不要先拦住整次问诊的
        （CLAUDE.md 改造期约定：追问是安全否决层的后门）。说成"区分 X 与 Y"
        会把一条安全问题伪装成鉴别问题。
      - 两个分叉不同：这才是真正的鉴别问题，照 §5.4 的样子说。
      - 两个分叉相同：这个问题只是在加强同一个结论，如实说，不要硬凑一个
        "区分 X 与 X"出来（那句话读起来像系统坏了）。
    """
    if candidate.get("safety_relevant"):
        return "危重征象——答「有」需先过安全判断，不直接进辨证"
    yes, no = candidate.get("if_yes_top"), candidate.get("if_no_top")
    if yes and no and yes != no:
        return f"区分{yes}与{no}"
    if yes:
        return f"答「有」会加强{yes}这一判断"
    return candidate.get("fallback_reason") or "十问歌固定顺序"


def intake_hints(text: str, *, k: int = HINTS_K, asked: list[str] | None = None) -> AssistResult:
    """主诉文本 → 还该问什么（≤k 条，每条带"为什么问"）。**零 LLM**。

    实测 0.18 秒（`question_candidates` 在 1087 个症状候选上算一遍信息增益，
    机器：本轮开发沙盒）。预算 3 秒，所以不做任何缓存——缓存会引入"改了主诉
    但提示没变"这一类问题，换来的那 0.18 秒没人感觉得到。

    切句复用 `core.ontology.parse_effects`：它就是这个项目里"把一段中文按
    顿号逗号切成短语、丢掉单字碎片"的那一处实现（R61 修
    `check_effect_matches_method` 时已经把治法复句交给它切过一次）。
    在这里另写一个 `text.split("，")` 就是同一件事的第三份实现。

    切出来的短语同时当 `known_symptoms`（从候选池里去掉，别再问一遍）和
    `asserted_symptoms`（进后验，让 IG 排在真正还没问的鉴别点上）。两者传
    同一份不是偷懒：患者自己说出来的症状，本来就既"已经知道了"又"是肯定的"。
    """
    t0 = time.monotonic()
    clauses = list(parse_effects([text or ""]))
    if not clauses:
        return AssistResult(
            ok=True, elapsed_s=time.monotonic() - t0,
            data={"hints": [], "clauses": [],
                  "note": "还没有可用的主诉文字——写下患者说的症状，这里会给出接着该问什么。"},
        )
    try:
        candidates = question_candidates(
            [], k=k, known_symptoms=clauses, asserted_symptoms=clauses,
            asked=list(asked or []),
        )
    except Exception as e:                      # noqa: BLE001 —— 见下
        # 图谱文件损坏/缺失这类问题不该让输入框旁边的提示条把整个页面带红：
        # 这一条是"锦上添花"，主诉照样能提交、辨证照样能跑。
        return AssistResult(ok=False, elapsed_s=time.monotonic() - t0,
                            error=f"问诊要点提示不可用：{e}")
    hints = [{
        "ask": c.get("question", ""),
        "symptom": c.get("symptom", ""),
        "why": _why_ask(c),
        "safety_relevant": bool(c.get("safety_relevant")),
        # 信息增益如实带出来但**不在产品面上显示**（§11.4：不显示 token 数、
        # 帧数、样本数这类内部读数）。留着是给内部模式与测试用的。
        "information_gain": c.get("information_gain"),
        "source": c.get("source"),
    } for c in candidates]
    return AssistResult(
        ok=True, elapsed_s=time.monotonic() - t0,
        data={"hints": hints, "clauses": clauses,
              "note": "" if hints else "当前主诉已经问得比较全，暂时没有更能区分证型的问题。"},
    )


# ---------- 公用：两个模型能力的调用外壳 ----------


def _herb_line(it: HerbItem | dict) -> str:
    """一味药排成一行给模型看。dict 与 HerbItem 都认——前端传来的是 dict，
    链路里传来的是模型对象，在调用点各转一次的话就有了两份写法。"""
    d = it if isinstance(it, dict) else it.model_dump()
    parts = [str(d.get("name") or "")]
    if d.get("dose") is not None:
        parts.append(f"{d['dose']:g}{d.get('dose_unit') or 'g'}")
    for key in ("processing", "decoction", "role"):
        if d.get(key):
            parts.append(str(d[key]))
    if d.get("function_in_formula"):
        parts.append(f"（{d['function_in_formula']}）")
    return " ".join(p for p in parts if p)


def _herbs_text(items) -> str:
    return "\n".join(f"- {_herb_line(i)}" for i in (items or [])) or "（方中还没有药）"


def _profile_text(profile: dict | None) -> str:
    """患者概况排成给模型看的几行。**空字段整项不出现**，不写"未记"
    （跟 `core/intake.py::form_to_text` 同一条纪律：满篇"未记"会把模型
    的注意力带到它不该关心的地方）。"""
    p = profile or {}
    rows = [
        ("年龄", f"{p['age_years']} 岁" if p.get("age_years") is not None else ""),
        ("性别", p.get("sex") or ""),
        ("生理阶段", p.get("life_stage") or ""),
        ("体质", p.get("constitution") or ""),
        ("基础病", "、".join(p.get("comorbidities") or [])),
        ("过敏史", "、".join(p.get("allergies") or [])),
        ("在服西药", "、".join(p.get("current_medications") or [])),
    ]
    lines = [f"{k}：{v}" for k, v in rows if v]
    return "\n".join(lines) or "（未填写患者概况）"


def _run_light_llm(prompt_name: str, schema, budget_s: float, **fields) -> AssistResult:
    """两个模型能力共用的调用外壳：关思考、低努力、超时不抛。

    **为什么统一在这里关思考**：这两件事都是"就着已经定下来的证型治法评价
    一次局部改动"，不是重新辨一次证。开思考在这一步是纯浪费，而且更容易把
    已经对的部分重新想一遍想坏——跟 `_verify_and_revise` 重开时关思考是
    同一条理由、同一份实测依据。

    **超时之后返回 `timed_out=True` 而不是抛**：调用方是界面上的一个小面板，
    它超时了主界面必须照常能用（模块文档字符串里那张表的最后一行）。
    这里量的是**墙钟**，不是给后端下一个硬超时——`core/llm.py` 自己有分相
    超时与墙钟兜底（`LLM_TIMEOUT_SECONDS`），在这里再下一层会变成两处各管
    一半、谁先触发说不清。这里的预算只决定"要不要把这次结果当成迟到的"。
    """
    t0 = time.monotonic()
    prompt = load_prompt(prompt_name)
    system = render(prompt["system"], **fields)
    try:
        out = get_llm().generate(
            system=system, user="", schema=schema,
            thinking="disabled", reasoning_effort="low",
        )
    except Exception as e:                      # noqa: BLE001
        elapsed = time.monotonic() - t0
        return AssistResult(ok=False, elapsed_s=elapsed, used_llm=True,
                            timed_out=elapsed >= budget_s,
                            error=f"{type(e).__name__}: {e}")
    elapsed = time.monotonic() - t0
    return AssistResult(ok=True, data=out.model_dump(), elapsed_s=elapsed,
                        used_llm=True, timed_out=elapsed >= budget_s)


# ---------- 2. 编辑助手（§7.3 第二层） ----------


def edit_advice(*, herb_items, diff: list[str], syndrome: str = "",
                principle: str = "", profile: dict | None = None,
                preferences_text: str = "", violations: list[dict] | None = None,
                budget_s: float = ADVICE_BUDGET_S) -> AssistResult:
    """医师改了方之后的那条 AI 提示。**只评价这次改动，不重写整张方**。

    `diff` 由 `core/prescription.py::compute_herb_diffs` 算出来（"加 黄连 6g"
    这种人类可读的一行），**不在这里另写一个比对器**——那个函数已经处理了
    "按药名配对而不是按下标配对"这个坑。

    `violations` 是第一层规则核查（`core/formula_check.py`）已经报出来的红条。
    传进来是为了让模型**优先解释那一条并给替代药**（§7.3 规格最后一句），
    而不是重新发现一遍——规则层已经判过的事，让模型再判一次只会出现两种
    说法不一致的情况。
    """
    if not diff:
        # 没有改动就没有"这次改动"可评价。返回 ok 而不是 error：
        # 界面上这是"没什么可说的"，不是"出问题了"。
        return AssistResult(ok=True, data={"comment": "", "options": [],
                                           "note": "本次没有检测到处方改动。"})
    return _run_light_llm(
        "assist_edit", EditAdvice, budget_s,
        syndrome=syndrome or "（本次未给出证型）",
        principle=principle or "（本次未给出治法）",
        profile=_profile_text(profile),
        herbs=_herbs_text(herb_items),
        diff="\n".join(f"- {d}" for d in diff),
        violations=_violations_text(violations),
        preferences=preferences_text or "（这位使用者还没有设置用药习惯）",
        n_min=ADVICE_OPTIONS_MIN, n_max=ADVICE_OPTIONS_MAX,
    )


def _violations_text(violations: list[dict] | None) -> str:
    """规则层已经报出来的问题排成给模型看的几行。

    刻意带上"**这几条是规则层已经判定的，不要重新判断**"这句：不带的话
    模型会把它们当成"供参考的线索"再评估一遍，然后给出一句跟红条不一致的
    结论——而界面上这两段是上下挨着的。
    """
    rows = violations or []
    if not rows:
        return "（规则核查这一层没有报出问题）"
    lines = [f"- [{v.get('severity') or v.get('kind') or ''}] {v.get('reason') or v.get('message') or ''}"
             for v in rows]
    return ("以下是规则层**已经判定**的问题，不要重新判断它们成不成立，"
            "直接针对它们给替代药：\n" + "\n".join(lines))


# ---------- 3. 组方检验（§9.3） ----------


def compose_verify(*, herb_items, syndrome: str = "", principle: str = "",
                   profile: dict | None = None, preferences_text: str = "",
                   budget_s: float = COMPOSE_BUDGET_S) -> AssistResult:
    """`[检验组方]`：君臣佐使分析 + 治法覆盖 + 缺什么。

    **冲突那一栏不问模型。** §9.3 把"十八反十九畏、寒热相悖、功效重复、
    超剂量、与患者概况的禁忌"跟君臣佐使分析列在同一个按钮下面，但这五条
    全部是已有的确定性规则（`core/formula_check.py` 的五条 + `core/
    individualize.py` 的妊娠/毒峻药/儿童剂量）。这个函数把规则层的结论**原样
    带出去**，同时喂给模型当既定事实，让它在 `gaps` 里针对这些冲突给替代药。

    这样分工有一个可以当场验证的好处：同一张方，规则层的结论是确定的
    ——换个模型、换个温度，红条一条不变。
    """
    rule = _rule_layer(herb_items, syndrome, profile)
    out = _run_light_llm(
        "assist_compose", ComposeAnalysis, budget_s,
        syndrome=syndrome or "（未指定证型）",
        principle=principle or "（未指定治法）",
        profile=_profile_text(profile),
        herbs=_herbs_text(herb_items),
        rule_findings=_violations_text(rule["findings"]),
        preferences=preferences_text or "（这位使用者还没有设置用药习惯）",
    )
    # 规则层的结论**无论模型这一路成不成功都要带出去**：模型超时了，
    # 君臣佐使那一栏空着，但"甘草与海藻同用属十八反"这条红字照样要显示。
    return AssistResult(
        ok=out.ok, elapsed_s=out.elapsed_s, timed_out=out.timed_out,
        error=out.error, used_llm=out.used_llm,
        data={**out.data, **rule},
    )


def _rule_layer(herb_items, syndrome: str, profile: dict | None) -> dict:
    """组方检验里零 LLM 的那一半。**问诊页的 §7.3 第一层走的是同一条路**
    （`core/formula_check.py::check_formula` + `core.individualize`），
    §9.4 原话「释义、核查规则、模板、用药习惯全部复用同一套实现，不另起一份」。
    """
    from core.formula_check import advice_dicts, check_formula
    from core.individualize import individualize
    from core.schemas import PatientProfile

    items = [i if isinstance(i, HerbItem) else HerbItem(**i) for i in (herb_items or [])]
    check = check_formula(syndrome or "", items)
    findings = advice_dicts(check)
    ind = None
    if profile:
        try:
            ind = individualize(PatientProfile(**profile),
                                [i.name for i in items], syndrome).model_dump()
        except Exception as e:                  # noqa: BLE001
            # 患者概况里有认不出的值（前端传了一个不在 Literal 里的体质名）
            # 不该让整个检验失败——如实记一句，其余照常。
            ind = {"items": [], "considered": [], "error": f"患者概况解析失败：{e}"}
    return {
        "rule_findings": findings,
        "rule_skipped": list(check.skipped),
        "individualization": ind,
        # 给 `_violations_text` 用的那一份，跟下发给前端的是同一批数据。
        "findings": findings,
    }
