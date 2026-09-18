"""web/index.html 里 hover tooltip 这部分前端代码的离线测试。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_stream_frontend.py /
test_western_drugs.py 同一个模式），测 describeNodeTooltip / describeEdgeTooltip
这两个纯函数：给一份 cytoscape 节点/边的 data()，应该出什么文字。这两个函数
同时服务两种数据形状——per-consult 图（模块6，靠 layer 字段分支）和图谱浏览器
的持久知识图谱（模块7，靠 node_type/edge_type 字段分支），复用同一个函数、
同一份测试文件，不为图谱浏览器另起一套 tooltip 逻辑或另一个测试文件。

不测真实鼠标 hover 事件本身（cytoscape 的 mouseover/mousemove/mouseout 绑定
逻辑）——那要么得起真浏览器（Playwright），要么得深度 mock cytoscape 的事件
系统，两者都测不出比"这几行 cy.on(...) 调用对不对"更多的信息，而这几行本身
很短、读代码就能确认对不对，不值得为它单独搭一套 cytoscape 事件模拟。
"""
import json
import subprocess

from pathlib import Path
from tests.web_harness import DOM_STUB, js_tmp, load_app_js

ROOT = Path(__file__).resolve().parent.parent



def _run_node(js_tail: str) -> str:
    script = load_app_js()
    proc = subprocess.run(
        ["node", js_tmp(DOM_STUB + script + "\n" + js_tail)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _describe_node(data: dict, physician_names: dict | None = None) -> str:
    js = (
        f"PHYSICIAN_NAMES = {json.dumps(physician_names or {}, ensure_ascii=False)};\n"
        f'process.stdout.write(describeNodeTooltip({json.dumps(data, ensure_ascii=False)}));'
    )
    return _run_node(js)


def _describe_edge(data: dict, source_label: str, target_label: str,
                   physician_names: dict | None = None, current_physician: str | None = None,
                   product_mode: bool = False) -> str:
    # R56 §6 第 2 条：λ1 那一行现在走 `graphHooks.checkProductMode()`（不是裸的
    # `isProductMode()`——graph.js 不许直接调 app.js 的全局函数，见
    # tests/test_web_split.py 的依赖方向测试；钩子名特意跟 app.js 的
    # `isProductMode` 函数名不同，因为那条测试按名字做跨文件调用的静态分析，
    # 同名会被当成"graph.js 直接调了 app.js 的函数"，哪怕实际走的是钩子）。
    # 直接改 `graphHooks` 这个对象上的字段，不去动 `document`——整个替换
    # `globalThis.document` 会连累 app.js 加载时挂的异步回调（`initDemoModeBanner`
    # 那类 `fetch('/health').then(...)`），脚本主体跑完之后、回调触发时
    # `document` 已经被换成没有 `getElementById` 的假对象，进程带着非零
    # 退出码收尾，尽管我们要断言的 stdout 内容其实是对的。
    js = (
        f"graphHooks.checkProductMode = () => {str(product_mode).lower()};\n"
        f"PHYSICIAN_NAMES = {json.dumps(physician_names or {}, ensure_ascii=False)};\n"
        f'process.stdout.write(describeEdgeTooltip({json.dumps(data, ensure_ascii=False)}, '
        f'{json.dumps(source_label, ensure_ascii=False)}, {json.dumps(target_label, ensure_ascii=False)}, '
        f'{json.dumps(current_physician, ensure_ascii=False)}));'
    )
    return _run_node(js)


# ---------- R42：九种节点类型（对应 to_graph() 的 layer 0-8）----------
#
# **fixture 里必须带 node_type 与 layer_label**，因为真实 payload 一定带：
# `to_graph.add_node` 无条件写这两个字段（layer_label 是后端下发的层中文名，
# 前端不写死一份——加层/改层名时前端跟着长）。R42 之前这个文件的 fixture 只给
# `layer`，前端那边靠一张写死的 `switch (data.layer)` 认，于是**测试用的形状
# 跟上线的形状不是同一个**；那正好掩盖了一个真 bug：问诊图的节点从 R16 起也带
# node_type，而 describeNodeTooltip 先判 node_type，所以证型/方剂/药材节点
# 实际走进了图谱浏览器那一支（见 describeNodeTooltip 上面那段）。


def test_symptom_node_shows_label_and_state():
    for state, expect in [("explained", "已解释"), ("residual", "残差辨证补充解释"), ("unexplained", "未解释")]:
        out = _describe_node({"id": "sym::纳差", "label": "纳差", "layer": 0,
                              "node_type": "symptom", "layer_label": "症状",
                              "state": state})
        assert "症状" in out
        assert "纳差" in out
        assert expect in out


def test_element_node_shows_location_vs_nature_and_residual_flag():
    # R42：证素拆成 脏腑(1)/病性(2) 两层，层名由后端下发。
    out = _describe_node({"id": "organ::脾", "label": "脾", "layer": 1,
                          "node_type": "organ", "layer_label": "脏腑",
                          "kind": "location"})
    assert "脏腑" in out and "脾" in out and "病位" in out
    assert "残差" not in out

    out = _describe_node({"id": "nature::气虚", "label": "气虚", "layer": 2,
                          "node_type": "nature", "layer_label": "病性",
                          "kind": "nature", "residual": True})
    assert "病性" in out
    assert "残差辨证补充" in out


def test_syndrome_node_shows_every_contributor_not_just_one():
    """R42 去掉医家分带之后**同名证型合并成一个节点**，谁给的记在
    `contributors` 里——所以这里要显示的是一串医家，不是一个。
    只显示第一个的话，"三位医家给出同一个证型"这件事在图上就看不见了
    （而那正是合并之后唯一还能表达它的地方）。"""
    out = _describe_node({"id": "syn::脾胃气虚证", "label": "胃痛 · 脾胃气虚证",
                          "layer": 3, "node_type": "syndrome", "layer_label": "证型",
                          "disease": "胃痛",
                          "contributors": ["ye_tianshi", "wu_jutong"]},
                         physician_names={"ye_tianshi": "叶天士", "wu_jutong": "吴鞠通"})
    assert "证型" in out and "脾胃气虚证" in out
    assert "叶天士" in out and "吴鞠通" in out
    assert "病名：胃痛" in out


def test_syndrome_node_still_reads_the_legacy_pname_field():
    """旧的审计记录里只有 `pname`（单个医家名），回看时不能变成空白。"""
    out = _describe_node({"id": "syn::ye_tianshi", "label": "脾胃气虚", "layer": 3,
                          "node_type": "syndrome", "layer_label": "证型",
                          "phys": "ye_tianshi", "pname": "叶天士"})
    assert "证型" in out and "脾胃气虚" in out and "叶天士" in out


def test_the_new_middle_layers_each_get_their_own_line():
    """R42 新增的三层（病机/治则/治法）各要有自己的 tooltip。
    **标题取后端下发的 layer_label**，所以这条同时钉住"层名不在前端写死"。"""
    mech = _describe_node({"id": "mech::脾失健运", "label": "脾失健运", "layer": 4,
                           "node_type": "pathogenesis", "layer_label": "病机",
                           "organ": "脾", "contributors": ["ye_tianshi"]},
                          physician_names={"ye_tianshi": "叶天士"})
    assert "病机" in mech and "脾失健运" in mech and "病位：脾" in mech
    for nid, ntype, label, text in [
        ("principle::健脾益气", "principle", "治则", "健脾益气"),
        ("method::益气健脾", "method", "治法", "益气健脾"),
    ]:
        out = _describe_node({"id": nid, "label": text, "layer": 5,
                              "node_type": ntype, "layer_label": label})
        assert label in out and text in out


def test_formula_node_shows_source_label_and_selected_and_safety_flags():
    """M5：layer 3 从"用药"改成"方剂"，source 三档要翻成中文，selected/
    safety_blocking 两个布尔标记要各自出一句人话，不是原样打印 true/false。"""
    out = _describe_node({
        "id": "formula::柴胡疏肝散", "label": "柴胡疏肝散", "layer": 7,
        "node_type": "formula", "layer_label": "方剂",
        "phys": "ye_tianshi", "pname": "叶天士", "source": "classic",
        "selected": True, "safety_blocking": False,
    })
    assert "方剂" in out and "柴胡疏肝散" in out and "叶天士" in out
    assert "经典方" in out
    assert "已选" in out
    assert "安全拦截" not in out

    out2 = _describe_node({
        "id": "formula::柴胡疏肝散加减", "label": "柴胡疏肝散加减", "layer": 7,
        "node_type": "formula", "layer_label": "方剂",
        "phys": "ye_tianshi", "pname": "叶天士", "source": "modified",
        "selected": False, "safety_blocking": True,
    })
    assert "加减方" in out2
    assert "已选" not in out2
    assert "安全拦截" in out2


def test_herb_node_falls_back_to_physician_names_map():
    """herb:: 节点的 data 里没有 pname（to_graph() 没存这份），必须靠前端自己
    从 PHYSICIAN_NAMES（renderConsultResult 里从 data.results 建的）查——
    这条测试钉住这条回退路径，不是钉住"节点数据恰好带全了"这个巧合。
    M5：id 从 herb::{phys}::{herb} 两段式变成 herb::{phys}::{方名}::{herb}
    三段式（同一味药可能出现在多个候选方里，不带方名会撞节点）；
    R42 去掉医家段，成了 herb::{方名}::{药名}，层号从 4 变 8。"""
    out = _describe_node(
        {"id": "herb::甘草泻心汤::党参", "label": "党参", "layer": 8,
         "node_type": "herb", "layer_label": "君臣佐使",
         "phys": "wu_jutong", "role": "臣", "dose": 9.0, "unit": "g"},
        physician_names={"wu_jutong": "吴鞠通"},
    )
    assert "君臣佐使" in out and "党参" in out and "吴鞠通" in out
    assert "臣药" in out


def test_herb_node_without_physician_names_map_falls_back_to_raw_id():
    """PHYSICIAN_NAMES 还没建好（比如图还没渲染过就被 hover，理论上不会发生，
    但代码不能因为查不到就崩），退到显示医家 id 本身，好歹不是空白。"""
    out = _describe_node(
        {"id": "herb::甘草泻心汤::党参", "label": "党参", "layer": 8,
         "node_type": "herb", "layer_label": "君臣佐使", "phys": "wu_jutong"}
    )
    assert "wu_jutong" in out


def test_herb_node_tooltip_includes_function_in_formula_when_present():
    """M7：function_in_formula（这味药在方中的作用）加进药材节点的 tooltip——
    to_graph() 早就把这个字段存进节点 data 了（M5 就有，见 api/main.py），
    这一轮只是前端把它显示出来，没有改后端契约。"""
    out = _describe_node(
        {"id": "herb::甘草泻心汤::党参", "label": "党参", "layer": 8,
         "node_type": "herb", "layer_label": "君臣佐使",
         "phys": "wu_jutong", "role": "臣", "dose": 9.0, "unit": "g",
         "function_in_formula": "补中益气，防苦寒药伤脾"},
        physician_names={"wu_jutong": "吴鞠通"},
    )
    assert "补中益气，防苦寒药伤脾" in out


def test_herb_node_tooltip_omits_function_line_when_absent():
    """没有这个字段（旧数据/模型没给）时不显示一行空的——不是显示"undefined"
    或者一行空 <div>，而是这一段 HTML 干脆不出现。"""
    out = _describe_node(
        {"id": "herb::甘草泻心汤::党参", "label": "党参", "layer": 8,
         "node_type": "herb", "layer_label": "君臣佐使",
         "phys": "wu_jutong", "role": "臣", "dose": 9.0, "unit": "g"},
    )
    assert out.count("tt-meta") == 1  # 只有原来那一行元信息，没有多出第二行空的


def test_unknown_layer_falls_back_to_label_or_id():
    """层号越界/层名缺失时**不崩、不空白**：显示一个陌生的 id 好过显示空白
    （至少能搜）。这条兜底是给旧审计记录回放用的。"""
    assert "神秘节点" in _describe_node({"id": "x::神秘节点", "label": "神秘节点", "layer": 99})
    assert "x::无标签" in _describe_node({"id": "x::无标签", "layer": 99})


# ---------- 边 ----------


def test_edge_shows_source_arrow_target():
    out = _describe_edge({}, "纳差", "脾")
    assert "纳差" in out and "脾" in out and "→" in out


def test_edge_with_phys_shows_physician_name():
    out = _describe_edge({"phys": "ye_tianshi"}, "脾", "脾胃气虚",
                         physician_names={"ye_tianshi": "叶天士"})
    assert "叶天士" in out


def test_edge_with_residual_flag_says_so():
    out = _describe_edge({"residual": True}, "乏力", "气虚")
    assert "残差辨证补充的连线" in out


def test_edge_without_phys_or_residual_shows_only_the_arrow():
    """症状->证素这条边（S2 全局共享）不带 phys、不带 residual，tooltip 不该
    凭空印出"undefined"或空的 meta 行。"""
    out = _describe_edge({}, "纳差", "脾")
    assert "undefined" not in out
    assert "tt-meta" not in out


# ---------- HTML 转义（沿用项目里既有的 escapeHtml，这里只验证真的被调用了）----------


def test_labels_are_html_escaped():
    out = _describe_node({"id": "sym::<script>", "label": "<script>x</script>", "layer": 0, "state": "explained"})
    assert "<script>x" not in out
    assert "&lt;script&gt;" in out


# ---------- 模块7：图谱浏览器节点（node_type，跟 per-consult 图的 layer 是两种数据形状）----------


def test_persistent_symptom_node_shows_label_only():
    out = _describe_node({"id": "symptom::纳呆", "label": "纳呆", "node_type": "symptom"})
    assert "症状" in out and "纳呆" in out


def test_persistent_element_node_shows_location_vs_nature():
    out = _describe_node({"id": "element::脾", "label": "脾", "node_type": "element", "category": "location"})
    assert "证素" in out and "病位" in out
    out = _describe_node({"id": "element::气虚", "label": "气虚", "node_type": "element", "category": "nature"})
    assert "病性" in out


def test_persistent_syndrome_node_shows_definition_and_category_flag():
    out = _describe_node({
        "id": "syndrome::SP-01", "label": "肝胃不和证", "node_type": "syndrome",
        "is_category": False, "definition": "肝气犯胃，胃失和降", "tongue_pulse": "舌淡红，苔薄白",
    })
    assert "证型" in out and "肝胃不和证" in out
    assert "肝气犯胃，胃失和降" in out
    assert "舌淡红，苔薄白" in out
    assert "类目" not in out

    out_cat = _describe_node({
        "id": "syndrome::CAT-01", "label": "脾胃病类", "node_type": "syndrome", "is_category": True,
    })
    assert "（类目）" in out_cat


def test_persistent_syndrome_node_without_definition_does_not_print_undefined():
    """definition/tongue_pulse 都是可选字段（合成图里可能不填），tooltip 不该
    因为缺了它们就印出"undefined"这种明显的渲染事故。"""
    out = _describe_node({"id": "syndrome::SP-01", "label": "肝胃不和证", "node_type": "syndrome"})
    assert "undefined" not in out


def test_persistent_case_node_shows_label():
    out = _describe_node({"id": "case::ye_tianshi-001", "label": "ye_tianshi-001", "node_type": "case"})
    assert "医案" in out


# ---------- 模块7：图谱浏览器的边（edge_type，跟 per-consult 图的 phys/residual 是两种数据形状）----------


def test_persistent_indicates_edge_shows_cardinal_and_lambda1_for_selected_physician():
    edge = {
        "edge_type": "indicates", "is_cardinal": True,
        "lambda1_by_physician": {"ye_tianshi": 0.0, "wu_jutong": 0.5},
    }
    out = _describe_edge(edge, "纳呆", "脾", physician_names={"ye_tianshi": "叶天士"},
                         current_physician="ye_tianshi")
    assert "纳呆" in out and "脾" in out and "indicates" in out
    assert "主症" in out
    assert "叶天士" in out and "0.00" in out


def test_the_lambda1_line_is_hidden_in_product_mode():
    """R56 §6 第 2 条：λ1 是内部统计权重符号，产品面（默认模式）不该露出来
    ——医师/患者看不懂这个记号，也不该需要看懂。"""
    edge = {
        "edge_type": "indicates", "is_cardinal": True,
        "lambda1_by_physician": {"ye_tianshi": 0.0},
    }
    out = _describe_edge(edge, "纳呆", "脾", physician_names={"ye_tianshi": "叶天士"},
                         current_physician="ye_tianshi", product_mode=True)
    assert "λ1" not in out
    assert "主症" in out  # 其余内容不受影响，只是少了 λ1 这一行


def test_persistent_indicates_edge_secondary_symptom_label():
    out = _describe_edge({"edge_type": "indicates", "is_cardinal": False}, "口苦", "热")
    assert "次症" in out


def test_persistent_indicates_edge_without_selected_physician_omits_lambda1_line():
    """没选医家（比如页面刚加载、还没触发 change 事件）时不该报 undefined 或
    随便挑一个医家的 λ1 出来充数——没选就不显示这一行。"""
    out = _describe_edge(
        {"edge_type": "indicates", "is_cardinal": True, "lambda1_by_physician": {"ye_tianshi": 0.0}},
        "纳呆", "脾", current_physician=None,
    )
    assert "λ1" not in out
    assert "undefined" not in out


def test_persistent_composes_edge_shows_only_arrow_and_type():
    """composes 边（证素->证型）不带 per-physician 权重，不该硬造一行"主症/次症"
    或 λ1 出来。"""
    out = _describe_edge({"edge_type": "composes"}, "脾", "脾胃气虚证")
    assert "脾" in out and "脾胃气虚证" in out and "composes" in out
    assert "主症" not in out and "次症" not in out
    assert "λ1" not in out
