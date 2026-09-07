"""western_drugs（A2 张锡纯「衷中参西」）的离线测试。

词表本身是从 books/584-医学衷中参西录.txt 的真实文本里挖出来的（张锡纯自己会
写「西药阿斯匹林」这种前缀），实测推翻了两个想当然的写法：阿司匹林 0 次 /
阿斯匹林 136 次，白布圣 0 次 / 百布圣 19 次。这里的用例就钉住这些真实写法。

**张锡纯没有注册进 core/physicians.py（维持 HANDOFF 步骤 3 的原设计），所以
真实链路上这个字段现在恒为空。** 下面全部是合成数据验证；真实链路验证要等
AutoDL 上注册张锡纯并抽完他的 cases.json 之后进行。
"""
import json
import subprocess
from pathlib import Path

import pytest

from core import chain
from core.herbs import is_western_drug, split_western_drugs
from core.schemas import CaseStructured, S3Syndrome, S3SyndromeUnreferenced
from tests.test_chain import FakeLLM, FakeRetriever, _fake_cases

ROOT = Path(__file__).resolve().parent.parent


# ---------- 识别与拆分 ----------


@pytest.mark.parametrize("name", [
    "阿斯匹林",        # 原书 136 次
    "阿斯匹林一瓦",     # 带「瓦」剂量，normalize_herb 剥不掉，所以必须在原串上匹配
    "西药阿斯匹林",     # 原书自带的「西药」前缀
    "百布圣六分",      # 原书 19 次（不是白布圣）
    "金鸡纳霜",
    "盐酸规尼涅",      # normalize_herb 会把它剥成「酸规尼涅」，更不能先归一再判
    "规泥涅",          # 原书的另一种写法
    "安知歇貌林半瓦",
    "灰锰氧",
    "臭剥一瓦",
    "硫酸镁二钱",
    "西药几阿苏六分",   # 词表里没收「几阿苏」，靠「西药」前缀兜住
])
def test_recognizes_real_western_drug_spellings(name):
    assert is_western_drug(name) is True


@pytest.mark.parametrize("name", [
    "生石膏", "山药", "鸡内金",  # 鸡内金：原书说它「含有稀盐酸」，是成分描述不是西药
    "薄荷",                      # 原书说薄荷「含有薄荷脑」，同上
    "薄荷脑", "樟脑", "缬草",     # 刻意不收：都是正经中药材/中药成分
    "党参", "白术", "",
])
def test_does_not_misclassify_chinese_materia_medica(name):
    """误判的代价比漏判大：一味真中药被剔出 herb_jaccard，直接污染分歧度主指标。"""
    assert is_western_drug(name) is False


def test_split_preserves_order_and_original_spelling():
    herbs, western = split_western_drugs(
        ["生石膏", "西药阿斯匹林", "山药", "百布圣六分", "薄荷"]
    )
    assert herbs == ["生石膏", "山药", "薄荷"]
    assert western == ["西药阿斯匹林", "百布圣六分"], "保留原始写法，不做归一"


def test_split_handles_empty_and_none():
    assert split_western_drugs([]) == ([], [])
    assert split_western_drugs(None) == ([], [])


# ---------- schema ----------


@pytest.mark.parametrize("model,kwargs", [
    (CaseStructured, {}),
    (S3Syndrome, dict(syndrome="x", reasoning="x", treatment_principle="x",
                      cited_case_ids=["a"])),
    (S3SyndromeUnreferenced, dict(syndrome="x", reasoning="x", treatment_principle="x")),
])
def test_western_drugs_defaults_to_empty_and_serializes(model, kwargs):
    obj = model(**kwargs)
    assert obj.western_drugs == []
    assert "western_drugs" in obj.model_dump()


# ---------- S0 抽取边界 ----------


def test_s0_extraction_moves_western_drugs_out_of_herbs(tmp_path, monkeypatch):
    """prompt 是约束不是保证：模型把阿斯匹林塞进 herbs 时，代码这层必须兜住。"""
    import offline.extract_cases as ec
    from core.schemas import CaseSequence, SegmentPatients, VisitStructured

    seg = {"seg_id": "zhang_xichun-0001", "physician": "zhang_xichun",
           "text": "原文", "head_hints": [], "follow_hints": []}
    result = SegmentPatients(patients=[CaseSequence(visits=[
        VisitStructured(symptoms=["寒热往来"],
                        herbs=["生石膏", "西药阿斯匹林", "山药"])  # 模型放错了
    ])])
    records = ec.expand_segment(seg, result)

    assert records[0].herbs == ["生石膏", "山药"]
    assert records[0].western_drugs == ["西药阿斯匹林"]


def test_s0_extraction_keeps_model_provided_western_drugs(tmp_path):
    """模型自己放对的部分原样保留，不因为代码兜底就丢掉。"""
    import offline.extract_cases as ec
    from core.schemas import CaseSequence, SegmentPatients, VisitStructured

    seg = {"seg_id": "zhang_xichun-0002", "physician": "zhang_xichun",
           "text": "原文", "head_hints": [], "follow_hints": []}
    result = SegmentPatients(patients=[CaseSequence(visits=[
        VisitStructured(symptoms=["x"], herbs=["山药", "百布圣"],
                        western_drugs=["金鸡纳霜"])
    ])])
    records = ec.expand_segment(seg, result)

    assert records[0].herbs == ["山药"]
    assert records[0].western_drugs == ["金鸡纳霜", "百布圣"], "已有的在前，兜底挑出来的在后"


# ---------- S3 边界 + 分歧度 ----------


def _s3(cid, herbs, western=None):
    return S3Syndrome(syndrome="脾胃气虚", reasoning="x", treatment_principle="健脾益气",
                      cited_case_ids=[cid], herbs=herbs, western_drugs=western or [])


def _run(monkeypatch, s3_ye, s3_wu):
    from core.physicians import PHYSICIANS as REG

    monkeypatch.setattr(chain, "PHYSICIANS", {k: REG[k] for k in ("ye_tianshi", "wu_jutong")})
    fake_llm = FakeLLM({"叶天士": s3_ye, "吴鞠通": s3_wu})
    monkeypatch.setattr(chain, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(chain, "get_retriever", lambda: FakeRetriever(_fake_cases()))
    return chain.consult("纳差乏力")


def test_s3_boundary_splits_western_drugs_out_of_herbs(monkeypatch):
    outcome = _run(monkeypatch,
                   _s3("ye_tianshi-001", ["党参", "白术"]),
                   _s3("wu_jutong-001", ["党参", "西药阿斯匹林"]))
    wu = next(r for r in outcome["results"] if r["physician"] == "wu_jutong")
    assert wu["s3"].herbs == ["党参"]
    assert wu["s3"].western_drugs == ["西药阿斯匹林"]


def test_herb_jaccard_excludes_western_drugs(monkeypatch):
    """核心动机：西药混进 herbs 会把跨学派分歧系统性推高，而那个推高是假的。
    这里两位医家的中药完全相同，只有一位多开了西药——分歧度必须是 0。"""
    outcome = _run(monkeypatch,
                   _s3("ye_tianshi-001", ["党参", "白术"]),
                   _s3("wu_jutong-001", ["党参", "白术", "西药阿斯匹林"]))
    assert outcome["divergence"]["herb_jaccard"] == 0.0, "西药不该算进药物 Jaccard"
    assert outcome["divergence"]["shared_herbs"] == ["党参", "白术"]


def test_western_drug_overlap_is_none_when_neither_uses_any(monkeypatch):
    """None 而不是 0——0 会被读成"两边西药完全一致"，实际是"这个维度不适用"。"""
    outcome = _run(monkeypatch,
                   _s3("ye_tianshi-001", ["党参"]),
                   _s3("wu_jutong-001", ["白术"]))
    assert outcome["divergence"]["western_drug_overlap"] is None


def test_western_drug_overlap_reported_separately(monkeypatch):
    outcome = _run(monkeypatch,
                   _s3("ye_tianshi-001", ["党参"]),
                   _s3("wu_jutong-001", ["党参", "西药阿斯匹林", "百布圣"]))
    wdo = outcome["divergence"]["western_drug_overlap"]
    assert wdo is not None
    assert wdo["jaccard"] == 1.0, "一方全有一方全无 = 完全不重叠"
    assert wdo["shared"] == []
    assert wdo["by_physician"]["ye_tianshi"] == []
    assert wdo["by_physician"]["wu_jutong"] == ["百布圣", "西药阿斯匹林"]


# ---------- 前端渲染（用 node 跑 index.html 里真实上线的那份代码）----------


def _run_western_drugs_html(s3: dict) -> str:
    """用 node 执行 index.html 里那整份 <script>，再调它的 westernDrugsHtml。
    测的是真实上线的那份代码，不是在测试里另抄一份实现。"""
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = html.split("<script>")[-1].split("</script>")[0]
    # 最小 DOM 桩：脚本末尾有 document.getElementById(...).addEventListener 这类
    # 加载期绑定，裸 node 会 ReferenceError。用 Proxy 吸收掉任意 DOM 调用，这样
    # 跑的仍然是整份真实脚本（顺带能抓到它的语法/加载期运行时错误），而不是把
    # 要测的函数单独抠出来跑——抠出来就测不到它在真实文件里是否可用了。
    dom_stub = """
    const anyNode = new Proxy(function(){}, {
      get: () => anyNode, set: () => true, apply: () => anyNode, construct: () => anyNode,
    });
    globalThis.document = anyNode;
    globalThis.window = anyNode;
    globalThis.cytoscape = anyNode;
    """
    js = (
        dom_stub
        + script
        + "\nprocess.stdout.write(westernDrugsHtml(" + json.dumps(s3, ensure_ascii=False) + "));"
    )
    proc = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败：\nstdout={proc.stdout}\nstderr={proc.stderr}"
    return proc.stdout


def test_frontend_renders_western_drugs_section_when_present():
    out = _run_western_drugs_html({"western_drugs": ["西药阿斯匹林", "百布圣"]})
    assert "参西用药" in out
    assert "西药阿斯匹林、百布圣" in out
    assert "western-drugs" in out


def test_frontend_renders_nothing_when_absent():
    """没有西药的医家（叶天士/吴鞠通）不显示空白栏。"""
    assert _run_western_drugs_html({"western_drugs": []}) == ""
    assert _run_western_drugs_html({}) == ""


def test_frontend_escapes_html_in_drug_names():
    out = _run_western_drugs_html({"western_drugs": ["<script>x</script>"]})
    assert "<script>x" not in out
    assert "&lt;script&gt;" in out
