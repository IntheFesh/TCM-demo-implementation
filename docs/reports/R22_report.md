# R22 报告：best-of-N 与 reasoning_effort

**基线** `972a30d`（R23 结束）。顺序是定死的：R22 排在 R23 后面，因为 best-of-N
的打分器**就是 R23 的 `score_formula`**——不新建第二把尺是这一轮最硬的一条约束。

一句话：S3 从「采一次」变成「并发采 N 次、按分挑一张」（默认 N=3），
`reasoning_effort` 从写死的 `high` 变成可配 + 按检索方式取默认（full_context → `max`）。
两个旋钮都**直接决定钱**，所以这一轮一半的工作量在"让账对得上"。

---

## 一、做了什么（逐项 ✅/❌）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 1 | `S3_REASONING_EFFORT`（`low`/`medium`/`high`/`max`），默认按检索方式取 | ✅ | `core/llm.py::s3_reasoning_effort`；full_context → `max`、top3 → `high` |
| 2 | `effort=max` 单独一档 `max_tokens`（65536） | ✅ | `_default_max_tokens(thinking, reasoning_effort)`，云端和**本地后端都走同一档** |
| 3 | `S3_BEST_OF_N`，默认 **3**；`1` = 关掉（走回 R21 那条路径） | ✅ | `core/llm.py::s3_best_of_n` |
| 4 | 采样并发，上限沿用 `LLM_MAX_INFLIGHT`（不设第二个闸门） | ✅ | `core/chain.py::_best_of_n_s3`，每个 worker 跑在 `copy_context().run` 里 |
| 5 | 打分用 R23 的 `score_formula`，**不新建第二把尺** | ✅ | `_score_candidate` 调 `check_formula(...).score` |
| 6 | `candidates_scored`：每次采样的分/方名/建议类别/选中标记 | ✅ | 每位医家的结果里，另带 `best_of_n` |
| 7 | 平手取下标最小；N 次里失败几次仍出结果，全灭才抛 | ✅ | 失败那次留 `score: null` + `error`，不从列表消失 |
| 8 | 安全层重开仍在 best-of-N **之后** | ✅ | 顺序没动；`candidates_scored` 记挑选时的分、`formula_score` 记最终那张方的分 |
| 9 | `CALLS_PER_CONSULT` → `2 + 医家数 × N` | ✅ | `core/usage.py::calls_per_consult()`，`estimate_calls()` 也改成问它 |
| 10 | `manifest` 记 `best_of_n` / `reasoning_effort` | ✅ | 两项都让数字不可比，所以是一等字段 |
| 11 | `FAST_MODE` 下 N 降到 1（**第四处降级**） | ✅ | `s3_best_of_n` 问 `fast_mode_enabled()`；那个函数的清单从"三处"改成"四处" |
| 12 | `tests/test_best_of_n.py` ≥ 10 条 | ✅ | **13** 条 |
| 13 | `tests/test_reasoning_effort.py` ≥ 4 条 | ✅ | **8** 条 |

### 附带

- ✅ **修掉一个真 bug**：`manifest.llm_calls` 漏算 N−1 次采样（详见第三、七节）。
- ✅ `bench_consult` 的每次跑和汇总都带 `best_of_n` / `reasoning_effort`。
- ✅ README 新增「best-of-N」一节 + 环境变量表两行；额度默认值 25/1000 → **55/2200**。
- ✅ `.env.example` 两个新旋钮 + 额度那段说明"写死就不会跟着 N 变"。
- ✅ `data/SOURCES.md` 第 66 条，七个编号点。
- ✅ **每轮快照加 `LATEST` 指针**（第五节第 4 条）：轮次顺序是人定的
  （R21 → R23 → R22），按轮次号大小推"最新"会把 R23 当成最新。

**❌ 没有一项。**

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **2853** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R22` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **37**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑，不落文件 |
| 凭据核对 | 退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **16** 种全过 | `python -m scripts.screenshot_states` |

带凭据记号的那几个（一行一个），引的是这一轮的不可变快照 `eval/bench/rounds/R22.json`：

- 测试 passed **2853** —— `bench/rounds/R22.json:round.R22.pytest_passed=2853`
- 测试 skipped **10** —— `bench/rounds/R22.json:round.R22.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R22.json:round.R22.pytest_failed=0`
- 全量测试墙钟 **90.8** 秒 —— `bench/rounds/R22.json:round.R22.pytest_wall_s=90.8`
- Playwright 通过 **16** 种 —— `bench/rounds/R22.json:round.R22.playwright_states_passed=16`

**`min_length=1` 37 处，一处没动**：这一轮没有新增 pydantic 字段。

**新增测试 24 条**（R23 的 2829 → 本轮 2853）：

| 文件 | 条数 | 要求 | 盯的是什么 |
|---|---|---|---|
| `tests/test_best_of_n.py` | **13** | ≥ 10 | 采够 N 次、按共享尺挑、平手取下标最小、部分失败仍出结果、全灭才抛、`llm_calls` 计全、安全重开在挑选之后、FAST_MODE 降到 1 |
| `tests/test_reasoning_effort.py` | **8** | ≥ 4 | 三档 `max_tokens`、`LLM_MAX_TOKENS` 仍压过档位、云端真的发出 effort、本地后端同档、截断报错带 effort |
| `tests/test_thinking_control.py` | +3 | —— | 两系各自的默认档、显式指定优先、拼错一档要吼 |

---

## 三、每项改动的验证方式与结果

**打分不新建第二把尺。** `test_the_scores_come_from_the_shared_ruler_not_a_second_one`
把 `candidates_scored` 里每一行的分跟 `check_formula(...).score` 逐个比。这条抓的是
「best-of-N 自己又写了一把尺」——那种退化下排序看着也合理，要等到有人改了权重、
发现"医生看到的建议严重度"和"排序结果"对不上时才暴露。

**平手规则是契约。** 三次同分时取下标最小的那次。不定的挑选会让 fixture 回放
（R3 的逐字节一致）和 ε（噪声地板）双双不可复现。

**部分失败。** N 次里一次 429 不该让整位医家失败。失败那次在 `candidates_scored`
里留一行 `score: null` + `error`，**不从列表里消失**——消失的话 N 对不上，
而"为什么这次只有两条"没人能回答。全灭才抛，抛的是真实异常（返回一个"没有候选方"
的假结果会在下游变成 IndexError，离根因十几帧远）。

**安全重开在挑选之后。** 测试让三次采样全是十八反禁忌方、第四次（重开）给干净方，
断言 `revised is True`、`candidates_scored` 三行都是 0.0、而 `formula_score` 是 1.0。
顺序反过来（先拦截再挑）会让"被拦掉的恰好是分最高的那张"静默消失。

**effort 的三档 `max_tokens`。** 关思考 8192 / 开思考 32768 / `effort=max` 65536。
max 单独一档的理由：max_tokens 盖住 reasoning + 可见输出两部分，而 max 档的推理
本身就能吃掉几万 token——沿用 32768 的后果是**推理没写完就撞上限**，
表现为一个在末尾处解析失败的 JSON（白烧一次调用才看出来）。本地后端的
`_sampling_params` 也收这个参数：不跟的话同一段代码在两个后端上会在不同长度处
被截断，那种差异极难归因。

---

## 四、成本与规模数字对照表

### 4.1 一次问诊的调用数（这一轮最重要的一张表）

| 配置 | `calls_per_consult()` | 对照 |
|---|---|---|
| R21 及之前（N 不存在） | **5** = 2 + 3×1 | 基准 |
| R22 默认（N=3） | **11** = 2 + 3×3 | 是基准的 2.2 倍 |
| `FAST_MODE=1`（N→1） | **5** | 跟 R21 一样——FAST_MODE 的承诺没打折 |
| 开 ReAct（N=3） | **26** = 11 + 3×5 | ReAct 仍然明显更贵，但倍数被 N 摊薄了（从 3× 降到 2.4×） |

额度默认值跟着变：每 IP `calls_per_consult() * 5` = **55**（仍然是"每天 5 次问诊"），
全站 `* 200` = **2200**。**这两个数是算出来的**，把 `S3_BEST_OF_N` 调回 1 它们
自动变回 25 / 1000。

钱的对照（按 R21 记的官方价、full_context 缓存命中）：一次问诊 3 位医家 × 3 次采样，
输入靠缓存命中仍然 ≈¥0.17（采样共用同一段前缀，命中的是同一份缓存），
**涨的是输出侧**——9 次 S3 的输出 token 是 3 次的三倍。所以 best-of-N 在
full_context 下比在 top3 下划算得多，这也是两个旋钮的默认值互相绑定的理由。

### 4.2 沙盒性能（R19 → R21 → R23 → R22，同一个脚本）

| # | 指标 | R19 | R21 | R23 | R22 | 该怎么读 |
|---|---|---|---|---|---|---|
| P1 | 冷进程 `import api.main` | 0.517 | 0.431 | 0.542 | **0.521** s | 四轮来回摆 0.1s，代码一路在变多 |
| P2/P3 | `/health` p50 五位 / 三位 | 12.237/12.554 | 8.481/8.603 | 13.14/12.49 | **12.782/13.025** | **六次测量方向三比三** → 这个开销量不出来 |
| P4 | 全量测试 条数 / 墙钟 | 2693/89.9 | 2783/66.2 | 2829/89.8 | **2853/90.8** s | 条数单调增；墙钟在 66~91 s 之间摆 |
| P5 | Playwright | 16/38.4 | 16/37.1 | 16/37.7 | **16/38.2** s | 三轮没动前端，测的是"没有回归" |

**P2/P3 现在有六次测量、方向三比三**，这是这个项目最干净的一个"测不出来"的例子。
P1 也一样：R21 那次 0.431 是四次里最快的一次，而它恰好是**新增了一个 import** 的
那一轮——如果当初把它写成"这一轮让启动变快了"，后面三轮都得编解释。

---

## 五、改了哪些测试断言（20 条，三类）

**第一类：把 `S3_BEST_OF_N` 钉成 1（9 条）。** 它们数的是别的东西——
S1 有没有跑两次、ReAct 几步、追问多花几次 S2、安全重开了没有、医家之间并发没并发。
采样次数会改掉它们的分子，钉成 1 让每条测试只测它自己那件事。每条都在代码里
写了一句为什么。

| 文件 | 测试 |
|---|---|
| `test_chain.py` | 共享阶段只跑一次 / 检索为空换 schema / prompt 里有 raw_excerpt / 安全重开三条 / ReAct 两条 / 无提问渠道 / 追问多花一次 S2 / 全否认不重跑 S2 |
| `test_chain_parallel.py` | 三位医家真并发 / max_workers 跟注册表 / 跨 worker 调用数汇总 |
| `test_eval_mode.py` | EVAL_MODE 旁路两条 |
| `test_bench_scripts.py` | 并发比值那条（best-of-N 在医家内部又开一层并发，两层叠加后 wall/sum 测的就不是医家级并发了） |

**第二类：改成从单一实现取期望值（6 条）。** 写死一个字面量就是把公式抄到第二处。

| 测试 | 原来 | 现在 |
|---|---|---|
| `test_a_valid_fake_run_makes_the_expected_number_of_calls`（原名 `..._exactly_five_calls`） | `== 2 + 3 == 5` | `== calls_per_consult(len(PHYSICIANS))` |
| `test_fake_backend_installs_synthetic_cases_when_the_repo_has_none` | `== 5` | 同上 |
| `test_every_call_records_the_arguments_it_actually_received` | `reasoning_effort == "high"` | `== thinking_for("s3")["reasoning_effort"]` |
| `test_the_quota_defaults_in_the_doc_match_the_code` | `CALLS_PER_CONSULT * 5` | `calls_per_consult() * 5` |
| `test_snapshot_converts_calls_into_something_a_visitor_can_read` | `usage.CALLS_PER_CONSULT` | `usage.calls_per_consult()` |
| `test_usage_endpoint_reports_the_ledger_without_spending_anything` | 同上 | 同上 |

**第三类：真正的契约变更（5 条）**，docstring 里写明变了什么、为什么。

| 测试 | 变更 |
|---|---|
| `test_s3_keeps_thinking_on_by_default` | 默认 effort 从写死 `high` 改成按检索方式（full_context → `max`） |
| `test_truncation_error_names_the_default_it_actually_used` | 报错文案多带 `reasoning_effort`——上限分了三档，只报 thinking 定位不到 |
| `test_openai_compat_backend_records_usage_from_the_response` | 替身 `_default_max_tokens` 改收 `*args`（方法多了一个参数） |
| `test_estimate_counts_react_as_several_times_the_baseline` | `plain == 5` → `== calls_per_consult(3)`；ReAct 倍数判据从 `≥3×` 放到 `≥1.5×`（N 变大摊薄了倍数，**这是事实不是放松**） |
| `test_fast_mode_integration_cuts_llm_calls` | 16/8 → **20/8**。正常模式涨、FAST_MODE 不动——**变化方向本身就是判据** |

最后那条的原 docstring 写着「这两个数字任何一个变了都要先想清楚为什么，
不要直接改期望值」。这一轮正是它预期的情况，所以新 docstring 把"为什么"写在了
原话旁边，并加了两条直接判据（`fake_f.calls.count("S3Syndrome") == 2` /
`fake_n... == 6`）。

**第 4 条附带的机制变更：`eval/bench/rounds/LATEST` 指针。** R23 引入每轮快照时，
"最新那一轮"是按轮次号大小推的。而这个系列的顺序是人定的
（R21 → R23 → R22 → R24…，因为 R22 依赖 R23 的打分器），按数字排会把 R23
当成最新——于是"最新快照要等于当前测量"这条判据会拿一份已经封版的快照去比。
指针文件由 `bench_sandbox --round` 同一次运行落盘，所以它跟快照不会分叉。

---

## 六、无法完成项（⏳ 上机，不是 ❌）

| # | 要跑的 | 命令 | 判据 |
|---|---|---|---|
| 1 | **`score_formula` 在真实分布上有没有分辨力** | `python -m scripts.bench_consult --backend real --repeat 2`，看 `candidates_scored` | N 张方的分**要能分开**。如果 3 张全是 1.0，这把尺在真实分布上没有分辨力——那时正确的反应是**加规则**（缺引经/重复两条要真实本草表才跑得起来），不是调权重去凑出差异 |
| 2 | best-of-N 到底提升了什么 | 同一批主诉各跑 `S3_BEST_OF_N=1` 和 `=3`，比 SDT 分与 MES 盲评 | **必须跟 ε 比**：分数差落在噪声地板里就是"测不出来"，不是"提升了" |
| 3 | `effort=max` 的耗时与 token 代价 | `S3_REASONING_EFFORT=high` 与 `max` 各跑一次 bench | 看 `usage_by_schema.S3*.reasoning_tokens` 的 mean/max。**两套数不可比、并列报**（同 S3_THINKING 那条） |
| 4 | 一次问诊 11 次调用的真实钱 | 看 `/api/usage` 的 `tokens_today` | ≤ ¥0.5（第二次起）。采样共用同一段前缀，所以涨的应该只有输出侧——**如果输入侧也涨了三倍，说明前缀没命中，要回去查 R21 那条链** |

沙盒里做不了的原因只有一个：**这四项都要真实 LLM 采样**。

---

## 七、发现但未动 / 发现并当场修了

**1. `manifest.llm_calls` 漏算 N−1 次采样（已修，被既有测试抓到）。**
`_build_manifest` 原来写 `len(results)`（每位医家一次 S3）。采 N 次之后真实调用是
`n × N`，而 **manifest 是额度结算的依据**——漏算的表现是账本持续少扣，
**而少扣不会报错**。抓到它的是 R10 写的那条判据：
`llm_calls == len(calls)`（manifest 自己数的 vs 观测层数的，两个独立来源）。
修法用每位医家**自己报的**采样条数（`len(r["candidates_scored"])`），
不是全局 `s3_best_of_n()`：中途改环境变量、或某位医家部分采样失败时，
全局那个数跟实际发生的次数不一致。

**这条值得单独记的理由**：R10 那条测试当时是为了抓"recorder 套娃多记"写的，
两个独立来源对账这个形状在 12 轮之后抓到了一个完全不同的 bug。

**2. 留着没动：`candidates_scored` 里不带各次采样的完整方药。**
只记分、方名、建议类别、选中标记。带完整方药的话响应会大三倍，而"没被选中的那两张
方具体开了什么"目前没有消费方（前端不展示，R24 也没打算展示）。要做应该等到
有人真的要看"三张方差在哪"，那时它是一个新功能而不是一个字段。

**3. 留着没动：采样之间没有多样性激励。** N 次采样用的是同一个 prompt、同一个
temperature（思考模式下 temperature 不生效），所以三次的差异全部来自模型自身的
随机性。加"请给出与上次不同的思路"这类提示会让 N 次之间不独立，
而 best-of-N 的前提正是独立采样——要动这里得先想清楚要的是"独立采样取最优"
还是"多样性搜索"，那是两个不同的东西。

---

## 八、13 条自查

| # | 自查项 | 结果 |
|---|---|---|
| 1 | prompt 模板只用 `string.Template` | ✅ 本轮没动模板；采样 N 次用的是同一个 system prompt（一次渲染，N 次复用） |
| 2 | LLM 输出全部用 pydantic 承接 | ✅ 每次采样都是 `S3Syndrome`/`S3SyndromeUnreferenced`；`candidates_scored` 是规则算出来的汇总行，不是模型输出 |
| 3 | `Field(min_length=1)` 没有放松 | ✅ **37** 处，一处没动，本轮没有新增 pydantic 字段 |
| 4 | 新 `.jsonl` 一律放 `data/standard/` | ✅ 本轮没有新数据文件（`eval/bench/rounds/*.json` 是量出来的产物，不是语料） |
| 5 | 加载模型/大文件的对象惰性初始化 | ✅ 本轮没有新的重对象；`s3_reasoning_effort` 和 `s3_best_of_n` 里的 import 都是函数级（避免把"检索"和"追问"倒灌进"调模型"） |
| 6 | S1 全局只跑一次 | ✅ best-of-N 只在 S3 这一步采样；`test_shared_stages_run_once...` 仍然钉着 S1 一次 |
| 7 | 任何数字旁边必须有对照基准 | ✅ 第四节 4.1 每一行都有对照（5 / 11 / 5 / 26）；4.2 六次测量并列 |
| 8 | 同一概念只有一处实现 | ✅ 打分尺 `score_formula` 一处、折算系数 `calls_per_consult()` 一处（`estimate_calls` 也问它）、并发上限只有 `LLM_MAX_INFLIGHT`、effort 取值只有 `s3_reasoning_effort`、FAST_MODE 判定只有 `fast_mode_enabled`（清单已更新为四处） |
| 9 | 标识符在边界过 `resolve_*_id` | ✅ 采样把 `physician`（id）透传给后端选 LoRA，没有新的标识符入口 |
| 10 | 安全否决在 S2 之前 | ✅ 没动；输出侧的安全重开仍在 best-of-N 之后（有专门测试） |
| 11 | 追问的回答先过 `check_safety` | ✅ 没动追问链路 |
| 12 | 评测代码放 `eval/`，`tests/` 不需要网络和 key | ✅ 两个新文件都用假后端；`test_the_local_backend_picks_the_same_tier` 用 `inspect.signature`，vllm 装不上的机器也能跑 |
| 13 | 改了按注册表/图层循环渲染的前端 → 跑 Playwright | ✅ 本轮没动前端，仍跑了一次：**16 种全过**。⚠ R24 要渲染 `candidates_scored`，那一轮必须再跑 |

---

## 九、下一轮（R24）前置条件

R24 是前端八项（自绘 select、首屏题记、离线字体子集化、对照带 + SVG 斜纹、
四级列层次 + 君臣佐使两列密排、去卡片化、双环图谱浏览器、顶栏折叠 +
**R23 建议渲染** + **R21 token 面板**）。前置条件：

- ✅ R23 的 `advice` / `advice_skipped` / `formula_score` 已在响应里，患者角色已裁剪。
- ✅ R21 的 `prefix_tokens_by_section` / `cache_hit_ratio` 已在 manifest 里
  （token 面板要的数据齐了；`tokens_today` 在 `/api/usage`）。
- ⚠ R22 新增的 `candidates_scored` **要不要渲染由 R24 决定**：它是"为什么选了这张方"
  的证据，学生/研究者形态下有价值，患者形态下不该出现（跟 advice 同一条边界，
  判据复用 `_role_gets_advice`，不要新写一个）。
- ⚠ **R24 改了按注册表/层数循环渲染的东西就必须跑 Playwright**（CLAUDE.md 那条：
  M5 的 `nodesByLayer` 漏 key、R18 的五位医家渲染成五列，两次都是 Python 测试全绿）。
