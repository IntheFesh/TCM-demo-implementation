# R21 报告：知识不走检索，走前缀缓存

**基线** `a22398a`（R19 结束）。**这一轮改的是产品形态的地基**：S3 看到的不再是
top-3 条检索结果，而是该医家的**全部**医案 + 本草/方剂表，靠 DeepSeek 的前缀缓存
把成本压下来。旧的四种检索模式一条不删，整体降为对照 arm（`top3` 系）。

一句话判据：**命中缓存的输入 token 便宜 30 倍（$0.044/M vs $1.32/M），
所以"全都给模型看"比"只给三条"更省钱**——前提是前缀逐字节稳定、真能命中。
这一轮做的就是把"逐字节稳定"和"真能命中"变成可验证的东西。

---

## 一、做了什么（逐项 ✅/❌）

### A. 前缀生成器（新文件 `core/context_prefix.py`，546 行）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 1 | 六段前缀：§1 指令与 schema / §2 本草速查表 / §3 方剂速查表 / §4 该医家医案全量 / §5 该医家用过的药材方剂完整条目 / §6 本次变量 | ✅ | `SECTION_*`、`EMIT_ORDER`、`assemble()` |
| 2 | 确定性：固定排序、无时间戳、无随机 id，两次跑 sha256 相同 | ✅ | `prefix_sha256()`；`test_context_prefix.py` 有一条专门钉它 |
| 3 | token 预算 500K/医家，超了按固定顺序裁（方剂表 → 本草表 → §5 条目），**医案永不裁**；裁不裁按最大的那位医家**全局**决定 | ✅ | `BudgetPlan`、`CUT_ORDER`、`budget_plan()` |
| 4 | `python -m core.context_prefix --report` 打四段 token 表；沙盒没有 `cases.json` 时用 `--synthetic 20` 合成语料，并在第一行标明是合成的 | ✅ | `main()`、`synthetic_cases()` |

### B. `full_context` 模式（默认）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 5 | 新模式 `full_context` 成为默认；旧四种合称 `top3` 系保留为对照 arm | ✅ | `retrieval_hybrid.py`：`DEFAULT_MODE`、`TOP3_MODES`、`effective_mode()` |
| 6 | S3 在 full_context 下用 `assemble()` 拼 system prompt；top3 下**原路一个字节没改** | ✅ | `core/chain.py::run_physician` 的两分支 |
| 7 | E3/E4 在 full_context 下可跑（own / swapped=换另一位医家全量语料 / none=不给语料块），**闸门仍 ≥ 0.4，没放宽** | ✅ | `eval/run_eval.py --retriever-mode`；`tests/test_e3e4_full_context.py` 用假后端跑通 |

### C. 缓存可观测

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 8 | 收 `usage.prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` | ✅ | `core/llm.py`：`new_usage_stats` / `record_usage` / `current_usage_stats` |
| 9 | manifest 带 `retriever_mode`、`prefix_tokens_by_section`、`cache_hit_tokens` / `cache_miss_tokens` / `cache_hit_ratio` | ✅ | `core/chain.py::_build_manifest`（7 处调用点全改） |
| 10 | bench 报**逐次**命中率并对闸门 0.9 给判决；沙盒用 `--simulate-cache` 模拟（产物里标 `simulated_cache: true`） | ✅ | `scripts/bench_consult.py`：`CACHE_HIT_GATE`、`FAKE_CACHE_BLOCK_TOKENS` |

### D. 峰谷时段

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 11 | 峰时段（UTC 01–04、06–10 = 北京 09–12、14–18）判定只有**一处实现**，bash 侧不自己算时区、shell out 给 Python；贵的段开跑前黄字提醒，**不拦、不 read** | ✅ | `core/usage.py::is_peak` / `peak_note`；`scripts/run_onsite.sh::warn_if_peak` |

### E. 患者 / 共享额度

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 12 | 额度账本记 token 与峰时段调用数；**额度单位仍是 `llm_calls` 不是钱**（钱变了、额度口径不变） | ✅ | `core/usage.py::record_tokens`、`snapshot()` 的 `tokens_today` / `peak_calls_today` |
| 13 | BYOK key 校验返回 `prefix_warmup_note`：缓存"几小时到几天"会被清，**演示前必须预热** | ✅ | `api/main.py::_prefix_warmup_note`（钱的算式也在这一处） |

### 附带（没在清单里但这一轮必须做完的）

- ✅ `eval/RESULTS.md` 新起一节「full_context 系」，5 行全标 ⏳；**上面那张 top3 系的表一个字没改**，
  并在表头前加了一段说明"1–9 号全是 top3 系的历史行"。
- ✅ `README.md` 新增一节「full_context：知识不走检索，走前缀缓存」（价格表、64-token 存储单位、
  六段结构、为什么 §1 排在 §2/§3 后面、命中率怎么看）。
- ✅ `data/SOURCES.md` 第 64 条，十个编号点。
- ✅ `.env.example` 补 `RETRIEVER_MODE` 一段（默认值、两系不可比、ReAct 关）。

**❌ 没有一项**。要真实 API 才有数的都落在第六节的 ⏳ 清单里，不算 ❌。

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **2783** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **36**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑，不落文件 |
| 凭据核对 | 退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **16** 种全过 | `python -m scripts.screenshot_states` |

带凭据记号的那几个（一行一个，核对器要求凭据说的数在同一行正文里也出现）。
引的是**这一轮的不可变快照** `eval/bench/rounds/R21.json`，不是会被下一轮覆盖的
`sandbox.json`——这一条是 R23 补的机制，理由见 R23 报告第五节和 SOURCES 第 65 条：

- 测试 passed **2783** —— `bench/rounds/R21.json:round.R21.pytest_passed=2783`
- 测试 skipped **10** —— `bench/rounds/R21.json:round.R21.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R21.json:round.R21.pytest_failed=0`
- 全量测试墙钟 **66.2** 秒 —— `bench/rounds/R21.json:round.R21.pytest_wall_s=66.2`
- Playwright 通过 **16** 种 —— `bench/rounds/R21.json:round.R21.playwright_states_passed=16`

`ruff`、`min_length=1` 计数、`--check` 退出码这三个**没有凭据记号也不该有**：
它们是命令的当场输出，不落任何文件。

**`min_length=1` 36 处，跟 R18 之二结束时一样，一处没动。** 这一轮没有新增
pydantic 字段，也没有任何一处从 `Field(min_length=1)` 改成可选或 `min_length=0`。

**新增测试 90 条**（R19 的 2693 → 2783）。其中 86 条在五个新文件里，另 4 条落在既有文件（`test_react` 拆成三条、`test_chain` 新增、`test_docs_numbers` 新增一条并因核对器多一份文档而多一个参数）：

| 文件 | 条数 | 要求 | 盯的是什么 |
|---|---|---|---|
| `tests/test_context_prefix.py` | **29** | ≥ 18 | 六段顺序、确定性 sha256、模板无损切分、裁剪顺序、token 尺惰性加载 |
| `tests/test_retriever_full_context.py` | **13** | ≥ 8 | 全量命中按 `case_id` 排序、固定分 1.0、k/min_score 非默认时告警一次 |
| `tests/test_cache_observability.py` | **15** | ≥ 6 | hit/miss 累计、比值算一处、没报过就是 None（不是 0） |
| `tests/test_onsite_peak_hours.py` | **24** | ≥ 4 | 峰谷边界半开区间、bash 不自己算时区、提醒不拦执行 |
| `tests/test_e3e4_full_context.py` | **5** | ≥ 3 | own/swapped/none 三种语料块在假后端下跑通、闸门仍 0.4 |

---

## 三、每项改动的验证方式与结果

**1–4 前缀生成器。** `python -m core.context_prefix --report --synthetic 20`
真跑出四段表（下面第四节是它的输出）。确定性不是"看起来一样"：
`prefix_sha256()` 跑两次比字符串。**模板无损切分**那条最值得说——
`split_s3_template()` 在字面标记 `证素分析：\n$elements_summary` 处切开，
测试断言 `head + tail == template` **逐字节**相等，标记没了直接 `ValueError`。
理由：如果悄悄把整个模板当成 head，本次主诉就会被拼进"稳定"前缀，
于是缓存永远不命中，而日志里什么都看不出来。

**5–6 模式与链路。** `test_retriever_full_context.py` 钉住三件事：全量命中按
`case_id` 排序（顺序漂一次，前缀就全 miss）、展示分固定 `1.0`（不是 0.0——那会被
读成"不相关"；也不是 None——那在前端变成 null）、传了非默认的 `k`/`min_score`
时告警一次而不是静默忽略。top3 路径没改这件事由既有 2600 多条测试保证。

**7 E3/E4。** `tests/test_e3e4_full_context.py` 用假后端跑完 own/swapped/none 三条路：
swapped = 换成另一位医家的**全量**语料，none = 整块不给。**这两个变体一行新代码都不需要**
——`assemble(case_block=…)` 传什么就是什么，跟 top3 系用的是同一个入口。

**8–10 缓存可观测。** 沙盒没有真实 API，所以 bench 加了 `--simulate-cache`：
按 64-token 块对齐算最长公共前缀。实跑三次：

```
前缀缓存命中率（逐次）：0.011、0.996、0.996
  最后一次 0.996，判据 ≥ 0.9 → 达标
```

第一次 0.011 是**对的**（全 miss）。闸门取 0.9 不取 1.0：§6 那段永远不命中，占约 1%。

**11 峰谷。** `test_onsite_peak_hours.py` 里有一条用 AST 查 `run_onsite.sh`：
shell 里不许出现自己算时区的代码，必须 shell out 给 `core.usage`。
另有一条钉"提醒不拦执行"——`warn_if_peak` 里不许有 `read`。

**12–13 额度与预热。** `snapshot()` 的 `cache_hit_ratio` 在什么都没报过时是
**None 而不是 0**：0 会被读成"一次都没命中"，而真相是"还不知道"。

---

## 四、性能 / 规模数字对照表

### 4.1 前缀规模（**合成语料**，不是真机数）

`python -m core.context_prefix --report --synthetic 20`，token 尺
`conservative-estimate`（沙盒没装 tiktoken，退化成声明过的保守上界）：

| 段 | li_ke | wang_yunqi | wu_jutong | ye_tianshi | zhang_xichun | 谁共享 |
|---|---|---|---|---|---|---|
| §2 本草速查表 | 77 | 77 | 77 | 77 | 77 | 所有医家相同 |
| §3 方剂速查表 | 65 | 65 | 65 | 65 | 65 | 所有医家相同 |
| §1 指令与 schema | 2,943 | 2,943 | 2,943 | 2,943 | 2,943 | 所有医家相同 |
| §4 医案全量 | 2,725 | 2,825 | 2,805 | 2,825 | 2,865 | 该医家 |
| §5 药材/方剂条目 | 58 | 58 | 58 | 58 | 58 | 该医家 |
| **稳定前缀合计** | **5,868** | 5,968 | 5,948 | 5,968 | **6,008** | —— |

对照基准有两个，都不是"这个数好不好看"：

1. **预算 500,000 / 医家**——合成规模下一段未裁。`--budget 6000` 能看到裁剪真的发生：
   `**裁掉的段**：方剂速查表（裁剪顺序固定 …，医案永不裁）`，合计 5,868 → 5,803。
2. **§2+§3 三位医家逐字节相同**（77/65 五列全等），所以跨医家共享那一段缓存。
   §1 排在它们后面就是为了这个：`s3_syndrome.yaml` 第一行含 `$name`，
   §1 放最前面会让前缀在第 10 字节处分叉，共享段一个字节也命中不了。

⚠ **合成语料的绝对值不是真机数**（每位医家 20 条造出来的医案）。真机一位医家
≈180K token，量级差 30 倍——真机跑 `--report` 不带 `--synthetic`，见第六节。

### 4.2 沙盒性能（R19 → R21，同一个脚本）

| # | 指标 | R19 | R21 | 该怎么读 |
|---|---|---|---|---|
| P1 | 冷进程 `import api.main` | 0.517 s | **0.431** s | 同一份代码路径 + 这一轮新增一个 import。**不能当成"变快了"**，见下 |
| P2/P3 | `/health` p50 五位 / 三位 | 12.237 / 12.554 ms | **8.481 / 8.603** ms | 四次测下来方向相反（两次一头、两次另一头）→ **这个开销量不出来** |
| P4 | 全量测试 条数 / 墙钟 | 2693 / 89.9 s | **2783 / 66.2** s | 条数 +90 而墙钟 −23.7 s → 中间夹着机器负载，两个数不许相减 |
| P5 | Playwright | 16 种 / 38.4 s | **16 种 / 37.1** s | 这一轮没动前端，它测的是"没有回归" |

**P1 这一行是这一轮最该被谨慎读的数。** 0.517 → 0.431 出现在一个**新增了 import
的版本**上（`core.chain` 多 import 了 `core.context_prefix`），按理该更慢。
所以正确的结论是"这台机器这两次的负载不同"，不是"这一轮让启动变快了"。
同理 P4 的墙钟。这两处跟 P2/P3 是同一个教训的第三、第四次复发。

### 4.3 钱（按官方价算，**不是实测**）

| 项 | 单价 | 单医家全量 ≈180K token |
|---|---|---|
| 输入 · 缓存未命中 | $1.32 / M | 首次 ≈ $0.24 |
| 输入 · **缓存命中** | $0.044 / M | 之后每次 ≈ $0.008 |
| 输出 | $3.96 / M | —— |

三位医家一次问诊 ≈ **¥0.17**。对照基准是同一次问诊**全 miss** 时的钱（≈¥5.1）。
算式写在 `api/main.py::_prefix_warmup_note` 一处，它自己会说明这是估算。

---

## 五、改了哪些测试断言（每条都是**有意的契约变更**）

| # | 测试 | 改法 | 为什么这是契约变更而不是"把测试改绿" |
|---|---|---|---|
| 1 | `test_allowed_modes_includes_graph` → `..._and_full_context` | 断言新的模式集合 + `TOP3_MODES` + `DEFAULT_MODE` | 合法模式集合本身变了（多一种），默认值也变了。旧断言描述的是旧契约 |
| 2 | `test_react_enabled_defaults_off` | 拆成三条：默认关 / `USE_REACT=1` 且 top3 → 开 / `USE_REACT=1` 且 full_context → **仍然关** | 第三条是这一轮**新加的约束**：ReAct 的工具在检索语料，而语料已在上下文里 |
| 3 | `test_use_react_none_reads_environment` | 加 `monkeypatch.setenv("RETRIEVER_MODE", "hybrid")` | 这条测的是"读不读环境变量"，不是"哪个模式"。默认模式变了之后不固定住模式，它测的就是另一件事了 |
| 4 | `test_all_four_docs_are_in_the_checker` → `test_the_four_fixed_docs_and_every_round_report_are_in_the_checker` | `==` 改 `<=`，并要求 `docs/reports/R*_report.md` 全都在核对器里（glob 进来，不是一轮一轮加名字） | 每轮一份报告是新的交付形态。靠人记着加名字，忘了的那一轮读起来跟被核过的一样 |
| 5 | `test_the_report_pins_this_rounds_five_numbers` | 查的对象从 `docs/R11-R19_report.md` 改成**当前轮**报告 | 见下面那段 ⚠ |
| 6 | `test_the_report_records_the_nine_round_test_counts_monotonically` | 末行判据从 `== 当前实测` 改成 `<= 当前实测`，并新加一条 `test_the_current_round_report_states_the_measured_test_count` 接手原来的责任 | 同上 |
| 7 | `test_tokenizer_name_is_reported_and_the_estimate_is_conservative` | 模块常量 `TOKENIZER_NAME` 改成函数 `tokenizer_name()` | 常量要在 import 时就把 BPE 表拉下来，见第七节 |

⚠ **第 5、6 条背后是这一轮抓到的一个凭据挂错对象的问题。**
`eval/bench/sandbox.json` 是"**当前这一轮的测量**"，不是逐轮追加的日志。R21 重跑
`bench_sandbox --all` 之后它整份被覆盖，于是 `docs/R11-R19_report.md` 里挂在它上面的
R19 凭据当场 `--check` 报了 6 处不一致。**这不是文档写错了，是凭据挂错了对象**：
一个会被下一轮覆盖的文件，不能给一个历史轮次的数当凭据。改法是历史值指到 git 的
那个 commit（`git show a22398a:eval/bench/sandbox.json`，跟 `archive/2026-09-12/`
那批 📦 归档凭据同一个道理），"当前轮"这个角色转给 `docs/reports/R<N>_report.md`。

---

## 六、无法完成项（全是 ⏳ 上机，不是 ❌）

| # | 要跑的 | 命令 | 判据 |
|---|---|---|---|
| 1 | 真实前缀规模 | `python -m core.context_prefix --report`（不带 `--synthetic`） | 三位医家各自 ≤ 500K；**没有段被裁** |
| 2 | 真实命中率 | `python -m scripts.bench_consult --backend real --repeat 2` | 第二次 `cache_hit_ratio ≥ 0.9` |
| 3 | 一次问诊的钱 | 看 `/api/usage` 的 `tokens_today`，或 bench 产物的 hit/miss | ≤ ¥0.5（第二次起） |
| 4 | E3/E4 在 full_context 下 | `RETRIEVER_MODE=full_context python -m eval.run_eval --e3 --e4` | 两个 change_rate 各自 ≥ 0.4（**跟 top3 系同一个闸门**） |
| 5 | tiktoken 真尺 | `pip install tiktoken` 后重跑第 1 项 | `--report` 第一行显示 `tiktoken:cl100k_base`；跟保守估算的差值要记下来 |

沙盒里做不了的原因只有一个：**这五项都要真实 API key 或真实 `cases.json`**。
每一条都给了命令和判据，没有"环境不支持"这种交代。

---

## 七、发现但未动 / 发现并当场修了

**1. `ContextVar` 不跨 `threading.Thread`（这一轮最贵的一个发现，已修）。**
症状：`--simulate-cache` 明明在算命中率，bench 里的 `cache_hit_ratio` 永远是 None，
**任何地方都不报错**。根因：`_complete` 跑在 `_complete_within_deadline` 起的
工作线程里，而**新线程拿到的是一个空 Context**，`record_usage` 看到的
`_usage_stats` 是 `None`，于是"没有人报过 usage"——跟"报了但都是 0"在日志里长得一样。
修法是 `copy_context().run()`。

为什么这个 bug 之前从没露头：重试统计 `_record_retry` 是在**调用方线程**里跑的，
所以同一套 ContextVar 机制一直工作正常。**推广出去的纪律**：以后任何新加的
ContextVar 遥测，都要先问一句"它会不会在 `threading.Thread` 的另一侧被写"。

**2. token 尺在模块顶层加载（已修，第五节第 7 条）。**
`_ENCODER = _load_tiktoken()` 写在模块顶层。`tiktoken.get_encoding()` **首次调用会去
网络拉 BPE 表**，而这个模块被 `core.chain` 导入、chain 被 `api.main` 导入——
等于 `import api.main` 时联网。沙盒里没装 tiktoken 所以一直看不出来；装了的机器上
P1 那个冷启动数会变成一次下载耗时，断网的机器连 import 都要等它超时。
这正是 CLAUDE.md 那条"加载模型/大文件的对象一律惰性初始化"要防的形状。
改成哨兵 + `tokenizer_name()` 函数，新加一条测试钉住"新装一份模块之后哨兵仍是未试过"。

**3. 留着没动**：`eval/bench/consult_fake_*.json` 每跑一次 bench 就多一个文件，
目前没有清理策略。不动的理由是它们都在 `eval/bench/` 下且体积很小，
真要治应该跟 R25 的产物归档一起做，这一轮单独加个清理脚本会变成第二套归档机制。

---

## 八、13 条自查

口径沿用 R11–R19 那一套，逐条给凭据：

| # | 自查项 | 结果 |
|---|---|---|
| 1 | prompt 模板只用 `string.Template`，没有 `str.format()` | ✅ `grep -n "\.format(" core/context_prefix.py` 无输出；§1 直接取自 `prompts/v1`，**yaml 一个字没改**（有测试钉 `head + tail == template` 逐字节） |
| 2 | LLM 输出全部用 pydantic 承接，不裸用 dict | ✅ 本轮没有新增 LLM 输出结构；`usage` 是**输入侧计量**不是模型输出，按 dict 累加是对的 |
| 3 | `Field(min_length=1)` 没有放松 | ✅ **36** 处，一处没动；没有任何字段改成可选或 `min_length=0` |
| 4 | 新 `.jsonl` 一律放 `data/standard/` | ✅ 本轮没有新增数据文件；读药理层走既有的 `pharmacology_read_path()` |
| 5 | 加载模型/大文件的对象惰性初始化 | ✅ **本轮抓到一处违反并修了**（第七节第 2 条）；`load_cases` 也在函数里 import |
| 6 | S1 全局只跑一次，两位医家共用 | ✅ 没动 S1；`assemble()` 收的是**已经算好的** s1/s2 结果 |
| 7 | 任何数字旁边必须有对照基准 | ✅ 第四节每个数都带对照；P1/P4 明确写了"不许当成变快了" |
| 8 | 同一概念只有一处实现 | ✅ 四处复用：医案格式化走 `_format_case_block`、可用医案判据走抽出来的 `load_cases`、峰谷判定只在 `core/usage.py`、`RETRIEVER_MODE` 这个字面量只有 `retrieval_hybrid` 一处（`RETRIEVER_MODE_ENV`） |
| 9 | 标识符在边界过 `resolve_*_id` | ✅ `physician_cases()` / `full_context_hits()` 收的都是 id；展示层名字不参与过滤 |
| 10 | 安全否决在 S2 之前 | ✅ 没动安全层；full_context 只换 S3 的 system prompt，拦截在它之前 |
| 11 | 追问的回答先过 `check_safety` | ✅ 没动追问链路 |
| 12 | 评测代码放 `eval/`，`tests/` 不需要网络和 key | ✅ `test_e3e4_full_context.py` 用假后端；`--simulate-cache` 让命中率在沙盒里可测 |
| 13 | 改了按注册表/图层循环渲染的前端 → 跑 Playwright | ✅ 本轮没动前端，仍然跑了一次：**16 种全过**（它这一轮验的是"没有回归"）。R24 要渲染 token 面板，那一轮必须再跑 |

---

## 九、下一轮（R23）前置条件

R23 是 `core/formula_check.py`（五条规则 + `Advice` schema + `score_formula` +
`/api/prescription/validate` 的 `advice[]`）。它的前置条件**都已就位**：

- 十八反十九畏表在 `core/safety_output.py`，剂量上限在 `DOSE_LIMITS`
  ——两者都**复用**，R23 不许另写一套字面匹配（CLAUDE.md「同一概念只能有一处实现」）。
- `score_formula` 会被 R22 的 best-of-N 打分器调用，所以**顺序不可调**：R23 先做。
- 本轮没有任何东西阻塞 R23：full_context 与方剂校验在两条不相交的路径上。

