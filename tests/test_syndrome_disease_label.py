"""同名证候的病名限定：图谱里重名的证候节点，显示层要带上病名，保证彼此可区分。

证候节点里有相当一部分重名。「肝郁气滞证」在 `data/standard/syndromes.jsonl`
里分属腹痛 / 胁痛 / 积聚 / 癃闭四条，**病机各不同**。重名并没有归零：剩下的
是另一类根因，见本文件最后那条判据。

**数据本身带病名**：jsonl 有 `disease` 字段，`offline/build_graph.py` 也把它写进了
节点。所以要在显示层补上——`_node_payload` 的 label 只取 name、tooltip 的 syndrome
分支只显示 label + definition 的话，图上会并排出现几个一模一样的方块，点开才知道
不是同一个证，而这正是图谱浏览器要回答的那类问题。
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

import api.main as api_main
from api.main import _display_label
from core.tools import get_graph_store

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


def _client():
    return TestClient(api_main.app)


def _syndrome_nodes():
    store = get_graph_store()
    assert store is not None, "data/graph.json 应当存在（缺失时 tests/conftest.py 会先构建）"
    return {nid: d for nid, d in store.g.nodes(data=True)
            if d.get("node_type") == "syndrome"}


def test_duplicate_syndrome_names_exist_in_the_graph():
    """图谱里确实存在重名的证候节点——有重名，病名限定才有必要。"""
    names = Counter(d.get("name") for d in _syndrome_nodes().values())
    dupes = {n: c for n, c in names.items() if c > 1}
    assert dupes, "没有重名的话病名限定就没有存在的必要"
    # 下限 40：重名还大量存在时，病名限定才有存在的必要。名字被修对（重名减少）
    # 是好事，所以这条读作"重名消失之前至少还有这么多"；归零的那天这条判据该整条
    # 删掉，而不是把 40 改成 0。
    assert sum(dupes.values()) >= 40, f"重名节点数 {sum(dupes.values())}"
    assert max(dupes.values()) >= 3, "最多的那个名字至少重复 3 次"


def test_every_duplicate_name_gets_a_distinct_label():
    """重名节点的 label 两两不同。
    只要有两个还一样，图上并排两个方块就还是分不开。"""
    from api.main import ambiguous_syndrome_keys

    nodes = _syndrome_nodes()
    ambiguous = ambiguous_syndrome_keys(get_graph_store())
    names = Counter(d.get("name") for d in nodes.values())
    dupe_names = {n for n, c in names.items() if c > 1}
    labels = [_display_label(nid, d, ambiguous) for nid, d in nodes.items()
              if d.get("name") in dupe_names]
    assert len(labels) == len(set(labels)), \
        "还有重名节点的 label 撞在一起：" + str([x for x, c in Counter(labels).items() if c > 1])


def test_a_syndrome_with_a_disease_is_qualified_by_it():
    assert _display_label("syndrome::X", {
        "node_type": "syndrome", "name": "肝郁气滞证", "disease": "胁痛",
    }) == "肝郁气滞证\n（胁痛）", "病名另起一行：写成一行会让节点过宽而压字"


def test_a_syndrome_without_a_disease_gets_no_empty_parens():
    """部分国标条目没有 disease。**一个空括号比没有更糟**。"""
    for blank in (None, "", "   "):
        assert _display_label("syndrome::X", {
            "node_type": "syndrome", "name": "脾胃气虚证", "disease": blank,
        }) == "脾胃气虚证"


def test_non_syndrome_nodes_are_untouched():
    """证素/症状/医案不带病名限定——它们本来就不重名，加括号只是噪音。"""
    assert _display_label("element::肝", {"node_type": "element", "name": "肝",
                                          "disease": "胁痛"}) == "肝"
    assert _display_label("symptom::纳呆", {"node_type": "symptom", "name": "纳呆"}) == "纳呆"


def test_searching_by_name_finds_every_same_named_entry():
    """**`name` 字段不许动。** `/api/graph/search` 匹配的是 name，跟着改的话
    搜「肝郁气滞证」会因为多出括号而一条都搜不到——那比重名更糟。"""
    resp = _client().get("/api/graph/search?q=肝郁气滞证&limit=20")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page"]["total"] >= 4, "同名的那几条要全都搜得到"
    labels = [n["data"]["label"] for n in body["graph"]["nodes"]]
    assert len(labels) == len(set(labels)), "搜出来的几条彼此要能区分"
    assert all(lb.startswith("肝郁气滞证") for lb in labels)


def test_the_payload_carries_the_raw_disease_field():
    """label 是给眼睛的，`disease` 字段是给 tooltip 和下游用的——两者都要在。"""
    resp = _client().get("/api/graph/search?q=肝郁气滞证&limit=5")
    node = resp.json()["graph"]["nodes"][0]["data"]
    assert node["disease"]
    assert node["disease"] in node["label"]


def test_the_tooltip_shows_the_disease_on_its_own_line():
    src = (WEB / "graph.js").read_text(encoding="utf-8")
    body = src[src.index('case "syndrome": {'):]
    body = body[:body.index("case \"case\":")]
    assert "病名：" in body
    assert "if (data.disease)" in body, "disease 为空时不出这一行"


def test_the_consult_graph_never_invents_a_disease_name():
    """问诊图的证型层同样是「病名 · 证型」，但**S3 没给病名时原样显示证型**——
    对不上教材条目的证型不许被补一个看起来合理的病名。

    锚点是 api/main.py 里「layer 3 证型（含病名）」那一段源码。"""
    src = (ROOT / "api" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("# ---- layer 3 证型（含病名） ----"):]
    body = body[:body.index("for hit in r[\"s2\"].elements:")]
    assert 'label = f"{s3.disease} · {s3.syndrome}" if s3.disease else s3.syndrome' in body


def test_the_label_shows_up_in_the_browser_payload_end_to_end():
    resp = _client().get("/api/graph?node_types=syndrome&limit=200")
    assert resp.status_code == 200
    labels = [n["data"]["label"] for n in resp.json()["graph"]["nodes"]]
    qualified = [lb for lb in labels if "\n（" in lb]
    assert len(qualified) >= 60, f"带病名限定的只有 {len(qualified)} 个"


def test_the_graph_data_file_really_carries_disease():
    """显示层修得再好，数据里没有这个字段也白搭——这条钉住 build_graph 的产出。"""
    data = json.loads((ROOT / "data" / "graph.json").read_text(encoding="utf-8"))
    syn = [n for n in data["nodes"] if n.get("node_type") == "syndrome"]
    with_disease = [n for n in syn if n.get("disease")]
    assert len(with_disease) >= len(syn) - 20, "有 disease 的条目太少，先查 build_graph"


def test_node_js_can_render_the_tooltip_without_a_disease():
    """node 侧真跑一遍两种情况，不只读源码。"""
    from tests.web_harness import DOM_STUB, js_tmp, load_app_js

    tail = """
      const withD = describeNodeTooltip({node_type: "syndrome", label: "肝郁气滞证（胁痛）",
                                         disease: "胁痛", definition: "定义"});
      const without = describeNodeTooltip({node_type: "syndrome", label: "脾胃气虚证",
                                           definition: "定义"});
      process.stdout.write(JSON.stringify({withD, without}));
    """
    proc = subprocess.run(["node", js_tmp(DOM_STUB + load_app_js() + tail)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "病名：胁痛" in out["withD"]
    assert "病名" not in out["without"]


def test_the_remaining_ambiguous_pairs_are_a_data_defect_not_a_display_bug():
    """**已知但未修的重名要有判据看着它。**

    加病名限定之后，仍有 3 组 (名字, 病名) 撞在一起。重名的根因分五类，前四类已修或已报：

      1. **证型标题的写法**：扫描件里的证型标题有几种写法（`# 1.胃中寒冷`、
         `（3）肝火犯肺`、`# （4）肺阴亏虚`、`1）痰热腑实`、`7.痰火扰心`，后几种
         还会丢掉行首的 `#`），`syndrome_heading()` 把它们放在一处判定。只认其中
         两种的话，认不出的标题会让上一条的名字被下一条沿用，相邻几条证型同名。
      2. **OCR 形近字**：`disease=黄疽`→黄疸、`name=温疤证`→温疟证、
         `name=黄痘`→黄疸、`name=虚瘩`→虚痞，由 `data/standard/ocr_fixes.tsv`
         的 scope 列修正。
      3. **教材原文本身掉字**：`# 6.阻心脉`（少「瘀」）、`# 第五节 逆`（少「呃」）、
         `# 第三节   闭`（少「癃」）。补哪个字要靠语义猜——只报不改，进
         `build_syndrome_textbook --report` 的「可疑条目」清单。
      4. **名称错切/合并**：`（6）哮喘脱证`。同上，只报不改。

    **剩下这 3 组是第五类：块边界配错位。** 三组里的两条**各有自己正确的标题**
    （`# 3.血虚` / `# 4.气虚阳微` / `（2）胃阴不足`），名字没被复用——是
    「临床表现：」块跟「证机概要：」块的配对错了位（TB-118 的 definition 里甚至
    吞进了「治法：…代表方：…常用药：」整段）。这是解析层的另一个根因，
    判据留在这里盯着。

    **这个数只能降不能升。** 降了：把这一行的数改成新的值。升了：解析器退化了，
    去查哪一步把名字弄丢了，**不是把这一行改成 `<=`**。
    """
    from api.main import ambiguous_syndrome_keys

    still = ambiguous_syndrome_keys(get_graph_store())
    assert len(still) == 3, f"仍撞在一起的组数变了：{len(still)}（期望 3）"


def test_the_remaining_ambiguous_ones_fall_back_to_the_code():
    """撞在一起的那几组补 code，其余不补——给每条都挂上 TB-001 这样的编号会让图上全是噪音。"""
    ambiguous = {("热证", "胃痛")}
    assert _display_label("syndrome::TB-114", {
        "node_type": "syndrome", "name": "热证", "disease": "胃痛", "code": "TB-114",
    }, ambiguous) == "热证\n（胃痛 TB-114）"
    assert _display_label("syndrome::SP-01", {
        "node_type": "syndrome", "name": "肝胃不和证", "disease": "胃痛", "code": "SP-01",
    }, ambiguous) == "肝胃不和证\n（胃痛）"
