"""web/index.html 里分歧横幅（R1 分层那一行）的离线测试：用 node 跑 index.html
里真实上线的那份 <script>，跟 tests/test_prescription_frontend.py /
test_herb_grouping.py 同一个模式。

**为什么必须真的跑前端代码**：M5 的教训是后端 JSON 结构测试全绿、前端渲染却漏了
新加的那一层（CLAUDE.md 那条"涉及图层结构变更时 Playwright 是必需的"）。这一轮
没有动 cytoscape 图层（分歧横幅只是一段 textContent），所以用 node 跑真实脚本
就够——但"后端字段对了就以为前端也对"这一步是不能省的。

测的是 divergenceBannerText（纯函数，拼文案），不是 renderDivergence（只负责把
文案塞进 DOM）。R1 把两者拆开就是为了让文案能这么被测到。
"""
import json
import subprocess

from pathlib import Path
from tests.web_harness import DOM_STUB, js_tmp, load_app_js

ROOT = Path(__file__).resolve().parent.parent


# 两位医家、君臣一致佐使全不同——实测形状（叶天士三次重复里君臣骨架全在、
# 变的全是佐使）。三个 ε 都有值，才能判断哪一层超出了自己的地板。
BASE = {
    "same": False,
    "herb_jaccard": 0.6,
    "shared_herbs": ["半夏", "茯苓"],
    "treatment_principle_same": True,
    "core_jaccard": 0.0,
    "adjunct_jaccard": 1.0,
    "shared_core_herbs": ["半夏", "茯苓"],
    "shared_adjunct_herbs": [],
    "n_unroled": {"ye_tianshi": 0, "wu_jutong": 1},
    "epsilon_online": 0.24,
    "epsilon_core": 0.08,
    "epsilon_adjunct": 0.41,
    "pairs": [{"a": "ye_tianshi", "b": "wu_jutong", "name_a": "叶天士", "name_b": "吴鞠通",
               "group": "lineage", "year_gap": 91, "herb_jaccard": 0.6}],
}


def _banner(divergence: dict | None) -> str:
    script = load_app_js()
    tail = ("\nconsole.log(JSON.stringify(divergenceBannerText("
            + json.dumps(divergence, ensure_ascii=False) + ")));")
    proc = subprocess.run(["node", js_tmp(DOM_STUB + script + tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return json.loads(proc.stdout.strip())


def _with(**overrides) -> dict:
    return {**BASE, **overrides}


def test_banner_shows_both_layers_with_their_own_epsilon():
    text = _banner(BASE)
    assert "分层：" in text
    # 每一层跟**自己那一层的** ε 比，不是拿 ε_online 卡三层
    assert "君臣 0（ε_core=0.08，在抖动范围内）" in text
    assert "佐使 1（ε_adjunct=0.41，已超出）" in text
    # 整方那一行照旧用 ε_online
    assert "ε=0.24（已超出噪声范围）" in text


def test_banner_states_the_interpretation_core_agrees_adjunct_differs():
    """分层的全部价值就在这一句：0.6 这个整方分歧里，核心判断是一致的，
    不同的只是加减用药。"""
    assert "核心判断一致、加减用药不同" in _banner(BASE)


def test_banner_interpretation_covers_the_other_three_combinations():
    both = _banner(_with(core_jaccard=0.9, adjunct_jaccard=0.9))
    assert "核心判断与加减用药都有分歧" in both
    core_only = _banner(_with(core_jaccard=0.9, adjunct_jaccard=0.1))
    assert "核心判断有分歧、加减用药一致" in core_only
    neither = _banner(_with(core_jaccard=0.0, adjunct_jaccard=0.1))
    assert "核心判断与加减用药都在抖动范围内" in neither


def test_banner_shows_unroled_counts_with_chinese_names():
    """n_unroled 数据层按 id 存，展示层要中文名（CLAUDE.md「数据文件里存 id，
    展示层用中文名」）——名字从 pairs 里取。"""
    text = _banner(BASE)
    assert "未标注 role：叶天士 0 味　吴鞠通 1 味" in text
    assert "ye_tianshi" not in text


def test_banner_falls_back_to_the_id_when_no_name_is_available():
    """pairs 里查不到名字就原样显示 id，不编一个名字出来。"""
    text = _banner(_with(pairs=[]))
    assert "未标注 role：ye_tianshi 0 味" in text


def test_banner_says_not_applicable_instead_of_zero_when_a_layer_is_null():
    """**null 不能显示成 0。** 0 的意思是"两边完全相同"，null 是"没数据"
    （至少一位医家这一层没标 role）——显示成 0 会凭空报出一个"核心用药完全
    一致"，这正是这一层指标最容易骗人的地方。"""
    text = _banner(_with(core_jaccard=None, shared_core_herbs=[]))
    assert "君臣 不适用（至少一位医家这一层没有标注 role 的药）" in text
    assert "君臣 0" not in text
    # 一层没数据时不给结论（结论要四个数都在）
    assert "核心判断" not in text


def test_banner_says_epsilon_not_measured_when_layer_epsilon_is_missing():
    """跑的是 R1 之前的 epsilon.json（没有分层 ε）时，分层的数照样显示，
    但必须如实标注"此数暂无参照"，不能假装它有对照。"""
    text = _banner(_with(epsilon_core=None, epsilon_adjunct=None))
    assert "君臣 0（ε_core 未测，此数暂无参照）" in text
    assert "佐使 1（ε_adjunct 未测，此数暂无参照）" in text
    assert "核心判断" not in text


def test_banner_omits_the_layer_line_on_a_pre_r1_payload():
    """api 返回的是 R1 之前的 divergence（没有分层字段）时不能凭空多出一行
    "分层：不适用 不适用"——那会让人以为测了。"""
    pre_r1 = {k: v for k, v in BASE.items()
              if k not in ("core_jaccard", "adjunct_jaccard", "shared_core_herbs",
                           "shared_adjunct_herbs", "n_unroled",
                           "epsilon_core", "epsilon_adjunct")}
    text = _banner(pre_r1)
    assert "分层：" not in text
    assert "药物 Jaccard 距离 0.6" in text  # 原来那两行一个字没少


def test_banner_keeps_the_existing_lines_unchanged_and_appends_the_layer_line():
    """R1 只往下加一行。整方那行和两两配对那行必须原样、顺序不变——它们是
    E2/E3/E4 的展示口径。两位医家时只有一对，本来就不打配对行（原有行为），
    所以这里用三位医家的形状验"配对行还在第二行、分层行排第三"。"""
    two = _banner(BASE).split("\n")
    assert two[0].startswith("治法一致；")
    assert "药物 Jaccard 距离 0.6，共用 半夏、茯苓；ε=0.24（已超出噪声范围）" in two[0]
    assert len(two) == 2 and two[1].startswith("分层：")  # 两位医家不打配对行

    three = _banner(_with(pairs=BASE["pairs"] + [
        {"a": "ye_tianshi", "b": "zhang_xichun", "name_a": "叶天士", "name_b": "张锡纯",
         "group": "cross_school", "year_gap": 193, "herb_jaccard": 0.83},
        {"a": "wu_jutong", "b": "zhang_xichun", "name_a": "吴鞠通", "name_b": "张锡纯",
         "group": "cross_school", "year_gap": 102, "herb_jaccard": 0.83},
    ], lineage_mean=0.6, cross_school_mean=0.83)).split("\n")
    assert len(three) == 3
    assert three[0] == two[0]
    assert three[1].startswith("两两配对：叶天士×吴鞠通 0.6（师承内，相隔 91 年）")
    assert "师承内均值 0.6 vs 跨学派均值 0.83（跨学派分歧更大）" in three[1]
    assert three[2].startswith("分层：")


def test_banner_is_null_when_there_is_nothing_to_show():
    assert _banner(None) is None
    assert _banner({"same": True, "herb_jaccard": None}) is None


def test_banner_shows_the_string_reason_when_only_syndrome_names_differ():
    assert "按证型名称精确比对" in _banner({"same": False, "herb_jaccard": None})
