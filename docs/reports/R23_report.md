# R23 报告：方剂建议层（跟安全层分开的第二层）

**基线** `6cec6bb`（R21 结束）。**顺序是定死的**：R23 必须排在 R22 前面，
因为 R22 的 best-of-N 要用这一轮的 `score_formula` 在 N 张候选方之间排序。

这一轮做的事一句话：安全层回答「这方能不能发出去」，**新增的建议层回答
「这方拟得好不好」**。两层分开，而分开的代价是建议层**不许重新实现任何判据**
——五条规则里三条直接调安全层的函数。

---

## 一、做了什么（逐项 ✅/❌）

| # | 项 | 状态 | 落在哪 |
|---|---|---|---|
| 1 | `core/formula_check.py::check_formula`，五条规则 | ✅ | 新文件，223 行 |
| 1a | 十八反十九畏（**复用** `check_incompatible`） | ✅ | `check_incompatible_advice` |
| 1b | 超药典常用上限（**复用** `check_dose_limits` / `DOSE_LIMITS`） | ✅ | `check_dose_advice` |
| 1c | 证型寒热方向相悖（**复用** `check_thermal_consistency`） | ✅ | `check_thermal_advice` |
| 1d | 缺引经药（药理层「归经」+ **复用** `core.elements.LOCATIONS`） | ✅ | `check_channel_advice` |
| 1e | 性味功效重复 ≥ 60% | ✅ | `check_duplicate_advice` |
| 2 | `Advice{kind, herbs, reason, source_span, severity}` | ✅ | `core/schemas.py`（挨着 `FormulaSafety`，同一条依赖边界） |
| 3 | `score_formula` = `1.0 − Σ权重`，权重只在 `ADVICE_WEIGHTS` 一处 | ✅ | 下限 0.0；`FormulaCheck.score` 是 property，问的是同一个函数 |
| 4 | `POST /api/prescription/validate` 带 `advice[]` | ✅ | 另带 `advice_skipped` / `formula_score`；安全层那六个键一个没动 |
| 5 | 每位医家 `results[i].advice` | ✅ | `core/chain.py::run_physician` 算，`_serialize_result` 序列化 |
| 6 | 患者角色拿不到建议层（服务端裁剪） | ✅ | `api/main.py::_role_gets_advice`，**两处共用这一个判据** |
| 7 | `tests/test_formula_check.py` ≥ 18 条 | ✅ | **30** 条 |
| 8 | `tests/test_api_prescription_advice.py` ≥ 6 条 | ✅ | **11** 条 |

### 附带（不在清单里但这一轮必须做完的）

- ✅ **修掉 R21 留下的一个凭据机制缺陷**（见第五、七节）：新增
  `eval/bench/rounds/R<N>.json` 每轮不可变快照 + `bench_sandbox --round`，
  并把 R21 报告的五个凭据记号改指它自己那份快照。
- ✅ `README.md` 新增一节「方剂建议层（R23）」：两层对照表、五条规则的权重与
  判据来源、缺数据三分法、患者边界。
- ✅ `data/SOURCES.md` 第 65 条，九个编号点。

**❌ 没有一项。**

---

## 二、五个数

| 数 | 值 | 怎么核 |
|---|---|---|
| 全量测试 | **2829** passed / **10** skipped / **0** failed | `python -m scripts.bench_sandbox --all --round R23` |
| ruff | 干净（`All checks passed!`） | `ruff check .` |
| 防幻觉字段数 | **37**（口径：`grep -c "= Field(min_length=1" core/schemas.py`） | 当场跑，不落文件 |
| 凭据核对 | 退出码 **0** | `python -m scripts.collect_results --check` |
| Playwright | **16** 种全过 | `python -m scripts.screenshot_states` |

带凭据记号的那几个（一行一个）。引的是**这一轮的不可变快照**
`eval/bench/rounds/R23.json`，不是会被下一轮覆盖的 `sandbox.json`：

- 测试 passed **2829** —— `bench/rounds/R23.json:round.R23.pytest_passed=2829`
- 测试 skipped **10** —— `bench/rounds/R23.json:round.R23.pytest_skipped=10`
- 测试 failed **0** —— `bench/rounds/R23.json:round.R23.pytest_failed=0`
- 全量测试墙钟 **89.8** 秒 —— `bench/rounds/R23.json:round.R23.pytest_wall_s=89.8`
- Playwright 通过 **16** 种 —— `bench/rounds/R23.json:round.R23.playwright_states_passed=16`

**`min_length=1` 36 → 37，纯新增一处**：`Advice.reason`。一条说不出理由的建议
在界面上是个空条目，人只会以为是 bug。没有任何一处从 `Field(min_length=1)`
改成可选或 `min_length=0`。

`Advice.source_span` 是 `str | None`，**这不是放松**：药理层那四个 schema 的
`source_span` 必须非空，因为那些值是**模型**从原文抽的、必须能回到原文核对；
`Advice` 是**规则**算出来的，十八反和超剂量两条能给出表里的出处，寒热/缺引经/
重复三条没有原文出处——那时留 None 是如实，编一句「根据中医理论」才是假的。

**新增测试 46 条**（R21 的 2783 → 本轮 2829）：

| 文件 | 条数 | 要求 | 盯的是什么 |
|---|---|---|---|
| `tests/test_formula_check.py` | **30** | ≥ 18 | 五条规则各自触发/静默、缺数据三分法、打分与排序确定性、**三条"换掉安全层函数看建议跟不跟着变"** |
| `tests/test_api_prescription_advice.py` | **11** | ≥ 6 | 三个新键、安全层旧键一个不少、患者边界（搜整个响应体）、角色判据只有一处 |
| `tests/test_collect_results.py` | +4 | —— | 每轮快照的 glob 注册、快照 ≠ 当前测量、轮次名形状在入口就拒错 |

---

## 三、每项改动的验证方式与结果

**1. 五条规则。** 每条都有一条"触发"和（可触发的那几条）一条"该静默时静默"。
最值得说的是**三条"换掉安全层函数"的测试**：

```python
monkeypatch.setattr(fc, "check_incompatible", lambda herbs: [("甲药", "乙药")])
# → 建议里必须出现 ('甲药','乙药')，否则说明建议层自己抄了一份十八反表
```

这种测法抓的是「复制了一份判据过来」这种退化。**只看输出对不对抓不到它**：
复制品的输出一开始是对的，要等到有人改了安全层那张表、而建议层没跟着改，
才会以"同一个方在两处得到相反结论"的形式爆出来——这个项目在同一堵墙上
已经撞过三次（SOURCES.md 第 31 条），所以这一轮直接把判据钉在测试里。

**2. 缺数据的三分法。** 沙盒里没有 `data/standard/materia_medica.jsonl`，所以
后两条规则跑不了。**不是静默少两条建议**：

```
advice_skipped = [
  {"rule": "missing_channel_guide", "available": false,
   "path": ".../data/standard/materia_medica.jsonl",
   "reason": "药理层本草表还没建出来（AutoDL 上跑 run_pharmacology_extraction 才有）"},
  ...
]
```

三种情况在测试里分别钉住：①表没建出来（`available: false` + 路径 + 上机命令）；
②表在、这味药不在表里（`available: true` + `n_checked: 1, n_herbs: 2`）；
③表在、查过了、确实没问题（不出现）。**①和③在界面上长得一模一样而含义相反**，
这就是要 `skipped` 这个字段的全部理由。

**3. 患者边界。** 判据不是逐键检查，是**把整个响应体序列化成字符串去搜**
「甘遂」「十八反」——这样"药名从某个没想到的字段漏出去"也会被抓到，
而逐键检查只能查到我们记得去查的那几个键（形状抄的是 R15 患者模式那条）。

**4. 角色判据只有一处。** `test_the_role_predicate_is_the_single_place_that_decides`
把 `_role_gets_advice` 换成恒 False，然后断言 `/api/prescription/validate`
的三个键都消失了——如果那个端点自己写了一遍 `if role == "patient"`，这条会失败。

---

## 四、规模与性能数字对照表

### 4.1 建议层本身的规模

| 项 | 数 | 对照 |
|---|---|---|
| 规则条数 | 5 | 安全层也是 5 条（十八反/剂量/煎法/毒性/寒热）。**两层各 5 条里有 3 条同源**——同源的那 3 条是调用，不是复制 |
| 复用安全层函数 | 3 / 5 | 剩下 2 条读药理层，沙盒里跑不了（⏳） |
| 新增 pydantic 字段带 `min_length=1` | 1 | 36 → 37 |
| 权重定义处 | **1**（`ADVICE_WEIGHTS`） | 对照是"如果散在代码里"：改一处排序权重要改 5 个地方 |
| `core/formula_check.py` | 223 行 | 其中模块文档字符串 30 行（写清"为什么不合并进安全层"） |

### 4.2 沙盒性能（R19 → R21 → R23，同一个脚本）

| # | 指标 | R19 | R21 | R23 | 该怎么读 |
|---|---|---|---|---|---|
| P1 | 冷进程 `import api.main` | 0.517 s | 0.431 s | **0.542** s | 三轮先降后升，**代码一直在变多** |
| P2/P3 | `/health` p50 五位 / 三位 | 12.237 / 12.554 | 8.481 / 8.603 | **13.14 / 12.49** | 五次测量方向来回翻 → 这个开销量不出来 |
| P4 | 全量测试 条数 / 墙钟 | 2693 / 89.9 s | 2783 / 66.2 s | **2829 / 89.8** s | 条数一路只增，墙钟先降 24s 又涨回去 |
| P5 | Playwright | 16 / 38.4 s | 16 / 37.1 s | **16 / 37.7** s | 这两轮都没动前端，测的是"没有回归" |

**P4 这一行把 R21 报告里那句话直接证实了。** R21 写的是「墙钟 89.9 → 66.2 s
**不是变快了**，条数多了 90 条而墙钟少了 23.7 s，说明这两个数之间夹着机器负载」。
R23 这一轮条数又涨了、墙钟也涨回 89 s 量级——**同一份代码家族，墙钟在三轮之间
走了一个来回，而条数单调递增**。如果当初把 66.2 s 写成"这一轮让测试变快了"，
这一轮就得写一篇"为什么又变慢了"的假解释。

---

## 五、改了哪些测试断言（每条都是**有意的契约变更**）

| # | 测试 | 改法 | 为什么这是契约变更 |
|---|---|---|---|
| 1 | `test_validate_clean_formula_has_no_problems` | 原来断言**整个响应体**逐键相等（六个安全键），改成"安全那六个键一个没变 + 建议层三个键存在" | R23 新增三个键。直接把新键塞进期望字典会让这条测试退化成"有什么就断言什么"的复印件，而它本来的作用是钉住**安全层**的形状 |
| 2 | `test_the_report_pins_this_rounds_five_numbers` | 凭据记号的形状从 `bench/sandbox.json:bench.*` 改成 `bench/rounds/R<N>.json:round.R<N>.*` | 见下面那段 ⚠ |
| 3 | `test_the_current_round_report_states_the_measured_test_count` → `test_the_latest_round_snapshot_matches_the_current_measurement` | 判据从"报告正文 == sandbox.json"改成"最新快照 == sandbox.json"（文件对文件） | 原写法会绕成死循环：报告要先写出来才能被量，而量出来的条数又取决于报告存不存在（它自己也是被测文件之一）。现在两处各管一段——`--check` 核"报告正文 == 快照"，这条核"快照 == 这一轮实测" |

⚠ **第 2、3 条修的是 R21 自己留下的缺陷。** R21 发现
「`sandbox.json` 会被下一轮覆盖，所以不能给历史轮次的数当凭据」，当时的改法是
**把历史值指到 git**（`git show a22398a:...`）。这个改法有个 R21 没看出来的问题：
**它要求每轮都手改上一轮的报告**，而手改的那一步一定有人会忘——忘了的那一轮，
`--check` 会当场报错，而最省事的"修法"是把凭据记号删掉，于是那一轮的数变成
没人核的数。R23 这一轮一开始就撞上了：重跑 `bench_sandbox` 之后，R21 报告的
五个凭据记号全部报错。

**正确的机制是每轮一份不可变快照**：`bench_sandbox --all --round R23` 同一次
运行写两份——`sandbox.json`（当前，RESULTS.md 的性能表引它）和
`bench/rounds/R23.json`（这一轮的，此后不再动，R23 报告引它）。凭据键由
`_register_round_evidence()` **glob 注册**（`round.R23.pytest_passed`），
不是一轮一轮往 `EVIDENCE` 里加九行——忘了加的那一轮同样会退化成"没人核的数"。
轮次名的形状在入口就校验（`--round r23` 直接报错退出）：写错一个字母的后果是
那份快照谁都不核，而它看起来跟被核过的一样。

---

## 六、无法完成项（⏳ 上机，不是 ❌）

| # | 要跑的 | 命令 | 判据 |
|---|---|---|---|
| 1 | 后两条规则在真实本草表上跑 | 先 `python -m scripts.run_pharmacology_extraction`，再 `POST /api/prescription/validate` | `advice_skipped` 里那两条 `available: false` 消失；缺引经/重复两类能真的产出建议 |
| 2 | 重复判定的假阳率 | 拿真实本草表跑一遍已有的经典方（如四君子汤） | 经典方**不该**被判出一堆"重复用药"。如果被判出来了，说明 0.6 这个闸门或"只切不归一"的切分器要调——**调之前先看真实数据，不要在沙盒里凭合成例子调** |
| 3 | `score_formula` 在 N 张真实候选方上的分布 | R22 的 best-of-N 跑起来之后看 `candidates_scored` | 分数要能把 N 张方**分开**。如果 N 张全是 1.0，这把尺在真实分布上就没有分辨力，那时要加规则而不是调权重 |

沙盒里做不了的原因只有一个：**这三项都要真实的药理层数据或真实 LLM 采样**。

---

## 七、发现但未动 / 发现并当场修了

**1. R21 的凭据机制缺陷（已修，见第五节）。** 值得单独记一句的是它暴露的方式：
不是有人想起来去检查，而是 R23 例行重跑 `bench_sandbox` 之后 `--check` 当场
报了 5 处不一致。**一个会被下一轮覆盖的文件，不能给历史轮次的数当凭据**——
R21 报告里已经写对了这句话，但给出的改法（指到 git）把执行负担放在了人身上。
这一轮把它换成机制。

**2. 0 味药不算「缺引经药」（写测试时才发现，已修）。** 可编辑处方表从空表开始，
医生删到 0 味时前端仍会调一次校验。那时证型里有「脾」，缺引经规则会报
「方中没有一味药归脾经」——字面为真但毫无用处。改成空方不判，并在 `skipped` 里
说「方里还没有药，无从判引经」。这跟当初决定「0 味药返回全空的 `FormulaSafety`
才是诚实结果、不该 422」是同一条判断的延伸。

**3. 留着没动：药性术语没有同义表。** `_terms()` 只按顿号切分、**不做同义归一**
——写法不同的同义功效（「健脾」vs「补脾」）这条规则目前抓不到，代码注释里
如实写了这一条。不动的理由：药性术语的同义表这个项目还没有，现编一张会让
「重复用药」这条建议建立在一个没人核过的表上。要做应该跟真实本草表一起做
（第六节第 2 项），那时能看到哪些写法真的成对出现。

**4. 留着没动：`/api/prescription/export` 不带建议层。** 那条接口的 422 detail
只列拦截级问题，理由是它回答的是"能不能导出"——建议层是软的，不该影响导出闸门。
如果以后要在导出前给医生看一眼建议，那是前端多调一次 `/validate` 的事，
不是让导出接口去算一份。

---

## 八、13 条自查

| # | 自查项 | 结果 |
|---|---|---|
| 1 | prompt 模板只用 `string.Template`，没有 `str.format()` | ✅ 本轮没动任何 prompt 模板（建议层不调 LLM） |
| 2 | LLM 输出全部用 pydantic 承接 | ✅ `Advice` 是 pydantic，尽管它不是 LLM 输出——规则输出也不裸用 dict（跟 `DoseViolation` 同一条先例） |
| 3 | `Field(min_length=1)` 没有放松 | ✅ 36 → **37**，纯新增 `Advice.reason`；`source_span` 是新 schema 的新字段，不是放松既有字段 |
| 4 | 新 `.jsonl` 一律放 `data/standard/` | ✅ 本轮没有新数据文件；读本草表走既有的 `pharmacology_read_path()` |
| 5 | 加载模型/大文件的对象惰性初始化 | ✅ `materia_index()` 只在 `check_formula` 里调，且文件不在时返回 None 不抛 |
| 6 | S1 全局只跑一次 | ✅ 没动 S1/S2 |
| 7 | 任何数字旁边必须有对照基准 | ✅ 第四节每个数都带对照；P4 那一行是这一轮最该读的对照 |
| 8 | 同一概念只有一处实现 | ✅ 三条规则复用安全层函数（有 monkeypatch 测试钉）、脏腑表复用 `LOCATIONS`、角色判据只有 `_role_gets_advice`、权重只有 `ADVICE_WEIGHTS`、序列化只有 `advice_dicts` |
| 8b | 走「例外条款」的那一处写清了区别 | ✅ `core/formula_check.py` 模块文档字符串第一节就是"两层回答的不是同一个问题"及不合并的代价 |
| 9 | 标识符在边界过 `resolve_*_id` | ✅ 建议层不收 physician/syndrome id，收的是药名与证型文本；药名查表按"原名 → `normalize_herb`"，跟 `check_dose_limits` 同一顺序 |
| 10 | 安全否决在 S2 之前 | ✅ 没动安全层；建议层在 S3 之后，不参与拦截 |
| 11 | 追问的回答先过 `check_safety` | ✅ 没动追问链路 |
| 12 | 评测代码放 `eval/`，`tests/` 不需要网络和 key | ✅ 两个新测试文件都不调 LLM、不需要数据文件（合成本草表写在测试里） |
| 13 | 改了按注册表/图层循环渲染的前端 → 跑 Playwright | ✅ 本轮没动前端，仍跑了一次：**16 种全过**。⚠ **R24 要把 advice 渲染出来，那一轮必须再跑** |

---

## 九、下一轮（R22）前置条件

R22 是 best-of-N（`S3_REASONING_EFFORT`、`S3_BEST_OF_N=3`、按 `score_formula`
打分、`candidates_scored`、`CALLS_PER_CONSULT = 2 + n_phys × N`）。前置条件：

- ✅ `score_formula` 已就位，权重在一处，`FormulaCheck.score` 是 property。
- ✅ 采样并发要受 `LLM_MAX_INFLIGHT` 约束——那个闸门 R12 就有了，R22 只要
  不绕过它。
- ⚠ **安全层仍然要在 best-of-N 之后**：N 张方里挑出一张之后再走拦截判据，
  不是先拦截再挑（先拦截会让"被拦掉的那张恰好是分最高的"这件事静默消失）。
- ⚠ **`score_formula` 在真实分布上有没有分辨力是 R22 才能看到的**（第六节第 3 项）。
  如果 N 张全是 1.0，正确的反应是加规则，不是调权重去凑出差异。
