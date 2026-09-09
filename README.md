# 名医辨证对照 Demo

输入患者症状，系统分别模拟两位古代名医——**叶天士**（`ye_tianshi`，《临证指南医案》）与
**吴鞠通**（`wu_jutong`，《吴鞠通医案》）——的辨证思路，各自给出证型、治法、方药，并把
两者并置对比、标出分歧。每条结论都必须引用它所依据的真实医案 id，用于防幻觉核查。

> 患者 / 学生 / 研究者模式：教学与研究用途，非诊断工具，不能替代执业医师。
> 医生模式：处方辅助工具。系统提供的方剂与剂量为建议，最终处方由执业医师
> 审核、修改并签发，医师承担全部临床责任。所有导出操作均记录审计日志。

## 这个 demo 能做什么、不能做什么

**能做：**
- 输入一段中文主诉，自动标准化为症状/舌象/脉象
- 危重症状（消化道出血、意识改变、休克、持续剧痛等信号）在证素推断**之前**
  被硬性拦截，直接拒绝辨证、不产出任何方药——不是事后在备注里提醒转诊
- 分别推断两位医家风格下的证素、证型、治法、方药
- 检索并展示每条结论所依据的真实医案（id + 相似度），可展开核查
- 检测并标红"幻觉引用"——模型引用了检索结果之外的医案 id
- 用 Cytoscape 图直观展示"症状 → 证素 → 病名·证型 → 方剂 → 药材"六层推理链
  （方剂/药材是 compound 父子节点，同一位医家的 2-3 个候选方各自带自己的药，
  症状不直接连方剂——完整链路才是推理，直连只是共现匹配）
- 以两位医家用药集合的 Jaccard 距离 + 治法是否一致为主指标标出分歧（证型名逐字比对只作附注，实测无区分度），
  并对照噪声地板 ε（`offline/estimate_epsilon.py`）判断这个分歧是不是真实的、不是重复采样的抖动
- 检索支持三种独立信号（语义相似度 dense / 关键词 bm25 / 证素结构 graph）互相融合，
  见「检索：三路融合」一节
- 同一病人的复诊序列可以按证素状态摆成轨迹（`core/transition.py`），不做转移概率预测（样本量不够）
- 用 McNemar 检验判断两种配置（比如两种检索模式）之间的差异是不是统计显著，不是靠肉眼比大小
  （`eval/run_eval.py` + `eval/mcnemar.py`）

**不能做（明确边界）：**
- **不是诊断工具**，不能替代执业医师——但这条边界要按模式分开讲，不能笼统
  说"系统不出具任何可执行的临床处方"：patient / student / researcher 三种
  模式确实如此；**doctor 模式的可编辑处方表能导出**（`POST
  /api/prescription/export`），导出的是**建议稿**，不是可以直接执行的
  医嘱——必须经执业医师在表格里审核、修改并签发，系统不代替这道签发
  流程，每次导出都记入审计日志（见「定位」「审计」两节）
- 只覆盖脾胃门（呕吐、痞满、肿胀、痰饮等相关证候），不是全科辨证系统
- 安全拦截的关键词表是最小 demo 范围（见 `core/safety.py`），**没有经过医学生
  审核**，可能漏报（关键词没覆盖到的危重表述）或误报（子串命中过宽）
- 样本量仅各 30 案（共 60 案），不足以支撑统计结论，仅够验证系统管线；带复诊
  序列的案叶天士 30/30、吴鞠通 28/30（regex 信号意义上，详见 `data/SOURCES.md`），
  离原研究方案 ≥50 例的门槛还有距离——而且这 60 案目前还没有真正跑通抽取管线
  （见下方「快速开始」第 3 步，这是新装这个 repo 时最容易卡住的地方）
- 数据模型已支持多诊次序列（`case_group_id`/`visit_index`/`prev_case_id`），
  但**纵向证型转移预测本身还没做**——目前每次辨证仍是单次快照式的
- 分歧主指标是药物集合的 Jaccard 距离与治法是否一致；证型名精确字符串比对只是附注
  （实测 10 条主诉 9/9 全判为分歧，没有区分度）。**Jaccard 这个数目前没有对照基准**
  ——噪声地板 ε（同一主诉重复跑出来的分歧）要在有真实模型的机器上估，估出来之前
  它只说明"两位用药有多不重合"，不说明"这个不重合是否显著"
- 两位医家分处清初（叶天士）与清中（吴鞠通），时代是混杂因素，观测到的
  差异中含时代成分，不能直接等同于个人风格差异
- **这台开发/沙箱环境本身连不上 huggingface hub**（跟连不上 DeepSeek API 是
  同一类网络限制），`dense`/`hybrid` 检索模式需要的 embedding 模型下载不了，
  这两个模式的真实语义相似度数字这台环境产不出来，代码路径靠受控假向量
  验证过；`bm25`/`graph` 两个模式不需要网络，已经在这台环境里 100% 真实
  验证过（见 `data/SOURCES.md` 第 19、21 条）
- V1 评测的具体指标（分歧度 vs ε、幻觉率、安全否决代价、检索模式对比）是
  按现有代码已经产出的信号重新设计的一套，不是某个外部规范文档里的原始
  编号——如果你手上有那份原始定义，以它为准核对调整（`data/SOURCES.md`
  第 23 条）

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
| `LLM_MODE` 取 `claude_cli` | 走本机 `claude` CLI，**仅用于没有 API 网络时的冒烟**：模型不是 deepseek-chat、单次约 $0.06（DeepSeek 约 $0.0007），跑出来的分数不可与他人比较。`manifest.comparability_warning` 会把这一点一路带进报告 |
| `LLM_TIMEOUT` | 单次 API 调用超时秒数，默认 120（SDK 自带重试已关，重试统一由 `generate()` 负责） |
| `LLM_MAX_TOKENS` | 单次输出上限，默认 8192（DeepSeek 默认 4096，S0 抽多病人粗段会被截断） |
| `CLAUDE_CLI_MODEL` / `CLAUDE_CLI_TIMEOUT` | `claude_cli` 模式下的模型名与超时，默认 `claude-sonnet-5` / 180 |
| `USE_REACT` | `1` 打开 ReAct 取证（默认关，见「ReAct 取证模式」一节） |
| `FAST_MODE` | `1` 同时降级三处：追问 0 轮、ReAct 步数上限降到 2、残差辨证整体关闭（默认关，见「追问」一节）。实测一次完整问诊 14 次调用 → 8 次 |
| `EVAL_MODE` | `1` **只**让安全否决不中止链路（检查照跑、命中原因照记进 `safety_flag`），给评测量化"安全否决花了多少分"用。默认关，demo 的拦截红线不受影响；不要在对外演示的机器上打开——开着时页面顶部会有一条红色横幅提示，结果不会静默照常显示 |
| `WARMUP_TIMEOUT_SECONDS` | 启动预热（加载 embedding 模型 + 编码语料）最多等这么久，默认 120；超过就先开始服务，预热在后台继续、首个问诊会等它。连不上 huggingface 的机器预热会卡在下载重试上，不设上限的话服务一分多钟都不监听端口 |
| `MAX_CONCURRENT_CONSULTS` | 同时进行的问诊数上限（`/api/consult` 与 `/api/consult/stream` 合计），默认 4。满了立刻 503 + `Retry-After: 10`，不排队。这是部署侧的进程级设置，跟逐请求的 `retriever_mode` 不是一回事 |

> **检索模式不是环境变量。** `RETRIEVER_MODE` 仍然存在（离线脚本/单机评测用），
> 但 HTTP 请求要切模式请用请求体里的 `retriever_mode` 字段——环境变量是进程级的，
> 两个并发请求各选一种模式会互相污染。详见「检索：三路融合」一节。

### 第 3 步：生成 `cases.json`（医案结构化数据）

**叶天士、吴鞠通的粗段已经随仓库提交**（`data/ye_tianshi/` 32 个、
`data/wu_jutong/` 25 个 `.json`），所以这两位可以**直接跳到第 3.4 步**跑抽取。
下面的 3.1–3.3 只在两种情况下需要做：想加张锡纯（第三本书 584 没有随仓库分发），
或者想改切分口径重切。

> R1 阶段那 30+30 个候选案 `.txt` 已经不在这两个目录里了：吴鞠通那 30 个归档到
> `data/_archive_r1_txt/`，叶天士那 30 个已删除（内容可由 3.1+3.2 从原书复现）。
> 当前抽取脚本只认 `.json` 粗段。

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
mkdir -p data/zhang_xichun && cp out/zhang_xichun/*.json data/zhang_xichun/   # 下载了 584 才有
```

`data/{physician}/` 下现在只有 `.json` 粗段（R1 的 `.txt` 已归档到 `data/_archive_r1_txt/`），
下一步的抽取脚本只认 `.json`。

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
  retrieval.py / retrieval_hybrid.py / retrieval_graph.py   稠密/BM25/证素三路检索（见下方"检索：三路融合"一节）
  transition.py 证素轨迹（trajectory-only，见下方"证素轨迹"一节）
offline/        离线脚本：医案切分/抽取、SFT 样本导出、图谱构建、三元组抽取、证素索引、配额审计
api/            FastAPI 服务（见下方「HTTP 接口」一节）
web/            前端单页 index.html，无构建步骤，Cytoscape.js 走 CDN；两个页签：问诊 / 图谱浏览器
prompts/v1/     版本化 prompt 模板（yaml，$var 占位符）
data/           医案原文（data/ye_tianshi/、data/wu_jutong/）、证候标准数据、SOURCES.md 版权说明
eval/           需要真实 LLM 的评测：patient_sim、sdt/（外部 TCMEval-SDT 适配器）、
                run_eval.py + mcnemar.py（V1 评测汇总）、mes/（盲评导出/收集），不进 pytest 自动跑
tests/          pytest 用例（全部不需要网络）+ tests/queries.txt 测试主诉
CLAUDE.md       项目架构与代码约定，改代码前建议先读
run.sh          一键运行脚本
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/consult` | 跑完整条推理链，一次性返回结果 + 图数据。请求体 `{complaint, retriever_mode?}` |
| `POST` | `/api/consult/stream` | 同一条链路的 **SSE 分步进度版**。请求体一样，响应是 `text/event-stream` |
| `POST` | `/api/consult/stream/{stream_id}/answer` | 回答流里 `need_input` 事件问出的追问，请求体 `{answer}` |
| `GET` | `/api/graph` | 持久知识图谱（`data/graph.json` 的国标结构层），图谱浏览器页签用 |
| `GET` | `/api/trajectories/{physician}` | 某位医家名下带复诊序列的病人证素轨迹 |
| `GET` | `/health` | 存活探针（`async def`，不进线程池，问诊把线程池占满时它也能答） |

**请求上限与错误契约**（企业化整改一轮加的硬约束）：

- `complaint` 1–2000 字、`answer` ≤ 500 字，超出 422，一次 LLM 调用都不花（`api/main.py`
  的 `MAX_COMPLAINT_CHARS` / `MAX_ANSWER_CHARS`）
- 同时进行的问诊超过 `MAX_CONCURRENT_CONSULTS` → 503 + `Retry-After`
- 后台线程里的未预期异常，客户端只拿到异常类型和一个错误编号（SSE 的 `error` 事件、
  或 500），完整异常连同编号打在服务端 stderr；`retrieval_error` 和 503 的 `detail`
  里不再带项目的绝对路径。模式名写错那类用户侧错误（400 / `error` 事件）文案原样给
- `/answer` 只在**确实有一个问题在等回答**时才接（404 否则）：还没提问、已超时、已答过
  都不收，上一问超时后迟到的答案不会漏给下一问
- 客户端断开 SSE 连接后，后台线程在下一次回调就停下，不再花 LLM 调用

**两条 consult 路径共用同一个序列化函数**（`api/main.py::_consult_response`），
所以流式端点 `done` 事件的 data 跟非流式端点的响应体逐字段相同，前端一份渲染
逻辑接两条路。

### SSE 事件与追问

`/api/consult/stream` 先发一条 `stream_id` 事件，然后按推理链的自然边界依次发
`s1_done` / `s2_done` / `followup_done` / `residual_done` / `physician_start` /
`react_step` / `s3_start` / `physician_done`，最后一条是 `done`（载荷即完整结果）
或 `error`。

追问走 `need_input` 事件：**流会真的停在那里等**（服务端那根线程阻塞在答案队列上，
HTTP 连接一直开着），客户端拿 `stream_id` POST 到 `/answer` 端点，线程解除阻塞、
流继续往下走。没人回答时按 `ANSWER_TIMEOUT_SECONDS`（默认 300 秒）超时，
按"提问方不打算回答"处理，不是报错。这是旧接口做不到的事——`/api/consult`
从不传提问渠道，追问问出的问题从来没人接。

```bash
# 看真实事件流
curl -N -X POST http://127.0.0.1:8000/api/consult/stream \
  -H "Content-Type: application/json" \
  -d '{"complaint": "胃脘胀痛，食后加重，嗳气泛酸"}'
```

## 前端页面

无构建步骤，一个 `web/index.html`，两个页签：

**问诊页**——输入主诉、选检索模式、点「辨证」。走的是 SSE 端点，所以推理期间
能看见分步进度（症状标准化 → 证素推断 → 追问 → 每位医家取证/开方），不再是
一句静态的"请耐心等待"；系统要追问时页面上直接弹输入框，答完流继续往下走。
结果出来后是六层生长图（症状→证素→病名·证型→方剂→药材，方剂/药材是
compound 父子节点）+ 两位医家的结论对照 + 分歧度。

**图谱浏览器页**——浏览持久知识图谱（`data/graph.json` 的国标结构层）。初始只
铺 17 个证型节点，点开才逐步展开它连着的证素/症状（123 个节点一次性铺开是一团
乱线）；支持按名字搜索、按医家切换 λ1 权重（边的透明度按 λ1 编码）。
类目证候用菱形节点区分。**页面顶部那段 λ1 说明不是装饰**：它是
`offline/graph_stats.py::lambda1_note()` 按当前这张图的实际内容算出来的，
两种成因（图里根本没挂医案 / 挂了但医案证型跟国标术语对不上）说的是不同的话，
前端原样显示、不改写。医案层为空时「国标层/医案层」切换按钮直接不显示，
而不是显示一个点了没反应的。

两张图的节点和边都能 hover 出速览（症状的解释状态、证素的病位/病性、证型的
定义与舌脉、用药所属医家、边的主症/次症与 λ1）。图上药名只显示药名本身，
剂量不进节点标签（"党参三钱"显示成"党参"），但节点 id 保留原始写法——
侧栏证据链靠它反查。

## 离线脚本一览

| 脚本 | 作用 |
|---|---|
| `offline/split_cases.py` | 见「快速开始」第 3.2 步。`--stats-only` 是零 LLM 调用的正则前置闸门 |
| `offline/extract_cases.py` | 见「快速开始」第 3.4 步 |
| `offline/export_sft.py` | 从 `cases.json` 派生 alpaca 格式的 SFT 训练样本 `sft.jsonl`（`python -m offline.export_sft`）。现在数据量不够训练，这一步只是把管道建好，并在代码层面强制过滤掉 `copyright_status == "copyrighted"` 的记录 |
| `offline/build_graph.py` | 从 `data/standard/syndromes.jsonl` 建知识图谱骨架（symptom/element/syndrome 三类节点，`python -m offline.build_graph`），并打印语料库门类覆盖检查 |
| `offline/graph_stats.py` | 给图里的 indicates 边算并写回医家级四层收缩权重，打印节点/边分布、λ1 分布等统计（`python -m offline.graph_stats`）——**λ 相关的数字务必看下面"知识图谱权重"一节的 λ2 说明再解读** |
| `offline/build_jieba_dict.py` | K3a：生成 BM25 检索用的中医术语自定义词典 `data/jieba_dict.txt` |
| `offline/estimate_epsilon.py` | E：估计噪声地板 ε（`epsilon_online`/`epsilon_s2`/`epsilon_extract`），写 `eval/epsilon.json`，供前端"分歧度"和 V1 的显著性判断做对照基准 |
| `offline/extract_case_triples.py` | X3：从每一诊原文用真实 LLM 抽取三元组（`{case_id,physician,s,p,o,source_span}`），写 `data/case_triples.jsonl`，`core/tools.py` 的 `query_case_graph` 工具消费这份数据 |
| `offline/build_element_index.py` | K3b：从 `cases.json` 的症状字段建证素索引 `data/element_index.json`，供检索的 `mode="graph"` 和 `core/transition.py` 用 |
| `offline/quota.py` | 附属：审计 `cases.json` 是否达到 `data/SOURCES.md` 里写明的样本量门槛（总案例 60、带复诊序列 50） |

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

另外，`λ1`（医家层权重）目前对所有边都是 0，权重全部退化到标准先验层。这不是
占位符，是真实计算结果，而且**有两种成因，别把它们混为一谈**（`graph_stats.py`
会按当前图的实际内容打印对应的那一种）：

- 本机没有 `cases.json` 时，图里根本没有 `case` 节点，`count_support()` 无边可数；
- **挂入真实医案之后 λ1 仍然接近 0**——实测 839 条医案里只有 91 条标了证型，
  这 91 条中 **0 条**能匹配国标证候名；症状端 1433 种表述与国标 93 个症状节点
  字面重合仅 8 种。这是清代医案与现代国标术语体系的差异，不是实现缺陷。

所以非零 λ1 **不会「接入数据就自动出现」**，要等一层术语映射。详见
`data/SOURCES.md` 第 8、10 条与 `HANDOFF.md`「已知问题」第 1 条。

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

## 检索：三路融合（K3a/K3b，进阶功能）

`core/retrieval.py` 的 `get_retriever()` 返回的是 `core/retrieval_hybrid.py`
的 `HybridRetriever`——`DenseRetriever`（稠密向量检索）的超集，另外叠加了
BM25 关键词检索（K3a）和证素路检索（K3b），用 Reciprocal Rank Fusion 融合。
`RETRIEVER_MODE` 环境变量或 `search(mode=...)` 参数选路，四种取值：

| mode | 依赖 | 说明 |
|---|---|---|
| `dense` | embedding 模型 | 语义相似度，原有行为不变 |
| `bm25` | `data/jieba_dict.txt`（可选，缺失时退化到 jieba 默认词典） | 关键词重合，不需要 embedding 模型 |
| `graph` | `data/element_index.json` + `query_elements` 参数 | 证素 Jaccard 相似度；不传 `query_elements` 直接报错，不静默退化成别的模式 |
| `hybrid`（默认） | 上面几路都可选 | 传了 `query_elements` 就三路融合，没传就退回 dense+bm25 两路，向后兼容 |

`min_score`（默认阈值 `MIN_RETRIEVAL_SCORE=0.70`）只作用于 dense 那一路——
bm25 的展示分是无界原始分，graph 的展示分是证素集合通常只有两三个元素时的
Jaccard 相似度，套用为稠密余弦相似度校准的阈值没有意义，也因此 `bm25`/
`graph`/`hybrid` 模式下离题主诉不会被过滤成空列表（这一条实测记在
`data/SOURCES.md` 第 19 条）。跑法：

```bash
python -m offline.build_jieba_dict          # K3a：BM25 分词词典
python -m offline.extract_case_triples      # X3：真实 LLM 抽取医案三元组（需要 cases.json）
python -m offline.build_element_index       # K3b：证素索引（需要 cases.json + data/graph.json）
RETRIEVER_MODE=hybrid ./run.sh              # 或 dense/bm25/graph
```

### 在线切模式：逐请求，不是环境变量

网页上的「检索模式」下拉框、以及 `POST /api/consult`（含 `/stream`）请求体里的
`retriever_mode` 字段，都是**逐请求**生效的：

```bash
curl -X POST http://127.0.0.1:8000/api/consult \
  -H "Content-Type: application/json" \
  -d '{"complaint": "胃脘胀痛，嗳气泛酸", "retriever_mode": "bm25"}'
```

**服务端不会因此去设 `RETRIEVER_MODE`。** 那个变量是进程级的，一个请求设了它，
同一进程里并发的另一个请求就跟着变了——这跟 `EVAL_MODE` 那个开关当初被做成
"显式参数优先、环境变量只作兜底"是同一条理由。`retriever_mode` 一路作为函数
参数传到检索层，任何时候都不写进程状态（`tests/test_retriever_mode.py` 里有一条
双线程并发测试专门钉这件事）。

两种失败分得很清楚：

- **模式名不认识** → 立刻 `400`，一次 LLM 调用都不花（校验发生在 S1 之前）。
- **模式认识、但这台机器上跑不起来**（`graph` 缺 `data/element_index.json`）
  → `200` + 响应体里的 `retrieval_error` 一句人话，**不静默降级到别的模式**。
  降级的话调用方会以为自己看到的是证素路的结果，E8 消融那组数字也就失去意义了。
  检索层照旧大声报错，只是不再让 500 裸奔到前端。

## 证素轨迹（附属，进阶功能）

`core/transition.py` 把同一病人（`case_group_id`）的复诊序列按 `visit_index`
排序，配上每一诊的证素状态（来自 K3b 的 `data/element_index.json`）。
**只做轨迹展示，不拟合转移概率模型**——demo 阶段样本量连
`offline/quota.py` 的门槛（带复诊序列 ≥50 例/医家）都够不上，此时拟合
转移核只会制造一个看着像结论、实际是噪声的数字。

```bash
GET /api/trajectories/{physician}   # 例如 /api/trajectories/ye_tianshi
```

未知医家返回 404；`cases.json`/`data/element_index.json` 还没生成返回 503
（不是 500——demo 环境没有真实数据是正常状态，不是系统故障）。

## 分歧度里的西药：单列，不混进 `herb_jaccard`

`CaseRecord` / `S3Syndrome` 都有 `western_drugs` 字段，跟 `herbs` 互斥。
张锡纯「衷中参西」的方子里会出现阿斯匹林、百布圣、金鸡纳霜这类西药，
混进 `herbs` 会把跨学派分歧系统性推高——而那个推高是假的，"叶天士没开
阿斯匹林"是学派记录体例的差异，不是辨证思路的差异。所以：

- `herb_jaccard`（分歧度主指标）只算中药；
- 西药单独报在 `divergence.western_drug_overlap`，两边都没开西药时是 `None`
  而不是 `0`——`0` 会被读成"两边西药完全一致"，实际是"这个维度不适用"；
- 拆分只有一处实现（`core.herbs.split_western_drugs`），S0 抽取和 S3 开方
  两个边界都调它。prompt 里也写了同样的要求，但 prompt 是约束不是保证，
  代码这层必须兜住。

词表是从 `books/584-医学衷中参西录.txt`（GB18030）真实文本里挖出来的，
实测推翻了两个想当然的写法（阿斯匹林 136 次 / 阿司匹林 0 次；百布圣 19 次 /
白布圣 0 次）。刻意不收单独的"盐酸/硫酸/碘"：原书 8 次"盐酸"里有 5 次是
「鸡内金……含有稀盐酸」这种成分描述，收进去会把真中药误判成西药，
而误判的代价（一味中药被剔出主指标）比漏判更糟。

> 张锡纯本人还没注册进 `core/physicians.py`（见 HANDOFF 步骤 3），
> 所以现在真实链路上这个字段恒为空，上面这套是为他进来那天准备的。

## 评测汇总（V1，需要真实 LLM，不进 pytest）

`eval/run_eval.py` 把 divergence（分歧度 vs ε 噪声地板）、幻觉率（按有无
参考医案分组）、安全否决率+代价、检索模式对比（自实现 McNemar 检验，见
`eval/mcnemar.py`）汇总成 `eval/report.json` / `eval/report.md`：

```bash
python -m eval.run_eval --queries-path tests/queries.txt
```

`eval/mes/`（盲评导出/收集）用于人工判断"这条辨证像不像话"这类自动指标
测不了的问题：`export.py` 把两位医家对同一条主诉的结果匿名成 A/B（隐去
`cited_case_ids` 以防暴露医家身份）导出评分表，人工填完 `winner` 之后
`collect.py` 换回身份、统计胜负、算 McNemar 显著性。

```bash
python -m eval.mes.export           # 导出 eval/mes/items.json + answer_key.json
# ……人工在 items.json 里给每条填 winner: "A"/"B"/"tie"……
python -m eval.mes.collect          # 统计胜负，写 eval/mes/collected.json
```

## 外部评测：TCMEval-SDT

`eval/sdt/` 是接入官方 [TCMEval-SDT](https://github.com) 评测集的适配器
（`adapter.py` 把 `core.chain` 的输出转成 SDT 要求的三段式格式，`score.py`
包一层官方计分脚本，`run.py` 是跑一整个 split 的入口），详见
[`eval/sdt/README.md`](eval/sdt/README.md)。跟 `eval/run_eval.py`（V1，
本项目自定义指标）是两套独立的评测：SDT 用外部数据集和外部计分标准，
V1 用本项目自己产出的信号（分歧度、幻觉率等）。

```bash
export SDT=<TCMEval-SDT 数据集路径>
python -m eval.sdt.run --sdt-dir $SDT --split Test --solver chain --out out/sdt_chain.txt
```

## 数据来源与版权

详见 [`data/SOURCES.md`](data/SOURCES.md)。简要结论：

- 叶天士《临证指南医案》30 案、吴鞠通《吴鞠通医案》30 案，均为公有领域（两位作者卒年
  远超著作权保护期），原文可自由使用
- 原文取自开源仓库 `xiaopangxia/TCM-Ancient-Books`，经「快速开始」第 3 步的切分/
  抽取流水线处理为完整诊次序列（保留复诊，不再只取初诊段）
- `tests/queries.txt` 的 10 条测试主诉为本项目合成，非真实病例，**使用前应先经医学生
  审核**，确认表述符合中医临床描述习惯
- 将来若接入现代出版的名老中医经验集，必须标 `copyright_status: copyrighted`，
  且只能抽取事实性三元组、不得让原文进前端展示或训练集——`export_sft.py` 的
  `filter_public_domain()` 会把它们排除在训练集之外，并把排除的条数和 id 打到 stderr

## 测试

```bash
pytest            # 856 条，秒级
ruff check .      # lint 基线在 ruff.toml；CI（.github/workflows/ci.yml）两条都跑
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
- `core/retrieval_hybrid.py` / `core/retrieval_graph.py`：RRF 融合公式、jieba 自定义
  词典生效、`min_score` 只作用于 dense 一路、`mode="graph"` 不传 `query_elements`
  报错不静默降级、`mode="hybrid"` 有/无证素两种融合路径
- `eval/mcnemar.py`：自实现的精确二项检验和连续性校正卡方近似，数值跟 scipy.stats
  逐用例核对过（scipy 只是开发期核对工具，不是项目运行时依赖）
- `eval/run_eval.py` / `eval/mes/`：分歧度/幻觉率/否决代价/检索模式对比四类指标、
  盲评导出隐去医家身份、盲评收集正确统计胜负

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

## 四种模式

`POST /api/consult`、`POST /api/consult/stream` 请求体都可以带 `role`
字段（`"patient" | "doctor" | "student" | "researcher"`，默认
`"researcher"`）。跟 `retriever_mode` 一样是**逐请求参数，不是进程级
设置**——同一个服务同时服务不同角色的请求，互不影响。

字段裁剪**在服务端做**，不是前端拿到完整数据再选择性隐藏——patient
角色拿到的响应体里 `formula_candidates` 这个键**根本不存在**，不是
存在但为空；前端过滤等于把完整处方发到客户端再藏起来，打开浏览器
devtools 照样能看到。

| 字段 | patient | doctor | student | researcher |
|---|---|---|---|---|
| disease / syndrome | ✅ | ✅ | ✅ | ✅ |
| triage（导诊） | ✅ | ✅ | — | — |
| formula_candidates | ❌ | ✅ | ✅ | ✅ |
| herb_items（含剂量） | ❌ | ✅ | ✅ | ✅ |
| 食疗 / 中成药 | ✅（M9 前恒为空列表） | ✅（M9 前恒为空列表） | — | — |
| reasoning | 通俗版 | 专业版 | 专业版 | 专业版 |
| react_trace（ReAct 取证轨迹） | ❌ | ❌ | ✅ | ✅ |
| refs（检索医案） | ❌ | ✅ | ✅ | ✅ |
| divergence（分歧度） | ❌ | ❌ | ✅ | ✅ |
| manifest（模型/prompt版本/耗时等技术元数据） | ❌ | ❌ | — | ✅ |
| safety 详情 | 简化（布尔摘要，不含药名） | ✅ | ✅ | ✅ |
| 六层图的方剂/药材层 | 不生成（不是生成后摘除） | 生成 | 生成 | 生成 |

`urgency=high`（红旗症状）时，patient/doctor 两种角色**不返回任何用药
相关字段**——食疗、中成药一并清空，这条闸门跟 `formula_candidates`
是否存在无关，独立生效，不因为将来接了真实食疗数据源就被绕过。

**patient**：只读，看得到证型/治法/导诊建议，看不到具体方药，`reasoning`
是模型跟专业版一起生成的通俗语言版本（不是事后翻译）。

**doctor**：完整信息（含具体方药），前端多一张**可编辑处方表**（见下面
「处方安全」「审计」两节），可以改方、校验、导出——是这四种角色里唯一
能产生"写入"这件事的模式。

**student**：完整信息，前端额外做了三处教学优化：药材按君臣佐使分组
显示（君药加粗）；点击任一症状节点高亮它到所有候选方的完整四步链路
（症状→证素→病名证型→方剂，不下探到药材层——层3方剂是 compound
父节点，框亮了药材自然看得见）；医家卡片的推理详情/ReAct 取证过程
默认展开（researcher 模式默认折叠）。

**researcher**（默认）：跟改造前的行为逐字节一致，包括 `manifest` 这类
只对开发/评测有意义的技术元数据。

## 处方安全

`core/safety_output.py::assess_formula_safety(syndrome, herb_items)`
是候选方安全检查**唯一的组装点**——五条规则都在这一个函数里跑，不是
散在各处各查各的：

| 规则 | 判据来源 | 分级 |
|---|---|---|
| 十八反十九畏配伍禁忌 | `check_incompatible` | 拦截级 |
| 剂量超出常用上限 | `check_dose_limits`（`DOSE_LIMITS` 表） | 拦截级 |
| 缺必要煎法（先煎/后下等） | `check_required_decoction`（`REQUIRED_DECOCTION` 表） | 警告级 |
| 含毒性/大毒药材 | `check_toxic_herbs` | 警告级 |
| 证型寒热方向与主方药性相悖 | `check_thermal_consistency` | 警告级 |

**拦截级**（`FormulaSafety.blocking`，即"配伍禁忌"或"剂量超限"任一命中）
会让 `core/chain.py` 里的 `run_physician` 重开一次方；**警告级**只提示
不打回——寒热错杂证本来就寒热并用、毒性药材的常规用量本就贴着上限、
漏标煎法不代表方子本身有问题，这三类逼模型重开只会把对的方子改坏。

`DOSE_LIMITS`/`REQUIRED_DECOCTION`/毒性分层的数据来源方法论（WebSearch
核实、非逐字核对原文 PDF 这层局限）如实记在 `core/safety_output.py`
文件开头的文档字符串里，不重复贴一份到这里。

`POST /api/prescription/validate`：纯规则、不调 LLM、毫秒级返回，独立于
`/api/consult`——医生可能想校验一张手写/临时改动的方，不依赖任何问诊
上下文。

```
POST /api/prescription/validate
body: {herb_items: HerbItem[], syndrome: string, disease?: string}
→ FormulaSafety 的字段 + 一个额外的 blocking 布尔（FormulaSafety.blocking
  是 pydantic 的 @property，model_dump() 不带计算属性，接口这里手动补上，
  避免调用方各自重新判断一遍"incompatible 或 dose_violations 非空就是
  blocking"）
```

## 审计

医生导出处方时（`POST /api/prescription/export`），无论方子有没有问题
都会追加一条记录到 `data/audit.jsonl`（哈希链，不进版本控制——`.gitignore`
的 `*.jsonl` 整体忽略规则已经会挡住它，运行时生成物不需要额外例外）。

**为什么不用 SQLite**：审计日志的核心属性是防篡改。SQLite 默认给不了——
任何人打开 db 文件改一行，改完看不出来。哈希链每条记录带前一条的哈希，
改中间任何一条，后面全部对不上，一条命令就能校验出来。而且零依赖、
纯文本可读，AutoDL 上就是普通文件写入，现在就能跑。

`core/audit.py::AuditRecord` 字段：

```python
seq: int                 # 严格递增，从 1 开始
timestamp: str            # ISO8601 UTC
doctor_id: str
patient_ref: str | None
model_suggestion: dict    # 模型当初给的候选方（FormulaCandidate.model_dump()）
final: dict                # 医生最终定的方
diffs: list[str]          # 逐味药的人类可读差异，见下方例子
safety_at_export: dict    # 导出那一刻服务端重新算出的 FormulaSafety（含 blocking）
override_reason: str | None  # blocking 为真时医生坚持导出必须填的理由
prev_hash: str             # 前一条记录的 sha256，第一条是 64 个 0
hash: str                  # 本条记录（除 hash 外全部字段）的 sha256
```

`diffs` 按药名配对比较 `model_suggestion` 和 `final`（不按列表下标——
医生中途插入/删除一味药会让下标错位），五类描述格式：

```
去 甘草                 # 原方有、定方没有
加 海藻 15g              # 原方没有、定方有
附子 10g→15g             # 剂量变了
半夏 炮制 生→姜制         # 炮制变了
附子 煎法 无→先煎         # 煎法变了
```

**安全判定服务端权威重算，不信任客户端传来的值**：`/api/prescription/export`
收到的 `formula.safety`（如果客户端带了）完全不作数，服务端用
`assess_formula_safety("", formula.herb_items)` 重新算一遍——跟 X2
输出侧安全检查"不能让模型自己说安全"是同一条原则的延伸，这里是"不能让
客户端自己说安全"（前端改一个布尔值不能绕过拦截）。`blocking` 为真且
没有非空 `override_reason` 时拒绝导出（422，列出具体问题）；医生传了
非空理由就放行，这条理由连同完整的 `safety_at_export` 一起写进审计
记录——它是"医生明知有问题仍坚持导出"唯一的书面记录。

并发写入用 `fcntl.flock(LOCK_EX)` 把"读最后一行取 prev_hash、算本条
hash、追加写入"整段包进临界区，不能只锁写入那一步——两个医生同时导出，
如果只锁写入，两边都可能在锁外先读到同一个 prev_hash，各自算出的哈希
都"合法"，但链会在这里分叉。

命令行手动校验一条链：

```bash
python -c "
from core.audit import verify_audit_chain
ok, problems = verify_audit_chain()
print('完整' if ok else '不完整', problems)
"
```

`verify_audit_chain` 分三个维度分别报问题（不是笼统一句"第 N 条有
问题"）：hash 对不上（内容被改但没跟着重算哈希）、seq 不连续（丢了一条
或插了一条不该在的）、prev_hash 跟上一条记录实际的 hash 对不上（链被
剪断重接）——比较基准用文件里实际写的 hash，一条记录的问题只在它自己
身上报一次，不会连累后面所有记录都被误判。

```
POST /api/prescription/export
body: {formula: FormulaCandidate, doctor_id: string, patient_ref?: string,
       model_suggestion: FormulaCandidate, override_reason?: string}
→ 成功 {text: "药房格式文本", audit_id: "<seq>"}
→ 422（blocking 且无 override_reason）{detail: {message, problems, safety}}
```

药房格式文本样例（`core/prescription.py::format_pharmacy_text`，中文
药名按东亚宽字符显示宽度对齐，不是按字符数）：

```
瓜蒌薤白半夏汤加减                    7 剂
  瓜蒌      15g
  薤白       9g
  半夏       9g   姜制  先煎
用法：水煎服，每日1剂，分2次温服
```

## 定位

> 患者 / 学生 / 研究者模式：教学与研究用途，非诊断工具，不能替代执业医师。
> 医生模式：处方辅助工具。系统提供的方剂与剂量为建议，最终处方由执业医师
> 审核、修改并签发，医师承担全部临床责任。所有导出操作均记录审计日志。

跟文件顶部那句一字不差——这段文案**只有一处真正的来源**：
`web/index.html::describeDisclaimer()`。前端有两个地方显示它，都是调用
这一个函数、不是各自硬编码一份：页头的免责声明横幅（角色一切换就跟着
换文案）、医生模式可编辑处方表顶部的紫色提示条。README 这里和 CLAUDE.md
如果要跟着改，改的应该是 `describeDisclaimer()` 那一处，然后把新文案
转录到这几个地方——不能先改 README、前端却没跟上（M8 报告里点名过的
风险：审计/教学/研究这类免责表述，一处改一处忘，比代码逻辑不一致更容易
被用户直接看到、也更容易造成误解）。
