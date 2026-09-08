"""M8：处方导出用的两个纯函数——比对模型建议方跟医生最终定方的差异
（compute_herb_diffs），以及把最终定方排成药房能直接照单抓药的文本
（format_pharmacy_text）。

这两个都是"格式化/比对内容"，不碰哈希链的完整性逻辑，所以没有放进
core/audit.py——core/audit.py 只管审计记录本身防不防篡改这一件事，
这个文件管的是另一件事（这两版方子哪里不一样、最终版长什么样），两者
概念上不是同一个问题，分开放（CLAUDE.md「同一概念只能有一处实现」的
反面：不是同一个概念，不需要挤进同一个文件）。
"""
from __future__ import annotations

import unicodedata

from core.schemas import FormulaCandidate


def compute_herb_diffs(model_suggestion: FormulaCandidate, final: FormulaCandidate) -> list[str]:
    """比对模型建议方和医生最终定方的 herb_items，生成人类可读的差异描述。

    按药名配对（同名视为"同一味药"），不是按列表下标配对——医生在中间
    插入/删除一味药会让后面所有下标错位，按下标比会把"插入一味药"误判成
    "后面每一味药都变了"，按名字配对不受插入/删除位置影响。

    五类描述格式（任务描述原文给的例子：["附子 10g→15g", "去 甘草",
    "加 海藻 15g"]）：
      去 X            —— 原方有、定方没有
      加 X 剂量单位     —— 原方没有、定方有
      X 旧剂量→新剂量   —— 剂量（或剂量单位）变了
      X 炮制 旧→新      —— 炮制变了
      X 煎法 旧→新      —— 煎法变了
    同一味药可能同时命中好几条（既改了剂量又改了炮制），各自独立生成一条
    ——审计要的是"逐项能对上"，合并成一句会丢信息，没法从一条 diff 反推
    出"具体是哪个字段变了"。
    """
    old_by_name = {item.name: item for item in model_suggestion.herb_items}
    new_by_name = {item.name: item for item in final.herb_items}

    diffs: list[str] = []
    for name, old_item in old_by_name.items():
        if name not in new_by_name:
            diffs.append(f"去 {name}")

    for name, new_item in new_by_name.items():
        if name not in old_by_name:
            dose_text = f"{new_item.dose:g}{new_item.dose_unit}" if new_item.dose is not None else ""
            diffs.append(f"加 {name}" + (f" {dose_text}" if dose_text else ""))

    for name, old_item in old_by_name.items():
        if name not in new_by_name:
            continue
        new_item = new_by_name[name]
        old_dose = f"{old_item.dose:g}{old_item.dose_unit}" if old_item.dose is not None else "未标注"
        new_dose = f"{new_item.dose:g}{new_item.dose_unit}" if new_item.dose is not None else "未标注"
        if (old_item.dose, old_item.dose_unit) != (new_item.dose, new_item.dose_unit):
            diffs.append(f"{name} {old_dose}→{new_dose}")
        if old_item.processing != new_item.processing:
            diffs.append(f"{name} 炮制 {old_item.processing or '无'}→{new_item.processing or '无'}")
        if old_item.decoction != new_item.decoction:
            diffs.append(f"{name} 煎法 {old_item.decoction or '无'}→{new_item.decoction or '无'}")

    return diffs


def _display_width(s: str) -> int:
    """东亚宽字符（中文/全角）按 2 列宽算，其余按 1 列——药房格式文本要让
    中文药名对齐，用 len(s) 算宽度会把"瓜蒌"（2 个宽字符，视觉宽度 4）
    和"甘草片"（3 个宽字符，视觉宽度 6）按同样的"字符数"补空格，实际
    显示宽度对不齐。"""
    width = 0
    for ch in s:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad_display(s: str, target_width: int) -> str:
    return s + " " * max(0, target_width - _display_width(s))


def format_pharmacy_text(formula: FormulaCandidate) -> str:
    """把最终定方排成药房格式文本——任务描述原文给的样例：

        瓜蒌薤白半夏汤加减                    7 剂
          瓜蒌      15g
          薤白       9g
          半夏       9g   姜制  先煎
          ...
        用法：水煎服，每日1剂，分2次温服

    doses_count/usage 缺失时对应那部分直接不出现（不编一个"7 剂"或
    "水煎服"出来——这是要给药房照单抓药的文本，编造用法/剂数比留白更危险）。
    """
    lines: list[str] = []

    header = formula.name
    if formula.doses_count is not None:
        dose_text = f"{formula.doses_count} 剂"
        pad = max(1, 40 - _display_width(header) - len(dose_text))
        header += " " * pad + dose_text
    lines.append(header)

    name_width = max((_display_width(item.name) for item in formula.herb_items), default=0)
    for item in formula.herb_items:
        dose_text = f"{item.dose:g}{item.dose_unit}" if item.dose is not None else ""
        row = "  " + _pad_display(item.name, name_width + 2) + dose_text.rjust(6)
        if item.processing:
            row += f"   {item.processing}"
        if item.decoction:
            row += f"  {item.decoction}"
        lines.append(row)

    if formula.usage:
        lines.append(f"用法：{formula.usage}")

    return "\n".join(lines)
