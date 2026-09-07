# TCMEval-SDT 适配

把本项目的推理链接到 TCMEval-SDT 基准上打分。

## 数据

来源 `github.com/zhuyan166/TCMEval`（CC BY 4.0），不随本仓库分发——跟 `books/`
同理，可独立获取的公开数据不入版本控制。

```bash
git clone --depth 1 https://github.com/zhuyan166/TCMEval.git
export SDT=$PWD/TCMEval/evaluation/TCMEval-SDT
# 官方 evaluate.py 顶层 import 了 pandas（虽然脚本里没用到）——已在 requirements.txt 里
```

## 跑

```bash
# 对照组：同模型、同三个输出头，不注入证素分析
python -m eval.sdt.run --sdt-dir $SDT --split Validation --solver baseline --out out/sdt_baseline.txt
# 实验组：先跑 S1+S2，把证素分析注入 Task2/3/4
python -m eval.sdt.run --sdt-dir $SDT --split Validation --solver chain    --out out/sdt_chain.txt
```

**两组都要跑。** 项目规则「任何数字都必须带对照」：只报「我们拿了 X 分」没有
基准，`chain − baseline` 才是这条结构化推理链的贡献。

成本：baseline 3 次调用/条，chain 5 次/条。Validation 50 条两组合计 400 次，
只该在能连 DeepSeek 的机器上跑。

## 读数

打分一律走官方 `evaluate.py`，**不重新实现计分逻辑**——分数要和论文里 15 个
模型可比，唯一的办法是跑他们那份脚本本身。

```python
from eval.sdt.score import score_submission
score_submission(SDT, "Validation", "out/sdt_chain.txt")
```

四条读数注意，全部是从官方文件实测出来的：

1. **`automated_score` 返回求和不是均值**，50 条的满分是 50.0。报告里同时给
   总分和 总分/条数，并写明条数。
2. **官方 Validation 金标准开头有 UTF-8 BOM**，`evaluate.py` 用默认方式读，
   BOM 粘在第一条病案 ID 上，那条恒 0 分。**保留这个行为**（论文里的分大概率
   也是这么跑出来的，"修好"反而不可比）。实测代价：满分提交在 Validation 上
   只能拿 48.9998/50。想量化它就传 `diagnose_bom=True`，那个数单独标注。
3. **Validation 和 Test 的金标准都在 `Results/*.txt` 里**，两个 split 都能评。
   JSON 里的答案字段是空的（只有 Train 有），照着 Train 的字段名写代码会得到
   一份全空的金标准而且不报错。
4. **安全否决会拦掉一部分记录**：Validation 3/50（6%）、Test 8/50（16%），
   命中 昏迷/呕血/休克 等危重信号。它们提交空答案、得 0 分。这是这套系统真实
   的行为，`--ignore-safety-veto` 只用来量化"安全层花了多少分"，用它跑出来的
   数必须单独标注，不能混进主结果。

## 这个适配器为什么不是「把 consult() 转个格式」

SDT 的任务形状跟这个 demo 不是一回事：demo 是「主诉 → 两位医家各自的证型/治法/
方药 + 分歧」，SDT 是「医案 → 原样摘录临床信息 / 病机多选 / 证型多选 / 写一段
辨证分析」。两位医家的分歧对照在 SDT 上没有对应物，而 Task1 要的是**原文片段**，
跟我们 S1 做的术语归一化正好相反（所以 ChainSolver 刻意不给 Task1 注入证素
分析）。适配器复用的是链的推理部分，另配三个 SDT 形状的输出头。
