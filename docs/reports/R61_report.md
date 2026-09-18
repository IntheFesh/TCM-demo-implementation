# R61 报告：三件事——C-D 一致率判据、A 组 R60 约定漏改、验证器一次通过率

用户真机 `--limit 2` 实测（`/tmp/r57_probe3.json`，2026-09-19）：A 组
`n_ok=0/2`（两条都挂 `herb_source_fabricated`）、B/C/D 三组 `n_ok=2/2`
（R60 的修复确实生效了，上一轮是八条挂七条），但 verifier_first_pass 全组
恒 0，且 C 与 D 的一致率判据在自由文本上天然不达标。三件事逐条修，**本轮
不产出诊断脚本给用户跑**。

## §1 C-D 一致率判据是错的（最优先，硬闸门）

### 1.1 根因

`eval/ablation/r57.py`（改前）用三元组逐字相等判"一致"：

```python
c_tuple = (cm.get("syndrome"), cm.get("method"), cm.get("formula"))
d_tuple = (dm.get("syndrome"), dm.get("method"), dm.get("formula"))
if c_tuple == d_tuple and all(c_tuple):
```

`method`（治法）是自由文本。用户实测 q1：C/D 两组的 `syndrome`（肝胃气滞证）
与 `formula`（柴胡疏肝散加减）字面完全相同，整条记录仍被判"不一致"——
唯一原因是 `method` 措辞不同（"疏肝理气，和胃止痛" vs "疏肝解郁，理气
和胃"）。两次独立 LLM 采样在自由文本上几乎不可能逐字相同，逐字相等在这
上面量到的是**采样方差**，不是 R54"事后佐证不回流改推导"这条不变式
本身。

这是 CLAUDE.md 已经记录两次的教训（覆盖检查比字面子串、分歧度比证型名
字面相等）第三次以新形态出现——判据是"这个判断此前有没有人做过"，不是
"这次实现有没有 bug"。

### 1.2 修复：噪声地板对照组 + 三项分开报

**新增 E 组**（`eval/ablation/spec.py`）：三个布尔跟 C 逐一相同——不是
任务书原文的第五种实验条件，是 C 组的复测，专门用来量"同一份配置、不
跑第三相佐证，纯粹重复采样一次"的分歧率，作为噪声地板。

**闸门改成相对判据**：`GATE_CD_CONSISTENCY_MIN`（绝对值 0.9）已删除，换
成 `GATE_CD_CONSISTENCY_MARGIN = 0.1`——`C-D 的值 ≥ C-E 的值 − margin`
才算过。噪声地板本身没测出来（没跑 E 组或样本不足）时闸门判 `None`
（⏳），不拿绝对阈值顶替。

**三项分开判、分开报**（`pair_consistency` 重写，`build_report` 里从
一条门变成三条门）：

| 维度 | 比较方式 | 理由 |
|---|---|---|
| `syndrome`（证型） | 归一后逐字相等，报命中率 | 受控词表，逐字相等有意义 |
| `formula`（主方名） | 归一后（统一"加减/加味"后缀）逐字相等，报命中率 | 化裁后缀近义写法不该判成分歧，但基础方名真的不同（黄芪建中汤 vs 理中汤）仍要判成不同 |
| `method`（治法） | 字符级 Jaccard **相似度**均值，不报相等/不相等 | 自由文本，逐字相等没有意义 |

新增共享辅助函数：`_split_formula_suffix`/`_normalize_formula_for_comparison`
（方名归一，注意跟 §4 的本体查找归一是两个不同的问题，见函数文档字符串）、
`_char_jaccard`（字符级相似度，跟 `core/chain.py::_layered_jaccard`
方向相反——那边报**距离**、这边报**相似度**，同一个系数不同方向，见函数
文档字符串）。

`--groups` 默认从 `ABCD` 改成 `ABCDE`；`merge_r57.py` 同步。**全量从
80 次问诊（20 条 × 4 组）变成 100 次（20 条 × 5 组）**——多出的 20 次
全部是 E 组噪声地板复测，不是新增了一条独立的实验维度。

> 用户任务书原文估算的是"4 组 40 次→5 组 50 次"；实测 `select_pi_wei_men_complaints`
> 的默认样本量是每组 20 条（`docs/ONSITE_R57_R58.md` 一直写的是"80 次
> 问诊"），所以这里按代码里的真实数字改成"80→100"，不是照抄任务书原文
> 的估算——CLAUDE.md 的号召是"改动前后都要报一个准确数"，用代码里能查到
> 的数，不用预判的数。

## §2 A 组：R60 的修复没同步到 structured 路径

### 2.1 根因

R60 的 commit `c360763` 只改了 `prompts/v1/s3_derived.yaml` 一个文件。三条
S3 路径用三份不同的提示词：

```
core/chain.py:1131  s3_prompt = load_prompt("s3_syndrome")     ← legacy
core/chain.py:1547  s3_prompt = load_prompt("s3_structured")   ← A 组走这条
core/chain.py:1744  s3_prompt = load_prompt("s3_derived")      ← B/C/D 走这条
```

`check_herb_source_fabricated`/`check_herb_source_paraphrased` 是全局规则
（三条路径共用同一个符号验证器），但 R60 只把"span 怎么填、抄不对会怎样"
这条约定写进了 `s3_derived.yaml`；`s3_structured.yaml` 的"## ontology_refs"
一节还是旧措辞（"知识块里的那一段原文，照抄"，没点名"可摘录原文"这个
小节，也没说清楚 veto/revise 的后果）。约定只讲给了一半路径听，A 组因此
在 R60 之后独自违规到 0/2 全挂 `herb_source_fabricated`。

`build_focused_knowledge`（R60 已经改好的知识块生成函数）本身是三条路径
共用的，A 组同样能看到"可摘录原文"小节——**缺口只在提示词文字**，不需要
再动 `core/context_prefix.py` 或 `core/ontology.py`。

### 2.2 代价评估：选 (a)

同步 `s3_structured.yaml` 的措辞是一个提示词文件的局部改动（20 行不到）
+ 一条回归测试扩展，代价明显小于把 A 组整体移出闸门（那会让"C 组一次
通过率 ≥ A 组"这条硬指标失去比较对象）。选 (a)。

### 2.3 修复

`prompts/v1/s3_structured.yaml` 的"## ontology_refs"一节改成跟
`s3_derived.yaml` 逐字同一套措辞：点名"可摘录原文"小节、点名
`check_herb_source_fabricated`/`check_herb_source_paraphrased` 两个函数、
说明 veto/revise 的后果。`core/formula_verifier.py` 的交叉引用注释与模块
文档字符串（新增"## R61"一节）同步说明两份提示词都要讲同一件事。
`tests/test_prompt_verifier_contract.py` 参数化到两份提示词文件（之前只
测 `s3_derived.yaml` 一份，R60 那次"只改一处"的漏改本身测不出来——现在
测得出来）。

## §3 验证器一次通过率 B/C/D 全 0

<!-- R61_SECTION3_PLACEHOLDER -->

## §4 B→C 净贡献：从定性观察到可统计指标

用户真机实测的定性观察：B 组（无医理、无医案）给出「疏肝和胃汤」「温中
健脾和胃方」——不是任何一张经典方，是按治法凑的字；C 组（有医理、无
医案）给出「柴胡疏肝散加减」「黄芪建中汤加减」——真实存在的经典方加减。

**量化为 `formula_ontology_hit_rate`**（`eval/ablation/r57.py`）：方名去掉
"加减/加味"这类化裁后缀（`_split_formula_suffix`，跟 §1 的比较用归一化
是两个不同的问题——这里要"基础方名"去查本体，那里要"基础方名+统一后缀"
去比较两次采样，函数文档字符串写明区别）之后，能不能在方剂本体（235 首，
`core.ontology.get_ontology().formulas`）里查到。

本体不可用（`ontology.available=False`，沙盒/新 clone 常态）时这一格是
`None`（不适用），不是 0——跟 `rule_refs_applicable` 区分"不适用"与"0"
同一条诚实约束，报告表格与 `to_markdown` 已经落实。`build_report` 新增
`b_to_c_formula_ontology_hit_delta`，跟既有的 `b_to_c_verifier_first_pass_delta`
并排报——医理规则层的净贡献现在有两个可统计指标，不止一个。

全量（100 次问诊）跑完之后，这一项会有真实的 B vs C 命中率数字；本轮
沙盒/单条真机验证的数字见 §5。

## §5 修复验证

<!-- R61_SECTION5_PLACEHOLDER -->

## §6 全量测试与静态检查

`python -m pytest tests/ -q` → 4748 passed / 7 skipped（含本轮新增测试，
`tests/test_ablation_r57.py`/`tests/test_merge_r57.py` 单独跑绿）。
`ruff check` 全部通过。

## §7 给用户的验收命令

```bash
python -m eval.ablation.r57 --backend real --limit 2 --queries-path tests/queries.txt
```

**判据**：
- A 组 `n_ok ≥ 1`，且失败原因（如果还有）不再是 `herb_source_fabricated`
- 五组 `n_ok` 全部 ≥ 1（新增 E 组，配置跟 C 组相同）
- C-D 一致率的三项（证型/主方/治法）分开看，且报告里能看到跟 C-E 噪声
  地板的对照——`--limit 2` 样本量小于 `GATE_MIN_SAMPLE_SIZE`（5），三条
  相对一致率闸门会判「⏳ 样本不足」，这是预期行为，不是没修好；要看到
  这三条门真的判出 ✅/❌，需要不带 `--limit` 的全量 100 次跑
