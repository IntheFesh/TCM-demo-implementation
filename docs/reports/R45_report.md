# R45 · 面向三甲落地的工程基线（**本轮中止，未完成**）

> **这一轮没做完。** 用户在实施过程中插队调整了执行顺序：录制项目展示视频是
> 下一步的交付目标，所以 R45 停在「手上工作可提交」的状态，先做录制路径上的
> R47、R46，R45 的剩余项与 R48–R50 一并在录完之后补齐。
>
> 本文件记录**停在哪、已完成的部分实测了什么、剩下什么**。它不是完成报告。
> 完成 R45 时这一份会被整篇重写，而不是追加一节。

---

## 1. 已完成：启动自检（§6.6「启动自检」那一条）

`scripts/preflight_deploy.py`——起服务之前跑一遍，缺数据/缺配置时明确报哪一
项缺并退出非 0，不带病运行。

### 判据表（13 项，`blocking` 一列决定它是拦还是只告警）

| # | 检查 | 阻断 | 判据 |
|---|------|------|------|
| 1 | Python ≥ 3.10 | 是 | 项目用 `X \| None` 语法，3.9 直接 SyntaxError |
| 2 | 运行时依赖 | 是 | fastapi / uvicorn / pydantic / httpx 四个 import 得到 |
| 3 | 数据文件（7 个） | 是 | `graph.json`、`syndromes.jsonl`、`materia_medica.jsonl`、`formulary.jsonl`、`prescribing_patterns.jsonl`、`effect_synonyms.tsv`、`element_index.json` |
| 4 | 前端资产（7 项） | 是 | `cytoscape.min.js`、`dagre/dagre.min.js`、字体子集目录、`index.html`、`app.js`、`graph.js`、`app.css` |
| 5 | 目录可写 | 是 | `data/`（审计日志）、`data/cache/`（向量缓存） |
| 6 | 磁盘余量 | <500 MB 阻断 | 2048 MB 以下告警 |
| 7 | 端口空闲 | 是 | 真 `bind()` 一次，不看 `ss` 的输出猜 |
| 8 | 系统时钟 | 否 | 审计日志要跟病历系统对得上时间 |
| 9 | 模型后端配置 | 否 | 走 `get_backend().backend_id()`，不在这里复述一遍 `LLM_MODE` 的映射 |
| 10 | 后端可达 | 否 | `GET $LLM_BASE_URL/models`，`--skip-network` 可跳 |
| 11 | 回放 fixture | `LLM_MODE=replay` 时是 | 断网演示的前提 |
| 12 | 并发上限 | 否 | `MAX_CONCURRENT_CONSULTS`，R40 实测拐点在 4 |
| 13 | `EVAL_MODE` 必须关 | 是 | 它会让安全否决不中止——**生产环境开着等于拆了危重拦截** |
| 14 | 安全否决未被环境变量绕过 | 是 | 同上，另一条入口 |

退出码：`0` 全绿、`2` 有告警可起、`1` 有阻断项不可起。

### 本机实测（2026-09-18，沙箱，`--skip-network`）

```
✓ Python 版本 ≥ 3.10（项目用 `X | None` 语法）　3.11.15
✓ data/graph.json（知识图谱）　2494 KB
✓ data/standard/materia_medica.jsonl（本草本体）　1853 KB
✓ web/vendor/dagre/dagre.min.js（布局库（R42））　277 KB
✓ 磁盘余量（阻断 <500 MB，告警 <2048 MB）　4430 MB 可用
✓ 端口 8000 空闲　空闲
! 模型后端配置（LLM_MODE=api）　backend=api，但 LLM_API_KEY 是空的
— 模型后端可达（跳过：--skip-network）
✓ EVAL_MODE 必须关（它会让安全否决不中止）　EVAL_MODE='0'
✓ 安全否决没有被环境变量绕过　生效
有告警项：能起来，但要知道代价。   exit=2
```

沙箱里没有 `LLM_API_KEY`，所以第 9 项是告警而不是绿——**这是对的**：
它照实说了"能起来，但这台机器上起来之后问诊会失败"，没有为了让输出好看
把它算成通过。

### 实施中修掉的一个自己的 bug

初版写的是 `from core.llm import backend_name`，而 `core/llm.py` 里没有这个
函数——后端 id 是各后端类的 `backend_id()` 方法，由 `get_backend()` 分派。
这不是改个名字的事：如果这里自己按 `LLM_MODE` 再写一遍映射表，以后加一种
后端，自检会悄悄报一个错误的后端名，而自检恰恰是那个"没人会去核对它说得对
不对"的地方。改法是调 `get_backend().backend_id()`，让分派只有一处。

---

## 2. 未完成项清单（停在这里，录完视频后补）

按 §6 的六节列，逐条写明剩什么：

| §6 条目 | 状态 | 剩余工作 |
|---------|------|----------|
| 1 可靠性：降级路径 + `degraded` 字段 | **未做** | 响应里显式标注 `{what, why, since, impact}`，前端显著提示 |
| 1 可靠性：混沌测试 | **未做** | `tests/test_chaos.py` ≥18（六类故障 × 三个注入点） |
| 1 可靠性：幂等与重试 | **未做** | 同一请求重复提交不产生两条审计记录 |
| 2 可观测性：`trace_id` 贯穿 + 结构化日志 | **部分已有** | `trace_id` 已有，各段耗时进结构化日志未做 |
| 2 可观测性：`GET /api/health/detail` | **未做** | 本体/embedding/图谱/fixture 状态 + p50/p95/错误率 + 版本号 + 规则表版本 |
| 2 可观测性：Prometheus 文本导出 | **未做** | 零依赖手写文本格式（不引 prometheus_client） |
| 2 可观测性：日志分级轮转、留存 ≥6 个月 | **未做** | 等保要求，配置指引 |
| 3 可审计：哈希链扩展到完整推理轨迹 | **部分已有** | 现有审计链未覆盖取证动作、验证轨迹、修订 diff、规则表版本 |
| 3 可审计：按 `trace_id` 回放 | **未做** | |
| 3 可审计：`PII_REDACT=1` 脱敏 | **未做** | 姓名/身份证/手机号/住址/病历号，规则表可配置 |
| 3 可审计：JSON Lines + 校验和导出 | **未做** | |
| 4 安全边界：`CLINICAL_MODE=1` | **未做** | `unverifiable` 非空即不下发 |
| 4 安全边界：`data/standard/red_flags.jsonl` | **未做** | 危重清单可配置、带出处、变更留痕 |
| 4 安全边界：本院禁用药目录导入 | **未做** | |
| 5 等保三级接口预留 | **未做** | R48 展开 |
| 6 可部署：Dockerfile + compose | **未做** | |
| 6 可部署：`DATA_DIR` 外挂、无硬编码路径 | **未做** | |
| 6 可部署：**启动自检** | **已完成** | 见上一节 |
| 6 可部署：离线安装包 | **未做** | |
| 6 可部署：升级与回滚 | **未做** | |
| 6.1 五个测试文件（≥60 条） | **未做** | chaos/observability/audit_replay/clinical_mode/deployability |

另外本轮原计划但没动的交付物：`deploy/README.md`、`deploy/tcm-agent.service`、
`deploy/nginx.conf.example`（SSE 必须 `proxy_buffering off`，否则 R43 的流式
输出会被 nginx 憋成一次性吐出）、`deploy/CAPACITY.md`（R40 实测拐点 4：并发 4
时 9.03 rps，并发 16 掉到 5.31，每次问诊约 10 MB RSS）、`scripts/backup_state.py`。

---

## 3. 这一轮没有可报的性能数字

§12 要求每项优化带改前/改后实测。本轮**没有做任何优化**，只加了一个自检脚本，
所以没有这类数字可报——上面那段实测输出是自检脚本自己的产出，不是性能对比。
不给它编一个对照基准（「任何数字都必须带对照」那条铁律的反面用法：宁可说
没有，也不摆一个没有基准的数）。

## 4. 防幻觉约束计数

`grep -c "= Field(min_length=1" core/schemas.py` = **72**，本轮未动
`core/schemas.py`，改前改后同数。
