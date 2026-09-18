"""R39：申报书 docx「只增不改」的判据。**零 LLM、零网络**，用合成 docx 跑。

用户手里那份申报书的黑色正文一个字都不许变，而"我小心地没动它"是一句无法核实
的话。这个文件测的就是那句话怎么变成机器判据——包括**故意改坏一处、看校验红不红**
（一条只会绿的校验等于没有校验）。
"""
from __future__ import annotations

import json

import pytest

docx = pytest.importorskip("docx", reason="python-docx 没装（可选依赖）")

from scripts.annotate_docx import (  # noqa: E402
    ADDITION_COLOR,
    annotate,
    fingerprint,
    main,
    verify,
)


@pytest.fixture()
def sample(tmp_path):
    """三段正文的合成申报书。"""
    d = docx.Document()
    d.add_paragraph("一、项目概述")
    d.add_paragraph("本项目模拟两位古代名医的辨证思路，并把分歧摆出来。")
    d.add_paragraph("二、技术路线")
    d.add_paragraph("检索层 + 推理链 + 安全层。")
    path = tmp_path / "申报书.docx"
    d.save(str(path))
    return path


def test_fingerprint_records_every_run(sample):
    fp = fingerprint(sample)
    assert fp["n_paragraphs"] == 4
    assert fp["n_black_runs"] >= 4
    assert fp["n_additions"] == 0, "原件里不该有补充色的 run"
    assert len(fp["paragraphs"]) == 4
    assert fp["color"] == ADDITION_COLOR


def test_annotate_adds_red_runs_and_leaves_the_original_untouched(sample, tmp_path):
    base = fingerprint(sample)
    out = tmp_path / "已补充.docx"
    result = annotate(sample, [{"anchor": "二、技术路线",
                                "text": "本轮补充：符号验证器七条规则。"}], out)
    assert result["color"] == ADDITION_COLOR
    report = verify(out, base)
    assert report["ok"] is True, report
    assert report["n_additions"] == 1
    # 补的那句话真的在文档里，且是红的
    d = docx.Document(str(out))
    reds = [r for p in d.paragraphs for r in p.runs
            if r.font.color and r.font.color.rgb
            and str(r.font.color.rgb).upper() == ADDITION_COLOR]
    assert len(reds) == 1 and "符号验证器" in reds[0].text


def test_the_addition_lands_right_after_its_anchor(sample, tmp_path):
    out = tmp_path / "已补充.docx"
    annotate(sample, [{"anchor": "一、项目概述", "text": "补在第一节下面。"}], out)
    texts = [p.text for p in docx.Document(str(out)).paragraphs]
    assert texts[0] == "一、项目概述"
    assert texts[1] == "补在第一节下面。", "补充内容没落在锚点后面：" + str(texts)
    # 原来的第二段还在，只是往后挪了一位（段落序号后移是预期内的）
    assert "两位古代名医" in texts[2]


def test_a_missing_anchor_is_an_error_not_a_guess(sample, tmp_path):
    """猜一个位置插进去 = 把补充内容放在错的章节下面，**比没加更糟**。"""
    with pytest.raises(ValueError) as e:
        annotate(sample, [{"anchor": "九、根本不存在的一节", "text": "x"}],
                 tmp_path / "x.docx")
    assert "找不到锚点" in str(e.value)


def test_an_ambiguous_anchor_is_an_error_too(tmp_path):
    d = docx.Document()
    d.add_paragraph("小结")
    d.add_paragraph("正文一")
    d.add_paragraph("小结")
    path = tmp_path / "dup.docx"
    d.save(str(path))
    with pytest.raises(ValueError) as e:
        annotate(path, [{"anchor": "小结", "text": "x"}], tmp_path / "y.docx")
    assert "出现了 2 次" in str(e.value)


def test_an_empty_addition_is_rejected(sample, tmp_path):
    with pytest.raises(ValueError):
        annotate(sample, [{"anchor": "一、项目概述", "text": "   "}], tmp_path / "z.docx")


def test_verify_turns_red_when_a_black_run_is_edited(sample, tmp_path):
    """**这一条是这个文件存在的理由**：一条只会绿的校验等于没有校验。
    故意改掉一处黑字，校验必须报出是哪一段哪一个 run。"""
    base = fingerprint(sample)
    d = docx.Document(str(sample))
    d.paragraphs[1].runs[0].text = "本项目模拟三位古代名医的辨证思路。"   # 动了原文
    tampered = tmp_path / "被改过.docx"
    d.save(str(tampered))
    report = verify(tampered, base)
    assert report["ok"] is False
    assert report["run_diff"], "改了原文却没报出来"
    # 报出来的必须是**哪一条**对不上，不是一句"有变化"
    assert report["run_diff"]["was_len"] != report["run_diff"]["now_len"]


def test_verify_notices_a_deleted_run(sample, tmp_path):
    base = fingerprint(sample)
    d = docx.Document(str(sample))
    p = d.paragraphs[3]
    p._element.getparent().remove(p._element)      # 整段删掉
    out = tmp_path / "删过.docx"
    d.save(str(out))
    report = verify(out, base)
    assert report["ok"] is False
    assert report["paragraph_diff"] or report["run_diff"], "删掉一整段却没报出来"


def test_reopening_and_saving_without_edits_still_verifies(sample, tmp_path):
    """python-docx 打开再保存会重写整个包（XML 重排、rsid 变化），
    **文件层面 diff 必然不一样**——所以校验的口径定在文本层。这条钉住那个选择：
    原封不动地存一遍，校验必须是绿的。"""
    base = fingerprint(sample)
    out = tmp_path / "重存.docx"
    docx.Document(str(sample)).save(str(out))
    # 字节一不一样不作断言（不同版本的 python-docx 行为不同）；
    # 要钉的是**文本层的校验必须绿**——那正是选这个口径的理由。
    assert verify(out, base)["ok"] is True


def test_previous_additions_are_not_treated_as_original_text(sample, tmp_path):
    """上一轮补的红字不算原文——允许再改。否则第二轮补充会把第一轮的红字
    锁死，而那不是"黑字不许动"这条规则要保护的东西。"""
    first = tmp_path / "第一轮.docx"
    annotate(sample, [{"anchor": "一、项目概述", "text": "第一轮补的。"}], first)
    fp1 = fingerprint(first)
    assert fp1["n_additions"] == 1
    d = docx.Document(str(first))
    for p in d.paragraphs:
        for r in p.runs:
            if r.font.color and r.font.color.rgb and \
                    str(r.font.color.rgb).upper() == ADDITION_COLOR:
                r.text = "第一轮补的内容改了。"
    second = tmp_path / "第二轮.docx"
    d.save(str(second))
    assert verify(second, fp1)["ok"] is True


def test_cli_round_trip(sample, tmp_path, capsys):
    """三个子命令串起来跑一遍：拍指纹 → 补充 → 校验，退出码都要是 0。"""
    fp_path = tmp_path / "fp.json"
    assert main(["fingerprint", str(sample), "--out", str(fp_path)]) == 0
    add_path = tmp_path / "additions.json"
    add_path.write_text(json.dumps(
        {"additions": [{"anchor": "二、技术路线", "text": "补充一句。"}]},
        ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out.docx"
    assert main(["annotate", str(sample), "--additions", str(add_path),
                 "--fingerprint", str(fp_path), "--out", str(out)]) == 0
    report_path = tmp_path / "verify.json"
    assert main(["verify", str(out), "--fingerprint", str(fp_path),
                 "--json-out", str(report_path)]) == 0
    printed = capsys.readouterr().out
    assert "原文未被改动" in printed
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["ok"] is True and saved["n_additions"] == 1


def test_cli_verify_exits_nonzero_when_the_original_changed(sample, tmp_path):
    """校验失败要**退出码非 0**——上机时它是一道闸，不是一行日志。"""
    fp_path = tmp_path / "fp.json"
    main(["fingerprint", str(sample), "--out", str(fp_path)])
    d = docx.Document(str(sample))
    d.paragraphs[1].runs[0].text = "改了"
    bad = tmp_path / "bad.docx"
    d.save(str(bad))
    assert main(["verify", str(bad), "--fingerprint", str(fp_path)]) == 1


def test_the_addition_colour_has_exactly_one_definition():
    """`E54C5E` 只能有一处定义——脚本、文档、报告都引它。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    src = (root / "scripts" / "annotate_docx.py").read_text(encoding="utf-8")
    assert src.count('"E54C5E"') == 1, "颜色值在脚本里出现了不止一次"
    assert "ADDITION_COLOR" in src
