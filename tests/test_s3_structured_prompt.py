"""R33 `prompts/v1/s3_structured.yaml`：模板本身的硬约束。

这个文件测的是**prompt 文件的内容**，不是模型的行为——后者要真实 LLM，归 eval/。
测它的理由跟 `tests/test_llm_backend.py` 里那批 s3_syndrome 的测试一样：
prompt 是这条链上唯一"改了不会报错"的东西，写死的约束（五步链、引 id 不引中文名、
span 照抄）掉了一条，表现只是产出质量下降，没有任何测试会红。
"""
from __future__ import annotations

import re

import pytest

from core.llm import PROMPTS_ROOT, load_prompt, render

PROMPT_NAME = "s3_structured"
PLACEHOLDERS = {"physicians", "physician_ids", "elements_summary", "symptoms", "refs"}


@pytest.fixture(scope="module")
def prompt() -> dict:
    return load_prompt(PROMPT_NAME)


def test_the_file_exists_and_has_system_and_notes(prompt):
    assert set(prompt) == {"system", "notes"}
    assert prompt["system"].strip() and prompt["notes"].strip()


def test_the_placeholders_are_exactly_the_five(prompt):
    found = set(re.findall(r"(?<!\$)\$\{?([A-Za-z_]\w*)\}?", prompt["system"]))
    assert found == PLACEHOLDERS


def test_render_needs_all_five_and_rejects_a_missing_one(prompt):
    """`render` 缺变量直接报错（core/llm.py 那条）——占位符原样留在 prompt 里、
    模型看到一个字面的 `$refs`、测试照样全绿，是这个项目防过的一种静默 bug。"""
    kwargs = {k: f"<{k}>" for k in PLACEHOLDERS}
    out = render(prompt["system"], **kwargs)
    for k in PLACEHOLDERS:
        assert f"<{k}>" in out
    assert "$" not in out.replace("$$", ""), "渲染后不该剩下任何占位符"

    for drop in sorted(PLACEHOLDERS):
        with pytest.raises(KeyError) as e:
            render(prompt["system"], **{k: "x" for k in PLACEHOLDERS if k != drop})
        assert drop in str(e.value)


def test_it_uses_string_template_syntax_not_str_format(prompt):
    """CLAUDE.md 已知的坑：prompt 里含 JSON 示例的花括号，`.format()` 会抛异常。
    这条直接验：拿 `.format()` 跑一定失败，拿 `string.Template` 跑一定成功。"""
    with pytest.raises((KeyError, IndexError, ValueError)):
        prompt["system"].format(**{k: "x" for k in PLACEHOLDERS})
    assert "{" in prompt["system"], "示例 JSON 的花括号还在，上面那条才有意义"


def test_the_five_steps_are_all_named_in_order(prompt):
    """五步链条来自申报书 2.1。顺序在 prompt 里也要是这个顺序——
    写乱了模型会按写的顺序推。"""
    sys_text = prompt["system"]
    order = ["organs（病变脏腑）", "syndrome（证型）", "method（治法）",
             "formula（方剂）", "herb_choices（药物组成）"]
    positions = [sys_text.index(x) for x in order]
    assert positions == sorted(positions), f"五步在 prompt 里的次序乱了：{positions}"


def test_it_demands_verbatim_quoting_between_steps(prompt):
    """跳步校验在 schema 层，但 prompt 要把它讲清楚——讲清楚是为了第一次就尽量对，
    不是为了当判据（两次重试有限）。"""
    s = prompt["system"]
    assert "逐字等于" in s
    assert "上述证型" in s, "要明确点名这种指代写法不行"


def test_it_demands_one_formula_not_a_candidate_list(prompt):
    s = prompt["system"]
    assert "一张方" in s
    assert "不是候选列表" in s


def test_it_tells_the_model_to_fill_ids_not_chinese_names(prompt):
    """SOURCES.md 第 31 条那个坑：模型只看到中文名就填中文名，而过滤用 id，
    医案层工具恒返回空、9 次调用全空。"""
    s = prompt["system"]
    assert "填 id，不要填中文名" in s
    assert "$physician_ids" in s


def test_the_prompt_does_not_hardcode_the_physician_id_list(prompt):
    """id 清单只能由 `$physician_ids`（= `physician_choices_text()`）填进来。
    prompt 里写死一份的话，注册表加第六位医家时那份副本不会跟着长，
    而模型看不到新 id 就永远不会填它（第 31 条）。"""
    from core.physicians import PHYSICIANS, physician_choices_text

    s = prompt["system"]
    assert "$physician_ids" in s
    for pid in PHYSICIANS:
        assert pid not in s, f"prompt 里写死了医家 id「{pid}」，应该走 $physician_ids"
    text = physician_choices_text()
    assert "ye_tianshi(叶天士)" in text and "li_ke(李可)" in text


def test_run_synthesis_feeds_the_prompt_from_the_registry():
    import inspect

    from core.chain import run_synthesis

    src = inspect.getsource(run_synthesis)
    assert 'load_prompt("s3_structured")' in src
    assert "physician_ids=physician_choices_text()" in src
    assert 'info["name"] for info in roster.values()' in src, "五位中文名也从注册表取"


def test_it_requires_a_case_id_for_every_physician_influence(prompt):
    s = prompt["system"]
    assert "说不出医案就不要写这一条" in s
    assert "宁可只写两家真的影响了你的，不要凑五家" in s, (
        "硬约束只能保证「引了 id」，凑数这件事要靠明确许可「可以少写」来减少"
    )


def test_it_forbids_inventing_an_ontology_span(prompt):
    s = prompt["system"]
    assert "照抄" in s
    assert "不要编一条 ref" in s
    assert "留空数组是允许的" in s, "允许留空才不会逼模型编"


def test_the_example_is_a_placeholder_skeleton_not_a_real_case(prompt):
    """V5 P0 的教训：完整病例示例会教会模型「这种情况开这个方」而不是
    「输出应该长这个形状」，而且当参考医案信息量低时示例会赢过参考医案。"""
    s = prompt["system"]
    assert "只示范字段结构与嵌套关系，不是内容示范" in s
    # 示例里不许出现真实方名/药名/证型
    for real in ("四君子汤", "小柴胡汤", "柴胡疏肝散", "党参", "黄芪", "脾胃气虚证"):
        assert real not in s, f"示例里出现了真实的「{real}」，会把模型带偏"
    for ph in ("脏腑占位", "证型占位", "治法占位", "方名占位", "药名X"):
        assert ph in s, f"占位符 {ph} 不在"


def test_it_keeps_the_safety_critical_decoction_and_dose_rules(prompt):
    """这两条跟 legacy 那份 prompt 是同样的硬要求，不许在新 prompt 里丢掉
    ——丢了模型就不标先煎，而 M2 的煎法检查只能报"没标"，救不回来。"""
    s = prompt["system"]
    assert "先煎" in s and "后下" in s and "包煎" in s
    assert "不确定时填 null，不要猜一个数" in s
    for toxic in ("附子", "川乌", "草乌"):
        assert toxic in s, f"毒性药举例里少了 {toxic}"


def test_it_keeps_the_restraint_on_adjunct_herbs(prompt):
    """R1 加的那四行（ε_online 里相当一部分是无依据的佐使加减）不许在新 prompt 里丢。"""
    s = prompt["system"]
    assert "佐使药只在确有必要时加" in s
    assert "参考医案是给你看这几位医家的思路，不是药材清单" in s


def test_the_notes_explain_why_two_prompts_coexist(prompt):
    n = prompt["notes"]
    assert "s3_syndrome.yaml" in n
    assert "legacy" in n
    assert "不可跳步" in n and "schema" in n


def test_the_legacy_prompt_was_not_touched():
    """§0.6 明确不做：不改 s3_syndrome.yaml。R1~R32 的数字全是在它下面跑出来的。"""
    import subprocess

    r = subprocess.run(["git", "diff", "--name-only", "HEAD", "--",
                        "prompts/v1/s3_syndrome.yaml"],
                       cwd=PROMPTS_ROOT.parent, capture_output=True, text=True)
    assert r.stdout.strip() == "", "s3_syndrome.yaml 被改动了"
