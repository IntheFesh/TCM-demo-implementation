"""scripts/train_lora.py 的离线测试。**不装 transformers / torch / peft 也要全绿**：
那几个依赖在 requirements-train.txt 里、只有训练机才装，所以脚本里凡是要它们的
代码都在函数内部 import，纯逻辑（过滤、渲染、tokenize 的 label 掩码、gap 计算、
目录布局）都能在沙盒里测。"""
import json

import pytest

from scripts.train_lora import (
    BASES,
    OVERFIT_RELATIVE_GAP,
    build_plan,
    count_truncated,
    filter_for_physician,
    format_gap_text,
    format_plan_text,
    gap_report,
    load_chain_samples,
    lora_dir_hint,
    output_dir,
    render_example,
    resolve_base,
    split_samples,
    tokenize_example,
)


def _case_sample(physician="ye_tianshi", split="train", case_id="ye_tianshi-001"):
    return {
        "input": "面肿；喘，舌绛",
        "chain": [
            {"step": "症状→证素", "output": ["肺", "寒"], "rationale": "风寒外束，肺气不宣。",
             "source": "standard:TB-001", "rationale_source": "standard:TB-001"},
            {"step": "证型→治法", "output": "清肃上焦", "rationale": "治以清肃上焦",
             "source": f"case:{case_id}", "rationale_source": f"case:{case_id}"},
            {"step": "方剂→药材", "output": [
                {"name": "飞滑石", "rationale": None, "rationale_source": None},
                {"name": "杏仁", "rationale": "杏仁降气止咳平喘",
                 "rationale_source": "materia_medica:中药学"}],
             "rationale": None, "source": f"case:{case_id}", "rationale_source": None}],
        "meta": {"source_kind": "case", "physician_id": physician, "case_id": case_id,
                 "case_group_id": case_id, "split": split, "split_source": "case_group_id"},
    }


def _sdt_sample(split="train"):
    return {
        "input": "胃脘胀痛",
        "chain": [
            {"step": "症状→病机", "output": "肝气横逆", "rationale": "肝气横逆犯胃",
             "source": "sdt:病例30", "rationale_source": "sdt:病例30"},
            {"step": "病机→证型", "output": "肝胃不和证", "rationale": None,
             "source": "sdt:病例30", "rationale_source": None}],
        "meta": {"source_kind": "sdt", "physician_id": None, "record_id": "病例30",
                 "split": split, "split_source": "sdt:Train"},
    }


# ---------- 基座（两个都要能跑，所以是参数不是常量） ----------


def test_both_bases_are_declared_and_one_is_marked_unverified():
    """两个基座都要能跑。`verified` 为 False 的那个是如实标注——R4 那轮六条下载 URL
    全靠猜、六条全错，所以这里不假装核对过。"""
    assert set(BASES) == {"qwen2.5-1.5b", "zhongjing-2-1.8b"}
    assert BASES["qwen2.5-1.5b"]["verified"] is True
    assert BASES["zhongjing-2-1.8b"]["verified"] is False


def test_resolve_base_returns_the_key_and_rejects_unknown():
    assert resolve_base("qwen2.5-1.5b")["model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert resolve_base("qwen2.5-1.5b")["key"] == "qwen2.5-1.5b"
    with pytest.raises(ValueError, match="未知基座"):
        resolve_base("llama3")


def test_base_model_override_also_clears_the_unverified_flag():
    """人手传进来的仓库 id，责任在传的人，不该再对他警告一遍。"""
    base = resolve_base("zhongjing-2-1.8b", "/root/models/zhongjing")
    assert base["model"] == "/root/models/zhongjing" and base["verified"] is True


# ---------- 样本读取与按医家过滤 ----------


def test_load_chain_samples_rejects_alpaca_format_instead_of_skipping(tmp_path):
    """alpaca 样本混进来不会报错、只会静默训出完全不同的东西，所以要硬失败。"""
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"instruction": "x", "input": "y", "output": "z",
                                "meta": {}}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="alpaca"):
        load_chain_samples(path)


def test_load_chain_samples_tells_you_how_to_produce_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="export_sft"):
        load_chain_samples(tmp_path / "nope.jsonl")


def test_load_chain_samples_skips_blank_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps(_case_sample(), ensure_ascii=False) + "\n\n", encoding="utf-8")
    assert len(load_chain_samples(path)) == 1


def test_filter_for_physician_keeps_shared_samples_whose_physician_is_none():
    """**这一条是 R5-3 最容易静默出错的地方。** physician_id=None 的是 SDT 那批
    专家标注的通用辨证链路，两位医家共用。当成"没有医家所以丢掉"不会报错，
    只会让每个 adapter 都少掉这批最干净的标注。"""
    samples = [_case_sample("ye_tianshi"), _case_sample("wu_jutong", case_id="wu-1"),
               _sdt_sample()]
    mine = filter_for_physician(samples, "ye_tianshi")
    assert [s["meta"].get("physician_id") for s in mine] == ["ye_tianshi", None]
    theirs = filter_for_physician(samples, "wu_jutong")
    assert [s["meta"].get("physician_id") for s in theirs] == ["wu_jutong", None]


def test_split_samples_uses_the_split_the_export_already_decided():
    """训练脚本**不重切**：切分在导出时按 case_group_id 做完了（SDT 用自带的），
    再切一次会让"train 和 heldout 无交集"这个保证失效。"""
    buckets = split_samples([_case_sample(split="train"),
                             _case_sample(split="heldout", case_id="b"),
                             _sdt_sample()])
    assert len(buckets["train"]) == 2 and len(buckets["heldout"]) == 1
    assert buckets["unlabeled"] == []


def test_split_samples_puts_unlabeled_in_its_own_bucket_not_silently_into_train():
    sample = _case_sample()
    del sample["meta"]["split"]
    buckets = split_samples([sample])
    assert len(buckets["unlabeled"]) == 1 and buckets["train"] == []


# ---------- 渲染：出处 id 不进训练目标 ----------


def test_render_example_never_puts_source_ids_into_the_completion():
    """教模型背医案 id，它在推理时就会凭记忆编一个 id 出来——那正是
    「每条结论必须引用真实医案 id」要防的事。依据原文要进（那是要教的东西），
    出处 id 不进（那是要靠检索层给的东西）。"""
    rendered = render_example(_case_sample())
    for forbidden in ("case:", "standard:", "materia_medica:", "sdt:", "formulary:"):
        assert forbidden not in rendered["completion"]
    # 依据原文本身要在
    assert "治以清肃上焦" in rendered["completion"]
    assert "杏仁降气止咳平喘" in rendered["completion"]


def test_render_example_omits_the_rationale_line_instead_of_writing_a_placeholder():
    """没有依据的步骤就不写依据行。写「依据：无」会被模型学成口头禅，而且跟
    「拿不到就填 null，不许编」是同一条约束的两个面。"""
    rendered = render_example(_sdt_sample())
    assert rendered["completion"].count("依据：") == 1      # 只有第一步有依据
    assert "无" not in rendered["completion"]
    # 没有依据的药材也不写依据行（飞滑石）
    herb_completion = render_example(_case_sample())["completion"]
    assert "飞滑石 依据" not in herb_completion


def test_render_example_lists_each_herb_with_its_own_rationale():
    completion = render_example(_case_sample())["completion"]
    assert "方剂→药材：飞滑石、杏仁" in completion
    assert "  杏仁 依据：杏仁降气止咳平喘" in completion


def test_render_example_prompt_carries_the_instruction_and_the_patient_text():
    rendered = render_example(_case_sample())
    assert "不要编" in rendered["prompt"]
    assert "面肿；喘，舌绛" in rendered["prompt"]


# ---------- tokenize：prompt 部分不算 loss ----------


class _FakeTokenizer:
    """一个字一个 token 的假分词器。真分词器要装 transformers（训练机才装），
    而这里要测的是 label 掩码和截断这两件纯逻辑。"""

    eos_token = "<eos>"

    def __call__(self, text, add_special_tokens=True):
        return {"input_ids": [ord(c) for c in text.replace(self.eos_token, "\x00")]}


def test_tokenize_masks_the_prompt_so_loss_only_covers_the_completion():
    """不 mask 的话模型会把那段固定指令也学一遍，loss 被它稀释，
    train/heldout 的 loss 差就不再反映学到了多少辨证。"""
    tok = _FakeTokenizer()
    example = {"prompt": "问：", "completion": "答"}
    row = tokenize_example(tok, example, max_len=999)
    assert row["labels"][:2] == [-100, -100]                 # prompt 的两个字
    assert row["labels"][2] == ord("答")                      # completion 从这里开始算 loss
    assert len(row["input_ids"]) == len(row["labels"]) == len(row["attention_mask"])


def test_tokenize_truncates_at_max_len_and_truncation_is_countable():
    tok = _FakeTokenizer()
    long = {"prompt": "问" * 10, "completion": "答" * 100}
    row = tokenize_example(tok, long, max_len=20)
    assert len(row["input_ids"]) == 20 and len(row["labels"]) == 20
    assert count_truncated([row], 20) == 1
    short = tokenize_example(tok, {"prompt": "问", "completion": "答"}, max_len=20)
    assert count_truncated([short], 20) == 0


# ---------- 输出目录必须跟推理侧的查找规则对齐 ----------


def test_output_dir_layout_is_exactly_what_the_backend_looks_for(tmp_path):
    """core/llm.py 的 `_resolve_lora_path` 找的是 `$LORA_DIR/<physician_id>`。
    布局错一级的后果是静默的：它会抛异常（刻意不退化成基座模型），但那是推理时
    才炸，不是训练时。所以这里直接拿推理侧那个函数来验，不靠注释约定。"""
    from core.llm import _resolve_lora_path

    out = output_dir(tmp_path, "qwen2.5-1.5b", "ye_tianshi")
    out.mkdir(parents=True)
    hint = lora_dir_hint(tmp_path, "qwen2.5-1.5b")
    lora_dir = hint.split("=", 1)[1]
    assert _resolve_lora_path(lora_dir, "ye_tianshi") == out


def test_lora_dir_hint_points_at_the_base_level_not_the_physician_level(tmp_path):
    hint = lora_dir_hint(tmp_path, "qwen2.5-1.5b")
    assert hint.startswith("export LORA_DIR=")
    assert hint.endswith("qwen2.5-1.5b")


# ---------- train/heldout gap 与过拟合警告 ----------


def test_gap_report_reports_the_gap_and_its_baseline():
    """heldout loss 的对照是**训练前**同一批 heldout 上的 loss。没有它，
    「heldout loss 1.8」这个数说明不了任何事。"""
    r = gap_report(train_loss=1.0, heldout_loss=1.1, baseline_heldout_loss=2.0)
    assert r["gap"] == pytest.approx(0.1)
    assert r["relative_gap"] == pytest.approx(0.1)
    assert r["heldout_improvement"] == pytest.approx(0.9)
    assert r["overfit_warning"] is None


def test_gap_report_warns_loudly_past_the_threshold():
    r = gap_report(train_loss=1.0, heldout_loss=1.0 + OVERFIT_RELATIVE_GAP + 0.01)
    assert r["overfit_warning"] and "过拟合警告" in r["overfit_warning"]
    # 警告要把人指向下游指标，而不是宣布训练失败——这条线是约定不是实测值
    assert "E3" in r["overfit_warning"] and "MES" in r["overfit_warning"]


def test_gap_report_at_exactly_the_threshold_does_not_warn():
    assert gap_report(1.0, 1.0 + OVERFIT_RELATIVE_GAP)["overfit_warning"] is None


def test_gap_report_says_unknown_not_fine_when_heldout_is_empty():
    """heldout 为空时过拟合完全不可观测。报"没问题"是错的，要报"不知道"。"""
    r = gap_report(train_loss=1.0, heldout_loss=None)
    assert r["gap"] is None
    assert "不知道" in r["overfit_warning"]


def test_format_gap_text_shows_dashes_for_missing_numbers():
    text = format_gap_text("qwen2.5-1.5b", "ye_tianshi", gap_report(1.0, None))
    assert "heldout —" in text and "qwen2.5-1.5b / ye_tianshi" in text


# ---------- 计划：动 GPU 之前先把数打出来 ----------


def test_build_plan_counts_shared_samples_separately_per_adapter():
    """共用样本在每个 adapter 里都被算了一遍，不单列会让人以为样本总数是
    各医家之和。"""
    samples = [_case_sample("ye_tianshi"), _case_sample("wu_jutong", case_id="wu-1"),
               _sdt_sample()]
    plan = build_plan(samples, ["ye_tianshi", "wu_jutong"], ["qwen2.5-1.5b"], "/out")
    assert plan["samples_total"] == 3
    assert plan["by_physician"] == {"ye_tianshi": 1, "wu_jutong": 1, "None": 1}
    assert [j["train"] for j in plan["jobs"]] == [2, 2]        # 各自 1 条 + 共用 1 条
    assert [j["shared"] for j in plan["jobs"]] == [1, 1]
    assert plan["jobs"][0]["out_dir"].endswith("/qwen2.5-1.5b/ye_tianshi")


def test_plan_text_warns_when_an_adapter_has_no_heldout():
    samples = [_case_sample("ye_tianshi")]
    plan = build_plan(samples, ["ye_tianshi"], ["qwen2.5-1.5b"], "/out")
    text = format_plan_text(plan, [resolve_base("qwen2.5-1.5b")])
    assert "heldout 为 0" in text and "不要拿去报数字" in text


def test_plan_text_marks_the_unverified_base_repo_id():
    plan = build_plan([_case_sample()], ["ye_tianshi"], ["zhongjing-2-1.8b"], "/out")
    text = format_plan_text(plan, [resolve_base("zhongjing-2-1.8b")])
    assert "未在真机核对过" in text and "--base-model" in text


def test_plan_text_warns_about_unlabeled_samples():
    sample = _case_sample()
    del sample["meta"]["split"]
    plan = build_plan([sample], ["ye_tianshi"], ["qwen2.5-1.5b"], "/out")
    text = format_plan_text(plan, [resolve_base("qwen2.5-1.5b")])
    assert "没有 split 标记" in text


# ---------- CLI ----------


def _write_samples(tmp_path):
    path = tmp_path / "sft_chain.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in
                              [_case_sample("ye_tianshi"),
                               _case_sample("wu_jutong", split="heldout", case_id="wu-1"),
                               _sdt_sample()]), encoding="utf-8")
    return path


def test_main_dry_run_prints_the_plan_and_a_rendered_sample_without_loading_a_model(tmp_path, capsys):
    from scripts import train_lora

    train_lora.main(["--samples", str(_write_samples(tmp_path)),
                     "--out-dir", str(tmp_path / "lora"), "--dry-run"])
    out = capsys.readouterr().out
    assert "要训的 adapter" in out
    assert "export LORA_DIR=" in out
    assert "--- completion ---" in out
    assert "没有加载模型，没有训练" in out


def test_main_trains_both_bases_by_default(tmp_path, capsys):
    from scripts import train_lora

    train_lora.main(["--samples", str(_write_samples(tmp_path)),
                     "--out-dir", str(tmp_path / "lora"), "--dry-run"])
    out = capsys.readouterr().out
    assert "qwen2.5-1.5b" in out and "zhongjing-2-1.8b" in out


def test_main_resolves_a_physician_chinese_name_at_the_cli_boundary(tmp_path, capsys):
    """CLAUDE.md「标识符只有一种规范形式，边界上统一解析」：CLI 是边界，
    中文名要在这里过 resolve_physician_id，不是在过滤处直接比较。"""
    from scripts import train_lora

    train_lora.main(["--samples", str(_write_samples(tmp_path)), "--physician", "叶天士",
                     "--out-dir", str(tmp_path / "lora"), "--base", "qwen2.5-1.5b",
                     "--dry-run"])
    out = capsys.readouterr().out
    assert "/qwen2.5-1.5b/ye_tianshi" in out
    # 只训一个 adapter。（吴鞠通仍会出现在"整份数据按医家的分布"那一行——那一行报的
    # 是数据集构成，不是这次要训什么，两者不能混着断言。）
    assert out.count("qwen2.5-1.5b / ") == 1
    assert "qwen2.5-1.5b / wu_jutong" not in out


def test_main_rejects_an_unknown_physician_with_the_available_values(tmp_path):
    from scripts import train_lora

    with pytest.raises(SystemExit, match="认不出医家"):
        train_lora.main(["--samples", str(_write_samples(tmp_path)),
                         "--physician", "李时珍", "--dry-run"])


def test_main_refuses_base_model_override_without_saying_which_base(tmp_path):
    from scripts import train_lora

    with pytest.raises(SystemExit, match="--base"):
        train_lora.main(["--samples", str(_write_samples(tmp_path)),
                         "--base-model", "/root/models/x", "--dry-run"])
