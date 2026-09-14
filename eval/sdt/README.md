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
4. **安全否决会拦掉一部分记录**：**Test 10/50（20%）**，命中 昏迷/呕血/休克/
   黑便 等危重信号（早期估算写的是 8/50，那是 `core/safety.py` 词表扩充之前的
   旧数，已作废；Validation 那个 3/50 出自同一次旧估算，同样待重数——
   见 `data/SOURCES.md` 第 14 条）。它们提交空答案、得 0 分。这是这套系统真实
   的行为，`--ignore-safety-veto` 只用来量化"安全层花了多少分"，用它跑出来的
   数必须单独标注，不能混进主结果。

## 失分分析（R2-1，零 LLM 调用）

```bash
python -m eval.sdt.run --sdt-dir $SDT --split Test --error-analysis out/sdt_chain_v2.txt
```

分数已经算出来了，这个模式只是重新聚合——**不构造 solver、不碰 `get_llm`、
不写提交文件**（`tests/test_sdt_error_analysis.py` 用"一调用就抛异常"的假后端
钉住这一点）。产出：

- 官方 `automated_score` 与我们逐条加权求和并排，以及两者差额（差额只该来自
  Validation 的 BOM；超出就是聚合这一层有问题，先查它）
- 逐条 T1/T2/T3/T4 得分、完全对 / 部分对 / 完全错分布
- **多选率 vs 少选率**，以及两边的**边际代价**：去掉错选能捞回多少分、补上漏选
  能捞回多少分，都用官方 `score_proportional` 实测（不按推测的公式估），
  并换算成总分里的分。另给**单个**选项的代价（一个错选 vs 一个漏选各值多少分）
- 失分最多的 10 条，模型答案与金标准并排 + 临床资料前 80 字
- 按病机 / 证型分组的得分（每个金标准选项各自成组，带 n）

`--diagnose-bom` 剥掉官方金标准的 BOM（只影响 Validation），算出来的是诊断值。

### 拿到分析结果之后怎么改 prompt（R2-2 的决策表）

**先看数，再决定改什么。** 判据写在
`eval/sdt/error_analysis.py::_selection_verdict`：较大的一边要同时满足
「≥ 0.5 分」和「≥ 1.5×」两道门槛才算定了主因（0.5 分的由来：chain 相对
baseline 的全部增益只有 0.77 分，能捞回 0.5 分意味着这一个方向就值 65% 的
现有增益；低于这个数改了也分不清是改动起作用还是噪声）。

**官方公式已验算（项目方拉下 evaluate.py 核对）：**

    score = max_score × correct / (len(gold) + wrong)

金标准 3 个时，多一个错选损失 0.250、少一个对选损失 0.333——**少选比多选贵 33%**。
所以上面 prompt 里那句「拿不准的宁可不选」方向是反的。**知道公式不等于可以跳过
实测**：公式回答的是「哪个方向」，`--error-analysis` 回答的是「这个方向值多少分」
（决定值不值得改、以及改了之后怎么验证有没有效）。按下面这张表走。

| 分析结果 | 该做什么 |
|---|---|
| 判据说主因是多选，**且**单个错选比单个漏选贵 | 收紧选择策略。但**不要再写一遍"宁可不选"**——下面那条说明它已经在 prompt 里了 |
| 判据说主因是少选，或单个漏选比单个错选贵 | 现在 prompt 里「拿不准的宁可不选」方向是反的，该删掉或反过来写。报告会直接打一句「方向是反的」 |
| 两边差距不足以定主因 | **不要改选择策略的 prompt。** 去看失分榜和分组得分，主因在别处 |

**已经在 prompt 里的那句话（重要）：** `prompts/v1/sdt_select.yaml` 从第一版就写着
「**选错要扣分，漏选只是不得分**——拿不准的宁可不选，不要为了凑数把把握不大的
选项也填上」。M3 观察到的过度选择（病例 247 选了 A/D/J 而金标准只有 D）就是在
这句话已经生效的情况下发生的，所以**再加一句同义的话不是修复**。真要往"收紧
选择"这个方向改，得换机制（例如让模型先给每个选项打把握度再按阈值取），不是
换措辞。

## 过拟合护栏（R2-3）

SDT Test 到今天已经跑过 3 次完整 + 1 次局部（台账 `test_run_log.jsonl`）。
再反复在 Test 上调 prompt 就是在测试集上过拟合，会让"外部可比的分数"这个本项目
最硬的证据失效。规矩：

1. **prompt 改动先在 Train 上验证方向**（200 条，金标准就写在 JSON 里，
   `--error-analysis` 在这个 split 上照样能跑，只是没有官方 `automated_score`，
   报告会明说这一点）
2. **Validation 做中间验证**（50 条）。引用它的数必须带上「满分上限 48.9998/50，
   因官方金标准带 BOM 致首条恒 0」这个 caveat——分析报告会自动打出来
3. **Test 只在最终定型后跑一次**

代码这一层：`--split Test` 时 stdout 打醒目提醒并报出已经跑过几次（数从台账
现算，不是写死在提示语里）；每次跑 Test 往 `test_run_log.jsonl` 追加一条
`run` 事件，每次算分追加一条 `scored` 事件（零调用，不计入暴露次数）。
**只提醒、不拦**：真到了最终定型那一次，拦住就没法跑了——判断该不该跑是人的事，
代码负责让人在有信息的情况下判断。

台账里那 4 条是**回填**的（`backfilled: true`，来源写在 `source` 字段里）：
这几次跑发生在台账建立之前，具体时刻和 commit 当时没有记录。台账从 0 开始的话
提醒会说「已跑过 0 次」——一句假话，而这个护栏的全部作用就是让人相信那个数。

## 这个适配器为什么不是「把 consult() 转个格式」

SDT 的任务形状跟这个 demo 不是一回事：demo 是「主诉 → 两位医家各自的证型/治法/
方药 + 分歧」，SDT 是「医案 → 原样摘录临床信息 / 病机多选 / 证型多选 / 写一段
辨证分析」。两位医家的分歧对照在 SDT 上没有对应物，而 Task1 要的是**原文片段**，
跟我们 S1 做的术语归一化正好相反（所以 ChainSolver 刻意不给 Task1 注入证素
分析）。适配器复用的是链的推理部分，另配三个 SDT 形状的输出头。
