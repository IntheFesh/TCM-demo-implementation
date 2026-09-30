# 评测结果

本文件汇总仓库内评测报告文件里的数字。**每个数字都带凭据记号**
`` `文件名:键=值` ``（文件路径相对 `eval/`），由下面的命令逐个去文件里取真值核对，
同时要求这个数也出现在同一行的正文里：

```bash
python -m scripts.collect_results              # 打印各报告文件里的实际值
python -m scripts.collect_results --check      # 核对本文件与 README.md；不一致时退出码 1
python -m scripts.collect_results --rerender   # 用 .json 重新渲染与之不一致的 .md 报告（零 LLM 调用）
```

按项目约定（[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) §7），每个数字都与它的对照基准并列：
对照组的数，或噪声地板 ε。

## 实验定义

| 编号 | 名称 | 做法 | 产出文件 |
|---|---|---|---|
| ε | 噪声地板 | 同一主诉、同一配置重复采样 3 次，同一位医家两两结果之间用药集合的 Jaccard 距离。按「主诉 × 医家」逐条给出，作为其他分歧指标的对照基准 | `eval/epsilon.json`（`python -m offline.estimate_epsilon`） |
| E2 | 师承内 vs 跨学派 | 对同一主诉，比较同学派医家之间与跨学派医家之间的用药分歧；判据「跨学派分歧 > 师承内分歧」在多数主诉上是否成立。只报出，不设闸门 | `report_e8.json` / `report_e9.json` 的 `school_pairs` |
| E3 | 参考医案换人 | `refs_mode=own` 对 `swapped`：把参考医案换成另一位医家的医案，按「主诉 × 医家」配对比较两次的用药集合，报 Jaccard 距离均值（改变率）；闸门 ≥ 0.4 | `report_e3.json`（`python -m eval.run_eval --e3`） |
| E4 | 去掉参考医案 | `refs_mode=own` 对 `none`，度量同 E3；闸门 ≥ 0.4 | `report_e4.json`（`--e4`） |
| E8 | 检索模式差异 | `dense` / `bm25` / `graph` / `hybrid` 四种检索模式下同一批查询的 S3 用药输出两两 Jaccard 距离均值 | `report_e8.json`（`--e8`） |
| E9 | ReAct 开关 | `use_react` 关对开，度量同 E3。ReAct 是过程性开关，「改变了多少」没有天然的及格线，只报出、不设闸门 | `report_e9.json`（`--e9`） |
| SDT | TCMEval-SDT | 外部基准 TCMEval-SDT 的 Test 集，官方评分脚本打分，满分 50。`chain` 注入本项目的证素分析，`baseline` 用同一模型、同样三个输出头但不注入 | `sdt/test_run_log.jsonl`（`python -m eval.sdt.run`，每次运行自动追加） |

E3/E4/E9 的「改变率」与 ε 逐条配对比较：一个样本的用药距离超出它自己那条主诉、
那位医家的噪声地板，才计为真实差异。不用全局 ε 一刀切，原因见下面「ε 按主诉分层」。

## 运行条件

- ε 与 E2/E3/E4/E8/E9：`deepseek-chat`（`LLM_MODE=api`），`tests/queries.txt` 的 10 条测试主诉，
  三位医家各自开方（`S3_MODE=legacy`），参考医案走 top-3 检索。`epsilon.json` 的 `model` /
  `backend` 字段记录了模型与后端；`report_e*.json` 本身不带模型字段。
- SDT：同一模型，台账每一行记录 `model` / `backend` / `prompt_version`。
- 换模型、换 `S3_MODE`、换检索方式（尤其是默认的 `full_context`）都是换实验条件，
  新的结果应作为新的一行并列报出，不覆盖已有的行。

## 结果

| # | 指标 | 当前值 | 对照 | 闸门 | 凭据 |
|---|---|---|---|---|---|
| 1 | ε_online（噪声地板） | mean 0.2611 / p50 0.1818 / p95 0.7857，9 条主诉可用 | 它本身是其他分歧指标的对照基准 | — | `epsilon.json:epsilon_online.mean=0.2611` `epsilon.json:epsilon_online.p50=0.1818` `epsilon.json:epsilon_online.p95=0.7857` `epsilon.json:epsilon_online.n_queries_used=9` |
| 2 | 分歧度超出逐条噪声地板的主诉数 | E3 报告 9 条 / E9 报告 8 条（另 1 条落在噪声地板以内） | 每条主诉与它自己的 ε 比较 | — | `report_e3.json:e3.paired_real_divergence=9` `report_e9.json:e9.paired_real_divergence=8` `report_e9.json:e9.paired_within_noise=1` |
| 3 | E3 参考医案换人的改变率 | 0.451，其中 0.593 的样本超出各自的噪声地板 | 闸门 0.4；逐条噪声地板 | ≥ 0.4，通过 | `report_e3.json:e3.change_rate=0.451` `report_e3.json:e3.rate_above_paired_epsilon=0.593` |
| 4 | E4 去掉参考医案的改变率 | 0.497，其中 0.667 的样本超出各自的噪声地板 | 闸门 0.4；逐条噪声地板 | ≥ 0.4，通过 | `report_e4.json:e4.change_rate=0.497` `report_e4.json:e4.rate_above_paired_epsilon=0.667` |
| 5 | E8 四种检索模式的输出差异率 | 0.437（p50 0.477 / p95 0.767） | ε_online 均值 0.2611 | 只报出 | `report_e8.json:e8.output_difference_rate=0.437` `report_e8.json:e8.p50=0.477` `report_e8.json:e8.p95=0.767` |
| 6 | E9 ReAct 开关的改变率 | 0.463，其中 0.63 的样本超出各自的噪声地板 | 逐条噪声地板 | 只报出 | `report_e9.json:e9.change_rate=0.463` `report_e9.json:e9.rate_above_paired_epsilon=0.63` |
| 7 | SDT Test（chain，注入证素分析） | 23.173 / 50 | baseline 22.068（同模型、同输出头、不注入）；同一 chain 组的首次完整运行 21.702 | chain > baseline | `sdt/test_run_log.jsonl:sdt.chain_last=23.173` `sdt/test_run_log.jsonl:sdt.baseline=22.068` `sdt/test_run_log.jsonl:sdt.chain_first=21.702` |
| 8 | SDT Test（关闭安全否决） | 27.729 / 50 | 开启否决时的 chain 23.173；差值是安全否决拒答的代价 | 只报出 | `sdt/test_run_log.jsonl:sdt.ignore_safety_veto=27.729` |
| 9 | 引用合法性（幻觉引用） | 27 条引用中 0 条引用了检索结果之外的医案 id | 两组分母分开报：有医案可引时 27 条；「无医案可引」一组的样本数为 0 | 幻觉数 = 0 | `report_e3.json:hallucination.n=27` `report_e3.json:hallucination.n_hallucinated=0` `report_e3.json:hallucination.n_without_refs=0` |
| 10 | 安全否决 | 10 条测试主诉中 1 条在 S2 之前被拦截，不产出方药 | 被拦与正常完成两组的平均调用数在报告里并列 | — | `report_e3.json:safety_veto.n_vetoed=1` `report_e3.json:safety_veto.n_queries=10` |
| 11 | E2 师承内 vs 跨学派（E9 报告） | 师承内 0.453 vs 跨学派 0.584，5 条主诉跨学派分歧更大 | 判据「多数主诉上跨学派 > 师承内」 | 只报出 | `report_e9.json:e9.school_lineage_mean=0.453` `report_e9.json:e9.school_cross_mean=0.584` `report_e9.json:e9.school_n_cross_gt_lineage=5` |
| 12 | E2 师承内 vs 跨学派（E8 报告） | 师承内 0.568 vs 跨学派 0.557，4 条主诉跨学派分歧更大 | 同上 | 只报出 | `report_e8.json:e8.school_lineage_mean=0.568` `report_e8.json:e8.school_cross_mean=0.557` `report_e8.json:e8.school_n_cross_gt_lineage=4` |

读数说明：

- **#9 幻觉数为 0 说明约束生效，不说明模型不会编造。** S3 的 prompt 只给出检索到的候选医案 id，
  且 `cited_case_ids` 必须引用其中的 id（`Field(min_length=1)` + 事后核对），模型几乎没有机会引到别的 id。
- **#11 与 #12 的结论方向相反。** 两份报告的判据一份成立、一份不成立，说明这个判据本身落在重复
  采样的噪声里，所以它只报出、不设闸门。
- **#5 里 `graph` 模式的差异**受证素索引覆盖率影响：没有被 `data/element_index.json` 覆盖的医案
  证素集合为空、相似度恒为 0，会系统性地排在检索结果之后。

## ε 按主诉分层

噪声地板在不同主诉之间差别很大，所以分歧指标必须逐条配对比较：

| # | 指标 | 当前值 | 对照 | 闸门 | 凭据 |
|---|---|---|---|---|---|
| 13 | 逐条地板的范围 | 9 条可用主诉的逐条均值从 0.0 到 0.6742 | 全局均值 0.2611 | — | `epsilon.json:epsilon.stratification.n_queries_used=9` `epsilon.json:epsilon.stratification.per_query_mean_min=0.0` `epsilon.json:epsilon.stratification.per_query_mean_max=0.6742` `epsilon.json:epsilon.stratification.global_mean=0.2611` |
| 14 | 用全局均值一刀切的偏差 | 5 条主诉的地板低于全局均值、4 条高于；最高的一条是全局均值的 2.58 倍 | 逐条地板 | — | `epsilon.json:epsilon.stratification.n_floor_below_global=5` `epsilon.json:epsilon.stratification.n_floor_above_global=4` `epsilon.json:epsilon.stratification.max_over_global=2.58` |

用全局均值做对照时，地板低于均值的主诉会被漏判真实分歧，地板高于均值的会把采样抖动误判为分歧。

## 知识库规模

| # | 指标 | 当前值 | 对照 | 闸门 | 凭据 |
|---|---|---|---|---|---|
| 15 | 本草三元组（`data/standard/materia_medica.jsonl`） | 10156 条 | 方剂三元组 5067 条 | — | `../data/standard/materia_medica.jsonl:pharmacology.n_materia_medica=10156` `../data/standard/formulary.jsonl:pharmacology.n_formulary=5067` |
| 16 | 名医用药规律（`data/standard/prescribing_patterns.jsonl`） | 1924 条，其中剂量规律 194 条、医家×证型分组 55 条；单条规律最多由 223 个诊次支持 | 规律由 `cases.json` 计数得出，每条回指 `case_id` | — | `../data/standard/prescribing_patterns.jsonl:patterns.n_patterns=1924` `../data/standard/prescribing_patterns.jsonl:patterns.n_dose=194` `../data/standard/prescribing_patterns.jsonl:patterns.n_physician_syndrome=55` `../data/standard/prescribing_patterns.jsonl:patterns.max_support=223` |

## 如何产生新的结果

需要真实 LLM 的评测都在 `eval/`，不进 pytest。常用入口：

```bash
python -m offline.estimate_epsilon --n-repeats 3                              # ε → eval/epsilon.json
python -m eval.run_eval --queries-path tests/queries.txt --e3 --e4 --e8 --e9  # → eval/report_e*.json/.md
python -m eval.sdt.run --sdt-dir $SDT --split Test --solver chain --out out/sdt_chain.txt
python -m eval.ablation --backend real --queries-path tests/queries.txt       # 工程开关消融 → eval/report_ablation.json
python -m eval.ablation.reasoning --backend real --sdt-dir $SDT                # 推理消融 → eval/report_ablation_reasoning.json
```

新结果落盘后，在本文件加一行并附上凭据记号，再运行 `python -m scripts.collect_results --check`。
TCMEval-SDT 的 Test 集每跑一次都会追加到 `eval/sdt/test_run_log.jsonl`：调整 prompt 应先在
Train / Validation 上验证方向，Test 只用于定型之后的评估（见 [`eval/sdt/README.md`](sdt/README.md)）。
