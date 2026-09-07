# 名医辨证对照 Demo

输入患者症状，系统分别模拟两位古代名医——**叶天士**（`ye_tianshi`，《临证指南医案》）与
**吴鞠通**（`wu_jutong`，《吴鞠通医案》）——的辨证思路，各自给出证型、治法、方药，并把
两者并置对比、标出分歧。每条结论都必须引用它所依据的真实医案 id，用于防幻觉核查。

> 教学与研究用途，非诊断工具，不能替代执业医师。

## 这个 demo 能做什么、不能做什么

**能做：**
- 输入一段中文主诉，自动标准化为症状/舌象/脉象
- 危重症状（消化道出血、意识改变、休克、持续剧痛等信号）在证素推断**之前**
  被硬性拦截，直接拒绝辨证、不产出任何方药——不是事后在备注里提醒转诊
- 分别推断两位医家风格下的证素、证型、治法、方药
- 检索并展示每条结论所依据的真实医案（id + 相似度），可展开核查
- 检测并标红"幻觉引用"——模型引用了检索结果之外的医案 id
- 用 Cytoscape 图直观展示"症状 → 证素 → 证型 → 药物"四层推理链
- 对证型名称做逐字比对，标出两位医家的粗略分歧

**不能做（明确边界）：**
- **不是诊断工具**，不能替代执业医师，不出具任何可执行的临床处方
- 只覆盖脾胃门（呕吐、痞满、肿胀、痰饮等相关证候），不是全科辨证系统
- 安全拦截的关键词表是最小 demo 范围（见 `core/safety.py`），**没有经过医学生
  审核**，可能漏报（关键词没覆盖到的危重表述）或误报（子串命中过宽）
- 样本量仅各 30 案（共 60 案），不足以支撑统计结论，仅够验证系统管线；带复诊
  序列的案叶天士 30/30、吴鞠通 28/30（regex 信号意义上，详见 `data/SOURCES.md`），
  离原研究方案 ≥50 例的门槛还有距离——而且这 60 案目前还没有真正跑通抽取管线
  （见下方「快速开始」第 3 步，这是新装这个 repo 时最容易卡住的地方）
- 数据模型已支持多诊次序列（`case_group_id`/`visit_index`/`prev_case_id`），
  但**纵向证型转移预测本身还没做**——目前每次辨证仍是单次快照式的
- 分歧判定是"证型名称精确字符串比对"的粗判，不是语义层面的辨证差异分析
- 两位医家分处清初（叶天士）与清中（吴鞠通），时代是混杂因素，观测到的
  差异中含时代成分，不能直接等同于个人风格差异

## 快速开始

跑通这个 demo 分四步，其中**第 3 步是唯一需要按顺序、分子步骤做的地方**——
其余步骤都是单条命令。

### 环境要求

- Python 3.10+
- 一个 OpenAI 兼容的 LLM API key（默认接 DeepSeek），第 3 步会用到
- 网络：下载古籍原文、调用 LLM API、首次运行下载 embedding 模型都需要网络

### 第 1 步：装依赖

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 第 2 步：配置 API key

```bash
cp .env.example .env
# 编辑 .env，至少填好 LLM_API_KEY
```

`.env` 关键字段：

| 字段 | 说明 |
|---|---|
| `LLM_MODE` | `api`（默认，走 OpenAI 兼容接口）或 `local`（正式阶段本地 vLLM 部署用，demo 阶段不要求跑通） |
| `LLM_API_KEY` | DeepSeek（或其他 OpenAI 兼容服务）的 API key |
| `LLM_BASE_URL` | 默认 `https://api.deepseek.com` |
| `LLM_MODEL` | 默认 `deepseek-chat` |

### 第 3 步：生成 `cases.json`（医案结构化数据）

**这是新装这个 repo 时最容易卡住的一步**，因为它不是一条命令，而是一个三段
流水线：切粗段 → 挪目录 → 抽取。仓库里已经有 `data/ye_tianshi/*.txt`、
`data/wu_jutong/*.txt` 各 30 个候选案原文，但**这些是旧架构留下的文件，
当前的抽取脚本读不了它们**——直接跳到第 4 步会得到一个空的 `cases.json`，
且不会报错提醒你（这一点 `run.sh` 会帮你检查，见下方"一键脚本"）。

**3.1 下载古籍原文**（三本书没有随仓库分发，体积大、且是可独立获取的公开数据）：

```bash
mkdir -p books
curl -o 367.txt "https://raw.githubusercontent.com/xiaopangxia/TCM-Ancient-Books/master/367-%E4%B8%B4%E8%AF%81%E6%8C%87%E5%8D%97%E5%8C%BB%E6%A1%88.txt"
curl -o 361.txt "https://raw.githubusercontent.com/xiaopangxia/TCM-Ancient-Books/master/361-%E5%90%B4%E9%9E%A0%E9%80%9A%E5%8C%BB%E6%A1%88.txt"
curl -o 584.txt "https://raw.githubusercontent.com/xiaopangxia/TCM-Ancient-Books/master/584-%E5%8C%BB%E5%AD%A6%E8%A1%B7%E4%B8%AD%E5%8F%82%E8%A5%BF%E5%BD%95.txt"
mv 367.txt "books/367-临证指南医案.txt" && mv 361.txt "books/361-吴鞠通医案.txt"
mv 584.txt "books/584-医学衷中参西录.txt"
```

584（张锡纯《医学衷中参西录》）是 A2 加进来的第三本。只下载前两本也能跑——
`split_cases.py` 会跳过缺书的医家并提示，不会崩。

连不上 GitHub？见下方[常见问题](#常见问题)。

**3.2 把古籍切成"粗段"**（纯正则，不需要 API key，不调用 LLM）：

```bash
# 先看一眼统计（秒级，不写文件）：
python -m offline.split_cases --stats-only --books-dir books

# 确认没问题，真正切出粗段，写到 out/{physician}/*.json：
python -m offline.split_cases --books-dir books
```

这一步产出的是"粗段"，还不是最终的病人级案例——一个粗段里可能有好几个病人，
具体有几个、每人几次复诊，交给第 3.4 步的 LLM 判断。想调整素材范围（比如某位
医家的粗段/病人数不够），能调的是 `split_cases.py` 里的 `gates`（门类关键词）
和 `--max-len`（粗段长度上限）。

**3.3 把粗段挪进 `data/`**（`extract_cases.py` 只从这里读）：

```bash
cp out/ye_tianshi/*.json data/ye_tianshi/
cp out/wu_jutong/*.json data/wu_jutong/
```

跟目录里已有的 30+30 个 `.txt` 共存没有问题——下一步的抽取脚本只认 `.json`。

**3.4 用 LLM 把粗段展开成结构化病人记录**：

```bash
python -m offline.extract_cases --limit 3   # 先小批量看结构化质量
python -m offline.extract_cases             # 确认无误再跑全量，生成 cases.json
```

跑完看一眼 `extract_warnings.json`——LLM 判断的诊次数和正则估计的诊次数差距
≥2 的案会记在这里（不会被丢弃，只是提示"建议人工看一眼"）。

### 第 4 步：启动服务

```bash
uvicorn api.main:app --reload
# 打开 http://localhost:8000
```

### 一键脚本

```bash
./run.sh
```

`run.sh` 自动做的是第 1、2、4 步，以及第 3.4 步（前提是 `data/{physician}/*.json`
已经存在）——**它不会替你做第 3.1-3.3 步**（下载古籍、切粗段、挪目录），这几步
涉及要不要重新下书、要不要调整切分参数，交给人判断更合适。如果 `data/{physician}/`
下还没有 `.json` 粗段，`run.sh` 会检测到并提示你先完成第 3.1-3.3 步，不会像
`extract_cases.py` 单独跑那样静默产出一个空 `cases.json`。

常用参数：

```bash
PORT=8080 ./run.sh                     # 换端口
./run.sh --skip-extract                # 跳过自动抽取（cases.json 已存在或想手动控制时用）
./run.sh --resplit-data=books          # 帮你跑第 3.2 步（仍需要你自己做 3.1 和 3.3）
```

`run.sh` 是幂等的，重复执行安全；`.env` 一旦存在就不会被覆盖。

## 项目结构

```
core/           数据模型（pydantic）、LLM 抽象层、证素表、检索、推理链、安全否决——全项目地基
  graph/        知识图谱存储层（K1/K2，见下方"知识图谱权重"一节）
offline/        离线脚本：医案切分/抽取、SFT 样本导出、图谱构建
api/            FastAPI 服务（/api/consult、/health、静态文件）
web/            前端单页 index.html，无构建步骤，Cytoscape.js 走 CDN
prompts/v1/     版本化 prompt 模板（yaml，$var 占位符）
data/           医案原文（data/ye_tianshi/、data/wu_jutong/）、证候标准数据、SOURCES.md 版权说明
tests/          pytest 用例（全部不需要网络）+ tests/queries.txt 测试主诉
CLAUDE.md       项目架构与代码约定，改代码前建议先读
run.sh          一键运行脚本
```

## 离线脚本一览

| 脚本 | 作用 |
|---|---|
| `offline/split_cases.py` | 见「快速开始」第 3.2 步。`--stats-only` 是零 LLM 调用的正则前置闸门 |
| `offline/extract_cases.py` | 见「快速开始」第 3.4 步 |
| `offline/export_sft.py` | 从 `cases.json` 派生 alpaca 格式的 SFT 训练样本 `sft.jsonl`（`python -m offline.export_sft`）。现在数据量不够训练，这一步只是把管道建好，并在代码层面强制过滤掉 `copyright_status == "copyrighted"` 的记录 |
| `offline/build_graph.py` | 从 `data/standard/syndromes.jsonl` 建知识图谱骨架（symptom/element/syndrome 三类节点，`python -m offline.build_graph`），并打印语料库门类覆盖检查 |
| `offline/graph_stats.py` | 给图里的 indicates 边算并写回医家级四层收缩权重，打印节点/边分布、λ1 分布等统计（`python -m offline.graph_stats`）——**λ 相关的数字务必看下面"知识图谱权重"一节的 λ2 说明再解读** |

## 知识图谱权重（进阶功能，非 consult 主流程必需）

`offline/build_graph.py` + `offline/graph_stats.py` 是独立于上面「快速开始」
consult 流程之外的一个附加模块：从标准证候定义建知识图谱骨架，并给每条
indicates（症状→证素）边算一个四层收缩权重——医家层 → 学派层 → 全局层 →
标准先验层，越往医家层数据越少就越往后收缩，标准先验（`syndromes.jsonl` 里的
`is_cardinal`）保证任何数据量下权重都有定义。跑法：

```bash
python -m offline.build_graph
python -m offline.graph_stats
```

`core/tools.py`（智能体工具层）里的 `query_graph`、`check_residual`、
`question_candidates` 读的就是这两步产出的 `data/graph.json`。图没建的时候它们
不会崩，但会退化：前两个返回带 `error` 的结果，`question_candidates` 退到十问歌
固定顺序。要看到基于信息增益的追问，先跑上面两条命令。

> **λ2（学派层）当前是假信号，不要引用它做任何跨学派结论。**
> 当前仅 1 个学派（2 位医家：叶天士、吴鞠通均为温病学派），λ2 学派层与医家层高度
> 共线，其数值不构成独立信号，等加入第二学派后需重新评估。凡是本项目里出现的
> λ 分布图/表，只要没有单独标注"已含第二学派"，都受这条限制约束。

另外，`λ1`（医家层权重）目前对所有边都是 0，权重全部退化到标准先验层——这不是
占位符，是真实计算结果：图里还没有真实 `case` 节点（`case_group_id` 会在真正
跑通「快速开始」第 3 步、生成非空 `cases.json` 之后才写入图）。等真实数据接入，
重跑 `graph_stats.py` 会自动出现非零 λ1，不需要改代码。详见 `data/SOURCES.md`
第 7 节。

## ReAct 取证模式（进阶功能，默认关）

默认路径是 S1→S2→检索→S3 四步直出。打开 `USE_REACT=1` 之后，S3 之前会多一轮
ReAct 取证：模型自己决定查什么（查国标图谱、查标准证候定义、查医案三元组、
算残差、或者向患者提问），最多 5 步，查到的东西作为附加证据接在 S3 提示词后面。

```bash
USE_REACT=1 ./run.sh          # 或 uvicorn api.main:app --reload
```

要点：

- **最终输出的 schema 没变**，还是 `S3Syndrome`，`cited_case_ids` 仍然只能引用
  检索到的参考医案 id。ReAct 只负责补证据，不负责下结论——让模型在最后一步
  直接吐结论，等于把防幻觉校验搬进一个更长更容易跑偏的上下文里。
- **不开 ReAct 时 S3 的提示词跟改造前逐字节一致**，所以 `use_react` 开/关可以
  直接做 A/B，不会混进 prompt 变化这个额外变量。
- **代价是调用数，而且实测是确定的 MAX_STEPS 次，不是"1–5 次"**。6 轮真实冒烟
  里没有一轮提前收尾（预算 5 步就在第 5 步 finish，预算 3 步就撞 max_steps）。
  两位医家开 ReAct，S3 阶段从 2 次调用变成 12 次。`manifest.llm_calls` 会如实
  计入，`manifest.use_react` 记这次开没开——拿调用数算成本时看这两个字段，
  别按"上限"估。试过去掉提示词里的剩余步数计数来让它早点收尾，n=8 的对照
  实验证明那样只会更糟（步数 5.00→4.75 几乎没降，finish 率却从 5/5 掉到 4/8）。
  详见 `data/SOURCES.md` 第 11、12 条。
- 结束原因分五种记在 `react_trace.terminated_by`：`finish`（模型自己判断够了）、
  `ask_user`（它要追问）、`max_steps`（撞上限）、`no_progress`（连续重复同一个
  调用）、`error`（LLM 调用失败）。**撞 max_steps 不等于正常结束**——它说明
  提示词没让模型知道什么时候算够了，看统计时这两者必须分开。

## 追问（G3，需要提问渠道才会启用）

`consult()` 收一个 `ask_fn`：给一个问题、返回患者的回答。不传就不追问——
没有提问渠道时静默跳过是对的，不是错误。

```python
from core.chain import consult
from eval.patient_sim import ScriptedPatient

consult("胃脘胀痛，嗳气泛酸，纳差", ask_fn=ScriptedPatient(present=["两胁胀满"]))
```

问什么由 `core/tools.py` 的信息增益决定（见上一节），最多问 `MAX_ASK_ROUNDS=3` 轮。
四件事值得知道：

- **每轮 0 次 LLM 调用**。答案解析走规则（问的是「有没有 X？」这种封闭问题，
  答案本质上就是是/否/不确定），后验更新是纯图计算。整个追问只在**问出了新症状
  之后**整体重跑一次 S2，把新症状并进证素——按轮收费的话这个 demo 就没法用了。
- **否定回答是证据，不只是去重**。患者说「没有口苦」会把以它为主症的证候压下去，
  跟他说「有口苦」把它们抬上来是同一件事的两面。这些否认也会写进 S3 的提示词，
  否则医家模型看不到，照样可能按那条症状开方。
- **回答先过 `check_safety`**。追问问出「有黑便」跟初始主诉里写了黑便是同一道
  否决：整轮终止、不产出任何方药。这是 CLAUDE.md 的硬约束——追问是安全否决层
  的后门，不堵上等于前面那道拦截白做。
- **`FAST_MODE=1` 跳过整个追问阶段**，演示嫌慢时用。

停止原因记在 `followup.stopped_by`，六种：`max_rounds`（问满）、`converged`
（再问也问不出信息了）、`no_candidate`（没问题可问）、`safety`（回答触发否决）、
`fast_mode`、`no_answer`（没有提问渠道 / 对方不答）。**`converged` 和 `max_rounds`
必须分开看**：前者说明追问设计有效，后者说明轮次上限卡住了它，混成一个就没法调
`MAX_ASK_ROUNDS`。

患者模拟器在 `eval/patient_sim.py`：`ScriptedPatient`（不调 LLM，按预设症状机械
作答，是追问效果的**上界**，报告里引用追问收益必须说明用的是哪个患者）和
`SimulatedPatient`（LLM 扮演，每问 1 次调用，只用于 eval/）。

## 数据来源与版权

详见 [`data/SOURCES.md`](data/SOURCES.md)。简要结论：

- 叶天士《临证指南医案》30 案、吴鞠通《吴鞠通医案》30 案，均为公有领域（两位作者卒年
  远超著作权保护期），原文可自由使用
- 原文取自开源仓库 `xiaopangxia/TCM-Ancient-Books`，经「快速开始」第 3 步的切分/
  抽取流水线处理为完整诊次序列（保留复诊，不再只取初诊段）
- `tests/queries.txt` 的 10 条测试主诉为本项目合成，非真实病例，**使用前应先经医学生
  审核**，确认表述符合中医临床描述习惯
- 将来若接入现代出版的名老中医经验集，必须标 `copyright_status: copyrighted`，
  且只能抽取事实性三元组、不得让原文进前端展示或训练集——`export_sft.py` 里的
  过滤断言会自动拦截

## 测试

```bash
pytest
```

全部 pytest 用例都不需要网络（LLM、embedding 模型调用均用假后端 mock 掉），可以在没有
API key 的环境里直接跑，覆盖：
- `core/schemas.py` 的防幻觉约束（`min_length=1`）确实拒绝空引用
- `core/safety.py`：危重症状关键词命中拦截、子串匹配、拒绝理由去重
- `core/chain.py`：S1 全局只跑一次、安全否决命中时 S2/S3 零调用、分歧判定、幻觉检测
- `core/retrieval.py`：`cases.json` 缺失时的报错、文本编码格式
- `api/main.py` 的 `to_graph()`：边的两端节点一定存在于 nodes 里（这是最容易出 bug 的
  地方，专门写了断言函数 `assert_graph_edges_valid`）、安全否决时端点返回空图而非崩溃
- `offline/export_sft.py`：版权过滤、按字段是否为空决定生成哪些任务样本
- `offline/extract_cases.py`：假 LLM 后端跑通整条抽取管线——诊次正确展开成
  `CaseRecord`、`case_id` 格式、`prev_case_id` 链式关系、`case_group_id` 一致性、
  交叉校验不一致时正确写进 `extract_warnings.json` 而不是被静默丢弃
- `core/graph/`：图存储的增删查改、四层收缩权重的数学边界（λ 恒和为 1）

以下需要真实 API key 和网络，属于人工验收范畴，不在 `pytest` 里自动跑：
「快速开始」第 3.4 步实际抽取质量（诊次切得准不准）、`core/chain.py` 的
`__main__` 块跑 10 条测试主诉、前端实际点击"辨证"看效果。

## 常见问题

**首次运行很慢？**
`sentence-transformers` 第一次调用会从 HuggingFace 下载 `BAAI/bge-small-zh-v1.5`
（约几百 MB），只发生一次，之后会走本地缓存。

**报错说 `cases.json` 不存在，或 `cases.json` 是空的？**
说明「快速开始」第 3 步没有跑完整——`extract_cases.py` 需要 `data/{physician}/*.json`
（粗段）才能生成非空的 `cases.json`；如果这些 `.json` 不存在，`extract_cases.py`
会正常退出但什么也不抽（不报错）。按第 3.1-3.4 步重新走一遍。`run.sh` 会在
`.json` 缺失时提前拦下来提示你，单独跑 `python -m offline.extract_cases` 不会。

**古籍原文从哪下载？网络受限怎么办？**
见「快速开始」第 3.1 步的 `curl` 命令，走的是 `raw.githubusercontent.com`。如果你的
网络环境连不上 GitHub 但能访问其他地方，`xiaopangxia/TCM-Ancient-Books` 仓库里的
`367-临证指南医案.txt` / `361-吴鞠通医案.txt` 也可以手动下载后放进任意目录，用
`--books-dir` 指给 `split_cases.py`。

**`extract_warnings.json` 是什么，要不要管？**
`offline/extract_cases.py` 会把 LLM 判断的诊次数和正则估计的诊次数对比，差距
≥2 的案会记进这个文件（不会因此丢弃数据）。它是"这个案可能被模型切错了，建议
人工看一眼"的清单，不是错误日志，也不需要每次都清空处理——但一次全量跑完后过一遍
它，是检查抽取质量最快的办法。

**DeepSeek 返回格式报错 / json_object 相关报错？**
DeepSeek 的 `response_format={"type":"json_object"}` 要求 prompt 里必须显式提到"JSON"
字样，否则会报错——`core/llm.py` 的 `OpenAICompatBackend` 已经在 system 提示词里带上了
schema 说明和"只输出 JSON"的要求。如果你换了其他 OpenAI 兼容服务商，注意确认它对
`json_object` 模式有没有类似要求。

**LLM 返回的 JSON 解析失败？**
`core/llm.py` 会自动剥离常见的 markdown 围栏（` ```json ... ``` `），并在校验失败时
把错误信息回灌给模型重试（最多 2 次）。如果 3 次都失败，会抛出带 step 名和原始返回
前 500 字的异常，方便定位是哪一步、模型实际返回了什么。

**换成本地部署（vLLM）？**
`.env` 里把 `LLM_MODE` 改成 `local`，配置 `LLM_MODEL_PATH` / `LORA_DIR`。
`core/llm.py` 里的 `VLLMBackend` 目前只是占位声明了接口形状，正式接入时需要按
类注释里的说明补全（guided_decoding 做结构化约束、LoRA 热切换按 physician 选不同
LoRA_DIR）。

## 架构约定

所有实现细节与代码风格约定见 [`CLAUDE.md`](CLAUDE.md)，其中最重要的几条：

- 这是 walking skeleton：所有实现都要能被单独替换而不改调用方
- prompt 模板一律用 `string.Template`（`$var`），禁止 `str.format()`
- S1（症状标准化）全局只跑一次，两位医家共用结果，避免症状节点 id 对不上
- 安全否决必须发生在 S2（证素推断）之前，被拦截的请求不产出任何方药
- 所有 LLM 输出都用 pydantic 模型承接，防幻觉的关键约束（`min_length=1`）不要放松
