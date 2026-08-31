# 名医辨证对照 Demo

输入患者症状，系统分别模拟两位古代名医——**叶天士**（`ye_tianshi`，《临证指南医案》）与
**吴鞠通**（`wu_jutong`，《吴鞠通医案》）——的辨证思路，各自给出证型、治法、方药，并把
两者并置对比、标出分歧。每条结论都必须引用它所依据的真实医案 id，用于防幻觉核查。

> 教学与研究用途，非诊断工具，不能替代执业医师。

## 这个 demo 能做什么、不能做什么

**能做：**
- 输入一段中文主诉，自动标准化为症状/舌象/脉象
- 分别推断两位医家风格下的证素、证型、治法、方药
- 检索并展示每条结论所依据的真实医案（id + 相似度），可展开核查
- 检测并标红"幻觉引用"——模型引用了检索结果之外的医案 id
- 用 Cytoscape 图直观展示"症状 → 证素 → 证型 → 药物"四层推理链
- 对证型名称做逐字比对，标出两位医家的粗略分歧

**不能做（明确边界）：**
- **不是诊断工具**，不能替代执业医师，不出具任何可执行的临床处方
- 只覆盖脾胃门（呕吐、痞满、肿胀、痰饮等相关证候），不是全科辨证系统
- 样本量仅各 15 案（共 30 案），不足以支撑统计结论，仅够验证系统管线
- 分歧判定是"证型名称精确字符串比对"的粗判，不是语义层面的辨证差异分析
- 遇到出血、剧痛等危重症状表现时，系统会在 `note` 中提示"建议转诊"，
  但**不做**安全否决层的强制拦截——这是 demo 阶段的已知简化，正式版需要补上
- 两位医家分处清初（叶天士）与清中（吴鞠通），时代是混杂因素，观测到的
  差异中含时代成分，不能直接等同于个人风格差异

## 项目结构

```
core/           数据模型（pydantic）、LLM 抽象层、证素表、检索、推理链——全项目地基
offline/        离线脚本：医案抽取（extract_cases.py）、SFT 样本导出（export_sft.py）
api/            FastAPI 服务（/api/consult、/health、静态文件）
web/            前端单页 index.html，无构建步骤，Cytoscape.js 走 CDN
prompts/v1/     版本化 prompt 模板（yaml，$var 占位符）
data/           医案原文（data/ye_tianshi/、data/wu_jutong/）与 SOURCES.md 版权说明
tests/          pytest 用例（全部不需要网络）+ tests/queries.txt 测试主诉
CLAUDE.md       项目架构与代码约定，改代码前建议先读
run.sh          一键运行脚本
```

## 环境准备

- Python 3.10+
- 一个 OpenAI 兼容的 LLM API key（默认接 DeepSeek；`sentence-transformers` 首次运行会
  从 HuggingFace 下载 `BAAI/bge-small-zh-v1.5` embedding 模型，需要网络）

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

## 一键运行

```bash
./run.sh
```

它会依次：建虚拟环境 → 装依赖 → 检查 `.env`（不存在则从 `.env.example` 生成并提示你去
填 key）→ 若 `cases.json` 不存在且已配置 key，自动跑一次 `offline.extract_cases` →
启动 `uvicorn`，打开 http://localhost:8000 即可使用。

常用参数：

```bash
PORT=8080 ./run.sh       # 换端口
./run.sh --skip-extract  # 跳过自动抽取（比如你想先小批量试跑再手动跑全量）
```

`run.sh` 是幂等的，重复执行安全；`.env` 一旦存在就不会被覆盖。

## 手动运行（等价于 run.sh 内部做的事）

```bash
pip install -r requirements.txt

# 医案原文已经放在 data/ye_tianshi/*.txt 和 data/wu_jutong/*.txt（见下方"数据来源"）
python -m offline.extract_cases            # 全量抽取，生成 cases.json
# 先跑几条试水：
python -m offline.extract_cases --limit 3

uvicorn api.main:app --reload
# 打开 http://localhost:8000
```

`cases.json`、`sft.jsonl`、`.venv` 都已在 `.gitignore` 中排除，不会被提交。

### 离线脚本一览

| 脚本 | 作用 |
|---|---|
| `offline/split_cases.py` | 从公版古籍全文（`367-临证指南医案.txt`、`361-吴鞠通医案.txt`）切出本仓库 `data/` 下的 30 个医案 txt。**仓库里已经是切好的结果**，不需要重新运行；仅在想扩大样本量或换书目时才需要，用法见 `data/SOURCES.md` 第 5 节 |
| `offline/extract_cases.py` | 用 LLM 把 `data/{physician}/*.txt` 抽取成结构化 `cases.json`，供检索层使用 |
| `offline/export_sft.py` | 从 `cases.json` 派生 alpaca 格式的 SFT 训练样本 `sft.jsonl`（`python -m offline.export_sft`）。现在数据量不够训练，这一步只是把管道建好，并在代码层面强制过滤掉 `copyright_status == "copyrighted"` 的记录 |

## 数据来源与版权

详见 [`data/SOURCES.md`](data/SOURCES.md)。简要结论：

- 叶天士《临证指南医案》15 案、吴鞠通《吴鞠通医案》15 案，均为公有领域（两位作者卒年
  远超著作权保护期），原文可自由使用
- 原文取自开源仓库 `xiaopangxia/TCM-Ancient-Books`，经 `offline/split_cases.py` 切分为
  单案初诊段
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
- prompt 渲染、markdown 围栏剥离
- `core/chain.py`：S1 全局只跑一次、分歧判定、幻觉检测
- `core/retrieval.py`：`cases.json` 缺失时的报错、文本编码格式
- `api/main.py` 的 `to_graph()`：边的两端节点一定存在于 nodes 里（这是最容易出 bug 的
  地方，专门写了断言函数 `assert_graph_edges_valid`）
- `offline/export_sft.py`：版权过滤、按字段是否为空决定生成哪些任务样本

以下需要真实 API key 和网络，属于人工验收范畴，不在 `pytest` 里自动跑：
`offline/extract_cases.py` 抽取质量、`core/chain.py` 的 `__main__` 块跑 10 条测试主诉、
前端实际点击"辨证"看效果。

## 常见问题

**首次运行很慢？**
`sentence-transformers` 第一次调用会从 HuggingFace 下载 `BAAI/bge-small-zh-v1.5`
（约几百 MB），只发生一次，之后会走本地缓存。

**报错说 `cases.json` 不存在？**
先运行 `python -m offline.extract_cases`（或直接用 `./run.sh`，它会自动检测并跑这一
步）。检索层（`core/retrieval.py`）在 `cases.json` 缺失时会给出明确的报错提示。

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
- 所有 LLM 输出都用 pydantic 模型承接，防幻觉的关键约束（`min_length=1`）不要放松
