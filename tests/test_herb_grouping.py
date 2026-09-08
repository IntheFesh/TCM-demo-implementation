"""web/index.html 里 M7 新增的几个纯函数的离线测试：君臣佐使分组
（groupHerbsByRole/herbItemLabel/herbGroupsHtml）、卡片详情默认展开状态
（defaultDetailsOpenForMode）。

用 node 跑 index.html 里真实上线的那份 <script>（跟 test_hover_tooltip.py /
test_stream_frontend.py 同一个模式），测的是真实上线的代码，不是在测试里
另抄一份实现。
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DOM_STUB = """
const anyNode = new Proxy(function(){}, {
  get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
});
globalThis.document = anyNode;
globalThis.window = anyNode;
globalThis.cytoscape = anyNode;
"""


def _run_node(js_tail: str) -> str:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    proc = subprocess.run(
        ["node", "-e", DOM_STUB + script + "\n" + js_tail],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def _group(herb_items: list) -> list:
    js = f"process.stdout.write(JSON.stringify(groupHerbsByRole({json.dumps(herb_items, ensure_ascii=False)})));"
    return json.loads(_run_node(js))


# ---------- groupHerbsByRole ----------


def test_group_herbs_by_role_null_role_goes_to_unlabeled_group():
    """M7 闸门明确要求的测试：role 为 null 的进"未标注"组，不强行塞进某一档。"""
    groups = _group([{"name": "甘草", "role": None}])
    assert len(groups) == 1
    assert groups[0]["role"] == "未标注"
    assert [h["name"] for h in groups[0]["herbs"]] == ["甘草"]


def test_group_herbs_by_role_fixed_order_jun_chen_zuo_shi():
    """四组固定顺序 君->臣->佐->使，不按输入顺序、不按字母序——这是给学生看的
    教学展示，顺序本身就是中医方剂学的常识，不能因为输入顺序不同就变了。"""
    herbs = [
        {"name": "甘草", "role": "使"},
        {"name": "白芍", "role": "佐"},
        {"name": "柴胡", "role": "君"},
        {"name": "枳壳", "role": "臣"},
    ]
    groups = _group(herbs)
    assert [g["role"] for g in groups] == ["君", "臣", "佐", "使"]


def test_group_herbs_by_role_omits_empty_roles():
    """没有臣药的方子不该显示一行空的"臣"——只返回实际有药的组。"""
    groups = _group([{"name": "柴胡", "role": "君"}, {"name": "甘草", "role": "使"}])
    assert [g["role"] for g in groups] == ["君", "使"]


def test_group_herbs_by_role_unrecognized_role_string_also_goes_unlabeled():
    """role 不是"君/臣/佐/使"四个字之一（比如模型吐了别的东西）时同样归入
    未标注，不是让它悄悄消失或者报错——这条防的是模型输出跟 schema 的
    Literal 约束不完全一致时前端还能兜住，不崩。"""
    groups = _group([{"name": "怪药", "role": "不知道"}])
    assert groups == [{"role": "未标注", "herbs": [{"name": "怪药", "role": "不知道"}]}]


def test_group_herbs_by_role_multiple_herbs_same_role_all_kept():
    groups = _group([
        {"name": "党参", "role": "君"},
        {"name": "黄芪", "role": "君"},
    ])
    assert len(groups) == 1
    assert [h["name"] for h in groups[0]["herbs"]] == ["党参", "黄芪"]


# ---------- herbItemLabel ----------


def _label(item: dict) -> str:
    js = f"process.stdout.write(herbItemLabel({json.dumps(item, ensure_ascii=False)}));"
    return _run_node(js)


def test_herb_item_label_includes_dose_when_present():
    assert _label({"name": "瓜蒌", "dose": 15, "dose_unit": "g"}) == "瓜蒌 15g"


def test_herb_item_label_omits_dose_when_missing():
    """古籍医案常常不写剂量（HerbItem 的文档字符串里明确这条）——dose 为
    None 时不能显示"瓜蒌 Noneg"这种半成品，应该干净地只显示药名。"""
    assert _label({"name": "瓜蒌", "dose": None}) == "瓜蒌"


def test_herb_item_label_wraps_processing_in_parens():
    assert _label({"name": "半夏", "dose": 9, "dose_unit": "g", "processing": "姜制"}) == "半夏 9g（姜制）"


# ---------- herbGroupsHtml：君药加粗 ----------


def _groups_html(cand: dict) -> str:
    js = f"process.stdout.write(herbGroupsHtml({json.dumps(cand, ensure_ascii=False)}));"
    return _run_node(js)


def test_herb_groups_html_bolds_only_jun_herbs():
    cand = {
        "herb_items": [
            {"name": "瓜蒌", "dose": 15, "dose_unit": "g", "role": "君"},
            {"name": "半夏", "dose": 9, "dose_unit": "g", "role": "臣"},
        ]
    }
    html = _groups_html(cand)
    assert '<span class="herb-jun">瓜蒌 15g</span>' in html
    assert "半夏 9g" in html
    assert '<span class="herb-jun">半夏' not in html


def test_herb_groups_html_empty_when_no_herb_items():
    assert _groups_html({"herb_items": []}) == ""
    assert _groups_html({}) == ""


# ---------- defaultDetailsOpenForMode ----------


def test_default_details_open_for_student_mode_is_true():
    """M7 闸门要求的测试：学生模式的默认展开状态。"""
    js = 'process.stdout.write(JSON.stringify(defaultDetailsOpenForMode("student")));'
    assert json.loads(_run_node(js)) is True


def test_default_details_open_for_researcher_mode_is_false():
    js = 'process.stdout.write(JSON.stringify(defaultDetailsOpenForMode("researcher")));'
    assert json.loads(_run_node(js)) is False


def test_default_details_open_for_unknown_mode_defaults_to_closed():
    """doctor/patient 目前没有专属展开状态设计（见 web/index.html 里
    defaultDetailsOpenForMode 的注释）——折叠是更安全的缺省值，不是把它们
    当成 researcher 处理。"""
    for mode in ["doctor", "patient", "", None]:
        js = f'process.stdout.write(JSON.stringify(defaultDetailsOpenForMode({json.dumps(mode)})));'
        assert json.loads(_run_node(js)) is False
