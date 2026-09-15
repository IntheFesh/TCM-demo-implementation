"""阶段五（M16）：用 sft_chain.jsonl 按医家训 LoRA。

**这一轮只准备脚本，不跑训练。** 训练的三个前提（实施总纲阶段五）：
E3 ≥ 40%（✅ 0.451）、SDT 有基线（✅ chain 23.173 / baseline 22.068）、
MES 盲评指出哪个维度弱（⏳ 要人评）。第三条没满足就没有靶子——那时候训出来的
模型不知道该拿什么判断它变好了没有。

    # 先导出训练数据（三源合并，见 offline/export_sft.py）
    python -m offline.export_sft --format chain --sdt-dir $SDT --out sft_chain.jsonl

    # 看计划：样本数、每位医家分到多少、输出目录、第一条样本长什么样（不加载模型）
    python -m scripts.train_lora --dry-run

    # 真训（两个基座 × 两位医家 = 四个 adapter）
    python -m scripts.train_lora --out-dir /root/autodl-tmp/lora

    # 只训一个基座、一位医家
    python -m scripts.train_lora --base qwen2.5-1.5b --physician ye_tianshi

**输出目录布局跟 core/llm.py 的 `_resolve_lora_path` 对齐**：它找的是
`$LORA_DIR/<physician_id>/`，所以这里写到 `<out-dir>/<基座>/<physician_id>/`，
推理时 `LORA_DIR` 指向**选定那个基座**的那一层。脚本最后会把每个基座对应的
`export LORA_DIR=…` 原样打出来，不用自己拼。布局错一级的后果是静默的：
`_resolve_lora_path` 会抛异常（它刻意不静默退化成基座模型），但那是在推理时
才炸，不是训练时。

**训练目标是链路本身，不是 S3Syndrome 的 JSON。** 把链路输出接回 S3 的结构化
输出格式是另一件事，这一轮不做，也不假装做了。

**目标里不含出处 id。** 链路样本里每步都带 `source`（`case:ye_tianshi-0012-p3-0`
这类），但那些 id **不进训练目标**：教模型背医案 id，它在推理时就会凭记忆编一个
id 出来，而那正是「每条结论必须引用真实医案 id」要防的事。推理时 id 由检索层放进
prompt、由 S3 的既有链路带出来，不靠模型记住。依据原文（`rationale`）进目标——
那是要教的东西（说出你的依据），不是要背的东西。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from core.physicians import (
    PHYSICIANS,
    physician_choices_text,
    physicians_all,
    resolve_physician_id,
)
from offline.export_sft import ITEMIZED_STEPS

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLES_PATH = ROOT / "sft_chain.jsonl"
DEFAULT_OUT_DIR = ROOT / "lora_out"

# 两个基座都要能跑，所以基座是**参数**不是写死的常量。RTX 5090 32GB 上 1.5B~1.8B
# 全参微调也塞得下，用 LoRA 是为了按医家出多个 adapter 共享一份基座权重
# （vLLM 一个进程按请求切 adapter，见 VLLMBackend），不是因为显存不够。
#
# ⚠ `verified` 为 False 的仓库 id **没在真机核对过**（沙盒没网络）。R4 那轮六条
# 下载 URL 全靠猜、六条全错，所以这里如实标出来而不是假装验过：跑的时候脚本会
# 把未核对的 id 打出来提醒，404 就用 --base-model 传真实 id。
BASES: dict[str, dict] = {
    "qwen2.5-1.5b": {
        "model": "Qwen/Qwen2.5-1.5B-Instruct",
        "verified": True,
        "note": "通用基座。中医知识靠 SFT 注入，好处是没有别人的中医偏见混进来",
    },
    "zhongjing-2-1.8b": {
        "model": "CMLM/ZhongJing-2-1_8b",
        "verified": False,
        "note": "已在中医语料上继续预训练过的基座。对照的意义是回答「先验中医知识"
                "有没有用」——它跟通用基座的差值就是那个答案",
    },
}

# LoRA 超参。这几个数是 peft 文档里 1B 级模型的常规起点，**不是在这个数据集上
# 调过的**——调参要有验证集上的靶子，而靶子（MES）还没有。写死在这里并标明来历，
# 比散在命令行里让人以为它们被调过要好。
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
# Qwen 系的注意力/MLP 线性层名。换基座（比如换成 Llama 架构）要跟着改——
# 名字对不上时 peft 会报 "Target modules ... not found"，不会静默少挂几层。
LORA_TARGET_MODULES = ("q_proj", "k_proj", "v_proj", "o_proj",
                       "gate_proj", "up_proj", "down_proj")

# 过拟合的早期警报线：heldout loss 比 train loss 高出 20% 就警告。
# **这是一个约定，不是实测值**——真正的判据是下游指标（E3/SDT/MES），loss gap
# 只是"训练已经开始背样本了"的早期信号。所以警告的措辞是"去看下游指标"，
# 不是"训练失败了"。
OVERFIT_RELATIVE_GAP = 0.20

PROMPT_HEADER = ("按辨证链路逐步作答。每一步给出结论；有原文依据的把依据一并写出，"
                 "没有原文依据的不要编。")


def resolve_base(key: str, base_model: str | None = None) -> dict:
    """基座 key → {key, model, verified, note}。--base-model 覆盖仓库 id
    （标着未核对的那个 404 时用，也可以用来传本地已下载的路径）。"""
    if key not in BASES:
        raise ValueError(f"未知基座 {key!r}，可用：{sorted(BASES)}")
    info = dict(BASES[key], key=key)
    if base_model:
        info["model"] = base_model
        info["verified"] = True      # 人手传进来的 id，责任在传的人，不再警告
        info["note"] = info["note"] + "（仓库 id 由 --base-model 指定）"
    return info


def load_chain_samples(path: Path) -> list[dict]:
    """读 sft_chain.jsonl。只认 chain 格式——alpaca 格式没有 chain 字段，
    混进来会静默训出完全不同的东西，所以这里直接报错而不是跳过。"""
    if not path.exists():
        raise FileNotFoundError(
            f"未找到 {path}。先跑 python -m offline.export_sft --format chain --out {path}")
    samples: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "chain" not in row or "input" not in row:
            raise ValueError(
                f"{path} 第 {i} 行没有 chain/input 字段——这看起来是 alpaca 格式的样本。"
                f"训练链路要用 --format chain 导出的文件。")
        samples.append(row)
    return samples


def filter_for_physician(samples: list[dict], physician_id: str) -> list[dict]:
    """按医家过滤：该医家的样本 **加上** `physician_id` 为 None 的共用样本。

    共用样本是 SDT 那 200 条专家标注的辨证链路——它是通用中医知识，不属于任何一位
    医家。把 None 当成「没有医家所以丢掉」会让每个 adapter 都少掉这批最干净的标注；
    当成「两位医家共用」才是对的。这一条必须有测试钉住：丢了它不会报错，只会让
    训练集静静变小。
    """
    return [s for s in samples
            if s["meta"].get("physician_id") in (physician_id, None)]


def split_samples(samples: list[dict]) -> dict[str, list[dict]]:
    """按样本自带的 `meta.split` 分桶。**这里不切分**——切分在导出时按
    case_group_id 做完了（SDT 用它自带的 split），训练脚本再切一次就会让
    「训练集和 heldout 无交集」这个保证失效。没有 split 字段的样本算 train
    并在计划里报出来，不静默塞进某一侧。"""
    out: dict[str, list[dict]] = {"train": [], "heldout": [], "unlabeled": []}
    for s in samples:
        split = s["meta"].get("split")
        out[split if split in ("train", "heldout") else "unlabeled"].append(s)
    return out


def _render_step(step: dict) -> list[str]:
    lines: list[str] = []
    if step["step"] in ITEMIZED_STEPS:
        items = step["output"]
        lines.append(f"{step['step']}：{'、'.join(i['name'] for i in items)}")
        for item in items:
            if item.get("rationale"):
                lines.append(f"  {item['name']} 依据：{item['rationale']}")
        return lines
    output = step["output"]
    text = "、".join(output) if isinstance(output, list) else str(output)
    lines.append(f"{step['step']}：{text}")
    if step.get("rationale"):
        lines.append(f"  依据：{step['rationale']}")
    return lines


def render_example(sample: dict) -> dict:
    """一条链路样本 → {prompt, completion}。**出处 id 不进 completion**，
    理由见模块文档字符串。没有 rationale 的步骤就不写依据行——不写「依据：无」，
    那种占位文字会被模型学成一个口头禅。"""
    lines: list[str] = []
    for step in sample["chain"]:
        lines.extend(_render_step(step))
    return {
        "prompt": f"{PROMPT_HEADER}\n\n患者：{sample['input']}\n",
        "completion": "\n".join(lines),
    }


def tokenize_example(tokenizer, example: dict, max_len: int) -> dict:
    """prompt 部分的 label 置 -100：只在 completion 上算 loss。不 mask 的话
    模型会把「按辨证链路逐步作答……」这段固定指令也学一遍，loss 被它稀释，
    train/heldout 的 loss 差就不再反映"学到了多少辨证"。

    截断在 max_len 处。**截断的条数要报出来**（见 count_truncated）：一条被截断的
    样本等于教模型说半句话就停，多了会体现在输出上而不会体现在任何报错里。
    """
    prompt_ids = tokenizer(example["prompt"], add_special_tokens=False)["input_ids"]
    completion_ids = tokenizer(example["completion"] + (tokenizer.eos_token or ""),
                               add_special_tokens=False)["input_ids"]
    input_ids = (prompt_ids + completion_ids)[:max_len]
    labels = ([-100] * len(prompt_ids) + completion_ids)[:max_len]
    return {"input_ids": input_ids, "labels": labels,
            "attention_mask": [1] * len(input_ids)}


def count_truncated(rows: list[dict], max_len: int) -> int:
    return sum(1 for r in rows if len(r["input_ids"]) >= max_len)


def output_dir(out_root: Path, base_key: str, physician_id: str) -> Path:
    """`<out-dir>/<基座>/<physician_id>/`。最后一级必须是 physician_id：
    core/llm.py 的 `_resolve_lora_path` 找的就是 `$LORA_DIR/<physician_id>`，
    所以推理时 LORA_DIR 指向的是中间那一层（某个基座的目录）。"""
    return Path(out_root) / base_key / physician_id


def lora_dir_hint(out_root: Path, base_key: str) -> str:
    return f"export LORA_DIR={Path(out_root) / base_key}"


def gap_report(train_loss: float | None, heldout_loss: float | None,
               baseline_heldout_loss: float | None = None) -> dict:
    """train/heldout 的 loss 差 + 过拟合警告。

    **每个数都带对照**（CLAUDE.md）：
      - heldout loss 的对照是 `baseline_heldout_loss` = **训练前**同一批 heldout 上的
        loss。LoRA 的 B 矩阵初始化为 0，所以训练前的 peft 模型在数值上就是基座——
        这个基线不需要再加载一次模型，跑一次 evaluate 就有。没有它，「heldout loss
        1.8」这个数说明不了任何事。
      - train loss 的对照是 heldout loss，两者之差就是 gap。
    """
    out: dict = {
        "train_loss": train_loss, "heldout_loss": heldout_loss,
        "baseline_heldout_loss": baseline_heldout_loss,
        "gap": None, "relative_gap": None,
        "heldout_improvement": None, "overfit_warning": None,
    }
    if baseline_heldout_loss is not None and heldout_loss is not None:
        out["heldout_improvement"] = baseline_heldout_loss - heldout_loss
    if train_loss is None or heldout_loss is None:
        out["overfit_warning"] = (
            "⚠ 没有 heldout loss，train/heldout gap 算不出来。这不是「没问题」，是「不知道」"
            "——heldout 为空（--heldout-ratio 给了 0，或者这位医家的样本全落在一侧）时"
            "过拟合完全不可观测，训出来的 adapter 不该拿去报任何数字。"
        )
        return out
    out["gap"] = heldout_loss - train_loss
    out["relative_gap"] = (heldout_loss - train_loss) / train_loss if train_loss else None
    if out["relative_gap"] is not None and out["relative_gap"] > OVERFIT_RELATIVE_GAP:
        out["overfit_warning"] = (
            f"⚠⚠ 过拟合警告：heldout loss {heldout_loss:.4f} 比 train loss "
            f"{train_loss:.4f} 高 {out['relative_gap']:.1%}（警报线 "
            f"{OVERFIT_RELATIVE_GAP:.0%}）。这条线是约定不是实测值，所以它不判"
            f"训练失败，它让你去看下游指标：E3/E4 改变率、SDT chain−baseline、"
            f"MES 盲评。下游没变好而 train loss 一直降，就是在背样本。"
        )
    return out


def format_gap_text(base_key: str, physician_id: str, report: dict) -> str:
    def fmt(x):
        return "—" if x is None else f"{x:.4f}"

    lines = [
        f"[{base_key} / {physician_id}] "
        f"train {fmt(report['train_loss'])} | heldout {fmt(report['heldout_loss'])} | "
        f"训练前 heldout {fmt(report['baseline_heldout_loss'])} | "
        f"gap {fmt(report['gap'])}"
        + (f"（{report['relative_gap']:.1%}）" if report["relative_gap"] is not None else ""),
    ]
    if report["heldout_improvement"] is not None:
        lines.append(f"  heldout 相比训练前降了 {report['heldout_improvement']:.4f}"
                     f"（这才是「训练有没有用」的数；train loss 降了多少不能回答这个问题）")
    if report["overfit_warning"]:
        lines.append("  " + report["overfit_warning"])
    return "\n".join(lines)


def build_plan(samples: list[dict], physician_ids: list[str], base_keys: list[str],
               out_root: Path) -> dict:
    """要训什么、每个 adapter 分到多少样本、写到哪。**先把这些数打出来再动 GPU**：
    某位医家 train 只有几十条、或者 heldout 是 0，在计划里就该看见，不该训完
    两小时才发现。"""
    by_split_all = split_samples(samples)
    jobs = []
    for base_key in base_keys:
        for pid in physician_ids:
            mine = filter_for_physician(samples, pid)
            buckets = split_samples(mine)
            jobs.append({
                "base": base_key, "physician_id": pid,
                "train": len(buckets["train"]), "heldout": len(buckets["heldout"]),
                "unlabeled": len(buckets["unlabeled"]),
                # 共用样本（physician_id=None）的条数单列：它在每个 adapter 里都被算了
                # 一遍，不标出来会让人以为样本总数是各医家之和
                "shared": sum(1 for s in mine if s["meta"].get("physician_id") is None),
                "out_dir": str(output_dir(out_root, base_key, pid)),
            })
    return {
        "samples_total": len(samples),
        "split_total": {k: len(v) for k, v in by_split_all.items()},
        "by_source_kind": _count_by(samples, "source_kind"),
        "by_physician": _count_by(samples, "physician_id"),
        "jobs": jobs,
        "lora_dir_hints": [lora_dir_hint(out_root, b) for b in base_keys],
    }


def _count_by(samples: list[dict], key: str) -> dict:
    from collections import Counter

    return dict(Counter(str(s["meta"].get(key)) for s in samples))


def format_plan_text(plan: dict, bases: list[dict]) -> str:
    lines = [
        f"样本总数 {plan['samples_total']}；按 split：{plan['split_total']}",
        f"按来源：{plan['by_source_kind']}；按医家（None=两家共用）：{plan['by_physician']}",
        "",
        "要训的 adapter：",
    ]
    for job in plan["jobs"]:
        lines.append(
            f"  {job['base']} / {job['physician_id']}：train {job['train']}"
            f"（其中共用 {job['shared']}）| heldout {job['heldout']}"
            f" → {job['out_dir']}")
        if job["unlabeled"]:
            lines.append(f"    ⚠ {job['unlabeled']} 条样本没有 split 标记，已算进 train")
        if job["train"] == 0:
            # R18-H：注册表从两位扩到五位之后，李可/王云启在 cases.json 里
            # 还没有条目时 train 会是 0。**必须在计划里就喊出来**：不喊的话
            # 五个 adapter 目录照样建出来，其中两个是空训练，看目录看不出区别。
            lines.append("    ⚠ train 为 0：这位医家在这份样本里没有任何条目，"
                         "训不出 adapter。先跑 offline/extract_cases_* 把他的医案"
                         "抽进 cases.json，再跑 offline/export_sft --format chain")
        if job["heldout"] == 0:
            lines.append("    ⚠ heldout 为 0：这个 adapter 的过拟合不可观测，"
                         "训出来不要拿去报数字")
    lines.append("")
    for b in bases:
        mark = "" if b["verified"] else "  ⚠ 仓库 id 未在真机核对过，404 就用 --base-model 传真实 id"
        lines.append(f"基座 {b['key']}：{b['model']}{mark}")
        lines.append(f"  {b['note']}")
    lines.append("")
    lines.append("训完推理时（选定一个基座）：")
    for hint in plan["lora_dir_hints"]:
        lines.append(f"  {hint}")
    return "\n".join(lines)


def train_one(base: dict, physician_id: str, samples: list[dict], out_dir: Path,
              epochs: float, lr: float, batch_size: int, grad_accum: int,
              max_len: int) -> dict:
    """真训一个 adapter，返回 gap_report 的结果。torch/transformers/peft 在函数里
    才 import——模块顶层 import 会让 --dry-run 和整套测试都得先装几个 G 的依赖
    （CLAUDE.md：加载模型/大文件的对象一律惰性初始化）。"""
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForSeq2Seq,
        Trainer,
        TrainingArguments,
    )

    buckets = split_samples(filter_for_physician(samples, physician_id))
    train_rows_src = buckets["train"] + buckets["unlabeled"]
    if not train_rows_src:
        raise SystemExit(f"{physician_id} 没有训练样本，先检查 sft_chain.jsonl 的 meta.physician_id")

    tokenizer = AutoTokenizer.from_pretrained(base["model"])
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    train_rows = [tokenize_example(tokenizer, render_example(s), max_len) for s in train_rows_src]
    heldout_rows = [tokenize_example(tokenizer, render_example(s), max_len)
                    for s in buckets["heldout"]]
    n_trunc = count_truncated(train_rows, max_len) + count_truncated(heldout_rows, max_len)
    if n_trunc:
        print(f"[train_lora] ⚠ {n_trunc}/{len(train_rows) + len(heldout_rows)} 条样本被截断到 "
              f"{max_len} token——截断的样本等于教模型说半句话就停。"
              f"要么加大 --max-len，要么先看看是哪些链路特别长。", file=sys.stderr)

    model = AutoModelForCausalLM.from_pretrained(base["model"], torch_dtype="auto")
    model = get_peft_model(model, LoraConfig(
        r=LORA_R, lora_alpha=LORA_ALPHA, lora_dropout=LORA_DROPOUT,
        target_modules=list(LORA_TARGET_MODULES), task_type="CAUSAL_LM",
    ))
    model.print_trainable_parameters()

    out_dir.mkdir(parents=True, exist_ok=True)
    trainer = Trainer(
        model=model,
        args=TrainingArguments(
            output_dir=str(out_dir / "_trainer"),
            per_device_train_batch_size=batch_size,
            per_device_eval_batch_size=batch_size,
            gradient_accumulation_steps=grad_accum,
            num_train_epochs=epochs, learning_rate=lr,
            logging_steps=10, save_strategy="no", report_to=[],
            bf16=True, remove_unused_columns=False,
        ),
        train_dataset=train_rows,
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100),
    )

    # 训练前先在 heldout 上量一次 = 这个基座的基线。LoRA 的 B 初始化为 0，
    # 此刻的 peft 模型在数值上就是基座本身，所以这个基线是免费的。
    baseline = trainer.evaluate(heldout_rows)["eval_loss"] if heldout_rows else None
    trainer.train()
    heldout_loss = trainer.evaluate(heldout_rows)["eval_loss"] if heldout_rows else None
    # train loss 也用 evaluate 量，不用训练过程里的滑动平均——那个数跟 heldout
    # 的 loss 不是同一个口径（一个带 dropout 和不同步的权重，一个不带），
    # 两个口径的数相减出来的 gap 没有意义。
    train_loss = trainer.evaluate(train_rows)["eval_loss"]

    model.save_pretrained(str(out_dir))
    tokenizer.save_pretrained(str(out_dir))
    return gap_report(train_loss, heldout_loss, baseline)


def report_deps() -> bool:
    """训练依赖装没装。**逐个报**而不是笼统说"环境不对"：这台机器上
    torch/transformers 装了、peft 没装，笼统报会让人重装一遍已经有的几个 G。

    R18-H 的现状：沙盒里 peft 缺失，所以 train_one 跑不了，只有 --dry-run 和
    --check-deps 能跑。这个函数就是那件事的**退出码**——不是在报告里写一句
    "环境不支持"就算完了。
    """
    import importlib.util

    need = {
        "torch": "pip install torch",
        "transformers": "pip install transformers",
        "peft": "pip install peft",
        "accelerate": "pip install accelerate",
    }
    missing = []
    for mod, how in need.items():
        if importlib.util.find_spec(mod) is None:
            missing.append((mod, how))
            print(f"  ✗ {mod} 未安装 → {how}")
        else:
            print(f"  ✓ {mod}")
    if missing:
        print(f"\n缺 {len(missing)} 个依赖，train_one 跑不了（--dry-run / --check-deps "
              f"仍然可跑）。一次装全：pip install -r requirements-train.txt")
        return False
    print("\n训练依赖齐了。")
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="按医家训 LoRA（阶段五 M16）。两个基座都能跑，并列出对照。")
    ap.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES_PATH,
                    help="offline/export_sft.py --format chain 的产出")
    ap.add_argument("--base", action="append", choices=sorted(BASES), default=None,
                    help="可重复。不传就是两个基座都训——同一份数据训两个基座，"
                         "对照是免费的，而「先验中医知识有没有用」只能靠这个对照回答。")
    ap.add_argument("--base-model", default=None,
                    help="覆盖仓库 id（未核对的那个 404 时用，或指向本地已下载的路径）。"
                         "只在 --base 恰好给一个时可用。")
    ap.add_argument("--physician", default="all",
                    help=f"医家 id 或中文名，或 all（默认，每位医家各训一个 adapter）。{physician_choices_text()}")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    ap.add_argument("--epochs", type=float, default=3.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--dry-run", action="store_true",
                    help="只打计划和第一条渲染好的样本，不加载模型、不训练")
    ap.add_argument("--check-deps", action="store_true",
                    help="只检查训练依赖装没装（peft/transformers/torch/accelerate），"
                         "缺哪个就报哪个，退出码非 0。不加载模型、不读样本")
    args = ap.parse_args(argv)

    if args.check_deps:
        return 0 if report_deps() else 1

    base_keys = args.base or sorted(BASES)
    if args.base_model and len(base_keys) != 1:
        raise SystemExit("--base-model 是给某一个基座换仓库 id 的，要同时用 --base 指定是哪个")
    bases = [resolve_base(k, args.base_model) for k in base_keys]

    if args.physician == "all":
        # 用 physicians_all 而不是 physicians_enabled：训练是"给每位**登记**的
        # 医家各训一个 adapter"，跟"这一次问诊出场哪几位"是两件事。enabled=False
        # 的医家也该有 adapter——正是因为还没训出来才没开。
        # 有语料才训得出来这件事由 build_plan 的 train=0 警示负责，不在这里拦。
        physician_ids = sorted(physicians_all(PHYSICIANS))
    else:
        pid = resolve_physician_id(args.physician)
        if pid is None:
            raise SystemExit(f"认不出医家 {args.physician!r}。{physician_choices_text()}")
        physician_ids = [pid]

    samples = load_chain_samples(args.samples)
    plan = build_plan(samples, physician_ids, base_keys, args.out_dir)
    print(format_plan_text(plan, bases))

    if args.dry_run:
        if not samples:
            print("\n--dry-run：样本文件是空的，没有可渲染的样本。"
                  "先跑 python -m offline.export_sft --format chain")
            return 1
        first = render_example(samples[0])
        print("\n第一条样本渲染后（prompt / completion，出处 id 不进 completion）：")
        print("--- prompt ---")
        print(first["prompt"])
        print("--- completion ---")
        print(first["completion"])
        print("\n--dry-run：没有加载模型，没有训练。**没有检查的事**："
              "基座仓库 id 能不能拉下来、显存够不够、peft 装没装"
              "（后者跑 --check-deps）。")
        return 0

    reports = []
    for base in bases:
        for pid in physician_ids:
            out = output_dir(args.out_dir, base["key"], pid)
            print(f"\n=== 训 {base['key']} / {pid} → {out} ===")
            report = train_one(base, pid, samples, out, args.epochs, args.lr,
                               args.batch_size, args.grad_accum, args.max_len)
            reports.append((base["key"], pid, report))
            print(format_gap_text(base["key"], pid, report))

    print("\n=== 并列对照（同一份数据、同样超参，只有基座不同）===")
    for base_key, pid, report in reports:
        print(format_gap_text(base_key, pid, report))
    print("\n这张表只说明「哪个基座在 heldout 上 loss 更低」。**loss 不是靶子**："
          "训完要按 eval/RESULTS.md 的口径重跑 ε/E3/E4/E8/E9/SDT/MES，"
          "并**并列**报在 DeepSeek 那组数旁边（不是覆盖）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
