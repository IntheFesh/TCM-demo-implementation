# 名医辨证对照 Demo

输入患者症状，系统分别模拟三位古代名医——**叶天士**（`ye_tianshi`，《临证指南医案》，
1667–1746，温病）、**吴鞠通**（`wu_jutong`，《吴鞠通医案》，1758–1836，温病）、
**张锡纯**（`zhang_xichun`，《医学衷中参西录》，1860–1933，衷中参西）——的辨证思路，
各自给出证型、治法、方药，并把结果并置对比、标出分歧。**每条结论都必须引用它所依据的
真实医案 id**，用于防幻觉核查。

> 患者 / 学生 / 研究者模式：教学与研究用途，非诊断工具，不能替代执业医师。
> 医生模式：处方辅助工具。系统提供的方剂与剂量为建议，最终处方由执业医师
> 审核、修改并签发，医师承担全部临床责任。所有导出操作均记录审计日志。

**这份 README 里的评测数字全部可核**，不是手抄的：

```bash
python -m scripts.collect_results --check    # 核对 README.md 和 eval/RESULTS.md；不一致退出码 1
```

带 `` `文件名:键=值` `` 记号的数字由 `--check` 逐个去 report 文件里取真值比对；
没有记号的数字要么标着「⏳ 还没跑过」，要么在正文里说明它为什么核不了。
各组数的当前值、对照、caveat 和凭据只维护在 [`eval/RESULTS.md`](eval/RESULTS.md) 一处。

## 六块速览

（下表只给一句话和去处，**数字的凭据记号在各自那一节**，不在这里重复。）

| 块 | 一句话 | 详细 |
|---|---|---|
| 系统构成 | 五层：知识图谱 / RAG / SRC 推理链 / ReAct / 安全双闸 | 下一节 |
| 外部基准 | SDT Test：chain 23.173 vs baseline 22.068，关安全否决 27.729 | 「外部基准」一节 |
| 四种模式 | patient / doctor / student / researcher。**patient 拿不到 `formula_candidates`——那是安全边界，不是功能裁剪**，裁剪在服务端做（键根本不存在，不是存在但为空） | 「四种模式」一节 |
| 三种后端 | api（开发评测）/ local（自部署）/ replay（演示） | 「三种后端」一节 |
| 已知混杂与局限 | 12 条，一条都不省 | 「已知混杂与局限」一节 |
| 如何复现 | 数据管线的命令序列，含 `graph_stats` 那个坑 | 「如何复现」一节 |

## 系统构成（五层）

| # | 层 | 它解决什么问题 | 规模（可核） |
|---|---|---|---|
| 1 | **知识图谱** | 给"证型—证素—方剂—药材"一个可查询的结构，而不是让模型凭记忆生成 | 国标层 **337** 条标准证候（`data/standard/syndromes.jsonl`，320 条教材 + 17 条国标/共识）；医案层 **34211** 条三元组（带 `source_span`）；药理层待抽（引擎已就绪，见「药理层数据源」） |
| 2 | **RAG 检索** | 把结论锚到真实医案上，让"引用了哪一条"可核 | 四路可切换：`dense`（语义）/ `bm25`（关键词）/ `graph`（证素结构）/ `hybrid`（三路融合），**逐请求切换**不是进程级设置 |
| 3 | **SRC 推理链** | 把"症状→方药"这一步拆开，每一步都能单独看对不对 | S1 症状标准化 → S2 证素推断（病位 10 / 病性 12 词表）→ S3 按医家开方 |
| 4 | **ReAct 智能体** | 让模型自己去查证据，而不是一次性生成 | 七件工具（六件取证 + `ask_user` 追问），**医案层优先**（工具描述只在 `core/tools.py` 一处，有扫描测试钉住） |
| 5 | **安全双闸** | 危重症状必须在辨证之前拦住，处方必须在输出之后校验 | 输入侧：18 个危重关键词 + 11 条上下文模式，**在 S2 之前**；输出侧：24 组十八反十九畏配伍 + **62** 味剂量上限 + **39** 味必要煎法 |

> ⚠ 「国标层 207 条证候」这个数在本仓库里核不出来：`data/standard/syndromes.jsonl`
> 实测是 **337** 条（`wc -l` 可复现）。207 大概是 `data/graph.json` 里国标层的节点数，
> 而那个文件是生成物、不入版本控制。要引用 207 就得先把它变成可核的数。

**能做的事**（每条都对应上面某一层）：

- 输入一段中文主诉 → 自动标准化为症状/舌象/脉象（S1）
- 危重症状在证素推断**之前**被硬性拦截，直接拒绝辨证、不产出任何方药——不是事后在备注里提醒转诊
- 分别推断三位医家风格下的证素、证型、治法、方药（S2/S3）
- 检索并展示每条结论所依据的真实医案（id + 相似度），可展开核查
- 检测并标红"幻觉引用"——模型引用了检索结果之外的医案 id
- Cytoscape 六层图：症状 → 证素 → 病名·证型 → 方剂 → 药材（方剂/药材是 compound 父子节点，
  同一位医家的 2–3 个候选方各自带自己的药；**症状不直接连方剂**——完整链路才是推理，直连只是共现匹配）
- 分歧度 = 用药集合的 Jaccard 距离（分整方/君臣/佐使三层）+ 治法是否一致，
  **并对照逐条配对的噪声地板 ε** 判断这个分歧是真的还是重复采样的抖动
- 同一病人的复诊序列可以按证素状态摆成轨迹（`core/transition.py`），**不做转移概率预测**（样本量不够）
- 用 McNemar 检验判断两种配置之间的差异是否统计显著，不靠肉眼比大小

## 外部基准：TCMEval-SDT

唯一一个**外部、可跟别人比**的分数。三个数并列，一个都不能省：

- **chain 23.173 / 50**（注入证素分析）`sdt/test_run_log.jsonl:sdt.chain_last=23.173`
- **baseline 22.068**（同模型、同三个输出头、**不**注入）`sdt/test_run_log.jsonl:sdt.baseline=22.068`
- **关掉安全否决 27.729** `sdt/test_run_log.jsonl:sdt.ignore_safety_veto=27.729`

`chain − baseline = +1.105` 才是这条结构化推理链的贡献；只报「我们拿了 23.173」没有基准。
第三个数是**安全否决的代价**（差 4.56 分），它不是缺陷：

**被拦的 10/50 条经人工逐条复核，全部为真危重，无一误伤**——蛛网膜下腔出血、
烧碱灼伤食管吐血 150ml、颅脑外伤昏迷、3 岁患儿高热 40℃、乙脑后遗症昏迷抽搐等。
「无一误伤」这四个字的来源是**人工把 10 条原文逐条读过**，不是代码判定的、也不是声称的；
10/50 这个数可以用官方 `Test_TCM_Data_v1.json` 过 `core.safety.check_safety` 现算复现
（`data/SOURCES.md` 第 14 条；早期估算写的 8/50 是安全层词表扩充前的旧数，已作废）。
⏳ 那份人工复核清单本身没有进版本控制，所以它在 `--check` 里核不了——引用时要带上这句。

**Test 集只在最终定型后跑**：已跑过 3 次完整 + 1 次局部（台账
`eval/sdt/test_run_log.jsonl`，每次自动追加）。prompt 改动先在 Train（200 条）上验证方向，
Validation 做中间验证（满分上限 **48.9998/50**，官方金标准带 BOM），细节见
[`eval/sdt/README.md`](eval/sdt/README.md)。数据怎么拿、提交文件什么格式见下面
「TCMEval-SDT 的数据获取与接法（细节）」一节。

## 三种后端

| 后端 | 用途 | 启用 | 怎么验证接上了 | 数字可不可比 |
|---|---|---|---|---|
| `api` | 开发、评测（现在所有数都是它跑的） | `LLM_MODE=api` + `.env` 里的 DeepSeek key | `curl /health` 返回 `demo_mode: null`；报告 `manifest.backend == "api"` | **基准口径**，RESULTS.md 里的数都是它 |
| `local` | 自部署、训练后评测 | `LLM_MODE=local` + vLLM server（`scripts/start_vllm.sh`） | `python -m scripts.verify_local_backend`（会把实际生效的 `guided_json` 键打出来） | **不可与 api 直接比**，`manifest.comparability_warning` 会一路带进报告；重跑要按 RESULTS.md「并列，不覆盖」加行 |
| `replay` | 竞赛演示、零成本、断网可用 | `LLM_MODE=replay`（需先 `python -m scripts.record_fixtures` 录一次） | `python -m scripts.verify_replay` 退出码 0 = 回放跟录制逐字节一致；`/health` 的 `demo_mode` 非 null | **不是实时调用**，`manifest.replayed_from` 非 None，前端顶部有「演示模式」小字，报告顶部写明 |

三种后端的 `model_name()` / `backend_id()` 各自说实话，**不许伪装成别的模型**——
报告里的后端标签从 `manifest` 抬上来，不读 `LLM_MODEL` 环境变量（那个变量在
`claude_cli` / `replay` 后端下还是 `deepseek-chat`）。

## 超时与进度（R9）

**一次 LLM 调用挂住不返回，重试逻辑永远不会触发。** R8 段 6 录制在真机上卡了
46 分钟（`wchan=do_poll`、socket 还在、最后一份 fixture 写于 46 分钟前），而客户端
**已经**设了 `timeout=120`——问题不是"没设超时"，是 **httpx 的 read 超时管的是
单次 socket 读，不是整个响应的期限**：中间任何一跳（CDN / 网关 / 反代）每隔几十秒
吐一个字节，每次读都不超时，请求可以挂到天荒地老。`MAX_ATTEMPTS=3` 的前提是
"这次调用返回了（成功或失败）"，挂住时它根本没机会跑。

所以超时是两层（`core/llm.py` 的 `CallTimeouts`）：

| 层 | 值（云端 API） | 管什么 |
|---|---|---|
| connect / read / write / pool **分别设** | 15 / 120 / 30 / 15 秒 | 各相位自己的上限——连接慢到 15 秒以上一定是网络坏了，等 120 秒没有意义 |
| **墙钟兜底** | 180 秒 | 整次调用的期限。超了抛 `LLMCallTimeout`（继承 `TimeoutError`）→ 走既有重试 → 三次都超时才 `LLMError` |

值的依据：单次 S3 调用实测 6~8 秒、ReAct 单步 2~3 秒、药理层抽一块 10~20 秒、
S0 抽多病人粗段最慢 60 秒量级。120 秒远超正常值、远低于"挂死"。

**按后端区分**：本地 vLLM server 600/900 秒（首个请求要等预热），进程内 vLLM
1800 秒（第一次调用在进程内加载权重，几分钟是正常的）。`LLM_TIMEOUT_SECONDS`
覆盖所有后端（旧名 `LLM_TIMEOUT` 仍然认）。

墙钟兜底的实现是"工作线程 + `join(deadline)`"：blocking 的 socket 读没法从外面
取消，所以超时后不等它（daemon 线程），只把它丢在后台自己去死，并顺手断掉连接池
（`abort_in_flight()`）让重试不排在同一个坏连接后面。**顺带解决"杀不掉"**：主线程
这会儿卡在 `join()` 而不是 C 层的 read 里，Ctrl-C 立刻生效。

**进度条**（`core/progress.py`，零第三方依赖）：所有长任务统一用它，打 **stderr**
（stdout 可能是结构化输出）。TTY 下 `\r` 原地刷新，非 TTY（`| tee`、`nohup`、CI）
每 15 秒或每 N 项打一整行。**一项都没完成也有心跳**（默认 30 秒），由后台守护线程
负责——主线程正卡在那次调用里，它自己没机会打。所以从 R9 起，**"静默"不再等于
"正常"**：超过心跳间隔还没有下一行就是真卡住了（`docs/onsite_troubleshooting.md`
第 0 条据此改写）。

## 已知混杂与局限

**12 条，一条都不省。** 这是这个项目最有价值的部分之一：一个说不清自己局限的
demo，它报的每个数都不可信。

1. **师承关系与时代学派共线，分不开。** 叶天士、吴鞠通同为温病派，张锡纯衷中参西；
   但「跨学派」同时混着时代差——生年相差 91 年（叶→吴）、193 年（叶→张）、102 年（吴→张）。
   观测到的差异里含时代成分，**不能直接等同于个人风格差异**。
2. **术语体系不对齐，λ1 恒 0。** 证型 **0/116** 可对齐国标，症状字面重合 **8/1433**。
   ⏳ 这是 **839 条**语料上的测量，**现在是 941 诊次，没有重测**——引用这个数必须带这句。
3. **`element_index` 覆盖率 47%（444/941）**，`graph` 检索模式因此受限：覆盖不到的医案
   证素集合为空、相似度恒 0，系统性排在检索结果之后。所以 graph 模式表现差
   **优先怀疑覆盖率，不要断言「知识图谱检索不如向量检索」**。
4. **幸存者偏差**：写进医案并流传下来的，多是治好的那些。系统学到的是"成功案例的用药分布"。
5. **残差辨证极少触发**（证素表覆盖面好，`check_residual` 基本查不出遗漏）——
   这条链路验证过能跑，但真实数据上几乎用不上。
6. **幻觉率恒 0 是设计使然，不构成「无幻觉」的证据。**
   实测：27 条引用里 0 条幻觉 `report_e3.json:hallucination.n=27` `report_e3.json:hallucination.n_hallucinated=0`
   但 prompt 里只给 3 个候选 id，模型几乎不可能引到别的；这个数说明的是"约束生效了"，
   不是"模型不会编"。
7. **安全层是关键词粗筛，未经医学生审核**（18 个关键词 + 11 条上下文模式，见 `core/safety.py`），
   可能漏报（表述没覆盖到）或误报（子串命中过宽）。
8. **李可医案含十八反配伍（海藻甘草同用），训练时排除**——否则教模型开反药，而输出侧
   `check_incompatible` 又会拦住它，训练出来的模型在自己的安全层面前跑不通。排除**只发生在
   训练导出那一环**（`offline/export_sft.py` 默认排除，`--include-incompatible` 才带上），
   **医案本身不删**。
9. **`query_case_graph` 的症状词不匹配**：21 次调用里只有 8 次非空。根因是医案三元组按**原文
   术语**抽取，而模型按**患者主诉的词**去查（「口苦」查不到、同模块的 `check_residual` 却能匹配上）。
   已统一走 `_match_graph_symptoms`，但覆盖面仍受原文术语限制。
10. **ε 按证型分层**：逐条实测 0.0 ~ 0.6742，
    全局均值 **0.2611** `epsilon.json:epsilon_online.mean=0.2611`。所以分歧指标**必须逐条配对
    比较**，拿全局均值一刀切会在 5 条主诉上漏判、4 条上误判（推导见 RESULTS.md「ε 按证型分层」）。
    ⚠ 分层现象稳定，但**逐条数值本身在重跑之间会大幅变化**（痰饮 0.0 → 0.2278、
    肝胃气滞 0.5099 → 0.3954），所以 ε 必须跟被它卡的指标同一轮次跑出来。
11. **MES 盲评未做**，缺一个不依赖自动指标的维度。⏳ 它要人评分，没有任何脚本能替它产出，
    也是训练（阶段五）三个前提里唯一没满足的那个。
12. **三位医家的数据量极不均衡。** 张锡纯只有 87 条医案、450 条三元组，
    而叶天士有 29247 条三元组，他那一路的检索质量因此更差。
    ⚠ **但语料量少不等于 ε 更高**：最新一轮按医家分层是 ye **0.3495** /
    wu 0.2092 / zhang 0.2247，叶天士反而最不稳定。上一轮（ye 0.2009 / wu 0.1972 /
    zhang 0.3247）曾把两件事绑在一句话里说，那个关联没有重现，已拆开。
    ⏳ 87 / 450 / 29247 这三个数来自 AutoDL 上的抽取统计，`cases.json` 和
    `case_triples.jsonl` 都不入版本控制，所以在本仓库里核不了。

另有三条关于"这个 demo 的范围"的边界，跟上面 12 条性质不同，放在这里一起说：

- **只覆盖脾胃门**（呕吐、痞满、肿胀、痰饮等相关证候），不是全科辨证系统。
  **肿瘤科医案不在当前范围内**：实测 337 条标准证候里跟肿瘤/积聚沾边的是 **0 条**
  （`python -m offline.assess_case_scope --input <文件>` 可复现）。R8 接进来的两份本地
  医案（`data/local_corpora/` 的王云启治癌验案录、李可肿瘤医案）按选项 ②处理：**接进
  仓库、标 `out_of_scope: true`、训练导出默认排除**（`--include-out-of-scope` 才带上）。
  依据是数出来的：段落里含脾胃门门类词的比例，王云启 **211/1599 = 13.2%**、李可
  **42/556 = 7.5%**，而《脾胃论》同一口径是 **147/671 = 21.9%**；含肿瘤词的比例
  王云启 46.7%、李可 40.3%、脾胃论 0%（`data/local_corpora/MANIFEST.json` 的
  `scope_stats`，`python -m scripts.normalize_local_corpora` 可复现）。两份都是现代出版物，
  `copyright_status: copyrighted` 这一道过滤也会把它们挡在训练集外——两道过滤各管各的。
- **doctor 模式能导出处方建议稿**（`POST /api/prescription/export`），那是建议不是医嘱，
  必须经执业医师审核签发，每次导出记审计日志。其余三种模式没有任何写入能力。
- **沙箱开发环境连不上 huggingface hub**，`dense`/`hybrid` 需要的 embedding 模型下不下来，
  这两个模式的真实语义相似度数字在沙箱里产不出来（代码路径用受控假向量验证过）；
  `bm25`/`graph` 不需要网络，已在沙箱里 100% 真实验证（`data/SOURCES.md` 第 19、21 条）。

## 如何复现

数据管线**有顺序依赖**，按这个顺序跑：

```bash
python -m offline.split_cases                  # 1. 原文 → 医案粗段
python -m offline.extract_cases                # 2. 粗段 → cases.json（要真实 LLM）
#    3. 第三位医家要先注册进 core/physicians.py（id 小写下划线），再抽他的医案
python -m offline.build_graph --all             # 4-6. 建图 + graph_stats + 证素索引
#    ↑ 三步是一件事，`--all` 一次跑完。**不加 --all 只跑第一步**，后两步漏跑不报错
#      但会静默退化（见下），所以单独跑 build_graph 时它会打一条黄字警告
python -m offline.extract_case_triples         # 7. 医案 → 三元组（要真实 LLM）
python -m offline.build_syndrome_textbook \
    --md-path /tmp/tcmds/十四五教材/中医内科学.md \
    --out data/standard/syndromes.jsonl --append   # 8. 教材证候扩表（零 LLM 调用）
```

> ⚠ **第 5 步 `graph_stats` 名字像只读统计，它实际会写 `weight_by_physician` 回图。**
> 漏跑不会报任何错——图能建出来、服务能起来、辨证也能跑，**但追问的贝叶斯后验会静默
> 退化**（所有医家权重退回均匀分布，信息增益算出来的候选问题失去区分度）。
> 这个坑记在 `data/SOURCES.md`；这里再写一遍，因为复现的人只看 README。

跑完之后的验证（都是零 LLM 调用）：

```bash
python -m scripts.collect_results --check      # 文档里的评测数字跟 report 文件一致
python -m pytest tests/ -q                     # 不需要网络、不需要 key、秒级跑完
ruff check .
```

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
| `LLM_MODE` | `api`（默认，走 OpenAI 兼容接口）/ `local`（本地 vLLM 的 OpenAI 兼容 server）/ `local_inproc`（进程内 vLLM，批量评测用）/ `claude_cli`。后两种见下面「本地模型部署」一节 |
| `LLM_API_KEY` | DeepSeek（或其他 OpenAI 兼容服务）的 API key |
| `LLM_BASE_URL` | 默认 `https://api.deepseek.com` |
| `LLM_MODEL` | 默认 `deepseek-v4-pro`。**原来的默认值 `deepseek-chat` 已于 2026-09 下线**——拿它发请求得到的是 HTTP 200 + **空响应体**（不是 404），症状是每次调用返回空串、校验失败、重试三次后 `LLMError`，看不出根因在模型名上。段 0 现在会查 `$LLM_BASE_URL/models`把这种情况在花第一分钱之前挡掉。⚠ 换模型 = 本仓库所有既有数字（ε/E3/E4/E8/E9/SDT）都不可直接比较，见 `eval/RESULTS.md` |
| `LLM_MODE` 取 `claude_cli` | 走本机 `claude` CLI，**仅用于没有 API 网络时的冒烟**：模型不是 deepseek-chat、单次约 $0.06（DeepSeek 约 $0.0007），跑出来的分数不可与他人比较。`manifest.comparability_warning` 会把这一点一路带进报告 |
| `LLM_TIMEOUT_SECONDS` | 单次调用的读超时秒数（旧名 `LLM_TIMEOUT` 仍然认）。不设时按后端取默认值：云端 API 读 120 秒 / 墙钟 180 秒，本地 vLLM server 600/900，进程内 vLLM 1800（首次调用要加载权重）。四个 HTTP 相位（connect/read/write/pool）**分别设**，另有一层墙钟兜底——理由见下面「超时」一节。SDK 自带重试已关，重试统一由 `generate()` 负责 |
| `LLM_MAX_TOKENS` | 单次输出上限。不设时按模型分两档：非推理模型 **8192**（DeepSeek 默认 4096，S0 抽多病人粗段会被截断）、推理模型 **16384**。推理模型要更大是因为 **max_tokens 同时盖住不可见的 reasoning tokens**：deepseek-v4-pro 实测「你好」一句就花 45 个 token、其中 36 个是 reasoning，8192 下 S3 的可见输出在 2081 字符处被砍断。判据在 `core/llm.py` 的 `_default_max_tokens()`（`REASONING_MODELS` 列出已知的推理模型），不靠人记着在 `.env` 里设对 |
| `CLAUDE_CLI_MODEL` / `CLAUDE_CLI_TIMEOUT` | `claude_cli` 模式下的模型名与超时，默认 `claude-sonnet-5` / 180 |
| `USE_REACT` | `1` 打开 ReAct 取证（默认关，见「ReAct 取证模式」一节） |
| `FAST_MODE` | `1` 同时降级三处：追问 0 轮、ReAct 步数上限降到 2、残差辨证整体关闭（默认关，见「追问」一节）。实测一次完整问诊 14 次调用 → 8 次 |
| `EVAL_MODE` | `1` **只**让安全否决不中止链路（检查照跑、命中原因照记进 `safety_flag`），给评测量化"安全否决花了多少分"用。默认关，demo 的拦截红线不受影响；不要在对外演示的机器上打开——开着时页面顶部会有一条红色横幅提示，结果不会静默照常显示 |
| `WARMUP_TIMEOUT_SECONDS` | 启动预热（加载 embedding 模型 + 编码语料）最多等这么久，默认 120；超过就先开始服务，预热在后台继续、首个问诊会等它。连不上 huggingface 的机器预热会卡在下载重试上，不设上限的话服务一分多钟都不监听端口 |
| `MAX_CONCURRENT_CONSULTS` | 同时进行的问诊数上限（`/api/consult` 与 `/api/consult/stream` 合计），默认 4。满了立刻 503 + `Retry-After: 10`，不排队。这是部署侧的进程级设置，跟逐请求的 `retriever_mode` 不是一回事 |
| `LLM_MAX_INFLIGHT` | 进程内**同时在途**的 LLM 请求数上限，默认 6。**跟 `MAX_CONCURRENT_CONSULTS` 是两件事**：那个限"同时几次问诊"，这个限"同时几个请求打到模型"。R12 三位医家改成并发之后两者相乘——4 个问诊槽 × 3 位医家 = 12 路同时打 API，会撞 DeepSeek 的速率限制（429）。只留一个闸拦不住 |
| `S3_THINKING` | S3（按医家开方）开不开思考模式，`enabled`（默认，配 `reasoning_effort=high`）/ `disabled`。S1/S2/追问/ReAct **一律关思考**（结构化抽取，思考无增益却慢几十倍），这张表在 `core/llm.py::STEP_THINKING`。⚠ **关掉 S3 思考跑出来的数字跟默认配置下的不可比**，`manifest.comparability_warning` 会带上这句话；另外**思考模式下 temperature 不生效**，所以 `manifest.temperature_effective` 按步分别记 |
| `EMBEDDING_CACHE` / `EMBEDDING_CACHE_DIR` | 语料向量的磁盘缓存：`0` 关掉，或指定目录（默认 `data/cache/`，已 gitignore）。命中判据是模型名 + 语料条数 + **被编码文本的 sha256** 三者全等；缓存省的是"给全部语料编码"那一段，模型本身不管命不命中都要加载（查询要用它） |
| `LOW_DISCRIMINATION_CUTOFF` | `0` 关闭（默认开）：检索到的候选之间没有真实区分度（top-1 与 top-k 原始相似度差 < 0.03）时只保留 top-1，避免塞几条弱相关候选进 prompt 稀释信号。这条不确定是不是净收益，做成开关是为了能跑两遍对比（`consult()` 返回的每位医家结果带 `low_discrimination` 标记） |

> **检索模式不是环境变量。** `RETRIEVER_MODE` 仍然存在（离线脚本/单机评测用，
> **默认 `full_context`**），但 HTTP 请求要切模式请用请求体里的 `retriever_mode`
> 字段——环境变量是进程级的，两个并发请求各选一种模式会互相污染。
> 详见「检索：三路融合」和「full_context」两节。

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

## 公开部署（把这个 demo 挂到公网上）

**默认配置不适合直接对外**：`LLM_MODE=api` + 你自己的 key = 任何人都能用你的钱
跑推理。下面三层是为"挂出去给人点"准备的，三层可以叠加，**一层都不开也能跑，
只是钱是你出**。

### 三层访问控制（docs/DESIGN.md §5.1）

| 层 | 作用 | 默认 |
|---|---|---|
| **BYOK** | 访问者填自己的 key，存 `sessionStorage`，服务端不落盘 | 零成本无上限 |
| **共享额度** | 不填 key 的用项目的 | 每 IP 5 次问诊/天，全局 200 次/天 |
| **用量看板** | `/api/usage` + 顶栏「今日约剩 N 次」 | 80% 预警，100% 降级 |

四条硬要求，每一条都有对应的实现位置：

1. **请求前拦截**——被拦的请求不产生费用（`core/usage.py`，问诊开始前先按
   "这次最少会花几次"预扣，不够就直接拦）
2. **按 `llm_calls` 计量不是按请求数**——开不开 ReAct 差 3 倍（6 次 vs 20 次），
   按请求数计量等于给开 ReAct 的人打三折
3. **超限降级到回放而不是报错**——访问者仍能看到预录主诉的完整效果，
   顶栏下方一行说明（`--surface-2` 底，**不是警告色**：降级不是错误）
4. **BYOK 只接受 `sk-` 格式，不做通用代理**

### 六个环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `QUOTA_PER_IP_DAILY_CALLS` | `25`（= 5 次问诊 × 每次 5 调用） | 每个 IP 每天的**模型调用**上限 |
| `QUOTA_GLOBAL_DAILY_CALLS` | `1000`（= 200 次问诊） | 全站每天的模型调用上限，防一个人换 IP 刷爆 |
| `QUOTA_MAX_TRACKED_IPS` | `5000` | 额度表里最多记多少个 IP。**这是内存保护**：不设上限的话，用海量伪造 IP 发请求能把进程内存撑爆 |
| `TRUSTED_PROXY_HOPS` | `0` | **默认 0 = 完全不读 `X-Forwarded-For`。** 直接信任 XFF 等于把限额送人——任何人加一个头就换一个"IP"。只有部署在**自己的**反代后面时才设成反代跳数（nginx 一层就是 1），此时从右往左数第 N 跳才是真实客户端；**绝不取最左跳**，最左跳是客户端自己写的 |
| `FORCE_REPLAY` | 未设 | `1` 强制全站走回放（演示日用）。零成本、断网可用、每次一致 |
| `LLM_MAX_INFLIGHT` | `6` | 进程内**同时在途**的 LLM 请求数。跟 `MAX_CONCURRENT_CONSULTS`（同时几次问诊）是两件事——4 个问诊槽 × 3 位医家 = 12 路同时打 API 会撞 429 |

### nginx 反代示例

```nginx
server {
    listen 443 ssl;
    server_name tcm.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        # SSE 要的三条：不缓冲、不超时、不降级到 HTTP/1.0
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_set_header Connection "";

        proxy_set_header Host $host;
        # 这一行配合 TRUSTED_PROXY_HOPS=1 使用：nginx 把真实客户端 IP 追加在
        # XFF 最右侧，服务端从右数第 1 跳取。只加这一行而不设 TRUSTED_PROXY_HOPS
        # 的话服务端仍然不读 XFF（默认 0），所有人会被算成同一个"IP"。
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

对应的服务端设置：

```bash
TRUSTED_PROXY_HOPS=1 QUOTA_PER_IP_DAILY_CALLS=25 QUOTA_GLOBAL_DAILY_CALLS=1000 \
  python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

### BYOK 的安全边界（原话，不要改写）

> 你的 key 只在本次请求中转发给 DeepSeek，不会存储在服务器上。
> 关闭标签页后即清除。

落地细节：key 存 `sessionStorage`（关标签页就没了），随请求走 `X-LLM-Key` 头，
**服务端不落盘、不进日志、不进 manifest**。入口在顶栏右侧、默认收起成一行小字
「用自己的 API key（不限次数）」——不做成弹窗，不在首次进入时拦路。

存之前先验一把：走 `/api/usage/validate-key`，它用 DeepSeek 官方的「查询余额」
接口（`GET /user/balance`）核，**零 token 消耗**。没有这一步的话，填错 key 的人
只能靠跑一次问诊才知道，而那一次可能已经走完 S1/S2 才失败。

## 前端页面

无构建步骤，一个 `web/index.html`，两个页签：

**问诊页**——输入主诉、选检索模式、点「辨证」。走的是 SSE 端点，所以推理期间
能看见分步进度（症状标准化 → 证素推断 → 追问 → 每位医家取证/开方），不再是
一句静态的"请耐心等待"；系统要追问时页面上直接弹输入框，答完流继续往下走。
结果出来后是六层生长图（症状→证素→病名·证型→方剂→药材，方剂/药材是
compound 父子节点）+ 各位医家的结论对照 + 分歧度（医家数取自 `core/physicians.py` 注册表，现在是三位）。

**图谱浏览器页**——浏览持久知识图谱（`data/graph.json` 的国标结构层）。
R16 起**首屏铺的是证素**（20 个 `../data/graph.json:graph.n_elements=20`），
点一个证素展开它的证型、点证型展开症状、再点收起；布局是 concentric，
枢纽在内圈。之前首屏铺 80 个证型方块——证型之间本来就没有边，力导向对一堆
孤立节点只能摊平，那张图不传达任何东西。

这张图当前的规模：节点 1315 `../data/graph.json:graph.n_nodes=1315`、
边 3771 `../data/graph.json:graph.n_edges=3771`、
证候 178 `../data/graph.json:graph.n_syndromes=178`、
症状 1117 `../data/graph.json:graph.n_symptoms=1117`。
**这四个数以前没有凭据记号**（它们不在 eval/ 下的任何 report 里），于是
"重建图谱之后忘了回写"没有任何机制拦得住——而它已经发生过一次（839 → 941）。
R17 把它们纳入了 `python -m scripts.collect_results --check`。

另外支持按名字搜索、「按门类浏览」下拉（门类就是证候表的 `location`）、
按医家切换 λ1 权重（边的透明度按 λ1 编码）。类目证候用菱形节点区分。**页面顶部那段 λ1 说明不是装饰**：它是
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
| `offline/estimate_epsilon.py` | E：估计噪声地板 ε（`epsilon_online`/`epsilon_core`/`epsilon_adjunct`/`epsilon_s2`/`epsilon_extract`），写 `eval/epsilon.json`，供前端"分歧度"和 V1 的显著性判断做对照基准。`epsilon_core`/`epsilon_adjunct` 是 R1 加的君臣/佐使分层地板（见「分歧度的三层」一节），跟 `epsilon_online` 是同一批调用切出来的，不额外花钱 |
| `offline/extract_case_triples.py` | X3：从每一诊原文用真实 LLM 抽取三元组（`{case_id,physician,s,p,o,source_span}`），写 `data/case_triples.jsonl`，`core/tools.py` 的 `query_case_graph` 工具消费这份数据 |
| `offline/build_element_index.py` | K3b：从 `cases.json` 的症状字段建证素索引 `data/element_index.json`，供检索的 `mode="graph"` 和 `core/transition.py` 用 |
| `offline/quota.py` | 附属：审计 `cases.json` 是否达到 `data/SOURCES.md` 里写明的样本量门槛（总案例 60、带复诊序列 50） |
| `offline/extract_materia_medica.py` | 阶段二：本草三元组（性味/归经/功效/用量）。`--crosscheck` 把「用量」跟 `DOSE_LIMITS` 对一遍：一致 / 不一致 / 表外 / **表里有但没抽到（覆盖率）** / 解析不了五类，**只报不改表** |
| `offline/extract_formulary.py` | 阶段二：方剂三元组（组成/君臣佐使/加减法） |
| `offline/tag_incompatible_cases.py` | 给任意医案文件（`.json` 数组 / `.jsonl`）打「含十八反十九畏」标记，训练导出默认排除这类样本 |
| `offline/assess_case_scope.py` | 接新一批医案**之前**量它在现有 337 条标准证候表里的覆盖率，未覆盖的列出来（零 LLM 调用；只报数不自动判决） |
| `offline/docx_to_text.py` | `.docx` 医案 → UTF-8 txt（段落之间空行，下游按空行切块；含表格，医案 docx 偶尔用表格排药物剂量） |
| `offline/pharmacology_sources.py` | 药理层六源的唯一一张表 + 按源类型的块级预过滤（R8-1）：过短 / 表格 / 超长 / 无结构标记四类，阈值和判据的依据都在文件里 |
| `scripts/run_pharmacology_extraction.py` | 段 5 的批量入口：按六源表逐个源调抽取，`--dry-run` 报预估调用数、`--limit-blocks 5` 试抽、`--crosscheck` 全量 |
| `scripts/normalize_local_corpora.py` | 把 `data/` 根目录的本地语料规范化进 `data/local_corpora/`（安全文件名 + MANIFEST + docx→txt），幂等 |

## 知识图谱权重（进阶功能，非 consult 主流程必需）

`offline/build_graph.py` + `offline/graph_stats.py` 是独立于上面「快速开始」
consult 流程之外的一个附加模块：从标准证候定义建知识图谱骨架，并给每条
indicates（症状→证素）边算一个四层收缩权重——医家层 → 学派层 → 全局层 →
标准先验层，越往医家层数据越少就越往后收缩，标准先验（`syndromes.jsonl` 里的
`is_cardinal`）保证任何数据量下权重都有定义。跑法：

```bash
python -m offline.build_graph --all    # 建图 + 写回权重 + 建证素索引，一次跑完
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
  （这次测量时的语料是 839 条，现在是 941 诊次；**λ1 没有在新语料上重测**，
  引用这个数时要带上这句）
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
  每位医家开 ReAct，S3 阶段的调用数按注册表里的医家数翻上去。`manifest.llm_calls` 会如实
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
`RETRIEVER_MODE` 环境变量或 `search(mode=...)` 参数选路，五种取值。
**R21 之后默认值是 `full_context`**，下面表里前四种合起来叫 **top3 系**
（每次只喂 3 条最相似医案），它们保留为对照 arm——两系的数**不可比**，
`eval/RESULTS.md` 分两节并列报，不覆盖：

| mode | 依赖 | 说明 |
|---|---|---|
| `dense` | embedding 模型 | 语义相似度，原有行为不变 |
| `bm25` | `data/jieba_dict.txt`（可选，缺失时退化到 jieba 默认词典） | 关键词重合，不需要 embedding 模型 |
| `graph` | `data/element_index.json` + `query_elements` 参数 | 证素 Jaccard 相似度；不传 `query_elements` 直接报错，不静默退化成别的模式 |
| `hybrid` | 上面几路都可选 | 传了 `query_elements` 就三路融合，没传就退回 dense+bm25 两路，向后兼容 |
| `full_context`（**默认**） | `cases.json`；本草/方剂速查表可选 | 不检索：该医家**全部**医案按 `case_id` 排序进 prompt，靠前缀缓存把钱压下来（见下面「full_context」一节）。展示分固定 `1.0`，不是相似度 |

`min_score`（默认阈值 `MIN_RETRIEVAL_SCORE=0.70`）只作用于 dense 那一路——
bm25 的展示分是无界原始分，graph 的展示分是证素集合通常只有两三个元素时的
Jaccard 相似度，套用为稠密余弦相似度校准的阈值没有意义，也因此 `bm25`/
`graph`/`hybrid` 模式下离题主诉不会被过滤成空列表（这一条实测记在
`data/SOURCES.md` 第 19 条）。跑法：

```bash
python -m offline.build_jieba_dict          # K3a：BM25 分词词典
python -m offline.extract_case_triples      # X3：真实 LLM 抽取医案三元组（需要 cases.json）
python -m offline.build_element_index       # K3b：证素索引（需要 cases.json + data/graph.json）
RETRIEVER_MODE=hybrid ./run.sh              # 或 dense/bm25/graph/full_context（默认后者）
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

## full_context：知识不走检索，走前缀缓存（R21，默认）

**为什么换。** 以前每次问诊只把 top-3 条最相似医案塞进 prompt——一位医家几十上百
条医案里，模型每次只看得到三条。DeepSeek 的前缀缓存让"全都看"变得比"看三条"更划算：

| 输入 token | 价格 | 倍数 |
|---|---|---|
| 缓存未命中 | $1.32 / M | —— |
| **缓存命中** | **$0.044 / M** | **便宜 30 倍** |
| 输出 | $3.96 / M | —— |

一位医家的全量医案 ≈ 180K token：**首次** $0.24，之后每次 $0.008。三位医家一次
问诊 ≈ ¥0.17。缓存是**默认开的、不需要改代码**，但有三条官方规矩决定了实现方式
（原始链接在 `core/context_prefix.py` 的模块注释里）：

1. 存储单位是 **64 token**，不足 64 token 的内容不进缓存；
2. 每段缓存前缀是**独立完整的单元**，一个请求只在**完全匹配**某个前缀单元时才命中；
3. 不再使用的缓存会自动清除，**通常几小时到几天**——所以演示前要预热，
   `POST /api/key/validate` 的返回里带 `prefix_warmup_note` 就是提醒这件事。

**prompt 按"稳定的放前面、变的放最后"排六段**（`core/context_prefix.py`）：

| 段 | 内容 | 谁共享 |
|---|---|---|
| §2 | 本草速查表（`名｜性味｜归经｜功效｜用量上限｜禁忌`，按名排序） | 所有医家相同 |
| §3 | 方剂速查表（`方名｜组成｜主治`，按名排序） | 所有医家相同 |
| §1 | 辨证指令与输出 schema（**直接取自 `prompts/v1`，一个字没改**） | 所有医家相同 |
| §4 | 该医家全部医案，按 `case_id` 排序，每条走 `_format_case_block()` | 该医家 |
| §5 | 该医家用过的药材/方剂的**完整**条目（六个/八个谓词全给） | 该医家 |
| §6 | 本次的 S1/S2 结果、主诉、输出要求 | 逐次都变 |

§1 排在 §2/§3 **后面**不是笔误：`s3_syndrome.yaml` 的第一行就含 `$name`（医家名），
把它放最前面会让前缀在第 10 个字节处就分叉，跨医家的共享段一个字节也命中不了。
§2/§3 对所有医家逐字节相同，放最前面，三位医家共用同一段缓存。

**生成器是确定性的**：固定排序、不带时间戳、不带随机 id，跑两次 sha256 相同
（`tests/test_context_prefix.py` 里有一条专门钉这个）。看一眼每段多大：

```bash
python -m core.context_prefix --report                 # 读真实 cases.json
python -m core.context_prefix --report --synthetic 20  # 沙盒里没有 cases.json 时用合成语料
python -m core.context_prefix --report --budget 6000   # 看超预算时按什么顺序裁
```

预算 **500,000 token / 医家**。超了按固定顺序裁：方剂速查表 → 本草速查表 →
§5 条目，**医案永不裁**（医案是这个项目的立身之本，裁它等于把要证明的东西扔了）。
裁不裁是**按最大的那位医家全局决定的**，不是每位医家各算一次——否则共享段会
因医家而异，跨医家缓存共享当场归零。用的 token 尺是 `tiktoken cl100k_base`
（装了就用），没装时退化成一把**声明过的保守上界**（CJK 记 1、其他记 0.5，向上取整），
`--report` 的第一行总会打印用的是哪一把。

**命中率怎么看**：`usage.prompt_cache_hit_tokens` / `(hit + miss)`，经
`core/llm.py` 的 `record_usage` 收进 manifest，再由 `scripts/bench_consult.py` 报出来。
沙盒里没有真实 API，所以 bench 有个 `--simulate-cache`：按 64-token 块对齐算
最长公共前缀，产物里标 `simulated_cache: true`——**模拟值不是真机值**。

```bash
python -m scripts.bench_consult --backend fake --simulate-cache --repeat 3
#  前缀缓存命中率（逐次）：0.011、0.996、0.996   ← 第一次全 miss 是对的
python -m scripts.bench_consult --backend real --repeat 2      # 上机：判据第二次 ≥ 0.9
```

闸门定 **0.9 不是 1.0**：§6（本次主诉）永远不命中，它占 prompt 约 1%。

**full_context 下 ReAct 一律关**，即使 `USE_REACT=1`（会打印一行说明）。ReAct 的
工具是去检索语料的，而语料已经全在上下文里了。因此 **E9（ReAct 开/关）是只对
top3 系有意义的指标**，见 `eval/RESULTS.md`。

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

## 分歧度的三层（整方 / 君臣 / 佐使）

`divergence` 里的药物 Jaccard 距离现在有三个，各自带**自己那一层**的噪声地板：

| 字段 | 含义 | 对照基准 |
|---|---|---|
| `herb_jaccard` | 整方用药（R1 之前唯一的那个数，算法一个字没改，E3/E4/E9 的历史数字靠它可比） | `epsilon_online` |
| `core_jaccard` | 只算 `role` 为君/臣 的药——这个证的**核心判断** | `epsilon_core` |
| `adjunct_jaccard` | 只算 `role` 为佐/使 的药——针对兼夹症状的**加减** | `epsilon_adjunct` |

分三层是因为整方那一个数说不清"0.53 里多少是核心判断不一致、多少只是加减不同"。
实测同一条主诉重复跑三次，君臣骨架三次全在、变的全是佐使，两者混成一个数就看不见
这件事。**不要拿 `epsilon_online` 去卡分层的数**：佐使层的抖动明显大于整方、君臣层
明显小于整方，用同一个地板卡三层会把核心的一致性低估、把加减的发散高估。

`role` 没标注的药**不进任何一层**（只留在 `herb_jaccard` 里），按医家计入
`n_unroled`；某一层至少有一位医家没有标注 role 的药时，那一层的值是 `null` 而不是
`0`——**`0` 的意思是"两边完全相同"，跟"没数据"是两回事**。所以分层的数能不能当
全貌读，取决于 role 填充率：

```bash
python -m scripts.verify_role_fill          # 退出码 0 = 填充率 >= 90%
```

这个闸门要先过，分层的两个数才有意义（顺带报 `function_in_formula` / `dose` 的
填充率和平均药味数）。退出码 1 = 填充率不够（先改 `prompts/v1/s3_syndrome.yaml`），
2 = 没测出来（全部调用失败或全被安全否决，连闸门都判不了）。

平均药味数（`mean_herbs_per_formula`，三层各一个）是配套的对照数：如果 ε 降了
但药味数大幅下降（9 味 → 5 味），那是"药少了所以碰巧一样"的假改善，不是真的稳定。

## 评测汇总（V1，需要真实 LLM，不进 pytest）

`eval/run_eval.py` 把 divergence（分歧度 vs ε 噪声地板）、幻觉率（按有无
参考医案分组）、安全否决率+代价、检索模式对比（自实现 McNemar 检验，见
`eval/mcnemar.py`）汇总成 `eval/report.json` / `eval/report.md`：

```bash
python -m eval.run_eval --queries-path tests/queries.txt
```

**这几组数字的当前值、对照、caveat 和凭据只维护在
[`eval/RESULTS.md`](eval/RESULTS.md) 一处**（ε_online / ε_core / ε_adjunct / 分歧度 /
E3 / E4 / E8 / E9 / SDT / 参考医案利用率 / E2 学派配对 / MES），README 和 DEMO.md 从那里
抄，哪一行的代码改了那一行就标"待重跑"。`report.json` 里另有 `school_pairs`
（师承内 vs 跨学派的两两配对，总纲 1.3）和 `react_process.samples`（ReAct 前 10 条完整
动作序列）两段，排查"某条该进没进"这类问题时看它们，不用手写 python -c 去抓。

**手抄的数会漂，所以 RESULTS.md 里每个数都带「凭据」列，并且有脚本核：**

```bash
python -m scripts.collect_results              # 打出各 report 文件里**实际**是什么数
python -m scripts.collect_results --check      # 核对 README.md + RESULTS.md；不一致退出码 1
python -m scripts.collect_results --rerender   # 把跟自己 .json 对不上的 .md 重渲染（零调用）
```

凭据记号的格式是 `` `文件名:键=值` ``，`--check` 逐个去文件里取真值比较，并要求同一
行的正文里也出现这个数（凭据和正文不许各说一套），**顺带检查每份 `report_e*.json`
旁边的 `.md` 是不是同一轮渲染的**——一份旧的人读报告躺在一份新的数据旁边，而人只读 md。
`tests/` 里有测试直接对着仓库真实文件跑这个核对，所以谁改了带凭据的数，pytest 就红。

**凭据分三种状态**（RESULTS.md「凭据的三种状态」一节）：✅ 仓库里有文件可核；
📦 那一轮的文件被覆盖过、已从 git 历史取回 `eval/archive/<日期>/`；
⏳ 真机数据**根本不存在**，不是文件丢了，只能等上机跑。**⏳ 和「文件丢了」分开写**，
否则看不出哪些能补、哪些必须等。

### SDT 失分分析与过拟合护栏

```bash
# 零 LLM 调用：对已有提交文件重新聚合（逐条得分、多选率 vs 少选率及两者的边际
# 代价、失分最多的 10 条、按病机/证型分组）
python -m eval.sdt.run --sdt-dir $SDT --split Test --error-analysis out/sdt_chain_v2.txt
```

Test 集已经跑过 3 次完整 + 1 次局部（台账 `eval/sdt/test_run_log.jsonl`，每次跑
自动追加）。`--split Test` 时会打醒目提醒并报出已跑过几次——**再反复在 Test 上调
prompt 就是在测试集上过拟合**，会让这个外部可比的分数失去意义。规矩是：prompt
改动先在 Train（200 条）上验证方向，Validation 做中间验证（满分上限 48.9998/50，
官方金标准带 BOM），Test 只在最终定型后跑一次。细节与「拿到分析结果之后怎么改
prompt」的决策表见 [`eval/sdt/README.md`](eval/sdt/README.md)。

`eval/mes/`（盲评导出/收集）用于人工判断"这条辨证像不像话"这类自动指标
测不了的问题：`export.py` 把注册表里全部医家对同一条主诉的结果匿名成
A/B/C（隐去 `cited_case_ids` 以防暴露医家身份，顺序按 seed 打乱）导出评分表，
人工填完 `winner` 之后 `collect.py` 换回身份、统计胜负、算 McNemar 显著性——
McNemar 是配对二元检验，三位医家就是三对各跑一次（`--all-pairs`）。

```bash
# 10 条测试主诉 + 从 TCMEval-SDT 抽 10 条专家标注病例（$SDT 见下一节）
python -m eval.mes.export --sdt-dir $SDT --sdt-sample 10
# ……人工在 items.json 里给每条填 winner: "A"/"B"/"C"/"tie"……
python -m eval.mes.collect --all-pairs   # 叶×吴、叶×张、吴×张各一份，写 eval/mes/collected.json
```

## TCMEval-SDT 的数据获取与接法（细节）

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
- S1（症状标准化）全局只跑一次，所有医家共用结果，避免症状节点 id 对不上
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

## 药理层数据源（阶段二，抽取前的准备）

抽取引擎已经写好（`offline/extract_reference_triples.py`，两个入口
`extract_materia_medica.py` / `extract_formulary.py`），这一节是**跑抽取之前的
两步准备**。都是零 LLM 调用，但它们决定了那几百次调用会不会白花。

```bash
bash scripts/fetch_pharmacology_sources.sh --dry-run   # 看清单：4 本教材 + 2 本古籍
bash scripts/fetch_pharmacology_sources.sh             # 下载 + 校验字节数
python -m scripts.verify_pharmacology_chunks           # 切块 + 预过滤统计 + 打印保留的前 3 块 / 跳过的前 5 块原文
python -m scripts.verify_pharmacology_chunks --compare-modes   # 三种切法并排比
python -m scripts.run_pharmacology_extraction --dry-run        # 六个源的预估调用数（预过滤后），零调用
python -m scripts.run_pharmacology_extraction --limit-blocks 5 # 段 5 卡点：每个源抽 5 块看质量
python -m scripts.run_pharmacology_extraction --crosscheck     # 全量 + DOSE_LIMITS 交叉校验
```

| 本地文件 | 用途 | `--source` | 编码 | `--chunk-by` | 字节数 |
|---|---|---|---|---|---|
| `中药学.md` | 性味归经功效用量 | modern | UTF-8 | heading | 1542065 |
| `临床中药学.md` | 临床用量、配伍 | modern | UTF-8 | heading | 951968 |
| `中药炮制学.md` | 炮制方法与目的 | modern | UTF-8 | heading | 1436809 |
| `方剂学.md` | 方剂组成、君臣佐使、加减法 | modern | UTF-8 | heading | 1098496 |
| `000-神农本草经.txt` | 古籍本草 | classic | GB18030 → 转 UTF-8 | heading | 180115 |
| `018-本草备要.txt` | 古籍本草 | classic | GB18030 → 转 UTF-8 | heading | 293521 |

这张表在代码里只有一份：`offline/pharmacology_sources.EXPECTED_SOURCES`（切块验证、
抽取引擎的预过滤、批量入口三处都读它；下载脚本是 bash，另列一份并有测试逐字段比对）。

四本教材是 **markdown（`.md`）而不是 `.txt`**，来自 `PanckooAI/TCM_Datasets` 的
`十四五教材/` 目录——`offline/build_syndrome_textbook.py` 已经在解析同一批文件里的
《中医内科学》，读法一致。两本古籍在同一仓库的 `books/` 下，文件名是三位补零的
`NNN-书名.txt`（从 `000` 开始，共 704 本），**不是 `1-`/`9-` 这种不补零的写法**。

**下载脚本必须校验字节数。** 367 那本曾经下到一个少约 10% 的残缺文件，而切粗段的
统计数字**碰巧没变**（案数、门类分布都在合理范围），差点带着残缺原文往下走。
所以规矩是：比字节数，不对就退出，不看"统计像不像话"。上表的字节数是在有网络的
机器上实测的 `wc -c`，脚本里的 `expected_bytes` 就是这几个数。

**校验字节数要在转码之前。** `expected_bytes` 是上游原始文件的大小；GB18030 →
UTF-8 会把中文从 2 字节变成 3 字节，先转码再比字节数，两本古籍每次都会"失败"。
所以顺序是下载 → 比字节 → 转码。

**编码写死在表里，不靠猜。** GB18030 的中文字节序列有相当概率能被 UTF-8 解码成
乱码而**不抛异常**——"试着按 UTF-8 读一遍看会不会报错"会得到静默的乱码语料。

**切块验证的三个阈值**（块数 < 50 / 中位数 > 3000 字 / 单块 > 10000 字）各有依据，
写在 `scripts/verify_pharmacology_chunks.py` 里；最后那个的硬依据是引擎的
`MAX_TOKENS=16384`——按中文约 1 token/字算，一万字的块注定顶到上限被判截断，
那次调用是纯浪费。**但阈值只能排除明显切错**，"切出来的是不是一味药 / 一张方"
只有人看原文才判得出来，所以这个脚本会把每个源的前 3 块原文打出来。

**教材按标题切，古籍也按标题切（`<篇名>`）——这不是审美问题，是防幻觉的前提。**
`s6_extract_materia_medica.yaml` 要求 `s` 填"原文里这一条目的药材正名"，而
`source_span` 校验只覆盖 `o`（宾语原文），**`s` 没有任何原文校验**。markdown 教材
按空行切的话，`# 麻黄` 自己是一块（5 字，被引擎的 `MIN_BLOCK_CHARS=8` 丢掉），
`【用法用量】煎服，2～10g。` 又是另一块——里面根本没有"麻黄"三个字，模型只能猜
`s`，猜错了没有任何一道闸能发现。所以引擎加了第三种切法 `heading`（`#`~`######`
标题起一块、标题行留在块内），一味药的正名和它的性味/功效/用量在同一块里。
古籍原来按空行切，R8 在真实数据上实测**同样的洞**：转录体例是 `<篇名>丹沙` 空一行
`内容：味甘，微寒…`，按空行切 `<篇名>丹沙` 7 字短于 `MIN_BLOCK_CHARS=8` 直接被丢，
神农本草经 379 味里 350 味的药名进不了模型。所以 `heading` 模式现在也认 `<篇名>`/
`<目录>` 行，六个源全部 `heading`。每个源的推荐切法写在上表和两个脚本里，
`--chunk-by` 强行改成别的会打警告；`--compare-modes` 把三种切法的块数/中位数
并排打出来供对比。

**heading 模式还认三种"不该开新块的标题"**（R8 实测，OCR 把它们也升成了 `#`）：
字段标签本身（`# 【临床应用】`——临床中药学 333 味药的用法用量全在这一块，按它开
新块那块里没有药名，353 处；炮制学 437 处）、页眉（`# 106 中药炮制学`、`# 38 方剂学`、
`# 16目录`，54 处，逐个核过没有一个是条目）、以及标题下面的出处行（`# 《金匮要略》`、
`# Xiongdanfen（《新修本草》)`——方剂学 35 张方、中药学 2 味药的正文挂在它下面，而真标题
`# 大黄附子汤` 6 字单独成块被 `MIN_BLOCK_CHARS` 丢掉，这一条是 R8 审查在真实数据上抓
出来的）。修正前六源切 4818 块，修正后 3390 块。

**块级预过滤（R8-1）：切完还要按源类型挑出"像条目的块"再喂模型。** 六源切完 3390
块，其中教材的封面/CIP/公众号/目录/药名索引表（一块 18686 字）、章节导语、复习题，
古籍的书头，都不是条目——喂给模型只会诱导它从几个字里编三元组。判据**全是排版结构、
没有关键词黑名单**（"公众号""版权页"这种黑名单是打地鼠）：教材 = 行首有 `【字段】`
标签，或标题是「附药：」子条目（中药学 15 个附药块 22 味药的字段是散文，R8 审查抓出来
原判据把它们全丢了）；古籍 = `<篇名>` + `内容：`/`属性：` 正文，或带剂量词（钱/两/分/
枚/铢）的方药。
五类跳过按顺序判：过短（< 30 字）/ 表格（HTML 表格占比 > 0.8）/ **索引**（正文里
「药名 + 页码」行占比 ≥ 0.5——目录页的附药索引跟 HTML 索引表是同一种东西，只是没排成
表格；实测那一块是 1.0，15 个真附药条目全是 0.0）/ 超长（> 10000 字）/ 无结构标记，
阈值的依据写在 `offline/pharmacology_sources.py`。实测：

| 源 | 切块 | 保留 | 过短 / 表格 / 索引 / 超长 / 无结构标记 | 对照 |
|---|---|---|---|---|
| 中药学.md | 740 | **457** | 9 / 8 / 25 / 0 / 241 | `【现代研究】` 443 次 = 443 味正条目 + 14 个「附药：」子条目（第 15 个是目录页索引，按「索引」跳掉） |
| 临床中药学.md | 700 | **341** | 11 / 3 / 41 / 1 / 303 | `【处方用名】` 333 次 = 333 味药 + 7 条本草史条目 + 1 块 OCR 碎片 |
| 中药炮制学.md | 694 | **254** | 29 / 5 / 29 / 0 / 377 | `【处方用名】` 240 次 = 240 味药 + 14 条子条目（「2.制马钱子」这类） |
| 方剂学.md | 389 | **235** | 3 / 4 / 20 / 0 / 127 | `【组成】` 235 次 = 235 张方 |
| 000-神农本草经.txt | 379 | **378** | 0 / 0 / 0 / 0 / 1 | `<篇名>` 379 个（含 3 篇序）；跳掉的 1 块是书头 |
| 018-本草备要.txt | 488 | **486** | 1 / 0 / 0 / 0 / 1 | `<篇名>` 488 个（含 3 篇序）；跳掉的是书头和一条 26 字的「银」 |
| **合计** | 3390 | **2151** | 53 / 20 / 115 / 1 / 1050 | 修正前 4818 块 ≈ ¥26.5，现在 2151 × ¥0.0055 ≈ ¥11.8 |

「索引」类净拦下 1 块新的（那个附药目录页），另外 114 块本来就在「无结构标记」里——
分出来是为了让 `--dry-run` 那一行说得出"被丢的是索引页"而不是笼统的"没有结构标记"。

保留数就是真实抽取的调用数，`scripts/run_onsite.sh` 段 5 的预估（2181 = 2151 + 每源
5 块试抽）从这里来。`--no-prefilter` 关掉它；`--dry-run` 打「N 块 → 保留 M 块（跳过：
过短 / 表格 / 超长 / 无结构标记）」；切块验证脚本把保留的前 3 块和跳过的前 5 块原文
都打出来——预过滤是按结构判的，判错了只有人看原文才看得出来。**两本古籍保留的
前 3 块是序（邵序/张序/孙序、陈序/童序/自序）**，它们跟药物条目一样是 `<篇名>` +
`内容：`，结构上分不开、也不该靠"序"这个字去分——6 块的代价是 6 次调用。

### 本地语料（R8-2）

用户上传到 `data/` 根目录的四个文件由 `scripts/normalize_local_corpora.py` 规范化进
`data/local_corpora/`（幂等，段 1 每次都跑）：

| 规范名 | 原名 | 类型 | 定位 | 版权 |
|---|---|---|---|---|
| `王云启医案.docx` | `2_王云启(1).docx` | 现代医案 docx | **out_of_scope**（肿瘤科） | copyrighted |
| `李可医案.docx` | `李可医案.docx` | 现代医案 docx | **out_of_scope**（肿瘤科；105/556 段含反药对，海藻甘草 75 段） | copyrighted |
| `脾胃论.txt` | `脾胃论 (中医经典文库) (金·李东垣 [金·李东垣], 古聖先賢) (z-library.sk,.txt` | 古籍排印本电子文本 | 在定位内 | public_domain |
| `584-医学衷中参西录.txt` | 同名 | 古籍 | 跟 `books/` 那本逐字节相同 → 删掉 data/ 这份（books/ 不进版本控制） | public_domain |

原名、字节数、sha256、来源说明、`scope_stats` 都在 `data/local_corpora/MANIFEST.json`。
docx 顺手转成同名 `.txt`（不进版本控制，派生物）供切块验证。

**这三份一份都不进药理层抽取**，理由写在 `offline/local_corpora.py` 的声明表里，
段 1 的切块验证和段 5 的 `--dry-run` 都会把它念出来：两份医案没有「性味/归经/功效/
用量」这些字段（药理层抽的就是这些）；《脾胃论》**在定位内**但按空行切出来方名和组成
不在同一块，而 `s`（方名）不过 `source_span` 核验，喂进去等于让模型猜方名。所以表上
有两个**独立**的布尔字段：`out_of_scope`（训练集要不要它，过滤在 `export_sft`）和
`pharmacology_source`（药理层能不能拿它当输入，闸门在抽取引擎里）——《脾胃论》就是
两者不一致的那个判据，合并成一个字段会看不出区别。引擎在**花第一次调用之前**拦：
`python -m offline.extract_materia_medica --input data/local_corpora/李可医案.txt …`
直接退出码 1，除非加 `--include-out-of-scope`（= `--allow-non-reference-input`）。
段 1 对它们跑切块验证只为看**段落粒度**，所以那里打的是「粒度参考 N 块（不进抽取，
不是调用数）」，不是预估调用数（脾胃论 671 → 106、李可 546 → 452、王云启 1434 → 937）。

## 录制回放（演示模式，`LLM_MODE=replay`）

作品提交、网站上线之后，一次问诊约 20 次调用（三位医家 + ReAct），访问者点几下
就能烧光余额；`.env` 里的 key 跟着部署走容易泄露；API 抖动或断网时演示直接挂。
**录制回放解决的就是这三件事**：零成本、零延迟、断网可用、每次结果完全一致。
（本地模型是长期方案，但它要 GPU 常驻，见下一节。）

```bash
python -m scripts.record_fixtures --dry-run   # 先看清单和预估调用数（约 272 次，¥1.5）
python -m scripts.record_fixtures             # 真录一次（需要真实 key）
python -m scripts.verify_replay               # 退出码 0 = 回放跟录制逐字节一致
LLM_MODE=replay uvicorn api.main:app --port 8000   # 演示
```

**索引是 `(schema 名, sha256(送进模型的 system 文本))`**，不是"第 N 次调用"——
按次序索引的话链路一变（加一步工具调用、ReAct 多走一步）整份 fixture 全体错位，
而且错位之后看不出是哪一条的问题。按内容索引则是"哪条没录到就报哪条"。

**未命中抛 `LLMError`，不返回空、不退回真实 API。** 静默退化会让「这是录制的
结果」这个声称变成假的，而且演示到一半悄悄开始发真实请求还会烧钱。错误消息里给
schema 名、prompt 前 100 字、sha12、fixtures 目录、已装载条数，以及**当前环境里
有哪个变量的取值在录制时从未出现过**（实测最常见的未命中原因不是忘了录，而是
`USE_REACT` / `RETRIEVER_MODE` 这类变量跟录制时不一样，链路走了另一条路）。

### 诚实标注不是可选项

- manifest：`backend` 是 `"replay"`（不伪装成实时调用），`model` 是
  `replay(deepseek-chat)`，另带 `replayed_from`（录制时间 / 模型 / commit /
  fixture 条数），`comparability_warning` 写明「本次结果来自 X 录制的推理
  （模型 Y），非实时调用」。
- 前端：header 下面一行小字「**演示模式：结果来自 2026-09-14 录制的真实推理
  （deepseek-chat），非实时调用**」。这行字**不挂在 manifest 上**——manifest 只
  下发给 researcher 角色，而演示给谁看就是给 patient/doctor/student 看的，挂在
  manifest 上等于对真正的观众隐身。它走一个独立的 `demo_mode` 字段，所有角色都
  拿得到，`/health` 也报，所以页面一加载就能显示、不必等跑完一次问诊。

### 三条要知道的边界

- **开 ReAct 和不开 ReAct 的 fixture 不共用**（prompt 模板不同），录制清单刻意
  各录一遍。
- **三种角色不需要各录一遍**：role 只在响应层裁剪字段，没有一次 LLM 调用跟它
  有关。`verify_replay` 会真的三种角色各跑一遍来证明这件事，不是嘴上说说。
- **追问路径的回放有固有限制**：访问者的回答文本变了 → 下游 prompt 变了 →
  未命中。对外演示建议 `FAST_MODE=1`（追问 0 轮）。

fixture 目录的说明见 [`fixtures/README.md`](fixtures/README.md)。

## 本地模型部署（vLLM）

两种模式，取舍不同，**都是可用实现**（不是占位）：

| | `LLM_MODE=local` | `LLM_MODE=local_inproc` |
|---|---|---|
| 怎么跑 | 对着 vLLM 起的 OpenAI 兼容 server 说话 | 进程内 `vllm.LLM(...)` 直接加载权重 |
| 适合 | 在线服务、演示（`api/main.py` 多线程问诊共用一个 server） | 批量评测（`run_eval`/`estimate_epsilon` 动辄上千次调用，省掉每次 HTTP 往返） |
| 代价 | 多一跳 HTTP | 模型跟脚本同进程：起停慢、并发要自己管、没法给 API 服务共用 |
| 实现 | `VLLMBackend`，**继承** `OpenAICompatBackend`（客户端构造/重试/max_tokens 只有一份实现） | `VLLMInProcessBackend` |

环境变量（跟 `scripts/start_vllm.sh` 读的是同一套）：

| 字段 | 说明 |
|---|---|
| `LLM_MODEL_PATH` | 本地权重目录。**`manifest.model` 记的是这个**，不是 served name——`tcm-local` 这种别名对复现没用 |
| `LLM_MODEL` | server 模式下是 `--served-model-name`（HTTP 请求里的 `model` 字段填它）。没设就用权重路径 |
| `LLM_BASE_URL` | 默认 `http://127.0.0.1:8000/v1` |
| `LLM_API_KEY` | 可不设：vLLM 默认不校验，代码会填一个明显的占位串 `EMPTY`（OpenAI SDK 不允许空值）。server 开了 `--api-key` 就设成真值 |
| `LORA_DIR` | 可选。阶段五每位医家一个 adapter，放在 `$LORA_DIR/<physician_id>/`。**不设 = 跑基座模型**；设了但某位医家的目录不存在 → **报错，不静默退化**（静默退化会让"这位医家用的是他自己的 LoRA"这句声称变成假的） |
| `VLLM_GUIDED_JSON_KEY` | 默认 `guided_json`。vLLM 这个参数名在版本间变过，撞上不认时改这个变量即可，不必改代码 |
| `VLLM_MAX_MODEL_LEN` / `VLLM_GPU_MEMORY_UTILIZATION` | 只对 `local_inproc` 生效（server 模式由启动参数决定），默认 8192 / 0.85 |

### 起服务并验证

```bash
export LLM_MODEL_PATH=/root/autodl-tmp/models/Qwen2.5-1.5B-Instruct
export LORA_DIR=/root/autodl-tmp/lora        # 可选，adapter 还没训就别设
bash scripts/start_vllm.sh                    # 另一个终端里

export LLM_MODE=local
export LLM_MODEL=tcm-local                    # 跟启动脚本的 --served-model-name 一致
python -m scripts.verify_local_backend        # 退出码 0 = 真的接上了
```

`verify_local_backend` 的三道闸门：① 一次 `generate()` 拿到通过 pydantic
校验的输出；② **这次只调用了后端一次**——guided_decoding 生效时输出必然
合法、不该触发 `generate()` 的重试，一旦重试说明 `guided_json` 这个键没被
这个 vLLM 版本认（配置问题，必须暴露，不能"反正重试也能成"地放过）；
③ `LORA_DIR` 设了的话每位已注册医家都有 adapter 目录。

`python -m scripts.verify_local_backend --show-prompt-budget` 不发请求，只从
真实 prompt 现算 `--max-model-len` 该给多少——`scripts/start_vllm.sh` 里那个
值就是这么来的（当前 16384，最坏路径实测约 13.5k tokens：ReAct trace + S3 +
两次重试回灌）。改了 prompt 就重算一遍，`tests/test_verify_local_backend.py`
有一条测试钉住脚本里的值跟现算值一致，脱节就会红。

### 相对云端 API 多出来的两件事

- **guided_decoding**：`extra_body={"guided_json": schema.model_json_schema()}`
  从解码层保证输出符合 schema。云端那套是"prompt 里塞 schema +
  `response_format=json_object`"的弱约束，模型仍可能给出不合 schema 的 JSON，
  靠三次重试兜底。
- **LoRA 热切换**：一个 server 进程按请求切 adapter
  （`extra_body={"lora_request": {...}}`）。`core/chain.py::run_physician` 把
  **医家 id**（不是中文名）传给 `generate(physician=...)`，每位医家的结果里
  带一个 `lora` 字段记实际挂的 adapter（`null` = 基座模型），`manifest.lora_dir`
  记 adapter 根目录。

> **本地模型跑出来的数字不可与 DeepSeek 的直接比较**，
> `manifest.comparability_warning` 会把后端、权重路径、LoRA 状态一路带进报告。

## 训练准备（阶段五 M15/M16，代码和数据就绪，还没训）

**训练的三个前提，两个满足**：E3 ≥ 40%（✅ 0.451 `report_e3.json:e3.change_rate=0.451`）、
SDT 有基线（✅ chain 23.173 `sdt/test_run_log.jsonl:sdt.chain_last=23.173` vs baseline 22.068 `sdt/test_run_log.jsonl:sdt.baseline=22.068`）、
MES 盲评指出哪个维度弱（⏳ 要人评）。第三条没满足就**没有靶子**
——那时候训出来的模型，不知道该拿什么判断它变好了没有。所以现在只准备代码和数据。

```bash
# 1) 导出六层链路训练数据（三源合并）
python -m offline.export_sft --format chain --sdt-dir $SDT --out sft_chain.jsonl

# 2) 看训练计划：样本数、每位医家分到多少、输出目录、第一条样本渲染成什么样
python -m scripts.train_lora --dry-run

# 3) 真训（训练机上先 pip install -r requirements-train.txt）
python -m scripts.train_lora --out-dir /root/autodl-tmp/lora
```

### 训练数据的三个来源，每步依据都能追到原文

| 来源 | 填目标链路的哪几步 | 依据原文 | 出处标签 |
|---|---|---|---|
| 医案（`cases.json` + `data/case_triples.jsonl`） | 证型→治法 / 治法→方剂 / 方剂→药材（另有 症状→病机 / 病机→证型） | 三元组的 `source_span` | `case:{case_id}` |
| TCMEval-SDT **Train**（`--sdt-dir`） | 症状→病机 / 病机→证型 | 官方金标准里专家撰写的辨证说明 | `sdt:{病案ID}` |
| 药理层（`data/materia_medica.jsonl`、`data/formulary.jsonl`） | 给 方剂→药材 / 治法→方剂 **补依据** | 本草/教材的 `source_span` | `materia_medica:{书名}`、`formulary:{书名}` |
| 教材证候表（`data/standard/syndromes.jsonl`） | 症状→证素 / 证素→病名 / 病名→证型 | 证机概要原文 | `standard:{证候编码}` |

第四行是**补上来的**：前三个来源一个都填不出目标链路的前三步（`CaseRecord` 既没有
证素字段也没有病名字段，SDT 的两个任务是病机和证型），不接教材证候表，"六步链路"
只能是五步。它已经在版本控制里，带 `disease` / `location` / `nature` / 证机概要原文。

**每步两个出处字段，不是一个**：`source` 是**输出**的出处，`rationale_source` 是
**依据文本**的出处。方名是医案里的，但"为什么这个治法用这张方"的原文依据来自
《方剂学》——合成一个字段就必然有一半在撒谎。`rationale` 拿不到就是 `null`，不编。

**教材前三步只接受证型名的精确命中。** 匹配复用 `core.tools.lookup_standard`
（唯一实现），但它最后一档"按名称部分匹配到唯一一条"在这里被拒绝：「湿热」能匹配上
「湿热布散三焦证」，一条错配会把另一个病的证素和病名写进样本，而依据是教材原文、
看起来完全正常。命中数和没命中的原因分布都在导出统计里。

### 划分与泄漏防护

- 医案按 `case_group_id` 切 train/heldout（`sha1` 定侧，不用随机——同一份
  `cases.json` 任何时候切出来都一样）。同一病人的所有诊次同侧：复诊跟初诊内容
  高度重复，分到两侧 heldout 就废了。
- **SDT 用它自带的 Train/Validation/Test，不重切**，而且只导 Train：另两个是评测集。
- 导出时报两个不同的泄漏数：`case_group_id` 交集（现在的切法结构上保证为 0，这一项
  是"将来有人改成按 case_id 切"的报警器），以及 **heldout 里 `input` 跟 train 逐字
  相同的样本数**——这一项现在就会非零（同一粗段两个病人共享原文）。**不自动去重**：
  去哪一侧是个取舍，代码替人决定会让"gap 是多少"变成一个来历不明的数。

### 训练脚本

- **两个基座都能跑**，基座是参数不是常量：`qwen2.5-1.5b`（通用）与
  `zhongjing-2-1.8b`（已在中医语料上继续预训练）。默认两个都训——**对照是免费的**，
  而"先验中医知识有没有用"只能靠这个对照回答。⚠ 后者的仓库 id 没在真机核对过，
  404 就用 `--base-model` 传真实 id（脚本会把这个提醒打出来）。
- **按医家训独立 adapter**：`--physician ye_tianshi`（中文名也认，走
  `resolve_physician_id`）。`physician_id` 为 `null` 的样本（SDT 那批）是**两位医家
  共用**，不是"没有医家所以丢掉"。
- 输出布局 `<out-dir>/<基座>/<physician_id>/`，最后一级正是
  `core/llm.py::_resolve_lora_path` 找的 `$LORA_DIR/<physician_id>`；脚本把每个基座
  对应的 `export LORA_DIR=…` 原样打出来。有一条测试直接拿推理侧那个函数验这个布局。
- **必须报 train/heldout gap**，而且 heldout loss 带它自己的基线：**训练前**同一批
  heldout 上的 loss（LoRA 的 B 矩阵初始化为 0，训练前的 peft 模型数值上就是基座，
  这个基线是免费的）。gap 超过 20% 打**过拟合警告**——那条线是约定不是实测值，
  所以警告是"去看下游指标"而不是"训练失败了"。heldout 为空时报的是"**不知道**"，
  不是"没问题"。
- **训练目标里不含出处 id。** 每步的 `source`（`case:ye_tianshi-0012-p3-0` 这类）
  不进训练目标：教模型背医案 id，它推理时就会凭记忆编一个出来，而那正是"每条结论
  必须引用真实医案 id"要防的事。依据原文进目标（那是要教的），id 不进（那是检索层
  该给的）。
- 训练依赖在 `requirements-train.txt`，**不在** `requirements.txt`：torch 那一套
  有好几个 G，装在只做推理的机器上纯浪费，CUDA 版本不匹配还会把能跑的环境搞坏。

### 训练之后：并列，不覆盖

本地模型重跑 ε/E3/E4/E8/E9/SDT/MES，结果**追加**到 `eval/RESULTS.md`（编号加
`-local` 后缀），DeepSeek 那组数一个字不动。理由和机制写在那个文件的
「同一指标、两个后端」一节：覆盖掉的那个数就是新数唯一的对照。

每个数字都带后端标签：`eval/run_eval.py` 的报告第一项是 `backend`（从每条结果自带的
`manifest` 抬上来，不读 `LLM_MODEL` 环境变量——那个变量在 claude_cli / replay 后端下
还是 `deepseek-chat`），`report.md` 顶部一行 `**后端**：…`，**一份报告里混了两个
后端会大声报出来**；`eval/sdt/run.py`、`eval/epsilon.json` 早就有；盲评的
`answer_key.json` 里加了 `_backend`，`eval/mes/collect.py` 把它写进结果。

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
body: {herb_items: HerbItem[], syndrome: string, disease?: string, role?: Role}
→ FormulaSafety 的字段 + 一个额外的 blocking 布尔（FormulaSafety.blocking
  是 pydantic 的 @property，model_dump() 不带计算属性，接口这里手动补上，
  避免调用方各自重新判断一遍"incompatible 或 dose_violations 非空就是
  blocking"）+ R23 的建议层三个键 advice / advice_skipped / formula_score
```

## 方剂建议层（R23）：跟安全层分开的第二层

`core/formula_check.py::check_formula(syndrome, herb_items)`。**跟上面的安全层
不是同一件事**，所以是两个模块、两套输出：

| | 安全层 `assess_formula_safety` | 建议层 `check_formula` |
|---|---|---|
| 回答的问题 | 这方**能不能发出去** | 这方**拟得好不好** |
| 命中后果 | 拦截级会让 `run_physician` 重开一次方 | 不打回，只展示；给 R22 的 best-of-N 排序 |
| 输出 | `FormulaSafety` | `Advice[]` + `skipped[]` + `score` |

两层不合并的理由就是 CLAUDE.md「同一概念只能有一处实现」的**例外条款**：
两处回答的不是同一个问题。合成一张表，以后调打分权重（排序需求）会连带改动
拦截判据（病人安全）。作为代价，建议层**不重新实现任何判据**——五条规则里
三条直接调安全层的函数：

| 规则 | 判据来源 | 权重 | 分级 |
|---|---|---|---|
| 十八反十九畏 | **复用** `check_incompatible` | 1.0 | blocking |
| 超药典常用上限 | **复用** `check_dose_limits`（`DOSE_LIMITS`） | 0.5 | blocking |
| 证型寒热方向相悖 | **复用** `check_thermal_consistency` | 0.3 | warning |
| 缺引经药 | 药理层「归经」+ `core.elements.LOCATIONS` | 0.15 | suggestion |
| 性味功效重复 ≥ 60% | 药理层「性味」「功效」 | 0.1 | suggestion |

`score_formula` = `1.0 − Σ权重`，下限 0.0，权重表只在 `ADVICE_WEIGHTS` 一处。
**这是一把粗排序尺，不是疗效评分**：它唯一的用途是在**同一次问诊**采样出的
N 张方之间挑一张（R22）。跨问诊比这个分没有意义——不同证型能触发的规则条数
本来就不同，分高只说明"这张方踩到的规则少"。

后两条规则要 `data/standard/materia_medica.jsonl`（药理层本草表，AutoDL 上跑
抽取才有）。**缺它的时候不是静默少两条建议**，而是在 `advice_skipped` 里如实
列出「哪条规则没跑、为什么、怎么才能跑」。三种情况分得开：

| 情况 | `advice_skipped` 里的样子 |
|---|---|
| 表还没建出来 | `available: false` + 产物路径 + 上机命令 |
| 表在、这味药不在表里 | `available: true` + `n_checked: 1, n_herbs: 2` |
| 表在、查过了、确实没问题 | 不出现（advice 里也没有这一类） |

**患者角色拿不到这一层**：每条建议的 reason 里都带着具体药名
（「甘草 与 甘遂 属配伍禁忌」），下发 advice 等于把处方内容从另一个字段漏出去。
判据是 `api/main.py::_role_gets_advice` 一个函数，`/api/prescription/validate`
和 `results[i]` 两处都问它。

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
