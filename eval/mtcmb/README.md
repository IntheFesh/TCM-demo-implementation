# MTCMB · TCM-PR（方剂推荐）适配

把本项目的推理链接到 MTCMB 的方剂推荐子任务上：给一段患者描述，输出一张方
（药味清单），按药味集合打分。

## ⚠ 先读这一段：字段映射**没有在这台机器上核过**

`eval/sdt/` 那一套敢写"格式已实测验证"，是因为当时真把 TCMEval 拉下来跑了一遍
（那份 README 里四条读数注意全是实测出来的）。这一轮做不到同一件事——写这套
适配的沙盒**连不上数据源**（huggingface 直接 403）。所以这里的字段名是
**声明的、可探测的**，不是"验过的"：

```bash
# 上机第一步，零 LLM、零打分
python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --probe
```

它会打印：这个目录下有哪些文件、记录里**实际**出现过哪些键、当前映射解析出
几条带参考方的记录、以及三类"多半映射错了"的形状：

| 探针报的问题 | 说明 | 怎么修 |
|---|---|---|
| 一条参考方都没读到 | 答案字段不叫 `answer/output/label/...` 里的任何一个 | 把真实字段名加进 `data.py` 的 `FIELD_CANDIDATES` |
| 有记录的参考方超过 60 味 | 多半把整段话当成了药名（分隔符不对、或字段指错） | 看 `--probe` 打印的样例，再决定改分隔符还是改字段 |
| 有记录没有参考方 | 测试集常常不公开答案 | **那份数据上不要算分**，换有答案的 split |

**跳过 `--probe` 的代价不是报错，是一份"所有人都得 0 分"的漂亮报告**——
SDT 那边踩过同一个形状的坑（JSON 里的答案字段是空的，照着 Train 的字段名
写代码会得到一份全空的金标准而且不报错）。

## 跑

```bash
# 两组都要跑——项目规则「任何数字都必须带对照」
python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --solver baseline --out out/pr_baseline.json
python -m eval.mtcmb.run --dir <MTCMB>/TCM-PR --solver chain    --out out/pr_chain.json
python -m eval.mtcmb.run --compare out/pr_baseline.json out/pr_chain.json
```

成本：baseline **1** 次调用/条，chain **3** 次/条（多 S1+S2）。

两个 solver 的**提示词是同一份**（`prompts/v1/mtcmb_prescribe.yaml`），
差别只在 `$extra` 里塞不塞本项目 S1+S2 跑出来的证素分析。提示词本身改一个字，
量到的就是"两份提示词的差"而不是"有没有证素分析的差"。

## 读数三条

1. **这里的分不是官方分。** `score_records()` 把 `"scorer": "eval.mtcmb.score"`
   写进返回值里。MTCMB 若附官方打分脚本，以它为准；这一份只用于本项目内部的
   chain vs baseline 对照。
2. **macro 和 micro 都报，报告里写清用的哪个。** micro 会让开了 20 味药的那条
   记录说了算；macro 每条一票。
3. **安全否决拦下的记录按"没作答"记，不按 0 分记。** 把它们当 0 分混进均值，
   等于把"系统拒绝作答"和"系统答错了"算成同一件事。`--ignore-safety-veto`
   只用来量化"安全层花了多少分"，用它跑出来的数必须单独标注。

## 跟开源中医模型比（BianCang / ShizhenGPT）

**能不能比，取决于三件事同时成立**，缺一条这两个数就不该并排放进一张表：

1. **同一个 split**（同样那批记录，同样的条数）；
2. **同一个打分脚本**（官方的那份，或者两边都用 `eval.mtcmb.score`）；
3. **同样的作答条件**——尤其是安全否决：我们这一侧会拒答一部分记录，
   对方不会。不把这件事标出来，我们的分会因为"拒答按没作答记"而看起来偏高，
   或者因为"拒答按 0 分记"而看起来偏低，取决于谁在算。

这三条跟 R5 给蒸馏模型定的那条护栏是同一件事：**并列报、不覆盖、标清条件**。
权重都在 HuggingFace 上（BianCang 基于 Qwen、ShizhenGPT 是多模态中医模型），
要 GPU 才跑得动；本项目的 `LLM_MODE=local_inproc` + `scripts/start_vllm.sh`
可以把它们当后端起起来，跑同一份 `eval.mtcmb.run --solver baseline`
——**用它们当后端跑 baseline，才是"同一个任务、同一个打分器"的比法**，
照抄论文里的分放进我们的表是第二种不可比。
