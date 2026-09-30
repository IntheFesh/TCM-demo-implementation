# 部署与运维

本文档说明如何把服务部署起来、如何配置模型后端与访问控制，以及常见问题的排查方法。
配置项的完整列表与默认值见 [`.env.example`](../.env.example)。

## 1. 部署前自检

```bash
python -m scripts.preflight_deploy                 # 全部检查
python -m scripts.preflight_deploy --skip-network  # 无外网环境跳过可达性检查
python -m scripts.preflight_deploy --json          # 机器可读输出
```

检查项包括 Python 版本、运行时依赖、数据文件、前端静态资源（`web/vendor/` 下的
cytoscape、dagre 与字体子集）、`data/` 目录可写、磁盘余量、端口占用、系统时钟、
模型后端配置与可达性、回放 fixture、并发上限，以及 `EVAL_MODE` 是否关闭。
退出码：`0` 全部通过；`1` 有阻断项未通过（不要上线）；`2` 只有警告项未通过。

`python -m scripts.preflight_runtime` 检查当前进程环境是否处于预期配置
（例如没有调试时遗留的 `RETRIEVER_MODE` / `EVAL_MODE` 等环境变量），`--strict` 时
提醒项也计为失败。

## 2. 启动

```bash
uvicorn api.main:app --host 127.0.0.1 --port 8000
# 或一键脚本：建虚拟环境、安装依赖、按需抽取医案并启动
./run.sh
```

- **产品形态由 `PRODUCT_MODE` 决定**（唯一分派点 `core/product_mode.py`）。默认 `1`：
  根路径跳转到 `/app/product/index.html`，角色为医师 / 学生 / 患者。设为 `0` 时根路径
  跳转到研究界面 `/app/index.html`，额外提供研究者角色、检索模式选择、运行清单与
  用量看板（`/api/usage`、`/api/usage/validate-key` 在产品形态下返回 404）。
- **预热**：服务启动后立即开始监听，加载 embedding 模型、编码语料等预热在后台线程中进行；
  预热完成前 `GET /health` 返回 503 与进度。`WARMUP_TIMEOUT_SECONDS`（默认 120 秒）只是
  压测与冒烟脚本等待就绪的时间预算，不影响服务何时开始监听。
- **健康检查**：`GET /health/live` 是存活探针（进程在运行即返回 200）；`GET /health`
  是就绪探针，返回 `status`（`ok` / `warming`）、医家注册表、回放模式信息等。
- 静态资源挂在 `/app`：`vendor/` 下的第三方文件带长期缓存头，其余前端文件每次向服务端
  验证（`no-cache`），升级后浏览器不会继续使用旧版 JS。

## 3. 模型后端

`LLM_MODE` 选择后端（`core/llm.py::get_backend`）。每个后端如实报告自己的
`model_name()` 与 `backend_id()`，写进每次问诊的 `manifest`；非默认后端还会带上
`comparability_warning`，提示其结果不可与默认后端直接比较。

| `LLM_MODE` | 用途 | 需要的配置 |
|---|---|---|
| `api`（默认） | OpenAI 兼容的云端 API | `LLM_API_KEY`、`LLM_BASE_URL`（默认 `https://api.deepseek.com`）、`LLM_MODEL`（默认 `deepseek-v4-pro`） |
| `local` | 本地 vLLM 的 OpenAI 兼容 server | `LLM_MODEL_PATH`、`LLM_MODEL`（server 的 served name）、`LLM_BASE_URL`（默认 `http://127.0.0.1:8000/v1`）、可选 `LORA_DIR` |
| `local_inproc` | 进程内 vLLM，适合批量评测 | `LLM_MODEL_PATH`、可选 `LORA_DIR`、`VLLM_MAX_MODEL_LEN`、`VLLM_GPU_MEMORY_UTILIZATION` |
| `replay` | 录制回放：零成本、断网可用、结果可复现 | `REPLAY_FIXTURES_DIR`（默认仓库根的 `fixtures/`） |

### 本地模型（vLLM）

```bash
export LLM_MODEL_PATH=/path/to/models/Qwen2.5-1.5B-Instruct
export LORA_DIR=/path/to/lora            # 可选：按医家挂 adapter，目录为 $LORA_DIR/<physician_id>/
bash scripts/start_vllm.sh               # 在另一个终端启动 server

export LLM_MODE=local LLM_MODEL=tcm-local
python -m scripts.verify_local_backend   # 退出码 0 = 后端可用
python -m scripts.verify_local_backend --show-prompt-budget   # 由真实 prompt 估算 --max-model-len
```

本地后端通过 vLLM 的 guided decoding 约束输出符合 schema（参数名由
`VLLM_GUIDED_JSON_KEY` 配置，默认 `guided_json`），并按医家 id 切换 LoRA adapter。
`LORA_DIR` 已设置但某位医家的 adapter 目录不存在时直接报错，不会静默退回基座模型。
`verify_local_backend` 会检查：一次调用得到通过校验的输出、这次调用没有触发重试
（重试说明 guided decoding 的参数名没有生效）、每位已注册医家都有 adapter 目录。

### 录制回放

```bash
python -m scripts.record_fixtures --dry-run    # 查看录制清单与预估调用数，不发请求
python -m scripts.record_fixtures              # 用真实后端录制（需要 key）
python -m scripts.verify_replay                # 退出码 0 = 回放与录制逐条一致
LLM_MODE=replay uvicorn api.main:app --port 8000
```

- fixture 按 `(schema 名, sha256(发给模型的 system 文本))` 索引，未命中时抛出 `LLMError`
  并在错误信息里列出可能的原因（最常见的是 `USE_REACT` / `RETRIEVER_MODE` 与录制时不同），
  不会退回真实 API。
- 开启与关闭 ReAct 的 prompt 不同，需要各录一遍；角色只影响响应裁剪，不需要分别录制。
- 追问的回答会改变后续 prompt，回放场景建议 `FAST_MODE=1`（不追问）。
- 回放时 `manifest.backend` 为 `replay` 并带 `replayed_from`；`/health` 与响应中的
  `replay_mode` 字段非空，页面顶部显示「回放模式」说明，表明结果来自录制而非实时调用。

录制清单（`scripts/record_fixtures.py` 的 `build_plan()`）包含下面三条主诉。回放按主诉原文
索引，差一个标点也会未命中，所以回放模式下请直接复制使用：

```
A: 胃脘胀痛，食后加重，嗳气泛酸，每因情志不畅而发，纳差，舌淡红苔薄白，脉弦。
B: 胸闷胸痛，冷汗
C: 胃脘疼痛数月，近日解黑色柏油样便，头晕心慌，面色苍白，倦怠乏力，舌淡，脉细数。
```

A 与 C 是 `tests/queries.txt` 的第 1、10 条（C 含黑便等危急征象，会在 S2 之前被安全否决）；
B 是患者模式导诊场景的主诉（`TRIAGE_COMPLAINT`）。

## 4. 并发与超时

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAX_CONCURRENT_CONSULTS` | 4 | 同时进行的问诊数上限（`/api/consult` 与 `/api/consult/stream` 合计），满了立即返回 503 + `Retry-After` |
| `LLM_MAX_INFLIGHT` | 6 | 进程内同时在途的 LLM 请求数上限。与 `MAX_CONCURRENT_CONSULTS` 是两件事：多个问诊槽 × 每次问诊内部的并发会同时打到模型服务上 |
| `LLM_TIMEOUT_SECONDS` | 按后端 | 单次调用的读超时；另有一层整次调用的墙钟兜底，超时后按既有重试逻辑重试 |
| `LLM_MAX_TOKENS` | 按模型 | 单次输出上限；推理模型的上限同时覆盖不可见的推理 token |

## 5. 公开部署

默认配置不适合直接对公网开放：`LLM_MODE=api` 加上部署者自己的 key，意味着任何访问者都在
消耗部署者的额度。下面三层访问控制可以叠加使用（设计见 docs/DESIGN.md §5.1）：

| 层 | 作用 | 默认 |
|---|---|---|
| **BYOK** | 访问者填自己的 key，存在浏览器 `sessionStorage`，服务端不落盘 | 不计入站点额度 |
| **共享额度** | 不填 key 的访问者使用站点额度 | 每 IP 每天 5 次问诊、全站每天 200 次问诊（按调用数折算，见下表） |
| **用量看板** | `/api/usage` + 顶栏「今日约剩 N 次」 | 80% 预警，100% 降级 |

四条硬要求：

1. **请求前拦截**：问诊开始前按"这次至少会花几次调用"预占额度，额度不足直接拦下，
   被拦的请求不产生费用；结束后按 `manifest.llm_calls` 的真实调用数结算。
2. **按 `llm_calls` 计量，不是按请求数**：开启 ReAct 等选项时一次问诊的调用数成倍增加，
   按请求数计量会让这类请求少计费用。
3. **超额降级到回放而不是报错**（需要已录制的 fixture）：访问者仍能看到录制主诉的完整
   结果，顶栏下方一行说明使用 `--surface-2` 底色，**不是警告色**——降级不是错误。
4. **BYOK 不做通用代理**：访问者的 key 只用于本次问诊对模型服务的调用。

| 变量 | 默认 | 说明 |
|---|---|---|
| `QUOTA_PER_IP_DAILY_CALLS` | `15` | 每个 IP 每天的**模型调用**上限，默认值 = `calls_per_consult() × 5`（产品默认配置下一次问诊 3 次调用）。调整 `S3_MODE` / `S3_BEST_OF_N` 时默认值随之变化 |
| `QUOTA_GLOBAL_DAILY_CALLS` | `600` | 全站每天的模型调用上限，默认值 = `calls_per_consult() × 200` |
| `QUOTA_MAX_TRACKED_IPS` | `5000` | 额度账本最多记录的 IP 数，防止伪造大量 IP 撑大进程内存 |
| `TRUSTED_PROXY_HOPS` | `0` | 默认完全不读 `X-Forwarded-For`：直接信任 XFF 等于让任何人加一个请求头就换一个"IP"。只有部署在自己的反向代理之后时才设成代理层数（nginx 一层就是 1），服务端从右侧数第 N 跳取客户端地址；最左侧那一跳由客户端自己填写，不可信 |
| `FORCE_REPLAY` | 未设 | 设为 `1` 时所有请求强制走录制回放：零成本、可离线、每次结果一致 |
| `LLM_MAX_INFLIGHT` | `6` | 见上一节；公开部署时它决定同时打到模型服务的请求数，设得过大容易触发服务端限流 |

额度账本保存在进程内存中，服务重启后重置。

### nginx 反向代理示例

```nginx
server {
    listen 443 ssl;
    server_name tcm.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        # SSE 需要：不缓冲、较长的读超时、不降级到 HTTP/1.0
        proxy_buffering off;
        proxy_read_timeout 600s;
        proxy_set_header Connection "";

        proxy_set_header Host $host;
        # 配合服务端 TRUSTED_PROXY_HOPS=1 使用：nginx 把真实客户端 IP 追加在 XFF 最右侧。
        # 只加这一行而不设 TRUSTED_PROXY_HOPS，服务端仍然不读 XFF，所有人会被算成同一个 IP。
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

对应的服务端启动命令：

```bash
TRUSTED_PROXY_HOPS=1 uvicorn api.main:app --host 127.0.0.1 --port 8000
```

### BYOK 的安全边界

页面上对访问者的说明（原文）：

> 你的 key 只在本次请求中转发给 DeepSeek，不会存储在服务器上。
> 关闭标签页后即清除。

实现：key 存在 `sessionStorage`（关闭标签页即清除），随请求放在 `X-LLM-Key` 请求头里，
**服务端不落盘、不进日志、不进 manifest**。入口在顶栏右侧，默认收起成一行小字，不做成弹窗。
保存前先调用 `/api/usage/validate-key` 校验：它请求模型服务的余额查询接口
（`GET /user/balance`），不消耗 token。

## 6. HIS 集成接口

`POST /api/integration/consult` 与 `GET /api/integration/emr/{record_id}` 供医院信息系统调用，
OpenAPI 文档由 FastAPI 自动生成（`/docs`）。鉴权规则（`core/integration_auth.py`）：

- 请求头 `X-API-Key` 必须等于 `HIS_API_KEYS`（逗号分隔）中的某一个；
- 设置了 `HIS_IP_ALLOWLIST`（逗号分隔）时，客户端 IP 还必须在白名单内；
- 没有配置 `HIS_API_KEYS` 时接口默认拒绝。鉴权失败统一返回 401，具体原因只写服务端日志。

## 7. 运行时数据

以下文件在运行中生成，均不进入版本控制：

| 文件 | 内容 |
|---|---|
| `data/audit.jsonl` | 处方导出审计日志（哈希链，见下） |
| `data/consult_history.jsonl`、`data/favorites.jsonl` | 问诊历史、收藏与批注 |
| `data/emr_drafts.jsonl` | 病历文书草稿及其修改记录 |
| `data/preferences.jsonl` | 医师的用药习惯与模板 |
| `data/cache/` | 语料向量的磁盘缓存（可随时删除，下次启动重建） |

审计日志每条记录带前一条的 sha256，任何一条被改动都能校验出来：

```bash
python -c "from core.audit import verify_audit_chain; print(verify_audit_chain())"
```

以下文件是离线管线的生成物，部署时需要一并准备（缺失时相应功能返回空或提示不可用）：

| 文件 | 生成命令 | 用途 |
|---|---|---|
| `data/graph.json` | `python -m offline.build_graph --all` | 知识图谱、追问候选、图谱浏览 |
| `data/element_index.json` | 同上（`--all` 会一并生成） | `graph` 检索模式、证素轨迹 |
| `data/jieba_dict.txt` | `python -m offline.build_jieba_dict` | `bm25` 检索的分词词典（缺失时退回 jieba 默认词典） |
| `data/case_triples.jsonl` | `python -m offline.extract_case_triples`（需要 LLM） | ReAct 的医案三元组查询工具 |

`offline/build_graph.py` 之后的 `offline/graph_stats.py` 会把医家层权重
`weight_by_physician` **写回**图谱文件；只运行建图而不运行它不会报错，但追问的后验
计算会退化为均匀权重。所以建图请使用 `--all`。

## 8. 数据与评测流水线

`scripts/run_pipeline.sh` 按依赖顺序串起数据准备、评测与性能基准的全部步骤，
每一步的退出码记录在状态文件中，可以从中断处继续：

```bash
bash scripts/run_pipeline.sh --dry-run   # 列出每一步与预估调用数，不执行
bash scripts/run_pipeline.sh --status    # 查看每一步上次的结果
bash scripts/run_pipeline.sh --resume    # 从第一个未成功的步骤继续
bash scripts/run_pipeline.sh --only N    # 只运行第 N 步
```

## 9. 常见问题排查

长时间运行的任务通过 `core/progress.py` 向 stderr 输出进度；即使一项都没有完成，也会
定期输出心跳。**心跳在、进度不动**，说明某一次模型调用在等待，墙钟上限到了会自动重试；
**连心跳都没有**，才说明进程卡住或已退出。

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 每次调用都返回空内容、校验失败、重试三次后 `LLMError` | `LLM_MODEL` 在服务端不可用（部分服务对已下线的模型返回 200 + 空响应体） | 查询 `$LLM_BASE_URL/models` 确认模型名，修改 `LLM_MODEL` |
| `LLMTruncatedError` | 输出撞到 `max_tokens` | 调大 `LLM_MAX_TOKENS`；推理模型的上限包含推理 token |
| `LLMCallTimeout` | 单次调用超过墙钟上限 | 会自动重试，三次都超时才失败；云端反复超时时检查网络，必要时调大 `LLM_TIMEOUT_SECONDS`。本地进程内模型首次调用需要加载权重，耗时较长属正常 |
| `graph` 检索模式返回 `retrieval_error` | `data/element_index.json` 不存在 | `python -m offline.build_graph --all` |
| `dense` / `hybrid` 模式报 `retrieval_error` | 无法从 Hugging Face Hub 下载 embedding 模型 | 在可以联网的环境预先下载模型；检索层不会静默降级为别的模式 |
| 追问的问题缺乏区分度 | 建图后没有运行 `graph_stats`，权重未写回 | `python -m offline.build_graph --all` |
| 回放时 `LLMError: 未命中` | 主诉、`USE_REACT`、`RETRIEVER_MODE` 等与录制时不同 | 按错误信息中的 prompt 摘要与环境变量提示排查；需要时重新录制 |
| vLLM 不认识 `guided_json` | 不同 vLLM 版本的参数名不同 | 设置 `VLLM_GUIDED_JSON_KEY`，无需改代码 |
| vLLM 显存不足 | 显存占用比例或最大长度过大 | 降低 `scripts/start_vllm.sh` 中的 `--gpu-memory-utilization`、`--max-num-seqs` 或 `--max-model-len` |
| 评测或服务结果与预期配置不符 | 环境中残留了 `RETRIEVER_MODE` / `USE_REACT` / `EVAL_MODE` 等变量 | `python -m scripts.preflight_runtime` 检查并清除 |
| `collect_results --check` 报数字不一致 | 重新运行了评测，但文档中的凭据记号没有更新 | 按报错中「文件里实际是 X」更新 `eval/RESULTS.md` / `README.md`；`.md` 报告与 `.json` 不一致时用 `--rerender` |
| 药理层抽取拒绝某个输入文件 | 输入不是本草 / 方剂参考文献（例如医案） | 药理层只从参考文献抽取性味、归经、功效、用量；确有需要时显式加 `--include-out-of-scope` |
| `EVAL_MODE=1` 出现在对外服务的机器上 | 调试时设置未清除 | 立即关闭：它会让直接调用 `consult()` 的链路在命中危重症状后继续推理 |
