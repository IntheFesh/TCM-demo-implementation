# AutoDL 交接清单

这个 sandbox 没有网络访问 DeepSeek、没有 `cases.json`。所有需要真实模型规模跑的
东西都留给 AutoDL。本文件说清楚：哪些已经做完、哪些只有骨架、按什么顺序跑、
每步拿什么判据验收。

**读这份清单之前先读两条纪律**（`CLAUDE.md` 的约定，下面每一步都受它约束）：

- **任何数字都必须带对照。** 没有基准的数字在这个项目里等于没有意义。
- **安全否决不许为了跑分关掉。** 需要量化它的代价时，两个数都报。

---

## 一、已完整的模块（代码 + 离线测试 + 真实模型冒烟）

真实冒烟全部用 `claude_cli` 后端跑的（这个环境连不上 DeepSeek）。**冒烟证明的是
代码路径在真实模型下跑得通，不是可比的分数**——换 DeepSeek 之后行为可能不同，
所有数字要重测。

| 模块 | 离线测试 | 真实冒烟做了什么 | 结论 |
|---|---|---|---|
| X2 输出侧安全（十八反十九畏 + 寒热一致性） | 24 条 | 3 次真实开方 + 2 次打回重开 | 0/3 踩禁忌；2/2 重开后避开构造的冲突 |
| G1 工具层（六件工具 + 信息增益追问候选） | 59 条 | 10 条主诉各算 top-3 候选问题（19 次调用） | 问题质量可用；实测出 30% 重复提问缺口 |
| G2 ReAct 取证 | 16 条 | 6 轮完整轨迹 + remaining 单变量实验 8 轮 | finish 位置跟着预算走；成本 = MAX_STEPS 次/医家 |
| A2 粗段切分（三本书） | 15 条 | 纯正则，**这一步不需要 LLM，已经跑完** | 叶 32 / 吴 25 / 张 43 段；张锡纯 43/43 段一段一病人 |
| SDT 适配器 | 18 条 | 格式验证 0 调用 + 5 条 prompt 质量冒烟（45 次调用） | Test 满分提交得 50.0000/50，格式逐字节正确；Task1 **0 条被改写**，逐字命中 36–38/47 |

整体视察（`data/SOURCES.md` 第 16 条）之后修了 58 处，其中 5 处是安全相关的真漏洞
（追问答「有」绕过否决、ReAct 的追问从未问出、安全正则误报/漏报、禁忌别名缺失、
寒热表条目永远匹配不上）。**修复只有离线测试覆盖，没有真实模型复测**——AutoDL 上
步骤 4/5 跑通后，X2/G2/G3 那几组冒烟值得重跑一遍确认行为没变。

`data/graph.json`（123 节点 / 377 边）已经建好并写入权重，**这一步也不需要 LLM**，
新环境里重跑 `build_graph` + `graph_stats` 即可复现。

---

## 二、只有代码和离线测试的模块（AutoDL 要补什么）

| 模块 | 现状 | AutoDL 上要补的 | 为什么这边补不了 |
|---|---|---|---|
| `core/chain.py` 端到端 `consult()` | 20 条离线测试全绿 | 用真实 `cases.json` 跑通 10 条主诉 | 没有 `cases.json`，检索器建不起来 |
| G3 追问闭环 | 23 条测试 + ScriptedPatient 离线演示 | `SimulatedPatient` 端到端；追问收益要以 ScriptedPatient 为上界做对照 | 同上（`consult` 需要检索器） |
| K2 医家级权重 λ1 | 12 条测试；λ1 恒为 0 | 挂入真实医案后重跑，确认 λ1 是否仍为 0 | 图里没有 case 节点 |
| K2 学派层 λ2 | 代码就绪；`num_schools=1` 时强制为 0 | **注册张锡纯之后** λ2 才第一次有真值 | 张锡纯还没进 `physicians.py`（见步骤 3） |
| X3 医案三元组 | `query_case_graph` 已就绪，读 `data/case_triples.jsonl` | 抽取脚本 + 产出这个文件 | 抽取要真实 LLM |
| SDT 全量评测 | 适配器 + 打分包装完成 | Test 50 条 × 两组 Solver | 400 次调用，成本与模型都不对 |

---

## 三、AutoDL 运行顺序

每步给：命令、预期产出、**验收判据**、不达标时怎么办。

### 步骤 0：环境自检

```bash
pip install -r requirements.txt
pytest -q                      # 判据：444 条全绿，秒级跑完（新 clone 上 conftest 会自动建图，不用先跑步骤 3）
python -c "import openai, os; print(bool(os.environ.get('LLM_API_KEY')))"
```

判据：测试全绿 + `.env` 里 `LLM_API_KEY` / `LLM_BASE_URL` 配好。
**不达标就别往下走**——后面每一步都比这一步贵。

### 步骤 1：三本书 → 粗段（纯正则，0 次调用，几秒）

```bash
# 下载见 README 第 3.1 步（三本：367 叶天士 / 361 吴鞠通 / 584 张锡纯）
python -m offline.split_cases --stats-only --books-dir books
python -m offline.split_cases --books-dir books
cp out/ye_tianshi/*.json data/ye_tianshi/ && cp out/wu_jutong/*.json data/wu_jutong/
mkdir -p data/zhang_xichun && cp out/zhang_xichun/*.json data/zhang_xichun/
```

预期产出：`out/{pid}/*.json`，叶 32 / 吴 25 / 张 43 段。

判据：`--stats-only` 打出来的三行统计与本文件第一节的数一致；张锡纯那行还会
多打一句「每段恰好一条『属性：』身份行：43/43」。**对不上就是书下错了或版本变了，
先查这个再往下走。**

⚠️ `head_hints` 跨书不可比（正则按叶天士案首体例写的，对张锡纯不触发），
别拿三本书的 `head_hints` 并排比较。见 `data/SOURCES.md` 第 13 条。

### 步骤 2：粗段 → `cases.json`（真实 LLM，约 100–150 次调用）

```bash
python -m offline.extract_cases --limit 3     # 先看 3 条的结构化质量
python -m offline.extract_cases               # 确认无误再全量
```

判据：
- `cases.json` 非空，且三位医家都有记录（`python -c "import json,collections;print(collections.Counter(c['physician'] for c in json.load(open('cases.json'))))"`）
- 看一眼 `extract_warnings.json`：LLM 判断的诊次数与正则估计差 ≥2 的案会记在这里，
  抽样人工核对几条
- **张锡纯这批应当特别干净**：原文一段一个病人、自带 `病因/证候/诊断/处方/效果/复诊`
  字段标记。如果他的抽取结果反而比另外两位差，说明 `s0_extract_case` 的 prompt
  对这种体例不适配，值得单独看一眼

### 步骤 3：注册张锡纯 + 重建图 + 权重（0 次调用）

**这是 A2 的后半段，我这边刻意没做**——注册进去会让 `num_schools` 变成 2、λ2 从恒 0
变成真值，同时让 `consult` 跑三位医家而他当时还没有 `cases.json`。现在有了才能做。

```python
# core/physicians.py 加一条（school 用「衷中参西」，跟温病那两位区分开）
"zhang_xichun": {"name": "张锡纯", "book": "医学衷中参西录",
                 "years": "1860-1933", "school": "衷中参西", "color": "<挑一个>"},
```

```bash
python -m offline.build_graph
python -m offline.graph_stats
```

注册之后 `pytest` 仍应全绿：chain/weights 的测试已钉到两位医家（`tests/test_chain.py`
`tests/test_weights.py` 顶部的 autouse fixture），registry 增长不影响它们。

判据：
- `graph_stats` 的「当前学派数」变成 2，**λ2 的强制归零警告不再出现**，取而代之
  的是「已有 2 个学派……某个学派下只有一位医家时 λ2 对他不构成独立信号」的提示
- λ1 分布：预期**仍然接近全 0**（清代医案术语与国标不对齐，见 `SOURCES.md` 第 8 条）。
  **如果 λ1 真的非 0 了，那是好消息但必须先确认不是 bug**——去看 `count_support`
  数到的是哪些 (症状, 证素) 对，抽查它们在医案里确实成立
- λ2 现在是第一次有真值。**它仍然不是可信信号**：两个学派、其中一个只有一位医家，
  这个数要标注样本结构后才能引用

### 步骤 4：`consult()` 端到端（真实 LLM，10 条主诉约 40–60 次调用）

```bash
python -m core.chain            # 跑 tests/queries.txt 的 10 条
```

判据：
- 第 10 条（黑色柏油样便）必须被安全否决拦下，且**不产出任何方药**
- 幻觉例数应为 0（`cited_case_ids` 全部来自检索结果）
- 分歧统计里主看 `herb_jaccard`，不要看 `same`（证型名字符串比对没有区分度，
  实测 9/9 全判为分歧，见 `SOURCES.md`）
- 记下平均耗时——它决定 SSE 分步进度是不是必须的（预期三位医家 + ReAct 约 20 次
  调用、P95 延迟约 70s，**基本可以确定是必须的**）

### 步骤 5：G3 追问端到端（真实 LLM）

```python
from core.chain import consult
from eval.patient_sim import ScriptedPatient, SimulatedPatient
consult(complaint, ask_fn=SimulatedPatient(profile="<病情，只有它自己知道>"))
```

判据与对照：
- **两个患者都要跑。** `ScriptedPatient` 是完美患者、给的是追问收益的**上界**；
  `SimulatedPatient` 才是现实。报追问收益必须说明用的哪个，两者的数不能混
- 看 `followup.stopped_by` 的分布：`converged` 与 `max_rounds` 要分开统计——
  前者说明追问设计有效，后者说明轮次上限卡住了它
- **重点看重复提问**：这边实测 8/27（约 30%）的候选问题问的是患者已经说过的症状
  （换了个说法所以匹配不上）。这是术语映射缺口，AutoDL 上要重新测一次比例
- 追问回答里若出现危重症状，必须整轮终止且不产出方药（`stopped_by="safety"`）。
  **两种形式都要验**：回答原文带危重词（「有，还解了黑便」），以及被问的症状本身
  危重、患者只答一个「有」（「有没有便血？」→「有」）——后者此前是漏的
- ReAct 开着时（`USE_REACT=1`）它自己的 `ask_user` 追问也会通过同一个 `ask_fn` 问出去，
  回答先过 `check_safety`；被拦时 `rejected=True` 且 `results` 为空

### 步骤 6：X3 三元组抽取

产出 `data/case_triples.jsonl`，一行一条 `{case_id, physician, s, p, o, source_span}`。
`source_span` 是这条三元组在医案原文里的出处，**不能省**——省了它，工具查出来的东西
跟凭空生成的没区别，防幻觉链条在工具这一层就断了。

判据：文件生成后 `query_case_graph` 的 `available` 变成 `true`；抽查若干条的
`source_span` 确实能在对应医案原文里找到。

### 步骤 7：SDT 评测（真实 LLM，约 400 + 64 次调用）

```bash
export SDT=<TCMEval>/evaluation/TCMEval-SDT
python -m eval.sdt.run --sdt-dir $SDT --split Test --solver baseline --out out/sdt_base.txt
python -m eval.sdt.run --sdt-dir $SDT --split Test --solver chain    --out out/sdt_chain.txt
# 量化安全否决的代价：只重跑被拦的那 8 条，其余 42 条两次输入完全相同
python -m eval.sdt.run --sdt-dir $SDT --split Test --solver chain --ignore-safety-veto \
    --only-ids <8 个病案 ID> --out out/sdt_chain_nosafety_part.txt
# 把这 8 行并回 out/sdt_chain.txt 的副本，再打一次分
```

打分：

```python
from eval.sdt.score import score_submission
score_submission(SDT, "Test", "out/sdt_chain.txt")
```

判据与报数口径：
- **主报告用 Test**（金标准不带 BOM，满分上限就是 50.0）。Validation 只作调试，
  引用它的数必须标注「上限 48.9998/50，因官方金标准带 BOM 致首条恒 0」
- **`chain − baseline` 才是结构化推理链的贡献**，单独一个 chain 分数说明不了任何事
- 安全否决两个数都报，句式：「本系统 SDT 得分为 X/50，其中 8 条（16%）因触发
  危重症状安全否决而未作答。关闭安全否决后得分为 Y/50。差值 Y−X 是安全设计的
  代价，我们选择保留它。」
- **Task1 的基线已经量出来了：n=5 上逐字命中 36/47，0 条被归一化改写**
  （`SOURCES.md` 第 15 条）。改 `prompts/v1/sdt_extract.yaml` 之前先读那一条——
  我已经试过加粒度规则，**反而从 36/47 降到 33/47**（修好了「多带修饰语」，
  却开始过度切分金标准保留为复合词的条目），已回退。正确方向是拿 Train 的
  200 条做 few-shot 学标注者的粒度习惯。**改完必须用同样这 5 条重测跟 36/47 比**
- **Task2/3 有明显的过度选择**（n=5 观察，待确认）。prompt 已经写了「拿不准
  宁可不选」，模型仍然多选，而官方 `score_proportional` 对多选有惩罚。
  ⚠️ 官方计分对 Task1 的多摘没有惩罚（惩罚代码被注释掉了）——**不要**因此
  去教模型在 Task1 上多摘，那是钻评分脚本的漏洞，不是提升系统

### 步骤 8：X1 打分 / ε 噪声地板 / K3a·K3b / V1 全量评测

**这几项的 spec 在 V3 计划文档里，不在代码库里，我这边没有。** 不替你补，
免得写出一份看起来完整、实际跟计划对不上的步骤。开跑前把它们的定义（指标怎么算、
对照组是什么、判据是什么）落到本文件里再动手。

---

## 四、已知问题（留给 AutoDL 或正式版）

按「会不会影响结论」排序。

1. **术语映射层缺失（影响面最大）。** 患者/古籍用词与国标术语对不上，这一条同时
   造成了四个下游问题：λ1 恒为 0（医案证型 91 条中 0 条匹配国标）、追问 30% 重复
   提问、ReAct 在患者原话上反复试探图谱烧步数、SDT Task1 与我们 S1 方向相反。
   `SOURCES.md` 第 8、10、12、14 条从四个角度记的是同一件事。
   建映射层是独立的工作量，不属于任何一轮的收尾。
2. **标准症状集合里有近重复节点。** 93 个节点里「大便稀溏 / 大便溏稀 / 大便溏薄 /
   便溏」是同一件事的四个节点，「口干唇燥 / 口干咽燥」两个。**不能直接合并**——
   节点名来自不同证候定义的原文措辞，合并会丢掉可追溯性。要归一得靠映射层。
3. **`食积` 不是任何一条证候的证素**，17 条证候里缺一条食滞胃脘证。实测主诉 6
   （嗳腐吞酸、恶闻食臭）因此后验完全是平的，追问退化成问全局最能分叉的问题。
   这是数据覆盖缺口，补一条证候定义即可，但要按项目规矩从可核实来源录入。
4. **ReAct 的 `terminated_by` 不能无条件当信号用。** 实测去掉 prompt 里的剩余步数
   计数之后，`max_steps` 收尾从 0/5 涨到 4/8——「证据够了」这个判断是会被 prompt
   改动弄丢的，不是模型稳定具备的能力。G3 的追问决策把它当参考，不当依据。
5. **`prompts/v1/s3_react.yaml` 里的「还剩 N 步」要保留。** 去掉是纯亏（步数
   5.00→4.75 几乎没降，finish 率 5/5→4/8）。降成本的杠杆是 `MAX_STEPS` 本身，
   但那是成本与收尾质量的取舍，不是免费午餐。
6. **`claude_cli` 后端跑出来的数一概不可比。** 它是 Claude 不是 deepseek-chat，
   且约 $0.063/次（DeepSeek 约 $0.0007/次，190 倍）。`manifest.comparability_warning`
   会一路带到报告里，别把它擦掉。

---

## 五、报数纪律（写材料时逐条对照）

- 每个数字旁边必须有对照基准（对照组的数，或噪声地板 ε）。
- 分母要写出来。`automated_score` 返回的是求和不是均值，「32.7」不写分母是 50
  还是 200，读的人无从判断。
- 后端和模型如实写。`manifest` 里的 `model` / `backend` / `use_react` /
  `llm_calls` 就是「这个数字怎么来的」的全部凭据。
- 安全否决拦掉的记录数要跟分数一起报，否则读的人会以为是模型答错了。
- `head_hints` 只在案首体例相同的书之间可比。
- λ2 在只有两个学派、其中一个只有一位医家时仍然不是可信信号。
