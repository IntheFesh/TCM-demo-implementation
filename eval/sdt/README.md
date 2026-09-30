# TCMEval-SDT 适配

把本项目的推理链接到 TCMEval-SDT 基准上评分。数据集的来源、授权与已知数据问题见
[`data/SOURCES.md`](../../data/SOURCES.md)「评测数据」一节。

## 数据

来源 [`zhuyan166/TCMEval`](https://github.com/zhuyan166/TCMEval)（CC BY 4.0），不随本仓库分发：

```bash
git clone --depth 1 https://github.com/zhuyan166/TCMEval.git
export SDT=$PWD/TCMEval/evaluation/TCMEval-SDT
# 官方 evaluate.py 在顶层 import 了 pandas，已写在 requirements.txt 里
```

## 运行

```bash
# 对照组：同模型、同三个输出头，不注入证素分析
python -m eval.sdt.run --sdt-dir $SDT --split Validation --solver baseline --out out/sdt_baseline.txt
# 实验组：先跑 S1+S2，把证素分析注入 Task2/3/4
python -m eval.sdt.run --sdt-dir $SDT --split Validation --solver chain    --out out/sdt_chain.txt
```

**两组都要运行**：只报「chain 拿了 X 分」没有基准，`chain − baseline` 才是这条结构化推理链的贡献。
调用成本：baseline 每条 3 次调用，chain 每条 5 次。`--limit N` 只跑前 N 条，用于先检查质量。

## 读数

评分一律使用官方 `evaluate.py`，不重新实现计分逻辑——只有运行同一份脚本，分数才能与论文中的
其他模型比较。

```python
from eval.sdt.score import score_submission
score_submission(SDT, "Validation", "out/sdt_chain.txt")
```

读数时注意：

1. **`automated_score` 返回总分，不是均值**，50 条的满分是 50.0。报告同时给出总分、条数与
   总分/条数。
2. **Validation 金标准开头有 UTF-8 BOM。** `evaluate.py` 按默认方式读取，BOM 粘在第一条病案 ID
   上，那一条恒得 0 分，满分提交在 Validation 上只能得 48.9998/50。评分保留这个行为（这样才与
   论文可比）；`--diagnose-bom` 剥掉 BOM 算出诊断值，单独标注。主报告使用没有这个问题的 Test。
3. **Validation 与 Test 的金标准在 `Results/*.txt` 里。** JSON 中的答案字段是空的（只有 Train 有），
   按 Train 的字段名读取会得到一份全空的金标准而不报错。
4. **安全否决会拦下一部分记录。** 命中昏迷、呕血、休克、黑便等危重信号的记录提交空答案、得 0 分，
   这是系统真实的行为。`--ignore-safety-veto` 只用于量化安全层的代价：配合 `--only-ids` 只重跑被
   拦下的记录，把结果并回主提交文件，得到的分数单独标注，不混入主结果。

## 失分分析（零 LLM 调用）

```bash
python -m eval.sdt.run --sdt-dir $SDT --split Test --error-analysis out/sdt_chain.txt
```

这个模式对已有提交重新聚合：不构造 solver、不调用模型、不写提交文件
（`tests/test_sdt_error_analysis.py` 用"一调用就抛异常"的假后端保证这一点）。输出：

- 官方 `automated_score` 与逐条加权求和并排，以及两者的差额（差额只应来自 Validation 的 BOM）
- 逐条 T1/T2/T3/T4 得分，完全对 / 部分对 / 完全错的分布
- **多选率与少选率**，以及两个方向的**边际代价**：去掉错选、补上漏选各能挽回多少分，用官方
  `score_proportional` 按反事实计算；另给单个错选与单个漏选各值多少分
- 失分最多的 10 条，模型答案与金标准并排，附临床资料开头
- 按病机 / 证型分组的得分（每个金标准选项各自成组，带 n）

### 根据分析结果调整 prompt

官方多选题公式为：

    score = max_score × correct / (len(gold) + wrong)

金标准有 3 个选项时，多一个错选损失 0.250，少一个正确选项损失 0.333——**漏选比错选贵**。
所以 `prompts/v1/sdt_select.yaml` 写的是「漏选比选错更贵，拿不准的也要选上」。

公式回答"哪个方向"，`--error-analysis` 回答"这个方向值多少分"。判据写在
`eval/sdt/error_analysis.py::_selection_verdict`：较大的一边要同时满足「≥ 0.5 分」与「≥ 1.5 倍」
两道门槛才算确定了主因；差距更小时，改动的效果与采样噪声分不开。

| 分析结果 | 该做什么 |
|---|---|
| 主因是多选，且单个错选比单个漏选贵 | 收紧选择策略；只改措辞不够，需要换机制（例如先给每个选项打把握度再按阈值取） |
| 主因是少选，或单个漏选比单个错选贵 | 保持或加强「拿不准也要选上」的方向 |
| 两边差距不足以确定主因 | 不改选择策略；去看失分榜和分组得分，主因在别处 |

## 过拟合护栏

在 Test 上反复调整 prompt 就是在测试集上过拟合，会让"外部可比的分数"失去意义。规则：

1. **prompt 改动先在 Train 上验证方向**（200 条，金标准就在 JSON 里；`--error-analysis` 在 Train 上
   同样可用，只是没有官方 `automated_score`，报告会说明）
2. **Validation 做中间验证**（50 条），引用它的数字时带上「满分上限 48.9998/50」的说明
3. **Test 只在定型后运行**

代码层面：`--split Test` 运行前打印醒目提醒，并报出台账里已经运行过几次（从台账计算）。每次在
Test 上运行都向 [`test_run_log.jsonl`](test_run_log.jsonl) 追加一条 `event=run`，每次计分追加一条
`event=scored`（零调用，不计入运行次数），两者按提交文件路径关联。脚本只提醒、不拦截：是否运行
由人决定，代码负责提供信息。台账中 `backfilled: true` 的几条是台账建立之前的运行，运行时刻与
commit 未记录。

## 为什么不是"把 consult() 的输出转个格式"

SDT 的任务形状与本系统不同：本系统是「主诉 → 各医家的证型/治法/方药 + 分歧」，SDT 是「医案 →
原样摘录临床信息 / 病机多选 / 证型多选 / 写一段辨证分析」。医家之间的分歧对照在 SDT 上没有对应物，
而 Task1 要的是**原文片段**，与 S1 做的术语归一化正好相反，所以 `ChainSolver` 不给 Task1 注入证素
分析。适配器复用推理链的推理部分，另配三个 SDT 形状的输出头。
