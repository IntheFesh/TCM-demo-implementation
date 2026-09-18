"""R40：**稳定段在前、变化段在后**——前缀缓存的公共前缀只到第一个变化点为止。

## 这一轮量到的问题

`prompts/v1/s3_structured.yaml` 里，每次问诊都变的两行（`$elements_summary`、
`$symptoms`）原来排在 **93%** 处，而全项目最大的一块稳定内容 `$refs`
（`full_context` 模式下是**整个语料**，实测 1060 条 / 1.25 MB 文本）排在 **94%**
——也就是**在变化段之后**。

前缀缓存按**公共前缀**命中。变化段一出现，后面的内容就都不在公共前缀里了，
于是那 1.25 MB 医案文本**每次问诊都要重新处理一遍**，缓存在最大的那一块上
命中率为 0。

R21 的 `core/context_prefix.assemble()` 已经为 legacy 那条路做对过这件事
（实测前缀缓存命中率 0.989），R33 新建 structured 模板时没有把那个次序带过来。
**同一个教训没有传到新入口**——跟 CLAUDE.md 记的那几次是同一形状，
所以这个文件把次序变成机器判据，而不是再依赖"记得把它放最后"。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

#: 每次问诊都变的占位符。**这张表是判据的一半**：漏一个，那个占位符就能
#: 悄悄挪到前面去而测试仍然绿。
VARIABLE_PLACEHOLDERS = ("$elements_summary", "$symptoms")

#: 同一台服务上逐次问诊之间逐字节相同的那些。`$refs` 在 full_context 下
#: 是整个语料，是这里面最大的一块。
STABLE_PLACEHOLDERS = ("$physicians", "$physician_ids", "$refs")


def _system(name: str) -> str:
    doc = yaml.safe_load((ROOT / "prompts" / "v1" / f"{name}.yaml")
                         .read_text(encoding="utf-8"))
    return doc["system"]


def test_the_structured_prompt_has_every_placeholder_the_chain_fills():
    """次序判据的前提：这些占位符真的都在模板里。少一个，下面的比较会拿到 -1
    而 -1 < 任何位置，测试会**假绿**。"""
    sys_t = _system("s3_structured")
    for ph in VARIABLE_PLACEHOLDERS + STABLE_PLACEHOLDERS:
        assert ph in sys_t, f"模板里没有 {ph}"


@pytest.mark.parametrize("stable", STABLE_PLACEHOLDERS)
@pytest.mark.parametrize("variable", VARIABLE_PLACEHOLDERS)
def test_every_stable_block_comes_before_every_variable_block(stable, variable):
    """**这是这个文件的核心判据。** 任何一个稳定段排到变化段后面，
    它就掉出公共前缀了。"""
    sys_t = _system("s3_structured")
    assert sys_t.index(stable) < sys_t.index(variable), (
        f"{stable} 排在 {variable} 之后 → 它掉出前缀缓存的公共前缀了")


def test_the_biggest_stable_block_is_refs():
    """`$refs` 在 full_context 下是整个语料。它排在哪里决定了缓存有没有意义。"""
    sys_t = _system("s3_structured")
    assert sys_t.index("$refs") < sys_t.index("$symptoms")


def test_the_variable_section_is_at_the_very_end():
    """变化段之后不该再有任何内容——后面每加一段，公共前缀就少一段。
    留 5% 的余量给收尾的一两行说明。"""
    sys_t = _system("s3_structured")
    last = max(sys_t.index(ph) for ph in VARIABLE_PLACEHOLDERS)
    assert last / len(sys_t) > 0.90, (
        f"最后一个变化占位符在 {last / len(sys_t):.0%} 处，后面还有 "
        f"{len(sys_t) - last} 字符的内容")


def test_the_reason_is_written_down_in_the_prompt_file_but_not_sent_to_the_model():
    """次序是个**看不出来**的约束：模板读起来两种排法都通顺，所以理由必须
    写在文件里，不然下一个人重排它不会觉得自己改了什么。

    **写在 `notes` 而不是 `system`**：那段话是给改这份文件的人看的，
    送给模型只是白花 input token，还会让模型去揣测我们的工程约束。"""
    doc = yaml.safe_load((ROOT / "prompts" / "v1" / "s3_structured.yaml")
                         .read_text(encoding="utf-8"))
    assert "前缀缓存" in doc["notes"]
    assert "稳定前缀" in doc["notes"]
    assert "前缀缓存" not in doc["system"], "工程理由不该送进 prompt"


def test_the_legacy_assemble_path_still_puts_this_consultation_last():
    """R21 那条路（legacy full_context，走 `core.context_prefix.assemble`）
    的次序不许被这一轮动到——它的 0.989 命中率是实测过的。"""
    from core.context_prefix import EMIT_ORDER, SECTION_TITLES, SECTION_VARIABLE

    assert EMIT_ORDER[-1] == SECTION_VARIABLE, (
        f"assemble 的最后一段变成了 {SECTION_TITLES[EMIT_ORDER[-1]]}，"
        "本次问诊不再排在最后 → 前缀缓存失效")


def test_both_paths_agree_on_the_principle():
    """两条路径（legacy assemble / structured 模板）对"本次问诊排最后"
    这件事必须给出同一个答案。**不是同一份实现，但必须是同一条规则**
    ——所以这条测试同时查两边，而不是只查其中一边。"""
    from core.context_prefix import EMIT_ORDER, SECTION_VARIABLE

    sys_t = _system("s3_structured")
    structured_ok = (sys_t.index("$refs") < sys_t.index("$symptoms"))
    legacy_ok = EMIT_ORDER[-1] == SECTION_VARIABLE
    assert structured_ok and legacy_ok, (
        f"structured={structured_ok}, legacy={legacy_ok}：两条路不一致")


def test_the_structured_prompt_still_renders(monkeypatch):
    """重排不能把模板弄坏。`string.Template`（**不是** `.format()`，
    prompt 里有 JSON 示例的花括号）渲染一遍，所有占位符都被替换掉。"""
    from core.llm import load_prompt, render

    out = render(load_prompt("s3_structured")["system"],
                 physicians="叶天士、吴鞠通", physician_ids="ye_tianshi / wu_jutong",
                 elements_summary="脾 气虚", symptoms="纳差、腹胀",
                 refs="（医案块）")
    assert "$" not in out.replace("$$", ""), "还有没替换的占位符"
    assert out.index("（医案块）") < out.index("纳差、腹胀")


def test_the_citation_requirement_still_points_the_right_direction():
    """引用要求里写的是"上面参考医案"。重排之后 refs 仍然在它上面
    ——如果反了，那句话就成了假指路。"""
    sys_t = _system("s3_structured")
    assert sys_t.index("$refs") < sys_t.index("## 引用要求")
