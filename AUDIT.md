# 企业落地级复查报告（2026-09）

对整个仓库做的一轮复查：并发/安全/代码质量三路独立审计（每条发现都回到源码
逐条核实过，审计给的结论不直接采信）+ `ruff` + `bandit` + `pytest-cov` + 真实
uvicorn 冒烟。基线是 `d7db777`（V3 收尾）。所有改动都在这条分支上、都有测试守着。

## 0. 验收结论（数字都带对照）

| 指标 | 复查前（d7db777） | 复查后 | 说明 |
|---|---|---|---|
| `pytest -q` | 802 通过 | **856 通过**（+54） | 新增 4 个测试文件 + 若干用例；1 条既有断言按契约变更改写（§4） |
| `ruff check .` | 45 条（F401×8、F811×17、F601×5、E741×7、F841×3…） | **0 条** | 规则基线固定在 `ruff.toml`（默认 E/F；tests 忽略 F811 夹具误报） |
| `bandit -r core api offline eval` | 5 条 Low、0 Medium/High | 4 条 Low、0 Medium/High | 剩 4 条全是误报（subprocess 列表调用、评测抽样用 random）；去掉的那条是 `export_sft.py` 的同义反复 assert |
| 语句覆盖率（core+api+offline） | 88%（2884 语句） | **89%**（3009 语句，未覆盖 344） | 0% 的两个文件：`core/graph/schema.py`（现在被 store 校验消费，已覆盖）、`offline/build_jieba_dict.py`（纯 CLI，仍无测试，见 §6） |
| 真实 uvicorn 冒烟（claude_cli 后端） | — | 见 §5 | 两次：无 cases.json 的默认路 + 合成 cases.json 的 bm25 全链路 |

这台沙箱的限制照旧：没有 `cases.json`、连不上 huggingface（dense/hybrid 检索跑不了）、
没有 docker 守护进程（Dockerfile 没法在这里验证，所以没写，进路线图）、GitHub Actions
只能写不能跑。

## 1. 发现与处置（按严重度）

"核实"一栏写的是我怎么确认它是真的，不是审计员怎么说的。

### P0（会在真实并发下出错）

| # | 发现 | 核实 | 处置 |
|---|---|---|---|
| 1 | `DenseRetriever._load()` 先发布 `_model`，编码语料十几秒后才发布 `_embeddings`；`_ensure_encoded()` 锁外快路径只看 `_model`。两个冷启动并发请求，后到的拿 `None` 做矩阵乘。 | 读 `core/retrieval.py:66-84`；写了用会卡住的假 `SentenceTransformer` 撑开窗口的测试，修前必红 | **已修**：建好再发布，快路径改看最后发布的 `_embeddings`。`tests/test_concurrency_init.py` |
| 2 | 无认证、无限流、每个 `/api/consult/stream` 起一根不设上限的线程：一个 for 循环里的 curl 能开几千根线程把 API key 额度烧光；客户端断开后线程照跑到底 | 读 `api/main.py` SSE 段；用真实 socket 复现"断开后 steps 继续涨" | **部分已修**：并发上限 `MAX_CONCURRENT_CONSULTS`（默认 4，满了 503+Retry-After）；断连后下一次回调即停（真实 socket 测试）。**认证/限流放反向代理**，进路线图（§6） |
| 3 | `complaint` 无长度上限；uvicorn 整个请求体读进内存 | 读 `ConsultRequest` | **已修**：1–2000 字，`answer` ≤500，超出 422，一次 LLM 都不花 |

### P1

| # | 发现 | 核实 | 处置 |
|---|---|---|---|
| 4 | XSS：`cardHtml` 里"幻觉引用"警告是整个卡片唯一没过 `escapeHtml` 就进 `innerHTML` 的插值；`hallucinated` 是模型原样输出的 id | 读 `web/index.html:1481`；node 跑真实脚本注入 `<img onerror>` 复现 | **已修** + `escapeHtml` 补转引号。`tests/test_stream_frontend.py` |
| 5 | `/answer` 流一开就登记队列：还没提问就能塞答案，下一问立刻被"回答"；上一问超时后迟到的答案留在队列喂给下一问（追问「有没有便血」吃到上一问的「有」→ 凭空安全否决） | 读旧 `stream_ask_fn`；单元测试复现 | **已修**：`_ConsultStream` 每个问题一条容量 1 的新队列，只在挂起时登记；404 否则。真实冒烟里三问三答验证 |
| 6 | 同步 `def` 端点占 anyio 线程池（默认 40 槽）；`/health` 也是同步的，问诊把槽占满时存活探针超时→编排器重启活着的进程；每条开着的 SSE 流长期占一个槽 | 读端点定义 + Starlette 源码 | **已修**：`/health` 改 `async def`；SSE 生成器改 async 轮询（不占槽、可取消）；并发上限兜底 |
| 7 | `get_retriever()` / `get_llm()` / `get_graph_store()` / `_load_standard()` / `_load_case_triples()` 五个惰性单例裸 `if x is None`；jieba `load_userdict` 改进程级 trie 却只有按实例的守卫 | 读五处；并发测试复现双建 | **已修**：模块级锁 + 双重检查；jieba 加模块级锁。测试用 8 线程 barrier 验证只建一份 |
| 8 | 拒绝文案在 `safety.py` / `chain.py` / `followup.py` 三处逐字拼一遍；「问的是危重症状而患者没否认」在后两处各写一套、看的文本不同、否定语义不同（一边 `honor_negation=False` 一边 `True`） | `grep` 文案只在三处；读两段判据 | **已修**：`veto_message()` + `danger_confirmed_by_answer()`（两个都看、取并集，更保守）。`tests/test_dedup_contracts.py` 用 grep 钉住"只在一处" |
| 9 | `eval/run_eval.py` 有一份跟 `core/chain.py` 逐字相同的 `_load_epsilon_online`——ε 是全项目最关键的对照基准，读它的代码有两份 | diff 两个函数体 | **已修**：只留 `core.chain.load_epsilon_online`，run_eval 导入它 |
| 10 | 提示词注入：主诉直接拼进 S1 的 **system** 段（`prompts/v1/s1_normalize.yaml`，`user=""`），S1 是唯一的标准化层，注入能操纵它输出什么给第二道安全网看 | 读 yaml 与 `normalize()` | **未改，进路线图**：确定性的 `check_safety` 对**原始主诉**已经是权威闸门（`core/chain.py` 先扫 `[complaint]` 再扫 S1 输出），漏洞只在正则漏、S1 又被操纵的交集；把主诉挪到 user role 要改 10 个 yaml 并在 AutoDL 上用真实 LLM 回归，这台沙箱做不了 |

### P2

| # | 发现 | 处置 |
|---|---|---|
| 11 | 后台异常 `str(e)` 原样下发（LLMError 带后端名、模型名、模型原始输出前 500 字，claude_cli 还带子进程整段 stderr）；`retrieval_error` / 503 detail 带项目绝对路径 | **已修**：客户端只拿异常类型 + 错误编号，全文在服务端 stderr；`_public_text()` 抹路径留文件名。用户侧错误（模式名写错）文案原样 |
| 12 | `EVAL_MODE` 把安全否决关掉时页面看不出来，`safety_flag` 字段前端从不消费 | **已修**：页面顶部红色横幅（`renderSafetyFlag`） |
| 13 | LLM 传输错误零间隔重试 3 次（429 变三个 429）；claude_cli 最坏 3×180s | **已修**：传输错误退避 1s/2s（校验错误不退避）；测试里 conftest 清零 |
| 14 | `query_case_graph` 用裸双向子串比症状文本，同模块 `_match_graph_symptoms` 拆并列名——同一个词两个工具答案相反（CLAUDE.md 第三次撞墙同款） | **已修**：`_symptom_text_matches()` 一处实现 |
| 15 | `_consult_response` 四个分支四套键集（10/15/14/15） | **已修**：一份 `base` 全键，分支只覆盖差异；测试钉住四分支同键集 |
| 16 | 追问候选两条产出路径键集不一致（十问歌兜底缺 `safety_relevant`） | **已修** + 测试比对两个字面量的键 |
| 17 | `run_eval` / `mes/export` 批量跑 `[consult(q) for q in queries]`，第 9 条挂了前 8 条已付费结果一起丢 | **已修**：`core.chain.consult_many()` 一处实现，失败进 `report.json` 的 `failed_queries` |
| 18 | `conftest` 只隔离 `USE_REACT`/`FAST_MODE`，`EVAL_MODE`/`RETRIEVER_MODE` 会从外面 shell 漏进来；`test_eval_mode.py` 直接写 `os.environ` 不用 monkeypatch，中途断言失败会把安全否决静默关掉留给后面的用例 | **已修** |
| 19 | `on_event("startup")` 已 deprecated；预热直接跑在事件循环上；预热没有上限——真实冒烟里有 `cases.json` 但连不上 huggingface 的机器，预热卡在下载重试上一分多钟，服务一直不监听端口 | **已修**：lifespan；预热在自己的线程里，最多等 `WARMUP_TIMEOUT_SECONDS`（默认 120）就先开始服务，预热在后台继续 |
| 20 | `core/safety.py::_scan` 每个起始位置 `re.compile` 一次（200 字 × 12 模式 ≈ 2400 次缓存查找/请求，热路径） | **已修**：模块级预编译 |
| 21 | `core/graph/schema.py` 整个模块无人引用，docstring 说的三个消费方全写字面量；`EDGE_SOURCES` 跟数据对不上 | **已修**：`NetworkXStore.add_node/add_edge` 对着词表校验类型（建图手误当场炸）；删掉与数据不符的 `EDGE_SOURCES` |
| 22 | 死代码：`RESIDUAL_MAX_ROUNDS`、`chain.HERB_ALIASES`/`strip_dose` 再导出、`herbs.strip_dose`、`ELEMENTS` 导入、`estimate_epsilon` 不可达分支、`sdt/run.py` 里读了没人用的金标准（缺 Results 还会让整次跑挂） | **已删** |
| 23 | `export_sft.py` 的 assert 断的是过滤的定义本身（恒真）且 `python -O` 会删；README 说"过滤断言会自动拦截" | **已修**：排除条数和 id 打到 stderr；README 改成实话 |
| 24 | `HERB_ALIASES` 五个重复键（值相同，静默覆盖） | **已删重复** |
| 25 | 文档与代码不符 7 处（`.txt` vs `.json`、"三位医家"、"13 条证候"、"9 个 yaml"、"任何环境都能跑"、numpy 不在依赖里、`.env.example` 缺 8 个变量） | **已改**：README/DEMO/HANDOFF/CLAUDE.md/`.env.example`/两处代码注释 |
| 26 | `requirements.txt` 全是 `>=` 下限；实际跑的是 openai 3.6 / pydantic 2.13 / fastapi 0.141 | **部分**：把验证过的版本写进 requirements 头部注释；锁文件要在目标机器（AutoDL，CUDA torch）生成，进路线图 |
| 27 | 没有 CI、没有 lint 配置 | **已加** `.github/workflows/ci.yml`（ruff + pytest，装 CPU torch）+ `ruff.toml`。**没法在沙箱里跑 Actions**，第一次 push 后要看一眼 |

### P3（记录，未改，理由在 §7）

`_build_manifest` 每次问诊对 `cases.json` 做一遍 sha256；`/api/trajectories` 每次请求重读并 re-validate 整份 `cases.json`；`/api/graph` 每次重建整个 Cytoscape 载荷；daemon 线程没有 shutdown 钩子；`consult()` 329 行、`run_physician()` 155 行；`offline/build_jieba_dict.py` 无测试；`Neo4jStore`/`VLLMBackend` 占位类；`cases.json` 在 9 处各自 `json.load + model_validate`。

## 2. 改动清单

- **core**：`retrieval.py`（发布顺序、单例锁）、`retrieval_hybrid.py`（jieba 模块锁）、`llm.py`（单例锁、退避）、`tools.py`（加载锁、`_symptom_text_matches`、候选键集）、`safety.py`（预编译、`veto_message`、`danger_confirmed_by_answer`）、`chain.py`（复用判据、`consult_many`、`load_epsilon_online`、死代码）、`followup.py`（复用判据）、`herbs.py`（`normalized_herb_set`、重复键）、`graph/store.py`（类型校验）、`graph/schema.py`、`setstats.py`（注释）
- **api/main.py**：lifespan、请求上限、并发上限、`_ConsultStream`、脱敏、`_consult_response` 同键集、`/health` async
- **web/index.html**：`escapeHtml` 转引号、幻觉 id 转义、EVAL_MODE 横幅
- **offline / eval**：`export_sft.py`、`estimate_epsilon.py`、`split_cases.py`、`build_graph.py`（注释）、`run_eval.py`、`mes/export.py`、`sdt/run.py`、`sdt/adapter.py`
- **tests**：新增 `test_api_hardening.py`(14)、`test_concurrency_init.py`(6)、`test_dedup_contracts.py`(20)；`test_api_stream.py` +7、`test_llm_backend.py` +3、`test_stream_frontend.py` +3；`conftest.py` 两个 autouse 夹具；ruff 清理若干
- **工程**：`ruff.toml`、`.github/workflows/ci.yml`、`.gitignore`、`.env.example`、`requirements.txt` 头注释
- **文档**：README（env 表、请求上限与错误契约、测试节、两处实话）、DEMO、HANDOFF（§六）、CLAUDE.md（数据路径）、`data/SOURCES.md` 第 26 条

## 3. 闸门逐条

- 没有占位符、没有"基本完成"；每个新公开函数都有测试（`veto_message`、`danger_confirmed_by_answer`、`normalized_herb_set`、`consult_many`、`load_epsilon_online`、`_symptom_text_matches`、`_ConsultStream` 四个方法、`_public_text`、`_public_error_detail`、`_acquire_consult_slot`）
- `core/schemas.py` 的 18 处 `min_length=1` 一处没动（`git diff d7db777 -- core/schemas.py` 为空）
- 安全否决仍在 S2 之前；追问回答仍先过 `check_safety`——这一轮只把两条路径的第二道判据合并成一处、且改得更保守（两段文本都看）
- 每个 `except` 都有"为什么"的注释；所有开关仍是"显式参数优先，未指定才读环境变量"，`retriever_mode` 仍然不碰进程状态（AST 测试没动）
- 没加缓存、数据库、日志框架、DI 容器。`print(file=sys.stderr)` 是 CLAUDE.md 下唯一被允许的运维输出；`MAX_CONCURRENT_CONSULTS` 是进程级的部署参数，跟逐请求的行为开关是两回事，在代码里写明了区别

## 4. 契约变更（逐条）

1. SSE `error` 事件 / 500 的 `detail`：从 `str(e)` 改为「服务端处理失败（<异常类型>，错误编号 xxxx）」；`tests/test_api_stream.py::test_exception_in_worker_becomes_error_event` 的断言从"原文在 detail 里"反转为"原文**不在** detail 里、在 stderr 里"。用户侧 `ValueError`（模式名写错）文案不变，另有测试守着。
2. `/api/consult` 与 `done` 事件：四个分支现在返回同一套 15 个键（`rejected` 分支多出 `insufficient`/`coverage`/`s2`/`residual` 等键，值为默认；`insufficient` 分支多出 `reject_reason: null`）。前端读的是 `data.x`，多出的键无影响。
3. `complaint` 空串 → 422（之前会真的调一次 S1）；超 2000 字 → 422；`answer` 超 500 字 → 422。
4. 并发问诊超上限 → 503 + `Retry-After: 10`（之前无上限）。
5. `/answer` 在"没有问题挂起"时 → 404（之前 200 并把答案存起来喂给下一问）。
6. `NetworkXStore.add_node/add_edge` 对未知 `node_type`/`edge_type` 抛 `ValueError`（之前照收）；`tests/test_api_graph.py` 里那条"没有 name 的节点"测试的 `node_type` 从 `"weird"` 改成 `"symptom"`——测的是缺 name，不是类型。
7. `filter_public_domain` 不再 `assert`，排除时打 stderr。
8. `eval/report.json` 多一个 `failed_queries` 字段；`run_eval`/`mes/export` 遇到单条失败继续跑。
9. `LLMBackend.generate` 传输错误重试之间睡 1s/2s；`tests/conftest.py` 全局清零，专门的退避测试在子类上设回真实值。
10. `run_eval._load_epsilon_online` 改名 `load_epsilon_online`（来自 `core.chain`）；`tests/test_run_eval.py` 三处 monkeypatch 的属性名同步改。
11. 启动预热超过 `WARMUP_TIMEOUT_SECONDS` 不再阻塞监听（之前预热多久服务就多久不可达）。
12. 默认检索模式自己跑不了时 `retrieval_error` 不再说"换用默认模式可以正常辨证"（真实冒烟踩到的）。

## 5. 真实产出（真实 uvicorn + 真实 `claude_cli` 后端，`curl`/httpx 打真实 socket）

两次冒烟，都是改完代码之后跑的，不是改前的记录。

**第一次：这台沙箱的真实状态（没有 `cases.json`）**

- `POST /api/consult` 带 2001 字主诉 → **422**，服务端日志里没有任何 LLM 调用
- 模式名写错 → **400**，文案「未知的 retriever_mode='没有这个模式'，目前支持 [...]」原样给（用户侧错误不脱敏）
- `retriever_mode=graph` → **200 + `retrieval_error`**，15 个键齐全；文案里是「未找到 cases.json」而不是 `/home/user/.../cases.json`——路径抹掉、文件名留着
- `POST /api/consult/stream`（默认模式）：`stream_id → s1_done → s2_done → need_input`，流真的停在追问上等；从另一条连接 POST `/answer` 三次，每次都恰好接到当时挂起的那个问题（「有没有两胁胀满？」→没有、「有没有脘腹痞满？」→有、「有没有胁肋胀痛或窜痛？」→没有）；对不存在的 stream_id → 404。最后 `physician_start → done`，`done` 里是 `retrieval_error`（默认的 hybrid 模式在这台机器也跑不了）——**就是这一步暴露了文案会说"换用默认模式可以正常辨证"这条错误建议**，已修（§4-12）。3 次 LLM 调用，173.5 s（S1、S2、追问后的 S2）。
- 启动那次预热卡了一分多钟才 403（huggingface 不可达），这段时间服务不监听端口——暴露了 §1-19 那条，已修：`WARMUP_TIMEOUT_SECONDS`。

**第二次：合成 6 条医案（明确标注合成、跑完即删、本来就 gitignore）走 `bm25` 全链路**

`HF_HUB_OFFLINE=1`（模型下载立刻失败，不再等重试）、`WARMUP_TIMEOUT_SECONDS=30`：服务 **6 s** 就监听了（第一次是 60 s 都没起来）。同一条主诉「胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦」，追问三轮全答「没有」：

| 项 | 叶天士 | 吴鞠通 |
|---|---|---|
| 证型 | 肝胃不和（肝气犯胃，胃失和降） | 肝气犯胃（肝郁气滞，胃失和降） |
| 治法 | 疏肝和胃，佐以制酸 | 疏肝理气，和胃制酸 |
| 方 | 柴胡疏肝散加减 | 四逆散合左金丸加减 |
| 药 | 柴胡、白芍、陈皮、香附、枳壳、炙甘草、煅瓦楞子、佛手 | 柴胡、白芍、枳壳、炙甘草、黄连、吴茱萸、香附、陈皮 |
| 引用 | `ye_tianshi-synthetic-001`（BM25 分 8.49，其余两条 1.6–1.7） | `wu_jutong-synthetic-001`（4.88，其余 2.2–2.4） |
| 幻觉引用 | 0 | 0 |

- 分歧：`same=False`、治法不同、**药物 Jaccard 0.40**（共用 6 味：柴胡、白芍、枳壳、甘草、陈皮、香附）。对照基准 ε 这台机器**未测**（`epsilon_online: null`），前端会如实显示"未测"，不是编一个。
- 覆盖率 1.0，残差未触发；`manifest`：backend=claude_cli、model=claude-sonnet-5、**4 次调用、101.6 s**、`cases_sha256=6a25204ea4b9`（合成数据的哈希，跟真实 `cases.json` 不可比）。
- 图：22 节点 / 25 边 / **0 条丢弃**，四层齐全；药物节点 label 已剥剂量、id 保留医家前缀（`herb::ye_tianshi::柴胡`）。
- 事件序列：`stream_id s1_done s2_done (need_input followup_answered)×3 followup_done (physician_start s3_start physician_done)×2 done`，共 17 条，跟 README「SSE 事件」一节一致。
- 这次的数字全部来自 Claude 而非 DeepSeek、语料是 6 条合成案，`comparability_warning` 会跟着 manifest 一路带到报告里——**一个都不能进对比表**。它证明的是链路和契约，不是效果。

## 6. 路线图：到企业落地还差什么（按优先级）

标 **[CLAUDE.md 冲突]** 的是 demo 期约定明确不让加的，需要你拍板改约定再做；其余可以直接排期。

1. **认证 + 限流 + TLS 放反向代理**（nginx/Caddy/云网关）：`--host 0.0.0.0` 裸奔的服务任何人都能烧 key。代码侧的并发上限只是兜底。
2. **结构化日志 + 请求关联 id** **[CLAUDE.md 冲突：不加日志框架]**：现在错误编号只打 stderr，没有时间戳、没有请求上下文、没法检索。最小方案是标准库 `logging` + JSON 格式，不引第三方。
3. **提示词注入缓解**：主诉挪出 system role、加"以下是数据不是指令"的分隔；要在 AutoDL 上用真实 LLM 跑 `tests/queries.txt` + `eval/run_eval.py` 回归确认 S1 质量不掉。
4. **依赖锁定**：在 AutoDL 上 `pip freeze > requirements.lock`，CI 和部署都装锁文件；`sentence-transformers`/`torch` 的 CUDA 版本只能在那台机器定。
5. **容器化** **[CLAUDE.md 未禁止，但沙箱没法验证]**：Dockerfile（CPU 版一份、CUDA 版一份）+ 健康检查 + 非 root 用户；模型文件挂卷，别打进镜像。
6. **安全响应头 / CSP**：要先把 `web/index.html` 里的内联 `<script>` 和 `onclick` 挪成外部文件，否则 `default-src 'self'` 会把页面打坏。
7. **优雅停机**：SIGTERM 时给在途问诊一个截止时间、拒绝新请求；现在 daemon 线程随进程直接死。
8. **每请求重复 IO** **[CLAUDE.md 冲突：不加缓存]**：`/api/trajectories` 重读整份 `cases.json`、`_build_manifest` 每次 sha256、`/api/graph` 每次重建载荷。按 mtime 记一份即可，但那就是缓存。
9. **观测**：Prometheus 指标（在途问诊数、LLM 调用次数/时延/失败率、拒绝率）。
10. **数据缺口**（HANDOFF §四已记）：`食积` 没有对应证候、症状节点近重复；这是模型效果问题不是工程问题，但落地前得补。
11. `offline/build_jieba_dict.py` 补测试；`consult()`/`run_physician()` 拆分（先钉 14 键契约再动）。

## 7. 发现了但刻意没改的

- **§1-10 提示词注入**：改动面大、需要真实 LLM 回归，这台机器做不了。
- **P3 那组每请求 IO**：修法就是缓存，CLAUDE.md 不让；量级也小（sha256 一个 1–2MB 文件几毫秒）。
- **`question_candidates` 的 `safety_relevant` 标记**：合并判据后 `followup.py` 不再读它，但它是给外部提问方（前端/患者模拟器）看的接口字段，测试也覆盖着，留。
- **`Neo4jStore` / `VLLMBackend` 占位类**：文档写明是给正式阶段留的接口形状，`get_backend()` 分支用到后者。
- **`.venv/bin/python -m ruff` 与系统 `ruff` 曾经规则集不同**：`ruff.toml` 固定后两者一致。
