"""R54 第三相：医案佐证。**绝不回头改推导**——这一相只读演绎推导（R52）经
符号验证（R53）定型之后的结论，去检索全部医家的医案库，看"历史上有没有
名医这么治过"，把结果作为独立的事后佐证附加上去，不修改、不重新生成任何
一步。

## 为什么这一相绝不能改推导

R51-R58 这轮改造的核心是把"先检索医案再模仿"倒过来，变成"先按医理演绎
推导，医案退到最后当佐证"（R52/R53）。如果这一相被允许反过来影响前两相的
结论——哪怕只是"发现医案不支持就换一个证型再试"——整个改造就名存实亡，
退回成原来的检索-模仿。所以 `corroborate()` 的函数签名上就没有"改推导"这
条路：只读 `s3`，返回一份独立的 `CorroborationResult`，不返回、也不修改
传入的那份推导结论。`tests/test_corroboration.py` 用 sha256 逐字节比对调用
前后的 `model_dump()`，把这条不变式钉死成一条能被机器验证的约束，不是一句
文档承诺。

## 四个结论桶

- `concordant`：查到的医案里，用药方向跟这次推导一致（集合 Jaccard 距离
  ≤ `CONCORDANT_MAX_DISTANCE`——CLAUDE.md 那条"改用药物集合的 Jaccard 距离"：
  不拿证型字符串相不相等去判，那会把"肝胃不和证"和"肝胃不和"判成分歧）
- `divergent`：查到了医案，但用药方向不一致
- `no_precedent`：这位医家的医案库里一条相关医案都没查到
- `physicians_with_precedent`：在 `concordant`/`divergent` 任一桶里出现过的
  医家 id（"有没有查到"，不分方向）——跟 R33 起的 `physician_influences`
  概念不是一回事：那个字段说的是"检索到的医案影响了推导过程"（检索发生在
  推导**之前**）；这里说的是"推导定型之后，历史上有没有这么治过"（检索发生
  在推导**之后**），两者顺序相反，所以不共用一个字段名，也不共用一处实现。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from core.herbs import normalized_herb_set
from core.physicians import PHYSICIANS
from core.schemas import S1Normalize, S2Elements
from core.setstats import jaccard_distance

#: R57 消融实验的开关：A/B/C 三组关掉这一相，只有 D 组（最终形态）开着——
#: 分组定义唯一出处见 `eval/ablation/spec.py`（这里曾经写过一条相反的注释，
#: 跟那份定义矛盾，已改对；不要在这两处分别维护同一件事）。默认 on——
#: 佐证是产品要交付的真实能力，不是一个需要显式开启的实验特性；off 是消融
#: 实验专用的降级路径，不是产品默认。
CORROBORATION_ENV = "CORROBORATION"
CORROBORATION_DEFAULT = "on"

#: 用药集合 Jaccard 距离 ≤ 这个阈值算"方向一致"。医案原方（古人手写、
#: 一次性）跟这次结构化五步链拟出的方本来就不会逐味相同，阈值定得偏宽松
#: （至少 30% 重叠）——真正的判断权交给读者去看 `shared_herbs` 具体是哪几味，
#: 不是靠这一个数字替读者下结论。
CONCORDANT_MAX_DISTANCE = 0.7


def corroboration_enabled() -> bool:
    """跟 `core.llm.s3_mode()` 同一条纪律：拼错就抛，不静默按默认处理——
    这个开关决定这一相跑不跑，跑不跑直接决定返回值里有没有佐证数据。"""
    raw = (os.environ.get(CORROBORATION_ENV) or CORROBORATION_DEFAULT).strip().lower()
    if raw in ("on", "1", "true"):
        return True
    if raw in ("off", "0", "false"):
        return False
    raise ValueError(
        f"{CORROBORATION_ENV}={raw!r} 不认识，只能是 on/off。"
    )


@dataclass(frozen=True)
class PrecedentCase:
    """一条查到的历史医案，跟这次推导的方比对之后的结果。"""

    case_id: str
    physician: str
    score: float
    herb_distance: float
    shared_herbs: tuple[str, ...]
    syndrome: str | None

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id, "physician": self.physician, "score": self.score,
            "herb_distance": round(self.herb_distance, 3),
            "shared_herbs": list(self.shared_herbs), "syndrome": self.syndrome,
        }


@dataclass(frozen=True)
class CorroborationResult:
    enabled: bool
    concordant: tuple[PrecedentCase, ...] = ()
    divergent: tuple[PrecedentCase, ...] = ()
    no_precedent: tuple[str, ...] = ()
    physicians_with_precedent: tuple[str, ...] = ()
    note: str | None = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "concordant": [c.to_dict() for c in self.concordant],
            "divergent": [c.to_dict() for c in self.divergent],
            "no_precedent": list(self.no_precedent),
            "physicians_with_precedent": list(self.physicians_with_precedent),
            "note": self.note,
        }


def corroborate(s3, s1: S1Normalize, s2: S2Elements, *,
                retriever_mode: str | None = None) -> CorroborationResult:
    """拿演绎推导定型的结论（`s3`：`S3Derived` 或任何有
    `formula.candidate.herb_items` 的同形状对象）去检索全部医家的医案库。

    **只读 `s3`，不修改它**——本函数体内没有任何一处给 `s3` 的字段赋值。
    调用方（`core/chain.py::run_derivation`）在验证闭环（R53）跑完、`s3`
    已经定型之后才调用这里，顺序本身就是"绝不回头改推导"的第一道保证；
    `tests/test_corroboration.py` 的 sha256 比对是第二道、机器可验证的保证。
    """
    if not corroboration_enabled():
        return CorroborationResult(
            enabled=False,
            note="CORROBORATION=off，这一相没有跑（R57 消融实验的 A/B/C 组用）。")

    from core.chain import _search_cases  # 延迟 import 破循环：chain 也要 import 这个模块

    formula_herbs = normalized_herb_set(
        [i.name for i in s3.formula.candidate.herb_items])
    symptoms_text = "；".join(s1.symptoms)
    query = f"{symptoms_text}。舌{s1.tongue or '未记'}，脉{s1.pulse or '未记'}"

    concordant: list[PrecedentCase] = []
    divergent: list[PrecedentCase] = []
    no_precedent: list[str] = []

    for pid in PHYSICIANS:
        hits, _low = _search_cases(query, pid, s2, retriever_mode)
        if not hits:
            no_precedent.append(pid)
            continue
        for case, score in hits:
            case_herbs = normalized_herb_set(case.herbs)
            dist = jaccard_distance(formula_herbs, case_herbs)
            row = PrecedentCase(
                case_id=case.case_id, physician=pid, score=round(score, 3),
                herb_distance=dist, shared_herbs=tuple(sorted(formula_herbs & case_herbs)),
                syndrome=case.syndrome)
            (concordant if dist <= CONCORDANT_MAX_DISTANCE else divergent).append(row)

    physicians_with_precedent = tuple(dict.fromkeys(
        c.physician for c in (*concordant, *divergent)))

    return CorroborationResult(
        enabled=True, concordant=tuple(concordant), divergent=tuple(divergent),
        no_precedent=tuple(no_precedent),
        physicians_with_precedent=physicians_with_precedent,
    )
